# conversation-archiver

A Claude Code plugin that archives every conversation turn — **your input and
Claude's text reply only (tool calls / tool results / thinking are excluded)** —
into a git repository, one markdown file per session, organized by month.

```
~/claude-conversations/
└── 2026-06/
    └── 2026-06-05-Learn-about-Claude-Code-plugin-development.md
```

## Requirements

`python3` and `git` must be on `PATH`. Claude Code has no arbitrary-code gate at
`/plugin install` time, so the plugin verifies these in a **`SessionStart` hook**
(`hooks/check_deps.sh`) — if either is missing it prints a one-line warning at
session start (and logs it). The archive hooks themselves are guarded
(`command -v python3 && command -v git && … || true`), so a missing dependency is
a clean no-op, never a blocked prompt. The check is written in bash so it runs
even when `python3` is absent. All locking is done with Python's `fcntl` (no
external `flock` binary), so it works on both Linux and macOS. The `SessionStart`
hook also records the plugin's path so the `:upload` command can find `archive.py`.

## How it works

A `Stop` hook (fires right after each assistant turn), a `UserPromptSubmit` hook
(fires when you send your next message), and a `SessionEnd` hook run
`scripts/archive.py`, which:

1. Reads the hook payload (`session_id`, `transcript_path`) from stdin.
2. Parses the session transcript JSONL and pulls out:
   - **User input** — `type: "user"` entries whose content is a typed string
     (tool-result entries, slash-command wrappers and `<system-reminder>` blocks
     are stripped).
   - **Claude's reply** — `type: "assistant"` `content[]` blocks of `type: "text"`
     (`thinking` and `tool_use` blocks are dropped).
   - **Compaction boundaries** — `type: "system"`, `subtype: "compact_boundary"`
     entries are turned into a divider line noting the trigger and token counts.
3. Accumulates new turns (keyed by message `uuid`) into a local, git-ignored
   **state file**, then rebuilds the session's markdown file from that state.

### When it archives

The archiver runs on three events, layered so the result is both prompt and
complete:

- **`Stop`** (right after each assistant turn) — low-latency: your answer lands in
  git as soon as the turn finishes, instead of waiting for your next message.
- **`UserPromptSubmit`** (your next message) — a backstop. When a `Stop` hook fires,
  the turn's final assistant message is not always flushed to the transcript JSONL
  yet, and a single answer often spans several assistant messages (text → tool call
  → more text). The next prompt re-runs the archiver once the previous turn is fully
  written, so **every message of the answer is captured**.
- **`SessionEnd`** — flushes the final turn when you close the session.

The archiver is **idempotent** — blocks are de-duplicated by message `uuid` — so the
overlapping triggers never produce duplicate content or duplicate commits.

### Filename

`<YYYY-MM>/<YYYY-MM-DD-HHMM>-<session-name>.md`

- Timestamp is the **session start** time, `HHMM` in local time (stable for the
  life of the session).
- Session name comes from the `ai-title` Claude Code generates for the session.
  It is generated lazily — until it exists the file is named after the short
  session id, and is renamed (via `git mv`, preserving history) once the title
  appears.
- The name is sanitized: whitespace → `-`, punctuation removed, CJK kept, no
  spaces, capped at 60 chars.

### Why content is never lost

- The archive is keyed on the **stable `session_id`**, so context compaction
  (which keeps the same session id and transcript path) never forks a session
  into a second file — it's always **one session, one file**.
- Turns are accumulated append-only into the state file by `uuid`. Even if the
  on-disk transcript were ever truncated after compaction, turns already
  archived survive. The markdown is rebuilt from the full accumulated state on
  every turn, so earlier content is never dropped.
- Note: Claude Code does **not** persist the compaction *summary text* to disk
  (only the `compact_boundary` marker with token metadata), so the archive
  records that compaction happened rather than the generated summary.

## Modes

Switch with the plugin's subcommands (manual-invoke only — Claude won't trigger
them automatically):

| Command | Effect |
| --- | --- |
| `/conversation-archiver:auto` | **AUTO** — each turn writes the file, then `git commit` + `git push` (push runs in the background). Default. |
| `/conversation-archiver:manual` | **MANUAL** — each turn writes the file locally only; no commit/push. |
| `/conversation-archiver:upload` | Commit + push the whole archive now (use in manual mode). |
| `/conversation-archiver:status` | Show current mode, repo path, remote, and recent commits. |
| `/conversation-archiver:connect <sb-connect link>` | Connect to your **Second Brain** — see below. |

Mode is stored in `~/.claude/cc-conversation-archiver/config.json`.

## Second Brain connect

The "Connect Claude Code" dialog (the Claude Code tile on your Second Brain
home) hands you ONE paste-ready message carrying an `…/sb-connect/<code>`
link. The code is a
**one-time, 10-minute** connection code — the script redeems it via the
backend's `/activate` endpoint and receives the push URL plus a freshly
minted push credential, so nothing the user copies ever contains a token.
Fallbacks: run with no arguments to self-resolve via your `gsk login`
credential, or pass explicit `<remote_url> <token>` (machines without gsk).

Connecting wires the archive into your personal `/memo` vault:

- the gsk push credential is stored via a git **credential-store file**
  (`~/.claude/cc-conversation-archiver/git-credentials`, chmod 600) — never
  plaintext in `.git/config`;
- `origin` points at your vault repo; the archive moves under the vault's
  `claude-code/` subfolder (the `subdir` config key) so pushes can never
  touch the vault's own files;
- the repo is **sparse-checkout**ed to that subfolder, so the rest of your
  vault never materializes as files on this machine;
- mode flips to **auto** — every turn commits and pushes, and the
  conversation shows up in your Second Brain's Personal Space.

Re-running the command is idempotent (refreshes the remote + credential).
Several machines can connect to the same brain; pushes rebase against each
other (see `do_push`).

## Auto-push setup

Auto mode pushes with plain `git push`, so configure a remote + upstream once.
The repo is created on the first archived turn; then:

```bash
git -C ~/claude-conversations remote add origin <your-remote-url>
git -C ~/claude-conversations push -u origin HEAD
```

Until a remote is configured, commits still happen locally and the push step is
skipped (logged, never blocks the session).

## Configuration

`~/.claude/cc-conversation-archiver/config.json`:

```json
{ "mode": "auto", "repo": "/home/you/claude-conversations" }
```

Environment overrides (take precedence): `CC_ARCHIVE_MODE`, `CC_ARCHIVE_REPO`.

## Files & logs (all under `~/.claude/cc-conversation-archiver/`)

- `config.json` — mode + optional repo path
- `state/<session_id>.json` — per-session accumulated turns (git-ignored, local)
- `state/_index.json` — relpath → session_id (filename collision guard)
- `archive.log` — per-run log
- `push.log` — background push output

## Install

This plugin lives in a local marketplace at the directory above
(`toolkits/cc-conversation-archiver`). From a Claude Code session, use the
absolute path of that directory on your machine:

```
/plugin marketplace add /ABSOLUTE/PATH/TO/gen-spark/toolkits/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

Then restart the session (hooks load at session start).

## Scope

The hook fires for **every** Claude Code session regardless of project; all
sessions are archived into the one repo, partitioned by month.
