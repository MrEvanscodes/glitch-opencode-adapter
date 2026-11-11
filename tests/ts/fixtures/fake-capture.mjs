// A fake `capture` bridge. Reads the payload on stdin and appends JSON lines to $FAKE_CAPTURE_LOG:
//   {phase:"start", mode, payload, state}   as soon as the payload is read
//   {phase:"end", mode, session}            just before it answers
// `state` is the text of $FAKE_CAPTURE_STATE at start (the test's stand-in for "the chat as it is now").
// $FAKE_CAPTURE_MODE: empty (default) | error | hang | exit1. $FAKE_CAPTURE_DELAY_MS delays the answer.
import { appendFileSync, existsSync, readFileSync } from "node:fs";

const log = process.env.FAKE_CAPTURE_LOG;
const statePath = process.env.FAKE_CAPTURE_STATE;
const mode = process.env.FAKE_CAPTURE_MODE || "empty";
const delay = Number(process.env.FAKE_CAPTURE_DELAY_MS || "0");

const write = (rec) => {
  if (log) appendFileSync(log, JSON.stringify(rec) + "\n");
};

let s = "";
process.stdin.on("data", (c) => (s += c));
process.stdin.on("end", () => {
  let payload = null;
  try {
    payload = JSON.parse(s);
  } catch {
    payload = { unparsed: true };
  }
  const state = statePath && existsSync(statePath) ? readFileSync(statePath, "utf8") : null;
  write({ phase: "start", mode: process.argv[2], payload, state });
  if (mode === "hang") {
    setTimeout(() => {}, 600000);
    return;
  }
  setTimeout(() => {
    write({ phase: "end", mode: process.argv[2], session: payload && payload.session_id });
    if (mode === "error") process.stdout.write(JSON.stringify({ error: "capture-export-failed" }));
    if (mode === "exit1") process.exit(1);
  }, delay);
});
