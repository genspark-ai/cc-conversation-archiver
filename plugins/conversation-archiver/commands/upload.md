---
description: Commit and push the entire conversation archive to the remote now.
disable-model-invocation: true
allowed-tools: Bash
---

!`ROOT=$(cat "$HOME/.claude/cc-conversation-archiver/plugin_root" 2>/dev/null); if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then echo "python3 and git are required"; elif [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/archive.py" ]; then echo "plugin not initialized yet — start a session with the plugin enabled first (SessionStart records its path)"; else python3 "$ROOT/scripts/archive.py" --upload; fi`

Report the upload result above to the user in one short sentence.
