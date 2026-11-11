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

import pytest

import codex_shared
import context
import opencode_bridge
import posttool
import pretool
import shared
from conftest import BRIDGE_DIR, GLITCH_ROOT, SCRIPTS_DIR

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
    return {"session_id": "ses_entry", "prompt": "hello", "session_dir": session_dir}


def _spawn(mode: str, data: dict[str, Any] | bytes, cwd: Path = SCRIPTS_DIR,
           directory: Path = SCRIPTS_DIR) -> subprocess.CompletedProcess[bytes]:
    """The bridge exactly as the plugin spawns it (uv, the scripts folder, the absolute path)."""
    if UV is None:  # pragma: no cover - uv is how the whole suite runs
        pytest.skip("uv not on PATH")
    raw = data if isinstance(data, bytes) else _BOM + json.dumps(data).encode("utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        [UV, "run", "--no-project", "--directory", str(directory), "python", str(BRIDGE), mode],
        input=raw, capture_output=True, env=dict(os.environ), cwd=str(cwd), timeout=120,
        check=False,
    )


def _one_json(stdout: bytes) -> dict[str, Any]:
    """stdout must be exactly one JSON object on one line."""
    text = stdout.decode("utf-8")
    assert text and "\n" not in text.rstrip("\n"), text
    assert len(text.splitlines()) == 1, text
    parsed = json.loads(text)
    assert isinstance(parsed, dict) and len(parsed) == 1, parsed
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
    assert _one_json(_main("session", _payload("session", R), capfd).encode()) == {
        "context": "SESSION-CTX"
    }
    monkeypatch.setattr(context, "build_session", lambda event: "")
    assert _main("session", _payload("session", R), capfd) == ""  # never {"context": ""}


def test_session_real_delegate_emits_the_identity(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("CODEX_INVOKED_BY", raising=False)
    monkeypatch.setenv(context.NO_KEEPER_ENV, "1")
    text = _one_json(_main("session", _payload("session", R), capfd).encode())["context"]
    assert "## Who you are working with" in text


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
