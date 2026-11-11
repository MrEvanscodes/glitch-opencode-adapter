# Glitch for OpenCode

**Use your Glitch memory and safety guards inside OpenCode, with any AI model you like.**

A community add-on by James, a Glitch Cat Club member. Free, MIT licence.
It is **not** part of Glitch and it is **not** made or supported by the Glitch team.

---

## What is this, in plain English?

- **Glitch** is your second brain. It remembers you, and it has safety guards that stop an AI doing something silly to your files.
- **OpenCode** is a free app (https://opencode.ai) that lets you chat with and code with lots of different AI models, including a ChatGPT login and cheap OpenRouter models.
- Glitch already works in Claude Code, Codex and Cursor. It did **not** work in OpenCode. This add-on fixes that.

With it installed, when you start OpenCode inside your Glitch folder:

1. **The AI knows you.** Your profile and memory are loaded at the start, so you do not have to explain yourself.
2. **The safety guards are on.** If the AI tries to do something Glitch forbids, Glitch stops it and tells the AI why. Examples: writing into Glitch's own files, reading your login files, or reading things outside your Glitch folder without asking you.

In any other folder, it does nothing at all. Your other OpenCode projects are untouched.

---

## Is this for me?

Tick all of these:

- [ ] I have **Glitch set up** (I have run `/setup` and have a Glitch folder, usually `~/Glitch`).
- [ ] I am on a **Mac**. (Tested on a Mac only. Windows and Linux are untested.)
- [ ] I have **OpenCode** installed, version **2.2.7**. (The only version tested.)
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
| "Glitch guards in OpenCode failed to run" | The add-on hit an error and let your command run anyway (on purpose, so it never locks you out). Run the `status` command above and check `uv --version` works. Details go to a file called `opencode-adapter-errors.log` in your Mac's temp folder (`open "$TMPDIR"` shows it). |
| I moved the folder | Run the install again from the new place (steps 3 and 5). |
| My OpenCode is not version 2.2.7 | It will warn you and carry on, but it may not work. Only 2.2.7 was tested. |
| I am on Windows or Linux | Untested. It may not work. |

### Turn it off

From this folder:

```bash
uv run --no-project python scripts/install.py uninstall
```

That puts your OpenCode settings back to how they were (or removes only this add-on's line if you have edited them since). Fully quit and reopen OpenCode afterwards.

### Updating later

Download the newer folder, put it in the same place, and run the install command again.

---

## Please read: privacy and limits

- **Your memory goes to the AI company you choose.** The add-on puts your Glitch profile and memory into the model's context, about 20,000 characters on every message. Whichever model you pick in OpenCode (OpenAI, an OpenRouter model, and so on) will see it. That is true of any AI you use with Glitch, but know it before you start.
- **The memory keeper.** When Glitch has memory housekeeping waiting, it asks the AI to run it in the background. That means your memory is read and updated through that company's model too.
- **It is a guardrail, not a wall.** It relies on the hooks OpenCode provides. Anything OpenCode does without using those hooks is not guarded.
- **It does not save your OpenCode chats into Glitch's memory yet.** That is planned for later.
- **It changes nothing in Glitch.** It only reads your Glitch's own guard code. The only thing it changes on your Mac is that one line in OpenCode's settings (backed up first).
- **Glitch updates could break it.** It reuses your Glitch's own guard code, so a big Glitch change might need an update here.
- **Logins and subscriptions.** Which logins you may use inside OpenCode (for example a Claude or ChatGPT subscription) is between you and those companies. Their rules change, so check their current terms. This add-on does not touch your logins.
- **No support is promised.** It was built carefully and tested, but it is one member's work. Please do not ask the Glitch team to fix problems with this add-on.

---

## For tinkerers

- `docs/HOW-IT-WORKS.md` explains the design and shows the test results.
- `docs/api-findings.md` records exactly how OpenCode 2.2.7's plugin system behaves, measured on a real install. Start there if you want to adapt this to a newer OpenCode.
- Plugin tests: from this folder, run `node --test tests/ts/*.test.ts` (needs Node 22.18 or newer).
- The Python tests only run when this folder sits inside a Glitch folder, because they check Glitch's own code. See `docs/HOW-IT-WORKS.md`.

## Licence

MIT, copyright 2026 James, Glitch Cat Club member. See `LICENSE`.
