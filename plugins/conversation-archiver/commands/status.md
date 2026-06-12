---
description: Show the conversation archiver mode, repo path, and recent commits.
disable-model-invocation: true
allowed-tools: Bash
---

!`REPO=$(python3 -c "import json,os; p=os.path.expanduser('~/.claude/cc-conversation-archiver/config.json'); d=(json.load(open(p)) if os.path.exists(p) else {}); print(os.path.expanduser(os.environ.get('CC_ARCHIVE_REPO') or d.get('repo') or '~/claude-conversations'))"); echo "mode: $(python3 -c "import json,os; p=os.path.expanduser('~/.claude/cc-conversation-archiver/config.json'); d=(json.load(open(p)) if os.path.exists(p) else {}); print(os.environ.get('CC_ARCHIVE_MODE') or d.get('mode') or 'auto (default)')")"; echo "repo: $REPO"; echo "remote: $(git -C "$REPO" remote -v 2>/dev/null | head -1 || echo none)"; echo "--- recent commits ---"; git -C "$REPO" log --oneline -5 2>/dev/null || echo "(no commits yet)"; echo "--- pending changes ---"; git -C "$REPO" status -s 2>/dev/null | head`

Summarize the archiver status above for the user.
