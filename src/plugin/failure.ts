/**
 * failure.ts - the visible fail-open record.
 *
 * Every time the bridge cannot judge a call (uv missing, a timeout, a crash, garbage output, a
 * bridge-reported `{error}`), the plugin allows the call AND records the failure here:
 *
 * 1. one timestamped line is appended to `<os.tmpdir()>/opencode-adapter-errors.log`
 *    (the REASON only: never a payload, never bridge stdout, newlines stripped, length capped);
 * 2. a per-session pending flag is set, so the next `context` injection for that session carries
 *    one plain line for the model (`takePendingNotice`).
 *
 * Two kinds, because the member must hear the truth: a "guards" failure (a hook could not judge a
 * call, the session text could not be built) says the guards failed to run; a "capture" failure
 * (the chat could not be saved to memory) says so, and that the guards still work. The kind is
 * passed by callBridge (mode `capture`) or derived from the plugin's own capture reasons
 * (`failureKindOf`). A session holds at most one pending reason per kind; both show, guards first.
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

export type FailureKind = "guards" | "capture";

type PendingEntry = { guards?: string; capture?: string };

const pending = new Map<string, PendingEntry>();

/** Bridge `capture` labels in plain words (opencode_bridge.py CAPTURE_ERRORS + the two extras). */
const CAPTURE_LABEL_WORDS: Readonly<Record<string, string>> = Object.freeze({
  "capture-unreachable": "OpenCode did not answer several times in a row",
  "capture-copy-failed": "the chat could not be copied",
  "capture-stub-failed": "the chat could not be queued for memory",
  "capture-refused": "OpenCode gave the chat in a shape Glitch cannot read yet",
  "capture-error": "an internal error",
  "capture-bad-payload": "the plugin sent a malformed request",
  "capture-unknown-outcome": "an outcome Glitch did not recognise",
});

/**
 * The kind of a failure from its reason alone: the plugin's own capture reasons
 * ("plugin error in capture...", "event subscription failed: capture") and a bridge `capture-*`
 * label are "capture"; everything else is "guards" (the old, unchanged wording).
 */
export function failureKindOf(reason: string): FailureKind {
  const text = cleanReason(reason);
  if (/^plugin error in capture\b/.test(text)) return "capture";
  if (text === "event subscription failed: capture") return "capture";
  if (/^bridge error: capture-/.test(text)) return "capture";
  return "guards";
}

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
  /** Which notice the member gets. Defaults to failureKindOf(reason). */
  kind?: FailureKind;
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
  const kind: FailureKind = opts.kind === "capture" || opts.kind === "guards" ? opts.kind : failureKindOf(clean);
  const key = sid ?? ANY_SESSION;
  const entry = pending.get(key) ?? {};
  entry[kind] = clean;
  pending.set(key, entry);
  return clean;
}

/** The notice line for a reason, exactly as the model sees it. */
export function noticeFor(reason: string): string {
  return `Glitch guards in OpenCode failed to run (${cleanReason(reason)}). Tell the member plainly.`;
}

/** The short reason a capture notice carries: a known bridge label in plain words, else the reason. */
function captureWords(reason: string): string {
  const clean = cleanReason(reason);
  const label = clean.replace(/^bridge error: /, "");
  return CAPTURE_LABEL_WORDS[label] ?? clean;
}

/** The notice line for a capture-kind reason, exactly as the model sees it. Never says the guards failed. */
export function captureNoticeFor(reason: string): string {
  return (
    `Glitch could not save your OpenCode chat to memory just now (${captureWords(reason)}). ` +
    "Your safety guards are still working. Tell the member once, in one sentence."
  );
}

function noticeOf(entry: PendingEntry): string {
  const lines: string[] = [];
  if (entry.guards !== undefined) lines.push(noticeFor(entry.guards));
  if (entry.capture !== undefined) lines.push(captureNoticeFor(entry.capture));
  return lines.join("\n");
}

/**
 * The pending notice for this session (or the one recorded without a session), then cleared.
 * Returns null when nothing is pending. Per kind, the latest failure's reason wins if several piled
 * up; when both kinds are pending, both lines show, the guards line first.
 */
export function takePendingNotice(sessionId: string): string | null {
  const own = pending.get(sessionId);
  if (own !== undefined) {
    pending.delete(sessionId);
    return noticeOf(own);
  }
  const any = pending.get(ANY_SESSION);
  if (any !== undefined) {
    pending.delete(ANY_SESSION);
    return noticeOf(any);
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
