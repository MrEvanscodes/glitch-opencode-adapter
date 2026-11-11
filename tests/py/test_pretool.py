"""Tests for src/bridge/pretool.py: OpenCode tool calls reach the right Glitch guard, unchanged.

Nothing here re-tests a rule: the rules live in ``guard_rules`` and the hook modules the bridge
delegates to.
What is pinned is that each MEASURED OpenCode tool shape (``docs/api-findings.md`` item 2) reaches
the right delegate in the delegate's own shape, that the verdict equals the delegate's own verdict
and the Cursor adapter's for the equivalent input (contract-drift twins), that a deny always
carries words for the member and the model, that an allow says nothing, and that a broken delegate
fails open, loudly.

Every path below is a harmless canary: nothing is created at it and nothing is read from it; the
guards judge spellings.
Fresh-interpreter tests use only a non-engine-tier deny (a forced ``git clean`` in the Brain),
because the engine-tier breadcrumb is redirected in-process only.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import codex_shared
import common
import pretool
import shared
from conftest import BRIDGE_DIR, GLITCH_ROOT, SCRIPTS_DIR

# Loading an engine hook by path must not leave bytecode in the engine's own hooks folder.
sys.dont_write_bytecode = True

R = str(GLITCH_ROOT)
HOOKS = GLITCH_ROOT / ".claude" / "hooks"
OUTSIDE = "/opt/glitch-opencode-canary"  # never created; not a folder Glitch trusts
SOUL = str(GLITCH_ROOT / "glitch-mem" / "Memory" / "SOUL.md")
UNLOCK = "GLITCH_" + "ALLOW_CANARY"  # split so this file never carries the token whole
_BOM = b"\xef\xbb\xbf"

# The V4A patch OpenCode really sent (spike/evidence/execute-before.jsonl), with a protected path.
V4A_ADD = "*** Begin Patch\n*** Add File: CLAUDE.md\n+x\n*** End Patch"
V4A_FULL = (
    "*** Begin Patch\n"
    "*** Add File: notes/a.md\n+alpha\n"
    "*** Update File: notes/b.md\n*** Move to: notes/c.md\n@@\n-old\n+new\n"
    "*** Delete File: notes/d.md\n"
    "*** End Patch"
)


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ev(tool: str, ti: Any, session_dir: str = R, session: str = "ses_test", **extra: Any) -> dict[str, Any]:
    """A bridge payload as the plugin builds it from a measured ``execute.before`` event."""
    ev: dict[str, Any] = {
        "tool_name": tool, "tool_input": ti, "session_dir": session_dir, "session_id": session,
    }
    ev.update(extra)
    return ev


def _reset_grants(session: str) -> None:
    """Forget the session's recorded pauses, so a twin's second side starts where its first did."""
    import read_grant

    Path(read_grant.STATE_FILE).unlink(missing_ok=True)
    Path(read_grant.once_file(session)).unlink(missing_ok=True)


class _Recorder:
    """Stands in for both delegates; records the inner event each one is handed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def loader(self, stem: str) -> Any:
        def decide(inner: dict[str, Any]) -> None:
            self.calls.append((stem, inner))
            return None

        return types.SimpleNamespace(decide=decide)


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(codex_shared, "load_hook_module", rec.loader)
    return rec


def _one(rec: _Recorder, event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    assert pretool.decide(event) is None
    assert len(rec.calls) == 1, rec.calls
    return rec.calls[0]


# --- (1) each measured shape reaches the right delegate in the right shape ----------------------


def test_shell_without_workdir_runs_in_the_session_folder(recorder: _Recorder) -> None:
    stem, inner = _one(recorder, _ev("shell", {"command": "echo hi"}, session_dir="/opt/sess"))
    assert stem == "write-guard-codex"
    assert inner == {
        "tool_name": "Bash", "tool_input": {"command": "echo hi", "workdir": "/opt/sess"},
        "cwd": "/opt/sess", "session_id": "ses_test",
    }


def test_shell_workdir_is_carried_as_given(recorder: _Recorder) -> None:
    _, inner = _one(recorder, _ev("shell", {"command": "ls", "workdir": "sub"}, session_dir="/opt/s"))
    assert inner["tool_input"] == {"command": "ls", "workdir": "sub"}
    assert inner["cwd"] == "/opt/s"  # the guard joins a relative workdir onto this


def test_shell_never_uses_this_process_cwd(recorder: _Recorder, tmp_path: Path) -> None:
    os.chdir(tmp_path)
    _, inner = _one(recorder, {"tool_name": "shell", "tool_input": {"command": "ls"}})
    assert inner["cwd"] == "" and inner["tool_input"]["workdir"] == ""
    assert str(tmp_path) not in json.dumps(inner)


@pytest.mark.parametrize(
    ("tool", "ti", "name", "expect_ti"),
    [
        ("read", {"path": "a.txt", "offset": 1, "limit": 5}, "Read",
         {"path": "/opt/sess/a.txt", "offset": 1, "limit": 5}),
        ("read", {"path": "/etc/hosts"}, "Read", {"path": "/etc/hosts"}),
        ("read", {"path": "~/notes.txt"}, "Read", {"path": "~/notes.txt"}),
        ("grep", {"pattern": "y", "include": "*.txt"}, "Grep", {"pattern": "y", "include": "*.txt"}),
        ("grep", {"pattern": "y", "path": "src"}, "Grep", {"pattern": "y", "path": "/opt/sess/src"}),
        ("glob", {"pattern": "*.txt"}, "Glob", {"pattern": "/opt/sess/*.txt"}),
        ("glob", {"pattern": "/abs/**/*.py"}, "Glob", {"pattern": "/abs/**/*.py"}),
        ("glob", {"pattern": "*.md", "path": "docs"}, "Glob", {"pattern": "*.md", "path": "/opt/sess/docs"}),
    ],
)
def test_read_tools_reach_the_read_guard_with_absolute_paths(
    recorder: _Recorder, tool: str, ti: dict[str, Any], name: str, expect_ti: dict[str, Any]
) -> None:
    stem, inner = _one(recorder, _ev(tool, ti, session_dir="/opt/sess"))
    assert stem == "read-guard"
    assert inner["tool_name"] == name
    assert inner["tool_input"] == expect_ti
    assert inner["cwd"] == "/opt/sess"


def test_windows_paths_are_judged_absolute_on_spelling(recorder: _Recorder) -> None:
    _, inner = _one(recorder, _ev("read", {"path": "C:\\Users\\m\\a.txt"}, session_dir="C:\\proj"))
    assert inner["tool_input"]["path"] == "C:\\Users\\m\\a.txt"
    recorder.calls.clear()
    _, inner = _one(recorder, _ev("read", {"path": "sub\\a.txt"}, session_dir="C:\\proj"))
    assert inner["tool_input"]["path"] == "C:/proj/sub\\a.txt"


def test_patch_reaches_apply_patch_with_the_v4a_text(recorder: _Recorder) -> None:
    stem, inner = _one(recorder, _ev("patch", {"patchText": V4A_ADD}))
    assert stem == "write-guard-codex"
    assert inner["tool_name"] == "apply_patch"
    assert inner["tool_input"] == {"command": V4A_ADD}


def test_opencode_v4a_is_the_grammar_apply_patch_parses() -> None:
    """The measured sample and every header form parse in the guard's own parser."""
    guard = codex_shared.load_hook_module("write-guard-codex")
    assert guard.parse_patch(V4A_ADD) == [
        {"op": "add", "path": "CLAUDE.md", "move_to": None, "added": ["x"]}
    ]
    entries = guard.parse_patch(V4A_FULL)
    assert [(e["op"], e["path"], e["move_to"]) for e in entries] == [
        ("add", "notes/a.md", None), ("update", "notes/b.md", "notes/c.md"),
        ("delete", "notes/d.md", None),
    ]


def test_edit_keys_are_renamed_for_the_content_scan(recorder: _Recorder) -> None:
    ti = {"path": "a.txt", "oldString": "x", "newString": "y", "replaceAll": True}
    stem, inner = _one(recorder, _ev("edit", ti))
    assert stem == "write-guard-codex"
    assert inner["tool_name"] == "edit"
    assert inner["tool_input"] == {
        "path": "a.txt", "old_string": "x", "new_string": "y", "replace_all": True,
    }


def test_write_passes_path_and_content_through(recorder: _Recorder) -> None:
    _, inner = _one(recorder, _ev("write", {"path": "a.txt", "content": "x"}))
    assert inner["tool_name"] == "write"
    assert inner["tool_input"] == {"path": "a.txt", "content": "x"}


@pytest.mark.parametrize(
    ("tool", "ti", "name", "expect_ti"),
    [
        ("experimental_image_generate", {"prompt": "p", "path": "x.png"}, "Write",
         {"prompt": "p", "path": "x.png"}),
        ("opencode_worktree_remove", {"directory": "wt", "force": True}, "opencode_worktree_remove",
         {"directory": "wt", "force": True, "path": "wt"}),
        ("opencode_worktree_create", {"directory": "wt"}, "opencode_worktree_create",
         {"directory": "wt", "path": "wt"}),
        ("opencode_session_move", {"directory": "/x"}, "opencode_session_move",
         {"directory": "/x", "path": "/x"}),
        ("opencode_worktree_list", {}, "opencode_worktree_list", {}),
        ("execute", {"code": "return 1"}, "execute", {"code": "return 1"}),
        ("webfetch", {"url": "https://example.com"}, "webfetch", {"url": "https://example.com"}),
        ("websearch", {"query": "q"}, "websearch", {"query": "q"}),
        ("subagent", {"agent": "general", "description": "d", "prompt": "p"}, "subagent",
         {"agent": "general", "description": "d", "prompt": "p"}),
        ("skill", {"id": "bootstrap"}, "skill", {"id": "bootstrap"}),
        ("question", {"questions": []}, "question", {"questions": []}),
        ("some_mcp_tool", {"path": "p", "content": "c"}, "some_mcp_tool", {"path": "p", "content": "c"}),
    ],
)
def test_other_tools_reach_the_shape_read_arm(
    recorder: _Recorder, tool: str, ti: dict[str, Any], name: str, expect_ti: dict[str, Any]
) -> None:
    stem, inner = _one(recorder, _ev(tool, ti))
    assert stem == "write-guard-codex"
    assert inner["tool_name"] == name
    assert inner["tool_input"] == expect_ti


def test_a_path_wins_over_directory(recorder: _Recorder) -> None:
    _, inner = _one(recorder, _ev("opencode_x", {"directory": "d", "path": "p"}))
    assert inner["tool_input"]["path"] == "p"


def test_sub_agent_marker(recorder: _Recorder) -> None:
    _, inner = _one(recorder, _ev("read", {"path": "a"}, parent_id="ses_parent"))
    assert inner["agent_id"] == "ses_parent"
    recorder.calls.clear()
    for empty in ("", None, False):
        _, inner = _one(recorder, _ev("read", {"path": "a"}, parent_id=empty))
        assert "agent_id" not in inner
        recorder.calls.clear()


def test_opencode_spellings_are_accepted(recorder: _Recorder) -> None:
    ev = {"tool": "read", "input": {"path": "a"}, "session_dir": "/s", "sessionID": "ses_x",
          "parentID": "ses_p"}
    _, inner = _one(recorder, ev)
    assert inner == {"tool_name": "Read", "tool_input": {"path": "/s/a"}, "cwd": "/s",
                     "session_id": "ses_x", "agent_id": "ses_p"}


def test_no_inner_event_ever_carries_cursor_version(recorder: _Recorder) -> None:
    for tool in ("shell", "read", "grep", "glob", "patch", "edit", "write", "webfetch", "x"):
        pretool.decide(_ev(tool, {"path": "a", "command": "ls"}, cursor_version="2026.1"))
    assert len(recorder.calls) == 9
    assert all("cursor_version" not in inner for _, inner in recorder.calls)


def test_the_read_guard_is_called_once_per_tool_call(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tool in ("read", "grep", "glob"):
        recorder.calls.clear()
        pretool.decide(_ev(tool, {"path": OUTSIDE, "pattern": "*"}))
        assert [stem for stem, _ in recorder.calls] == ["read-guard"]


# --- the contract-drift twins --------------------------------------------------------------------

# (tool, OpenCode input, expected kind or None, the Cursor-dialect equivalent (tool, input))
TABLE: list[tuple[str, dict[str, Any], str | None, tuple[str, dict[str, Any]]]] = [
    ("shell", {"command": "git clean -fdx"}, "git-clean", ("Shell", {"command": "git clean -fdx"})),
    ("shell", {"command": "ls"}, None, ("Shell", {"command": "ls"})),
    ("shell", {"command": "rm CLAUDE.md"}, "protected-file", ("Shell", {"command": "rm CLAUDE.md"})),
    ("read", {"path": ".claude/data/google-accounts/canary/token.json"}, "secret-read",
     ("Read", {"path": ".claude/data/google-accounts/canary/token.json"})),
    ("read", {"path": f"{OUTSIDE}/notes.txt"}, "read-outside",
     ("Read", {"path": f"{OUTSIDE}/notes.txt"})),
    ("read", {"path": f"{R}/README.md"}, None, ("Read", {"path": f"{R}/README.md"})),
    ("glob", {"pattern": f"{OUTSIDE}/*.txt"}, "read-outside", ("Glob", {"pattern": f"{OUTSIDE}/*.txt"})),
    ("grep", {"pattern": "x", "path": OUTSIDE}, "read-outside", ("Grep", {"pattern": "x", "path": OUTSIDE})),
    ("write", {"path": "CLAUDE.md", "content": "x"}, "engine",
     ("Write", {"path": "CLAUDE.md", "content": "x"})),
    ("write", {"path": SOUL, "content": "x"}, "soul", ("Write", {"path": SOUL, "content": "x"})),
    ("write", {"path": "workspaces/opencode-canary/notes.md", "content": "x"}, None,
     ("Write", {"path": "workspaces/opencode-canary/notes.md", "content": "x"})),
    ("edit", {"path": ".claude/settings.local.json", "oldString": "{",
              "newString": '{"env": {"' + UNLOCK + '": "1"},'}, "settings-unlock",
     ("Edit", {"path": ".claude/settings.local.json", "old_string": "{",
               "new_string": '{"env": {"' + UNLOCK + '": "1"},'})),
    ("patch", {"patchText": V4A_ADD}, "engine", ("apply_patch", {"command": V4A_ADD})),
    ("patch", {"patchText": "*** Begin Patch\n*** Delete File: README.md\n*** End Patch"},
     "protected-file",
     ("apply_patch", {"command": "*** Begin Patch\n*** Delete File: README.md\n*** End Patch"})),
    ("patch", {"patchText": "*** Begin Patch\n*** Add File: workspaces/opencode-canary/a.md\n+x\n"
                            "*** End Patch"}, None,
     ("apply_patch", {"command": "*** Begin Patch\n*** Add File: workspaces/opencode-canary/a.md\n"
                                 "+x\n*** End Patch"})),
    ("opencode_session_move", {"directory": "CLAUDE.md"}, "protected-file",
     ("opencode_session_move", {"path": "CLAUDE.md"})),
    ("experimental_image_generate", {"prompt": "a cat", "path": "CLAUDE.md"}, "engine",
     ("Write", {"prompt": "a cat", "path": "CLAUDE.md"})),
    ("webfetch", {"url": "https://example.com"}, None, ("webfetch", {"url": "https://example.com"})),
]
_IDS = [f"{i}-{row[0]}-{row[2]}" for i, row in enumerate(TABLE)]


def _direct(inner_route: str, inner: dict[str, Any]) -> tuple[str, str, str] | None:
    """The delegate's own verdict on the inner event, shaped the way the Cursor adapter shapes it."""
    if inner_route == "write":
        hit = codex_shared.load_hook_module("write-guard-codex").decide(inner)
        return None if hit is None else (str(hit[0]), str(hit[1]), str(hit[1]))
    guard = codex_shared.load_hook_module("read-guard")
    result = guard.decide(inner)
    if result is None:
        return None
    out = result["hookSpecificOutput"]
    reason = out["permissionDecisionReason"]
    contract = out.get("additionalContext") or ""
    return guard._detail(result), reason, f"{reason}\n\n{contract}" if contract else reason


@pytest.mark.parametrize(("tool", "ti", "kind", "cursor"), TABLE, ids=_IDS)
def test_twin_with_the_delegate_called_directly(
    tool: str, ti: dict[str, Any], kind: str | None, cursor: tuple[str, dict[str, Any]]
) -> None:
    event = _ev(tool, ti, session="ses_twin")
    routed = pretool.translate(event)
    assert routed is not None
    direct = _direct(*routed)
    _reset_grants("ses_twin")
    via = pretool.decide(event)
    assert via == direct
    assert (None if via is None else via[0]) == kind


@pytest.mark.parametrize(("tool", "ti", "kind", "cursor"), TABLE, ids=_IDS)
def test_twin_with_the_cursor_adapter(
    tool: str, ti: dict[str, Any], kind: str | None, cursor: tuple[str, dict[str, Any]]
) -> None:
    guard_cursor = _load("opencode_twin_write_guard_cursor", HOOKS / "write-guard-cursor.py")
    cursor_tool, cursor_ti = cursor
    cursor_event = {
        "hook_event_name": "preToolUse", "tool_name": cursor_tool, "tool_input": cursor_ti,
        "conversation_id": "ses_twin", "session_id": "ses_twin", "workspace_roots": [R],
    }
    theirs = guard_cursor.decide(cursor_event)
    _reset_grants("ses_twin")
    ours = pretool.decide(_ev(tool, ti, session="ses_twin"))
    assert ours == theirs
    assert (None if ours is None else ours[0]) == kind


# --- (4) invariant sweep: a deny always carries words for both readers ---------------------------


def test_every_deny_carries_member_and_model_words() -> None:
    denies = 0
    for i, (tool, ti, kind, _) in enumerate(TABLE):
        hit = pretool.judge(_ev(tool, ti, session=f"ses_sweep_{i}"))
        if kind is None:
            assert hit is None, (tool, ti, hit)
            continue
        denies += 1
        assert hit is not None and set(hit) == {"deny"}, (tool, ti, hit)
        deny = hit["deny"]
        assert deny["kind"] == kind
        assert deny["member"].strip() and deny["model"].strip(), (tool, ti)
        assert deny["member"] in deny["model"]
    assert denies >= 12


def test_sub_agent_pause_hands_the_truth_back_up_and_teaches_nothing() -> None:
    hit = pretool.decide(_ev("read", {"path": f"{OUTSIDE}/a.txt"}, session="ses_sub",
                             parent_id="ses_parent"))
    assert hit is not None and hit[0] == "read-outside"
    assert "sub-agent" in hit[2]
    assert "SAY THIS VERBATIM" not in hit[2]
    member = pretool.decide(_ev("read", {"path": f"{OUTSIDE}/b.txt"}, session="ses_member"))
    assert member is not None and "SAY THIS VERBATIM" in member[2]


# --- where a shell command lands -----------------------------------------------------------------


def test_workdir_inside_glitch_from_a_session_outside_is_still_judged_there() -> None:
    """A relative operand lands in the workdir: ``rm CLAUDE.md`` there is Glitch's CLAUDE.md."""
    hit = pretool.decide(_ev("shell", {"command": "rm -f CLAUDE.md", "workdir": R},
                             session_dir=OUTSIDE))
    assert hit is not None and hit[0] == "protected-file"


def test_workdir_outside_glitch_aimed_at_glitch_is_still_denied() -> None:
    """The case the plan pins: a ``/tmp``-style workdir never switches the guards off."""
    hit = pretool.decide(_ev("shell", {"command": f"rm -f {R}/CLAUDE.md", "workdir": OUTSIDE}))
    assert hit is not None and hit[0] == "protected-file"
    hit = pretool.decide(_ev("shell", {"command": "git clean -fdx", "workdir": OUTSIDE}))
    assert hit is not None and hit[0] == "git-clean"


def test_workdir_outside_glitch_judges_relative_operands_where_they_land() -> None:
    """``rm CLAUDE.md`` in an unrelated folder is that folder's file, not Glitch's."""
    assert pretool.decide(_ev("shell", {"command": "rm -f CLAUDE.md", "workdir": OUTSIDE})) is None
    assert pretool.decide(_ev("shell", {"command": "rm -f CLAUDE.md"})) is not None


def test_relative_read_resolves_against_the_session_not_the_glitch_root() -> None:
    """``README.md`` from a session outside Glitch is outside Glitch, and is paused."""
    hit = pretool.decide(_ev("read", {"path": "README.md"}, session_dir=OUTSIDE))
    assert hit is not None and hit[0] == "read-outside"
    assert pretool.decide(_ev("read", {"path": "README.md"})) is None


# --- (3) cold-start twin ----------------------------------------------------------------------------


def test_cold_start_twin_in_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import read_grant

    cold = tmp_path / "never-made" / "read-grant"
    monkeypatch.setattr(read_grant, "STATE_DIR", cold)
    monkeypatch.setattr(read_grant, "STATE_FILE", cold / "read-grant.json")
    for tool, ti in (("shell", {"command": "git clean -fdx"}), ("read", {"path": f"{OUTSIDE}/c"})):
        first = pretool.decide(_ev(tool, ti, session="ses_cold"))
        again = pretool.decide(_ev(tool, ti, session="ses_cold"))
        assert first is not None and again is not None
        assert first[0] == again[0]
        assert all(part.strip() for part in (*first, *again))
    assert pretool.decide(_ev("shell", {"command": "ls"}, session="ses_cold")) is None


_DRIVER = (
    "import sys\n"
    f"sys.path.insert(0, {str(BRIDGE_DIR)!r})\n"
    "import common\n"
    f"common.engine_on_path({R!r})\n"
    "import pretool\n"
    "verdict = pretool.judge(common.read_event())\n"
    "if verdict is not None:\n"
    "    common.emit(verdict)\n"
)


def _fresh(event: dict[str, Any] | bytes, env_extra: dict[str, str] | None = None,
           cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    data = event if isinstance(event, bytes) else _BOM + json.dumps(event).encode("utf-8")
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env_extra or {})}
    return subprocess.run(
        [sys.executable, "-c", _DRIVER], input=data, capture_output=True, env=env,
        cwd=str(cwd or SCRIPTS_DIR), timeout=60, check=False,
    )


def test_cold_start_twin_in_a_fresh_interpreter(tmp_path: Path) -> None:
    env = {"GLITCH_READ_GRANT_STATE_DIR": str(tmp_path / "absent" / "read-grant"),
           "GLITCH_HOOK_LOG_FILE": str(tmp_path / "hook.log")}
    event = _ev("shell", {"command": "git clean -fdx"}, session="ses_fresh")
    first = _fresh(event, env, cwd=tmp_path)
    again = _fresh(event, env, cwd=tmp_path)
    assert first.returncode == again.returncode == 0
    assert first.stdout == again.stdout
    deny = json.loads(first.stdout)["deny"]
    assert deny["kind"] == "git-clean" and deny["member"] and deny["model"]


# --- (5) allow is None and prints nothing -------------------------------------------------------


def test_allow_is_none_and_prints_nothing(tmp_path: Path) -> None:
    assert pretool.judge(_ev("shell", {"command": "ls"})) is None
    done = _fresh(_ev("shell", {"command": "ls"}), {"GLITCH_HOOK_LOG_FILE": str(tmp_path / "l")})
    assert done.returncode == 0 and done.stdout == b"", done.stderr


# --- (6) fail open, loudly --------------------------------------------------------------------------


def _log_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    real = common.log

    def spy(*args: Any) -> None:
        calls.append(args)
        real(*args)

    monkeypatch.setattr(common, "log", spy)
    return calls


@pytest.mark.parametrize("tool", ["shell", "read"])
def test_a_delegate_that_will_not_load_fails_open_with_an_error_log(
    monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    calls = _log_calls(monkeypatch)
    monkeypatch.setattr(codex_shared, "load_hook_module", lambda stem: None)
    with pytest.raises(RuntimeError):
        pretool.decide(_ev(tool, {"command": "git clean -fdx", "path": OUTSIDE}))
    assert pretool.judge(_ev(tool, {"command": "git clean -fdx", "path": OUTSIDE})) == {
        "error": "RuntimeError"
    }
    assert [(c[0], c[1], c[2]) for c in calls] == [("opencode-pre", tool, "ERROR")]
    assert calls[0][4] == "RuntimeError"  # label only: exception text can echo a command
    log_text = Path(shared.HOOK_LOG_FILE).read_text(encoding="utf-8")
    assert "opencode-pre" in log_text and "ERROR" in log_text


def test_a_delegate_that_raises_fails_open_with_an_error_log(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _log_calls(monkeypatch)

    def boom(_inner: dict[str, Any]) -> None:
        raise ValueError("delegate broke")

    monkeypatch.setattr(codex_shared, "load_hook_module",
                        lambda stem: types.SimpleNamespace(decide=boom))
    assert pretool.judge(_ev("write", {"path": "CLAUDE.md", "content": "x"})) == {
        "error": "ValueError"
    }
    assert calls and calls[0][2] == "ERROR" and "ValueError" in calls[0][4]


def test_a_deny_is_logged_as_a_deny(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _log_calls(monkeypatch)
    pretool.judge(_ev("shell", {"command": "git clean -fdx"}))
    assert [(c[0], c[1], c[2], c[4]) for c in calls] == [
        ("opencode-pre", "shell", "DENY", "git-clean")
    ]


# --- (7) unknown or missing tool -------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [{}, {"tool_input": {"command": "git clean -fdx"}, "session_dir": R},
     {"tool_name": "", "tool_input": {"command": "git clean -fdx"}},
     {"tool_name": 7, "tool_input": {"command": "git clean -fdx"}}, "not a dict", None],
)
def test_no_tool_name_is_nothing_to_judge(recorder: _Recorder, event: Any) -> None:
    assert pretool.translate(event) is None
    assert pretool.decide(event) is None
    assert pretool.judge(event) is None
    assert recorder.calls == []


def test_an_unknown_tool_with_a_harmless_shape_allows() -> None:
    assert pretool.decide(_ev("brand_new_tool", {"query": "x"})) is None


def test_an_unknown_tool_with_a_write_shape_is_still_judged() -> None:
    hit = pretool.decide(_ev("brand_new_tool", {"path": "CLAUDE.md", "content": "x"}))
    assert hit is not None and hit[0] == "engine"


# --- (8) a JSON-string tool input is decoded --------------------------------------------------------


def test_a_json_string_tool_input_is_decoded() -> None:
    hit = pretool.decide(_ev("shell", json.dumps({"command": "git clean -fdx"})))
    assert hit is not None and hit[0] == "git-clean"


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]", "42", ""])
def test_an_undecodable_tool_input_is_an_empty_shape(recorder: _Recorder, raw: str) -> None:
    _, inner = _one(recorder, _ev("write", raw))
    assert inner["tool_input"] == {}
