"""Tests for ``capture.py``: OpenCode chats into the pending memory queue.

The real ``opencode`` is never called. A fake executable (made in ``tmp_path``) stands in through
the ``GLITCH_OPENCODE_BIN`` door, the pattern ``test_chatsource.py`` uses, extended here to answer
per chat id: ``session list`` prints a configured list, ``session export <id>`` prints that id's
configured export, and an id it does not hold answers exactly like OpenCode 2.2.7 does (measured:
exit 1, stderr ``Session not found: <id>``). Each subcommand can be switched to ``exit`` (an
unreachable store) or ``sleep`` (a stalled one).

conftest isolates everything: the pending queue, the state folder and the data folder are a per-test
temp folder (``chatcache.state_roots()``), the hook log is per test, and the REAL queue, ledger and
copies are fingerprinted around every test.

The secrets seeded here are clearly fake, scanner-recognisable shapes, the same ones
``test_chatcache.py`` uses.

STATUS x OPERATION GRID (ledger state before -> what the operation does). ``*`` marks the surprising
cells, each with its own test below.

| state \\ op         | hook turn            | sweep                    | unchanged re-run | grown re-run              | source gone                         | unreadable, then fixed  |
|--------------------|----------------------|--------------------------|------------------|---------------------------|-------------------------------------|-------------------------|
| staged             | re-read, unchanged   | re-read, unchanged       | unchanged        | * ONE stub replaced, copy rewritten | * pruned with copy ONLY on not-found from a reachable store | refused (no marker) -> re-staged |
| too short          | re-read              | re-read                  | unchanged        | staged once at the floor  | pruned on not-found                 | refused -> staged       |
| before first sweep | never read           | never read               | never read       | * stays unstaged for good | pruned on not-found                 | never read              |
| refused            | read again           | * read again, loud again | * loud again     | read again                | pruned on not-found                 | staged, refusal cleared |
| absent             | judged by started-at | judged by list `created` | -                | -                         | nothing to prune                    | refused -> staged       |

Also: a chat absent from the list that still exports is NOT pruned (*), and an unreachable store
prunes nothing (*).
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import threading
import time
from pathlib import Path
from typing import Any

import config
import pytest
import shared
import transcript_filter

import capture
import chatcache
import chatsource
from conftest import GLITCH_ROOT

FAKE_AWS = "AKIAFAKEFAKEFAKEFAKE"
FAKE_GH = "ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234"
SECRETS = (FAKE_AWS, FAKE_GH)

OLD_MS = 1577836800000  # 2020-01-01: before any first-run stamp
SID = "ses_e34e04da3ffeMsm84Y9oT218K8"


def _new_ms() -> int:
    """A start an hour from now: after any stamp written during the test."""
    return int((time.time() + 3600) * 1000)


FAKE = r'''#!{python}
import json, os, sys, time
cfg = json.loads(open(os.environ["FAKE_OC_CONFIG"], encoding="utf-8").read())
sub = sys.argv[2] if len(sys.argv) > 2 else ""
with open(cfg["record"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({{"sub": sub, "argv": sys.argv[1:]}}) + "\n")
mode = cfg.get(sub + "_mode", "ok")
if sub == "export":
    mode = cfg.get("id_modes", {{}}).get(sys.argv[-1], mode)
if mode == "sleep":
    time.sleep(60)
if mode == "slow":
    time.sleep(cfg.get("slow_seconds", 0.6))
if mode == "exit":
    sys.stderr.write("boom\n")
    sys.exit(3)
if sub == "list":
    sys.stdout.write(cfg.get("list", "[]"))
    sys.exit(0)
if sub == "export":
    sid = sys.argv[-1]
    out = cfg.get("exports", {{}}).get(sid)
    if out is None:
        sys.stderr.write("Session not found: " + sid + "\n")
        sys.exit(1)
    sys.stdout.buffer.write(out.encode("utf-8"))
    sys.exit(0)
sys.exit(2)
'''


# --- builders (shape of the author's private test evidence) ------------------------------------


def _messages(n: int, start_ms: int, *, secret: bool = False, long: int = 0) -> list[dict]:
    """``n`` records alternating member / assistant (odd n ends on a member message)."""
    msgs: list[dict[str, Any]] = []
    for i in range(n):
        ts = start_ms + i * 1000
        body = f"message {i}" + (" " + "x" * long if long else "")
        if i % 2 == 0:
            text = body + (f" my key is {FAKE_AWS}" if secret else "")
            msgs.append({"type": "user", "id": f"u{i}", "time": {"created": ts}, "text": text,
                         "files": []})
        else:
            content: list[dict[str, Any]] = [{"type": "text", "text": body + (
                f" token {FAKE_GH}\n<local-command-stdout>AWS={FAKE_AWS}</local-command-stdout>"
                if secret else "")}]
            if secret:
                content.append({"type": "tool", "id": "t", "name": "shell", "state": {
                    "status": "completed", "input": {"command": "env"},
                    "content": [{"type": "text", "text": f"AWS_ACCESS_KEY_ID={FAKE_AWS}"}],
                    "metadata": {}}, "executed": True, "time": {"start": ts, "end": ts}})
            msgs.append({"type": "assistant", "id": f"a{i}", "time": {"created": ts},
                         "content": content, "finish": "stop"})
    return msgs


def _export(sid: str, msgs: list[dict]) -> dict[str, Any]:
    return {"info": {"id": sid, "time": {"created": 0}}, "messages": msgs}


class Fake:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.record = root / "record.jsonl"
        self.config = root / "config.json"
        self.bin = root / "opencode"
        self.bin.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv(chatsource.BIN_ENV, str(self.bin))
        monkeypatch.setenv("FAKE_OC_CONFIG", str(self.config))
        self.cfg: dict[str, Any] = {"record": str(self.record), "exports": {}, "list": "[]"}
        self.rows: list[dict[str, Any]] = []
        self._save()

    def _save(self) -> None:
        self.cfg["list"] = json.dumps(self.rows)
        self.config.write_text(json.dumps(self.cfg), encoding="utf-8")

    def chat(self, sid: str, n: int, start_ms: int, *, listed: bool = True, folder: str | None = None,
             **kw: Any) -> None:
        """Hold (or replace) chat ``sid`` with ``n`` records starting at ``start_ms``."""
        self.cfg["exports"][sid] = json.dumps(_export(sid, _messages(n, start_ms, **kw)))
        self.rows = [r for r in self.rows if r["id"] != sid]
        if listed:
            # `updated` moves with the chat, as OpenCode's does: the time of its latest record.
            self.rows.insert(0, {"id": sid, "title": "t", "created": start_ms,
                                 "updated": start_ms + max(n - 1, 0) * 1000, "projectId": "p",
                                 "directory": folder or str(GLITCH_ROOT)})
        self._save()

    def raw(self, sid: str, text: str) -> None:
        self.cfg["exports"][sid] = text
        self._save()

    def forget(self, sid: str) -> None:
        """Delete the chat from the fake store (export now answers not found; list drops it)."""
        self.cfg["exports"].pop(sid, None)
        self.rows = [r for r in self.rows if r["id"] != sid]
        self._save()

    def unlist(self, sid: str) -> None:
        self.rows = [r for r in self.rows if r["id"] != sid]
        self._save()

    def mode(self, sub: str, mode: str) -> None:
        self.cfg[f"{sub}_mode"] = mode
        self._save()

    def id_mode(self, sid: str, mode: str) -> None:
        """One chat's export only: ``exit`` / ``sleep`` / ``slow`` / ``ok``."""
        self.cfg.setdefault("id_modes", {})[sid] = mode
        self._save()

    def calls(self, sub: str | None = None) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        rows = [json.loads(x) for x in self.record.read_text(encoding="utf-8").splitlines()]
        return [r for r in rows if sub is None or r["sub"] == sub]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    root = tmp_path / "fake bin"
    root.mkdir()
    return Fake(root, monkeypatch)


# --- helpers ------------------------------------------------------------------------------------


def _ledger() -> dict[str, Any]:
    return json.loads(capture.ledger_path().read_text(encoding="utf-8"))


def _stamp() -> None:
    """A steady-state ledger: first-run stamped now, nothing recorded."""
    capture.save_ledger({"version": 1, "first_run": config.now_local().isoformat(),
                         "sessions": {}, "refused": {}})


def _stubs() -> list[Path]:
    return sorted(chatcache.state_roots().pending.glob("*.md"))


def _turn(sid: str = SID, reason: str = "turn", parent: str = "", folder: Any = None) -> str:
    return capture.capture_one(sid, str(folder or GLITCH_ROOT), parent, reason)


def _log() -> str:
    path = Path(shared.HOOK_LOG_FILE)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _header_fields(stub: Path) -> list[str]:
    head = stub.read_text(encoding="utf-8").split("\n", 1)[0]
    assert head.startswith("<!-- ") and head.endswith(" -->")
    return head[5:-4].split(" | ")


def _load_flush_hook() -> Any:
    path = GLITCH_ROOT / ".claude" / "hooks" / "session-end-flush.py"
    spec = importlib.util.spec_from_file_location("_opencode_test_session_end_flush", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# === (4) CONTRACT DRIFT =========================================================================


def test_constants_match_the_flush_hook() -> None:
    hook = _load_flush_hook()
    assert capture.MAX_TURNS == hook.MAX_TURNS == 30
    assert capture.MAX_CONTEXT_CHARS == hook.MAX_CONTEXT_CHARS == 15_000
    assert capture.MIN_TURNS_TO_STAGE == hook.MIN_TURNS_TO_FLUSH == 5


def test_render_excerpt_matches_cursor_transcripts() -> None:
    import cursor_transcripts

    for n, long in ((3, 0), (12, 0), (45, 0), (40, 900), (7, 4000)):
        turns, _ = chatsource.parse_export(SID, _export(SID, _messages(n, OLD_MS, long=long)))
        as_dicts = [{"role": t.role, "text": t.text} for t in turns]
        assert capture.render_excerpt(turns) == cursor_transcripts.render_excerpt(as_dicts)
    assert capture.STAGED_BY == "opencode-transcripts"
    assert cursor_transcripts.NOTE_STAGED == capture.NOTE_STAGED
    assert cursor_transcripts.NOTE_TOO_SHORT == capture.NOTE_TOO_SHORT
    assert cursor_transcripts.NOTE_BEFORE_FIRST_SWEEP == capture.NOTE_BEFORE_FIRST_SWEEP


def test_staged_text_matches_what_the_flush_hook_builds_from_the_copy(fake: Fake) -> None:
    hook = _load_flush_hook()
    _stamp()
    fake.chat(SID, 9, _new_ms())
    assert _turn() == "staged"
    (stub,) = _stubs()
    copy = chatcache.copy_path(SID)
    hook_context, hook_count = hook.extract_conversation_context(copy)
    body = stub.read_text(encoding="utf-8").split("\n", 2)[2]
    assert body == hook.redact_staged_context(hook_context)
    assert _header_fields(stub)[4] == f"{hook_count} turns"


def test_known_edge_whitespace_divergence_is_the_cursor_siblings() -> None:
    """Pinned, not fixed: the Claude flush hook strips each turn's text, the engine's shared reader
    (what cursor_transcripts renders from) keeps it. capture follows cursor_transcripts exactly."""
    import cursor_transcripts

    turns = [{"role": "User", "text": "  padded  "}, {"role": "Assistant", "text": "ok\n"}]
    ours, _ = capture.render_excerpt(turns)
    assert ours == cursor_transcripts.render_excerpt(turns)[0]
    stripped = [{"role": t["role"], "text": t["text"].strip()} for t in turns]
    assert ours != capture.render_excerpt(stripped)[0]


def test_stub_pointer_and_copy_read_back_through_the_engine(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 6, _new_ms())
    assert _turn() == "staged"
    (stub,) = _stubs()
    pointer = transcript_filter.pointer_from_stub(stub.read_text(encoding="utf-8"))
    assert pointer == str(chatcache.copy_path(SID))
    assert Path(pointer).is_file()
    distillate = transcript_filter.filter_transcript(Path(pointer))
    assert distillate.engine == transcript_filter.ENGINE_CLAUDE
    source = chatsource.read_turns(SID)
    read = [("User" if t.speaker == transcript_filter.SPEAKER_MEMBER else "Assistant", t.text)
            for t in distillate.turns]
    assert read == [(t.role, t.text) for t in source]


# === unit: excerpt, floor, naming, header ======================================================


def test_excerpt_bounds_turns_and_chars() -> None:
    turns, _ = chatsource.parse_export(SID, _export(SID, _messages(40, OLD_MS)))
    context, count = capture.render_excerpt(turns)
    assert count == 30
    assert context.startswith("**User:** message 10\n")
    turns, _ = chatsource.parse_export(SID, _export(SID, _messages(20, OLD_MS, long=1500)))
    context, count = capture.render_excerpt(turns)
    assert count == 20
    assert len(context) <= 15_000
    assert context.startswith("**")  # trimmed at a turn boundary, never mid-turn
    assert context.endswith("\n")


@pytest.mark.parametrize(("records", "outcome"), [(4, "too short"), (5, "staged")])
def test_floor_four_vs_five(fake: Fake, records: int, outcome: str) -> None:
    _stamp()
    fake.chat(SID, records, _new_ms())
    assert _turn() == outcome
    assert len(_stubs()) == (1 if outcome == "staged" else 0)
    rec = _ledger()["sessions"][SID]
    assert rec["note"] == ("staged" if outcome == "staged" else "too short to keep")


def test_stub_name_and_header_field_order(fake: Fake) -> None:
    assert capture.stub_name("ses_AbC") == "opencode-ses_AbC.md"
    _stamp()
    fake.chat(SID, 5, _new_ms())
    _turn(reason="compact")
    (stub,) = _stubs()
    assert stub.name == f"opencode-{SID}.md"
    fields = _header_fields(stub)
    assert fields[0] == "staged by opencode-transcripts"
    assert fields[1] == "reason=compact"
    assert fields[2] == f"session={SID}"
    assert capture._parse_stamp(fields[3]) is not None
    assert fields[4] == "5 turns"
    assert fields[5] == f"transcript={chatcache.copy_path(SID)}"  # pointer LAST
    assert len(fields) == 6
    assert stub.read_text(encoding="utf-8").split("\n")[1] == ""  # "-->\n\n"


def test_ids_verbatim_case_kept(fake: Fake) -> None:
    _stamp()
    fake.chat("ses_AbC", 5, _new_ms())
    fake.chat("ses_abc", 7, _new_ms())
    assert _turn("ses_AbC") == "staged"
    sessions = _ledger()["sessions"]
    assert "ses_AbC" in sessions and "ses_abc" not in sessions
    assert capture.stub_name("ses_AbC") != capture.stub_name("ses_abc")
    lower_stub = chatcache.state_roots().pending / capture.stub_name("ses_abc")
    if lower_stub.exists():
        # Case-insensitive filesystem: the two names land on one file on disk. Names and ledger
        # keys stay distinct; the on-disk sub-assert is skipped (chatcache's test does the same).
        return
    assert _turn("ses_abc") == "staged"
    sessions = _ledger()["sessions"]
    assert sessions["ses_AbC"]["turns"] == 5 and sessions["ses_abc"]["turns"] == 7
    assert len(_stubs()) == 2


# === unit: skips ================================================================================


def test_child_automated_outside_and_bad_id_skip(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    before = capture.ledger_path().read_bytes()
    assert _turn(parent="ses_parent") == "skipped:child"
    outside = tmp_path / "not glitch"
    outside.mkdir()
    assert _turn(folder=outside) == "skipped:outside"
    assert capture.capture_one("../evil", str(GLITCH_ROOT), "", "turn") == "skipped:bad id"
    import codex_shared

    monkeypatch.setenv(codex_shared.CODEX_RECURSION_ENV, "glitch-test")
    assert _turn() == "skipped:automated"
    assert capture.sweep().skipped == "automated"  # never sweeps from a Glitch-driven process
    assert fake.calls() == []  # nothing was even read
    assert capture.ledger_path().read_bytes() == before
    assert _stubs() == []
    assert "skipped:child" in _log()


def test_other_glitch_root_skips(fake: Fake, tmp_path: Path) -> None:
    other = tmp_path / "other glitch"
    (other / ".claude" / "hooks").mkdir(parents=True)
    (other / "CLAUDE.md").write_text("", encoding="utf-8")
    (other / ".claude" / "hooks" / "write-guard.py").write_text("", encoding="utf-8")
    _stamp()
    fake.chat(SID, 9, _new_ms())
    assert _turn(folder=other / "sub") == "skipped:other root"


def test_workspace_subfolder_counts_as_this_root(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    assert _turn(folder=GLITCH_ROOT / "workspaces" / "opencode-adapter") == "staged"


# === (2) COLD-START TWINS =======================================================================


def test_cold_start_from_sweep_stages_nothing_then_steady_state_stages(fake: Fake) -> None:
    fake.chat("ses_old1", 9, _new_ms())  # even a chat dated "new" is old on the first sweep
    fake.chat("ses_old2", 9, OLD_MS)
    result = capture.sweep()
    assert result.cold_start and result.staged == [] and result.before_first_sweep == 2
    assert _stubs() == []
    assert fake.calls("export") == []  # judged from the list alone, never read
    ledger = _ledger()
    assert capture._parse_stamp(ledger["first_run"]) is not None
    assert {k: v["note"] for k, v in ledger["sessions"].items()} == {
        "ses_old1": "started before the first sweep", "ses_old2": "started before the first sweep"}
    # Steady state: a chat that started after the stamp is staged by the next sweep.
    fake.chat("ses_new", 6, _new_ms())
    result = capture.sweep()
    assert not result.cold_start and result.staged == ["ses_new"]
    assert [p.name for p in _stubs()] == ["opencode-ses_new.md"]


def test_cold_start_from_a_turn_captures_that_chat_only(fake: Fake) -> None:
    fake.chat(SID, 9, OLD_MS)  # its turns predate the stamp: the first-chat exception lets it in
    fake.chat("ses_other", 9, OLD_MS)
    assert _turn() == "cold start"
    assert _stubs() == []  # the first run itself stages nothing
    assert _ledger()["sessions"][SID]["note"] == "first chat"
    assert _turn() == "staged"  # its next turn
    assert _turn("ses_other") == "before first sweep"  # every other existing chat stays old
    fake.chat("ses_other", 15, OLD_MS)
    assert _turn("ses_other") == "before first sweep"
    assert [p.name for p in _stubs()] == [f"opencode-{SID}.md"]


def test_cold_start_from_start_records_nothing(fake: Fake) -> None:
    fake.chat(SID, 9, OLD_MS)
    assert _turn(reason="start") == "cold start"
    assert _ledger()["sessions"] == {}
    assert _turn() == "before first sweep"  # an old chat resumed after the stamp stays old
    fake.chat("ses_fresh", 5, _new_ms())
    assert _turn("ses_fresh") == "staged"  # a chat begun after the stamp is eligible


# --- D1: the first chat after install, through the sweep its own start runs --------------------
# The measured order (docs/api-findings.md items 18-23, the author's private test evidence): the
# plugin's `session` call (and its sweep) comes BEFORE the chat's first turn end, and the session
# list already holds the live chat, created a moment before the first-run stamp.

LIVE = "ses_e348b7107ffeGc05JfJWoCx212"


def _just_now_ms() -> int:
    """A chat created a few seconds ago: BEFORE the first-run stamp the sweep is about to write."""
    return int((time.time() - 5) * 1000)


def _live_turns(fake: Fake, sid: str, start: int, turns: int) -> list[str]:
    """``turns`` member turns ending one by one, each a member + assistant record, each reported by
    the plugin's turn-end trigger. Returns the outcome of every trigger."""
    outcomes: list[str] = []
    for k in range(1, turns + 1):
        fake.chat(sid, 2 * k, start)
        outcomes.append(_turn(sid))
    return outcomes


def test_d1_first_chat_after_install_is_captured_from_its_next_turn(fake: Fake) -> None:
    fake.chat("ses_oldA", 9, OLD_MS)
    start = _just_now_ms()
    fake.chat(LIVE, 1, start)  # the live chat, listed, holding the member's first message
    result = capture.sweep(live_session_id=LIVE)  # the startup `session` call's sweep
    assert result.cold_start and result.first_chat == LIVE
    assert result.before_first_sweep == 1 and result.staged == []
    assert fake.calls("export") == []  # the cold sweep reads nothing
    notes = {k: v["note"] for k, v in _ledger()["sessions"].items()}
    assert notes == {LIVE: "first chat", "ses_oldA": "started before the first sweep"}
    assert capture._parse_stamp(_ledger()["first_run"]).timestamp() * 1000 > start  # really older

    outcomes = _live_turns(fake, LIVE, start, 5)
    assert outcomes == ["too short", "too short", "staged", "staged", "staged"]
    (stub,) = _stubs()
    assert stub.name == f"opencode-{LIVE}.md"
    assert _header_fields(stub)[4] == "10 turns"
    copy = chatcache.copy_path(LIVE)
    assert transcript_filter.pointer_from_stub(stub.read_text(encoding="utf-8")) == str(copy)
    assert len(copy.read_text(encoding="utf-8").splitlines()) == 10
    rec = _ledger()["sessions"][LIVE]
    assert rec["note"] == "staged" and rec["staged"] is True and rec["turns"] == 10

    # The other chat that was already there stays unstaged however it grows, on both roads.
    fake.chat("ses_oldA", 25, OLD_MS)
    assert _turn("ses_oldA") == "before first sweep"
    assert capture.sweep(live_session_id="ses_oldA").staged == []
    assert [p.name for p in _stubs()] == [f"opencode-{LIVE}.md"]


def test_d1_the_first_trigger_at_the_floor_stages(fake: Fake) -> None:
    """Five triggers, records 1..5: the fifth (the first with 5 records) stages."""
    start = _just_now_ms()
    fake.chat(LIVE, 1, start)
    capture.sweep(live_session_id=LIVE)
    outcomes = []
    for n in range(1, 6):
        fake.chat(LIVE, n, start)
        outcomes.append(_turn(LIVE))
    assert outcomes == ["too short"] * 4 + ["staged"]
    assert len(_stubs()) == 1


def test_d1_without_the_live_id_the_chat_is_lost_as_before(fake: Fake) -> None:
    """The twin that shows the defect: a cold sweep with no live id marks the live chat old."""
    start = _just_now_ms()
    fake.chat(LIVE, 1, start)
    capture.sweep()
    assert _live_turns(fake, LIVE, start, 5) == ["before first sweep"] * 5
    assert _stubs() == []


@pytest.mark.parametrize("how", ["unlisted", "unreachable"])
def test_d1_live_chat_not_yet_listed_or_list_unreachable_is_still_the_first_chat(
    fake: Fake, how: str
) -> None:
    fake.chat("ses_oldA", 9, OLD_MS)
    if how == "unreachable":
        fake.mode("list", "exit")
    result = capture.sweep(live_session_id=LIVE)
    assert result.cold_start and result.first_chat == LIVE
    assert result.unreachable is (how == "unreachable")
    sessions = _ledger()["sessions"]
    assert sessions[LIVE]["note"] == "first chat"
    assert set(sessions) == ({LIVE} if how == "unreachable" else {LIVE, "ses_oldA"})
    fake.mode("list", "ok")
    assert _live_turns(fake, LIVE, _just_now_ms(), 3) == ["too short", "too short", "staged"]


def test_d1_live_chat_listed_outside_this_root_is_not_recorded(fake: Fake, tmp_path: Path) -> None:
    fake.chat(LIVE, 1, _just_now_ms(), folder=str(tmp_path))
    result = capture.sweep(live_session_id=LIVE)
    assert result.cold_start and result.first_chat == "" and result.outside == 1
    assert LIVE not in _ledger()["sessions"]


@pytest.mark.parametrize("bad", ["", None, "../evil", "a/b", "ses_x\x00y", "C:\\x", " ses_x", 7])
def test_d1_an_empty_or_unsafe_live_id_is_ignored(fake: Fake, bad: Any) -> None:
    start = _just_now_ms()
    fake.chat(LIVE, 1, start)
    result = capture.sweep(live_session_id=bad)
    assert result.cold_start and result.first_chat == ""
    assert {k: v["note"] for k, v in _ledger()["sessions"].items()} == {
        LIVE: "started before the first sweep"}
    assert not any(isinstance(k, str) and "/" in k for k in _ledger()["sessions"])


def test_d1_live_id_on_an_existing_ledger_changes_nothing(fake: Fake) -> None:
    """Non-cold: the live chat is judged like any listed chat (the cp2b second-chat run)."""
    _stamp()
    start = _new_ms()
    fake.chat(LIVE, 3, start)
    fake.chat("ses_other", 6, _new_ms())
    with_live = capture.sweep(live_session_id=LIVE)
    assert not with_live.cold_start and with_live.first_chat == ""
    assert with_live.too_short == [LIVE] and with_live.staged == ["ses_other"]
    assert _ledger()["sessions"][LIVE]["note"] == "too short to keep"
    fake.chat(LIVE, 5, start)  # it grows: re-judged, and staged at the floor
    assert capture.sweep(live_session_id=LIVE).staged == [LIVE]


def test_d1_live_id_on_an_existing_ledger_matches_a_plain_sweep(fake: Fake) -> None:
    def run(live: str | None) -> dict[str, Any]:
        _stamp()
        fake.chat(LIVE, 3, _new_ms())
        fake.chat("ses_old", 9, OLD_MS)
        capture.sweep(live_session_id=live)
        out = {k: (v["note"], v.get("turns")) for k, v in _ledger()["sessions"].items()}
        capture.ledger_path().unlink()
        return out

    assert run(LIVE) == run(None)


def test_d1_a_glitch_driven_process_never_sweeps_even_with_a_live_id(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    import codex_shared

    fake.chat(LIVE, 1, _just_now_ms())
    monkeypatch.setenv(codex_shared.CODEX_RECURSION_ENV, "glitch-test")
    assert capture.sweep(live_session_id=LIVE).skipped == "automated"
    assert not capture.ledger_path().exists()
    assert fake.calls() == []


def test_d1_first_chat_record_has_one_shape_on_both_roads(fake: Fake) -> None:
    fake.chat(LIVE, 1, _just_now_ms())
    capture.sweep(live_session_id=LIVE)
    by_sweep = _ledger()["sessions"][LIVE]
    capture.ledger_path().unlink()
    assert _turn(LIVE) == "cold start"
    by_turn = _ledger()["sessions"][LIVE]
    assert set(by_sweep) == set(by_turn) == {"staged", "note", "folder", "at"}
    assert {k: by_sweep[k] for k in ("staged", "note")} == {k: by_turn[k] for k in ("staged", "note")}


# --- the sweep's bounds as arguments (no module global is touched) ------------------------------


def test_sweep_passes_its_read_timeout_to_every_opencode_call(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp()
    fake.chat(SID, 6, _new_ms())
    fake.chat("ses_gone", 5, _new_ms())
    assert capture.sweep().staged == ["ses_gone", SID]
    fake.forget("ses_gone")  # a prune candidate: confirmed with one read
    fake.chat(SID, 8, _new_ms())
    seen: list[tuple[str, Any]] = []
    real_list, real_read = chatsource.list_sessions, chatsource.read_turns_with_stats

    def spy_list(**kw: Any) -> Any:
        seen.append(("list", kw.get("timeout")))
        return real_list(**kw)

    def spy_read(sid: str, **kw: Any) -> Any:
        seen.append(("export", kw.get("timeout")))
        return real_read(sid, **kw)

    monkeypatch.setattr(chatsource, "list_sessions", spy_list)
    monkeypatch.setattr(chatsource, "read_turns_with_stats", spy_read)
    result = capture.sweep(read_timeout=3.5)
    assert result.staged == [SID] and result.pruned == ["ses_gone"]
    assert seen == [("list", 3.5), ("export", 3.5), ("export", 3.5)]
    assert chatsource.TIMEOUT_SECONDS == 20.0 and capture.LOCK_TIMEOUT == 10.0


def test_sweep_read_timeout_bounds_a_stalled_store(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    fake.mode("export", "sleep")
    began = time.monotonic()
    result = capture.sweep(read_timeout=1.0, budget_seconds=1.5)
    assert time.monotonic() - began < 6.0
    assert result.unreachable and result.staged == []
    assert chatsource.TIMEOUT_SECONDS == 20.0


def test_sweep_lock_timeout_is_an_argument(fake: Fake) -> None:
    _stamp()
    with shared.file_lock(capture.ledger_path(), timeout=5):
        began = time.monotonic()
        result = capture.sweep(lock_timeout=0.2)
        assert time.monotonic() - began < 3.0
    assert result.busy
    assert capture.LOCK_TIMEOUT == 10.0


# --- the updated hint ----------------------------------------------------------------------------


def test_list_row_updated_is_parsed_like_created() -> None:
    rows = chatsource.parse_list(
        [{"id": "a", "created": 1794409031000, "updated": 1794409040000},
         {"id": "b", "created": 1794409031000}], cwd="/w")
    assert rows[0].updated == "2026-11-11T14:57:20Z"
    assert rows[1].updated is None
    assert rows[0] == chatsource.SessionInfo("a", "2026-11-11T14:57:11Z", "/w", "")


def test_sweep_skips_rereading_a_staged_chat_whose_updated_is_unchanged(fake: Fake) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 5, start)
    assert capture.sweep().staged == [SID]
    assert _ledger()["sessions"][SID]["listed_updated"] == chatsource._iso(fake.rows[0]["updated"])
    exports = len(fake.calls("export"))
    result = capture.sweep()
    assert result.unchanged == 1 and len(fake.calls("export")) == exports  # no read
    fake.chat(SID, 7, start)  # it grows: `updated` moves, so it is read and restaged
    assert capture.sweep().staged == [SID]
    assert _ledger()["sessions"][SID]["turns"] == 7


def test_sweep_skips_rereading_a_too_short_chat_until_it_grows(fake: Fake) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 3, start)
    assert capture.sweep().too_short == [SID]
    exports = len(fake.calls("export"))
    assert capture.sweep().unchanged == 1 and len(fake.calls("export")) == exports
    fake.chat(SID, 5, start)
    assert capture.sweep().staged == [SID]


def test_a_hook_staged_chat_is_read_once_by_the_sweep_then_skipped(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    assert _turn() == "staged"
    assert "listed_updated" not in _ledger()["sessions"][SID]  # the hook road has no list row
    exports = len(fake.calls("export"))
    assert capture.sweep().unchanged == 1
    assert len(fake.calls("export")) == exports + 1  # read: nothing recorded to compare
    assert "listed_updated" in _ledger()["sessions"][SID]
    assert capture.sweep().unchanged == 1
    assert len(fake.calls("export")) == exports + 1  # now skipped


@pytest.mark.parametrize("updated", [None, "not a time", "2026-11-11T14:57:20"],
                         ids=["absent", "unparsable", "naive"])
def test_an_absent_or_unparsable_updated_reads_as_today(fake: Fake, updated: Any) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    for row in fake.rows:
        if updated is None:
            row.pop("updated")
        else:
            row["updated"] = updated
    fake._save()
    assert capture.sweep().staged == [SID]
    assert "listed_updated" not in _ledger()["sessions"][SID]
    for _ in range(2):
        before = len(fake.calls("export"))
        assert capture.sweep().unchanged == 1
        assert len(fake.calls("export")) == before + 1  # read every time, as before


def test_a_refused_chat_is_never_skipped_by_the_updated_hint(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    assert capture.sweep().staged == [SID]
    fake.raw(SID, "not json")  # the export breaks; the list row (and its updated) does not move
    ledger = _ledger()
    ledger["refused"][SID] = {"why": "x", "folder": str(GLITCH_ROOT), "at": "t", "notice": True}
    capture.save_ledger(ledger)
    assert capture.sweep().refusals == [SID]  # read again, loud again


def test_first_copy_creates_folder_then_reuses_it(fake: Fake) -> None:
    _stamp()
    assert not chatcache.chats_dir().exists()
    fake.chat("ses_one", 5, _new_ms())
    assert _turn("ses_one") == "staged"
    assert chatcache.copy_path("ses_one").is_file()
    fake.chat("ses_two", 5, _new_ms())
    assert _turn("ses_two") == "staged"
    assert sorted(p.name for p in chatcache.chats_dir().iterdir()) == [
        "ses_one.jsonl", "ses_two.jsonl"]


@pytest.mark.parametrize("garbage", ["{not json", "[]", '{"version": 1}',
                                     ('{"version": 1, "first_run": "2026-01-01T00:00:00", '
                                      '"sessions": {}}')])
def test_corrupt_ledger_is_a_cold_start(fake: Fake, garbage: str) -> None:
    path = capture.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(garbage, encoding="utf-8")
    fake.chat(SID, 9, _new_ms())
    result = capture.sweep()
    assert result.cold_start and result.staged == []
    assert _ledger()["sessions"][SID]["note"] == "started before the first sweep"


# === (3) THE GRID's surprising cells ===========================================================


def test_before_first_sweep_chat_that_grows_stays_unstaged(fake: Fake) -> None:
    fake.chat(SID, 9, OLD_MS)
    capture.sweep()  # cold: marks it old
    fake.chat(SID, 25, OLD_MS)
    assert _turn() == "before first sweep"
    result = capture.sweep()
    assert result.staged == [] and result.before_first_sweep == 1
    assert _stubs() == []
    assert fake.calls("export") == []  # never read, however much it grows


def test_staged_chat_that_grows_replaces_its_one_stub_and_copy(fake: Fake) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 5, start)
    assert _turn() == "staged"
    copy = chatcache.copy_path(SID)
    assert len(copy.read_text(encoding="utf-8").splitlines()) == 5
    assert _turn() == "unchanged"
    fake.chat(SID, 11, start)
    assert _turn() == "staged"
    (stub,) = _stubs()
    assert _header_fields(stub)[4] == "11 turns"
    assert len(copy.read_text(encoding="utf-8").splitlines()) == 11
    assert len(list(chatcache.chats_dir().iterdir())) == 1
    assert _ledger()["sessions"][SID]["turns"] == 11


def test_too_short_chat_staged_once_it_reaches_the_floor(fake: Fake) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 3, start)
    assert _turn() == "too short"
    assert _turn() == "unchanged"
    fake.chat(SID, 5, start)
    assert capture.sweep().staged == [SID]


def test_refused_chat_records_no_marker_and_is_loud_every_sweep(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    fake.raw(SID, json.dumps({"info": {"id": SID}, "messages": "not a list " + FAKE_AWS}))
    first = capture.sweep()
    assert first.refusals == [SID]
    ledger = _ledger()
    assert SID not in ledger["sessions"]  # no marker
    assert ledger["refused"][SID]["why"] == "the export has no messages list"
    notice = capture.capture_notice()
    assert "1 OpenCode chat could not be read into memory" in notice
    assert capture.capture_notice() == ""  # reported once...
    second = capture.sweep()
    assert second.refusals == [SID]  # ...read again, loud again
    assert len(fake.calls("export")) == 2
    assert "could not be read" in capture.capture_notice()
    # Fixed: staged, refusal cleared.
    fake.chat(SID, 9, _new_ms())
    assert capture.sweep().staged == [SID]
    assert SID not in _ledger()["refused"]
    assert FAKE_AWS not in _log()


def test_gone_chat_pruned_with_its_copy_only_on_not_found(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    fake.chat("ses_kept", 5, _new_ms())
    assert capture.sweep().staged == ["ses_kept", SID]
    assert chatcache.copy_path(SID).is_file()
    # Unreachable store: reads like an empty one, prunes nothing.
    fake.forget(SID)
    fake.mode("list", "exit")
    result = capture.sweep()
    assert result.unreachable and result.pruned == []
    assert SID in _ledger()["sessions"] and chatcache.copy_path(SID).is_file()
    # Reachable list, listed chats read fine, but the CONFIRMING export of the unlisted one
    # cannot reach OpenCode: no answer is never "gone", so still nothing pruned.
    fake.mode("list", "ok")
    fake.id_mode(SID, "exit")
    result = capture.sweep()
    assert not result.unreachable and result.unchanged == 1
    assert result.pruned == []
    assert SID in _ledger()["sessions"] and chatcache.copy_path(SID).is_file()
    # Reachable, and OpenCode says not found: pruned, record and copy.
    fake.id_mode(SID, "ok")
    result = capture.sweep()
    assert result.pruned == [SID]
    assert SID not in _ledger()["sessions"]
    assert not chatcache.copy_path(SID).exists()
    assert chatcache.copy_path("ses_kept").is_file()
    assert (chatcache.state_roots().pending / capture.stub_name(SID)).is_file()  # queue's own


def test_listed_absent_chat_that_still_exports_is_not_pruned(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    assert _turn() == "staged"
    fake.unlist(SID)  # e.g. another project's list, or a moved chat
    result = capture.sweep()
    assert result.pruned == []
    assert SID in _ledger()["sessions"] and chatcache.copy_path(SID).is_file()
    assert "confirmed" in _ledger()["sessions"][SID]


def test_unreachable_source_on_a_turn_changes_nothing_but_the_miss_count(fake: Fake) -> None:
    _stamp()
    fake.mode("export", "exit")
    before = _ledger()
    assert _turn() == "unreachable"
    after = _ledger()
    assert after.pop(capture.UNREACHABLE_KEY) == 1  # B1: counted, so a streak becomes visible
    assert after == before


# === (1) INVARIANT SWEEP ========================================================================


def test_no_secret_anywhere_under_the_capture_root_or_the_log(
    fake: Fake, _isolate_opencode_capture: Path
) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 7, start, secret=True)
    assert _turn() == "staged"
    fake.chat(SID, 12, start, secret=True)
    fake.chat("ses_b", 9, _new_ms(), secret=True)
    capture.sweep()
    fake.raw("ses_c", json.dumps({"info": {"id": "ses_c"}, "messages": [FAKE_AWS]}))
    fake.rows.insert(0, {"id": "ses_c", "created": _new_ms(), "directory": str(GLITCH_ROOT)})
    fake._save()
    capture.sweep()
    capture.capture_notice()
    files = [p for p in _isolate_opencode_capture.rglob("*") if p.is_file()]
    files.append(Path(shared.HOOK_LOG_FILE))
    names = {p.name for p in files}
    assert f"opencode-{SID}.md" in names and f"{SID}.jsonl" in names
    assert "opencode-staged.json" in names
    for path in files:
        data = path.read_bytes()
        for secret in SECRETS:
            assert secret.encode() not in data, f"secret found in {path.name}"
    stub = (chatcache.state_roots().pending / f"opencode-{SID}.md").read_text(encoding="utf-8")
    assert "[REDACTED:aws-access-key]" in stub


# === unit: the sweep's bounds ===================================================================


def test_sweep_bounded_by_count_and_newest_first(fake: Fake) -> None:
    _stamp()
    base = _new_ms()
    for i in range(5):
        fake.chat(f"ses_{i}", 5, base + i)  # each insert goes to the top: ses_4 is newest
    result = capture.sweep(limit=2)
    assert result.staged == ["ses_4", "ses_3"] and result.deferred == 3
    # The list shows ses_4 and ses_3 as they were staged: no read, no budget spent on them.
    result = capture.sweep(limit=2)
    assert result.unchanged == 2 and result.staged == ["ses_2", "ses_1"] and result.deferred == 1
    assert capture.sweep(limit=20).staged == ["ses_0"]


def test_unchanged_reads_still_count_when_the_list_carries_no_updated(fake: Fake) -> None:
    _stamp()
    base = _new_ms()
    for i in range(5):
        fake.chat(f"ses_{i}", 5, base + i)
    for row in fake.rows:
        row.pop("updated")
    fake._save()
    result = capture.sweep(limit=2)
    assert result.staged == ["ses_4", "ses_3"] and result.deferred == 3
    result = capture.sweep(limit=2)
    assert result.unchanged == 2 and result.deferred == 3  # unchanged reads still count
    assert capture.sweep(limit=20).staged == ["ses_2", "ses_1", "ses_0"]


def test_stalled_source_is_bounded_by_the_sweep_budget(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp()
    for i in range(5):
        fake.chat(f"ses_{i}", 5, _new_ms())
    fake.mode("export", "sleep")
    monkeypatch.setattr(chatsource, "TIMEOUT_SECONDS", 1.0)
    began = time.monotonic()
    result = capture.sweep(budget_seconds=1.5)
    assert time.monotonic() - began < 6.0
    assert result.staged == [] and result.unreachable
    assert result.deferred == 4  # one read timed out; the rest left for the next start
    assert _ledger()["sessions"] == {}


def test_sweep_budget_by_wall_clock(fake: Fake) -> None:
    _stamp()
    for i in range(6):
        fake.chat(f"ses_{i}", 5, _new_ms())
    fake.mode("export", "slow")  # each read takes about 0.6 s
    began = time.monotonic()
    result = capture.sweep(budget_seconds=1.0)
    assert time.monotonic() - began < 4.0
    assert 1 <= len(result.staged) <= 3
    assert len(result.staged) + result.deferred == 6  # nothing lost: the rest are deferred


def test_sweep_never_reads_outside_or_child_rows(fake: Fake, tmp_path: Path) -> None:
    _stamp()
    fake.chat("ses_out", 9, _new_ms(), folder=str(tmp_path))
    result = capture.sweep()
    assert result.outside == 1 and fake.calls("export") == []
    list_call = fake.calls("list")[0]
    assert list_call["argv"] == ["session", "list", "--standalone", "--format", "json"]


# === unit: copy failure, spaces, concurrency ===================================================


def test_copy_refused_writes_no_stub(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    _stamp()
    fake.chat(SID, 6, _new_ms())
    monkeypatch.setitem(sys.modules, "secret_scan", None)  # redactor gone: chatcache refuses
    assert _turn() == "copy failed"
    assert _stubs() == []
    assert not chatcache.copy_path(SID).exists()
    assert SID not in _ledger()["sessions"]
    assert "opencode-capture | turn | ERROR" in _log() and "copy failed (CacheRefused)" in _log()


def test_copy_oserror_keeps_previous_stub_and_copy(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    _stamp()
    start = _new_ms()
    fake.chat(SID, 5, start)
    assert _turn() == "staged"
    (stub,) = _stubs()
    before = stub.read_bytes()

    def disk_full(*_a: Any, **_k: Any) -> Any:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chatcache, "write_copy", disk_full)
    fake.chat(SID, 9, start)
    assert _turn() == "copy failed"
    assert stub.read_bytes() == before
    assert _ledger()["sessions"][SID]["turns"] == 5  # nothing recorded: retried next time


def test_redact_degrades_to_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "secret_scan", None)
    assert capture.redact(f"k={FAKE_AWS}") == f"k={FAKE_AWS}"


def test_paths_with_spaces_and_non_ascii(
    fake: Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "my glitch ü 日本"
    (root / ".claude" / "hooks").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("", encoding="utf-8")
    (root / ".claude" / "hooks" / "write-guard.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(config, "PROJECT_ROOT", root)
    state = tmp_path / "capture state é"
    state.mkdir()
    monkeypatch.setenv(chatcache.CAPTURE_STATE_ENV, str(state))
    _stamp()
    fake.chat(SID, 5, _new_ms(), folder=str(root))
    assert capture.capture_one(SID, str(root / "work space"), "", "turn") == "staged"
    (stub,) = _stubs()
    pointer = transcript_filter.pointer_from_stub(stub.read_text(encoding="utf-8"))
    assert pointer == str(state / "data" / "opencode-chats" / f"{SID}.jsonl")
    assert len(transcript_filter.filter_transcript(Path(pointer)).turns) == 5
    assert capture.sweep().unchanged == 1  # the list ran with cwd = that root


def test_two_concurrent_captures_leave_one_consistent_stub(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 7, _new_ms())
    outcomes: list[str] = []
    threads = [threading.Thread(target=lambda: outcomes.append(_turn())) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["staged", "unchanged"]
    (stub,) = _stubs()
    assert _header_fields(stub)[4] == "7 turns"
    ledger = _ledger()
    assert ledger["sessions"][SID]["note"] == "staged"
    pending = chatcache.state_roots().pending
    assert [p.name for p in pending.iterdir()] == [stub.name]  # no stray temp files
    assert len(transcript_filter.filter_transcript(chatcache.copy_path(SID)).turns) == 7


# === unit: the notice ===========================================================================


def test_notice_wording_once_and_reads_no_opencode(fake: Fake) -> None:
    assert capture.capture_notice() == ""  # no ledger yet
    _stamp()
    fake.chat("ses_a", 5, _new_ms())
    fake.chat("ses_b", 5, _new_ms())
    capture.sweep()
    calls = len(fake.calls())
    assert capture.capture_notice() == "_2 OpenCode chats queued for your memory since last time._"
    assert len(fake.calls()) == calls  # the ledger only
    assert capture.capture_notice() == ""  # not repeated
    fake.chat("ses_a", 7, _new_ms())
    capture.sweep()
    assert capture.capture_notice() == "_1 OpenCode chat queued for your memory since last time._"


ADDON_ROAD = (
    "This is part of the OpenCode add-on, not Glitch itself. Re-install the add-on from its "
    "folder; if it keeps happening, tell the add-on's author."
)


def test_notice_unread_line_names_the_addon_road(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    fake.raw(SID, "not json")
    capture.sweep()
    notice = capture.capture_notice()
    assert notice.startswith("_1 OpenCode chat could not be read into memory.")
    assert ADDON_ROAD in notice
    assert "/raise-issue" not in notice and "Glitch team" not in notice and "/update" not in notice


def _string_constants(module: Any) -> list[str]:
    import ast

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def test_no_string_in_the_capture_modules_sends_the_member_to_the_glitch_team() -> None:
    """B3: a reader or format problem is the add-on's, never Glitch's: no string in these modules
    (member-facing message, docstring or otherwise) names /raise-issue or the Glitch team."""
    import context

    for module in (capture, chatsource, chatcache, context):
        for text in _string_constants(module):
            assert "/raise-issue" not in text, (module.__name__, text)
            assert "glitch team" not in text.lower(), (module.__name__, text)


# === B1: an OpenCode that cannot be reached, again and again, becomes visible ===================


def test_unreachable_streak_counts_and_any_success_resets(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    for n in (1, 2, 3):
        _turn()
        assert _ledger()[capture.UNREACHABLE_KEY] == n
    fake.mode("export", "ok")
    assert _turn() == "staged"
    assert _ledger().get(capture.UNREACHABLE_KEY, 0) == 0


def test_a_missing_binary_counts_as_unreachable(fake: Fake, monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    _stamp()
    monkeypatch.setenv(chatsource.BIN_ENV, str(tmp_path / "no such opencode"))
    assert _turn() == "unreachable"
    assert _ledger()[capture.UNREACHABLE_KEY] == 1


def test_not_found_and_refusal_are_answers_and_reset_the_streak(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    _turn()
    _turn()
    fake.mode("export", "ok")
    assert _turn("ses_gone") == "not found"
    assert _ledger().get(capture.UNREACHABLE_KEY, 0) == 0
    fake.mode("export", "exit")
    _turn()
    fake.mode("export", "ok")
    fake.raw(SID, "not json")
    assert _turn() == "refused"
    assert _ledger().get(capture.UNREACHABLE_KEY, 0) == 0


def test_sweep_unreachable_list_counts_and_a_listed_answer_resets(fake: Fake) -> None:
    _stamp()
    fake.mode("list", "exit")
    capture.sweep()
    capture.sweep()
    assert _ledger()[capture.UNREACHABLE_KEY] == 2
    fake.mode("list", "ok")
    capture.sweep()
    assert _ledger().get(capture.UNREACHABLE_KEY, 0) == 0


def test_old_ledger_without_the_counter_reads_as_zero(fake: Fake) -> None:
    _stamp()  # the version-1 shape written before the counter existed
    assert capture.UNREACHABLE_KEY not in _ledger()
    assert capture.load_ledger()[capture.UNREACHABLE_KEY] == 0
    assert capture.capture_notice() == ""
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    _turn()
    assert _ledger()[capture.UNREACHABLE_KEY] == 1


UNREACHABLE_LINE = (
    "_Glitch could not reach OpenCode to read your chats the last 3 times, so they are not being "
    "saved to your memory (OpenCode may have changed or moved). " + ADDON_ROAD + "_"
)


def test_notice_says_the_unreachable_line_once_per_streak_at_three(fake: Fake) -> None:
    assert capture.UNREACHABLE_NOTICE_AFTER == 3
    _stamp()
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    _turn()
    _turn()
    assert capture.capture_notice() == ""  # two in a row: OpenCode closing, not a break
    _turn()
    assert capture.capture_notice() == UNREACHABLE_LINE
    assert capture.capture_notice() == ""  # one-shot per streak
    _turn()
    assert capture.capture_notice() == ""  # the same streak, already said
    fake.mode("export", "ok")
    assert _turn() == "staged"  # success clears the streak and its notice
    assert capture.capture_notice() == "_1 OpenCode chat queued for your memory since last time._"
    fake.mode("export", "exit")
    for _ in range(3):
        _turn()
    assert capture.capture_notice() == UNREACHABLE_LINE  # a new streak is said again


def test_notice_unreachable_line_counts_sweep_misses_too(fake: Fake) -> None:
    _stamp()
    fake.mode("list", "exit")
    for _ in range(3):
        capture.sweep()
    assert capture.capture_notice() == UNREACHABLE_LINE


def test_hook_road_flags_the_streak_once_at_the_threshold(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    assert [_turn() for _ in range(4)] == [
        "unreachable", "unreachable", capture.OUT_UNREACHABLE_STREAK, "unreachable",
    ]
    fake.mode("export", "ok")
    assert _turn() == "staged"
    fake.mode("export", "exit")
    assert [_turn() for _ in range(3)][-1] == capture.OUT_UNREACHABLE_STREAK  # a new streak


def test_hook_road_flags_a_streak_the_sweeps_built(fake: Fake) -> None:
    """Three start sweeps that could not reach OpenCode: the next turn's capture is the loud one."""
    _stamp()
    fake.mode("list", "exit")
    for _ in range(3):
        capture.sweep()
    fake.chat(SID, 9, _new_ms())
    fake.mode("export", "exit")
    assert _turn() == capture.OUT_UNREACHABLE_STREAK
    assert _turn() == "unreachable"


def test_streak_logic_lives_in_one_helper() -> None:
    import inspect
    import re

    helper = inspect.getsource(capture._note_reach)
    # load_ledger only normalises a missing or damaged count to 0 on read; it counts nothing.
    rest = inspect.getsource(capture).replace(helper, "").replace(
        inspect.getsource(capture.load_ledger), "")
    assign = re.compile(r"\[UNREACHABLE_KEY\]\s*[+-]?=")
    assert assign.search(helper)
    assert not assign.search(rest), "the miss count is written outside _note_reach"


# === B4: the hook road finishes well inside the plugin's capture timeout =========================


def test_hook_road_worst_case_fits_inside_the_plugin_capture_timeout() -> None:
    import re

    from conftest import WORKSPACE

    ts = (WORKSPACE / "src" / "plugin" / "bridge-client.ts").read_text(encoding="utf-8")
    match = re.search(r"\bcapture:\s*([\d_]+)", ts)
    assert match is not None
    capture_s = int(match.group(1).replace("_", "")) / 1000
    assert capture.HOOK_WORST_CASE_SECONDS == (
        capture.HOOK_LOCK_TIMEOUT + capture.HOOK_READ_TIMEOUT + chatsource._KILL_GRACE_SECONDS
    )
    # One second of margin for uv and the interpreter.
    assert capture.HOOK_WORST_CASE_SECONDS + 1.0 <= capture_s, (capture.HOOK_WORST_CASE_SECONDS,
                                                                 capture_s)
    assert capture.HOOK_LOCK_TIMEOUT <= 3.0 and capture.HOOK_READ_TIMEOUT <= 4.0
    assert capture.HOOK_READ_TIMEOUT < capture_s  # the child's own timeout is below the parent's


def test_hook_road_passes_its_bounds_to_the_lock_and_the_read(
    fake: Fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stamp()
    fake.chat(SID, 9, _new_ms())
    seen: dict[str, Any] = {}
    real_lock = shared.file_lock
    real_read = chatsource.read_turns_with_stats

    def lock(path: Any, timeout: float = 10.0) -> Any:
        if Path(path) == capture.ledger_path():  # other engine writes take their own locks
            seen["lock"] = timeout
        return real_lock(path, timeout=timeout)

    def read(session_id: str, **kw: Any) -> Any:
        seen["read"] = kw.get("timeout")
        return real_read(session_id, **kw)

    monkeypatch.setattr(shared, "file_lock", lock)
    monkeypatch.setattr(chatsource, "read_turns_with_stats", read)
    assert _turn() == "staged"
    assert seen == {"lock": capture.HOOK_LOCK_TIMEOUT, "read": capture.HOOK_READ_TIMEOUT}


# === B5: an unreadable list of chats is recorded and reported while it lasts ====================


def test_unreadable_list_is_recorded_reported_and_cleared(fake: Fake) -> None:
    _stamp()
    fake.cfg["list"] = '{"not": "a list"}'
    fake.config.write_text(json.dumps(fake.cfg), encoding="utf-8")
    result = capture.sweep()
    assert result.list_unreadable
    rec = _ledger()["refused"][capture.LIST_REFUSAL_KEY]
    assert rec["why"] == "the list is not a JSON array" and rec["notice"] is True
    notice = capture.capture_notice()
    assert notice == LIST_LINE
    assert capture.capture_notice() == ""  # said once per start
    capture.sweep()
    assert capture.capture_notice() == LIST_LINE  # still broken: said again
    fake.chat("ses_new", 5, _new_ms())  # a readable list again
    capture.sweep()
    assert capture.LIST_REFUSAL_KEY not in _ledger()["refused"]
    assert "list of chats" not in capture.capture_notice()


def test_unreadable_list_key_is_never_read_as_a_chat(fake: Fake) -> None:
    _stamp()
    fake.cfg["list"] = "not json"
    fake.config.write_text(json.dumps(fake.cfg), encoding="utf-8")
    capture.sweep()
    fake.chat("ses_new", 5, _new_ms())
    capture.sweep()
    assert all(capture.LIST_REFUSAL_KEY not in c["argv"] for c in fake.calls())


LIST_LINE = (
    "_Glitch could not read OpenCode's list of your chats, so a chat that was not saved at the end "
    "of a turn is not being caught up. " + ADDON_ROAD + "_"
)


# === B9: an id that could read as an option never reaches OpenCode ==============================


def test_prune_never_reads_an_unsafe_id(fake: Fake) -> None:
    _stamp()
    chats = chatcache.chats_dir()
    chats.mkdir(parents=True, exist_ok=True)
    (chats / "--help.jsonl").write_text("", encoding="utf-8")
    capture.sweep()
    assert all("--help" not in c["argv"] for c in fake.calls())


def test_notice_silent_on_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _stamp()

    def boom() -> Path:
        raise RuntimeError("x")

    with monkeypatch.context() as m:
        m.setattr(capture, "ledger_path", boom)
        assert capture.capture_notice() == ""
    with monkeypatch.context() as m:
        m.setattr(capture, "load_ledger", boom)
        assert capture.capture_notice() == ""
    with monkeypatch.context() as m:
        m.setenv(chatcache.CAPTURE_STATE_ENV, "relative/not-temp")  # state_roots refuses
        assert capture.capture_notice() == ""


# === unit: the payload door ====================================================================


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"session_id": "", "session_dir": "/x", "reason": "turn"},
        {"session_id": SID, "session_dir": "", "reason": "turn"},
        {"session_id": SID, "session_dir": "/x", "reason": "idle"},
        {"session_id": SID, "session_dir": "/x", "reason": "turn", "parent_id": 3},
    ],
)
def test_capture_payload_rejects_bad_payloads(payload: Any) -> None:
    with pytest.raises(ValueError):
        capture.capture_payload(payload)


def test_capture_payload_runs_capture_one(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    payload = {"session_id": SID, "session_dir": str(GLITCH_ROOT), "reason": "turn"}
    assert capture.capture_payload(payload) == "staged"
    assert capture.capture_payload({**payload, "parent_id": "ses_p"}) == "skipped:child"


def test_log_lines_carry_labels_only(fake: Fake) -> None:
    _stamp()
    fake.chat(SID, 5, _new_ms())
    _turn()
    capture.sweep()
    log = _log()
    assert "opencode-capture | turn | OK" in log and "| staged" in log
    assert "opencode-capture | sweep | OK" in log
    assert "message 0" not in log and SID not in log
    assert os.fspath(GLITCH_ROOT) not in log
