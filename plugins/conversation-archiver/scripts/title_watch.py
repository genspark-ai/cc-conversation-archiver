#!/usr/bin/env python3
"""Per-session title watcher daemon — the /rename immediacy fix.

`/rename` fires NO hook (verified against the full mid-2026 hook event list:
there is no SessionRename, and ConfigChange/FileChanged cover config and
workspace files, not ~/.claude session metadata). So the hook-driven reporter
(report_title.py) only delivers a manual rename on the NEXT hook event: within
seconds while a turn is actively making tool calls, but not until the next
prompt when the session is idle — which is exactly when humans rename
(2026-07-30 report: "sometimes immediate, sometimes next prompt").

This daemon closes the idle gap. report_title.ensure_watcher() spawns one
detached instance per session from the ordinary hook runs; the daemon polls
the two title sources every POLL_SECONDS and pushes a change through the same
notify.emit / state-file path the hook reporter uses, so the two paths never
double-report (shared last-reported marker, atomic replace; a lost race
re-emits an identical notification consumers already treat as a no-op).

Cheap by construction: each tick stats the session registry dir and the
transcript (no parsing); only a changed fingerprint re-reads anything. The
title recompute itself is archive.display_title — registry name first,
transcript ai-title else.

Exit conditions (all polled, no signals):
  - superseded: the pidfile no longer names this process (a newer watcher won
    a spawn race — newest wins, we bow out);
  - session gone: no live-pid registry entry for the session for
    PID_MISS_LIMIT consecutive liveness checks (claude exited, or /clear moved
    the process to a new session id);
  - MAX_LIFETIME as a leak backstop (the next hook run respawns).

POSIX only — the liveness probe is os.kill(pid, 0), which on Windows would
TERMINATE the target instead of probing it (same guard as
archive.user_session_name). ensure_watcher never spawns on non-POSIX.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import archive  # noqa: E402  (sibling module; import-safe, stdlib-only)
import notify  # noqa: E402
import report_title  # noqa: E402

POLL_SECONDS = 1.5
# Liveness is parsed JSON + kill(0) probes, heavier than the stat fingerprint,
# so it runs every LIVENESS_EVERY ticks (~6s). Exit needs PID_MISS_LIMIT
# consecutive misses (~24s total) so a registry rewrite window never kills a
# healthy watcher.
LIVENESS_EVERY = 4
PID_MISS_LIMIT = 4
MAX_LIFETIME_SECONDS = 7 * 24 * 3600


def live_session_pid(session_id: str) -> int | None:
    """Pid of a live process whose registry entry claims `session_id`."""
    try:
        entries = list(archive.SESSIONS_DIR.glob("*.json"))
    except Exception:
        return None
    for f in entries:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(e, dict) or e.get("sessionId") != session_id:
            continue
        pid = e.get("pid")
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                return pid
            except OSError:
                continue
    return None


def _fingerprint(transcript_path: Path) -> tuple:
    """Change detector: (name, mtime_ns, size) of every registry file plus the
    transcript. Stat-only — a tick with an unchanged fingerprint parses
    nothing. Registry rewrites and transcript appends both move it."""
    parts: list[tuple] = []
    try:
        for f in sorted(archive.SESSIONS_DIR.glob("*.json")):
            try:
                st = f.stat()
                parts.append((f.name, st.st_mtime_ns, st.st_size))
            except OSError:
                continue
    except Exception:
        pass
    try:
        st = transcript_path.stat()
        parts.append((transcript_path.name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(parts)


class Watch:
    """One session's watch state; `tick()` is the whole per-poll step,
    factored off the sleep loop so tests can drive it synchronously."""

    def __init__(self, session_id: str, transcript_path: Path):
        self.session_id = session_id
        self.transcript_path = transcript_path
        self.last_fp: tuple | None = None
        self.ticks = 0
        self.pid_misses = 0

    def _target_tty(self) -> str | None:
        """Freshest tty recorded by the hook runs (report_title refreshes
        watch_ttyfile on every invocation). Re-read before each emit — the
        spawn-time value can be missing (early SessionStart could not resolve
        one) and a later hook run may have filled it in. Outside tmux this is
        the daemon's ONLY usable target; under tmux, emit()'s own resolution
        still works from the inherited $TMUX env, so None degrades fine."""
        try:
            tty = report_title.watch_ttyfile(self.session_id).read_text(
                encoding="utf-8").strip()
            return tty or None
        except Exception:
            return None

    def _owns_pidfile(self) -> bool:
        try:
            raw = report_title.watch_pidfile(self.session_id).read_text(
                encoding="utf-8").strip()
            return int(raw) == os.getpid()
        except Exception:
            # Missing/garbled pidfile: keep running — the spawner recreates it
            # and a stale watcher still exits via the liveness check.
            return True

    def _report_once(self) -> None:
        title = archive.display_title(self.session_id, self.transcript_path)
        if not title or title == report_title.last_reported(self.session_id):
            return
        emitted = notify.emit(
            source="conversation-archiver",
            source_id=self.session_id,
            event="TitleChanged",
            title=title,
            body=report_title._body(self.session_id, False),
            tmux=notify.tmux_context(),
            target_tty=self._target_tty(),
        )
        if emitted:
            report_title.remember(self.session_id, title)
        else:
            # No usable tty this tick — clear the fingerprint so the next
            # tick retries instead of waiting for another source change.
            self.last_fp = None

    def tick(self) -> str | None:
        """One poll step. Returns an exit reason, or None to keep running."""
        if not self._owns_pidfile():
            return "superseded"
        self.ticks += 1
        if self.ticks % LIVENESS_EVERY == 0:
            if live_session_pid(self.session_id) is None:
                self.pid_misses += 1
                if self.pid_misses >= PID_MISS_LIMIT:
                    return "session-exited"
            else:
                self.pid_misses = 0
        fp = _fingerprint(self.transcript_path)
        if fp != self.last_fp:
            self.last_fp = fp
            self._report_once()
        return None


def run(session_id: str, transcript_path: Path) -> None:
    watch = Watch(session_id, transcript_path)
    deadline = time.monotonic() + MAX_LIFETIME_SECONDS
    try:
        while time.monotonic() < deadline:
            if watch.tick() is not None:
                return
            time.sleep(POLL_SECONDS)
    finally:
        # Best-effort: drop the pidfile only if it is still ours, so a
        # successor's file is never removed.
        try:
            pf = report_title.watch_pidfile(session_id)
            if int(pf.read_text(encoding="utf-8").strip()) == os.getpid():
                pf.unlink()
        except Exception:
            pass


def main() -> None:
    if os.name != "posix" or os.environ.get("CC_ARCHIVE_NO_NOTIFY"):
        return
    p = argparse.ArgumentParser()
    p.add_argument("--session-id", required=True)
    p.add_argument("--transcript", required=True)
    a = p.parse_args()
    run(a.session_id, Path(a.transcript))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
