/**
 * bridge-client.ts - spawns the Python bridge once per hook call and reads its one-object answer.
 *
 * Default invocation (argv array, never a shell):
 *
 *     uv run --no-project --directory <root>/.claude/scripts python <bridgePath> <mode>
 *
 * The JSON payload goes on stdin; stdout is the bridge's contract: nothing (allowed / no context /
 * inert) or ONE JSON object `{deny}` (pre) | `{note}` (post) | `{context}` (session, prompt) |
 * `{error}` (any mode: an allow plus a visible failure).
 *
 * Every failure returns `{ok:false, reason}` and is recorded through `failure.record` (log line +
 * one pending notice for the session). Reasons are fixed short strings; they never contain the
 * payload or any stdout/stderr text (both can carry secrets).
 *
 * Seams for tests: `opts.command` replaces `uv`, `opts.buildArgs` replaces the argv builder, so a
 * test runs `node fake-bridge.mjs <mode>` without monkeypatching.
 */

import { spawn } from "node:child_process";
import { basename, join } from "node:path";

import { cleanReason, record } from "./failure.ts";

export type Mode = "pre" | "post" | "session" | "prompt";

export interface DenyResult {
  deny: { kind: string; member: string; model: string };
}
export interface NoteResult {
  note: string;
}
export interface ContextResult {
  context: string;
}
export type BridgeResult = DenyResult | NoteResult | ContextResult;

export type BridgeOutcome = { ok: true; result: BridgeResult | null } | { ok: false; reason: string };

export type ArgsBuilder = (root: string, bridgePath: string, mode: Mode) => string[];

export interface CallOptions {
  /** Kill the bridge after this long. Defaults per mode (DEFAULT_TIMEOUTS). */
  timeoutMs?: number;
  /** The bridge script. Defaults to resolveBridgePath() (override env, else the workspace copy). */
  bridgePath?: string;
  /** The executable. Defaults to "uv". */
  command?: string;
  /** The argv after the command. Defaults to uvArgs. */
  buildArgs?: ArgsBuilder;
  /** The child's environment base (copied; PYTHONDONTWRITEBYTECODE=1 is added). Defaults to process.env. */
  env?: NodeJS.ProcessEnv;
  /** Session to flag for the visible notice. Defaults to payload.session_id. */
  sessionId?: string;
  /** Where failure lines go (tests). Defaults to failure.errorsLogPath(). */
  logPath?: string;
}

export const DEFAULT_TIMEOUTS: Readonly<Record<Mode, number>> = Object.freeze({
  pre: 10_000,
  post: 10_000,
  session: 30_000,
  prompt: 10_000,
});

export const BRIDGE_OVERRIDE_ENV = "GLITCH_OPENCODE_BRIDGE_OVERRIDE";

/** stdout larger than this is not the bridge talking: treated as bad output. */
export const MAX_STDOUT_BYTES = 8 * 1024 * 1024;

const MODES: readonly Mode[] = ["pre", "post", "session", "prompt"];

/** The default argv: `run --no-project --directory <root>/.claude/scripts python <bridge> <mode>`. */
export const uvArgs: ArgsBuilder = (root, bridgePath, mode) => [
  "run",
  "--no-project",
  "--directory",
  join(root, ".claude", "scripts"),
  "python",
  bridgePath,
  mode,
];

/** This file's folder, under Bun (import.meta.dir) or Node >= 20.11 (import.meta.dirname). */
function pluginDir(): string {
  const meta = import.meta as unknown as { dirname?: string; dir?: string };
  const dir = meta.dirname ?? meta.dir;
  if (typeof dir !== "string" || dir === "") {
    throw new Error("cannot resolve the plugin folder");
  }
  return dir;
}

/** The committed bridge, relative to this plugin file: `<workspace>/src/bridge/opencode_bridge.py`. */
export function defaultBridgePath(): string {
  return join(pluginDir(), "..", "bridge", "opencode_bridge.py");
}

/**
 * The bridge to run: the TEST-ONLY override env var if set (a loud line on stderr EVERY time it is
 * resolved, so a forgotten override can never pass unnoticed), else the default.
 * The plugin resolves this once per start and passes it as `opts.bridgePath`.
 */
export function resolveBridgePath(
  env: NodeJS.ProcessEnv = process.env,
  warn: (line: string) => void = (line) => console.error(line),
): string {
  const override = env[BRIDGE_OVERRIDE_ENV];
  if (typeof override === "string" && override.trim() !== "") {
    warn(
      `[glitch-opencode] WARNING: ${BRIDGE_OVERRIDE_ENV} is set (TEST-ONLY): the guards run ` +
        `${override} instead of the real bridge. Unset it for real use.`,
    );
    return override;
  }
  return defaultBridgePath();
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

type Parsed = { kind: "result"; result: BridgeResult | null } | { kind: "error"; label: string } | { kind: "bad" };

/** Validate stdout against the contract for this mode. */
export function parseBridgeOutput(stdout: string, mode: Mode): Parsed {
  const text = stdout.replace(/^﻿/, "").trim();
  if (text === "") return { kind: "result", result: null };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { kind: "bad" };
  }
  if (!isPlainObject(value)) return { kind: "bad" };
  const keys = Object.keys(value);
  if (keys.length !== 1) return { kind: "bad" };
  const key = keys[0];
  const body = value[key];
  if (key === "error") {
    return typeof body === "string" && body !== "" ? { kind: "error", label: body } : { kind: "bad" };
  }
  if (key === "deny" && mode === "pre" && isPlainObject(body)) {
    const { kind, member, model } = body;
    if (typeof kind === "string" && typeof member === "string" && typeof model === "string" && model !== "") {
      return { kind: "result", result: { deny: { kind, member, model } } };
    }
    return { kind: "bad" };
  }
  if (key === "note" && mode === "post" && typeof body === "string") {
    return { kind: "result", result: body === "" ? null : { note: body } };
  }
  if (key === "context" && (mode === "session" || mode === "prompt") && typeof body === "string") {
    return { kind: "result", result: body === "" ? null : { context: body } };
  }
  return { kind: "bad" };
}

function killTree(child: ReturnType<typeof spawn>): void {
  try {
    if (process.platform !== "win32" && typeof child.pid === "number") {
      process.kill(-child.pid, "SIGKILL"); // the whole group: uv AND the python it started
      return;
    }
  } catch {
    // fall through to the direct kill
  }
  try {
    child.kill("SIGKILL");
  } catch {
    // already gone
  }
}

function sessionOf(payload: unknown): string | undefined {
  if (isPlainObject(payload) && typeof payload.session_id === "string" && payload.session_id !== "") {
    return payload.session_id;
  }
  return undefined;
}

/**
 * Run the bridge once. Never rejects: every failure resolves `{ok:false, reason}` after recording.
 */
export function callBridge(root: string, mode: Mode, payload: unknown, opts: CallOptions = {}): Promise<BridgeOutcome> {
  const sessionId = opts.sessionId ?? sessionOf(payload);
  const fail = (reason: string): BridgeOutcome => {
    const clean = record(reason, sessionId, { logPath: opts.logPath });
    return { ok: false, reason: clean };
  };

  if (!MODES.includes(mode)) return Promise.resolve(fail("bad mode"));

  let input: string;
  try {
    input = JSON.stringify(payload);
    if (typeof input !== "string") throw new Error("unserialisable");
  } catch {
    return Promise.resolve(fail("bad payload"));
  }

  const command = opts.command ?? "uv";
  const timeoutMs = opts.timeoutMs ?? DEFAULT_TIMEOUTS[mode];

  let bridgePath: string;
  let args: string[];
  try {
    bridgePath = opts.bridgePath ?? resolveBridgePath();
    args = (opts.buildArgs ?? uvArgs)(root, bridgePath, mode);
  } catch {
    return Promise.resolve(fail("bridge path unresolved"));
  }

  return new Promise<BridgeOutcome>((resolve) => {
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const finish = (outcome: BridgeOutcome): void => {
      if (settled) return;
      settled = true;
      if (timer !== undefined) clearTimeout(timer);
      resolve(outcome);
    };
    // Record a failure only if nothing has settled yet: a spawn 'error' is followed by a 'close'
    // (code -2 for ENOENT), and a timeout by the killed child's 'close'; each must count once.
    const failOnce = (reason: string): void => {
      if (settled) return;
      finish(fail(reason));
    };

    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(command, args, {
        env: { ...(opts.env ?? process.env), PYTHONDONTWRITEBYTECODE: "1" },
        stdio: ["pipe", "pipe", "pipe"],
        shell: false,
        windowsHide: true,
        detached: process.platform !== "win32", // own process group, so a timeout kills uv's child too
      });
    } catch (err) {
      const code = (err as { code?: unknown }).code;
      failOnce(code === "ENOENT" ? notFound(command) : `spawn failed: ${safeCode(code)}`);
      return;
    }

    const chunks: Buffer[] = [];
    let size = 0;
    let overflow = false;
    let timedOut = false;

    timer = setTimeout(() => {
      timedOut = true;
      killTree(child);
      failOnce("timeout");
    }, timeoutMs);

    child.on("error", (err: NodeJS.ErrnoException) => {
      failOnce(err.code === "ENOENT" ? notFound(command) : `spawn failed: ${safeCode(err.code)}`);
    });

    child.stdout?.on("data", (chunk: Buffer) => {
      size += chunk.length;
      if (size > MAX_STDOUT_BYTES) {
        if (!overflow) {
          overflow = true;
          killTree(child);
        }
        return;
      }
      chunks.push(chunk);
    });
    child.stderr?.on("data", () => {
      // drained so the child never blocks on a full pipe; never read into a reason
    });
    child.stdin?.on("error", () => {
      // EPIPE when the child exits without reading: the exit code reports the real problem
    });

    child.on("close", (code: number | null, signal: NodeJS.Signals | null) => {
      if (timedOut) return;
      if (overflow) {
        failOnce("bad output");
        return;
      }
      if (code !== 0) {
        failOnce(`exit ${code ?? signal ?? "unknown"}`);
        return;
      }
      const parsed = parseBridgeOutput(Buffer.concat(chunks).toString("utf8"), mode);
      if (parsed.kind === "result") {
        finish({ ok: true, result: parsed.result });
      } else if (parsed.kind === "error") {
        failOnce(`bridge error: ${cleanReason(parsed.label).slice(0, 80)}`);
      } else {
        failOnce("bad output");
      }
    });

    child.stdin?.end(input, "utf8");
  });
}

function notFound(command: string): string {
  const name = basename(command).replace(/\.(exe|cmd|bat)$/i, "");
  return `${name || "command"} not found`;
}

function safeCode(code: unknown): string {
  return typeof code === "string" && /^[A-Z0-9_]{1,32}$/.test(code) ? code : "unknown";
}
