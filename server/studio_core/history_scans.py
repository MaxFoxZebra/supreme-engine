"""Backups, version history, document rename/delete, and morning job scans."""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

from .context import (
    backups,
    cjkfonts,
    ctx,
    history,
    is_letter,
    jobstore,
    letters,
    posting,
    rel,
    review,
    safe_path,
    scans,
)
from . import documents as _docs
from . import letters_prep as _lp
from . import provenance as _prov
from . import workspace as _ws


def backup_dir() -> Path:
    return cjkfonts.cache_dir().parent


def history_dir() -> Path:
    """This workspace's history: beside its backups, named the same way."""
    b = backups.folder(backup_dir(), ctx.workspace)
    return b.parent.parent / "history" / b.name


def history_files() -> list[Path]:
    """What history keeps every version of: the documents and the applications."""
    out = [f for f, _, _ in _ws.document_files()]
    t = ctx.workspace / "tracker"
    if t.is_dir():
        out += [f for f in sorted(t.glob("*.yaml")) if not f.name.startswith(".")]
    return out


def note_history(who: str, agent: str | None = None, tool: str | None = None,
                 note: str | None = None) -> int:
    """Look for files that changed and keep their new versions, as `who`'s.
    Never what fails the call it sits beside."""
    if not ctx.workspace.is_dir() or _ws.in_sample():
        return 0
    try:
        return history.sweep(history_dir(), ctx.workspace, history_files(), who, agent, tool, note)
    except Exception as exc:
        print(f"history: {exc}", file=sys.stderr)
        return 0


def history_of(path: str) -> dict:
    """Every version of a document or an application's file, newest first."""
    rel_path = rel(safe_path(path))
    store = history_dir()
    out = history.versions(store, rel_path)
    for i, v in enumerate(out):
        older = out[i + 1]["hash"] if i + 1 < len(out) else None
        try:
            v["lines"] = history.summary(history.text_of(store, older or v.get("prev")),
                                         history.text_of(store, v["hash"]))
        except (OSError, ValueError):
            v["lines"] = None
    p = ctx.workspace / rel_path
    return {"path": rel_path, "exists": p.is_file(), "versions": out}


def history_diff(path: str, vid: str, against: str = "before") -> dict:
    """What a version changed: against the version before it, or against the
    file as it is now."""
    rel_path = rel(safe_path(path))
    store = history_dir()
    v = history.version(store, vid)
    if v["path"] != rel_path:
        raise ValueError("That version is of another file.")
    new = history.text_of(store, v["hash"])
    if against == "now":
        p = ctx.workspace / rel_path
        old, new = new, (p.read_text(encoding="utf-8", errors="replace") if p.is_file() else "")
    else:
        old = history.text_of(store, v.get("prev"))
    return {"version": v, "against": against, "lines": history.diff(old, new),
            "summary": history.summary(old, new)}


def history_recent(limit: int = 100) -> list[dict]:
    """The latest changes; an application's is labelled with its company and
    role, read from the version itself, since its file name says little."""
    store = history_dir()
    out = history.recent(store, limit)
    names: dict[str, str] = {}
    for c in out:
        if not c["path"].startswith("tracker/"):
            continue
        if c["path"] not in names:
            try:
                text = history.text_of(store, c["hash"] or c["prev"])
            except (OSError, ValueError):
                text = ""
            get = lambda k: (re.search(rf"^{k}:\s*['\"]?(.+?)['\"]?\s*$", text, re.M) or [None, ""])[1]
            names[c["path"]] = ", ".join(x for x in (get("company"), get("title")) if x)
        c["label"] = names[c["path"]] or None
    return out


def restore_version(path: str, vid: str, who: str = "you", agent: str | None = None,
                    tool: str = "restore") -> dict:
    """Put a document or an application back as one of its versions. The
    restore is a version of its own: nothing between is lost."""
    rel_path = rel(safe_path(path))
    store = history_dir()
    note_history("elsewhere")
    v = history.version(store, vid)
    if v["path"] != rel_path:
        raise ValueError("That version is of another file.")
    if v["deleted"]:
        raise ValueError("That version is the file being deleted. Restore the one before it.")
    text = history.text_of(store, v["hash"])
    p = ctx.workspace / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    if rel_path.startswith("tracker/"):
        if jobstore is None:
            raise ValueError("The application store is unavailable.")
        with jobstore._locked(ctx.workspace):
            tmp = p.with_name(f".{p.name}.restore.tmp")
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, p)
    else:
        _docs.write_doc(p, text, tool)
    when = (v.get("at") or "")[:16].replace("T", " ")
    note_history(who, agent, tool, note=f"Restored the version of {when}")
    return {"ok": True, "path": rel_path, "restored": v}


# --- job scans ----------------------------------------------------------------
# What a scheduled job search found, waiting for a yes or a no: a spreadsheet
# in a folder of the workspace (scans.py). A yes adds the application the way
# add_job does, posting read from its link; a no is remembered in the sheet,
# so the same job is not offered again.

OWN_FOLDERS = {"profile", "applications", "tracker", "letters", "assets"}


def scan_folder() -> Path:
    """Settings > Job scans, else `Job scans` in the workspace. The sample
    data keeps its own, wherever yours is."""
    if _ws.in_sample():
        return ctx.workspace / scans.DEFAULT_FOLDER
    return scans.folder_for(ctx.workspace, (_ws.load_prefs() or {}).get("scan_folder"))


def scan_folder_choices() -> list[str]:
    """The workspace's own folders a scan could write to: not CV Studio's."""
    try:
        return sorted((p.name for p in ctx.workspace.iterdir()
                       if p.is_dir() and not p.name.startswith((".", "_")) and p.name not in OWN_FOLDERS),
                      key=str.casefold)
    except OSError:
        return []


def _all_jobs() -> list[dict]:
    return jobstore.list_jobs(ctx.workspace) if jobstore is not None else []


def scans_state() -> dict:
    st = scans.state(scan_folder(), _all_jobs())
    st["setting"] = (_ws.load_prefs() or {}).get("scan_folder") or ""
    st["default"] = scans.DEFAULT_FOLDER
    st["choices"] = scan_folder_choices()
    return st


def scan_record(found: list[dict], scan: str | None = None, day: str | None = None,
                by: str = "") -> dict:
    """Record what a scan found. A job handed over with a match is scored
    here as an application would be: the score in the sheet's Score column,
    the reasons beside it, and {scored: [{key, company, role, score}]}."""
    folder, scored, matches = scan_folder(), [], {}
    found = [dict(j) for j in found]
    for j in found:
        m = j.pop("match", None)
        if not m or jobstore is None:
            continue
        m = jobstore.clean_match({**m, "by": by, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        key = scans.key_of({"link": j.get("link") or j.get("url"), "company": j.get("company"),
                            "role": j.get("role") or j.get("title")})
        if not key:
            continue
        j["details"] = {**(j.get("details") if isinstance(j.get("details"), dict) else {}),
                        scans.SCORE: f"{m['score']:.1f}"}
        matches[key] = m
        scored.append({"key": key, "company": j.get("company") or "",
                       "role": j.get("role") or j.get("title") or "", "score": m["score"]})
    r = scans.record(folder, found, _all_jobs(), scan, day)
    scans.save_matches(folder, matches)
    return {**r, "scored": scored}


def scan_decide(keys: list[str], status: str) -> dict:
    return scans.set_status(scan_folder(), [str(k) for k in keys or []], status, _all_jobs())


def scan_add(key: str) -> dict:
    """Yes to a job a scan found: it becomes an application, with the
    posting's own title and text when its link can be read, and the scan's
    reason in its notes. {job, posting: read | unread | none, already}."""
    if jobstore is None:
        raise ValueError("The application store is unavailable.")
    folder = scan_folder()
    row = scans.find(folder, key)
    if row is None:
        raise ValueError("That job is no longer in the scans folder.")
    jobs = _all_jobs()
    index = scans.tracked_index(jobs)
    if scans.is_tracked(row, index):
        link = scans.norm_link(row.get("link"))
        pair = (scans._fold(row.get("company")), scans._fold(row.get("role")))
        have = next((j for j in jobs if link and scans.norm_link(j.get("url")) == link), None) or next(
            (j for j in jobs if (scans._fold(j.get("company")), scans._fold(j.get("title"))) == pair), None)
        scans.set_status(folder, [key], scans.ADDED, jobs)
        return {"ok": True, "job": have, "already": True, "posting": "none"}
    company, title = row.get("company") or "", row.get("role") or ""
    url, location = row.get("link") or None, row.get("location") or None
    description = row.get("description") or None
    read_how = "none"
    if url:
        try:
            got = posting.read(url)
        except Exception:  # the application matters, the reading does not
            read_how = "unread"
        else:
            read_how = "read"
            title = got.get("title") or title
            description = got.get("description") or description
            location = location or got.get("location")
            company = company or got.get("company") or ""
    notes = "\n".join(x for x in (row.get("why"), row.get("notes")) if x) or None
    job = jobstore.add_job(ctx.workspace, {
        "company": company, "title": title, "url": url, "location": location,
        "source": row.get("scan") or "Job scan", "description": description, "notes": notes,
        "logo": _ws.stored_logo(company) if company else None})
    scans.set_status(folder, [key], scans.ADDED, jobs + [job])
    # The score the scan gave it comes along, fingerprinted against the
    # posting as saved now. No posting or no base CV: it stays on the card.
    m = scans.load_matches(folder).get(key)
    if m and description:
        try:
            job["match"] = _lp.save_match(job["id"], {k: v for k, v in m.items()
                                                  if k in ("criteria", "summary", "strengths", "gaps")},
                                      by=m.get("by") or "")
        except ValueError:
            pass
    return {"ok": True, "job": job, "already": False, "posting": read_how}


def scan_undo(key: str, job_id: str | None = None) -> dict:
    """Take a yes or a no back: the application a yes made goes to the trash,
    and the job is waiting again."""
    if job_id and jobstore is not None:
        jobstore.delete_job(ctx.workspace, job_id)
    return scans.set_status(scan_folder(), [key], scans.NEW, _all_jobs())


def start_backups() -> None:
    """The day's backup of the workspace you work in, checked each hour.
    Never of sample data, which is made fresh every time anyway."""
    def loop() -> None:
        time.sleep(20)                  # out of the way of the first render
        while True:
            try:
                if not _ws.in_sample() and ctx.workspace.is_dir() and backups.due(backup_dir(), ctx.workspace):
                    backups.make(backup_dir(), ctx.workspace)
            except Exception as exc:    # a failed backup must never take the app down
                print(f"backup failed: {exc}", file=sys.stderr)
            time.sleep(3600)
    threading.Thread(target=loop, daemon=True).start()


def _rewrite_refs(old: str, new: str | None) -> None:
    """Point everything that named `old` at `new` (or at nothing): the
    bookkeeping beside the documents, the applications, and the letters that
    say which CV they look like."""
    data = _prov._edits_read()
    docs = data.get("docs") or {}
    entry = docs.pop(old, None)
    if entry is not None and new:
        docs[new] = entry

    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return new if x == old else x
    _prov._edits_write(walk(data))
    if jobstore is not None:
        for j in jobstore.list_jobs(ctx.workspace):
            for key in ("cv_path", "letter_path"):
                if j.get(key) == old:
                    jobstore.update_job(ctx.workspace, j["id"], {key: new})
    for f in (ctx.workspace / "letters").glob("*" + letters.EXT):
        try:
            meta, body = letters.parse(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if any(v == old for v in meta.values()):
            meta = {k: (new if v == old else v) for k, v in meta.items()}
            f.write_text(letters.dump(meta, body), encoding="utf-8")


def _clean_name(raw: str) -> str:
    return "".join(c for c in (raw or "") if c.isalnum() or c in "-_ ").strip()


def rename_document(path: str, name: str) -> dict:
    """Rename a CV or a letter where it is, keeping a translation's language
    suffix (my-cv.fr.yaml stays .fr), and repoint everything that named it."""
    src = safe_path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    clean = _clean_name(name)
    if not clean:
        raise ValueError("Please give it a name.")
    ext = src.suffix
    stem = src.name[: -len(ext)] if ext else src.name
    link = ((_prov._edits_read().get("docs") or {}).get(rel(src)) or {}).get("translation") or {}
    suffix = ""
    m = re.match(r"^.+(\.[a-z]{2}(?:-[a-z]{2})?)$", stem, re.I)
    if link and m:
        suffix = m.group(1)
    dest = src.with_name(clean + suffix + ext)
    # As text: Windows paths compare without case, and my-cv to My-CV is a
    # rename all the same.
    if str(dest) == str(src):
        return {"ok": True, "path": rel(src)}
    if dest.exists() and not _same_doc(dest, src):
        raise ValueError(f"Something called {dest.name} already exists.")
    was = rel(src)
    src.rename(dest)
    now = rel(dest)
    _rewrite_refs(was, now)
    review.moved(ctx.workspace, was, now)
    return {"ok": True, "path": now, "was": was}


def _same_doc(a: Path, b: Path) -> bool:
    """Whether two names are one file: my-cv and My-CV are, on the file
    systems Windows and macOS use, so that rename only changes the case."""
    try:
        return a.samefile(b)
    except OSError:
        return False


def delete_document(path: str) -> dict:
    """Move a CV or a letter to the workspace's .trash folder and detach it
    from its application. The base CV, and a document other languages are
    translated from, are refused: both would leave something pointing at
    nothing."""
    src = safe_path(path)
    if not src.is_file():
        raise FileNotFoundError(path)
    rp = rel(src)
    base = _prov.base_cv()
    if base and base.get("path") == rp:
        raise ValueError("This is the base CV, which every tailored CV is copied from. "
                         "Choose another base first, on Documents.")
    kids = [d for d in _ws.list_documents() if d.get("translation_of") == rp]
    if kids:
        raise ValueError("Other languages are translated from it ("
                         + ", ".join(sorted(str(d.get("lang")) for d in kids))
                         + "). Delete those first.")
    trash = ctx.workspace / ".trash"
    trash.mkdir(exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    dest = trash / f"{stamp}-{src.name}"
    n = 1
    while dest.exists():
        # Two documents of one name, deleted in the same second.
        n += 1
        dest = trash / f"{stamp}-{n}-{src.name}"
    shutil.move(str(src), str(dest))
    _rewrite_refs(rp, None)
    review.moved(ctx.workspace, rp, None)
    return {"ok": True, "trashed": dest.relative_to(ctx.workspace).as_posix()}




def scans_stamp() -> str | None:
    """A fingerprint of the scans folder, so a morning's search shows up in
    the open app without waiting for a reload."""
    try:
        return scans.stamp(scan_folder())
    except Exception:
        return None


