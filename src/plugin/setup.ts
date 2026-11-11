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
//
// CP4 adds memory-out: `registerCapture(ctx, state, deps)` subscribes to the host's event stream
// (`ctx.event.subscribe()` with no arguments, the wildcard form measured in docs/api-findings.md
// items 10 and 17) and runs bridge `capture` at every turn end and after every compaction.
//
// MESSAGES_PERSIST=no: the per-message text is re-pushed on every model call of its turn, and the
// session text is rebuilt after a compaction (the same event stream, via EventHooks) and on a new
// local day; a resumed session's first build says source "resume" (history in `messages`, or the
// `session.get` `outcome` marker when a compaction left only one message).

import * as fs from "node:fs";
import * as nodePath from "node:path";

import { callBridge as realCallBridge, resolveBridgePath as realResolveBridgePath } from "./bridge-client.ts";
import type { BridgeOutcome, CallOptions, Mode } from "./bridge-client.ts";
import { record as realRecord, takePendingNotice } from "./failure.ts";
import { findGlitchRoot as realFindGlitchRoot, realpathLoose } from "./root.ts";
import {
  appendNoteToResult,
  pushContext,
  toBridgeCapture,
  toBridgePost,
  toBridgePre,
  toBridgePrompt,
  localDay,
  contextHasHistory,
  sessionHadHistory,
  sessionResultOf,
  sourceForFirstContext,
  toBridgeSession,
} from "./translate.ts";
import type {
  CaptureReason,
  ContextEvent,
  ExecuteAfterEvent,
  ExecuteBeforeEvent,
  PromptEvent,
  SessionSource,
} from "./translate.ts";

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
  /** `subscribe()` with no arguments is the wildcard: an async iterable of every host event (measured). */
  event?: { subscribe?: (...args: unknown[]) => unknown };
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
  /** Non-strict realpath for a session's own directory (root.ts does the same for the start folder). */
  realpath: (p: string) => string;
  /** The wall clock in ms (tests pin it). Only used for the day-change check; optional for old fakes. */
  now?: () => number;
}

export const defaultDeps: SetupDeps = {
  callBridge: realCallBridge,
  findGlitchRoot: realFindGlitchRoot,
  failure: { record: (reason, sessionId) => realRecord(reason, sessionId) },
  resolveBridgePath: () => realResolveBridgePath(),
  warn: (line) => console.error(line),
  realpath: (p) => realpathLoose(p, nodePath, (x) => fs.realpathSync.native(x)),
  now: () => Date.now(),
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
  /** The same lookup, telling "primary" apart from "could not tell" (capture needs the difference). */
  sessionInfo: SessionInfo;
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

  const sessionInfo = makeSessionInfo(ctx);
  const state: GlitchState = {
    root,
    sessionDir,
    bridgePath,
    parentIdOf: parentLookupFrom(sessionInfo),
    sessionInfo,
  };

  registerToolGuards(ctx, state, d);
  const memoryIn = registerMemoryIn(ctx, state, d);
  registerCapture(ctx, state, d, { onCompactionReceived: memoryIn.noteCompaction, onCompaction: memoryIn.onCompaction });
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

/** What is known about one session: its parent ("" = primary) and its own directory, if given. */
export type SessionLookup = { ok: true; parentId: string; directory: string | undefined } | { ok: false };

export interface SessionInfo {
  /** ctx.session.get, bounded by PARENT_LOOKUP_TIMEOUT_MS. Only a successful lookup is cached. */
  lookup: (sessionID: string) => Promise<SessionLookup>;
  /** Fill the cache from a `session.created` event's data (it carries `parentID` for a child). */
  seed: (data: unknown) => void;
  /**
   * Whether the session already had activity before this process (the `session.get` `outcome`
   * marker, translate.ts sessionHadHistory). Answered from a cached real `get` when one exists
   * (the prompt hook's parent lookup usually made it), else ONE fresh bounded `get` per session id
   * per instance (a `session.created` seed carries no marker). Never rejects and records nothing:
   * a missing field is `false`; a failed, timed-out or impossible lookup is `null` (could not tell),
   * which the caller treats as `false` (the old behaviour).
   */
  history: (sessionID: string) => Promise<boolean | null>;
}

function directoryOf(info: unknown): string | undefined {
  const dir = (info as { location?: { directory?: unknown } } | null | undefined)?.location?.directory;
  return typeof dir === "string" && dir !== "" ? dir : undefined;
}

/** A cached lookup; `hadHistory` is known only from a real `get` (undefined from a seed). */
type CachedLookup = { ok: true; parentId: string; directory: string | undefined; hadHistory?: boolean };

/** Per-instance cache of sessionID -> {parentId, directory}. Failures (reject, timeout, no get) are not cached. */
export function makeSessionInfo(ctx: PluginCtx): SessionInfo {
  const cache = new Map<string, CachedLookup>();
  const histories = new Map<string, Promise<boolean | null>>();

  /** One bounded `ctx.session.get`; caches and returns the value, or null on any failure. */
  const fetchInfo = async (sessionID: string): Promise<CachedLookup | null> => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const get = ctx?.session?.get;
      if (typeof get !== "function") return null;
      const timeout = new Promise<"timeout">((resolve) => {
        timer = setTimeout(() => resolve("timeout"), PARENT_LOOKUP_TIMEOUT_MS);
      });
      const info = await Promise.race([get.call(ctx.session, { sessionID }), timeout]);
      if (info === "timeout" || info === null || typeof info !== "object") return null;
      const parent = (info as { parentID?: unknown }).parentID;
      const value: CachedLookup = {
        ok: true,
        parentId: typeof parent === "string" ? parent : "",
        directory: directoryOf(info),
        hadHistory: sessionHadHistory(info),
      };
      cache.set(sessionID, value);
      return value;
    } catch {
      return null;
    } finally {
      if (timer !== undefined) clearTimeout(timer);
    }
  };

  return {
    seed: (data: unknown): void => {
      try {
        const sid = (data as { sessionID?: unknown } | null)?.sessionID;
        if (typeof sid !== "string" || sid === "") return;
        const parent = (data as { parentID?: unknown }).parentID;
        cache.set(sid, { ok: true, parentId: typeof parent === "string" ? parent : "", directory: directoryOf(data) });
      } catch {
        // a malformed event teaches nothing; the lookup still runs
      }
    },
    lookup: async (sessionID: string): Promise<SessionLookup> => {
      if (typeof sessionID !== "string" || sessionID === "") return { ok: false };
      const hit = cache.get(sessionID);
      if (hit !== undefined) return { ok: true, parentId: hit.parentId, directory: hit.directory };
      const got = await fetchInfo(sessionID);
      return got === null ? { ok: false } : { ok: true, parentId: got.parentId, directory: got.directory };
    },
    history: (sessionID: string): Promise<boolean | null> => {
      if (typeof sessionID !== "string" || sessionID === "") return Promise.resolve(null);
      const known = cache.get(sessionID)?.hadHistory;
      if (known !== undefined) return Promise.resolve(known);
      let pending = histories.get(sessionID);
      if (pending === undefined) {
        pending = fetchInfo(sessionID).then(
          (got) => (got === null ? null : got.hadHistory === true),
          () => null,
        );
        histories.set(sessionID, pending); // at most ONE fresh get per session id per instance
      }
      return pending;
    },
  };
}

/** The guards' view: the parent id, "" for a primary session OR when it cannot be told (fail-open). */
function parentLookupFrom(info: SessionInfo): (sessionID: unknown) => Promise<string> {
  return async (sessionID: unknown): Promise<string> => {
    if (typeof sessionID !== "string" || sessionID === "") return "";
    const found = await info.lookup(sessionID);
    return found.ok ? found.parentId : "";
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

/** The one sentence put before the per-message text on the 2nd and later model calls of a turn. */
export const REPEAT_FRAMING = "You were already told this earlier in this turn; do not act on it twice.";

/** The zone the day-change check uses when the bridge names none (the engine's own zone). */
export const DEFAULT_TIMEZONE = "Europe/London";

/** What memory-in hands the rest of the plugin (the capture's event stream drives `onCompaction`). */
export interface MemoryIn {
  /**
   * A `session.compaction.ended` for this session, SYNCHRONOUSLY at event receipt (before the
   * compact capture is awaited): a first build that starts from here on uses source "compact",
   * and an existing session gets ONE compact rebuild queued, held until `onCompaction`.
   */
  noteCompaction: (sessionID: string) => void;
  /**
   * The compaction's capture has finished: release the rebuild `noteCompaction` held. Called alone
   * (no note first), it records the compaction itself, as before.
   */
  onCompaction: (sessionID: string) => void;
  /** Resolves once no session-text build is running or queued (tests, shutdown). */
  settled: () => Promise<void>;
}

/** The per-message text of the current turn and how many model calls have carried it. */
type TurnStash = { text: string; shown: number };

/** One session's session-start text and what drives its rebuilds. */
type SessionEntry = {
  /** The last good text. A failed or empty rebuild never replaces it. */
  text: string;
  /** The bridge's local build date "YYYY-MM-DD" from the last answer that carried one. */
  date: string | null;
  /** The zone the bridge named on its last answer that carried one, else DEFAULT_TIMEZONE is used. */
  timezone: string | null;
  /** The local day the plugin last checked or asked about: at most one day refresh per value. */
  askedDay: string | null;
  /** The first build's source, until the runner takes it. */
  initial: SessionSource | null;
  /** The first build's source is still being decided (the history lookup): the runner waits. */
  deciding: boolean;
  /** The history lookup could not tell (failed or timed out): the first build skips its parent lookup. */
  lookupFailed: boolean;
  /** Resolves once the first build has been tried (the first context call waits for it). */
  ready: Promise<void>;
  /** The one build running for this session (single flight), or null. */
  inflight: Promise<void> | null;
  pendingCompact: boolean;
  /** Compactions noted at receipt whose capture has not finished: the compact rebuild waits for them. */
  compactHeld: number;
  pendingRefresh: boolean;
  /** A day refresh failed: the next member message lets the next context call ask again. */
  refreshFailed: boolean;
};

/**
 * session.prompt -> bridge `prompt` on EVERY member message (it stamps the member turn even when it
 * returns nothing). The session's previous stash is dropped FIRST; any returned text becomes the
 * new stash and is kept for the WHOLE turn.
 * session.context -> pushes, on EVERY call (MESSAGES_PERSIST=no and system pushes do not persist,
 * findings 6d and 13): the pending visible-failure line, the session-start text, and the turn's
 * stash (unchanged on the first call of the turn, behind REPEAT_FRAMING on every later one).
 * The member's own message is never modified.
 *
 * Session-start text: built once per session id in this process at its first `context` call
 * (source "resume" when the session already holds history there, see sourceForFirstContext), then
 * rebuilt in the background on a `session.compaction.ended` (source "compact") or a new local day
 * (refresh). One build at a time per session; calls during a rebuild keep pushing the old text,
 * and a failed or empty rebuild keeps it (the failure is recorded, so it shows as a notice).
 *
 * Day change, the choice and its limits: the plugin cannot ask the bridge for "today" on every
 * model call (one `uv` spawn each), so it asks only when a cheap local check says a new day may
 * have started: the day of now() in the bridge's zone (the `timezone` its `session` answer names,
 * the engine's own clock zone; DEFAULT_TIMEZONE "Europe/London" only when it names none or the
 * host's Intl data does not know it) differs from BOTH the bridge's last `date` and the last day
 * it asked about. The bridge's answer `date` is then stored; the plugin never decides the date.
 * Limits: (1) an engine clock with no IANA zone name (a bare fixed offset) sends no zone, so that
 * member is refreshed at London midnight, not their own; (2) at most one refresh per local-day
 * value, so a refresh that returns the same date is not repeated until the next local day;
 * (3) a failed refresh is retried only after
 * the next member message, not on every model call; (4) the call that notices the new day still
 * pushes the old text (the rebuild runs in the background), the next call carries the new one;
 * (5) the host's process clock and Intl zone data are trusted.
 */
export function registerMemoryIn(ctx: PluginCtx, state: GlitchState, d: SetupDeps): MemoryIn {
  const opts: CallOptions = state.bridgePath === undefined ? {} : { bridgePath: state.bridgePath };
  const sessions = new Map<string, SessionEntry>();
  const stash = new Map<string, TurnStash>();
  /** Sessions whose compaction ended before their first context call here (rare; tiny). */
  const compactedEarly = new Set<string>();
  /**
   * Compactions noted at receipt while the session had no entry here, whose capture has not
   * finished: compactedEarly (or the compact first build it caused) already covers each one, so
   * the matching `onCompaction` must not queue a second build (docs/api-findings.md item 22).
   */
  const notedEarly = new Map<string, number>();
  const now = (): number => (typeof d.now === "function" ? d.now() : Date.now());
  /** Today in the bridge's zone; DEFAULT_TIMEZONE when it named none or the host's Intl data lacks it. */
  const todayFor = (entry: SessionEntry): string | null => {
    const at = now();
    return (entry.timezone !== null ? localDay(at, entry.timezone) : null) ?? localDay(at, DEFAULT_TIMEZONE);
  };

  /** One bridge `session` call. True when the bridge answered (a {ok:false} is recorded by callBridge). */
  const build = async (sid: string, entry: SessionEntry, source: SessionSource, refresh: boolean): Promise<boolean> => {
    // The history lookup just failed for this session: the parent lookup would wait the same
    // bound again for the same answer, so the first build sends "" (what a failed lookup gives).
    const skipLookup = entry.lookupFailed;
    entry.lookupFailed = false;
    const parentId = skipLookup ? "" : await state.parentIdOf(sid);
    const outcome = await d.callBridge(state.root, "session", toBridgeSession(sid, state.sessionDir, parentId, source, refresh), opts);
    if (!outcome || !outcome.ok) return false;
    const got = sessionResultOf(outcome.result);
    if (got.date !== null) entry.date = got.date;
    if (got.timezone !== null) entry.timezone = got.timezone;
    if (got.text !== "") entry.text = got.text; // an empty answer never blanks a session
    return true;
  };

  /** The single-flight runner: the first build, then any queued compaction or day refresh, in turn. */
  const drain = (sid: string, entry: SessionEntry, markReady: () => void): void => {
    if (entry.inflight !== null || entry.deciding) return;
    let finished = false;
    const run = (async () => {
      try {
        for (;;) {
          if (entry.initial !== null) {
            const source = entry.initial;
            entry.initial = null;
            try {
              await build(sid, entry, source, false);
            } catch {
              recordPluginError(d, "session", { sessionID: sid });
            }
            // The first build is today's in the zone it just named (askedDay was stamped before
            // the zone was known, in DEFAULT_TIMEZONE): without this, London's date could hide
            // the bridge zone's own next midnight.
            entry.askedDay = todayFor(entry);
            markReady();
            continue;
          }
          if (!entry.pendingCompact && !entry.pendingRefresh) break;
          // The pre-compaction chat is captured before the compact rebuild runs (onCompaction drains).
          if (entry.pendingCompact && entry.compactHeld > 0) break;
          const compact = entry.pendingCompact;
          const refresh = entry.pendingRefresh;
          entry.pendingCompact = false;
          entry.pendingRefresh = false;
          let ok = false;
          try {
            ok = await build(sid, entry, compact ? "compact" : "startup", refresh);
          } catch {
            recordPluginError(d, "session rebuild", { sessionID: sid });
          }
          if (!ok && refresh) entry.refreshFailed = true;
        }
      } finally {
        markReady();
        entry.inflight = null;
        finished = true;
      }
    })();
    if (!finished) entry.inflight = run;
  };

  const entryFor = (sid: string, ev: ContextEvent): SessionEntry => {
    const found = sessions.get(sid);
    if (found !== undefined) return found;
    let markReady: () => void = () => {};
    const ready = new Promise<void>((r) => {
      markReady = r;
    });
    const early = compactedEarly.delete(sid);
    // Decided at once when the call itself tells: an in-time compaction, or history in `messages`.
    const known: SessionSource | null = early ? "compact" : contextHasHistory(ev) ? "resume" : null;
    const entry: SessionEntry = {
      text: "",
      date: null,
      timezone: null,
      askedDay: null,
      initial: known,
      deciding: known === null,
      lookupFailed: false,
      ready,
      inflight: null,
      pendingCompact: false,
      compactHeld: 0,
      pendingRefresh: false,
      refreshFailed: false,
    };
    entry.askedDay = todayFor(entry); // the first build is today's: no refresh until the day moves
    sessions.set(sid, entry);
    if (known !== null) {
      drain(sid, entry, markReady);
      return entry;
    }
    // ONE message: after a compaction the host shows one message although the chat has history
    // (docs/api-findings.md items 22, 25), and the compaction event can reach the plugin only after
    // this call. The `session.get` history marker tells a chat with history from a new one.
    void (async () => {
      let hadHistory = false;
      try {
        const told = await state.sessionInfo.history(sid);
        hadHistory = told === true;
        entry.lookupFailed = told === null;
      } catch {
        hadHistory = false; // fail open to the old rule, silently
      }
      try {
        entry.initial = sourceForFirstContext(ev, hadHistory);
        if (entry.pendingCompact) {
          // A compaction noted while the lookup ran wins: the first build IS the compact one, and
          // its pending capture-finished signal must not queue a second build.
          entry.initial = "compact";
          entry.pendingCompact = false;
          if (entry.compactHeld > 0) {
            notedEarly.set(sid, (notedEarly.get(sid) ?? 0) + entry.compactHeld);
            entry.compactHeld = 0;
          }
        }
      } catch {
        entry.initial = "startup";
      } finally {
        entry.deciding = false;
        drain(sid, entry, markReady);
      }
    })();
    return entry;
  };

  const noteCompaction = (sid: string): void => {
    try {
      if (typeof sid !== "string" || sid === "") return;
      const entry = sessions.get(sid);
      if (entry === undefined) {
        // The host goes straight on to the model call; its context call must already see this.
        compactedEarly.add(sid);
        notedEarly.set(sid, (notedEarly.get(sid) ?? 0) + 1);
        return;
      }
      entry.pendingCompact = true;
      entry.compactHeld++;
    } catch {
      recordPluginError(d, "session rebuild", { sessionID: sid });
    }
  };

  const onCompaction = (sid: string): void => {
    try {
      if (typeof sid !== "string" || sid === "") return;
      const early = notedEarly.get(sid) ?? 0;
      if (early > 0) {
        if (early === 1) notedEarly.delete(sid);
        else notedEarly.set(sid, early - 1);
        return; // recorded at receipt: the first build is (or will be) the compact one
      }
      const entry = sessions.get(sid);
      if (entry === undefined) {
        compactedEarly.add(sid);
        return;
      }
      if (entry.compactHeld > 0) {
        entry.compactHeld--; // the rebuild was queued at receipt; it may run now
      } else {
        entry.pendingCompact = true;
      }
      drain(sid, entry, () => {});
    } catch {
      recordPluginError(d, "session rebuild", { sessionID: sid });
    }
  };

  const settled = async (): Promise<void> => {
    for (;;) {
      const busy = [...sessions.values()].map((e) => e.inflight).filter((p) => p !== null);
      if (busy.length === 0) return;
      await Promise.all(busy);
    }
  };

  register(
    ctx?.session,
    "prompt",
    async (ev: PromptEvent) => {
      const sid = typeof ev?.sessionID === "string" ? ev.sessionID : "";
      // FIRST, before anything can fail: a quiet or failed turn never re-shows last turn's card.
      if (sid !== "") stash.delete(sid);
      try {
        const entry = sessions.get(sid);
        if (entry !== undefined && entry.refreshFailed) {
          entry.refreshFailed = false;
          entry.askedDay = null; // lets the next context call ask about the day again
        }
        const parentId = await state.parentIdOf(sid);
        const text = contextOf(await d.callBridge(state.root, "prompt", toBridgePrompt(ev, state.sessionDir, parentId), opts));
        if (text !== "" && sid !== "") stash.set(sid, { text, shown: 0 });
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
        const entry = entryFor(sid, ev);
        await entry.ready; // the first build only; later rebuilds never hold a model call
        const today = todayFor(entry);
        if (today !== null && today !== entry.date && today !== entry.askedDay) {
          entry.askedDay = today;
          entry.pendingRefresh = true;
          drain(sid, entry, () => {});
        }
        const parts: string[] = [];
        const notice = takePendingNotice(sid);
        if (notice !== null) parts.push(notice);
        if (entry.text !== "") parts.push(entry.text);
        const turn = stash.get(sid);
        if (turn !== undefined) {
          parts.push(turn.shown === 0 ? turn.text : `${REPEAT_FRAMING}\n\n${turn.text}`);
          turn.shown++;
        }
        if (parts.length > 0) pushContext(ev, parts.join("\n\n"));
      } catch {
        recordPluginError(d, "context", ev);
      }
    },
    d,
    "the memory injection (session context)",
  );

  return { noteCompaction, onCompaction, settled };
}

// ---------------------------------------------------------------------------------------------
// Memory-out: capture (CP4).

/** Turn end (docs/api-findings.md 10a, 12): `succeeded` is measured; `failed`/`interrupted` are named in the binary. */
export const TURN_END_EVENTS: readonly string[] = Object.freeze([
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
]);

/** COMPACTION_SIGNAL (docs/api-findings.md 14a). */
export const COMPACTION_EVENT = "session.compaction.ended";

/** Carries `parentID` for a child session (docs/api-findings.md 17); may be missed for the primary. */
export const SESSION_CREATED_EVENT = "session.created";

/** The testable seam: what the event stream drives, callable without the host. */
export interface CaptureHandler {
  /**
   * One host event, as the wildcard stream delivers it (`{id, created, type, durable, data}`).
   * A turn end resolves at once (the capture runs in the background); a compaction resolves only
   * once the capture covering it has finished. Never rejects.
   */
  handleEvent: (ev: unknown) => Promise<void>;
  /** Run (or coalesce) a capture for one session. Resolves when the run covering it ends. Never rejects. */
  capture: (sessionID: string, reason: CaptureReason) => Promise<void>;
  /** Resolves once no capture is running or pending for any session (tests, shutdown). */
  settled: () => Promise<void>;
}

/**
 * Other listeners on the ONE event subscription (there is never a second `subscribe()`).
 * `onCompactionReceived` runs synchronously when the compaction event arrives, before the capture
 * is awaited (the host's next model call does not wait for it; docs/api-findings.md item 22);
 * `onCompaction` runs after the compaction's capture has finished; memory-in rebuilds there.
 */
export interface EventHooks {
  onCompactionReceived?: (sessionID: string) => void;
  onCompaction?: (sessionID: string) => void;
}

type Pending = { reason: CaptureReason; promise: Promise<void>; resolve: () => void };
type Slot = { running: Promise<void> | null; next: Pending | null };

/**
 * Build the capture logic for one plugin instance.
 *
 * Coalescing, per session: at most ONE bridge `capture` in flight plus at most ONE pending re-run.
 * An event that arrives while a run is in flight joins the pending re-run (a compaction upgrades
 * its reason to `compact`); the re-run starts when the in-flight one ends, so it reads the chat as
 * it stands after the last event. Sessions never wait on each other.
 *
 * Child sessions (a `parentID`) are skipped. A session whose parent cannot be told (lookup
 * rejected, timed out, or no `session.get`) is skipped AND recorded visibly; the failed lookup is
 * not cached, so the next event retries it. Only a successful lookup with no parent is primary.
 */
export function createCapture(state: GlitchState, d: SetupDeps, hooks: EventHooks = {}): CaptureHandler {
  const opts: CallOptions = state.bridgePath === undefined ? {} : { bridgePath: state.bridgePath };
  const slots = new Map<string, Slot>();

  const sessionDirFor = (directory: string | undefined): string => {
    if (directory === undefined) return state.sessionDir;
    try {
      const real = d.realpath(directory);
      return typeof real === "string" && real !== "" ? real : directory;
    } catch {
      return directory;
    }
  };

  const runOnce = async (sid: string, reason: CaptureReason): Promise<void> => {
    try {
      const info = await state.sessionInfo.lookup(sid);
      if (!info.ok) {
        recordPluginError(d, "capture (session lookup)", { sessionID: sid });
        return;
      }
      if (info.parentId !== "") return; // a sub-agent's chat is not the member's chat
      // {ok:false} is recorded by callBridge itself (visible failure); nothing to add here.
      await d.callBridge(state.root, "capture", toBridgeCapture(sid, sessionDirFor(info.directory), info.parentId, reason), opts);
    } catch {
      recordPluginError(d, "capture", { sessionID: sid });
    }
  };

  const start = (sid: string, reason: CaptureReason, slot: Slot): Promise<void> =>
    runOnce(sid, reason).then(() => {
      try {
        const next = slot.next;
        slot.next = null;
        if (next === null) {
          slot.running = null;
          if (slots.get(sid) === slot) slots.delete(sid);
          return;
        }
        const rerun = start(sid, next.reason, slot);
        slot.running = rerun;
        void rerun.then(next.resolve, next.resolve);
      } catch {
        recordPluginError(d, "capture", { sessionID: sid });
      }
    });

  const capture = (sid: string, reason: CaptureReason): Promise<void> => {
    try {
      let slot = slots.get(sid);
      if (slot === undefined) {
        slot = { running: null, next: null };
        slots.set(sid, slot);
      }
      if (slot.running === null) {
        const run = start(sid, reason, slot);
        slot.running = run;
        return run;
      }
      if (slot.next === null) {
        let resolve: () => void = () => {};
        const promise = new Promise<void>((r) => {
          resolve = r;
        });
        slot.next = { reason, promise, resolve };
      } else if (reason === "compact") {
        slot.next.reason = "compact";
      }
      return slot.next.promise;
    } catch {
      recordPluginError(d, "capture", { sessionID: sid });
      return Promise.resolve();
    }
  };

  const handleEvent = async (ev: unknown): Promise<void> => {
    let sid: unknown;
    try {
      const e = ev as { type?: unknown; data?: unknown; properties?: unknown } | null;
      const type = e?.type;
      if (typeof type !== "string") return;
      const data = (e?.data ?? e?.properties) as { sessionID?: unknown } | null | undefined;
      if (type === SESSION_CREATED_EVENT) {
        state.sessionInfo.seed(data);
        return;
      }
      sid = data?.sessionID;
      if (typeof sid !== "string" || sid === "") return;
      if (TURN_END_EVENTS.includes(type)) {
        void capture(sid, "turn"); // never delays the member: not awaited, and it never rejects
        return;
      }
      if (type === COMPACTION_EVENT) {
        // FIRST, before any await: a context call that runs while the capture is in flight must
        // already build with source "compact". Its own try: it can never stop the capture.
        try {
          hooks.onCompactionReceived?.(sid);
        } catch {
          recordPluginError(d, "session rebuild", { sessionID: sid });
        }
        await capture(sid, "compact"); // awaited: the stage lands before the next turn runs on the summary
        // Then the session text is rebuilt (source "compact"), the order Claude Code keeps
        // (PreCompact capture, then SessionStart compact). Not awaited: it runs in the background,
        // single flight, and context calls keep the old text meanwhile. Its own try, so a throw
        // here can never be mistaken for a capture failure.
        try {
          hooks.onCompaction?.(sid as string);
        } catch {
          recordPluginError(d, "session rebuild", { sessionID: sid });
        }
      }
    } catch {
      recordPluginError(d, "capture", { sessionID: sid });
    }
  };

  const settled = async (): Promise<void> => {
    for (;;) {
      const busy = [...slots.values()].flatMap((s) => [s.running, s.next?.promise]).filter((p) => p != null);
      if (busy.length === 0) return;
      await Promise.all(busy);
    }
  };

  return { handleEvent, capture, settled };
}

/**
 * Subscribe the capture to the host's events: `ctx.event.subscribe()` with NO arguments, the
 * wildcard form the probe proved (it resolves to an async iterable of every event; findings 10, 17).
 * An observable-shaped answer (`.subscribe(fn)`) is accepted too (not measured).
 * A missing or refusing subscribe is recorded and warned, never thrown: capture is then OFF.
 * Returns the handler (also when the subscription failed) so callers and tests can drive it.
 */
export function registerCapture(ctx: PluginCtx, state: GlitchState, d: SetupDeps, hooks: EventHooks = {}): CaptureHandler {
  const handler = createCapture(state, d, hooks);
  const off = (): void => {
    try {
      const reason = d.failure.record("event subscription failed: capture");
      d.warn(`${LOG_PREFIX} WARNING: could not subscribe to OpenCode events (${reason}); chats are NOT captured into memory.`);
    } catch {
      // the warning must never stop setup
    }
  };

  let subscription: unknown;
  try {
    const events = ctx?.event;
    if (!events || typeof events.subscribe !== "function") throw new Error("no subscribe function");
    subscription = events.subscribe();
  } catch {
    off();
    return handler;
  }

  void (async () => {
    try {
      let stream: any = subscription;
      if (stream && typeof stream.then === "function") stream = await stream;
      if (stream && typeof stream[Symbol.asyncIterator] === "function") {
        for await (const ev of stream) await handler.handleEvent(ev);
        return; // the stream ends when the host shuts the location down (measured)
      }
      if (stream && typeof stream.subscribe === "function") {
        stream.subscribe((ev: unknown) => {
          void handler.handleEvent(ev);
        });
        return;
      }
      off();
    } catch {
      off();
    }
  })();

  return handler;
}
