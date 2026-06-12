# Releasing the conversation-archiver plugin

The plugin is developed here in the gen-spark monorepo
(`toolkits/cc-conversation-archiver/**`) and **published** to a dedicated public
marketplace repo that users add to Claude Code:

> **Release address:** https://github.com/genspark-ai/cc-conversation-archiver

Users install with:

```
/plugin marketplace add genspark-ai/cc-conversation-archiver
/plugin install conversation-archiver@cc-conversation-archiver
```

and update with `claude plugin update conversation-archiver@cc-conversation-archiver`
(Claude Code pulls the latest from that repo's `main`). Why a separate public
repo rather than a backend `checkupdate` endpoint: a Claude Code plugin is not a
self-updating binary — the plugin system distributes and updates plugins from a
**git marketplace** itself, so the right "release address" is a public git repo,
not a CDN pointer like the desktop clients use.

## One-time setup

1. **Create the public repo** `genspark-ai/cc-conversation-archiver` (empty, public).
   Its content is produced entirely by the release workflow — do not hand-edit it.
2. **Mint a token** with `Contents: write` on that repo (a fine-grained PAT scoped
   to just `genspark-ai/cc-conversation-archiver`, or a GitHub App installation
   token). Cross-org pushes can't use the default `GITHUB_TOKEN`.
3. **Add it as an Actions secret** in the gen-spark repo named
   `CC_ARCHIVER_PUBLISH_TOKEN` (Settings → Secrets and variables → Actions).

## Cutting a release

1. **Bump the version in a normal PR.** Edit `version` in
   `plugins/conversation-archiver/.claude-plugin/plugin.json` (semver), get it
   reviewed, and merge to `main`. The release workflow never bumps the version
   itself — every release is an ordinary reviewed change.
2. **Run the workflow.** Actions → **CC Archiver Release** → *Run workflow*
   (optionally tick *dry run* first to stage + validate without publishing).

   It mirrors the plugin tree to the public repo's `main` (the plugin's
   `.claude-plugin/marketplace.json` becomes the repo-root marketplace manifest),
   tags it `conversation-archiver--v<version>`, and cuts a matching GitHub
   Release. `tests/`, `__pycache__/`, and `*.pyc` are excluded from the published
   tree.

The workflow **refuses to run if a GitHub Release for
`conversation-archiver--v<version>` already exists** on the public repo — that's
the guard that forces step 1 (a version bump) before each release. (It keys on
the published Release rather than the tag, so a rerun after a partial failure —
e.g. the tag got pushed but the release wasn't created — can still finish.)

It must be dispatched from `main` (it hard-fails otherwise and always checks out
`main`), so an unmerged version bump on a feature branch can never be published.

## Notes

- The published tag scheme `conversation-archiver--v<version>` matches what
  `claude plugin tag` produces, so the public repo's tags line up with the
  Claude Code plugin tooling.
- The source of truth is always this monorepo. The public repo is a
  publish target; reconcile drift by re-running the workflow, never by editing
  the public repo directly.
