"""Install, uninstall or report the Glitch OpenCode plugin in the member's OpenCode config.

Usage::

    python install.py install   [--dry-run] [--config-dir PATH]
    python install.py uninstall [--dry-run] [--config-dir PATH]
    python install.py status    [--config-dir PATH]

The loading route is the one measured in ``docs/api-findings.md`` item 1: a ``plugins`` entry in
``<config dir>/opencode.json`` naming the absolute DIRECTORY that holds the plugin's ``index.ts``
(here ``workspaces/opencode-adapter/src/plugin``, derived from this script's own location).
``opencode plugin list`` never shows a local plugin (item 1d), so ``status`` reads the config text.

Guarantees:

- The config is edited by minimal text insertion: every byte outside the inserted text is kept,
  comments and JSONC included. A shape that cannot be edited safely is refused, nothing written.
- ``install`` writes a timestamped backup of the file it touches first (a marker when the file did
  not exist), into ``<config dir>/glitch-opencode-backups/``.
- ``uninstall`` restores the backup byte for byte when the file is still exactly what install made
  of it, and verifies; if the member edited the file since, it removes only the plugin entry.
- A second ``install`` changes nothing. ``--dry-run`` prints the diff and writes nothing.

Standard library only. Paths are pathlib throughout and no shell is ever invoked.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "src" / "plugin"
# OpenCode reads both names from its config dir (binary strings); we edit whichever one exists.
CONFIG_NAMES = ("opencode.json", "opencode.jsonc")
DEFAULT_NAME = "opencode.json"
# Keys whose array may name the plugin: the measured `plugins` (item 1e) and the documented `plugin`.
PLUGIN_KEYS = ("plugins", "plugin")
INSERT_KEY = "plugins"
BACKUP_DIR_NAME = "glitch-opencode-backups"
STALE_SUFFIX = "/opencode-adapter/src/plugin"

_LITERAL_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")


class Refusal(Exception):
    """The config's shape cannot be edited safely; nothing was written."""


# --------------------------------------------------------------------------------------------
# JSONC scanning and parsing with character spans
# --------------------------------------------------------------------------------------------


@dataclass
class Tok:
    kind: str  # one of { } [ ] : , str lit
    start: int
    end: int
    value: object = None


@dataclass
class Node:
    kind: str  # object | array | scalar
    start: int
    end: int
    value: object = None  # decoded scalar value
    # object: (key_tok, key, value_node, comma_tok | None); array: (value_node, comma_tok | None)
    members: list[tuple[Tok, str, Node, Tok | None]] = field(default_factory=list)
    elements: list[tuple[Node, Tok | None]] = field(default_factory=list)


def _scan(text: str) -> list[Tok]:
    toks: list[Tok] = []
    i = 1 if text.startswith("﻿") else 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            if j < 0:
                raise Refusal(f"unterminated /* comment at character {i}")
            i = j + 2
        elif c in "{}[]:,":
            toks.append(Tok(c, i, i + 1))
            i += 1
        elif c == '"':
            j = i + 1
            while True:
                if j >= n:
                    raise Refusal(f"unterminated string at character {i}")
                d = text[j]
                if d == "\\":
                    j += 2
                    continue
                if d == '"':
                    break
                if ord(d) < 0x20:
                    raise Refusal(f"control character inside a string at character {j}")
                j += 1
            raw = text[i : j + 1]
            try:
                value = json.loads(raw)
            except ValueError as exc:
                raise Refusal(f"invalid string at character {i}: {exc}") from None
            toks.append(Tok("str", i, j + 1, value))
            i = j + 1
        else:
            m = _LITERAL_RE.match(text, i)
            if not m or m.end() == i:
                raise Refusal(f"unexpected character {c!r} at character {i}")
            end = m.end()
            if end < n and (text[end].isalnum() or text[end] in "_.+-"):
                raise Refusal(f"unexpected literal at character {i}")
            toks.append(Tok("lit", i, end, json.loads(m.group(0))))
            i = end
    return toks


class _Parser:
    def __init__(self, toks: list[Tok]) -> None:
        self.toks = toks
        self.i = 0

    def peek(self) -> Tok:
        if self.i >= len(self.toks):
            raise Refusal("unexpected end of file")
        return self.toks[self.i]

    def take(self, kind: str | None = None) -> Tok:
        tok = self.peek()
        if kind is not None and tok.kind != kind:
            raise Refusal(f"expected {kind!r} at character {tok.start}, found {tok.kind!r}")
        self.i += 1
        return tok

    def value(self) -> Node:
        tok = self.peek()
        if tok.kind == "{":
            return self.obj()
        if tok.kind == "[":
            return self.arr()
        if tok.kind in ("str", "lit"):
            self.i += 1
            return Node("scalar", tok.start, tok.end, tok.value)
        raise Refusal(f"unexpected {tok.kind!r} at character {tok.start}")

    def obj(self) -> Node:
        open_tok = self.take("{")
        node = Node("object", open_tok.start, -1)
        while self.peek().kind != "}":
            key_tok = self.take("str")
            self.take(":")
            val = self.value()
            comma = self.take() if self.peek().kind == "," else None
            node.members.append((key_tok, str(key_tok.value), val, comma))
            if comma is None and self.peek().kind != "}":
                raise Refusal(f"expected ',' or '}}' at character {self.peek().start}")
        node.end = self.take("}").end
        return node

    def arr(self) -> Node:
        open_tok = self.take("[")
        node = Node("array", open_tok.start, -1)
        while self.peek().kind != "]":
            val = self.value()
            comma = self.take() if self.peek().kind == "," else None
            node.elements.append((val, comma))
            if comma is None and self.peek().kind != "]":
                raise Refusal(f"expected ',' or ']' at character {self.peek().start}")
        node.end = self.take("]").end
        return node


def parse(text: str) -> Node:
    """Parse JSONC (comments, trailing commas) into a span tree; the top level must be an object."""
    toks = _scan(text)
    if not toks:
        raise Refusal("the file is empty (no JSON object in it)")
    p = _Parser(toks)
    root = p.value()
    if root.kind != "object":
        raise Refusal("the top level is not a JSON object")
    if p.i != len(toks):
        raise Refusal(f"unexpected text after the top-level object at character {toks[p.i].start}")
    return root


# --------------------------------------------------------------------------------------------
# The edit itself
# --------------------------------------------------------------------------------------------


def encode_path(path: str) -> str:
    """The JSON string literal for a path (Windows backslashes doubled, quotes escaped)."""
    return json.dumps(path, ensure_ascii=False)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def is_ours(value: object, plugin_dir: Path) -> bool:
    return isinstance(value, str) and bool(value) and _norm(value) == _norm(str(plugin_dir))


def is_stale(value: object, plugin_dir: Path) -> bool:
    if not isinstance(value, str) or not value or is_ours(value, plugin_dir):
        return False
    shape = value.replace("\\", "/").rstrip("/").lower()
    return shape.endswith(STALE_SUFFIX)


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _line_indent(text: str, pos: int) -> str | None:
    """The indentation of pos's line when pos is the first non-blank character on it, else None."""
    line_start = max(text.rfind("\n", 0, pos) + 1, 0)
    prefix = text[line_start:pos]
    if prefix.lstrip("﻿").strip(" \t") == "" and line_start > 0:
        return prefix.lstrip("﻿")
    return None


def _plugin_arrays(root: Node) -> list[tuple[str, Node]]:
    """Every top-level plugin key with its value; duplicates and non-arrays are refused."""
    found: list[tuple[str, Node]] = []
    for key in PLUGIN_KEYS:
        hits = [m for m in root.members if m[1] == key]
        if len(hits) > 1:
            raise Refusal(f'the key "{key}" appears more than once at the top level')
        if hits:
            val = hits[0][2]
            if val.kind != "array":
                raise Refusal(f'the "{key}" value is not an array')
            found.append((key, val))
    return found


def created_text(plugin_dir: Path) -> str:
    return "{\n  " + f'"{INSERT_KEY}": [{encode_path(str(plugin_dir))}]' + "\n}\n"


def apply_install(text: str | None, plugin_dir: Path) -> str:
    """Return the config text with the plugin entry added (unchanged when already present)."""
    if text is None:
        return created_text(plugin_dir)
    root = parse(text)
    arrays = _plugin_arrays(root)
    lit = encode_path(str(plugin_dir))
    nl = _newline(text)
    for _key, arr in arrays:
        if any(is_ours(el.value, plugin_dir) for el, _c in arr.elements):
            return text  # idempotent
    for _key, arr in arrays:
        for el, _c in arr.elements:
            if is_stale(el.value, plugin_dir):  # repoint a moved checkout in place
                return text[: el.start] + lit + text[el.end :]
    target = next((arr for key, arr in arrays if key == INSERT_KEY), None)
    if target is not None:
        return _insert_into_array(text, target, lit, nl)
    member = f'"{INSERT_KEY}": [{lit}]'
    return _insert_member(text, root, member, nl)


def _insert_into_array(text: str, arr: Node, lit: str, nl: str) -> str:
    inner_start, inner_end = arr.start + 1, arr.end - 1
    if not arr.elements:
        if text[inner_start:inner_end].strip() == "":
            return text[:inner_start] + lit + text[inner_end:]
        return text[:inner_start] + lit + text[inner_start:]
    last, comma = arr.elements[-1]
    indent = _line_indent(text, last.start)
    sep = (nl + indent) if indent is not None else " "
    if comma is not None:
        return text[: comma.end] + sep + lit + text[comma.end :]
    return text[: last.end] + "," + sep + lit + text[last.end :]


def _insert_member(text: str, root: Node, member: str, nl: str) -> str:
    inner_start, inner_end = root.start + 1, root.end - 1
    if not root.members:
        if text[inner_start:inner_end].strip() == "":
            return text[:inner_start] + nl + "  " + member + nl + text[inner_end:]
        return text[:inner_start] + nl + "  " + member + text[inner_start:]
    key_tok, _key, val, comma = root.members[-1]
    indent = _line_indent(text, key_tok.start)
    sep = (nl + indent) if indent is not None else " "
    if comma is not None:
        return text[: comma.end] + sep + member + text[comma.end :]
    return text[: val.end] + "," + sep + member + text[val.end :]


def remove_entries(text: str, plugin_dir: Path) -> str:
    """Remove every entry naming this plugin (current or stale path), one element at a time."""
    while True:
        root = parse(text)
        hit = None
        for _key, arr in _plugin_arrays(root):
            for idx, (el, _comma) in enumerate(arr.elements):
                if is_ours(el.value, plugin_dir) or is_stale(el.value, plugin_dir):
                    hit = (arr, idx)
                    break
            if hit:
                break
        if hit is None:
            return text
        arr, idx = hit
        el, comma = arr.elements[idx]
        if idx + 1 < len(arr.elements):
            start, end = el.start, arr.elements[idx + 1][0].start
        elif idx > 0:
            prev_comma = arr.elements[idx - 1][1]
            assert prev_comma is not None
            start, end = prev_comma.start, (comma.end if comma else el.end)
        else:
            start, end = el.start, (comma.end if comma else el.end)
        text = text[:start] + text[end:]


# --------------------------------------------------------------------------------------------
# Files: config location, backups, atomic writes
# --------------------------------------------------------------------------------------------


def default_config_dir() -> Path:
    """OPENCODE_CONFIG_DIR replaces the config dir (findings 1f); else XDG config home / opencode.

    OpenCode resolves its global config through the XDG layout on every OS, so on Windows this is
    %USERPROFILE%\\.config\\opencode (Path.home()) unless XDG_CONFIG_HOME is set. Windows is
    docs-only: nothing has been run there (findings item 9).
    """
    env = os.environ.get("OPENCODE_CONFIG_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "opencode"


def config_file(config_dir: Path) -> Path:
    existing = [config_dir / name for name in CONFIG_NAMES if (config_dir / name).is_file()]
    if len(existing) > 1:
        names = " and ".join(p.name for p in existing)
        raise Refusal(f"both {names} exist in {config_dir}; OpenCode merges them, so which one to edit is ambiguous. Merge them into one first.")
    return existing[0] if existing else config_dir / DEFAULT_NAME


def read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file():
        raise Refusal(f"{path} is not a regular file")
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise Refusal(f"{path} is not UTF-8 text") from None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
        if path.exists():
            try:
                os.chmod(tmp, path.stat().st_mode & 0o7777)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def backup_dir(config_dir: Path) -> Path:
    return config_dir / BACKUP_DIR_NAME


def write_backup(config_dir: Path, cfg: Path, text: str | None) -> Path:
    bdir = backup_dir(config_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if text is None:
        dest = bdir / f"{cfg.name}.{stamp}.absent"
        dest.write_bytes(b"")
    else:
        dest = bdir / f"{cfg.name}.{stamp}.bak"
        dest.write_bytes(text.encode("utf-8"))
    return dest


def list_backups(config_dir: Path, cfg: Path) -> list[Path]:
    bdir = backup_dir(config_dir)
    if not bdir.is_dir():
        return []
    found = [p for p in bdir.iterdir() if p.name.startswith(cfg.name + ".") and p.suffix in (".bak", ".absent")]
    return sorted(found, key=lambda p: p.name, reverse=True)


def _diff(cfg: Path, before: str | None, after: str | None) -> str:
    a = [] if before is None else before.splitlines(keepends=True)
    b = [] if after is None else after.splitlines(keepends=True)
    label_a = "/dev/null" if before is None else str(cfg)
    label_b = "/dev/null" if after is None else str(cfg)
    return "".join(difflib.unified_diff(a, b, label_a, label_b))


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def cmd_install(config_dir: Path, plugin_dir: Path, dry_run: bool) -> int:
    cfg = config_file(config_dir)
    before = read_text(cfg)
    after = apply_install(before, plugin_dir)
    if after == before:
        print(f"already installed: {cfg} names {plugin_dir}; nothing changed")
        return 0
    parse(after)  # the result must itself be a clean config
    if dry_run:
        print(f"dry run: would edit {cfg} (nothing written)")
        print(_diff(cfg, before, after), end="")
        return 0
    backup = write_backup(config_dir, cfg, before)
    _atomic_write(cfg, after)
    if read_text(cfg) != after or classify(config_dir, plugin_dir)[0] != "installed":
        print(f"error: {cfg} did not read back as installed; restore from {backup}", file=sys.stderr)
        return 1
    print(f"installed: {cfg} now names {plugin_dir}")
    print(f"backup: {backup}")
    print(_diff(cfg, before, after), end="")
    return 0


def cmd_uninstall(config_dir: Path, plugin_dir: Path, dry_run: bool) -> int:
    cfg = config_file(config_dir)
    current = read_text(cfg)
    if current is None or remove_entries(current, plugin_dir) == current:
        print(f"not installed: {cfg} names no Glitch plugin; nothing changed")
        return 0
    # Exact restore: the newest backup whose install result is byte-identical to the file now.
    restore: Path | None = None
    restore_text: str | None = None
    for b in list_backups(config_dir, cfg):
        b_text = None if b.suffix == ".absent" else b.read_bytes().decode("utf-8", errors="strict")
        try:
            if apply_install(b_text, plugin_dir) == current:
                restore, restore_text = b, b_text
                break
        except Refusal:
            continue
    if restore is not None:
        after = restore_text
        how = f"restoring the backup {restore} byte for byte"
    else:
        after = remove_entries(current, plugin_dir)
        how = "the file was edited since install, so removing only the plugin entry"
    if dry_run:
        print(f"dry run: would edit {cfg}, {how} (nothing written)")
        print(_diff(cfg, current, after), end="")
        return 0
    if after is None:
        cfg.unlink()
    else:
        _atomic_write(cfg, after)
    if read_text(cfg) != after or classify(config_dir, plugin_dir)[0] != "not-installed":
        print(f"error: {cfg} did not verify after uninstall", file=sys.stderr)
        return 1
    print(f"uninstalled: {how}; verified")
    print(_diff(cfg, current, after), end="")
    return 0


def classify(config_dir: Path, plugin_dir: Path) -> tuple[str, list[str]]:
    """not-installed | installed | installed-stale-path, with detail lines, from the config text."""
    notes: list[str] = []
    ours = stale = False
    for name in (*CONFIG_NAMES, "config.json"):
        path = config_dir / name
        text = read_text(path) if path.is_file() else None
        if text is None:
            continue
        try:
            arrays = _plugin_arrays(parse(text))
        except Refusal as exc:
            notes.append(f"{path}: cannot read ({exc})")
            continue
        for key, arr in arrays:
            for el, _c in arr.elements:
                if is_ours(el.value, plugin_dir):
                    ours = True
                    notes.append(f'{path}: "{key}" names {el.value}')
                elif is_stale(el.value, plugin_dir):
                    stale = True
                    notes.append(f'{path}: "{key}" names a stale path {el.value} (current: {plugin_dir})')
    pdir = config_dir / "plugins"
    if pdir.is_dir():
        for child in sorted(pdir.iterdir()):
            try:
                same = child.resolve() == plugin_dir.resolve()
            except OSError:
                same = False
            if same or child.name.lower().startswith("glitch"):
                notes.append(f"{child}: a plugin directory that may be Glitch's (not managed by this script)")
    state = "installed" if ours else ("installed-stale-path" if stale else "not-installed")
    return state, notes


def cmd_status(config_dir: Path, plugin_dir: Path) -> int:
    state, notes = classify(config_dir, plugin_dir)
    print(state)
    for line in notes:
        print(f"  {line}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="install.py", description="Install the Glitch OpenCode plugin.")
    ap.add_argument("command", choices=("install", "uninstall", "status"))
    ap.add_argument("--dry-run", action="store_true", help="print the diff and write nothing")
    ap.add_argument("--config-dir", type=Path, default=None, help="OpenCode config dir (default: per OS)")
    args = ap.parse_args(argv)
    config_dir = (args.config_dir or default_config_dir()).expanduser().absolute()
    try:
        if args.command == "install":
            return cmd_install(config_dir, PLUGIN_DIR, args.dry_run)
        if args.command == "uninstall":
            return cmd_uninstall(config_dir, PLUGIN_DIR, args.dry_run)
        return cmd_status(config_dir, PLUGIN_DIR)
    except Refusal as exc:
        print(f"refused, nothing changed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
