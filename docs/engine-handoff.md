# Proposed engine changes for OpenCode (for the Glitch team)

This workspace cannot change the engine, so these are proposals, each with the engine file it touches and a one-line why.
Facts cite `docs/api-findings.md item N` (measured on OpenCode v2.2.7, macOS) or `<file>:<line>` (paths from the Glitch folder, read on 2026-11-11 at engine commit `af1aae2`).
Today the engine names OpenCode nowhere: a search of `.claude/scripts/*.py`, `operations-reference.md` and `CLAUDE.md` for "opencode" finds nothing.
Order is by value to the member: the first three remove a local workaround or a member-visible gap; the rest are delivery.

## 1. A row for OpenCode in "What works in which tool"

- **File:** `operations-reference.md` (the table at `operations-reference.md:2175`, heading at `operations-reference.md:2168`).
- **Why:** the table is where a member learns that per-window project and task differ by tool, and OpenCode behaves like Cursor there but is not listed (`operations-reference.md:2181`).
- **Proposed row (until item 3 lands):**
  - Which project and task: **Machine-wide**, the same three sentences as the Cursor row, because OpenCode gives a command no session id (`docs/api-findings.md item 16`).
  - Where watched mail reaches you: the line at the start of each chat and the report on your next message (the adapter runs the per-message context, `docs/HOW-IT-WORKS.md` "Memory-in"); no status line count; no notification from the plugin, because a plugin has no notice call (`docs/api-findings.md item 15`).
  - Can a scheduled check wake an open session: **No**, no inbound push is exposed to a plugin (`docs/api-findings.md item 15`).
  - Read from: the measured findings in this workspace, since OpenCode's own docs were not the source.

## 2. An OpenCode adapter in `transcript_filter.py`

- **File:** `.claude/scripts/transcript_filter.py` (the registry at `.claude/scripts/transcript_filter.py:789`, the engine names at `.claude/scripts/transcript_filter.py:79`, the path fallback at `.claude/scripts/transcript_filter.py:828`).
- **Why:** it removes the need for the adapter's second, redacted local copy of every chat (`src/bridge/chatcache.py:1`, `docs/parity-checklist.md` row 29 and ruling 5); the module is built so a new engine is "a third adapter class and one registry line" (`.claude/scripts/transcript_filter.py:39`, `.claude/scripts/transcript_filter.py:40`).
- **The catch to design for:** every existing adapter reads a JSONL file on disk, while OpenCode keeps chats in a SQLite store that also holds account and credential tables, so a store reader must never be pointed at a real data folder (`docs/api-findings.md item 11`).
  So the OpenCode adapter should read the JSON that `opencode session export` prints (section 9), not the database.
- **Shape to read** (`docs/api-findings.md item 11`): `{info, messages[]}`; member text is `messages[type=user].text`; assistant text is `messages[type=assistant].content[type=text].text`; drop `idle`, `compaction`, tool parts and reasoning, counted, as the adapter's reader does (`src/bridge/chatsource.py:19`).
- **The pointer:** `header_pointer` / `pointer_from_stub` (`.claude/scripts/transcript_filter.py:143`, `.claude/scripts/transcript_filter.py:148`) would need a non-file form, for example `opencode:<session id>`, resolved by running the export.
- **Until then:** the adapter's Claude-shaped copy at `.claude/data/opencode-chats/<id>.jsonl` works with the engine unchanged (`src/bridge/chatcache.py:16`).

## 3. An OpenCode entry in `pane_identity.py`

- **File:** `.claude/scripts/pane_identity.py` (`which_tool` at `.claude/scripts/pane_identity.py:115`, `current_pane` at `.claude/scripts/pane_identity.py:384`, the unused hook-written `record` road at `.claude/scripts/pane_identity.py:52`).
- **Why:** it is the one reader of "which window is this" (`.claude/scripts/pane_identity.py:3`), and without an OpenCode answer an OpenCode session is never confident, so it is always asked for its project and never holds a task (`.claude/scripts/workspaces.py:1536`, `.claude/scripts/workspaces.py:1476`).
- **What OpenCode exposes today (measured):** no session id and no agent pid in a shell command's environment, only `OPENCODE_CONFIG_DIR`, `OPENCODE_DISABLE_AUTOUPDATE`, `OPENCODE_PRINT_LOGS`, `OPENCODE_TERMINAL` (`docs/api-findings.md item 16`, row 16a).
  The plugin, however, sees `sessionID` on every hook (`docs/api-findings.md item 2`) and sees the shell's `env` in `shell.create.before` before the shell starts (`docs/api-findings.md item 2`).
- **What OpenCode would need to expose, best first:**
  1. Upstream: OpenCode putting the root session id into every shell command's environment, as Codex does (`.claude/scripts/pane_identity.py:25`, `.claude/scripts/pane_identity.py:27`).
  2. Adapter-side, unmeasured: the plugin adding the session id to `env` in `shell.create.before`; whether a change there reaches the shell was not tested.
  3. Either way the "both halves" rule needs an agent process too (`.claude/scripts/pane_identity.py:387`); the name of OpenCode's process in a command's ancestry was not measured, and without `--standalone` shells may run under one shared background service for every window (`docs/api-findings.md item 1`, row 1g), which is the Codex desktop app's shared-process case (`.claude/scripts/pane_identity.py:146`).
- **A sub-agent must keep the parent's window:** OpenCode gives a child session its own id with a `parentID` (`docs/api-findings.md item 17`), so the id exported to a child's shell must be the root session's, as Codex's is (`.claude/scripts/pane_identity.py:30`).

### 3a. What that entry would also unlock

- **#320 (a new conversation keeps the task):** today keyed on a Codex window (`.claude/scripts/workspaces.py:1400`, `.claude/hooks/session-start-context-codex.py:399`); with an OpenCode pane id the adapter's session start could run the same hand-over.
  OpenCode's own "new conversation in the same window" behaviour was not measured.
- **Task lanes in OpenCode:** a task's owner is only recorded for a confident window (`.claude/scripts/lanes.py:3140`, `.claude/scripts/lanes.py:3141`), and a pick-up is refused without one (`.claude/scripts/lanes.py:3273`).
- **Builder panes with their own task lanes:** the start role already reaches the bridge under `--standalone` (inferred, `docs/api-findings.md item 16`); what is missing is the window identity the lanes need (`operations-reference.md:788`).
- **OpenCode panes in the task dashboard:** the embedded pane launches `claude` only (`.claude/scripts/pane_embed.py:236`); an `opencode` pane there is a separate, larger change.
- **The table row in item 1** would then say **Per-pane** for OpenCode.

## 4. "Reading a past chat": OpenCode's place

- **File:** `operations-reference.md` (`### Reading a past chat` at `operations-reference.md:108`; Claude Code and Codex bullets at `operations-reference.md:117` and `operations-reference.md:121`).
- **Why:** the trigger-map line "it was in the chat I cleared" sends the model there (`CLAUDE.md:66`), and it names no OpenCode location.
- **Proposed bullet:** OpenCode keeps its chats in its own database, not as files, so read one with `opencode session export --standalone <id>` and list this project's chats with `opencode session list --standalone --format json` (`docs/api-findings.md item 11`); while the adapter's copy exists, `.claude/data/opencode-chats/<id>.jsonl` reads with the existing `transcript_filter.py` lines (`src/bridge/chatcache.py:16`, `operations-reference.md:128`).
- **Not measured:** the real default location of OpenCode's data folder; the probes used a throwaway `XDG_DATA_HOME`, where the store was `opencode/opencode.db` (`docs/api-findings.md item 11`).
  The doc should point at the command, never at the database, because the database holds credential tables (`docs/api-findings.md item 11`).

## 5. `/update` delivery of the plugin wiring

- **Files:** `.claude/scripts/update.py` (`.claude/scripts/update.py:3`) and a new tracked folder for the plugin, beside the tracked `.codex/hooks.json` and `.cursor/hooks.json` (both in `git ls-files` at the Glitch root).
- **Why:** today the member runs this workspace's `scripts/install.py` once, which adds an absolute plugin directory to their own OpenCode config (`scripts/install.py:9`, `scripts/install.py:11`), so an engine update cannot refresh the plugin (`docs/parity-checklist.md` row 11).
- **Shape:** ship the plugin and bridge as engine files; OpenCode loads a plain `.ts` directory with no build step and no `node_modules` (`docs/api-findings.md item 1`, rows 1b and 1c), and edits hot-reload (`docs/api-findings.md item 1`).
  The one step that stays outside the engine is the `plugins` entry in the member's own OpenCode config, because OpenCode has no project-level plugin folder that was measured to load (see item 8).

## 6. A `/setup` walk

- **File:** `.claude/skills/setup/SKILL.md`, plus a `## OpenCode` section in `operations-reference.md` beside `### Codex CLI — the daily-driver walk` (`operations-reference.md:2252`) and `## Cursor` (`operations-reference.md:2304`).
- **Why:** Codex and Cursor each have a written first-launch walk; OpenCode has an installer with a dry run, a backup and an uninstall (`scripts/install.py:14`, `scripts/install.py:22`) but no walk a member is led through.
- **The walk:** dry run, show the one-line config change, install on a yes, then one OpenCode chat in the Glitch folder to see the hand-off sentence; and the rule that OpenCode must be started in the Glitch folder, because OpenCode takes its project from `$PWD` (`docs/api-findings.md item 10`).

## 7. A trigger-map line in `CLAUDE.md`

- **File:** `CLAUDE.md`, beside the Codex and Cursor lines (`CLAUDE.md:71`, `CLAUDE.md:72`).
- **Why:** the capability reflex requires every capability to be registered there (`CLAUDE.md:49`), and a member asking "does Glitch work in OpenCode" today matches nothing.
- **Proposed line:** "does Glitch work in OpenCode / set up OpenCode / use my ChatGPT login with Glitch" → **the OpenCode walk** (item 6: install the plugin once, open OpenCode in the Glitch folder; guards, identity and memory, and capture into memory; project and task are machine-wide there).

## 8. A tracked `.opencode/` hook layer, if OpenCode ever reads one

- **File:** a new tracked `.opencode/` at the Glitch root, delivered by `/update` like `.cursor/hooks.json` (`operations-reference.md:2307`).
- **Why:** a project-level layer would remove the member's one manual config edit (item 5).
- **Measured today:** plugins loaded from `$OPENCODE_CONFIG_DIR/opencode.json` `plugins` and from `$OPENCODE_CONFIG_DIR/plugins/<dir>/` (`docs/api-findings.md item 1`); the only project-level file OpenCode was seen to read at the Glitch root is `AGENTS.md` (`docs/api-findings.md item 7`).
  A project `.opencode/` folder was not tested, so this item waits on that measurement.

## 9. A reader built on `opencode session export`

- **File:** a new engine module (or the `transcript_filter.py` adapter in item 2) and its sweep, the OpenCode twin of `cursor_transcripts.py`.
- **Why:** `opencode session export` is the supported read path, and it avoids the credential-holding database (`docs/api-findings.md item 11`).
- **Exact commands and measured facts:**
  - Read one chat: `opencode session export --standalone <id>` prints `{info, messages[]}` JSON in about 0.24 s, needs no background service, and works while another server holds the same data folder (`docs/api-findings.md item 11`, row 11a).
  - `--standalone` is required: without it the command talks to the background service, which ignores the environment's overrides (`docs/api-findings.md item 1`, row 1g).
  - An unknown id: exit 1 and `Session not found: <id>` on stderr, measured on 2.2.7 (`src/bridge/chatsource.py:124`); treat only that answer as "gone".
  - List chats: `opencode session list --standalone --format json` returns `[{id, title, updated, created, projectId, directory}]`, newest first, the **top-level** chats of the **current project only** (the `$PWD` folder), in about 0.29 s; children are not listed and there is no `parentID` (`docs/api-findings.md item 11`, row 11b).
  - So a missing row is not proof a chat is gone (it may be another project's), and a sub-agent chat is found only through its parent's `subagent` tool part or `info.parentID` in its own export (`docs/api-findings.md item 17`).
  - Run it with `PWD` set to the Glitch folder, because OpenCode takes the project from `$PWD` (`docs/api-findings.md item 10`).
  - Set the recursion guard on the child, because `--standalone` starts a private server that loads plugins (`src/bridge/chatsource.py:34`, `docs/api-findings.md item 1`).
  - The export keeps every original turn after a compaction (`docs/api-findings.md item 14`).

## 10. The read guard's login-file list, and test doors for the state folder and the catch diary

- **Files:** `.claude/hooks/read-guard.py` (the login-secret prefilter `_SECRET_ANCHORS` at `.claude/hooks/read-guard.py:90`), `.claude/scripts/config.py` (`STATE_DIR` at `.claude/scripts/config.py:137`, `PENDING_DIR` at `.claude/scripts/config.py:142`) and `.claude/hooks/post-tool-secret-guard.py` (the catch diary at `.claude/hooks/post-tool-secret-guard.py:110`).
- **Found in a real run:** a read of `.claude/data/google-accounts/cp5-canary/token.json` was denied as a login secret through the installed plugin, while a read of `.claude/data/google/token.json` (no `-accounts`) came back allowed from the bridge (`docs/api-findings.md item 31`).
  The prefilter names `.claude/data/google-accounts`, `.claude/data/microsoft-accounts`, `.claude/data/imap-accounts` and `.claude/scripts/integrations` only (`.claude/hooks/read-guard.py:90`).
  No engine script names a `.claude/data/google/` folder today (a search of `.claude/scripts/*.py` finds none), so this may be harmless; whether any older layout, migration or tool ever wrote a token there was not checked.
- **Recommendation:** the Glitch team reviews the login-file list against every place a token has ever lived, and adds a test that a token-shaped name under `.claude/data/` outside the known homes is either denied or deliberately allowed.
- **Test doors:** `config.PENDING_DIR` and `config.STATE_DIR` have no environment override (`.claude/scripts/config.py:137`, `.claude/scripts/config.py:142`), so this adapter added its own test-only door, `GLITCH_OPENCODE_CAPTURE_STATE`, accepted only for a folder strictly inside the system temp folder (`src/bridge/chatcache.py`, module docstring; `tests/py/test_chatcache.py::test_state_roots_refuses_non_temp`).
  The secret-catch diary (`secret-catches.jsonl` under `config.STATE_DIR`, `.claude/hooks/post-tool-secret-guard.py:102`) has no test door either, which is why the post-tool scan could not be re-run with a model without writing to the member's real state folder (`docs/api-findings.md item 32`).
  An engine-owned override for both, of the kind `GLITCH_WATCH_STATE_DIR` and `GLITCH_READ_GRANT_STATE_DIR` already are (`docs/api-findings.md item 18`), would let every adapter test against the real engine safely.

## Not verified

- Windows and Linux: nothing ran there (`docs/api-findings.md item 9`).
- Any OpenCode version other than 2.2.7.
- Whether a plugin can add a variable to a shell's environment in `shell.create.before` (item 3, route 2).
- OpenCode's process name in a command's ancestry, and whether a shell runs under one shared process for several windows without `--standalone` (item 3).
- A project-level `.opencode/` folder being read by OpenCode (item 8).
- The real default location of OpenCode's data folder (item 4).
- Nothing here was run from inside an OpenCode session in the Glitch folder; that run was stopped by the free model quota (`docs/api-findings.md item 16`).
