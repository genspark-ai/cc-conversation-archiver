---
description: Connect the archiver to your Second Brain. Paste the connect link from the Claude Code tile on your Second Brain home (one-time code; the script redeems it for the push target — no token ever shown). No args falls back to your gsk login credential.
disable-model-invocation: true
allowed-tools: Bash
argument-hint: <sb-connect link from the website> (or empty to use gsk login)
---

!`ROOT=$(cat "$HOME/.claude/cc-conversation-archiver/plugin_root" 2>/dev/null); if ! command -v python3 >/dev/null 2>&1 || ! command -v git >/dev/null 2>&1; then echo "python3 and git are required"; elif [ -z "$ROOT" ] || [ ! -f "$ROOT/scripts/archive.py" ]; then echo "plugin not initialized yet — start a session with the plugin enabled first (SessionStart records its path)"; else python3 "$ROOT/scripts/archive.py" --connect $ARGUMENTS; fi`

Report the connect result above to the user in one short sentence. Never echo
the token back to the user.
