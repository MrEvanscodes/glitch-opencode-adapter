// root.ts - which Glitch folder (if any) an OpenCode session belongs to.
//
// The same rule as the bridge's `common.resolve_root` (src/bridge/common.py):
// the first folder, walking up from the start, that holds BOTH `CLAUDE.md` and
// `.claude/hooks/write-guard.py`. `CLAUDE.md` alone is not enough: every
// workspace under `workspaces/` carries a stamped `CLAUDE.md` of its own, and
// the walk must go past it to the real root.
//
// The start is realpathed first (OpenCode's ctx.location.directory already is,
// so ours must be too, or a /var vs /private/var start would compare unequal).
// Like Python's non-strict os.path.realpath, a start that does not exist still
// resolves: the deepest existing ancestor is realpathed and the missing tail is
// kept lexically, so a deleted subfolder of Glitch still finds Glitch (a guard
// that switched itself off there would fail in the unsafe direction).
//
// node:fs / node:path / node:os only: the host is Bun, the tests are Node.

import * as fs from "node:fs";
import * as os from "node:os";
import * as nodePath from "node:path";

export const ROOT_MARKER_FILE = "CLAUDE.md";
export const ROOT_MARKER_GUARD_PARTS = [".claude", "hooks", "write-guard.py"];

/** The filesystem and path seams, injectable so the walk is testable at string level. */
export interface RootDeps {
  path: typeof nodePath.posix;
  isFile: (p: string) => boolean;
  realpath: (p: string) => string;
  homedir: () => string;
}

function isFileOnDisk(p: string): boolean {
  try {
    return fs.statSync(p).isFile();
  } catch {
    return false;
  }
}

/** Non-strict realpath: realpath the deepest existing ancestor, keep the missing tail. */
export function realpathLoose(p: string, path: typeof nodePath.posix, realpath: (p: string) => string): string {
  const tail: string[] = [];
  let current = path.resolve(p);
  while (true) {
    try {
      const real = realpath(current);
      return tail.length ? path.join(real, ...tail.reverse()) : real;
    } catch {
      const parent = path.dirname(current);
      if (parent === current) {
        // Nothing on this path exists at all: keep it lexical.
        return path.resolve(p);
      }
      tail.push(path.basename(current));
      current = parent;
    }
  }
}

function expandUser(raw: string, path: typeof nodePath.posix, homedir: () => string): string {
  if (raw === "~") return homedir();
  if (raw.startsWith("~/") || raw.startsWith("~\\")) return path.join(homedir(), raw.slice(2));
  return raw;
}

export function isGlitchRoot(folder: string, deps: RootDeps): boolean {
  return (
    deps.isFile(deps.path.join(folder, ROOT_MARKER_FILE)) &&
    deps.isFile(deps.path.join(folder, ...ROOT_MARKER_GUARD_PARTS))
  );
}

/** The walk with explicit seams. Total: any error is null. */
export function findGlitchRootWith(startDir: unknown, deps: RootDeps): string | null {
  try {
    if (typeof startDir !== "string" || startDir.trim() === "") return null;
    let current = realpathLoose(expandUser(startDir, deps.path, deps.homedir), deps.path, deps.realpath);
    while (true) {
      if (isGlitchRoot(current, deps)) return current;
      const parent = deps.path.dirname(current);
      if (parent === current) return null;
      current = parent;
    }
  } catch {
    return null;
  }
}

const defaultDeps: RootDeps = {
  path: nodePath as typeof nodePath.posix,
  isFile: isFileOnDisk,
  realpath: (p) => fs.realpathSync.native(p),
  homedir: () => os.homedir(),
};

// Positives only: a null is never cached, so a Glitch root created after a
// first miss is still found on the next call.
const cache = new Map<string, string>();

/** The Glitch root that `startDir` sits in, or null when it sits in none. */
export function findGlitchRoot(startDir: string): string | null {
  if (typeof startDir !== "string" || startDir.trim() === "") return null;
  const hit = cache.get(startDir);
  if (hit !== undefined) return hit;
  const found = findGlitchRootWith(startDir, defaultDeps);
  if (found !== null) cache.set(startDir, found);
  return found;
}

/** Test seam: forget every cached root. */
export function clearRootCache(): void {
  cache.clear();
}
