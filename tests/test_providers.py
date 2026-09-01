"""Provider event normalisation, with the focus on Azure tool-call streaming.

Accumulating tool-call deltas is the part most likely to break silently: the
arguments arrive as a long run of string fragments, and parsing early produces
plausible-looking corruption rather than an error.

File path: tests/test_providers.py
"""

import json
from types import SimpleNamespace

import pytest
from terminus.ai.providers import (
    AIError,
    AzureProvider,
    Capabilities,
    Done,
    OllamaProvider,
    TextChunk,
    ToolCall,
    assistant_message,
    build_provider,
    provider_capabilities,
    public_schema,
    secret_fields,
    tool_message,
)

AZURE_CONFIG = {
    "endpoint": "https://example.openai.azure.com",
    "token_url": "https://login.example.com/token",
    "client_id": "cid",
    "client_secret": "csecret",
    "app_key": r'weird"key\with\escapes',
    "api_version": "2024-06-01",
    "model": "gpt-4o",
}

RUN_COMMANDS_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "run_commands",
            "description": "Run read-only commands on selected sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "targets": {"type": "array"},
                },
                "required": ["reason", "targets"],
            },
        },
    }
]


# ---------------------------------------------------------------------------
# Fakes shaped like the openai SDK's streaming objects
# ---------------------------------------------------------------------------
def text_chunk(text, finish=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=finish,
            )
        ]
    )


def tool_chunk(index=0, call_id=None, name=None, arguments=None, finish=None):
    call = SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[call]),
                finish_reason=finish,
            )
        ]
    )


def empty_chunk():
    """Azure emits these for usage and content-filter results."""
    return SimpleNamespace(choices=[])


class FakeCompletions:
    def __init__(self, chunks, fail_first_with=None):
        self.chunks = chunks
        self.fail_first_with = fail_first_with
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_first_with is not None and len(self.calls) == 1:
            error, self.fail_first_with = self.fail_first_with, None
            raise error
        return iter(self.chunks)


class FakeClient:
    def __init__(self, chunks, fail_first_with=None):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(chunks, fail_first_with)
        )


def azure(chunks, fail_first_with=None, config=None):
    """Build a provider with auth pre-satisfied and a fake client injected."""
    provider = AzureProvider(config or AZURE_CONFIG)
    provider._token = "fake-token"
    provider._token_expires_at = float("inf")
    provider._client = FakeClient(chunks, fail_first_with)
    return provider


def drain(provider, messages=None, tools=None):
    return list(
        provider.chat(
            messages or [{"role": "user", "content": "hi"}], tools=tools
        )
    )


# ---------------------------------------------------------------------------
# Text streaming
# ---------------------------------------------------------------------------
def test_text_is_streamed_in_order():
    provider = azure(
        [
            text_chunk("Interface "),
            text_chunk("Gi0/1 "),
            text_chunk("is up.", finish="stop"),
        ]
    )
    events = drain(provider)
    assert [e.text for e in events if isinstance(e, TextChunk)] == [
        "Interface ",
        "Gi0/1 ",
        "is up.",
    ]
    assert isinstance(events[-1], Done)
    assert events[-1].reason == "stop"
    assert events[-1].tool_calls == []


def test_empty_choices_chunks_are_skipped():
    provider = azure(
        [empty_chunk(), text_chunk("ok", finish="stop"), empty_chunk()]
    )
    events = drain(provider)
    assert [e.text for e in events if isinstance(e, TextChunk)] == ["ok"]


def test_ask_collects_text_only():
    provider = azure([text_chunk("a"), text_chunk("b", finish="stop")])
    assert provider.ask([{"role": "user", "content": "hi"}]) == "ab"


# ---------------------------------------------------------------------------
# Tool-call accumulation
# ---------------------------------------------------------------------------
def test_tool_call_split_across_many_deltas():
    """The realistic case: arguments arrive one fragment at a time."""
    arguments = json.dumps(
        {
            "reason": "discovery",
            "targets": [{"alias": "S1", "commands": ["show version"]}],
        }
    )
    chunks = [
        tool_chunk(call_id="call_abc", name="run_commands", arguments="")
    ]
    chunks += [
        tool_chunk(arguments=arguments[i : i + 7])
        for i in range(0, len(arguments), 7)
    ]
    chunks.append(text_chunk("", finish="tool_calls"))

    events = drain(azure(chunks), tools=RUN_COMMANDS_TOOL)
    calls = [e for e in events if isinstance(e, ToolCall)]

    assert len(calls) == 1
    assert calls[0].id == "call_abc"
    assert calls[0].name == "run_commands"
    assert calls[0].error == ""
    assert calls[0].arguments["reason"] == "discovery"
    assert calls[0].arguments["targets"][0]["commands"] == ["show version"]
    assert events[-1].reason == "tool_calls"


def test_parallel_tool_calls_are_kept_separate():
    """GPT-4-class models emit several calls in one turn, interleaved."""
    args_a = json.dumps({"reason": "a", "targets": [{"alias": "S1"}]})
    args_b = json.dumps({"reason": "b", "targets": [{"alias": "S2"}]})
    chunks = [
        tool_chunk(
            index=0, call_id="call_a", name="run_commands", arguments=""
        ),
        tool_chunk(
            index=1, call_id="call_b", name="run_commands", arguments=""
        ),
        tool_chunk(index=0, arguments=args_a[:12]),
        tool_chunk(index=1, arguments=args_b[:12]),
        tool_chunk(index=0, arguments=args_a[12:]),
        tool_chunk(index=1, arguments=args_b[12:]),
        text_chunk("", finish="tool_calls"),
    ]
    calls = [
        e
        for e in drain(azure(chunks), tools=RUN_COMMANDS_TOOL)
        if isinstance(e, ToolCall)
    ]

    assert [c.id for c in calls] == ["call_a", "call_b"]
    assert calls[0].arguments["reason"] == "a"
    assert calls[1].arguments["reason"] == "b"


def test_text_then_tool_call_in_one_turn():
    provider = azure(
        [
            text_chunk("Let me check the platform first.\n"),
            tool_chunk(
                call_id="c1",
                name="run_commands",
                arguments='{"reason":"check","targets":[]}',
            ),
            text_chunk("", finish="tool_calls"),
        ]
    )
    events = drain(provider, tools=RUN_COMMANDS_TOOL)
    assert isinstance(events[0], TextChunk)
    assert isinstance(events[1], ToolCall)
    assert isinstance(events[2], Done)


def test_malformed_arguments_are_reported_not_raised():
    """The model must get a chance to correct itself."""
    provider = azure(
        [
            tool_chunk(
                call_id="c1",
                name="run_commands",
                arguments='{"reason": "oops", "targets": [',
            ),
            text_chunk("", finish="tool_calls"),
        ]
    )
    calls = [
        e
        for e in drain(provider, tools=RUN_COMMANDS_TOOL)
        if isinstance(e, ToolCall)
    ]
    assert calls[0].arguments is None
    assert "Malformed tool arguments" in calls[0].error
    assert calls[0].raw.startswith('{"reason": "oops"')


def test_non_object_arguments_rejected():
    provider = azure(
        [
            tool_chunk(
                call_id="c1", name="run_commands", arguments='["not", "obj"]'
            ),
            text_chunk("", finish="tool_calls"),
        ]
    )
    calls = [
        e
        for e in drain(provider, tools=RUN_COMMANDS_TOOL)
        if isinstance(e, ToolCall)
    ]
    assert calls[0].arguments is None
    assert "not an object" in calls[0].error


def test_missing_finish_reason_still_flushes_tool_calls():
    """Some gateways drop finish_reason; the call must not be lost."""
    provider = azure(
        [
            tool_chunk(
                call_id="c1", name="run_commands", arguments='{"reason":"x"}'
            ),
        ]
    )
    events = drain(provider, tools=RUN_COMMANDS_TOOL)
    calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(calls) == 1
    assert events[-1].reason == "tool_calls"


def test_missing_index_defaults_to_zero():
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=None,
                            id="c1",
                            function=SimpleNamespace(
                                name="run_commands", arguments='{"reason":"x"}'
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ]
    )
    calls = [
        e
        for e in drain(azure([chunk]), tools=RUN_COMMANDS_TOOL)
        if isinstance(e, ToolCall)
    ]
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------
def test_tools_are_passed_with_auto_choice():
    provider = azure([text_chunk("ok", finish="stop")])
    drain(provider, tools=RUN_COMMANDS_TOOL)
    kwargs = provider._client.chat.completions.calls[0]
    assert kwargs["tools"] == RUN_COMMANDS_TOOL
    assert kwargs["tool_choice"] == "auto"
    assert kwargs["stream"] is True


def test_no_tools_key_when_none_requested():
    provider = azure([text_chunk("ok", finish="stop")])
    drain(provider)
    assert "tools" not in provider._client.chat.completions.calls[0]


def test_app_key_is_valid_json_even_with_quotes():
    """M6: an f-string here produced invalid JSON for keys containing quotes."""
    provider = azure([text_chunk("ok", finish="stop")])
    drain(provider)
    user = provider._client.chat.completions.calls[0]["user"]
    assert json.loads(user) == {"appkey": AZURE_CONFIG["app_key"]}


def test_redaction_happens_at_the_egress_point():
    """No caller can forget it, because chat() applies it itself."""
    provider = azure([text_chunk("ok", finish="stop")])
    drain(
        provider,
        messages=[
            {"role": "user", "content": "snmp-server community LEAKME RO"}
        ],
    )
    sent = provider._client.chat.completions.calls[0]["messages"]
    assert "LEAKME" not in sent[0]["content"]


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_expired_token_triggers_refresh_and_client_rebuild(monkeypatch):
    provider = AzureProvider(AZURE_CONFIG)
    provider._token = "stale"
    provider._token_expires_at = 0.0  # already expired
    provider._client = object()  # must be discarded

    refreshed = {"count": 0}

    def fake_token(force=False):
        refreshed["count"] += 1
        provider._token = "fresh"
        provider._token_expires_at = float("inf")
        provider._client = None
        return "fresh"

    monkeypatch.setattr(provider, "_get_token", fake_token)
    monkeypatch.setattr(
        provider,
        "_get_client",
        lambda: (fake_token(), FakeClient([text_chunk("ok", finish="stop")]))[
            1
        ],
    )

    list(provider.chat([{"role": "user", "content": "hi"}]))
    assert refreshed["count"] >= 1
    assert provider._token == "fresh"


def test_auth_failure_retries_once_with_a_fresh_token():
    class AuthenticationError(Exception):
        pass

    provider = azure(
        [text_chunk("recovered", finish="stop")],
        fail_first_with=AuthenticationError("401 Unauthorized"),
    )
    tokens = {"count": 0}

    def fake_token(force=False):
        # Must not delegate to the real _get_token — that makes a live request.
        tokens["count"] += 1
        provider._token = f"token-{tokens['count']}"
        provider._token_expires_at = float("inf")
        return provider._token

    provider._get_token = fake_token
    events = drain(provider)

    assert [e.text for e in events if isinstance(e, TextChunk)] == [
        "recovered"
    ]
    assert len(provider._client.chat.completions.calls) == 2
    assert tokens["count"] >= 2


def test_non_auth_error_is_not_retried():
    provider = azure([], fail_first_with=RuntimeError("500 upstream boom"))
    with pytest.raises(AIError, match="Azure request failed"):
        drain(provider)
    assert len(provider._client.chat.completions.calls) == 1


# ---------------------------------------------------------------------------
# Capabilities and gating
# ---------------------------------------------------------------------------
def test_azure_advertises_tool_support():
    caps = provider_capabilities("azure")
    assert caps.supports_tools is True
    assert caps.supports_tools_while_streaming is True


def test_ollama_does_not_advertise_tool_support():
    assert provider_capabilities("ollama").supports_tools is False


def test_tool_request_to_a_text_only_provider_is_refused():
    provider = OllamaProvider(
        {"host": "http://localhost:11434", "model": "llama3.1:8b"}
    )
    with pytest.raises(AIError, match="does not support tool calling"):
        list(
            provider.chat(
                [{"role": "user", "content": "hi"}], tools=RUN_COMMANDS_TOOL
            )
        )


def test_unknown_provider_capabilities_are_conservative():
    assert provider_capabilities("nope") == Capabilities()


# ---------------------------------------------------------------------------
# Validation (H7)
# ---------------------------------------------------------------------------
def test_ollama_requires_host_and_model():
    with pytest.raises(AIError, match="missing required setting"):
        OllamaProvider({})
    with pytest.raises(AIError, match="missing required setting"):
        OllamaProvider({"host": "http://localhost:11434"})


def test_ollama_accepts_a_complete_config():
    provider = OllamaProvider(
        {
            "host": "http://ollama.lan:11434/",
            "model": "llama3.1:8b",
            "timeout": "45",
        }
    )
    assert provider.host == "http://ollama.lan:11434"  # trailing slash gone
    assert provider.timeout == 45


def test_azure_requires_every_field():
    for missing in AZURE_CONFIG:
        config = dict(AZURE_CONFIG)
        config[missing] = ""
        with pytest.raises(AIError, match="missing required setting"):
            AzureProvider(config)


def test_azure_temperature_default_and_override():
    assert AzureProvider(AZURE_CONFIG).temperature == 0.2
    assert (
        AzureProvider({**AZURE_CONFIG, "temperature": "0.7"}).temperature
        == 0.7
    )
    assert (
        AzureProvider({**AZURE_CONFIG, "temperature": "nonsense"}).temperature
        == 0.2
    )


def test_build_provider_rejects_unknown():
    with pytest.raises(AIError, match="Unknown AI provider"):
        build_provider("gemini", {})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_public_schema_exposes_capabilities_for_ui_gating():
    schema = public_schema()
    assert schema["azure"]["capabilities"]["supports_tools"] is True
    assert schema["ollama"]["capabilities"]["supports_tools"] is False


def test_public_schema_does_not_share_mutable_references():
    """M16: it used to hand out the live field list."""
    first = public_schema()
    first["azure"]["fields"][0]["label"] = "MUTATED"
    assert public_schema()["azure"]["fields"][0]["label"] != "MUTATED"


def test_secret_fields():
    assert set(secret_fields("azure")) == {"client_secret", "app_key"}
    assert secret_fields("ollama") == ("auth_token",)
    assert secret_fields("nope") == ()


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------
def test_assistant_message_replays_tool_calls_verbatim():
    """Azure 400s if the replayed arguments differ from what it sent."""
    call = ToolCall(
        id="c1",
        name="run_commands",
        arguments={"reason": "x"},
        raw='{"reason": "x"}',
    )
    message = assistant_message("checking", [call])
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["id"] == "c1"
    assert (
        message["tool_calls"][0]["function"]["arguments"] == '{"reason": "x"}'
    )


def test_assistant_message_falls_back_to_reserialising():
    call = ToolCall(id="c1", name="run_commands", arguments={"reason": "x"})
    args = assistant_message("", [call])["tool_calls"][0]["function"][
        "arguments"
    ]
    assert json.loads(args) == {"reason": "x"}


def test_tool_message_shape():
    message = tool_message("c1", json.dumps({"results": []}))
    assert message == {
        "role": "tool",
        "tool_call_id": "c1",
        "content": '{"results": []}',
    }
