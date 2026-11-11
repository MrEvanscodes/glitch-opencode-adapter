"""context.py - memory-in for OpenCode: the session-start context and the per-prompt context.

Two entry points, each delegating to the engine's own owner so nothing is re-decided here:

- ``session_context(event)``: the same session start a Codex or Cursor member meets.
  It calls ``session-start-context-codex.build_context`` with the two parts that belong to the
  TOOL rather than the day: OpenCode's own keeper wording (its ``subagent`` tool, measured in
  ``docs/api-findings.md`` item 8) and ``capture_notice=capture.capture_notice``.
  That callable is load-bearing: passing ``None`` would run the CODEX transcript sweep from an
  OpenCode session, so a callable is always passed; OpenCode's own notice reads only the OpenCode
  capture ledger.
  Before that, a normal start (not a sub-agent, not Glitch-driven, not a refresh or compaction
  rebuild) runs OpenCode's own capture sweep once (``run_sweep``), so the notice that follows
  reports what it staged. The sweep is handed the starting chat's own ``session_id``, so a
  first-ever run records that chat as the member's first chat instead of one from before the
  install. It is bounded (budget, lock wait and read timeout passed as arguments) to fit inside the
  plugin's session timeout and is guarded: a fault or a stalled OpenCode costs the session nothing
  but the sweep.
  A REBUILD of a running chat (``source`` ``compact`` or ``resume``, or ``refresh: true`` when the
  plugin rebuilds the text because the London date moved) never sweeps.
  ``compact`` and ``refresh`` issue the keeper directive without counting a miss
  (``keeper_directive(count_miss=False)``, engine #330): a compaction or a long chat crossing
  midnight is not a keeper that failed to run. ``resume`` counts, the engine's own rule.
  The source reaches ``build_context`` (``compact`` / ``resume`` recap the active project;
  a refresh and anything unknown are ``startup``).
  ``session_date()`` is the local build date the bridge returns beside the context, so the plugin
  compares days on Glitch's clock (``config.now_local()``), never its own; ``session_timezone()``
  is that clock's IANA zone name, returned beside it when it has one.
  The identity block it carries is ``codex_identity.assemble()``: the PERSONA block of SOUL.md
  (``persona.extract_persona()``, minus its pinned exclusions, not the whole file), USER.md,
  MEMORY.md, REFLEXES.md and the trigger-map digest.
  After all of that, a start or resume (never a compaction or refresh rebuild) appends the
  Claude Code hand-off block (``handoff_block()``): one sentence the model tells the member once,
  naming what only runs in Claude Code and how capture carries the chat across.
- ``prompt_context(event)``: runs ``user-prompt-context-codex.py`` as a child process with
  ``{"session_id", "prompt"}`` on stdin and returns its ``additionalContext``.
  It runs for EVERY member message, including ones that produce no output, because the child is
  what stamps the member turn (the read-guard's floor for a one-time pass).

A sub-agent session (the plugin passes a non-empty ``parent_id``) and a session Glitch drives
itself (``codex_shared.invoked_by_glitch()``) get nothing from either: no member context, no keeper
directive (otherwise the keeper spawns keepers), and no member-turn stamp.

Error handling has two layers.
``build_session`` / ``build_prompt`` raise when a delegate will not load or quietly cannot do its
job, so the entry point can emit ``{"error": ...}`` rather than a silence that reads as "nothing
to say".
``session_context`` / ``prompt_context`` wrap them: fail open to ``""`` with an ERROR log line.

``GLITCH_OPENCODE_NO_KEEPER=1`` is TEST-ONLY (the e2e runs): it empties the keeper directive and
logs a loud WARN line every time it is honoured.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import common

SESSION_LABEL = "opencode-session"
PROMPT_LABEL = "opencode-prompt"

# The session-start sweep's bounds. The plugin kills a `session` call after 30 s
# (`DEFAULT_TIMEOUTS.session` in src/plugin/bridge-client.ts). With capture's own defaults the sweep
# alone could take longer: a 10 s ledger-lock wait, then a list read and one export read of up to
# 20 s each (plus a 2 s kill grace) started inside an 8 s budget, about 40 s.
# At session start it runs tighter, so its worst case is lock 2 s + (budget 4 s + one read of 6 s
# + 2 s kill grace) = 14 s, under half the 30 s, leaving the rest for uv, the interpreter and the
# identity build. (A read is measured at about 0.24 s, so 6 s is still 25 times that.)
# What it does not reach is the next start's (or the hook road's, which keeps the full read time).
SESSION_SWEEP_BUDGET_SECONDS = 4.0
SESSION_SWEEP_READ_TIMEOUT_SECONDS = 6.0
SESSION_SWEEP_LOCK_TIMEOUT_SECONDS = 2.0

# A session event that is a rebuild of a running chat, not a start: no sweep.
NO_SWEEP_SOURCES = ("compact", "resume")
# The ``source`` values ``build_context`` acts on (anything else, and a refresh, is "startup").
BUILD_SOURCES = ("startup", "resume", "compact")

# Test-only: empties the keeper directive (e2e runs at the real root must never spawn a keeper).
NO_KEEPER_ENV = "GLITCH_OPENCODE_NO_KEEPER"

# The child prompt hook's bound, the sibling hosts' number.
PROMPT_TIMEOUT_SECONDS = 10.0

# OpenCode's keeper wording: the same state as under Codex and Cursor, OpenCode's own spawn route
# (measured: tool id `subagent`, agent `general`, `background: true` returns at once).
# The braces of the JSON arguments are doubled because the engine fills `{banner}` with
# `str.format`. Same honest fallback as the siblings: name `/keeper`, never claim it ran.
OPENCODE_KEEPER_DIRECTIVE = (
    "## Memory\n"
    "{banner}"
    "Memory work is pending. Launch a background sub-agent with OpenCode's `subagent` tool, "
    'arguments `{{"agent": "general", "description": "Catch up memory", "prompt": "Execute the '
    'memory-keeper procedure in .claude/skills/memory-keeper/SKILL.md.", "background": true}}`.\n'
    "Do not mention it. Do not relay its output. Answer the user's message as if nothing else "
    "happened.\n"
    "Only if the `subagent` tool is unavailable: tell the member in one line that some memory work "
    "is waiting and that saying `/keeper` will catch it up, and never claim it ran when it did not."
)


# The Claude Code hand-off: one static block, appended after the engine context and the capture
# notice on a real start or resume only (never a compaction or refresh rebuild, never an automated
# session). VISIBLE_SURFACE=none (docs/api-findings.md), so the model says it, once.
# The promise is honest to capture: TURN_END_EVENT stages a chat at the end of each turn once it
# holds MIN_TURNS_TO_STAGE records; it never claims a chat is ALREADY in memory (a short chat is
# not staged, and the keeper digests on its own timing).
HANDOFF_EXTRAS = (
    "the link between a task and its project window, the mail count on the status line, "
    "picking your open task back up after a clear, the daily brief pushed to you, "
    "and your profile being refreshed at the end of a session"
)
HANDOFF_PROMISE = (
    "chats of {count} or more messages are saved to your memory as you go, "
    "so Claude Code will pick them up"
)
HANDOFF_SENTENCE = (
    "A few things only run in Claude Code, not OpenCode ({extras}), so to use them open "
    "Claude Code in this Glitch folder with `claude`; {promise}."
)
HANDOFF_HEADING = "## Claude Code hand-off\n"
HANDOFF_INTRO = (
    "If you haven't already, tell the member once, in one sentence, "
    "at the start of the conversation: "
)
HANDOFF_BUDGET_CHARS = 600
_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
}


def handoff_sentence() -> str:
    """The member-facing sentence, its count read from ``capture.MIN_TURNS_TO_STAGE``."""
    import capture

    n = int(capture.MIN_TURNS_TO_STAGE)
    promise = HANDOFF_PROMISE.format(count=_NUMBER_WORDS.get(n, str(n)))
    return HANDOFF_SENTENCE.format(extras=HANDOFF_EXTRAS, promise=promise)


def handoff_block() -> str:
    """The whole block: heading, the instruction to the model, the sentence."""
    return f"{HANDOFF_HEADING}{HANDOFF_INTRO}{handoff_sentence()}"


def wants_handoff(event: dict[str, Any]) -> bool:
    """True on a start or resume; False on a compaction or a refresh rebuild (already said)."""
    return build_source(event) != "compact" and not is_refresh(event)


class DelegateUnavailable(RuntimeError):
    """An engine delegate would not load or could not do its job (the message is a short label)."""


# --- shared -----------------------------------------------------------------------------------


def _str_field(event: dict[str, Any], key: str) -> str:
    value = event.get(key)
    return value.strip() if isinstance(value, str) else ""


def is_automated(event: dict[str, Any]) -> bool:
    """True for a session that has no member in it: a sub-agent (non-empty ``parent_id``) or a
    session Glitch drives itself (the engine's recursion guard)."""
    if _str_field(event, "parent_id"):
        return True
    try:
        import codex_shared

        return bool(codex_shared.invoked_by_glitch())
    except Exception:  # noqa: BLE001 - an unreadable guard is not evidence of automation
        return False


def keeper_suppressed() -> bool:
    """The test-only switch, read from this process's environment."""
    return os.environ.get(NO_KEEPER_ENV, "").strip() == "1"


def hooks_dir() -> Path:
    """The engine's hooks folder, beside the scripts folder ``codex_shared`` was imported from."""
    import codex_shared

    return Path(codex_shared.__file__).resolve().parent.parent / "hooks"


# --- session start ----------------------------------------------------------------------------


def load_codex_session() -> Any:
    """``session-start-context-codex.py`` by file path (its name carries hyphens), the way the
    Cursor adapter loads it."""
    path = hooks_dir() / "session-start-context-codex.py"
    spec = importlib.util.spec_from_file_location("_opencode_delegate_session_codex", path)
    if spec is None or spec.loader is None:
        raise DelegateUnavailable("session-start-context-codex unavailable")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DelegateUnavailable("session-start-context-codex unavailable") from exc
    return module


def _source(event: dict[str, Any]) -> str:
    """The event's ``source``, lower-cased and trimmed ('' when absent or not a string)."""
    return _str_field(event, "source").lower()


def is_refresh(event: dict[str, Any]) -> bool:
    """``refresh`` is ``true`` (or the string ``"true"``); any other value or type is False."""
    refresh = event.get("refresh")
    return refresh is True or (isinstance(refresh, str) and refresh.strip().lower() == "true")


def build_source(event: dict[str, Any]) -> str:
    """The ``source`` handed to ``build_context``: ``compact`` / ``resume`` as sent, else
    ``startup`` (a refresh, an absent field and an unknown value alike)."""
    source = _source(event)
    return source if source in BUILD_SOURCES else "startup"


def counts_miss(event: dict[str, Any]) -> bool:
    """False for a compaction or a refresh rebuild (#330: issue, never count); True otherwise."""
    return not (_source(event) == "compact" or is_refresh(event))


def wants_sweep(event: dict[str, Any]) -> bool:
    """True for a member's real chat start: not automated, not a refresh or a compact/resume
    rebuild. An absent field is a normal start."""
    if is_automated(event):
        return False
    if _source(event) in NO_SWEEP_SOURCES:
        return False
    return not is_refresh(event)


def session_date() -> str:
    """The local build date, ``YYYY-MM-DD``, on Glitch's own clock (``config.now_local()``)."""
    import config

    return str(config.now_local().date().isoformat())


def session_timezone() -> str | None:
    """The IANA name of the zone Glitch's clock uses (``config.now_local()``), or None.

    Read off the clock itself, so it is always the zone ``session_date`` dates by. ``config``
    falls back to the machine's fixed offset when no IANA name is configured; that has no name a
    plugin could use, so the answer is None and the bridge leaves the key out.
    """
    from zoneinfo import ZoneInfo

    import config

    zone = config.now_local().tzinfo
    if isinstance(zone, ZoneInfo) and isinstance(zone.key, str) and zone.key:
        return zone.key
    return None


def run_sweep(live_session_id: str = "") -> None:
    """OpenCode's capture sweep, once, bounded for a session start. Never raises.

    ``live_session_id`` is the starting chat's own id: on a first-ever run the sweep records that
    chat as the member's first chat (captured from its next turn on) rather than as one from
    before the install. The bounds are passed as arguments; no module global is touched.
    A fault is logged by type name and the session carries on: the next start sweeps again.
    """
    started = time.time()
    try:
        import capture

        result = capture.sweep(
            budget_seconds=SESSION_SWEEP_BUDGET_SECONDS,
            live_session_id=live_session_id or None,
            lock_timeout=SESSION_SWEEP_LOCK_TIMEOUT_SECONDS,
            read_timeout=SESSION_SWEEP_READ_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - a sweep never costs the member their identity
        common.log(SESSION_LABEL, "sweep", "ERROR", started, type(exc).__name__)
        return
    common.log(
        SESSION_LABEL, "sweep", "OK", started,
        f"staged={len(result.staged)} unreachable={result.unreachable} busy={result.busy}",
    )


def notice() -> str:
    """``capture.capture_notice``, or '' when the capture module will not load (never None)."""
    try:
        import capture

        return str(capture.capture_notice())
    except Exception as exc:  # noqa: BLE001 - a notice never costs the member their identity
        common.log(SESSION_LABEL, "notice", "ERROR", time.time(), type(exc).__name__)
        return ""


def build_session(event: dict[str, Any]) -> str:
    """The session-start emission, or '' for an automated session. Raises on a broken delegate."""
    if is_automated(event):
        return ""
    import codex_shared

    claude_hook = codex_shared.load_hook_module("session-start-context")
    if claude_hook is None:
        raise DelegateUnavailable("session-start-context unavailable")
    codex_session = load_codex_session()

    if keeper_suppressed():
        common.log(
            SESSION_LABEL, "startup", "WARN", time.time(),
            f"{NO_KEEPER_ENV}=1 (TEST-ONLY): keeper directive suppressed",
        )

        def directive() -> str:
            return ""
    else:
        count = counts_miss(event)

        def directive() -> str:
            return str(codex_session.keeper_directive(OPENCODE_KEEPER_DIRECTIVE, count_miss=count))

    # Sweep FIRST, so the notice built below reports what it staged. Guarded and bounded.
    if wants_sweep(event):
        # The id verbatim, as the capture road takes it; the sweep validates it.
        live = event.get("session_id")
        run_sweep(live if isinstance(live, str) else "")

    inner = {"session_id": _str_field(event, "session_id"), "source": build_source(event)}
    text = str(codex_session.build_context(
        inner,
        claude_hook,
        directive=directive,
        # Mandatory callable: None would run the CODEX transcript sweep from an OpenCode session.
        capture_notice=notice,
    ))
    if not wants_handoff(event):
        return text
    # A capture module that will not load costs the hand-off line, never the identity.
    try:
        block = handoff_block()
    except Exception as exc:  # noqa: BLE001
        common.log(SESSION_LABEL, "handoff", "ERROR", time.time(), type(exc).__name__)
        return text
    return f"{text.rstrip()}\n\n{block}" if text.strip() else block


def session_context(event: dict[str, Any]) -> str:
    """``build_session``, failing open to '' with an ERROR log line."""
    started = time.time()
    try:
        text = build_session(event)
    except Exception as exc:  # noqa: BLE001 - context never breaks a session
        common.log(SESSION_LABEL, "startup", "ERROR", started, type(exc).__name__)
        return ""
    common.log(SESSION_LABEL, "startup", "OK" if text else "SKIP", started, f"{len(text)} chars")
    return text


# --- every prompt -----------------------------------------------------------------------------


def prompt_hook_path() -> Path:
    """The engine's host-neutral prompt hook."""
    return hooks_dir() / "user-prompt-context-codex.py"


def build_prompt(event: dict[str, Any]) -> str:
    """Run the prompt hook for this member message; its context, or ''.

    Raises when the child cannot run, times out, exits non-zero or prints something that is not
    the expected JSON, so a broken delegate is never mistaken for "nothing to say".
    The child gets this process's own environment and interpreter (the bridge already runs on the
    scripts environment, so no second ``uv run``).
    """
    if is_automated(event):
        return ""
    hook = prompt_hook_path()
    if not hook.is_file():
        raise DelegateUnavailable("user-prompt-context-codex unavailable")
    prompt = event.get("prompt")
    payload = {
        "session_id": _str_field(event, "session_id"),
        "prompt": prompt if isinstance(prompt, str) else "",
    }
    try:
        proc = subprocess.run(  # fixed argv, no shell
            [sys.executable, str(hook)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            timeout=PROMPT_TIMEOUT_SECONDS,
            env=dict(os.environ),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise DelegateUnavailable("prompt hook timed out") from exc
    if proc.returncode != 0:
        raise DelegateUnavailable(f"prompt hook exit {proc.returncode}")
    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return ""
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError as exc:
        raise DelegateUnavailable("prompt hook printed non-JSON") from exc
    if not isinstance(parsed, dict):
        raise DelegateUnavailable("prompt hook printed non-object")
    specific = parsed.get("hookSpecificOutput")
    if not isinstance(specific, dict):
        raise DelegateUnavailable("prompt hook output missing hookSpecificOutput")
    text = specific.get("additionalContext")
    return text if isinstance(text, str) else ""


def prompt_context(event: dict[str, Any]) -> str:
    """``build_prompt``, failing open to '' with an ERROR log line."""
    started = time.time()
    try:
        text = build_prompt(event)
    except Exception as exc:  # noqa: BLE001 - context never breaks a member's message
        detail = str(exc) if isinstance(exc, DelegateUnavailable) else type(exc).__name__
        common.log(PROMPT_LABEL, "prompt", "ERROR", started, detail)
        return ""
    common.log(PROMPT_LABEL, "prompt", "OK" if text else "SKIP", started, f"{len(text)} chars")
    return text
