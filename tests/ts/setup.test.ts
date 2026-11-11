import { test } from "node:test";
import assert from "node:assert/strict";

import { setupGlitch, SUPPORTED_OPENCODE_VERSION, PARENT_LOOKUP_TIMEOUT_MS } from "../../src/plugin/setup.ts";
import type { SetupDeps } from "../../src/plugin/setup.ts";
import type { BridgeOutcome, CallOptions, Mode } from "../../src/plugin/bridge-client.ts";

const GLITCH = "/Users/member/Glitch";
const OUTSIDE = "/Users/member/elsewhere";
const BRIDGE = "/fake/bridge/opencode_bridge.py";

// Every ctx key OpenCode 2.2.7 hands a plugin (docs/api-findings.md item 1, measured).
const CTX_KEYS = [
  "app", "location", "options", "agent", "aisdk", "command", "credential", "event", "experimental",
  "generate", "model", "provider", "integration", "mcp", "permission", "plugin", "reference", "rpc",
  "skill", "storage", "tool", "vcs", "websearch", "worktree", "session", "shell",
];

type Registration = { domain: string; name: string; fn: (ev: any) => Promise<unknown> };
type Call = { root: string; mode: Mode; payload: any; opts?: CallOptions };

function fakeCtx(opts: { directory?: unknown; version?: unknown; sessionGet?: (a: { sessionID: string }) => Promise<unknown> } = {}) {
  const regs: Registration[] = [];
  const getCalls: string[] = [];
  const ctx: Record<string, any> = {};
  for (const key of CTX_KEYS) {
    // Every domain records any hook registration, so the inert sweep sees all of them.
    ctx[key] = { hook: (name: string, fn: any) => regs.push({ domain: key, name, fn }) };
  }
  ctx.app = { version: "version" in opts ? opts.version : SUPPORTED_OPENCODE_VERSION, hook: ctx.app.hook };
  ctx.location = { directory: "directory" in opts ? opts.directory : GLITCH, hook: ctx.location.hook };
  ctx.session.get = async (args: { sessionID: string }) => {
    getCalls.push(args.sessionID);
    return opts.sessionGet ? opts.sessionGet(args) : { id: args.sessionID };
  };
  return { ctx, regs, getCalls };
}

function fakeDeps(outcome: BridgeOutcome | ((c: Call) => BridgeOutcome | Promise<BridgeOutcome>) = { ok: true, result: null }) {
  const calls: Call[] = [];
  const warnings: string[] = [];
  const recorded: Array<{ reason: string; sessionId?: string }> = [];
  const lookups: string[] = [];
  let bridgeResolves = 0;
  const deps: SetupDeps = {
    callBridge: async (root, mode, payload, opts) => {
      const call = { root, mode, payload, opts };
      calls.push(call);
      return typeof outcome === "function" ? outcome(call) : outcome;
    },
    findGlitchRoot: (dir) => {
      lookups.push(dir);
      return dir === GLITCH || dir.startsWith(GLITCH + "/") ? GLITCH : null;
    },
    failure: {
      record: (reason, sessionId) => {
        recorded.push({ reason, sessionId });
        return reason;
      },
    },
    resolveBridgePath: () => {
      bridgeResolves++;
      return BRIDGE;
    },
    warn: (line) => warnings.push(line),
  };
  return { deps, calls, warnings, recorded, lookups, resolves: () => bridgeResolves };
}

function hook(regs: Registration[], domain: string, name: string) {
  const found = regs.filter((r) => r.domain === domain && r.name === name);
  assert.equal(found.length, 1, `exactly one ${domain}.${name} registration`);
  return found[0].fn;
}

const shellEvent = (input: Record<string, unknown>, sessionID = "ses_primary") => ({
  tool: "shell",
  sessionID,
  agent: "build",
  messageID: "msg_1",
  id: "toolu_1",
  input,
});

// ---------------------------------------------------------------------------------------------

test("outside Glitch: zero registrations on every ctx domain, no bridge, no warnings", async () => {
  for (const directory of [OUTSIDE, "", undefined, null, 42]) {
    const { ctx, regs, getCalls } = fakeCtx({ directory, version: "9.9.9" });
    const f = fakeDeps();
    await setupGlitch(ctx, f.deps);
    assert.deepEqual(regs, [], `no hook registered for directory=${String(directory)}`);
    assert.equal(f.calls.length, 0);
    assert.equal(f.resolves(), 0, "the bridge path is not even resolved outside Glitch");
    assert.deepEqual(f.warnings, [], "no version warning outside Glitch either");
    assert.deepEqual(getCalls, []);
  }
});

test("a ctx with no location at all is inert and does not throw", async () => {
  const f = fakeDeps();
  await setupGlitch({} as any, f.deps);
  await setupGlitch(undefined as any, f.deps);
  assert.equal(f.calls.length, 0);
});

test("findGlitchRoot throwing is treated as no root", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps();
  f.deps.findGlitchRoot = () => {
    throw new Error("boom");
  };
  await setupGlitch(ctx, f.deps);
  assert.deepEqual(regs, []);
});

test("inside Glitch: registers exactly the two tool guards and the two memory hooks, root resolved once", async () => {
  const { ctx, regs } = fakeCtx({ directory: GLITCH + "/workspaces/proj" });
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  assert.deepEqual(
    regs.map((r) => `${r.domain}.${r.name}`).sort(),
    ["session.context", "session.prompt", "tool.execute.after", "tool.execute.before"],
  );
  assert.deepEqual(f.lookups, [GLITCH + "/workspaces/proj"]);
  assert.equal(f.resolves(), 1);
  assert.deepEqual(f.warnings, []);

  // Several calls later, the root is still not looked up again.
  const pre = hook(regs, "tool", "execute.before");
  await pre(shellEvent({ command: "echo a" }));
  await pre(shellEvent({ command: "echo b" }));
  assert.equal(f.lookups.length, 1);
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].root, GLITCH);
  assert.equal(f.calls[0].opts?.bridgePath, BRIDGE);
});

test("a bridge deny throws an Error whose message is deny.model verbatim", async () => {
  const model = "BLOCKED by Glitch: `git clean -fdx` would wipe workspaces/.\nUse a scoped clean instead. «ünïcode» $HOME";
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: { deny: { kind: "brain-clean", member: "member words", model } } });
  await setupGlitch(ctx, f.deps);
  const pre = hook(regs, "tool", "execute.before");
  await assert.rejects(pre(shellEvent({ command: "git clean -fdx" })), (err: unknown) => {
    assert.ok(err instanceof Error);
    assert.equal(err.message, model);
    return true;
  });
  assert.equal(f.calls[0].mode, "pre");
  assert.deepEqual(f.recorded, [], "a deny is not a failure");
});

test("bridge {ok:false} allows (no throw) and the plugin does not double-record", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: false, reason: "uv not found" });
  await setupGlitch(ctx, f.deps);
  const pre = hook(regs, "tool", "execute.before");
  const ev = shellEvent({ command: "rm -rf x" });
  await pre(ev);
  assert.equal(f.calls.length, 1);
  assert.deepEqual(f.recorded, [], "callBridge records its own failures");
  assert.deepEqual(ev.input, { command: "rm -rf x" }, "the call is not rewritten");
});

test("bridge null (judged and allowed) allows", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: null });
  await setupGlitch(ctx, f.deps);
  await hook(regs, "tool", "execute.before")(shellEvent({ command: "ls" }));
  assert.equal(f.calls.length, 1);
});

test("a non-deny result in pre (e.g. a note) allows", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: { note: "x" } });
  await setupGlitch(ctx, f.deps);
  await hook(regs, "tool", "execute.before")(shellEvent({ command: "ls" }));
});

test("callBridge rejecting (contract broken) allows and records a plugin error", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps(() => {
    throw new Error("secret-bearing message that must not be logged");
  });
  await setupGlitch(ctx, f.deps);
  await hook(regs, "tool", "execute.before")(shellEvent({ command: "ls" }, "ses_X"));
  assert.deepEqual(f.recorded, [{ reason: "plugin error in pre", sessionId: "ses_X" }]);
  await hook(regs, "tool", "execute.after")({ ...shellEvent({}, "ses_X"), status: "completed", result: { content: [] } });
  assert.deepEqual(f.recorded[1], { reason: "plugin error in post", sessionId: "ses_X" });
});

test("shell with workdir outside Glitch is still bridged, session_dir = ctx.location.directory", async () => {
  const startDir = GLITCH + "/workspaces/proj";
  const { ctx, regs } = fakeCtx({ directory: startDir });
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  const ev = shellEvent({ command: `rm -rf ${GLITCH}/workspaces`, workdir: "/tmp" });
  await hook(regs, "tool", "execute.before")(ev);
  assert.equal(f.calls.length, 1, "the guard ran despite workdir:/tmp");
  const p = f.calls[0].payload;
  assert.equal(p.session_dir, startDir);
  assert.equal(p.tool_name, "shell");
  assert.deepEqual(p.tool_input, { command: `rm -rf ${GLITCH}/workspaces`, workdir: "/tmp" });
  assert.equal(p.session_id, "ses_primary");
  assert.equal(p.parent_id, "");
  assert.deepEqual(f.lookups, [startDir], "the tool's workdir was never walked");
});

test("file tools with absolute paths outside Glitch are still bridged", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  const pre = hook(regs, "tool", "execute.before");
  await pre({ tool: "write", sessionID: "s", input: { path: "/etc/x", content: "y" } });
  await pre({ tool: "read", sessionID: "s", input: { path: "/Users/other/.ssh/id_rsa" } });
  assert.deepEqual(f.calls.map((c) => [c.payload.tool_name, c.payload.session_dir]), [
    ["write", GLITCH],
    ["read", GLITCH],
  ]);
});

test("a subagent session passes parent_id, looked up once per session", async () => {
  const { ctx, regs, getCalls } = fakeCtx({
    sessionGet: async ({ sessionID }) =>
      sessionID === "ses_child" ? { id: sessionID, parentID: "ses_parent", agent: "general" } : { id: sessionID },
  });
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  const pre = hook(regs, "tool", "execute.before");
  await pre(shellEvent({ command: "echo 1" }, "ses_child"));
  await pre(shellEvent({ command: "echo 2" }, "ses_child"));
  await pre(shellEvent({ command: "echo 3" }, "ses_parent"));
  assert.deepEqual(f.calls.map((c) => c.payload.parent_id), ["ses_parent", "ses_parent", ""]);
  assert.deepEqual(getCalls, ["ses_child", "ses_parent"], "cached per session id");
});

test("ctx.session.get rejecting never blocks: parent_id '' and the guard still runs", async () => {
  const { ctx, regs, getCalls } = fakeCtx({
    sessionGet: async () => {
      throw new Error("session store down");
    },
  });
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  const pre = hook(regs, "tool", "execute.before");
  await pre(shellEvent({ command: "echo hi" }, "ses_a"));
  await pre(shellEvent({ command: "echo hi" }, "ses_a"));
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[0].payload.parent_id, "");
  assert.equal(getCalls.length, 2, "a failed lookup is not cached, so it is retried");
});

test("ctx.session.get rejecting still lets a deny through", async () => {
  const { ctx, regs } = fakeCtx({ sessionGet: () => Promise.reject(new Error("x")) });
  const f = fakeDeps({ ok: true, result: { deny: { kind: "k", member: "m", model: "MODEL-WORDS" } } });
  await setupGlitch(ctx, f.deps);
  await assert.rejects(hook(regs, "tool", "execute.before")(shellEvent({ command: "x" })), { message: "MODEL-WORDS" });
});

test("ctx.session.get missing or hanging never stalls the tool", async (t) => {
  // A real timer, taken before mocking, so a stalled hook fails fast instead of hanging the run.
  const realSetTimeout = globalThis.setTimeout;
  let guard: ReturnType<typeof setTimeout> | undefined;
  const stalled = new Promise((_, reject) => {
    guard = realSetTimeout(() => reject(new Error("the tool hook stalled on session.get")), 3_000);
  });
  t.after(() => clearTimeout(guard));
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { ctx, regs } = fakeCtx({ sessionGet: () => new Promise(() => {}) });
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  const done = hook(regs, "tool", "execute.before")(shellEvent({ command: "echo" }));
  // Let the lookup start, then pass the timeout.
  await Promise.resolve();
  t.mock.timers.tick(PARENT_LOOKUP_TIMEOUT_MS + 1);
  await Promise.race([done, stalled]);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].payload.parent_id, "");

  const second = fakeCtx();
  delete second.ctx.session.get;
  const g = fakeDeps();
  await setupGlitch(second.ctx, g.deps);
  await hook(second.regs, "tool", "execute.before")(shellEvent({ command: "echo" }));
  assert.equal(g.calls[0].payload.parent_id, "");
});

test("post: a note is appended to the tool result content", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: { note: "Glitch: that output may contain a secret." } });
  await setupGlitch(ctx, f.deps);
  const ev: any = {
    ...shellEvent({ command: "cat .env" }),
    status: "completed",
    result: { output: "x", content: [{ type: "text", text: "TOKEN=..." }], metadata: { a: 1 } },
  };
  await hook(regs, "tool", "execute.after")(ev);
  assert.equal(f.calls[0].mode, "post");
  assert.equal(f.calls[0].payload.session_dir, GLITCH);
  assert.deepEqual(ev.result, {
    output: "x",
    metadata: { a: 1 },
    content: [
      { type: "text", text: "TOKEN=..." },
      { type: "text", text: "Glitch: that output may contain a secret." },
    ],
  });
});

test("post: no note, {ok:false}, or a missing result leaves the result alone and never throws", async () => {
  for (const outcome of [{ ok: true, result: null }, { ok: false, reason: "timeout" }] as BridgeOutcome[]) {
    const { ctx, regs } = fakeCtx();
    const f = fakeDeps(outcome);
    await setupGlitch(ctx, f.deps);
    const result = { output: "o", content: [{ type: "text", text: "t" }] };
    const ev: any = { ...shellEvent({}), status: "completed", result };
    await hook(regs, "tool", "execute.after")(ev);
    assert.equal(ev.result, result);
  }
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: { note: "n" } });
  await setupGlitch(ctx, f.deps);
  const ev: any = { ...shellEvent({}), status: "error", error: "failed" };
  await hook(regs, "tool", "execute.after")(ev);
  assert.equal(ev.result, undefined);
});

test("version mismatch warns once and still registers the guards", async () => {
  for (const version of ["2.3.0", undefined]) {
    const { ctx, regs } = fakeCtx({ version });
    const f = fakeDeps();
    await setupGlitch(ctx, f.deps);
    assert.equal(f.warnings.length, 1);
    assert.match(f.warnings[0], /not the measured 2\.2\.7/);
    assert.equal(regs.length, 4);
  }
});

test("resolveBridgePath throwing still registers the guards (callBridge resolves per call)", async () => {
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps();
  f.deps.resolveBridgePath = () => {
    throw new Error("cannot resolve the plugin folder");
  };
  await setupGlitch(ctx, f.deps);
  await hook(regs, "tool", "execute.before")(shellEvent({ command: "ls" }));
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].opts?.bridgePath, undefined);
});

test("a host that refuses a registration is recorded and warned, never thrown", async () => {
  const { ctx } = fakeCtx();
  ctx.tool = {
    hook: () => {
      throw new Error("unknown hook");
    },
  };
  const f = fakeDeps();
  await setupGlitch(ctx, f.deps);
  assert.equal(f.recorded.length, 2);
  assert.match(f.recorded[0].reason, /hook registration failed/);
  assert.equal(f.warnings.length, 2);
});

test("the real resolveBridgePath is the only way the override env var is read", async () => {
  const { resolveBridgePath, BRIDGE_OVERRIDE_ENV } = await import("../../src/plugin/bridge-client.ts");
  const lines: string[] = [];
  assert.equal(resolveBridgePath({ [BRIDGE_OVERRIDE_ENV]: "/x/fake.py" }, (l) => lines.push(l)), "/x/fake.py");
  assert.equal(lines.length, 1);
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/plugin/setup.ts", import.meta.url), "utf8");
  assert.ok(!src.includes("GLITCH_OPENCODE_BRIDGE_OVERRIDE"), "setup.ts never reads the env var itself");
  assert.ok(!/process\.env/.test(src), "setup.ts reads no environment directly");
});

// ---------------------------------------------------------------------------------------------
// CP3: memory-in

import { record as recordFailure, resetPendingForTests } from "../../src/plugin/failure.ts";

const ctxEvent = (sessionID = "ses_primary", withOptions = false) => ({
  sessionID,
  system: [{ type: "text", text: "base" }] as Array<{ type: string; text: string }>,
  ...(withOptions ? { options: { claudeCodeInstructions: "claude base" } } : {}),
});

const bridgeByMode = (texts: Partial<Record<Mode, string>>) => (c: Call): BridgeOutcome => {
  const text = texts[c.mode];
  return { ok: true, result: text ? { context: text } : null };
};

test("memory-in: first context call runs bridge session once and pushes it; later calls re-push the cache", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps(bridgeByMode({ session: "IDENTITY+MEMORY" }));
  await setupGlitch(ctx as any, f.deps);
  const context = hook(regs, "session", "context");
  const first = ctxEvent();
  await context(first);
  const second = ctxEvent();
  await context(second);
  assert.equal(f.calls.filter((c) => c.mode === "session").length, 1);
  assert.deepEqual(first.system.map((p) => p.text), ["base", "IDENTITY+MEMORY"]);
  assert.deepEqual(second.system.map((p) => p.text), ["base", "IDENTITY+MEMORY"]);
  const sent = f.calls.find((c) => c.mode === "session")!;
  assert.equal(sent.payload.session_dir, GLITCH);
  assert.equal(sent.payload.session_id, "ses_primary");
});

test("memory-in: claude-agent models get the text through claudeCodeInstructions too", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps(bridgeByMode({ session: "MEM" }));
  await setupGlitch(ctx as any, f.deps);
  const ev = ctxEvent("ses_primary", true);
  await hook(regs, "session", "context")(ev);
  assert.equal(ev.options!.claudeCodeInstructions, "claude base\n\nMEM");
});

test("memory-in: prompt hook calls the bridge for every message, even with no output", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps({ ok: true, result: null });
  await setupGlitch(ctx as any, f.deps);
  const prompt = hook(regs, "session", "prompt");
  await prompt({ sessionID: "ses_primary", prompt: { text: "hello" } });
  await prompt({ sessionID: "ses_primary", prompt: { text: "yes" } });
  const calls = f.calls.filter((c) => c.mode === "prompt");
  assert.equal(calls.length, 2);
  assert.equal(calls[1].payload.prompt, "yes");
  assert.equal(calls[0].payload.session_dir, GLITCH);
});

test("memory-in: prompt output is stashed and consumed by the next context call only", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps(bridgeByMode({ session: "SESSION", prompt: "TURN-CARD" }));
  await setupGlitch(ctx as any, f.deps);
  await hook(regs, "session", "prompt")({ sessionID: "ses_primary", prompt: { text: "hi" } });
  const a = ctxEvent();
  await hook(regs, "session", "context")(a);
  const b = ctxEvent();
  await hook(regs, "session", "context")(b);
  assert.deepEqual(a.system.map((p) => p.text), ["base", "SESSION\n\nTURN-CARD"]);
  assert.deepEqual(b.system.map((p) => p.text), ["base", "SESSION"]);
});

test("memory-in: a pending failure notice rides the next context push, once", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps(bridgeByMode({}));
  await setupGlitch(ctx as any, f.deps);
  recordFailure("uv not found", "ses_primary", { logPath: "/dev/null" });
  const a = ctxEvent();
  await hook(regs, "session", "context")(a);
  const b = ctxEvent();
  await hook(regs, "session", "context")(b);
  assert.match(a.system[1].text, /Glitch guards in OpenCode failed to run \(uv not found\)/);
  assert.equal(b.system.length, 1);
});

test("memory-in: sessions are independent, and a sub-agent's parent id is sent", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx({
    sessionGet: async (a) => (a.sessionID === "ses_child" ? { parentID: "ses_primary" } : { id: a.sessionID }),
  });
  const f = fakeDeps(bridgeByMode({ session: "MEM" }));
  await setupGlitch(ctx as any, f.deps);
  const context = hook(regs, "session", "context");
  await context(ctxEvent("ses_primary"));
  await context(ctxEvent("ses_child"));
  const sessions = f.calls.filter((c) => c.mode === "session");
  assert.equal(sessions.length, 2);
  assert.equal(sessions[1].payload.parent_id, "ses_primary");
});

test("memory-in: bridge failure or throw allows, pushes nothing, and never throws", async () => {
  resetPendingForTests();
  for (const outcome of [
    { ok: false, reason: "boom" } as BridgeOutcome,
    (() => { throw new Error("x"); }) as unknown as (c: Call) => BridgeOutcome,
  ]) {
    const { ctx, regs } = fakeCtx();
    const f = fakeDeps(outcome);
    await setupGlitch(ctx as any, f.deps);
    const ev = ctxEvent();
    await hook(regs, "session", "context")(ev);
    await hook(regs, "session", "prompt")({ sessionID: "ses_primary", prompt: { text: "hi" } });
    assert.equal(ev.system.length, 1);
  }
});

test("memory-in: outside Glitch nothing is registered (no prompt or context hooks)", async () => {
  const { ctx, regs } = fakeCtx({ directory: OUTSIDE });
  const f = fakeDeps();
  await setupGlitch(ctx as any, f.deps);
  assert.equal(regs.length, 0);
});
