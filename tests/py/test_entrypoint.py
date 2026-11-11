"""Tests for ``opencode_bridge.py``: the one process the plugin spawns per hook call.

Two kinds of test:

- Fresh interpreter, spawned EXACTLY as the plugin spawns it
  (``uv run --no-project --directory <root>/.claude/scripts python <bridge> <mode>``), with a UTF-8
  byte-order mark on stdin like the sibling suites.
  ``config.STATE_DIR`` has no environment door, so these use only calls that write nothing there:
  a non-engine-tier deny (a forced ``git clean``), an allow, a clean post scan, garbage, an
  outside-Glitch session, and a fake root with no engine at all.
  The hook log and read-grant state reach the child through the env doors conftest pins.
- In-process ``main()`` for the shapes that need a monkeypatched delegate or that would write
  engine state (session start, a flagged secret), all moved per test by conftest.

The stdout contract: nothing, or exactly one JSON document on one line; exit code always 0.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import codex_shared
import config
import pytest
import shared

import capture
import chatcache
import chatsource
import context
import opencode_bridge
import posttool
import pretool
from conftest import BRIDGE_DIR, GLITCH_ROOT, SCRIPTS_DIR
from test_capture import Fake, _new_ms

R = str(GLITCH_ROOT)
BRIDGE = BRIDGE_DIR / "opencode_bridge.py"
_BOM = b"\xef\xbb\xbf"
UV = shutil.which("uv")
FAKE_AWS = "AKIA" + "Z4XK7PQR2WMN5TYB"  # split so this file is not itself a secret-scan hit
GRANT_PHRASE = "always allow commands outside Glitch"


def _pre(command: str, session_dir: str = R, **tool_input: Any) -> dict[str, Any]:
    return {
        "tool_name": "shell",
        "tool_input": {"command": command, **tool_input},
        "session_dir": session_dir,
        "session_id": "ses_entry",
    }


def _payload(mode: str, session_dir: str) -> dict[str, Any]:
    """A well-formed payload for ``mode``; the ``pre`` one is a deny inside Glitch."""
    if mode == "pre":
        return _pre("git clean -fdx", session_dir)
    if mode == "post":
        return {"tool_name": "shell", "session_id": "ses_entry", "status": "completed",
                "result": {"output": {"output": "hello\n", "exit": 0}}, "error": None,
                "session_dir": session_dir}
    if mode == "session":
        return {"session_id": "ses_entry", "session_dir": session_dir}
    if mode == "capture":
        return {"session_id": "ses_entry", "session_dir": session_dir, "reason": "turn"}
    return {"session_id": "ses_entry", "prompt": "hello", "session_dir": session_dir}


@pytest.fixture(autouse=True)
def _no_real_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The session start now sweeps: never let it (or a capture call) reach the real opencode.

    An absolute path to nothing reads as "not installed" (unreachable); a test that wants a store
    installs the fake binary over it.
    """
    monkeypatch.setenv(chatsource.BIN_ENV, str(tmp_path / "no-opencode-here"))


def _spawn(mode: str, data: dict[str, Any] | bytes, cwd: Path = SCRIPTS_DIR,
           directory: Path = SCRIPTS_DIR) -> subprocess.CompletedProcess[bytes]:
    """The bridge exactly as the plugin spawns it (uv, the scripts folder, the absolute path)."""
    if UV is None:  # pragma: no cover - uv is how the whole suite runs
        pytest.skip("uv not on PATH")
    raw = data if isinstance(data, bytes) else _BOM + json.dumps(data).encode("utf-8")
    return subprocess.run(  # fixed argv, no shell
        [UV, "run", "--no-project", "--directory", str(directory), "python", str(BRIDGE), mode],
        input=raw, capture_output=True, env=dict(os.environ), cwd=str(cwd), timeout=120,
        check=False,
    )


def _one_json(stdout: bytes, keys: int = 1) -> dict[str, Any]:
    """stdout must be exactly one JSON object on one line, with ``keys`` keys (a session start
    answers with up to three: its context, its date and its time zone)."""
    text = stdout.decode("utf-8")
    assert text and "\n" not in text.rstrip("\n"), text
    assert len(text.splitlines()) == 1, text
    parsed = json.loads(text)
    assert isinstance(parsed, dict) and len(parsed) == keys, parsed
    return parsed


def _log_lines() -> list[str]:
    path = Path(shared.HOOK_LOG_FILE)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _main(mode: str, event: dict[str, Any] | bytes, capfd: pytest.CaptureFixture[str]) -> str:
    raw = event if isinstance(event, bytes) else _BOM + json.dumps(event).encode("utf-8")
    capfd.readouterr()
    assert opencode_bridge.main(["opencode_bridge.py", mode], io.BytesIO(raw)) == 0
    return capfd.readouterr().out


def _fake_root(folder: Path) -> Path:
    """A folder that passes the root walk but holds no engine at all."""
    (folder / ".claude" / "hooks").mkdir(parents=True)
    (folder / ".claude" / "scripts").mkdir()
    (folder / ".claude" / "hooks" / "write-guard.py").write_text("", encoding="utf-8")
    (folder / "CLAUDE.md").write_text("# root\n", encoding="utf-8")
    return Path(os.path.realpath(folder))


# --- pre, in a fresh interpreter -----------------------------------------------------------------


def test_pre_deny_with_a_bom_prints_one_deny_line() -> None:
    done = _spawn("pre", _pre("git clean -fdx"))
    assert done.returncode == 0, done.stderr
    deny = _one_json(done.stdout)["deny"]
    assert deny["kind"] == "git-clean"
    assert deny["member"].strip() and deny["model"].strip()
    assert any("opencode-pre" in line and "DENY" in line for line in _log_lines())


def test_pre_allow_prints_nothing() -> None:
    done = _spawn("pre", _pre("ls"))
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""


def test_a_brain_destroying_command_from_a_tmp_workdir_is_still_denied() -> None:
    # The session is in Glitch; the tool's workdir is /tmp; the command aims at the Brain.
    # Nothing is executed: only the guard judges the words.
    done = _spawn("pre", _pre(f"git -C {R} clean -fdx", workdir="/tmp"))
    assert done.returncode == 0, done.stderr
    assert _one_json(done.stdout)["deny"]["kind"] == "git-clean"


@pytest.mark.parametrize(
    "raw",
    [b"garbage{", b"", _BOM, b"[1, 2]", b"\"a string\"", b"\xff\xfe\x00bad", b"{}"],
    ids=["garbage", "empty", "bom-only", "array", "string", "undecodable", "empty-object"],
)
def test_garbage_stdin_is_an_error_logged_and_exit_zero(raw: bytes) -> None:
    done = _spawn("pre", raw)
    assert done.returncode == 0, done.stderr
    assert _one_json(done.stdout) == {"error": "bad-event"}
    assert any("ERROR" in line and "bad-event" in line for line in _log_lines())


def test_an_event_with_no_session_dir_is_an_error_not_silence() -> None:
    event = _pre("git clean -fdx")
    del event["session_dir"]
    done = _spawn("pre", event)
    assert done.returncode == 0
    assert _one_json(done.stdout) == {"error": "no-session-dir"}


def test_an_unknown_mode_is_an_error() -> None:
    done = _spawn("bogus", _pre("ls"))
    assert done.returncode == 0
    assert _one_json(done.stdout) == {"error": "bad-mode"}


# --- invariant sweep: outside Glitch, nothing happens, in every mode ----------------------------


@pytest.mark.parametrize("mode", opencode_bridge.MODES)
@pytest.mark.parametrize("where", ["tmp", "missing"])
def test_outside_glitch_every_mode_is_silent(mode: str, where: str, tmp_path: Path) -> None:
    outside = tmp_path if where == "tmp" else tmp_path / "never" / "made"
    assert not str(os.path.realpath(outside)).startswith(R)
    before = _log_lines()
    done = _spawn(mode, _payload(mode, str(outside)), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""
    assert _log_lines() == before  # not even a log line: the engine was never reached


# --- a delegate that will not load -----------------------------------------------------------------


@pytest.mark.parametrize("mode", opencode_bridge.MODES)
def test_a_root_with_no_engine_is_an_error_in_every_mode(mode: str, tmp_path: Path) -> None:
    fake = _fake_root(tmp_path / "glitch")
    done = _spawn(mode, _payload(mode, str(fake)), cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "error" in _one_json(done.stdout)


def test_pre_delegate_that_will_not_load_is_an_error_with_a_log(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_shared, "load_hook_module", lambda stem: None)
    out = _main("pre", _pre("git clean -fdx"), capfd)
    assert _one_json(out.encode()) == {"error": "RuntimeError"}
    assert any("opencode-pre" in line and "ERROR" in line for line in _log_lines())


def test_session_delegate_that_will_not_load_is_an_error_with_its_label(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(codex_shared, "load_hook_module", lambda stem: None)
    out = _main("session", _payload("session", R), capfd)
    assert _one_json(out.encode()) == {"error": "session-start-context unavailable"}
    assert any(
        "opencode-session" in line and "ERROR" in line and "session-start-context unavailable" in line
        for line in _log_lines()
    )


def test_prompt_delegate_missing_is_an_error_with_its_label(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(context, "prompt_hook_path", lambda: tmp_path / "gone.py")
    out = _main("prompt", _payload("prompt", R), capfd)
    assert _one_json(out.encode()) == {"error": "user-prompt-context-codex unavailable"}
    assert any("opencode-prompt" in line and "ERROR" in line for line in _log_lines())


def test_post_scanner_unavailable_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    real = codex_shared.load_hook_module

    def no_scanner(stem: str) -> Any:
        module = real(stem)
        if stem == "post-tool-secret-guard":
            monkeypatch.setattr(module, "secret_scan", None)
        return module

    monkeypatch.setattr(codex_shared, "load_hook_module", no_scanner)
    out = _main("post", _payload("post", R), capfd)
    assert _one_json(out.encode()) == {"error": "scanner-unavailable"}


def test_a_delegate_module_that_will_not_import_is_an_error_with_a_log(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # A None entry makes `import pretool` raise ModuleNotFoundError.
    monkeypatch.setitem(sys.modules, "pretool", None)
    out = _main("pre", _pre("ls"), capfd)
    assert _one_json(out.encode()) == {"error": "ModuleNotFoundError"}
    assert any("opencode-pre" in line and "ModuleNotFoundError" in line for line in _log_lines())


# --- post / session / prompt shapes ------------------------------------------------------------------


def test_post_clean_output_prints_nothing_in_a_fresh_interpreter() -> None:
    done = _spawn("post", _payload("post", R))
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""


def test_post_a_printed_secret_is_a_note(capfd: pytest.CaptureFixture[str]) -> None:
    event = _payload("post", R)
    event["result"] = {"output": {"output": f"key={FAKE_AWS}\n", "exit": 0}}
    out = _main("post", event, capfd)
    note = _one_json(out.encode())["note"]
    assert note.strip() and FAKE_AWS not in note


def test_session_context_shape(monkeypatch: pytest.MonkeyPatch,
                               capfd: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(context, "build_session", lambda event: "SESSION-CTX")
    assert _one_json(_main("session", _payload("session", R), capfd).encode(), keys=3) == {
        "context": "SESSION-CTX", "date": config.now_local().date().isoformat(),
        "timezone": "Europe/London",
    }
    monkeypatch.setattr(context, "build_session", lambda event: "")
    assert _main("session", _payload("session", R), capfd) == ""  # never {"context": ""}


def test_session_date_is_the_london_date_not_the_utc_one(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    # 23:30 UTC on 1 July is 00:30 BST on 2 July: the London day has already turned.
    instant = datetime(2026, 7, 1, 23, 30, tzinfo=UTC).astimezone(ZoneInfo("Europe/London"))
    monkeypatch.setattr(config, "now_local", lambda: instant)
    monkeypatch.setattr(context, "build_session", lambda event: "SESSION-CTX")
    out = _one_json(_main("session", _payload("session", R), capfd).encode(), keys=3)
    assert out["date"] == "2026-07-02"
    assert out["timezone"] == "Europe/London"


def test_session_with_an_unreadable_clock_still_answers_with_its_context(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    def broken() -> Any:
        raise OSError("no clock")

    monkeypatch.setattr(config, "now_local", broken)
    monkeypatch.setattr(context, "build_session", lambda event: "SESSION-CTX")
    assert _one_json(_main("session", _payload("session", R), capfd).encode()) == {
        "context": "SESSION-CTX"
    }


def test_session_timezone_is_the_iana_zone_of_glitch_clock(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The plugin's day boundary follows the member's Glitch zone, not London's by assumption."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    instant = datetime(2026, 7, 2, 3, 30, tzinfo=UTC).astimezone(ZoneInfo("America/New_York"))
    monkeypatch.setattr(config, "now_local", lambda: instant)
    monkeypatch.setattr(context, "build_session", lambda event: "SESSION-CTX")
    out = _one_json(_main("session", _payload("session", R), capfd).encode(), keys=3)
    assert out == {"context": "SESSION-CTX", "date": "2026-07-01", "timezone": "America/New_York"}


def test_session_timezone_is_left_out_when_not_an_iana_name(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """config's fallback is the machine's fixed offset (no IANA name): the key is omitted."""
    from datetime import datetime, timedelta, timezone

    instant = datetime(2026, 7, 2, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    monkeypatch.setattr(config, "now_local", lambda: instant)
    monkeypatch.setattr(context, "build_session", lambda event: "SESSION-CTX")
    out = _one_json(_main("session", _payload("session", R), capfd).encode(), keys=2)
    assert out == {"context": "SESSION-CTX", "date": "2026-07-02"}


def test_a_rebuild_that_fails_is_the_normal_error_label(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """A refresh or compaction rebuild that cannot build answers with the error label, never an
    empty context (the plugin keeps the previous text)."""
    monkeypatch.setattr(codex_shared, "load_hook_module", lambda stem: None)
    for extra in ({"refresh": True}, {"source": "compact"}):
        out = _main("session", {**_payload("session", R), **extra}, capfd)
        assert _one_json(out.encode()) == {"error": "session-start-context unavailable"}


def test_session_real_delegate_emits_the_identity(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CODEX_INVOKED_BY", raising=False)
    monkeypatch.setenv(context.NO_KEEPER_ENV, "1")
    out = _one_json(_main("session", _payload("session", R), capfd).encode(), keys=3)
    assert "## Who you are working with" in out["context"]
    assert out["date"] == config.now_local().date().isoformat()


def test_session_for_a_sub_agent_prints_nothing(capfd: pytest.CaptureFixture[str]) -> None:
    event = {**_payload("session", R), "parent_id": "ses_parent"}
    assert _main("session", event, capfd) == ""


def test_prompt_context_shape(monkeypatch: pytest.MonkeyPatch,
                              capfd: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(context, "build_prompt", lambda event: "PROMPT-CTX")
    assert _one_json(_main("prompt", _payload("prompt", R), capfd).encode()) == {
        "context": "PROMPT-CTX"
    }
    monkeypatch.setattr(context, "build_prompt", lambda event: "")
    assert _main("prompt", _payload("prompt", R), capfd) == ""


def test_prompt_real_delegate_confirms_the_grant_phrase(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CODEX_INVOKED_BY", raising=False)
    event = {**_payload("prompt", R), "prompt": GRANT_PHRASE, "session_id": "ses_grant"}
    text = _one_json(_main("prompt", event, capfd).encode())["context"]
    assert text.strip()


# --- stdout carries JSON only ---------------------------------------------------------------------


def test_stray_prints_never_reach_stdout(monkeypatch: pytest.MonkeyPatch,
                                         capfd: pytest.CaptureFixture[str]) -> None:
    def noisy(event: dict[str, Any]) -> dict[str, Any]:
        print("STRAY-PRINT")
        os.write(1, b"STRAY-FD\n")
        return {"deny": {"kind": "k", "member": "m", "model": "m"}}

    monkeypatch.setattr(pretool, "judge", noisy)
    raw = _BOM + json.dumps(_pre("ls")).encode("utf-8")
    capfd.readouterr()
    assert opencode_bridge.main(["opencode_bridge.py", "pre"], io.BytesIO(raw)) == 0
    captured = capfd.readouterr()
    assert _one_json(captured.out.encode()) == {"deny": {"kind": "k", "member": "m", "model": "m"}}
    assert "STRAY-PRINT" in captured.err and "STRAY-FD" in captured.err


def test_an_unserialisable_result_is_an_error_not_half_a_document(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(posttool, "scan_or_error", lambda event: {"note": object()})
    assert _one_json(_main("post", _payload("post", R), capfd).encode()) == {"error": "emit-failed"}


def test_a_delegate_that_calls_exit_is_an_error(monkeypatch: pytest.MonkeyPatch,
                                                capfd: pytest.CaptureFixture[str]) -> None:
    def bail(event: dict[str, Any]) -> None:
        raise SystemExit(3)

    monkeypatch.setattr(pretool, "judge", bail)
    assert _one_json(_main("pre", _pre("ls"), capfd).encode()) == {"error": "SystemExit"}


# --- capture -----------------------------------------------------------------------------------
# The bridge answer per capture outcome (opencode_bridge.CAPTURE_SILENT / CAPTURE_ERRORS):
# silence for a stage, a rightful skip or a transient miss; a capture-* error label for a chat
# that could not be kept and for an internal fault.

CAP_SID = "ses_capEntry0001"

EXPECTED_ANSWER: dict[str, str | None] = {
    capture.OUT_STAGED: None,
    capture.OUT_TOO_SHORT: None,
    capture.OUT_UNCHANGED: None,
    capture.OUT_BEFORE: None,
    capture.OUT_COLD_START: None,
    capture.OUT_NOT_FOUND: None,
    capture.OUT_UNREACHABLE: None,
    capture.OUT_BUSY: None,
    capture.OUT_DEFERRED: None,
    capture.SKIP_CHILD: None,
    capture.SKIP_AUTOMATED: None,
    capture.SKIP_OUTSIDE: None,
    capture.SKIP_OTHER_ROOT: None,
    capture.SKIP_BAD_ID: None,
    capture.OUT_REFUSED: "capture-refused",
    capture.OUT_COPY_FAILED: "capture-copy-failed",
    capture.OUT_STUB_FAILED: "capture-stub-failed",
    capture.OUT_ERROR: "capture-error",
    capture.OUT_UNREACHABLE_STREAK: "capture-unreachable",
}


def _cap(reason: str = "turn", sid: str = CAP_SID, **extra: Any) -> dict[str, Any]:
    return {"session_id": sid, "session_dir": R, "reason": reason, **extra}


def _stamp_ledger() -> None:
    """A steady-state ledger (first run already happened), written where the child will read it."""
    capture.save_ledger({"version": 1, "first_run": config.now_local().isoformat(),
                         "sessions": {}, "refused": {}})


def _stubs() -> list[Path]:
    return sorted(chatcache.state_roots().pending.glob("*.md"))


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    root = tmp_path / "fake bin"
    root.mkdir()
    return Fake(root, monkeypatch)


def test_every_capture_outcome_constant_has_a_mapping() -> None:
    """Every OUT_* / SKIP_* label capture.py defines is classified, and classified as expected."""
    defined = {v for k, v in vars(capture).items()
               if k.startswith(("OUT_", "SKIP_")) and isinstance(v, str)}
    assert defined == set(EXPECTED_ANSWER)
    for outcome, label in EXPECTED_ANSWER.items():
        want = None if label is None else {"error": label}
        assert opencode_bridge.capture_answer(outcome) == want, outcome


def test_an_unknown_capture_outcome_is_loud(monkeypatch: pytest.MonkeyPatch,
                                            capfd: pytest.CaptureFixture[str]) -> None:
    assert opencode_bridge.capture_answer("something new") == {"error": "capture-unknown-outcome"}
    monkeypatch.setattr(capture, "capture_payload", lambda event: "something new")
    assert _one_json(_main("capture", _cap(), capfd).encode()) == {
        "error": "capture-unknown-outcome"
    }


@pytest.mark.parametrize("outcome", sorted(EXPECTED_ANSWER))
def test_capture_mode_answers_each_outcome(outcome: str, monkeypatch: pytest.MonkeyPatch,
                                           capfd: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(capture, "capture_payload", lambda event: outcome)
    out = _main("capture", _cap(), capfd)
    label = EXPECTED_ANSWER[outcome]
    if label is None:
        assert out == ""
    else:
        assert _one_json(out.encode()) == {"error": label}


# Through the real bridge in a fresh interpreter, a fake opencode behind GLITCH_OPENCODE_BIN.


def test_capture_subprocess_stages_a_grown_chat_silently(fake: Fake) -> None:
    _stamp_ledger()
    fake.chat(CAP_SID, 9, _new_ms())
    done = _spawn("capture", _cap())
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""
    assert [p.name for p in _stubs()] == [capture.stub_name(CAP_SID)]
    assert any("opencode-capture" in line and "staged" in line for line in _log_lines())


def test_capture_subprocess_cold_start_is_silent_and_stamps(fake: Fake) -> None:
    fake.chat(CAP_SID, 9, _new_ms())
    done = _spawn("capture", _cap())
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""
    assert _stubs() == []
    assert capture.load_ledger()["sessions"][CAP_SID]["note"] == capture.NOTE_FIRST_CHAT


def test_d1_first_chat_after_install_through_the_bridge(
    fake: Fake, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """The measured sequence (docs/api-findings.md items 18-23): a fresh install, the live chat's
    startup `session` call (it sweeps), then its turn ends, each a `capture` call.

    The session start runs through the bridge's own ``main()`` in-process: a fresh interpreter's
    session start would write the real engine's state folder (no environment door, see the module
    docstring). Every turn end runs through the real bridge in a fresh interpreter.
    """
    monkeypatch.delenv("CODEX_INVOKED_BY", raising=False)
    monkeypatch.setenv(context.NO_KEEPER_ENV, "1")
    assert not capture.ledger_path().exists()
    start = int((config.now_local().timestamp() - 5) * 1000)  # created before the first-run stamp
    fake.chat("ses_before_install", 9, 1577836800000)
    fake.chat(CAP_SID, 1, start)
    event = {"session_id": CAP_SID, "session_dir": R, "parent_id": "", "source": "startup",
             "refresh": False}  # toBridgeSession's shape
    out = _one_json(_main("session", event, capfd).encode(), keys=3)
    assert "## Who you are working with" in out["context"]
    sessions = capture.load_ledger()["sessions"]
    assert sessions[CAP_SID]["note"] == capture.NOTE_FIRST_CHAT
    assert sessions["ses_before_install"]["note"] == capture.NOTE_BEFORE_FIRST_SWEEP
    for k in range(1, 6):
        fake.chat(CAP_SID, 2 * k, start)
        done = _spawn("capture", _cap())
        assert done.returncode == 0, done.stderr
        assert done.stdout == b""
        assert bool(_stubs()) is (2 * k >= capture.MIN_TURNS_TO_STAGE), k
    assert [p.name for p in _stubs()] == [capture.stub_name(CAP_SID)]
    rec = capture.load_ledger()["sessions"][CAP_SID]
    assert rec["note"] == capture.NOTE_STAGED and rec["turns"] == 10
    assert chatcache.copy_path(CAP_SID).is_file()
    fake.chat("ses_before_install", 30, 1577836800000)
    done = _spawn("capture", _cap(sid="ses_before_install"))
    assert done.returncode == 0 and done.stdout == b""
    assert [p.name for p in _stubs()] == [capture.stub_name(CAP_SID)]


@pytest.mark.parametrize(
    ("setup", "event"),
    [
        ("short", {}),
        ("not-held", {}),
        ("unreachable", {}),
        ("held", {"parent_id": "ses_parent"}),
    ],
    ids=["too-short", "not-found", "unreachable", "sub-agent"],
)
def test_capture_subprocess_quiet_outcomes(fake: Fake, setup: str, event: dict[str, Any]) -> None:
    _stamp_ledger()
    if setup == "short":
        fake.chat(CAP_SID, 3, _new_ms())
    elif setup == "unreachable":
        fake.chat(CAP_SID, 9, _new_ms())
        fake.mode("export", "exit")
    elif setup == "held":
        fake.chat(CAP_SID, 9, _new_ms())
    done = _spawn("capture", _cap(**event))
    assert done.returncode == 0, done.stderr
    assert done.stdout == b""
    assert _stubs() == []


def test_capture_subprocess_refused_chat_is_an_error_label(fake: Fake) -> None:
    _stamp_ledger()
    fake.raw(CAP_SID, "SECRET-CHAT-TEXT not json")
    done = _spawn("capture", _cap())
    assert done.returncode == 0, done.stderr
    assert _one_json(done.stdout) == {"error": "capture-refused"}
    assert b"SECRET-CHAT-TEXT" not in done.stdout
    assert _stubs() == []


@pytest.mark.parametrize(
    "event",
    [
        {"session_id": CAP_SID, "session_dir": R},  # no reason
        {"session_id": CAP_SID, "session_dir": R, "reason": "SECRET-REASON"},
        {"session_dir": R, "reason": "turn"},  # no session id
        {"session_id": CAP_SID, "session_dir": R, "reason": "turn", "parent_id": 7},
    ],
    ids=["no-reason", "bad-reason", "no-session-id", "parent-not-a-string"],
)
def test_capture_subprocess_bad_payload_is_a_label_only(event: dict[str, Any]) -> None:
    done = _spawn("capture", event)
    assert done.returncode == 0, done.stderr
    assert _one_json(done.stdout) == {"error": "capture-bad-payload"}
    assert b"SECRET" not in done.stdout
    assert all("SECRET-REASON" not in line for line in _log_lines())
    assert _stubs() == []


# In-process, for the failures a fresh interpreter cannot be steered into.


def test_capture_copy_failed_is_an_error_label(fake: Fake, monkeypatch: pytest.MonkeyPatch,
                                               capfd: pytest.CaptureFixture[str]) -> None:
    _stamp_ledger()
    fake.chat(CAP_SID, 9, _new_ms())

    def refuse(*_a: Any, **_k: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(chatcache, "write_copy", refuse)
    assert _one_json(_main("capture", _cap(), capfd).encode()) == {"error": "capture-copy-failed"}
    assert _stubs() == []


def test_capture_stub_failed_is_an_error_label(fake: Fake, monkeypatch: pytest.MonkeyPatch,
                                               capfd: pytest.CaptureFixture[str]) -> None:
    _stamp_ledger()
    fake.chat(CAP_SID, 9, _new_ms())

    def refuse(*_a: Any, **_k: Any) -> int:
        raise OSError("read-only")

    monkeypatch.setattr(capture, "stage", refuse)
    assert _one_json(_main("capture", _cap(), capfd).encode()) == {"error": "capture-stub-failed"}


def test_capture_internal_error_is_an_error_label(monkeypatch: pytest.MonkeyPatch,
                                                  capfd: pytest.CaptureFixture[str]) -> None:
    def boom(*_a: Any) -> str:
        raise RuntimeError("SECRET-DETAIL")

    monkeypatch.setattr(capture, "_capture_one", boom)
    out = _main("capture", _cap(), capfd)
    assert _one_json(out.encode()) == {"error": "capture-error"}
    assert "SECRET-DETAIL" not in out


def test_capture_unreachable_three_in_a_row_is_an_error_label_once(
    fake: Fake, capfd: pytest.CaptureFixture[str]
) -> None:
    """B1: one unreachable OpenCode is silence (it closes); the third in a row is loud, once."""
    _stamp_ledger()
    fake.chat(CAP_SID, 9, _new_ms())
    fake.mode("export", "exit")
    answers = [_main("capture", _cap(), capfd) for _ in range(4)]
    assert answers[0] == "" and answers[1] == ""
    assert _one_json(answers[2].encode()) == {"error": "capture-unreachable"}
    assert answers[3] == ""
    fake.mode("export", "ok")
    assert _main("capture", _cap(), capfd) == ""  # staged; the streak is over


def test_capture_busy_ledger_is_silent(fake: Fake, monkeypatch: pytest.MonkeyPatch,
                                       capfd: pytest.CaptureFixture[str]) -> None:
    _stamp_ledger()
    fake.chat(CAP_SID, 9, _new_ms())
    monkeypatch.setattr(capture, "HOOK_LOCK_TIMEOUT", 0.2)
    with shared.file_lock(capture.ledger_path(), timeout=5):
        assert _main("capture", _cap(), capfd) == ""
    assert _stubs() == []
