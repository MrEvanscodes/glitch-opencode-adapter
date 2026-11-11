# Glitch for OpenCode

**Use your Glitch memory and safety guards inside OpenCode, with any AI model you like.**

A community add-on by James, Glitch Cat Club member. Free, MIT licence.
It is **not** part of Glitch and it is **not** made or supported by the Glitch team.

---

## What is this, in plain English?

- **Glitch** is your second brain. It remembers you, and it has safety guards that stop an AI doing something silly to your files.
- **OpenCode** is a free app (https://opencode.ai) that lets you chat with and code with lots of different AI models, including a ChatGPT login and cheap OpenRouter models.
- Glitch already works in Claude Code, Codex and Cursor. It did **not** work in OpenCode. This add-on fixes that.

With it installed, when you start OpenCode inside your Glitch folder:

1. **The AI knows you.** Your profile and memory are loaded at the start, so you do not have to explain yourself.
2. **The safety guards are on.** If the AI tries to do something Glitch forbids, Glitch stops it and tells the AI why. Examples: writing into Glitch's own files, reading your login files, or reading things outside your Glitch folder without asking you.
3. **Your chats go into your memory.** An OpenCode chat is saved into Glitch's memory as you go, so Glitch, and Claude Code in the same folder, can find it later. Details in "Memory out" below.

In any other folder, it does nothing at all. Your other OpenCode projects are untouched.

---

## Is this for me?

Tick all of these:

- [ ] I have **Glitch set up** (I have run `/setup` and have a Glitch folder, usually `~/Glitch`).
- [ ] I am on a **Mac**. (Tested on a Mac only. Windows and Linux are not tested.)
- [ ] I have **OpenCode** installed, version **2.2.7**. (The only version tested. It came out on 2026-11-10 and was still the newest 2.x release when checked on 2026-11-11.)
- [ ] I want to use **AI models other than Claude** with Glitch, or I just want Glitch inside OpenCode.

If you only ever use Claude Code, you do not need this.

### Quick check of what you have

Open the **Terminal** app (press `⌘ + Space`, type `Terminal`, press Enter), then copy and paste these one at a time:

```bash
opencode --version
```

It should mention `2.2.7`.

```bash
uv --version
```

It should print a version number. If it says `command not found`, run Glitch's `/setup` again, or install uv from https://docs.astral.sh/uv/.

```bash
ls ~/Glitch
```

It should list Glitch's files. If your Glitch lives somewhere else, use that folder wherever you see `~/Glitch` below.

---

## Install (5 steps, about 3 minutes)

**1. Get the files.**
On this GitHub page, click the green **Code** button, then **Download ZIP**, and open the zip so it unpacks into a folder.
(If you know git, you can clone this page instead.)

**2. Put the folder somewhere permanent.**
Drag it into your **home folder** (the one with your name on it in Finder), and rename it `glitch-opencode-adapter`.
Do not leave it in Downloads. OpenCode will be pointed at this folder, so if you move it later you must run the install again.

**3. Open Terminal and go into the folder.**
Copy and paste:

```bash
cd ~/glitch-opencode-adapter
```

**4. Preview the install. This changes nothing.**

```bash
uv run --no-project python scripts/install.py install --dry-run
```

It prints what it would change. Normally that is one new line in OpenCode's settings file, `~/.config/opencode/opencode.json`.
That is the folder it uses when you do not name another one.
If you keep OpenCode's settings somewhere else, add `--config-dir <that folder>` to this command and the next one.

**5. Install it.**

```bash
uv run --no-project python scripts/install.py install
```

It makes a backup of your OpenCode settings first, then adds that one entry. You should see the word `installed`.

**Now fully quit OpenCode and open it again.** Any OpenCode window that was already open will not have the add-on. Start OpenCode **from inside your Glitch folder**:

```bash
cd ~/Glitch
opencode
```

(If you use the OpenCode app instead of the terminal, open your Glitch folder as the project. That route is less tested. If the checks below fail, use the terminal route above.)

---

## Check that it works (2 minutes)

In the OpenCode window you just opened, type these one at a time:

1. `What is my current objective (my north star)? Answer from your context only, do not use any tools.`
   **Expected:** it tells you the north star you gave Glitch during `/setup`.

2. `Run the shell command cat /etc/hosts and show me the output.`
   **Expected:** the file is **not** shown straight away. Either Glitch stops it ("outside your Glitch folder, I need your permission, OK?") or the AI asks you first. If you answer `yes`, that one read goes through. That is the guard working.

3. `Run echo ok in the shell and tell me what it printed.`
   **Expected:** it just prints `ok`, with no Glitch message. Normal things run normally.

All three? You are done.

---

## Problems? Start here

| What you see | What to do |
|---|---|
| `uv: command not found` | Run Glitch's `/setup` again, or install uv from https://docs.astral.sh/uv/ |
| The AI does not know me, or no guard messages | Did you start OpenCode from inside your Glitch folder? Did you fully quit and reopen it after installing? Run `uv run --no-project python scripts/install.py status` from this folder. It should say `installed`. |
| "Glitch could not save your OpenCode chat to memory just now" | Your safety guards still work; only saving that chat to memory failed. It tries again at the end of your next turn and when you next start a chat. If it keeps happening, run the `status` command above and check `opencode --version` works. Details go to the same `opencode-adapter-errors.log` file as below. |
| "Glitch guards in OpenCode failed to run" | The add-on hit an error and let your command run anyway (on purpose, so it never locks you out). Run the `status` command above and check `uv --version` works. Details go to a file called `opencode-adapter-errors.log` in your Mac's temp folder (`open "$TMPDIR"` shows it). |
| I moved the folder | Run the install again from the new place (steps 3 and 5). |
| My OpenCode is not version 2.2.7 | It will warn you and carry on, with the guards still on, but it may not work. Only 2.2.7 was tested. |
| I am on Windows or Linux | Not tested. It may not work. |
| I installed an older copy before | Run the install from this folder. If the older copy's folder was named `glitch-opencode-adapter` (as step 2 says), it updates that entry in place rather than adding a second one. |

### Turn it off

From this folder:

```bash
uv run --no-project python scripts/install.py uninstall
```

That puts your OpenCode settings back to how they were (or removes only this add-on's line if you have edited them since). Fully quit and reopen OpenCode afterwards.

### Updating later

Download the newer folder, put it in the same place, and run the install command again.

---

## Memory out: your chats go into Glitch's memory

**Which chats.**
A chat you start in your Glitch folder, or in any folder inside it, is saved once it holds five or more messages.
Your messages and the AI's replies count together, so with one reply to each message the chat is saved at the end of your third message, when it holds six.
That includes your very first chat after installing (tested on a real install).

**When.**
At the end of each turn, and again right after OpenCode compacts (shortens) a long chat.
When a new chat starts, a quick catch-up also saves any chat that grew without being saved, for example because OpenCode was closed at the wrong moment.
A chat that keeps growing is saved again, replacing its earlier save.

**What goes into memory.**
The last 30 messages of the chat, at most 15,000 characters, with anything that looks like a secret replaced by Glitch's own secret scan.
Only your words and the AI's visible replies are kept; tool output and the AI's hidden reasoning are left out.
The memory keeper then folds it into your daily log the next time it runs, the same as a Claude Code chat.
At the start of a later chat the AI may tell you "1 OpenCode chat queued for your memory since last time".

**What is never saved.**

- Chats from before you installed this add-on, however much they grow.
- Helper chats the AI starts for itself (sub-agents), and chats Glitch runs on its own.
- Chats started outside your Glitch folder.
- One gap to know about: the start-up catch-up only looks at the chats OpenCode files under your Glitch folder itself. A chat started inside one of your projects (for example `workspaces/<project>`) is still saved at the end of each turn, but the catch-up may not see it, so if OpenCode is closed before a turn finishes saving, that turn may wait until the chat's next turn.

**The local copy.**
To let Glitch read the whole chat later, the add-on keeps a second copy of your chat text on this computer, in Glitch's data area at `.claude/data/opencode-chats/` inside your Glitch folder.
That folder is ignored by git, so the copy is never committed or backed up to GitHub by Glitch.
Every message in it goes through Glitch's secret scan first, and if the scan cannot run, no copy is written at all.
A copy is removed when OpenCode no longer has that chat (for example after you delete the chat in OpenCode); this is checked when a later chat starts.

A handful of strings that Glitch's chat reader would otherwise silently drop, such as a complete `<system-reminder>...</system-reminder>` block typed in your own message, are written with one backslash added so the reader keeps them.
The exact positions of those backslashes are stored beside the message, so your original text can be put back exactly.

To clear the copies, delete the `.claude/data/opencode-chats/` folder inside your Glitch folder.
They are written again only for chats that grow after that; a chat that does not grow keeps its saved summary in memory but has no full copy.

---

## Claude Code hand-off

At the start of a chat the AI tells you once, in one sentence, which few Glitch extras only run in Claude Code (the link between a task and its project window, the mail count on the status line, picking your open task back up after a clear, the daily brief pushed to you, and your profile being refreshed at the end of a session), and that you open Claude Code in the same Glitch folder with `claude` to use them.
Your memory, people, meetings, tasks note, plans and project list are the same files in both tools, and a saved OpenCode chat reaches Claude Code through memory once the memory keeper has run.
Which project and task "this window" is on is not shared with OpenCode.
The measured report on what is shared and what is not is `docs/claude-code-handoff.md`.

---

## Cost

With the plugin on, each model call carries about 5,100 more prompt tokens than OpenCode in the same Glitch folder without it (OpenCode loads the folder's AGENTS.md either way). Within a turn, OpenAI's prompt cache usually serves that extra as cached input. Measured on openai/gpt-5.6-luna with a starter profile; a fuller memory costs more, in proportion to its size.

The plugin's extra tokens per model call, ON minus OFF, medians of all 14 counted runs (from `docs/cost.md`):

| call | whole prompt (input + cache read) | cached input (cache read) | uncached input | output |
|---|---|---|---|---|
| 1 | +5,121 | +0 | +5,121 | +0 |
| 2 | +5,121 | +5,120 | -18 | +0 |

Honest limits:

- One model (`openai/gpt-5.6-luna`), 14 runs (7 with the plugin, 7 without), one short chat shape: one turn with two model calls around one file read.
- That one file read was refused by OpenCode's own permission rule in every run, with and without the plugin, so the numbers compare like with like but are not a completed read.
- The sign-in reported a cost of 0, so this gives tokens, not money.
- Not measured: other providers, a longer chat, a compaction, Windows.

---

## Tested on

| What | Result |
|---|---|
| OpenCode 2.2.7 (the newest 2.x release when checked on 2026-11-11) | tested |
| Any other OpenCode version | not tested; it warns and keeps running |
| macOS | tested |
| Windows | not tested |
| Linux | not tested |

Everything was tested headless (`opencode run`, one process per turn) with a throwaway OpenCode sign-in folder; the install itself was tested only against throwaway settings folders, never a real `~/.config/opencode`.

---

## Differences from Claude Code

| What | In Claude Code | In OpenCode with this add-on |
|---|---|---|
| Your profile and memory in the AI's context | Loaded once per session | Sent again on every model call, because OpenCode keeps nothing between calls; that is the cost above |
| Refresh after a compaction | Straight away | Only after OpenCode says the compaction ended, so the first reply after a compaction still uses the earlier text |
| Refresh on a new day | Not compared | At midnight in your Glitch time zone (London midnight only if Glitch's clock has no named zone), and the call that notices the new day still uses the old text |
| Messages to you from Glitch | Some are shown on screen (for example the status line) | OpenCode gives an add-on no way to show you a message, so everything (the hand-off line, the "chats queued" line, a failure warning) goes through the AI |
| The hand-off and "chats queued" lines | n/a | Said once in an interactive window; in headless use (`opencode run`) each turn is a new process, so the AI is reminded every turn |
| Saving before a compaction | Saved just before | Saved just after; nothing is lost, because OpenCode keeps every original message |
| Headless use (`opencode run`) and the memory keeper | Counts a missed memory catch-up once per session start or resume | Each turn after the first is a new process that starts as a resume, so while memory work is waiting the missed-catch-up count can rise once per turn; an interactive OpenCode window is one process and counts once |
| Start-up catch-up | n/a | Only sees chats OpenCode files under your Glitch folder itself (see "Memory out") |
| Reading back a whole chat | Glitch reads Claude Code's own chat file | Glitch reads the local copy described in "Memory out" |
| Secret scan of tool output | Adds a warning note | The same: adds a warning note, does not hide the secret |
| Which project and task this window is on | Remembered per window | Not shared: Glitch cannot tell one OpenCode window from another, so it asks you which project |
| A new chat keeps your open task | Yes | No, for the same reason |
| Builder panes | Yes | Only partly, and not tested end to end |
| Getting updates | Glitch's `/update` | Download the new folder and run the install again |
| OpenCode versions | n/a | 2.2.7 only; another version warns and keeps running |
| Windows and Linux | Supported | Not tested |
| Where the install writes | n/a | `~/.config/opencode` unless you name another folder with `--config-dir` |

The full row-by-row comparison, with the evidence for each row, is `docs/parity-checklist.md`.

---

## Please read: privacy and limits

- **Your memory goes to the AI company you choose.** The add-on puts your Glitch profile and memory into the model's context, about 21,000 characters with a starter profile, on every model call. Whichever model you pick in OpenCode (OpenAI, an OpenRouter model, and so on) will see it. That is true of any AI you use with Glitch, but know it before you start.
- **The memory keeper.** When Glitch has memory housekeeping waiting, it asks the AI to run it in the background. That means your memory, including your saved OpenCode chats, is read and updated through that company's model too.
- **It is a guardrail, not a wall.** It relies on the hooks OpenCode provides. Anything OpenCode does without using those hooks is not guarded.
- **It changes no Glitch file.** It only reads your Glitch's own code. What it writes is data in Glitch's git-ignored data area (`.claude/data/` in your Glitch folder): the chat copies, the saved chats waiting for the memory keeper, and a small record of which chats it has saved. Outside your Glitch folder it changes one line in OpenCode's settings (backed up first) and writes an error log in your Mac's temp folder if something fails.
- **Glitch updates could break it.** It reuses your Glitch's own guard code, so a big Glitch change might need an update here.
- **Logins and subscriptions.** Which logins you may use inside OpenCode (for example a Claude or ChatGPT subscription) is between you and those companies. Their rules change, so check their current terms. This add-on does not touch your logins.
- **No support is promised.** It was built carefully and tested, but it is one member's work. Please do not ask the Glitch team to fix problems with this add-on.

---

## For tinkerers

- `docs/HOW-IT-WORKS.md` explains the design, including how a chat is saved.
- `docs/api-findings.md` records exactly how OpenCode 2.2.7's plugin system behaves, measured on a real install. Start there if you want to adapt this to a newer OpenCode.
- `docs/cost.md` has the full token measurement, and `docs/engine-handoff.md` lists the changes this add-on would need from Glitch itself.
- Plugin tests: from this folder, run `node --test tests/ts/*.test.ts` (needs Node 22.18 or newer).
- The Python tests only run when this folder sits inside a Glitch folder, because they check Glitch's own code. See `docs/HOW-IT-WORKS.md`.

## Licence

MIT, copyright 2026 James, Glitch Cat Club member. See `LICENSE`.
