"""Tests for ``chatsource.py``: reading OpenCode chats through ``opencode session export``.

The real ``opencode`` is never called.
A fake executable (a small Python script made in ``tmp_path``) stands in through the
``GLITCH_OPENCODE_BIN`` door: it prints a fixture by subcommand and records its argv, working
folder, ``PWD``, stdin and which environment keys it saw.
Fixtures are synthetic text in exactly the measured shape of
`the author's private test evidence` (keys per message type, part types, the list row keys).
conftest points every XDG / OpenCode home at temp, so even a stray real binary would see nothing.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import chatsource

SID = "ses_e34e04da3ffeMsm84Y9oT218K8"
CHILD_SID = "ses_e34d7c1bbffeJaPOj15SJIKZ2b"
SECRET_TEXT = "SYNTHETIC-CHAT-BODY-7781"

FAKE = r'''#!{python}
import json, os, sys, time, subprocess
cfg = json.loads(open(os.environ["FAKE_OC_CONFIG"], encoding="utf-8").read())
rec = {{
    "argv": sys.argv[1:],
    "cwd": os.getcwd(),
    "pwd": os.environ.get("PWD"),
    "stdin": sys.stdin.read() if sys.stdin is not None else None,
    "env_keys": sorted(k for k in ("CODEX_INVOKED_BY", "CLAUDE_INVOKED_BY",
                                   "OPENCODE_DISABLE_AUTOUPDATE") if k in os.environ),
    "codex_invoked_by": os.environ.get("CODEX_INVOKED_BY"),
    "xdg_data": os.environ.get("XDG_DATA_HOME"),
}}
with open(cfg["record"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
mode = cfg.get("mode", "ok")
if mode == "sleep":
    time.sleep(60)
if mode == "grandchild":
    subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(60)
if mode == "exit":
    sys.stderr.write("boom\n")
    sys.exit(3)
if mode == "notfound":
    sys.stderr.write(cfg.get("stderr", "Session not found: " + sys.argv[-1] + "\n"))
    sys.exit(cfg.get("code", 1))
sub = sys.argv[2] if len(sys.argv) > 2 else ""
out = cfg.get(sub)
if out is not None:
    sys.stdout.buffer.write(out.encode("utf-8"))
'''


# --- fixture builders (shape of the author's private test evidence) ----------------------------


def _user(text: str, *, mid: str = "msg_u", ts: int = 1794409031547) -> dict[str, Any]:
    return {"type": "user", "id": mid, "time": {"created": ts}, "text": text, "files": []}


def _assistant(content: list[dict[str, Any]], *, mid: str = "msg_a") -> dict[str, Any]:
    return {
        "type": "assistant",
        "id": mid,
        "time": {"created": 1794409032000, "completed": 1794409033000},
        "agent": "build",
        "model": {"id": "deepseek-v4.1-flash-free", "providerID": "opencode", "variant": None},
        "content": content,
        "finish": "stop",
        "rawFinish": "stop",
        "cost": 0,
        "tokens": {"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool() -> dict[str, Any]:
    return {
        "type": "tool",
        "id": "toolu_x",
        "name": "shell",
        "state": {
            "status": "completed",
            "input": {"command": "echo TOOL-INPUT-SENTINEL"},
            "content": [{"type": "text", "text": "TOOL-OUTPUT-SENTINEL"}],
            "metadata": {},
        },
        "executed": True,
        "time": {"start": 1794409031547, "end": 1794409031551},
    }


def _idle() -> dict[str, Any]:
    return {"type": "idle", "id": "msg_i", "time": {"created": 1794409034000}, "outcome": "succeeded"}


def _compaction() -> dict[str, Any]:
    return {
        "type": "compaction", "id": "msg_c", "time": {"created": 1794409031000}, "reason": "auto",
        "status": "completed", "summary": "COMPACTION-SUMMARY-SENTINEL", "model": {}, "cost": 0,
        "tokens": {},
    }


def _info(sid: str = SID, *, parent: str | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {
        "id": sid, "projectID": "f834", "cost": 0, "outcome": "succeeded", "title": "t",
        "model": {"id": "m", "providerID": "opencode", "variant": None},
        "tokens": {"input": 0, "output": 0, "reasoning": 0, "cache": {}},
        "time": {"created": 1794409031000, "updated": 1794409034000},
        "location": {"directory": "/private/var/work"},
    }
    if parent:
        info["parentID"] = parent
        info["agent"] = "general"
    return info


def _export(messages: list[dict[str, Any]], sid: str = SID, **kw: Any) -> dict[str, Any]:
    return {"info": _info(sid, **kw), "messages": messages}


# --- the fake binary ----------------------------------------------------------------------------


class Fake:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.record = root / "record.jsonl"
        self.config = root / "config.json"
        self.bin = root / "opencode"
        self.bin.write_text(FAKE.format(python=sys.executable), encoding="utf-8")
        self.bin.chmod(self.bin.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv(chatsource.BIN_ENV, str(self.bin))
        monkeypatch.setenv("FAKE_OC_CONFIG", str(self.config))
        self.set()

    def set(self, *, export: Any = None, listing: Any = None, raw_export: str | None = None,
            raw_list: str | None = None, mode: str = "ok", stderr: str | None = None,
            code: int = 1) -> None:
        cfg: dict[str, Any] = {"record": str(self.record), "mode": mode, "code": code}
        if stderr is not None:
            cfg["stderr"] = stderr
        if raw_export is not None:
            cfg["export"] = raw_export
        elif export is not None:
            cfg["export"] = json.dumps(export, ensure_ascii=False)
        if raw_list is not None:
            cfg["list"] = raw_list
        elif listing is not None:
            cfg["list"] = json.dumps(listing, ensure_ascii=False)
        self.config.write_text(json.dumps(cfg), encoding="utf-8")

    def calls(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    root = tmp_path / "fake bin"
    root.mkdir()
    return Fake(root, monkeypatch)


# --- argv, env, never a shell ---------------------------------------------------------------------


def test_export_argv_exact_and_recursion_env_set(fake: Fake) -> None:
    fake.set(export=_export([_user("hi"), _assistant([_text("hello")])]))
    chatsource.read_turns(SID)
    (call,) = fake.calls()
    assert call["argv"] == ["session", "export", "--standalone", SID]
    assert "CODEX_INVOKED_BY" in call["env_keys"]
    assert "CLAUDE_INVOKED_BY" in call["env_keys"]
    assert call["codex_invoked_by"] == chatsource.INVOKED_BY_VALUE
    assert call["stdin"] == ""  # stdin is DEVNULL
    # XDG left as inherited (conftest points it at temp).
    assert call["xdg_data"] == os.environ["XDG_DATA_HOME"]


def test_recursion_name_is_the_one_the_engine_reads() -> None:
    import codex_shared

    env = chatsource.child_env()
    assert env[codex_shared.CODEX_RECURSION_ENV] == chatsource.INVOKED_BY_VALUE
    assert codex_shared.CODEX_RECURSION_ENV not in os.environ  # the parent is not marked


def test_home_override_reroots_xdg(fake: Fake, tmp_path: Path) -> None:
    fake.set(export=_export([_user("hi")]))
    chatsource.read_turns(SID, home=tmp_path / "alt home")
    (call,) = fake.calls()
    assert call["xdg_data"] == str(tmp_path / "alt home" / ".local" / "share")


def test_metacharacters_in_id_are_one_argument_not_a_shell(fake: Fake, tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    sid = f"x; touch {marker} $(touch {marker})"
    fake.set(export=_export([_user("hi")], sid=sid))
    chatsource.read_turns(sid)
    (call,) = fake.calls()
    assert call["argv"][-1] == sid
    assert not marker.exists()


def test_source_never_uses_a_shell() -> None:
    source = Path(chatsource.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert "os.system" not in source


# --- keeping and dropping -------------------------------------------------------------------------


def test_text_kept_byte_identical() -> None:
    member = "naïve café \U0001f600 line1\r\nline2\t<system-reminder>not real</system-reminder>"
    reply = "```python\nprint('x')\n```\n\u200bzero-width and trailing space "
    turns, stats = chatsource.parse_export(SID, _export([_user(member), _assistant([_text(reply)])]))
    assert [(t.role, t.text) for t in turns] == [("User", member), ("Assistant", reply)]
    assert stats.kept_user == 1 and stats.kept_assistant == 1


def test_text_kept_byte_identical_through_the_child(fake: Fake) -> None:
    member = "naïve \U0001f600 <system-reminder>x</system-reminder>\n```\ncode\n```"
    fake.set(export=_export([_user(member), _assistant([_text("ok ✓")])]))
    turns = chatsource.read_turns(SID)
    assert turns[0].text == member
    assert turns[1].text == "ok ✓"


def test_tool_reasoning_file_compaction_idle_dropped() -> None:
    doc = _export([
        _compaction(),
        _user("question"),
        _assistant([{"type": "reasoning", "text": "REASONING-SENTINEL"}, _tool()], mid="a1"),
        _assistant([_text("answer"), {"type": "file", "url": "FILE-SENTINEL"}], mid="a2"),
        _idle(),
    ])
    doc["messages"][1]["files"] = [{"path": "FILE-DUMP-SENTINEL"}]
    turns, stats = chatsource.parse_export(SID, doc)
    assert [(t.role, t.text) for t in turns] == [("User", "question"), ("Assistant", "answer")]
    flat = json.dumps([t.text for t in turns])
    for sentinel in ("REASONING", "TOOL-INPUT", "TOOL-OUTPUT", "FILE", "COMPACTION"):
        assert sentinel not in flat
    assert stats.dropped_parts == {"reasoning": 1, "tool": 1, "file": 2}
    assert stats.dropped_messages == {"compaction": 1, "idle": 1}
    assert stats.dropped_empty == 1  # the tool-only assistant message
    assert stats.unknown_parts == 0 and stats.unknown_messages == 0


def test_unknown_part_and_message_types_dropped_and_counted() -> None:
    doc = _export([
        _user("q"),
        _assistant([_text("a"), {"type": "hologram", "text": "UNKNOWN-PART-SENTINEL"}]),
        {"type": "telepathy", "id": "m", "text": "UNKNOWN-MSG-SENTINEL"},
    ])
    turns, stats = chatsource.parse_export(SID, doc)
    assert [t.text for t in turns] == ["q", "a"]
    assert stats.unknown_parts == 1
    assert stats.unknown_messages == 1
    assert "SENTINEL" not in stats.summary()


def test_synthetic_text_part_dropped() -> None:
    doc = _export([_user("q"), _assistant([{"type": "text", "text": "PUSHED", "synthetic": True},
                                             _text("real")])])
    turns, stats = chatsource.parse_export(SID, doc)
    assert turns[-1].text == "real"
    assert stats.dropped_parts == {"synthetic": 1}


def test_text_parts_of_one_message_joined_in_order() -> None:
    turns, _ = chatsource.parse_export(SID, _export([_assistant([_text("one"), _tool(),
                                                                  _text("two")])]))
    assert [t.text for t in turns] == ["one\n\ntwo"]


def test_two_assistant_messages_with_text_stay_two_records() -> None:
    doc = _export([_user("q"), _assistant([_text("first")], mid="a1"),
                   _assistant([_text("second")], mid="a2")])
    turns, _ = chatsource.parse_export(SID, doc)
    assert [(t.role, t.text) for t in turns] == [
        ("User", "q"), ("Assistant", "first"), ("Assistant", "second")]


def test_empty_messages_dropped() -> None:
    doc = _export([_user(""), _user("   \n"), _assistant([]), _assistant([_text("")]),
                   _user("real")])
    turns, stats = chatsource.parse_export(SID, doc)
    assert [t.text for t in turns] == ["real"]
    assert stats.dropped_empty == 4


def test_timestamps_epoch_ms_to_iso_utc() -> None:
    turns, _ = chatsource.parse_export(SID, _export([_user("q", ts=1794409031547)]))
    assert turns[0].ts == "2026-11-11T14:57:11.547000Z"
    turns, _ = chatsource.parse_export(SID, {"info": _info(), "messages": [
        {"type": "user", "id": "m", "text": "q", "files": []}]})
    assert turns[0].ts is None


def test_five_turn_chat_with_tool_turn() -> None:
    msgs: list[dict[str, Any]] = []
    for i in range(5):
        msgs.append(_user(f"q{i}", mid=f"u{i}"))
        if i == 2:
            msgs.append(_assistant([_tool()], mid=f"t{i}"))
        msgs.append(_assistant([_text(f"a{i}")], mid=f"a{i}"))
        msgs.append(_idle())
    turns, _ = chatsource.parse_export(SID, _export(msgs))
    assert [t.role for t in turns] == ["User", "Assistant"] * 5


def test_child_session_export_reads(fake: Fake) -> None:
    fake.set(export=_export([_user("sub task"), _assistant([_text("done")])], sid=CHILD_SID,
                            parent=SID))
    turns = chatsource.read_turns(CHILD_SID)
    assert [t.text for t in turns] == ["sub task", "done"]


# --- unreadable shapes ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"not json {SECRET_TEXT}",
        json.dumps([SECRET_TEXT]),
        json.dumps({"messages": [_user(SECRET_TEXT)]}),
        json.dumps({"info": _info(), "msgs": [_user(SECRET_TEXT)]}),
        json.dumps({"info": _info(), "messages": {"0": _user(SECRET_TEXT)}}),
        json.dumps({"info": _info(), "messages": [SECRET_TEXT]}),
        json.dumps({"info": _info(), "messages": [{"id": "m", "text": SECRET_TEXT}]}),
        json.dumps({"info": _info(), "messages": [{"type": "user", "body": SECRET_TEXT}]}),
        json.dumps({"info": _info(), "messages": [{"type": "user", "text": [SECRET_TEXT]}]}),
        json.dumps({"info": _info(), "messages": [
            {"type": "assistant", "parts": [_text(SECRET_TEXT)]}]}),
        json.dumps({"info": _info(), "messages": [_assistant([SECRET_TEXT])]}),
        json.dumps({"info": _info(), "messages": [_assistant([{"text": SECRET_TEXT}])]}),
        json.dumps({"info": _info(), "messages": [
            _assistant([{"type": "text", "content": SECRET_TEXT}])]}),
        json.dumps({"info": _info("ses_other"), "messages": [_user(SECRET_TEXT)]}),
    ],
)
def test_unreadable_shape_raises_naming_id_without_text(fake: Fake, raw: str) -> None:
    fake.set(raw_export=raw)
    with pytest.raises(chatsource.ChatUnreadableError) as info:
        chatsource.read_turns(SID)
    message = str(info.value)
    assert SID in message
    assert SECRET_TEXT not in message
    assert "Nothing from it was added" in message
    assert ADDON_ROAD in message
    assert "/raise-issue" not in message and "Glitch team" not in message


ADDON_ROAD = (
    "This is part of the OpenCode add-on, not Glitch itself. Re-install the add-on from its "
    "folder; if it keeps happening, tell the add-on's author."
)


def test_unreadable_list_message_names_the_addon_road() -> None:
    message = str(chatsource.SessionListUnreadableError("the list is not JSON"))
    assert ADDON_ROAD in message
    assert "/raise-issue" not in message and "/update" not in message


@pytest.mark.parametrize("sid", ["-x", "--help", "--standalone"])
def test_an_id_that_reads_as_an_option_never_reaches_opencode(fake: Fake, sid: str) -> None:
    fake.set(export=_export([_user("hi")], sid=sid))
    with pytest.raises(ValueError):
        chatsource.read_turns(sid)
    with pytest.raises(ValueError):
        chatsource.export_argv("/bin/opencode", sid)
    assert fake.calls() == []


def test_invalid_utf8_is_unreadable(fake: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    fake.set(export=_export([_user("hi")]))
    monkeypatch.setattr(chatsource, "_decode_json", lambda raw: (b"\xff").decode("utf-8"))
    with pytest.raises(chatsource.ChatUnreadableError):
        chatsource.read_turns(SID)


# --- unreachable ----------------------------------------------------------------------------------


def test_binary_missing_is_unreachable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(chatsource.BIN_ENV, str(tmp_path / "no-such-opencode"))
    assert chatsource.find_binary() is None
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.read_turns(SID)
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.list_sessions(cwd=tmp_path)


def test_relative_door_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(chatsource.BIN_ENV, "opencode")
    assert chatsource.find_binary() is None


def test_find_binary_falls_back_to_installer_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(chatsource.BIN_ENV, raising=False)
    monkeypatch.setattr(chatsource.shutil, "which", lambda name: None)
    monkeypatch.setattr(chatsource.Path, "home", classmethod(lambda cls: tmp_path))
    assert chatsource.find_binary() is None
    target = tmp_path / ".opencode" / "bin" / "opencode"
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    assert chatsource.find_binary() == str(target)


def test_nonzero_exit_is_unreachable(fake: Fake) -> None:
    fake.set(mode="exit")
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.read_turns(SID)


def test_unknown_id_is_session_not_found(fake: Fake) -> None:
    # Measured on opencode 2.2.7: exit 1, stderr "Session not found: <id>\n".
    fake.set(mode="notfound")
    with pytest.raises(chatsource.SessionNotFound):
        chatsource.read_turns(SID)
    with pytest.raises(chatsource.SessionNotFound):
        chatsource.read_turns_with_stats(SID)
    # Still a SourceUnreachable, so a caller that knows only the parent never reads "no chats".
    assert issubclass(chatsource.SessionNotFound, chatsource.SourceUnreachable)


@pytest.mark.parametrize(
    "stderr",
    [
        "boom\n",
        "Error: Session not found: x\n",  # not the FIRST thing on the first line
        "something else\nSession not found: x\n",  # not the first line
        "",
    ],
)
def test_other_failures_are_not_session_not_found(fake: Fake, stderr: str) -> None:
    fake.set(mode="notfound", stderr=stderr)
    with pytest.raises(chatsource.SourceUnreachable) as info:
        chatsource.read_turns(SID)
    assert not isinstance(info.value, chatsource.SessionNotFound)


def test_list_failure_is_never_session_not_found(fake: Fake, tmp_path: Path) -> None:
    fake.set(mode="notfound")
    with pytest.raises(chatsource.SourceUnreachable) as info:
        chatsource.list_sessions(cwd=tmp_path)
    assert not isinstance(info.value, chatsource.SessionNotFound)


def test_not_found_stderr_never_logged(fake: Fake) -> None:
    import shared

    fake.set(mode="notfound", stderr=f"Session not found: {SECRET_TEXT}\n")
    with pytest.raises(chatsource.SessionNotFound) as info:
        chatsource.read_turns(SID)
    assert SECRET_TEXT not in str(info.value)
    log = Path(shared.HOOK_LOG_FILE)
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    assert "export | not-found" in text
    assert SECRET_TEXT not in text


@pytest.mark.parametrize("mode", ["sleep", "grandchild"])
def test_stalled_source_times_out(fake: Fake, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr(chatsource, "TIMEOUT_SECONDS", 1.0)
    fake.set(mode=mode)
    started = time.monotonic()
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.read_turns(SID)
    assert time.monotonic() - started < 6.0
    started = time.monotonic()
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.list_sessions(cwd=fake.root)
    assert time.monotonic() - started < 6.0


@pytest.mark.parametrize("mode", ["sleep", "grandchild"])
def test_a_timeout_argument_bounds_one_call_and_leaves_the_constant(
    fake: Fake, mode: str
) -> None:
    fake.set(mode=mode)
    assert chatsource.TIMEOUT_SECONDS == 20.0
    started = time.monotonic()
    with pytest.raises(chatsource.SourceUnreachable, match="within 1 seconds"):
        chatsource.read_turns_with_stats(SID, timeout=1.0)
    assert time.monotonic() - started < 6.0
    started = time.monotonic()
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.list_sessions(cwd=fake.root, timeout=1.0)
    assert time.monotonic() - started < 6.0
    assert chatsource.TIMEOUT_SECONDS == 20.0


# --- the list -------------------------------------------------------------------------------------


def _row(sid: str, directory: str | None = "/private/var/work") -> dict[str, Any]:
    row: dict[str, Any] = {"id": sid, "title": "t", "updated": 1794409040000,
                           "created": 1794409031000, "projectId": "f834"}
    if directory is not None:
        row["directory"] = directory
    return row


def test_list_sessions_parses_and_passes_cwd_and_pwd(fake: Fake, tmp_path: Path) -> None:
    work = tmp_path / "my project ü 日本"
    work.mkdir()
    fake.set(listing=[_row(SID, str(work)), _row("ses_two", None)])
    rows = chatsource.list_sessions(cwd=work)
    (call,) = fake.calls()
    assert call["argv"] == ["session", "list", "--standalone", "--format", "json"]
    assert os.path.realpath(call["cwd"]) == os.path.realpath(work)
    assert call["pwd"] == str(work)
    assert "CODEX_INVOKED_BY" in call["env_keys"]
    assert rows == [
        chatsource.SessionInfo(SID, "2026-11-11T14:57:11Z", str(work), ""),
        chatsource.SessionInfo("ses_two", "2026-11-11T14:57:11Z", str(work), ""),
    ]


def test_empty_store_is_an_empty_list_not_an_error(fake: Fake, tmp_path: Path) -> None:
    fake.set(raw_list="[]")
    assert chatsource.list_sessions(cwd=tmp_path) == []


def test_unreachable_store_raises_never_empty(fake: Fake, tmp_path: Path) -> None:
    fake.set(mode="exit")
    with pytest.raises(chatsource.SourceUnreachable):
        chatsource.list_sessions(cwd=tmp_path)


@pytest.mark.parametrize(
    "raw", ["{not json", json.dumps({"rows": []}), json.dumps([1]), json.dumps([{"title": "x"}])]
)
def test_unreadable_list_raises(fake: Fake, tmp_path: Path, raw: str) -> None:
    fake.set(raw_list=raw)
    with pytest.raises(chatsource.SessionListUnreadableError):
        chatsource.list_sessions(cwd=tmp_path)
