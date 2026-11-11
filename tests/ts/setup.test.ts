import { test } from "node:test";
import assert from "node:assert/strict";

import {
  setupGlitch,
  registerMemoryIn,
  REPEAT_FRAMING,
  SUPPORTED_OPENCODE_VERSION,
  PARENT_LOOKUP_TIMEOUT_MS,
} from "../../src/plugin/setup.ts";
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

/** A controllable host event stream: what `ctx.event.subscribe()` resolves to (an async iterable). */
function eventStream() {
  const queue: unknown[] = [];
  let wake: (() => void) | undefined;
  let ended = false;
  return {
    push(ev: unknown) {
      queue.push(ev);
      wake?.();
    },
    end() {
      ended = true;
      wake?.();
    },
    iterable: {
      async *[Symbol.asyncIterator]() {
        for (;;) {
          while (queue.length > 0) yield queue.shift();
          if (ended) return;
          await new Promise<void>((r) => (wake = r));
          wake = undefined;
        }
      },
    },
  };
}

function fakeCtx(opts: { directory?: unknown; version?: unknown; sessionGet?: (a: { sessionID: string }) => Promise<unknown> } = {}) {
  const regs: Registration[] = [];
  const getCalls: string[] = [];
  const stream = eventStream();
  const subscribes: unknown[][] = [];
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
  // The measured wildcard form: subscribe() with no arguments resolves to an async iterable.
  ctx.event = {
    hook: ctx.event.hook,
    subscribe: (...args: unknown[]) => {
      subscribes.push(args);
      return Promise.resolve(stream.iterable);
    },
  };
  return { ctx, regs, getCalls, stream, subscribes };
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
    realpath: (p) => p,
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
    const { ctx, regs, getCalls, subscribes } = fakeCtx({ directory, version: "9.9.9" });
    const f = fakeDeps();
    await setupGlitch(ctx, f.deps);
    assert.deepEqual(regs, [], `no hook registered for directory=${String(directory)}`);
    assert.deepEqual(subscribes, [], "no event subscription outside Glitch");
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

test("memory-in: the turn's card rides EVERY model call of its turn, framed after the first; a quiet next turn carries none", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  let card = "TURN-CARD";
  const f = fakeDeps((c) => {
    if (c.mode === "session") return { ok: true, result: { context: "SESSION" } };
    if (c.mode === "prompt") return { ok: true, result: card ? { context: card } : null };
    return { ok: true, result: null };
  });
  await setupGlitch(ctx as any, f.deps);
  const prompt = hook(regs, "session", "prompt");
  const context = hook(regs, "session", "context");

  // Turn 1: a one-tool-call turn is two model calls (findings item 6).
  await prompt({ sessionID: "ses_primary", prompt: { text: "hi" } });
  const a = ctxEvent();
  await context(a);
  const b = ctxEvent();
  await context(b);
  assert.deepEqual(a.system.map((p) => p.text), ["base", "SESSION\n\nTURN-CARD"], "first call: the text unchanged");
  assert.deepEqual(b.system.map((p) => p.text), ["base", `SESSION\n\n${REPEAT_FRAMING}\n\nTURN-CARD`], "second call: framed once");
  assert.equal(b.system[1].text.split(REPEAT_FRAMING).length, 2, "exactly one framing sentence");
  const c3 = ctxEvent();
  await context(c3);
  assert.equal(c3.system[1].text.split(REPEAT_FRAMING).length, 2, "still one framing sentence on the third call");

  // Turn 2: the bridge returns nothing, so neither of its calls may re-show turn 1's card.
  card = "";
  await prompt({ sessionID: "ses_primary", prompt: { text: "thanks" } });
  for (let i = 0; i < 2; i++) {
    const ev = ctxEvent();
    await context(ev);
    assert.deepEqual(ev.system.map((p) => p.text), ["base", "SESSION"], `turn 2 call ${i + 1}: no card`);
  }
  assert.equal(f.calls.filter((c) => c.mode === "session").length, 1, "session text built once");
});

test("memory-in: two sessions never share a turn card", async () => {
  resetPendingForTests();
  const { ctx, regs } = fakeCtx();
  const f = fakeDeps((c) =>
    c.mode === "prompt" ? { ok: true, result: { context: `CARD-${c.payload.session_id}` } } : { ok: true, result: null },
  );
  await setupGlitch(ctx as any, f.deps);
  await hook(regs, "session", "prompt")({ sessionID: "ses_a", prompt: { text: "x" } });
  const b = ctxEvent("ses_b");
  await hook(regs, "session", "context")(b);
  assert.equal(b.system.length, 1, "ses_b has no card of its own and never sees ses_a's");
  const a1 = ctxEvent("ses_a");
  await hook(regs, "session", "context")(a1);
  assert.deepEqual(a1.system.map((p) => p.text), ["base", "CARD-ses_a"]);
  await hook(regs, "session", "prompt")({ sessionID: "ses_b", prompt: { text: "y" } });
  const a2 = ctxEvent("ses_a");
  await hook(regs, "session", "context")(a2);
  assert.deepEqual(a2.system.map((p) => p.text), ["base", `${REPEAT_FRAMING}\n\nCARD-ses_a`], "ses_b's prompt did not clear ses_a's turn");
  const b2 = ctxEvent("ses_b");
  await hook(regs, "session", "context")(b2);
  assert.deepEqual(b2.system.map((p) => p.text), ["base", "CARD-ses_b"]);
});

test("memory-in: a failed prompt-mode call leaves no stale card from the turn before", async () => {
  for (const failure of ["ok-false", "throw"]) {
    resetPendingForTests();
    const { ctx, regs } = fakeCtx();
    let fail = false;
    const f = fakeDeps((c) => {
      if (c.mode === "prompt" && fail) {
        if (failure === "throw") throw new Error("boom");
        return { ok: false, reason: "timeout" };
      }
      return { ok: true, result: c.mode === "prompt" ? { context: "OLD-CARD" } : null };
    });
    await setupGlitch(ctx as any, f.deps);
    await hook(regs, "session", "prompt")({ sessionID: "ses_primary", prompt: { text: "1" } });
    await hook(regs, "session", "context")(ctxEvent());
    fail = true;
    await hook(regs, "session", "prompt")({ sessionID: "ses_primary", prompt: { text: "2" } });
    resetPendingForTests(); // the failure's own notice is covered elsewhere; look only for the card
    const ev = ctxEvent();
    await hook(regs, "session", "context")(ev);
    assert.equal(ev.system.length, 1, `${failure}: no card after a failed prompt call`);
  }
});

// --- session text: resume, compaction and day-change rebuilds -------------------------------

/** A bridge whose `session` answers are scripted per call; `prompt`/`capture` answer nothing. */
function scriptedSession(answers: Array<BridgeOutcome | (() => Promise<BridgeOutcome>) | "throw">) {
  let n = 0;
  return (c: Call): BridgeOutcome | Promise<BridgeOutcome> => {
    if (c.mode !== "session") return { ok: true, result: null };
    const next = answers[Math.min(n++, answers.length - 1)];
    if (next === "throw") throw new Error("contract broken");
    return typeof next === "function" ? next() : next;
  };
}

const sessionAnswer = (context: string, date?: string): BridgeOutcome =>
  ({ ok: true, result: date === undefined ? { context } : { context, date } }) as unknown as BridgeOutcome;

// 2026-11-11 12:00 London (GMT) and the next London day.
const NOV11_NOON = Date.UTC(2026, 10, 11, 12, 0);
const NOV12_0010 = Date.UTC(2026, 10, 12, 0, 10);

async function memorySetup(bridge: (c: Call) => BridgeOutcome | Promise<BridgeOutcome>, nowMs = NOV11_NOON) {
  resetPendingForTests();
  const h = fakeCtx();
  const f = fakeDeps(bridge);
  let clock = nowMs;
  f.deps.now = () => clock;
  const mem = registerMemoryIn(h.ctx as any, stateFor(h.ctx), f.deps);
  const context = hook(h.regs, "session", "context");
  const prompt = hook(h.regs, "session", "prompt");
  const push = async (sid = "ses_primary", extra: Record<string, unknown> = {}) => {
    const ev = { ...ctxEvent(sid), ...extra };
    await context(ev);
    return ev.system.slice(1).map((p) => p.text);
  };
  return { ...h, f, mem, context, prompt, push, setClock: (t: number) => (clock = t) };
}

const sessionCalls = (f: { calls: Call[] }) => f.calls.filter((c) => c.mode === "session").map((c) => c.payload);

test("memory-in: a first context call already holding history sends source resume; a fresh one sends startup", async () => {
  const m = await memorySetup(scriptedSession([sessionAnswer("S", "2026-11-11")]));
  await m.push("ses_old", { messages: [{ role: "user" }, { role: "assistant" }, { role: "user" }] });
  await m.push("ses_new", { messages: [{ role: "user" }] });
  await m.push("ses_unknown"); // no messages field: cannot tell, treated as startup
  await m.push("ses_old", { messages: [] }); // later calls never rebuild on their own
  assert.deepEqual(
    sessionCalls(m.f).map((p) => [p.session_id, p.source, p.refresh]),
    [["ses_old", "resume", false], ["ses_new", "startup", false], ["ses_unknown", "startup", false]],
  );
});

test("memory-in: a new London day refreshes the session text once, through the bridge's date; the noticing call keeps the old text", async () => {
  const m = await memorySetup(scriptedSession([sessionAnswer("DAY-11", "2026-11-11"), sessionAnswer("DAY-12", "2026-11-12")]));
  assert.deepEqual(await m.push(), ["DAY-11"]);
  assert.deepEqual(await m.push(), ["DAY-11"]);
  assert.equal(sessionCalls(m.f).length, 1, "no refresh within the same day");
  m.setClock(NOV12_0010);
  assert.deepEqual(await m.push(), ["DAY-11"], "the call that notices still pushes the old text");
  await m.mem.settled();
  assert.deepEqual(await m.push(), ["DAY-12"]);
  assert.deepEqual(await m.push(), ["DAY-12"]);
  const sent = sessionCalls(m.f);
  assert.equal(sent.length, 2, "exactly one refresh");
  assert.equal(sent[1].refresh, true);
  assert.equal(sent[1].source, "startup");
});

test("memory-in: a bridge date in another zone never causes a refresh storm", async () => {
  // The bridge already says the 12th while London says the 11th (an engine zone ahead of London).
  const m = await memorySetup(scriptedSession([sessionAnswer("S", "2026-11-12"), sessionAnswer("S2", "2026-11-12")]));
  for (let i = 0; i < 3; i++) await m.push();
  assert.equal(sessionCalls(m.f).length, 1, "the first build counts as today's");
  m.setClock(NOV12_0010); // London reaches the bridge's date: nothing to refresh
  for (let i = 0; i < 3; i++) await m.push();
  await m.mem.settled();
  assert.equal(sessionCalls(m.f).length, 1);
});

test("B7: the day refresh follows the bridge's zone (New York), not London's midnight", async () => {
  const ny = (context: string, date: string): BridgeOutcome =>
    ({ ok: true, result: { context, date, timezone: "America/New_York" } }) as unknown as BridgeOutcome;
  // 2026-11-11 23:00 London = 18:00 New York: both still the 11th.
  const m = await memorySetup(scriptedSession([ny("NY-11", "2026-11-11"), ny("NY-12", "2026-11-12")]), Date.UTC(2026, 10, 11, 23, 0));
  assert.deepEqual(await m.push(), ["NY-11"]);
  // 00:10 London on the 12th = 19:10 New York on the 11th: London's midnight must NOT refresh.
  m.setClock(Date.UTC(2026, 10, 12, 0, 10));
  for (let i = 0; i < 3; i++) await m.push();
  await m.mem.settled();
  assert.equal(sessionCalls(m.f).length, 1, "no refresh at London midnight for a New York engine");
  // 23:50 New York on the 11th: still no refresh.
  m.setClock(Date.UTC(2026, 10, 12, 4, 50));
  await m.push();
  await m.mem.settled();
  assert.equal(sessionCalls(m.f).length, 1, "no refresh before New York midnight");
  // 00:10 New York on the 12th: exactly one refresh.
  m.setClock(Date.UTC(2026, 10, 12, 5, 10));
  assert.deepEqual(await m.push(), ["NY-11"], "the noticing call keeps the old text");
  await m.mem.settled();
  assert.deepEqual(await m.push(), ["NY-12"]);
  await m.push();
  await m.mem.settled();
  const sent = sessionCalls(m.f);
  assert.equal(sent.length, 2, "exactly one refresh, at New York midnight");
  assert.equal(sent[1].refresh, true);
});

test("B7: a session first built after London midnight but before New York's still refreshes at New York midnight", async () => {
  const ny = (context: string, date: string): BridgeOutcome =>
    ({ ok: true, result: { context, date, timezone: "America/New_York" } }) as unknown as BridgeOutcome;
  // 00:30 London on the 12th = 19:30 New York on the 11th: the first build is New York's 11th.
  const m = await memorySetup(scriptedSession([ny("NY-11", "2026-11-11"), ny("NY-12", "2026-11-12")]), Date.UTC(2026, 10, 12, 0, 30));
  assert.deepEqual(await m.push(), ["NY-11"]);
  await m.mem.settled();
  assert.equal(sessionCalls(m.f).length, 1);
  m.setClock(Date.UTC(2026, 10, 12, 5, 10)); // 00:10 New York on the 12th
  await m.push();
  await m.mem.settled();
  assert.deepEqual(await m.push(), ["NY-12"], "New York's new day refreshed although London's 12th was seen first");
  assert.equal(sessionCalls(m.f).length, 2);
});

test("memory-in: a failed day refresh KEEPS the old text, is recorded, and is retried only after the next member message", async () => {
  for (const failure of [{ ok: false, reason: "timeout" } as BridgeOutcome, "throw" as const]) {
    const m = await memorySetup(scriptedSession([sessionAnswer("OLD", "2026-11-11"), failure, sessionAnswer("NEW", "2026-11-12")]));
    await m.push();
    m.setClock(NOV12_0010);
    await m.push();
    await m.mem.settled();
    assert.deepEqual(await m.push(), ["OLD"], "never an empty session after a failed rebuild");
    assert.deepEqual(await m.push(), ["OLD"]);
    assert.equal(sessionCalls(m.f).length, 2, "no retry on every model call");
    if (failure === "throw") assert.deepEqual(m.f.recorded, [{ reason: "plugin error in session rebuild", sessionId: "ses_primary" }]);
    await m.prompt({ sessionID: "ses_primary", prompt: { text: "next" } });
    await m.push();
    await m.mem.settled();
    assert.deepEqual(await m.push(), ["NEW"], "retried after the member's next message");
    assert.equal(sessionCalls(m.f).length, 3);
  }
});

test("memory-in: an empty rebuild answer keeps the old text", async () => {
  const m = await memorySetup(scriptedSession([sessionAnswer("KEEP", "2026-11-11"), { ok: true, result: null }]));
  await m.push();
  m.mem.onCompaction("ses_primary");
  await m.mem.settled();
  assert.deepEqual(await m.push(), ["KEEP"]);
  assert.equal(sessionCalls(m.f)[1].source, "compact");
});

test("memory-in: one rebuild at a time per session; calls during it push the old text; a second trigger queues one re-run", async () => {
  let release: () => void = () => {};
  const slow = () => new Promise<BridgeOutcome>((r) => (release = () => r(sessionAnswer("AFTER-1", "2026-11-11"))));
  const m = await memorySetup(scriptedSession([sessionAnswer("BEFORE", "2026-11-11"), slow, sessionAnswer("AFTER-2", "2026-11-11")]));
  await m.push();
  m.mem.onCompaction("ses_primary");
  await waitFor(() => sessionCalls(m.f).length === 2, 1_000, "the rebuild to start");
  m.mem.onCompaction("ses_primary");
  m.mem.onCompaction("ses_primary");
  m.setClock(NOV12_0010); // a day change during the rebuild joins the queued re-run
  const during = await Promise.all([m.push(), m.push(), m.push()]);
  assert.deepEqual(during, [["BEFORE"], ["BEFORE"], ["BEFORE"]], "the old text while the rebuild runs");
  assert.equal(sessionCalls(m.f).length, 2, "no second rebuild while one is in flight");
  release();
  await m.mem.settled();
  const sent = sessionCalls(m.f);
  assert.equal(sent.length, 3, "the triggers during the flight coalesced into ONE re-run");
  assert.deepEqual([sent[2].source, sent[2].refresh], ["compact", true]);
  assert.deepEqual(await m.push(), ["AFTER-2"]);
  // Another session is not held by this one's queue.
  assert.deepEqual(await m.push("ses_other"), ["AFTER-2"]);
});

test("memory-in: a compaction before the session's first context call here makes the first build source compact", async () => {
  const m = await memorySetup(scriptedSession([sessionAnswer("S", "2026-11-11")]));
  m.mem.onCompaction("ses_primary");
  assert.equal(sessionCalls(m.f).length, 0, "nothing to rebuild yet");
  await m.push("ses_primary", { messages: [{}, {}, {}] });
  assert.deepEqual(sessionCalls(m.f).map((p) => p.source), ["compact"]);
});

test("compaction via setupGlitch: ONE subscription drives the capture first, then the session rebuild (source compact)", async () => {
  resetPendingForTests();
  const { ctx, regs, stream, subscribes } = fakeCtx();
  let sessionN = 0;
  const f = fakeDeps((c) => {
    if (c.mode === "session") return sessionAnswer(++sessionN === 1 ? "PRE-COMPACT" : "POST-COMPACT", "2026-11-11");
    return { ok: true, result: null };
  });
  f.deps.now = () => NOV11_NOON;
  await setupGlitch(ctx as any, f.deps);
  const context = hook(regs, "session", "context");
  const first = ctxEvent();
  await context(first);
  assert.deepEqual(first.system.map((p) => p.text), ["base", "PRE-COMPACT"]);
  stream.push(compaction("ses_primary"));
  await waitFor(() => f.calls.filter((c) => c.mode === "session").length === 2, 1_000, "the rebuild");
  await waitFor(() => sessionN === 2, 1_000, "the rebuild answer");
  await new Promise((r) => setTimeout(r, 10));
  const after = ctxEvent();
  await context(after);
  stream.end();
  assert.deepEqual(after.system.map((p) => p.text), ["base", "POST-COMPACT"]);
  assert.deepEqual(subscribes, [[]], "still exactly one subscription");
  assert.deepEqual(
    f.calls.map((c) => [c.mode, c.payload.reason ?? c.payload.source]),
    [["session", "startup"], ["capture", "compact"], ["session", "compact"]],
    "capture first, then the rebuild",
  );
  assert.deepEqual(f.recorded, []);
});

test("compaction via setupGlitch: a failed rebuild keeps the old text and the capture still ran", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx();
  let sessionN = 0;
  const f = fakeDeps((c) => {
    if (c.mode !== "session") return { ok: true, result: null };
    if (++sessionN === 1) return sessionAnswer("ORIGINAL", "2026-11-11");
    throw new Error("bridge contract broken");
  });
  f.deps.now = () => NOV11_NOON;
  await setupGlitch(ctx as any, f.deps);
  const context = hook(regs, "session", "context");
  await context(ctxEvent());
  stream.push(compaction("ses_primary"));
  await waitFor(() => f.recorded.length === 1, 1_000, "the rebuild failure to be recorded");
  stream.end();
  assert.deepEqual(f.recorded, [{ reason: "plugin error in session rebuild", sessionId: "ses_primary" }]);
  const ev = ctxEvent();
  await context(ev);
  assert.deepEqual(ev.system.map((p) => p.text), ["base", "ORIGINAL"]);
  assert.ok(f.calls.some((c) => c.mode === "capture" && c.payload.reason === "compact"));
});

// --- D2: the turn's own model call after a compaction (docs/api-findings.md item 22) ---------

/**
 * Deps whose `capture` is held until released (or a timer), as the real ~70 ms bridge spawn is,
 * while `session` answers at once. Tracks when the capture finished and what each session call saw.
 */
function heldCaptureDeps(captureMs: number | null) {
  let sessionN = 0;
  const state = { captureDone: 0, captureStarted: 0, sessionSawCaptureDone: [] as boolean[] };
  const releases: Array<() => void> = [];
  const f = fakeDeps((c) => {
    if (c.mode === "capture") {
      state.captureStarted++;
      return new Promise<BridgeOutcome>((r) => {
        const done = () => {
          state.captureDone++;
          r({ ok: true, result: null });
        };
        if (captureMs === null) releases.push(done);
        else setTimeout(done, captureMs);
      });
    }
    if (c.mode === "session") {
      state.sessionSawCaptureDone.push(state.captureDone === state.captureStarted);
      return sessionAnswer(`BUILD-${++sessionN}`, "2026-11-11");
    }
    return { ok: true, result: null };
  });
  f.deps.now = () => NOV11_NOON;
  return { f, state, release: () => releases.splice(0).forEach((r) => r()) };
}

/** A context event after a compaction: the host's messages are the summary plus the prompt (item 14). */
const afterCompactionCall = (sid = "ses_primary") => ({ ...ctxEvent(sid), messages: [{ role: "user" }] });

test("D2: new process, the turn's context call arrives while the compact capture is in flight: its build is source compact, ONE session call", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx();
  const h = heldCaptureDeps(200);
  await setupGlitch(ctx as any, h.f.deps);
  const context = hook(regs, "session", "context");
  stream.push(compaction("ses_primary"));
  await waitFor(() => h.state.captureStarted === 1, 1_000, "the compact capture to start");
  const ev = afterCompactionCall();
  await context(ev);
  assert.equal(h.state.captureDone, 0, "the model call was not held behind the capture");
  assert.deepEqual(ev.system.map((p) => p.text), ["base", "BUILD-1"]);
  assert.deepEqual(
    sessionCalls(h.f).map((p) => [p.session_id, p.source, p.refresh]),
    [["ses_primary", "compact", false]],
    "the turn's own model call carried the compact build (no sweep, no hand-off, no keeper miss)",
  );
  await waitFor(() => h.state.captureDone === 1, 1_000, "the capture to finish");
  await new Promise((r) => setTimeout(r, 20));
  await context(afterCompactionCall());
  stream.end();
  assert.equal(sessionCalls(h.f).length, 1, "exactly ONE session build for this compaction");
  assert.deepEqual(h.f.calls.map((c) => [c.mode, c.payload.reason ?? c.payload.source]), [["capture", "compact"], ["session", "compact"]]);
  assert.deepEqual(h.f.recorded, []);
});

test("D2: existing entry: the compact rebuild is queued at receipt, waits for the capture, and runs ONCE", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx();
  const h = heldCaptureDeps(null);
  await setupGlitch(ctx as any, h.f.deps);
  const context = hook(regs, "session", "context");
  const first = ctxEvent();
  await context(first);
  assert.deepEqual(first.system.map((p) => p.text), ["base", "BUILD-1"]);
  stream.push(compaction("ses_primary"));
  await waitFor(() => h.state.captureStarted === 1, 1_000, "the compact capture to start");
  const during = afterCompactionCall();
  await context(during);
  assert.deepEqual(during.system.map((p) => p.text), ["base", "BUILD-1"], "the old text while the capture runs");
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sessionCalls(h.f).length, 1, "no rebuild before the capture of the pre-compaction chat has finished");
  h.release();
  await waitFor(() => sessionCalls(h.f).length === 2, 1_000, "the compact rebuild");
  await new Promise((r) => setTimeout(r, 20));
  const after = afterCompactionCall();
  await context(after);
  stream.end();
  assert.deepEqual(after.system.map((p) => p.text), ["base", "BUILD-2"]);
  assert.deepEqual(h.state.sessionSawCaptureDone, [true, true], "the capture completed before the rebuild ran");
  assert.deepEqual(
    h.f.calls.map((c) => [c.mode, c.payload.reason ?? c.payload.source]),
    [["session", "startup"], ["capture", "compact"], ["session", "compact"]],
    "exactly one rebuild, after the capture",
  );
  assert.deepEqual(h.f.recorded, []);
});

test("D2: a day refresh noticed while the compact capture runs waits for it, and joins the ONE compact rebuild", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx();
  const h = heldCaptureDeps(null);
  let clock = NOV11_NOON;
  h.f.deps.now = () => clock;
  await setupGlitch(ctx as any, h.f.deps);
  const context = hook(regs, "session", "context");
  await context(ctxEvent());
  stream.push(compaction("ses_primary"));
  await waitFor(() => h.state.captureStarted === 1, 1_000, "the compact capture to start");
  clock = NOV12_0010; // this call notices a new day and drains the runner during the capture
  await context(afterCompactionCall());
  await new Promise((r) => setTimeout(r, 20));
  assert.equal(sessionCalls(h.f).length, 1, "nothing rebuilt before the capture finished");
  h.release();
  await waitFor(() => sessionCalls(h.f).length === 2, 1_000, "the compact rebuild");
  await new Promise((r) => setTimeout(r, 20));
  stream.end();
  assert.deepEqual(h.state.sessionSawCaptureDone, [true, true]);
  assert.deepEqual(sessionCalls(h.f).map((p) => [p.source, p.refresh]), [["startup", false], ["compact", true]]);
});

test("D2: a context call with one message and NO compaction event stays startup", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx();
  const h = heldCaptureDeps(0);
  await setupGlitch(ctx as any, h.f.deps);
  const context = hook(regs, "session", "context");
  stream.push(turnEnd("ses_other"));
  stream.push(compaction("ses_other")); // another session's compaction changes nothing here
  await waitFor(() => h.state.captureDone === 2, 1_000, "the other session's captures");
  await context(afterCompactionCall("ses_primary"));
  stream.end();
  assert.deepEqual(sessionCalls(h.f).map((p) => [p.session_id, p.source]), [["ses_primary", "startup"]]);
});

test("D2: duplicate compaction events for one session coalesce into ONE build (new process and existing entry)", async () => {
  for (const existing of [false, true]) {
    resetPendingForTests();
    const h = heldCaptureDeps(null);
    const c = fakeCtx();
    const state = stateFor(c.ctx);
    const mem = registerMemoryIn(c.ctx as any, state, h.f.deps);
    const cap = createCapture(state, h.f.deps, { onCompactionReceived: mem.noteCompaction, onCompaction: mem.onCompaction });
    const context = hook(c.regs, "session", "context");
    if (existing) await context(ctxEvent());
    const one = cap.handleEvent(compaction("ses_primary"));
    const two = cap.handleEvent(compaction("ses_primary"));
    await context(afterCompactionCall());
    h.release();
    await waitFor(() => h.state.captureStarted === 2, 1_000, "the coalesced re-run capture");
    h.release();
    await Promise.all([one, two]);
    await mem.settled();
    await context(afterCompactionCall());
    const sources = sessionCalls(h.f).map((p) => p.source);
    assert.deepEqual(sources, existing ? ["startup", "compact"] : ["compact"], `existing=${existing}`);
    assert.deepEqual(h.f.recorded, []);
  }
});

// --- D2 (second fix): the session.get history marker (docs/api-findings.md 12b, items 22, 25) -
//
// The real host calls `context` before (or with) delivering `session.compaction.ended`, and after
// a compaction `messages` is length 1. The measured marker of a session with history is the key
// `outcome` on `ctx.session.get` (five-turns-resume.jsonl rows 1 vs 5; events-turn-end.jsonl rows
// 5 vs 54). Key sets below are the measured ones; the `outcome` value's type was not measured.

const NEW_SESSION_INFO = (id: string) => ({ id, projectID: "p", model: {}, cost: 0, tokens: {}, time: {}, location: { directory: GLITCH } });
const RESUMED_SESSION_INFO = (id: string) => ({ ...NEW_SESSION_INFO(id), outcome: "succeeded" });

/** memory-in with a scripted `ctx.session.get`, so the history marker can be shown or withheld. */
function historySetup(sessionGet: (a: { sessionID: string }) => Promise<unknown>) {
  resetPendingForTests();
  const c = fakeCtx({ sessionGet });
  const h = heldCaptureDeps(null);
  const state = stateFor(c.ctx);
  const mem = registerMemoryIn(c.ctx as any, state, h.f.deps);
  const cap = createCapture(state, h.f.deps, { onCompactionReceived: mem.noteCompaction, onCompaction: mem.onCompaction });
  const context = hook(c.regs, "session", "context");
  const prompt = hook(c.regs, "session", "prompt");
  return { ...c, h, state, mem, cap, context, prompt };
}

test("D2 marker: a first context call with ONE message on a session whose session.get carries `outcome` builds source resume", async () => {
  const m = historySetup(async (a) => RESUMED_SESSION_INFO(a.sessionID));
  const ev = afterCompactionCall();
  await m.context(ev);
  assert.deepEqual(ev.system.map((p) => p.text), ["base", "BUILD-1"], "the call waited for its first build");
  assert.deepEqual(sessionCalls(m.h.f).map((p) => [p.session_id, p.source, p.refresh]), [["ses_primary", "resume", false]]);
  assert.deepEqual(m.h.f.recorded, []);
});

test("D2 marker: the same call without the marker (the measured brand-new key set) stays startup", async () => {
  const m = historySetup(async (a) => NEW_SESSION_INFO(a.sessionID));
  await m.context(afterCompactionCall());
  await m.context(afterCompactionCall()); // turn 1's later calls never rebuild
  assert.deepEqual(sessionCalls(m.h.f).map((p) => p.source), ["startup"]);
  assert.deepEqual(m.h.f.recorded, []);
});

test("D2 marker: a compaction noted in time still wins over the marker: source compact, ONE build", async () => {
  // (a) noted before the context call: decided at once, no history lookup needed.
  const a = historySetup(async (x) => RESUMED_SESSION_INFO(x.sessionID));
  const one = a.cap.handleEvent(compaction("ses_primary"));
  await a.context(afterCompactionCall());
  assert.deepEqual(sessionCalls(a.h.f).map((p) => p.source), ["compact"]);
  await waitFor(() => a.h.state.captureStarted === 1, 1_000, "the compact capture to start");
  a.h.release();
  await one;
  await a.mem.settled();
  assert.deepEqual(sessionCalls(a.h.f).map((p) => p.source), ["compact"], "no second build");

  // (b) noted WHILE the history lookup runs (the real host's race): still ONE compact build.
  const answers: Array<() => void> = [];
  const b = historySetup((x) => new Promise((r) => answers.push(() => r(RESUMED_SESSION_INFO(x.sessionID)))));
  const call = b.context(afterCompactionCall());
  await waitFor(() => b.getCalls.length === 1, 1_000, "the history lookup to start");
  const two = b.cap.handleEvent(compaction("ses_primary"));
  answers.splice(0).forEach((r) => r());
  await call;
  assert.deepEqual(sessionCalls(b.h.f).map((p) => p.source), ["compact"]);
  await waitFor(() => b.h.state.captureStarted === 1, 1_000, "the compact capture to start");
  b.h.release();
  await two;
  await b.mem.settled();
  await b.context(afterCompactionCall());
  assert.deepEqual(sessionCalls(b.h.f).map((p) => p.source), ["compact"], "the capture-finished signal queued no second build");
  assert.deepEqual(b.h.f.recorded, []);
});

test("D2 marker: a post-compaction first call classed resume is followed by the compact build when the event arrives", async () => {
  const m = historySetup(async (a) => RESUMED_SESSION_INFO(a.sessionID));
  await m.context(afterCompactionCall()); // the host's call came first (items 22, 25)
  const done = m.cap.handleEvent(compaction("ses_primary"));
  await waitFor(() => m.h.state.captureStarted === 1, 1_000, "the compact capture to start");
  m.h.release();
  await done;
  await m.mem.settled();
  assert.deepEqual(
    m.h.f.calls.map((c) => [c.mode, c.payload.reason ?? c.payload.source]),
    [["session", "resume"], ["capture", "compact"], ["session", "compact"]],
    "resume (no sweep, per the engine) first, then the compact rebuild after the capture",
  );
  assert.deepEqual(m.h.state.sessionSawCaptureDone, [true, true]);
});

test("D2 marker: session.get rejecting, hanging, null, no `outcome`, or `outcome: null` fails open to startup, recording nothing", async (t) => {
  const cases: Array<[string, (a: { sessionID: string }) => Promise<unknown>]> = [
    ["rejects", async () => {
      throw new Error("host refused");
    }],
    ["null", async () => null],
    ["not an object", async () => "ses"],
    ["no outcome", async (a) => NEW_SESSION_INFO(a.sessionID)],
    ["outcome null", async (a) => ({ ...NEW_SESSION_INFO(a.sessionID), outcome: null })],
  ];
  for (const [label, get] of cases) {
    const m = historySetup(get);
    await m.context(afterCompactionCall());
    assert.deepEqual(sessionCalls(m.h.f).map((p) => p.source), ["startup"], label);
    assert.deepEqual(m.h.f.recorded, [], `${label}: not recorded`);
  }
  // No session.get at all.
  const none = historySetup(async () => ({}));
  delete none.ctx.session.get;
  await none.context(afterCompactionCall());
  assert.deepEqual(sessionCalls(none.h.f).map((p) => p.source), ["startup"]);
  assert.deepEqual(none.h.f.recorded, []);
  // A lookup that never answers: the PARENT_LOOKUP_TIMEOUT_MS bound, then startup.
  const hang = historySetup(() => new Promise(() => {}));
  const started = Date.now();
  await hang.context(afterCompactionCall());
  const waited = Date.now() - started;
  // ONE bound, not two: the first build does not wait again on the parent lookup it would repeat.
  assert.ok(waited >= PARENT_LOOKUP_TIMEOUT_MS - 50 && waited < PARENT_LOOKUP_TIMEOUT_MS * 1.5, `one bounded wait (${waited} ms)`);
  assert.deepEqual(sessionCalls(hang.h.f).map((p) => [p.source, p.parent_id]), [["startup", ""]]);
  assert.equal(hang.getCalls.length, 1);
  assert.deepEqual(hang.h.f.recorded, []);
  t.diagnostic(`hanging lookup waited ${waited} ms`);
});

test("D2 marker: at most ONE extra session.get per session per process, and none when the prompt hook's lookup already made one", async () => {
  // A session.created seed carries no marker: one fresh get, shared by concurrent first calls.
  const seeded = historySetup(async (a) => RESUMED_SESSION_INFO(a.sessionID));
  seeded.state.sessionInfo.seed({ sessionID: "ses_primary" });
  await Promise.all([seeded.context(afterCompactionCall()), seeded.context(afterCompactionCall())]);
  await seeded.context(afterCompactionCall());
  assert.deepEqual(await seeded.state.sessionInfo.history("ses_primary"), true);
  assert.deepEqual(seeded.getCalls, ["ses_primary"], "one get for this session in this process");
  assert.deepEqual(sessionCalls(seeded.h.f).map((p) => p.source), ["resume"]);

  // The prompt hook runs first on the real host (it looks the parent up): its get is reused.
  const prompted = historySetup(async (a) => RESUMED_SESSION_INFO(a.sessionID));
  await prompted.prompt({ sessionID: "ses_primary", prompt: { text: "hi" } });
  await prompted.context(afterCompactionCall());
  assert.deepEqual(prompted.getCalls, ["ses_primary"], "no extra round trip");
  assert.deepEqual(sessionCalls(prompted.h.f).map((p) => p.source), ["resume"]);

  // A failed lookup is not retried on every call (once per session per process), and fails open.
  let n = 0;
  const failing = historySetup(async () => {
    n++;
    throw new Error("down");
  });
  failing.state.sessionInfo.seed({ sessionID: "ses_primary" });
  await failing.context(afterCompactionCall());
  await failing.state.sessionInfo.history("ses_primary");
  assert.equal(n, 1);

  // History already in `messages`: no lookup at all.
  const resumed = historySetup(async (a) => RESUMED_SESSION_INFO(a.sessionID));
  resumed.state.sessionInfo.seed({ sessionID: "ses_primary" });
  await resumed.context({ ...ctxEvent(), messages: [{}, {}, {}] });
  assert.deepEqual(resumed.getCalls, []);
  assert.deepEqual(sessionCalls(resumed.h.f).map((p) => p.source), ["resume"]);
});

test("D2 marker: a brand-new session at turn 1 through setupGlitch stays startup (default fake get has no marker)", async () => {
  resetPendingForTests();
  const { ctx, regs, stream } = fakeCtx(); // session.get -> {id} only, as a new session's lacks `outcome`
  const h = heldCaptureDeps(0);
  await setupGlitch(ctx as any, h.f.deps);
  await hook(regs, "session", "prompt")({ sessionID: "ses_new", prompt: { text: "hello" } });
  await hook(regs, "session", "context")({ ...ctxEvent("ses_new"), messages: [{ role: "user" }] });
  stream.end();
  assert.deepEqual(sessionCalls(h.f).map((p) => [p.session_id, p.source]), [["ses_new", "startup"]]);
  assert.deepEqual(h.f.recorded, []);
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

// ---------------------------------------------------------------------------------------------
// CP4: capture (memory-out)

import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { callBridge as realCallBridge } from "../../src/plugin/bridge-client.ts";
import { captureNoticeFor, takePendingNotice } from "../../src/plugin/failure.ts";
import {
  COMPACTION_EVENT,
  TURN_END_EVENTS,
  createCapture,
  makeSessionInfo,
} from "../../src/plugin/setup.ts";
import type { GlitchState } from "../../src/plugin/setup.ts";

// Any rejection nobody handles, anywhere in this file, fails the last test.
const unhandled: unknown[] = [];
process.on("unhandledRejection", (reason) => unhandled.push(reason));

const FAKE_CAPTURE = join(import.meta.dirname, "fixtures", "fake-capture.mjs");

const hostEvent = (type: string, data: Record<string, unknown>) => ({ id: "evt_1", created: 1, type, durable: true, data });
const turnEnd = (sessionID: string, type = "session.execution.succeeded") => hostEvent(type, { sessionID });
const compaction = (sessionID: string) =>
  hostEvent(COMPACTION_EVENT, { sessionID, reason: "auto", model: {}, text: "summary", cost: 0, tokens: {} });

function stateFor(ctx: any, sessionDir = GLITCH): GlitchState {
  const sessionInfo = makeSessionInfo(ctx);
  return {
    root: GLITCH,
    sessionDir,
    bridgePath: BRIDGE,
    sessionInfo,
    parentIdOf: async (sid) => {
      const r = await sessionInfo.lookup(String(sid));
      return r.ok ? r.parentId : "";
    },
  };
}

async function waitFor(cond: () => boolean, ms = 3_000, what = "condition"): Promise<void> {
  const until = Date.now() + ms;
  while (!cond()) {
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, 10));
  }
}

/** Deps whose callBridge spawns the real client against fake-capture.mjs (a real child process). */
function processDeps(behaviour: { mode?: string; delayMs?: number; timeoutMs?: number } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "oc-capture-"));
  const logPath = join(dir, "errors.log");
  const capLog = join(dir, "capture.jsonl");
  const statePath = join(dir, "state.txt");
  const f = fakeDeps();
  const settings = { mode: behaviour.mode ?? "empty", delayMs: behaviour.delayMs ?? 0, timeoutMs: behaviour.timeoutMs ?? 5_000 };
  f.deps.callBridge = (root, mode, payload, opts) => {
    f.calls.push({ root, mode, payload, opts });
    return realCallBridge(root, mode, payload, {
      ...opts,
      command: process.execPath,
      bridgePath: FAKE_CAPTURE,
      buildArgs: (_r, b, m) => [b, m],
      logPath,
      timeoutMs: settings.timeoutMs,
      env: {
        ...process.env,
        FAKE_CAPTURE_LOG: capLog,
        FAKE_CAPTURE_STATE: statePath,
        FAKE_CAPTURE_MODE: settings.mode,
        FAKE_CAPTURE_DELAY_MS: String(settings.delayMs),
      },
    });
  };
  const lines = (): Array<Record<string, any>> =>
    existsSync(capLog) ? readFileSync(capLog, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
  const errors = (): string => (existsSync(logPath) ? readFileSync(logPath, "utf8") : "");
  return { ...f, dir, settings, lines, errors, statePath, cleanup: () => rmSync(dir, { recursive: true, force: true }) };
}

test("capture: a turn end and a compaction send {session_id, session_dir, parent_id, reason}, session_dir realpathed", async () => {
  resetPendingForTests();
  const p = processDeps();
  try {
    const real = mkdtempSync(join(p.dir, "real-"));
    const link = join(p.dir, "link");
    symlinkSync(real, link);
    const { ctx } = fakeCtx({ sessionGet: async ({ sessionID }) => ({ id: sessionID, location: { directory: link } }) });
    p.deps.realpath = (x) => realpathSync.native(x);
    const cap = createCapture(stateFor(ctx), p.deps);
    await cap.handleEvent(turnEnd("ses_primary"));
    await cap.settled();
    await cap.handleEvent(compaction("ses_primary"));
    const starts = p.lines().filter((l) => l.phase === "start");
    assert.equal(starts.length, 2);
    assert.ok(starts.every((s) => s.mode === "capture"));
    const expectDir = realpathSync.native(real);
    assert.deepEqual(starts[0].payload, { session_id: "ses_primary", session_dir: expectDir, parent_id: "", reason: "turn" });
    assert.deepEqual(starts[1].payload, { session_id: "ses_primary", session_dir: expectDir, parent_id: "", reason: "compact" });
    assert.equal(p.errors(), "", "empty output is success: nothing recorded");
    assert.equal(takePendingNotice("ses_primary"), null);
    assert.deepEqual(p.recorded, []);
  } finally {
    p.cleanup();
  }
});

test("capture: a session with no location falls back to the plugin's bound directory", async () => {
  const f = fakeDeps();
  const { ctx } = fakeCtx(); // session.get -> {id} only
  const cap = createCapture(stateFor(ctx, GLITCH + "/workspaces/proj"), f.deps);
  await cap.capture("ses_primary", "turn");
  assert.equal(f.calls[0].payload.session_dir, GLITCH + "/workspaces/proj");
  // and a realpath that throws keeps the raw directory
  const g = fakeDeps();
  g.deps.realpath = () => {
    throw new Error("ENOENT");
  };
  const second = fakeCtx({ sessionGet: async () => ({ location: { directory: "/gone/dir" } }) });
  await createCapture(stateFor(second.ctx), g.deps).capture("ses_primary", "turn");
  assert.equal(g.calls[0].payload.session_dir, "/gone/dir");
});

test("capture: {error: label} from the bridge is recorded visibly (errors log + pending notice)", async () => {
  resetPendingForTests();
  const p = processDeps({ mode: "error" });
  try {
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), p.deps);
    await cap.handleEvent(compaction("ses_err"));
    assert.match(p.errors(), /session=ses_err bridge error: capture-export-failed/);
    // B2: a capture failure is told as a chat not saved, never as the guards failing.
    assert.equal(takePendingNotice("ses_err"), captureNoticeFor("bridge error: capture-export-failed"));
  } finally {
    p.cleanup();
  }
});

test("capture: exit non-zero is recorded visibly", async () => {
  resetPendingForTests();
  const p = processDeps({ mode: "exit1" });
  try {
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), p.deps);
    await cap.handleEvent(turnEnd("ses_x1"));
    await cap.settled();
    assert.match(p.errors(), /session=ses_x1 exit 1/);
  } finally {
    p.cleanup();
  }
});

test("capture: a child session (parentID) is skipped, looked up once", async () => {
  const f = fakeDeps();
  const { ctx, getCalls } = fakeCtx({ sessionGet: async ({ sessionID }) => ({ id: sessionID, parentID: "ses_parent" }) });
  const cap = createCapture(stateFor(ctx), f.deps);
  await cap.handleEvent(turnEnd("ses_child"));
  await cap.settled();
  await cap.handleEvent(compaction("ses_child"));
  assert.equal(f.calls.length, 0);
  assert.deepEqual(f.recorded, []);
  assert.deepEqual(getCalls, ["ses_child"], "the parent is cached after one successful lookup");
});

test("capture: an unresolved parent is skipped, recorded, and retried on the next event", async () => {
  let n = 0;
  const f = fakeDeps();
  const { ctx, getCalls } = fakeCtx({
    sessionGet: async ({ sessionID }) => {
      n++;
      if (n === 1) throw new Error("session store down");
      return { id: sessionID };
    },
  });
  const cap = createCapture(stateFor(ctx), f.deps);
  await cap.handleEvent(turnEnd("ses_a"));
  await cap.settled();
  assert.equal(f.calls.length, 0, "not treated as primary when the lookup failed");
  assert.deepEqual(f.recorded, [{ reason: "plugin error in capture (session lookup)", sessionId: "ses_a" }]);
  await cap.handleEvent(turnEnd("ses_a"));
  await cap.settled();
  assert.equal(f.calls.length, 1, "the retry succeeded and the chat is captured");
  assert.deepEqual(getCalls, ["ses_a", "ses_a"]);
});

test("capture: a host with no session.get, or a lookup that hangs, never counts as primary", async (t) => {
  const none = fakeCtx();
  delete none.ctx.session.get;
  const f = fakeDeps();
  await createCapture(stateFor(none.ctx), f.deps).capture("ses_a", "turn");
  assert.equal(f.calls.length, 0);
  assert.equal(f.recorded.length, 1);

  t.mock.timers.enable({ apis: ["setTimeout"] });
  const hung = fakeCtx({ sessionGet: () => new Promise(() => {}) });
  const g = fakeDeps();
  const done = createCapture(stateFor(hung.ctx), g.deps).capture("ses_b", "turn");
  await Promise.resolve();
  await Promise.resolve();
  t.mock.timers.tick(PARENT_LOOKUP_TIMEOUT_MS + 1);
  await done;
  assert.equal(g.calls.length, 0);
  assert.deepEqual(g.recorded, [{ reason: "plugin error in capture (session lookup)", sessionId: "ses_b" }]);
});

test("capture: a burst of 5 turn ends for one session spawns at most 2 bridge calls, the last reading the latest state", async () => {
  const p = processDeps({ delayMs: 300 });
  try {
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), p.deps);
    writeFileSync(p.statePath, "v1");
    await cap.handleEvent(turnEnd("ses_burst"));
    await waitFor(() => p.lines().some((l) => l.phase === "start"), 5_000, "the first capture to start");
    for (const v of ["v2", "v3", "v4", "v5"]) {
      writeFileSync(p.statePath, v);
      await cap.handleEvent(turnEnd("ses_burst"));
    }
    await cap.settled();
    const starts = p.lines().filter((l) => l.phase === "start");
    assert.equal(starts.length, 2, "one in flight + one coalesced re-run");
    assert.deepEqual(starts.map((s) => s.state), ["v1", "v5"]);
    assert.equal(p.calls.length, 2);
  } finally {
    p.cleanup();
  }
});

test("capture: a burst that arrives all at once still spawns at most 2 calls; compaction upgrades the pending reason", async () => {
  const f = fakeDeps();
  const { ctx } = fakeCtx();
  const cap = createCapture(stateFor(ctx), f.deps);
  const events = [turnEnd("s"), turnEnd("s"), compaction("s"), turnEnd("s"), turnEnd("s")];
  const handled = Promise.all(events.map((e) => cap.handleEvent(e)));
  await handled;
  await cap.settled();
  assert.deepEqual(f.calls.map((c) => c.payload.reason), ["turn", "compact"]);
});

test("capture: different sessions are independent (one stuck session does not hold another)", async () => {
  let releaseA: () => void = () => {};
  const f = fakeDeps((c) =>
    c.payload.session_id === "ses_A"
      ? new Promise<BridgeOutcome>((r) => (releaseA = () => r({ ok: true, result: null })))
      : { ok: true, result: null },
  );
  const { ctx } = fakeCtx();
  const cap = createCapture(stateFor(ctx), f.deps);
  await cap.handleEvent(turnEnd("ses_A"));
  await waitFor(() => f.calls.length === 1, 1_000, "ses_A in flight");
  await cap.handleEvent(turnEnd("ses_A")); // pending behind A's own run
  await cap.handleEvent(turnEnd("ses_B"));
  await waitFor(() => f.calls.some((c) => c.payload.session_id === "ses_B"), 1_000, "ses_B to run");
  assert.equal(f.calls.filter((c) => c.payload.session_id === "ses_A").length, 1, "A's re-run waits for A only");
  releaseA();
  await waitFor(() => f.calls.filter((c) => c.payload.session_id === "ses_A").length === 2, 1_000, "A's re-run");
  releaseA();
  await cap.settled();
});

test("capture: the compaction handler resolves only after the bridge call ends; a turn end returns at once", async () => {
  const p = processDeps({ delayMs: 300 });
  try {
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), p.deps);
    await cap.handleEvent(compaction("ses_c"));
    assert.ok(p.lines().some((l) => l.phase === "end" && l.session === "ses_c"), "the stage landed before the handler resolved");

    const t0 = Date.now();
    await cap.handleEvent(turnEnd("ses_t"));
    const took = Date.now() - t0;
    assert.ok(took < 100, `turn-end handler took ${took}ms`);
    assert.ok(!p.lines().some((l) => l.phase === "end" && l.session === "ses_t"), "the turn capture is still running");
    await cap.settled();
    assert.ok(p.lines().some((l) => l.phase === "end" && l.session === "ses_t"));
  } finally {
    p.cleanup();
  }
});

test("capture: a hanging bridge never blocks the turn-end handler, and its timeout is recorded visibly", async () => {
  resetPendingForTests();
  const p = processDeps({ mode: "hang", timeoutMs: 400 });
  try {
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), p.deps);
    const t0 = Date.now();
    await cap.handleEvent(turnEnd("ses_h"));
    await cap.handleEvent(turnEnd("ses_h"));
    assert.ok(Date.now() - t0 < 100, "both turn-end handlers returned at once");
    await cap.settled();
    assert.match(p.errors(), /session=ses_h timeout/);
    assert.equal(p.calls.length, 2, "the coalesced re-run still ran after the hung one was killed");
  } finally {
    p.cleanup();
  }
});

test("capture: a throwing or rejecting callBridge, and a throwing recorder, never escape", async () => {
  for (const bad of [
    (() => {
      throw new Error("sync");
    }) as unknown as (c: Call) => BridgeOutcome,
    (() => Promise.reject(new Error("async"))) as unknown as (c: Call) => BridgeOutcome,
  ]) {
    const f = fakeDeps(bad);
    const { ctx } = fakeCtx();
    const cap = createCapture(stateFor(ctx), f.deps);
    await cap.handleEvent(turnEnd("s"));
    await cap.handleEvent(compaction("s"));
    await cap.settled();
    assert.ok(f.recorded.length >= 1);
    assert.match(f.recorded[0].reason, /plugin error in capture/);

    const g = fakeDeps(bad);
    g.deps.failure.record = () => {
      throw new Error("log disk full");
    };
    const second = createCapture(stateFor(fakeCtx().ctx), g.deps);
    await second.handleEvent(turnEnd("s"));
    await second.handleEvent(compaction("s"));
    await second.settled();
  }
  for (const junk of [null, undefined, 42, "x", {}, { type: 7 }, { type: "session.execution.succeeded" }, { type: "session.execution.succeeded", data: null }]) {
    const f = fakeDeps();
    const cap = createCapture(stateFor(fakeCtx().ctx), f.deps);
    await cap.handleEvent(junk);
    assert.equal(f.calls.length, 0);
  }
});

test("capture via setupGlitch: subscribe() is called with no arguments, the three turn-end names trigger capture, unrelated events do nothing", async () => {
  resetPendingForTests();
  const { ctx, stream, subscribes } = fakeCtx();
  const f = fakeDeps();
  await setupGlitch(ctx as any, f.deps);
  assert.deepEqual(subscribes, [[]], "the wildcard form, once");
  assert.deepEqual(f.warnings, []);
  for (const type of ["session.step.ended", "session.compaction.started", "session.prompt", "session.renamed", "location.shutdown", "session.idle"]) {
    stream.push(hostEvent(type, { sessionID: "ses_u" }));
  }
  stream.push(turnEnd("ses_1", TURN_END_EVENTS[0]));
  await waitFor(() => f.calls.length === 1, 1_000, "succeeded");
  stream.push(turnEnd("ses_2", TURN_END_EVENTS[1]));
  await waitFor(() => f.calls.length === 2, 1_000, "failed");
  stream.push(turnEnd("ses_3", TURN_END_EVENTS[2]));
  await waitFor(() => f.calls.length === 3, 1_000, "interrupted");
  stream.push(compaction("ses_4"));
  await waitFor(() => f.calls.length === 4, 1_000, "compaction");
  stream.end();
  assert.deepEqual(
    f.calls.map((c) => [c.mode, c.payload.session_id, c.payload.reason]),
    [["capture", "ses_1", "turn"], ["capture", "ses_2", "turn"], ["capture", "ses_3", "turn"], ["capture", "ses_4", "compact"]],
  );
  assert.equal(f.calls[0].opts?.bridgePath, BRIDGE);
  assert.deepEqual(f.recorded, []);
});

test("capture via setupGlitch: a session.created event seeds the parent, so a child is skipped without a lookup", async () => {
  const { ctx, stream, getCalls } = fakeCtx();
  const f = fakeDeps();
  await setupGlitch(ctx as any, f.deps);
  stream.push(hostEvent("session.created", { sessionID: "ses_kid", parentID: "ses_mum", location: { directory: GLITCH } }));
  stream.push(turnEnd("ses_kid"));
  stream.push(turnEnd("ses_other"));
  await waitFor(() => f.calls.length === 1, 1_000, "the primary capture");
  stream.end();
  assert.equal(f.calls[0].payload.session_id, "ses_other");
  assert.deepEqual(getCalls, ["ses_other"], "the child was never looked up");
});

test("setup does not throw when event.subscribe is missing, throws, rejects, or answers junk; the other hooks still register", async () => {
  const variants: Array<[string, (ctx: any) => void]> = [
    ["no event domain", (ctx) => delete ctx.event],
    ["no subscribe", (ctx) => delete ctx.event.subscribe],
    ["subscribe throws", (ctx) => (ctx.event.subscribe = () => { throw new Error("unknown"); })],
    ["subscribe rejects", (ctx) => (ctx.event.subscribe = () => Promise.reject(new Error("refused")))],
    ["subscribe answers junk", (ctx) => (ctx.event.subscribe = () => 42)],
    ["stream errors", (ctx) => (ctx.event.subscribe = () => ({ async *[Symbol.asyncIterator]() { throw new Error("broken"); } }))],
  ];
  for (const [label, mutate] of variants) {
    const { ctx, regs } = fakeCtx();
    mutate(ctx);
    const f = fakeDeps();
    await setupGlitch(ctx as any, f.deps);
    await waitFor(() => f.recorded.length === 1, 1_000, `${label}: recorded`);
    assert.equal(f.recorded[0].reason, "event subscription failed: capture", label);
    assert.equal(f.warnings.length, 1, label);
    assert.match(f.warnings[0], /NOT captured into memory/, label);
    assert.equal(regs.length, 4, `${label}: guards and memory-in still registered`);
  }
});

test("no unhandled promise rejection anywhere in this file", async () => {
  await new Promise((r) => setTimeout(r, 50));
  assert.deepEqual(unhandled, []);
});
