"""capture.py - OpenCode chats into the pending memory queue.

The OpenCode twin of the engine's ``cursor_transcripts``: the same queue, the same stub shape, the
same excerpt bounds and the same no-backfill rule, so ``/digest`` and the keeper read an OpenCode
chat with no idea it came from a different tool.
Function for function it mirrors ``cursor_transcripts`` (``render_excerpt``, ``redact``,
``stub_name``, ``stage``, the ledger, one per-chat decision, the hook's road, the sweep, the notice).

WHERE THE CHATS ARE. OpenCode keeps them in its own store; ``chatsource`` reads one through
``opencode session export --standalone <id>`` and lists this project's top-level chats through
``opencode session list``. The engine's memory read knows Claude Code's transcript shape, so each
staged chat is first written out as a Claude-shaped copy (``chatcache.write_copy``) and the stub's
pointer names that copy.

THE ORDER IS THE INVARIANT: copy, then stub, then ledger. The stub's pointer must never name a copy
that does not exist, so a copy that cannot be written (redaction unavailable, disk full) writes no
stub, changes no ledger record and logs one loud ERROR line; the next turn or sweep tries again.
A staged chat that grows REPLACES its one stub (``opencode-<id>.md``) and rewrites its one copy.

TWO ROADS IN, ONE DOOR.
- ``capture_one`` is the hook's road: the plugin reports a turn end (OpenCode fires
  ``session.execution.succeeded`` / ``.failed`` / ``.interrupted`` with only ``sessionID``), a
  compaction, or a start. Each re-stages the chat once it has reached the floor.
- ``sweep`` is the backstop, run at a chat's start: it lists this project's chats newest first and
  stages any that grew without being staged. It is bounded per call by a count AND by wall-clock
  (``time.monotonic``); what it did not reach is counted as ``deferred`` and is the next start's.
Both run the same per-chat decision (``_settle``) under one file lock on the ledger.

THE SCAR-TISSUE RULE: chats from before the update are never backfilled. The first run writes a
first-run stamp and stages nothing. A chat is eligible only if it started after that stamp; one
judged older stays that way for good, however much it grows. One deliberate exception: the LIVE
chat that triggers the first run is recorded as eligible (note ``first chat``), so a member's first
OpenCode chat after install is captured from its next turn on, as in Claude Code. In OpenCode a
chat's start always comes before its first turn end, so the usual road is the sweep: the session
start passes its own id (``live_session_id``) and a cold sweep records THAT chat as ``first chat``
while marking every OTHER listed chat ``started before the first sweep``. A first run from a live
turn (reason ``turn`` or ``compact``) records that chat the same way. On a ledger that already
exists the live id changes nothing: the live chat is judged like any other listed chat.

THE UPDATED HINT. A list row carries ``updated``. The sweep records it beside a staged or
too-short chat's marker (``listed_updated``) and skips re-reading that chat while the list shows
the same value; an absent or unparsable value, or one never recorded, reads the chat as before.

STARTED-AT. The ledger's own record wins once written (so a chat's age never changes under it).
Before that: the list row's ``created`` (the sweep's road, no read needed), else the EARLIEST
timestamp among the exported turns (the hook's road; OpenCode's export keeps every original turn,
compaction included, item 14). A chat with no readable timestamp at all is judged old: the safe
direction for the no-backfill rule.

THE FLOOR. ``MIN_TURNS_TO_STAGE`` counts conversation records, member and assistant alike, exactly
as ``cursor_transcripts`` and the Claude flush hook count them (``len(turns)``); a tool-only
assistant message is no record (``chatsource`` drops it as empty).

SKIPPED, never staged: a sub-agent chat (non-empty ``parent_id``), a chat Glitch drives itself
(``codex_shared.invoked_by_glitch()``, the guard ``chatsource`` sets on every OpenCode child it
starts), and a chat whose folder does not resolve to this Glitch root. The sweep cannot recognise a
Glitch-driven chat from the store (the store keeps no such mark), and it refuses to run at all
inside a Glitch-driven process, so an export's own private server never sweeps recursively.

REFUSALS. A chat OpenCode answers for in a shape ``chatsource`` does not know records NO marker, so
it is read again (and is loud again) on every sweep until the reader is updated. A LIST of chats in
a shape the reader does not know is recorded the same way, under the fixed key
``LIST_REFUSAL_KEY``, and cleared by the next readable list.

UNREACHABLE. One OpenCode that does not answer (closing, a timeout) is common and stays quiet. The
ledger counts misses in a row (``UNREACHABLE_KEY``; every read OpenCode answers, "not found" and a
refusal included, resets it), written only by ``_note_reach``. At ``UNREACHABLE_NOTICE_AFTER`` in a
row the start notice says so once per streak, and the hook road answers ``unreachable streak`` once
per streak, which the bridge turns into the visible ``capture-unreachable``.

PRUNING. ``session list`` covers this project's top-level chats only, so a chat absent from it is
NOT assumed gone. Each candidate is confirmed with a read, and its ledger record and copy are
removed only on OpenCode's own not-found answer (``chatsource.SessionNotFound``). An unreachable
store reads like an empty one and never prunes. A stub already in the queue is the queue's and is
left alone.

Every log line carries a label and counts only, never chat text. Like ``chatcache``, this module
imports nothing from the engine at import time.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import chatcache
import chatsource
import common

LABEL: Final = "opencode-capture"

# === The queue's existing excerpt contract (mirrored from the flush hooks; pinned by test) ===

MAX_TURNS: Final = 30
MAX_CONTEXT_CHARS: Final = 15_000
MIN_TURNS_TO_STAGE: Final = 5

STAGED_BY: Final = "opencode-transcripts"

# The sweep's per-call bounds: chats READ (an export, about 0.24 s each) and wall-clock seconds.
SWEEP_LIMIT: Final = 20
SWEEP_BUDGET_SECONDS: Final = 8.0
# Of those reads, at most this many go to confirming that an unlisted chat is gone.
PRUNE_CONFIRM_LIMIT: Final = 5

LEDGER_NAME: Final = "opencode-staged.json"
LEDGER_VERSION: Final = 1
LOCK_TIMEOUT: Final = 10.0
# The hook road's bounds. The plugin kills a `capture` call after 10 s
# (`DEFAULT_TIMEOUTS.capture` in src/plugin/bridge-client.ts); a bridge killed mid-export would
# leave its export child behind. Worst case: the lock wait, one export, the kill grace after a
# timed-out export = 3 + 3 + 2 = 8 s, leaving 2 s for uv and the interpreter. (An export is
# measured at about 0.24 s.) Pinned against the TS number by test.
HOOK_LOCK_TIMEOUT: Final = 3.0
HOOK_READ_TIMEOUT: Final = 3.0
HOOK_WORST_CASE_SECONDS: Final = (
    HOOK_LOCK_TIMEOUT + HOOK_READ_TIMEOUT + chatsource._KILL_GRACE_SECONDS
)
NOTICE_LOCK_TIMEOUT: Final = 1.0

NOTE_STAGED: Final = "staged"
NOTE_TOO_SHORT: Final = "too short to keep"
NOTE_BEFORE_FIRST_SWEEP: Final = "started before the first sweep"
NOTE_FIRST_CHAT: Final = "first chat"

# The ledger's unreachable streak: misses in a row, and whether the notice / the hook road have
# already said so for this streak. A ledger written before these existed reads as 0 / not said.
UNREACHABLE_KEY: Final = "unreachable"
UNREACHABLE_SAID_KEY: Final = "unreachable_said"
UNREACHABLE_FLAGGED_KEY: Final = "unreachable_flagged"
UNREACHABLE_NOTICE_AFTER: Final = 3
# The refusal record of an unreadable session LIST (no chat id can look like it: never a prefix).
LIST_REFUSAL_KEY: Final = "__session_list__"

REASONS: Final = ("turn", "compact", "start", "sweep")
# The reasons a LIVE chat reports its own turn with: only these make a cold start's "first chat".
LIVE_REASONS: Final = ("turn", "compact")

# capture_one's outcome labels.
OUT_STAGED: Final = "staged"
OUT_TOO_SHORT: Final = "too short"
OUT_UNCHANGED: Final = "unchanged"
OUT_BEFORE: Final = "before first sweep"
OUT_COLD_START: Final = "cold start"
OUT_REFUSED: Final = "refused"
OUT_NOT_FOUND: Final = "not found"
OUT_UNREACHABLE: Final = "unreachable"
OUT_UNREACHABLE_STREAK: Final = "unreachable streak"
OUT_COPY_FAILED: Final = "copy failed"
OUT_STUB_FAILED: Final = "stub failed"
OUT_BUSY: Final = "busy"
OUT_ERROR: Final = "error"
OUT_DEFERRED: Final = "deferred"
SKIP_CHILD: Final = "skipped:child"
SKIP_AUTOMATED: Final = "skipped:automated"
SKIP_OUTSIDE: Final = "skipped:outside"
SKIP_OTHER_ROOT: Final = "skipped:other root"
SKIP_BAD_ID: Final = "skipped:bad id"


@dataclass
class SweepResult:
    """What one sweep did, in facts a summary line can be built from (ids and counts only)."""

    listed: int = 0
    scanned: int = 0
    staged: list[str] = field(default_factory=list)
    too_short: list[str] = field(default_factory=list)
    before_first_sweep: int = 0
    unchanged: int = 0
    deferred: int = 0
    outside: int = 0
    pruned: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    copy_failed: int = 0
    unreachable: bool = False
    list_unreadable: bool = False
    cold_start: bool = False
    first_chat: str = ""
    busy: bool = False
    skipped: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "listed": self.listed,
            "scanned": self.scanned,
            "staged": list(self.staged),
            "too_short": list(self.too_short),
            "before_first_sweep": self.before_first_sweep,
            "unchanged": self.unchanged,
            "deferred": self.deferred,
            "outside": self.outside,
            "pruned": list(self.pruned),
            "refusals": list(self.refusals),
            "copy_failed": self.copy_failed,
            "unreachable": self.unreachable,
            "list_unreadable": self.list_unreadable,
            "cold_start": self.cold_start,
            "first_chat": self.first_chat,
            "busy": self.busy,
            "skipped": self.skipped,
            "error": self.error,
        }

    def summary(self) -> str:
        """One log-safe line: counts only."""
        return (
            f"listed={self.listed} scanned={self.scanned} staged={len(self.staged)} "
            f"too_short={len(self.too_short)} before={self.before_first_sweep} "
            f"unchanged={self.unchanged} deferred={self.deferred} outside={self.outside} "
            f"pruned={len(self.pruned)} refused={len(self.refusals)} "
            f"copy_failed={self.copy_failed} unreachable={self.unreachable} "
            f"cold={self.cold_start} first_chat={bool(self.first_chat)}"
        )


# === Paths and guards ========================================================================


def ledger_path() -> Path:
    """``<state>/opencode-staged.json``, decided at call time through ``chatcache.state_roots``."""
    return chatcache.state_roots().state / LEDGER_NAME


def this_root() -> Path | None:
    """The Glitch root whose engine this process runs (``config.PROJECT_ROOT``, read now)."""
    try:
        import config

        return common.resolve_root(config.PROJECT_ROOT)
    except Exception:  # noqa: BLE001 - no engine: no root to capture into
        return None


def _same_folder(a: Path | str, b: Path | str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def in_this_root(folder: str | os.PathLike[str] | None, root: Path | None) -> str:
    """'' when ``folder`` resolves to ``root``; otherwise the skip label."""
    where = common.resolve_root(folder)
    if where is None or root is None:
        return SKIP_OUTSIDE
    return "" if _same_folder(where, root) else SKIP_OTHER_ROOT


def invoked_by_glitch() -> bool:
    """The engine's recursion guard, read the way ``context.is_automated`` reads it."""
    try:
        import codex_shared

        return bool(codex_shared.invoked_by_glitch())
    except Exception:  # noqa: BLE001 - an unreadable guard is not evidence of automation
        return False


# === The turns, and the excerpt the queue takes ==============================================


def _field(turn: Any, name: str) -> Any:
    if isinstance(turn, Mapping):
        return turn.get(name)
    return getattr(turn, name, None)


def render_excerpt(turns: Iterable[Any]) -> tuple[str, int]:
    """The queue's excerpt shape, byte-for-byte as ``cursor_transcripts.render_excerpt`` and the
    flush hooks build it. Takes ``chatsource.Turn`` objects or ``{role, text}`` dicts."""
    recent = list(turns)[-MAX_TURNS:]
    parts: list[str] = []
    for turn in recent:
        parts.append(f"**{_field(turn, 'role')}:** {_field(turn, 'text')}\n")
    context = "\n".join(parts)
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[-MAX_CONTEXT_CHARS:]
        boundary = context.find("\n**")
        if boundary > 0:
            context = context[boundary + 1 :]
    return context, len(recent)


def redact(context: str) -> str:
    """Redact secret-shaped spans before staging; a missing or failing scan passes the excerpt
    through, exactly like the flush hooks (the COPY does not: ``chatcache`` refuses instead)."""
    try:
        import secret_scan
    except Exception:  # noqa: BLE001 - the degrade path the flush hooks take
        return context
    try:
        redacted, _findings = secret_scan.redact_text(context, source=STAGED_BY)
        return str(redacted)
    except Exception:  # noqa: BLE001 - a redaction fault must never cost the chat
        return context


def stub_name(session_id: str) -> str:
    """The chat's one stub, the id verbatim: the same chat always writes the same path."""
    return f"opencode-{session_id}.md"


def stage(copy: Path, session_id: str, turns: list[Any], reason: str) -> int:
    """Write (or REWRITE) this chat's one stub, its pointer naming ``copy``. Returns the turns
    rendered. Byte-for-byte the shape of ``cursor_transcripts.stage``; the pointer rides LAST."""
    import config
    from shared import atomic_write_text
    from transcript_filter import header_pointer

    context, turn_count = render_excerpt(turns)
    pending = chatcache.state_roots().pending
    pending.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- staged by {STAGED_BY} | reason={reason} | session={session_id} | "
        f"{config.now_local().isoformat()} | {turn_count} turns | {header_pointer(copy)} -->\n\n"
    )
    atomic_write_text(pending / stub_name(session_id), header + redact(context))
    return turn_count


# === The ledger =============================================================================


def _parse_stamp(value: object) -> datetime | None:
    """An aware datetime, or None. A naive stamp is refused rather than assumed into a zone."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def load_ledger() -> dict[str, Any]:
    """The ledger as it stands, or ``{}``. Missing OR unreadable is a cold start, the safe
    direction: re-stamp and stage nothing, never guess a date that lets old chats in."""
    try:
        data = json.loads(ledger_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("version") != LEDGER_VERSION:
        return {}
    if not isinstance(data.get("sessions"), dict) or _parse_stamp(data.get("first_run")) is None:
        return {}
    if not isinstance(data.get("refused"), dict):
        data["refused"] = {}
    count = data.get(UNREACHABLE_KEY)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        data[UNREACHABLE_KEY] = 0  # an older ledger, or a damaged count: no streak
    return data


def save_ledger(ledger: dict[str, Any]) -> None:
    from shared import atomic_write_text

    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(ledger, indent=2, sort_keys=True) + "\n")


def _open_ledger(now: datetime) -> tuple[dict[str, Any], bool]:
    """``(ledger, cold)``: the ledger, or a fresh one stamped ``now`` when there was none."""
    ledger = load_ledger()
    if ledger:
        return ledger, False
    return {"version": LEDGER_VERSION, "first_run": now.isoformat(), "sessions": {},
            "refused": {}, UNREACHABLE_KEY: 0}, True


def _note_reach(ledger: dict[str, Any], reached: bool) -> bool:
    """The one writer of the unreachable streak. True when it changed the ledger.

    ``reached`` False adds a miss; True (OpenCode answered anything at all) ends the streak and
    clears what was said about it, so the next streak is reported afresh.
    """
    if not reached:
        ledger[UNREACHABLE_KEY] = int(ledger.get(UNREACHABLE_KEY) or 0) + 1
        return True
    if not ledger.get(UNREACHABLE_KEY) and not ledger.get(UNREACHABLE_SAID_KEY) and not ledger.get(
        UNREACHABLE_FLAGGED_KEY
    ):
        return False
    ledger[UNREACHABLE_KEY] = 0
    ledger.pop(UNREACHABLE_SAID_KEY, None)
    ledger.pop(UNREACHABLE_FLAGGED_KEY, None)
    return True


def _record_first_chat(ledger: dict[str, Any], session_id: str, folder: str, now: datetime) -> None:
    """A cold start's one exception: the LIVE chat that triggered it is eligible (``first chat``).

    The one writer for both roads (a live turn's cold start and a cold sweep from a chat's start),
    so the record has one shape. It carries no ``started_at``: ``_settle`` judges a known record
    by its note, never by age, so the chat is read and staged from its next turn on.
    """
    ledger["sessions"][session_id] = {
        "staged": False, "note": NOTE_FIRST_CHAT, "folder": folder, "at": now.isoformat(),
    }


def _valid_id(value: object) -> str | None:
    """``value`` when it is a usable chat id (``chatcache.copy_path``'s rules), else None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        chatcache.copy_path(value)
    except ValueError:
        return None
    return value


# === The one per-chat decision ===============================================================


def _marker(turns: list[Any]) -> str:
    """The chat's high-water marker: its record count and its latest timestamp."""
    stamps = [ts for ts in (_field(t, "ts") for t in turns) if isinstance(ts, str) and ts]
    return f"{len(turns)}:{max(stamps) if stamps else ''}"


def _earliest(turns: list[Any]) -> datetime | None:
    parsed = [p for p in (_parse_stamp(_field(t, "ts")) for t in turns) if p is not None]
    return min(parsed) if parsed else None


def _started_at(
    known: dict[str, Any] | None, listed: str | None, turns: list[Any] | None
) -> datetime | None:
    """Ledger record, else the list row's ``created``, else the earliest exported turn."""
    if known is not None:
        recorded = _parse_stamp(known.get("started_at"))
        if recorded is not None:
            return recorded
    from_list = _parse_stamp(listed)
    if from_list is not None:
        return from_list
    return _earliest(turns) if turns is not None else None


def _record(**fields: Any) -> dict[str, Any]:
    return {k: v for k, v in fields.items() if v is not None}


def _settle(
    session_id: str,
    ledger: dict[str, Any],
    *,
    now: datetime,
    reason: str,
    folder: str,
    listed_started: str | None = None,
    listed_updated: str | None = None,
    budget: Callable[[], bool] | None = None,
    read_timeout: float | None = None,
) -> tuple[str, bool]:
    """Decide one chat and record the decision. Returns ``(outcome, ledger_changed)``.

    ``budget`` (the sweep's) is asked only when the chat actually has to be READ; it answers False
    once the count or the clock is spent, and the chat is then ``deferred``.
    ``listed_updated`` (the sweep's list row) lets a staged or too-short chat whose ``updated`` is
    the one recorded beside its marker answer ``unchanged`` with no read and no budget spent.
    ``read_timeout`` bounds the one export (default ``chatsource.TIMEOUT_SECONDS``).
    """
    sessions: dict[str, Any] = ledger["sessions"]
    refused: dict[str, Any] = ledger["refused"]
    raw_known = sessions.get(session_id)
    known = raw_known if isinstance(raw_known, dict) else None
    stamp = now.isoformat()
    first_run = _parse_stamp(ledger.get("first_run")) or now

    if known is not None and known.get("note") == NOTE_BEFORE_FIRST_SWEEP:
        return OUT_BEFORE, False  # from before the update, for good: never read again

    if known is None and listed_started is not None:
        began = _parse_stamp(listed_started)
        if began is None or began <= first_run:
            sessions[session_id] = _record(
                staged=False, note=NOTE_BEFORE_FIRST_SWEEP, folder=folder, at=stamp,
                started_at=began.isoformat() if began else None,
            )
            return OUT_BEFORE, True

    updated = listed_updated if _parse_stamp(listed_updated) is not None else None
    if (
        updated is not None
        and known is not None
        and known.get("note") in (NOTE_STAGED, NOTE_TOO_SHORT)
        and known.get("listed_updated") == updated
        and session_id not in refused
    ):
        return OUT_UNCHANGED, False  # the list says nothing changed since the marker: no read

    if budget is not None and not budget():
        return OUT_DEFERRED, False

    try:
        turns, _stats = chatsource.read_turns_with_stats(session_id, timeout=read_timeout)
    except chatsource.ChatUnreadableError as exc:
        # No marker, on purpose: an unread chat stays loud on every sweep until it is read.
        refused[session_id] = {"why": exc.detail, "folder": folder, "at": stamp, "notice": True}
        _note_reach(ledger, True)
        return OUT_REFUSED, True
    except chatsource.SessionNotFound:
        return OUT_NOT_FOUND, _note_reach(ledger, True)
    except chatsource.SourceUnreachable:
        return OUT_UNREACHABLE, _note_reach(ledger, False)

    dirty = _note_reach(ledger, True)
    dirty |= refused.pop(session_id, None) is not None
    marker = _marker(turns)
    if (
        known is not None
        and known.get("marker") == marker
        and known.get("note") in (NOTE_STAGED, NOTE_TOO_SHORT)
    ):
        if updated is not None and known.get("listed_updated") != updated:
            known["listed_updated"] = updated  # the next sweep may skip the read
            dirty = True
        return OUT_UNCHANGED, dirty

    began = _started_at(known, listed_started, turns)
    started_iso = began.isoformat() if began is not None else None
    if known is None and (began is None or began <= first_run):
        sessions[session_id] = _record(
            marker=marker, staged=False, note=NOTE_BEFORE_FIRST_SWEEP, folder=folder, at=stamp,
            started_at=started_iso,
        )
        return OUT_BEFORE, True

    if len(turns) < MIN_TURNS_TO_STAGE:
        previous = dict(known) if known is not None else {}
        record = _record(
            marker=marker, staged=False, note=NOTE_TOO_SHORT, turns=len(turns), folder=folder,
            at=stamp, started_at=started_iso, listed_updated=updated,
        )
        # A chat staged earlier keeps its stub fact (a chat never shrinks in OpenCode; belt only).
        if previous.get("stub"):
            record["stub"] = previous["stub"]
        sessions[session_id] = record
        return OUT_TOO_SHORT, True

    # Copy, THEN stub, THEN ledger: the stub's pointer must never name a copy that does not exist.
    started = time.time()
    try:
        chatcache.write_copy(session_id, turns, started_iso)
        copy = chatcache.copy_path(session_id)
    except (chatcache.CacheRefused, ValueError, TypeError, OSError) as exc:
        common.log(
            LABEL, reason, "ERROR", started,
            f"copy failed ({type(exc).__name__}); no stub written, nothing recorded",
        )
        return OUT_COPY_FAILED, dirty
    try:
        turn_count = stage(copy, session_id, turns, reason)
    except OSError as exc:
        common.log(LABEL, reason, "ERROR", started, f"stub failed ({type(exc).__name__})")
        return OUT_STUB_FAILED, dirty
    sessions[session_id] = _record(
        marker=marker, staged=True, note=NOTE_STAGED, stub=stub_name(session_id),
        turns=turn_count, folder=folder, at=stamp, started_at=started_iso, notice=True,
        listed_updated=updated,
    )
    return OUT_STAGED, True


# === The hook's road ========================================================================


def capture_one(session_id: str, session_dir: str, parent_id: str | None, reason: str) -> str:
    """Stage the chat a hook just reported, once it has reached the floor; returns the outcome.

    Never raises: a busy ledger stands aside (``busy``) and the next turn or the next start picks
    the chat up; any other fault is logged by type name and answers ``error``.
    """
    started = time.time()
    outcome = OUT_ERROR
    try:
        outcome = _capture_one(session_id, session_dir, parent_id, reason)
    except Exception as exc:  # noqa: BLE001 - FAIL-OPEN; the sweep is the second chance
        common.log(LABEL, str(reason), "ERROR", started, type(exc).__name__)
        return OUT_ERROR
    status = "ERROR" if outcome in (OUT_COPY_FAILED, OUT_STUB_FAILED, OUT_UNREACHABLE_STREAK) else (
        "SKIP" if outcome.startswith("skipped:") else "OK"
    )
    common.log(LABEL, str(reason), status, started, outcome)
    return outcome


def _capture_one(session_id: str, session_dir: str, parent_id: str | None, reason: str) -> str:
    try:
        chatcache.copy_path(session_id)  # the id check: verbatim, never a path out of the folder
    except ValueError:
        return SKIP_BAD_ID
    if isinstance(parent_id, str) and parent_id.strip():
        return SKIP_CHILD
    if invoked_by_glitch():
        return SKIP_AUTOMATED
    root = this_root()
    where = in_this_root(session_dir, root)
    if where:
        return where
    folder = str(common.resolve_root(session_dir) or root)
    from shared import file_lock

    lock = ledger_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        with file_lock(lock, timeout=HOOK_LOCK_TIMEOUT):
            return _capture_locked(session_id, folder, reason)
    except TimeoutError:
        return OUT_BUSY


def _capture_locked(session_id: str, folder: str, reason: str) -> str:
    import config

    now = config.now_local()
    ledger, cold = _open_ledger(now)
    if cold:
        if reason in LIVE_REASONS:
            _record_first_chat(ledger, session_id, folder, now)
        save_ledger(ledger)
        return OUT_COLD_START
    outcome, dirty = _settle(
        session_id, ledger, now=now, reason=reason, folder=folder, read_timeout=HOOK_READ_TIMEOUT,
    )
    if (
        outcome == OUT_UNREACHABLE
        and int(ledger.get(UNREACHABLE_KEY) or 0) >= UNREACHABLE_NOTICE_AFTER
        and not ledger.get(UNREACHABLE_FLAGGED_KEY)
    ):
        # Once per streak: the plugin records it as a visible failure; the rest stay quiet.
        ledger[UNREACHABLE_FLAGGED_KEY] = True
        outcome, dirty = OUT_UNREACHABLE_STREAK, True
    if dirty:
        save_ledger(ledger)
    return outcome


def capture_payload(payload: dict[str, Any]) -> str:
    """The bridge's door: ``{session_id, session_dir, parent_id?, reason}`` -> outcome label.

    Raises ``ValueError`` on a bad payload (the bridge reports it; nothing is read or written).
    """
    if not isinstance(payload, dict):
        raise ValueError("capture payload must be an object")  # noqa: TRY004 - one error kind
    session_id = payload.get("session_id")
    session_dir = payload.get("session_dir")
    parent_id = payload.get("parent_id")
    reason = payload.get("reason")
    if not isinstance(session_id, str) or not session_id.strip():
        raise ValueError("capture payload needs a session_id")
    if not isinstance(session_dir, str) or not session_dir.strip():
        raise ValueError("capture payload needs a session_dir")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError("capture payload parent_id must be a string")
    if reason not in REASONS:
        raise ValueError(f"capture payload reason must be one of {', '.join(REASONS)}")
    return capture_one(session_id, session_dir, parent_id, str(reason))


# === The backstop ===========================================================================


def sweep(
    *,
    limit: int = SWEEP_LIMIT,
    budget_seconds: float = SWEEP_BUDGET_SECONDS,
    live_session_id: str | None = None,
    lock_timeout: float | None = None,
    read_timeout: float | None = None,
) -> SweepResult:
    """Stage every chat of this project that grew without being staged, newest first, at most
    ``limit`` read and at most ``budget_seconds`` spent starting reads. One sweep at a time.

    A single read already started (the list, or one export) is bounded by ``read_timeout``
    (default ``chatsource.TIMEOUT_SECONDS``); the wait for the ledger lock by ``lock_timeout``
    (default ``LOCK_TIMEOUT``). The sweep starts no read once its budget is spent, and stops at the
    first read that cannot reach OpenCode.

    ``live_session_id`` is the chat whose start runs this sweep. On a cold start it is recorded as
    ``first chat`` (eligible) while every other listed chat is ``started before the first sweep``;
    on an existing ledger it changes nothing. An empty or unsafe id is ignored.
    """
    result = SweepResult()
    started = time.time()
    if invoked_by_glitch():
        result.skipped = "automated"
        common.log(LABEL, "sweep", "SKIP", started, "automated")
        return result
    root = this_root()
    if root is None:
        result.skipped = "no root"
        common.log(LABEL, "sweep", "SKIP", started, "no root")
        return result
    try:
        from shared import file_lock

        lock = ledger_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(lock, timeout=LOCK_TIMEOUT if lock_timeout is None else lock_timeout):
            _sweep_locked(
                result, root=root, limit=limit, budget_seconds=budget_seconds,
                live=_valid_id(live_session_id), read_timeout=read_timeout,
            )
    except TimeoutError:
        result.busy = True
    except Exception as exc:  # noqa: BLE001 - the next start sweeps again
        result.error = type(exc).__name__
        common.log(LABEL, "sweep", "ERROR", started, result.error)
        return result
    common.log(LABEL, "sweep", "BUSY" if result.busy else "OK", started, result.summary())
    return result


def _copy_ids() -> set[str]:
    folder = chatcache.chats_dir()
    try:
        names = os.listdir(folder)
    except OSError:
        return set()
    return {n[: -len(chatcache.COPY_SUFFIX)] for n in names if n.endswith(chatcache.COPY_SUFFIX)}


def _sweep_locked(
    result: SweepResult,
    *,
    root: Path,
    limit: int,
    budget_seconds: float,
    live: str | None = None,
    read_timeout: float | None = None,
) -> None:
    import config

    clock = time.monotonic()
    now = config.now_local()
    ledger, cold = _open_ledger(now)
    result.cold_start = cold
    dirty = cold
    sessions: dict[str, Any] = ledger["sessions"]
    spent = 0
    if cold and live is not None:
        # Recorded before the list is read, so an unreachable or lagging list cannot cost the
        # member their first chat (the turn road on a cold ledger does the same, unlisted).
        _record_first_chat(ledger, live, str(root), now)
        result.first_chat = live

    def budget() -> bool:
        nonlocal spent
        if spent >= limit or time.monotonic() - clock >= budget_seconds:
            return False
        spent += 1
        return True

    refused: dict[str, Any] = ledger["refused"]
    try:
        rows = chatsource.list_sessions(cwd=str(root), timeout=read_timeout)
    except chatsource.SessionListUnreadableError as exc:
        result.list_unreadable = True
        result.refusals.append(exc.detail)
        # Recorded like a refused chat, so the start notice says it while it lasts.
        refused[LIST_REFUSAL_KEY] = {"why": exc.detail, "at": now.isoformat(), "notice": True}
        _note_reach(ledger, True)
        save_ledger(ledger)
        return
    except chatsource.SourceUnreachable:
        # An unreachable store reads like an empty one: nothing is judged, nothing is pruned.
        result.unreachable = True
        _note_reach(ledger, False)
        save_ledger(ledger)
        return
    result.listed = len(rows)
    dirty |= _note_reach(ledger, True)
    dirty |= refused.pop(LIST_REFUSAL_KEY, None) is not None

    listed_ids: set[str] = set()
    stopped = False
    for row in rows:
        listed_ids.add(row.id)
        if row.parent_id or in_this_root(row.folder, root) or _valid_id(row.id) is None:
            result.outside += 1  # listed by id and folder only; never read
            if cold and row.id == result.first_chat:
                sessions.pop(row.id, None)  # the list places the live chat outside this root
                result.first_chat = ""
            continue
        folder = str(root)
        if cold and row.id == result.first_chat:
            continue  # the live chat: already recorded as the first chat, never marked old
        if cold:
            sessions[row.id] = _record(
                staged=False, note=NOTE_BEFORE_FIRST_SWEEP, folder=folder, at=now.isoformat(),
                started_at=row.started_at,
            )
            result.before_first_sweep += 1
            continue
        if stopped:
            result.deferred += 1
            continue
        result.scanned += 1
        outcome, changed = _settle(
            row.id, ledger, now=now, reason="sweep", folder=folder,
            listed_started=row.started_at, listed_updated=row.updated, budget=budget,
            read_timeout=read_timeout,
        )
        dirty |= changed
        if outcome == OUT_STAGED:
            result.staged.append(row.id)
        elif outcome == OUT_TOO_SHORT:
            result.too_short.append(row.id)
        elif outcome == OUT_BEFORE:
            result.before_first_sweep += 1
        elif outcome == OUT_UNCHANGED:
            result.unchanged += 1
        elif outcome == OUT_DEFERRED:
            result.deferred += 1
        elif outcome == OUT_REFUSED:
            result.refusals.append(row.id)
        elif outcome == OUT_COPY_FAILED:
            result.copy_failed += 1
        elif outcome == OUT_UNREACHABLE:
            result.unreachable = True
            stopped = True  # OpenCode is not answering: the rest are the next start's

    if not cold and not result.unreachable:
        dirty |= _prune(ledger, listed_ids, result, budget, read_timeout=read_timeout)

    if dirty:
        save_ledger(ledger)


def _prune(
    ledger: dict[str, Any],
    listed_ids: set[str],
    result: SweepResult,
    budget: Callable[[], bool],
    *,
    read_timeout: float | None = None,
) -> bool:
    """Remove records and copies of chats OpenCode itself says are gone. True when it changed
    the ledger. Least recently confirmed first, so a chat that stays unlisted (another project,
    a moved chat) costs one read per sweep at most and the others take their turn."""
    sessions: dict[str, Any] = ledger["sessions"]
    refused: dict[str, Any] = ledger["refused"]
    candidates = {
        key for key in (set(sessions) | set(refused) | _copy_ids()) - listed_ids - {LIST_REFUSAL_KEY}
        if _valid_id(key) is not None  # never hand OpenCode an id it could read as an option
    }

    def last_confirmed(key: str) -> str:
        rec = sessions.get(key)
        return str(rec.get("confirmed", "")) if isinstance(rec, dict) else ""

    gone: list[str] = []
    changed = False
    checked = 0
    for key in sorted(candidates, key=lambda k: (last_confirmed(k), k)):
        if checked >= PRUNE_CONFIRM_LIMIT or not budget():
            result.deferred += 1
            continue
        checked += 1
        try:
            chatsource.read_turns_with_stats(key, timeout=read_timeout)
        except chatsource.SessionNotFound:
            gone.append(key)
            continue
        except chatsource.SourceUnreachable:
            _note_reach(ledger, False)
            changed = True
            break  # no answer is never "gone"
        except chatsource.ChatUnreadableError:
            pass  # it exists; the refusal road is the listed chats'
        rec = sessions.get(key)
        if isinstance(rec, dict):
            rec["confirmed"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            changed = True
    for key in gone:
        sessions.pop(key, None)
        refused.pop(key, None)
        changed = True
    if gone:
        keep = _copy_ids() - set(gone)
        chatcache.prune(keep, source_reachable=True)
        result.pruned.extend(sorted(gone))
    return changed


# === The notice =============================================================================


def capture_notice() -> str:
    """Up to four context lines for a chat's start, or ''.

    The same wording shape as ``cursor_transcripts.capture_notice``, for OpenCode. Reads the
    LEDGER only (no OpenCode call), so it is fast; each staged chat and each refusal is reported
    once, then marked reported (a refusal, the unreadable list's included, is marked again by
    every sweep that meets it, so it stays loud). An unreachable streak of
    ``UNREACHABLE_NOTICE_AFTER`` or more is said once per streak. Guarded to silence: a capture
    hiccup must never cost the member their chat.
    """
    try:
        from shared import file_lock

        path = ledger_path()
        if not path.is_file():
            return ""
        with file_lock(path, timeout=NOTICE_LOCK_TIMEOUT):
            ledger = load_ledger()
            if not ledger:
                return ""
            staged = [r for r in ledger["sessions"].values()
                      if isinstance(r, dict) and r.get("notice")]
            unread = [r for k, r in ledger["refused"].items()
                      if k != LIST_REFUSAL_KEY and isinstance(r, dict) and r.get("notice")]
            listing = ledger["refused"].get(LIST_REFUSAL_KEY)
            list_line = isinstance(listing, dict) and bool(listing.get("notice"))
            misses = int(ledger.get(UNREACHABLE_KEY) or 0)
            say_misses = misses >= UNREACHABLE_NOTICE_AFTER and not ledger.get(
                UNREACHABLE_SAID_KEY
            )
            for rec in (*staged, *unread, *([listing] if list_line else [])):
                rec.pop("notice", None)
            if say_misses:
                ledger[UNREACHABLE_SAID_KEY] = True
            if staged or unread or list_line or say_misses:
                save_ledger(ledger)
        road = chatsource.ADDON_ROAD
        lines: list[str] = []
        if staged:
            plural = "s" if len(staged) != 1 else ""
            lines.append(
                f"_{len(staged)} OpenCode chat{plural} queued for your memory since last time._"
            )
        if unread:
            plural = "s" if len(unread) != 1 else ""
            lines.append(
                f"_{len(unread)} OpenCode chat{plural} could not be read into memory. {road}_"
            )
        if list_line:
            lines.append(
                "_Glitch could not read OpenCode's list of your chats, so a chat that was not "
                f"saved at the end of a turn is not being caught up. {road}_"
            )
        if say_misses:
            lines.append(
                f"_Glitch could not reach OpenCode to read your chats the last {misses} times, so "
                "they are not being saved to your memory (OpenCode may have changed or moved). "
                f"{road}_"
            )
        return "\n".join(lines)
    except Exception:  # noqa: BLE001 - the next start reports again
        return ""
