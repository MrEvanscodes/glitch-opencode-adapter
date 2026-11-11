# How the Glitch OpenCode adapter works

Glitch's safety guards and memory, inside OpenCode.

## What it is

This is an OpenCode plugin plus a small Python bridge that give an OpenCode session started in a Glitch folder the same guards and the same memory a Claude Code or Codex session gets there.
It lets a member use any model OpenCode offers them (a ChatGPT login, a Claude login, OpenRouter) without losing Glitch's protections or their second brain.
The plugin (`src/plugin/`, TypeScript, loaded by OpenCode's own Bun runtime with no build step) catches OpenCode's tool and session events and hands each one to the bridge.
The bridge (`src/bridge/`, Python, run on the Glitch scripts environment) imports Glitch's own guards and context builders from the Glitch folder and delegates to them.
No rule is re-implemented here: every verdict and every word of every deny is the engine's.
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
- A deny blocks the call by throwing the engine's own model words from the hook; OpenCode turns that into the tool's error, the model sees the words verbatim, and the turn continues (measured in `docs/api-findings.md`).
- The read pause, the login-file read deny, the engine-file write deny, the forced `git clean` deny and every other engine rule arrive this way, unchanged.
- A sub-agent's calls are guarded too, and are marked as a sub-agent's (from the session's `parentID`) so the engine never teaches a sub-agent "always allow" or asks it a question meant for the member.
- **After a tool runs** (`execute.after`, bridge mode `post`): the printed output is scanned by the engine's `post-tool-secret-guard`, and a possible secret appends the engine's note to the tool result the model reads.
- The post scan never blocks.

## Memory-in

The bridge's `session` mode builds the same session-start context Codex gets, through the engine's `session-start-context-codex.build_context`.
That identity block is USER.md, MEMORY.md, the PERSONA block of SOUL.md (via the engine's `persona.extract_persona()`), the reflexes and the trigger map, plus the day's context (health notice, keeper directive, the day's dynamic lines).
It is the SOUL.md persona block that is injected, NOT the whole SOUL.md file.
The bridge's `prompt` mode runs the engine's `user-prompt-context-codex.py` on every member message, which stamps the member's turn, redeems a yes or no to a paused read, handles the "always allow commands outside Glitch" card and adds watched-mail lines.
OpenCode does not keep injected text from one model call to the next, so the plugin re-injects the session text on every model call (`session.context`).
It uses two channels on each call: `system.push` for ordinary providers, and appending to `options.claudeCodeInstructions`, which is the only channel `claude-agent/*` models read.
OpenCode already loads the root `AGENTS.md` itself, so the plugin never pastes `AGENTS.md` or `CLAUDE.md` again.
Measured size: about 20,500 characters (20,572 in the end-to-end run), which cost roughly 5-7k extra input tokens on every model call.
The session text is built once per session and cached; the per-message text is consumed by the first model call after the message.

## Fail-open, and how a failure becomes visible

The adapter fails open on its own errors: if the bridge cannot judge a call, the call runs.
A failure is never silent.
Each failure (`uv not found`, a timeout, a crash, garbage output, a delegate that will not load) appends one line with a short reason, never the payload, to `<tmpdir>/opencode-adapter-errors.log`.
The next model call in that session then carries the line "Glitch guards in OpenCode failed to run (<reason>)." with an instruction to tell the member plainly.
The bridge itself treats silence as "judged and allowed" only, so any internal problem prints an `error` object instead of nothing.
Timeouts are 10 seconds for `pre`, `post` and `prompt`, and 30 seconds for `session`.
A plugin start on an OpenCode version other than 2.2.7 prints a warning and keeps the guards running.

## What it does NOT do

- It does not capture OpenCode chats into memory: saving OpenCode sessions into the daily log is phase 2, and nothing here blocks it.
- It does not edit the Glitch engine: everything lives in this workspace, and the engine's files are only read and imported.
- It does not re-decide any rule, and it does not explicitly allow anything OpenCode would otherwise ask about (an allow emits nothing).

## The bridge contract

The plugin spawns one fresh process per call: `uv run --no-project --directory <root>/.claude/scripts python src/bridge/opencode_bridge.py <pre|post|session|prompt>`.
A fresh process each time is deliberate, because the write guard snapshots its opt-in flags from its own launch environment at import.
Stdin carries one JSON event (a UTF-8 byte-order mark is tolerated):

- `pre`: `{tool_name, tool_input, session_dir, session_id, parent_id?, cwd?}`
- `post`: `{tool_name, session_id, result, error, status, session_dir}`
- `session`: `{session_id, session_dir, parent_id?}`
- `prompt`: `{session_id, prompt, session_dir, parent_id?}`

Stdout carries nothing at all (judged and allowed, clean, no context, or not a Glitch session) or exactly one JSON object with no trailing newline:

- `{"deny": {"kind": "...", "member": "...", "model": "..."}}` from `pre` only.
- `{"note": "..."}` from `post` only.
- `{"context": "..."}` from `session` and `prompt`.
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
- Only OpenCode v2.2.7 was tested; other versions run with a warning and may have a changed hook API.
- Sub-agent coverage was measured and holds: a denied shell call inside a `subagent` call was blocked and logged in the end-to-end run.
- A sub-agent is recognised by a session lookup with a 2 second limit; if that lookup fails, the session is treated as the member's own.
- MCP tools are not tested, because no MCP server was configured; the findings suggest they surface through Code Mode, which is hooked.
- `claude-agent/*` models are not covered by the end-to-end run (the member's decision); their memory channel was measured only in the spike.
- Windows is not tested: nothing ran on Windows.
- The Windows risks named in the plan and the findings: path shapes in tool inputs (backslashes, drive letters) and the case and realpath behaviour of the start folder, resolving and spawning `uv` from Bun (`uv.exe`, argv quoting), which shell OpenCode runs and so what a shell command looks like, plugin loading and the `file:///C:/...` entry form, the `%APPDATA%` config layout, and killing `uv`'s child Python on a timeout (Mac and Linux kill the whole process group; Windows has no such group here).
- The engine's write guard allows `opencode_worktree_remove` on `.claude/hooks`, the same as under Codex; the OS read-only lock on engine files is the floor there.
- Code Mode `execute {code}` is judged as a shell command, so its JavaScript is read by the shell-command rules; its inner tool calls are also judged on their own.
- The per-message card reaches only the first model call of a turn, not the later calls of a tool loop; that was enough for the yes or no path in the end-to-end run.
- A post-scan note is dropped when the errored call has no result to attach it to.
- A real install into `~/.config/opencode` is not tested yet.

## Measured results

These were measured on 2026-11-11 on a Mac with real OpenCode v2.2.7, using a throwaway OpenCode config and an OpenAI model on a ChatGPT login (about 11 one-line model runs). The scripted end-to-end harness is not included in this package because it is tied to the author's machine; the table is its recorded outcome.

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

## Tests

- `tests/ts/` holds the plugin tests (Node 22.18 or newer): `node --test tests/ts/*.test.ts` from this folder.
- `tests/py/` holds the bridge tests. They import Glitch's own guard code, so they only run when this folder sits inside a Glitch folder (for example `<your Glitch folder>/workspaces/glitch-opencode-adapter`). Run them with `uv run --no-project --directory <your Glitch folder>/.claude/scripts --with pytest python -m pytest <adapter folder>/tests/py -q`. pytest is borrowed for the run and never installed into Glitch's environment.
