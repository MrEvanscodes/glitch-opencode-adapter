"""Tests for chatcache.py: the Claude-shaped OpenCode chat copy.

Every test runs under conftest's autouse isolation: ``GLITCH_OPENCODE_CAPTURE_STATE`` points at a
fresh per-test folder under the system temp folder (``_isolate_opencode_capture`` yields it), the
hook log is a per-test file, and the real-state guard fails any test that touched real state.
The secrets seeded here are clearly fake, scanner-recognisable shapes (an ``AKIA`` + ``FAKE``
pattern and a ``ghp_`` + ``FAKE`` pattern), never real credentials.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import config
import pytest
import shared
import transcript_filter

import chatcache
from conftest import GLITCH_ROOT

FAKE_AWS = "AKIAFAKEFAKEFAKEFAKE"
FAKE_GH = "ghp_FAKEfakeFAKEfakeFAKEfakeFAKEfake1234"
SECRETS = (FAKE_AWS, FAKE_GH)
STARTED = "2026-11-11T09:00:00+00:00"


def _log_text() -> str:
    path = Path(shared.HOOK_LOG_FILE)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _member_texts(path: Path) -> list[str]:
    distillate = transcript_filter.filter_transcript(path)
    return [t.text for t in distillate.turns if t.speaker == transcript_filter.SPEAKER_MEMBER]


# --- state_roots --------------------------------------------------------------------------------


def test_state_roots_env_door_set(_isolate_opencode_capture: Path) -> None:
    root = _isolate_opencode_capture
    roots = chatcache.state_roots()
    assert roots.pending == root / "pending"
    assert roots.state == root / "state"
    assert roots.data == root / "data"
    assert chatcache.chats_dir() == root / "data" / "opencode-chats"
    log = _log_text()
    assert "opencode-chatcache | state-roots | WARN" in log
    assert str(root) not in log  # label only, never the path


def test_state_roots_env_unset_reads_config_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(chatcache.CAPTURE_STATE_ENV)
    roots = chatcache.state_roots()
    assert roots == (Path(config.PENDING_DIR), Path(config.STATE_DIR), Path(config.DATA_DIR))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "moved-data")
    assert chatcache.state_roots().data == tmp_path / "moved-data"
    assert chatcache.chats_dir() == tmp_path / "moved-data" / "opencode-chats"


@pytest.mark.parametrize(
    "value",
    [
        str(Path(__file__).resolve().parent),  # a real folder outside temp
        "relative/folder",
        tempfile.gettempdir(),  # temp itself, not inside it
    ],
)
def test_state_roots_refuses_non_temp(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(chatcache.CAPTURE_STATE_ENV, value)
    with pytest.raises(ValueError, match="REFUSED"):
        chatcache.state_roots()
    with pytest.raises(ValueError, match="REFUSED"):
        chatcache.write("ses_x", [{"role": "User", "text": "hi", "ts": None}], STARTED)
    assert "opencode-chatcache | state-roots | ERROR" in _log_text()


def test_state_roots_refuses_temp_file(
    monkeypatch: pytest.MonkeyPatch, _isolate_opencode_capture: Path
) -> None:
    blocker = _isolate_opencode_capture / "a-file"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setenv(chatcache.CAPTURE_STATE_ENV, str(blocker))
    with pytest.raises(ValueError, match="REFUSED"):
        chatcache.state_roots()


# --- id safety ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "..", "../x", "a/b", "a\\b", "x\0y", "C:evil", "..\\up", "a..b", "a\nb", " a",
     "-x", "--help", "--standalone"],
)
def test_hostile_ids_refused(bad: str) -> None:
    with pytest.raises(ValueError):
        chatcache.copy_path(bad)
    with pytest.raises(ValueError):
        chatcache.write(bad, [], STARTED)


def test_non_string_id_refused() -> None:
    with pytest.raises(ValueError):
        chatcache.copy_path(None)  # type: ignore[arg-type]


def test_ids_case_kept_verbatim() -> None:
    upper = chatcache.copy_path("ses_AbC")
    lower = chatcache.copy_path("ses_abc")
    assert upper.name == "ses_AbC.jsonl"
    assert lower.name == "ses_abc.jsonl"
    assert upper.name != lower.name
    chatcache.write("ses_AbC", [{"role": "User", "text": "upper", "ts": None}], STARTED)
    if lower.exists():
        # Case-insensitive filesystem (default macOS APFS, Windows NTFS): the two names land on one
        # file. Names stay distinct; content distinctness cannot hold there, so that sub-assert is
        # skipped. OpenCode's ids carry a time-ordered prefix, so a real case-only clash is not
        # expected.
        return
    chatcache.write("ses_abc", [{"role": "User", "text": "lower", "ts": None}], STARTED)
    assert _member_texts(upper) == ["upper"]
    assert _member_texts(lower) == ["lower"]


# --- write shape --------------------------------------------------------------------------------


def test_write_shape_and_reader_round_trip() -> None:
    turns = [
        {"role": "User", "text": "Plan the week, please.", "ts": "2026-11-11T09:01:00+00:00"},
        {"role": "Assistant", "text": "Here is the plan.", "ts": datetime(2026, 11, 11, 9, 2, tzinfo=UTC)},
        {"role": "User", "text": "Thanks — ünïcode too.", "ts": None},
    ]
    path = chatcache.write("ses_Shape1", turns, STARTED)
    assert path == chatcache.copy_path("ses_Shape1")
    events = _events(path)
    assert [e["type"] for e in events] == ["user", "assistant", "user"]
    for index, event in enumerate(events):
        assert set(event) == {"type", "uuid", "sessionId", "timestamp", "message"}
        assert event["sessionId"] == "ses_Shape1"
        assert event["message"]["role"] == event["type"]
        assert event["message"]["content"] == [{"type": "text", "text": turns[index]["text"]}]
        assert transcript_filter.ClaudeAdapter.sniff(event)
    assert events[0]["timestamp"] == "2026-11-11T09:01:00+00:00"
    assert events[1]["timestamp"] == "2026-11-11T09:02:00+00:00"
    assert events[2]["timestamp"] == STARTED  # falls back to started_at
    assert len({e["uuid"] for e in events}) == 3
    # Stable: a rewrite names every turn the same way.
    again = _events(chatcache.write("ses_Shape1", turns, STARTED))
    assert [e["uuid"] for e in again] == [e["uuid"] for e in events]

    distillate = transcript_filter.filter_transcript(path)
    assert distillate.engine == transcript_filter.ENGINE_CLAUDE
    assert [t.text for t in distillate.turns] == [t["text"] for t in turns]
    assert [t.speaker for t in distillate.turns] == [
        transcript_filter.SPEAKER_MEMBER, transcript_filter.SPEAKER_GLITCH,
        transcript_filter.SPEAKER_MEMBER,
    ]
    assert sum(distillate.stats.dropped.values()) == 0


def test_turn_objects_accepted() -> None:
    class T:
        def __init__(self, role: str, text: str) -> None:
            self.role, self.text, self.ts = role, text, None

    path = chatcache.write("ses_obj", [T("User", "a"), T("Assistant", "b")], STARTED)
    assert [t.text for t in transcript_filter.filter_transcript(path).turns] == ["a", "b"]


def test_bad_role_refused_nothing_written() -> None:
    with pytest.raises(ValueError):
        chatcache.write("ses_bad", [{"role": "tool", "text": "x", "ts": None}], STARTED)
    assert not chatcache.copy_path("ses_bad").exists()


def test_code_fences_byte_identical() -> None:
    text = "Look:\n```python\nprint('<b>')\n```\n\n  indented `tick` line\n"
    result = chatcache.write_copy("ses_fence", [{"role": "User", "text": text, "ts": None}], STARTED)
    assert result.escaped == 0 and result.reader_altered == 0
    assert _member_texts(result.path) == [text]
    assert chatcache.ESCAPES_KEY not in _events(result.path)[0]


def test_lone_system_reminder_mention_kept_verbatim() -> None:
    text = "What does the `<system-reminder>` tag do in Claude Code?"
    result = chatcache.write_copy("ses_lone", [{"role": "User", "text": text, "ts": None}], STARTED)
    assert result.escaped == 0 and result.reader_altered == 0
    assert _member_texts(result.path) == [text]


@pytest.mark.parametrize(
    "text",
    [
        "Explain <system-reminder>this span</system-reminder> to me",
        "<system-reminder>only a span</system-reminder>",
        "<command-message>pasted from a transcript</command-message>",
        "   <bash-stdout>ls output</bash-stdout>",
        "Caveat: The messages below were generated by the user while running local commands",
        "[Request interrupted by user] was what it said",
        "a note about <command-name>/foo</command-name> mid-text",
        "<hook-thing> and <system-reminder>a</system-reminder><system-reminder>b</system-reminder>",
    ],
)
def test_reader_triggers_neutralised_counted_and_reversible(text: str) -> None:
    result = chatcache.write_copy("ses_trig", [{"role": "User", "text": text, "ts": None}], STARTED)
    assert result.escaped == 1
    assert result.reader_altered == 0  # the reader now keeps the turn whole
    event = _events(result.path)[0]
    written = event["message"]["content"][0]["text"]
    assert written != text
    assert _member_texts(result.path) == [written]  # kept, nothing stripped
    assert chatcache.restore_text(written, event[chatcache.ESCAPES_KEY]) == text
    log = _log_text()
    assert "turns=1 escaped=1 reader_altered=0" in log
    assert text not in log


def test_without_neutralisation_the_reader_would_strip() -> None:
    """The counter-proof: the same text written raw IS altered by the reader."""
    text = "Explain <system-reminder>this span</system-reminder> to me"
    path = chatcache.chats_dir() / "raw.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "user", "uuid": "u", "sessionId": "raw", "timestamp": STARTED,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    distillate = transcript_filter.filter_transcript(path)
    assert [t.text for t in distillate.turns] == ["Explain  to me"]
    assert distillate.stats.reminders_stripped == 1


def test_assistant_text_never_escaped() -> None:
    text = "<command-message>assistant quoting a tag</command-message>"
    result = chatcache.write_copy(
        "ses_asst", [{"role": "Assistant", "text": text, "ts": None}], STARTED
    )
    assert result.escaped == 0 and result.reader_altered == 0
    assert [t.text for t in transcript_filter.filter_transcript(result.path).turns] == [text]


def test_whitespace_turn_counted_as_reader_altered() -> None:
    result = chatcache.write_copy(
        "ses_ws",
        [{"role": "User", "text": "   ", "ts": None}, {"role": "Assistant", "text": "ok", "ts": None}],
        STARTED,
    )
    assert result.reader_altered == 1
    assert "reader_altered=1" in _log_text()
    assert "opencode-chatcache | write | WARN" in _log_text()


def test_restore_text_rejects_wrong_offsets() -> None:
    with pytest.raises(ValueError):
        chatcache.restore_text("abc", [1])


# --- redaction ----------------------------------------------------------------------------------


def _seeded_turns() -> list[dict]:
    return [
        {"role": "User", "text": f"my key is {FAKE_AWS} ok", "ts": None},
        {"role": "Assistant", "text": f"Do not paste {FAKE_GH} here.", "ts": None},
        {
            "role": "User",
            "text": f"<local-command-stdout>AWS_KEY={FAKE_AWS}\nGH={FAKE_GH}</local-command-stdout>",
            "ts": None,
        },
        {"role": "User", "text": f"```\n$ env\nAWS={FAKE_AWS}\n```", "ts": None},
    ]


def test_redaction_member_assistant_and_tool_output() -> None:
    path = chatcache.write("ses_secret", _seeded_turns(), STARTED)
    raw = path.read_text(encoding="utf-8")
    for secret in SECRETS:
        assert secret not in raw
    assert "[REDACTED:aws-access-key]" in raw
    assert "[REDACTED:github-token]" in raw
    texts = [t.text for t in transcript_filter.filter_transcript(path).turns]
    assert len(texts) == 4
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


def test_invariant_sweep_no_secret_anywhere(_isolate_opencode_capture: Path) -> None:
    chatcache.write("ses_sweep1", _seeded_turns(), STARTED)
    chatcache.write("ses_sweep2", list(reversed(_seeded_turns())), STARTED)
    chatcache.write("ses_sweep1", _seeded_turns()[:2], STARTED)  # a rewrite too
    chatcache.prune({"ses_sweep1"}, source_reachable=True)
    files = [p for p in _isolate_opencode_capture.rglob("*") if p.is_file()]
    files.append(Path(shared.HOOK_LOG_FILE))
    assert files
    for path in files:
        data = path.read_bytes()
        for secret in SECRETS:
            assert secret.encode() not in data, f"secret found in {path.name}"


def test_refused_when_secret_scan_cannot_import(monkeypatch: pytest.MonkeyPatch) -> None:
    previous = chatcache.write("ses_ref", [{"role": "User", "text": "before", "ts": None}], STARTED)
    before = previous.read_bytes()
    monkeypatch.setitem(sys.modules, "secret_scan", None)  # import now raises ImportError
    with pytest.raises(chatcache.CacheRefused):
        chatcache.write("ses_ref", [{"role": "User", "text": "after", "ts": None}], STARTED)
    with pytest.raises(chatcache.CacheRefused):
        chatcache.write("ses_new", [{"role": "User", "text": "x", "ts": None}], STARTED)
    assert previous.read_bytes() == before
    assert not chatcache.copy_path("ses_new").exists()
    assert sorted(p.name for p in chatcache.chats_dir().iterdir()) == ["ses_ref.jsonl"]
    assert "opencode-chatcache | write | REFUSED" in _log_text()


def test_refused_when_redaction_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import secret_scan

    previous = chatcache.write("ses_boom", [{"role": "User", "text": "before", "ts": None}], STARTED)
    before = previous.read_bytes()

    def boom(text: str, *, source: str = "text") -> tuple[str, list]:
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(secret_scan, "redact_text", boom)
    with pytest.raises(chatcache.CacheRefused):
        chatcache.write("ses_boom", [{"role": "User", "text": "after", "ts": None}], STARTED)
    assert previous.read_bytes() == before
    assert [p.name for p in chatcache.chats_dir().iterdir()] == ["ses_boom.jsonl"]  # no temp left


# --- file mode and atomic rewrite ---------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_file_and_folder_mode() -> None:
    path = chatcache.write("ses_mode", [{"role": "User", "text": "x", "ts": None}], STARTED)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    os.chmod(path, 0o644)
    chatcache.write("ses_mode", [{"role": "User", "text": "y", "ts": None}], STARTED)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_rewrite_replaces_whole_copy() -> None:
    first = [{"role": "User", "text": f"turn {i}", "ts": None} for i in range(5)]
    path = chatcache.write("ses_rw", first, STARTED)
    assert len(_events(path)) == 5
    chatcache.write("ses_rw", first[:2], STARTED)
    assert _member_texts(path) == ["turn 0", "turn 1"]
    assert [p.name for p in path.parent.iterdir()] == ["ses_rw.jsonl"]


def test_spaces_and_non_ascii_folder_and_id(
    monkeypatch: pytest.MonkeyPatch, _isolate_opencode_capture: Path
) -> None:
    odd = _isolate_opencode_capture / "dossier with spaces é ü 漢"
    odd.mkdir()
    monkeypatch.setenv(chatcache.CAPTURE_STATE_ENV, str(odd))
    session_id = "ses_é ü 漢"
    path = chatcache.write(session_id, [{"role": "User", "text": "héllo", "ts": None}], STARTED)
    assert path == odd / "data" / "opencode-chats" / f"{session_id}.jsonl"
    assert path.exists()
    assert _member_texts(path) == ["héllo"]


# --- prune --------------------------------------------------------------------------------------


def test_prune_only_when_reachable() -> None:
    for sid in ("ses_a", "ses_b", "ses_c"):
        chatcache.write(sid, [{"role": "User", "text": sid, "ts": None}], STARTED)
    folder = chatcache.chats_dir()
    (folder / "notes.txt").write_text("keep", encoding="utf-8")
    (folder / "ses_dir.jsonl").mkdir()

    assert chatcache.prune(set(), source_reachable=False) == []
    assert len(list(folder.glob("*.jsonl"))) == 4

    assert chatcache.prune({"ses_a"}, source_reachable=True) == ["ses_b", "ses_c"]
    assert sorted(p.name for p in folder.iterdir()) == ["notes.txt", "ses_a.jsonl", "ses_dir.jsonl"]


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_prune_leaves_symlinks(tmp_path: Path) -> None:
    chatcache.write("ses_a", [{"role": "User", "text": "a", "ts": None}], STARTED)
    outside = tmp_path / "outside.jsonl"
    outside.write_text("precious", encoding="utf-8")
    (chatcache.chats_dir() / "ses_link.jsonl").symlink_to(outside)
    assert chatcache.prune({"ses_a"}, source_reachable=True) == []
    assert outside.read_text(encoding="utf-8") == "precious"


def test_prune_absent_folder() -> None:
    assert chatcache.prune([], source_reachable=True) == []


# --- the Brain keeps the copies out of git ------------------------------------------------------


def test_copies_ignored_by_brain_gitignore() -> None:
    result = subprocess.run(
        ["git", "-C", str(GLITCH_ROOT), "check-ignore", "-q",
         ".claude/data/opencode-chats/x.jsonl"],
        capture_output=True, check=False,
    )
    assert result.returncode == 0
