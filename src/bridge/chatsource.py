"""chatsource.py - read an OpenCode chat (and the list of chats) through OpenCode's own CLI.

``CAPTURE_SOURCE=export-cli`` (``docs/api-findings.md``, decision lines and item 11).
The read path is the supported command ``opencode session export --standalone <id>``, which
prints ``{info, messages[]}`` JSON in about 0.24 s, needs no background service, and works while
another server holds the same data dir.
``--standalone`` is load-bearing: without it the CLI talks to the background service, which
ignores the environment's data dir (item 1g).
The listing is ``opencode session list --standalone --format json``: top-level sessions of the
CURRENT project only, and OpenCode takes the project from ``$PWD``, not the process working folder
(items 10b, 11b).

What this module does NOT do: it writes no file, takes no lock and opens no database.
OpenCode's raw store (``opencode.db``) also holds account and credential tables, which is why the
CLI is the road and the database is never opened here (item 11).
Log lines carry labels and counts only, never chat text.

The reader checks the SHAPE it needs, never a version number.
Keeping is DEFAULT-DROP: only the member's text (``messages[type=user].text``) and the assistant's
visible text (``messages[type=assistant].content[type=text].text``) are kept.
Tool calls, tool output, reasoning, files, compaction summaries, idle markers, synthetic parts and
any part or message type this reader does not know are dropped and counted in ``Stats``, so the
capture can log what it left behind without ever logging its text.

Turn records are per MESSAGE: one member message gives one ``User`` turn, one assistant message
with visible text gives one ``Assistant`` turn.
In a measured tool turn the assistant writes two messages (one holding only the tool call, one
holding the answer); the tool-only one carries no visible text and is dropped as empty, so a
measured turn reads as one ``User`` plus one ``Assistant``.
Two assistant messages that both carry text stay two ``Assistant`` records, in order (they are
never merged: what the member saw is what is kept).
The text parts of ONE assistant message are joined in order with a blank line between them.

The child is always launched with the Glitch recursion guard set (``CODEX_INVOKED_BY``, the name
``codex_shared.invoked_by_glitch`` reads, and ``CLAUDE_INVOKED_BY``, the Claude side's twin):
``--standalone`` starts a private server that loads plugins (item 1), and the Glitch plugin inside
that child must treat it as automated and never capture or inject.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import common

LABEL = "opencode-chatsource"

# Test door: an absolute path to the binary (a fake one in tests).
BIN_ENV = "GLITCH_OPENCODE_BIN"

# Per-call bound. The measured export takes 0.24 s and the list 0.29 s (items 11a, 11b).
TIMEOUT_SECONDS = 20.0

# Grace for the second wait after a timed-out child is killed.
_KILL_GRACE_SECONDS = 2.0

# The recursion guard: the Codex name the bridge's `is_automated` reads through
# `codex_shared.invoked_by_glitch()`, and the Claude side's name. Value is a label only.
_CODEX_RECURSION_FALLBACK = "CODEX_INVOKED_BY"
CLAUDE_RECURSION_ENV = "CLAUDE_INVOKED_BY"
INVOKED_BY_VALUE = "glitch-opencode-capture"

# Measured types (docs/api-findings.md item 11, the author's private test evidence).
_MSG_USER = "user"
_MSG_ASSISTANT = "assistant"
_MSG_KNOWN_DROPPED = frozenset({"idle", "compaction"})
_PART_TEXT = "text"
_PART_KNOWN_DROPPED = frozenset({"tool", "reasoning", "file"})


# The member's road for a reader or format problem: it is this add-on's to fix, never Glitch's.
ADDON_ROAD = (
    "This is part of the OpenCode add-on, not Glitch itself. Re-install the add-on from its "
    "folder; if it keeps happening, tell the add-on's author."
)


class ChatUnreadableError(RuntimeError):
    """OpenCode answered, but not in a shape this reader knows.

    The message names the chat (its id), says nothing was staged and gives the member's road.
    It never carries chat text; ``detail`` is a short fixed label chosen by this module.
    """

    def __init__(self, session_id: str, detail: str) -> None:
        self.session_id = session_id
        self.detail = detail
        super().__init__(
            f"An OpenCode chat could not be read: {detail}. The chat is {session_id or '(no id)'}. "
            f"Nothing from it was added to the memory queue. {ADDON_ROAD}"
        )


class SessionListUnreadableError(ChatUnreadableError):
    """The session list came back in a shape this reader does not know (no chat text inside)."""

    def __init__(self, detail: str) -> None:
        self.session_id = ""
        self.detail = detail
        RuntimeError.__init__(
            self,
            f"OpenCode's list of chats could not be read: {detail}. "
            f"Nothing was added to the memory queue. {ADDON_ROAD}",
        )


class SourceUnreachable(RuntimeError):
    """OpenCode could not be asked: no binary, a timeout, or a failing run that is not a shape
    problem. Never means "no chats": a caller must not read it as an empty store."""


class SessionNotFound(SourceUnreachable):
    """OpenCode answered and said this chat does not exist (export exit non-zero, stderr's first
    line starting ``Session not found``; measured on 2.2.7 as ``Session not found: <id>``, exit 1).

    The ONE signal a caller may read as "the chat is gone". A subclass of ``SourceUnreachable`` so a
    caller that only knows the parent still treats it as "no answer", never as "no chats"."""


# The measured first line of the export's stderr for an unknown id (docs/api-findings.md).
_NOT_FOUND_PREFIX = b"Session not found"


@dataclass(frozen=True)
class Turn:
    role: Literal["User", "Assistant"]
    text: str
    ts: str | None


@dataclass(frozen=True)
class SessionInfo:
    id: str
    started_at: str | None
    folder: str
    parent_id: str
    # The row's ``updated`` (parsed like ``created``): a hint for the sweep, not part of equality.
    updated: str | None = field(default=None, compare=False)


@dataclass
class Stats:
    """What the reader left behind, by category (counts only, safe to log)."""

    kept_user: int = 0
    kept_assistant: int = 0
    dropped_empty: int = 0
    dropped_messages: dict[str, int] = field(default_factory=dict)
    dropped_parts: dict[str, int] = field(default_factory=dict)
    unknown_messages: int = 0
    unknown_parts: int = 0

    def _drop(self, bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def summary(self) -> str:
        """One log-safe line: counts and type names only."""
        msgs = ",".join(f"{k}={v}" for k, v in sorted(self.dropped_messages.items()))
        parts = ",".join(f"{k}={v}" for k, v in sorted(self.dropped_parts.items()))
        return (
            f"user={self.kept_user} assistant={self.kept_assistant} empty={self.dropped_empty} "
            f"dropped_msgs[{msgs}] dropped_parts[{parts}] "
            f"unknown_msgs={self.unknown_messages} unknown_parts={self.unknown_parts}"
        )


# --- the binary and the child -------------------------------------------------------------------


def find_binary() -> str | None:
    """The OpenCode binary: ``$GLITCH_OPENCODE_BIN`` (absolute, test door), else ``opencode`` on
    PATH, else the installer's ``~/.opencode/bin/opencode``; ``None`` when none exists."""
    door = os.environ.get(BIN_ENV, "").strip()
    if door:
        path = Path(door)
        return str(path) if path.is_absolute() and path.is_file() else None
    found = shutil.which("opencode")
    if found:
        return found
    bin_dir = Path.home() / ".opencode" / "bin"
    for name in ("opencode", "opencode.exe"):
        candidate = bin_dir / name
        if candidate.is_file():
            return str(candidate)
    return None


def _recursion_env_name() -> str:
    """The engine's own name for the guard, or the measured name when the engine is off the path."""
    try:
        import codex_shared
    except Exception:  # noqa: BLE001 - the engine off the path: the measured name still holds
        return _CODEX_RECURSION_FALLBACK
    name = getattr(codex_shared, "CODEX_RECURSION_ENV", None)
    return name if isinstance(name, str) and name else _CODEX_RECURSION_FALLBACK


def child_env(
    *, home: str | os.PathLike[str] | None = None, cwd: str | os.PathLike[str] | None = None
) -> dict[str, str]:
    """The child's environment: this process's, plus the recursion guard.

    ``XDG_*`` and ``OPENCODE_*`` stay as inherited (tests point them at temp), except:
    ``home`` (optional) re-roots the four XDG homes under it, the XDG default layout
    (``<home>/.local/share``, ``<home>/.local/state``, ``<home>/.cache``, ``<home>/.config``), for a
    caller that reads a store other than the inherited one; ``cwd`` (optional) sets ``PWD``,
    because OpenCode takes its project from ``$PWD`` (item 10b).
    ``OPENCODE_DISABLE_AUTOUPDATE`` defaults to ``1`` so a capture never starts an update.
    """
    env = dict(os.environ)
    env[_recursion_env_name()] = INVOKED_BY_VALUE
    env[CLAUDE_RECURSION_ENV] = INVOKED_BY_VALUE
    env.setdefault("OPENCODE_DISABLE_AUTOUPDATE", "1")
    if home is not None:
        base = Path(home)
        env["XDG_DATA_HOME"] = str(base / ".local" / "share")
        env["XDG_STATE_HOME"] = str(base / ".local" / "state")
        env["XDG_CACHE_HOME"] = str(base / ".cache")
        env["XDG_CONFIG_HOME"] = str(base / ".config")
    if cwd is not None:
        env["PWD"] = str(cwd)
    return env


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Kill the child and anything it started (``--standalone`` starts a private server)."""
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _bound(timeout: float | None) -> float:
    """The per-call bound: ``timeout`` when given, else ``TIMEOUT_SECONDS`` read at call time."""
    return TIMEOUT_SECONDS if timeout is None else float(timeout)


def _run(
    argv: list[str], *, env: dict[str, str], cwd: str | None, what: str,
    timeout: float | None = None,
) -> bytes:
    """Run the child (list argv, never a shell, stdin closed) and return its stdout bytes.

    ``timeout`` bounds this one call (default ``TIMEOUT_SECONDS``).
    Raises ``SourceUnreachable`` on a missing binary, a timeout or a non-zero exit.
    """
    limit = _bound(timeout)
    started = time.time()
    popen_kw: dict[str, Any] = {}
    if sys.platform != "win32":
        popen_kw["start_new_session"] = True  # own process group, so a timeout kills it all
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            **popen_kw,
        )
    except OSError as exc:
        common.log(LABEL, what, "unreachable", started, f"spawn failed: {type(exc).__name__}")
        raise SourceUnreachable(f"OpenCode could not be started ({type(exc).__name__}).") from exc
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired as exc:
        _kill(proc)
        try:
            proc.communicate(timeout=_KILL_GRACE_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        common.log(LABEL, what, "unreachable", started, f"timeout {limit}s")
        raise SourceUnreachable(
            f"OpenCode did not answer within {limit:g} seconds."
        ) from exc
    if proc.returncode != 0:
        first_line = (err or b"").split(b"\n", 1)[0]
        if what == "export" and first_line.startswith(_NOT_FOUND_PREFIX):
            # The label only: stderr's bytes are never logged or carried.
            common.log(LABEL, what, "not-found", started, f"exit {proc.returncode}")
            raise SessionNotFound("OpenCode has no chat with this id.")
        common.log(LABEL, what, "unreachable", started, f"exit {proc.returncode}")
        raise SourceUnreachable(f"OpenCode exited with code {proc.returncode}.")
    common.log(LABEL, what, "ok", started, f"{len(out)} bytes")
    return out


def _decode_json(raw: bytes) -> Any:
    """Strict: the findings record the export and the list as JSON on stdout, nothing else."""
    return json.loads(raw.decode("utf-8"))


# --- timestamps ---------------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    """An export timestamp as ISO-8601 UTC, or ``None``.

    Accepts epoch milliseconds (OpenCode's unit in every measured event, e.g. ``time.start``), an
    ISO string (passed through), or an object carrying ``created`` / ``start``.
    """
    if isinstance(value, dict):
        for key in ("created", "start"):
            if key in value:
                return _iso(value[key])
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000.0 if value > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


# --- the export ---------------------------------------------------------------------------------


def export_argv(binary: str, session_id: str) -> list[str]:
    """The measured export command (item 11a).

    Raises ``ValueError`` for an id that starts with ``-``: OpenCode would read it as an option.
    Whether OpenCode's CLI honours ``--`` before a positional was never measured, so the id check
    is the guard (``chatcache`` refuses the same ids before any capture reaches here).
    """
    if not isinstance(session_id, str) or not session_id or session_id.startswith("-"):
        raise ValueError("session id must be non-empty and must not start with '-'")
    return [binary, "session", "export", "--standalone", session_id]


def list_argv(binary: str) -> list[str]:
    """The measured list command (item 11b)."""
    return [binary, "session", "list", "--standalone", "--format", "json"]


def _assistant_text(session_id: str, message: dict[str, Any], stats: Stats) -> str:
    content = message.get("content")
    if not isinstance(content, list):
        raise ChatUnreadableError(session_id, "an assistant message has no content list")
    texts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            raise ChatUnreadableError(session_id, "an assistant part is not an object")
        kind = part.get("type")
        if not isinstance(kind, str):
            raise ChatUnreadableError(session_id, "an assistant part has no type")
        if kind == _PART_TEXT:
            text = part.get("text")
            if not isinstance(text, str):
                raise ChatUnreadableError(session_id, "a text part has no text")
            if part.get("synthetic") or part.get("ignored"):
                stats._drop(stats.dropped_parts, "synthetic")
                continue
            texts.append(text)
        elif kind in _PART_KNOWN_DROPPED:
            stats._drop(stats.dropped_parts, kind)
        else:
            stats.unknown_parts += 1
            stats._drop(stats.dropped_parts, "unknown")
    return "\n\n".join(texts)


def parse_export(session_id: str, doc: Any) -> tuple[list[Turn], Stats]:
    """The kept turns of one parsed export document, plus what was dropped."""
    if not isinstance(doc, dict):
        raise ChatUnreadableError(session_id, "the export is not a JSON object")
    info = doc.get("info")
    messages = doc.get("messages")
    if not isinstance(info, dict):
        raise ChatUnreadableError(session_id, "the export has no info object")
    if not isinstance(messages, list):
        raise ChatUnreadableError(session_id, "the export has no messages list")
    info_id = info.get("id")
    if isinstance(info_id, str) and info_id and info_id != session_id:
        raise ChatUnreadableError(session_id, "the export answered for a different chat")

    stats = Stats()
    turns: list[Turn] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ChatUnreadableError(session_id, "a message is not an object")
        kind = message.get("type")
        if not isinstance(kind, str):
            raise ChatUnreadableError(session_id, "a message has no type")
        if kind == _MSG_USER:
            text = message.get("text")
            if not isinstance(text, str):
                raise ChatUnreadableError(session_id, "a member message has no text")
            if message.get("files"):
                stats._drop(stats.dropped_parts, "file")
            if not text.strip():
                stats.dropped_empty += 1
                continue
            turns.append(Turn("User", text, _iso(message.get("time"))))
            stats.kept_user += 1
        elif kind == _MSG_ASSISTANT:
            text = _assistant_text(session_id, message, stats)
            if not text.strip():
                stats.dropped_empty += 1
                continue
            turns.append(Turn("Assistant", text, _iso(message.get("time"))))
            stats.kept_assistant += 1
        elif kind in _MSG_KNOWN_DROPPED:
            stats._drop(stats.dropped_messages, kind)
        else:
            stats.unknown_messages += 1
            stats._drop(stats.dropped_messages, "unknown")
    return turns, stats


def read_turns_with_stats(
    session_id: str, *, home: str | os.PathLike[str] | None = None, timeout: float | None = None
) -> tuple[list[Turn], Stats]:
    """Export one chat and return its kept turns plus the drop counts.

    Raises ``SourceUnreachable`` (no binary, timeout, failing run), its subclass ``SessionNotFound``
    (OpenCode said the chat does not exist) or ``ChatUnreadableError``
    (OpenCode answered in a shape this reader does not know), and ``ValueError`` for an id that
    starts with ``-`` (``export_argv``; nothing is run). ``home`` re-roots the XDG homes for
    the child (see ``child_env``); by default the child reads the inherited store.
    ``timeout`` bounds the export (default ``TIMEOUT_SECONDS``).
    A child (sub-agent) session exports the same way; its parent is not needed here.
    """
    binary = find_binary()
    if binary is None:
        common.log(LABEL, "export", "unreachable", time.time(), "no binary")
        raise SourceUnreachable("OpenCode is not installed where Glitch can find it.")
    raw = _run(
        export_argv(binary, session_id), env=child_env(home=home), cwd=None, what="export",
        timeout=timeout,
    )
    try:
        doc = _decode_json(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ChatUnreadableError(session_id, "the export is not JSON") from exc
    turns, stats = parse_export(session_id, doc)
    common.log(LABEL, "export", "parsed", time.time(), stats.summary())
    return turns, stats


def read_turns(session_id: str, *, home: str | os.PathLike[str] | None = None) -> list[Turn]:
    """The kept turns of one chat (see ``read_turns_with_stats``)."""
    return read_turns_with_stats(session_id, home=home)[0]


# --- the list -----------------------------------------------------------------------------------


def parse_list(doc: Any, *, cwd: str) -> list[SessionInfo]:
    """Rows of a parsed ``session list --format json`` document."""
    if not isinstance(doc, list):
        raise SessionListUnreadableError("the list is not a JSON array")
    rows: list[SessionInfo] = []
    for row in doc:
        if not isinstance(row, dict):
            raise SessionListUnreadableError("a list row is not an object")
        sid = row.get("id")
        if not isinstance(sid, str) or not sid:
            raise SessionListUnreadableError("a list row has no id")
        folder = row.get("directory")
        rows.append(
            SessionInfo(
                id=sid,
                started_at=_iso(row.get("created")),
                folder=folder if isinstance(folder, str) and folder else cwd,
                parent_id="",
                updated=_iso(row.get("updated")),
            )
        )
    return rows


def list_sessions(
    *, cwd: str | os.PathLike[str], home: str | os.PathLike[str] | None = None,
    timeout: float | None = None,
) -> list[SessionInfo]:
    """The top-level chats of the project at ``cwd``.

    The child runs with its working folder AND ``PWD`` set to ``cwd``: OpenCode takes the project
    from ``$PWD`` (item 10b).
    The list holds TOP-LEVEL sessions only (sub-agent chats are not listed, item 11b), so every
    ``parent_id`` is ``""``.
    ``folder`` is the session's start folder from the row's ``directory``, else ``cwd``.
    ``updated`` is the row's last-change time, parsed like ``created`` (``None`` when absent).
    ``timeout`` bounds the list call (default ``TIMEOUT_SECONDS``).
    An unreachable OpenCode raises ``SourceUnreachable``; it never returns ``[]``, which means a
    reachable store that holds no chats for this project.
    """
    folder = str(cwd)
    binary = find_binary()
    if binary is None:
        common.log(LABEL, "list", "unreachable", time.time(), "no binary")
        raise SourceUnreachable("OpenCode is not installed where Glitch can find it.")
    raw = _run(
        list_argv(binary), env=child_env(home=home, cwd=folder), cwd=folder, what="list",
        timeout=timeout,
    )
    if not raw.strip():
        return []
    try:
        doc = _decode_json(raw)
    except (UnicodeDecodeError, ValueError) as exc:
        raise SessionListUnreadableError("the list is not JSON") from exc
    return parse_list(doc, cwd=folder)
