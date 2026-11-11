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

export type BridgeSession = {
  session_id: string;
  session_dir: string;
  parent_id: string;
};

export type BridgePrompt = {
  session_id: string;
  prompt: string;
  session_dir: string;
  parent_id: string;
};

// ---------------------------------------------------------------------------------------------
// Mappers.

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

/** First `context` call for a session id -> bridge `session`. */
export function toBridgeSession(sessionId: string, sessionDir: string, parentId?: string): BridgeSession {
  return { session_id: sessionId, session_dir: sessionDir, parent_id: parentId ?? "" };
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
