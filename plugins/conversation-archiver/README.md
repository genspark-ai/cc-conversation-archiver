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

A `Stop` hook (fires right after each assistant turn), a `SubagentStop` hook
(fires when an async subagent / background Task finishes), a `UserPromptSubmit`
hook (fires when you send your next message), and a `SessionEnd` hook run
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

The archiver runs on four events, layered so the result is both prompt and
complete:

- **`Stop`** (right after each assistant turn) — low-latency: your answer lands in
  git as soon as the turn finishes, instead of waiting for your next message.
- **`SubagentStop`** (an async subagent / background Task finishes) — flushes the
  conversation as soon as a long-running async task completes, even if you've
  stepped away and won't send another message for a while. The hook is handed the
  *subagent's* transcript, so the archiver detects that (a sidechain transcript
  under `…/subagents/`) and redirects to the **main** session transcript — the
  subagent's own task prompt and replies are never archived.
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

Switch with the plugin's subcommands. All are manual-invoke only — Claude won't
trigger them automatically — **except `doctor`**, which is read-only and
model-invocable, so Claude can run it from a plain prompt:

| Command | Effect |
| --- | --- |
| `/conversation-archiver:auto` | **AUTO** — each turn writes the file, then `git commit` + `git push` (push runs in the background). Default. |
| `/conversation-archiver:manual` | **MANUAL** — each turn writes the file locally only; no commit/push. |
| `/conversation-archiver:upload` | Commit + push the whole archive now (use in manual mode). |
| `/conversation-archiver:repo <path>` | Repoint the archive to a new local repo path. **Repoint-only**: updates the `repo` config and archives there from the next turn; the existing archive (and git history) is left untouched at the old path (never moved/copied/deleted). The new path is created on the next turn. If you were connected to Second Brain, re-run `:connect` so the new repo gets the remote. |
| `/conversation-archiver:backfill` | Archive every existing Claude Code transcript on disk — the sessions that ran **before** the plugin was installed (the hooks never saw them) — then commit + push once. Idempotent: re-running only adds new turns. |
| `/conversation-archiver:status` | Show current mode, repo path, remote, and recent commits. |
| `/conversation-archiver:doctor` | **Diagnose** the setup — dependencies, config, repo, remote auth (read-only `git ls-remote` probe), sync state, and recent log errors — and print a verdict with fixes. Read-only; model-invocable so you can trigger it from a plain prompt. |
| `/conversation-archiver:connect <sb-connect link>` | Connect to your **Second Brain** — see below. |

Mode is stored in `~/.claude/cc-conversation-archiver/config.json`.

## Second Brain connect

The "Connect Claude Code" dialog (the Claude Code row on your Second Brain
Sources page) hands you ONE paste-ready message carrying an
`…/sb-connect/<code>` link. The code is a
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

The `repo` path can be changed with `/conversation-archiver:repo <path>`
(repoint-only — see the table above). Environment overrides take precedence:
`CC_ARCHIVE_MODE`, `CC_ARCHIVE_REPO` (so a `:repo` change won't take effect while
`CC_ARCHIVE_REPO` is set).

## Files & logs (all under `~/.claude/cc-conversation-archiver/`)

- `config.json` — mode + optional repo path
- `state/<session_id>.json` — per-session accumulated turns (git-ignored, local)
- `state/_index.json` — relpath → session_id (filename collision guard)
- `archive.log` — per-run log
- `push.log` — background push output

## Install

The plugin is **published** to its public Claude Code marketplace repo,
[`genspark-ai/cc-conversation-archiver`](https://github.com/genspark-ai/cc-conversation-archiver).
From a Claude Code session:

```
/plugin marketplace add genspark-ai/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

Then restart the session (hooks load at session start).

### Updating

Claude Code distributes and updates plugins from the marketplace git repo
itself (a plugin is not a self-updating binary), so an update just re-pulls the
latest published tree from that repo's `main`:

```
claude plugin update conversation-archiver@cc-conversation-archiver
```

Run it whenever a new version is released (the version lives in
`.claude-plugin/plugin.json`), then restart the session so the refreshed hooks
load.

### Install from this monorepo (development)

To run the in-development copy instead of the published one, point the
marketplace at the absolute path of **this** plugin's marketplace directory on
your machine (the folder containing `.claude-plugin/marketplace.json`):

```
/plugin marketplace add /ABSOLUTE/PATH/TO/gen-spark/toolkits/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

### Releasing (maintainers)

Development happens in the gen-spark monorepo under
`toolkits/cc-conversation-archiver/**`; releases are **mirrored** to the public
repo by the **CC Archiver Release** GitHub Action — never hand-edit the public
repo. The flow: bump `version` in `.claude-plugin/plugin.json` via a normal
reviewed PR, merge to `main`, then dispatch the workflow from `main` (it tags
`conversation-archiver--v<version>`, cuts a matching GitHub Release, and refuses
to run if that release already exists — which forces the version bump first).
See [`RELEASING.md`](../../RELEASING.md) for the full procedure.

## Scope

The hook fires for **every** Claude Code session regardless of project; all
sessions are archived into the one repo, partitioned by month.

## GenTerminal inbox notifications

When the session runs inside the **GenTerminal** app, the archiver pushes a
notification into GenTerminal's in-app inbox on **Stop** (a turn finished) and
**SessionEnd** — so you can step away and get pulled back when Claude is done.
Clicking the notification jumps to that terminal tab and, if the session is
running under tmux, switches tmux to the right window.

This works in local, SSH, and mesh tabs alike: the notification is written as an
OSC escape sequence to the controlling terminal, so it rides the terminal stream
back to the originating tab — no network port, token, or tunnel involved. Under
tmux it is wrapped in tmux's DCS passthrough (and `allow-passthrough` is enabled
best-effort). If the session is not in a GenTerminal tab, the sequence is simply
ignored — archiving is unaffected.

### Session-title sync (GenTerminal sidebar)

The plugin also reports the session's display title over the same channel
(`TitleChanged`), which GenTerminal uses to name its sidebar "Sessions" rows
and tabs. The title is the **first `ai-title`** Claude Code generates — unless
you `/rename` the session, in which case **your explicit name wins** from then
on.

Timeliness: hook runs report a changed title within seconds during an active
turn, and a small **per-session watcher** (`scripts/title_watch.py`, spawned
automatically, POSIX only) covers the idle gaps — `/rename` fires no hook, so
without the watcher an idle rename would only land at your next prompt. The
watcher polls cheaply (stat fingerprints, ~1.5s), exits by itself when the
`claude` process ends, and never double-reports (it shares the hook reporter's
last-reported state).

`/clear` resets the label to the **working directory's basename** — the new,
empty conversation should not keep wearing the previous conversation's name.
Claude Code actually carries an explicit `/rename` name across the clear
boundary in its process registry; the plugin neutralizes exactly that
carried-over string (a fresh `/rename` still wins), and the new conversation's
own first ai-title replaces the directory label once it is generated.

Set `CC_ARCHIVE_NO_NOTIFY=1` to disable these notifications entirely (archiving
still runs as normal; the watcher is not spawned either).

### Reusing the notification mechanism

`scripts/notify.py` is tool-agnostic — any CLI tool can surface a GenTerminal
inbox notification the same way. The wire format is documented at the top of
that file (also parsed by GenTerminal's `utils/osc.ts`):

```
ESC ] 9999 ; <base64(JSON)> ESC \
```

From Python:

```python
import notify
notify.emit(source="my-tool", source_id="run-123",
            event="done", title="Build finished", body="42 tests passed",
            tmux=notify.tmux_context())
```

From a shell:

```sh
python3 path/to/notify.py --source my-tool --source-id run-123 \
    --title "Build finished" --body "42 tests passed"
```

`source_id` groups notifications: a new notification with the same `source_id`
on the same tab replaces the previous one in the inbox instead of stacking.
