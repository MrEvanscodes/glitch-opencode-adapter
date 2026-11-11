"""Tests for scripts/install.py: every case runs in tmp_path through --config-dir.

No test touches ~/.config/opencode: each call passes an explicit temp config dir, and the
autouse guard below fails the test if the real config dir changes while it runs.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install.py"
_spec = importlib.util.spec_from_file_location("glitch_opencode_install", SCRIPT)
assert _spec and _spec.loader
inst = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = inst
_spec.loader.exec_module(inst)

PLUGIN = inst.PLUGIN_DIR
LIT = inst.encode_path(str(PLUGIN))
REAL_CONFIG = Path.home() / ".config" / "opencode"


def _snapshot(d: Path) -> dict[str, bytes]:
    if not d.is_dir():
        return {}
    return {str(p.relative_to(d)): p.read_bytes() for p in d.rglob("*") if p.is_file()}


# Only these names in the REAL config folder are ever read for content; everything else there
# (auth, tokens, credentials, databases, the service file, backups) is fingerprinted by stat only.
_REAL_CONTENT_NAMES = frozenset({*inst.CONFIG_NAMES, "config.json"})


def real_fingerprint(d: Path) -> dict[str, tuple[str, object]]:
    """The real config tree: sha256 of top-level config files, (size, mtime_ns) for every other
    entry, and the entry kind, so an added, removed or rewritten entry anywhere shows. Never reads
    a file whose name is not a config file name."""
    import hashlib

    if not d.exists():
        return {}
    out: dict[str, tuple[str, object]] = {".": ("dir", d.stat().st_mtime_ns)}
    for p in d.rglob("*"):
        rel = p.relative_to(d).as_posix()
        st = p.lstat()
        if p.is_symlink():
            out[rel] = ("link", (st.st_size, st.st_mtime_ns))
        elif p.is_dir():
            out[rel] = ("dir", st.st_mtime_ns)
        elif p.parent == d and p.name in _REAL_CONTENT_NAMES:
            out[rel] = ("sha256", hashlib.sha256(p.read_bytes()).hexdigest())
        else:
            out[rel] = ("stat", (st.st_size, st.st_mtime_ns))
    return out


@pytest.fixture(scope="module", autouse=True)
def _real_config_untouched_by_module():
    """The whole module, every test and every subprocess it starts, leaves the real folder as it was."""
    before = real_fingerprint(REAL_CONFIG)
    yield
    assert real_fingerprint(REAL_CONFIG) == before, "this test module changed the real ~/.config/opencode"


@pytest.fixture(autouse=True)
def _real_config_untouched():
    before = real_fingerprint(REAL_CONFIG)
    yield
    assert real_fingerprint(REAL_CONFIG) == before, "a test changed the real ~/.config/opencode"


def test_real_fingerprint_reads_only_config_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "opencode"
    (d / "sub").mkdir(parents=True)
    (d / "opencode.json").write_text("{}")
    for name in ("auth.json", "token.json", "credentials.json", "opencode.db", "service.json", "sub/opencode.json"):
        (d / name).write_text("SECRET-NEVER-READ")
    opened: list[str] = []
    real_read = Path.read_bytes

    def spy(self: Path) -> bytes:
        if self.is_relative_to(d):
            opened.append(self.relative_to(d).as_posix())
        return real_read(self)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_bytes", spy)
        fp = real_fingerprint(d)
    assert opened == ["opencode.json"]
    assert fp["auth.json"][0] == "stat" and fp["sub/opencode.json"][0] == "stat"
    (d / "token.json").write_text("SECRET-NEVER-READ-2")
    assert real_fingerprint(d) != fp  # a change to a stat-only file still shows


def run(*args: str, cfg_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *args, "--config-dir", str(cfg_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def config_files(d: Path) -> dict[str, bytes]:
    """Everything in the config dir except the backup folder."""
    return {k: v for k, v in _snapshot(d).items() if not k.startswith(inst.BACKUP_DIR_NAME)}


WITH_COMMENTS = """{
  // my settings
  "$schema": "https://opencode.ai/config.json",
  /* block
     comment */
  "theme": "dark", // trailing note
  "plugins": [
    "opencode-foo@1.2.3", // a package plugin
    "/somewhere/else/plugin"
  ],
  "model": "x/y",
}
"""

CASES = {
    "no-file": None,
    "empty-object": "{}\n",
    "comments-and-plugins": WITH_COMMENTS,
    "empty-plugins": '{\n  "$schema": "https://opencode.ai/config.json",\n  "plugins": []\n}\n',
    "one-line": '{"theme": "dark"}',
    "crlf": '{\r\n  "theme": "dark"\r\n}\r\n',
    "bom": '\ufeff{\n  "theme": "dark"\n}\n',
}


def _seed(tmp_path: Path, content: str | None) -> Path:
    d = tmp_path / "opencode"
    d.mkdir(parents=True)
    if content is not None:
        (d / "opencode.json").write_bytes(content.encode("utf-8"))
    return d


def _strip_jsonc(text: str) -> object:
    """Independent check: the result parses as JSON once comments/trailing commas are removed."""
    root = inst.parse(text)
    assert root.kind == "object"
    joined = " ".join(text[t.start : t.end] for t in inst._scan(text))
    return json.loads(joined.replace(", }", " }").replace(", ]", " ]"))


@pytest.mark.parametrize("name", list(CASES))
def test_install_then_uninstall_is_byte_identical(tmp_path: Path, name: str) -> None:
    d = _seed(tmp_path, CASES[name])
    before = config_files(d)
    r = run("install", cfg_dir=d)
    assert r.returncode == 0, r.stderr
    text = (d / "opencode.json").read_bytes().decode("utf-8")
    parsed = _strip_jsonc(text)
    assert str(PLUGIN) in parsed["plugins"]  # type: ignore[index]
    # every original byte is still there, in order (pure insertion)
    if CASES[name]:
        orig = CASES[name]
        it = iter(text)
        assert all(ch in it for ch in orig), "install removed or reordered original text"
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "installed"
    r = run("uninstall", cfg_dir=d)
    assert r.returncode == 0, r.stderr
    assert "byte for byte" in r.stdout
    assert config_files(d) == before
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "not-installed"


def test_exact_text_added(tmp_path: Path) -> None:
    d = _seed(tmp_path, '{\n  "$schema": "https://opencode.ai/config.json",\n  "plugins": []\n}\n')
    run("install", cfg_dir=d)
    assert (d / "opencode.json").read_text() == (
        '{\n  "$schema": "https://opencode.ai/config.json",\n  "plugins": [' + LIT + "]\n}\n"
    )
    d2 = _seed(tmp_path / "b", None)
    run("install", cfg_dir=d2)
    assert (d2 / "opencode.json").read_text() == "{\n  \"plugins\": [" + LIT + "]\n}\n"


def test_comments_preserved_and_appended_in_style(tmp_path: Path) -> None:
    d = _seed(tmp_path, WITH_COMMENTS)
    run("install", cfg_dir=d)
    expected = WITH_COMMENTS.replace(
        '"/somewhere/else/plugin"\n', '"/somewhere/else/plugin",\n    ' + LIT + "\n"
    )
    assert (d / "opencode.json").read_text() == expected


def test_missing_key_inserted_after_last_member(tmp_path: Path) -> None:
    src = '{\n  "theme": "dark"\n}\n'
    d = _seed(tmp_path, src)
    run("install", cfg_dir=d)
    assert (d / "opencode.json").read_text() == '{\n  "theme": "dark",\n  "plugins": [' + LIT + "]\n}\n"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    for i, content in enumerate(CASES.values()):
        d = _seed(tmp_path / str(i), content)
        before = _snapshot(d)
        r = run("install", "--dry-run", cfg_dir=d)
        assert r.returncode == 0, r.stderr
        assert "dry run" in r.stdout and "+" in r.stdout and LIT in r.stdout
        assert _snapshot(d) == before
        assert not (d / inst.BACKUP_DIR_NAME).exists()
    # dry-run uninstall too
    d = _seed(tmp_path / "u", "{}\n")
    run("install", cfg_dir=d)
    before = _snapshot(d)
    r = run("uninstall", "--dry-run", cfg_dir=d)
    assert r.returncode == 0 and "dry run" in r.stdout
    assert _snapshot(d) == before


def test_second_install_changes_nothing(tmp_path: Path) -> None:
    d = _seed(tmp_path, WITH_COMMENTS)
    run("install", cfg_dir=d)
    after_first = _snapshot(d)
    r = run("install", cfg_dir=d)
    assert r.returncode == 0 and "already installed" in r.stdout
    assert _snapshot(d) == after_first  # no new backup either


def test_backup_exists_after_install(tmp_path: Path) -> None:
    d = _seed(tmp_path, WITH_COMMENTS)
    run("install", cfg_dir=d)
    backups = list((d / inst.BACKUP_DIR_NAME).iterdir())
    assert len(backups) == 1 and backups[0].suffix == ".bak"
    assert backups[0].read_bytes() == WITH_COMMENTS.encode()
    d2 = tmp_path / "none"
    d2.mkdir()
    run("install", cfg_dir=d2)
    markers = list((d2 / inst.BACKUP_DIR_NAME).iterdir())
    assert len(markers) == 1 and markers[0].suffix == ".absent"


def test_status_states(tmp_path: Path) -> None:
    d = _seed(tmp_path, None)
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "not-installed"
    (d / "opencode.json").write_text('{"plugins": ["/old/checkout/workspaces/opencode-adapter/src/plugin"]}')
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "installed-stale-path"
    (d / "opencode.json").write_text('{"plugins": [' + LIT + "]}")
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "installed"
    (d / "opencode.json").unlink()
    (d / "opencode.jsonc").write_text('{ // c\n "plugin": [' + LIT + "]}")
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "installed"


def test_stale_path_repointed_and_uninstall_restores(tmp_path: Path) -> None:
    src = '{"plugins": ["C:\\\\old\\\\workspaces\\\\opencode-adapter\\\\src\\\\plugin"]}\n'
    d = _seed(tmp_path, src)
    assert run("install", cfg_dir=d).returncode == 0
    assert (d / "opencode.json").read_text() == '{"plugins": [' + LIT + "]}\n"
    r = run("uninstall", cfg_dir=d)
    # The restore is the member's original file; it used to exit 1 "did not verify" here.
    assert r.returncode == 0, r.stderr
    assert "byte for byte" in r.stdout and "older copy" in r.stdout
    assert (d / "opencode.json").read_text() == src


def test_uninstall_after_member_edit_removes_only_entry(tmp_path: Path) -> None:
    d = _seed(tmp_path, '{\n  "plugins": ["a"]\n}\n')
    run("install", cfg_dir=d)
    cfg = d / "opencode.json"
    cfg.write_text(cfg.read_text().replace('"a"', '"b"'))
    r = run("uninstall", cfg_dir=d)
    assert r.returncode == 0 and "removing only the plugin entry" in r.stdout
    assert cfg.read_text() == '{\n  "plugins": ["b"]\n}\n'


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   \n",
        "{ not json }",
        '{"plugins": "x"}',
        '{"plugins": [], "plugins": []}',
        '{"a": 1 /* unterminated',
        "[1, 2]",
        '{"a": 1} trailing',
        "{'a': 1}",
        '{"a": 1,, "b": 2}',
    ],
)
def test_unsafe_shape_refuses_and_leaves_file(tmp_path: Path, bad: str) -> None:
    d = _seed(tmp_path, bad)
    before = _snapshot(d)
    for args in (("install",), ("install", "--dry-run")):
        r = run(*args, cfg_dir=d)
        assert r.returncode == 1
        assert "refused, nothing changed" in r.stderr
        assert _snapshot(d) == before


def test_both_config_names_refuses(tmp_path: Path) -> None:
    d = _seed(tmp_path, "{}")
    (d / "opencode.jsonc").write_text("{}")
    before = _snapshot(d)
    r = run("install", cfg_dir=d)
    assert r.returncode == 1 and "ambiguous" in r.stderr
    assert _snapshot(d) == before


def test_jsonc_file_is_the_one_edited(tmp_path: Path) -> None:
    d = _seed(tmp_path, None)
    (d / "opencode.jsonc").write_text("{\n  // hi\n}\n")
    run("install", cfg_dir=d)
    assert not (d / "opencode.json").exists()
    assert LIT in (d / "opencode.jsonc").read_text()
    run("uninstall", cfg_dir=d)
    assert (d / "opencode.jsonc").read_text() == "{\n  // hi\n}\n"


def test_windows_path_escaping() -> None:
    win = r"C:\Users\Jo \"Q\"\Glitch\workspaces\opencode-adapter\src\plugin"
    lit = inst.encode_path(win)
    assert lit == '"C:\\\\Users\\\\Jo \\\\\\"Q\\\\\\"\\\\Glitch\\\\workspaces\\\\opencode-adapter\\\\src\\\\plugin"'
    assert json.loads(lit) == win
    text = inst.apply_install('{"plugins": []}', Path(win))
    assert json.loads(text)["plugins"] == [win]
    assert inst.is_stale(win, Path("/elsewhere/opencode-adapter/src/plugin"))


def test_default_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path / "x"))
    assert inst.default_config_dir() == tmp_path / "x"
    monkeypatch.delenv("OPENCODE_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert inst.default_config_dir() == tmp_path / "xdg" / "opencode"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert inst.default_config_dir() == Path.home() / ".config" / "opencode"


def test_plugin_dir_is_the_adapter_plugin() -> None:
    assert PLUGIN.is_absolute()
    assert (PLUGIN / "index.ts").is_file()
    # The Glitch workspace name, or the folder name the README tells a member to use.
    assert PLUGIN.parts[-2:] == ("src", "plugin")
    assert PLUGIN.parts[-3] in ("opencode-adapter", "glitch-opencode-adapter")


# --- stale-path recognition: the two checkout names this plugin ships under -------------------------

README_CHECKOUT = "/Users/someone/glitch-opencode-adapter/src/plugin"  # README step 2's folder name


def test_own_path_is_never_stale() -> None:
    assert inst.is_ours(str(PLUGIN), PLUGIN)
    assert not inst.is_stale(str(PLUGIN), PLUGIN)
    assert not inst.is_stale(str(PLUGIN) + "/", PLUGIN)
    for name in ("opencode-adapter", "glitch-opencode-adapter"):
        here = Path(f"/a/{name}/src/plugin")
        assert not inst.is_stale(str(here), here)


def test_readme_checkout_name_is_recognised_as_stale(tmp_path: Path) -> None:
    """README step 2 names the folder glitch-opencode-adapter; a member who moved it (or ran the
    install from the Glitch workspace afterwards) must get the old entry repointed, never a second
    copy of the plugin loaded beside it."""
    assert inst.is_stale(README_CHECKOUT, PLUGIN)
    assert inst.is_stale("C:\\Users\\Jo\\glitch-opencode-adapter\\src\\plugin\\", PLUGIN)
    src = '{\n  "plugins": ["' + README_CHECKOUT + '"]\n}\n'
    d = _seed(tmp_path, src)
    assert run("status", cfg_dir=d).stdout.splitlines()[0] == "installed-stale-path"
    r = run("install", cfg_dir=d)
    assert r.returncode == 0, r.stderr
    assert (d / "opencode.json").read_text() == '{\n  "plugins": [' + LIT + "]\n}\n"
    r = run("uninstall", cfg_dir=d)
    assert r.returncode == 0 and "byte for byte" in r.stdout
    assert (d / "opencode.json").read_text() == src


def test_unrelated_lookalike_paths_are_not_stale() -> None:
    for other in (
        "/x/my-opencode-adapter/src/plugin",
        "/x/opencode-adapter-fork/src/plugin",
        "/x/opencode-adapter/src/plugins",
        "/x/opencode-adapter/plugin",
        "opencode-foo@1.2.3",
    ):
        assert not inst.is_stale(other, PLUGIN), other


# --- the real config folder is touched only when nothing else is named -----------------------------


def _run_env(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(SCRIPT), *args], capture_output=True, text=True,
                          check=False, env=env)


def _bare_env(home: Path) -> dict[str, str]:
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith(("OPENCODE_", "XDG_"))}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


def test_explicit_config_dir_never_touches_the_default_folder(tmp_path: Path) -> None:
    """--config-dir (or OPENCODE_CONFIG_DIR) wins; the default folder under HOME is never created,
    read or written. HOME is a fake here, so this cannot reach the real folder even on failure."""
    home = tmp_path / "home"
    default = home / ".config" / "opencode"
    default.mkdir(parents=True)
    (default / "opencode.json").write_text('{"theme": "dark"}\n')
    before = _snapshot(default)
    target = tmp_path / "chosen"
    for cmd in (("install", "--dry-run"), ("install",), ("status",), ("install",), ("uninstall",)):
        r = _run_env(*cmd, "--config-dir", str(target), env=_bare_env(home))
        assert r.returncode == 0, r.stderr
        assert str(default) not in r.stdout + r.stderr
    env = _bare_env(home)
    env["OPENCODE_CONFIG_DIR"] = str(tmp_path / "by-env")
    assert _run_env("install", env=env).returncode == 0
    assert LIT in (tmp_path / "by-env" / "opencode.json").read_text()
    assert _snapshot(default) == before


def test_default_folder_is_used_only_when_nothing_is_named(tmp_path: Path) -> None:
    """With no flag and no env, the OS default (HOME/.config/opencode) is the target: README step 5
    relies on this. Run against a fake HOME, never the real one."""
    home = tmp_path / "home"
    r = _run_env("install", "--dry-run", env=_bare_env(home))
    assert r.returncode == 0, r.stderr
    assert str(home / ".config" / "opencode" / "opencode.json") in r.stdout
    assert not (home / ".config").exists()  # dry run: nothing created
