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
read the hook payload, resolve the display title (a manual /rename from the
process registry first — archive.user_session_name — else the transcript's
FIRST ai-title, the first-wins stale-after-/clear fix), and emit a
notification when it changed. No git, no archive lock, no repo config: the
whole run is one registry scan + one transcript scan plus at most one tty
write.

/rename itself fires NO hook, so the hook cadence alone delivered a manual
rename within seconds only during an active turn (PostToolUse) but not until
the NEXT PROMPT when the session was idle — which is when humans actually
rename (2026-07-30 report: "sometimes immediate, sometimes next prompt").
ensure_watcher() below closes that gap: every hook run keeps a per-session
title_watch.py daemon alive that polls the title sources and reports a change
within ~2s regardless of hook timing. Watcher and hook share the
last-reported state file, so they never double-report.

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


def watch_pidfile(session_id: str) -> Path:
    """Pidfile of the session's title watcher daemon (title_watch.py). Written
    ONLY by the spawner below; the daemon reads it and exits when a newer
    watcher's pid replaces its own (spawn races converge to newest-wins)."""
    return archive.STATE_DIR / f"{session_id}.watch"


def watch_ttyfile(session_id: str) -> Path:
    """Freshest resolvable target tty for the session's watcher daemon.

    The daemon itself cannot resolve a tty outside tmux (setsid: no
    controlling terminal, reparented to init so no useful ancestor chain), and
    resolution CAN fail in the hook that happens to spawn the watcher (early
    SessionStart). So the tty is decoupled from spawn time: EVERY hook run
    refreshes this file when it can resolve a tty, and the daemon re-reads it
    before each emit — a watcher born tty-less starts emitting the moment any
    later hook run resolves one (Bugbot finding on the first cut: the spawn-
    time --tty argv froze a missing tty for the daemon's whole lifetime)."""
    return archive.STATE_DIR / f"{session_id}.tty"


def _refresh_watch_tty(session_id: str) -> None:
    """Best-effort: record the currently-resolvable target tty for the
    daemon. Failures (no tty this run, unwritable state dir) leave the
    previous value in place — never raise into the hook."""
    try:
        import notify  # noqa: PLC0415

        tty = notify._target_tty()
        if not tty:
            return
        tf = watch_ttyfile(session_id)
        try:
            if tf.read_text(encoding="utf-8").strip() == tty:
                return
        except Exception:
            pass
        archive.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = tf.with_suffix(".tty.tmp")
        tmp.write_text(tty, encoding="utf-8")
        tmp.replace(tf)
    except Exception:
        pass


def _spawn_watcher(argv: list[str]) -> int | None:
    """Start the watcher fully detached (own session, no inherited stdio) and
    return its pid. Split out so tests can stub the actual process launch."""
    import subprocess  # noqa: PLC0415

    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def ensure_watcher(session_id: str, transcript_path: Path) -> bool:
    """Keep one title_watch.py daemon alive for the session; returns True when
    a new one was spawned.

    /rename fires no hook, so the hook-driven report() above only delivers a
    manual rename on the NEXT hook event — next prompt, when the session is
    idle. The daemon polls the title sources and closes that gap; every hook
    run re-ensures it (cheap pidfile + kill(0) probe) so a crashed or
    lifetime-expired watcher heals on the session's next activity.

    The target tty travels through watch_ttyfile, refreshed on EVERY hook
    run — not just at spawn — because the run that spawns the watcher may be
    unable to resolve one (early SessionStart) while a later run can.
    """
    if os.name != "posix":
        # Liveness probing is kill(0)-based; on Windows that terminates the
        # probed process. The hook cadence remains the only reporter there.
        return False
    _refresh_watch_tty(session_id)
    pf = watch_pidfile(session_id)
    try:
        os.kill(int(pf.read_text(encoding="utf-8").strip()), 0)
        return False  # a watcher is alive
    except Exception:
        pass  # missing/garbled pidfile or dead pid — spawn a fresh one
    try:
        argv = [
            sys.executable,
            str(SCRIPTS / "title_watch.py"),
            "--session-id", session_id,
            "--transcript", str(transcript_path),
        ]
        pid = _spawn_watcher(argv)
        if pid is None:
            return False
        archive.STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = pf.with_suffix(".watch.tmp")
        tmp.write_text(str(pid), encoding="utf-8")
        tmp.replace(pf)
        return True
    except Exception:
        return False


def _report_clear(session_id: str, payload: dict) -> bool:
    """/clear reset (2026-07-30 request): the new, empty conversation must
    not keep wearing the previous conversation's name. Two steps:

    1. Snapshot the registry name Claude Code carries across the boundary —
       archive.effective_user_session_name ignores exactly that string for
       this session from now on, so the old explicit name cannot outrank the
       new conversation's ai-title (it otherwise would, forever).
    2. Report the working directory's basename as the fresh label. The
       conversation's own first ai-title replaces it later through the
       ordinary changed-title path.
    """
    stale = archive.user_session_name(session_id)
    if stale:
        archive.remember_stale_name(session_id, stale)
    # Hook payloads normally carry cwd, but older payload shapes omit fields
    # (see the `source` default above) — the hook process itself runs in the
    # project directory, so its own cwd is an equivalent fallback (Bugbot).
    cwd = payload.get("cwd") or os.getcwd()
    title = Path(cwd).name.strip()
    if not title:
        return False
    # Persist BEFORE emitting: as the lowest-priority display_title source,
    # a label whose emit fails here (no tty yet) is delivered by the next
    # hook run / watcher tick through the ordinary changed-title path.
    archive.remember_clear_label(session_id, title)
    if title == last_reported(session_id):
        return False
    try:
        import notify  # noqa: PLC0415  (sibling module, lazy like report())
    except Exception:
        return False
    emitted = notify.emit(
        source="conversation-archiver",
        source_id=session_id,
        event="TitleChanged",
        title=title,
        body="Session cleared",
        tmux=notify.tmux_context(),
    )
    if emitted:
        remember(session_id, title)
    return emitted


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
    # A subagent sidechain transcript has no ai-title of its own — the title
    # lives in the main session transcript, same redirect the archiver does.
    # (A sidechain payload whose main transcript cannot be found is the one
    # case that spawns no watcher — the main session's own hooks do that.)
    if tpath.exists() and archive._is_sidechain_transcript(tpath):
        tpath = archive._main_transcript_for(session_id)
        if tpath is None:
            return False

    # Keep the per-session watcher daemon alive BEFORE the transcript-exists
    # gate below: a brand-new session's SessionStart fires before the
    # transcript is written, and an idle /rename needs only the process
    # registry — the daemon tolerates a missing transcript and picks it up
    # once it appears (Bugbot).
    ensure_watcher(session_id, tpath)

    event = payload.get("hook_event_name", "?")
    # /clear takes its own path: reset the label to the working directory's
    # basename and neutralize the carried-over registry name.
    if event == "SessionStart" and payload.get("source") == "clear":
        return _report_clear(session_id, payload)

    if not tpath.exists():
        return False

    # Explicit /rename outranks the transcript's ai-title (they are separate
    # channels — see archive.user_session_name). Without this, a manual
    # rename never reached consumers at all.
    title = archive.display_title(session_id, tpath)
    if not title:
        return False

    # The unconditional heal is for a fresh process looking at a possibly
    # stale consumer: SessionStart source "startup"/"resume" (and, defensive
    # default, hook payloads too old to carry `source`). SessionStart for
    # "clear" was handled above; "compact" is a mid-session event that would
    # otherwise re-push a misleading "Session resumed" notification on every
    # compaction, so it takes the ordinary changed-title path instead.
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
