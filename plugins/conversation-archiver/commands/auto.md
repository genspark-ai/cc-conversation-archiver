---
description: Switch the conversation archiver to AUTO mode (commit + push every turn).
disable-model-invocation: true
allowed-tools: Bash
---

!`python3 -c "import json,os; p=os.path.expanduser('~/.claude/cc-conversation-archiver/config.json'); os.makedirs(os.path.dirname(p),exist_ok=True); d=(json.load(open(p)) if os.path.exists(p) else {}); d['mode']='auto'; json.dump(d,open(p,'w')); print('conversation-archiver mode -> auto; repo:', d.get('repo') or '~/claude-conversations (default)')"`

The conversation archiver is now in **AUTO** mode. From now on, every time a turn
finishes, the per-session markdown file is written, committed, and pushed to the
archive repo. (The `mode` field is merged into the existing config, so any custom
`repo` path is preserved.) Confirm this to the user in one short sentence.
