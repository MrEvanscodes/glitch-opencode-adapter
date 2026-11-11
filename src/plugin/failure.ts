/**
 * failure.ts - the visible fail-open record.
 *
 * Every time the bridge cannot judge a call (uv missing, a timeout, a crash, garbage output, a
 * bridge-reported `{error}`), the plugin allows the call AND records the failure here:
 *
 * 1. one timestamped line is appended to `<os.tmpdir()>/opencode-adapter-errors.log`
 *    (the REASON only: never a payload, never bridge stdout, newlines stripped, length capped);
 * 2. a per-session pending flag is set, so the next `context` injection for that session carries
 *    one plain line telling the model the guards failed (`takePendingNotice`).
 *
 * The log path can be pointed elsewhere with `opts.logPath` (tests) or the TEST-ONLY env var
 * `GLITCH_OPENCODE_ERRORS_LOG`. Recording never throws: a failure to log must not break the call.
 */

import { appendFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

export const LOG_FILE_NAME = "opencode-adapter-errors.log";
export const ERRORS_LOG_ENV = "GLITCH_OPENCODE_ERRORS_LOG";
export const MAX_REASON_CHARS = 200;

/** Key used when a failure is recorded without a session id: the next session to ask gets it. */
const ANY_SESSION = "\u0000any";

const pending = new Map<string, string>();

/** The errors log path: the env override (test-only) or `<tmpdir>/opencode-adapter-errors.log`. */
export function errorsLogPath(): string {
  const override = process.env[ERRORS_LOG_ENV];
  if (typeof override === "string" && override.trim() !== "") return override;
  return join(tmpdir(), LOG_FILE_NAME);
}

/** One line, control characters flattened to spaces, capped at MAX_REASON_CHARS. */
export function cleanReason(reason: unknown): string {
  let text = typeof reason === "string" ? reason : String(reason);
  text = text.replace(/[\u0000-\u001f\u007f\u2028\u2029]+/g, " ").replace(/ {2,}/g, " ").trim();
  if (text === "") text = "unknown failure";
  if (text.length > MAX_REASON_CHARS) text = text.slice(0, MAX_REASON_CHARS - 3) + "...";
  return text;
}

export interface RecordOptions {
  logPath?: string;
}

/**
 * Record one failure: append a line to the errors log and flag the session for one notice.
 * Returns the cleaned reason (what was written). Never throws.
 */
export function record(reason: string, sessionId?: string, opts: RecordOptions = {}): string {
  const clean = cleanReason(reason);
  const sid = typeof sessionId === "string" && sessionId !== "" ? sessionId : undefined;
  const line = `${new Date().toISOString()} session=${sid ? cleanReason(sid).slice(0, 80) : "-"} ${clean}\n`;
  try {
    appendFileSync(opts.logPath ?? errorsLogPath(), line, { encoding: "utf8" });
  } catch {
    // logging must never break the call it reports on; the in-memory notice still fires
  }
  pending.set(sid ?? ANY_SESSION, clean);
  return clean;
}

/** The notice line for a reason, exactly as the model sees it. */
export function noticeFor(reason: string): string {
  return `Glitch guards in OpenCode failed to run (${cleanReason(reason)}). Tell the member plainly.`;
}

/**
 * The one pending notice for this session (or one recorded without a session), then cleared.
 * Returns null when nothing is pending. The latest failure's reason wins if several piled up.
 */
export function takePendingNotice(sessionId: string): string | null {
  const own = pending.get(sessionId);
  if (own !== undefined) {
    pending.delete(sessionId);
    return noticeFor(own);
  }
  const any = pending.get(ANY_SESSION);
  if (any !== undefined) {
    pending.delete(ANY_SESSION);
    return noticeFor(any);
  }
  return null;
}

/** Whether a notice is pending for this session (does not consume it). */
export function hasPendingNotice(sessionId: string): boolean {
  return pending.has(sessionId) || pending.has(ANY_SESSION);
}

/** Test helper: forget every pending notice. */
export function resetPendingForTests(): void {
  pending.clear();
}
