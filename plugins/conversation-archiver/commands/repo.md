---
description: Change where the conversation archive lives (the local git repo path). Repoint-only — the existing archive at the old path is left untouched; new turns archive to the new path. Pass an absolute path or ~/path.
disable-model-invocation: true
allowed-tools: Bash
argument-hint: <absolute path or ~/path for the archive repo>
---

!`ROOT=$(cat "$HOME/.claude/cc-conversation-archiver/plugin_root" 2>/dev/null); if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then echo "python3 and git are required"; elif [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/archive.py" ]; then echo "plugin not initialized yet — start a session with the plugin enabled first (SessionStart records its path)"; else python3 "$ROOT/scripts/archive.py" --set-repo "$ARGUMENTS"; fi`

Report the result above to the user in one short sentence. If it printed a
WARNING or NOTE line, surface that too.
