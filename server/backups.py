"""Backups of a workspace: a zip a day, beside the app, the last fourteen kept.

A workspace is plain files, and it is meant to be the user's to copy. But nobody copies it until the day they need a copy, so the
app makes one: once a day, into the app's data folder (never the workspace,
which may itself be synced or versioned), one folder per workspace.

What goes in is what cannot be made again: the CVs, the letters, the
applications, the bookkeeping beside them, the photo and the company logos.
Rendered pages and previews are left out; they come back on the next render.
Every application is written whole to a new file renamed into place, so a
backup taken mid-write has the one before or the one after, never half.

A backup from before applications were files has applications.db in it;
restoring one moves its applications back out into files.

Standard library only.
"""

from __future__ import annotations

import hashlib
import threading
import time
import zipfile
from pathlib import Path

KEEP = 14
EVERY = 24 * 3600
DB = "applications.db"

# Left out: regenerable or not the user's. Paths are workspace-relative, POSIX.
SKIP_DIRS = (".trash/",)
SKIP_PREFIX = (".cvstudio-preview", ".cvstudio-theme")
KEEP_UNDER_ASSETS = ("assets/logos/",)

_lock = threading.Lock()


def folder(data_dir: Path, workspace: Path) -> Path:
    """This workspace's backups: named after it, keyed by where it is."""
    ws = workspace.resolve()
    key = hashlib.sha1(str(ws).encode("utf-8")).hexdigest()[:10]
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws.name)[:40] or "workspace"
    return data_dir / "backups" / f"{safe}-{key}"


def _wanted(rel: str) -> bool:
    if rel.startswith(DB + "-"):
        return False                      # a database's journal, never on its own
    if rel.startswith(SKIP_DIRS) or rel.split("/")[-1].startswith(SKIP_PREFIX):
        return False
    if rel.startswith("assets/"):
        return rel.startswith(KEEP_UNDER_ASSETS)
    return True


def listing(data_dir: Path, workspace: Path) -> list[dict]:
    """Backups of this workspace, newest first."""
    d = folder(data_dir, workspace)
    out = []
    for f in sorted(d.glob("*.zip"), reverse=True):
        st = f.stat()
        out.append({"name": f.name, "at": st.st_mtime, "size": st.st_size})
    return out


def make(data_dir: Path, workspace: Path, reason: str = "daily") -> dict:
    """Zip the workspace now. Returns the new backup's entry."""
    with _lock:
        d = folder(data_dir, workspace)
        d.mkdir(parents=True, exist_ok=True)
        # Names sort in the order they were made, to the millisecond, so the
        # newest is always first and pruning never takes it.
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}"
        dest = d / f"{stamp}-{reason}.zip"
        while dest.exists():
            time.sleep(0.002)
            now = time.time()
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"{int(now * 1000) % 1000:03d}"
            dest = d / f"{stamp}-{reason}.zip"
        part = dest.with_suffix(".part")
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(workspace.rglob("*")):
                if not f.is_file():
                    continue
                rel = f.relative_to(workspace).as_posix()
                if _wanted(rel):
                    z.write(f, rel)
        part.replace(dest)
        for old in sorted(d.glob("*.zip"), reverse=True)[KEEP:]:
            try:
                old.unlink()
            except OSError:
                pass
        st = dest.stat()
        return {"name": dest.name, "at": st.st_mtime, "size": st.st_size}


def due(data_dir: Path, workspace: Path) -> bool:
    items = listing(data_dir, workspace)
    return not items or time.time() - items[0]["at"] >= EVERY


def restore(data_dir: Path, workspace: Path, name: str) -> dict:
    """Put a backup's files back. What is there now is backed up first, so
    a restore can itself be undone; files the backup does not have are left
    alone."""
    src = folder(data_dir, workspace) / Path(name).name
    if not src.is_file() or src.suffix != ".zip":
        raise FileNotFoundError(name)
    before = make(data_dir, workspace, "before-restore")
    legacy = False
    with _lock, zipfile.ZipFile(src) as z:
        root = workspace.resolve()
        for info in z.infolist():
            target = (root / info.filename).resolve()
            if root not in target.parents or info.is_dir():
                continue                  # nothing outside the workspace
            target.parent.mkdir(parents=True, exist_ok=True)
            if info.filename == DB:
                legacy = True
                for side in ("-wal", "-shm"):
                    (root / (DB + side)).unlink(missing_ok=True)
            with z.open(info) as fh:
                target.write_bytes(fh.read())
    if legacy:
        import jobs
        jobs.migrate(workspace, overwrite=True)
    return {"restored": src.name, "before": before["name"]}
