"""chatcache.py - the OpenCode chat copy: one Claude-shaped JSONL file per OpenCode session.

OpenCode keeps its chats in its own store, in its own shape.
The engine's memory read (``transcript_filter.filter_transcript``) already knows Claude Code's
transcript shape, so the bridge writes each OpenCode chat out as a Claude-shaped copy that the
engine's ``ClaudeAdapter`` reads with no new adapter, and the flush stub points at that copy.

Public API:

- ``state_roots()``: the three folders the capture code writes under (pending queue, state, data),
  resolved AT CALL TIME.
  ``GLITCH_OPENCODE_CAPTURE_STATE`` is a test-only door: an absolute folder strictly inside the
  system temp folder, or the call is refused (``ValueError``); honoured, it logs a loud WARN line.
  Unset, the answer is ``config.PENDING_DIR`` / ``config.STATE_DIR`` / ``config.DATA_DIR``, read
  off the module at call time so a patched ``config`` is always honoured.
- ``chats_dir()`` / ``copy_path(session_id)``: ``<data>/opencode-chats/<session_id>.jsonl``.
  The id is used verbatim (no case folding); an id that could leave the folder, or that starts
  with ``-`` (OpenCode's CLI would read it as an option), is refused.
- ``write(session_id, turns, started_at)`` / ``write_copy(...)``: the whole copy, rewritten
  atomically, mode 0o600 in a 0o700 folder (POSIX), every text through the secret redactor.
  If the redactor cannot be imported or raises, the write is REFUSED (``CacheRefused``): nothing is
  written and any previous copy stays as it was.
  A chat copy unredacted is worse than no copy.
- ``prune(keep_ids, source_reachable=...)``: removes copies whose session no longer exists, and
  only when the OpenCode store was actually reachable (an unreachable store reads like an empty
  one, and must never empty the folder).
- ``restore_text(text, escapes)``: undoes the neutralisation below.

THE READER'S STRIPS, AND THE NEUTRALISATION (counted, reversible, never silent).
``ClaudeAdapter`` treats some member text as harness-injected, because in a real Claude Code
transcript it is: it excises complete ``<system-reminder>...</system-reminder>`` spans, turns any
text containing ``<command-name>`` into a reconstructed command line (or drops it), and drops text
that starts (after leading whitespace) with an injected, relay or interrupt prefix
(``<command-message>``, ``<bash-stdout>``, ``<hook-``, ``Caveat: The messages below...``,
``[Request interrupted`` and the rest).
In an OpenCode chat none of that is injected by a harness: it is the member's own typing, and the
memory read must see it.
So, for MEMBER text only, one backslash is inserted right after the first character of each
trigger the reader would act on:

- every ``<system-reminder>`` opening tag, but only when the text holds at least one complete span
  (a lone mention is left verbatim: the reader does not touch it);
- every ``<command-name>``;
- the leading prefix, when the text starts with one.

``<command-message>`` becomes ``<\\command-message>``, and so on: a visible, zero-width-free
marker that a model reads as "escaped".
The offsets of the inserted backslashes (in the WRITTEN text) are stored on the event under
``glitchEscapes``, a key the Claude reader ignores, so ``restore_text`` gives back the member's
exact text.
Ordinary text, code fences and a lone ``<system-reminder>`` mention get no escape at all and are
written byte-identical (after redaction).
After building the copy, every event is put through the real ``ClaudeAdapter`` in memory, and any
turn it would still drop or change (for instance a whitespace-only turn, or a trigger a newer
engine added) is counted as ``reader_altered`` and logged; it is never hidden.
The trigger lists are read from the engine's own ``transcript_filter`` so the two cannot drift
apart unnoticed: if a name is missing the escape for it is skipped, and the in-memory check counts
whatever the reader then does.

Logging carries labels and counts only, never chat text.
This module imports nothing from the engine at import time (the bridge puts the engine on the path
after resolving the root); each engine module is imported when it is needed.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import common

LABEL = "opencode-chatcache"
CAPTURE_STATE_ENV = "GLITCH_OPENCODE_CAPTURE_STATE"
CHATS_FOLDER = "opencode-chats"
COPY_SUFFIX = ".jsonl"
REDACT_SOURCE = "opencode-chat-cache"
ESCAPES_KEY = "glitchEscapes"
ESCAPE_CHAR = "\\"

FILE_MODE = 0o600
DIR_MODE = 0o700

# The namespace the per-turn uuids hang off: stable across rewrites, so a re-written copy names
# every old turn the same way.
_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "glitch:opencode-chat-cache")

_ROLES = {"user": "user", "assistant": "assistant"}

# Characters an id may never hold: path separators (both, on every OS), the drive colon (on
# Windows ``C:x`` joined onto a folder is drive-relative and leaves it), and NUL.
_FORBIDDEN_ID_CHARS = ("/", "\\", ":", "\0")


class CacheRefused(RuntimeError):
    """The copy could not be written safely (redaction unavailable or failing); nothing written."""


class StateRoots(NamedTuple):
    """The capture code's three folders."""

    pending: Path
    state: Path
    data: Path


@dataclass(frozen=True)
class WriteResult:
    """What one write did, in counts only.

    ``escaped``: member turns that needed the neutralisation (each counted once).
    ``reader_altered``: turns the Claude reader would still drop or change, or ``None`` when the
    engine's reader could not be loaded to check.
    """

    path: Path
    turns: int
    escaped: int
    reader_altered: int | None


# --- the state roots ---------------------------------------------------------------------------


def _system_temp() -> str:
    return os.path.normcase(os.path.realpath(tempfile.gettempdir()))


def _strictly_under_temp(raw: str) -> str | None:
    """The realpath of ``raw`` when it is absolute and strictly inside the system temp folder."""
    if not os.path.isabs(raw):
        return None
    real = os.path.realpath(raw)
    norm = os.path.normcase(real)
    temp = _system_temp()
    try:
        common_root = os.path.commonpath([norm, temp])
    except ValueError:  # different drives on Windows
        return None
    if common_root != temp or norm == temp:
        return None
    if os.path.exists(real) and not os.path.isdir(real):
        return None
    return real


def state_roots() -> StateRoots:
    """The pending, state and data folders, decided now (never at import).

    Raises ``ValueError`` when the test door is set to anything but a folder inside the system
    temp folder: a door that could point the capture at a real folder is refused outright.
    """
    raw = os.environ.get(CAPTURE_STATE_ENV, "")
    if raw.strip():
        real = _strictly_under_temp(raw.strip())
        if real is None:
            common.log(LABEL, "state-roots", "ERROR", time.time(), f"{CAPTURE_STATE_ENV} refused")
            raise ValueError(
                f"REFUSED: {CAPTURE_STATE_ENV} must be an absolute folder inside the system "
                "temp folder (it is a test-only door)"
            )
        common.log(
            LABEL, "state-roots", "WARN", time.time(),
            f"TEST-ONLY {CAPTURE_STATE_ENV} honoured: capture state is NOT the real state",
        )
        base = Path(real)
        return StateRoots(base / "pending", base / "state", base / "data")
    import config

    return StateRoots(Path(config.PENDING_DIR), Path(config.STATE_DIR), Path(config.DATA_DIR))


def chats_dir() -> Path:
    """``<data>/opencode-chats``."""
    return state_roots().data / CHATS_FOLDER


def _check_id(session_id: Any) -> str:
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("session id must be a non-empty string")
    if any(ch in session_id for ch in _FORBIDDEN_ID_CHARS) or ".." in session_id:
        raise ValueError("session id holds a path separator, '..', ':' or NUL")
    if any(ord(ch) < 0x20 for ch in session_id) or session_id.strip() != session_id:
        raise ValueError("session id holds a control character or edge whitespace")
    if session_id.startswith("-"):
        # It would reach `opencode session export` as an option, not as the chat's id.
        raise ValueError("session id starts with '-'")
    return session_id


def copy_path(session_id: str) -> Path:
    """``<data>/opencode-chats/<session_id>.jsonl``, the id verbatim (case kept)."""
    return chats_dir() / f"{_check_id(session_id)}{COPY_SUFFIX}"


# --- the neutralisation ------------------------------------------------------------------------


def _reader_triggers() -> tuple[re.Pattern[str] | None, tuple[str, ...]]:
    """The reminder regex and the leading prefixes, from the engine's own reader."""
    try:
        import transcript_filter as tf
    except Exception:  # noqa: BLE001 - no reader: nothing to escape for; the check counts it
        return None, ()
    reminder = getattr(tf, "_SYSTEM_REMINDER_RE", None)
    prefixes: list[str] = []
    for name in ("_CLAUDE_INJECTED_PREFIXES", "_CLAUDE_RELAY_PREFIXES"):
        value = getattr(tf, name, ())
        if isinstance(value, tuple):
            prefixes.extend(p for p in value if isinstance(p, str) and p)
    interrupt = getattr(tf, "_CLAUDE_INTERRUPT_PREFIX", None)
    if isinstance(interrupt, str) and interrupt:
        prefixes.append(interrupt)
    return (reminder if isinstance(reminder, re.Pattern) else None), tuple(prefixes)


_COMMAND_NAME_TAG = "<command-name>"
_REMINDER_OPEN_TAG = "<system-reminder>"


def neutralise_member_text(text: str) -> tuple[str, list[int]]:
    """Escape what the Claude reader would strip from member text.

    Returns ``(written_text, escapes)``; ``escapes`` are the offsets of the inserted backslashes in
    ``written_text`` (empty, and the text unchanged, when the reader would keep it as typed).
    """
    reminder_re, prefixes = _reader_triggers()
    points: set[int] = set()
    if reminder_re is not None and reminder_re.search(text):
        start = text.find(_REMINDER_OPEN_TAG)
        while start != -1:
            points.add(start + 1)
            start = text.find(_REMINDER_OPEN_TAG, start + 1)
    start = text.find(_COMMAND_NAME_TAG)
    while start != -1:
        points.add(start + 1)
        start = text.find(_COMMAND_NAME_TAG, start + 1)
    probe = text.lstrip()
    if probe and prefixes and probe.startswith(prefixes):
        points.add(len(text) - len(probe) + 1)
    if not points:
        return text, []
    pieces: list[str] = []
    escapes: list[int] = []
    previous = 0
    for index, point in enumerate(sorted(points)):
        pieces.append(text[previous:point])
        escapes.append(point + index)
        pieces.append(ESCAPE_CHAR)
        previous = point
    pieces.append(text[previous:])
    return "".join(pieces), escapes


def restore_text(text: str, escapes: Iterable[int]) -> str:
    """The member's exact text back from a written text and its ``glitchEscapes`` offsets."""
    chars = list(text)
    for offset in sorted(set(escapes), reverse=True):
        if not (0 <= offset < len(chars)) or chars[offset] != ESCAPE_CHAR:
            raise ValueError("escape offsets do not match this text")
        del chars[offset]
    return "".join(chars)


# --- the write ---------------------------------------------------------------------------------


def _field(turn: Any, name: str) -> Any:
    if isinstance(turn, Mapping):
        return turn.get(name)
    return getattr(turn, name, None)


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value
    return None


def _normalise(turns: Iterable[Any]) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for turn in turns:
        role = _field(turn, "role")
        text = _field(turn, "text")
        if not isinstance(role, str) or role.strip().lower() not in _ROLES:
            raise ValueError("turn role must be 'User' or 'Assistant'")
        if not isinstance(text, str):
            raise TypeError("turn text must be a string")
        out.append((_ROLES[role.strip().lower()], text, _iso(_field(turn, "ts"))))
    return out


def _redactor() -> Any:
    try:
        import secret_scan
    except Exception as exc:  # any import failure is a refusal
        raise CacheRefused("the secret redactor could not be loaded; nothing written") from exc
    redact = getattr(secret_scan, "redact_text", None)
    if not callable(redact):
        raise CacheRefused("the secret redactor is missing; nothing written")
    return redact


def _redact_all(texts: list[str]) -> list[str]:
    redact = _redactor()
    out: list[str] = []
    for text in texts:
        try:
            result = redact(text, source=REDACT_SOURCE)
        except Exception as exc:  # a failing redactor is a refusal
            raise CacheRefused(
                f"the secret redactor failed ({type(exc).__name__}); nothing written"
            ) from exc
        if not (isinstance(result, tuple) and result and isinstance(result[0], str)):
            raise CacheRefused("the secret redactor returned an unexpected shape; nothing written")
        out.append(result[0])
    return out


def _reader_check(events: list[dict[str, Any]]) -> int | None:
    """How many events the real ClaudeAdapter would drop or change; None if it cannot load."""
    try:
        import transcript_filter as tf

        adapter = tf.ClaudeAdapter(tf.FilterStats(engine=tf.ENGINE_CLAUDE))
    except Exception:  # noqa: BLE001 - reported as "unchecked", never as clean
        return None
    altered = 0
    for line, event in enumerate(events, start=1):
        expected = event["message"]["content"][0]["text"]
        try:
            kept = adapter.read(event, line)
        except Exception:  # noqa: BLE001 - a reader crash on a turn is an alteration
            altered += 1
            continue
        if len(kept) != 1 or kept[0].text != expected:
            altered += 1
    return altered


def _chmod(path: Path, mode: int) -> None:
    if os.name == "nt":
        return
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def write_copy(session_id: str, turns: Iterable[Any], started_at: Any) -> WriteResult:
    """Write the whole copy for one session and say what it did (counts only).

    Raises ``ValueError`` for a hostile id or a bad role, ``TypeError`` for non-string text, and ``CacheRefused`` when redaction
    is unavailable; in both cases nothing is written and a previous copy is untouched.
    """
    started = time.time()
    target = copy_path(session_id)
    normalised = _normalise(turns)
    fallback_ts = _iso(started_at)
    try:
        redacted = _redact_all([text for _, text, _ in normalised])
    except CacheRefused:
        common.log(LABEL, "write", "REFUSED", started, "redaction unavailable; nothing written")
        raise

    events: list[dict[str, Any]] = []
    escaped = 0
    for index, ((role, _, ts), text) in enumerate(zip(normalised, redacted, strict=True)):
        escapes: list[int] = []
        if role == "user":
            text, escapes = neutralise_member_text(text)
            escaped += 1 if escapes else 0
        event: dict[str, Any] = {
            "type": role,
            "uuid": str(uuid.uuid5(_UUID_NAMESPACE, f"{session_id}\0{index}")),
            "sessionId": session_id,
            "timestamp": ts or fallback_ts,
            "message": {"role": role, "content": [{"type": "text", "text": text}]},
        }
        if escapes:
            event[ESCAPES_KEY] = escapes
        events.append(event)

    body = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)
    import shared

    folder = target.parent
    folder.mkdir(parents=True, exist_ok=True)
    _chmod(folder, DIR_MODE)
    shared.atomic_write_text(target, body)
    _chmod(target, FILE_MODE)

    altered = _reader_check(events)
    status = "OK" if altered == 0 else "WARN"
    common.log(
        LABEL, "write", status, started,
        f"turns={len(events)} escaped={escaped} "
        f"reader_altered={'unchecked' if altered is None else altered}",
    )
    return WriteResult(target, len(events), escaped, altered)


def write(session_id: str, turns: Iterable[Any], started_at: Any) -> Path:
    """Write the whole copy for one session; returns its path (see ``write_copy``)."""
    return write_copy(session_id, turns, started_at).path


# --- the prune ---------------------------------------------------------------------------------


def prune(keep_ids: Iterable[str], *, source_reachable: bool) -> list[str]:
    """Remove copies whose id is not in ``keep_ids``; nothing at all unless the store was reached.

    Only regular ``.jsonl`` files are considered (a symlink, a folder or any other file is left
    alone). Returns the removed ids, sorted.
    """
    started = time.time()
    if not source_reachable:
        common.log(LABEL, "prune", "SKIP", started, "source unreachable; nothing removed")
        return []
    keep = set(keep_ids)
    folder = chats_dir()
    removed: list[str] = []
    try:
        entries = list(os.scandir(folder))
    except FileNotFoundError:
        return []
    for entry in entries:
        if not entry.name.endswith(COPY_SUFFIX):
            continue
        session_id = entry.name[: -len(COPY_SUFFIX)]
        if session_id in keep:
            continue
        try:
            mode = os.lstat(entry.path).st_mode
        except OSError:
            continue
        if not stat.S_ISREG(mode):
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            continue
        removed.append(session_id)
    removed.sort()
    common.log(LABEL, "prune", "OK", started, f"removed={len(removed)}")
    return removed
