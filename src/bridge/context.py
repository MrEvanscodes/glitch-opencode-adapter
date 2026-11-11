"""context.py - memory-in for OpenCode: the session-start context and the per-prompt context.

Two entry points, each delegating to the engine's own owner so nothing is re-decided here:

- ``session_context(event)``: the same session start a Codex or Cursor member meets.
  It calls ``session-start-context-codex.build_context`` with the two parts that belong to the
  TOOL rather than the day: OpenCode's own keeper wording (its ``subagent`` tool, measured in
  ``docs/api-findings.md`` item 8) and ``capture_notice=lambda: ""``.
  That empty callable is load-bearing: passing ``None`` would run the Codex transcript sweep from an
  OpenCode session, and phase 1 captures nothing.
  The identity block it carries is ``codex_identity.assemble()``: the PERSONA block of SOUL.md
  (``persona.extract_persona()``, minus its pinned exclusions, not the whole file), USER.md,
  MEMORY.md, REFLEXES.md and the trigger-map digest.
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
    except Exception as exc:  # noqa: BLE001 - re-raised as one labelled kind
        raise DelegateUnavailable("session-start-context-codex unavailable") from exc
    return module


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

        def directive() -> str:
            return str(codex_session.keeper_directive(OPENCODE_KEEPER_DIRECTIVE))

    inner = {"session_id": _str_field(event, "session_id"), "source": "startup"}
    return str(codex_session.build_context(
        inner,
        claude_hook,
        directive=directive,
        capture_notice=lambda: "",  # mandatory: None would run the Codex transcript sweep
    ))


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
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
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
