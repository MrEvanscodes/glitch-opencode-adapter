/**
 * e2e-bridge.test.ts - the REAL Python bridge, spawned by the REAL `callBridge`, against the real
 * Glitch root this workspace sits in (session start reads real memory, read-only).
 *
 * Invocation: callBridge's own default, `uv run --no-project --directory <root>/.claude/scripts
 * python <workspace>/src/bridge/opencode_bridge.py <mode>`, with only `env` and `logPath` set.
 *
 * Isolation is by environment, the conftest doors: GLITCH_OPENCODE_CAPTURE_STATE (a fresh folder
 * under the system temp folder), GLITCH_OPENCODE_BIN (a path that does not exist, so no real
 * opencode runs), GLITCH_OPENCODE_NO_KEEPER=1, XDG_* / OPENCODE_CONFIG_DIR, the hook log, the watch
 * and read-grant state, all in temp. uv's own cache and Python install folder are pinned to their
 * real locations first, so moving XDG_* does not make uv download a Python per run.
 *
 * The guard: the REAL pending queue, ledger opencode-staged.json and opencode-chats folder (paths
 * asked of the engine's own config) are fingerprinted before and after and must not move; so must
 * every non-lock file in the real state folder and the memory vault. SQLite `-shm`/`-wal` and
 * `.lock` files are left out: a read touches them without changing anything.
 *
 * B10, the seam: every mode and every answer shape the bridge really emits goes through the REAL
 * `callBridge` and its REAL `parseBridgeOutput` here, so a parser that rejects a shape the bridge
 * sends is red in this file (pre deny + allow, post note + silence, prompt card + silence, session
 * with context, date and timezone, capture silence + a bad payload + the unreachable streak).
 * One call needs more than an environment door: a `post` catch writes the engine's catch diary under
 * `config.STATE_DIR`, which only moves in-process (conftest's door). That call runs the same bridge
 * file through a launcher that moves `config.STATE_DIR` to temp first (`stateDirLauncher`); the
 * fingerprint guard proves the real state folder did not move.
 *
 * Skipped, with the reason, when `uv` is not on PATH or the engine config cannot be read.
 */

import { strict as assert } from "node:assert";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, resolve } from "node:path";
import { after, before, test } from "node:test";

import { callBridge, uvArgs } from "../../src/plugin/bridge-client.ts";
import type { ArgsBuilder, BridgeOutcome } from "../../src/plugin/bridge-client.ts";
import { captureNoticeFor, resetPendingForTests, takePendingNotice } from "../../src/plugin/failure.ts";
import { localDay } from "../../src/plugin/translate.ts";

const WORKSPACE = resolve(import.meta.dirname, "..", "..");
const GLITCH_ROOT = resolve(WORKSPACE, "..", "..");
const SCRIPTS = join(GLITCH_ROOT, ".claude", "scripts");
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const BIG_FILE_BYTES = 5 * 1024 * 1024;
const SKIP_NAME = /(\.lock|-shm|-wal|-journal)$/;

function uvOnPath(): boolean {
  const r = spawnSync("uv", ["--version"], { encoding: "utf8" });
  return r.status === 0;
}

function uvLine(args: string[]): string | null {
  const r = spawnSync("uv", args, { encoding: "utf8" });
  const out = r.status === 0 ? r.stdout.trim() : "";
  return out === "" ? null : out;
}

interface RealPaths {
  pending: string;
  ledger: string;
  chats: string;
  state: string;
  memory: string;
}

/** The real capture paths, asked of the engine's own config (unpatched, as conftest does). */
function realPaths(): RealPaths | null {
  const code =
    "import json, config\n" +
    "from pathlib import Path\n" +
    "print(json.dumps({'pending': str(config.PENDING_DIR), 'ledger': str(Path(config.STATE_DIR) / 'opencode-staged.json'),\n" +
    " 'chats': str(Path(config.DATA_DIR) / 'opencode-chats'), 'state': str(config.STATE_DIR), 'memory': str(config.MEMORY_DIR)}))";
  const r = spawnSync("uv", ["run", "--no-project", "--directory", SCRIPTS, "python", "-c", code], {
    encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
  });
  if (r.status !== 0) return null;
  try {
    return JSON.parse(r.stdout.trim().split("\n").pop() ?? "") as RealPaths;
  } catch {
    return null;
  }
}

function filesUnder(target: string, skipLocks: boolean): string[] {
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const p = join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (entry.isFile() && !(skipLocks && SKIP_NAME.test(entry.name))) out.push(p);
    }
  };
  walk(target);
  return out.sort();
}

/** sha256 of a file or folder tree (relative path + bytes; a big file by size + mtime), or "absent". */
function fingerprint(target: string, skipLocks = false): string {
  if (!existsSync(target)) return "absent";
  const digest = createHash("sha256");
  const isFile = statSync(target).isFile();
  for (const p of isFile ? [target] : filesUnder(target, skipLocks)) {
    digest.update(`${isFile ? "." : relative(target, p)}\0`);
    try {
      const st = statSync(p);
      digest.update(st.size > BIG_FILE_BYTES ? `big:${st.size}:${st.mtimeMs}` : readFileSync(p));
    } catch (err) {
      digest.update(`unreadable:${(err as { code?: string }).code ?? "?"}`);
    }
    digest.update("\0");
  }
  return digest.digest("hex");
}

const skipReason = ((): string | false => {
  if (!uvOnPath()) return "uv is not on PATH: the real bridge cannot run";
  if (!existsSync(join(SCRIPTS, "config.py"))) return `no Glitch engine at ${SCRIPTS}`;
  return false;
})();

let tmp = "";
let env: NodeJS.ProcessEnv = {};
let paths: RealPaths | null = null;
let guardTargets: Record<string, [string, boolean]> = {};
let fingerprintsBefore: Record<string, string> = {};

function snapshot(): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, [p, skipLocks]] of Object.entries(guardTargets)) out[name] = fingerprint(p, skipLocks);
  return out;
}

before(() => {
  if (skipReason) return;
  paths = realPaths();
  if (paths === null) return;
  guardTargets = {
    "real pending queue": [paths.pending, false],
    "real ledger opencode-staged.json": [paths.ledger, false],
    "real opencode-chats folder": [paths.chats, false],
    "real state folder (locks/shm left out)": [paths.state, true],
    "real memory vault (locks left out)": [paths.memory, true],
  };
  fingerprintsBefore = snapshot();

  tmp = mkdtempSync(join(tmpdir(), "oc-e2e-bridge-"));
  const uvCache = uvLine(["cache", "dir"]);
  const uvPython = uvLine(["python", "dir"]);
  const base: NodeJS.ProcessEnv = { ...process.env };
  delete base.CODEX_INVOKED_BY; // the engine's recursion guard: set, the session reads as automated
  delete base.CLAUDE_INVOKED_BY;
  delete base.GLITCH_OPENCODE_BRIDGE_OVERRIDE;
  env = {
    ...base,
    ...(uvCache ? { UV_CACHE_DIR: uvCache } : {}),
    ...(uvPython ? { UV_PYTHON_INSTALL_DIR: uvPython } : {}),
    GLITCH_OPENCODE_CAPTURE_STATE: join(tmp, "capture"),
    GLITCH_OPENCODE_BIN: join(tmp, "no-such-dir", "opencode"),
    GLITCH_OPENCODE_NO_KEEPER: "1",
    XDG_DATA_HOME: join(tmp, "xdg-data"),
    XDG_STATE_HOME: join(tmp, "xdg-state"),
    XDG_CACHE_HOME: join(tmp, "xdg-cache"),
    XDG_CONFIG_HOME: join(tmp, "xdg-config"),
    OPENCODE_CONFIG_DIR: join(tmp, "opencode-config"),
    OPENCODE_DISABLE_AUTOUPDATE: "1",
    GLITCH_HOOK_LOG_FILE: join(tmp, "hook.log"),
    GLITCH_WATCH_STATE_DIR: join(tmp, "watch-state"),
    GLITCH_READ_GRANT_STATE_DIR: join(tmp, "read-grant"),
  };
  resetPendingForTests();
});

after(() => {
  if (tmp !== "") rmSync(tmp, { recursive: true, force: true });
});

/** Each test its own failure log, so a red in one call never colours another. */
function opts(name: string, extraEnv: NodeJS.ProcessEnv = {}): { env: NodeJS.ProcessEnv; logPath: string } {
  return { env: { ...env, ...extraEnv }, logPath: join(tmp, `errors-${name}.log`) };
}

function logLines(name: string): string[] {
  const log = join(tmp, `errors-${name}.log`);
  return existsSync(log) ? readFileSync(log, "utf8").split("\n").filter(Boolean) : [];
}

/**
 * The real bridge file, launched by `python -c` so `config.STATE_DIR` is moved to temp before the
 * bridge runs (the conftest door; the catch diary is resolved from it at call time). Same argv
 * shape as uvArgs, with the launcher between `python` and the bridge.
 */
const STATE_DIR_LAUNCHER =
  "import os, runpy, sys\n" +
  "from pathlib import Path\n" +
  "import config\n" +
  "config.STATE_DIR = Path(os.environ['E2E_ENGINE_STATE_DIR'])\n" +
  "bridge = sys.argv[1]\n" +
  "sys.path.insert(0, str(Path(bridge).parent))\n" +
  "sys.argv = [bridge] + sys.argv[2:]\n" +
  "runpy.run_path(bridge, run_name='__main__')\n";

const stateDirLauncher: ArgsBuilder = (root, bridgePath, mode) => {
  const argv = uvArgs(root, bridgePath, mode);
  const at = argv.indexOf("python");
  return [...argv.slice(0, at + 1), "-c", STATE_DIR_LAUNCHER, ...argv.slice(at + 1)];
};

const SECRET_PATH = (): string => join(GLITCH_ROOT, ".claude", "data", "google-accounts", "canary", "token.json");
// The fake key the engine's secret-scan suites use, split so this file is not itself a hit.
const FAKE_AWS = "AKIA" + "Z4XK7PQR2WMN5TYB";

function noFailureLogged(name: string): void {
  const log = join(tmp, `errors-${name}.log`);
  assert.ok(!existsSync(log) || readFileSync(log, "utf8") === "", "callBridge recorded a failure");
}

function needsEngine(t: { skip: (msg: string) => void }): boolean {
  if (skipReason) {
    t.skip(skipReason);
    return false;
  }
  if (paths === null) {
    t.skip("the engine config could not be read through uv");
    return false;
  }
  return true;
}

test("e2e: a real session start answers ok with context and a YYYY-MM-DD date", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = { session_id: "ses_e2e_start", session_dir: GLITCH_ROOT, parent_id: "", source: "startup", refresh: false };
  const out = await callBridge(GLITCH_ROOT, "session", payload, opts("start"));
  assert.equal(out.ok, true, `session failed: ${out.ok ? "" : out.reason}`);
  if (!out.ok || out.result === null || !("context" in out.result)) throw new Error("no context");
  assert.ok(out.result.context.length > 0, "empty context");
  assert.match(out.result.date ?? "", DATE_RE);
  // B7/B10: the engine's zone rides along, and it is one this host can date by.
  const zone = out.result.timezone;
  assert.equal(typeof zone, "string", "the real bridge sent no timezone");
  assert.notEqual(localDay(Date.now(), zone as string), null, `the host cannot date by ${zone}`);
  noFailureLogged("start");
});

test("e2e: capture for a session OpenCode does not have answers silence, no failure", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = { session_id: "ses_e2e_missing", session_dir: GLITCH_ROOT, parent_id: "", reason: "turn" };
  const out = await callBridge(GLITCH_ROOT, "capture", payload, opts("capture"));
  assert.deepEqual(out, { ok: true, result: null });
  noFailureLogged("capture");
});

test("e2e: a compaction rebuild (source compact) answers ok", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = { session_id: "ses_e2e_compact", session_dir: GLITCH_ROOT, parent_id: "", source: "compact", refresh: false };
  const out = await callBridge(GLITCH_ROOT, "session", payload, opts("compact"));
  assert.equal(out.ok, true, `compact failed: ${out.ok ? "" : out.reason}`);
  if (!out.ok || out.result === null || !("context" in out.result)) throw new Error("no context");
  assert.ok(out.result.context.length > 0);
  assert.match(out.result.date ?? "", DATE_RE);
  noFailureLogged("compact");
});

// --- B10: every mode and answer shape, through the real callBridge --------------------------------

test("e2e pre: a read of a login-secret file is denied with the engine's words", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = {
    tool_name: "read",
    tool_input: { path: SECRET_PATH() },
    session_dir: GLITCH_ROOT,
    session_id: "ses_e2e_pre_deny",
    parent_id: "",
  };
  const out = await callBridge(GLITCH_ROOT, "pre", payload, opts("pre-deny"));
  assert.equal(out.ok, true, `pre failed: ${out.ok ? "" : out.reason}`);
  if (!out.ok || out.result === null || !("deny" in out.result)) throw new Error(`no deny: ${JSON.stringify(out)}`);
  assert.equal(out.result.deny.kind, "secret-read");
  assert.ok(out.result.deny.model.length > 0 && out.result.deny.member.length > 0);
  noFailureLogged("pre-deny");
});

test("e2e pre: an ordinary read inside Glitch is allowed (silence)", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = {
    tool_name: "read",
    tool_input: { path: join(GLITCH_ROOT, "README.md") },
    session_dir: GLITCH_ROOT,
    session_id: "ses_e2e_pre_allow",
    parent_id: "",
  };
  assert.deepEqual(await callBridge(GLITCH_ROOT, "pre", payload, opts("pre-allow")), { ok: true, result: null });
  noFailureLogged("pre-allow");
});

test("e2e post: clean output answers silence (no note)", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const payload = {
    tool_name: "shell",
    session_id: "ses_e2e_post_clean",
    result: { output: "hello", content: [{ type: "text", text: "hello" }] },
    error: null,
    status: "completed",
    session_dir: GLITCH_ROOT,
  };
  assert.deepEqual(await callBridge(GLITCH_ROOT, "post", payload, opts("post-clean")), { ok: true, result: null });
  noFailureLogged("post-clean");
});

test("e2e post: a key printed by a tool comes back as a note (catch diary in temp)", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const leaked = `aws_access_key_id = ${FAKE_AWS}\n`;
  const payload = {
    tool_name: "shell",
    session_id: "ses_e2e_post_note",
    result: { output: leaked, content: [{ type: "text", text: leaked }] },
    error: null,
    status: "completed",
    session_dir: GLITCH_ROOT,
  };
  const diary = join(tmp, "engine-state");
  const out = await callBridge(GLITCH_ROOT, "post", payload, {
    ...opts("post-note", { E2E_ENGINE_STATE_DIR: diary }),
    buildArgs: stateDirLauncher,
  });
  assert.equal(out.ok, true, `post failed: ${out.ok ? "" : out.reason}`);
  if (!out.ok || out.result === null || !("note" in out.result)) throw new Error(`no note: ${JSON.stringify(out)}`);
  assert.match(out.result.note, /possible secret/);
  assert.ok(!out.result.note.includes(FAKE_AWS), "the note repeated the key");
  assert.ok(existsSync(join(diary, "secret-catches.jsonl")), "the catch diary did not land in the temp state folder");
  noFailureLogged("post-note");
});

test("e2e prompt: an ordinary message answers silence; the read-grant words answer a card", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  const base = { session_id: "ses_e2e_prompt", session_dir: GLITCH_ROOT, parent_id: "" };
  const quiet = await callBridge(GLITCH_ROOT, "prompt", { ...base, prompt: "just an ordinary message" }, opts("prompt"));
  assert.deepEqual(quiet, { ok: true, result: null });
  // The card writes only the read-grant state, which GLITCH_READ_GRANT_STATE_DIR keeps in temp.
  const card = await callBridge(GLITCH_ROOT, "prompt", { ...base, prompt: "always allow commands outside Glitch" }, opts("prompt"));
  assert.equal(card.ok, true, `prompt failed: ${card.ok ? "" : card.reason}`);
  if (!card.ok || card.result === null || !("context" in card.result)) throw new Error(`no card: ${JSON.stringify(card)}`);
  assert.match(card.result.context, /^Setting saved/);
  assert.ok(existsSync(join(tmp, "read-grant")), "the grant did not land in the temp state folder");
  noFailureLogged("prompt");
});

test("e2e capture: a bad payload is a visible capture failure, told as a chat not saved", { timeout: 120_000 }, async (t) => {
  if (!needsEngine(t)) return;
  resetPendingForTests();
  const payload = { session_id: "ses_e2e_bad", session_dir: GLITCH_ROOT, parent_id: "", reason: "not-a-reason" };
  const out = await callBridge(GLITCH_ROOT, "capture", payload, opts("capture-bad"));
  assert.deepEqual(out, { ok: false, reason: "bridge error: capture-bad-payload" });
  assert.equal(logLines("capture-bad").length, 1);
  assert.match(logLines("capture-bad")[0], /session=ses_e2e_bad bridge error: capture-bad-payload$/);
  assert.equal(takePendingNotice("ses_e2e_bad"), captureNoticeFor("bridge error: capture-bad-payload"));
});

test("e2e capture: OpenCode unreachable in a row gives capture-unreachable exactly once per streak", { timeout: 300_000 }, async (t) => {
  if (!needsEngine(t)) return;
  resetPendingForTests();
  // A fresh capture state of its own: the first call is the cold start, then each read misses
  // (GLITCH_OPENCODE_BIN points at a path that does not exist).
  const own = { GLITCH_OPENCODE_CAPTURE_STATE: join(tmp, "capture-streak") };
  const payload = { session_id: "ses_e2e_streak", session_dir: GLITCH_ROOT, parent_id: "", reason: "turn" };
  const outs: BridgeOutcome[] = [];
  for (let i = 0; i < 7; i++) outs.push(await callBridge(GLITCH_ROOT, "capture", payload, opts("capture-streak", own)));
  const failed = outs.map((o, i) => [i, o] as const).filter(([, o]) => !o.ok);
  assert.deepEqual(
    failed.map(([i, o]) => [i, o.ok ? "" : o.reason]),
    [[3, "bridge error: capture-unreachable"]],
    "cold start, two quiet misses, the third miss says it once, then quiet for the rest of the streak",
  );
  for (const [i, o] of outs.map((o, i) => [i, o] as const)) if (i !== 3) assert.deepEqual(o, { ok: true, result: null }, `call ${i}`);
  const lines = logLines("capture-streak");
  assert.equal(lines.length, 1, "recorded once");
  assert.match(lines[0], /session=ses_e2e_streak bridge error: capture-unreachable$/);
  const notice = takePendingNotice("ses_e2e_streak") ?? "";
  assert.equal(notice, captureNoticeFor("bridge error: capture-unreachable"));
  assert.doesNotMatch(notice, /guards[^.]*failed/i);
});

// Declared last: node:test runs a file's top-level tests in order, so this sees every call above.
test("e2e guard: the real queue, ledger, chats, state folder and vault did not move", (t) => {
  if (!needsEngine(t)) return;
  const now = snapshot();
  const moved = Object.keys(fingerprintsBefore).filter((name) => fingerprintsBefore[name] !== now[name]);
  assert.deepEqual(
    moved,
    [],
    `real Glitch state changed during the e2e run: ${moved.join(", ")} ` +
      "(a live Glitch session writing in the same seconds would also do this: rerun; if it repeats, it is the test)",
  );
});
