"""Tests for ``posttool.py``: the OpenCode ``execute.after`` secret scan.

Every result shape here is a REAL ``execute.after`` event captured in CP0
(``fixtures/execute-after.jsonl``, copied verbatim from ``spike/evidence/``), cut to the bridge
payload the plugin sends: ``{tool_name, session_id, result, error, status}``.
The delegate is the REAL ``post-tool-secret-guard`` unless a test says otherwise.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import posttool

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "execute-after.jsonl"

# The fake token the engine's secret-scan suites use, split so this file is not itself a hit.
FAKE_AWS = "AKIA" + "Z4XK7PQR2WMN5TYB"


def _load_events() -> list[dict[str, Any]]:
    rows = []
    for line in FIXTURES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line)["event"])
    return rows


EVENTS = _load_events()


def _bridge(event: dict[str, Any]) -> dict[str, Any]:
    """The payload the plugin sends: the measured event cut to the bridge's keys."""
    return {
        "tool_name": event["tool"],
        "session_id": event["sessionID"],
        "result": copy.deepcopy(event.get("result")),
        "error": copy.deepcopy(event.get("error")),
        "status": event["status"],
    }


def _first(tool: str, status: str = "completed") -> dict[str, Any]:
    for event in EVENTS:
        if event["tool"] == tool and event["status"] == status:
            return _bridge(event)
    raise AssertionError(f"no {tool}/{status} fixture")


@pytest.fixture
def _isolate_catch_diary(_isolate_machine_state: Path) -> Path:
    """Where the guard's catch diary lands: ``config.STATE_DIR``, moved per test by conftest."""
    import config

    assert Path(config.STATE_DIR) == _isolate_machine_state / "engine-state"
    return _isolate_machine_state / "engine-state"


def _log_text() -> str:
    import shared

    path = Path(shared.HOOK_LOG_FILE)
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- the fixtures themselves ---------------------------------------------------------------------


def test_fixture_covers_the_measured_tools() -> None:
    tools = {e["tool"] for e in EVENTS}
    assert {"shell", "read", "grep", "glob", "webfetch", "subagent", "write", "edit"} <= tools
    assert any(e["status"] == "error" for e in EVENTS)


@pytest.mark.parametrize("event", EVENTS, ids=lambda e: f"{e['tool']}-{e['status']}")
def test_every_real_fixture_is_clean(event: dict[str, Any]) -> None:
    assert posttool.scan(_bridge(event)) == ""


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("shell", "hi\n"),
        ("read", "1: x"),
        ("grep", "Line 1: y"),
        ("webfetch", "This domain is for use"),
        ("subagent", "PROBE-BLOCK-MSG"),
        ("execute", "worktrees"),
    ],
)
def test_text_is_extracted_from_each_measured_shape(tool: str, expected: str) -> None:
    import codex_shared

    codex_post = codex_shared.load_hook_module("post-tool-secret-codex")
    assert expected in posttool.printed_text(_first(tool), codex_post)


def test_error_text_is_extracted() -> None:
    import codex_shared

    codex_post = codex_shared.load_hook_module("post-tool-secret-codex")
    text = posttool.printed_text(_first("shell", "error"), codex_post)
    assert "PROBE-BLOCK-MSG" in text
    # Ids and rule tables are not text: the session id is never scanned.
    assert "ses_" not in text


# --- catches ---------------------------------------------------------------------------------------


def test_token_in_shell_output_yields_a_note() -> None:
    payload = _first("shell")
    leaked = f"aws_access_key_id = {FAKE_AWS}\n"
    payload["result"]["output"]["output"] = leaked
    payload["result"]["content"][0]["text"] = leaked
    note = posttool.scan(payload)
    assert "possible secret" in note
    assert FAKE_AWS not in note


def test_one_key_in_content_and_output_is_one_catch(_isolate_catch_diary: Path) -> None:
    payload = _first("shell")
    leaked = f"key {FAKE_AWS}\n"
    payload["result"]["output"]["output"] = leaked
    payload["result"]["content"][0]["text"] = leaked
    assert posttool.scan(payload)
    diary = (_isolate_catch_diary / "secret-catches.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in diary.splitlines()]
    assert len(rows) == 1 and rows[0]["count"] == 1
    assert FAKE_AWS not in diary


def test_token_in_raw_file_content_of_a_read_yields_a_note() -> None:
    payload = _first("read")
    payload["result"]["output"]["content"] = f"KEY={FAKE_AWS}\n"
    assert posttool.scan(payload)


def test_token_in_a_grep_match_yields_a_note() -> None:
    payload = _first("grep")
    payload["result"]["output"][0]["text"] = f"KEY={FAKE_AWS}"
    payload["result"]["content"] = []
    assert posttool.scan(payload)


def test_token_in_webfetch_output_yields_a_note() -> None:
    payload = _first("webfetch")
    payload["result"]["output"]["output"] = f"leaked {FAKE_AWS} here"
    payload["result"]["content"] = []
    assert posttool.scan(payload)


def test_token_in_an_error_message_yields_a_note() -> None:
    payload = _first("shell", "error")
    payload["error"]["error"]["reason"] = f"failed near {FAKE_AWS}"
    assert posttool.scan(payload)


def test_a_bare_string_result_is_scanned() -> None:
    assert posttool.scan({"tool_name": "x", "session_id": "s", "result": f"k {FAKE_AWS}"})


def test_tool_input_is_never_read() -> None:
    payload = _first("shell")
    payload["input"] = {"command": f"echo {FAKE_AWS}"}
    payload["tool_input"] = {"command": f"echo {FAKE_AWS}"}
    assert posttool.scan(payload) == ""


# --- clean / empty -----------------------------------------------------------------------------


def test_clean_output_is_empty_string() -> None:
    assert posttool.scan(_first("shell")) == ""
    assert posttool.scan_or_error(_first("shell")) == {}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tool_name": "shell", "result": None},
        {"tool_name": "shell", "result": {"output": {"exit": 0, "output": ""}, "content": []}},
        {"tool_name": "shell", "result": {"output": "   ", "content": [{"type": "text"}]}},
    ],
)
def test_empty_output_is_empty_string(payload: dict[str, Any]) -> None:
    assert posttool.scan(payload) == ""


def test_scan_or_error_returns_the_note() -> None:
    payload = _first("shell")
    payload["result"]["content"][0]["text"] = f"k {FAKE_AWS}"
    out = posttool.scan_or_error(payload)
    assert set(out) == {"note"} and out["note"]
    assert "opencode-post | shell | FLAG" in _log_text()


# --- fail open ---------------------------------------------------------------------------------


def _with_guard(monkeypatch: pytest.MonkeyPatch, guard: Any) -> None:
    import codex_shared

    real = codex_shared.load_hook_module

    def fake(stem: str) -> Any:
        return guard if stem == "post-tool-secret-guard" else real(stem)

    monkeypatch.setattr(codex_shared, "load_hook_module", fake)


def _leaky_shell() -> dict[str, Any]:
    payload = _first("shell")
    payload["result"]["content"][0]["text"] = f"k {FAKE_AWS}"
    return payload


def test_scanner_unavailable_is_an_error_not_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real guard with its optional ``secret_scan`` import failed: it would return None."""
    import codex_shared

    guard = codex_shared.load_hook_module("post-tool-secret-guard")
    monkeypatch.setattr(guard, "secret_scan", None)
    _with_guard(monkeypatch, guard)
    # Prove the hazard: the guard itself says nothing for a leaky output.
    assert guard.process(
        {"tool_name": "Bash", "tool_response": {"stdout": f"k {FAKE_AWS}"}, "session_id": "s"}
    ) is None

    with pytest.raises(posttool.ScannerUnavailable):
        posttool.scan(_leaky_shell())
    assert posttool.scan_or_error(_leaky_shell()) == {"error": "scanner-unavailable"}
    log = _log_text()
    assert "opencode-post | shell | ERROR" in log and "scanner-unavailable" in log
    assert FAKE_AWS not in log


def test_scanner_unavailable_is_reported_even_for_empty_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_guard(monkeypatch, SimpleNamespace(secret_scan=None, process=lambda _e: None))
    assert posttool.scan_or_error({"tool_name": "shell"}) == {"error": "scanner-unavailable"}


def test_delegate_unavailable_fails_open_with_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_guard(monkeypatch, None)
    with pytest.raises(posttool.DelegateUnavailable):
        posttool.scan(_leaky_shell())
    assert posttool.scan_or_error(_leaky_shell()) == {"error": "delegate-unavailable"}
    log = _log_text()
    assert "opencode-post | shell | ERROR" in log and "delegate-unavailable" in log


def test_delegate_raising_logs_only_the_label(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(event: dict[str, Any]) -> None:
        raise ValueError(f"cannot parse {event['tool_response']['stdout']}")

    _with_guard(monkeypatch, SimpleNamespace(secret_scan=object(), process=boom))
    assert posttool.scan_or_error(_leaky_shell()) == {"error": "ValueError"}
    log = _log_text()
    assert "opencode-post | shell | ERROR" in log and "ValueError" in log
    assert FAKE_AWS not in log
