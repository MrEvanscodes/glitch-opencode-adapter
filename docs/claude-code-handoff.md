# Moving between OpenCode and Claude Code in one Glitch folder

This is the measured report on what an OpenCode session and a Claude Code session share when both are opened in the same Glitch folder, and what they do not.
Every factual claim cites either `docs/api-findings.md item N` (this workspace's measured authority) or an engine file and line (`<file>:<line>`, paths from the Glitch folder).
Adapter files are cited from this workspace (`src/...`).
Line numbers were read from the files on 2026-11-11, against the engine at commit `af1aae2`.

## In short

1. Your memory is shared: the notes, the people and meetings, your tasks list, the project list and the chats Glitch has saved are the same files whichever tool you open.
2. Which project and which task "this window" is on is not shared with OpenCode, because Glitch cannot tell one OpenCode window from another; OpenCode asks you which project you are in rather than guess.
3. An OpenCode chat reaches Claude Code through memory: once it has five or more messages it is saved as you go, and after the memory keeper's next catch-up a Claude Code session in the same folder can find it.
4. Three things need a change to Glitch itself before they can work: OpenCode windows in the task dashboard, OpenCode knowing which window it is (so a task follows the window), and Glitch pushing a message into an OpenCode chat that is already open.

## (a) What is shared across tools

All of these are ordinary files and one database inside the Glitch folder, so any tool started there reads and writes the same copy.
None of them is keyed to a tool or a window.

| What | Where it lives | Evidence |
|---|---|---|
| The vault (USER.md, MEMORY.md, daily logs, project notes) | `glitch-mem/Memory/` | `.claude/scripts/config.py:113`, `.claude/scripts/config.py:118`, `.claude/scripts/config.py:121`, `.claude/scripts/config.py:208` |
| The people and brand spine | `glitch-mem/Memory/people/`, `brands/`, projected into `memory.db` | `.claude/scripts/config.py:214`, `.claude/scripts/config.py:215`, `CLAUDE.md:83` |
| The meetings and actions spine | `glitch-mem/Memory/meetings/`, projected into `memory.db` | `.claude/scripts/config.py:222`, `CLAUDE.md:84` |
| The one database (search index, people, meetings, task lanes) | `.claude/data/memory.db` | `.claude/scripts/config.py:752`, `.claude/scripts/people_db.py:45`, `.claude/scripts/lanes.py:6953` |
| The tasks note | `glitch-mem/Memory/tasks/` | `.claude/scripts/config.py:230` |
| Task lanes (each task, its worktree, its owner) | rows in `memory.db`, written only by `lanes.py` | `.claude/scripts/lanes.py:3`, `.claude/scripts/lanes.py:4` |
| Plans written by `/glitch-plan` | the Glitch folder's own `plans/` | `.claude/skills/glitch-plan/SKILL.md:738`, `.claude/skills/glitch-plan/SKILL.md:746` |
| The project registry | `workspaces/registry.json` | `.claude/scripts/config.py:173` |
| The memory keeper's queue (chats waiting to be digested) | `.claude/data/state/pending/` | `.claude/scripts/config.py:142`, `CLAUDE.md:81` |
| An OpenCode chat after capture | a stub `opencode-<id>.md` in that queue, pointing at a redacted copy `.claude/data/opencode-chats/<id>.jsonl` | `src/bridge/capture.py:18`, `src/bridge/capture.py:299`, `src/bridge/chatcache.py:16`, `src/bridge/chatcache.py:85` |

How the saved chat travels, step by step:

1. OpenCode marks the end of every turn with `session.execution.succeeded` (`docs/api-findings.md item 10`).
2. At that moment the adapter reads the whole chat through OpenCode's own command (`opencode session export --standalone <id>`, `docs/api-findings.md item 11`) and, once the chat holds at least five messages (`src/bridge/capture.py:102`), writes the redacted copy and then the stub (`src/bridge/capture.py:15`).
3. The stub's header carries a pointer in the engine's own spelling (`src/bridge/capture.py:307`, `.claude/scripts/transcript_filter.py:143`), and the digest reads it back the same way it reads a Codex or Cursor stub (`.claude/scripts/transcript_filter.py:148`, `.claude/scripts/headless_digest.py:322`).
4. The memory keeper digests the queue into the daily log, and the dream folds the daily logs into MEMORY.md (`CLAUDE.md:81`).
5. A compaction does not lose anything: the export keeps every original turn after a compaction (`docs/api-findings.md item 14`).

## (b) What is not shared: "this window's" project and task

Claude Code and Codex remember the project and the task per window.
OpenCode cannot, today, and it behaves the way Cursor and Gemini do.

**Why.**
Glitch has one reader for "which window is this", `pane_identity.current_pane()` (`.claude/scripts/pane_identity.py:3`, `.claude/scripts/pane_identity.py:384`).
It knows exactly two ways in: Claude Code's session variable and process number (`.claude/scripts/pane_identity.py:75`, `.claude/scripts/pane_identity.py:76`) and Codex's session variable plus a `codex` process above the command (`.claude/scripts/pane_identity.py:79`, `.claude/scripts/pane_identity.py:401`).
Anything else answers "unknown, not confident" (`.claude/scripts/pane_identity.py:407`, `.claude/scripts/pane_identity.py:408`).
OpenCode gives a shell command no session id: the only variables it adds are `OPENCODE_CONFIG_DIR`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_PRINT_LOGS` and `OPENCODE_TERMINAL` (`docs/api-findings.md item 16`, row 16a).
The engine names no OpenCode marker anywhere: a search of `pane_identity.py`, `transcript_filter.py`, `operations-reference.md` and `CLAUDE.md` for "opencode" finds nothing.

**What the measurement showed.**
The in-OpenCode run was not possible: the free model quota ran out first (`429`, `provider.free-limit`), so no command was run from inside an OpenCode session (`docs/api-findings.md item 16`).
Instead the engine's two read commands were run directly, with the shell environment OpenCode was measured to give a command (the caller's own Claude, Codex and Gemini variables removed) (`docs/api-findings.md item 16`, point 2):

- `pane_identity.current_pane()` answered `{'pane_id': None, 'session_id': None, 'pid': None, 'tool': 'unknown', 'source': 'none', 'confident': False}`.
- `workspaces.py active` answered `scope: "none"`, `pane: null`, `slug: null`, `task: null`, with the "say work on <project>" sentence.
- So: not confident, scope `none`, the machine-wide record not read on this machine, and no task.

**Why scope is `none` and not `machine`.**
The engine reads the machine-wide record only when no window on the machine has ever claimed a project (`.claude/scripts/workspaces.py:1523`, `.claude/scripts/workspaces.py:1550`).
On this machine pane records exist (`.claude/data/state/pane-workspaces.json` is present), so an unidentified window is asked instead (`.claude/scripts/workspaces.py:1520`, `.claude/scripts/workspaces.py:1551`).
On a machine where only OpenCode is ever used, the same code would take the `machine` leg (`.claude/scripts/workspaces.py:1553`); that case was not measured.

**What that means for you in OpenCode** (read from the engine's code, not run inside OpenCode):

1. When Glitch needs your project, OpenCode asks: "I don't know which project this pane is working in, so I'm not going to guess at it." (`.claude/scripts/workspaces.py:1141`).
2. Saying "work on <project>" there writes only the machine-wide record (`.claude/scripts/workspaces.py:1088`); this window's own copy is skipped because the window cannot name itself (`.claude/scripts/workspaces.py:1249`, `.claude/scripts/workspaces.py:1250`).
   On a machine with other windows' records, the next read from OpenCode still takes the `none` leg (`.claude/scripts/workspaces.py:1551`), so expect to be asked again (inferred from the code; not run).
3. No task is ever "this window's" in OpenCode: the task lookup needs a confident window id and answers nothing without one (`.claude/scripts/workspaces.py:1476`, `.claude/scripts/workspaces.py:1477`, `.claude/scripts/workspaces.py:1577`).
4. Picking up another window's task from OpenCode is refused before anything moves, with the engine's own words naming Claude Code or Codex as the place to do it (`.claude/scripts/lanes.py:1973`, `.claude/scripts/lanes.py:3273`).
5. A task started from OpenCode is recorded with no owning window, which the engine treats as a task no open window holds (`.claude/scripts/lanes.py:3136`, `.claude/scripts/lanes.py:3140`, `.claude/scripts/lanes.py:3141`); read from the code, not run.

This is the same behaviour the engine documents for Cursor, Gemini and Antigravity (`operations-reference.md:2179`, `operations-reference.md:2181`).

## (c) Parity rows 18 and 19

**Row 18: a new conversation keeps the open task (#320).**
Not available in OpenCode.
Under Codex, the session-start hook hands the window's task to a new conversation in the same window (`.claude/hooks/session-start-context-codex.py:131`, `.claude/hooks/session-start-context-codex.py:399`), through `workspaces.rebind_after_codex_new` (`.claude/scripts/workspaces.py:1400`), keyed on a Codex window identity (`.claude/scripts/workspaces.py:1418`, `.claude/scripts/workspaces.py:1419`).
The adapter calls only `build_context` from that hook (`src/bridge/context.py:352`), not the hand-over, because there is no OpenCode window identity to key it on (`docs/api-findings.md item 16`).
The plugin does see a `sessionID` on every hook (`docs/api-findings.md item 2`), so an OpenCode road could be built, but nothing in the engine reads one now (`docs/api-findings.md item 16`).
Measured basis: OpenCode's shell env carries no session id (`docs/api-findings.md item 16`, row 16a); the in-OpenCode confirmation run did not happen (quota).

**Row 19: the builder-pane start role (`GLITCH_START`).**
Partly available, not measured end to end.
The switch is an environment variable read once per window (`.claude/scripts/start_role.py:9`, `.claude/scripts/start_role.py:37`), and the Codex-shaped session start the adapter calls already honours it (`.claude/hooks/session-start-context-codex.py:85`, `.claude/hooks/session-start-context-codex.py:96`).
The plugin starts the bridge with its own process environment (`src/plugin/bridge-client.ts:278`).
Under `opencode --standalone` that is the launching shell's environment: variables set by the launcher reached both the plugin and the shell (measured), so `GLITCH_START=builder opencode --standalone` would reach the bridge (inferred, not run) (`docs/api-findings.md item 16`).
Without `--standalone` the plugin runs inside OpenCode's shared background service, whose environment is fixed when that service starts, so a per-window `GLITCH_START` would not reach it (unmeasured: the real service was not touched) (`docs/api-findings.md item 16`).
What a builder pane would still not get in OpenCode: per-window task lanes, for the reason in (b).

## (d) What is possible, and what is not without an engine change

**Possible today.**

1. You chat in OpenCode in your Glitch folder; once the chat has five or more messages it is saved into the keeper's queue as you go (`src/bridge/capture.py:102`, `docs/api-findings.md item 10`).
2. You open Claude Code in the same folder with `claude`.
   After the memory keeper's digest has run, the chat is in the daily log and in memory search, the same as a Claude Code chat (`CLAUDE.md:81`).
   Until the keeper has run, it is waiting in the queue, not yet in memory.
3. The exact words are on this machine in the redacted copy (`src/bridge/chatcache.py:16`), and because that copy is Claude-shaped the engine's existing chat reader can read it (`src/bridge/chatcache.py:4`, `.claude/scripts/transcript_filter.py:33`).
4. OpenCode tells you this once at the start of a chat, in one sentence, because a plugin has no toast or notice of its own (`src/bridge/context.py:110`, `src/bridge/context.py:121`, `docs/api-findings.md item 15`).
5. Your profile, memory, people, meetings, tasks note, plans and project list are the same in both tools, per (a).

**Not possible without an engine change** (each proposed in `docs/engine-handoff.md`):

1. OpenCode windows in the task dashboard: the dashboard's embedded pane launches Claude Code only (`.claude/scripts/pane_embed.py:236`), and a task can only be shown as a window's when its owner is a confident window id (`.claude/scripts/lanes.py:3141`).
2. A window identity for OpenCode, so a project and a task follow the OpenCode window, and #320 and builder-pane task lanes work there: `pane_identity` knows only Claude Code and Codex (`.claude/scripts/pane_identity.py:115`, `.claude/scripts/pane_identity.py:384`).
3. Pushing a message into an OpenCode chat that is already open, from outside it: the plugin has no visible-notice call (`docs/api-findings.md item 15`), and the engine documents no inbound push for any tool (`operations-reference.md:2177`, `operations-reference.md:2181`).
4. Telling you where your OpenCode chats are when you ask "what did I say in that chat": the engine's "Reading a past chat" names Claude Code and Codex folders only (`operations-reference.md:117`, `operations-reference.md:121`).

## For the Glitch team

- The Claude-Code-only extras the adapter names to the member are the armed task link, the status-line mail count, the `/clear` pane hand-over, the pushed daily brief and the SessionEnd persona sync (`src/bridge/context.py:115`, `docs/parity-checklist.md` "Claude-only by design").
- A compaction rebuild and a date-change refresh re-issue the keeper directive without counting a miss (`src/bridge/context.py:18`).
- The export reader treats an unknown id as gone only on OpenCode's own `Session not found: <id>` answer with exit 1, measured on 2.2.7 (`src/bridge/chatsource.py:124`).

## What this report could not verify

- Nothing here was run from inside an OpenCode session in the Glitch folder: the free model quota ran out before that run (`docs/api-findings.md item 16`).
  The pane and task answers come from the engine's read commands run with OpenCode's measured shell environment.
- The `machine` leg (a machine where no window has ever claimed a project) was not measured.
- That a Claude Code session finds a specific OpenCode chat after a real keeper digest was not walked live for this report; the chain is read from the code and the cited measurements.
- `GLITCH_START` under `--standalone` is inferred, and under the background service it is unmeasured.
- Points (b)2, (b)5 and the "asked again" behaviour are read from engine code, not run.
- Windows and Linux: nothing ran there (`docs/api-findings.md item 9`).
