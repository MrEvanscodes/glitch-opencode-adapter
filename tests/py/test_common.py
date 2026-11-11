"""Tests for src/bridge/common.py: root walk, event read, log, emit."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import common
from conftest import BRIDGE_DIR, GLITCH_ROOT, SCRIPTS_DIR, WORKSPACE


def _make_root(folder: Path) -> Path:
    """A fake Glitch root: both markers present."""
    (folder / ".claude" / "hooks").mkdir(parents=True)
    (folder / ".claude" / "hooks" / "write-guard.py").write_text("", encoding="utf-8")
    (folder / "CLAUDE.md").write_text("# root\n", encoding="utf-8")
    return Path(os.path.realpath(folder))


# --- resolve_root -------------------------------------------------------------------------------


def test_resolve_root_at_root(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "glitch")
    assert common.resolve_root(str(root)) == root


def test_resolve_root_from_subfolder(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "glitch")
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert common.resolve_root(str(deep)) == root


def test_resolve_root_accepts_pathlike(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "glitch")
    assert common.resolve_root(root / ".claude") == root


def test_workspace_with_its_own_claude_md_is_walked_past(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "glitch")
    ws = root / "workspaces" / "proj"
    (ws / "src").mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# stamped workspace\n", encoding="utf-8")
    assert common.resolve_root(str(ws)) == root
    assert common.resolve_root(str(ws / "src")) == root


def test_half_markers_are_not_a_root(tmp_path: Path) -> None:
    """CLAUDE.md alone, or the guard alone, never makes a root."""
    only_md = tmp_path / "only-md"
    only_md.mkdir()
    (only_md / "CLAUDE.md").write_text("x", encoding="utf-8")
    only_guard = tmp_path / "only-guard"
    (only_guard / ".claude" / "hooks").mkdir(parents=True)
    (only_guard / ".claude" / "hooks" / "write-guard.py").write_text("", encoding="utf-8")
    assert common.resolve_root(str(only_md)) is None
    assert common.resolve_root(str(only_guard)) is None


def test_real_workspace_resolves_to_the_real_root() -> None:
    """The plan's GOTCHA, pinned on the real tree: this workspace's stamped CLAUDE.md is passed."""
    assert (WORKSPACE / "CLAUDE.md").is_file()
    assert not (WORKSPACE / ".claude" / "hooks" / "write-guard.py").exists()
    assert common.resolve_root(str(WORKSPACE)) == GLITCH_ROOT
    assert common.resolve_root(str(BRIDGE_DIR)) == GLITCH_ROOT
    assert (GLITCH_ROOT / ".claude" / "hooks" / "write-guard.py").is_file()


def test_symlinked_folder_resolves_through_the_link(tmp_path: Path) -> None:
    root = _make_root(tmp_path / "glitch")
    (root / "sub").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = outside / "link-to-sub"
    try:
        link.symlink_to(root / "sub", target_is_directory=True)
    except OSError:  # pragma: no cover - Windows without symlink rights
        pytest.skip("symlinks not permitted here")
    # Lexically the link sits outside any root; only the realpath finds it.
    assert common.resolve_root(str(link)) == root


def test_symlink_out_of_glitch_is_not_in_glitch(tmp_path: Path) -> None:
    """A link INSIDE a root that points outside resolves where it points: not in Glitch."""
    root = _make_root(tmp_path / "glitch")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "link-out"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:  # pragma: no cover
        pytest.skip("symlinks not permitted here")
    assert common.resolve_root(str(link)) is None


def test_none_found_returns_none(tmp_path: Path) -> None:
    plain = tmp_path / "plain" / "deeper"
    plain.mkdir(parents=True)
    assert common.resolve_root(str(plain)) is None


def test_nonexistent_start_inside_a_root_still_finds_it(tmp_path: Path) -> None:
    """A deleted subfolder must not switch the guard off."""
    root = _make_root(tmp_path / "glitch")
    assert common.resolve_root(str(root / "gone" / "also-gone")) == root


def test_nonexistent_start_outside_any_root_is_none(tmp_path: Path) -> None:
    assert common.resolve_root(str(tmp_path / "nope" / "nothing")) is None


@pytest.mark.parametrize("start", ["", "   ", None])
def test_empty_start_is_none_never_process_cwd(
    start: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(GLITCH_ROOT)  # even with the process sitting in a root
    assert common.resolve_root(start) is None


def test_resolve_root_never_raises() -> None:
    assert common.resolve_root("\x00bad") is None


# --- engine_on_path / import order --------------------------------------------------------------


def test_engine_on_path_inserts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(SCRIPTS_DIR)])
    assert common.engine_on_path(GLITCH_ROOT) == SCRIPTS_DIR
    common.engine_on_path(GLITCH_ROOT)
    assert sys.path.count(str(SCRIPTS_DIR)) == 1
    assert sys.path[0] == str(SCRIPTS_DIR)


def test_common_imports_without_the_engine() -> None:
    """Fresh interpreter, isolated mode, no engine on the path: import must succeed and pull in
    no engine module."""
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import common; "
        "bad = [m for m in ('codex_shared', 'shared', 'config') if m in sys.modules]; "
        "assert not bad, bad; "
        "assert not any(p.endswith('scripts') and '.claude' in p for p in sys.path), sys.path; "
        "print('ok')"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(BRIDGE_DIR)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


# --- read_event ---------------------------------------------------------------------------------


def _fake_stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))


def test_read_event_strips_bom_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_stdin(monkeypatch, b"\xef\xbb\xbf" + json.dumps({"tool": "shell"}).encode())
    assert common.read_event() == {"tool": "shell"}


def test_read_event_strips_bom_from_raw_text() -> None:
    assert common.read_event("\ufeff{\"a\": 1}") == {"a": 1}


def test_read_event_without_bom(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_stdin(monkeypatch, b'{"sessionID": "ses_1", "input": {"command": "echo hi"}}')
    assert common.read_event() == {"sessionID": "ses_1", "input": {"command": "echo hi"}}


@pytest.mark.parametrize("data", [b"", b"   \n", b"not json", b"[1, 2]", b"\xef\xbb\xbf"])
def test_read_event_garbage_is_empty(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    _fake_stdin(monkeypatch, data)
    assert common.read_event() == {}


def test_read_event_invalid_utf8_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_stdin(monkeypatch, b'{"a": "\xff\xfe"}')
    assert common.read_event() == {}


def test_read_event_tolerates_windows_backslashes() -> None:
    assert common.read_event('{"path": "C:\\Users\\me\\a.txt"}') == {
        "path": "C:\\Users\\me\\a.txt"
    }


def test_read_event_uses_the_engine_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    import codex_shared

    seen: list[str] = []

    def spy(raw: str | None = None) -> dict[str, object]:
        seen.append(raw or "")
        return {"via": "engine"}

    monkeypatch.setattr(codex_shared, "read_event", spy)
    assert common.read_event("\ufeff{}") == {"via": "engine"}
    assert seen == ["{}"]


def test_read_event_falls_back_when_engine_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "codex_shared", None)  # import now raises ImportError
    assert common.read_event("\ufeff{\"k\": \"C:\\x\"}") == {"k": "C:\\x"}
    assert common.read_event("nope") == {}


def test_read_event_bom_in_a_fresh_interpreter() -> None:
    """The real stdin path, BOM-prefixed bytes, engine put on the path the bridge's way."""
    code = (
        "import sys, json; sys.path.insert(0, sys.argv[1]); import common; "
        "root = common.resolve_root(sys.argv[2]); common.engine_on_path(root); "
        "common.emit(common.read_event())"
    )
    payload = b"\xef\xbb\xbf" + json.dumps({"tool": "read", "input": {"path": "a.txt"}}).encode()
    out = subprocess.run(
        [sys.executable, "-c", code, str(BRIDGE_DIR), str(WORKSPACE)],
        input=payload, capture_output=True, timeout=60, check=False,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"tool": "read", "input": {"path": "a.txt"}}


# --- emit ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "obj",
    [{}, {"a": 1}, {"text": "naïve — \u2603 \"quoted\"\n"}, [1, "two", None, True], "s", 0],
)
def test_emit_writes_exactly_one_valid_json_document(
    obj: object, capsys: pytest.CaptureFixture[str]
) -> None:
    common.emit(obj)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == obj
    assert captured.err == ""


@pytest.mark.parametrize("bad", [{"x": object()}, {"n": float("nan")}, {"i": float("inf")}])
def test_emit_unserialisable_raises_and_writes_nothing(
    bad: object, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises((TypeError, ValueError)):
        common.emit(bad)
    assert capsys.readouterr().out == ""


# --- log ----------------------------------------------------------------------------------------


def test_log_lands_in_the_isolated_hook_log(_isolate_machine_state: Path) -> None:
    import shared

    common.log("opencode-pre", "shell", "OK", time.time(), "detail-xyz")
    assert shared.HOOK_LOG_FILE == _isolate_machine_state / "hook-execution.log"
    text = shared.HOOK_LOG_FILE.read_text(encoding="utf-8")
    assert "opencode-pre | shell | OK" in text
    assert "detail-xyz" in text


def test_log_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import shared

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(shared, "log_hook_execution", boom)
    common.log("opencode-pre", "shell", "ERROR", time.time(), "d")  # no exception


def test_isolation_points_away_from_real_state(_isolate_machine_state: Path) -> None:
    import config
    import read_grant

    real_state = GLITCH_ROOT / ".claude" / "data" / "state"
    for path in (
        config.REFUSED_ENGINE_EDITS_FILE,
        config.KEEPER_HEALTH_FILE,
        config.RUN_LEDGER_FILE,
        read_grant.STATE_FILE,
    ):
        assert real_state not in path.parents, path
    assert os.environ["GLITCH_READ_GRANT_STATE_DIR"] == str(read_grant.STATE_DIR)
