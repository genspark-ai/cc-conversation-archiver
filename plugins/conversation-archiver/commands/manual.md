---
description: Switch the conversation archiver to MANUAL mode (write locally; upload on demand).
disable-model-invocation: true
allowed-tools: Bash
---

!`python3 -c "import json,os; p=os.path.expanduser('~/.claude/cc-conversation-archiver/config.json'); os.makedirs(os.path.dirname(p),exist_ok=True); d=(json.load(open(p)) if os.path.exists(p) else {}); d['mode']='manual'; json.dump(d,open(p,'w')); print('conversation-archiver mode -> manual; repo:', d.get('repo') or '~/claude-conversations (default)')"`

The conversation archiver is now in **MANUAL** mode. Each turn still updates the
per-session markdown file locally (so nothing is lost), but commits and pushes are
NOT made automatically. Run `/conversation-archiver:upload` to commit + push the
whole archive when you want. (The `mode` field is merged into the existing config,
so any custom `repo` path is preserved.) Confirm this to the user in one short sentence.
