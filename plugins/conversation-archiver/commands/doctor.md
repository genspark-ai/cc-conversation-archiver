---
description: Diagnose the conversation-archiver setup — checks python3/git, config, the archive repo, remote auth (a read-only ls-remote connection test), sync state, and recent errors, then reports problems with fixes. Use when archiving isn't working, a push isn't landing, or to verify a remote right after connecting it.
allowed-tools: Bash
argument-hint: (none)
---

!`ROOT=$(cat "$HOME/.claude/cc-conversation-archiver/plugin_root" 2>/dev/null); if ! command -v python3 >/dev/null 2>&1; then echo "python3 is required but not on PATH — conversation archiving is disabled until it is installed."; elif [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/archive.py" ]; then echo "plugin not initialized yet — start a Claude Code session with the plugin enabled first (SessionStart records its path), then re-run."; else python3 "$ROOT/scripts/archive.py" --doctor; fi`

Read the doctor report above and explain it to the user in plain language:
briefly summarize what's healthy, then for every problem or note it lists, give
the exact next step or command to resolve it (reference the README's step 3 for
remote setup, `/conversation-archiver:connect` for Second Brain, or
`/conversation-archiver:backfill` for pre-install sessions as appropriate). If
everything passed, say so in one line. Never print any credential/token value.
