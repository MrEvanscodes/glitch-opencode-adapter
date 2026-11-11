"""CP6, the end-to-end capture check: an OpenCode chat staged by the real capture path is read by the
engine's digest at FULL depth (the transcript), not the fallback excerpt.

The chain under test, every link the real module:

    fake ``opencode`` (session list / session export)
      -> capture.sweep (the chat's own start, cold: the first chat after install)
      -> capture.capture_one per turn end (the plugin's turn-end trigger)
      -> chatcache copy + pending stub + ledger
      -> headless_digest.read_session(stub)    (the engine's keeper read)
      -> transcript_filter.py <copy>            (the engine's layer-0 CLI, a real subprocess)

The real ``opencode`` is never called: the fake executable is the ``test_capture.py`` pattern (the
``GLITCH_OPENCODE_BIN`` door), cut down to ``session list`` and ``session export <id>``.
The export shape is the one measured in the real run of docs/api-findings.md item 36
(`the author's private test evidence`): per turn one ``user``, one ``assistant`` and one
``idle`` record, five turns, 15 records.

conftest isolates everything: the pending queue, the ledger and the copies are a per-test temp folder
(``chatcache.state_roots()`` through ``GLITCH_OPENCODE_CAPTURE_STATE``), and the REAL queue, ledger
and copies are fingerprinted around every test. ``read_session`` reads only the stub it is handed
and the file the stub's pointer names; it claims and consumes nothing.
"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import headless_digest
import pytest
import transcript_filter

import capture
import chatcache
import chatsource
from conftest import GLITCH_ROOT, SCRIPTS_DIR

SID = "ses_e3450e3a4ffe3vimxrCeKR5CEH"  # the real run's id shape (item 36)
MARKERS = [f"E2E-ALPHA-{n}" for n in range(1, 6)]
PROMPTS = [f"Reply with exactly the word {m}. Use no tools." for m in MARKERS]  # the author's private test harness prompt

FAKE = r'''#!{python}
import json, os, sys
cfg = json.loads(open(os.environ["FAKE_OC_CONFIG"], encoding="utf-8").read())
sub = sys.argv[2] if len(sys.argv) > 2 else ""
if sub == "list":
    sys.stdout.write(json.dumps(cfg["list"]))
    sys.exit(0)
if sub == "export":
    sid = sys.argv[-1]
    out = cfg["exports"].get(sid)
    if out is None:
        sys.stderr.write("Session not found: " + sid + "\n")
        sys.exit(1)
    sys.stdout.buffer.write(json.dumps(out).encode("utf-8"))
    sys.exit(0)
sys.exit(2)
'''


def _records(turns: int, start_ms: int) -> list[dict[str, Any]]:
    """``turns`` member turns as OpenCode exports them: user, assistant, idle."""
    out: list[dict[str, Any]] = []
    for i in range(turns):
        ts = start_ms + i * 3000
        out.append({"type": "user", "id": f"msg_u{i}", "time": {"created": ts}, "text": PROMPTS[i],
                    "files": []})
        out.append({"type": "assistant", "id": f"msg_a{i}", "time": {"created": ts + 1000},
                    "content": [{"type": "text", "text": MARKERS[i]}], "finish": "stop"})
        out.append({"type": "idle", "id": f"msg_i{i}", "time": {"created": ts + 2000}})
    return out


class FakeOpenCode:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config = root / "config.json"
        self.bin = root / "opencode"
        self.bin.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv(chatsource.BIN_ENV, str(self.bin))
        monkeypatch.setenv("FAKE_OC_CONFIG", str(self.config))

    def hold(self, sid: str, turns: int, start_ms: int) -> None:
        """The store now holds chat ``sid`` with ``turns`` member turns (0 = just started)."""
        recs = _records(turns, start_ms)
        if turns == 0:  # the live chat at its start: the member's first message only
            recs = _records(1, start_ms)[:1]
        updated = recs[-1]["time"]["created"]
        cfg = {"list": [{"id": sid, "title": "t", "created": start_ms, "updated": updated,
                         "projectId": "p", "directory": str(GLITCH_ROOT)}],
               "exports": {sid: {"info": {"id": sid, "time": {"created": start_ms}}, "messages": recs}}}
        self.config.write_text(json.dumps(cfg), encoding="utf-8")


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A five-turn chat staged through the real capture path; returns (stub, copy)."""
    root = tmp_path / "fake bin"
    root.mkdir()
    fake = FakeOpenCode(root, monkeypatch)
    start = int((time.time() - 5) * 1000)  # created a moment before the first-run stamp
    fake.hold(SID, 0, start)
    result = capture.sweep(live_session_id=SID)  # the chat's own start: a fresh install
    assert result.cold_start and result.first_chat == SID
    outcomes = []
    for k in range(1, 6):
        fake.hold(SID, k, start)
        outcomes.append(capture.capture_one(SID, str(GLITCH_ROOT), "", "turn"))
    assert outcomes == ["too short", "too short", "staged", "staged", "staged"]  # item 36, as measured

    roots = chatcache.state_roots()
    stubs = sorted(roots.pending.glob("*.md"))
    assert [p.name for p in stubs] == [f"opencode-{SID}.md"]
    copy = chatcache.copy_path(SID)
    assert copy.is_file()
    ledger = json.loads(capture.ledger_path().read_text(encoding="utf-8"))
    rec = ledger["sessions"][SID]
    assert rec["note"] == "staged" and rec["staged"] is True and rec["turns"] == 10
    return stubs[0], copy


def _member_blocks(text: str) -> list[str]:
    """The member's turns out of a layer-0 distillate's text (``MEMBER:\\n<text>`` blocks)."""
    return [b.split("\n", 1)[1] for b in text.split("\n\n") if b.startswith("MEMBER:\n")]


def test_read_session_reads_the_staged_chat_from_its_transcript(staged: tuple[Path, Path]) -> None:
    stub, copy = staged
    assert transcript_filter.pointer_from_stub(stub.read_text(encoding="utf-8")) == str(copy)
    read = headless_digest.read_session(stub)
    assert read.source == headless_digest.SOURCE_TRANSCRIPT, read.note
    assert read.note == ""
    assert _member_blocks(read.text) == PROMPTS  # all five, verbatim, in order
    for marker in MARKERS:
        assert marker in read.text


def test_transcript_filter_cli_gives_the_member_messages_verbatim(staged: tuple[Path, Path]) -> None:
    _, copy = staged
    res = subprocess.run([sys.executable, str(SCRIPTS_DIR / "transcript_filter.py"), str(copy)],
                         capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    assert res.returncode == 0, res.stderr
    assert _member_blocks(res.stdout.strip()) == PROMPTS
    stats = subprocess.run([sys.executable, str(SCRIPTS_DIR / "transcript_filter.py"), str(copy), "--stats"],
                           capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    doc = json.loads(stats.stdout)
    assert doc["turns_kept"] == {"member": 5, "glitch": 5, "total": 10}
    assert doc["dropped_total"] == 0


def test_without_the_copy_read_session_falls_back_loudly(staged: tuple[Path, Path]) -> None:
    """The negative twin: the copy gone, the digest still gets the session, and SAYS it is thin."""
    stub, copy = staged
    copy.unlink()
    read = headless_digest.read_session(stub)
    assert read.source == headless_digest.SOURCE_STUB
    assert read.source != headless_digest.SOURCE_TRANSCRIPT
    assert "no longer on disk" in read.note  # named, never silent
    assert read.text.strip()  # the bounded excerpt survives
    assert not read.text.startswith("<!--")  # the header is bookkeeping, not conversation
