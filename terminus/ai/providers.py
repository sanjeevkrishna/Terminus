"""AI provider abstraction and redaction.

Providers are described by :data:`PROVIDER_SCHEMA` so the settings UI can be
generated from the backend rather than hardcoded — adding a provider means one
schema entry plus one handler class.

This module is the **provider layer only**. It knows how to talk to a model,
normalise its event stream, and mask secrets. It holds no conversation state,
builds no prompts, and knows nothing about sessions or commands — that is
:mod:`terminus.ai.agent`.

Every payload passes through :func:`redact` inside :meth:`BaseProvider.chat`,
so masking cannot be bypassed by a caller that forgets to apply it.

File path: terminus/ai/providers.py
"""

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


class AIError(Exception):
    """Raised when a provider is misconfigured or a request fails."""


# ---------------------------------------------------------------------------
# Normalised streaming events
# ---------------------------------------------------------------------------
@dataclass
class TextChunk:
    """A fragment of assistant prose, for immediate display."""

    text: str


@dataclass
class ToolCall:
    """A completed tool invocation request.

    ``arguments`` is ``None`` when the model produced malformed JSON; the
    caller should feed ``error`` back as the tool result rather than failing
    the turn, so the model gets a chance to correct itself.
    """

    id: str
    name: str
    arguments: dict = None
    raw: str = ""
    error: str = ""


@dataclass
class Done:
    """End of one assistant turn."""

    reason: str = "stop"
    tool_calls: list = field(default_factory=list)


@dataclass(frozen=True)
class Capabilities:
    supports_streaming: bool = True
    supports_tools: bool = False
    supports_tools_while_streaming: bool = False

    def as_dict(self):
        return {
            "supports_streaming": self.supports_streaming,
            "supports_tools": self.supports_tools,
            "supports_tools_while_streaming": self.supports_tools_while_streaming,
        }


def _truthy(value, default=False):
    """Interpret a stored config string as a boolean."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


# ---------------------------------------------------------------------------
# Message helpers (OpenAI-shaped; Azure needs no translation)
# ---------------------------------------------------------------------------
def system_message(text):
    return {"role": "system", "content": text}


def user_message(text):
    return {"role": "user", "content": text}


def assistant_message(text="", tool_calls=None):
    """Build the assistant turn to replay before tool results.

    The provider that emitted ``tool_calls`` must see them echoed back, and
    every id must be answered by exactly one tool message — a mismatch is a
    hard 400 from Azure, not a soft error.
    """
    message = {"role": "assistant", "content": text or ""}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.raw or json.dumps(call.arguments or {}),
                },
            }
            for call in tool_calls
        ]
    return message


def tool_message(tool_call_id, content):
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


# ---------------------------------------------------------------------------
# Ollama message translation
# ---------------------------------------------------------------------------
# Ollama's chat API is OpenAI-shaped but differs in two ways that matter:
#   * tool-call arguments are a JSON *object*, not a JSON string
#   * tool results are matched by name, not by an opaque tool_call_id
# The conversation is stored in OpenAI shape (see assistant_message), so it is
# translated at the boundary rather than forking the agent's history model.
def _to_ollama_messages(messages):
    """Translate OpenAI-shaped history into Ollama's chat format."""
    out = []
    call_names = {}  # tool_call_id -> function name

    for message in messages or []:
        role = message.get("role")

        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                name = function.get("name") or ""
                raw = function.get("arguments")
                if isinstance(raw, str):
                    try:
                        arguments = json.loads(raw or "{}")
                    except ValueError:
                        arguments = {}
                else:
                    arguments = raw or {}
                call_names[call.get("id")] = name
                calls.append(
                    {"function": {"name": name, "arguments": arguments}}
                )
            out.append(
                {
                    "role": "assistant",
                    "content": message.get("content") or "",
                    "tool_calls": calls,
                }
            )
            continue

        if role == "tool":
            entry = {"role": "tool", "content": message.get("content") or ""}
            name = call_names.get(message.get("tool_call_id"))
            if name:
                entry["tool_name"] = name  # ignored by older builds
            out.append(entry)
            continue

        out.append({"role": role, "content": message.get("content") or ""})

    return out


# ---------------------------------------------------------------------------
# Provider schema — drives the settings form and validation
# ---------------------------------------------------------------------------
PROVIDER_SCHEMA = {
    "azure": {
        "label": "Azure OpenAI",
        "hint": "OAuth2 client-credentials flow against an Azure-hosted model. "
        "Supports the interactive Assistant.",
        "fields": [
            {
                "key": "endpoint",
                "label": "Endpoint",
                "type": "text",
                "required": True,
                "placeholder": "https://your-resource.openai.azure.com",
            },
            {
                "key": "token_url",
                "label": "Token URL",
                "type": "text",
                "required": True,
            },
            {
                "key": "client_id",
                "label": "Client ID",
                "type": "text",
                "required": True,
            },
            {
                "key": "client_secret",
                "label": "Client secret",
                "type": "password",
                "required": True,
                "secret": True,
            },
            {
                "key": "app_key",
                "label": "App key",
                "type": "password",
                "required": True,
                "secret": True,
            },
            {
                "key": "api_version",
                "label": "API version",
                "type": "text",
                "required": True,
                "placeholder": "2024-06-01",
            },
            {
                "key": "model",
                "label": "Model / deployment",
                "type": "text",
                "required": True,
                "hint": "A GPT-4-class deployment. Smaller models propose "
                "wrong commands and mishandle tool calls.",
            },
            {
                "key": "temperature",
                "label": "Temperature",
                "type": "number",
                "required": False,
                "placeholder": "0.2",
            },
        ],
    },
    "ollama": {
        "label": "Ollama (local or self-hosted)",
        "hint": "Runs on the host you point it at — nothing leaves that "
        "machine. The Assistant needs tool calling, which requires a "
        "large model to be reliable.",
        "fields": [
            {
                "key": "host",
                "label": "Host URL",
                "type": "text",
                "required": True,
                "placeholder": "http://localhost:11434",
            },
            {
                "key": "model",
                "label": "Model",
                "type": "text",
                "required": True,
                "placeholder": "qwen2.5:32b",
                "hint": "For the Assistant, use a tool-calling family "
                "(qwen2.5, qwen3, llama3.1/3.3, mistral-nemo, command-r, "
                "hermes3, granite3) at ~24B parameters or more.",
            },
            {
                "key": "assistant",
                "label": "Enable the interactive Assistant",
                "type": "checkbox",
                "required": False,
                "hint": "Lets the model propose commands via tool calling. "
                "Small models emit malformed calls and wrong-platform "
                "commands — every one is still policy-checked and needs "
                "your approval.",
            },
            {
                "key": "stream",
                "label": "Stream responses",
                "type": "checkbox",
                "required": False,
                "default": True,
                "hint": "Turn off if tool calls arrive malformed or truncated "
                "on your Ollama build.",
            },
            {
                "key": "temperature",
                "label": "Temperature",
                "type": "number",
                "required": False,
                "placeholder": "0.2",
            },
            {
                "key": "auth_token",
                "label": "Bearer token",
                "type": "password",
                "required": False,
                "secret": True,
                "hint": "Only needed behind a reverse proxy.",
            },
            {
                "key": "timeout",
                "label": "Timeout (s)",
                "type": "number",
                "required": False,
                "placeholder": "120",
            },
        ],
    },
}


def secret_fields(provider):
    """Return the field keys that must be encrypted at rest."""
    schema = PROVIDER_SCHEMA.get(provider) or {}
    return tuple(f["key"] for f in schema.get("fields", []) if f.get("secret"))


def public_schema():
    """Schema for the client — same shape, no values, no shared references."""
    return {
        key: {
            "label": spec["label"],
            "hint": spec.get("hint", ""),
            "fields": [dict(field) for field in spec["fields"]],
            "capabilities": (
                _PROVIDERS[key].capabilities.as_dict()
                if key in _PROVIDERS
                else {}
            ),
        }
        for key, spec in PROVIDER_SCHEMA.items()
    }


# ---------------------------------------------------------------------------
# Redaction — applied to every payload before egress
# ---------------------------------------------------------------------------
_MASK = "«redacted»"

# A credential directive is not always followed immediately by its value: a
# type/encoding digit, key id, hash algorithm or qualifier can sit in between
# (`enable secret 5 …`, `ntp authentication-key 1 md5 …`, `wpa-psk ascii 0 …`,
# `pre-shared-key local …`). These are skipped so the *value* gets masked.
_QUALIFIERS = (
    r"(?:\s+(?:\d+|2c|ascii|hex|hexadecimal|cipher|simple|encrypted|clear"
    r"|plain|plaintext|md5|sha|sha1|sha256|sha512|aes|des|3des|hmac[\w-]*"
    r"|local|remote|address|level|type|algorithm))*"
)

# Words that legitimately follow "key" in diagnostic output. Masking these
# would destroy useful error text ("no matching key exchange method found").
_KEY_BENIGN = r"(?!\s+(?:chain|exchange|length|id|management|pair|size)\b)"

_REDACT_RULES = (
    # -- directive [qualifiers] value ---------------------------------------
    (
        re.compile(
            r"\b(password|passwd|secret|pre-shared-key|key-string|shared-secret"
            r"|wpa-psk|authentication-key|auth-key|encryption-key|md5|hmac)\b"
            r"(" + _QUALIFIERS + r"\s+)(\S+)",
            re.I,
        ),
        r"\1\2" + _MASK,
    ),
    # bare `key` — the gap that leaked `crypto isakmp key`, `tacacs-server
    # key`, `radius-server key`, `ntp authentication-key`.
    (
        re.compile(
            r"\b(key)\b" + _KEY_BENIGN + r"(" + _QUALIFIERS + r"\s+)(\S+)",
            re.I,
        ),
        r"\1\2" + _MASK,
    ),
    # -- SNMP ---------------------------------------------------------------
    (re.compile(r"\b(snmp-server\s+community)\s+(\S+)", re.I), r"\1 " + _MASK),
    # The community follows the host, but `vrf`/`version`/`traps` etc. can sit
    # between them — consume those before masking.
    (
        re.compile(
            r"\b(snmp-server\s+host\s+\S+"
            r"(?:\s+(?:vrf\s+\S+|informs?|traps?|version|udp-port\s+\d+"
            r"|auth|noauth|priv|2c|\d+))*)"
            r"\s+(\S+)",
            re.I,
        ),
        r"\1 " + _MASK,
    ),
    (re.compile(r"\b(set\s+snmp\s+community)\s+(\S+)", re.I), r"\1 " + _MASK),
    (re.compile(r"\b(community(?:-string)?)\s+(\S+)", re.I), r"\1 " + _MASK),
    # -- user / enable credentials ------------------------------------------
    (
        re.compile(
            r"\b(username\s+\S+(?:\s+privilege\s+\d+)?\s+"
            r"(?:password|secret)(?:\s+\d)?)\s+(\S+)",
            re.I,
        ),
        r"\1 " + _MASK,
    ),
    (
        re.compile(
            r"\b(enable\s+(?:password|secret)(?:\s+\d)?)\s+(\S+)", re.I
        ),
        r"\1 " + _MASK,
    ),
    # -- Juniper / PAN-OS ---------------------------------------------------
    (
        re.compile(r"\b(plain-text-password\S*)(\s+)(\S+)", re.I),
        r"\1\2" + _MASK,
    ),
    (re.compile(r"\b(encrypted-password)\s+(\S+)", re.I), r"\1 " + _MASK),
    # -- key=value / key: value (JSON, env, API payloads) -------------------
    (
        re.compile(
            r'("?(?:api[_-]?key|apikey|token|access[_-]?token|secret'
            r'|client[_-]?secret|password|passwd|credential|bearer)"?'
            r'\s*[:=]\s*)"?([^\s",;}]+)"?',
            re.I,
        ),
        r"\1" + f'"{_MASK}"',
    ),
    (
        re.compile(r"\b(Authorization:\s*(?:Bearer|Basic))\s+(\S+)", re.I),
        r"\1 " + _MASK,
    ),
    # -- PEM blocks. Complete form first, then an unterminated block to EOF,
    # which a tail-truncated log will very often contain.
    (
        re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.S),
        f"-----BEGIN KEY-----\n{_MASK}\n-----END KEY-----",
    ),
    (
        re.compile(r"-----BEGIN [^-]+-----.*\Z", re.S),
        f"-----BEGIN KEY-----\n{_MASK}",
    ),
    (
        re.compile(r"\bssh-(?:rsa|ed25519|dss)\s+[A-Za-z0-9+/=]{32,}"),
        f"ssh-rsa {_MASK}",
    ),
    # -- Cisco type-5/7/8/9 hashes -----------------------------------------
    (re.compile(r"\$[0-9a-z]\$[^\s]{8,}"), _MASK),
    # -- opaque blobs that are almost certainly secrets ---------------------
    # Widened to include urlsafe base64 (`-` and `_`), which covers JWTs and
    # most modern API tokens.
    (re.compile(r"\b[A-Za-z0-9+/_-]{48,}={0,2}\b"), _MASK),
    (
        re.compile(
            r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
            r"\.[A-Za-z0-9_-]{10,}\b"
        ),
        _MASK,
    ),
    (re.compile(r"\b(?:[0-9a-f]{2}:){15,}[0-9a-f]{2}\b", re.I), _MASK),
    (re.compile(r"\b[0-9a-f]{40,}\b", re.I), _MASK),
)


def redact(text):
    """Mask credentials and secrets in *text*.

    Applied unconditionally to anything sent to a provider. Intentionally
    conservative: it may over-mask (a long hash gets flagged) rather than risk
    leaking a key. Idempotent — running it twice is harmless.
    """
    if not text:
        return text
    for pattern, replacement in _REDACT_RULES:
        text = pattern.sub(replacement, text)
    return text


def redact_messages(messages):
    """Redact the content of every message in a conversation."""
    out = []
    for message in messages or []:
        copy = dict(message)
        if isinstance(copy.get("content"), str):
            copy["content"] = redact(copy["content"])
        out.append(copy)
    return out


# ---------------------------------------------------------------------------
# Text budgeting — used by the agent to fit transcripts into context
# ---------------------------------------------------------------------------
CHUNK_CHARS = 12000
MAX_CHUNKS = 40

# Lines worth prioritising when text must be sampled down.
_SIGNAL_RE = re.compile(
    r"%\w+-\d-\w+"  # Cisco syslog tags
    r"|\b(?:error|failed|failure|denied|invalid|unreachable|down|flap"
    r"|reject|timeout|exceeded|drop|crash|traceback|exception)\b"
    r"|\^\s*$"  # caret pointing at a syntax error
    r"|^%",  # NX-OS / IOS error prefix
    re.I | re.M,
)


def _split_lines(text, limit):
    """Split *text* into chunks of at most *limit* chars, on line boundaries."""
    chunks, buf, size = [], [], 0
    for line in text.splitlines(keepends=True):
        if size + len(line) > limit and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


def chunk_log(text, chunk_chars=CHUNK_CHARS, max_chunks=MAX_CHUNKS):
    """Chunk *text*, sampling if it exceeds *max_chunks*.

    Returns ``(chunks, meta)``. When sampling is needed the head and tail are
    always kept — they hold the command and its outcome — and the remaining
    budget goes to the most error-dense middle chunks.
    """
    chunks = _split_lines(text, chunk_chars)
    meta = {
        "total_chunks": len(chunks),
        "sampled": False,
        "used_chunks": len(chunks),
        "chars": len(text),
    }
    if len(chunks) <= max_chunks:
        return chunks, meta

    head_n = max(1, max_chunks // 4)
    tail_n = max(1, max_chunks // 4)
    head, tail = chunks[:head_n], chunks[-tail_n:]
    middle = chunks[head_n:-tail_n]

    budget = max_chunks - head_n - tail_n
    scored = sorted(
        enumerate(middle),
        key=lambda pair: len(_SIGNAL_RE.findall(pair[1])),
        reverse=True,
    )[:budget]
    picked = [chunk for _, chunk in sorted(scored, key=lambda pair: pair[0])]

    meta.update(sampled=True, used_chunks=len(head) + len(picked) + len(tail))
    return head + picked + tail, meta


def sample_text(text, budget):
    """Fit *text* into *budget* characters, keeping the head, tail and the
    most error-dense middle. Returns ``(text, meta)``.

    This is what the agent uses on session transcripts: a plain tail cut would
    discard the command that produced an error while keeping only its
    aftermath.
    """
    if not text:
        return "", {"chars": 0, "sampled": False}
    if len(text) <= budget:
        return text, {"chars": len(text), "sampled": False}

    chunk_chars = max(1000, budget // 8)
    max_chunks = max(2, budget // chunk_chars)
    chunks, meta = chunk_log(
        text, chunk_chars=chunk_chars, max_chunks=max_chunks
    )
    joined = "\n[…]\n".join(chunks) if meta["sampled"] else "".join(chunks)
    if len(joined) > budget:
        joined = joined[-budget:]
        newline = joined.find("\n")
        if 0 <= newline < 200:
            joined = joined[newline + 1 :]
        meta["sampled"] = True
    meta["chars"] = len(text)
    return joined, meta


def sampling_note(meta):
    """Tell the model when content was dropped, so it can caveat its answer."""
    if not meta.get("sampled"):
        return ""
    return (
        f" (partial: {meta['chars']:,} characters were reduced to fit — "
        f"beginning, end and the most error-dense sections were kept)"
    )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
class BaseProvider:
    """Common interface: validate, test, and run a multi-turn chat."""

    name = ""
    capabilities = Capabilities()

    def __init__(self, config):
        self.config = config or {}
        self._validate()
        if "capabilities" not in self.__dict__:
            self.capabilities = type(self).capabilities

    def _require(self, *keys):
        missing = [
            k for k in keys if not str(self.config.get(k) or "").strip()
        ]
        if missing:
            raise AIError(
                f"{self.name}: missing required setting(s): {', '.join(missing)}"
            )

    def _float(self, key, default):
        try:
            return float(self.config.get(key) or default)
        except (TypeError, ValueError):
            return default

    def _validate(self):
        raise NotImplementedError

    def test(self):
        """Return a short human-readable success message, or raise AIError."""
        raise NotImplementedError

    # -- the one method the agent calls --------------------------------------
    def chat(self, messages, tools=None):
        """Yield :class:`TextChunk` / :class:`ToolCall`, then :class:`Done`.

        Redaction happens here, at the single egress point, so no caller can
        forget it.
        """
        prepared = redact_messages(messages)
        if tools and not self.capabilities.supports_tools:
            raise AIError(
                f"{self.name} does not support tool calling. The interactive "
                f"Assistant needs a tool-capable provider."
            )
        yield from self._chat(prepared, tools)

    def _chat(self, messages, tools):
        raise NotImplementedError

    def ask(self, messages):
        """Blocking convenience wrapper — text only, tool calls ignored."""
        parts = []
        for event in self.chat(messages):
            if isinstance(event, TextChunk):
                parts.append(event.text)
        return "".join(parts).strip()

    @classmethod
    def capabilities_for(cls, config):
        """Capabilities implied by *config*, without building a provider.

        Used by the settings API to gate the Assistant UI before any request
        has been made.
        """
        return cls.capabilities


# ---------------------------------------------------------------------------
class OllamaProvider(BaseProvider):
    """Ollama — local or reverse-proxied, with optional tool calling.

    Tool calling is off by default. The Assistant's schema has nested arrays,
    and small models emit malformed calls, invent aliases, or propose commands
    for the wrong platform. Enabling it is therefore an explicit choice, and
    the model is checked against a list of families documented to support
    tools plus a rough parameter-count floor.
    """

    name = "ollama"
    capabilities = Capabilities(supports_streaming=True, supports_tools=False)

    DEFAULT_TIMEOUT = 120
    KEEP_ALIVE = "5m"
    DEFAULT_TEMPERATURE = 0.2

    # Model families documented to support tool calling. Prefix match against
    # the tag, so `qwen2.5:32b-instruct-q4_K_M` matches `qwen2.5`.
    _TOOL_FAMILIES = (
        "llama3.1",
        "llama3.2",
        "llama3.3",
        "llama4",
        "qwen2.5",
        "qwen3",
        "qwq",
        "mistral-nemo",
        "mistral-large",
        "mistral-small",
        "devstral",
        "command-r",
        "command-a",
        "hermes3",
        "granite3",
        "granite4",
        "athene",
        "firefunction",
        "nemotron",
        "gpt-oss",
        "magistral",
        "cogito",
    )

    # Below this the nested-array schema is unreliable in practice. Not a hard
    # block — the user can proceed — but the UI says so.
    _RECOMMENDED_PARAMS_B = 24

    def _validate(self):
        # The schema marks these required; honour it rather than silently
        # falling back to localhost and reporting "active" for a blank config.
        self._require("host", "model")
        self.host = str(self.config["host"]).strip().rstrip("/")
        self.model = str(self.config["model"]).strip()
        self.timeout = int(self._float("timeout", self.DEFAULT_TIMEOUT))
        self.temperature = self._float("temperature", self.DEFAULT_TEMPERATURE)
        self.auth_token = self.config.get("auth_token") or None
        self.assistant_enabled = _truthy(self.config.get("assistant"))
        self.use_streaming = _truthy(self.config.get("stream"), default=True)
        self._client = None
        self.capabilities = self.capabilities_for(self.config)

    # -- capability gating ---------------------------------------------------
    @staticmethod
    def _params_b(model):
        """Best-effort parameter count in billions, parsed from the tag."""
        text = (model or "").lower()
        match = re.search(r"(?:^|[:\-])(\d+(?:\.\d+)?)\s*b\b", text)
        if match:
            return float(match.group(1))
        # Mixture-of-experts tags such as `8x7b` — use the expert size.
        match = re.search(r"(\d+)x(\d+(?:\.\d+)?)b", text)
        if match:
            return float(match.group(2))
        return None

    @classmethod
    def _tool_family(cls, model):
        bare = (model or "").lower().split(":", 1)[0]
        return any(bare.startswith(family) for family in cls._TOOL_FAMILIES)

    @classmethod
    def capabilities_for(cls, config):
        config = config or {}
        if not _truthy(config.get("assistant")):
            return cls.capabilities
        if not cls._tool_family(config.get("model") or ""):
            return cls.capabilities
        return Capabilities(
            supports_streaming=True,
            supports_tools=True,
            supports_tools_while_streaming=True,
        )

    def model_warning(self):
        """Human-readable caveat about the configured model, or ``''``."""
        if not self.assistant_enabled:
            return ""
        if not self._tool_family(self.model):
            return (
                f"'{self.model}' is not a known tool-calling model family, "
                f"so the Assistant stays disabled. Known families: "
                f"{', '.join(self._TOOL_FAMILIES[:8])}…"
            )
        size = self._params_b(self.model)
        if size is not None and size < self._RECOMMENDED_PARAMS_B:
            return (
                f"'{self.model}' is roughly {size:g}B parameters. The "
                f"Assistant's command schema is nested; models under "
                f"~{self._RECOMMENDED_PARAMS_B}B often emit malformed "
                f"calls or wrong-platform commands."
            )
        return ""

    # -- client --------------------------------------------------------------
    def _get_client(self):
        if self._client is None:
            try:
                from ollama import Client
            except ImportError as exc:
                raise AIError(
                    "The 'ollama' package is not installed. "
                    "Run: pip install ollama"
                ) from exc
            headers = (
                {"Authorization": f"Bearer {self.auth_token}"}
                if self.auth_token
                else None
            )
            self._client = Client(
                host=self.host, headers=headers, timeout=self.timeout
            )
        return self._client

    def list_models(self):
        resp = self._get_client().list()
        models = getattr(resp, "models", None)
        if models is None and isinstance(resp, dict):
            models = resp.get("models", [])
        names = []
        for entry in models or []:
            name = getattr(entry, "model", None)
            if name is None and isinstance(entry, dict):
                name = entry.get("model") or entry.get("name")
            if name:
                names.append(name)
        return names

    def test(self):
        try:
            installed = self.list_models()
        except AIError:
            raise
        except Exception as exc:
            raise AIError(
                f"Cannot reach Ollama at {self.host}: {exc}"
            ) from exc

        bare = self.model.split(":", 1)[0]
        if (
            installed
            and self.model not in installed
            and not any(m.split(":", 1)[0] == bare for m in installed)
        ):
            raise AIError(
                f"Model '{self.model}' is not installed. "
                f"Available: {', '.join(installed[:8]) or 'none'}"
            )

        parts = [f"Connected to {self.host} — model '{self.model}' ready."]
        if self.capabilities.supports_tools:
            parts.append("Tool calling enabled: the Assistant is available.")
        elif self.assistant_enabled:
            parts.append("Assistant unavailable — see the note below.")
        else:
            parts.append(
                "Text generation only "
                "(enable the Assistant to use tool calling)."
            )
        warning = self.model_warning()
        if warning:
            parts.append(warning)
        return " ".join(parts)

    # -- chat ----------------------------------------------------------------
    def _options(self):
        return {"temperature": self.temperature}

    @staticmethod
    def _extract(chunk, attribute, default=None):
        """Read an attribute from an SDK object or a plain dict."""
        value = getattr(chunk, attribute, None)
        if value is None and isinstance(chunk, dict):
            value = chunk.get(attribute)
        return default if value is None else value

    def _tool_calls_from(self, message, seen):
        """Normalise Ollama tool calls, skipping duplicates across chunks.

        Unlike Azure, Ollama emits each call complete rather than as a run of
        argument fragments — but a call can be repeated across chunks, so they
        are de-duplicated by (name, arguments).
        """
        raw_calls = self._extract(message, "tool_calls") or []
        out = []
        for index, call in enumerate(raw_calls):
            function = self._extract(call, "function") or {}
            fname = self._extract(function, "name") or ""
            arguments = self._extract(function, "arguments")

            error = ""
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments or "{}")
                except ValueError as exc:
                    arguments, error = None, f"Malformed tool arguments: {exc}"
            if arguments is not None and not isinstance(arguments, dict):
                arguments, error = None, "Tool arguments were not an object."

            raw = (
                json.dumps(arguments) if isinstance(arguments, dict) else "{}"
            )
            key = (fname, raw)
            if key in seen:
                continue
            seen.add(key)

            call_id = self._extract(call, "id") or f"call_{len(seen)}_{index}"
            out.append(
                ToolCall(
                    id=str(call_id),
                    name=fname,
                    arguments=arguments,
                    raw=raw,
                    error=error,
                )
            )
        return out

    def _chat(self, messages, tools):
        if tools and not self.capabilities.supports_tools:
            # BaseProvider.chat already guards this; belt and braces.
            raise AIError(
                f"Tool calling is not enabled for Ollama model "
                f"'{self.model}'. Enable the Assistant under Settings → AI."
            )

        client = self._get_client()
        payload = _to_ollama_messages(messages)
        stream = bool(self.use_streaming)

        logger.info(
            "Ollama chat → %s (%s, %d message(s), tools=%s, stream=%s)",
            self.model,
            self.host,
            len(payload),
            bool(tools),
            stream,
        )

        kwargs = {
            "model": self.model,
            "messages": payload,
            "stream": stream,
            "keep_alive": self.KEEP_ALIVE,
            "options": self._options(),
        }
        if tools:
            kwargs["tools"] = tools

        seen = set()
        calls = []
        reason = "stop"
        try:
            response = client.chat(**kwargs)

            if not stream:
                message = self._extract(response, "message") or {}
                content = self._extract(message, "content", "") or ""
                if content:
                    yield TextChunk(content)
                calls = self._tool_calls_from(message, seen)
                for call in calls:
                    yield call
                reason = "tool_calls" if calls else "stop"
            else:
                for chunk in response:
                    message = self._extract(chunk, "message") or {}
                    content = self._extract(message, "content", "") or ""
                    if content:
                        yield TextChunk(content)
                    for call in self._tool_calls_from(message, seen):
                        calls.append(call)
                        yield call
                    if self._extract(chunk, "done"):
                        reason = self._extract(chunk, "done_reason") or (
                            "tool_calls" if calls else "stop"
                        )
                        break
                if calls and reason == "stop":
                    reason = "tool_calls"
        except AIError:
            raise
        except Exception as exc:
            raise AIError(f"Ollama request failed: {exc}") from exc

        yield Done(reason, calls)


# ---------------------------------------------------------------------------
class AzureProvider(BaseProvider):
    """Azure OpenAI via OAuth2 client credentials, with tool calling."""

    name = "azure"
    capabilities = Capabilities(
        supports_streaming=True,
        supports_tools=True,
        supports_tools_while_streaming=True,
    )

    _REQUIRED = (
        "endpoint",
        "token_url",
        "client_id",
        "client_secret",
        "app_key",
        "api_version",
        "model",
    )
    _TOKEN_MARGIN = 60.0  # refresh this long before expiry
    _TOKEN_FALLBACK_TTL = 3600.0
    _DEFAULT_TEMPERATURE = 0.2

    def _validate(self):
        self._require(*self._REQUIRED)
        for key in self._REQUIRED:
            setattr(self, key, str(self.config[key]).strip())
        self.temperature = self._float(
            "temperature", self._DEFAULT_TEMPERATURE
        )
        self._token = None
        self._token_expires_at = 0.0
        self._client = None

    # -- auth ---------------------------------------------------------------
    def _get_token(self, force=False):
        """Return a valid bearer token, refreshing shortly before expiry.

        An agent turn can make five or more requests over several minutes, so
        a token cached for the provider's lifetime is not sufficient.
        """
        if (
            not force
            and self._token
            and time.monotonic() < self._token_expires_at - self._TOKEN_MARGIN
        ):
            return self._token

        import json
        import urllib.error
        import urllib.request

        credentials = f"{self.client_id}:{self.client_secret}".encode()
        headers = {
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {base64.b64encode(credentials).decode()}",
        }
        request = urllib.request.Request(
            self.token_url,
            data=b"grant_type=client_credentials",
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Read the body: OAuth servers put the actual reason in there.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                pass
            raise AIError(
                f"OAuth token request failed: HTTP {exc.code} {exc.reason}"
                + (f" - {detail}" if detail else "")
            ) from exc
        except Exception as exc:
            raise AIError(f"OAuth token request failed: {exc}") from exc

        token = payload.get("access_token")
        if not token:
            raise AIError("OAuth response contained no access_token.")

        try:
            ttl = float(payload.get("expires_in") or self._TOKEN_FALLBACK_TTL)
        except (TypeError, ValueError):
            ttl = self._TOKEN_FALLBACK_TTL

        self._token = token
        self._token_expires_at = time.monotonic() + ttl
        self._client = None  # rebuild, the api_key has changed
        logger.debug("Azure token refreshed; valid for %.0fs.", ttl)
        return token

    def _get_client(self):
        token = self._get_token()
        if self._client is None:
            try:
                from openai import AzureOpenAI
            except ImportError as exc:
                raise AIError(
                    "The 'openai' package is not installed. "
                    "Run: pip install openai"
                ) from exc
            self._client = AzureOpenAI(
                azure_endpoint=self.endpoint,
                api_key=token,
                api_version=self.api_version,
            )
        return self._client

    def test(self):
        self._get_token(force=True)
        try:
            client = self._get_client()
            client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": "ok"}],
                max_tokens=1,
                user=self._user_field(),
            )
        except Exception as exc:
            raise AIError(
                f"Authenticated, but the model call failed — check the "
                f"deployment name and API version: {exc}"
            ) from exc
        return (
            f"Authenticated with Azure — deployment '{self.model}' "
            f"responded. Tool calling supported."
        )

    # -- helpers ------------------------------------------------------------
    def _user_field(self):
        # json.dumps, not an f-string: a quote or backslash in the app key
        # would otherwise produce invalid JSON.
        return json.dumps({"appkey": self.app_key})

    @staticmethod
    def _is_auth_error(exc):
        text = str(exc)
        return (
            "401" in text
            or "Unauthorized" in text
            or exc.__class__.__name__ == "AuthenticationError"
        )

    def _create(self, messages, tools, retry=True):
        client = self._get_client()
        kwargs = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "user": self._user_field(),
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            if retry and self._is_auth_error(exc):
                logger.info("Azure returned auth failure; refreshing token.")
                self._get_token(force=True)
                return self._create(messages, tools, retry=False)
            raise AIError(f"Azure request failed: {exc}") from exc

    @staticmethod
    def _accumulate(store, deltas):
        """Merge streamed tool-call fragments, keyed by index.

        Azure sends the id and name once, then the JSON arguments as a long
        run of string fragments. Parsing before the stream ends is guaranteed
        to fail.
        """
        for delta in deltas or []:
            index = getattr(delta, "index", None)
            if index is None:
                index = 0
            slot = store.setdefault(index, {"id": "", "name": "", "args": ""})
            if getattr(delta, "id", None):
                slot["id"] = delta.id
            function = getattr(delta, "function", None)
            if function is not None:
                if getattr(function, "name", None):
                    slot["name"] = function.name
                fragment = getattr(function, "arguments", None)
                if fragment:
                    slot["args"] += fragment

    @staticmethod
    def _finish_tool_calls(store):
        calls = []
        for index in sorted(store):
            slot = store[index]
            raw = slot["args"] or "{}"
            try:
                arguments, error = json.loads(raw), ""
                if not isinstance(arguments, dict):
                    arguments, error = (
                        None,
                        "Tool arguments were not an object.",
                    )
            except ValueError as exc:
                arguments, error = None, f"Malformed tool arguments: {exc}"
            calls.append(
                ToolCall(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"] or "",
                    arguments=arguments,
                    raw=raw,
                    error=error,
                )
            )
        return calls

    def _chat(self, messages, tools):
        logger.info(
            "Azure chat → %s (%d message(s), tools=%s)",
            self.model,
            len(messages),
            bool(tools),
        )
        stream = self._create(messages, tools)

        pending = {}
        reason = "stop"
        try:
            for chunk in stream:
                choices = getattr(chunk, "choices", None)
                if not choices:
                    continue  # usage-only or content-filter chunk
                choice = choices[0]
                delta = getattr(choice, "delta", None)

                if delta is not None:
                    content = getattr(delta, "content", None)
                    if content:
                        yield TextChunk(content)
                    self._accumulate(
                        pending, getattr(delta, "tool_calls", None)
                    )

                finish = getattr(choice, "finish_reason", None)
                if finish:
                    reason = finish
                    break
        except AIError:
            raise
        except Exception as exc:
            raise AIError(f"Azure stream failed: {exc}") from exc

        # Flush even when finish_reason never arrived — some gateways omit it.
        calls = self._finish_tool_calls(pending)
        yield from calls
        if calls and reason == "stop":
            reason = "tool_calls"
        yield Done(reason, calls)


_PROVIDERS = {
    "azure": AzureProvider,
    "ollama": OllamaProvider,
}


def build_provider(provider, config):
    """Instantiate a provider handler, raising :class:`AIError` if unknown."""
    handler = _PROVIDERS.get(provider)
    if handler is None:
        raise AIError(f"Unknown AI provider: {provider!r}")
    return handler(config)


def provider_capabilities(provider, config=None):
    """Return the :class:`Capabilities` a provider offers under *config*."""
    handler = _PROVIDERS.get(provider)
    if handler is None:
        return Capabilities()
    try:
        return handler.capabilities_for(config or {})
    except Exception:
        logger.debug(
            "capabilities_for failed for %s.", provider, exc_info=True
        )
        return handler.capabilities


def active_provider():
    """Build the configured provider, or raise :class:`AIError`."""
    from .settings import get_ai_store

    store = get_ai_store()
    if not store.is_active():
        raise AIError(
            "AI is not enabled. Configure a provider under Settings → AI."
        )
    settings = store.get(reveal=True)
    return build_provider(settings["provider"], settings["config"])


def test_provider(provider_name, config):
    """Validate a provider configuration; return ``(ok, message)``."""
    try:
        provider = build_provider(provider_name, config)
        message = provider.test()
        return True, message
    except AIError as exc:
        return False, str(exc)
    except Exception:
        logger.exception("AI provider test failed.")
        return False, "Provider test failed — see the server log for detail."
