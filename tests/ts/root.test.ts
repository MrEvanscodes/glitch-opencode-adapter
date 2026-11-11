import { test, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

import {
  findGlitchRoot,
  findGlitchRootWith,
  clearRootCache,
  type RootDeps,
} from "../../src/plugin/root.ts";

let tmp: string; // as handed out by the OS (on macOS: /var/..., a symlink)
let realTmp: string; // its realpath (/private/var/...)
let root: string; // realpathed Glitch root

function makeGlitch(dir: string): void {
  fs.mkdirSync(path.join(dir, ".claude", "hooks"), { recursive: true });
  fs.writeFileSync(path.join(dir, "CLAUDE.md"), "# root\n");
  fs.writeFileSync(path.join(dir, ".claude", "hooks", "write-guard.py"), "# guard\n");
}

before(() => {
  tmp = fs.mkdtempSync(path.join(os.tmpdir(), "glitch-root-test-"));
  realTmp = fs.realpathSync(tmp);
  root = path.join(realTmp, "Glitch");
  makeGlitch(root);
  fs.mkdirSync(path.join(root, "glitch-mem", "Memory"), { recursive: true });
  // A workspace carrying its own stamped CLAUDE.md (but no guard).
  fs.mkdirSync(path.join(root, "workspaces", "x", "src"), { recursive: true });
  fs.writeFileSync(path.join(root, "workspaces", "x", "CLAUDE.md"), "# workspace\n");
  // A folder with only the guard, no CLAUDE.md.
  fs.mkdirSync(path.join(realTmp, "half-guard", ".claude", "hooks"), { recursive: true });
  fs.writeFileSync(path.join(realTmp, "half-guard", ".claude", "hooks", "write-guard.py"), "");
  fs.mkdirSync(path.join(realTmp, "outside", "deep"), { recursive: true });
  fs.symlinkSync(path.join(root, "workspaces", "x"), path.join(realTmp, "link-to-x"), "dir");
});

after(() => {
  fs.rmSync(tmp, { recursive: true, force: true });
});

beforeEach(() => clearRootCache());

test("at the root", () => {
  assert.equal(findGlitchRoot(root), root);
});

test("from a subfolder", () => {
  assert.equal(findGlitchRoot(path.join(root, "glitch-mem", "Memory")), root);
});

test("workspaces/x with its own CLAUDE.md walks past it", () => {
  assert.equal(findGlitchRoot(path.join(root, "workspaces", "x")), root);
  assert.equal(findGlitchRoot(path.join(root, "workspaces", "x", "src")), root);
});

test("a symlinked dir resolves to the realpathed root", () => {
  assert.equal(findGlitchRoot(path.join(realTmp, "link-to-x")), root);
});

test("an un-realpathed start (OS tmpdir spelling) returns the realpath", () => {
  // On macOS tmpdir is /var/... -> /private/var/...; elsewhere this is the identity.
  assert.equal(findGlitchRoot(path.join(tmp, "Glitch", "workspaces", "x")), root);
});

test("no Glitch above is null", () => {
  assert.equal(findGlitchRoot(path.join(realTmp, "outside", "deep")), null);
});

test("guard alone is not a root", () => {
  assert.equal(findGlitchRoot(path.join(realTmp, "half-guard")), null);
});

test("a nonexistent start inside the root still finds the root", () => {
  assert.equal(findGlitchRoot(path.join(root, "workspaces", "gone", "deeper")), root);
  assert.equal(findGlitchRoot(path.join(tmp, "Glitch", "never", "made")), root);
  assert.equal(findGlitchRoot(path.join(realTmp, "link-to-x", "ghost")), root);
});

test("empty, blank and non-string input is null", () => {
  assert.equal(findGlitchRoot(""), null);
  assert.equal(findGlitchRoot("   "), null);
  assert.equal(findGlitchRoot(undefined as unknown as string), null);
  assert.equal(findGlitchRoot(null as unknown as string), null);
  assert.equal(findGlitchRoot(42 as unknown as string), null);
});

test("a miss is not cached: a root created later is found", () => {
  const later = path.join(realTmp, "later");
  fs.mkdirSync(path.join(later, "sub"), { recursive: true });
  assert.equal(findGlitchRoot(path.join(later, "sub")), null);
  makeGlitch(later);
  assert.equal(findGlitchRoot(path.join(later, "sub")), later);
});

test("a hit is cached by input string", () => {
  const start = path.join(root, "glitch-mem");
  assert.equal(findGlitchRoot(start), root);
  assert.equal(findGlitchRoot(start), root);
});

// String-level Windows normalisation: path.win32 with a fake filesystem.
function winDeps(files: string[]): RootDeps {
  const set = new Set(files.map((f) => path.win32.normalize(f).toLowerCase()));
  return {
    path: path.win32 as unknown as typeof path.posix,
    isFile: (p) => set.has(path.win32.normalize(p).toLowerCase()),
    realpath: (p) => path.win32.normalize(p),
    homedir: () => "C:\\Users\\me",
  };
}

const WIN_ROOT = "C:\\Users\\me\\Glitch";
const WIN_FILES = [
  `${WIN_ROOT}\\CLAUDE.md`,
  `${WIN_ROOT}\\.claude\\hooks\\write-guard.py`,
  `${WIN_ROOT}\\workspaces\\x\\CLAUDE.md`,
];

test("windows: backslash path walks up to the drive root", () => {
  const deps = winDeps(WIN_FILES);
  assert.equal(findGlitchRootWith("C:\\Users\\me\\Glitch\\workspaces\\x\\src", deps), WIN_ROOT);
  assert.equal(findGlitchRootWith("D:\\elsewhere\\deep", deps), null);
});

test("windows: forward slashes, trailing separator and dot segments normalise", () => {
  const deps = winDeps(WIN_FILES);
  assert.equal(findGlitchRootWith("C:/Users/me/Glitch/workspaces/x/", deps), WIN_ROOT);
  assert.equal(findGlitchRootWith("C:\\Users\\me\\Glitch\\workspaces\\..\\glitch-mem\\.", deps), WIN_ROOT);
});

test("windows: ~ expands against the home folder", () => {
  const deps = winDeps(WIN_FILES);
  assert.equal(findGlitchRootWith("~\\Glitch\\workspaces", deps), WIN_ROOT);
});

test("windows: CLAUDE.md alone (a workspace) is not a root", () => {
  const deps = winDeps([`${WIN_ROOT}\\workspaces\\x\\CLAUDE.md`]);
  assert.equal(findGlitchRootWith("C:\\Users\\me\\Glitch\\workspaces\\x", deps), null);
});
