#!/usr/bin/env bash
# SessionStart dependency check for the conversation-archiver plugin.
#
# Claude Code has no arbitrary-code gate at `/plugin install` time (by design),
# so the idiomatic place to verify runtime dependencies is a SessionStart hook —
# it runs whenever the plugin activates. This checks the two binaries the
# archive hook needs (python3 + git) and prints a visible warning if either is
# missing. It never fails the session (always exits 0).
#
# Written in bash on purpose: it must run even when python3 is absent.

# Persist the plugin root so slash commands (which don't reliably get
# ${CLAUDE_PLUGIN_ROOT}) can locate bundled scripts like archive.py.
appdir="$HOME/.claude/cc-conversation-archiver"
if [ -n "${CLAUDE_PLUGIN_ROOT:-}" ] && mkdir -p "$appdir" 2>/dev/null; then
  printf '%s' "$CLAUDE_PLUGIN_ROOT" > "$appdir/plugin_root" 2>/dev/null || true
fi

missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v git >/dev/null 2>&1 || missing+=("git")

if [ "${#missing[@]}" -gt 0 ]; then
  msg="[conversation-archiver] missing dependency: ${missing[*]} — conversation archiving is DISABLED until it is installed (the archive hook needs python3 and git on PATH)."
  echo "$msg"
  log="$HOME/.claude/cc-conversation-archiver/archive.log"
  if mkdir -p "$(dirname "$log")" 2>/dev/null; then
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" >> "$log" 2>/dev/null || true
  fi
fi

exit 0
