# conversation-archiver

A [Claude Code](https://code.claude.com) plugin that saves every conversation
to a git repo — **one markdown file per session**, organized by month. It
records your prompts and Claude's text replies (tool calls, tool results, and
thinking are excluded), so you keep a clean, readable transcript of your work.

```
~/claude-conversations/
└── 2026-06/
    └── 2026-06-05-1432-Learn-about-Claude-Code-plugins.md
```

Archiving runs automatically after every turn — there is nothing to remember to
do. Optionally connect it to your **Second Brain** (or any git remote) to sync
the archive off your machine.

## Requirements

`python3` and `git` on your `PATH`. That's it — no Python packages to install.
If either is missing the plugin prints one warning at session start and quietly
does nothing (it never blocks a prompt).

## Quick start (just ask Claude)

You don't have to memorize the commands below — once installed, you can drive
most of this from a prompt. Try:

- *"Set up conversation-archiver to sync to my Second Brain."* — Claude walks you
  through `connect`.
- *"Is my conversation archive syncing? Diagnose it."* — Claude runs the
  read-only **doctor** and explains what's wrong and how to fix it.
- *"Archive all my old Claude Code sessions."* — Claude runs `backfill`.

The one thing Claude **cannot** do for you is the initial `/plugin install`
(that's a Claude Code built-in you run yourself, step 1). Everything after —
connecting a remote, switching modes, uploading, backfilling, and diagnosing — is
prompt-drivable.

## 1. Install

From any Claude Code session:

```
/plugin marketplace add genspark-ai/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

Then **restart the session** — hooks load at session start.

Update later with:

```
claude plugin update conversation-archiver@cc-conversation-archiver
```

## 2. What happens after install (zero config)

Right away, with no further setup:

- Every turn is archived to `~/claude-conversations/<YYYY-MM>/<session>.md`.
- Each session is **one file**, keyed on the session id — context compaction and
  multi-message replies never fork it or lose content.
- The file is named after the session's auto-generated title once it exists
  (renamed via `git mv`, preserving history), with the session-start timestamp.
- Changes are committed locally on every turn. **Pushing is skipped until you
  configure a remote** (step 3) — so out of the box this is a private, local
  git history of your conversations.

You can confirm anytime with `/conversation-archiver:status`.

## 3. Sync the archive off your machine (optional)

The archive is an ordinary git repo at `~/claude-conversations`. Give it a
remote and auto mode will commit **and** push every turn. Pick whichever fits.

> Auto-mode pushes run in the background and **cannot prompt for a password**,
> so the remote must use non-interactive auth — an SSH key loaded in your agent,
> or a stored HTTPS credential / token (covered per-option below). The Second
> Brain `connect` flow sets this up for you.

### A. Second Brain (`sb-git` vault) — Genspark users

This is the only option that needs no manual git or credential setup — the
plugin wires the remote, the credential, and the subfolder for you.

On your Second Brain home, click the **Claude Code** tile → **Connect** and copy
the one-line message. It contains a one-time link (10-min, single-use). Paste it:

```
/conversation-archiver:connect https://www.genspark.ai/.../sb-connect/<code>
```

The plugin redeems the code for the vault push URL plus a freshly minted push
credential — the link itself carries no token, so nothing you copy is secret.
Your conversations then appear in your Second Brain's Personal Space, confined to
the vault's `claude-code/` subfolder (the rest of your vault never touches this
machine, and the archive can never overwrite your other vault files).

**No link handy?** Run `/conversation-archiver:connect` with **no arguments** and
it resolves the same vault from your `gsk` (Genspark CLI) credential instead. For
this path you need `gsk` installed and signed in once:

```bash
npm install -g @genspark/cli   # install the Genspark CLI (one time)
gsk login                      # opens your browser — click Allow, nothing to copy
```

`gsk login` stores the credential at `~/.genspark-tool-cli/config.json`; the
plugin reuses it automatically, so you never paste a token. Then:

```
/conversation-archiver:connect
```

(The pasted-link path above does **not** need `gsk` — the one-time code already
carries the credential. `gsk` is only required for this no-argument fallback,
e.g. on a machine where you'd rather not open the website.)

Re-running `connect` is safe (it just refreshes the remote + credential), and
several machines can connect to the same vault — pushes auto-rebase against each
other.

### B. Your personal GitHub repo

1. Create a **new empty repo** on GitHub (no README/.gitignore — the archive
   already has commits). Say `you/claude-conversations`.
2. Add it as the remote and push once. Choose the auth that matches how you
   already use GitHub:

   **SSH (recommended if you have keys set up)** — push needs no password as long
   as your key is in `ssh-agent`:

   ```bash
   git -C ~/claude-conversations remote add origin git@github.com:you/claude-conversations.git
   git -C ~/claude-conversations push -u origin HEAD
   ```

   **HTTPS with a Personal Access Token** — GitHub no longer accepts a password
   over HTTPS, so store a token once (background pushes can't prompt). Easiest is
   the `gh` CLI, which configures git's credential helper for you:

   ```bash
   gh auth login          # choose GitHub.com → HTTPS → authenticate in browser
   git -C ~/claude-conversations remote add origin https://github.com/you/claude-conversations.git
   git -C ~/claude-conversations push -u origin HEAD
   ```

   Or do it without `gh`: create a fine-grained PAT (Contents: read & write on
   that repo) and let git's credential helper cache it on first push —
   `git config --global credential.helper osxkeychain` (macOS) or
   `store` / `cache` (Linux), then push and paste the PAT as the password.

3. Done — auto mode keeps it in sync from here.

### C. Any other git remote (GitLab, Gitea, self-hosted…)

Same as GitHub — create an empty repo, then:

```bash
git -C ~/claude-conversations remote add origin <your-remote-url>
git -C ~/claude-conversations push -u origin HEAD
```

Use an SSH remote with a loaded key, or store an HTTPS token via your platform's
credential helper, so background pushes never need a prompt.

> Tip: `/conversation-archiver:status` shows the configured remote and recent
> commits, and `~/.claude/cc-conversation-archiver/push.log` records each push
> attempt — check it if a push silently isn't landing (usually missing auth).

### Verify the connection

After any of the three options, confirm the remote actually accepts a push —
don't wait for a real turn to find out auth is broken.

1. **Check the remote is set** — run:

   ```
   /conversation-archiver:status
   ```

   The `remote:` line should show your URL (for Second Brain, the vault repo).
   If it says `none`, the remote wasn't added — revisit step 3.

2. **Force a test push** — run:

   ```
   /conversation-archiver:upload
   ```

   This commits any pending archive and pushes right now, then prints the result:

   - `pushed (<repo>)` → **connection works.** ✅
   - `push skipped/failed — configure a remote: …` → the push was rejected.
     Almost always an auth problem (missing SSH key in the agent, or no stored
     HTTPS token) or a wrong/typo'd remote URL. Fix the auth from step 3 and
     re-run `/conversation-archiver:upload`.

   (The Second Brain `connect` flow already does a push at the end and reports
   success or failure, so a clean connect message means step 2 is already green —
   running `upload` again just re-confirms it.)

3. **Eyeball the remote** — open the repo in your browser (or
   `git -C ~/claude-conversations ls-remote origin`) and confirm a `YYYY-MM/`
   folder with your `.md` files is there. For Second Brain, open your Personal
   Space and look under `claude-code/`.

If a push fails, the full git error is in
`~/.claude/cc-conversation-archiver/push.log`.

## 4. Commands

All commands except `doctor` are manual-invoke only (Claude never runs them on
its own). `doctor` is read-only and **model-invocable**, so Claude can run it for
you straight from a prompt (e.g. *"diagnose my conversation archiver"*):

| Command | What it does |
| --- | --- |
| `/conversation-archiver:doctor` | **Diagnose** the whole setup — deps, config, repo, a read-only remote auth test, sync state, recent errors — and report problems with fixes. Safe to run anytime. |
| `/conversation-archiver:status` | Show mode, repo path, remote, and recent commits. |
| `/conversation-archiver:connect <link>` | Connect to your Second Brain (see step 3A). |
| `/conversation-archiver:backfill` | Archive every **pre-existing** Claude Code session on disk (the ones from before you installed the plugin), then commit + push once. Idempotent. |
| `/conversation-archiver:auto` | **AUTO** mode (default) — commit + push every turn. |
| `/conversation-archiver:manual` | **MANUAL** mode — write the file locally only; no commit/push. |
| `/conversation-archiver:upload` | Commit + push the whole archive now (use in manual mode). |

Already had sessions before installing? Run `/conversation-archiver:backfill`
once to capture them all.

## Configuration

Settings live in `~/.claude/cc-conversation-archiver/config.json`:

```json
{ "mode": "auto", "repo": "/home/you/claude-conversations" }
```

- `repo` — where the archive lives (defaults to `~/claude-conversations`).
- `mode` — `auto` or `manual`.

Environment variables override the file: `CC_ARCHIVE_MODE`, `CC_ARCHIVE_REPO`.

Other files under `~/.claude/cc-conversation-archiver/`: `state/` (per-session
accumulated turns, local only), `archive.log`, and `push.log`.

## Troubleshooting

**First stop: `/conversation-archiver:doctor`** (or just ask Claude *"diagnose my
conversation archiver"*). It checks dependencies, config, the repo, remote auth
(a live read-only `ls-remote` test), sync state, and recent errors, then prints a
verdict with the fix for anything it finds. It's read-only and never pushes.

Common cases it catches:

- **Nothing is being archived** — restart your session after installing (hooks
  load at session start), and check `python3` + `git` are on `PATH`.
- **Commits happen but nothing pushes** — no remote configured yet (step 3), or
  the remote's auth is failing (doctor's connection test shows the git error).
- **Deeper digging** — `~/.claude/cc-conversation-archiver/archive.log` (per-run)
  and `push.log` (push output).

## How it works (details)

See [`plugins/conversation-archiver/README.md`](plugins/conversation-archiver/README.md)
for the full behavior reference: the four archiving triggers (`Stop`,
`SubagentStop`, `UserPromptSubmit`, `SessionEnd`), idempotency by message uuid,
filename rules, compaction handling, and the Second Brain integration internals.

## Development & releasing (maintainers)

This repo is **published** — its source of truth is the
[gen-spark](https://github.com/) monorepo under
`toolkits/cc-conversation-archiver/**`, mirrored here by the **CC Archiver
Release** GitHub Action. Don't hand-edit the published repo. To run the
in-development copy locally:

```
/plugin marketplace add /ABSOLUTE/PATH/TO/gen-spark/toolkits/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

Release procedure: [`RELEASING.md`](RELEASING.md).
