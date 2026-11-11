# CP0 capture and persistence decisions, measured 2026-11-11 on opencode v2.2.7 (items 10-17)
CAPTURE_SOURCE=export-cli
TURN_END_EVENT=session.execution.succeeded
COMPACTION_SIGNAL=session.compaction.ended
MESSAGES_PERSIST=no
VISIBLE_SURFACE=none
NEWEST_VERSION=2.2.7
STORED_PUSHED=no
# NEWEST_VERSION note (2026-11-11, measured from the release list): v2.2.7 (published 2026-11-10) IS the newest published 2.x release, and it is the installed one, so there is nothing newer to download and no newer binary was tested. The installer's "latest" endpoint reports v1.19.5 because it only serves the OpenCode 1 line, which this adapter does not target.
GO
BLOCK_ROUTE=throw
FALLBACK_ORDER=throw > permission > rewrite (rewrite is shell-only and reports the call as "completed"; never use it as a primary route)

# OpenCode v2.2.7 plugin API: measured findings (CP0)

This file is the authority for every OpenCode name used by the adapter.
Every row says how it is known: **measured** (a real headless run, log line quoted), **docs-only** (official docs or the binary's own strings, not exercised), or **unresolved**.

Host: `~/.opencode/bin/opencode` `opencode v2.2.7`, macOS (Darwin), Bun runtime embedded in the binary.
Harness: the author's private test harness (not included in this folder): a probe plugin, a throwaway config dir, `--standalone`, `OPENCODE_DISABLE_AUTOUPDATE=1`, data dir untouched, working folder a fresh temp dir (except item 7).
Raw logs and the payload lines this file relies on were kept in the author's private test records (not included); everything the findings need is quoted in the items below.
Brain-root snapshot (`git status --porcelain --ignored`) was taken before and after every run: clean on all of them (`BRAIN-SNAPSHOT: clean` in every run log).

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
| 10a | Turn-end event for a primary session | `session.execution.succeeded` (`data: {sessionID}`), once per turn, after the last `session.step.ended`; no `session.idle`/`session.status`/`message.completed` exists | measured |
| 10b | OpenCode's project directory comes from `$PWD`, not the process cwd | yes: with an inherited `PWD` the session's directory was the caller's folder | measured |
| 11a | Supported CLI read path | `opencode session export --standalone <id>` -> `{info, messages[]}` JSON, about 0.24 s, works with no background service and while another server holds the same data dir | measured |
| 11b | Listing sessions | `opencode session list --standalone --format json`: top-level sessions of the CURRENT project only (`id,title,created,updated,projectId,directory`); no children, no `parentID` | measured |
| 11c | Plugin API returns messages | no: `ctx.session` has `hook,list,create,get,remove,switchAgent,switchModel,prompt,generate,command,compact,synthetic,interrupt,update,move,wait,context,instructions`; `get` returns info only; `ctx.storage` is a key/value store | measured (names) / docs-only (no message method exercised) |
| 11d | Raw store | SQLite `opencode.db` (`session_v2`, `session_message`, `session_part`); member text in `session_message.data.text` (type `user`), assistant text in `data.content[].text` (type `assistant`) | measured |
| 12a | Export returns all five turns; turn end fires five times | yes, both | measured |
| 12b | Plugin can tell a resume | yes: first `context` in the new process carries the earlier history (`messagesLength` 3, 5, 7...), and `session.get` gains `outcome` | measured |
| 13a | `MESSAGES_PERSIST` (message pushed into `ev.messages`) | no: gone by the second model call of the SAME turn | measured |
| 13b | `STORED_PUSHED` | no: sentinel absent from export, `session_message`, `session_part`, `session_inbox` and every file in the data dir | measured |
| 13c | A pushed message reaches the model on its own call | yes | measured |
| 14a | `COMPACTION_SIGNAL` | `session.compaction.started {sessionID, reason}` then `session.compaction.ended {sessionID, reason, model, text, cost, tokens}` (`.failed`/`.interrupted` exist in the binary) | measured (`failed`/`interrupted` docs-only) |
| 14b | Force compaction headlessly | config `"compaction": {"auto": true, "buffer": "99%"}`; it ran before every turn's first model call | measured |
| 14c | The compaction model call fires `session.context` | no | measured |
| 15 | `VISIBLE_SURFACE` from a plugin | none: no toast/notification/tui call on `ctx`; `ctx.session.synthetic` and `ctx.experimental.terminal` exist, not exercised | measured (names) |
| 16a | OpenCode puts a session id in the shell env | no (`OPENCODE_CONFIG_DIR`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_PRINT_LOGS`, `OPENCODE_TERMINAL` only) | measured |
| 16b | `pane_identity` / `workspaces.py active` under that env | unconfident; scope `none`; the machine-wide record is NOT read on this machine; task null | measured outside OpenCode (in-OpenCode run blocked by quota) |
| 16c | Config permission rules | key `permissions`, ordered `[{action, resource, effect}]`, later rule wins; a config deny never reaches `permission.evaluate` | measured |
| 17a | Child turn end carries a parent id | no: `session.execution.succeeded` is `{sessionID}` only; `session.created` and `ctx.session.get` carry `parentID` | measured |
| 17b | Export exposes parentID | yes for the child (`info.parentID`); the parent's `subagent` tool part has `state.metadata.sessionID` = child id; `session list` hides children | measured |

## Item 1: loading (measured)

Config route, from `stderr.log` of `load-config`:

```
msg="loading plugin" id=<probe plugin folder> entrypoint=file://<probe plugin folder>/index.ts
```

Directory route, from `load-dir`:

```
msg="loading plugin" id=/var/folders/.../config/plugins/glitch-probe entrypoint=file:///private/var/folders/.../config/plugins/glitch-probe/index.ts
```

Both logged one `"hook":"setup"` line from the probe, with no model call (`PROBE_NOMODEL=1` runs `opencode debug agents --standalone`, which starts a private server and loads plugins).
The control (`PROBE_LOAD=none`) logged zero setup lines.
The probe directory still holds only `index.ts` after all runs: the host created no `package.json` or `node_modules`.
The plugin directory is watched (`watcher subscribe path=... type=directory`), so edits hot-reload.
`setup` received:

```
{"appVersion":"2.2.7","location":{"directory":"/private/var/folders/.../work","project":{"id":"b3122d...","directory":"/private/var/.../work","canonical":"/private/var/.../work"}},"ctxKeys":["app","location","options","agent","aisdk","command","credential","event","experimental","generate","model","provider","integration","mcp","permission","plugin","reference","rpc","skill","storage","tool","vcs","websearch","worktree","session","shell"],"options":{}}
```

Note `ctx.location.directory` is the realpath (`/private/var/...` for a `/var/...` cwd): the root walk must realpath its own input too.
`ctx.app.version` is `"2.2.7"` (the version check in CP2-T4 can rely on it).

## Item 2: `execute.before` raw payloads (measured)

Copied verbatim from `tests/ts/fixtures/evidence-execute-before.jsonl` (the `event` field is the object the hook received).

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

Run log: `exit=0`, `ran-file=ABSENT`.
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
`permission.evaluate` payloads (verbatim in `tests/ts/fixtures/evidence-permission-evaluate.jsonl`): `{"sessionID","agent","action","resources","metadata?","source":{"type":"tool","messageID","id"},"effect"}`; `action` is `shell` / `read` / `edit` (for write, edit AND patch) / `glob` / `grep` / `webfetch` / `subagent`; `resources` is the command string, path(s), pattern or URL; the default rule set is allow-all (`{"action":"*","resource":"*","effect":"allow"}` plus `read *.env` ask).
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

`execute.after` event: `{tool, sessionID, agent, messageID, id, input, status, result?, error?}`; `result` is `{output: <tool-specific>, content: [{type:"text", text}], metadata?}`; shell `result.output` is `{exit, truncated, output, status}`; on error `status:"error"` and `error` is a `Tool.Error` object (verbatim samples in `tests/ts/fixtures/evidence-execute-after.jsonl`).
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

## Items 10-17: capture and context persistence (measured 2026-11-11)

All runs used `opencode/deepseek-v4.1-flash-free` through the author's private test harness (throwaway config, data, state and cache dirs, `--standalone`, `OPENCODE_DISABLE_AUTOUPDATE=1`).
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.
Every run log ends `BRAIN-SNAPSHOT: clean` with all three `GUARDED` paths `unchanged`.

## Item 10: events and the turn-end signal (measured)

Run `p1-events-v2`: turn 1 forces `shell echo hi`, turn 2 is a plain question; each turn is its own `opencode run` process.
Hooks fired per turn: `session.prompt`, then `session.context` (twice in the tool turn, once in the plain turn), `tool.execute.before`, `shell.create.before`, `permission.evaluate`, `tool.execute.after`.
Wildcard events, in order, for the tool turn (deltas omitted):
`session.inbox.enqueued`, `session.execution.started`, `session.instructions.updated`, `session.inbox.delivered`, `session.step.started`, `session.tool.input.started`, `session.tool.input.ended`, `session.tool.called`, `session.step.streamed`, `session.tool.progress`, `session.tool.success`, `session.step.ended`, `session.usage.updated`, `session.step.started`, `session.text.started`, `session.text.ended`, `session.step.streamed`, `session.step.ended`, `session.usage.updated`, `session.execution.succeeded`, then `session.renamed` (first turn only) and `location.shutdown`.
Also seen: `shell.created`, `shell.exited`, and startup `*.updated` events.
No event named `session.idle`, `session.status`, `message.completed` or `step.finished` fired.

```
{"hook":"event",...,"type":"session.execution.succeeded","known":true,"keys":["id","created","type","durable","data"],"dataKeys":["sessionID"],"sessionID":"ses_e34e16ea8ffeWU6Tjl7ruS9jb2","hasParentID":false,"sentinels":[]}
```

`TURN_END_EVENT=session.execution.succeeded`: one per turn, after the final `session.step.ended`, in both turns.
`session.step.ended` fires per model step (twice in a tool turn), so it is not the turn end.
The binary also names `session.execution.failed` and `session.execution.interrupted` (docs-only); the adapter should treat all three as turn end.
The store writes a matching `idle` row (`session_message.type = 'idle'`, `data.outcome = "succeeded"`) at each turn end.

**Harness defect found and fixed (item 10b):** OpenCode takes its project directory from `$PWD`, not from the process working folder.
The harness passed the caller's `PWD` through, so the smoke run and the first `p1-events` run used the Glitch folder as their project (stderr `directory=<Glitch folder>`, store `project.worktree = <Glitch folder>`, `vcs = git`).
Nothing was written there (`BRAIN-SNAPSHOT: clean`; no `.git/opencode` file; `.git/index` untouched), but OpenCode built a snapshot repo in the throwaway data dir with `objects/info/alternates` pointing at the Brain's git objects.
The author's private test harness now sets `PWD` to the work folder; `p1-events-v2` shows `directory=/private/var/.../work`.
Consequence for the adapter: whatever starts OpenCode decides the project by `PWD`, and a plugin should take the session directory from `ctx.location` / `session.get().location.directory`, never from `process.cwd()`.

## Item 11: storage and the capture source (measured)

`CAPTURE_SOURCE=export-cli`.
There is no top-level `opencode export` in 2.2.7; the command is `opencode session export [--sanitize] [<session>]` (`opencode session` has `list`, `delete`, `export`, `import`).
`opencode session export --standalone <id>` returned the whole chat as JSON in 0.23-0.24 s (`/usr/bin/time -p`, `real 0.24`).
`--standalone` is needed: it starts a private server on the data dir from `XDG_DATA_HOME`; without it the CLI talks to the background service (item 1g), which a member's real data sits behind.
It needs no running service, and it worked while a separate `opencode serve` held the same data dir (`ok msgs 4`, `real 0.24`).
Shape (from the author's private test records, not included):

```
{"info": {id, projectID, model{id,providerID,variant}, cost, tokens{input,output,reasoning,cache}, outcome, time{created,updated,busy,idle}, title, location{directory}, parentID (child only), agent (child only)},
 "messages": [ {type:"user", id, time, text, files}
             | {type:"assistant", id, time, agent, model, content[], finish, rawFinish, cost, tokens}
             | {type:"idle", id, time, outcome}
             | {type:"compaction", id, time, reason, status, summary, model, cost, tokens, metadata?} ]}
assistant content part types seen: text {type,text}; tool {type,id,name,state{status,input,content,metadata},executed,time}; reasoning
```

Member text is `messages[type=user].text`; assistant text is `messages[type=assistant].content[type=text].text`.
Listing: `opencode session list --standalone --format json` gives `[{id, title, updated, created, projectId, directory}]`, newest first, top-level sessions of the current project (the `PWD` folder) only, 0.29 s; it carries the start folder (`directory`) but no `parentID`, and child sessions are not listed.
`plugin-api` is not a capture source: `ctx.session` has no message-returning method (names in item 15), and `ctx.session.get` returns session info only.
Raw store (read-only, throwaway dir): `data/opencode/opencode.db` (+ `-wal`, `-shm`), `log/opencode.log`, `shell/<project>/<shell id>.out`, `snapshot/<project>/...`.
Tables include `session_v2` (`id, parent_id, directory, title, agent, time_idle, idle_outcome, time_compacting`, ...), `session_message` (`id, session_id, type, seq, data`), `session_part` (0 rows: parts live inside `session_message.data.content`), `session_inbox`, `event` (0 rows).
The same database holds `account`, `control_account` and `credential` tables with token columns: a store reader must never be pointed at a real data dir casually; the export CLI is the safer road.

## Item 12: five turns and resume (measured)

Run `p3-five-turns` (turn 3 calls the shell): `session.execution.succeeded` fired five times, once per process; the export returned all five user messages and six assistant messages (two in the tool turn), each turn closed by an `idle` row.
Resume (P3b): every turn after the first is a NEW `opencode run --session <id>` process, so turns 2-5 are resumes.
At the first `session.context` of each new process the history is already there:

```
{"hook":"session.context",...,"pid":30504,...,"messagesLength":11,"messageRoles":["user","assistant","user","assistant","user","assistant","tool","assistant","user","assistant","user"],...,"firstForSession":true,...}
```

and `session.get` on a resumed session carries `outcome` (and `title`), which a brand-new one lacks (from the author's private test records, not included).
So "first `context` for this sessionID in this process with more than one message" = resume.

## Item 13: does a pushed message persist (measured)

`MESSAGES_PERSIST=no`. `STORED_PUSHED=no`.
Run `p4-push-messages`: `PROBE_PUSH=messages_first` with `PROBE_PUSH_NEW_ONLY=1` (push only when the session has no history, so later turn processes do not re-push) put a user message holding `P4-PUSH-6633` into `ev.messages` on call 1 of the tool turn.

```
{"hook":"session.context",...,"messagesLength":1,"messageRoles":["user"],...,"pushed":"messages","messagesLengthAfter":2,...}
{"hook":"session.context",...,"messagesLength":3,"messageRoles":["user","assistant","tool"],"messagesSentinels":[],...,"pushed":"none",...}
```

Call 2 of the SAME turn had 3 messages (user, assistant, tool) and no sentinel: the host rebuilds `messages` from its store on every call.
Turn 2 asked for the word; the model answered `NONE`.
Control `p4-push-system` (system push on every call): turn 2 answered `P4-SYS-6644`.
`p4-push-reach` (single turn, push then ask in the same call): the model answered `P4-PUSH-6655`, so a pushed message does reach the model on its own call.
Stored: the sentinel was in neither the export, `session_message`, `session_part`, `session_inbox`, nor any file under the data dir (`grep -rl` found nothing).
Consequence: context must be re-injected on every `session.context` call (as item 6d already found for `system`); nothing pushed into `messages` survives or lands in the chat record.

## Item 14: compaction (measured)

`COMPACTION_SIGNAL=session.compaction.ended`.
Config key `compaction` ("Conversation compaction behavior"); `buffer` is "Room kept free below the model's context window before automatic compaction, as a share of the window from "1%" to "99%" ... A token count is also accepted" (binary string; `reserved` is an older alias, `preserve_recent_tokens` and `auto` also exist).
Run `p5-compaction` with `--extra-config` `{"compaction": {"auto": true, "buffer": "99%"}}`, three plain turns: an automatic compaction ran before each turn's first model call.

```
{"hook":"event",...,"type":"session.compaction.started",...,"dataKeys":["sessionID","reason"],...}
{"hook":"event",...,"type":"session.compaction.ended",...,"dataKeys":["sessionID","reason","model","text","cost","tokens"],...,"sentinels":["P5-T1-9101"]}
```

Between them came `session.compaction.delta {sessionID, text}` events; then one `session.context` with `messagesLength: 1` (the summary checkpoint plus the new prompt), then `session.execution.succeeded`.
The compaction's own model call did NOT fire `session.context`.
The export keeps every original turn after compaction and adds a `{type:"compaction", reason:"auto", status:"completed", summary, ...}` message before each turn's assistant reply, so capture via export loses nothing.
`.failed` and `.interrupted` are named in the binary (docs-only).
A plugin can also request one: `ctx.session.compact` exists (not exercised).
CLI: no `compact` subcommand; `/compact` is mentioned only in an error string.

## Item 15: a member-visible surface (measured, names only)

`VISIBLE_SURFACE=none`.
`PROBE_SURFACE=1` (`p6-surface`) found no `toast`, `notif`, `alert`, `log` or `tui` function on `ctx`; candidates: `[]`.
`ctx` shape (from the author's private test records, not included): `session: [hook, list, create, get, remove, switchAgent, switchModel, prompt, generate, command, compact, synthetic, interrupt, update, move, wait, context, instructions]`, `storage: [get, set, remove, scan]`, `experimental: [terminal, generate, artifact]`, `event: [subscribe]`, others as in item 1.
The server has a `tui.toast.show` route (binary string) but the plugin context exposes no call to it.
Not exercised (each has side effects on a session or terminal): `ctx.session.synthetic` (an event `session.synthetic` exists), `ctx.experimental.terminal`.

## Item 16: pane identity, task hand-over, start role (partly measured)

Free-model quota ran out before the in-OpenCode run (`{"type":"provider.free-limit","status":429,"retryAfterMs":31675000}` on `p7-deny-all-test`), so the planned run with the work folder in the Glitch folder did NOT happen: **unresolved in OpenCode**.
What was measured instead:

1. Shell env (`shell.create.before`, `p7-deny-test`): OpenCode adds `OPENCODE_CONFIG_DIR`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_PRINT_LOGS`, `OPENCODE_TERMINAL` and otherwise passes the launching env through (the `PROBE_*` keys reach the shell); there is NO session id or agent pid variable (the author's private test records, not included; key names only).
2. The two read commands, run directly with that env (caller's `CLAUDE*`/`CODEX_*`/`GEMINI_*` removed):
   `pane_identity.current_pane()` -> `{'pane_id': None, 'session_id': None, 'pid': None, 'tool': 'unknown', 'source': 'none', 'confident': False}`;
   `workspaces.py active` -> `scope: "none"`, `pane: null`, `slug: null`, `task: null`, with the "say work on <project>" sentence.
   Not confident; scope `none` because pane records exist on this machine, so the machine-wide record is not read (it would be, as scope `machine`, only on a machine where no pane has ever claimed a project).
3. Permission config (`p7-deny-test`, temp work folder): key `permissions`, an ordered list of `{action, resource, effect}`; the later rule wins (`shell *` deny then `shell "echo P7-ALLOWED-3301"` allow: the exact command ran, every other shell call failed `Permission denied: shell`); with `edit` denied the model was not offered `write`.
   Gap found: Code Mode `execute` (inner `opencode_session_rename`) still ran under those rules; a safe in-Brain run needs `{"action":"*","resource":"*","effect":"deny"}` first (that run is the one the quota stopped).

Parity row 18 (new conversation keeps the open task, #320): **not available in OpenCode today**.
The task lookup keys on a confident pane id; OpenCode gives a shell command no session id, so a new OpenCode session gets `task: null` and is asked for its project.
The plugin does see `sessionID` on every hook, so an OpenCode road would have to be built (the plugin passing the session id to the bridge); nothing in the engine reads one now.
Parity row 19 (`GLITCH_START` start role): the bridge is spawned with `{...(opts.env ?? process.env), PYTHONDONTWRITEBYTECODE: "1"}` (`src/plugin/bridge-client.ts:278`), i.e. the plugin process's env.
Under `--standalone` that is the launching shell's env (measured: `PROBE_*` set by the launcher reached the plugin and the shell), so a `GLITCH_START` exported before `opencode --standalone` would reach the bridge (inferred, not run).
Without `--standalone` the plugin runs inside the shared background service, whose env is fixed when the service starts, so a per-pane `GLITCH_START` would not reach it (unmeasured: the real service was not touched).

## Item 17: subagent chats (measured)

Run `p8-subagent`: the primary spawned `subagent {agent:"general"}`.

```
{"hook":"session.get",...,"sessionID":"ses_e34d7c1bbffeJaPOj15SJIKZ2b","keys":["id","parentID","projectID","agent","model","cost","tokens","time","title","location"],"hasParentID":true,"parentID":"ses_e34d7c85cffeQDKWgTzhRU08K2","agent":"general"}
{"hook":"event",...,"type":"session.created",...,"dataKeys":["sessionID","projectID","location","subpath","parentID","slug","title","agent","model","version"],"sessionID":"ses_e34d7c1bbffeJaPOj15SJIKZ2b","hasParentID":true,...}
```

The child's own `session.execution.succeeded` fired (before the parent's) with `dataKeys: ["sessionID"]` only: the turn-end payload carries NO parent id.
So the adapter resolves the parent once per session id from `session.created` or `ctx.session.get` and caches it (item 4).
Read path: `session export --standalone <child id>` returns `info.parentID` (= the primary) and `info.agent: "general"`; the parent's export has a `subagent` tool part whose `state.metadata.sessionID` is the child id; `session list` shows only the primary.
`session.created` for the PRIMARY session was seen in the smoke run but not in `p1-events-v2` or `p8-subagent`: the wildcard subscription can miss it (race at start), so it is not a reliable once-per-session signal (consistent with item 6g).

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
10. The export command is `opencode session export`, not `opencode export`, and it must run with `--standalone` (item 11).
11. OpenCode's project is the launching `$PWD` (item 10b); anything that spawns OpenCode must set `PWD`, and the author's first two test runs had the Glitch folder as their project until the harness was fixed.
12. Turn end is `session.execution.succeeded` (plus `.failed`/`.interrupted`), not `session.idle`; it carries only `sessionID`, so the child/parent split needs the cached `parentID` (items 10, 17).
13. Nothing pushed into `messages` survives even the next call of the same turn or reaches the store: inject on every `context` call (item 13).
14. A plugin has no toast or notification call (item 15).
15. OpenCode gives shell commands no session id, so `pane_identity` is never confident under OpenCode and the #320 task hand-over does not happen (item 16).
16. In headless `opencode run`, every turn is a new process: per-process "first call" state is not per-session state (items 12, 13).

## Probe run ledger

18 headless `opencode run` invocations with a model call (one failed fast with HTTP 400 and spent nothing), plus 3 load-only runs with no model call:
observe x2 (incl. validation), throw-shell, observe-filetools, throw-filetools, permdeny, permdeny-auto, rewrite, subagent-throw, inject-codemode, inject-claude, inject-openai (400), inject-free, observe-patch, throw-patch, after-rewrite, root-observe, subagent-bg; load-config, load-dir, load-none.
Models: `claude-agent/claude-haiku-4-5-20251001` (14 runs), `openai/gpt-5.5` (1, refused), `openai/gpt-5.6-luna` (2), `opencode/deepseek-v4.1-flash-free` (1).
No secret was placed in any prompt.
Every run: `BRAIN-SNAPSHOT: clean`.

Items 10-17 (2026-11-11), all `opencode/deepseek-v4.1-flash-free` via the author's private test harness, 22 `opencode run` turn processes:
smoke (1 turn, before this batch), p1-events (2, Glitch folder was the project, see item 10b), p1-events-v2 (2), p3-five-turns (5), p4-push-messages (2), p4-push-system (2), p4-push-reach (1), p6-surface (1), p5-compaction (3), p8-subagent (1), p7-deny-test (1), p7-deny-all-test (1, refused by the free-quota limit before any tool call).
CLI calls without a model, all `--standalone` against throwaway dirs: `--help`, `session --help`, `session list --help`, `session export --help`, `api --help`, `run --help`, `debug --help`, `serve --help`, `session list`, `session export` (x6), one `serve` on 127.0.0.1 (killed after the concurrency check).
Harness and plugin changes for this batch: the author's private test harness sets `PWD` to the work folder, drops inherited `CLAUDE*`/`CODEX_*`/`GEMINI_*` keys, gained `--extra-config <json>` (merges top-level keys, refuses `plugins`) and `--allow-workdir-in-brain` (work folder only, stores stay in temp; self-test 32/32, never used because of the quota); the probe plugin gained `PROBE_PUSH_NEW_ONLY=1`.

## Items 18-23: real-run validation of CP2 (capture), CP3 (memory-in) and compaction (measured 2026-11-11)

All runs used `openai/gpt-5.6-luna` on the member's ChatGPT sign-in (the author's private test harness, with a temporary login folder), with the Glitch plugin (`src/plugin`) registered FIRST and the probe recorder second, the work folder set to the Glitch root (`--allow-workdir-in-brain`), and an OpenCode `permissions` config of deny-all (CP2, compaction) or deny-all then `read` allowed (CP3).
Orchestration: the author's private test harness (not included).
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.
Every run log ends `BRAIN-SNAPSHOT: clean`, all three `GUARDED` paths `unchanged`, and both new `STAT-WATCH` roots (the real `.claude/data/state` folder and the memory vault, by stat only) `0 changed`.

## Item 18: the harness for real runs (measured)

The author's private test harness gained: a repeatable `--plugin-dir` (registered in the order given, so the recorder sees what the plugin under test pushed); the engine's launch-only state doors pointed into temp on every run (`GLITCH_HOOK_LOG_FILE` -> `results/<run>/hook.log`, `GLITCH_WATCH_STATE_DIR`, `GLITCH_READ_GRANT_STATE_DIR`, `GLITCH_CODEX_PANE_SEEN_FILE`, `GLITCH_OPENCODE_ERRORS_LOG`); `UV_CACHE_DIR`/`UV_PYTHON_INSTALL_DIR` pinned to the caller's own uv folders (without that, `XDG_*` pointing at the login folder would make uv cache and install Python INTO the login folder); `--capture-state DIR` and `--glitch-state-dir DIR` (reuse across runs, strictly inside temp); `--session-id` (continue one of this login's chats); `STAT-WATCH` lines; `--settle` (default 5 s) before the after-snapshot.
Self-test 48/48, ruff clean.
**Harness defect found and fixed (`--settle`):** the plugin's turn-end capture runs in a detached bridge child that outlives `opencode run` (item 20), so the after-snapshot of every run in this job (all six were made before the fix) could run before the last capture wrote (measured in cp2b: snapshot at 16:33:12.353, last stub write 12.551).
Those late writes went to the TEST capture-state (the door is honoured: every capture logs `TEST-ONLY GLITCH_OPENCODE_CAPTURE_STATE honoured`), and after the job the real `opencode-staged.json` and `opencode-chats/` are still absent and the real pending folder holds only a file from 15:43, before these runs.
A second trace of the same race: two removed run roots (`glitch-probe-*`, 16:33 and 16:35, the runs whose last capture did an export) reappeared holding one empty `config/` folder, re-created by the late child's `opencode session export --standalone` under the removed `OPENCODE_CONFIG_DIR`; both were deleted.
The model answered the one-token check with the probe plugin alone (`reply_texts: ["OK"]`).

## Item 19: CP2, the first chat after install is never captured (measured; DEFECT in src/)

Run `cp2-five-turns`: a fresh capture-state (no ledger), five chat-only turns, one session, each turn its own `opencode run` process.

```
16:31:58.436 | opencode-capture | sweep | OK | 0.4s | listed=1 scanned=0 staged=0 too_short=0 before=1 ... cold=True
16:32:00.808 | opencode-capture | turn | OK | 0.0s | before first sweep
16:32:03.648 | opencode-capture | turn | OK | 0.0s | before first sweep
16:32:07.493 | opencode-capture | turn | OK | 0.0s | before first sweep
16:32:10.687 | opencode-capture | turn | OK | 0.0s | before first sweep
16:32:16.603 | opencode-capture | turn | OK | 0.0s | before first sweep
```

Ledger after the run: one record, note `started before the first sweep`, `staged: false`; no stub, no copy (from the author's private test records, not included).
Cause: turn 1's first `session.context` builds the session text with source `startup` (one message), so the bridge runs the start sweep BEFORE the turn ends; with no ledger the sweep is a cold start, and `session list` already lists the live chat, so the cold sweep stamps it as from before the update.
Every later turn capture then reads that record and answers `before first sweep`, for good.
Expected (capture.py docstring, "THE SCAR-TISSUE RULE"): when the first run is triggered by a live chat's own turn, that chat is recorded `first chat` and staged from its next turn on.
In OpenCode a session start always precedes the first turn end of the same chat, so that exception cannot fire through the normal road; the member's first OpenCode chat after install is lost.
The same happened in every other run that started with a fresh capture-state (cp3, compaction): `cold=True`, the live chat `before first sweep`.
Not fixed here (src/ is out of scope for this job).

## Item 20: CP2, the second chat is captured; the capture survives the process exit (measured)

Run `cp2b-second-chat`: the SAME capture-state (the ledger from item 19 exists), a new five-turn chat.

```
16:32:57.265 | opencode-capture | sweep | OK | 0.5s | listed=2 scanned=2 staged=0 too_short=1 before=1 unchanged=0 ... cold=False
16:33:00.121 | opencode-capture | turn | OK | 0.2s | too short
16:33:03.003 | opencode-capture | turn | OK | 0.2s | too short
16:33:06.461 | opencode-chatcache | write | OK | 0.0s | turns=6 escaped=0 reader_altered=0
16:33:06.463 | opencode-capture | turn | OK | 0.2s | staged
16:33:09.416 | opencode-chatcache | write | OK | 0.0s | turns=8 escaped=0 reader_altered=0
16:33:09.419 | opencode-capture | turn | OK | 0.2s | staged
16:33:12.550 | opencode-chatcache | write | OK | 0.0s | turns=10 escaped=0 reader_altered=0
16:33:12.552 | opencode-capture | turn | OK | 0.3s | staged
```

Every turn was staged or judged by its OWN turn-end trigger (reason `turn`): turns 1-2 `too short` (2 and 4 records), turns 3-5 `staged` (6, 8, 10 records; the floor is 5).
The sweep ran once, at turn 1's start, and judged this chat `too short` (1 record) and item 19's chat `before`.
Turns 2-5 are resumes (`messagesLength` 3, 5, 7, 9 at the first `context` call), their session text is built with source `resume`, and a resume never sweeps (`context.NO_SWEEP_SOURCES`); so in headless use there is NO next-start sweep inside a chat, and nothing was left for one: no turn trigger was missed.
Final state (the final re-read after settling): `pending/opencode-<id>.md`, `data/opencode-chats/<id>.jsonl`, ledger `{first_run, refused, sessions, version}` with this chat `note: staged`, `staged: true`, `turns: 10`, `stub: opencode-<id>.md`.
Stub header, contract order with the pointer last:

```
<!-- staged by opencode-transcripts | reason=turn | session=<id> | 2026-11-11T16:33:12.551246+00:00 | 10 turns | transcript=<capture-state>/data/opencode-chats/<id>.jsonl -->
```

`transcript_filter.py <copy> --stats`: `engine: claude`, `turns_kept: {member: 5, glitch: 5, total: 10}`, `dropped_total: 0`; the export holds 5 user and 5 assistant messages.
**Capture survives the headless exit: yes.** `location.shutdown` fires about 20-30 ms after `session.execution.succeeded`, and the bridge child keeps running after OpenCode is gone: turn 5's process wrote its last log line at 16:33:12.280Z and closed its output by 12.309, while the capture read the chat at 12.306 and wrote the copy at 12.550 and the stub at 12.551 (`timing_utc` in the evidence).
The child is spawned detached (its own process group, `bridge-client.ts`), so it is not killed with OpenCode; this was not tested with a slow export, a closed terminal or on Windows.
Inferred, not shown: the session text grew from 21,236 to 21,298 chars at turn 4, the size of the one-line capture notice, so the chat was told about its own capture mid-chat.

## Item 21: CP3, the per-message text reaches both model calls of a turn (measured)

Sentinel choice (from the author's private test records, not included): the engine's prompt hook returns text only for the bash-card phrases, a read yes/no, or watched mail, so the run seeded ONE fake arrival into the TEMP watch folder through `watch_state.record_arrivals`; the real bridge `prompt` then answered 521 chars starting `## Watched mail`, and nothing on the next prompt (the arrival was claimed).
Sentinels: `## Watched mail` (per-message), `You were already told this earlier in this turn` (`REPEAT_FRAMING`), `## Who you are working with (Glitch identity + memory` and `## Claude Code hand-off` (session text).
Run `cp3-two-calls`, turn 1 forced one `read` of `AGENTS.md` (`tool.execute.before read`, `tool.execute.after read completed`), turn 2 was a quiet prompt:

```
16:34:01.789 | opencode-prompt | prompt | OK | 0.1s | 521 chars
{"ts":"16:34:02.237Z","messagesLength":1,"systemLength":5,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off","## Watched mail"]}
{"ts":"16:34:04.727Z","messagesLength":3,"systemLength":5,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off","## Watched mail","You were already told this earlier in this turn"]}
16:34:07.699 | opencode-prompt | prompt | SKIP | 0.1s | 0 chars
{"ts":"16:34:07.950Z","messagesLength":5,"systemLength":5,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off"]}
```

Both `session.context` calls of turn 1 carry the per-message text; the framing sentence is on the second only; the quiet turn's call carries neither (the stash is dropped at the next prompt).
The recorder saw `systemLength` 5 (the host's 4 plus the plugin's one part) both before and after its own hook, confirming the plugin ran first and pushes one joined part; `optionsSentinels` is empty, as expected for `openai` (the `claudeCodeInstructions` channel exists only for `claude-agent`).
In every run the session text carried `## Claude Code hand-off` on EVERY turn, because each headless turn is a new process whose first build is `resume`, and `wants_handoff` is true for resume: the "tell the member once" line is re-sent every turn (the replies here were single words, so whether a model repeats the sentence is unmeasured).

## Item 22: compaction drives capture `compact` and a `compact` rebuild, but the first call of the turn gets a `startup` build (measured; DEFECT in src/)

First turn of a new chat (`cp-compaction`): NO compaction ran (no `session.compaction.*` event); a turn with no history has nothing to compact, which narrows item 14b to "every turn after the first".
Second turn of that chat, continued with `--session-id` in a fresh capture-state (`cp-compaction-resume`), 2 of the 2 runs allowed:

```
{"ts":"16:35:09.016Z","type":"session.compaction.started","dataKeys":["sessionID","reason"]}
{"ts":"16:35:14.313Z","type":"session.compaction.ended","dataKeys":["sessionID","reason","model","text","cost","tokens"]}
16:35:14.380 | opencode-capture | compact | OK | 0.0s | cold start
16:35:14.920 | opencode-capture | sweep | OK | 0.5s | listed=4 scanned=4 staged=0 too_short=1 before=3 ... cold=False
16:35:14.930 | opencode-session | startup | OK | 0.6s | 21057 chars
{"ts":"16:35:14.940Z","hook":"session.context","messagesLength":1,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off"]}
16:35:15.135 | opencode-session | startup | OK | 0.2s | 20685 chars
{"ts":"16:35:21.077Z","type":"session.execution.succeeded"}
16:35:21.356 | opencode-capture | turn | OK | 0.2s | too short
```

The compaction handler ran: capture with reason `compact` (here a cold start, so the live-reason `first chat` road DID fire, the one item 19 says the normal road never reaches), then a session rebuild without a sweep and without the hand-off (20,685 chars), which is the `compact` build.
(The bridge logs every session build under the trigger `startup`; the source is told apart by the sweep line and the hand-off.)
But the order is wrong for the turn's own model call: `onCompaction` runs only after the AWAITED compact capture (a bridge spawn, about 70 ms), while the host goes straight on to the model call; the `context` hook got there first, created the session entry from `sourceForFirstContext`, and after a compaction `messagesLength` is 1 (summary plus prompt), so the first build was `startup`: it ran a mid-chat sweep, appended the hand-off, and is what the call at 16:35:14.940 carried.
The `compact` text arrived after that call and reaches only later calls of the same process (none here: one call).
Expected (setup.ts, `compactedEarly`): a compaction that ends before the session's first `context` call makes the first build source `compact`.
Also consequent: with the keeper directive on (not suppressed in these runs), a `startup` build counts a keeper miss where `compact` would not (`counts_miss`).
The compaction's own model call fired no `session.context` (as item 14c).

## Item 23: run ledger for items 18-22

15 headless `opencode run` invocations, all `openai/gpt-5.6-luna` (one more than the planned 14: the compaction needed a second turn, item 22): model-check 1, cp2 5, cp2b 5, cp3 2 (3 model calls: the read turn made two), compaction 1 + 1 (plus OpenCode's own compaction call).
No-model calls: the real bridge `prompt` twice (preflight), `transcript_filter.py --stats` per analysed copy, `opencode session export --standalone` once per analysed chat (message types and count only, a fresh empty config folder, our own sessions only), the engine's `watch_state.record_arrivals` into temp twice.
Not read at any point: anything under the login folder's `data/`, any `opencode.log`, any environment value.

## Items 24-27: real-run re-verification after the D1 and D2 fixes (measured 2026-11-11, from commit c80ad49)

Same harness and safety settings as items 18-23: `openai/gpt-5.6-luna` on the member's ChatGPT sign-in, the Glitch plugin first and the probe recorder second, the work folder set to the Glitch root (`--allow-workdir-in-brain`), OpenCode `permissions` deny-all, a fresh test capture-state per job, `GLITCH_OPENCODE_NO_KEEPER=1`, `--settle` 5 s.
Orchestration: the author's private test harness (not included): a chat of N one-turn runs with the ledger read after EVERY turn, one forced-compaction turn continuing a chat with a fresh capture-state (as item 22), and a listing of the `session list` created/updated of our chats only.
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.
Every one of the 7 run logs ends `BRAIN-SNAPSHOT: clean`, all three `GUARDED` paths `unchanged`, both `STAT-WATCH` roots `0 changed`, `login-root-new-data-files=0`.

## Item 24: D1 is fixed: the member's first chat after install is captured (measured)

Run `d1-first-chat` (`t1`..`t5`): a fresh capture-state with no ledger, one chat, five chat-only turns, each its own `opencode run` (and its own harness invocation, so each turn's hook log and the ledger after it are separate).
Turn 1, the session start then the turn end:

```
16:50:56.683908 | opencode-capture | sweep | OK | 0.3s | listed=5 scanned=0 staged=0 too_short=0 before=4 unchanged=0 deferred=0 outside=0 pruned=0 refused=0 copy_failed=0 unreachable=False cold=True first_chat=True
16:50:56.702579 | opencode-session | startup | OK | 0.4s | 21057 chars
16:50:59.141856 | opencode-capture | turn | OK | 0.2s | too short
```

The cold sweep marked the 4 OTHER listed chats (earlier runs on this login) `started before the first sweep` and recorded the live one as the first chat (`first_chat=True`, `before=4` of `listed=5`; item 19 had `before=1` for the live chat itself).
Per turn, every outcome from that turn's OWN turn-end trigger (reason `turn`; `session.execution.succeeded` 0.28-0.31 s before each line):

| turn | hook-log outcome | ledger note for this chat after the turn | turns | files |
|---|---|---|---|---|
| 1 | `turn ... too short` | `too short to keep` | 2 | ledger only |
| 2 | `turn ... too short` | `too short to keep` | 4 | ledger only |
| 3 | `chatcache write turns=6` + `turn ... staged` | `staged` | 6 | stub + copy |
| 4 | `chatcache write turns=8` + `turn ... staged` | `staged` | 8 | stub + copy |
| 5 | `chatcache write turns=10` + `turn ... staged` | `staged` | 10 | stub + copy |

Note on the expectation "after turn 1 the ledger shows `first chat`": the `first chat` note is written by the start sweep and lives about 2.4 s, until turn 1's own end capture rewrites the record as `too short to keep` (2 records); the ledger read after turn 1 therefore shows `too short to keep`, and the sweep line is the evidence of `first chat`.
What matters for the member holds: the chat is never `started before the first sweep` and is staged from turn 3 on.
Final state: `pending/opencode-<id>.md`, `data/opencode-chats/<id>.jsonl`, ledger keys `{first_run, refused, sessions, version}`, this chat `note: staged`, `staged: true`, `turns: 10`; stub header in contract order, pointer last:

```
<!-- staged by opencode-transcripts | reason=turn | session=<id> | 2026-11-11T16:51:33.767520+00:00 | 10 turns | transcript=<capture-state>/data/opencode-chats/<id>.jsonl -->
```

`transcript_filter.py <copy> --stats`: exit 0, `engine: claude`, `turns_kept: {member: 5, glitch: 5, total: 10}`, `dropped_total: 0`; the export holds 15 messages: 5 user, 5 assistant, 5 `idle`.
Second chat, same capture-state (`d1-second-chat-t1`, one turn), judged normally:

```
16:52:01.185199 | opencode-capture | sweep | OK | 0.8s | listed=6 scanned=6 staged=0 too_short=1 before=4 unchanged=1 deferred=0 outside=0 pruned=0 refused=0 copy_failed=0 unreachable=False cold=False first_chat=False
16:52:03.611658 | opencode-capture | turn | OK | 0.3s | too short
```

Ledger after it: the new chat `too short to keep`, 2 records (not `before`); the first chat still `staged`, 10 turns, now with `listed_updated` beside its marker (the sweep read it once, found it unchanged and recorded the list's `updated`, as capture.py's "THE UPDATED HINT" says for a value never recorded).
Pass.

## Item 25: D2 is NOT fixed in a real run: the first model call after a compaction still gets a `startup` build (measured; DEFECT in src/, not fixed here)

Run `d2-compaction-resume`: the second chat of item 24 (one earlier turn) continued with `--extra-config {"compaction": {"auto": true, "buffer": "99%"}}`, a fresh capture-state, one turn.
Probe (recorder second) and bridge log merged:

```
{"ts":"16:52:16.188Z","type":"session.compaction.started","dataKeys":["sessionID","reason"]}
{"ts":"16:52:21.266Z","type":"session.compaction.ended","dataKeys":["sessionID","reason","model","text","cost","tokens"]}
16:52:21.337730 | opencode-capture | compact | OK | 0.0s | cold start
16:52:21.345661 | opencode-session | startup | WARN | 0.0s | GLITCH_OPENCODE_NO_KEEPER=1 (TEST-ONLY): keeper directive suppressed
16:52:21.660811 | opencode-chatsource | list | ok | 0.3s | 1582 bytes
16:52:21.871827 | opencode-capture | sweep | OK | 0.5s | listed=6 scanned=6 staged=0 too_short=1 before=5 unchanged=0 ... cold=False first_chat=False
16:52:21.884302 | opencode-session | startup | OK | 0.6s | 21057 chars
{"ts":"16:52:21.894Z","hook":"session.context","messagesLength":1,"messageRoles":["user"],"firstForSession":true,"systemLength":5,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off"]}
16:52:21.962979 | opencode-session | startup | WARN | 0.0s | GLITCH_OPENCODE_NO_KEEPER=1 (TEST-ONLY): keeper directive suppressed
16:52:22.099024 | opencode-session | startup | OK | 0.2s | 20686 chars
{"ts":"16:52:27.954Z","type":"session.execution.succeeded"}
16:52:28.267252 | opencode-capture | turn | OK | 0.2s | too short
```

Observed: TWO session builds for one compaction; the first ran a sweep and carried the hand-off (21,057 chars, the `startup` build), and it is the build the turn's only model call carried (16:52:21.894, `## Claude Code hand-off` present); the second, without sweep or hand-off (20,686 chars, the `compact` build), finished after that call and reached no call.
Expected (the D2 fix): one build, source `compact`, no sweep, no hand-off, carried by the first call after `session.compaction.ended`.
This is the same sequence as item 22, so the fix did not change the real-host behaviour.
What the logs allow, inferred, not shown: `noteCompaction` in setup.ts only makes the first build `compact` when the compaction event reaches the plugin BEFORE the session's first `context` call creates its entry; the two-build shape (startup first, then a compact rebuild from `pendingCompact`) is exactly what the code does when the entry already exists at event receipt, or when `noteCompaction` never ran.
The bridge spawns for the compact capture and the startup build both began at about 16:52:21.27 (each line minus its ~70 ms Python start-up), within a few ms of the probe's own receipt of the event, so the host appears to call the next `context` hook at the same moment as (or before) it delivers `session.compaction.ended` to plugin subscribers: an event-stream signal cannot be relied on to precede the next model call.
Not measured: the order, inside the Glitch plugin, of its event receipt and its `context` entry (neither is timestamped by the plugin; the recorder is second, so its `context` line is logged only after the Glitch build).
A diagnostic that would settle it without a fix: one more forced-compaction turn with the recorder registered FIRST (its `context` entry and its event receipt are then both timestamped before the Glitch plugin acts).
Also seen: after this compaction the call's `messages` is length 1, role `user` only (the new prompt), as item 22.
Sub-run stopped here (no further compaction run).

## Item 26: `session list` `updated` advances with the last message, and a capture's read does not move it (measured)

`opencode session list --standalone --format json`, `PWD` and cwd the Glitch folder, the same throwaway login env, a fresh empty config folder; row keys `{created, directory, id, projectId, title, updated}`, epoch milliseconds (titles not read).

| chat | list taken | created (UTC) | updated (UTC) | its last turn's `session.execution.succeeded` |
|---|---|---|---|---|
| first chat (5 turns) | 16:51:52, after turn 5 | 16:50:55.655 | 16:51:33.453 | 16:51:33.458 |
| first chat | after the d2 run (its turn ended 16:52:27.954) | 16:50:55.655 | 16:51:33.453 (same) | (no new message) |
| second chat (2 turns, compaction on turn 2) | after the d2 run | 16:51:59.499 | 16:52:27.949 | 16:52:27.954 (turn 1 ended 16:52:03.226) |

`updated` is the time of the chat's last message write (about 5 ms before `session.execution.succeeded`) and advanced from the second chat's turn 1 to its turn 2; it did not move for the first chat between the two lists, although the sweep (16:52:01) and the capture exported it in between: reading a chat does not touch `updated`.
Unmeasured: whether a compaction alone (with no new turn) moves `updated`, and the per-turn value inside one chat (one list per chat end only).

## Item 27: run ledger for items 24-26

7 headless `opencode run` invocations, all `openai/gpt-5.6-luna` (budget 9): d1 first chat 5, d1 second chat 1, d2 compaction resume 1 (plus OpenCode's own compaction call).
No-model calls: `transcript_filter.py --stats` once, `opencode session export --standalone` twice (message types and count only), `opencode session list --standalone --format json` twice.
Not read at any point: anything under the login folder's `data/`, any `opencode.log`, the `opencode.db`, any environment value.
Temp roots removed after the job (`glitch-e2e-*`); the login root kept; no `opencode serve` was started (the one `opencode serve --service` running since 09:26 is not this job's and was left alone).

## Item 28: D2 redone: after a compaction the first model call gets a `resume` build, then the `compact` rebuild follows (measured)

The redone fix is uncommitted work in `src/plugin/setup.ts` and `translate.ts` on top of c80ad49.
At a session's first `context` call it classes the chat `resume` when `messages.length > 1`, or when `session.get` carries a non-null `outcome`.
A compaction event that arrives in time still wins with `compact`.
Same settings as items 24-27, 2 of the 3 allowed model runs: a NEW chat's first turn (`d2v2-turn1-t1`, deny-all, fresh capture-state), then its second turn with `{"compaction": {"auto": true, "buffer": "99%"}}` in a fresh capture-state (`d2v2-compaction-resume`).
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.

Turn 1, a brand-new chat, is NOT misclassed as resume: one build with a sweep, the `startup` build:

```
17:01:12.370601 | opencode-capture | sweep | OK | 0.3s | listed=7 scanned=0 ... before=6 ... cold=True first_chat=True
17:01:12.383142 | opencode-session | startup | OK | 0.4s | 21057 chars
17:01:14.795093 | opencode-capture | turn | OK | 0.2s | too short
```

Turn 2, the compaction turn, with the probe and bridge logs merged:

```
{"ts":"17:01:27.960Z","hook":"session.get","keys":["id","projectID","model","cost","tokens","outcome","time","title","location"]}
{"ts":"17:01:27.980Z","type":"session.compaction.started","dataKeys":["sessionID","reason"]}
{"ts":"17:01:35.022Z","type":"session.compaction.ended","dataKeys":["sessionID","reason","model","text","cost","tokens"]}
17:01:35.109318 | opencode-capture | compact | OK | 0.0s | cold start
17:01:35.115909 | opencode-session | startup | WARN | 0.0s | GLITCH_OPENCODE_NO_KEEPER=1 (TEST-ONLY): keeper directive suppressed
17:01:35.268090 | opencode-session | startup | OK | 0.2s | 21239 chars
{"ts":"17:01:35.280Z","hook":"session.context","messagesLength":1,"messageRoles":["user"],"firstForSession":true,"systemLength":5,"systemSentinels":["## Who you are working with (Glitch identity + memory","## Claude Code hand-off"]}
17:01:35.349093 | opencode-session | startup | WARN | 0.0s | GLITCH_OPENCODE_NO_KEEPER=1 (TEST-ONLY): keeper directive suppressed
17:01:35.488527 | opencode-session | startup | OK | 0.2s | 20688 chars
{"ts":"17:01:38.201Z","type":"session.execution.succeeded"}
17:01:38.493164 | opencode-capture | turn | OK | 0.2s | too short
```

Build sequence, in order:
1. The compaction capture (`compact`, cold start), done at 35.109.
2. Build 1 (35.116 to 35.268): 21,239 chars, no `chatsource list` and no `sweep` line, the hand-off present. That is the `resume` build: a resume carries the hand-off and never sweeps, and its size matches the resume builds of item 20 (21,236 and 21,298).
   This is the build the turn's only model call carried (35.280).
3. Build 2 (35.349 to 35.488): 20,688 chars, no sweep, no hand-off. That is the `compact` build, run after the compaction capture had finished. It reached no model call in this one-call process.

The bridge logs every build under the label `startup`; I told the source apart by the sweep line, the hand-off sentinel and the size, as in item 22.
Compared with item 25: there is no start sweep mid-chat any more (item 25 had `listed=6 ... before=5` and a 21,057-char `startup` build at this point).
There are still TWO builds for one compaction, resume then compact. That is the designed order: the compaction event cannot win the race (item 25), so the compact text reaches only later calls of the same process.
The `context` call again reached the Glitch plugin before the compaction event did: had the event come first, the first build would have been `compact`, with no resume build.
Pass against the stated expectation.

The `outcome` key on `session.get` is measured as key presence only, not its value:
- On the brand-new chat's turn 1 (17:01:11.899) the key is ABSENT: keys `{id, projectID, model, cost, tokens, time, location}`, with no `outcome` and no `title`.
- On the resumed chat's turn 2 (17:01:27.960) it is PRESENT.
- The item 25 run also had it present.

Both recorder `get` calls ran at the prompt hook, BEFORE `session.compaction.started` (27.980).
Not measured: a `session.get` made AFTER the compaction. The recorder logs one `get` per session id per process, and the plugin's own lookup is cached from the prompt hook's parent lookup.
So the `resume` decision here most likely rested on that pre-compaction `get`, which is inferred: the plugin does not log which source of its history answer it used.
Not measured either: a `session.get` taken right after a compaction in a process with no earlier `get`.

## Item 29: run ledger for item 28

2 headless `opencode run` invocations, `openai/gpt-5.6-luna` (budget 3, 1 spare not used), plus OpenCode's own compaction call.
Both run logs end `BRAIN-SNAPSHOT: clean`, all three `GUARDED` paths `unchanged`, both `STAT-WATCH` roots `0 changed`, `exit=0`.
No-model calls: `opencode session export --standalone` once (message types and count only).
Temp roots `glitch-e2e-*` removed, the login root kept, no `opencode serve` started; nothing under the login folder's `data/`, no `opencode.log` and no `opencode.db` was read.

## Items 30-34: CP5, install, versions and platforms (measured 2026-11-11)

Same safety settings as items 24-29: `openai/gpt-5.6-luna` on the member's ChatGPT sign-in (`--login-root`, nothing under its `data/` read), the work folder set to the Glitch root (`--allow-workdir-in-brain`), OpenCode `permissions` deny-all (plus one exact-path `read` allow for item 31), a fresh test capture-state per job, `GLITCH_OPENCODE_NO_KEEPER=1`, `--settle` 5 s.
New: the adapter is loaded by the member's own road, not `--plugin-dir`.
The author's private test harness, in its install route, ran `scripts/install.py install --config-dir <the run's own throwaway config dir>` as a subprocess, then appends the probe recorder after it (plugin under test first), and writes what install.py printed plus `install.py status` into the run log.
Self-test 52/52.

## Item 30: install.py against throwaway config folders (measured, no model)

Run from this workspace (`workspaces/opencode-adapter`) with `--config-dir` under the system temp folder, four starting files: a JSONC `opencode.jsonc` with line and block comments and a trailing comma, an `opencode.json` that already names two other plugins (a package and a directory), no file at all, and a file naming the README checkout `~/glitch-opencode-adapter/src/plugin`.
For every one: `install --dry-run` printed the diff and wrote nothing (no backup folder); `install` wrote one backup and the entry; `status` said `installed`; a second `install` said `already installed ... nothing changed` with the file byte-identical (sha256) and still one backup; `uninstall` said `restoring the backup ... byte for byte; verified` and the file's sha256 equals the original (absent stays absent).
Two defects found and fixed in `scripts/install.py`, each with a test that failed first:
1. `STALE_SUFFIX` did not mis-flag this workspace's own path (`is_ours` is checked first), but it MISSED the folder name README step 2 tells a member to use: `.../glitch-opencode-adapter/src/plugin` does not end in `/opencode-adapter/src/plugin` (a hyphen, not a slash, precedes it).
   So a moved member checkout (or a Glitch-workspace install after a README install) was read as an unrelated plugin: `status` said `not-installed` and `install` added a SECOND entry, loading two copies of the plugin.
   The real `~/.config/opencode/opencode.json` on this Mac names exactly such a path (read for this check; nothing written).
   Fix: `STALE_SUFFIXES` holds both names; the entry is now repointed in place (`test_readme_checkout_name_is_recognised_as_stale`; lookalikes such as `my-opencode-adapter` stay unrelated, `test_unrelated_lookalike_paths_are_not_stale`).
2. `uninstall` after a repoint restored the original byte for byte but then exited 1 "did not verify", because the restored file names the older copy and the check demanded `not-installed`.
   Fix: the check is now "this checkout is no longer named", with a note line when the restored file names an older copy (`test_stale_path_repointed_and_uninstall_restores` now asserts the exit code, which it did not before).
Not changed, flagged: with no `--config-dir` and no `OPENCODE_CONFIG_DIR` the script targets the OS default `~/.config/opencode`; README step 5 relies on that, so it is not refused.
Tests prove an explicit `--config-dir` or `OPENCODE_CONFIG_DIR` never touches the default folder, and that the default is chosen only when nothing is named (both against a fake `HOME`).
A module-wide fixture fingerprints the REAL `~/.config/opencode` before and after the whole test module (sha256 of the top-level config files only; names, sizes and mtimes for everything else, so auth, token, credential and database files are never read) and fails on any change.
`tests/py/test_install.py`: 37 passed.

## Item 31: the read guard denies through the installed plugin (measured)

Run `cp5-g1-deny` (one model run): the model was asked to read `<Glitch root>/.claude/data/google-accounts/cp5-canary/token.json`, a login-secret file name that does not exist on this machine (the folder was checked absent before and after).
The permissions allowed `read` for that exact path only, so a failed guard would have read nothing.

```
install.py status: installed
{"tool": "read", "status": "error", "error_head": "**Login-secret file — never read into chat, in any spelling.** Do the task through the proper flow instead: ..."}
17:16:07.747005 | opencode-pre | read | DENY | 0.0s | secret-read
```

The error is the plugin's thrown deny text, not a file-not-found and not OpenCode's `Permission denied`; the model quoted it back.
Shell and write deny targets were not re-run with a model: no safe target exists whose failure could not do damage in the Glitch folder; they are unit-covered by `tests/py/test_pretool.py` (the contract-drift table) and were measured with the probe in items 2-3.
Not every login-file spelling is denied by name: a read of `.claude/data/google/token.json` (no `-accounts`) came back allowed from the bridge `pre` (no model), so that name was not used; the engine's read-guard owns that list.
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.

## Item 32: the post-tool scan was not re-run with a model, and it notes rather than redacts (read from the code)

Two reasons, both from the code, no run made:
1. The Claude guard the bridge delegates to (`post-tool-secret-guard.process`) appends every catch to the REAL `.claude/data/state/secret-catches.jsonl` (`config.STATE_DIR`, which has no environment door), so a real run with a fake key would change the member's state folder and break the `STAT-WATCH 0 changed` rule.
2. The plugin does not redact: `posttool.py` returns a note and `setup.ts` appends it to `result.content` (`appendNoteToResult`), so the model sees the key AND the warning, which is Claude Code's parity (a note via `additionalContext`); item 6h showed redaction is possible on this host, and it is not built.
A check that expects a redaction marker in place of the key would FAIL by design.
Covered without a model by `tests/py/test_posttool.py` (catches, failure contract; its catch diary is moved per test) and `tests/ts` for the append.

## Item 33: session text, per-message text and capture through the installed plugin (measured)

Run `cp5-g3-t1..t3` (three model runs, one chat, each turn its own `opencode run`), a fresh capture-state with one seeded watched-mail arrival in the TEMP watch folder (as item 21):

| turn | `session.context` sentinels | bridge lines | ledger for this chat |
|---|---|---|---|
| 1 | session heading, hand-off, `## Watched mail` (messagesLength 1) | `prompt OK 521 chars`; sweep `cold=True first_chat=True`; startup build 21,057 chars; `turn too short` | `too short to keep`, 2 |
| 2 | session heading, hand-off (messagesLength 3) | `prompt SKIP 0 chars`; resume build 21,232 chars, no sweep; `turn too short` | `too short to keep`, 4 |
| 3 | session heading, hand-off (messagesLength 5) | resume build; `chatcache write turns=6`; `turn staged` | `staged`, 6, stub + copy |

Stub header in contract order, pointer last (`reason=turn`, 6 turns, `transcript=`); `transcript_filter.py --stats` exit 0, `turns_kept {member: 3, glitch: 3, total: 6}`, `dropped_total: 0`.
Compaction was not re-run: item 28 (from the author's private test records, not included) ran on the committed code, and no file under `src/` has changed since.
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.
All four CP5 run logs end `BRAIN-SNAPSHOT: clean`, the three `GUARDED` paths `unchanged`, both `STAT-WATCH` roots `0 changed`, `login-root-new-data-files=0`, `exit=0`.

## Item 34: versions, platforms, and a version mismatch

OpenCode versions tested: one, v2.2.7, the newest published 2.x release (published 2026-11-10, NEWEST_VERSION line above), so there is no second version to test.
Tested 2026-11-11.

| Behaviour | v2.2.7 | Evidence |
|---|---|---|
| Guards deny (read, login-secret name, via installed plugin) | pass | the author's private test records (not included); shell/file tools: items 2-3 (probe) and `tests/py/test_pretool.py` |
| Guards scan (post-tool) | not re-run with a model (writes the real catch diary); notes, does not redact | item 32; `tests/py/test_posttool.py` |
| Session text (identity + hand-off reach every model call) | pass | the author's private test records (not included) |
| Prompt text (per-message, watched mail) | pass | the author's private test records (not included) (turn 1) |
| Capture (first chat after install staged) | pass | the author's private test records (not included) |
| Compaction (capture `compact`, resume then compact build) | pass | the author's private test records (not included) (item 28) |
| Install / uninstall | pass, after the two install.py fixes of item 30 | `tests/py/test_install.py`; item 30 |

| Platform | Result |
|---|---|
| macOS (Darwin 25.6.0, this Mac) | tested |
| Windows | not tested (item 9) |
| Linux | not tested |

Version mismatch, read from the code: `setup.ts` compares `ctx.app.version` with `SUPPORTED_OPENCODE_VERSION` ("2.2.7") only after the Glitch root is found, calls `d.warn` with "the Glitch guards still run, but the hook API may have changed", inside a try that can never stop setup, and goes on to register every hook: a mismatch WARNS and RUNS.
Already tested: `tests/ts/setup.test.ts` "version mismatch warns once and still registers the guards" (versions `2.3.0` and `undefined`), and "no version warning outside Glitch either"; no new test was added.
Not run against a real other version (none exists to download).

## Item 35: run ledger for items 30-34

4 headless `opencode run` invocations, `openai/gpt-5.6-luna` (budget 8, 4 unused): cp5-g1-deny 1, cp5-g3 3.
No-model calls: the bridge `pre` three times on read events (temp state doors), `transcript_filter.py --stats` once, install.py runs against temp config folders.
Temp roots `glitch-e2e-*` and the install walk folders removed; no `glitch-probe-*` root left; the login root kept; nothing under its `data/`, no `opencode.log`, no `opencode.db` read; `~/.config/opencode` not written (its one config file read once for item 30).

## Item 36: CP6, a captured OpenCode chat is read by the digest from its transcript, not the fallback (measured 2026-11-11)

Run `e2e-digest-t1..t5`: the author's private test harness (not included), with the adapter installed through `scripts/install.py` as in item 33; one chat, five chat-only turns, a fresh TEMP capture-state, `GLITCH_OPENCODE_NO_KEEPER=1`, OpenCode `permissions` deny-all.
The member's message of turn n is `Reply with exactly the word E2E-ALPHA-n. Use no tools.` (`d1_prompt`), so the marker `E2E-ALPHA-n` names it.
Capture, per turn as item 24: `too short` (2), `too short` (4), then `staged` at 6, 8 and 10 records; final capture-state `pending/opencode-<id>.md`, `data/opencode-chats/<id>.jsonl`, `state/opencode-staged.json` (this chat `note: staged`, `staged: true`, `turns: 10`); stub header in contract order, `reason=turn`, 10 turns, pointer last; `transcript_filter.py --stats` exit 0, `engine: claude`, `turns_kept {member: 5, glitch: 5, total: 10}`, `dropped_total: 0`; the export 15 messages (5 user, 5 assistant, 5 `idle`).
The cold sweep (`listed=24 ... before=23 cold=True first_chat=True`) marked the 23 other chats on this login `started before the first sweep` and the live one the first chat.

The digest's read, run once by a scratch script (not committed) against that TEMP capture-state, with the engine's own interpreter and temp hook-log, read-grant and watch doors: `headless_digest.read_session(<stub>)` returned source `transcript` (`SOURCE_TRANSCRIPT`, not the `stub` fallback), an empty note, the stub's pointer equal to the copy's path, all five markers present, and the five member messages verbatim and in order as its `MEMBER:` blocks.
`transcript_filter.py <copy>` (the CLI, no `--stats`): exit 0, the five member messages verbatim and in order.
Nothing was claimed or consumed: `read_session` reads only the stub it is handed and the file its pointer names; `digest.py` was not run.

The committed check is `tests/py/test_e2e_digest.py` (3 tests), on a synthetic equivalent built by code through the real modules: a fake `opencode` (the `test_capture.py` pattern) holds the same export shape (user, assistant, idle per turn), `capture.sweep(live_session_id=...)` then `capture.capture_one(..., "turn")` five times (outcomes as measured above), then `read_session` (asserts `SOURCE_TRANSCRIPT`, empty note, the five `MEMBER:` blocks equal the five prompts), the CLI as a subprocess (the same five, `--stats` 5/5/10, 0 dropped), and a negative twin: the copy deleted, `read_session` answers `SOURCE_STUB` with the note "no longer on disk", never silently.
Mutation: the stub's pointer rewritten to a missing file makes the transcript test fail (`assert 'stub' == 'transcript'`, the fallback note as the message); reverted.
Evidence: kept in the author's private test records (not included); the key names, labels and counts it showed are stated in the items above.
Every one of the 5 run logs ends `BRAIN-SNAPSHOT: clean`, the three `GUARDED` paths `unchanged`, both `STAT-WATCH` roots `0 changed`, `login-root-new-data-files=0`, `exit=0`, `install.py status: installed`.
Pass.

## Item 37: run ledger for item 36

5 headless `opencode run` invocations, `openai/gpt-5.6-luna` (budget 6, 1 unused): e2e-digest t1..t5.
No-model calls: `opencode session export --standalone` once (message types only), `transcript_filter.py` once with `--stats` (d1's own) and twice without (the scratch read), `read_session` twice (the scratch read, then again to write the evidence file).
Temp roots `glitch-e2e-*` removed after the read; the login root kept; nothing under its `data/`, no `opencode.log`, no `opencode.db` read; `~/.config/opencode` not touched.
