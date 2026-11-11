"""common.py - the dialect-free seam every OpenCode bridge module shares.

What lives here, and why it is small:

- ``resolve_root(start)``: which Glitch folder (if any) an OpenCode session belongs to.
  The answer is the first folder, walking up from ``start``, that holds BOTH ``CLAUDE.md`` and
  ``.claude/hooks/write-guard.py``.
  ``CLAUDE.md`` alone is not enough: every workspace under ``workspaces/`` carries a stamped
  ``CLAUDE.md`` of its own, and the walk must go past it to the real root.
- ``engine_on_path(root)``: put ``<root>/.claude/scripts`` on ``sys.path`` so the engine's own
  modules (``codex_shared``, ``shared``, ``config``) import exactly as the sibling adapters do.
- ``read_event()``: one event off stdin, decoded ``utf-8-sig``, through the engine's own tolerant
  parser once the engine is on the path.
- ``log(...)``: the hook execution log, best-effort, never raising.
- ``emit(obj)``: the ONE place the bridge writes stdout, and only ever valid JSON.

This module imports nothing from the engine at import time.
The bridge resolves the root first and only then calls ``engine_on_path``, so importing this file
must succeed on a machine (or from a folder) where no engine is reachable at all.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# The two markers that together mean "this folder is a Glitch root".
ROOT_MARKER_FILE = "CLAUDE.md"
ROOT_MARKER_GUARD = os.path.join(".claude", "hooks", "write-guard.py")


def is_glitch_root(folder: str | os.PathLike[str]) -> bool:
    """True when ``folder`` holds both root markers."""
    base = os.fspath(folder)
    return os.path.isfile(os.path.join(base, ROOT_MARKER_FILE)) and os.path.isfile(
        os.path.join(base, ROOT_MARKER_GUARD)
    )


def resolve_root(start: str | os.PathLike[str] | None) -> Path | None:
    """The Glitch root that ``start`` sits in, or None when it sits in none.

    ``start`` is realpathed first, so a symlinked working folder resolves to the folder it points
    at (OpenCode itself reports the realpath, e.g. ``/private/var/...`` for ``/var/...``).
    The walk is lexical from there: a start folder that no longer exists still resolves to the
    Glitch root above it, because a guard that switched itself off for a deleted subfolder would
    fail in the unsafe direction.
    An empty or missing ``start`` resolves to None and never falls back to this process's own
    working folder, which is the bridge's folder, not the session's.
    Total: any error is None.
    """
    try:
        if start is None:
            return None
        raw = os.fspath(start)
        if not raw.strip():
            return None
        current = os.path.realpath(os.path.expanduser(raw))
        while True:
            if is_glitch_root(current):
                return Path(current)
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
    except Exception:  # noqa: BLE001 - an unresolvable start is "not in Glitch"
        return None


def scripts_dir(root: str | os.PathLike[str]) -> Path:
    """``<root>/.claude/scripts``: the engine's importable modules."""
    return Path(root) / ".claude" / "scripts"


def engine_on_path(root: str | os.PathLike[str]) -> Path:
    """Put ``<root>/.claude/scripts`` first on ``sys.path`` (once) and return it."""
    path = scripts_dir(root)
    entry = str(path)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return path


def _fallback_parse(text: str) -> dict[str, Any]:
    """The engine parser's own shape, for the one case the engine cannot be imported.

    Strict JSON first, then the lone-backslash repair Windows paths need.
    """
    try:
        if not text.strip():
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = json.loads(re.sub(r'(?<!\\)\\(?!["\\])', r"\\\\", text))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - unreadable is silence
        return {}


def _read_stdin() -> str:
    """All of stdin as text, a leading byte-order mark removed."""
    stdin_bytes = getattr(sys.stdin, "buffer", None)
    if stdin_bytes is not None:
        data: bytes = stdin_bytes.read()
        return data.decode("utf-8-sig")
    return sys.stdin.read().removeprefix("﻿")


def read_event(raw: str | None = None) -> dict[str, Any]:
    """One event (stdin when ``raw`` is None), or ``{}`` for anything unreadable.

    Delegates to ``codex_shared.read_event`` (call ``engine_on_path`` first), which decodes
    ``utf-8-sig`` and repairs unescaped Windows backslashes.
    ``raw`` text has a leading BOM removed here too, because the engine's parser only strips it
    on the stdin path.
    If the engine cannot be imported the same parse runs locally, so a missing engine never turns
    a readable payload into silence.
    """
    try:
        text = _read_stdin() if raw is None else raw.removeprefix("﻿")
    except Exception:  # noqa: BLE001 - undecodable stdin is silence, never a crash
        return {}
    try:
        import codex_shared

        result = codex_shared.read_event(text)
        return result if isinstance(result, dict) else {}
    except Exception:  # noqa: BLE001 - engine absent: parse it ourselves
        return _fallback_parse(text)


def log(label: str, tool: str, status: str, started: float, detail: str = "") -> None:
    """Best-effort line in the engine's hook execution log; never raises, never gates.

    ``started`` is the ``time.time()`` the call began; the log records the elapsed seconds.
    """
    try:
        from shared import log_hook_execution

        log_hook_execution(label, tool, status, time.time() - started, detail)
    # This IS the log: a failure here (engine absent, or its writer raising anything) has nowhere
    # to be logged, and logging it would recurse. Fail open silently so a hook never crashes on
    # its own bookkeeping; ``Exception`` is the narrowest true type (import or arbitrary write error).
    except Exception:  # noqa: BLE001, S110 - an unlogged logger failure cannot be logged
        pass


def emit(obj: Any) -> None:
    """Write ``obj`` to stdout as one JSON document, and nothing else.

    The object is serialised in full BEFORE anything is written, so a value that cannot be
    serialised raises (for the caller to log) and leaves stdout untouched: the plugin parses this
    stream, and a half-written document would be read as garbage.
    ``allow_nan=False`` keeps NaN/Infinity out, which are not valid JSON.
    """
    text = json.dumps(obj, allow_nan=False)
    sys.stdout.write(text)
    sys.stdout.flush()
