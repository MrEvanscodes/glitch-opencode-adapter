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
   - OpenCode capture state (fixture ``_isolate_opencode_capture``): ``GLITCH_OPENCODE_CAPTURE_STATE``
     is set per test to a fresh folder under the system temp folder (the bridge's
     ``chatcache.state_roots()`` reads it at call time and answers ``<it>/pending``, ``<it>/state``,
     ``<it>/data``; a child inherits it), and ``config.PENDING_DIR`` / ``config.DATA_DIR`` are
     moved to ``<it>/pending`` / ``<it>/data`` for the in-process (env unset) path.
     ``config.STATE_DIR`` stays at the per-test ``<glitch-state>/engine-state`` set above (an
     existing test pins that folder), so with the env unset the ledger lands there: still temp.
     Any by-value copies named ``PENDING_DIR`` / ``STATE_DIR`` / ``DATA_DIR`` on an already-imported
     ``capture`` or ``chatcache`` module are moved to the ``<it>`` sub folders.
   - OpenCode's own home (fixture ``_isolate_opencode_home``): ``XDG_DATA_HOME``,
     ``XDG_STATE_HOME``, ``XDG_CACHE_HOME``, ``XDG_CONFIG_HOME`` and ``OPENCODE_CONFIG_DIR`` point
     at per-test temp folders and ``OPENCODE_DISABLE_AUTOUPDATE=1``, so no test, sweep or child
     ``opencode`` reads the member's real OpenCode store (like ``CODEX_HOME`` above).

3. The real-state guard (fixture ``_real_capture_state_unchanged``, autouse, set up before every
   isolation door and checked after they are undone): it fingerprints the REAL pending queue, the
   REAL ledger ``opencode-staged.json`` and the REAL ``opencode-chats`` folder (paths taken from
   ``config`` at conftest import, before any patching) and fails the test, naming which one moved,
   if any of them changed.
   A live Glitch session staging a flush into the real queue during the same second would also
   trip it; rerun, and if it repeats it is the test.
"""

from __future__ import annotations

import hashlib
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

import common  # (bridge dir must be on the path first)

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

import config  # (engine import must follow the env pins above)
import shared

# --- The REAL capture state, captured BEFORE any fixture patches config ---------------------------
CAPTURE_STATE_ENV = "GLITCH_OPENCODE_CAPTURE_STATE"
REAL_PENDING_DIR = Path(config.PENDING_DIR)
REAL_LEDGER_FILE = Path(config.STATE_DIR) / "opencode-staged.json"
REAL_CHATS_DIR = Path(config.DATA_DIR) / "opencode-chats"
_BIG_FILE_BYTES = 5 * 1024 * 1024
_SYS_TEMP = Path(os.path.realpath(tempfile.gettempdir()))


def fingerprint(target: Path) -> str:
    """Return a sha256 fingerprint of a file or folder tree, or ``"absent"``.

    Hashes each file's relative path and bytes; a file over 5 MB is hashed by size and mtime
    instead, so the guard stays cheap on a large queue.
    """
    if not target.exists():
        return "absent"
    digest = hashlib.sha256()
    files = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for path in files:
        rel = "." if path == target else path.relative_to(target).as_posix()
        digest.update(rel.encode("utf-8") + b"\0")
        try:
            stat = path.stat()
            if stat.st_size > _BIG_FILE_BYTES:
                digest.update(f"big:{stat.st_size}:{stat.st_mtime_ns}".encode())
            else:
                digest.update(path.read_bytes())
        except OSError as exc:  # vanished mid-walk: still a difference worth seeing
            digest.update(f"unreadable:{type(exc).__name__}".encode())
        digest.update(b"\0")
    return digest.hexdigest()


def _real_capture_targets() -> dict[str, Path]:
    return {
        "real pending queue": REAL_PENDING_DIR,
        "real ledger opencode-staged.json": REAL_LEDGER_FILE,
        "real opencode-chats folder": REAL_CHATS_DIR,
    }


@pytest.fixture(autouse=True)
def _real_capture_state_unchanged() -> Iterator[None]:
    """Fail any test that changed the real pending queue, ledger or chat copies."""
    targets = _real_capture_targets()
    before = {name: fingerprint(path) for name, path in targets.items()}
    yield
    changed = [
        f"{name} ({path})"
        for name, path in targets.items()
        if fingerprint(path) != before[name]
    ]
    if changed:
        pytest.fail(
            "this test changed the member's REAL OpenCode capture state: "
            + "; ".join(changed)
            + ". Route the write through chatcache.state_roots() / the patched config dirs.",
            pytrace=False,
        )


def _under_sys_temp(path: Path) -> bool:
    real = Path(os.path.realpath(path))
    return real == _SYS_TEMP or _SYS_TEMP in real.parents


@pytest.fixture(autouse=True)
def _isolate_opencode_capture(
    _real_capture_state_unchanged: None,
    _isolate_machine_state: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Path]:
    """Point the capture code's three folders (pending, state, data) at a per-test temp folder.

    Requests ``_isolate_machine_state`` so its ``config.STATE_DIR`` move is overridden here, and
    the real-state guard so that guard checks only after every patch is undone.
    Yields the capture root; its ``pending`` / ``state`` / ``data`` sub folders exist.
    """
    root = Path(os.path.realpath(tmp_path_factory.mktemp("opencode-capture")))
    if not _under_sys_temp(root):  # e.g. --basetemp outside the temp folder: the door would refuse
        root = Path(os.path.realpath(tempfile.mkdtemp(prefix="opencode-bridge-test-capture-")))
    assert _under_sys_temp(root), f"capture test root {root} is not under {_SYS_TEMP}"
    dirs = {"PENDING_DIR": root / "pending", "STATE_DIR": root / "state", "DATA_DIR": root / "data"}
    for folder in dirs.values():
        folder.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv(CAPTURE_STATE_ENV, str(root))
    # config.STATE_DIR keeps the per-test ``<glitch-state>/engine-state`` that
    # ``_isolate_machine_state`` set (test_posttool asserts that exact folder); PENDING_DIR and
    # DATA_DIR, copied by value at engine import, are moved here.
    assert _under_sys_temp(Path(config.STATE_DIR)), f"config.STATE_DIR {config.STATE_DIR} not moved"
    monkeypatch.setattr(config, "PENDING_DIR", dirs["PENDING_DIR"])
    monkeypatch.setattr(config, "DATA_DIR", dirs["DATA_DIR"])
    # By-value copies on the capture modules, only if a test already imported them.
    for mod_name in ("capture", "chatcache"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        for name, folder in dirs.items():
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, folder)
    yield root


@pytest.fixture(autouse=True)
def _isolate_opencode_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Path]:
    """Point OpenCode's config/data/state/cache homes at a per-test temp folder."""
    home = tmp_path_factory.mktemp("opencode-home")
    for env, sub in (
        ("XDG_DATA_HOME", "xdg-data"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("OPENCODE_CONFIG_DIR", "opencode-config"),
    ):
        (home / sub).mkdir()
        monkeypatch.setenv(env, str(home / sub))
    monkeypatch.setenv("OPENCODE_DISABLE_AUTOUPDATE", "1")
    yield home


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
