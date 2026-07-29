---
description: Show the conversation archiver mode, repo path, and recent commits.
disable-model-invocation: true
allowed-tools: Bash
---

!`ROOT=$(cat "$HOME/.claude/cc-conversation-archiver/plugin_root" 2>/dev/null); if ! command -v python3 >/dev/null 2>&1; then echo "python3 is required but not on PATH — conversation archiving is disabled until it is installed."; elif [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/archive.py" ]; then echo "plugin not initialized yet — start a Claude Code session with the plugin enabled first (SessionStart records its path), then re-run."; else python3 "$ROOT/scripts/archive.py" --status; fi`

Summarize the archiver status above for the user.
