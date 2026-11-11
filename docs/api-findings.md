GO
BLOCK_ROUTE=throw
FALLBACK_ORDER=throw > permission > rewrite (rewrite is shell-only and reports the call as "completed"; never use it as a primary route)

# OpenCode v2.2.7 plugin API: measured findings (CP0)

This file is the authority for every OpenCode name used by the adapter.
Every row says how it is known: **measured** (a real headless run, log line quoted), **docs-only** (official docs or the binary's own strings, not exercised), or **unresolved**.

Host: `~/.opencode/bin/opencode` `opencode v2.2.7`, macOS (Darwin), Bun runtime embedded in the binary.
Harness: `spike/run-probe.sh` with the probe plugin `spike/probe-plugin/index.ts`, throwaway config dir, `--standalone`, `OPENCODE_DISABLE_AUTOUPDATE=1`, data dir untouched, working folder a fresh temp dir (except item 7).
Raw logs live in `spike/results/<stamp>-<tag>/` (gitignored); the payload lines this file relies on are copied verbatim into `spike/evidence/*.jsonl` (committed).
Brain-root snapshot (`git status --porcelain --ignored`) was taken before and after every run: clean on all of them (`BRAIN-SNAPSHOT: clean` in every `run.txt`).

## Gate

| Gate condition | Result | Confidence |
|---|---|---|
| (i) the route stops the tool | `throw` in `execute.before`: `ran.txt` / `ran-w.txt` / `ran-p.txt` ABSENT in every block run | measured |
| (ii) the deny text reaches the model or member | the thrown `Error.message` arrives verbatim as the tool error; the model quoted it back every time; the JSON event stream shows it as `state.error` | measured |
| (iii) shell and file tools | shell, write, read, edit, glob, grep (claude-agent) and patch (GPT) all blocked with the message | measured |

GO.
`BLOCK_ROUTE=throw`.
Fallback `permission` (works, measured for shell/write/read and with `--auto`, but its event carries only `action` + `resources`, not the full tool input).
Last resort `rewrite` (shell only; the call shows as `completed` with the original command in the member's stream, which is misleading).

## The truth table

| # | Question | Answer | Confidence |
|---|---|---|---|
| 1a | Loads from `plugins: ["<abs dir>"]` in `$OPENCODE_CONFIG_DIR/opencode.json` | yes | measured |
| 1b | Loads from `$OPENCODE_CONFIG_DIR/plugins/<dir>/index.ts` with an empty config | yes | measured |
| 1c | Plain `.ts`, no build step, no `package.json`, no `node_modules` | yes; `import { Plugin } from "@opencode/plugin"` is resolved by the host itself | measured |
| 1d | `opencode plugin list` / `plugin check` see a local directory plugin | no: both print "No plugins found" / "No package plugins found" even with `--standalone` | measured |
| 1e | Config key that works | `plugins` (array of strings; a local entry must be a directory) | measured |
| 1f | `OPENCODE_CONFIG_DIR` replaces `~/.config/opencode` (not added to it) under `--standalone` | yes; `debug config --standalone` lists the temp dir and `~/.opencode`, not `~/.config/opencode` | measured |
| 1g | Without `--standalone`, CLI commands talk to the background service, which ignores the env overrides | yes; `debug config` without it listed the real `~/.config/opencode` | measured |
| 2 | `execute.before` payload | `{tool, sessionID, agent, messageID, id, input}`; NO cwd/directory field (see table below) | measured |
| 3a | `throw` blocks, message reaches model, session continues | yes; next step ran, `opencode run` exit 0 | measured |
| 3b | `throw` stops `permission.evaluate` and `execute.after` from firing | yes (execute.before runs first) | measured |
| 3c | `permission-deny` blocks shell, write, read; message reaches model | yes; error is `Tool.Error` wrapping `Permission.BlockedError`, message `PROBE-BLOCK-MSG` | measured |
| 3d | `--auto` bypasses a hook-set deny | no | measured |
| 3e | `execute.before` input rewrite | works (`ev.input = {...}` replaced the command); member stream still shows the ORIGINAL input and status `completed` | measured |
| 4 | Hooks fire for subagent tool calls, foreground and background | yes, with the CHILD session id and `agent:"general"` | measured |
| 5a | Code Mode (`execute` tool) inner calls fire `execute.before` | yes, as a separate event with id `<namespace>_<tool>` (e.g. `opencode_worktree_list`) after the `execute` event | measured |
| 5b | MCP tools | not tested (no MCP server configured); docs/binary suggest they surface through Code Mode | unresolved |
| 6a | `context` + `event.system.push` reaches a non-Claude-Agent model | yes (`opencode/deepseek-v4.1-flash-free` echoed `PROBE-SENTINEL-7731-SYS`) | measured |
| 6b | `context` + `event.system.push` reaches a `claude-agent/*` model | **NO**: dropped; the provider builds its prompt from `event.options.claudeCodeInstructions` | measured |
| 6c | Appending to `event.options.claudeCodeInstructions` reaches a `claude-agent/*` model | yes (echoed `PROBE-SENTINEL-7731-OPT`, not the SYS one) | measured |
| 6d | System pushes persist to the next call | no; every `context` call starts at the same `system` length (4), so push on every call | measured |
| 6e | `prompt` gives the raw member text + session id | yes: `event.prompt.text`, `event.sessionID` | measured |
| 6f | `prompt` then `context` for the same turn share `sessionID` and process | yes (same pid, `prompt` fires first) | measured |
| 6g | Once-per-session signal | none from the host for the primary session (no `session.created` delivered for it); the plugin keys "first `context` for this `sessionID`" itself | measured |
| 6h | `execute.after` can rewrite the result the model sees | yes (`PROBE-REDACTED` reached the model) | measured |
| 7 | At the Glitch root OpenCode already loads | root `AGENTS.md` (verbatim, under "Instructions from: .../AGENTS.md"), plus Glitch's `.claude/skills` in `<available_skills>`; NOT `CLAUDE.md`, NOT USER.md/MEMORY.md content | measured |
| 8 | Subagent tool for the keeper directive | `subagent {agent, description, prompt, background?, model?, sessionID?}`; agents `general`, `explore` | measured |
| 9 | Windows | nothing run on Windows (list below) | unresolved |

## Item 1: loading (measured)

Config route, from `stderr.log` of `load-config`:

```
msg="loading plugin" id=<adapter folder>/spike/probe-plugin entrypoint=file://<adapter folder>/spike/probe-plugin/index.ts
```

Directory route, from `load-dir`:

```
msg="loading plugin" id=/var/folders/.../config/plugins/glitch-probe entrypoint=file:///private/var/folders/.../config/plugins/glitch-probe/index.ts
```

Both logged one `"hook":"setup"` line from the probe, with no model call (`PROBE_NOMODEL=1` runs `opencode debug agents --standalone`, which starts a private server and loads plugins).
The control (`PROBE_LOAD=none`) logged zero setup lines.
The probe directory still holds only `index.ts` after all runs: the host created no `package.json` or `node_modules`.
The plugin directory is watched (`watcher subscribe path=.../probe-plugin type=directory`), so edits hot-reload.
`setup` received:

```
{"appVersion":"2.2.7","location":{"directory":"/private/var/folders/.../work","project":{"id":"b3122d...","directory":"/private/var/.../work","canonical":"/private/var/.../work"}},"ctxKeys":["app","location","options","agent","aisdk","command","credential","event","experimental","generate","model","provider","integration","mcp","permission","plugin","reference","rpc","skill","storage","tool","vcs","websearch","worktree","session","shell"],"options":{}}
```

Note `ctx.location.directory` is the realpath (`/private/var/...` for a `/var/...` cwd): the root walk must realpath its own input too.
`ctx.app.version` is `"2.2.7"` (the version check in CP2-T4 can rely on it).

## Item 2: `execute.before` raw payloads (measured)

Copied verbatim from `spike/evidence/execute-before.jsonl` (the `event` field is the object the hook received).

| Tool id | Raw `event` |
|---|---|
| `shell` | `{"tool":"shell","sessionID":"ses_e35b75d7dffeeLUtMOUl3rWTAq","agent":"build","messageID":"msg_1ca48a437001QtXJBLYGnohDKO","id":"toolu_01ExLYSz9VjrRBpFT9JvLFMj","input":{"command":"echo hi"}}` |
| `read` | `{"tool":"read","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca495399001cap4ueedl5mNgh","id":"toolu_014GQZrRNsVnKMZbW4NXhUaN","input":{"path":"a.txt"}}` |
| `write` | `{"tool":"write","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca494923001CEZx7w6rmFgm84","id":"toolu_01Rey69aRfdJexNk9fFzf4ur","input":{"path":"a.txt","content":"x"}}` |
| `edit` | `{"tool":"edit","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca4957b400161Qj750hHxt9sm","id":"toolu_01FBQLwvW1XxTHrWWvdRJY5j","input":{"path":"a.txt","oldString":"x","newString":"y"}}` |
| `patch` | `{"tool":"patch","sessionID":"ses_e35b10a2dffeDXC7JCgJyqMGpO","agent":"build","messageID":"msg_1ca4ef742001S6E2REaUQ5BMgS","id":"call_3bbfedc692414dbb9e1d790fc997f368","input":{"patchText":"*** Begin Patch\n*** Add File: a.txt\n+x\n*** End Patch"}}` |
| `glob` | `{"tool":"glob","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca495c470016NuucEbBnqduva","id":"toolu_01XbFidzgsn6tBNjT2kV6VpZ","input":{"pattern":"*.txt"}}` |
| `grep` | `{"tool":"grep","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca49605f001sS0Qop9CRLaQxT","id":"toolu_01CM42hTbs1DmtubqXozPgGv","input":{"pattern":"y","include":"*.txt"}}` |
| `webfetch` | `{"tool":"webfetch","sessionID":"ses_e35b6b867ffeuv4BjvBbRkyglw","agent":"build","messageID":"msg_1ca4964c7001O6An32r9g73rf5","id":"toolu_01A3AoRdYXyRygWETEn1VSCi","input":{"url":"https://example.com"}}` |
| `subagent` | `{"tool":"subagent","sessionID":"ses_e35b5489bffecS9TKiNyAEM4Vc","agent":"build","messageID":"msg_1ca4ab8e9001L3CzAaLbaOAsxH","id":"toolu_01KVUufknkksuLL3tkpBCKqt","input":{"agent":"general","description":"Run probe command","prompt":"Run this shell command and report the exact result: echo PROBE-RAN > ran.txt"}}` |
| `execute` | `{"tool":"execute","sessionID":"ses_e35b47aa1ffeYvmWNuFarJ4Ubw","agent":"build","messageID":"msg_1ca4b86f5001RbPqTgMXlQrJEU","id":"toolu_0137e1J4jWViYQLKP5hxYaUT","input":{"code":"return await tools.opencode.worktree_list()"}}` |
| `opencode_worktree_list` (Code Mode inner call) | `{"tool":"opencode_worktree_list","sessionID":"ses_e35b47aa1ffeYvmWNuFarJ4Ubw","agent":"build","messageID":"msg_1ca4b86f5001RbPqTgMXlQrJEU","id":"toolu_0137e1J4jWViYQLKP5hxYaUT","input":{}}` |

Field names: session id `sessionID`; agent `agent` (`build`/`plan` primary, `general`/`explore` subagent); call id `id`; tool id `tool` (a plain string); arguments `input`.
**There is no cwd/directory field in the event.**
Relative paths (`a.txt`) are relative to the session directory: take it from `ctx.location.directory` at setup, or per session from `(await ctx.session.get({sessionID})).location.directory` (measured, item 4).
The shell's resolved working folder is only visible in `shell.create.before` (`{"command":"echo hi","cwd":"/private/var/.../work","timeout":120000,"shell":"/bin/zsh","env":{...}}`), which fires after `execute.before`.

Full argument schemas, read from the `tools` map the `context` hook receives (measured, `subagent-throw` and `inject-openai` runs):

| Tool id | Input keys (required in bold) |
|---|---|
| `shell` | **command**, workdir, timeout, background |
| `read` | **path**, offset, limit (also reads directories; there is no separate `list` tool) |
| `write` | **path**, **content** |
| `edit` | **path**, **oldString**, **newString**, replaceAll |
| `patch` | **patchText** (V4A: `*** Begin Patch` / `*** Add File:` / `*** Update File:` / `*** Move to:` / `*** Delete File:` / `*** End Patch`) |
| `glob` | **pattern**, path, hidden, limit |
| `grep` | **pattern**, path, include, literal, caseSensitive, limit |
| `webfetch` | **url**, format, timeout |
| `websearch` | **query** |
| `subagent` | **agent**, **description**, **prompt**, model, sessionID, background |
| `skill` | **id** |
| `question` | **questions** |
| `experimental_image_generate` | **prompt**, images, model, path (writes a file at `path`: a write tool) |
| `execute` | **code** (Code Mode JavaScript; its inner tool calls fire their own `execute.before`) |

Tool sets differ by model (measured):
`claude-agent/claude-haiku-4-5-20251001` and `opencode/deepseek-v4.1-flash-free` get `edit` + `write` and no `patch`;
`openai/gpt-5.5` gets `patch` and NO `edit`/`write`.
There is no `todo`, `schedule`, `list` or `task` tool in this build's catalog for these models.
Code Mode catalog (only callable inside `execute`): `opencode.session_move`, `opencode.worktree_create`, `opencode.worktree_list`, `opencode.worktree_remove` (has `force`), `opencode.list_mcp_resources`, `opencode.models`, `opencode.read_mcp_resource`, `opencode.session_rename`; inner ids seen by hooks are `opencode_<name>`.

## Item 3: block mechanism (measured)

**throw** (`throw-shell`): the hook threw `new Error("PROBE-BLOCK-MSG")`; JSON stream:

```
tool_use {"status": "error", "input": {"command": "echo PROBE-RAN > ran.txt"}, "error": "PROBE-BLOCK-MSG", ...}
text "The command failed with this exact error:\n\n```\nPROBE-BLOCK-MSG\n```"
```

`run.txt`: `exit=0`, `ran-file=ABSENT`.
The model's next `context` call carried the tool result `{"type":"error","value":{"error":{"type":"unknown","message":"PROBE-BLOCK-MSG"},"content":[]}}`.
The docs' "a plain rejection ends the current step" did not happen: a second step ran and the model answered.
`throw-filetools` (`PROBE_TOOLS=write,read,edit,glob,grep`): all five `tool_use` lines `error PROBE-BLOCK-MSG`, `ran-w.txt` absent, model quoted all five.
`throw-patch` (`openai/gpt-5.6-luna`): `TOOL patch error PROBE-BLOCK-MSG`, `ran-p.txt` absent.
`subagent-throw`: the child session's shell call was blocked the same way and the parent got the text back.
With `throw`, neither `permission.evaluate` nor `execute.after` fires for that call (no such lines in `throw-shell/probe.log`).

**permission** (`permdeny`): setting `ev.effect="deny"; ev.message="PROBE-BLOCK-MSG"` in `permission.evaluate`:

```
TOOL shell error PROBE-BLOCK-MSG {"command": "echo PROBE-RAN > ran.txt"}
TOOL write error PROBE-BLOCK-MSG {"path": "ran-w.txt", "content": "x"}
TOOL read error PROBE-BLOCK-MSG {"path": "ran.txt"}
```

`execute.after` then fires with `"status":"error","error":{"_tag":"Tool.Error","error":{"_tag":"Permission.BlockedError","rules":[{"action":"*","resource":"*","effect":"allow"}],"permission":"shell",...}}`.
`permdeny-auto` (same with `--auto`): still `TOOL shell error PROBE-BLOCK-MSG`, `ran-file=ABSENT`.
`permission.evaluate` payloads (verbatim in `spike/evidence/permission-evaluate.jsonl`): `{"sessionID","agent","action","resources","metadata?","source":{"type":"tool","messageID","id"},"effect"}`; `action` is `shell` / `read` / `edit` (for write, edit AND patch) / `glob` / `grep` / `webfetch` / `subagent`; `resources` is the command string, path(s), pattern or URL; the default rule set is allow-all (`{"action":"*","resource":"*","effect":"allow"}` plus `read *.env` ask).
It fires AFTER `shell.create.before` for shell, but the shell did not run.
Weakness for a guard: no full `input` (no `workdir`, no `content`, no `oldString`), so the write-guard's content checks cannot run from here.

**rewrite** (`rewrite`): `ev.input = {...ev.input, command: "echo PROBE-BLOCK-MSG"}` worked: `shell.create.before` saw `echo PROBE-BLOCK-MSG`, `ran-file=ABSENT`, model printed `PROBE-BLOCK-MSG`.
But the member's stream shows `TOOL shell completed {"command": "echo PROBE-RAN > ran.txt"} "PROBE-BLOCK-MSG\n"`: the original command with status completed.
Shell-only; not a primary route.

## Item 4: subagent coverage and marker (measured)

`subagent-throw` and `subagent-bg` (`background: true`): the child's calls fire every hook with the child's `sessionID` and `agent:"general"`.
The child's `prompt` hook fires too, with text prefixed by the host: `"You are a subagent spawned by another session.\nRun the shell command: echo BG-HI"`.
**Subagent marker (use this):** `await ctx.session.get({sessionID})` returns `parentID` for a child and no `parentID` for a primary session:

```
session.get {"id":"ses_e35afbc9cffeRDv2RBF34YP3Sh","projectID":"f834...","model":{...},"time":{...},"location":{"directory":"/private/var/.../work"}}
session.get {"id":"ses_e35afac4dffeABW3gLX1l9acME","parentID":"ses_e35afbc9cffeRDv2RBF34YP3Sh","projectID":"f834...","agent":"general",...,"title":"Run echo command","location":{...}}
```

The same `parentID` arrives in the `session.created` event (`data.parentID`) via `ctx.event.subscribe`.
Secondary signal: `event.agent` is a subagent-mode agent (`general`, `explore`; `opencode debug agents` lists `mode: subagent`).
The bridge's `agent_id` (read-guard `_is_sub_agent`) should be set from `parentID` presence (or the child `sessionID`), resolved once per session id and cached.

## Item 5: other tools (measured / unresolved)

Code Mode inner calls are hooked (measured): `tool.execute.before` for `execute`, then for `opencode_worktree_list` with its own `input`.
So `opencode_worktree_remove {directory, force}` and `opencode_session_move {directory}` reach the guard, under those ids; the guard must read `directory` as a path (flag for CP1).
MCP tools: unresolved (no MCP server configured; record "not tested").
`skill`, `question`, `websearch`, `experimental_image_generate`: in the catalog (measured), their `execute.before` payloads not captured (docs-only for payload shape; schema measured).

## Item 6: injection, field names (measured)

`session.prompt` raw event:

```
{"sessionID":"ses_e35b75d7dffeeLUtMOUl3rWTAq","messageID":"msg_1ca48a29d001aO9wJt2A8qchF9","prompt":{"text":"run the shell command: echo hi","files":[]},"delivery":"steer"}
```

Fields: `sessionID`, `messageID`, `prompt.text`, `prompt.files`, `delivery`.
It fires once per member message, before the first `context` call, in the same process.

`session.context` event keys: `["sessionID","model","system","messages","options","agent","tools"]`.
`model` is `{"id","providerID","variant"}`; `system` is an array of `{type:"text", text}` (4 parts, starting length 4 on every call); `messages` is the model-bound transcript; `tools` is a map id -> `{description, input}`; `options` for claude-agent holds `claudeCodeAgent` and `claudeCodeInstructions` (strings).
`context` fires on EVERY model call (twice for a one-tool turn), so a push must be repeated each call.

**Two channels are needed (deviation from the plan, which assumed `system.push` alone):**

1. `event.system.push({type:"text", text})` works for normal providers (measured with `opencode/deepseek-v4.1-flash-free`: reply `PROBE-SENTINEL-7731-SYS`).
2. For `providerID === "claude-agent"` the provider ignores `system`: its built-in `ClaudeInstructions` context hook sets `options.claudeCodeInstructions = system.slice(1)` joined (binary strings: `o.options[e8]=YK(o.system)`), and it runs before user plugins.
The provider then calls Claude Code with `systemPrompt: {type:"preset", preset:"claude_code", append: [runtime_info, agent role, claudeCodeInstructions]}`.
Measured: with both channels the claude-agent model replied only `Probe code B: PROBE-SENTINEL-7731-OPT` (the options channel); in `inject-codemode` (system push only) it said it saw no probe word.
So the plugin must also do `if (typeof ev.options?.claudeCodeInstructions === "string") ev.options.claudeCodeInstructions += "\n\n" + text`.

The claude-agent provider spawns Claude Code with `settingSources: []`, `settings: {disableAllHooks: true}`, `tools: []`, `allowedTools: ["mcp__opencode__*"]`, `permissionMode: "dontAsk"` (binary strings, docs-only): Glitch's own Claude Code hooks never run under OpenCode, so the plugin is the only guard there.

`execute.after` event: `{tool, sessionID, agent, messageID, id, input, status, result?, error?}`; `result` is `{output: <tool-specific>, content: [{type:"text", text}], metadata?}`; shell `result.output` is `{exit, truncated, output, status}`; on error `status:"error"` and `error` is a `Tool.Error` object (verbatim samples in `spike/evidence/execute-after.jsonl`).
Rewriting is possible (measured, `after-rewrite`): assigning `ev.result.output`/`ev.result.content` changed what the model saw (`TEXT PROBE-REDACTED`).
The host's own plugins append notes as `ev.result = {...ev.result, content: [...ev.result.content, {type:"text", text}]}` (binary strings): the natural way to attach the secret-scan note.

## Item 7: what OpenCode already loads at the Glitch root (measured)

Run `root-observe`, cwd `<your Glitch folder>`, observe-only probe (it pushes nothing, so it stands in for "plugin disabled"; prompt forbade tools, none were called).
The instructions block (system part 2, 54,711 chars) contains `Instructions from: <your Glitch folder>/AGENTS.md` followed by that file verbatim.
It does NOT contain `CLAUDE.md`'s content (only mentions of the name inside AGENTS.md), and does not contain `Build and test an OpenCode adapter` (USER.md) or any `@glitch-mem` import.
The model's answer: `Instruction files in context: AGENTS.md (from <your Glitch folder>/)` and the `@` form is absent.
`<available_skills>` includes Glitch's own `.claude/skills` (e.g. `bootstrap`) and `~/.claude/skills`; the `skill` tool can load them.
Watched instruction paths (stderr): `~/AGENTS.md`, `<your Glitch folder>/AGENTS.md`, `$OPENCODE_CONFIG_DIR/AGENTS.md`.
Consequence: the plugin injects identity + memory + day context only (what `build_context` returns) and never pastes AGENTS.md or CLAUDE.md.

## Item 8: keeper directive wording (measured)

Tool id `subagent`; arguments `{"agent": "general", "description": "<3-5 words>", "prompt": "Execute the memory-keeper procedure in .claude/skills/memory-keeper/SKILL.md.", "background": true}`.
Background mode measured: the tool returns at once (`"status":"running"`, "You will be notified automatically when it finishes"), the child's tool calls are hooked, and `opencode run` waited for it before exiting.
Available agents (`opencode debug agents`): primary `build`, `plan` (+ hidden `compaction`, `summary`, `title`); subagent `general`, `explore`.

## Item 9: not tested on this Mac (Windows)

- Nothing ran on Windows: the binary, plugin loading, `@opencode/plugin` resolution on Windows paths, and the `file:///C:/...` entrypoint form are all unresolved.
- Whether `shell` runs PowerShell or `cmd` or Git Bash (`OPENCODE_GIT_BASH_PATH` exists in the binary) and therefore what `input.command` looks like.
- Path shapes in `input.path` (backslashes, drive letters) and `ctx.location.directory` case/realpath behaviour.
- Spawning `uv` from the Bun runtime on Windows (`uv.exe` resolution, argv quoting).
- `OPENCODE_CONFIG_DIR` / `%APPDATA%` config layout on Windows.

## What the plan got wrong or did not know (flag for the executor)

1. Memory-in needs two channels: `system.push` is silently dropped for `claude-agent/*` models; append to `options.claudeCodeInstructions` as well (CP3).
2. `execute.before` has no cwd; the plugin must supply `session_dir` from `ctx.location.directory` (realpathed) and resolve relative `input.path` against it.
3. The docs' "plain rejection ends the current step" is not what happens: the throw becomes the tool's error and the turn continues; this is why `throw` is the chosen route.
4. Tool catalog: no `list`, `todo`, `schedule` or `task`; `read` lists directories; `patch` only for GPT models (instead of `edit`/`write`); extra write-capable ids the guard must treat as writes: `experimental_image_generate` (`path`), `opencode_worktree_remove` (`directory`, `force`), `opencode_session_move` (`directory`), `opencode_worktree_create`.
5. Subagent marker is `parentID` from `ctx.session.get`, not something in the tool event.
6. `opencode plugin list` does not show local directory plugins; install/status (CP5) must check the config text and the plugins dir itself.
7. CLI commands without `--standalone` go to the background service and ignore `OPENCODE_CONFIG_DIR`; every scripted run must pass `--standalone`.
8. `openai/gpt-5.5` is refused for a ChatGPT login (`"The 'gpt-5.5' model is not supported when using Codex with a ChatGPT account."`); `openai/gpt-5.6-luna` worked.
9. The session can be moved (`opencode_session_move`); the plan's "bind the root once at setup" still holds, but `ctx.location` is per plugin instance, so per-session directories should come from `session.get(...).location.directory` if they ever differ.

## Probe run ledger

18 headless `opencode run` invocations with a model call (one failed fast with HTTP 400 and spent nothing), plus 3 load-only runs with no model call:
observe x2 (incl. validation), throw-shell, observe-filetools, throw-filetools, permdeny, permdeny-auto, rewrite, subagent-throw, inject-codemode, inject-claude, inject-openai (400), inject-free, observe-patch, throw-patch, after-rewrite, root-observe, subagent-bg; load-config, load-dir, load-none.
Models: `claude-agent/claude-haiku-4-5-20251001` (14 runs), `openai/gpt-5.5` (1, refused), `openai/gpt-5.6-luna` (2), `opencode/deepseek-v4.1-flash-free` (1).
No secret was placed in any prompt.
Every run: `BRAIN-SNAPSHOT: clean`.
