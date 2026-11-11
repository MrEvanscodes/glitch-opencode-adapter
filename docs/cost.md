# What the Glitch plugin costs in tokens (measured)

Measured 2026-11-11 with the author's private test harness (not included) on OpenCode v2.2.7, model `openai/gpt-5.6-luna`.
Every token number below is the host's own usage report for one model call; nothing is estimated.
Model runs made: 14 (budget 14), so 28 model calls; 14 runs counted (7 ON, 7 OFF).

## The answer

With the plugin on, every model call carries about **5,121 more prompt tokens** than OpenCode in the same folder without it (median, call 1 of a run, cold cache).
Call 1's whole prompt: 8,024 tokens ON, 2,903 OFF.
Within a turn the extra is served from OpenAI's prompt cache: on call 2 it arrives as cached input, and the uncached part barely moves.

The plugin's extra tokens per model call, ON minus OFF, medians of all 14 counted runs:

| call | whole prompt (input + cache read) | cached input (cache read) | uncached input | output |
|---|---|---|---|---|
| 1 | +5,121 | +0 | +5,121 | +0 |
| 2 | +5,121 | +5,120 | -18 | +0 |

The same differences over only the first 3 runs of each condition (the planned N): call 1: whole prompt +5,121, cached +0, uncached +5,121, output +0; call 2: whole prompt +5,121, cached +5,120, uncached +1, output +0.

For scale: the real bridge built a 21,057-character session text for this Glitch folder (sizes below): about 4.1 characters per extra prompt token (a ratio of the two measured numbers).

## Read this first: the one tool call was denied in every run

The plan was one COMPLETED `read`. In every run the model made exactly one `read` call on the named file and the host ended it `error` (`Permission denied: read`), in both conditions, then the model replied `DONE`.
The deny is OpenCode's own permission rule, not Glitch: the OFF runs, with no Glitch plugin loaded, were denied the same way.
The permissions config allowed `read` with the file's ABSOLUTE path as the resource after a deny-everything rule; `opencode debug agents` shows both rules in that order, yet the read was denied.
So in v2.2.7 a `read` resource is evidently not matched against the absolute path (a project-relative path or a glob is the likely form: an earlier run with `resource: "*"` completed its read, api-findings item 21).
That is inferred, not measured; the budget was spent before a corrected config could be tried.

Why the numbers still answer the question: every counted run has the same shape in both conditions (two model calls around one `read` call), so ON minus OFF is the plugin alone.
What it changes: call 2 carries a short error as the tool result instead of the file's 440 bytes, so call 2's absolute size is about a hundred tokens smaller than a completed read would give, identically in ON and OFF.
Why 14 runs and not 6: the first version of the job retried a run that was not the plan, and the deny repeated every time until the budget ran out.
The job now stops at the first run that is not the plan, with no retry.

## What was compared

- **ON**: the finished plugin, installed into the run's own throwaway OpenCode config by `scripts/install.py install` (the author's private test harness, install route), with the probe recorder after it.
- **OFF**: the probe recorder alone (it observes and pushes nothing).
- Both conditions run with the Glitch folder as OpenCode's work folder, so OpenCode loads the Glitch folder's `AGENTS.md` in BOTH.
  The OFF baseline is therefore **OpenCode in the Glitch folder without the plugin**, not OpenCode in an empty folder; the difference is the plugin's own cost on top of what OpenCode already loads (`AGENTS.md` is 7,931 characters today).
- Same throwaway sign-in, same model, same permissions config (deny everything, then allow `read` of the one file), same prompt, interleaved ON, OFF, ON, OFF so both see the same cache weather.
- Every run went through the author's private test harness: a fresh capture-state, `GLITCH_OPENCODE_NO_KEEPER=1`, a throwaway config, and the Brain snapshot, guarded-path and stat-watch checks; the job aborts on the first run that is not clean.

The prompt (one run = one turn = two model calls: the call that asks for the read, then the call that answers):

```
Use the read tool exactly once, on the file <your Glitch folder>/.gitattributes . Use no other tool. Then reply with exactly the word DONE.
```

The file named: `<your Glitch folder>/.gitattributes` (440 bytes of git line-ending rules; nothing personal). The permissions config used:

```json
{"permissions": [{"action": "*", "resource": "*", "effect": "deny"}, {"action": "read", "resource": "<your Glitch folder>/.gitattributes", "effect": "allow"}]}
```

## Where the numbers come from

Each `step_finish` part of `opencode run --format json` carries `tokens {input, output, reasoning, cache {read, write}}` for one model call.
Every run was cross-checked against `opencode session export --standalone <id>` of the same chat (the assistant messages' `tokens`, compared number for number; no text kept): all runs matched.
The host's `input` is the uncached part of the prompt: in a warm call `input` + `cache.read` equals the cold call's prompt give or take the new turn (inferred from the numbers below).
The whole prompt of a call is therefore `input + cache.read`, a sum of two host numbers.
Cache fields on this model: `cache.read` is reported, and non-zero on warm calls (in steps of 128 tokens); `cache.write` is reported as 0 on every call: OpenAI does not report a cache write, so 0 here means not reported, not nothing written.
A call is **cold** when cache read is under 10% of its whole prompt, **warm** at 50% or more, **partial** between.

## Summary per condition and call (medians of all 14 counted runs)

| condition | call | n | input (uncached) | cache read | cache write | whole prompt | output | reasoning | cache state per run |
|---|---|---|---|---|---|---|---|---|---|
| ON | 1 | 7 | 8,024 | 0 | 0 | 8,024 | 32 | 0 | cold, cold, cold, cold, cold, warm, partial |
| ON | 2 | 7 | 150 | 7,936 | 0 | 8,086 | 5 | 0 | warm, warm, warm, warm, warm, warm, warm |
| OFF | 1 | 7 | 2,903 | 0 | 0 | 2,903 | 32 | 0 | cold, cold, cold, cold, cold, cold, cold |
| OFF | 2 | 7 | 168 | 2,816 | 0 | 2,965 | 5 | 0 | warm, warm, warm, warm, cold, cold, cold |

## Raw numbers per run

| run | condition | started | run.txt | read | export match | context calls (heading seen) | call | input | cache read | cache write | output | reasoning | cache state |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | ON | 2026-11-11T17:26:18+00:00 | clean | error | True | 2 (2) | 1 | 8,024 | 0 | 0 | 32 | 0 | cold |
| 1 | ON | 2026-11-11T17:26:18+00:00 | clean | error | True | 2 (2) | 2 | 150 | 7,936 | 0 | 5 | 0 | warm |
| 2 | OFF | 2026-11-11T17:26:32+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 32 | 0 | cold |
| 2 | OFF | 2026-11-11T17:26:32+00:00 | clean | error | True | 2 (0) | 2 | 149 | 2,816 | 0 | 5 | 0 | warm |
| 3 | ON | 2026-11-11T17:26:43+00:00 | clean | error | True | 2 (2) | 1 | 8,024 | 0 | 0 | 32 | 0 | cold |
| 3 | ON | 2026-11-11T17:26:43+00:00 | clean | error | True | 2 (2) | 2 | 150 | 7,936 | 0 | 5 | 0 | warm |
| 4 | OFF | 2026-11-11T17:26:56+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 32 | 0 | cold |
| 4 | OFF | 2026-11-11T17:26:56+00:00 | clean | error | True | 2 (0) | 2 | 149 | 2,816 | 0 | 5 | 0 | warm |
| 5 | ON | 2026-11-11T17:27:07+00:00 | clean | error | True | 2 (2) | 1 | 8,024 | 0 | 0 | 34 | 25 | cold |
| 5 | ON | 2026-11-11T17:27:07+00:00 | clean | error | True | 2 (2) | 2 | 177 | 7,936 | 0 | 5 | 0 | warm |
| 6 | OFF | 2026-11-11T17:27:20+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 34 | 17 | cold |
| 6 | OFF | 2026-11-11T17:27:20+00:00 | clean | error | True | 2 (0) | 2 | 168 | 2,816 | 0 | 5 | 0 | warm |
| 7 | ON | 2026-11-11T17:27:32+00:00 | clean | error | True | 2 (2) | 1 | 8,024 | 0 | 0 | 32 | 0 | cold |
| 7 | ON | 2026-11-11T17:27:32+00:00 | clean | error | True | 2 (2) | 2 | 150 | 7,936 | 0 | 5 | 0 | warm |
| 8 | OFF | 2026-11-11T17:27:46+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 32 | 0 | cold |
| 8 | OFF | 2026-11-11T17:27:46+00:00 | clean | error | True | 2 (0) | 2 | 149 | 2,816 | 0 | 5 | 0 | warm |
| 9 | ON | 2026-11-11T17:27:58+00:00 | clean | error | True | 2 (2) | 1 | 8,024 | 0 | 0 | 32 | 0 | cold |
| 9 | ON | 2026-11-11T17:27:58+00:00 | clean | error | True | 2 (2) | 2 | 150 | 7,936 | 0 | 7 | 12 | warm |
| 10 | OFF | 2026-11-11T17:28:11+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 32 | 0 | cold |
| 10 | OFF | 2026-11-11T17:28:11+00:00 | clean | error | True | 2 (0) | 2 | 2,965 | 0 | 0 | 5 | 0 | cold |
| 11 | ON | 2026-11-11T17:28:22+00:00 | clean | error | True | 2 (2) | 1 | 88 | 7,936 | 0 | 32 | 0 | warm |
| 11 | ON | 2026-11-11T17:28:22+00:00 | clean | error | True | 2 (2) | 2 | 150 | 7,936 | 0 | 5 | 0 | warm |
| 12 | OFF | 2026-11-11T17:28:34+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 34 | 13 | cold |
| 12 | OFF | 2026-11-11T17:28:34+00:00 | clean | error | True | 2 (0) | 2 | 2,980 | 0 | 0 | 5 | 0 | cold |
| 13 | ON | 2026-11-11T17:28:47+00:00 | clean | error | True | 2 (2) | 1 | 5,336 | 2,688 | 0 | 34 | 26 | partial |
| 13 | ON | 2026-11-11T17:28:47+00:00 | clean | error | True | 2 (2) | 2 | 178 | 7,936 | 0 | 5 | 0 | warm |
| 14 | OFF | 2026-11-11T17:28:59+00:00 | clean | error | True | 2 (0) | 1 | 2,903 | 0 | 0 | 34 | 18 | cold |
| 14 | OFF | 2026-11-11T17:28:59+00:00 | clean | error | True | 2 (0) | 2 | 2,985 | 0 | 0 | 5 | 0 | cold |

`context calls (heading seen)`: the model calls the probe recorder saw, and how many of them carried the plugin's session-text heading (ON 2 (2), OFF 2 (0) is the expected shape).
Every run.txt: clean (BRAIN-SNAPSHOT clean, every GUARDED path unchanged, every STAT-WATCH 0 changed, exit 0).
Per-run rows (numbers only) stayed in the author's private test records (not included); the table above carries every run.

What the cache did (from the cache-read fields above, not assumed):

- ON: call 1 was 5 cold, 1 partial, 1 warm (in run order: cold, cold, cold, cold, cold, warm, partial); call 2 was 0 cold, 0 partial, 7 warm (in run order: warm, warm, warm, warm, warm, warm, warm).
- OFF: call 1 was 7 cold, 0 partial, 0 warm (in run order: cold, cold, cold, cold, cold, cold, cold); call 2 was 3 cold, 0 partial, 4 warm (in run order: warm, warm, warm, warm, cold, cold, cold).
- Call 2 resends call 1's prefix seconds later, and call 1 resends the previous same-condition run's prefix about half a minute later.
  A cold call 2 or a warm call 1 is OpenAI's cache being best effort, not a change in what was sent.

## Sizes for context (characters, no model call)

What Claude Code loads at session start in this Glitch folder (file sizes today; Claude Code's own system prompt, the tool definitions and the SessionStart hook's dynamic context are not included):

| file | why it loads | bytes | characters |
|---|---|---|---|
| `CLAUDE.md` | project instructions | 27,230 | 26,893 |
| `glitch-mem/Memory/USER.md` | @-import in CLAUDE.md | 4,778 | 4,727 |
| `glitch-mem/Memory/MEMORY.md` | @-import in CLAUDE.md | 1,647 | 1,625 |
| `CLAUDE.local.md` | loaded by Claude Code when present | 78 | 78 |
| `.claude/output-styles/glitch.md` | the persona (output style, synced from SOUL.md) | 4,359 | 4,253 |
| `glitch-mem/Memory/SOUL.md` | source of the output style (not counted twice) | 4,593 | 4,491 |
| **total loaded** | | **38,092** | **37,576** |

What the plugin injects, built by the real bridge (`opencode_bridge.py session` and `prompt`, the way `tests/ts/e2e-bridge.test.ts` runs it, isolated by environment: temp capture-state, hook log, watch and read-grant state, `GLITCH_OPENCODE_NO_KEEPER=1`, no real OpenCode):

| bridge mode | when the plugin pushes it | characters | bytes |
|---|---|---|---|
| `session` (startup) | on every model call of the chat | 21,057 | 21,325 |
| `prompt` (this prompt) | on the model calls of a turn that has per-message text (watched mail and the like) | 0 (silent: nothing to add) | 0 |

Real capture paths unchanged by the bridge runs: True; real state and memory files changed (stat): 0.
What OpenCode itself loads in both conditions: `AGENTS.md`, 8,001 bytes, 7,931 characters today.
api-findings item 7 recorded a 54,711-character instructions block earlier the same day; AGENTS.md measured here is 8,001 bytes and the engine holds it under an 8 KiB cap, so that figure was most likely the whole system part (AGENTS.md plus what else OpenCode put in it), not AGENTS.md alone (inferred, not re-measured), and should not be quoted as AGENTS.md's size.
The OFF condition's whole first-call prompt, 2,903 tokens, is everything OpenCode sends here without the plugin under this deny-everything config; a denied tool is not offered to the model (api-findings item 16), so with tools allowed both conditions would be larger by the same tool list.

## Limits (read before quoting)

- One model (`openai/gpt-5.6-luna`), one tool-call shape (one `read`, two model calls, the read denied by the host), 14 runs, medians.
- The host reported a cost of 0 for every call on this sign-in, so this page gives tokens, not money.
- Cache behaviour is OpenAI's: whether a call is warm depends on OpenAI's cache window and routing, which is not under our control; each call is reported with its measured cache state, never assumed.
- ON vs OFF both load `AGENTS.md`: the difference is the plugin alone, not Glitch's whole footprint in OpenCode.
- The plugin's session text is built from this folder's memory, which is the starter's (a nearly empty profile and memory); a member with a full USER.md and MEMORY.md pays more, in proportion to the characters above.
- Claude Code numbers are file sizes only; no Claude model was called and Claude Code's own token use was not measured.
- Not measured: other providers, a longer chat, a compaction, per-message text (no watched mail was seeded), a completed read, Windows.

## Reproduce

The measuring script was the author's private test harness and is not included in this folder, so this page cannot be re-run from here.
The method is stated above (same prompt, same model, interleaved ON and OFF, the host's own usage report); anyone can repeat it by hand with their own harness.
