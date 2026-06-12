# cc-conversation-archiver (local plugin marketplace)

A single-plugin [Claude Code plugin marketplace](https://code.claude.com/docs/en/plugin-marketplaces)
that distributes the **conversation-archiver** plugin: it archives each Claude
Code conversation turn (your input + Claude's text reply, excluding tool calls)
into a git repo as one markdown file per session, organized by month.

```
cc-conversation-archiver/
├── .claude-plugin/
│   └── marketplace.json                 # marketplace catalog
└── plugins/
    └── conversation-archiver/           # the plugin (see its README)
        ├── .claude-plugin/plugin.json
        ├── hooks/hooks.json             # UserPromptSubmit + SessionEnd
        ├── scripts/archive.py
        └── commands/{auto,manual,upload,status,connect}.md
```

## Install (local)

From a Claude Code session, point the marketplace at the absolute path of
**this** directory on your machine (the folder that contains
`.claude-plugin/marketplace.json`), then install the plugin:

```
/plugin marketplace add /ABSOLUTE/PATH/TO/gen-spark/toolkits/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

See [`plugins/conversation-archiver/README.md`](plugins/conversation-archiver/README.md)
for behavior, modes, filename rules, compaction handling, and auto-push setup.
