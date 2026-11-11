import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  toBridgePre,
  toBridgePost,
  toBridgeSession,
  toBridgePrompt,
  appendNoteToResult,
  pushContext,
} from "../../src/plugin/translate.ts";
import type { ExecuteBeforeEvent, ExecuteAfterEvent, ContextEvent } from "../../src/plugin/translate.ts";

const FIX = join(dirname(fileURLToPath(import.meta.url)), "fixtures");
const SESSION_DIR = "/Users/member/Glitch";

function loadEvents(name: string): Map<string, Record<string, unknown>> {
  const out = new Map<string, Record<string, unknown>>();
  for (const line of readFileSync(join(FIX, name), "utf8").split("\n")) {
    if (!line.trim()) continue;
    const rec = JSON.parse(line);
    // First (observe/inject/throw) sample per tool id; the permission-deny samples come later in
    // some files, so prefer a non-deny line.
    const key = rec.toolId as string;
    if (!out.has(key) || (out.get(key) as { __mode?: string }).__mode === "permission-deny") {
      out.set(key, { ...rec.event, __mode: rec.mode });
    }
  }
  for (const v of out.values()) delete v.__mode;
  return out;
}

const BEFORE = loadEvents("evidence-execute-before.jsonl");
const AFTER = loadEvents("evidence-execute-after.jsonl");

const BEFORE_TOOLS = [
  "shell", "read", "write", "edit", "patch", "glob", "grep",
  "webfetch", "subagent", "execute", "opencode_worktree_list",
];
const AFTER_TOOLS = BEFORE_TOOLS;

test("fixtures hold every measured tool shape", () => {
  for (const t of BEFORE_TOOLS) assert.ok(BEFORE.has(t), `before fixture missing ${t}`);
  for (const t of AFTER_TOOLS) assert.ok(AFTER.has(t), `after fixture missing ${t}`);
});

for (const tool of BEFORE_TOOLS) {
  test(`toBridgePre maps the measured ${tool} event`, () => {
    const ev = BEFORE.get(tool) as unknown as ExecuteBeforeEvent;
    const snapshot = structuredClone(ev);
    const p = toBridgePre(ev, SESSION_DIR);
    assert.equal(p.tool_name, tool, "OpenCode tool id kept verbatim");
    assert.equal(p.session_id, ev.sessionID);
    assert.equal(p.session_dir, SESSION_DIR);
    assert.equal(p.parent_id, "");
    assert.deepEqual(p.tool_input, snapshot.input, "input passed through untouched");
    assert.deepEqual(ev, snapshot, "event not mutated");
    assert.deepEqual(Object.keys(p).sort(), ["parent_id", "session_dir", "session_id", "tool_input", "tool_name"]);
  });
}

for (const tool of AFTER_TOOLS) {
  test(`toBridgePost maps the measured ${tool} event`, () => {
    const ev = AFTER.get(tool) as unknown as ExecuteAfterEvent;
    const snapshot = structuredClone(ev);
    const p = toBridgePost(ev, SESSION_DIR, "ses_parent");
    assert.equal(p.tool_name, tool);
    assert.equal(p.session_id, ev.sessionID);
    assert.equal(p.session_dir, SESSION_DIR);
    assert.equal(p.status, ev.status);
    assert.deepEqual(p.result, snapshot.result ?? null);
    assert.deepEqual(p.error, snapshot.error ?? null);
    assert.ok(!("tool_input" in p) && !("input" in p), "post never carries input");
    assert.deepEqual(ev, snapshot, "event not mutated");
  });
}

test("toBridgePost maps a measured error event (permission-deny shell)", () => {
  const line = readFileSync(join(FIX, "evidence-execute-after.jsonl"), "utf8")
    .split("\n").filter(Boolean).map((l) => JSON.parse(l))
    .find((r) => r.mode === "permission-deny" && r.toolId === "shell");
  assert.ok(line, "error sample present");
  const p = toBridgePost(line.event, SESSION_DIR);
  assert.equal(p.status, "error");
  assert.equal(p.result, null);
  assert.deepEqual(p.error, line.event.error);
});

test("session_dir is never taken from the tool input (workdir:/tmp stays data)", () => {
  const ev: ExecuteBeforeEvent = {
    tool: "shell",
    sessionID: "ses_x",
    agent: "build",
    messageID: "msg_x",
    id: "toolu_x",
    input: { command: "rm -rf /Users/member/Glitch/workspaces", workdir: "/tmp" },
  };
  const p = toBridgePre(ev, SESSION_DIR);
  assert.equal(p.session_dir, SESSION_DIR);
  assert.deepEqual(p.tool_input, { command: "rm -rf /Users/member/Glitch/workspaces", workdir: "/tmp" });
  // A directory-shaped argument (Code Mode) is data too.
  const q = toBridgePre({ tool: "opencode_worktree_remove", sessionID: "s", input: { directory: "/elsewhere", force: true } }, SESSION_DIR);
  assert.equal(q.session_dir, SESSION_DIR);
  assert.equal(q.tool_name, "opencode_worktree_remove");
  assert.deepEqual(q.tool_input, { directory: "/elsewhere", force: true });
  // Post too: a cwd-ish field anywhere in the event never wins.
  const r = toBridgePost({ ...ev, status: "completed", result: { output: { cwd: "/tmp" }, content: [] } } as ExecuteAfterEvent, SESSION_DIR);
  assert.equal(r.session_dir, SESSION_DIR);
});

test("parent_id is passed through for a sub-agent and empty for a primary session", () => {
  const ev = BEFORE.get("shell") as unknown as ExecuteBeforeEvent;
  assert.equal(toBridgePre(ev, SESSION_DIR, "ses_parent").parent_id, "ses_parent");
  assert.equal(toBridgePre(ev, SESSION_DIR, undefined).parent_id, "");
});

test("toBridgePre tolerates a missing input and a string input", () => {
  assert.deepEqual(toBridgePre({ tool: "x", sessionID: "s" }, SESSION_DIR).tool_input, {});
  assert.equal(toBridgePre({ tool: "x", sessionID: "s", input: '{"a":1}' }, SESSION_DIR).tool_input, '{"a":1}');
});

test("toBridgeSession", () => {
  assert.deepEqual(toBridgeSession("ses_1", SESSION_DIR), { session_id: "ses_1", session_dir: SESSION_DIR, parent_id: "" });
  assert.deepEqual(toBridgeSession("ses_2", SESSION_DIR, "ses_1"), { session_id: "ses_2", session_dir: SESSION_DIR, parent_id: "ses_1" });
});

test("toBridgePrompt maps the measured session.prompt event", () => {
  // Verbatim from docs/api-findings.md item 6.
  const ev = {
    sessionID: "ses_e35b75d7dffeeLUtMOUl3rWTAq",
    messageID: "msg_1ca48a29d001aO9wJt2A8qchF9",
    prompt: { text: "run the shell command: echo hi", files: [] },
    delivery: "steer",
  };
  assert.deepEqual(toBridgePrompt(ev, SESSION_DIR), {
    session_id: "ses_e35b75d7dffeeLUtMOUl3rWTAq",
    prompt: "run the shell command: echo hi",
    session_dir: SESSION_DIR,
    parent_id: "",
  });
  assert.equal(toBridgePrompt(ev, SESSION_DIR, "ses_p").parent_id, "ses_p");
  assert.equal(toBridgePrompt({ sessionID: "s" }, SESSION_DIR).prompt, "", "missing text -> empty string");
  assert.equal(toBridgePrompt({ sessionID: "s", prompt: { text: "yes" } }, SESSION_DIR).prompt, "yes");
});

test("appendNoteToResult appends to a measured result without mutating it", () => {
  const ev = AFTER.get("shell") as unknown as ExecuteAfterEvent;
  const before = structuredClone(ev.result);
  const out = appendNoteToResult(ev.result, "NOTE");
  assert.deepEqual(ev.result, before, "original untouched");
  assert.deepEqual(out.output, before?.output);
  assert.deepEqual(out.content, [...(before?.content ?? []), { type: "text", text: "NOTE" }]);
});

test("appendNoteToResult on undefined, null and content-less results", () => {
  assert.deepEqual(appendNoteToResult(undefined, "n"), { content: [{ type: "text", text: "n" }] });
  assert.deepEqual(appendNoteToResult(null, "n"), { content: [{ type: "text", text: "n" }] });
  assert.deepEqual(appendNoteToResult({ output: "x" }, "n"), { output: "x", content: [{ type: "text", text: "n" }] });
  assert.deepEqual(
    appendNoteToResult({ output: "x", content: undefined }, "n"),
    { output: "x", content: [{ type: "text", text: "n" }] },
  );
});

test("pushContext uses both channels when both are present", () => {
  const ev: ContextEvent = {
    sessionID: "s",
    system: [{ type: "text", text: "a" }],
    options: { claudeCodeAgent: "x", claudeCodeInstructions: "base" },
  };
  pushContext(ev, "CTX");
  assert.deepEqual(ev.system, [{ type: "text", text: "a" }, { type: "text", text: "CTX" }]);
  assert.equal(ev.options?.claudeCodeInstructions, "base\n\nCTX");
  assert.equal(ev.options?.claudeCodeAgent, "x");
});

test("pushContext uses system only for a non-claude-agent provider", () => {
  const ev: ContextEvent = { system: [], options: {} };
  pushContext(ev, "CTX");
  assert.deepEqual(ev.system, [{ type: "text", text: "CTX" }]);
  assert.ok(!("claudeCodeInstructions" in (ev.options ?? {})), "never creates the option");
});

test("pushContext with neither channel present is a no-op, never throws", () => {
  const ev: ContextEvent = { sessionID: "s" };
  pushContext(ev, "CTX");
  assert.deepEqual(ev, { sessionID: "s" });
  const ev2: ContextEvent = { options: { claudeCodeInstructions: 5 } };
  pushContext(ev2, "CTX");
  assert.deepEqual(ev2, { options: { claudeCodeInstructions: 5 } });
  assert.doesNotThrow(() => pushContext(undefined, "CTX"));
  assert.doesNotThrow(() => pushContext(null, "CTX"));
});

test("pushContext with only the claude-agent channel present", () => {
  const ev: ContextEvent = { options: { claudeCodeInstructions: "" } };
  pushContext(ev, "CTX");
  assert.equal(ev.options?.claudeCodeInstructions, "\n\nCTX");
});
