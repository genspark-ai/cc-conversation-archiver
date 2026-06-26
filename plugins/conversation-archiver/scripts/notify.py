#!/usr/bin/env python3
"""Generic GenTerminal app-notification emitter.

Pushes a notification into the GenTerminal app's in-app inbox by writing a
custom OSC 9999 escape sequence to the controlling terminal. Because the
sequence rides the terminal's own data stream, it works identically whether the
tool runs in a local tab, an SSH tab, or a mesh tab — GenTerminal receives it on
the originating tab and can jump back there (and switch tmux) when the user
clicks the notification. No network port, token, or reverse tunnel is involved.

This module is intentionally tool-agnostic so any CLI tool can reuse it:

    import notify
    notify.emit(source="my-tool", source_id="run-123",
                event="done", title="Build finished", body="42 tests passed",
                tmux=notify.tmux_context())

or from a shell:

    python3 notify.py --source my-tool --source-id run-123 \
        --title "Build finished" --body "42 tests passed"

Every call is best-effort: with no controlling tty (or if tmux passthrough can't
be arranged) it silently does nothing and never raises.

Wire protocol (also parsed by GenTerminal's utils/osc.ts):
    ESC ] 9999 ; <base64(JSON)> ESC \\
where JSON is {v, magic:"genterm-notify", source, sourceId, event, title,
body, tmux?}. tmux = {socket, session, windowId, windowIndex, windowName}.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys

OSC_PREFIX = "\033]9999;"
ST = "\033\\"
MAGIC = "genterm-notify"


def tmux_context() -> dict | None:
    """Capture the current tmux socket/session/window so the GenTerminal inbox
    can switch to it on click. Returns None when not running under tmux, the
    binary is missing, or the query fails — the field is then simply omitted."""
    if not os.environ.get("TMUX"):
        return None
    fmt = "#{socket_path}\t#S\t#{window_id}\t#{window_index}\t#{window_name}"
    try:
        res = subprocess.run(
            ["tmux", "display-message", "-p", fmt],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    parts = res.stdout.rstrip("\n").split("\t")
    if len(parts) < 5:
        return None
    socket, session, window_id, window_index, window_name = parts[:5]
    ctx: dict = {}
    if socket:
        ctx["socket"] = socket
    if session:
        ctx["session"] = session
    if window_id:
        ctx["windowId"] = window_id
    if window_index.isdigit():
        ctx["windowIndex"] = int(window_index)
    if window_name:
        ctx["windowName"] = window_name
    return ctx or None


def _wrap_for_tmux(seq: str) -> str:
    """Wrap an escape sequence in tmux's DCS passthrough so it reaches the outer
    terminal instead of being swallowed by tmux. Every ESC inside the payload
    must be doubled."""
    inner = seq.replace("\033", "\033\033")
    return "\033Ptmux;" + inner + "\033\\"


def _target_tty() -> str | None:
    """Best terminal device to write the sequence to.

    Claude Code runs hooks DETACHED from the controlling terminal, so /dev/tty
    is ENXIO ("Device not configured") inside a hook — we cannot rely on it.
    Resolution order:
      1. tmux: the current pane's tty (``#{pane_tty}``). Writing there feeds
         tmux's pane output, which forwards via passthrough to the attached
         client — works even with no controlling terminal.
      2. /dev/tty, when we actually have a controlling terminal (a tool invoked
         interactively, not as a detached hook).
      3. the controlling tty of an ancestor process (the shell / app pty), for a
         detached hook running in a non-tmux terminal.
    Returns the device path, or None if none could be resolved.
    """
    if os.environ.get("TMUX"):
        try:
            r = subprocess.run(["tmux", "display-message", "-p", "#{pane_tty}"],
                               capture_output=True, text=True, timeout=5)
            t = r.stdout.strip()
            if t:
                return t
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
    try:
        fd = os.open("/dev/tty", os.O_WRONLY | os.O_NOCTTY)
        os.close(fd)
        return "/dev/tty"
    except OSError:
        pass
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            tty = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if tty and tty not in ("??", "?", "-"):
                return tty if tty.startswith("/dev/") else "/dev/" + tty
            pid = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                                     capture_output=True, text=True, timeout=5).stdout.strip() or "1")
        except (FileNotFoundError, subprocess.SubprocessError, ValueError):
            break
    return None


def emit(source: str, source_id: str, event: str, title: str,
         body: str = "", tmux: dict | None = None) -> bool:
    """Emit one notification to the terminal. Returns True if the sequence was
    written, False otherwise (no usable tty, write error). Never raises."""
    if not source or not source_id or not title:
        return False
    payload: dict = {
        "v": 1,
        "magic": MAGIC,
        "source": source,
        "sourceId": source_id,
        "event": event,
        "title": title,
        "body": body,
    }
    if tmux:
        payload["tmux"] = tmux
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    seq = OSC_PREFIX + base64.b64encode(raw).decode("ascii") + ST

    if os.environ.get("TMUX"):
        # tmux drops unknown OSC sequences unless passthrough is enabled and the
        # sequence is wrapped in its DCS passthrough envelope. Enabling is
        # best-effort (and pane-scoped); the wrap is required.
        try:
            subprocess.run(
                ["tmux", "set", "-p", "allow-passthrough", "on"],
                capture_output=True, timeout=5,
            )
        except (FileNotFoundError, subprocess.SubprocessError):
            pass
        seq = _wrap_for_tmux(seq)

    target = _target_tty()
    if not target:
        return False
    try:
        # O_NOCTTY: never let writing to a tty make it our controlling terminal.
        fd = os.open(target, os.O_WRONLY | os.O_NOCTTY)
        try:
            os.write(fd, seq.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def _main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Emit a GenTerminal inbox notification.")
    p.add_argument("--source", required=True, help="tool identifier, e.g. my-tool")
    p.add_argument("--source-id", required=True,
                   help="stable id grouping notifications from one run/session")
    p.add_argument("--event", default="notify")
    p.add_argument("--title", required=True)
    p.add_argument("--body", default="")
    p.add_argument("--no-tmux", action="store_true",
                   help="do not attach tmux switch context")
    a = p.parse_args(argv)
    tmux = None if a.no_tmux else tmux_context()
    ok = emit(a.source, a.source_id, a.event, a.title, a.body, tmux)
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(_main(sys.argv[1:]))
    except Exception:
        # Never disrupt a caller that shells out to us.
        sys.exit(1)
