import { strict as assert } from "node:assert";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, test } from "node:test";

import {
  ERRORS_LOG_ENV,
  LOG_FILE_NAME,
  MAX_REASON_CHARS,
  cleanReason,
  errorsLogPath,
  hasPendingNotice,
  noticeFor,
  record,
  resetPendingForTests,
  takePendingNotice,
} from "../../src/plugin/failure.ts";

let dir = "";
let logPath = "";

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "oc-failure-"));
  logPath = join(dir, "errors.log");
  resetPendingForTests();
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

test("record appends one timestamped line with the reason and session", () => {
  record("uv not found", "ses_a", { logPath });
  record("timeout", "ses_b", { logPath });
  const lines = readFileSync(logPath, "utf8").split("\n").filter(Boolean);
  assert.equal(lines.length, 2);
  assert.match(lines[0], /^\d{4}-\d{2}-\d{2}T\S+Z session=ses_a uv not found$/);
  assert.match(lines[1], /session=ses_b timeout$/);
});

test("newlines are stripped so one failure is always one line", () => {
  const clean = record("bad\nthing\r\nhere\u2028x", "ses_a", { logPath });
  assert.equal(clean, "bad thing here x");
  const text = readFileSync(logPath, "utf8");
  assert.equal(text.split("\n").filter(Boolean).length, 1);
});

test("reasons are capped", () => {
  const clean = record("y".repeat(10_000), "ses_a", { logPath });
  assert.equal(clean.length, MAX_REASON_CHARS);
  assert.ok(clean.endsWith("..."));
  const line = readFileSync(logPath, "utf8");
  assert.ok(line.length < MAX_REASON_CHARS + 120, `line too long: ${line.length}`);
  assert.ok((takePendingNotice("ses_a") ?? "").length < MAX_REASON_CHARS + 100);
});

test("empty reason becomes a named placeholder", () => {
  assert.equal(cleanReason(""), "unknown failure");
  assert.equal(cleanReason("\n\n"), "unknown failure");
});

test("the notice is delivered once, then cleared", () => {
  record("timeout", "ses_a", { logPath });
  assert.equal(hasPendingNotice("ses_a"), true);
  assert.equal(
    takePendingNotice("ses_a"),
    "Glitch guards in OpenCode failed to run (timeout). Tell the member plainly.",
  );
  assert.equal(takePendingNotice("ses_a"), null);
  assert.equal(hasPendingNotice("ses_a"), false);
});

test("a notice belongs to its session", () => {
  record("exit 1", "ses_a", { logPath });
  assert.equal(takePendingNotice("ses_b"), null);
  assert.equal(takePendingNotice("ses_a"), noticeFor("exit 1"));
});

test("the latest failure wins when several pile up", () => {
  record("exit 1", "ses_a", { logPath });
  record("timeout", "ses_a", { logPath });
  assert.equal(takePendingNotice("ses_a"), noticeFor("timeout"));
  assert.equal(takePendingNotice("ses_a"), null);
});

test("a failure with no session goes to the next session that asks, once", () => {
  record("bad payload", undefined, { logPath });
  assert.match(readFileSync(logPath, "utf8"), /session=- bad payload/);
  assert.equal(takePendingNotice("ses_x"), noticeFor("bad payload"));
  assert.equal(takePendingNotice("ses_y"), null);
});

test("an unwritable log path never throws and still sets the notice", () => {
  const bad = join(dir, "missing-folder", "deeper", "errors.log");
  assert.doesNotThrow(() => record("timeout", "ses_a", { logPath: bad }));
  assert.equal(existsSync(bad), false);
  assert.equal(takePendingNotice("ses_a"), noticeFor("timeout"));
});

test("the default log path is in the temp folder; the env var overrides it", () => {
  const saved = process.env[ERRORS_LOG_ENV];
  try {
    delete process.env[ERRORS_LOG_ENV];
    assert.equal(errorsLogPath(), join(tmpdir(), LOG_FILE_NAME));
    process.env[ERRORS_LOG_ENV] = logPath;
    assert.equal(errorsLogPath(), logPath);
    record("via env", "ses_a");
    assert.match(readFileSync(logPath, "utf8"), /via env/);
  } finally {
    if (saved === undefined) delete process.env[ERRORS_LOG_ENV];
    else process.env[ERRORS_LOG_ENV] = saved;
  }
});
