// setup.ts - everything the Glitch OpenCode plugin does at start, as one plain exported function.
//
// index.ts only wraps `setupGlitch` in `Plugin.define` (the host import does not resolve under
// `node --test`, so no test imports index.ts). Authority for every host name used here:
// docs/api-findings.md (CP0, measured on OpenCode v2.2.7).
//
// The rules this file holds:
//   1. The Glitch root is resolved ONCE, from ctx.location.directory (the session's start folder).
//      No root: register NOTHING and return, so other OpenCode projects pay zero cost.
//   2. session_dir sent to the bridge is ALWAYS ctx.location.directory. A tool's own workdir or
//      path arguments are data inside tool_input and never decide whether the guards run.
//   3. A bridge deny blocks by throwing `new Error(<the engine's model words verbatim>)` in
//      `execute.before` (BLOCK_ROUTE=throw, measured: the message reaches the model).
//   4. Anything else that goes wrong (bridge {ok:false}, the plugin's own errors, a session lookup
//      that rejects or hangs) is an ALLOW. callBridge records its own failures; the plugin's own
//      errors are recorded here through `failure.record`, so the fail-open is visible.
//
// CP3 adds memory-in: `registerMemoryIn(ctx, state, deps)` registering ctx.session.hook("prompt")
// and ctx.session.hook("context") is called from setupGlitch next to registerToolGuards, sharing
// the same GlitchState (root, sessionDir, bridgePath, parentIdOf).

import { callBridge as realCallBridge, resolveBridgePath as realResolveBridgePath } from "./bridge-client.ts";
import type { BridgeOutcome, CallOptions, Mode } from "./bridge-client.ts";
import { record as realRecord, takePendingNotice } from "./failure.ts";
import { findGlitchRoot as realFindGlitchRoot } from "./root.ts";
import {
  appendNoteToResult,
  pushContext,
  toBridgePost,
  toBridgePre,
  toBridgePrompt,
  toBridgeSession,
} from "./translate.ts";
import type { ContextEvent, ExecuteAfterEvent, ExecuteBeforeEvent, PromptEvent } from "./translate.ts";

/** The OpenCode version the adapter was measured against (docs/api-findings.md). */
export const SUPPORTED_OPENCODE_VERSION = "2.2.7";

/** A session lookup slower than this is treated as "no parent" so it can never stall a tool. */
export const PARENT_LOOKUP_TIMEOUT_MS = 2_000;

/** Prefix of every line the plugin writes to stderr. */
export const LOG_PREFIX = "[glitch-opencode]";

// ---------------------------------------------------------------------------------------------
// The host surface the plugin touches (a structural subset of OpenCode's ctx).

export type HookFn = (ev: any) => unknown | Promise<unknown>;

export interface HookDomain {
  hook: (name: string, fn: HookFn) => unknown;
}

export interface PluginCtx {
  app?: { version?: unknown };
  location?: { directory?: unknown };
  tool?: HookDomain;
  session?: HookDomain & { get?: (args: { sessionID: string }) => Promise<unknown> };
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------------------------
// Injectable dependencies (tests pass fakes; the host gets the real ones).

export interface SetupDeps {
  callBridge: (root: string, mode: Mode, payload: unknown, opts?: CallOptions) => Promise<BridgeOutcome>;
  findGlitchRoot: (startDir: string) => string | null;
  failure: { record: (reason: string, sessionId?: string) => string };
  resolveBridgePath: () => string;
  warn: (line: string) => void;
}

export const defaultDeps: SetupDeps = {
  callBridge: realCallBridge,
  findGlitchRoot: realFindGlitchRoot,
  failure: { record: (reason, sessionId) => realRecord(reason, sessionId) },
  resolveBridgePath: () => realResolveBridgePath(),
  warn: (line) => console.error(line),
};

/** What every registration shares for the life of this plugin instance. */
export interface GlitchState {
  root: string;
  /** ctx.location.directory, verbatim: the only session_dir ever sent to the bridge. */
  sessionDir: string;
  /** undefined when it could not be resolved: callBridge then resolves (and fails) per call. */
  bridgePath: string | undefined;
  /** The parent session id of a sub-agent session, "" for a primary one or when unknown. */
  parentIdOf: (sessionID: unknown) => Promise<string>;
}

// ---------------------------------------------------------------------------------------------

/**
 * The plugin's setup. Returns nothing; registers nothing outside a Glitch folder.
 * Never throws: a failure here must not stop OpenCode starting.
 */
export async function setupGlitch(ctx: PluginCtx, deps: Partial<SetupDeps> = {}): Promise<void> {
  const d: SetupDeps = { ...defaultDeps, ...deps };

  let sessionDir: unknown;
  let root: string | null = null;
  try {
    sessionDir = ctx?.location?.directory;
    root = typeof sessionDir === "string" && sessionDir !== "" ? d.findGlitchRoot(sessionDir) : null;
  } catch {
    root = null;
  }
  if (root === null || typeof sessionDir !== "string") return; // inert: zero hooks, zero cost

  try {
    const version = ctx?.app?.version;
    if (version !== SUPPORTED_OPENCODE_VERSION) {
      d.warn(
        `${LOG_PREFIX} WARNING: OpenCode ${String(version)} is not the measured ${SUPPORTED_OPENCODE_VERSION}; ` +
          `the Glitch guards still run, but the hook API may have changed.`,
      );
    }
  } catch {
    // a warning must never stop the guards
  }

  let bridgePath: string | undefined;
  try {
    bridgePath = d.resolveBridgePath();
  } catch {
    bridgePath = undefined;
  }

  const state: GlitchState = {
    root,
    sessionDir,
    bridgePath,
    parentIdOf: makeParentLookup(ctx),
  };

  registerToolGuards(ctx, state, d);
  registerMemoryIn(ctx, state, d);
}

// ---------------------------------------------------------------------------------------------

/** Register one hook; a host that refuses the registration is recorded and warned, never thrown. */
export function register(domain: HookDomain | undefined, name: string, fn: HookFn, d: SetupDeps, label: string): boolean {
  try {
    if (!domain || typeof domain.hook !== "function") throw new Error("no hook function");
    domain.hook(name, fn);
    return true;
  } catch {
    const reason = d.failure.record(`hook registration failed: ${label}`);
    d.warn(`${LOG_PREFIX} WARNING: could not register ${label} (${reason}); that Glitch protection is OFF.`);
    return false;
  }
}

/** Per-instance cache of sessionID -> parent id. Only successful lookups are cached. */
function makeParentLookup(ctx: PluginCtx): (sessionID: unknown) => Promise<string> {
  const cache = new Map<string, string>();
  return async (sessionID: unknown): Promise<string> => {
    if (typeof sessionID !== "string" || sessionID === "") return "";
    const hit = cache.get(sessionID);
    if (hit !== undefined) return hit;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const get = ctx?.session?.get;
      if (typeof get !== "function") return "";
      const timeout = new Promise<"timeout">((resolve) => {
        timer = setTimeout(() => resolve("timeout"), PARENT_LOOKUP_TIMEOUT_MS);
      });
      const info = await Promise.race([get.call(ctx.session, { sessionID }), timeout]);
      if (info === "timeout") return "";
      const parent = (info as { parentID?: unknown } | null | undefined)?.parentID;
      const value = typeof parent === "string" ? parent : "";
      cache.set(sessionID, value);
      return value;
    } catch {
      return "";
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  };
}

/** The text a deny throws: the engine's model words verbatim. */
function denyText(deny: { model?: unknown; member?: unknown }): string {
  if (typeof deny.model === "string" && deny.model !== "") return deny.model;
  if (typeof deny.member === "string" && deny.member !== "") return deny.member;
  return "Blocked by a Glitch guard.";
}

/** execute.before -> bridge pre (deny = throw); execute.after -> bridge post (note = appended). */
export function registerToolGuards(ctx: PluginCtx, state: GlitchState, d: SetupDeps): void {
  const opts: CallOptions = state.bridgePath === undefined ? {} : { bridgePath: state.bridgePath };

  register(
    ctx?.tool,
    "execute.before",
    async (ev: ExecuteBeforeEvent) => {
      let block: string | null = null;
      try {
        const parentId = await state.parentIdOf(ev?.sessionID);
        const outcome = await d.callBridge(state.root, "pre", toBridgePre(ev, state.sessionDir, parentId), opts);
        // {ok:false}: allow; callBridge has already recorded the failure.
        if (outcome && outcome.ok && outcome.result && "deny" in outcome.result && outcome.result.deny) {
          block = denyText(outcome.result.deny);
        }
      } catch {
        recordPluginError(d, "pre", ev);
        block = null;
      }
      // Outside the try on purpose: this throw IS the block (BLOCK_ROUTE=throw).
      if (block !== null) throw new Error(block);
    },
    d,
    "the tool guard (execute.before)",
  );

  register(
    ctx?.tool,
    "execute.after",
    async (ev: ExecuteAfterEvent) => {
      try {
        const outcome = await d.callBridge(state.root, "post", toBridgePost(ev, state.sessionDir), opts);
        if (outcome && outcome.ok && outcome.result && "note" in outcome.result && outcome.result.note) {
          if (ev && ev.result !== undefined && ev.result !== null) {
            ev.result = appendNoteToResult(ev.result, outcome.result.note);
          }
        }
      } catch {
        recordPluginError(d, "post", ev);
      }
    },
    d,
    "the tool-output check (execute.after)",
  );
}

function recordPluginError(d: SetupDeps, where: string, ev: unknown): void {
  try {
    const sid = (ev as { sessionID?: unknown } | null)?.sessionID;
    d.failure.record(`plugin error in ${where}`, typeof sid === "string" ? sid : undefined);
  } catch {
    // recording must never turn into a block
  }
}

// ---------------------------------------------------------------------------------------------
// Memory-in (CP3).

/** The text out of a bridge outcome: "" for anything but `{ok:true, result:{context}}`. */
function contextOf(outcome: BridgeOutcome | null | undefined): string {
  if (outcome && outcome.ok && outcome.result && "context" in outcome.result) {
    const text = outcome.result.context;
    if (typeof text === "string") return text;
  }
  return "";
}

/**
 * session.prompt -> bridge `prompt` on EVERY member message (it stamps the member turn even when it
 * returns nothing); any returned text is stashed by session id.
 * session.context -> pushes, on EVERY call (pushes do not persist, findings 6d): the pending
 * visible-failure line, the session-start text (bridge `session`, run once per session id and
 * cached), and the stashed per-prompt text (consumed by this call).
 * The member's own message is never modified.
 */
export function registerMemoryIn(ctx: PluginCtx, state: GlitchState, d: SetupDeps): void {
  const opts: CallOptions = state.bridgePath === undefined ? {} : { bridgePath: state.bridgePath };
  const sessionText = new Map<string, Promise<string>>();
  const stash = new Map<string, string>();

  const loadSessionText = (sid: string): Promise<string> => {
    const cached = sessionText.get(sid);
    if (cached !== undefined) return cached;
    const started = (async () => {
      try {
        const parentId = await state.parentIdOf(sid);
        return contextOf(await d.callBridge(state.root, "session", toBridgeSession(sid, state.sessionDir, parentId), opts));
      } catch {
        recordPluginError(d, "session", { sessionID: sid });
        return "";
      }
    })();
    sessionText.set(sid, started);
    return started;
  };

  register(
    ctx?.session,
    "prompt",
    async (ev: PromptEvent) => {
      try {
        const sid = typeof ev?.sessionID === "string" ? ev.sessionID : "";
        const parentId = await state.parentIdOf(sid);
        const text = contextOf(await d.callBridge(state.root, "prompt", toBridgePrompt(ev, state.sessionDir, parentId), opts));
        if (text !== "" && sid !== "") stash.set(sid, text);
      } catch {
        recordPluginError(d, "prompt", ev);
      }
    },
    d,
    "the per-message memory (session prompt)",
  );

  register(
    ctx?.session,
    "context",
    async (ev: ContextEvent) => {
      try {
        const sid = typeof ev?.sessionID === "string" ? ev.sessionID : "";
        if (sid === "") return;
        const parts: string[] = [];
        const session = await loadSessionText(sid); // first: its failure is noticed below
        const notice = takePendingNotice(sid);
        if (notice !== null) parts.push(notice);
        if (session !== "") parts.push(session);
        const turn = stash.get(sid);
        if (turn !== undefined) {
          stash.delete(sid);
          parts.push(turn);
        }
        if (parts.length > 0) pushContext(ev, parts.join("\n\n"));
      } catch {
        recordPluginError(d, "context", ev);
      }
    },
    d,
    "the memory injection (session context)",
  );
}
