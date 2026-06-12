#!/usr/bin/env python3
"""Claude Code conversation archiver — UserPromptSubmit / SessionEnd hook.

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
import subprocess
import sys
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
# Slash-command / local-command wrappers that show up as "user" string content.
_COMMAND_WRAPPER_RE = re.compile(r"^\s*<(command-[a-z-]+|local-command-[a-z-]+)>")


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


def parse_transcript(path: Path):
    """Yield ordered (key, role, text) blocks for new content.

    role is one of: "user", "assistant", "compact".
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
                    yield _entry_key(entry, "user", text), "user", text
            elif etype == "assistant":
                text = _assistant_text((entry.get("message") or {}).get("content"))
                if text:
                    yield _entry_key(entry, "assistant", text), "assistant", text
            elif etype == "system" and entry.get("subtype") == "compact_boundary":
                text = _compact_divider(entry)
                yield _entry_key(entry, "compact", text), "compact", text


def session_title(path: Path) -> str | None:
    """Last ai-title in the transcript, if any (titles are generated lazily)."""
    title = None
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
    except Exception:
        pass
    return title or None


def session_start(path: Path) -> datetime:
    """Local-time datetime of the first timestamped entry (stable per session)."""
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
                    blocks: list[dict]) -> str:
    n_user = sum(1 for b in blocks if b["role"] == "user")
    n_asst = sum(1 for b in blocks if b["role"] == "assistant")
    heading = title or f"Session {short_sid(session_id)}"

    lines = [
        f"# {heading}",
        "",
        f"- **Session**: `{session_id}`",
        f"- **Started**: {start.strftime('%Y-%m-%d %H:%M %z')}",
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


# --------------------------------------------------------------------------- #
# Git
# --------------------------------------------------------------------------- #

def run_git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    """Run git, tolerating a missing git binary or a timeout. A missing git
    (FileNotFoundError) or a command that exceeds `timeout` (TimeoutExpired)
    would otherwise raise mid-run and bypass every caller's returncode check —
    e.g. a slow `pull --rebase` would skip both the mid-rebase `rebase --abort`
    cleanup and the subsequent `git push`. Instead we return a synthetic
    non-zero CompletedProcess (rc=127 for missing git, rc=124 for timeout) so
    callers degrade gracefully and the failure is logged once."""
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        log("git not found on PATH")
        return subprocess.CompletedProcess(
            args=["git", *args], returncode=127, stdout="", stderr="git not found",
        )
    except subprocess.TimeoutExpired:
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
    link from the Claude Code connect dialog (the Claude Code tile on the
    user's Second Brain home). We POST the code to the
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
                "dialog by clicking the Claude Code tile on your Second "
                "Brain home and paste the fresh message."
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
    link from the Claude Code connect dialog (the Claude Code tile on the
    user's Second Brain home) — the script redeems the
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

    # Serialize concurrent hook runs: different sessions share _index.json and
    # the one git repo, so without a lock two runs could pick the same filename,
    # overwrite each other's markdown, and corrupt the index. The flock releases
    # when the fd closes at the end of the with-block.
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with open(ARCHIVE_LOCK, "w") as lockf:
        fcntl.flock(lockf, fcntl.LOCK_EX)
        _archive_locked(session_id, tpath, event)


def _archive_locked(session_id: str, tpath: Path, event: str,
                    do_commit: bool = True) -> None:
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
    content = render_markdown(session_id, start, title, state["blocks"])

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

    # 5. Commit / push according to mode. Manual mode only writes the file;
    #    the /conversation-archiver:upload command commits + pushes on demand.
    #    do_commit=False (backfill) writes only — the caller commits + pushes
    #    once for the whole sweep instead of once per session.
    if not do_commit or mode != "auto":
        return

    run_git(repo, "add", "-A")
    status = run_git(repo, "status", "--porcelain")
    if not status.stdout.strip():
        return
    msg = f"archive: {start.strftime('%Y-%m-%d')} {title or short_sid(session_id)}"
    commit = run_git(repo, "commit", "-m", msg)
    if commit.returncode != 0:
        log(f"commit failed: {commit.stderr.strip()}")
        return
    push_background(repo)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("ERROR\n" + traceback.format_exc())
    sys.exit(0)  # never disrupt the session
