"""Shared pytest setup for the OpenCode bridge suite.

Two jobs:

1. ``sys.path``: the bridge folder (``src/bridge``) and the engine's ``<root>/.claude/scripts``,
   so tests import ``common`` and the engine modules the way the bridge does at runtime.
2. Isolation: no test may read or write this machine's real Glitch state.
   The engine resolves some state paths at IMPORT time from an environment door, so those doors
   are pinned here at module level, BEFORE anything imports the engine, and a subprocess or
   fresh-interpreter test inherits them through the environment.
   The rest are module constants, moved per test by the autouse fixture below.
   Only the doors and names the engine really reads are used (each one is cited):

   - ``GLITCH_READ_GRANT_STATE_DIR``: ``read_grant.py`` line 93, read at import
     (``STATE_DIR`` / ``STATE_FILE``); also patched per test on the module.
   - ``GLITCH_HOOK_LOG_FILE``: ``shared.py`` line 624, read at import (``HOOK_LOG_FILE``).
   - ``GLITCH_WATCH_STATE_DIR``: ``config.py`` line 58 (the watch files the prompt and session
     hooks claim from).
   - ``GLITCH_CODEX_PANE_SEEN_FILE``: ``config.py`` line 61, pinned inside a plain file so a
     child can never write it (the engine suite's own trick).
   - ``config.REFUSED_ENGINE_EDITS_FILE`` (the engine-tier breadcrumb; in-process only, which is
     why subprocess deny tests use only non-engine-tier denies).
   - ``config.STATE_DIR`` (no env door: in-process only) and ``lane_settings.HEADSUP_STATE_FILE``.
   - ``PYTHONDONTWRITEBYTECODE=1`` / ``sys.dont_write_bytecode``: no ``__pycache__`` in the engine.
   - Keeper state: ``config.RUN_LEDGER_FILE`` (re-exported as ``run_ledger.RUN_LEDGER_FILE``),
     ``config.KEEPER_HEALTH_FILE`` (the miss counter ``consolidate.note_miss`` writes, imported by
     value into ``consolidate``), ``config.KEEPER_BRIEF_FILE``, ``config.KEEPER_RUN_RECORD_FILE``.
"""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# No bytecode anywhere: loading an engine hook by path (in-process, or in a child that inherits
# this environment) must never leave a __pycache__ in the Brain's own .claude/hooks or scripts.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

_HERE = Path(__file__).resolve().parent
WORKSPACE = _HERE.parents[1]
BRIDGE_DIR = WORKSPACE / "src" / "bridge"

if str(BRIDGE_DIR) not in sys.path:
    sys.path.insert(0, str(BRIDGE_DIR))

import common  # noqa: E402  (bridge dir must be on the path first)

_root = common.resolve_root(WORKSPACE)
if _root is None:  # pragma: no cover - the suite only runs inside a Glitch folder
    raise RuntimeError(f"no Glitch root above {WORKSPACE}")
GLITCH_ROOT: Path = _root
SCRIPTS_DIR = common.scripts_dir(GLITCH_ROOT)

# --- Import-time environment doors: set BEFORE the engine is imported ---------------------------
# `setdefault`, so an outer caller's explicit override still wins.
_pid = os.getpid()
os.environ.setdefault(
    "GLITCH_HOOK_LOG_FILE",
    str(Path(tempfile.gettempdir()) / f"opencode-bridge-test-hook-log-{_pid}.log"),
)
os.environ.setdefault(
    "GLITCH_READ_GRANT_STATE_DIR",
    tempfile.mkdtemp(prefix="opencode-bridge-test-read-grant-"),
)
os.environ.setdefault(
    "GLITCH_WATCH_STATE_DIR",
    tempfile.mkdtemp(prefix="opencode-bridge-test-watch-"),
)
_codex_seen_blocker = Path(tempfile.mkdtemp(prefix="opencode-bridge-test-codex-seen-")) / "file"
_codex_seen_blocker.write_text("", encoding="utf-8")
os.environ.setdefault("GLITCH_CODEX_PANE_SEEN_FILE", str(_codex_seen_blocker / "seen.json"))

# A suite run from a real Claude Code / Codex pane must not lend the tests that pane's identity.
for _marker in (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CLAUDECODE", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
    "CLAUDE_CONFIG_DIR",
):
    os.environ.pop(_marker, None)

common.engine_on_path(GLITCH_ROOT)

import config  # noqa: E402  (engine import must follow the env pins above)
import shared  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_machine_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Point every piece of machine state a bridge test could touch at a per-test folder.

    The folder is NOT ``tmp_path`` itself (a sibling made by the factory), so a test that lists its
    own ``tmp_path`` sees only what it wrote.
    Yields the state folder, for tests that want to look inside it.
    """
    state = tmp_path_factory.mktemp("glitch-state")

    # Read grants: the env door (for children) and the module constants (already imported).
    grant_dir = state / "read-grant"
    grant_dir.mkdir()
    monkeypatch.setenv("GLITCH_READ_GRANT_STATE_DIR", str(grant_dir))
    import read_grant

    monkeypatch.setattr(read_grant, "STATE_DIR", grant_dir)
    monkeypatch.setattr(read_grant, "STATE_FILE", grant_dir / "read-grant.json")

    # Hook log.
    log_file = state / "hook-execution.log"
    monkeypatch.setenv("GLITCH_HOOK_LOG_FILE", str(log_file))
    monkeypatch.setattr(shared, "HOOK_LOG_FILE", log_file)

    # Engine-tier refusal breadcrumb.
    monkeypatch.setattr(config, "REFUSED_ENGINE_EDITS_FILE", state / "refused-engine-edits.json")

    # Keeper: miss counter, run ledger, brief, run record (config + the modules that copied them).
    monkeypatch.setattr(config, "KEEPER_HEALTH_FILE", state / "keeper-health.json")
    monkeypatch.setattr(config, "RUN_LEDGER_FILE", state / "keeper-run.json")
    monkeypatch.setattr(config, "KEEPER_BRIEF_FILE", state / "keeper-brief.json")
    monkeypatch.setattr(config, "KEEPER_RUN_RECORD_FILE", state / "keeper-run-record.json")
    import consolidate
    import run_ledger

    monkeypatch.setattr(consolidate, "KEEPER_HEALTH_FILE", state / "keeper-health.json")
    monkeypatch.setattr(consolidate, "KEEPER_BRIEF_FILE", state / "keeper-brief.json")
    monkeypatch.setattr(run_ledger, "RUN_LEDGER_FILE", state / "keeper-run.json")

    # Watch files and the Codex pane record (read at call time from config).
    watch_dir = state / "watch"
    watch_dir.mkdir()
    monkeypatch.setattr(config, "WATCH_STATE_FILE", watch_dir / "watch-state.json")
    monkeypatch.setattr(config, "WATCH_CONFIG_FILE", watch_dir / "watch-config.json")
    monkeypatch.setattr(config, "WATCH_CLOCK_FILE", watch_dir / "watch-clock.json")
    monkeypatch.setattr(config, "CODEX_PANE_SEEN_FILE", state / "codex-pane-seen.json")

    # The engine's general state folder (``config.STATE_DIR``, read at call time, e.g. by the
    # post-tool guard's catch diary). It has NO environment door, so a fresh-interpreter test can
    # only use paths that do not write there (a clean post scan, a non-engine-tier deny).
    monkeypatch.setattr(config, "STATE_DIR", state / "engine-state")

    # The open-task heads-up cadence file the session-start context writes (copied from
    # ``config.STATE_DIR`` at import, so moving STATE_DIR alone does not move it).
    import lane_settings

    monkeypatch.setattr(lane_settings, "HEADSUP_STATE_FILE", state / "lane-headsup-state.json")

    # Codex's own home, which the session sweep derives its folder from (a child inherits it).
    monkeypatch.setenv("CODEX_HOME", str(state / "codex-home"))

    yield state
