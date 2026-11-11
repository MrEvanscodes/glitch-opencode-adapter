# OpenCode parity checklist

Written at the Align step; every row set to its final status on 2026-11-11, after the real runs recorded in `docs/api-findings.md` items 18-37.
Legend: has = the adapter does it; differs = it does it, but not the same way, or only partly; missing = it cannot do it today; n/a = does not apply.
Compared against Claude Code (CC), Codex and Cursor as Glitch ships them.
Evidence is a file and line in this workspace, a test name (`tests/py/<file>::<name>` or a `tests/ts/<file>` test title), or a numbered item of `docs/api-findings.md` ("item N").
Engine files in "Who has it" are cited from the Glitch folder and were read at engine commit `af1aae2`.
Test names are cited as the test that covers the row; this checklist does not itself re-run the suites.

## Guards

| # | Behaviour | Who has it | OpenCode | Evidence |
|---|---|---|---|---|
| 1 | Write-guard on shell and file writes, engine's own deny words | CC, Codex, Cursor | has | `src/bridge/pretool.py` delegates to `write-guard-codex.decide` (module docstring); `tests/py/test_pretool.py::test_every_deny_carries_member_and_model_words`; shell and write denies measured with the probe, items 2-3 (not re-run through the installed plugin, item 31) |
| 2 | Read-guard: outside-Glitch pause and login-file deny | CC, Cursor; Codex via shell only | has | `pretool.py` routes read, grep, glob to `read-guard.decide`; login-file read denied through the installed plugin, item 31; `tests/py/test_pretool.py::test_read_tools_reach_the_read_guard_with_absolute_paths` |
| 3 | Deny reaches the model verbatim, turn continues | all | has | item 3 (probe); item 31 (installed plugin: the model quoted the deny back) |
| 4 | Helper agents marked, never taught "always allow" | CC, Cursor | has | `parentID` marker, item 4; `tests/py/test_pretool.py::test_sub_agent_pause_hands_the_truth_back_up_and_teaches_nothing`; `tests/ts/setup.test.ts` "a subagent session passes parent_id, looked up once per session" |
| 5 | Secret scan on tool output | CC (Bash), Codex (all), Cursor (Shell) | has, all tools | `src/bridge/posttool.py`; it adds a note and does not redact, which is Claude Code's behaviour too (item 32); `tests/py/test_posttool.py::test_token_in_shell_output_yields_a_note`; not re-run with a model, because the engine's catch diary has no test door (item 32) |
| 6 | Fail-open with the failure made visible | Codex and Cursor fail open silently | has, better | `src/plugin/failure.ts`; `tests/ts/failure.test.ts` "the notice is delivered once, then cleared"; one exception: chat capture that cannot reach OpenCode is quiet until 3 misses in a row (any answer resets the count), then said once per streak at the next chat start and answered `capture-unreachable` by the next capture that also misses (`src/bridge/capture.py` `_note_reach`; `tests/py/test_capture.py::test_notice_says_the_unreachable_line_once_per_streak_at_three`, `tests/py/test_entrypoint.py::test_capture_unreachable_three_in_a_row_is_an_error_label_once`); an unreadable list of chats is reported at each chat start until it reads again (`tests/py/test_capture.py::test_unreadable_list_is_recorded_reported_and_cleared`) |
| 7 | Guarded when started inside `workspaces/<project>` | CC; Codex needs the cover; Cursor loads no hooks | has, better | root walk-up, `src/plugin/root.ts:75`; `tests/ts/root.test.ts` "workspaces/x with its own CLAUDE.md walks past it" |
| 8 | Inert outside a Glitch folder | all | has | `tests/ts/setup.test.ts` "outside Glitch: zero registrations on every ctx domain, no bridge, no warnings"; HOW-IT-WORKS "Measured results" case 6 |
| 9 | Works across OpenCode versions | Codex: 0.145+ floor, no ceiling | differs | only v2.2.7 tested, the newest 2.x release when checked (item 34, NEWEST_VERSION line); another version warns and keeps the guards on (`src/plugin/setup.ts:148`; `tests/ts/setup.test.ts` "version mismatch warns once and still registers the guards") |
| 10 | Works on Windows and Linux | Cursor, Codex proven on Windows | missing (untested) | nothing ran on Windows or Linux, item 9 and item 34 platform table; Windows path shapes are unit-covered only (`tests/ts/root.test.ts` "windows: backslash path walks up to the drive root", `tests/py/test_install.py::test_windows_path_escaping`) |
| 11 | Wiring survives `/update` | engine ships CC, Codex, Cursor wiring | differs | the engine cannot ship OpenCode wiring; the member runs `scripts/install.py` once (item 30); proposal 5 in `docs/engine-handoff.md` |

## Memory in

| # | Behaviour | Who has it | OpenCode | Evidence |
|---|---|---|---|---|
| 12 | Identity and memory at session start | all | has | `src/bridge/context.py:342` calls `build_context`; `tests/py/test_context.py::test_session_injects_persona_block_user_and_memory`; reached every model call through the installed plugin, item 33 |
| 13 | Day context (date, open work, watched mail, projects, log tail, health notice) | all | has | same builder; watched mail measured in item 21 and item 33 |
| 14 | Keeper directive in the tool's own spawn wording, honest `/keeper` fallback | all | has | `src/bridge/context.py:97` `OPENCODE_KEEPER_DIRECTIVE`; `tests/py/test_context.py::test_keeper_directive_leads_and_names_opencode_subagent` |
| 15 | Injected once, kept for the session | CC, Codex, Cursor | differs | OpenCode drops pushes after each call (item 6d, item 13), so the session text (21,057 characters for a starter profile, item 28) is re-sent on every model call; measured cost about 5,121 prompt tokens per call, `docs/cost.md` |
| 16 | Session text refreshed on compaction, resume, `/clear` | CC | differs | compaction: rebuilt after OpenCode's `session.compaction.ended`, so the first call after a compaction carries the `resume` build and the `compact` text reaches later calls (item 28; `tests/ts/setup.test.ts` "D2 marker: a post-compaction first call classed resume is followed by the compact build when the event arrives"); resume: detected from history in `messages` or the `session.get` `outcome` key (`src/plugin/translate.ts:207`, item 28); day change: noticed at midnight in the engine's own zone (the bridge's `timezone`; London only when it names none), and the noticing call still carries the old text (`src/plugin/setup.ts:441`, `tests/ts/setup.test.ts` "memory-in: a new London day refreshes the session text once, through the bridge's date; the noticing call keeps the old text"); `/clear` in OpenCode not measured |
| 17 | Per-message context (yes/no redeem, opt-out card, watched mail, member-turn stamp) | CC, Codex; Cursor cannot answer back | has | reaches every model call of its turn, framed after the first (item 21; `tests/ts/setup.test.ts` "memory-in: the turn's card rides EVERY model call of its turn, framed after the first; a quiet next turn carries none") |
| 18 | New conversation keeps the open task (#320) | Codex | missing | needs an engine change: OpenCode gives a shell command no session id, so the engine never knows which OpenCode window it is (item 16; `docs/claude-code-handoff.md` (c); proposal 3 in `docs/engine-handoff.md`); the in-OpenCode confirmation run was stopped by the free-model quota (item 16) |
| 19 | Builder-pane start role | CC, Codex | differs (partly) | `GLITCH_START` would reach the bridge under `opencode --standalone` (inferred, not run) and not under the shared background service (unmeasured); per-window task lanes still need the window identity of row 18 (item 16; `docs/claude-code-handoff.md` (c)) |
| 20 | No context for automated sessions | all | has | `src/bridge/context.py:172` `is_automated`; `tests/py/test_context.py::test_subagent_session_gets_nothing` |

## Memory out (capture)

| # | Behaviour | Who has it | OpenCode | Evidence |
|---|---|---|---|---|
| 21 | Stage the chat into `pending/` | CC (end, pre-compact), Codex (sweep), Cursor (per turn, sweep, end, pre-compact) | has: per turn, after a compaction, and a sweep at chat start | `src/bridge/capture.py:581` `capture_one`, `src/bridge/capture.py:677` `sweep`; turn end `session.execution.succeeded` (item 10, `src/plugin/setup.ts:715`); measured end to end through the installed plugin and read back by the digest (item 33, item 36; `tests/py/test_e2e_digest.py::test_read_session_reads_the_staged_chat_from_its_transcript`) |
| 22 | Excerpt contract: 30 turns, 15,000 chars, min 5 turns, redacted, header with pointer, atomic write | all | has | `src/bridge/capture.py:100`-`102`; `tests/py/test_capture.py::test_constants_match_the_flush_hook`, `::test_excerpt_bounds_turns_and_chars`, `::test_floor_four_vs_five`, `::test_stub_name_and_header_field_order`; redaction `::test_no_secret_anywhere_under_the_capture_root_or_the_log` |
| 23 | One stub per chat, replaced as it grows | Codex, Cursor | has | `src/bridge/capture.py:306` `stub_name`; `tests/py/test_capture.py::test_staged_chat_that_grows_replaces_its_one_stub_and_copy`; item 24 |
| 24 | Ledger, no backfill, file lock | Codex, Cursor | has | ledger `opencode-staged.json` (`src/bridge/capture.py:112`); `tests/py/test_capture.py::test_before_first_sweep_chat_that_grows_stays_unstaged`, `::test_two_concurrent_captures_leave_one_consistent_stub`; the member's first chat after install is captured (item 24, `::test_d1_first_chat_after_install_is_captured_from_its_next_turn`) |
| 25 | Helper-agent chats never staged | Codex, Cursor | has | `tests/py/test_capture.py::test_child_automated_outside_and_bad_id_skip`; `tests/ts/setup.test.ts` "capture: a child session (parentID) is skipped, looked up once" |
| 26 | Backstop for a killed window | Cursor, Codex | has | the start sweep (`src/bridge/context.py:272` `run_sweep`); the turn-end capture runs in a detached child that outlives `opencode run` (item 20, `src/plugin/bridge-client.ts:282`); `tests/py/test_capture.py::test_sweep_bounded_by_count_and_newest_first` |
| 27 | Stage before a compaction | CC, Cursor | has (after, not before) | OpenCode exposes only `session.compaction.ended` (item 14), so the chat is staged right after a compaction; nothing is lost because the export keeps every original turn (item 14); measured in item 22 and item 28 |
| 28 | "N chats queued for your memory" line at session start | Codex, Cursor | has | `src/bridge/capture.py:903` `capture_notice`; `tests/py/test_capture.py::test_notice_wording_once_and_reads_no_opencode`; `tests/py/test_context.py::test_notice_reports_a_chat_the_start_sweep_captured` |
| 29 | Pointer to the whole chat | CC, Codex, Cursor | differs | the engine has no OpenCode reader, so the stub points at a Claude-shaped, redacted local copy (`src/bridge/chatcache.py`, ruling 5); read back by the engine's digest (item 36); proposal 2 in `docs/engine-handoff.md` |
| 30 | OpenCode chat store, turn-end and compaction events | n/a | has (measured) | items 10, 11, 14 |
| 31 | Capture scope setting | Codex only | n/a | the plugin is inert outside a Glitch folder (row 8) |

## Found during the build

| # | Behaviour | Who has it | OpenCode | Evidence |
|---|---|---|---|---|
| 32 | A visible notice the tool shows the member (toast, status line, banner) | CC | missing | a plugin has no notice call (item 15, `VISIBLE_SURFACE=none`), so the hand-off line, the capture line and a failure notice all go through the model |
| 33 | The hand-off line and capture line said once | CC (shown once) | differs | interactive: the session text, hand-off included, is built once per chat and re-pushed unchanged, so the one instruction to say it once stands for the whole chat (read from `src/plugin/setup.ts:435`; every real run was headless, so this was not run); headless `opencode run` is one process per turn, so every turn rebuilds the text and the hand-off instruction and any new "chats queued for your memory" line arrive again each turn (item 20, item 21); whether a model repeats the sentence was not measured |
| 34 | The start-up catch-up finds every chat | Codex, Cursor | differs | the sweep lists only the chats OpenCode files under the Glitch folder's own project (`opencode session list` is per project, item 11; `src/bridge/capture.py:770`); a chat started in a subfolder is still captured at each turn end (`tests/py/test_capture.py::test_workspace_subfolder_counts_as_this_root`), but whether the catch-up lists it depends on which OpenCode project that folder belongs to, which was not measured |
| 35 | Which project and task "this window" is on | CC, Codex | missing | not shared with OpenCode: the engine cannot identify an OpenCode window (item 16, `docs/claude-code-handoff.md` (b)) |
| 36 | Wiring written into the tool's config by default | engine ships it | differs | `scripts/install.py` targets `~/.config/opencode` when no `--config-dir` or `OPENCODE_CONFIG_DIR` is named (item 30; `tests/py/test_install.py::test_default_folder_is_used_only_when_nothing_is_named`) |
| 37 | Keeper-miss count in headless use (`opencode run`) | CC, Codex count once per session start or resume | differs | each `opencode run` turn after the first is a new process whose first build is a `resume` (`src/plugin/translate.ts:207`), and `counts_miss` is True for a resume (`src/bridge/context.py:233`), so while memory work is pending and no keeper started within the engine's spawn grace, the miss count can rise once per turn; an interactive session is one process and counts once at its start |

## Claude-only by design

Armed task link, status-line count, SessionEnd persona sync, `/clear` pane hand-over, the day's pushed brief.
Codex and Cursor leave them out.
Decision: leave them out of OpenCode, and tell the member once where they live: the hand-off line (`src/bridge/context.py:116`, `tests/py/test_context.py::test_handoff_names_every_claude_code_only_extra_and_the_way_in`) and the measured report `docs/claude-code-handoff.md`.

## Engine side, out of reach (goes to `docs/engine-handoff.md`)

The "What works in which tool" row, an OpenCode adapter in `transcript_filter.py`, the "Reading a past chat" paths, `/update` delivery, a `/setup` walk, a trigger-map line, the login-file list of the read guard, and test doors for the state folder and the secret-catch diary.

## Housekeeping (settled)

- The probe harness and the real-run and cost scripts were the author's private test harness and are not included in this folder.
- HOW-IT-WORKS now names this workspace `workspaces/opencode-adapter`.
- `install.py` `STALE_SUFFIX` did not match this workspace's own path (`is_ours` is checked first); the gap was the README checkout name `glitch-opencode-adapter`, which it missed. Fixed with `STALE_SUFFIXES` (`scripts/install.py:52`) and tests `tests/py/test_install.py::test_own_path_is_never_stale`, `::test_readme_checkout_name_is_recognised_as_stale`, `::test_unrelated_lookalike_paths_are_not_stale` (item 30).

## Rulings from Align

Overall: as close to Claude Code with Glitch as possible; ties go to the closest and most effective.

1. Same outcome for the member; every unavoidable difference written down plainly. Measure extra cost in Verify: tokens per model call with and without the plugin, cached and uncached, on an OpenAI model; real numbers in the README.
2. Measure structure only: read-only on OpenCode's data and config folders (table layout, the probes' own test chats). Never other chats, never login, token or auth data. Databases opened read-only. Throwaway config for probe runs.
3. Per-turn staging if events exist, otherwise the sweep.
4. Import stable engine helpers, own the excerpt, ledger and stub logic, pin constants to the hook source by test.
5. Claude-shaped redacted copy of each OpenCode chat that started in a Glitch folder, in a gitignored local cache under Glitch's data area; stub points at it. README states plainly it is a second local copy of chat text.
6. Measure whether pushing into `messages` persists; if so inject once, else re-push until the next member message.
7. Rebuild session text on compaction and on a local day change, no keeper miss on compaction, if a compaction signal exists.
8. Mac only: 2.2.7 plus the newest OpenCode in a throwaway folder (ask the user first with exact download details). Windows and Linux marked unverified. Warn-but-run on a version mismatch.
9. Never touch the real OpenCode config. No change to the public adapter repository without the user's explicit yes.
10. Write `docs/engine-handoff.md`.
11. Match Codex and Cursor, plus a smooth OpenCode to Claude Code hand-off: shared memory through capture, a visible session-start line naming the Claude-Code-only extras and what to do, and a check of whether task and project state is shared across tools. Report what is and is not possible.
