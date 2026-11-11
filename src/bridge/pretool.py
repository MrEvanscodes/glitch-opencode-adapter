"""pretool.py - OpenCode's ``execute.before`` tool calls, judged by Glitch's own guards.

Nothing is decided here.
This module translates the bridge payload the plugin sends for one OpenCode tool call into the
shape an existing Glitch guard already reads, and DELEGATES, so every rule stays in the one module
that owns it (the pattern is ``.claude/hooks/write-guard-cursor.py``; only the dialect differs).

The bridge payload (built by the plugin from the measured ``execute.before`` event, see
``docs/api-findings.md`` item 2):

    {"tool_name": "<opencode tool id>", "tool_input": {...} | "<json string>",
     "session_dir": "<the session's start folder>", "session_id": "ses_...",
     "parent_id": "<parent session id, only for a sub-agent>", "cwd": "<optional>"}

The OpenCode spellings ``tool`` / ``input`` / ``sessionID`` / ``parentID`` are accepted too.

The routes, per measured tool id:

- ``shell {command, workdir}`` -> ``write-guard-codex.decide`` as ``Bash``.
  The tool event carries NO cwd (measured), so an absent ``workdir`` becomes the session folder,
  never this process's cwd, and a relative one is resolved against the session folder by the
  guard itself (``_effective_cwd``).
  A ``workdir`` outside Glitch is judged THERE: a Brain-destroying command aimed at Glitch from a
  ``/tmp`` workdir is still judged on the paths it names.
- ``read {path}`` / ``grep {pattern, path}`` / ``glob {pattern, path}`` -> ``read-guard.decide``
  as ``Read`` / ``Grep`` / ``Glob``.
  ``read-guard.normalise`` resolves a relative path against the Glitch root, not the session, so
  every path (and a path-less Glob's pattern) is made absolute against the session folder here
  first.
  The stateful out-of-tree pause is reached exactly ONCE per tool call: a second call would spend
  the member's one-time pass on the first and pause again on the second.
- ``patch {patchText}`` -> ``write-guard-codex.decide`` as ``apply_patch`` with the V4A text in
  ``tool_input.command`` (measured: OpenCode's patch grammar IS V4A).
- ``edit {path, oldString, newString, replaceAll}`` -> the write guard's shape-read arm under its
  own name, with ``oldString``/``newString``/``replaceAll`` renamed to the guard's
  ``old_string``/``new_string``/``replace_all``.
  Without the rename the settings-unlock content scan (control 8) never sees an edit's new text.
- ``write {path, content}`` and every other tool id (``webfetch``, ``subagent``, ``skill``,
  ``question``, ``websearch``, ``execute``, a Code Mode inner ``opencode_<name>``, an MCP tool,
  a future built-in) -> the shape-read arm under its own name, arguments passed through.
  A ``directory`` argument (``opencode_worktree_remove`` / ``_create``, ``opencode_session_move``)
  is also offered as ``path``, which is a key the arm reads; their names already carry a write or
  destructive verb the arm recognises.
  ``experimental_image_generate`` writes a file at ``path`` but its name carries no write verb and
  its payload no content field, so the arm would read it as harmless: it is delegated as ``Write``.

A sub-agent (``parent_id`` non-empty) is marked with ``agent_id`` on the inner event, the field
``read-guard._is_sub_agent`` reads, so a sub-agent is never taught "always allow" and never told
to ask a member who is not in its window.
No inner event ever carries ``cursor_version`` (``read-guard.main`` skips such events).

The write guard snapshots its ``GLITCH_ALLOW_*`` launch flags at IMPORT from its own process env,
which is why the bridge runs as one fresh process per call: never make this long-lived.

Public API (what ``opencode_bridge.py pre`` calls, after ``common.engine_on_path(root)``):

- ``translate(event) -> (route, inner) | None``
- ``decide(event) -> (kind, member_words, model_words) | None``; raises when a delegate will not
  load.
- ``judge(event) -> {"deny": {...}} | {"error": label} | None``: the fail-open wrapper.
  ``None`` means judged and allowed (the bridge prints nothing); ``error`` is an allow that the
  plugin records visibly; every deny and every error is logged under ``opencode-pre``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import common

LABEL = "opencode-pre"

# OpenCode tool id -> the read-guard's own tool name.
_READ_TOOLS = {"read": "Read", "grep": "Grep", "glob": "Glob"}

# OpenCode edit argument names -> the write guard's (``_CONTENT_KEYS`` / ``introduces_allow_flag``).
_EDIT_KEY_MAP = {"oldString": "old_string", "newString": "new_string", "replaceAll": "replace_all"}

# Tools that write a file at ``path`` but whose NAME the shape-read arm would not recognise.
_WRITE_ALIASES = {"experimental_image_generate": "Write"}

_DRIVE = re.compile(r"^[A-Za-z]:/")


def _text(value: Any) -> str:
    """A non-blank string, stripped, or ``""``."""
    return value.strip() if isinstance(value, str) and value.strip() else ""


def _first(event: dict[str, Any], *keys: str) -> Any:
    """The first present, non-None value among ``keys``."""
    for key in keys:
        if event.get(key) is not None:
            return event[key]
    return None


def _tool_input(event: dict[str, Any]) -> dict[str, Any]:
    """The tool's arguments as a dict: a JSON string is decoded, anything unreadable is ``{}``."""
    ti = _first(event, "tool_input", "input")
    if isinstance(ti, str):
        try:
            ti = json.loads(ti)
        except Exception:  # noqa: BLE001 - an undecodable payload has no shape to read
            return {}
    return dict(ti) if isinstance(ti, dict) else {}


def _is_absolute(path: str) -> bool:
    """Absolute on any OS (POSIX root, drive letter, UNC) or home-relative, judged on spelling."""
    slashed = path.replace("\\", "/")
    return slashed.startswith(("/", "~")) or _DRIVE.match(slashed) is not None


def absolute(path: str, base: str) -> str:
    """``path`` made absolute against ``base`` (the session folder), on spelling alone.

    An already-absolute or ``~`` path is returned as given; with no usable ``base`` the path is
    returned as given too (the guard then resolves it against the Glitch root, its own fallback).
    Never touches the disk.
    """
    raw = path.strip()
    if not raw or _is_absolute(raw) or not base or not _is_absolute(base):
        return path
    return base.replace("\\", "/").rstrip("/") + "/" + raw


def session_base(event: dict[str, Any]) -> str:
    """The folder relative paths and an absent shell ``workdir`` resolve against.

    The session's start folder (``session_dir``); the optional ``cwd`` only when no session folder
    was sent; ``""`` when neither is there, which every guard reads as "the Glitch root".
    Never this process's own cwd.
    """
    return _text(event.get("session_dir")) or _text(event.get("cwd"))


def translate(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The bridge payload as (route, the delegate's own event), or None for nothing to judge.

    Route is ``write`` (``write-guard-codex.decide``) or ``read`` (``read-guard.decide``).
    """
    if not isinstance(event, dict):
        return None
    tool = _first(event, "tool_name", "tool")
    if not isinstance(tool, str) or not tool.strip():
        return None
    tool = tool.strip()
    ti = _tool_input(event)
    base = session_base(event)
    session = _text(str(_first(event, "session_id", "sessionID") or ""))
    parent = _first(event, "parent_id", "parentID")

    def inner(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "tool_name": name, "tool_input": tool_input, "cwd": base, "session_id": session,
        }
        if parent:  # a sub-agent: no member in its window to answer a question
            out["agent_id"] = str(parent)
        return out

    if tool == "shell":
        command = ti.get("command")
        workdir = _text(ti.get("workdir")) or base
        return "write", inner("Bash", {"command": command, "workdir": workdir})

    if tool in _READ_TOOLS:
        name = _READ_TOOLS[tool]
        mapped = dict(ti)
        path = _text(mapped.get("path"))
        if path:
            mapped["path"] = absolute(path, base)
        elif name == "Glob" and _text(mapped.get("pattern")):
            # A path-less Glob walks the session folder: its pattern's literal prefix is the root
            # the read-guard judges, so a relative pattern is anchored there.
            mapped["pattern"] = absolute(_text(mapped.get("pattern")), base)
        return "read", inner(name, mapped)

    if tool == "patch":
        return "write", inner("apply_patch", {"command": ti.get("patchText")})

    mapped = {_EDIT_KEY_MAP.get(key, key): value for key, value in ti.items()}
    if _text(mapped.get("directory")) and not _text(mapped.get("path")):
        mapped["path"] = mapped["directory"]
    return "write", inner(_WRITE_ALIASES.get(tool, tool), mapped)


def _load(stem: str) -> Any:
    """A hyphenated engine hook module, or raise: a delegate that will not load is an ERROR."""
    import codex_shared

    module = codex_shared.load_hook_module(stem)
    if module is None:
        raise RuntimeError(f"{stem} unavailable")
    return module


def decide(event: dict[str, Any]) -> tuple[str, str, str] | None:
    """(kind, member words, model words) for a deny, or None to allow silently.

    Raises when a delegate will not load, so the caller logs it as the error it is.
    """
    routed = translate(event)
    if routed is None:
        return None
    route, inner = routed

    if route == "write":
        hit = _load("write-guard-codex").decide(inner)
        return None if hit is None else (str(hit[0]), str(hit[1]), str(hit[1]))

    guard = _load("read-guard")
    result = guard.decide(inner)  # the ONE call per tool call (stateful pause)
    if result is None:
        return None
    out = result.get("hookSpecificOutput") or {}
    reason = str(out.get("permissionDecisionReason") or "")
    if not reason:
        return None
    # The read-guard keeps the member's words and the model's relay contract apart; OpenCode's
    # thrown error reaches the model, so the model words carry both.
    contract = str(out.get("additionalContext") or "")
    kind = guard._detail(result) if hasattr(guard, "_detail") else "read-outside"
    return str(kind), reason, f"{reason}\n\n{contract}" if contract else reason


def judge(event: dict[str, Any]) -> dict[str, Any] | None:
    """The fail-open verdict the bridge prints: a deny object, an error object, or None (allow).

    Never raises.
    Any exception (a delegate that will not load included) is logged as an ERROR line and comes
    back as ``{"error": "<ExceptionName>"}``, which the plugin treats as an allow plus a visible
    failure note.
    """
    started = time.time()
    tool = "unknown"
    try:
        if isinstance(event, dict):
            tool = str(_first(event, "tool_name", "tool") or "unknown")
        hit = decide(event)
    except Exception as exc:  # noqa: BLE001 - fail open on this module's own errors, loudly
        common.log(LABEL, tool, "ERROR", started, type(exc).__name__)  # label only: text may echo a command
        return {"error": type(exc).__name__}
    if hit is None:
        return None
    kind, member, model = hit
    common.log(LABEL, tool, "DENY", started, kind)
    return {"deny": {"kind": kind, "member": member, "model": model}}
