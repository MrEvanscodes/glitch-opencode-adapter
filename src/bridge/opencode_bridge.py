"""opencode_bridge.py - the one process the OpenCode plugin spawns per hook call.

Invocation (exactly how the plugin spawns it)::

    uv run --no-project --directory <root>/.claude/scripts python <this file> <pre|post|session|prompt|capture>

One JSON event arrives on stdin (a UTF-8 byte-order mark is tolerated).
The payloads, by mode:

- ``pre``     ``{tool_name, tool_input, session_dir, session_id, parent_id?, cwd?}``
- ``post``    ``{tool_name, session_id, result, error, status, session_dir}``
- ``session`` ``{session_id, session_dir, parent_id?, source?, refresh?}``
  (source: startup, resume, compact; default startup. refresh: bool, default false)
- ``prompt``  ``{session_id, prompt, session_dir, parent_id?}``
- ``capture`` ``{session_id, session_dir, parent_id?, reason}`` (reason: turn, compact, start, sweep)

The stdout contract (the plugin's own envelope, not a host's):

- nothing at all  -> judged and allowed / clean / no context / inert (not a Glitch session) /
  a chat staged or rightly left alone (``capture``)
- ``{"deny": {"kind": str, "member": str, "model": str}}``  (``pre`` only)
- ``{"note": str}``     (``post`` only: a possible secret was printed)
- ``{"context": str}``  (``prompt``)
- ``{"context": str, "date": "YYYY-MM-DD", "timezone": str}``  (``session``: the local build date
  the plugin compares days against, left out only if Glitch's clock cannot be read; and the IANA
  zone name of that clock, e.g. ``Europe/London``, left out when the clock cannot be read or its
  zone has no IANA name)
- ``{"error": str}``    a short safe label; the plugin treats it as an allow plus a visible failure
  (``capture`` answers with a ``capture-*`` label, see ``CAPTURE_ERRORS``)

At most ONE JSON document is written, with no trailing newline, and the exit code is ALWAYS 0.
Silence means only "judged and allowed" (or "outside Glitch"): any internal exception, a delegate
that will not load, a delegate that quietly cannot do its job, garbage stdin or an unknown mode
prints ``{"error": ...}`` instead.

The root is resolved from the event's ``session_dir`` (the session's start folder, which the plugin
takes from ``ctx.location.directory``), never from the tool's own cwd/workdir and never from this
process's cwd: a ``session_dir`` that sits in no Glitch folder makes the bridge inert, for every
mode.
The engine is put on ``sys.path`` only after that, so the bridge imports nothing of the engine
outside Glitch.

stdout carries JSON only.
While the delegates run, Python's ``sys.stdout`` AND file descriptor 1 both point at stderr, so a
stray ``print`` (or a child process inheriting the descriptor) can never corrupt the result; both
are restored just before the one ``emit``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import common

MODES = ("pre", "post", "session", "prompt", "capture")

# The log label per mode (the delegate modules own the same strings; repeated here so an import
# failure of the delegate module itself is still logged under the right label).
LABELS = {
    "pre": "opencode-pre",
    "post": "opencode-post",
    "session": "opencode-session",
    "prompt": "opencode-prompt",
    "capture": "opencode-capture",
}
BRIDGE_LABEL = "opencode-bridge"


def _label(exc: BaseException) -> str:
    """A safe, short label for an exception: never its payload-bearing message.

    ``context.DelegateUnavailable`` messages ARE short labels by contract, so they are kept;
    anything else is reduced to its class name.
    """
    try:
        import context

        if isinstance(exc, context.DelegateUnavailable) and str(exc):
            return str(exc)
    except Exception as inner:  # noqa: BLE001 - the label must never itself fail
        common.log(BRIDGE_LABEL, "unknown", "WARN", time.time(), f"label: {type(inner).__name__}")
    return type(exc).__name__


def _read_text(stdin: IO[bytes] | IO[str] | None) -> str | None:
    """All of stdin as text with a leading BOM removed, or None when it cannot be decoded."""
    stream: Any = sys.stdin if stdin is None else stdin
    try:
        raw = getattr(stream, "buffer", stream).read()
        if isinstance(raw, bytes):
            return raw.decode("utf-8-sig")
        return str(raw).removeprefix("﻿")
    except Exception:  # noqa: BLE001 - undecodable stdin is garbage, reported as an error
        return None


def _log_root_for_garbage() -> Path | None:
    """Where to log an error that arrived before any session folder was known.

    The process cwd (the plugin spawns with ``--directory <root>/.claude/scripts``), then this
    file's own folder. Used ONLY to log a malformed call; it never makes the bridge judge anything.
    """
    return common.resolve_root(os.getcwd()) or common.resolve_root(Path(__file__).resolve().parent)


def _session(event: dict[str, Any]) -> dict[str, Any] | None:
    import context

    started = time.time()
    text = context.build_session(event)  # raises on a broken delegate
    common.log(LABELS["session"], "startup", "OK" if text else "SKIP", started, f"{len(text)} chars")
    if not text:
        return None
    result: dict[str, Any] = {"context": text}
    try:
        result["date"] = context.session_date()
    except Exception as exc:  # noqa: BLE001 - a missing date costs a refresh, never the context
        common.log(LABELS["session"], "startup", "WARN", started, f"date: {type(exc).__name__}")
    try:
        zone = context.session_timezone()
    except Exception as exc:  # noqa: BLE001 - a missing zone leaves the plugin's own default
        common.log(LABELS["session"], "startup", "WARN", started, f"zone: {type(exc).__name__}")
        zone = None
    if zone:
        result["timezone"] = zone
    return result


def _prompt(event: dict[str, Any]) -> dict[str, Any] | None:
    import context

    started = time.time()
    text = context.build_prompt(event)  # raises on a broken delegate
    common.log(LABELS["prompt"], "prompt", "OK" if text else "SKIP", started, f"{len(text)} chars")
    return {"context": text} if text else None


def _pre(event: dict[str, Any]) -> dict[str, Any] | None:
    import pretool

    return pretool.judge(event)  # logs its own DENY / ERROR lines


def _post(event: dict[str, Any]) -> dict[str, Any] | None:
    import posttool

    result = posttool.scan_or_error(event)  # logs its own FLAG / ERROR lines
    return result or None


# The capture outcome -> bridge answer mapping (``capture.capture_payload`` returns the outcome).
# SILENCE: the chat was staged, or rightly left alone, or the next turn or start sweep picks it
# up with nothing for anyone to do: ``staged``, ``too short``, ``unchanged``,
# ``before first sweep``, ``cold start``, ``not found``, ``deferred``, every ``skipped:*``, and
# the two transient ones, ``unreachable`` (OpenCode did not answer: it is often just closing) and
# ``busy`` (another capture holds the ledger).
# AN ERROR LABEL, so the plugin records a visible failure: a chat OpenCode answered for that could
# not be kept (``refused``: a shape the reader does not know; ``copy failed`` / ``stub failed``:
# nothing staged), OpenCode unreachable ``capture.UNREACHABLE_NOTICE_AFTER`` times in a row
# (``unreachable streak``, once per streak) and an internal fault (``error``).
# An outcome this table does not know is loud too (``capture-unknown-outcome``): silence must only
# ever mean "fine". A bad payload is ``capture-bad-payload`` (nothing read or written).
CAPTURE_SILENT = frozenset({
    "staged", "too short", "unchanged", "before first sweep", "cold start", "not found",
    "deferred", "unreachable", "busy",
})
CAPTURE_ERRORS = {
    "refused": "capture-refused",
    "copy failed": "capture-copy-failed",
    "stub failed": "capture-stub-failed",
    "unreachable streak": "capture-unreachable",
    "error": "capture-error",
}
CAPTURE_BAD_PAYLOAD = "capture-bad-payload"
CAPTURE_UNKNOWN = "capture-unknown-outcome"


def capture_answer(outcome: str) -> dict[str, Any] | None:
    """The bridge's answer for one capture outcome label: None (silence) or ``{"error": label}``."""
    if outcome in CAPTURE_SILENT or outcome.startswith("skipped:"):
        return None
    return {"error": CAPTURE_ERRORS.get(outcome, CAPTURE_UNKNOWN)}


def _capture(event: dict[str, Any]) -> dict[str, Any] | None:
    import importlib

    import capture

    # The session is in a Glitch root (resolved above), so its engine must load: without this a
    # root with no engine would read as capture's "outside" skip, a silence that hides a break.
    importlib.import_module("config")
    started = time.time()
    try:
        outcome = capture.capture_payload(event)  # capture_one logs its own outcome line
    except ValueError:
        # Only the label travels or is logged, never the message or the payload.
        common.log(LABELS["capture"], "capture", "ERROR", started, CAPTURE_BAD_PAYLOAD)
        return {"error": CAPTURE_BAD_PAYLOAD}
    return capture_answer(str(outcome))


_DISPATCH: dict[str, Callable[[dict[str, Any]], dict[str, Any] | None]] = {
    "pre": _pre, "post": _post, "session": _session, "prompt": _prompt, "capture": _capture,
}


def _tool_of(mode: str, event: dict[str, Any]) -> str:
    if mode in ("pre", "post"):
        tool = event.get("tool_name", event.get("tool"))
        return tool if isinstance(tool, str) and tool else "unknown"
    if mode == "capture":
        return "capture"
    return "startup" if mode == "session" else "prompt"


def run(argv: list[str], stdin: IO[bytes] | IO[str] | None = None) -> dict[str, Any] | None:
    """The one result object for this call, or None for silence. Never raises."""
    started = time.time()
    mode = argv[1] if len(argv) > 1 else ""

    text = _read_text(stdin)
    event = common.read_event(text) if text is not None else {}
    if not event:
        root = _log_root_for_garbage()
        if root is not None:
            common.engine_on_path(root)
        common.log(LABELS.get(mode, BRIDGE_LABEL), "unknown", "ERROR", started, "bad-event")
        return {"error": "bad-event"}

    session_dir = event.get("session_dir")
    if not isinstance(session_dir, str) or not session_dir.strip():
        root = _log_root_for_garbage()
        if root is not None:
            common.engine_on_path(root)
        common.log(LABELS.get(mode, BRIDGE_LABEL), "unknown", "ERROR", started, "no-session-dir")
        return {"error": "no-session-dir"}

    root = common.resolve_root(session_dir)
    if root is None:
        return None  # inert: this session is not in a Glitch folder

    common.engine_on_path(root)  # BEFORE any delegate module is imported

    if mode not in MODES:
        common.log(BRIDGE_LABEL, "unknown", "ERROR", started, "bad-mode")
        return {"error": "bad-mode"}

    try:
        return _DISPATCH[mode](event)
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - fail open, loudly, with a label only
        # pretool.judge / posttool.scan_or_error never raise (they log their own errors), so an
        # exception here is the delegate MODULE failing to import, or a context build failing:
        # neither has been logged yet.
        label = _label(exc)
        common.log(LABELS[mode], _tool_of(mode, event), "ERROR", started, label)
        return {"error": label}


class _StdoutFence:
    """Points ``sys.stdout`` and file descriptor 1 at stderr until ``release``."""

    def __init__(self) -> None:
        self.saved_stream = sys.stdout
        self.saved_fd: int | None = None
        try:
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001 - a closed stream must not stop the fence
            common.log(BRIDGE_LABEL, "unknown", "WARN", time.time(), f"flush: {type(exc).__name__}")
        try:
            self.saved_fd = os.dup(1)
            os.dup2(2, 1)
        except Exception:  # noqa: BLE001 - no real descriptor (embedded/test stream): Python-level only
            self.saved_fd = None
        sys.stdout = sys.stderr

    def release(self) -> None:
        try:
            sys.stderr.flush()
        except Exception as exc:  # noqa: BLE001 - a closed stream must not stop the release
            common.log(BRIDGE_LABEL, "unknown", "WARN", time.time(), f"flush: {type(exc).__name__}")
        if self.saved_fd is not None:
            try:
                os.dup2(self.saved_fd, 1)
            finally:
                os.close(self.saved_fd)
                self.saved_fd = None
        sys.stdout = self.saved_stream


def main(argv: list[str] | None = None, stdin: IO[bytes] | IO[str] | None = None) -> int:
    """Run one call and write at most one JSON document to stdout. Always returns 0."""
    fence = _StdoutFence()
    try:
        try:
            result = run(list(sys.argv if argv is None else argv), stdin)
        except BaseException as exc:  # noqa: BLE001 - never silence: silence means "allowed"
            result = {"error": type(exc).__name__}
    finally:
        fence.release()
    if result:
        try:
            common.emit(result)
        except Exception:  # noqa: BLE001 - unserialisable: emit wrote nothing, so say so
            common.emit({"error": "emit-failed"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
