"""Every version of every CV, letter and application, and who made it.

The daily backup keeps a day; the review keeps what a document said before an
AI client's change until someone keeps or undoes it. Neither answers "what did
my CV say on Tuesday, before Claude rewrote it, and can I have that back":
this does. Each time a file in the workspace is seen to have changed, its new
text is kept, with when, who (you, Claude, Codex, or something outside CV
Studio) and through what (a tool, a save, a restore). A version is never
deleted to make another; restoring one is a version of its own.

It is not a hook on every place that writes, of which there are many: the app
and the MCP server each look at the files before and after anything that may
write (`sweep`), and a change found before is somebody else's, one found
after is theirs. A change made in another editor is caught the next time
either looks. The first look at a workspace keeps every file as it is then.

It lives beside the backups, outside the workspace: the workspace may be in
OneDrive or iCloud, which copy a store like this one badly mid-write. Each
text is kept once, compressed, by its hash, so a hundred saves of a CV that
went back and forth cost what its distinct versions cost.

Your own saves of one file within a few minutes are one version: the editor
saves as you type, and a version per keystroke would bury the ones that
matter. An AI client's every call stays a version of its own.

Standard library only.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import difflib
import hashlib
import json
import os
import threading
import uuid
import zlib
from pathlib import Path

LOG = "log.jsonl"
SEEN = "seen.json"
OBJECTS = "objects"
MERGE_MINUTES = 10
MAX_FILE = 4 * 1024 * 1024      # a CV or an application is a few kilobytes

_tlock = threading.Lock()


# --- the store ------------------------------------------------------------------

@contextlib.contextmanager
def _locked(store: Path):
    """One writer at a time, across the app and the MCP server."""
    store.mkdir(parents=True, exist_ok=True)
    with _tlock:
        fh = open(store / "lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:     # gives up after ten seconds; try again
                        continue
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _put(store: Path, data: bytes) -> str:
    h = hashlib.sha256(data).hexdigest()
    p = store / OBJECTS / h[:2] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{h}.{os.getpid()}.tmp")
        tmp.write_bytes(zlib.compress(data, 6))
        os.replace(tmp, p)
    return h


def content(store: Path, h: str) -> bytes:
    if not (isinstance(h, str) and len(h) == 64 and all(c in "0123456789abcdef" for c in h)):
        raise ValueError("not a version")
    return zlib.decompress((store / OBJECTS / h[:2] / h).read_bytes())


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, data) -> None:
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def entries(store: Path) -> list[dict]:
    """The log, oldest first."""
    out = []
    try:
        with open(store / LOG, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue            # a line cut short by a crash
    except OSError:
        pass
    return out


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _age_minutes(stamp: str) -> float:
    try:
        return (dt.datetime.now().astimezone() - dt.datetime.fromisoformat(stamp)).total_seconds() / 60
    except (TypeError, ValueError):
        return 1e9


# --- looking ----------------------------------------------------------------------

def sweep(store: Path, workspace: Path, files: list[Path], who: str,
          agent: str | None = None, tool: str | None = None, note: str | None = None) -> int:
    """Keep the new version of every file in `files` that changed since the
    last look, and mark the ones gone; all of it as `who`'s doing. On the
    first look, everything as it is, as where history begins. The number of
    versions kept."""
    with _locked(store):
        seen = _read_json(store / SEEN, None)
        first = seen is None
        seen = seen or {}
        log = entries(store) if not first else []
        last_by_path: dict[str, dict] = {}
        for e in log:
            last_by_path[e["path"]] = e
        now, kept, rewrite = _now(), 0, False
        present = set()
        new_lines: list[dict] = []
        for f in files:
            try:
                rel = f.resolve().relative_to(workspace.resolve()).as_posix()
                st = f.stat()
            except (OSError, ValueError):
                continue
            present.add(rel)
            sig = [st.st_mtime_ns, st.st_size]
            have = seen.get(rel)
            if have and have[:2] == sig:
                continue
            if st.st_size > MAX_FILE:
                continue
            try:
                data = f.read_bytes()
            except OSError:
                continue
            h = _put(store, data)
            seen[rel] = sig + [h]
            if have and have[2] == h:
                continue                # touched, not changed
            e = {"id": uuid.uuid4().hex[:12], "at": now, "path": rel, "hash": h, "size": len(data),
                 "who": "start" if first else who, "agent": None if first else agent,
                 "tool": None if first else tool, "note": None if first else note,
                 "prev": have[2] if have else None}
            prev = last_by_path.get(rel)
            # Your own saves of a file a few minutes apart are one version.
            if (not first and who == "you" and prev and prev.get("who") == "you"
                    and prev.get("tool") == tool and _age_minutes(prev.get("at")) < MERGE_MINUTES
                    and prev in log):
                e["prev"] = prev.get("prev")
                e["since"] = prev.get("since") or prev.get("at")
                log.remove(prev)
                rewrite = True
            last_by_path[rel] = e
            log.append(e)
            new_lines.append(e)
            kept += 1
        if not first:
            for rel in list(seen):
                if rel not in present:
                    e = {"id": uuid.uuid4().hex[:12], "at": now, "path": rel, "hash": None, "size": 0, "who": who,
                         "agent": agent, "tool": tool, "note": note, "prev": seen[rel][2]}
                    log.append(e)
                    new_lines.append(e)
                    del seen[rel]
                    kept += 1
        if rewrite:
            tmp = store / f".{LOG}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                for e in log:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            os.replace(tmp, store / LOG)
        elif new_lines:
            with open(store / LOG, "a", encoding="utf-8") as f:
                for e in new_lines:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
        _write_json(store / SEEN, seen)
        return kept


# --- reading ----------------------------------------------------------------------

def _out(e: dict, n: int) -> dict:
    return {"id": e.get("id") or str(n), "at": e.get("at"), "since": e.get("since"), "path": e.get("path"),
            "hash": e.get("hash"), "size": e.get("size"), "who": e.get("who"),
            "agent": e.get("agent"), "tool": e.get("tool"), "note": e.get("note"),
            "prev": e.get("prev"), "deleted": e.get("hash") is None}


def versions(store: Path, rel: str, limit: int = 300) -> list[dict]:
    """Every version of one file, newest first."""
    out = [_out(e, n) for n, e in enumerate(entries(store)) if e.get("path") == rel]
    return out[::-1][:limit]


def recent(store: Path, limit: int = 100) -> list[dict]:
    """The latest changes across the workspace, newest first; where history
    began is left out."""
    out = [_out(e, n) for n, e in enumerate(entries(store)) if e.get("who") != "start"]
    return out[::-1][:limit]


def version(store: Path, vid: str) -> dict:
    for n, e in enumerate(entries(store)):
        if (e.get("id") or str(n)) == str(vid):
            return _out(e, n)
    raise ValueError("No such version.")


def text_of(store: Path, h: str | None) -> str:
    if not h:
        return ""
    return content(store, h).decode("utf-8", errors="replace")


def diff(old: str, new: str, context: int = 3) -> list[dict]:
    """Line by line: {t: "same" | "add" | "del" | "skip", text}. A long run of
    unchanged lines keeps `context` lines beside each change and says how many
    it left out."""
    a, b = old.splitlines(), new.splitlines()
    ops = difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes()
    out: list[dict] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(ops):
        if tag != "equal":
            out += [{"t": "del", "text": x} for x in a[i1:i2]]
            out += [{"t": "add", "text": x} for x in b[j1:j2]]
            continue
        run = a[i1:i2]
        head = context if k > 0 else 0              # after the change before
        tail = context if k < len(ops) - 1 else 0   # before the change after
        if len(run) > head + tail + 1:
            out += [{"t": "same", "text": x} for x in run[:head]]
            out.append({"t": "skip", "text": f"{len(run) - head - tail} unchanged lines"})
            out += [{"t": "same", "text": x} for x in run[len(run) - tail:]]
        else:
            out += [{"t": "same", "text": x} for x in run]
    return out


def summary(old: str, new: str) -> dict:
    a, b = old.splitlines(), new.splitlines()
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return {"added": added, "removed": removed}
