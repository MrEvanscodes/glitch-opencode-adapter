import { strict as assert } from "node:assert";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import {
  BRIDGE_OVERRIDE_ENV,
  DEFAULT_TIMEOUTS,
  type CallOptions,
  type Mode,
  callBridge,
  defaultBridgePath,
  parseBridgeOutput,
  resolveBridgePath,
  uvArgs,
} from "../../src/plugin/bridge-client.ts";
import { captureNoticeFor, noticeFor, resetPendingForTests, takePendingNotice } from "../../src/plugin/failure.ts";

const FIXTURES = join(import.meta.dirname, "fixtures");
const WORKSPACE = resolve(import.meta.dirname, "..", "..");
const MARKER = "SECRET-MARKER-7f3a9c";
const PAYLOAD = { tool_name: "shell", tool_input: { command: `echo ${MARKER}` }, session_id: "ses_t", session_dir: "/x" };

let dir = "";
let logPath = "";

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "oc-bridge-client-"));
  logPath = join(dir, "errors.log");
  resetPendingForTests();
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/** Run a fake bridge through node instead of uv: the seam, no monkeypatching. */
function fake(name: string, extra: CallOptions = {}): CallOptions {
  return {
    command: process.execPath,
    bridgePath: join(FIXTURES, name),
    buildArgs: (_root, bridgePath, mode) => [bridgePath, mode],
    logPath,
    ...extra,
  };
}

function logText(): string {
  return existsSync(logPath) ? readFileSync(logPath, "utf8") : "";
}

function assertNoLeak(reason: string): void {
  assert.ok(!reason.includes(MARKER), `reason leaked the payload: ${reason}`);
  assert.ok(!logText().includes(MARKER), "errors log leaked the payload");
  assert.ok(!(takePendingNotice("ses_t") ?? "").includes(MARKER), "notice leaked the payload");
}

test("default timeouts per mode", () => {
  assert.deepEqual({ ...DEFAULT_TIMEOUTS }, { pre: 10_000, post: 10_000, session: 30_000, prompt: 10_000, capture: 10_000 });
});

test("uvArgs is the documented invocation", () => {
  assert.deepEqual(uvArgs("/G", "/b/opencode_bridge.py", "pre"), [
    "run", "--no-project", "--directory", join("/G", ".claude", "scripts"), "python", "/b/opencode_bridge.py", "pre",
  ]);
});

test("the default bridge path is the workspace bridge, relative to the plugin file", () => {
  const path = defaultBridgePath();
  assert.equal(resolve(path), join(WORKSPACE, "src", "bridge", "opencode_bridge.py"));
  assert.ok(existsSync(path));
});

test("the override env var replaces the path and warns loudly every time", () => {
  const lines: string[] = [];
  const env = { [BRIDGE_OVERRIDE_ENV]: "/tmp/broken.py" };
  assert.equal(resolveBridgePath(env, (l) => lines.push(l)), "/tmp/broken.py");
  assert.equal(resolveBridgePath(env, (l) => lines.push(l)), "/tmp/broken.py");
  assert.equal(lines.length, 2);
  assert.match(lines[0], /GLITCH_OPENCODE_BRIDGE_OVERRIDE/);
  assert.match(lines[0], /TEST-ONLY/);
  const quiet: string[] = [];
  assert.equal(resolveBridgePath({}, (l) => quiet.push(l)), defaultBridgePath());
  assert.equal(quiet.length, 0);
});

test("empty stdout is an allow (result null), nothing recorded", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-empty.mjs"));
  assert.deepEqual(out, { ok: true, result: null });
  assert.equal(logText(), "");
  assert.equal(takePendingNotice("ses_t"), null);
});

test("a deny object comes back whole", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-deny.mjs"));
  assert.deepEqual(out, {
    ok: true,
    result: { deny: { kind: "git-clean", member: "Blocked for you.", model: "BLOCKED: forced git clean." } },
  });
});

test("a note comes back on post", async () => {
  const out = await callBridge(dir, "post", PAYLOAD, fake("fake-note.mjs"));
  assert.deepEqual(out, { ok: true, result: { note: "A possible secret was printed." } });
});

test("a context comes back on session and prompt", async () => {
  for (const mode of ["session", "prompt"] as Mode[]) {
    const out = await callBridge(dir, mode, PAYLOAD, fake("fake-context.mjs"));
    assert.deepEqual(out, { ok: true, result: { context: "You are Glitch." } });
  }
});

test("the payload goes on stdin as JSON, mode as argv, PYTHONDONTWRITEBYTECODE=1 in env", async () => {
  const out = await callBridge(dir, "prompt", PAYLOAD, fake("fake-echo.mjs", { env: { PATH: process.env.PATH } }));
  assert.equal(out.ok, true);
  if (!out.ok || out.result === null || !("context" in out.result)) throw new Error("no context");
  const seen = JSON.parse(out.result.context);
  assert.deepEqual(JSON.parse(seen.stdin), PAYLOAD);
  assert.equal(seen.mode, "prompt");
  assert.equal(seen.pydwb, "1");
});

test("a result shape in the wrong mode is bad output (a deny only counts on pre)", async () => {
  const out = await callBridge(dir, "post", PAYLOAD, fake("fake-deny.mjs"));
  assert.deepEqual(out, { ok: false, reason: "bad output" });
});

test("exit 1 -> 'exit 1', recorded, no payload leak", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-exit1.mjs"));
  assert.deepEqual(out, { ok: false, reason: "exit 1" });
  assert.match(logText(), /session=ses_t exit 1\n$/);
  assertNoLeak(out.ok ? "" : out.reason);
});

test("a hang past the timeout -> 'timeout', and the child is killed", async () => {
  const started = Date.now();
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-hang.mjs", { timeoutMs: 300 }));
  const took = Date.now() - started;
  assert.deepEqual(out, { ok: false, reason: "timeout" });
  assert.ok(took < 5_000, `took ${took}ms`);
  assert.match(logText(), /timeout/);
});

test("garbage stdout -> 'bad output', and the garbage (holding the payload) never reaches the reason", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-garbage.mjs"));
  assert.deepEqual(out, { ok: false, reason: "bad output" });
  assertNoLeak(out.ok ? "" : out.reason);
});

test("two JSON documents are bad output", async () => {
  const out = await callBridge(dir, "session", PAYLOAD, fake("fake-two-objects.mjs"));
  assert.deepEqual(out, { ok: false, reason: "bad output" });
});

test("{error} -> 'bridge error: <label>', recorded, notice pending once", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-error.mjs"));
  assert.deepEqual(out, { ok: false, reason: "bridge error: DelegateUnavailable" });
  assert.match(logText(), /bridge error: DelegateUnavailable/);
  assert.equal(takePendingNotice("ses_t"), noticeFor("bridge error: DelegateUnavailable"));
  assert.equal(takePendingNotice("ses_t"), null);
});

test("a huge multi-line {error} label is flattened and capped", async () => {
  const out = await callBridge(dir, "pre", PAYLOAD, fake("fake-long-error.mjs"));
  assert.equal(out.ok, false);
  if (out.ok) return;
  assert.ok(out.reason.startsWith("bridge error: xxx"));
  assert.ok(out.reason.length <= 100, `reason length ${out.reason.length}`);
  assert.ok(!out.reason.includes("\n"));
  assert.equal(logText().split("\n").filter(Boolean).length, 1);
});

test("uv missing -> 'uv not found' (ENOENT), recorded", async () => {
  const empty = mkdtempSync(join(dir, "nopath-"));
  const out = await callBridge(dir, "pre", PAYLOAD, {
    command: "uv",
    bridgePath: "/nowhere/opencode_bridge.py",
    env: { PATH: empty, Path: empty },
    logPath,
  });
  assert.deepEqual(out, { ok: false, reason: "uv not found" });
  assert.match(logText(), /uv not found/);
  // the 'error' event is followed by a 'close' (code -2): the failure must count once, not twice
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(logText().split("\n").filter(Boolean).length, 1, logText());
  assertNoLeak("uv not found");
});

test("the session for the notice comes from opts.sessionId over payload.session_id", async () => {
  await callBridge(dir, "pre", PAYLOAD, fake("fake-exit1.mjs", { sessionId: "ses_other" }));
  assert.equal(takePendingNotice("ses_t"), null);
  assert.equal(takePendingNotice("ses_other"), noticeFor("exit 1"));
});

test("an unserialisable payload fails without spawning", async () => {
  const cyclic: Record<string, unknown> = { session_id: "ses_t" };
  cyclic.self = cyclic;
  const out = await callBridge(dir, "pre", cyclic, fake("fake-empty.mjs"));
  assert.deepEqual(out, { ok: false, reason: "bad payload" });
});

test("parseBridgeOutput: the contract table", () => {
  assert.deepEqual(parseBridgeOutput("", "pre"), { kind: "result", result: null });
  assert.deepEqual(parseBridgeOutput("﻿", "pre"), { kind: "result", result: null });
  assert.deepEqual(parseBridgeOutput('{"context":""}', "prompt"), { kind: "result", result: null });
  assert.deepEqual(parseBridgeOutput('{"error":"bad-event"}', "post"), { kind: "error", label: "bad-event" });
  assert.deepEqual(parseBridgeOutput("[]", "pre"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput("null", "pre"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"deny":{"kind":"k","member":"m"}}', "pre"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"note":"n","extra":1}', "post"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"error":""}', "post"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"c"}', "pre"), { kind: "bad" });
});

test("parseBridgeOutput: session accepts {context} or {context, date} (timezone: see below), nothing looser", () => {
  const p = (s: string) => parseBridgeOutput(s, "session");
  assert.deepEqual(p(""), { kind: "result", result: null });
  assert.deepEqual(p('{"context":"x"}'), { kind: "result", result: { context: "x" } });
  assert.deepEqual(p('{"context":"x","date":"2026-11-11"}'), { kind: "result", result: { context: "x", date: "2026-11-11" } });
  assert.deepEqual(p('{"date":"2026-11-11","context":"x"}'), { kind: "result", result: { context: "x", date: "2026-11-11" } });
  assert.deepEqual(p('{"error":"session-start-context unavailable"}'), { kind: "error", label: "session-start-context unavailable" });
  // bad date shapes
  assert.deepEqual(p('{"context":"x","date":"11/11/2026"}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"x","date":"2026-11-11T00:00"}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"x","date":""}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"x","date":20261111}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"x","date":null}'), { kind: "bad" });
  // extra or unknown keys, wrong types, empty context
  assert.deepEqual(p('{"context":"x","extra":1}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"x","error":"e"}'), { kind: "bad" });
  assert.deepEqual(p('{"date":"2026-11-11"}'), { kind: "bad" });
  assert.deepEqual(p('{"context":""}'), { kind: "bad" });
  assert.deepEqual(p('{"context":"","date":"2026-11-11"}'), { kind: "bad" });
  assert.deepEqual(p('{"context":5,"date":"2026-11-11"}'), { kind: "bad" });
  assert.deepEqual(p('{"note":"n"}'), { kind: "bad" });
  // every other mode keeps its one-key strictness: a date never rides along
  assert.deepEqual(parseBridgeOutput('{"context":"x","date":"2026-11-11"}', "prompt"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"x","date":"2026-11-11"}', "capture"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"x","date":"2026-11-11"}', "pre"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"x","date":"2026-11-11"}', "post"), { kind: "bad" });
});

test("parseBridgeOutput: session accepts an optional IANA timezone, as strictly as the date", () => {
  const p = (s: string) => parseBridgeOutput(s, "session");
  // with and without a timezone, any key order
  assert.deepEqual(p('{"context":"x","date":"2026-11-11","timezone":"Europe/London"}'), {
    kind: "result",
    result: { context: "x", date: "2026-11-11", timezone: "Europe/London" },
  });
  assert.deepEqual(p('{"timezone":"America/New_York","context":"x","date":"2026-11-11"}'), {
    kind: "result",
    result: { context: "x", date: "2026-11-11", timezone: "America/New_York" },
  });
  assert.deepEqual(p('{"context":"x","timezone":"UTC"}'), { kind: "result", result: { context: "x", timezone: "UTC" } });
  for (const zone of ["America/Argentina/Buenos_Aires", "Etc/GMT+5", "America/Port-au-Prince", "Asia/Kolkata"]) {
    assert.deepEqual(p(JSON.stringify({ context: "x", timezone: zone })), { kind: "result", result: { context: "x", timezone: zone } }, zone);
  }
  assert.deepEqual(p('{"context":"x","date":"2026-11-11"}'), { kind: "result", result: { context: "x", date: "2026-11-11" } });
  // bad timezone values: not a string, empty, an offset, a path, spaces, too long
  for (const bad of [
    '""',
    "null",
    "5",
    '["Europe/London"]',
    '"+01:00"',
    '"/Europe/London"',
    '"Europe/London/"',
    '"Europe//London"',
    '"../etc/passwd"',
    '"Europe/London "',
    '"Europe London"',
    '"Europe/Lon\\ndon"',
    JSON.stringify("A/" + "b".repeat(80)),
  ]) {
    assert.deepEqual(p(`{"context":"x","date":"2026-11-11","timezone":${bad}}`), { kind: "bad" }, bad);
  }
  // still no fourth key, and the zone rides on no other mode
  assert.deepEqual(p('{"context":"x","date":"2026-11-11","timezone":"UTC","extra":1}'), { kind: "bad" });
  assert.deepEqual(p('{"timezone":"UTC"}'), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"x","timezone":"UTC"}', "prompt"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"context":"x","timezone":"UTC"}', "capture"), { kind: "bad" });
});

test("no reason ever contains the payload marker (sweep over every failing fake)", async () => {
  for (const name of ["fake-exit1.mjs", "fake-garbage.mjs", "fake-error.mjs", "fake-two-objects.mjs"]) {
    const out = await callBridge(dir, "pre", PAYLOAD, fake(name));
    assert.equal(out.ok, false, name);
    if (!out.ok) assert.ok(!out.reason.includes(MARKER), `${name}: ${out.reason}`);
  }
  assert.ok(!logText().includes(MARKER));
});

// ---------------------------------------------------------------------------------------------
// CP4: capture mode

const CAPTURE = { session_id: "ses_t", session_dir: "/x", parent_id: "", reason: "turn" };

function fakeCapture(captureMode: string, extra: CallOptions = {}): CallOptions {
  return fake("fake-capture.mjs", {
    env: { ...process.env, FAKE_CAPTURE_LOG: join(dir, "capture.jsonl"), FAKE_CAPTURE_MODE: captureMode },
    ...extra,
  });
}

function captureLog(): Array<Record<string, any>> {
  const p = join(dir, "capture.jsonl");
  return existsSync(p) ? readFileSync(p, "utf8").split("\n").filter(Boolean).map((l) => JSON.parse(l)) : [];
}

test("capture: empty stdout is success (result null), nothing recorded; the payload and mode arrive", async () => {
  const out = await callBridge(dir, "capture", CAPTURE, fakeCapture("empty"));
  assert.deepEqual(out, { ok: true, result: null });
  assert.equal(logText(), "");
  assert.equal(takePendingNotice("ses_t"), null);
  const start = captureLog().find((r) => r.phase === "start")!;
  assert.equal(start.mode, "capture");
  assert.deepEqual(start.payload, CAPTURE);
});

test("capture: {error: label} is a visible failure (logged + notice pending)", async () => {
  const out = await callBridge(dir, "capture", CAPTURE, fakeCapture("error"));
  assert.deepEqual(out, { ok: false, reason: "bridge error: capture-export-failed" });
  assert.match(logText(), /session=ses_t bridge error: capture-export-failed/);
  // B2: a capture failure is told as a chat not saved, never as the guards failing.
  assert.equal(takePendingNotice("ses_t"), captureNoticeFor("bridge error: capture-export-failed"));
});

test("capture: exit non-zero and a hang are visible failures", async () => {
  assert.deepEqual(await callBridge(dir, "capture", CAPTURE, fakeCapture("exit1")), { ok: false, reason: "exit 1" });
  assert.deepEqual(await callBridge(dir, "capture", CAPTURE, fakeCapture("hang", { timeoutMs: 300 })), {
    ok: false,
    reason: "timeout",
  });
  assert.match(logText(), /exit 1\n.*timeout\n$/s);
});

test("parseBridgeOutput: capture accepts only empty or {error}", () => {
  assert.deepEqual(parseBridgeOutput("", "capture"), { kind: "result", result: null });
  assert.deepEqual(parseBridgeOutput("\uFEFF", "capture"), { kind: "result", result: null });
  assert.deepEqual(parseBridgeOutput('{"error":"capture-locked"}', "capture"), { kind: "error", label: "capture-locked" });
  assert.deepEqual(parseBridgeOutput('{"context":"c"}', "capture"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"note":"n"}', "capture"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"deny":{"kind":"k","member":"m","model":"x"}}', "capture"), { kind: "bad" });
  assert.deepEqual(parseBridgeOutput('{"error":""}', "capture"), { kind: "bad" });
});

test("B2: any capture-mode failure (exit, timeout) is told as a chat not saved; the same failure on pre stays a guard failure", async () => {
  await callBridge(dir, "capture", CAPTURE, fakeCapture("exit1"));
  assert.equal(takePendingNotice("ses_t"), captureNoticeFor("exit 1"));
  await callBridge(dir, "capture", CAPTURE, fakeCapture("hang", { timeoutMs: 300 }));
  assert.equal(takePendingNotice("ses_t"), captureNoticeFor("timeout"));
  await callBridge(dir, "pre", PAYLOAD, fake("fake-exit1.mjs"));
  assert.equal(takePendingNotice("ses_t"), noticeFor("exit 1"));
});
