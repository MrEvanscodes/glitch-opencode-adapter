"""posttool.py - the post-tool secret scan for OpenCode's ``tool.execute.after``.

ONE SCAN, A FOURTH DIALECT.
The plugin hands the bridge ``{tool_name, session_id, result, error, status}``, cut from the
measured ``execute.after`` event (``docs/api-findings.md`` item 6:
``{tool, sessionID, agent, messageID, id, input, status, result?, error?}``).
This module pulls the tool's printed text out of that payload and DELEGATES the decision to the
Claude guard's own ``post-tool-secret-guard.process``, exactly as ``post-tool-secret-cursor.py``
does, so the detectors, the fingerprint-only catch diary and the member-facing note stay in the
one module that owns them.
It never blocks (the tool has already run) and it never reads ``input``: the member's own command
line belongs to the inline-key guard, before the command runs.

Where the text comes from (measured shapes, ``spike/evidence/execute-after.jsonl``):

- ``result.content[].text``: what the model actually sees, for every tool.
- ``result.output``: the tool-specific object. A string is taken whole; an object or list is read
  by the Codex adapter's tolerant ``output_text`` (its key set includes ``output``, ``content``,
  ``stdout``, ``stderr``), plus a ``text`` field on list items (grep matches carry ``text``).
  Shell's ``{exit, truncated, output, status}`` yields its ``output``; read's file object yields
  its raw ``content`` (not line-numbered, so a key split by the line prefix is still whole);
  webfetch and subagent yield their ``output``.
- ``error``: on ``status:"error"`` a ``Tool.Error`` object; its ``message`` / ``reason`` strings
  are scanned (a tool's own error text can quote what it printed).

Identical strings are scanned once, so one key printed in both ``content`` and ``output`` is one
catch in the diary, not two.

DECISION, scanned tools: EVERY tool is scanned, not only ``shell``.
The measured result of read, grep, glob, webfetch, subagent, execute and the Code Mode inner tools
all carry text that reaches the model, and a key in a file the model reads is as exposed as one a
command printed.
The write-side tools (write, edit, patch) are scanned too: their results echo diffs and targets,
which costs nothing when clean and catches a key the model itself put into a file.
Only text fields are read, never ids, paths-as-keys or metadata, so session ids and URIs do not
become false hits.

QUESTION (CP0, answered): can ``execute.after`` rewrite the result before the model sees it?
Measured yes (``after-rewrite``: assigning ``ev.result.output`` / ``ev.result.content`` put
``PROBE-REDACTED`` in front of the model).
So redaction would beat Claude Code's parity (which can only add a note).
It is NOT built here: this module returns the note only, and the plugin appends it to
``result.content`` the way the host's own plugins do.

Failure contract (for the entrypoint author, ``opencode_bridge.py``):

- ``scan(event)`` returns the note or ``""``.
  It RAISES ``DelegateUnavailable`` when the guard module will not load, and
  ``ScannerUnavailable`` when it loads but its ``secret_scan`` import failed
  (``post-tool-secret-guard`` sets ``secret_scan = None`` and then ``process`` returns ``None``
  quietly, which would read as "clean").
  Both subclass ``PosttoolUnavailable`` and carry ``.label`` (``"delegate-unavailable"`` /
  ``"scanner-unavailable"``); any other exception propagates as itself.
- ``scan_or_error(event)`` is the fail-open wrapper the entrypoint should call: it never raises,
  logs ONE ``ERROR`` line (exception label only) and returns ``{"error": <label>}``
  (``{"error": "scanner-unavailable"}`` for the case above, the exception class name otherwise),
  ``{"note": <text>}`` on a catch, or ``{}`` when clean.
  The entrypoint emits that dict as-is (nothing when empty) and must not log the error again.

Logging: label ``opencode-post``, the tool name and an exception LABEL only.
The payload IS the scanned output, so no exception text and no payload is ever logged.
"""

from __future__ import annotations

import time
from typing import Any

import common

LOG_LABEL = "opencode-post"

# Keys of an ``error`` object whose string values are human text worth scanning.
_ERROR_TEXT_KEYS = ("message", "reason", "error", "output")


class PosttoolUnavailable(RuntimeError):
    """The scan could not be judged; the entrypoint turns this into ``{"error": self.label}``."""

    label = "posttool-unavailable"


class DelegateUnavailable(PosttoolUnavailable):
    """``post-tool-secret-guard`` (or the Codex output reader) would not load."""

    label = "delegate-unavailable"


class ScannerUnavailable(PosttoolUnavailable):
    """The guard loaded, but its ``secret_scan`` import failed: it would silently pass."""

    label = "scanner-unavailable"


def _load_delegates() -> tuple[Any, Any]:
    """The guard and the Codex output reader, or a specific exception. Engine must be on path."""
    import codex_shared

    guard = codex_shared.load_hook_module("post-tool-secret-guard")
    codex_post = codex_shared.load_hook_module("post-tool-secret-codex")
    if guard is None or codex_post is None:
        raise DelegateUnavailable("post-tool secret delegate unavailable")
    # The guard's module-level optional import: `secret_scan = None` on failure.
    if getattr(guard, "secret_scan", None) is None:
        raise ScannerUnavailable("post-tool-secret-guard has no secret_scan")
    return guard, codex_post


def _tool_name(event: dict[str, Any]) -> str:
    raw = event.get("tool_name", event.get("tool"))
    return raw if isinstance(raw, str) and raw else "unknown"


def _error_texts(value: Any, depth: int, out: list[str]) -> None:
    if isinstance(value, str):
        if value:
            out.append(value)
        return
    if depth <= 0 or not isinstance(value, dict):
        return
    for key in _ERROR_TEXT_KEYS:
        if key in value:
            _error_texts(value[key], depth - 1, out)


def printed_text(event: dict[str, Any], codex_post: Any) -> str:
    """Every printed text the tool result carries, deduplicated, joined. '' when none.

    Reads ``result`` and ``error`` only; ``input`` is never touched.
    """
    found: list[str] = []
    result = event.get("result")
    if isinstance(result, str):
        found.append(result)
    elif isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    found.append(part["text"])
        elif isinstance(content, str):
            found.append(content)
        output = result.get("output")
        if isinstance(output, str):
            found.append(output)
        elif output is not None:
            found.append(str(codex_post.output_text({"output": output})))
            if isinstance(output, list):
                for item in output:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        found.append(item["text"])
    _error_texts(event.get("error"), 4, found)

    seen: set[str] = set()
    unique: list[str] = []
    for text in found:
        if text and text.strip() and text not in seen:
            seen.add(text)
            unique.append(text)
    return "\n".join(unique)


def scan(event: dict[str, Any]) -> str:
    """The guard's note for this tool result, or ``""``. Raises ``PosttoolUnavailable`` subclasses.

    The delegates are checked BEFORE the text, so a broken scanner is reported on every call
    rather than only on calls that happened to print something.
    """
    guard, codex_post = _load_delegates()
    text = printed_text(event, codex_post)
    if not text.strip():
        return ""
    session = event.get("session_id", event.get("sessionID"))
    verdict = guard.process(
        {"tool_name": "Bash", "tool_response": {"stdout": text}, "session_id": session}
    )
    if not isinstance(verdict, dict):
        return ""
    return str((verdict.get("hookSpecificOutput") or {}).get("additionalContext") or "")


def scan_or_error(event: dict[str, Any]) -> dict[str, str]:
    """Fail-open wrapper: ``{}``, ``{"note": ...}`` or ``{"error": <label>}``. Never raises."""
    started = time.time()
    tool = "unknown"
    try:
        tool = _tool_name(event)
        note = scan(event)
    except PosttoolUnavailable as exc:
        common.log(LOG_LABEL, tool, "ERROR", started, exc.label)
        return {"error": exc.label}
    except Exception as exc:  # noqa: BLE001 - fail open; the LABEL only, the payload is a secret
        common.log(LOG_LABEL, tool, "ERROR", started, type(exc).__name__)
        return {"error": type(exc).__name__}
    if note:
        common.log(LOG_LABEL, tool, "FLAG", started, "delegate flagged a possible secret")
        return {"note": note}
    return {}
