#!/usr/bin/env python3
"""Claude Code conversation archiver — Stop / SubagentStop / UserPromptSubmit / SessionEnd hook.

Reads the hook payload from stdin (session_id, transcript_path, ...), parses the
session transcript, extracts the user inputs and Claude's *text* replies (tool
calls / tool results / thinking are excluded), and maintains ONE markdown file
per session in a git archive repo, organized by month:

    <repo>/<YYYY-MM>/<YYYY-MM-DD-HHMM>-<session-name>.md

Design goals (per requirements):
  * One session  ->  exactly one file (no duplicates). The file is keyed on the
    stable session_id, so context compaction (which keeps the same session_id
    and transcript path) never forks it into a second file.
  * Content is NEVER deleted. We accumulate every turn into a local, git-ignored
    state file (keyed by message uuid) and rebuild the markdown from that state.
    Even if a future Claude Code version were to truncate the on-disk transcript
    after compaction, already-archived turns survive in our state.
  * Context-compaction boundaries are recorded as a divider in the markdown.
    (Claude Code does NOT persist the compaction *summary text* to disk, only a
    `compact_boundary` marker with token metadata, so that is what we surface.)

Modes (switched via the /conversation-archiver:* commands, stored in config):
  * "auto"   (default): each turn -> write file, git commit, git push (background)
  * "manual"          : each turn -> write file only; commit+push happens when the
                        user runs /conversation-archiver:upload

The hook always exits 0 so it can never disrupt the session; errors go to the log.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths & config
# --------------------------------------------------------------------------- #

HOME = Path.home()
APP_DIR = HOME / ".claude" / "cc-conversation-archiver"
CONFIG_PATH = APP_DIR / "config.json"
STATE_DIR = APP_DIR / "state"
INDEX_PATH = STATE_DIR / "_index.json"   # rel-path -> session_id (collision guard)
LOG_PATH = APP_DIR / "archive.log"
PUSH_LOG = APP_DIR / "push.log"
PUSH_LOCK = APP_DIR / "push.lock"
ARCHIVE_LOCK = APP_DIR / "archive.lock"  # serializes concurrent hook runs

DEFAULT_REPO = HOME / "claude-conversations"
MAX_SLUG_LEN = 60

# Second Brain integration: when connected (``--connect``), archives are
# written under this subfolder of the user's /memo vault repo so the push
# never collides with the vault's own content. Must match the backend's
# ``_CLAUDE_CODE_SUBFOLDER`` (backend/memo_v2/mounts.py).
SB_DEFAULT_SUBDIR = "claude-code"
CRED_FILE = APP_DIR / "git-credentials"

# Second Brain backend base URL + the push-target resolver. The plugin
# self-resolves the vault push URL from the user's gsk token (so the connect
# command needs no pasted URL). Overridable via GSK_BASE_URL for local dev.
SB_BASE_URL = os.environ.get("GSK_BASE_URL", "https://www.genspark.ai").rstrip("/")
SB_RESOLVE_PATH = "/api/memo_v2/sources/claude-code/resolve"
# Where `gsk login` stores the token (single ``api_key`` field). The plugin
# reuses this credential so the user never pastes a token.
GSK_CLI_CONFIG = HOME / ".genspark-tool-cli" / "config.json"


def log(msg: str) -> None:
    try:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_repo(cfg: dict) -> Path:
    repo = os.environ.get("CC_ARCHIVE_REPO") or cfg.get("repo")
    return Path(repo).expanduser() if repo else DEFAULT_REPO


def get_mode(cfg: dict) -> str:
    mode = (os.environ.get("CC_ARCHIVE_MODE") or cfg.get("mode") or "auto").lower()
    return mode if mode in ("auto", "manual") else "auto"


def get_subdir(cfg: dict) -> str:
    """Repo-relative subfolder all archive files live under ('' = repo root).

    Set by ``--connect`` (Second Brain mode): the remote is the user's /memo
    vault repo, and the archive is confined to ``claude-code/`` so it can
    never touch the vault's own files.
    """
    sub = (cfg.get("subdir") or "").strip().strip("/")
    return sub


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


# --------------------------------------------------------------------------- #
# Transcript parsing
# --------------------------------------------------------------------------- #

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
# A slash-command invocation arrives as "user" content carrying the command name
# and the typed args in separate tags (their order varies). The skill/command
# body it expands into arrives as a separate isMeta entry we already skip.
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
# Other command / local-command plumbing (the local `!cmd` caveat + stdout, or a
# bare wrapper with no command-name) is machinery, not the user's input.
_COMMAND_WRAPPER_RE = re.compile(r"^\s*<(command|local-command)-[a-z-]+>")


def _clean_user_text(content) -> str | None:
    """Return the human-typed text of a user entry, or None if it is not real input.

    User entries come in two shapes:
      * a plain string  -> a typed prompt (what we want)
      * a list of blocks -> usually tool_result blocks (NOT user input; skip), but
        may contain text blocks (e.g. a message with an attached image).
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        text = "\n\n".join(p for p in parts if p.strip())
    else:
        return None

    text = _SYSTEM_REMINDER_RE.sub("", text).strip()
    if not text:
        return None
    if "<command-name>" in text:
        # A slash command IS the user's input. This entry used to be dropped
        # entirely, which erased the opening turn of any command-initiated
        # session — the archive then started at the assistant's reply. Keep it
        # as the typed "/command args" line (args carry the real question).
        name_m = _COMMAND_NAME_RE.search(text)
        args_m = _COMMAND_ARGS_RE.search(text)
        name = name_m.group(1).strip() if name_m else ""
        args = args_m.group(1).strip() if args_m else ""
        if name == "/clear":
            # /clear is session plumbing, never input: Claude Code seeds its
            # record into the NEW session's transcript, so keeping it archived
            # a bare "/clear" turn (or a whole junk file) into every cleared
            # session.
            return None
        return f"{name} {args}".strip() or None
    if _COMMAND_WRAPPER_RE.match(text):
        return None
    return text


def _assistant_text(content) -> str | None:
    """Return only the assistant's visible text (drop thinking / tool_use)."""
    if not isinstance(content, list):
        return None
    parts = [
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    text = "\n\n".join(p for p in parts if p.strip()).strip()
    return text or None


def _entry_key(entry: dict, role: str, text: str) -> str:
    uuid = entry.get("uuid")
    if uuid:
        return str(uuid)
    digest = hashlib.sha1(f"{role}:{text}".encode("utf-8")).hexdigest()[:16]
    return f"h:{digest}"


def _compact_divider(entry: dict) -> str:
    meta = entry.get("compactMetadata") or {}
    trigger = meta.get("trigger", "?")
    pre = meta.get("preTokens")
    post = meta.get("postTokens")
    detail = f"{trigger}"
    if pre is not None and post is not None:
        detail += f", {pre:,}→{post:,} tokens"
    return (
        f"> \U0001f5dc️ **Context compacted** ({detail}). "
        "Earlier turns above are preserved in this archive; "
        "Claude Code does not persist the compaction summary text to disk."
    )


def _queued_prompt(entry: dict) -> str | None:
    """The human-typed text of a queued (mid-turn) message, or None.

    A message sent while Claude is still working is NOT recorded as a
    ``type: "user"`` entry — only as a ``queue-operation`` line plus an
    ``attachment`` of type ``queued_command`` carrying the prompt. Without
    handling it, every mid-turn user message silently vanishes from the
    archive."""
    if entry.get("type") != "attachment":
        return None
    att = entry.get("attachment") or {}
    if att.get("type") != "queued_command":
        return None
    if (att.get("origin") or {}).get("kind") != "human":
        return None
    prompt = (att.get("prompt") or "").strip()
    return prompt or None


def _content_entries(path: Path):
    """Yield ordered (entry, role, text) for every archivable block.

    role is one of: "user", "assistant", "compact". Shared by
    parse_transcript (block extraction) and session_start (first-content
    timestamp) so both agree on what counts as content.

    A message typed while Claude is still working lands as a
    ``queued_command`` attachment; a message submitted normally lands as a
    ``type: "user"`` entry — one shape per message, never both (verified
    across 34 real queued prompts: zero reappeared as a user entry). So each
    shape is archived as a user turn with NO cross-shape text deduplication:
    text-based suppression would drop a genuine turn whenever the user
    re-sends the same words (queued or typed) later in the session.
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            etype = entry.get("type")
            if etype == "user":
                if entry.get("isMeta"):
                    continue
                text = _clean_user_text((entry.get("message") or {}).get("content"))
                if text:
                    yield entry, "user", text
            elif etype == "attachment":
                text = _queued_prompt(entry)
                if text:
                    yield entry, "user", text
            elif etype == "assistant":
                text = _assistant_text((entry.get("message") or {}).get("content"))
                if text:
                    yield entry, "assistant", text
            elif etype == "system" and entry.get("subtype") == "compact_boundary":
                yield entry, "compact", _compact_divider(entry)


def parse_transcript(path: Path):
    """Yield ordered (key, role, text) blocks for new content."""
    for entry, role, text in _content_entries(path):
        yield _entry_key(entry, role, text), role, text


# Claude Code's per-process session registry. Each running (or exited)
# process leaves a <pid>.json carrying sessionId, name and nameSource.
SESSIONS_DIR = HOME / ".claude" / "sessions"


def user_session_name(session_id: str) -> str | None:
    """The EXPLICITLY-set session name from Claude Code's process registry
    (~/.claude/sessions/<pid>.json), or None.

    `/rename` writes ONLY here — it sets `name` and REMOVES the
    `nameSource: "derived"` marker; nothing about a manual rename ever
    reaches the transcript, whose ai-title entries are a separate channel
    (verified live 2026-07-29: after /rename the registry held the new name
    while all 180 ai-title stamps kept the old auto title). Conversely the
    ai-title never flows into the registry. Consumers that want "what the
    user calls this session" therefore need BOTH sources, explicit name
    first.

    A name with nameSource "derived" is the automatic directory label
    ("dir-xx") and must NEVER be surfaced as a display title — that was the
    original GenTerminal name-sync bug. Multiple registry files can share a
    session_id (a resumed session leaves the previous process's file
    behind): prefer a live process, newest updatedAt as the tiebreak. The
    liveness probe is POSIX-only — on Windows os.kill(pid, 0) TERMINATES
    the target instead of probing it.
    """
    best: tuple[bool, int, str] | None = None
    try:
        entries = list(SESSIONS_DIR.glob("*.json"))
    except Exception:
        return None
    for f in entries:
        try:
            e = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(e, dict) or e.get("sessionId") != session_id:
            continue
        name = (e.get("name") or "").strip()
        if not name or e.get("nameSource") == "derived":
            continue
        alive = False
        pid = e.get("pid")
        if os.name == "posix" and isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        updated = e.get("updatedAt")
        rank = (alive, updated if isinstance(updated, int) else 0, name)
        if best is None or rank[:2] > best[:2]:
            best = rank
    return best[2] if best else None


def display_title(session_id: str, transcript_path: Path) -> str | None:
    """Title to surface to consumers (GenTerminal's Sessions sidebar): the
    user's explicit name when one exists, else the transcript's ai-title.
    Once a session has been manually renamed, later ai-title re-stamps no
    longer override it. The ARCHIVE document keeps using the ai-title
    (session_title) — the markdown title is a stable content identifier,
    not a UI label."""
    return user_session_name(session_id) or session_title(transcript_path)


def session_title(path: Path) -> str | None:
    """FIRST ai-title in the transcript, if any (titles are generated lazily).

    First — not last — wins. Claude Code re-stamps ai-title entries
    periodically from a cache that can hold a STALE title after /clear:
    observed as seven consecutive post-clear sessions whose transcripts each
    open with their own correct title (generated right after the first prompt)
    and are later re-stamped with a title generated for a conversation days
    earlier. Last-wins renamed every one of those archives to the stale title;
    the first stamp is the one generated for THIS conversation.
    """
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if '"ai-title"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") == "ai-title" and entry.get("aiTitle"):
                    title = entry["aiTitle"].strip()
                    if title:
                        return title
    except Exception:
        pass
    return None


def session_start(path: Path) -> datetime:
    """Local-time datetime of the first *content* block (stable per session).

    Keyed to the first archivable block — not to the transcript's first
    timestamped entry — because a post-/clear transcript is seeded with the
    /clear command record carrying the timestamp of when the PREVIOUS session
    was cleared, which can be days before this session's first real prompt
    (observed: a session first used on Jul 8 dated Jul 4 by its seed). Falls
    back to the first timestamped entry of any kind, then to now.
    """
    try:
        for entry, _role, _text in _content_entries(path):
            ts = entry.get("timestamp")
            if ts:
                return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        pass
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("timestamp")
                if ts:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    return dt.astimezone()
    except Exception:
        pass
    return datetime.now(timezone.utc).astimezone()


# --------------------------------------------------------------------------- #
# Machine / environment metadata
# --------------------------------------------------------------------------- #

def machine_hostname() -> str:
    """The computer's hostname, or '' if it can't be determined."""
    try:
        return socket.gethostname().strip()
    except Exception:
        return ""


def machine_ip() -> str:
    """Best-effort primary LAN IP of the machine, or '' on failure.

    Opens a UDP socket toward a public address to discover which local
    interface the OS would route through, then reads that interface's address.
    No packets are actually sent (UDP connect only sets the destination), so
    this works offline-ish and never blocks on the network."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return ""
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


def tmux_session() -> str | None:
    """Name of the tmux session the hook is running under, or None if not in tmux.

    Detects tmux via $TMUX (set inside every tmux pane), then asks tmux for the
    current session name via ``display-message``. Returns None when tmux isn't
    present, the binary is missing, or the query fails — the field is then
    simply omitted from the archive."""
    if not os.environ.get("TMUX"):
        return None
    try:
        res = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    name = res.stdout.strip()
    return name or None


def machine_meta() -> dict:
    """Collect the host metadata stamped into each archive header: computer
    name, LAN IP, and (when present) the tmux session name. Keys with no value
    are omitted so the renderer only shows what was actually resolved."""
    meta: dict = {}
    host = machine_hostname()
    if host:
        meta["hostname"] = host
    ip = machine_ip()
    if ip:
        meta["ip"] = ip
    tmux = tmux_session()
    if tmux:
        meta["tmux"] = tmux
    return meta


# --------------------------------------------------------------------------- #
# Filename / slug
# --------------------------------------------------------------------------- #

def slugify(name: str) -> str:
    """Spaces -> '-', drop punctuation, keep word chars (incl. CJK), dash, underscore."""
    name = name.strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w\-]", "", name, flags=re.UNICODE)  # \w keeps CJK + _
    name = re.sub(r"-{2,}", "-", name).strip("-_")
    if len(name) > MAX_SLUG_LEN:
        name = name[:MAX_SLUG_LEN].rstrip("-_")
    return name or "untitled"


def short_sid(session_id: str) -> str:
    return (session_id or "").split("-")[0][:8] or "session"


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def state_file(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}.json"


def load_state(session_id: str) -> dict:
    """Load per-session state, always normalized to the expected shape. A
    missing, corrupt, or partial state file (e.g. `{}` or missing keys/blocks)
    must not crash the hook, so defaults are filled and list fields coerced."""
    data: dict = {}
    try:
        loaded = json.loads(state_file(session_id).read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except Exception:
        data = {}
    data.setdefault("title", None)
    data.setdefault("file", None)
    if not isinstance(data.get("blocks"), list):
        data["blocks"] = []
    if not isinstance(data.get("keys"), list):
        data["keys"] = []
    return data


def save_state(session_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = state_file(session_id).with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_file(session_id))


def load_index() -> dict:
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_index(index: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INDEX_PATH)


def resolve_relpath(repo: Path, session_id: str, start: datetime,
                    title: str | None, current_rel: str | None = None,
                    subdir: str = "") -> str:
    """Compute the per-session relative path, guarding against collisions. Tries
    the bare name, then progressively longer session-id suffixes, returning the
    first *safe* candidate.

    A candidate is safe if it is already ours (recorded in the index OR equal to
    this session's `current_rel`), or it is genuinely free — meaning the index
    has no owner AND no file exists at that path on disk. The on-disk check
    matters because the index lives under ~/.claude, not in the repo: after a
    fresh install, an index wipe, or a `git pull` on another machine, a markdown
    file can exist with no index entry; without the disk check we would overwrite
    another session's archive. The full-session-id candidate is globally unique,
    so it always resolves (an orphan file with our full id is our own)."""
    month = start.strftime("%Y-%m")
    date = start.strftime("%Y-%m-%d-%H%M")  # date + HHMM (session-start, stable)
    base_slug = slugify(title) if title else short_sid(session_id)
    index = load_index()

    # Connected (Second Brain) mode prefixes every path with the vault
    # subfolder; standalone mode keeps the original repo-root layout.
    pre = f"{subdir}/" if subdir else ""
    candidates = [f"{pre}{month}/{date}-{base_slug}.md"]
    if session_id:
        candidates.append(f"{pre}{month}/{date}-{base_slug}-{short_sid(session_id)}.md")
        candidates.append(f"{pre}{month}/{date}-{base_slug}-{session_id}.md")

    for rel in candidates:
        owner = index.get(rel)
        if owner == session_id:
            return rel
        # Only claim an unowned path: as our stale-but-recorded file, or when it
        # is genuinely free on disk. A path the index assigns to another session
        # is never reused, even if our state still points at it.
        if owner is None and (rel == current_rel or not (repo / rel).exists()):
            return rel
    # Full-session-id candidate: unique to us even if an orphan file exists.
    return candidates[-1]


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #

def render_markdown(session_id: str, start: datetime, title: str | None,
                    blocks: list[dict], machine: dict | None = None) -> str:
    n_user = sum(1 for b in blocks if b["role"] == "user")
    n_asst = sum(1 for b in blocks if b["role"] == "assistant")
    heading = title or f"Session {short_sid(session_id)}"

    lines = [
        f"# {heading}",
        "",
        f"- **Session**: `{session_id}`",
        f"- **Started**: {start.strftime('%Y-%m-%d %H:%M %z')}",
    ]
    machine = machine or {}
    host = machine.get("hostname")
    ip = machine.get("ip")
    if host or ip:
        where = host or ""
        if ip:
            where = f"{where} ({ip})" if where else ip
        lines.append(f"- **Machine**: {where}")
    if machine.get("tmux"):
        lines.append(f"- **tmux session**: `{machine['tmux']}`")
    lines += [
        f"- **Turns archived**: {n_user} user / {n_asst} assistant",
        "",
        "---",
        "",
    ]
    for b in blocks:
        role = b["role"]
        text = b["text"].rstrip()
        if role == "user":
            lines.append("## \U0001f9d1 User")
            lines.append("")
            lines.append(text)
        elif role == "assistant":
            lines.append("## \U0001f916 Assistant")
            lines.append("")
            lines.append(text)
        elif role == "compact":
            lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def turns_body(md_text: str) -> str:
    """The per-turn portion of a rendered archive (everything after the header
    rule). Because turns accumulate append-only, an earlier render's body is
    always a *prefix* of a later one for the same session — so a stale file is
    safe to delete iff its body is a prefix of the new file's body. Used by the
    cleanup to never drop turns the new file wouldn't contain."""
    parts = md_text.split("\n---\n", 1)
    return (parts[1] if len(parts) == 2 else md_text).strip()


def body_covers(old_body: str, new_body: str) -> bool:
    """True iff ``new_body`` contains ``old_body``'s turns as its leading turns —
    i.e. ``old_body`` is ``new_body`` truncated at a *turn boundary*.

    A raw ``new_body.startswith(old_body)`` is not enough: a turn whose text is a
    string prefix of a longer turn (e.g. ``testing`` vs ``testing123``) would
    false-positive and mark a non-covered stale file as safe to delete. Blocks
    are rendered separated by a blank line, so a genuine turn-boundary prefix is
    either the whole body or continues with ``\\n\\n``; requiring that rejects
    mid-turn string matches."""
    if not old_body or old_body == new_body:
        return True
    return (new_body.startswith(old_body)
            and new_body[len(old_body):].startswith("\n\n"))


def _on_disk_covered(path: Path, new_body: str) -> bool:
    """Whether the archive already at ``path`` is fully contained (as a
    turn-boundary prefix) in ``new_body`` — i.e. overwriting it loses nothing.

    Returns False if the file can't be read: a read error must NOT be mistaken
    for "empty, safe to overwrite" (treating the body as "" would make
    body_covers report covered and let a smaller render replace richer,
    unreadable-but-real content). The never-shrink guard then preserves it."""
    try:
        return body_covers(turns_body(path.read_text(encoding="utf-8")), new_body)
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #

def run_git(repo: Path, *args: str, timeout: int = 30,
            quiet: bool = False) -> subprocess.CompletedProcess:
    """Run git, tolerating a missing git binary or a timeout. A missing git
    (FileNotFoundError) or a command that exceeds `timeout` (TimeoutExpired)
    would otherwise raise mid-run and bypass every caller's returncode check —
    e.g. a slow `pull --rebase` would skip both the mid-rebase `rebase --abort`
    cleanup and the subsequent `git push`. Instead we return a synthetic
    non-zero CompletedProcess (rc=127 for missing git, rc=124 for timeout) so
    callers degrade gracefully and the failure is logged once.

    `quiet=True` suppresses those failure log writes — used by the read-only
    `--doctor` path, which inspects git state and surfaces a missing binary or a
    timeout in its own report; without quiet it would append to archive.log and
    so pollute the very 'recent errors' section it is reading back."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        if not quiet:
            log("git not found on PATH")
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127, stdout="", stderr="git not found",
        )
    except subprocess.TimeoutExpired:
        if not quiet:
            log(f"git timed out after {timeout}s: {' '.join(args)}")
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=124, stdout="", stderr="git timed out",
        )


def ensure_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        run_git(repo, "init")
        log(f"git init {repo}")
    # Ensure a commit identity exists (fall back to a local one if global is unset).
    ident = run_git(repo, "config", "user.email")
    if not ident.stdout.strip():
        run_git(repo, "config", "user.email", "cc-archiver@localhost")
        run_git(repo, "config", "user.name", "cc-conversation-archiver")
    # Keep local-only bookkeeping out of the committed tree.
    gitignore = repo / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(".DS_Store\n", encoding="utf-8")


def do_push(repo: Path) -> subprocess.CompletedProcess:
    """Push under an exclusive push lock, held via Python's fcntl.flock (works on
    Linux and macOS — unlike the `flock` *binary*, which macOS does not ship).
    Serializes all pushes (background auto-pushes and manual uploads) so they
    never overlap; every commit still gets its own push attempt."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(PUSH_LOCK, "w") as pl:
        fcntl.flock(pl, fcntl.LOCK_EX)
        # Rebase our local per-turn commits on top of the shared remote before
        # pushing, so a repo pushed to from several machines never wedges us in
        # a permanent non-fast-forward reject (and history stays linear). The
        # "-X theirs" auto-resolves any conflict in favour of the LOCAL commit
        # being replayed — "last commit wins" (note: under rebase, "theirs"
        # refers to the commit being applied, i.e. ours). autostash guards any
        # stray worktree change. Best-effort: skipped without a remote; if a
        # rebase somehow can't auto-resolve, abort back to a clean state so we
        # never strand the repo mid-rebase, then let the push below surface the
        # real error for the next turn to retry.
        if run_git(repo, "remote").stdout.strip():
            pull = run_git(repo, "-c", "rebase.autoStash=true",
                           "pull", "--rebase", "-X", "theirs", timeout=120)
            if pull.returncode != 0:
                log(f"pull --rebase rc={pull.returncode}: "
                    f"{((pull.stderr or '') + (pull.stdout or '')).strip()[:200]}")
                if (repo / ".git" / "rebase-merge").exists() or \
                        (repo / ".git" / "rebase-apply").exists():
                    run_git(repo, "rebase", "--abort")
        res = run_git(repo, "push", timeout=120)
    out = ((res.stdout or "") + (res.stderr or "")).strip()
    try:
        with PUSH_LOG.open("a", encoding="utf-8") as fh:
            fh.write(out + "\n")
    except Exception:
        pass
    if res.returncode != 0:
        log(f"push rc={res.returncode}")
    return res


def push_background(repo: Path) -> None:
    """Spawn a detached push so a slow network push never blocks the turn. The
    child re-runs this script in --push-only mode, which serializes via fcntl —
    cross-platform, no external `flock` binary needed."""
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--push-only", str(repo)],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        log(f"push spawn failed: {exc}")


def do_upload() -> None:
    """Manual upload (invoked as `archive.py --upload` by /conversation-archiver:upload).
    Commits any pending archive changes under the archive lock, then pushes under
    the push lock. All locking is fcntl-based (cross-platform). Prints a one-line
    result for the command to relay."""
    repo = get_repo(load_config())
    if not (repo / ".git").exists():
        print(f"archive repo not initialized yet ({repo}) — "
              "created on the first archived turn")
        return
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        run_git(repo, "add", "-A")
        if run_git(repo, "status", "--porcelain").stdout.strip():
            msg = "manual upload: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            commit = run_git(repo, "commit", "-m", msg)
            print(f"committed ({repo})" if commit.returncode == 0
                  else f"commit failed: {commit.stderr.strip()}")
        else:
            print(f"nothing new to upload ({repo})")
    res = do_push(repo)
    if res.returncode != 0:
        print(f"push skipped/failed — configure a remote: "
              f"git -C {repo} remote add origin <url> && git -C {repo} push -u origin HEAD")
    else:
        print(f"pushed ({repo})")


def claude_projects_dir() -> Path:
    """Where Claude Code stores per-session transcript JSONL files. Layout is
    ``<projects>/<encoded-project>/<session-id>.jsonl``. Overridable via
    CLAUDE_PROJECTS_DIR (mainly for tests)."""
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    return Path(env).expanduser() if env else HOME / ".claude" / "projects"


def _is_sidechain_transcript(tpath: Path) -> bool:
    """True if ``tpath`` is a subagent (Task) transcript, not the main conversation.

    Claude Code stores subagent transcripts under
    ``<projects>/<proj>/<main-sid>/subagents/agent-*.jsonl`` and tags their
    entries ``isSidechain: true``. A ``SubagentStop`` hook may hand us such a
    path; archiving it directly would leak the subagent's task prompt and replies
    into the session's archive file, so callers detect and redirect these to the
    main transcript instead. Path-based detection is primary (free, matches the
    documented layout); the ``isSidechain`` first-entry read is a cheap fallback
    in case the on-disk layout changes."""
    if "subagents" in tpath.parts:
        return True
    try:
        with tpath.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                return bool(isinstance(entry, dict) and entry.get("isSidechain"))
    except (OSError, json.JSONDecodeError):
        return False
    return False


def _main_transcript_for(session_id: str) -> Path | None:
    """Locate a session's MAIN transcript (``<projects>/<proj>/<sid>.jsonl``) by id.

    Used to redirect a ``SubagentStop`` payload that points at a subagent
    (sidechain) transcript back to the real conversation. The subagent transcript
    carries the *parent* session id (verified on disk), so the lookup is exact.
    Returns None if no such top-level transcript exists (then the caller skips
    rather than archiving sidechain content)."""
    projects = claude_projects_dir()
    if not session_id or not projects.is_dir():
        return None
    for cand in projects.glob(f"*/{session_id}.jsonl"):
        if cand.is_file():
            return cand
    return None


def transcript_session_id(tpath: Path) -> str | None:
    """Session id for a transcript: the ``sessionId`` field if present, else the
    filename stem (Claude Code names each transcript ``<session-id>.jsonl``)."""
    try:
        with tpath.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict):
                    sid = entry.get("sessionId") or entry.get("session_id")
                    if sid:
                        return str(sid)
                break  # only the first non-blank line carries the id
    except OSError:
        return None
    return tpath.stem or None


def transcript_has_content(tpath: Path) -> bool:
    """True if the transcript yields at least one archivable block (user /
    assistant / compact), so empty or tool-only sessions are skipped."""
    for _ in parse_transcript(tpath):
        return True
    return False


def _final_reply_pending(tpath: Path) -> bool:
    """True while a Stop / SubagentStop turn's closing assistant text is still
    being flushed — the precise signal the archiver waits on so it never commits
    a reply-less turn during that race.

    Claude Code writes the closing message's reasoning as a finalized
    (``end_turn``) *thinking-only* block to the transcript a moment BEFORE it
    writes that same message's sibling text. So a transcript whose LAST entry is
    an ``end_turn`` thinking-only block has its visible reply imminent — wait for
    it. Every other terminal state reports False so the poll exits immediately:
    the text is already on disk (last entry is a text block), or the turn ended
    on a tool call / produced no closing text (e.g. a SubagentStop fired while
    the main turn's last act was a tool_use). That keeps tool-ending and
    text-less turns at zero added latency. The rarer no-thinking flush race is
    left to the existing next-event backstop."""
    last_is_thinking = False
    last_stop = None
    try:
        with tpath.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "assistant":
                    msg = entry.get("message") or {}
                    content = msg.get("content")
                    kinds = ({b.get("type") for b in content if isinstance(b, dict)}
                             if isinstance(content, list) else set())
                    last_is_thinking = kinds == {"thinking"}
                    last_stop = msg.get("stop_reason")
                else:
                    last_is_thinking = False
                    last_stop = None
    except OSError:
        return False
    return last_is_thinking and last_stop == "end_turn"


def do_backfill() -> None:
    """Backfill (``archive.py --backfill``): archive every existing Claude Code
    transcript on disk — the sessions that ran *before* the plugin was installed,
    which the hooks never saw. Reuses the normal per-session archiving logic, so
    it is fully idempotent (re-running only adds new turns and never forks a
    file). Writes the whole sweep under one archive lock, then commits + pushes
    ONCE (not per session). Prints a one-line summary for the command to relay."""
    cfg = load_config()
    repo = get_repo(cfg)
    projects = claude_projects_dir()
    if not projects.is_dir():
        print(f"no Claude Code transcripts dir at {projects} — nothing to backfill")
        return
    # Top-level session transcripts only: <projects>/<encoded-cwd>/<sid>.jsonl.
    # Deeper files (e.g. <…>/<sid>/subagents/agent-*.jsonl) are sub-agent / Task
    # transcripts, not user conversations, and are deliberately excluded.
    transcripts = sorted(projects.glob("*/*.jsonl"))
    if not transcripts:
        print(f"no transcripts found under {projects} — nothing to backfill")
        return

    ensure_repo(repo)
    archived = skipped = 0
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        for tpath in transcripts:
            sid = transcript_session_id(tpath)
            if not sid or not transcript_has_content(tpath):
                skipped += 1
                continue
            try:
                # do_commit=False: write files only; we commit the whole sweep
                # once below instead of once per (potentially hundreds of) session.
                _archive_locked(sid, tpath, "backfill", do_commit=False)
                archived += 1
            except Exception as exc:  # one bad transcript must not abort the sweep
                log(f"backfill {tpath.name} failed: {exc}")
                skipped += 1
            if archived and archived % 25 == 0:
                print(f"  … {archived} sessions archived")
        # One commit for the whole backfill. Track the three outcomes
        # separately — a clean tree and a *failed* commit must not look alike.
        run_git(repo, "add", "-A")
        had_changes = bool(run_git(repo, "status", "--porcelain").stdout.strip())
        committed = False
        commit_err = ""
        if had_changes:
            msg = f"backfill {archived} session(s): " + \
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            commit = run_git(repo, "commit", "-m", msg)
            committed = commit.returncode == 0
            if not committed:
                commit_err = ((commit.stderr or "") + (commit.stdout or "")).strip()
                log(f"backfill commit failed: {commit_err}")

    log(f"[backfill] archived={archived} skipped={skipped} "
        f"transcripts={len(transcripts)}")
    print(f"backfilled {archived} session(s) "
          f"(skipped {skipped} empty/unreadable of {len(transcripts)} transcripts)")

    # A failed commit (staged changes that wouldn't commit) is fatal — surface it
    # rather than pushing a half-done state.
    if had_changes and not committed:
        print(f"commit failed ({repo}): {commit_err[:200] or 'see archive.log'} — "
              f"files written but NOT committed; re-run /conversation-archiver:upload")
        return
    if not had_changes:
        print(f"nothing new to commit ({repo})")
    # Always attempt a push — like --upload — even when nothing was committed this
    # run, so commits a previous backfill made locally but failed to push still
    # get retried on a later run.
    res = do_push(repo)
    if res.returncode != 0:
        print(f"push skipped/failed — configure a remote: "
              f"git -C {repo} remote add origin <url> && git -C {repo} push -u origin HEAD")
    else:
        print(f"pushed ({repo})")


def _migrate_into_subdir(repo: Path, subdir: str) -> list[str]:
    """Move existing top-level ``YYYY-MM`` month dirs under ``subdir`` and
    rewrite the index + per-session state bookkeeping to the prefixed paths.
    Idempotent — already-prefixed entries and a missing archive are no-ops.
    Returns the list of month dirs moved."""
    moved: list[str] = []
    month_re = re.compile(r"^\d{4}-\d{2}$")
    (repo / subdir).mkdir(parents=True, exist_ok=True)
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or not month_re.match(child.name):
            continue
        dest_rel = f"{subdir}/{child.name}"
        mv = run_git(repo, "mv", child.name, dest_rel)
        if mv.returncode != 0:
            # Untracked yet (manual mode / never committed) — plain move.
            try:
                child.replace(repo / dest_rel)
            except Exception as exc:
                log(f"connect: move {child.name} -> {dest_rel} failed: {exc}")
                continue
        moved.append(child.name)
    if moved:
        prefix = f"{subdir}/"
        index = load_index()
        save_index({
            (rel if rel.startswith(prefix) else prefix + rel): sid
            for rel, sid in index.items()
        })
        for sf in STATE_DIR.glob("*.json"):
            if sf == INDEX_PATH:
                continue
            try:
                st = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                continue
            rel = st.get("file")
            if rel and not rel.startswith(prefix):
                st["file"] = prefix + rel
                sf.write_text(json.dumps(st, ensure_ascii=False),
                              encoding="utf-8")
    return moved


def _read_local_gsk_token() -> str:
    """Reuse the token ``gsk login`` already wrote, so the user never pastes
    one. Priority: ``$GSK_API_KEY`` env > ``~/.genspark-tool-cli/config.json``
    ``api_key`` field (the gsk CLI's own precedence order). Empty if neither
    is present (caller then guides the user through ``gsk login``)."""
    env = os.environ.get("GSK_API_KEY", "").strip()
    if env:
        return env
    try:
        data = json.loads(GSK_CLI_CONFIG.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return str(data.get("api_key", "")).strip()
    except Exception:
        pass
    return ""


def _resolve_push_url(token: str) -> str:
    """Ask the backend WHERE to push, authenticating with the gsk token
    (Bearer). Returns the vault push URL, or empty on any failure. Uses
    stdlib urllib so the plugin stays dependency-free."""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        SB_BASE_URL + SB_RESOLVE_PATH,
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        url = str(body.get("remote_url", "")).strip()
        return url
    except urllib.error.HTTPError as e:
        log(f"resolve push url HTTP {e.code}: {e.reason}")
        return ""
    except Exception as e:
        log(f"resolve push url failed: {e}")
        return ""


def _redeem_connect_code(sb_connect_url: str) -> tuple:
    """Redeem a one-time connect code for the push target + credential.

    ``sb_connect_url`` is the ``…/sources/claude-code/sb-connect/<CODE>``
    link from the Claude Code connect dialog (the Claude Code row on the
    user's Second Brain Sources page). We POST the code to the
    sibling ``/activate`` endpoint on the same origin; the backend consumes
    the code (single-use, 10-min TTL) and answers with the vault push URL
    and a freshly minted push token. Returns ``(remote_url, token)`` or
    ``("", "")`` after printing a user-facing reason."""
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    parts = urlsplit(sb_connect_url)
    code = parts.path.rstrip("/").rsplit("/", 1)[-1].strip()
    if parts.scheme not in ("http", "https") or not parts.netloc or not code:
        print(f"invalid connect link: {sb_connect_url}")
        return "", ""
    activate_url = (
        f"{parts.scheme}://{parts.netloc}"
        "/api/memo_v2/sources/claude-code/activate"
    )
    req = urllib.request.Request(
        activate_url,
        data=json.dumps({"code": code}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        remote_url = str(body.get("remote_url", "")).strip()
        token = str(body.get("token", "")).strip()
        if not remote_url or not token:
            print("connect failed: backend answered without a push target.")
            return "", ""
        return remote_url, token
    except urllib.error.HTTPError as e:
        log(f"activate HTTP {e.code}: {e.reason}")
        if e.code == 404:
            print(
                "This connect link has expired or was already used (codes "
                "are single-use and last 10 minutes). Reopen the Connect "
                "dialog by clicking the Claude Code row on your Second "
                "Brain Sources page and paste the fresh message."
            )
        else:
            print(f"connect failed: backend error {e.code}. Try again in a "
                  "moment or grab a fresh link from the Connect dialog.")
        return "", ""
    except Exception as e:
        log(f"activate failed: {e}")
        print("connect failed: could not reach the Second Brain backend. "
              "Check your network and try again.")
        return "", ""


def _guide_gsk_setup() -> None:
    """Print the guided path for a machine that has no gsk token yet —
    install (one npm command) + login (one browser-consent click). Both are
    standard gsk onboarding; the connect command re-run picks the token up
    automatically afterward."""
    print(
        "To link this computer I need your Second Brain credential, which "
        "the `gsk` CLI provides.\n"
        "\n"
        "  1. Install it (once):   npm i -g @genspark/cli\n"
        "  2. Sign in:             gsk login\n"
        "     (opens your browser — just click Allow; nothing to copy)\n"
        "\n"
        "Then re-run /conversation-archiver:connect — it picks up the "
        "credential automatically.\n"
        "\n"
        "Already have a token? Pass it directly: "
        "/conversation-archiver:connect <remote_url> <token>"
    )


def do_connect(remote_url: str = "", token: str = "",
               subdir: str = SB_DEFAULT_SUBDIR) -> None:
    """Connect the archive to the user's Second Brain ``/memo`` vault
    (invoked as ``archive.py --connect`` by /conversation-archiver:connect).

    Primary path (zero credential): the argument is the ``sb-connect/<code>``
    link from the Claude Code connect dialog (the Claude Code row on the
    user's Second Brain Sources page) — the script redeems the
    one-time code via ``/activate`` and receives the push URL + a freshly
    minted token directly from the backend. Fallbacks, in order: no args +
    a local ``gsk login`` token (self-resolve via ``/resolve``); explicit
    ``<remote_url> <token>`` args (machines without gsk).

    Steps: store the credential OUTSIDE the repo (git credential-store file
    under ~/.claude, chmod 600 — never plaintext in .git/config), point
    ``origin`` at the vault repo, move any existing archive under the vault
    subfolder, sparse-checkout that subfolder only (the rest of the user's
    vault never materializes on this machine), integrate the remote history
    and push. Flips mode to auto so every turn syncs from now on."""
    from urllib.parse import urlsplit

    # sb-connect link → redeem the one-time code for both URL and token.
    if remote_url and "/sb-connect/" in remote_url and not token:
        remote_url, token = _redeem_connect_code(remote_url)
        if not remote_url:
            return
    # Self-resolve the credential the user didn't paste. Token first (from
    # gsk login), then ask the backend for the push URL with that token.
    if not token:
        token = _read_local_gsk_token()
        if not token:
            _guide_gsk_setup()
            return
    if not remote_url:
        remote_url = _resolve_push_url(token)
        if not remote_url:
            print(
                "Couldn't resolve your vault push URL. Make sure you're "
                "signed in (`gsk login`) and try again. If it keeps failing, "
                "pass the URL explicitly: "
                "/conversation-archiver:connect <remote_url> <token>"
            )
            return

    parts = urlsplit(remote_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        print(f"invalid remote url: {remote_url}")
        return

    cfg = load_config()
    repo = get_repo(cfg)
    ensure_repo(repo)

    # 1. Credential — git credential-store file scoped to this repo only.
    #    Create with 0o600 ATOMICALLY (os.open O_CREAT|mode) so the token is
    #    never world-readable even for the instant between write and chmod;
    #    unlink any pre-existing looser-mode file first so we don't inherit it.
    APP_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.unlink(CRED_FILE)
    except FileNotFoundError:
        pass
    cred_line = f"{parts.scheme}://x-access-token:{token}@{parts.netloc}\n"
    fd = os.open(str(CRED_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(cred_line)
    # Single-quoted for git's sh-style helper parsing — survives a HOME
    # containing spaces.
    run_git(repo, "config", "credential.helper",
            f"store --file '{CRED_FILE}'")
    # Plain `git push` / `git pull` must follow the upstream even when the
    # local branch name (master/main) differs from the vault's `main`.
    run_git(repo, "config", "push.default", "upstream")

    # 2. Remote.
    if "origin" in run_git(repo, "remote").stdout.split():
        run_git(repo, "remote", "set-url", "origin", remote_url)
    else:
        run_git(repo, "remote", "add", "origin", remote_url)

    # 3. Move the existing archive under the vault subfolder + commit.
    moved = _migrate_into_subdir(repo, subdir)
    run_git(repo, "add", "-A")
    if run_git(repo, "status", "--porcelain").stdout.strip():
        run_git(repo, "commit", "-m", f"connect: move archive under {subdir}/")

    # 4. Persist config BEFORE the network steps — a flaky first push must
    #    not leave future turns writing to the un-prefixed layout.
    cfg.update({"mode": "auto", "subdir": subdir})
    save_config(cfg)

    # 5. Working tree shows our subfolder only; the vault's other content
    #    stays as git objects, never as files on this machine. Best-effort —
    #    an old git without sparse-checkout just materializes everything.
    run_git(repo, "sparse-checkout", "set", subdir)

    # 6. Integrate the vault history and push. A brand-new repo (connect
    #    before the first archived turn) has no HEAD — adopt the remote
    #    branch instead of rebasing onto it.
    fetch = run_git(repo, "fetch", "origin", "main", timeout=120)
    if fetch.returncode != 0:
        print("connect failed: could not reach the vault repo "
              f"({(fetch.stderr or '').strip()[:200]}). "
              "Check the URL/token and re-run the command.")
        return
    if run_git(repo, "rev-parse", "--verify", "HEAD").returncode == 0:
        pull = run_git(repo, "-c", "rebase.autoStash=true",
                       "pull", "--rebase", "-X", "theirs", "origin", "main",
                       timeout=120)
        if pull.returncode != 0:
            log(f"connect pull --rebase rc={pull.returncode}: "
                f"{((pull.stderr or '') + (pull.stdout or '')).strip()[:200]}")
            if (repo / ".git" / "rebase-merge").exists() or \
                    (repo / ".git" / "rebase-apply").exists():
                run_git(repo, "rebase", "--abort")
    else:
        run_git(repo, "reset", "--hard", "FETCH_HEAD")
    push = run_git(repo, "push", "-u", "origin", "HEAD:main", timeout=120)
    if push.returncode != 0:
        print("connect: remote + credential saved, but the first push failed "
              f"({(push.stderr or '').strip()[:200]}). It will be retried on "
              "your next archived turn.")
        return

    moved_note = (f" (moved {len(moved)} month folder(s) under {subdir}/)"
                  if moved else "")
    print(f"connected — archive now syncs to your Second Brain vault under "
          f"{subdir}/{moved_note}. Mode: auto (every turn pushes).")


def do_set_repo(path: str = "") -> None:
    """Repoint the archive to a new local repo path (``archive.py --set-repo``),
    invoked by /conversation-archiver:repo.

    Option A — *repoint only*: it updates the ``repo`` path in config and nothing
    else. From the next turn on, archiving writes to the new location; the
    EXISTING archive (and its git history) is LEFT IN PLACE at the old path —
    this command never moves, copies, or deletes it. The repo at the new path is
    created on the next archived turn, exactly like a fresh install. (Ongoing
    sessions rebuild their full markdown from accumulated state, so their
    history re-materializes in the new repo on their next turn.)"""
    path = (path or "").strip()
    if not path:
        print("usage: /conversation-archiver:repo <absolute path or ~/path>")
        return
    new = Path(path).expanduser()
    if not new.is_absolute():
        print(f"please pass an absolute path (or ~/...): got '{path}'")
        return
    if new.exists() and not new.is_dir():
        print(f"not a directory: {new} — pass a folder path for the archive repo")
        return

    cfg = load_config()
    new_str = str(new)
    # The CONFIG's current archive path, env-independent and ~-expanded (the env
    # override is surfaced separately below). DEFAULT_REPO when no key is set.
    cur = cfg.get("repo")
    config_old = Path(cur).expanduser() if cur else DEFAULT_REPO
    if cur == new_str:
        print(f"archive repo is already set to {new_str}")
    elif config_old == new:
        # The stored value differs textually (no key yet → the default, or a
        # ``~``/unexpanded form) but resolves to the SAME directory — just persist
        # the normalized value; do NOT claim a repoint that didn't happen.
        cfg["repo"] = new_str
        save_config(cfg)
        print(f"archive repo confirmed at {new_str} "
              "(location unchanged; config normalized).")
    else:
        cfg["repo"] = new_str
        save_config(cfg)
        print(f"archive repo path set to {new_str}")
        print(f"(was {config_old}). Option A — repoint only: the previous archive "
              "is left untouched at the old path; new turns archive to the new "
              "path, which is created on the next archived turn.")

    # These describe the EFFECTIVE archiving state, so print them whether we just
    # changed the path or it was already set — otherwise an idempotent re-run
    # could look successful while an env override silently archives elsewhere.
    env_repo = os.environ.get("CC_ARCHIVE_REPO")
    if env_repo:
        print(f"WARNING: CC_ARCHIVE_REPO is set ({env_repo}) and overrides config, "
              f"so archiving currently goes there — not to {new_str}. Unset it to "
              "use the configured path.")
    if get_subdir(cfg):
        print("NOTE: you're connected to Second Brain — make sure this repo has "
              "the vault remote (re-run /conversation-archiver:connect if needed), "
              "otherwise archives here stay local-only.")


# --------------------------------------------------------------------------- #
# Doctor (diagnostics)
# --------------------------------------------------------------------------- #

def _log_tail(path: Path, n: int = 6) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
    except OSError:
        return []


# Redact the userinfo of an http(s) remote URL before printing it — a manually
# configured remote can embed the push token directly, so the doctor must never
# echo it back. Two credential shapes both leak and both must be masked:
#   * ``https://user:<token>@host``   (token as the password)
#   * ``https://<token>@host``        (token as the whole userinfo, no colon)
# so the ``:<pass>`` half is optional. The password half may contain ``/`` or
# ``+`` (e.g. a base64 Azure DevOps PAT / app-password), hence ``[^@\s]*`` there.
# The user half excludes ``/`` so a credential-free URL whose *path* contains an
# ``@`` (``https://host/a@b``) is NOT over-masked — the ``@`` we target is always
# in the authority, before the first path ``/``. SSH remotes (``git@host:path``,
# no ``://``) and credential-free URLs carry no secret and are left untouched.
_URL_CRED_RE = re.compile(r"(https?://)[^/@\s]+(?::[^@\s]*)?@")


def _mask_url(url: str) -> str:
    return _URL_CRED_RE.sub(r"\1***@", url)


def do_status() -> None:
    """Print a compact, read-only status report without exposing remote secrets."""
    cfg = load_config()
    repo = get_repo(cfg)
    mode = get_mode(cfg)

    print(f"mode: {mode}")
    print(f"repo: {repo}")
    if not (repo / ".git").exists():
        print("remote: none")
        print("--- recent commits ---")
        print("(no commits yet)")
        print("--- pending changes ---")
        return

    remote = run_git(repo, "remote", "-v", quiet=True)
    first_remote = remote.stdout.splitlines()[0] if remote.stdout.strip() else "none"
    print(f"remote: {_mask_url(first_remote)}")
    print("--- recent commits ---")
    recent = run_git(repo, "log", "--oneline", "-5", quiet=True)
    print(recent.stdout.rstrip() or "(no commits yet)")
    print("--- pending changes ---")
    pending = run_git(repo, "status", "--short", quiet=True)
    print("\n".join(pending.stdout.splitlines()[:10]))


def do_doctor() -> None:
    """Diagnose the archiver setup (``archive.py --doctor``), invoked by
    /conversation-archiver:doctor.

    READ-ONLY: inspects dependencies, config, the archive repo, the remote
    (with a live ``git ls-remote`` auth probe — read-only, never pushes), sync
    state, recent activity, and recent log errors, then prints a structured
    report ending in a verdict + concrete fixes. The slash command is
    model-invocable, so a user can diagnose install / connect / sync problems
    straight from a natural-language prompt and have Claude explain the result.
    Never mutates the repo and never raises (top-level guard exits 0 anyway)."""
    import shutil

    cfg = load_config()
    repo = get_repo(cfg)
    mode = get_mode(cfg)
    subdir = get_subdir(cfg)

    lines: list[str] = []
    problems: list[str] = []   # broken — needs a fix
    notes: list[str] = []      # advisory — works, but worth knowing

    def p(s: str = "") -> None:
        lines.append(s)

    def g(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
        # Every git call the doctor makes is quiet: --doctor is read-only and
        # must not append to archive.log on a missing-git / timeout path — that
        # would pollute the very 'recent errors' section it reads back below.
        return run_git(repo, *args, timeout=timeout, quiet=True)

    p("# conversation-archiver doctor")
    p("")

    # --- Dependencies ---
    p("## Dependencies")
    p(f"- python3: OK ({sys.version.split()[0]})")
    git_path = shutil.which("git")
    if git_path:
        p(f"- git: OK ({git_path})")
    else:
        p("- git: MISSING — archiving is disabled until git is on PATH")
        problems.append("Install git and ensure it's on your PATH.")
    p("")

    # --- Config ---
    p("## Config")
    p(f"- mode: {mode}" + (
        "  (each turn is committed + pushed)" if mode == "auto"
        else "  (turns saved locally; committed + pushed only on "
             "/conversation-archiver:upload)"))
    p(f"- repo: {repo}")
    if subdir:
        p(f"- subdir: {subdir}/ (Second Brain connected mode)")
    if os.environ.get("CC_ARCHIVE_MODE") or os.environ.get("CC_ARCHIVE_REPO"):
        p("- note: CC_ARCHIVE_* environment override(s) are active")
    p("")

    # Without a git binary every `git` probe below returns empty/rc!=0, which
    # would misreport a real repo/remote as absent — so gate the git-dependent
    # sections on git being present (the missing binary is already a problem
    # above) and report them as skipped rather than asserting a false state.
    git_ok = git_path is not None

    # --- Archive repo ---
    p("## Archive repo")
    is_git = (repo / ".git").exists()
    if not repo.exists():
        p("- not created yet — the repo is initialized on the first archived turn")
    elif not is_git:
        p(f"- {repo} exists but is not a git repo yet (created on the first turn)")
    elif not git_ok:
        p("- git repo present, but git is not on PATH — repo/sync checks skipped")
    else:
        p("- git repo: OK")
        last = g("log", "-1", "--format=%cd | %s", "--date=local")
        p(f"- last commit: {last.stdout.strip() or '(none yet)'}")
        pending = g("status", "--porcelain").stdout.strip()
        p(f"- pending uncommitted changes: "
          f"{len(pending.splitlines()) if pending else 0}")
    p("")

    # --- Sync / remote ---
    p("## Sync / remote")
    if not is_git:
        p("- no remote yet (repo not initialized)")
    elif not git_ok:
        p("- skipped — git is not on PATH, so the remote can't be inspected")
    else:
        rv = g("remote", "-v")
        remotes = rv.stdout.strip()
        if rv.returncode != 0:
            # git is present (checked above) but the query itself failed —
            # report the failure instead of claiming there is no remote.
            p("- remote: UNKNOWN — `git remote` failed; can't determine sync state")
            notes.append(
                "Couldn't query git remotes (git command failed). Re-run "
                "/conversation-archiver:doctor once git works to verify syncing."
            )
        elif not remotes:
            p("- remote: NONE — commits stay LOCAL only; conversations are not synced")
            notes.append(
                "No git remote configured, so the archive is local-only. To sync "
                "off this machine, run /conversation-archiver:connect (Second "
                "Brain) or add a personal git remote (README step 3)."
            )
        else:
            # Probe the remote `git push` will ACTUALLY use, so the connection
            # test matches the real push path. do_push runs a bare `git push`,
            # which follows the current branch's upstream remote (--connect sets
            # push.default=upstream). Derive that remote from @{u}; only when no
            # upstream is configured do we fall back to 'origin' (else the first
            # remote) as a best guess. Display and probe always use the same name.
            names = g("remote").stdout.split()
            up = g("rev-parse", "--abbrev-ref", "--symbolic-full-name",
                   "@{u}").stdout.strip()
            up_remote = up.split("/", 1)[0] if "/" in up else ""
            if up_remote and up_remote in names:
                name = up_remote
            elif "origin" in names:
                name = "origin"
            else:
                name = names[0]
            url = next((parts[1] for ln in remotes.splitlines()
                        if (parts := ln.split()) and parts[0] == name
                        and parts[-1] == "(push)"), "")
            p(f"- remote: {name} -> {_mask_url(url)}")
            if not up:
                notes.append(
                    f"No upstream is set for the current branch, so `git push` "
                    f"has no default target; the test below probes '{name}' as a "
                    "best guess. Set one with 'git push -u origin HEAD' (or re-run "
                    "/conversation-archiver:connect)."
                )
            elif name != "origin":
                notes.append(
                    f"Your push remote is '{name}', not 'origin' (the name the "
                    "README's connect/setup steps use). Pushes still work as long "
                    "as the branch tracks it (see the upstream line)."
                )
            # Live, read-only auth probe — verifies reachability + credentials
            # WITHOUT pushing anything.
            ls = g("ls-remote", "--heads", name, timeout=20)
            if ls.returncode == 0:
                p(f"- connection test (git ls-remote {name}): OK — reachable, auth works")
            elif ls.returncode == 124:
                # run_git returns rc 124 on timeout — a slow/offline network, NOT
                # an auth failure, so don't send the user chasing credentials.
                p(f"- connection test (git ls-remote {name}): TIMED OUT (>20s)")
                notes.append(
                    "The remote didn't respond within 20s — likely a slow or "
                    "offline network rather than an auth problem. Re-run "
                    "/conversation-archiver:doctor when you're back online."
                )
            else:
                p(f"- connection test (git ls-remote {name}): FAILED")
                for e in (((ls.stderr or "") + (ls.stdout or "")).strip()
                          .splitlines()[:4]):
                    p(f"    {_mask_url(e)}")
                problems.append(
                    "Remote is configured but not reachable/authorized. Check auth "
                    "(SSH key loaded in your agent, or a stored HTTPS token) and the "
                    "remote URL — see README step 3."
                )
            p(f"- upstream: {up}" if up else
              "- upstream: not set (first push uses 'git push -u origin HEAD')")
    p("")

    # --- Second Brain credential ---
    if subdir:
        p("## Second Brain credential")
        if CRED_FILE.exists():
            p(f"- credential file: present ({oct(CRED_FILE.stat().st_mode & 0o777)})")
        else:
            p("- credential file: MISSING")
            problems.append(
                "Second Brain credential file is missing — re-run "
                "/conversation-archiver:connect to refresh it."
            )
        p("")

    # --- Archive activity ---
    p("## Archive activity")
    state_files = ([f for f in STATE_DIR.glob("*.json") if f != INDEX_PATH]
                   if STATE_DIR.exists() else [])
    p(f"- sessions tracked (state files): {len(state_files)}")
    if state_files:
        newest = max(state_files, key=lambda f: f.stat().st_mtime)
        ts = datetime.fromtimestamp(newest.stat().st_mtime).strftime(
            "%Y-%m-%d %H:%M:%S")
        p(f"- most recent archived turn: {ts}")
    else:
        p("- none yet — if you've been chatting, restart the session so the "
          "hooks load, then send a message")
        notes.append(
            "No sessions archived yet. Hooks load at session start, so after "
            "installing/updating the plugin you must restart the Claude Code "
            "session. Existing pre-install sessions: run "
            "/conversation-archiver:backfill."
        )
    p("")

    # --- Recent errors / push output ---
    err_lines = [ln for ln in _log_tail(LOG_PATH, 200)
                 if "ERROR" in ln or "failed" in ln]
    if err_lines:
        p("## Recent errors (archive.log)")
        for ln in err_lines[-6:]:
            p(f"- {_mask_url(ln)}")
        p("")
    push_tail = _log_tail(PUSH_LOG, 4)
    if push_tail:
        p("## Recent push output (push.log)")
        for ln in push_tail:
            p(f"- {_mask_url(ln)}")
        p("")

    # --- Verdict ---
    p("## Verdict")
    if problems:
        p(f"Found {len(problems)} problem(s) to fix:")
        for i, pr in enumerate(problems, 1):
            p(f"  {i}. {pr}")
    elif notes:
        p("Healthy, with notes:")
        for i, nt in enumerate(notes, 1):
            p(f"  {i}. {nt}")
    elif mode == "manual":
        p("All checks passed — archiving is set up. Mode is MANUAL: turns are "
          "saved locally and committed/pushed only when you run "
          "/conversation-archiver:upload.")
    else:
        p("All checks passed — archiving is set up and syncing.")

    print("\n".join(lines))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> None:
    # CLI sub-modes (no stdin payload): invoked by push_background and by the
    # /conversation-archiver:upload command.
    if "--push-only" in sys.argv:
        repo = Path(sys.argv[-1]) if len(sys.argv) > 2 else get_repo(load_config())
        do_push(repo)
        return
    if "--upload" in sys.argv:
        do_upload()
        return
    if "--backfill" in sys.argv:
        do_backfill()
        return
    if "--doctor" in sys.argv:
        do_doctor()
        return
    if "--status" in sys.argv:
        do_status()
        return
    if "--set-repo" in sys.argv:
        args = sys.argv[sys.argv.index("--set-repo") + 1:]
        do_set_repo(args[0] if args else "")
        return
    if "--connect" in sys.argv:
        # Primary: a single sb-connect/<code> link (one-time code redeem).
        # Zero-arg self-resolves via gsk login; explicit <remote_url> <token>
        # [subdir] still works for back-compat.
        args = sys.argv[sys.argv.index("--connect") + 1:]
        do_connect(
            remote_url=(args[0] if len(args) > 0 else ""),
            token=(args[1] if len(args) > 1 else ""),
            subdir=(args[2] if len(args) > 2 else SB_DEFAULT_SUBDIR),
        )
        return

    raw = sys.stdin.read()
    payload = json.loads(raw) if raw.strip() else {}

    session_id = payload.get("session_id")
    transcript_path = payload.get("transcript_path")
    event = payload.get("hook_event_name", "?")

    if not session_id or not transcript_path:
        return
    tpath = Path(transcript_path)
    if not tpath.exists():
        return

    # A SubagentStop hook (and any sidechain) may hand us a *subagent* transcript
    # — the Task's own prompt + replies, stored under <main-sid>/subagents/. Never
    # archive that into the session file: redirect to the MAIN transcript (the
    # subagent transcript carries the parent session id) so an async-task
    # completion just flushes the real conversation, exactly as Stop does. If the
    # main transcript can't be located, skip rather than archive sidechain content.
    if _is_sidechain_transcript(tpath):
        main_tpath = _main_transcript_for(session_id)
        if main_tpath is None:
            log(f"{event}: sidechain transcript, no main transcript for "
                f"{short_sid(session_id)} — skipping")
            return
        tpath = main_tpath

    # Close the Stop-vs-flush race: a Stop / SubagentStop hook can fire a few
    # hundred ms before Claude Code writes the turn's closing assistant text to
    # the transcript. Without this wait that reply is archived only on the NEXT
    # event (next prompt / SessionEnd) — leaving the repo showing the question
    # with no answer until then (observed: a 31-min gap). Poll ONLY while the
    # reply is genuinely still in flight (never for a turn that produced no
    # archivable text), and give up after ~3s so the hook can never hang — the
    # existing next-event backstop still covers anything that lands later.
    if event in ("Stop", "SubagentStop"):
        for _ in range(6):
            if not _final_reply_pending(tpath):
                break
            time.sleep(0.5)

    # Serialize concurrent hook runs: different sessions share _index.json and
    # the one git repo, so without a lock two runs could pick the same filename,
    # overwrite each other's markdown, and corrupt the index. The flock releases
    # when the fd closes at the end of the with-block.
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        summary = _archive_locked(session_id, tpath, event)

    # Surface a GenTerminal inbox notification when a turn finishes (Stop) or the
    # session ends. Other events (UserPromptSubmit / SubagentStop) still archive
    # but stay silent so the inbox isn't noisy. Done OUTSIDE the lock so the tty
    # write never holds up another session's archive. Best-effort throughout.
    if summary is not None and event in ("Stop", "SessionEnd"):
        _maybe_notify(session_id, event, summary)


def _maybe_notify(session_id: str, event: str, summary: dict) -> None:
    """Push a GenTerminal inbox notification for this archive. Imported lazily
    and fully guarded so a missing module or any runtime error never disrupts
    the hook (which must always exit 0)."""
    # Opt-out: lets users (and the test suite) disable notifications entirely
    # without affecting archiving.
    if os.environ.get("CC_ARCHIVE_NO_NOTIFY"):
        return
    try:
        import notify  # sibling module in this scripts/ dir
    except Exception:
        return
    try:
        title = summary.get("title") or "Claude Code"
        turns = summary.get("turns", 0)
        unit = "turn" if turns == 1 else "turns"
        if event == "SessionEnd":
            body = f"Session ended · {turns} {unit} archived"
        else:
            body = f"Turn complete · {turns} {unit} archived"
        emitted = notify.emit(
            source="conversation-archiver",
            source_id=session_id,
            event=event,
            title=title,
            body=body,
            tmux=notify.tmux_context(),
        )
        if emitted and summary.get("title"):
            # Keep the fast-path title reporter (report_title.py) in sync so
            # it does not re-report the title this notification just carried.
            import report_title  # sibling module in this scripts/ dir

            report_title.remember(session_id, summary["title"])
    except Exception:
        log("notify failed\n" + traceback.format_exc())


def _archive_locked(session_id: str, tpath: Path, event: str,
                    do_commit: bool = True) -> dict | None:
    cfg = load_config()
    repo = get_repo(cfg)
    mode = get_mode(cfg)

    # 1. Accumulate new blocks into per-session state (append-only).
    state = load_state(session_id)
    seen = set(state.get("keys", []))
    new_count = 0
    for key, role, text in parse_transcript(tpath):
        if key in seen:
            continue
        seen.add(key)
        state["keys"].append(key)
        state["blocks"].append({"role": role, "text": text})
        new_count += 1

    # No visible turn yet — e.g. a fresh post-/clear transcript holding only
    # the seeded /clear record, or a tool-only session so far. Writing now
    # would create (and commit) a header-only file for a session that may
    # never be used; skip entirely — the first real turn archives normally.
    if not state["blocks"]:
        return None

    title = session_title(tpath)
    if title:
        state["title"] = title
    title = state.get("title")

    # Compute the session start once and persist it in state. Recomputing every
    # run is unstable when the transcript has no timestamps (session_start would
    # fall back to "now"), which would drift the Started line / dated path and
    # cause empty rewrites + commits. The stored value is authoritative.
    start = None
    stored_start = state.get("start")
    if stored_start:
        try:
            start = datetime.fromisoformat(stored_start)
        except ValueError:
            start = None
    if start is None:
        start = session_start(tpath)
        state["start"] = start.isoformat()

    # Stamp the host metadata (computer name, LAN IP, tmux session) into state so
    # the archive header records where the session ran. Refresh each run with
    # whatever resolves now, but never let a transient blank (e.g. tmux query
    # failure, offline IP lookup) clobber a value we captured earlier.
    stored_machine = state.get("machine")
    meta = dict(stored_machine) if isinstance(stored_machine, dict) else {}
    fresh = machine_meta()
    meta.update(fresh)
    # tmux presence is authoritative when $TMUX is unset: the session is
    # definitively NOT in tmux now, so drop any stale name a prior in-tmux turn
    # stored. (When $TMUX IS set but the name query failed, machine_meta also
    # omits "tmux" — but then we keep the prior value, since that's a transient
    # failure rather than a real exit from tmux.)
    if "tmux" not in fresh and not os.environ.get("TMUX"):
        meta.pop("tmux", None)
    state["machine"] = meta

    # 2. Resolve the (possibly new) per-session file path; handle title renames.
    #    ensure_repo first so the on-disk collision check sees real repo state.
    old_rel = state.get("file")
    ensure_repo(repo)
    new_rel = resolve_relpath(repo, session_id, start, title, old_rel,
                              subdir=get_subdir(cfg))

    new_abs = repo / new_rel
    new_abs.parent.mkdir(parents=True, exist_ok=True)

    if old_rel and old_rel != new_rel:
        old_abs = repo / old_rel
        moved = True
        if old_abs.exists():
            mv = run_git(repo, "mv", old_rel, new_rel)
            if mv.returncode != 0:  # not tracked yet (e.g. manual mode)
                try:
                    old_abs.replace(new_abs)
                except Exception as exc:
                    # Both git mv and the filesystem move failed: keep writing to
                    # the OLD path this run so we don't leave the old file behind
                    # AND create a second file for the same session. The rename is
                    # retried on the next turn.
                    log(f"rename {old_rel} -> {new_rel} failed, keeping old path: {exc}")
                    moved = False
        if moved:
            index = load_index()
            index.pop(old_rel, None)
            save_index(index)
        else:
            new_rel = old_rel
            new_abs = repo / new_rel

    # 3. Render the file from the full accumulated state (written in step 4,
    #    after cleanup, so a dir-pruning git rm can't race the write).
    content = render_markdown(session_id, start, title, state["blocks"],
                              state.get("machine"))

    # 3b. Enforce one-session-one-file: remove any *other* file the index still
    #     attributes to THIS session. The git-mv above migrates the path recorded
    #     in state (old_rel); this catches the stragglers that reconstruction by
    #     name would miss — a legacy date-only file under a *historical* slug (the
    #     short-sid name used before an ai-title existed, or an older title slug
    #     after a rename), left orphaned when state lost its pointer. Keyed on the
    #     index owner, so it is slug-, title-, and format-agnostic and only ever
    #     touches files this session owns — never another session's archive.
    #
    #     A stale file is deleted ONLY when its turns are a prefix of the new
    #     file's (turns accumulate append-only, so that proves the new file
    #     contains everything the stale one does). If state was lost/reset and the
    #     stale file holds turns the current transcript no longer has, the prefix
    #     check fails and the file is kept — content is never silently dropped.
    new_body = turns_body(content)
    index = load_index()
    for rel in [r for r, owner in index.items()
                if owner == session_id and r != new_rel]:
        stale_abs = repo / rel
        if stale_abs.exists():
            try:
                old_body = turns_body(stale_abs.read_text(encoding="utf-8"))
            except OSError as exc:
                log(f"stale cleanup read {rel} failed, keeping: {exc}")
                continue
            if not body_covers(old_body, new_body):
                log(f"kept stale file {rel} for session {short_sid(session_id)}: "
                    "content not covered by current state (possible state loss)")
                continue
            rm = run_git(repo, "rm", "-q", "--", rel)
            if rm.returncode != 0:  # untracked (e.g. manual mode) — unlink directly
                try:
                    stale_abs.unlink()
                except OSError as exc:
                    # Removal failed and the file still exists — keep the index
                    # entry so bookkeeping still tracks it; retried next turn.
                    log(f"stale cleanup remove {rel} failed, keeping index: {exc}")
                    continue
            log(f"cleaned stale duplicate {rel} for session {short_sid(session_id)}")
        index.pop(rel, None)

    # 3c. Never-shrink guard. State (append-only) is normally a superset of what
    #     is on disk, so an earlier render is always a turn-boundary prefix of
    #     the new one. If it is NOT — the file on disk holds turns the new render
    #     would drop — then state has diverged from disk: a wiped/corrupt state
    #     file, or an archive copy synced from another machine where this session
    #     ran. Overwriting would lose those turns. So release our claim on the
    #     richer file (it stays on disk, preserved, and is committed as-is) and
    #     re-resolve to a fresh suffixed path for this render — the SAME suffixing
    #     resolve_relpath applies to any occupied name. That keeps the session on
    #     the new path on later turns (resolve_relpath returns it as the owner)
    #     instead of resolving back onto the richer file and renaming over it. A
    #     file we can't read is treated as not-covered too — see _on_disk_covered.
    if new_abs.exists() and not _on_disk_covered(new_abs, new_body):
        index.pop(new_rel, None)
        save_index(index)  # persist the release so resolve_relpath sees it
        diverted = resolve_relpath(repo, session_id, start, title, None,
                                   subdir=get_subdir(cfg))
        log(f"never-shrink: {new_rel} is richer than current state "
            f"(state loss or unreadable); preserving it, diverting render to "
            f"{diverted}")
        new_rel = diverted
        new_abs = repo / new_rel

    # 4. Write the file. Re-ensure the parent dir: a `git rm` above can prune a
    #    now-empty month directory, which would otherwise make the write fail.
    new_abs.parent.mkdir(parents=True, exist_ok=True)
    new_abs.write_text(content, encoding="utf-8")

    state["file"] = new_rel
    save_state(session_id, state)

    index[new_rel] = session_id
    save_index(index)

    log(f"{event}: session={short_sid(session_id)} +{new_count} blocks "
        f"total={len(state['blocks'])} mode={mode} file={new_rel}")

    # Summary handed back to the caller so it can surface a GenTerminal inbox
    # notification without re-deriving the counts / title. `turns` counts user
    # prompts (one per turn) rather than every block, since state["blocks"]
    # also holds assistant replies and compact dividers.
    summary = {
        "new_count": new_count,
        "total": len(state["blocks"]),
        "turns": sum(1 for b in state["blocks"] if b.get("role") == "user"),
        # Notification title only — a manual /rename outranks the ai-title
        # (display_title); the archived document above keeps the ai-title.
        "title": user_session_name(session_id) or title,
        "file": new_rel,
    }

    # 5. Commit / push according to mode. Manual mode only writes the file;
    #    the /conversation-archiver:upload command commits + pushes on demand.
    #    do_commit=False (backfill) writes only — the caller commits + pushes
    #    once for the whole sweep instead of once per session.
    if not do_commit or mode != "auto":
        return summary

    run_git(repo, "add", "-A")
    status = run_git(repo, "status", "--porcelain")
    if not status.stdout.strip():
        return summary
    msg = f"archive: {start.strftime('%Y-%m-%d')} {title or short_sid(session_id)}"
    commit = run_git(repo, "commit", "-m", msg)
    if commit.returncode != 0:
        log(f"commit failed: {commit.stderr.strip()}")
        return summary
    push_background(repo)
    return summary


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR\n" + traceback.format_exc())
    sys.exit(0)  # never disrupt the session
