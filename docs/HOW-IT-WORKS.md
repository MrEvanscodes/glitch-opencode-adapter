# How the Glitch OpenCode adapter works

Glitch's safety guards and memory, inside OpenCode.

## What it is

This is an OpenCode plugin plus a small Python bridge that give an OpenCode session started in a Glitch folder the same guards and the same memory a Claude Code or Codex session gets there.
It lets a member use any model OpenCode offers them (a ChatGPT login, a Claude login, OpenRouter) without losing Glitch's protections or their second brain.
The plugin (`src/plugin/`, TypeScript, loaded by OpenCode's own Bun runtime with no build step) catches OpenCode's tool, session and event-stream signals and hands each one to the bridge.
The bridge (`src/bridge/`, Python, run on the Glitch scripts environment) imports Glitch's own guards and context builders from the Glitch folder and delegates to them.
No guard rule is re-implemented here: every verdict and every word of every deny is the engine's.
It was built and measured against OpenCode v2.2.7, and `docs/api-findings.md` is the measured authority for every OpenCode name it uses.

## How it activates

At start, the plugin walks up from the session's start folder (OpenCode's `ctx.location.directory`) to the first folder that holds both `CLAUDE.md` and `.claude/hooks/write-guard.py`.
That folder is the Glitch root, and the plugin is bound to it for the whole session.
A project folder under `workspaces/` has its own `CLAUDE.md` but no engine hooks, so the walk passes it and finds the real Glitch folder above.
If no Glitch folder is found, the plugin registers no hook at all, so any other OpenCode project pays nothing and sees nothing.
A session that starts outside Glitch stays inert for its whole life, and one that starts inside stays guarded for its whole life.
A tool's own working folder or path arguments never decide whether the guards run: they are passed to the guards as data, so a shell call with `workdir: "/tmp"` aimed at the Glitch folder is still judged.

## The guards

Every tool call goes through the bridge before it runs, and every tool result is scanned after it.

- **Before a tool runs** (`execute.before`, bridge mode `pre`): shell calls, Code Mode `execute` code and write-class tools (`write`, `edit`, `patch`, `experimental_image_generate`, the Code Mode `opencode_*` worktree and session tools) are judged by the engine's `write-guard-codex.decide`.
- `read`, `grep` and `glob` are judged by the engine's `read-guard.decide`, with relative paths made absolute against the session's start folder first.
- A deny blocks the call by throwing the engine's own model words from the hook; OpenCode turns that into the tool's error, the model sees the words verbatim, and the turn continues (`docs/api-findings.md` item 3, and item 31 through the installed plugin).
- The read pause, the login-file read deny, the engine-file write deny, the forced `git clean` deny and every other engine rule arrive this way, unchanged.
- A sub-agent's calls are guarded too, and are marked as a sub-agent's (from the session's `parentID`) so the engine never teaches a sub-agent "always allow" or asks it a question meant for the member.
- **After a tool runs** (`execute.after`, bridge mode `post`): the printed output is scanned by the engine's `post-tool-secret-guard`, and a possible secret appends the engine's note to the tool result the model reads.
- The post scan never blocks and never redacts: the model sees the output and the note, which is what Claude Code does too (item 32).

## Memory in

The bridge's `session` mode builds the same session-start context Codex gets, through the engine's `session-start-context-codex.build_context`.
That identity block is USER.md, MEMORY.md, the PERSONA block of SOUL.md (via the engine's `persona.extract_persona()`), the reflexes and the trigger map, plus the day's context (health notice, keeper directive, the day's dynamic lines) and the capture line ("N OpenCode chats queued for your memory since last time", `src/bridge/capture.py:903`).
It is the SOUL.md persona block that is injected, NOT the whole SOUL.md file.
The bridge's `prompt` mode runs the engine's `user-prompt-context-codex.py` on every member message, which stamps the member's turn, redeems a yes or no to a paused read, handles the "always allow commands outside Glitch" card and adds watched-mail lines.
OpenCode does not keep injected text from one model call to the next (items 6d and 13), so the plugin re-injects on every model call (`session.context`).
It uses two channels on each call: `system.push` for ordinary providers, and appending to `options.claudeCodeInstructions`, which is the only channel `claude-agent/*` models read.
OpenCode already loads the root `AGENTS.md` itself, so the plugin never pastes `AGENTS.md` or `CLAUDE.md` again.
Measured size: 21,057 characters for a starter profile (item 28, `docs/cost.md`), which costs about 5,121 extra prompt tokens on every model call on `openai/gpt-5.6-luna` (`docs/cost.md`).
The per-message text rides every model call of its turn, with a "You were already told this earlier in this turn" sentence before it from the second call on, and is dropped at the next member message (item 21).

### When the session text is built and rebuilt

- **First build.** The session text is built once per chat in the plugin process, at the chat's first `session.context` call.
  Its source is `resume` when the call already holds more than one message, or when `session.get` carries the `outcome` key that a chat with history has and a brand-new one lacks (`src/plugin/translate.ts:207`, item 12, item 28); otherwise it is `startup`.
- **Start-up sweep.** Only a `startup` build of a member's own chat runs the capture sweep (below); a `resume`, a `compact` and a day refresh never sweep (`src/bridge/context.py:83`).
- **Compaction.** OpenCode's only compaction signal is `session.compaction.ended` (item 14).
  On it the plugin captures the chat (reason `compact`), then rebuilds the text with source `compact`.
  The host goes straight on to the next model call without waiting for plugins, so in a real run the first call after a compaction carried the `resume` build and the `compact` text reached later calls of the same process (item 28).
  A compaction that reaches the plugin before the chat's first `context` call makes that first build `compact` (`tests/ts/setup.test.ts` "memory-in: a compaction before the session's first context call here makes the first build source compact").
- **Day change.** The plugin asks the bridge for a new build only when the local day in the engine's own time zone differs from the bridge's last build date (`src/plugin/setup.ts:441`); the bridge names that zone in its `session` answer (`timezone`, `src/bridge/context.py:255`), and Europe/London is used only when it names none.
  The call that notices the new day still carries the old text; the next call carries the new one.
- **Keeper count.** A compaction rebuild and a day refresh issue the keeper directive without counting a missed keeper run (`src/bridge/context.py:233`).
- A failed or empty rebuild keeps the last good text, and the failure is recorded visibly.

### The Claude Code hand-off line

A `startup` or `resume` build (never a compaction or day rebuild, never a sub-agent or Glitch-driven session) ends with a `## Claude Code hand-off` block (`src/bridge/context.py:150`).
It tells the model to say once, in one sentence, which extras only run in Claude Code (the task-to-window link, the status-line mail count, picking an open task back up after a clear, the pushed daily brief, your profile being refreshed at the end of a session), that `claude` in the same Glitch folder is the way to them, and that chats of five or more messages are saved to memory as they go.
OpenCode gives a plugin no visible notice of its own (item 15), so the model is the only way to say it.
In headless `opencode run` every turn is a new process whose first build is `resume`, so the instruction arrives fresh each turn (item 21); whether a model then repeats the sentence was not measured.
The measured report on what OpenCode and Claude Code share is `docs/claude-code-handoff.md`.

## Memory out (capture)

A member's OpenCode chat in a Glitch folder is saved into the memory keeper's queue as it goes, the OpenCode twin of the engine's Cursor capture (`src/bridge/capture.py`, module docstring).

### The data flow at the end of a turn

1. OpenCode ends a turn with `session.execution.succeeded` (`.failed` and `.interrupted` are treated the same; item 10, `src/plugin/setup.ts:715`).
   The plugin's one wildcard event subscription catches it and spawns bridge mode `capture` with reason `turn`, in the background (at most one run in flight and one queued per chat).
   The bridge child is detached, so it finishes after a headless `opencode run` has exited (item 20).
2. The bridge skips a sub-agent chat (a `parentID`), a chat Glitch drives itself (the engine's recursion guard), and a chat whose start folder does not resolve to this Glitch root.
   A start folder anywhere under the Glitch folder resolves to it, `workspaces/<project>` included (`tests/py/test_capture.py::test_workspace_subfolder_counts_as_this_root`).
3. It reads the whole chat through `opencode session export --standalone <id>` (`src/bridge/chatsource.py`, item 11), never through OpenCode's database, which also holds credential tables.
   It keeps the member's text and the assistant's visible text only; tool calls, tool output, reasoning, files, compaction summaries and unknown types are dropped and counted.
4. Under one file lock on the ledger it decides the chat: older than the first capture run (never captured), fewer than five records (too short), unchanged, or to stage.
5. To stage, it writes in a fixed order, so a stub never points at a missing file:
   1. **The copy:** the whole chat as a Claude-shaped JSONL file at `.claude/data/opencode-chats/<id>.jsonl`, every text through Glitch's secret redactor, mode 0600 in a 0700 folder; if the redactor cannot load or fails, nothing is written (`src/bridge/chatcache.py`).
   2. **The stub:** `.claude/data/state/pending/opencode-<id>.md`, the queue's excerpt shape (the last 30 records, at most 15,000 characters, redacted), with the copy's path as the header's last field (`src/bridge/capture.py:311`).
   3. **The ledger:** `.claude/data/state/opencode-staged.json` records the chat as staged, with a marker of what was read.
6. A chat that grows is staged again: its one stub and its one copy are rewritten.
7. The memory keeper digests the stub like a Cursor or Codex stub; the digest reads the chat from the copy through the engine's own Claude reader (item 36, `tests/py/test_e2e_digest.py::test_read_session_reads_the_staged_chat_from_its_transcript`).

A compaction drives the same capture with reason `compact`.
Nothing is lost to a compaction, because the export keeps every original turn (item 14).

### The copy's escapes

The engine's Claude reader drops or rewrites some text it takes to be harness-injected: a complete `<system-reminder>...</system-reminder>` span, anything holding `<command-name>`, and text that starts with an injected prefix such as `<command-message>` or `[Request interrupted`.
In an OpenCode chat that text is the member's own typing, so in member text one backslash is inserted after the first character of each such trigger, and the offsets are stored on the line under `glitchEscapes`, a key the reader ignores, so `restore_text` gives back the exact original (`src/bridge/chatcache.py`, module docstring; `tests/py/test_chatcache.py::test_reader_triggers_neutralised_counted_and_reversible`).
Ordinary text, code fences and a lone `<system-reminder>` mention are written unchanged.

### The start-up sweep, and the first chat after install

A `startup` build first runs a bounded sweep (`src/bridge/context.py:272`): it lists this project's top-level chats through `opencode session list --standalone --format json` and stages any that grew without being staged (at most 20 reads and a few seconds; the rest wait for the next start).
OpenCode lists only the chats of the project it takes from `$PWD`, here the Glitch folder (item 11), so a chat OpenCode files under another project is not seen by the sweep, although its own turn ends still capture it.
The first capture run on a machine writes a first-run stamp; chats that started before it are never captured, however much they grow.
The sweep is handed the starting chat's own id, so on that first run it records the live chat as `first chat` and every other listed chat as from before the install (item 24).
Measured: the member's first chat after install was staged from its third exchange on, when it held 6 records (item 24, item 33).
The sweep also removes the ledger record and the copy of a chat OpenCode itself answers "not found" for; an unreachable OpenCode never prunes anything (`tests/py/test_capture.py::test_gone_chat_pruned_with_its_copy_only_on_not_found`).

## Fail-open, and how a failure becomes visible

The adapter fails open on its own errors: if the bridge cannot judge a call, the call runs.
A failure is never silent, with one deliberate exception for chat capture.
A capture that cannot reach OpenCode at all (OpenCode closing, a timeout, the program missing) stays quiet, because one such miss is common; the capture ledger counts misses in a row and any answer from OpenCode resets the count (`src/bridge/capture.py` `_note_reach`).
At 3 in a row, the next chat start tells the member once, in plain words, that their OpenCode chats are not being saved to memory, and the next capture that also cannot reach OpenCode answers `capture-unreachable`, which the plugin records as a visible failure; each is said once per streak (`tests/py/test_capture.py::test_notice_says_the_unreachable_line_once_per_streak_at_three`, `tests/py/test_entrypoint.py::test_capture_unreachable_three_in_a_row_is_an_error_label_once`).
A list of chats OpenCode returns in a shape the reader does not know is reported at each chat start until a readable list clears it (`tests/py/test_capture.py::test_unreadable_list_is_recorded_reported_and_cleared`).
Each failure (`uv not found`, a timeout, a crash, garbage output, a delegate that will not load) appends one line with a short reason, never the payload, to `<tmpdir>/opencode-adapter-errors.log`.
The next model call in that session then carries the line "Glitch guards in OpenCode failed to run (<reason>)." with an instruction to tell the member plainly.
A failure to save a chat to memory (the `capture` calls) is told differently, because the guards are still working: "Glitch could not save your OpenCode chat to memory just now (<reason in plain words>). Your safety guards are still working.", said once, in one sentence (`src/plugin/failure.ts` `captureNoticeFor`).
The bridge itself treats silence as "judged and allowed" only, so any internal problem prints an `error` object instead of nothing.
Timeouts are 10 seconds for `pre`, `post`, `prompt` and `capture`, and 30 seconds for `session` (`src/plugin/bridge-client.ts:68`).
A plugin start on an OpenCode version other than 2.2.7 prints a warning and keeps the guards running (`src/plugin/setup.ts:148`).

## What it does NOT do

- It does not edit the Glitch engine: everything lives in this workspace, and the engine's files are only read and imported.
  What it writes in the Glitch folder is runtime data under the gitignored `.claude/data/`: the chat copies, the stubs in the keeper's queue and the capture ledger.
- It does not re-decide any guard rule, and it does not explicitly allow anything OpenCode would otherwise ask about (an allow emits nothing).
- It does not redact secrets in tool output (it notes them, as Claude Code does).
- It does not know which project or task "this window" is on: the engine cannot identify an OpenCode window (`docs/claude-code-handoff.md` (b)).

## The bridge contract

The plugin spawns one fresh process per call: `uv run --no-project --directory <root>/.claude/scripts python src/bridge/opencode_bridge.py <pre|post|session|prompt|capture>`.
A fresh process each time is deliberate, because the write guard snapshots its opt-in flags from its own launch environment at import.
Stdin carries one JSON event (a UTF-8 byte-order mark is tolerated):

- `pre`: `{tool_name, tool_input, session_dir, session_id, parent_id?, cwd?}`
- `post`: `{tool_name, session_id, result, error, status, session_dir}`
- `session`: `{session_id, session_dir, parent_id?, source?, refresh?}` (source is `startup`, `resume` or `compact`)
- `prompt`: `{session_id, prompt, session_dir, parent_id?}`
- `capture`: `{session_id, session_dir, parent_id?, reason}` (reason is `turn`, `compact`, `start` or `sweep`; the plugin sends `turn` and `compact`)

Stdout carries nothing at all (judged and allowed, clean, no context, a chat staged or rightly left alone, or not a Glitch session) or exactly one JSON object with no trailing newline:

- `{"deny": {"kind": "...", "member": "...", "model": "..."}}` from `pre` only.
- `{"note": "..."}` from `post` only.
- `{"context": "..."}` from `prompt`, and `{"context": "...", "date": "YYYY-MM-DD"}` from `session`.
- `{"error": "<short label>"}`, which the plugin treats as an allow plus a visible failure.

The exit code is always 0, and the root is resolved from `session_dir`, never from a tool's cwd or the process's cwd.
One `pre` allow through real `uv` measured about 65 ms.

## The keeper note

When memory work is pending, the session-start context tells the OpenCode model to launch the memory keeper as a background sub-agent with OpenCode's `subagent` tool.
The keeper then reads and writes the member's memory through whichever model vendor the member picked in OpenCode.
That is the member's own choice of model, but it means their memory goes to that vendor.
If the `subagent` tool is unavailable, the model is told to say that memory work is waiting and that `/keeper` will catch it up, never to claim it ran.

## Honest bounds

- This is a guardrail, not an enforcement boundary: like the Codex adapter, it depends on the host's own hooks, and anything OpenCode does that never fires a plugin hook is not guarded.
- Only OpenCode v2.2.7 was tested (the newest 2.x release when checked, item 34); other versions run with a warning and may have a changed hook API.
- Sub-agent coverage was measured and holds: a denied shell call inside a `subagent` call was blocked and logged in the end-to-end run.
- A sub-agent is recognised by a session lookup with a 2 second limit; if that lookup fails, the guards treat the session as the member's own, while capture skips the chat and records the failure.
- MCP tools are not tested, because no MCP server was configured; the findings suggest they surface through Code Mode, which is hooked.
- `claude-agent/*` models are not covered by the end-to-end run (the member's decision); their memory channel was measured only in the author's private test runs.
- Windows and Linux are not tested: nothing ran on either (item 9, item 34).
- The Windows risks named in the plan and the findings: path shapes in tool inputs (backslashes, drive letters) and the case and realpath behaviour of the start folder, resolving and spawning `uv` from Bun (`uv.exe`, argv quoting), which shell OpenCode runs and so what a shell command looks like, plugin loading and the `file:///C:/...` entry form, the `%APPDATA%` config layout, and killing `uv`'s child Python on a timeout (Mac and Linux kill the whole process group; Windows has no such group here).
- The engine's write guard allows `opencode_worktree_remove` on `.claude/hooks`, the same as under Codex; the OS read-only lock on engine files is the floor there.
- Code Mode `execute {code}` is judged as a shell command, so its JavaScript is read by the shell-command rules; its inner tool calls are also judged on their own.
- A post-scan note is dropped when the errored call has no result to attach it to.
- The post-tool scan was not re-run with a model, because the engine's catch diary has no test door and a run would write to the member's real state folder (item 32).
- Not every spelling of a login file is denied by name: `.claude/data/google/token.json` was allowed by the engine's read guard while `.claude/data/google-accounts/<id>/token.json` is denied (item 31); the list is the engine's (proposal 10 in `docs/engine-handoff.md`).
- A real install into `~/.config/opencode` is not tested yet: `scripts/install.py` was run only against throwaway config folders (item 30), and the real folder was only read.
- Everything was measured headless (`opencode run`); an interactive OpenCode window was not driven.

## Measured results

The first end-to-end run was made on 2026-11-11 on a Mac with real OpenCode v2.2.7, using a throwaway OpenCode config and an OpenAI model on a ChatGPT login (about 11 one-line model runs).
Its script is not in this workspace because it was tied to the author's machine; the table is its recorded outcome.

| Case | What it proves | Result |
|---|---|---|
| 1 | Shell deny reaches the model; `/etc/hosts` content absent | PASS |
| 2 | Login-file read denied with the secret-read words, decoy never created | PASS |
| 3 | Allowed call is silent (`echo ok`) | PASS |
| 4 | Memory-in: objective answered with plugin, not without (control) | PASS |
| 5 | Two turns: `yes` redeems the one-time pass (prompt path ran) | PASS |
| 6 | Inert outside Glitch: no deny, no memory | PASS |
| 7a | Broken bridge: call still runs, failure logged, model told | PASS |
| 7b | Delegate fails inside a working bridge: same visible result | PASS |
| 8 | Deny holds inside a subagent | PASS |
| 9 | Latency measured | PASS |

Every later real run was made with the author's private test harness, which is not included in this folder, and is recorded in `docs/api-findings.md`:

- The harness ran OpenCode headless in a throwaway setup (config, data, capture-state) and checked the Glitch folder was unchanged after each run (items 1-17, 18 and 30).
- It drove the real-run jobs on top of that: capture, per-message text and compaction (items 19-22), the first-chat and compaction fixes (items 24-28), install, deny and capture through the installed plugin (items 31-33), and a captured chat read back by the digest (item 36).
- A cost run with and without the plugin is written up in `docs/cost.md`.
- Run ledgers: items 23, 27, 29, 35 and 37.
- The evidence it recorded (key names, labels and counts only, no message bodies) stays with the author; the findings are quoted in `docs/api-findings.md`.

## Tests

- `tests/ts/` holds the plugin tests (Node 22.18 or newer): `node --test tests/ts/*.test.ts` from this folder.
- `tests/py/` holds the bridge tests. They import Glitch's own guard code, so they only run when this folder sits inside a Glitch folder (for example `<your Glitch folder>/workspaces/opencode-adapter`). Run them with `uv run --no-project --directory <your Glitch folder>/.claude/scripts --with pytest python -m pytest <adapter folder>/tests/py -q`. pytest is borrowed for the run and never installed into Glitch's environment.
