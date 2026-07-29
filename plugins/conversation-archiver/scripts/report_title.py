#!/usr/bin/env python3
"""Fast-path session-title reporter.

GenTerminal's sidebar Sessions section renames a managed tmux session record
from the archiver's OSC 9999 notifications (title = the conversation's
ai-title, tmux.session = the record's tmux name). The archive hook only fires
on turn boundaries (Stop / SessionEnd / UserPromptSubmit), so a title that
Claude Code generates seconds into the FIRST turn used to reach the sidebar
only when that turn ended — for long agentic turns, many minutes later
(2026-07-29 user report: "report immediately when the session name changes").

This script is the immediacy path. It is registered on SessionStart,
UserPromptSubmit and PostToolUse (PostToolUse fires after every tool call, so
an active turn re-checks the title every few seconds) and does the minimum:
read the hook payload, scan the transcript for the FIRST ai-title (same
first-wins semantics as archive.session_title — the stale-after-/clear fix),
and emit a notification when it changed. No git, no archive lock, no repo
config: the whole run is one transcript scan plus at most one tty write.

State: the last REPORTED title lives in its own tiny file
(state/<sid>.title), deliberately NOT in the archiver's per-session state
JSON — that file is read-modify-written under the archive lock, and a
lock-free writer here could lose archived blocks in a concurrent update.
A race between two hook runs at worst re-emits an identical notification,
which every consumer treats as a no-op (GenTerminal skips same-name renames;
the inbox upserts by source/sourceId).

SessionStart with source "startup"/"resume" re-reports an existing title even
unchanged: the receiving app may have restarted or another device may hold a
stale record, and one notification per claude launch is cheap insurance that
heals the sidebar on attach without waiting for the next turn. SessionStart
also fires for "compact"/"clear" mid-session — those take the changed-title
path so a compaction never pushes a spurious "Session resumed" notification.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import archive  # noqa: E402  (sibling module; import-safe, stdlib-only)


def title_state_file(session_id: str) -> Path:
    return archive.STATE_DIR / f"{session_id}.title"


def last_reported(session_id: str) -> str | None:
    try:
        value = title_state_file(session_id).read_text(encoding="utf-8").strip()
        return value or None
    except Exception:
        return None


def remember(session_id: str, title: str) -> None:
    """Record `title` as reported. Also called by archive._maybe_notify after
    its own Stop/SessionEnd notification so the fast path does not re-report
    a title the slow path just delivered."""
    try:
        archive.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = title_state_file(session_id).with_suffix(".title.tmp")
        tmp.write_text(title, encoding="utf-8")
        tmp.replace(title_state_file(session_id))
    except Exception:
        pass


def forget(session_id: str) -> None:
    """Drop the reported-title marker so the next hook run re-reports."""
    try:
        title_state_file(session_id).unlink(missing_ok=True)
    except Exception:
        pass


def _body(session_id: str, resumed: bool) -> str:
    """Inbox-friendly body, consistent with the archive notifications
    ("Turn complete · N turns archived"). Reads the archiver state read-only;
    a missing state (title arrived before the first archive run) degrades to
    a countless label."""
    try:
        state = archive.load_state(session_id)
        turns = sum(1 for b in state.get("blocks", []) if b.get("role") == "user")
    except Exception:
        turns = 0
    label = "Session resumed" if resumed else "Title updated"
    if turns <= 0:
        return label
    unit = "turn" if turns == 1 else "turns"
    return f"{label} · {turns} {unit} archived"


def report(payload: dict) -> bool:
    """One fast pass. Returns True when a notification was emitted."""
    if os.environ.get("CC_ARCHIVE_NO_NOTIFY"):
        return False
    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    if not session_id or not transcript_path:
        return False
    tpath = Path(transcript_path)
    if not tpath.exists():
        return False
    # A subagent sidechain transcript has no ai-title of its own — the title
    # lives in the main session transcript, same redirect the archiver does.
    if archive._is_sidechain_transcript(tpath):
        tpath = archive._main_transcript_for(session_id)
        if tpath is None:
            return False

    title = archive.session_title(tpath)
    if not title:
        return False

    event = payload.get("hook_event_name", "?")
    # The unconditional heal is for a fresh process looking at a possibly
    # stale consumer: SessionStart source "startup"/"resume" (and, defensive
    # default, hook payloads too old to carry `source`). SessionStart ALSO
    # fires for "compact" and "clear" — mid-session events that would
    # otherwise re-push a misleading "Session resumed" notification on every
    # compaction; those take the ordinary changed-title path instead.
    resumed = event == "SessionStart" and payload.get("source", "startup") in (
        "startup",
        "resume",
    )
    if not resumed and title == last_reported(session_id):
        return False

    try:
        import notify  # noqa: PLC0415  (sibling module, lazy like archive does)
    except Exception:
        return False
    emitted = notify.emit(
        source="conversation-archiver",
        source_id=session_id,
        event="TitleChanged",
        title=title,
        body=_body(session_id, resumed),
        tmux=notify.tmux_context(),
    )
    if emitted:
        # Only mark on success: with no tty yet (early SessionStart), the next
        # hook run retries instead of going silent for the whole session.
        remember(session_id, title)
    elif resumed:
        # The heal case is the one failure the success-only remember above
        # does NOT cover: the title usually EQUALS the remembered one (from a
        # previous process), so after a failed SessionStart emit every later
        # PostToolUse/UserPromptSubmit would return early on the unchanged-
        # title check and the heal would never retry. Forget the marker so
        # the next hook run re-reports.
        forget(session_id)
    return emitted


def main() -> None:
    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}
    report(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)  # never disrupt the session
