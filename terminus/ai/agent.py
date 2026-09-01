"""Conversation orchestration for the interactive Assistant.

Wires together the four layers built in earlier phases:

    transcript.py  → what the model reads
    ai.py          → how it thinks and asks for things
    policy.py      → what it is allowed to ask for
    executor.py    → how those requests actually run

The loop is deliberately linear rather than event-driven: a turn runs on one
background thread and *blocks* on a :class:`threading.Event` while waiting for
the user's approval. In Socket.IO threading mode that is safe, and it keeps the
control flow readable — the alternative (resuming a serialised state machine
from a socket handler) scatters the same logic across five callbacks.

State machine, per user message::

    IDLE
     └─ send ──→ THINKING (stream assistant text)
                  ├─ no tool call ──────────────→ DONE
                  └─ tool call ────────────────→ AWAITING_APPROVAL
                       ├─ deny  → tool result "denied" → THINKING
                       └─ approve → EXECUTING → tool result → THINKING
                            (capped at MAX_ROUNDS, then a forced answer)

File path: terminus/ai/agent.py
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import executor as ex
from .policy import (
    RISK_READ_ONLY,
    ValidatedPlan,
    confirmation_phrase,
    validate_plan,
)
from .providers import (
    AIError,
    Done,
    TextChunk,
    ToolCall,
    active_provider,
    assistant_message,
    sample_text,
    sampling_note,
    system_message,
    tool_message,
    user_message,
)

logger = logging.getLogger(__name__)

# -- states -----------------------------------------------------------------
STATE_IDLE = "idle"
STATE_THINKING = "thinking"
STATE_AWAITING_APPROVAL = "awaiting_approval"
STATE_EXECUTING = "executing"
STATE_DONE = "done"
STATE_ERROR = "error"
STATE_CANCELLED = "cancelled"

# -- guardrails -------------------------------------------------------------
MAX_ROUNDS = 5
APPROVAL_TIMEOUT = 900.0  # abandon a turn nobody answers
TURN_DEADLINE = 900.0
MAX_TURN_OUTPUT_CHARS = ex.MAX_TURN_OUTPUT_CHARS

# -- context budgets (characters, not tokens — deliberately conservative) ---
TRANSCRIPT_BUDGET_TOTAL = 60_000
TRANSCRIPT_BUDGET_MIN = 4_000
HISTORY_MAX_MESSAGES = 40

# The risk ceiling this build will execute. Raising it to RISK_MUTATING is what
# turns on phase 2 — the tiers, confirmation phrase and mode-balance checks are
# already implemented below and in policy.py.
RISK_CEILING = RISK_READ_ONLY


# ---------------------------------------------------------------------------
# Tool definition
# ---------------------------------------------------------------------------
TOOL_RUN_COMMANDS = {
    "type": "function",
    "function": {
        "name": "run_commands",
        "description": (
            "Run diagnostic commands on one or more of the selected live "
            "sessions and return their output. Commands must be read-only. "
            "The user sees every command and must approve before anything "
            "runs, so state plainly why you need each one. Prefer the "
            "smallest set of commands that answers the question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": (
                        "One sentence, shown to the user in the approval "
                        "prompt, explaining what you intend to find out."
                    ),
                },
                "targets": {
                    "type": "array",
                    "description": (
                        "One entry per session. Commands may differ per "
                        "session — use the syntax correct for that platform."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "alias": {
                                "type": "string",
                                "description": "Session alias, e.g. 'S1'.",
                            },
                            "commands": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "Commands in execution order. One command "
                                    "per string; no shell pipes, no chaining, "
                                    "no newlines."
                                ),
                            },
                        },
                        "required": ["alias", "commands"],
                    },
                },
            },
            "required": ["reason", "targets"],
        },
    },
}

_TOOL_NAME = TOOL_RUN_COMMANDS["function"]["name"]


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = """\
You are Terminus Assistant, embedded in a terminal application used by network
engineers. You are connected to live SSH sessions on real production
infrastructure.

## How you work

You can read each session's recent terminal transcript, and you can request
commands via the `run_commands` tool. You never execute anything yourself: the
user sees each proposed command and must approve it. Assume they will read what
you propose, so be precise and minimal.

Only read-only diagnostic commands are permitted. Configuration changes,
reloads, clears and file operations are refused by the safety layer before the
user ever sees them — do not propose them. If a task genuinely requires a
change, say so and tell the user to make it themselves via the Broadcast tab.

## Rules

- Reference concrete values from actual output: interface names, IP addresses,
  AS numbers, counters, versions, timestamps. Never invent output.
- Use the command syntax correct for each session's platform. The roster below
  gives you each platform; different sessions may need different commands.
- If the transcript already answers the question, answer from it. Do not run
  commands to confirm something you can already see.
- Values shown as «redacted» are credentials masked before you saw them. They
  are intentionally hidden, not errors, and not something to ask about.
- A command may come back with status `error` (the device rejected it),
  `timeout`, `busy`, `wrong_mode`, `blocked` or `session_gone`. Read the status,
  do not assume success. If a command was rejected, the syntax was probably
  wrong for that platform — correct it or say you cannot.
- If output is marked truncated, say which conclusions are therefore partial.
- You have a limited number of command rounds per question. Plan the commands
  you need in one batch rather than discovering them one at a time.

## Output

Short Markdown. Use `##` headings and bullets. Tables when comparing devices.
No preamble, no filler, no restating the question. When you are done, stop — do
not offer further help unprompted.
"""

_ROSTER_HEADER = """\
## Selected sessions

You may only target these aliases. Each is a live, already-authenticated
session.

"""

_FORCE_ANSWER = """\
You have used all available command rounds for this question. Answer now using
only what you already have. State plainly which parts remain unverified.
"""


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """The user's answer to an approval request."""

    approved: bool
    items: list = field(default_factory=list)  # possibly edited rows
    reason: str = ""


@dataclass
class PendingPlan:
    plan_id: str
    reason: str
    plan: ValidatedPlan
    calls: list  # the ToolCall(s) that produced it


class Conversation:
    """Server-side state for one chat.

    Ephemeral: created on ``chat_start``, destroyed on disconnect or reset.
    Nothing is persisted — a page reload begins a new conversation.
    """

    def __init__(self, chat_id, sid, emit, get_session, notify=None):
        self.chat_id = chat_id
        self.sid = sid
        self._emit = emit
        self._get_session = get_session
        self._notify = notify

        self.history = []  # user / assistant / tool messages
        self.state = STATE_IDLE
        self.round = 0
        self.auto_approve_read_only = False

        self.roster = {}  # alias -> info, rebuilt per turn
        self.pending = None  # PendingPlan
        self._decision = None

        self._lock = threading.RLock()
        self._approval = threading.Event()
        self._cancel = threading.Event()
        self._turn_active = threading.Event()

    # -- emit helpers --------------------------------------------------------
    def emit(self, event, **payload):
        try:
            self._emit(event, chat_id=self.chat_id, **payload)
        except Exception:
            logger.debug(
                "Chat emit failed for %s.", self.chat_id, exc_info=True
            )

    def set_state(self, state):
        with self._lock:
            self.state = state
        self.emit(
            "chat_state", state=state, round=self.round, max_rounds=MAX_ROUNDS
        )

    # -- external control ----------------------------------------------------
    def cancel(self):
        """Request cancellation; wakes an approval wait and stops execution."""
        self._cancel.set()
        self._approval.set()

    @property
    def cancelled(self):
        return self._cancel.is_set()

    @property
    def busy(self):
        return self._turn_active.is_set()

    def decide(self, plan_id, approved, items=None, reason=""):
        """Record an approval or denial. Returns True if it was expected."""
        with self._lock:
            pending = self.pending
            if pending is None or pending.plan_id != plan_id:
                return False
            self._decision = Decision(
                approved=approved, items=items or [], reason=reason
            )
        self._approval.set()
        return True

    def reset(self):
        with self._lock:
            self.history.clear()
            self.round = 0
            self.pending = None
            self._decision = None
        self._cancel.clear()
        self.set_state(STATE_IDLE)

    # -- history -------------------------------------------------------------
    def append(self, message):
        with self._lock:
            self.history.append(message)
            self._trim_history()

    def _trim_history(self):
        """Drop the oldest messages, never orphaning a tool result.

        Azure rejects a ``tool`` message whose ``tool_call_id`` has no matching
        assistant ``tool_calls`` entry earlier in the list, so the front of the
        history can only be cut at a boundary that keeps those pairs intact.
        Skipping a leading tool-request turn as well costs one extra message and
        removes any chance of a half-pair surviving.
        """
        if len(self.history) <= HISTORY_MAX_MESSAGES:
            return
        cut = len(self.history) - HISTORY_MAX_MESSAGES
        while cut < len(self.history) and self.history[cut]["role"] == "tool":
            cut += 1
        while cut < len(self.history) and self.history[cut].get("tool_calls"):
            cut += 1
        self.history[:] = self.history[cut:]

    # -- context -------------------------------------------------------------
    def build_messages(self, force_answer=False):
        """Assemble the full message list for one provider call.

        The roster and transcripts are rebuilt every call rather than stored in
        history, so the model always sees current session state and a five-round
        turn does not carry five copies of a 60 KB transcript.
        """
        messages = [
            system_message(_SYSTEM_PROMPT),
            system_message(self._context_block()),
        ]
        with self._lock:
            messages.extend(self.history)
        if force_answer:
            messages.append(system_message(_FORCE_ANSWER))
        return messages

    def _context_block(self):
        """Roster plus per-session transcripts, within budget."""
        aliases = list(self.roster)
        if not aliases:
            return "## Selected sessions\n\nNone — no sessions are selected."

        parts = [_ROSTER_HEADER]
        for alias in aliases:
            info = self.roster[alias]
            sess = self._get_session(info["session_id"])
            live = "live" if sess else "CLOSED since this chat began"
            parts.append(
                f"- **{alias}** — {info['hostname']} · "
                f"platform `{info['device_type'] or 'unknown'}` · "
                f"prompt `{info.get('base_prompt') or '?'}` · {live}"
            )

        budget_each = max(
            TRANSCRIPT_BUDGET_MIN,
            TRANSCRIPT_BUDGET_TOTAL // max(1, len(aliases)),
        )

        parts.append("\n## Session transcripts\n")
        parts.append(
            "Recent terminal output for each session, cleaned of escape "
            "sequences. Lines beginning `── [Terminus AI]` mark commands you "
            "ran earlier in this conversation.\n"
        )
        for alias in aliases:
            info = self.roster[alias]
            sess = self._get_session(info["session_id"])
            transcript = (sess or {}).get("transcript")
            if transcript is None:
                parts.append(
                    f"\n### {alias} — {info['hostname']}\n"
                    f"(no transcript available)\n"
                )
                continue
            raw, _clipped = transcript.read()
            text, meta = sample_text(raw, budget_each)
            note = sampling_note(meta)
            parts.append(
                f"\n### {alias} — {info['hostname']}{note}\n"
                f"```\n{text.strip() or '(no output captured yet)'}\n```\n"
            )
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_conversations = {}
_registry_lock = threading.Lock()


def get_or_create(chat_id, sid, emit, get_session, notify=None):
    with _registry_lock:
        conv = _conversations.get(chat_id)
        if conv is None:
            conv = Conversation(chat_id, sid, emit, get_session, notify)
            _conversations[chat_id] = conv
            logger.info("Chat %s created.", chat_id)
        else:
            conv.sid = sid  # survive a reconnect on the same chat_id
        return conv


def get(chat_id):
    with _registry_lock:
        return _conversations.get(chat_id)


def drop(chat_id):
    with _registry_lock:
        conv = _conversations.pop(chat_id, None)
    if conv is not None:
        conv.cancel()
        logger.info("Chat %s dropped.", chat_id)


def drop_for_sid(sid):
    """Tear down every conversation belonging to a disconnected client."""
    with _registry_lock:
        stale = [
            cid for cid, conv in _conversations.items() if conv.sid == sid
        ]
    for chat_id in stale:
        drop(chat_id)
    return stale


def active_count():
    with _registry_lock:
        return len(_conversations)


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------
def build_roster(session_ids, get_session):
    """Assign stable short aliases to the selected sessions.

    Short aliases (``S1``) rather than internal ids: less for the model to
    hallucinate, and it keeps session ids out of the prompt entirely.
    """
    roster = {}
    for index, session_id in enumerate(session_ids or [], start=1):
        sess = get_session(session_id)
        if not sess:
            continue
        roster[f"S{index}"] = {
            "session_id": session_id,
            "hostname": sess.get("hostname") or session_id,
            "device_type": sess.get("device_type") or "",
            "base_prompt": sess.get("base_prompt") or "",
        }
    return roster


# ---------------------------------------------------------------------------
# Plan assembly
# ---------------------------------------------------------------------------
def _merge_targets(calls):
    """Flatten possibly-parallel tool calls into one target list.

    GPT-4-class models emit several ``run_commands`` calls in a single turn.
    Presenting two approval cards for one intent is confusing, so merge them,
    concatenating commands per alias in call order.
    """
    reasons, merged, order = [], {}, []
    for call in calls:
        arguments = call.arguments or {}
        reason = str(arguments.get("reason") or "").strip()
        if reason:
            reasons.append(reason)
        for target in arguments.get("targets") or []:
            if not isinstance(target, dict):
                continue
            alias = str(target.get("alias") or "").strip()
            if not alias:
                continue
            commands = target.get("commands")
            if isinstance(commands, str):  # tolerate a bare string
                commands = [commands]
            if not isinstance(commands, list):
                continue
            if alias not in merged:
                merged[alias] = []
                order.append(alias)
            merged[alias].extend(str(command) for command in commands)
    targets = [{"alias": alias, "commands": merged[alias]} for alias in order]
    return " ".join(reasons), targets


def _plan_row(item):
    return {
        "alias": item.alias,
        "hostname": item.hostname,
        "device_type": item.device_type,
        "command": item.command,
        "risk": item.risk,
        "reason": item.reason,
    }


def _plan_payload(plan, plan_id, reason):
    """Shape the approval card the client renders."""
    return {
        "plan_id": plan_id,
        "reason": reason,
        "items": [_plan_row(item) for item in plan.approved],
        "blocked": [_plan_row(item) for item in plan.blocked],
        "max_risk": plan.max_risk,
        "needs_confirmation": plan.needs_confirmation,
        "confirmation_phrase": (
            confirmation_phrase(plan) if plan.needs_confirmation else ""
        ),
    }


def _revalidate_edits(items, roster):
    """Re-run policy over user-edited commands.

    The client can edit a command before approving. Trusting that text would
    make the browser the security boundary, so it goes through exactly the same
    validation as the model's original proposal.
    """
    targets, order = {}, []
    for item in items or []:
        alias = str((item or {}).get("alias") or "").strip()
        command = str((item or {}).get("command") or "").strip()
        if not alias or not command:
            continue
        if alias not in targets:
            targets[alias] = []
            order.append(alias)
        targets[alias].append(command)
    return validate_plan(
        [{"alias": alias, "commands": targets[alias]} for alias in order],
        roster,
        max_risk=RISK_CEILING,
    )


# ---------------------------------------------------------------------------
# Tool results
# ---------------------------------------------------------------------------
def _tool_result_json(results, blocked, budget=MAX_TURN_OUTPUT_CHARS):
    """Serialise execution results for the model, honouring a char budget."""
    payload = {"results": [], "blocked": []}

    for item in blocked or []:
        payload["blocked"].append(
            {
                "alias": item.alias,
                "command": item.command,
                "status": ex.STATUS_BLOCKED,
                "detail": item.reason,
            }
        )

    remaining = budget
    for result in results or []:
        entry = result.to_dict()
        output = entry.get("output") or ""
        if len(output) > remaining:
            output = output[-max(0, remaining) :]
            if output:
                newline = output.find("\n")
                if 0 <= newline < 200:
                    output = output[newline + 1 :]
            entry["output"] = output
            entry["truncated"] = True
        remaining = max(0, remaining - len(output))
        payload["results"].append(entry)

    return json.dumps(payload, ensure_ascii=False)


def _denial_json(reason):
    return json.dumps(
        {
            "results": [],
            "blocked": [],
            "denied": True,
            "detail": (
                reason or "The user declined to run these commands."
            ).strip(),
        }
    )


def _error_json(message):
    return json.dumps({"results": [], "blocked": [], "error": message})


# ---------------------------------------------------------------------------
# The turn
# ---------------------------------------------------------------------------
def run_turn(conv, text, session_ids):
    """Run one full user turn. Intended to be called in a background task."""
    if conv.busy:
        conv.emit(
            "chat_error",
            message="A question is already in progress in this chat.",
        )
        return

    conv._turn_active.set()
    logger.info(
        "Chat %s: turn start, %d session(s), question=%r",
        conv.chat_id,
        len(session_ids or []),
        text[:80],
    )
    conv._cancel.clear()
    conv.round = 0
    deadline = time.monotonic() + TURN_DEADLINE
    output_left = MAX_TURN_OUTPUT_CHARS

    try:
        provider = active_provider()
        if not provider.capabilities.supports_tools:
            raise AIError(
                f"The configured provider ({provider.name}) cannot run the "
                f"interactive Assistant — it does not support tool calling. "
                f"Configure a tool-capable provider under Settings → AI."
            )

        conv.roster = build_roster(session_ids, conv._get_session)
        if not conv.roster:
            raise AIError("Select at least one live session first.")

        conv.append(user_message(text))
        conv.emit(
            "chat_scope",
            sessions=[
                {
                    "alias": alias,
                    "hostname": info["hostname"],
                    "session_id": info["session_id"],
                }
                for alias, info in conv.roster.items()
            ],
        )

        for round_index in range(1, MAX_ROUNDS + 1):
            conv.round = round_index
            if conv.cancelled:
                conv.set_state(STATE_CANCELLED)
                return
            if time.monotonic() > deadline:
                conv.emit(
                    "chat_error",
                    message="This question exceeded its time budget.",
                )
                conv.set_state(STATE_ERROR)
                return

            calls = _think(conv, provider)
            if calls is None:  # cancelled or failed
                return
            if not calls:
                conv.set_state(STATE_DONE)
                conv.emit("chat_done")
                return

            handled, output_left = _handle_tool_calls(
                conv, calls, output_left, deadline
            )
            if not handled:
                return

        # Round cap reached — force a final text answer rather than leaving the
        # user with five approval cards and no conclusion.
        conv.round = MAX_ROUNDS
        if _think(conv, provider, force_answer=True) is None:
            return
        conv.set_state(STATE_DONE)
        conv.emit("chat_done")

    except AIError as exc:
        logger.info("Chat %s failed: %s", conv.chat_id, exc)
        conv.emit("chat_error", message=str(exc))
        conv.set_state(STATE_ERROR)
    except Exception as exc:
        logger.exception("Chat %s crashed.", conv.chat_id)
        conv.emit(
            "chat_error",
            message=f"Unexpected error: {exc.__class__.__name__}. "
            f"See the server log.",
        )
        conv.set_state(STATE_ERROR)
    finally:
        conv.pending = None
        conv._turn_active.clear()


def _think(conv, provider, force_answer=False):
    """Stream one assistant turn.

    Returns the list of tool calls (possibly empty), or ``None`` if the turn
    was cancelled or the provider failed.
    """

    conv.set_state(STATE_THINKING)
    messages = conv.build_messages(force_answer=force_answer)
    tools = None if force_answer else [TOOL_RUN_COMMANDS]

    parts, calls = [], []
    try:
        for event in provider.chat(messages, tools=tools):
            if conv.cancelled:
                conv.set_state(STATE_CANCELLED)
                return None
            if isinstance(event, TextChunk):
                parts.append(event.text)
                conv.emit("chat_delta", text=event.text)
            elif isinstance(event, ToolCall):
                calls.append(event)
            elif isinstance(event, Done):
                break
    except AIError as exc:
        conv.emit("chat_error", message=str(exc))
        conv.set_state(STATE_ERROR)
        return None

    text = "".join(parts)
    conv.append(assistant_message(text, calls))
    if text.strip():
        conv.emit("chat_message", role="assistant", text=text)
    return calls


def _handle_tool_calls(conv, calls, output_left, deadline):
    """Validate, seek approval for, and execute one round of tool calls.

    Returns ``(continue_turn, output_left)``. Every tool call id must receive
    exactly one tool message, or the next provider request is rejected.
    """

    logger.info(
        "Chat %s: %d tool call(s): %s",
        conv.chat_id,
        len(calls),
        [c.name for c in calls],
    )

    for call in [c for c in calls if c.name != _TOOL_NAME]:
        conv.append(
            tool_message(call.id, _error_json(f"No such tool: {call.name!r}."))
        )

    usable = [call for call in calls if call.name == _TOOL_NAME]
    if not usable:
        return True, output_left

    for call in [c for c in usable if c.arguments is None]:
        conv.append(
            tool_message(
                call.id,
                _error_json(
                    f"{call.error} Re-issue the call with valid JSON."
                ),
            )
        )
    usable = [call for call in usable if call.arguments is not None]
    if not usable:
        return True, output_left

    reason, targets = _merge_targets(usable)
    plan = validate_plan(targets, conv.roster, max_risk=RISK_CEILING)
    primary, extra = usable[0], usable[1:]

    # Merged calls still need one tool message each.
    for call in extra:
        conv.append(
            tool_message(
                call.id,
                json.dumps({"note": f"Merged into tool call {primary.id}."}),
            )
        )

    if not plan.approved:
        conv.append(
            tool_message(primary.id, _tool_result_json([], plan.blocked))
        )
        conv.emit(
            "chat_plan",
            **_plan_payload(plan, "n/a", reason),
            auto_rejected=True,
        )
        return True, output_left

    plan_id = f"p_{uuid.uuid4().hex[:10]}"
    conv.pending = PendingPlan(plan_id, reason, plan, usable)
    conv.emit("chat_plan", **_plan_payload(plan, plan_id, reason))

    decision = _await_decision(conv, plan, deadline)
    if decision is None:
        conv.pending = None
        conv.append(
            tool_message(
                primary.id,
                _denial_json("No response — the request timed out."),
            )
        )
        conv.set_state(STATE_CANCELLED)
        return False, output_left

    if not decision.approved:
        conv.pending = None
        conv.append(tool_message(primary.id, _denial_json(decision.reason)))
        return True, output_left

    items = plan.approved
    if decision.items:
        revalidated = _revalidate_edits(decision.items, conv.roster)
        items = revalidated.approved
        if revalidated.blocked:
            conv.emit(
                "chat_plan",
                **_plan_payload(revalidated, plan_id, reason),
                post_edit=True,
            )
        if not items:
            conv.pending = None
            conv.append(
                tool_message(
                    primary.id, _tool_result_json([], revalidated.blocked)
                )
            )
            return True, output_left

    conv.pending = None
    conv.set_state(STATE_EXECUTING)

    def progress(payload):
        conv.emit("chat_exec", plan_id=plan_id, **payload)

    budget = min(output_left, MAX_TURN_OUTPUT_CHARS)
    results = ex.run_batch(
        items,
        conv._get_session,
        notify=conv._notify,
        cancel_event=conv._cancel,
        progress=progress,
        deadline=max(5.0, deadline - time.monotonic()),
        turn_budget=budget,
    )

    for result in results:
        if result.output:
            conv.emit(
                "chat_exec_output",
                plan_id=plan_id,
                alias=result.alias,
                command=result.command,
                output=result.output,
                truncated=result.truncated,
            )

    used = sum(len(result.output or "") for result in results)
    conv.append(
        tool_message(
            primary.id, _tool_result_json(results, plan.blocked, budget)
        )
    )

    if conv.cancelled:
        conv.set_state(STATE_CANCELLED)
        return False, output_left
    return True, max(0, output_left - used)


def _await_decision(conv, plan, deadline):
    """Block until the user approves, denies, or the wait expires.

    Returns a :class:`Decision`, or ``None`` on timeout.
    """
    if conv.auto_approve_read_only and not plan.needs_confirmation:
        logger.info(
            "Chat %s: auto-approving %d read-only command(s).",
            conv.chat_id,
            len(plan.approved),
        )
        return Decision(approved=True)

    conv.set_state(STATE_AWAITING_APPROVAL)
    conv._approval.clear()
    with conv._lock:
        conv._decision = None

    budget = min(APPROVAL_TIMEOUT, max(5.0, deadline - time.monotonic()))
    if not conv._approval.wait(budget):
        logger.info("Chat %s: approval timed out.", conv.chat_id)
        return None

    if conv.cancelled:
        return Decision(approved=False, reason="The user cancelled.")

    with conv._lock:
        decision = conv._decision
        conv._decision = None
    return decision or Decision(approved=False, reason="No decision recorded.")
