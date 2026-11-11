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
import re
import sys
import time
from pathlib import Path
from typing import Any

import codex_identity  # (engine path is set by conftest)
import config
import pytest
import read_grant

import capture
import chatcache
import chatsource
import common
import context
from conftest import GLITCH_ROOT, WORKSPACE
from test_capture import Fake, _new_ms

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


@pytest.fixture(autouse=True)
def _no_real_opencode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every session start sweeps: it must never reach the real opencode. An absolute path to
    nothing reads as "not installed"; the capture tests install the fake binary over it."""
    monkeypatch.setenv(chatsource.BIN_ENV, str(tmp_path / "no-opencode-here"))


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


# --- session start: OpenCode's own capture sweep and notice ----------------------------------

CAPTURED_LINE = "_1 OpenCode chat queued for your memory since last time._"
CTX_SID = "ses_ctxCapture001"


@pytest.fixture
def fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fake:
    root = tmp_path / "fake bin"
    root.mkdir()
    return Fake(root, monkeypatch)


def _stamp_ledger() -> None:
    capture.save_ledger({"version": 1, "first_run": config.now_local().isoformat(),
                         "sessions": {}, "refused": {}})


def test_notice_reports_a_chat_the_start_sweep_captured(vault: Path, fake: Fake) -> None:
    _stamp_ledger()
    fake.chat(CTX_SID, 9, _new_ms())
    text = context.session_context({"session_id": "ses_start", "session_dir": "/x"})
    assert CAPTURED_LINE in text
    assert PERSONA_SENTINEL in text
    assert fake.calls("list")  # the sweep ran, before the notice
    # Reported once: the next start, with nothing new, says nothing about capture.
    again = context.session_context({"session_id": "ses_start2"})
    assert "OpenCode chat" not in again
    assert PERSONA_SENTINEL in again


def test_notice_is_silent_when_nothing_was_captured(vault: Path, fake: Fake) -> None:
    _stamp_ledger()
    text = context.session_context({"session_id": "ses_quiet"})
    assert "OpenCode chat" not in text
    assert PERSONA_SENTINEL in text
    assert fake.calls("list")


def test_notice_is_passed_as_a_callable_never_none(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = context.load_codex_session

    def spy_loader() -> Any:
        module = real()
        inner = module.build_context

        def spy(*a: Any, **k: Any) -> Any:
            seen.append(k.get("capture_notice"))
            return inner(*a, **k)

        monkeypatch.setattr(module, "build_context", spy)
        return module

    monkeypatch.setattr(context, "load_codex_session", spy_loader)
    context.session_context({"session_id": "ses_callable"})
    assert len(seen) == 1 and callable(seen[0])


@pytest.mark.parametrize("how", ["parent", "glitch"])
def test_automated_session_gets_no_notice_and_no_sweep(
    vault: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    _stamp_ledger()
    fake.chat(CTX_SID, 9, _new_ms())
    notices: list[int] = []
    monkeypatch.setattr(capture, "capture_notice", lambda: notices.append(1) or "")
    event: dict[str, Any] = {"session_id": "ses_kid"}
    if how == "parent":
        event["parent_id"] = "ses_parent"
    else:
        monkeypatch.setenv("CODEX_INVOKED_BY", "glitch")
    assert context.session_context(event) == ""
    assert fake.calls() == []
    assert notices == []


@pytest.mark.parametrize(
    "extra",
    [{"source": "compact"}, {"source": "resume"}, {"source": "COMPACT"}, {"refresh": True},
     {"refresh": "true"}],
    ids=["compact", "resume", "compact-upper", "refresh", "refresh-str"],
)
def test_refresh_or_compaction_rebuild_skips_the_sweep(
    vault: Path, fake: Fake, extra: dict[str, Any]
) -> None:
    _stamp_ledger()
    fake.chat(CTX_SID, 9, _new_ms())
    text = context.session_context({"session_id": "ses_rebuild", **extra})
    assert PERSONA_SENTINEL in text
    assert fake.calls() == []
    assert CAPTURED_LINE not in text


@pytest.mark.parametrize(
    "extra", [{"source": "startup"}, {"refresh": False}, {"source": ""}],
    ids=["startup", "refresh-false", "empty-source"],
)
def test_a_normal_start_with_the_future_fields_still_sweeps(
    vault: Path, fake: Fake, extra: dict[str, Any]
) -> None:
    _stamp_ledger()
    context.session_context({"session_id": "ses_normal", **extra})
    assert fake.calls("list")


@pytest.mark.parametrize("sub", ["list", "export"])
def test_a_stalled_opencode_still_returns_the_identity(
    vault: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch, sub: str,
    logs: list[tuple[str, str, str, str]],
) -> None:
    _stamp_ledger()
    fake.chat(CTX_SID, 9, _new_ms())
    fake.mode(sub, "sleep")  # the fake sleeps 60 s
    monkeypatch.setattr(context, "SESSION_SWEEP_READ_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(context, "SESSION_SWEEP_BUDGET_SECONDS", 2.0)  # room to START the read
    began = time.monotonic()
    text = context.session_context({"session_id": "ses_stall"})
    elapsed = time.monotonic() - began
    assert PERSONA_SENTINEL in text and USER_SENTINEL in text
    assert CAPTURED_LINE not in text
    assert elapsed < 15, elapsed  # one bounded read (+ the kill grace), never the fake's 60 s
    assert ("opencode-session", "sweep", "ERROR") not in [(lb, t, s) for lb, t, s, _d in logs]


def test_an_unreachable_opencode_still_returns_the_identity(vault: Path) -> None:
    # The autouse fixture points the binary at nothing: "not installed".
    _stamp_ledger()
    text = context.session_context({"session_id": "ses_noopencode"})
    assert PERSONA_SENTINEL in text


def test_a_sweep_that_raises_still_returns_the_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str, str, str]]
) -> None:
    def boom(**_k: Any) -> Any:
        raise RuntimeError("SECRET-SWEEP-DETAIL")

    monkeypatch.setattr(capture, "sweep", boom)
    text = context.session_context({"session_id": "ses_boom"})
    assert PERSONA_SENTINEL in text
    assert (context.SESSION_LABEL, "sweep", "ERROR", "RuntimeError") in logs
    assert all("SECRET" not in d for _l, _t, _s, d in logs)


def test_a_notice_that_will_not_load_still_returns_the_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A None entry makes `import capture` raise inside both guarded calls.
    monkeypatch.setitem(sys.modules, "capture", None)
    text = context.session_context({"session_id": "ses_nocapture"})
    assert PERSONA_SENTINEL in text


def test_the_sweep_runs_tightened_by_arguments_and_touches_no_global(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_before, read_before = capture.LOCK_TIMEOUT, chatsource.TIMEOUT_SECONDS
    during: list[dict[str, Any]] = []

    def spy(**kw: Any) -> capture.SweepResult:
        during.append({**kw, "globals": (capture.LOCK_TIMEOUT, chatsource.TIMEOUT_SECONDS)})
        raise RuntimeError("a fault costs the session nothing")

    monkeypatch.setattr(capture, "sweep", spy)
    text = context.session_context({"session_id": "ses_bounds"})
    assert PERSONA_SENTINEL in text
    assert during == [{
        "budget_seconds": context.SESSION_SWEEP_BUDGET_SECONDS,
        "live_session_id": "ses_bounds",
        "lock_timeout": context.SESSION_SWEEP_LOCK_TIMEOUT_SECONDS,
        "read_timeout": context.SESSION_SWEEP_READ_TIMEOUT_SECONDS,
        "globals": (lock_before, read_before),  # untouched even while the sweep runs
    }]
    assert (capture.LOCK_TIMEOUT, chatsource.TIMEOUT_SECONDS) == (lock_before, read_before)
    assert not hasattr(context, "_session_bounds")


@pytest.mark.parametrize("sid", [None, "", 7])
def test_a_start_with_no_usable_id_sweeps_with_no_live_id(
    vault: Path, monkeypatch: pytest.MonkeyPatch, sid: Any
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(capture, "sweep", lambda **kw: seen.append(kw["live_session_id"]) or
                        capture.SweepResult())
    event: dict[str, Any] = {} if sid is None else {"session_id": sid}
    context.session_context(event)
    assert seen == [None]


# --- D1: the first chat after install, through the real session start -----------------------------

D1_LIVE = "ses_e348b7107ffeGc05JfJWoCx212"


def test_d1_first_start_records_the_live_chat_then_its_turns_stage(
    vault: Path, fake: Fake
) -> None:
    assert not capture.ledger_path().exists()  # fresh install
    start = int((time.time() - 5) * 1000)  # created just before the first-run stamp
    fake.chat("ses_before_install", 9, 1577836800000)
    fake.chat(D1_LIVE, 1, start)
    text = context.build_session({"session_id": D1_LIVE, "session_dir": "/x", "source": "startup"})
    assert PERSONA_SENTINEL in text
    notes = {k: v["note"] for k, v in capture.load_ledger()["sessions"].items()}
    assert notes == {D1_LIVE: capture.NOTE_FIRST_CHAT,
                     "ses_before_install": capture.NOTE_BEFORE_FIRST_SWEEP}
    outcomes = []
    for k in range(1, 6):
        fake.chat(D1_LIVE, 2 * k, start)
        outcomes.append(capture.capture_one(D1_LIVE, str(GLITCH_ROOT), "", "turn"))
    assert outcomes == ["too short", "too short", "staged", "staged", "staged"]
    assert capture.load_ledger()["sessions"][D1_LIVE]["note"] == capture.NOTE_STAGED
    assert (chatcache.state_roots().pending / capture.stub_name(D1_LIVE)).is_file()
    fake.chat("ses_before_install", 30, 1577836800000)
    assert capture.capture_one("ses_before_install", str(GLITCH_ROOT), "", "turn") == (
        capture.OUT_BEFORE)
    # The next start reports the capture once.
    assert CAPTURED_LINE in context.session_context({"session_id": "ses_next"})


@pytest.mark.parametrize("how", ["parent", "glitch"])
def test_d1_an_automated_first_start_never_sweeps_or_stamps(
    vault: Path, fake: Fake, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    fake.chat(D1_LIVE, 1, _new_ms())
    event: dict[str, Any] = {"session_id": D1_LIVE}
    if how == "parent":
        event["parent_id"] = "ses_parent"
    else:
        monkeypatch.setenv("CODEX_INVOKED_BY", "glitch")
    assert context.session_context(event) == ""
    assert fake.calls() == []
    assert not capture.ledger_path().exists()


def test_the_sweep_worst_case_fits_inside_the_plugin_session_timeout() -> None:
    ts = (WORKSPACE / "src" / "plugin" / "bridge-client.ts").read_text(encoding="utf-8")
    match = re.search(r"\bsession:\s*([\d_]+)", ts)
    assert match is not None
    session_ms = int(match.group(1).replace("_", ""))
    worst = (
        context.SESSION_SWEEP_LOCK_TIMEOUT_SECONDS
        + context.SESSION_SWEEP_BUDGET_SECONDS
        + context.SESSION_SWEEP_READ_TIMEOUT_SECONDS
        + chatsource._KILL_GRACE_SECONDS
    )
    # Half the session's time at most: uv, the interpreter and the identity build need the rest.
    assert worst <= session_ms / 1000 / 2, (worst, session_ms)


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


# --- rebuilds: compaction, refresh, resume (engine #330) ---------------------------------------


def _misses() -> int:
    """The REAL keeper miss counter (``consolidate.note_miss`` writes it; conftest moves the file
    to per-test state)."""
    import consolidate

    return int(consolidate.load_health()["misses"])


def _spy_codex(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """Wrap the loaded engine module's ``keeper_directive`` and ``build_context`` (both still run
    for real), recording the ``count_miss`` and the ``source`` each was given."""
    seen: dict[str, list[Any]] = {"count_miss": [], "source": []}
    real = context.load_codex_session

    def spy_loader() -> Any:
        module = real()
        real_directive = module.keeper_directive
        real_build = module.build_context

        def directive_spy(*a: Any, **k: Any) -> Any:
            seen["count_miss"].append(k.get("count_miss", "absent"))
            return real_directive(*a, **k)

        def build_spy(event: dict[str, Any], *a: Any, **k: Any) -> Any:
            seen["source"].append(event.get("source"))
            return real_build(event, *a, **k)

        monkeypatch.setattr(module, "keeper_directive", directive_spy)
        monkeypatch.setattr(module, "build_context", build_spy)
        return module

    monkeypatch.setattr(context, "load_codex_session", spy_loader)
    return seen


@pytest.mark.parametrize(
    "extra",
    [{"source": "compact"}, {"source": "COMPACT"}, {"refresh": True}, {"refresh": "true"},
     {"source": "resume", "refresh": True}],
    ids=["compact", "compact-upper", "refresh", "refresh-str", "resume-refresh"],
)
def test_a_compact_or_refresh_rebuild_issues_the_directive_but_never_counts_a_miss(
    vault: Path, keeper_pending: None, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    seen = _spy_codex(monkeypatch)
    before = _misses()
    text = context.session_context({"session_id": "ses_rebuild_keeper", **extra})
    assert "## Memory\n" in text and "`subagent`" in text  # issued
    assert _misses() == before  # the real counter did not move
    assert seen["count_miss"] == [False]


@pytest.mark.parametrize(
    "extra",
    [{}, {"source": "startup"}, {"source": "resume"}, {"source": "garbage"}, {"source": 7},
     {"refresh": False}, {"refresh": 1}, {"refresh": "yes"}, {"refresh": []}, {"refresh": None}],
    ids=["plain", "startup", "resume", "unknown-source", "source-not-a-string", "refresh-false",
         "refresh-int", "refresh-yes", "refresh-list", "refresh-null"],
)
def test_a_start_or_resume_counts_the_miss_as_before(
    vault: Path, keeper_pending: None, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> None:
    seen = _spy_codex(monkeypatch)
    before = _misses()
    text = context.session_context({"session_id": "ses_start_keeper", **extra})
    assert "## Memory\n" in text and PERSONA_SENTINEL in text
    assert _misses() == before + 1  # the real counter moved by exactly one
    assert seen["count_miss"] == [True]


def test_the_real_counter_is_live_across_a_start_then_a_compaction(
    vault: Path, keeper_pending: None
) -> None:
    """No spy: a real start counts, the real compaction after it does not."""
    before = _misses()
    context.session_context({"session_id": "ses_live"})
    assert _misses() == before + 1
    context.session_context({"session_id": "ses_live", "source": "compact"})
    context.session_context({"session_id": "ses_live", "refresh": True})
    assert _misses() == before + 1


@pytest.mark.parametrize(
    ("extra", "expected"),
    [({}, "startup"), ({"source": "startup"}, "startup"), ({"source": "resume"}, "resume"),
     ({"source": "compact"}, "compact"), ({"source": " Compact "}, "compact"),
     ({"refresh": True}, "startup"), ({"source": "compact", "refresh": True}, "compact"),
     ({"source": "garbage"}, "startup"), ({"source": 7}, "startup"),
     ({"source": "clear"}, "startup")],
    ids=["absent", "startup", "resume", "compact", "compact-padded", "refresh",
         "compact-refresh", "unknown", "not-a-string", "clear-not-ours"],
)
def test_the_source_reaches_build_context(
    vault: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any], expected: str
) -> None:
    seen = _spy_codex(monkeypatch)
    text = context.session_context({"session_id": "ses_source", **extra})
    assert PERSONA_SENTINEL in text
    assert seen["source"] == [expected]


@pytest.mark.parametrize(
    "extra", [{"refresh": 1}, {"refresh": "yes"}, {"refresh": []}, {"refresh": {"a": 1}},
              {"source": "garbage"}],
    ids=["refresh-int", "refresh-yes", "refresh-list", "refresh-dict", "unknown-source"],
)
def test_bad_values_are_a_normal_start_that_sweeps(
    vault: Path, fake: Fake, extra: dict[str, Any]
) -> None:
    _stamp_ledger()
    text = context.session_context({"session_id": "ses_badfields", **extra})
    assert PERSONA_SENTINEL in text
    assert fake.calls("list")


@pytest.mark.parametrize("extra", [{"source": "compact"}, {"refresh": True}],
                         ids=["compact", "refresh"])
def test_a_sub_agent_rebuild_still_gets_nothing(
    vault: Path, keeper_pending: None, fake: Fake, extra: dict[str, Any]
) -> None:
    before = _misses()
    assert context.session_context(
        {"session_id": "ses_kid", "parent_id": "ses_parent", **extra}) == ""
    assert _misses() == before
    assert fake.calls() == []


# --- the Claude Code hand-off block ------------------------------------------------------------

HANDOFF_FORBIDDEN_WORDS = ("engine", "bridge", "spine", "hook", "hooks", "plugin", "plugins")
HANDOFF_FALSE_CLAIMS = (
    "already saved", "already in your memory", "is in your memory", "are in your memory",
    "has been saved", "have been saved", "was saved", "is saved", "already captured",
)


@pytest.mark.parametrize(
    "extra", [{}, {"source": "startup"}, {"source": "resume"}],
    ids=["plain", "startup", "resume"],
)
def test_handoff_appears_once_on_start_and_resume(vault: Path, extra: dict[str, Any]) -> None:
    text = context.session_context({"session_id": "ses_handoff", **extra})
    block = context.handoff_block()
    assert text.count(block) == 1
    assert text.count(context.HANDOFF_HEADING) == 1
    assert text.endswith(block)  # after the engine context and the capture notice
    assert text.index(IDENTITY_HEAD) < text.index(block)


def test_handoff_follows_the_capture_notice(vault: Path, fake: Fake) -> None:
    _stamp_ledger()
    fake.chat(CTX_SID, 9, _new_ms())
    text = context.session_context({"session_id": "ses_handoff_notice"})
    assert text.index(CAPTURED_LINE) < text.index(context.HANDOFF_HEADING)


@pytest.mark.parametrize(
    "extra",
    [{"source": "compact"}, {"source": "COMPACT"}, {"refresh": True}, {"refresh": "true"},
     {"source": "resume", "refresh": True}],
    ids=["compact", "compact-upper", "refresh", "refresh-str", "resume-refresh"],
)
def test_handoff_absent_on_compact_and_refresh(vault: Path, extra: dict[str, Any]) -> None:
    text = context.session_context({"session_id": "ses_handoff_rebuild", **extra})
    assert PERSONA_SENTINEL in text  # the rebuild still carries identity
    assert context.HANDOFF_HEADING not in text
    assert context.handoff_sentence() not in text


@pytest.mark.parametrize("how", ["parent", "glitch"])
def test_handoff_absent_for_automated_sessions(
    vault: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    event: dict[str, Any] = {"session_id": "ses_handoff_kid"}
    if how == "parent":
        event["parent_id"] = "ses_parent"
    else:
        monkeypatch.setenv("CODEX_INVOKED_BY", "glitch")
    assert context.session_context(event) == ""


def test_handoff_sentence_has_no_internal_words() -> None:
    words = set(re.findall(r"[a-z]+", context.handoff_sentence().lower()))
    assert not words & set(HANDOFF_FORBIDDEN_WORDS), words & set(HANDOFF_FORBIDDEN_WORDS)


def test_handoff_never_claims_a_chat_is_already_in_memory() -> None:
    lowered = context.handoff_block().lower()
    for phrase in HANDOFF_FALSE_CLAIMS:
        assert phrase not in lowered, phrase
    assert "saved to your memory as you go" in lowered


def test_handoff_names_every_claude_code_only_extra_and_the_way_in() -> None:
    sentence = context.handoff_sentence()
    for extra in ("task and its project window", "mail count on the status line",
                  "open task back up after a clear", "daily brief",
                  "your profile being refreshed at the end of a session"):
        assert extra in sentence, extra
    assert "persona" not in sentence.lower() and "sync" not in sentence.lower()
    assert "`claude`" in sentence and "Glitch folder" in sentence
    assert context.HANDOFF_INTRO.startswith("If you haven't already, tell the member once")
    assert "repeat" not in context.handoff_block().lower()


def test_handoff_fits_its_budget(vault: Path) -> None:
    with_block = context.session_context({"session_id": "ses_budget"})
    without = context.session_context({"session_id": "ses_budget", "refresh": True})
    assert PERSONA_SENTINEL in without and context.HANDOFF_HEADING not in without
    assert len(context.handoff_block()) + 2 <= context.HANDOFF_BUDGET_CHARS
    # The real delta (the rest of the text is the same build, minus the refresh-only differences).
    assert with_block.endswith(context.handoff_block())
    delta = len(with_block) - len(with_block[: -len(context.handoff_block())].rstrip())
    assert delta <= context.HANDOFF_BUDGET_CHARS


def test_handoff_count_tracks_the_capture_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drift note: the promise's number IS capture's floor, formatted in, never hard-coded."""
    assert capture.MIN_TURNS_TO_STAGE == 5
    assert "chats of five or more messages" in context.handoff_sentence()
    assert "{count}" in context.HANDOFF_PROMISE and "five" not in context.HANDOFF_PROMISE
    monkeypatch.setattr(capture, "MIN_TURNS_TO_STAGE", 7)
    assert "chats of seven or more messages" in context.handoff_sentence()
    monkeypatch.setattr(capture, "MIN_TURNS_TO_STAGE", 12)
    assert "chats of 12 or more messages" in context.handoff_sentence()


def test_handoff_that_will_not_build_still_returns_the_identity(
    vault: Path, monkeypatch: pytest.MonkeyPatch, logs: list[tuple[str, str, str, str]]
) -> None:
    def boom() -> str:
        raise RuntimeError("no")

    monkeypatch.setattr(context, "handoff_block", boom)
    text = context.session_context({"session_id": "ses_handoff_boom"})
    assert PERSONA_SENTINEL in text
    assert (context.SESSION_LABEL, "handoff", "ERROR", "RuntimeError") in logs


def test_session_date_is_the_london_date_near_midnight(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    # 23:30 UTC on 1 July is 00:30 BST on 2 July.
    summer = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
    monkeypatch.setattr(config, "now_local", lambda: summer.astimezone(london))
    assert context.session_date() == "2026-07-02"
    assert summer.date().isoformat() == "2026-07-01"  # the UTC day differs: the probe is live
    # 23:59 GMT on 31 December is the same day in London and UTC.
    winter = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    monkeypatch.setattr(config, "now_local", lambda: winter.astimezone(london))
    assert context.session_date() == "2026-12-31"


def test_session_date_matches_glitch_clock() -> None:
    assert context.session_date() == config.now_local().date().isoformat()
