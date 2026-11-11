"""Tests for ``context.py``: session-start memory-in and the per-prompt delegate.

Isolation: ``conftest.py`` points read grants, the hook log, keeper state and watch state at a
per-test folder (env doors for the child process, module constants in-process).
(conftest also moves ``config.STATE_DIR`` and the open-task heads-up cadence file, the one
session-start getter that writes.)
This file swaps the identity vault for a fixture vault:
``config.MEMORY_DIR`` (read at call time by ``codex_identity`` and at load time by the Claude
session hook) and ``persona.SOUL_FILE`` (the module constant ``extract_persona`` reads).
Each test also checks that the real read-grant file is byte-for-byte unchanged.

The prompt tests run the REAL ``user-prompt-context-codex.py`` in a child process, exactly as the
bridge does; the pause the guard would record is written with ``read_grant.record_pause``, the
same writer the read guard calls.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import common
import context

import codex_identity  # noqa: E402  (engine path is set by conftest)
import config  # noqa: E402
import read_grant  # noqa: E402

_REAL_GRANT_FILE = read_grant.CLAUDE_DIR / "data" / "state" / "read-grant.json"

PERSONA_SENTINEL = "FIXTURE-PERSONA-VOICE-5531"
EXCLUDED_SENTINEL = "FIXTURE-EXCLUDED-HOW-IT-WORKS-5531"
USER_SENTINEL = "FIXTURE-USER-OBJECTIVE-5531"
MEMORY_SENTINEL = "FIXTURE-MEMORY-FACT-5531"
SCRUBBED_SENTINEL = "FIXTURE-HOME-ADDRESS-5531"
IDENTITY_HEAD = "## Who you are working with"


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _real_grant_untouched() -> Any:
    """Every test leaves the machine's real read-grant file exactly as it found it."""
    before = _digest(_REAL_GRANT_FILE)
    yield
    assert _digest(_REAL_GRANT_FILE) == before


@pytest.fixture(autouse=True)
def _isolate_headsup(monkeypatch: pytest.MonkeyPatch) -> None:
    # The heads-up cadence file itself is moved by conftest; these are this file's own env pins.
    monkeypatch.delenv("CODEX_INVOKED_BY", raising=False)
    monkeypatch.delenv(context.NO_KEEPER_ENV, raising=False)


@pytest.fixture
def vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fixture identity vault: SOUL.md (PERSONA block + an excluded section), USER.md, MEMORY.md."""
    import persona

    mem = tmp_path / "Memory"
    mem.mkdir()
    (mem / "SOUL.md").write_text(
        "# SOUL\n\n"
        f"{config.IDENTITY_PERSONA_MARKER_START}\n"
        f"I speak plainly. {PERSONA_SENTINEL}\n"
        f"{config.IDENTITY_PERSONA_MARKER_END}\n\n"
        "## How This Works\n"
        f"{EXCLUDED_SENTINEL}\n",
        encoding="utf-8",
    )
    (mem / "USER.md").write_text(
        "# USER\n\n"
        f"- Objective: {USER_SENTINEL}\n"
        f"- Home: {SCRUBBED_SENTINEL} (scrubbed from any shared replica)\n",
        encoding="utf-8",
    )
    (mem / "MEMORY.md").write_text(
        f"# MEMORY\n\n## Important Facts\n\n- {MEMORY_SENTINEL}\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "MEMORY_DIR", mem)
    monkeypatch.setattr(persona, "SOUL_FILE", mem / "SOUL.md")
    return mem


@pytest.fixture
def keeper_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Memory work is pending and no keeper ran recently (state the directive reads)."""
    import consolidate
    import run_ledger

    monkeypatch.setattr(
        consolidate, "detect",
        lambda: {"digest_pending": 1, "digest_claimed": 0, "dream_due": False, "errors": []},
    )
    monkeypatch.setattr(run_ledger, "run_open_recently", lambda *_a, **_k: False)


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, str]]:
    """Every ``common.log`` call, as (label, tool, status, detail)."""
    seen: list[tuple[str, str, str, str]] = []

    def record(label: str, tool: str, status: str, started: float, detail: str = "") -> None:
        seen.append((label, tool, status, detail))

    monkeypatch.setattr(common, "log", record)
    return seen


# --- session start ----------------------------------------------------------------------------


def test_session_injects_persona_block_user_and_memory(vault: Path) -> None:
    text = context.session_context({"session_id": "ses_fixture", "session_dir": "/x"})
    assert IDENTITY_HEAD in text
    # The PERSONA block of SOUL.md, not the whole file: the pinned exclusion stays out.
    assert PERSONA_SENTINEL in text
    assert EXCLUDED_SENTINEL not in text
    assert USER_SENTINEL in text
    assert MEMORY_SENTINEL in text
    # Lines the vault tags as scrubbed never reach a second provider.
    assert SCRUBBED_SENTINEL not in text


def test_keeper_directive_leads_and_names_opencode_subagent(vault: Path, keeper_pending: None) -> None:
    text = context.session_context({"session_id": "ses_keeper"})
    assert "## Memory\n" in text
    assert text.index("## Memory\n") < text.index(IDENTITY_HEAD)
    head = text[: text.index(IDENTITY_HEAD)]
    assert "`subagent`" in head
    assert '"agent": "general"' in head
    assert '"background": true' in head
    assert "Execute the memory-keeper procedure in .claude/skills/memory-keeper/SKILL.md." in head
    assert "`/keeper`" in head
    for foreign in ("multi_agent_v1", "spawn_agent", "Task tool", "run_in_background"):
        assert foreign not in text


def test_keeper_wording_constant_is_format_safe() -> None:
    rendered = context.OPENCODE_KEEPER_DIRECTIVE.format(banner="")
    args = rendered[rendered.index("`{") + 1 : rendered.index("}`") + 1]
    assert json.loads(args) == {
        "agent": "general",
        "description": "Catch up memory",
        "prompt": "Execute the memory-keeper procedure in .claude/skills/memory-keeper/SKILL.md.",
        "background": True,
    }
    assert 3 <= len(json.loads(args)["description"].split()) <= 5


def test_codex_capture_sweep_is_never_called(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(codex_identity, "capture_notice", lambda: calls.append("swept") or "")
    context.session_context({"session_id": "ses_nosweep"})
    assert calls == []
    # The probe is live: the same engine call with capture_notice=None DOES sweep.
    codex_session = context.load_codex_session()
    import codex_shared

    codex_session.build_context(
        {"session_id": "ses_twin", "source": "startup"},
        codex_shared.load_hook_module("session-start-context"),
        directive=lambda: "",
    )
    assert calls == ["swept"]


def test_no_keeper_env_empties_directive_and_logs_loudly(
    vault: Path, keeper_pending: None, monkeypatch: pytest.MonkeyPatch,
    logs: list[tuple[str, str, str, str]],
) -> None:
    monkeypatch.setenv(context.NO_KEEPER_ENV, "1")
    text = context.session_context({"session_id": "ses_nokeeper"})
    assert PERSONA_SENTINEL in text
    assert "memory-keeper" not in text
    assert any(s == "WARN" and context.NO_KEEPER_ENV in d for _l, _t, s, d in logs)


@pytest.mark.parametrize("parent", ["ses_parent_1"])
def test_subagent_session_gets_nothing(
    vault: Path, keeper_pending: None, parent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import consolidate

    misses: list[int] = []
    monkeypatch.setattr(consolidate, "note_miss", lambda: misses.append(1) or 1)
    assert context.session_context({"session_id": "ses_child", "parent_id": parent}) == ""
    assert misses == []  # the keeper miss counter is not touched by a sub-agent


def test_empty_parent_id_is_a_primary_session(vault: Path) -> None:
    assert PERSONA_SENTINEL in context.session_context({"session_id": "s", "parent_id": ""})


def test_invoked_by_glitch_session_gets_nothing(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_INVOKED_BY", "glitch")
    assert context.session_context({"session_id": "ses_auto"}) == ""


def test_session_cold_start_twin(vault: Path) -> None:
    """First-ever start (no keeper state, no grant state) and a re-run both carry identity."""
    first = context.session_context({"session_id": "ses_cold"})
    second = context.session_context({"session_id": "ses_cold"})
    for text in (first, second):
        assert PERSONA_SENTINEL in text and USER_SENTINEL in text


def test_session_fails_open_when_claude_hook_unavailable(
    vault: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str, str, str]]
) -> None:
    import codex_shared

    monkeypatch.setattr(codex_shared, "load_hook_module", lambda _stem: None)
    assert context.session_context({"session_id": "ses_broken"}) == ""
    assert (context.SESSION_LABEL, "startup", "ERROR", "DelegateUnavailable") in logs
    with pytest.raises(context.DelegateUnavailable):
        context.build_session({"session_id": "ses_broken"})


def test_session_fails_open_when_codex_session_will_not_load(
    vault: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    logs: list[tuple[str, str, str, str]],
) -> None:
    monkeypatch.setattr(context, "hooks_dir", lambda: tmp_path / "no-hooks")
    assert context.session_context({"session_id": "ses_broken2"}) == ""
    assert any(s == "ERROR" for _l, _t, s, _d in logs)


# --- every prompt -----------------------------------------------------------------------------


def _prompt(session: str, text: str) -> str:
    return context.prompt_context({"session_id": session, "prompt": text, "session_dir": "/x"})


def test_always_allow_phrase_flips_the_isolated_grant() -> None:
    assert read_grant.STATE_FILE != _REAL_GRANT_FILE
    assert not read_grant.allows_any(read_grant.BASH_KINDS)
    out = _prompt("ses_grant", "always allow commands outside Glitch")
    assert out.startswith("Setting saved: shell commands")
    stored = read_grant.stored()
    assert all(stored.get(k) is True for k in read_grant.BASH_KINDS)
    # And the words turn it back off, through the same child.
    back = _prompt("ses_grant", "stop allowing commands outside Glitch")
    assert "ask again" in back
    assert all(read_grant.stored().get(k) is False for k in read_grant.BASH_KINDS)


def test_phrase_inside_a_longer_sentence_grants_nothing() -> None:
    out = _prompt("ses_long", "please always allow commands outside Glitch for this build")
    assert out == ""
    assert not read_grant.allows_any(read_grant.BASH_KINDS)


def test_prompt_cold_start_twin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No read-grant state folder at all (first ever message) vs the steady state."""
    cold = tmp_path / "never-made"
    monkeypatch.setenv("GLITCH_READ_GRANT_STATE_DIR", str(cold))
    monkeypatch.setattr(read_grant, "STATE_DIR", cold)
    monkeypatch.setattr(read_grant, "STATE_FILE", cold / "read-grant.json")
    assert not cold.exists()
    first = _prompt("ses_cold", "always allow commands outside Glitch")
    assert first.startswith("Setting saved")
    assert read_grant.member_turn("ses_cold") == 1
    second = _prompt("ses_cold", "always allow commands outside Glitch")
    assert second.startswith("Setting saved")
    assert read_grant.member_turn("ses_cold") == 2


def test_every_member_message_stamps_the_turn_even_with_no_output() -> None:
    for n in (1, 2, 3):
        assert _prompt("ses_stamp", f"just an ordinary message {n}") == ""
        assert read_grant.member_turn("ses_stamp") == n


def test_subagent_prompt_is_not_stamped_and_gets_nothing() -> None:
    out = context.prompt_context(
        {"session_id": "ses_kid", "prompt": "always allow commands outside Glitch",
         "parent_id": "ses_parent"}
    )
    assert out == ""
    assert read_grant.member_turn("ses_kid") == 0
    assert not read_grant.allows_any(read_grant.BASH_KINDS)


def test_invoked_by_glitch_prompt_gets_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_INVOKED_BY", "glitch")
    assert _prompt("ses_auto", "always allow commands outside Glitch") == ""
    assert read_grant.member_turn("ses_auto") == 0


# The read-pause status x operation grid, the cells the engine lets us drive.

PAUSED = "/etc/opencode-adapter-test-paused.txt"
OTHER = "/opt/opencode-adapter-test-other.txt"


def _pause_on_turn_one(session: str, path: str = PAUSED) -> None:
    assert _prompt(session, "read that file for me") == ""  # turn 1 (the member's ask)
    read_grant.record_pause(session, path)  # what the read guard writes on its deny


def test_yes_redeems_one_pass_and_a_spent_pass_does_not_allow_a_second_read() -> None:
    sid = "ses_yes"
    _pause_on_turn_one(sid)
    out = _prompt(sid, "yes")
    assert "one-time pass" in out and PAUSED in out
    assert read_grant.consume_pass(sid, PAUSED) is True
    assert read_grant.consume_pass(sid, PAUSED) is False  # spent: the same file again
    assert read_grant.consume_pass(sid, OTHER) is False  # a different file never had one


def test_no_declines_and_leaves_nothing_to_redeem() -> None:
    sid = "ses_no"
    _pause_on_turn_one(sid)
    out = _prompt(sid, "no")
    assert out.startswith("The member said no.")
    assert read_grant.pending_paths(sid) == []
    ok, _msg = read_grant.allow_once(PAUSED, session=sid)
    assert ok is False
    assert read_grant.consume_pass(sid, PAUSED) is False


def test_yes_on_the_wrong_turn_grants_nothing() -> None:
    sid = "ses_late"
    _pause_on_turn_one(sid)
    assert _prompt(sid, "something else entirely") == ""  # turn 2: not an answer
    assert _prompt(sid, "yes") == ""  # turn 3: too late to answer turn 1's question
    assert read_grant.consume_pass(sid, PAUSED) is False


def test_yes_in_the_same_turn_as_the_pause_grants_nothing() -> None:
    sid = "ses_same"
    _pause_on_turn_one(sid)
    ok, _msg = read_grant.allow_once(PAUSED, session=sid)  # no member reply yet
    assert ok is False
    assert read_grant.consume_pass(sid, PAUSED) is False


def test_yes_inside_a_longer_message_grants_nothing() -> None:
    sid = "ses_buried"
    _pause_on_turn_one(sid)
    assert _prompt(sid, "yes but first tell me what it contains") == ""
    assert read_grant.consume_pass(sid, PAUSED) is False


def test_yes_in_another_session_grants_nothing() -> None:
    _pause_on_turn_one("ses_a")
    assert _prompt("ses_b", "yes") == ""
    assert read_grant.consume_pass("ses_a", PAUSED) is False
    assert read_grant.consume_pass("ses_b", PAUSED) is False


def test_no_pause_yes_is_not_an_answer() -> None:
    assert _prompt("ses_nopause", "yes") == ""
    assert read_grant.consume_pass("ses_nopause", PAUSED) is False


# Fail open.


def _fake_hook(tmp_path: Path, body: str) -> Path:
    hooks = tmp_path / "hooks"
    hooks.mkdir(exist_ok=True)
    (hooks / "user-prompt-context-codex.py").write_text(body, encoding="utf-8")
    return hooks


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ("import sys; sys.exit(3)\n", "prompt hook exit 3"),
        ("print('not json')\n", "prompt hook printed non-JSON"),
        ("print('[1]')\n", "prompt hook printed non-object"),
        ("print('{}')\n", "prompt hook output missing hookSpecificOutput"),
        ("import time; time.sleep(5)\n", "prompt hook timed out"),
    ],
)
def test_prompt_fails_open_with_error_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str, str, str]],
    body: str, detail: str,
) -> None:
    hooks = _fake_hook(tmp_path, body)
    monkeypatch.setattr(context, "hooks_dir", lambda: hooks)
    monkeypatch.setattr(context, "PROMPT_TIMEOUT_SECONDS", 0.5)
    assert _prompt("ses_fail", "SECRET-PAYLOAD-MUST-NOT-BE-LOGGED") == ""
    assert (context.PROMPT_LABEL, "prompt", "ERROR", detail) in logs
    assert all("SECRET-PAYLOAD" not in d for _l, _t, _s, d in logs)
    with pytest.raises(context.DelegateUnavailable):
        context.build_prompt({"session_id": "ses_fail", "prompt": "x"})


def test_prompt_fails_open_when_hook_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str, str, str]]
) -> None:
    monkeypatch.setattr(context, "hooks_dir", lambda: tmp_path / "empty")
    assert _prompt("ses_missing", "hello") == ""
    assert (
        context.PROMPT_LABEL, "prompt", "ERROR", "user-prompt-context-codex unavailable"
    ) in logs


def test_prompt_child_uses_this_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = (
        "import json, sys\n"
        "event = json.loads(sys.stdin.read())\n"
        "print(json.dumps({'hookSpecificOutput': {'additionalContext': "
        "sys.executable + '|' + ','.join(sorted(event))}}))\n"
    )
    monkeypatch.setattr(context, "hooks_dir", lambda: _fake_hook(tmp_path, body))
    out = _prompt("ses_exe", "hi")
    exe, keys = out.split("|")
    assert Path(exe).resolve() == Path(sys.executable).resolve()
    assert keys == "prompt,session_id"  # only the two keys the hook reads; no host markers
