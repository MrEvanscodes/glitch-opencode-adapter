// translate.ts - pure mapping from OpenCode v2.2.7 hook events to the Glitch bridge payloads.
//
// No I/O, no host imports: every function here is a plain data transform, so it runs under
// `node --test` and inside OpenCode's Bun runtime alike.
//
// Authority: docs/api-findings.md (CP0, measured) for the event shapes, and the docstrings of
// src/bridge/opencode_bridge.py, pretool.py, posttool.py and context.py for the payloads.
//
// Two invariants the guards depend on:
//   1. `session_dir` is ALWAYS the argument the caller passes (ctx.location.directory, the
//      session's start folder). It is never read from the event or the tool's input: a shell
//      `workdir: "/tmp"` stays data inside `tool_input` and can never switch the guards off.
//   2. The OpenCode tool id is passed through as `tool_name` unchanged; the Python side maps it.

// ---------------------------------------------------------------------------------------------
// Measured OpenCode event shapes (docs/api-findings.md items 2, 6).

/** `tool.execute.before` payload: `{tool, sessionID, agent, messageID, id, input}` (no cwd field). */
export type ExecuteBeforeEvent = {
  tool: string;
  sessionID: string;
  agent?: string;
  messageID?: string;
  id?: string;
  input?: Record<string, unknown> | string;
};

/** A text part, as OpenCode's tool results and system prompt both carry them. */
export type TextPart = { type: "text"; text: string };

/** `result` of a completed tool: `{output: <tool-specific>, content: [{type:"text", text}], metadata?}`. */
export type ToolResult = {
  output?: unknown;
  content?: Array<TextPart | Record<string, unknown>>;
  metadata?: unknown;
  [key: string]: unknown;
};

/** `tool.execute.after` payload: the before shape plus `status`, `result?`, `error?`. */
export type ExecuteAfterEvent = ExecuteBeforeEvent & {
  status?: string;
  result?: ToolResult;
  error?: unknown;
};

/** `session.prompt` payload: `{sessionID, messageID, prompt: {text, files}, delivery}`. */
export type PromptEvent = {
  sessionID: string;
  messageID?: string;
  prompt?: { text?: string; files?: unknown[] };
  delivery?: string;
};

/** `session.context` payload (the fields the plugin touches). */
export type ContextEvent = {
  sessionID?: string;
  system?: TextPart[];
  options?: { claudeCodeInstructions?: unknown; [key: string]: unknown };
  [key: string]: unknown;
};

// ---------------------------------------------------------------------------------------------
// Bridge payloads (src/bridge/opencode_bridge.py module docstring).

export type BridgePre = {
  tool_name: string;
  tool_input: Record<string, unknown> | string;
  session_dir: string;
  session_id: string;
  parent_id: string;
};

export type BridgePost = {
  tool_name: string;
  session_id: string;
  result: ToolResult | null;
  error: unknown;
  status: string | null;
  session_dir: string;
};

/** Why the session text is being built (the bridge's `source`; it defaults to "startup"). */
export type SessionSource = "startup" | "resume" | "compact";

/**
 * Bridge `session`: `{session_id, session_dir, parent_id, source, refresh}`.
 * `source` is "resume" when the session already held history at its first `context` call in this
 * process, "compact" after a compaction; `refresh` is true when a new local day may have started.
 */
export type BridgeSession = {
  session_id: string;
  session_dir: string;
  parent_id: string;
  source: SessionSource;
  refresh: boolean;
};

/**
 * What a `session` answer carries: `{context, date?, timezone?}`, `date` being the engine's local
 * build date "YYYY-MM-DD" and `timezone` the IANA name of the engine clock's zone that date is in
 * (bridge-client.ts parseSession validates both; either is absent when the engine cannot say).
 */
export type BridgeSessionResult = { context?: string; date?: string; timezone?: string };

export type BridgePrompt = {
  session_id: string;
  prompt: string;
  session_dir: string;
  parent_id: string;
};

/** Why a capture runs (opencode_bridge.py / capture.py REASONS). The plugin sends turn and compact. */
export type CaptureReason = "turn" | "compact" | "start" | "sweep";

/**
 * Bridge `capture`: `{session_id, session_dir, parent_id, reason}`. No `turns` field: the bridge
 * reads the chat itself through `opencode session export` (CAPTURE_SOURCE=export-cli).
 * `parent_id` is "" for a primary session (capture.py requires a string when present).
 */
export type BridgeCapture = {
  session_id: string;
  session_dir: string;
  parent_id: string;
  reason: CaptureReason;
};

// ---------------------------------------------------------------------------------------------
// Mappers.

/** A turn end or compaction for one session -> bridge `capture`. */
export function toBridgeCapture(
  sessionId: string,
  sessionDir: string,
  parentId: string | null | undefined,
  reason: CaptureReason,
): BridgeCapture {
  return {
    session_id: sessionId,
    session_dir: sessionDir,
    parent_id: typeof parentId === "string" ? parentId : "",
    reason,
  };
}

/** `execute.before` -> bridge `pre`. `tool_input` is the event's own `input`, untouched. */
export function toBridgePre(ev: ExecuteBeforeEvent, sessionDir: string, parentId?: string): BridgePre {
  return {
    tool_name: ev.tool,
    tool_input: ev.input ?? {},
    session_dir: sessionDir,
    session_id: ev.sessionID,
    parent_id: parentId ?? "",
  };
}

/** `execute.after` -> bridge `post`. `input` is never sent (posttool.py never reads it). */
export function toBridgePost(ev: ExecuteAfterEvent, sessionDir: string, _parentId?: string): BridgePost {
  return {
    tool_name: ev.tool,
    session_id: ev.sessionID,
    result: ev.result ?? null,
    error: ev.error ?? null,
    status: ev.status ?? null,
    session_dir: sessionDir,
  };
}

/** First `context` call for a session id (or a rebuild) -> bridge `session`. */
export function toBridgeSession(
  sessionId: string,
  sessionDir: string,
  parentId?: string,
  source: SessionSource = "startup",
  refresh = false,
): BridgeSession {
  return { session_id: sessionId, session_dir: sessionDir, parent_id: parentId ?? "", source, refresh: refresh === true };
}

/** True when a `context` event already carries earlier history (more than one message). */
export function contextHasHistory(ev: ContextEvent | null | undefined): boolean {
  const messages = (ev as { messages?: unknown } | null | undefined)?.messages;
  return Array.isArray(messages) && messages.length > 1;
}

/**
 * The history marker on a `ctx.session.get` answer (docs/api-findings.md 12b, measured in
 * the author's private test evidence and events-turn-end.jsonl): a session that has finished at
 * least one turn carries the key `outcome` (the store's `idle_outcome`, written at each turn end);
 * a brand-new one does not (its keys are id, projectID, model, cost, tokens, time, location).
 * Only the key's presence with a non-null value is read; its value's type was never measured.
 * Anything else (no answer, not an object, no `outcome`) reads as "no history": the old behaviour.
 */
export function sessionHadHistory(info: unknown): boolean {
  if (info === null || typeof info !== "object") return false;
  const outcome = (info as { outcome?: unknown }).outcome;
  return outcome !== undefined && outcome !== null;
}

/**
 * The source for a session's FIRST `context` call in this process (docs/api-findings.md 12b, P3b):
 * a resumed session arrives with its history already in `messages` (lengths 3, 5, 7... measured),
 * a new one with just the member's first message (length 1). So more than one message = resume.
 * After a compaction the host shows ONE message (summary folded in, items 22 and 25) although the
 * chat has history, so `hadHistory` (the `session.get` marker, see sessionHadHistory) also means
 * resume. When `messages` is missing or not an array and there is no marker, it says "startup".
 */
export function sourceForFirstContext(ev: ContextEvent | null | undefined, hadHistory = false): SessionSource {
  return contextHasHistory(ev) || hadHistory === true ? "resume" : "startup";
}

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** `{text, date, timezone}` out of a `session` result object; anything malformed reads as absent. */
export function sessionResultOf(result: unknown): { text: string; date: string | null; timezone: string | null } {
  const r = (typeof result === "object" && result !== null ? result : {}) as BridgeSessionResult;
  const text = typeof r.context === "string" ? r.context : "";
  const date = typeof r.date === "string" && DATE_RE.test(r.date) ? r.date : null;
  const timezone = typeof r.timezone === "string" && r.timezone !== "" ? r.timezone : null;
  return { text, date, timezone };
}

/**
 * The calendar day "YYYY-MM-DD" of `nowMs` in `timeZone`, or null when the zone is unknown to
 * this runtime's Intl data (the caller then never refreshes on a day change).
 */
export function localDay(nowMs: number, timeZone: string): string | null {
  try {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(
      new Date(nowMs),
    );
    const get = (type: string): string => parts.find((p) => p.type === type)?.value ?? "";
    const day = `${get("year")}-${get("month")}-${get("day")}`;
    return DATE_RE.test(day) ? day : null;
  } catch {
    return null;
  }
}

/** `session.prompt` -> bridge `prompt`, with the member's raw text (`prompt.text`). */
export function toBridgePrompt(ev: PromptEvent, sessionDir: string, parentId?: string): BridgePrompt {
  const text = ev.prompt?.text;
  return {
    session_id: ev.sessionID,
    prompt: typeof text === "string" ? text : "",
    session_dir: sessionDir,
    parent_id: parentId ?? "",
  };
}

// ---------------------------------------------------------------------------------------------
// Result and context helpers.

/**
 * A copy of an after-event result with one text part appended to `content`, the way the host's
 * own plugins attach notes (`{...result, content: [...result.content, {type:"text", text}]}`).
 * Tolerates an undefined/null result or a missing `content`.
 */
export function appendNoteToResult(result: ToolResult | null | undefined, text: string): ToolResult {
  const base: ToolResult = result ?? {};
  const content = Array.isArray(base.content) ? base.content : [];
  return { ...base, content: [...content, { type: "text", text }] };
}

/**
 * Push `text` into a `context` event through BOTH measured channels (docs/api-findings.md 6a-6c):
 * `system.push` for normal providers, and `options.claudeCodeInstructions` (appended after a
 * blank line) for `claude-agent/*`, whose provider ignores `system`. Missing fields are skipped.
 */
export function pushContext(ev: ContextEvent | null | undefined, text: string): void {
  if (!ev) return;
  if (Array.isArray(ev.system)) ev.system.push({ type: "text", text });
  const opts = ev.options;
  if (opts && typeof opts.claudeCodeInstructions === "string") {
    opts.claudeCodeInstructions = opts.claudeCodeInstructions + "\n\n" + text;
  }
}
