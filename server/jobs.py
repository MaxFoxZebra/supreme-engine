"""Job application records: one YAML file each, in the workspace's tracker folder.

CVs are documents and applications are records, but both are the user's, so
both are plain files in the folder they own. An application is
tracker/acme-backend-engineer-3f9a1c2e.yaml, readable and editable in any
editor, and the workspace can sit in OneDrive, Dropbox, iCloud or Syncthing and
follow them from one computer to the next. A database file could not: a sync
client copies it whole, mid-write or from two machines at once, and the whole
tracker goes with it. With a file each, the worst a sync conflict can do is
leave two copies of one application, and the newer one is shown.

Parsing hundreds of YAML files on every list would be slow, so what was read is
kept in an index outside the workspace, beside the backups, keyed by each
file's size and modification time. It is only ever a copy: delete it and the
next list reads the files again. This is how Obsidian keeps a vault of
Markdown files fast.

Two processes write here, the app and an AI client's MCP server. Every write
goes to a temporary file renamed into place, so a reader never sees half of
one, and every read-modify-write holds a lock (on this machine, outside the
workspace), so two status changes cannot both read the same history and drop
one of its entries.

A workspace from before this kept its applications in applications.db. The
first read moves them out into files and the database into .trash.

The record follows the one from the job tracker this was ported from, including
`status_history`, which is what makes the funnel chart meaningful over time.
Export to JSON and CSV stays, for spreadsheets.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path

# The status vocabulary. `rejected_interviewing` and `ghosted_interviewing`
# exist so the funnel can tell a rejection after a first application apart from
# one after interviews, which are very different signals about an application.
STATUSES = [
    "pending", "applied", "interviewing", "offer", "accepted",
    "refused", "rejected", "ghosted", "rejected_interviewing", "ghosted_interviewing",
]

# Each funnel node is the set of statuses that have reached that stage, so the
# chart shows cumulative progress rather than only where things sit right now.
#
# Rejections and ghostings are two nodes each, before and after interviews.
# They could be one, but then a single node would be the target of two
# different stages, which makes the sankey ambiguous to read -- and the two
# say very different things about a CV.
NODE_STATUSES: dict[str, list[str]] = {
    "all":         STATUSES,
    "pending":     ["pending"],
    "applied_s":   ["applied", "interviewing", "offer", "accepted", "refused",
                    "rejected", "ghosted", "rejected_interviewing", "ghosted_interviewing"],
    "awaiting":    ["applied"],
    "interview_s": ["interviewing", "offer", "accepted", "refused",
                    "rejected_interviewing", "ghosted_interviewing"],
    "still_iv":    ["interviewing"],
    "offer_s":     ["offer", "accepted", "refused"],
    "deciding":    ["offer"],
    "accepted":    ["accepted"],
    "refused":     ["refused"],
    "rejected":    ["rejected"],
    "ghosted":     ["ghosted"],
    "rejected_iv": ["rejected_interviewing"],
    "ghosted_iv":  ["ghosted_interviewing"],
}

LABELS = {
    "all": "All applications", "pending": "Draft", "applied_s": "Applied",
    "awaiting": "Awaiting reply", "interview_s": "Interviewed",
    "still_iv": "Interviewing", "offer_s": "Offers", "deciding": "Offer pending",
    "accepted": "Accepted", "refused": "Declined by me", "rejected": "Rejected",
    "ghosted": "Ghosted", "rejected_iv": "Rejected after interview",
    "ghosted_iv": "Ghosted after interview",
}

# source, target, and the statuses that flow along that edge.
FLOWS: list[tuple[str, str, list[str]]] = [
    ("all", "pending", ["pending"]),
    ("all", "applied_s", NODE_STATUSES["applied_s"]),
    ("applied_s", "awaiting", ["applied"]),
    ("applied_s", "interview_s", NODE_STATUSES["interview_s"]),
    ("applied_s", "rejected", ["rejected"]),
    ("applied_s", "ghosted", ["ghosted"]),
    ("interview_s", "still_iv", ["interviewing"]),
    ("interview_s", "offer_s", NODE_STATUSES["offer_s"]),
    ("interview_s", "rejected_iv", ["rejected_interviewing"]),
    ("interview_s", "ghosted_iv", ["ghosted_interviewing"]),
    ("offer_s", "deciding", ["offer"]),
    ("offer_s", "accepted", ["accepted"]),
    ("offer_s", "refused", ["refused"]),
]

DIR = "tracker"
TRASH_DIR = ".trash/tracker"
LEGACY_DB = "applications.db"

# The order a file's keys are written in: what you look for first at the top,
# the long text at the bottom, the bookkeeping last. Keys someone added by hand
# are kept, after these. Empty ones are left out; reading puts them back.
ORDER = [
    "company", "title", "status", "location", "country", "url", "source",
    "language", "score", "salary_expected", "salary_offered", "salary_currency",
    "followup_date", "interview_at", "interview_tz", "contact_email",
    "last_contact_at", "cv_path", "letter_path", "logo", "people", "rounds",
    "status_history", "prep", "notes", "description",
    "id", "created_at", "updated_at",
]

FIELDS = [
    "title", "company", "location", "country", "description", "url", "source",
    "score", "status", "notes", "followup_date", "interview_at", "interview_tz",
    "contact_email", "last_contact_at", "salary_expected",
    "salary_offered", "salary_currency", "cv_path", "letter_path", "logo",
    "language",
]

# How the three alert rules are tuned. Here rather than buried in alerts() so
# that changing "silent" from a fortnight is a one-line edit with the reasoning
# next to it.
#
# INTERVIEW_SOON_DAYS is a week because that is the horizon over which you can
# still prepare. SILENT_DAYS is a fortnight because most employers that intend
# to answer have done so by then, and a shorter window would nag about
# applications that are simply still being read.
INTERVIEW_SOON_DAYS = 7
SILENT_DAYS = 14

# Statuses where nothing is expected to happen again, derived rather than
# hand-written so that adding a status to STATUSES cannot silently create a
# terminal one the alerts keep nagging about.
TERMINAL = {"accepted", "refused", "rejected", "ghosted",
            "rejected_interviewing", "ghosted_interviewing"}



# What is kept on disk besides FIELDS. Everything a list returns, so a row
# read back is the row that was written.
STORED = ["id", *FIELDS, "people", "rounds", "prep", "status_history",
          "created_at", "updated_at"]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def tracker_dir(workspace: Path) -> Path:
    return workspace / DIR


# --- the files ---------------------------------------------------------------

def _plain(v):
    """A value as JSON would hold it. YAML reads an unquoted 2026-09-12 as a
    date; everything here compares and sorts them as ISO text."""
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None).isoformat(timespec="seconds")
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _same(a, b) -> bool:
    """Equal as stored: an empty field is not written, so "" and None and []
    are the same nothing."""
    return (None if _empty(a) else a) == (None if _empty(b) else b)


def _block(v):
    """Notes and postings as indented blocks rather than one long quoted line."""
    from ruamel.yaml.scalarstring import LiteralScalarString
    if isinstance(v, str) and "\n" in v and all(c in "\n\t" or c.isprintable() for c in v):
        return LiteralScalarString(v)
    if isinstance(v, dict):
        return {k: _block(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_block(x) for x in v]
    return v


def _text(rec: dict) -> str:
    from ruamel.yaml import YAML
    out = {k: _block(rec[k]) for k in ORDER + [k for k in rec if k not in ORDER]
           if k in rec and not _empty(rec[k])}
    y = YAML(typ="rt")
    y.width = 4096                  # a long line stays one line
    y.allow_unicode = True
    y.indent(mapping=2, sequence=4, offset=2)
    buf = io.StringIO()
    y.dump(out, buf)
    return buf.getvalue()


def _parse(path: Path, mtime: float) -> dict | None:
    """One file as a record, or None when it is not one (broken YAML, a list,
    a sync client's half-written copy). A file written by hand needs no id or
    dates: the file's name and time stand in until the app next saves it."""
    from ruamel.yaml import YAML
    try:
        data = YAML(typ="safe", pure=True).load(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    rec = _plain(data)
    rec["id"] = str(rec.get("id") or hashlib.sha1(path.name.encode("utf-8")).hexdigest())
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(mtime))
    rec.setdefault("created_at", stamp)
    rec.setdefault("updated_at", stamp)
    return rec


def _replace(tmp: Path, dest: Path) -> None:
    # Windows refuses to replace a file someone has open, and a sync client or
    # the other process reading it counts; that lasts milliseconds.
    for attempt in range(40):
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def _write(path: Path, rec: dict) -> None:
    """Whole or not at all: a reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_text(rec))
    _replace(tmp, path)


def _slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _filename(rec: dict) -> str:
    """company-role-id.yaml: findable by eye, unique by the id. Given once,
    when the application is made; a renamed role keeps its file."""
    words = [_slug(rec.get(k))[:40].strip("-") for k in ("company", "title")]
    stem = "-".join(w for w in words if w) or "application"
    return f"{stem}-{rec['id'][:8]}.yaml"


# --- the index, and the lock: both outside the workspace, on this machine ----

def _key(workspace: Path) -> str:
    ws = workspace.resolve()
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in ws.name)[:40] or "workspace"
    return f"{safe}-{hashlib.sha1(str(ws).encode('utf-8')).hexdigest()[:10]}"


def _index_dir() -> Path:
    try:
        import cjkfonts
        return cjkfonts.cache_dir().parent / "index"
    except Exception:
        import tempfile
        return Path(tempfile.gettempdir()) / "cv-studio-index"


INDEX_VERSION = 1

# Per workspace: {"files": {name: [mtime_ns, size, inode, record]}, "ids":
# {id: name}, "dirty": bool}. The record is None for a file that is not one.
_mem: dict[str, dict] = {}
_mem_lock = threading.Lock()
_conflicts: dict[str, dict[str, list[str]]] = {}


def _load_index(workspace: Path) -> dict:
    try:
        data = json.loads((_index_dir() / f"{_key(workspace)}.json").read_text(encoding="utf-8"))
        if data.get("version") == INDEX_VERSION and isinstance(data.get("files"), dict):
            return {"files": data["files"], "ids": {}, "dirty": False}
    except (OSError, ValueError, AttributeError):
        pass
    return {"files": {}, "ids": {}, "dirty": False}


def _save_index(workspace: Path, state: dict) -> None:
    d = _index_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{_key(workspace)}.json"
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"version": INDEX_VERSION, "files": state["files"]},
                                  ensure_ascii=False), encoding="utf-8")
        _replace(tmp, path)
        state["dirty"] = False
    except OSError:
        pass                            # only ever a copy: the files are the truth


_tlocks: dict[str, threading.RLock] = {}
_held = threading.local()


@contextlib.contextmanager
def _locked(workspace: Path):
    """One writer at a time on this workspace, across processes. Reentrant,
    because a write reads first and the first read may migrate."""
    key = _key(workspace)
    with _mem_lock:
        rl = _tlocks.setdefault(key, threading.RLock())
    with rl:
        depth = getattr(_held, "depth", {})
        _held.depth = depth
        if depth.get(key):
            depth[key] += 1
            try:
                yield
            finally:
                depth[key] -= 1
            return
        d = _index_dir()
        d.mkdir(parents=True, exist_ok=True)
        fh = open(d / f"{key}.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                        break
                    except OSError:     # LK_LOCK gives up after ten seconds
                        continue
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            depth[key] = 1
            try:
                yield
            finally:
                depth[key] = 0
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


def _sig(st: os.stat_result) -> list:
    # The inode as well: every write is a new file renamed into place, so it
    # changes even when the size and the clock tick do not. Not on Windows,
    # where a directory listing reports it as 0 and a stat does not, and
    # NTFS keeps time to a tenth of a microsecond anyway.
    return [st.st_mtime_ns, st.st_size, 0 if os.name == "nt" else st.st_ino]


def _scan(workspace: Path) -> dict:
    """The folder as it is now, reading only the files that changed."""
    d = tracker_dir(workspace)
    key = _key(workspace)
    with _mem_lock:
        state = _mem.get(key)
        if state is None:
            state = _mem[key] = _load_index(workspace)
        files = state["files"]
        seen: dict[str, list] = {}
        try:
            entries = list(os.scandir(d))
        except OSError:
            entries = []
        for e in entries:
            if e.name.startswith(".") or not e.name.endswith((".yaml", ".yml")):
                continue
            try:
                if not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            sig = _sig(st)
            hit = files.get(e.name)
            if hit and hit[:3] == sig:
                seen[e.name] = hit
            else:
                seen[e.name] = [*sig, _parse(Path(e.path), st.st_mtime)]
                state["dirty"] = True
        if seen.keys() != files.keys():
            state["dirty"] = True
        state["files"] = seen
        # One application, two files: a sync client keeping both sides of a
        # conflict ("… (conflicted copy).yaml"), or a copy made by hand. The
        # newer is the application; the others are listed by conflicts().
        ids: dict[str, str] = {}
        dup: dict[str, list[str]] = {}
        for name in sorted(seen):
            rec = seen[name][3]
            if not rec:
                continue
            other = ids.get(rec["id"])
            if other is None:
                ids[rec["id"]] = name
                continue
            # A tie goes to the shorter name: the copy is the one the sync
            # client added words to.
            mine = (str(rec.get("updated_at") or ""), -len(name))
            theirs = (str(seen[other][3].get("updated_at") or ""), -len(other))
            keep, drop = (name, other) if mine > theirs else (other, name)
            ids[rec["id"]] = keep
            dup.setdefault(rec["id"], []).append(drop)
        state["ids"] = ids
        _conflicts[key] = dup
        if state["dirty"]:
            _save_index(workspace, state)
        return state


def _records(workspace: Path) -> list[dict]:
    if (workspace / LEGACY_DB).exists():
        migrate(workspace)
    state = _scan(workspace)
    return [state["files"][n][3] for n in state["ids"].values()]


def _find(workspace: Path, job_id: str) -> tuple[str, dict] | None:
    """The file an application is in, without rescanning the folder when
    the one file it was in last time is still as it was."""
    if (workspace / LEGACY_DB).exists():
        migrate(workspace)
    key = _key(workspace)
    with _mem_lock:
        state = _mem.get(key)
        name = state and state["ids"].get(job_id)
        if name:
            try:
                if _sig((tracker_dir(workspace) / name).stat()) == state["files"][name][:3]:
                    return name, state["files"][name][3]
            except OSError:
                pass
    state = _scan(workspace)
    name = state["ids"].get(job_id)
    return (name, state["files"][name][3]) if name else None


def _put(workspace: Path, name: str, rec: dict) -> None:
    """Write one application and remember it as written."""
    path = tracker_dir(workspace) / name
    _write(path, rec)
    key = _key(workspace)
    with _mem_lock:
        state = _mem.get(key)
        if state is not None:
            try:
                state["files"][name] = [*_sig(path.stat()), _plain(json.loads(json.dumps(rec)))]
                state["ids"][rec["id"]] = name
                state["dirty"] = True
            except OSError:
                pass


def _new_name(workspace: Path, rec: dict) -> str:
    name = _filename(rec)
    if (tracker_dir(workspace) / name).exists():
        name = name[:-5] + rec["id"][8:16] + ".yaml"
    return name


def conflicts(workspace: Path) -> dict[str, list[str]]:
    """Applications that are in more than one file, by id: the files that
    are not the one shown, in tracker/."""
    _records(workspace)
    return dict(_conflicts.get(_key(workspace), {}))


def stamp(workspace: Path) -> str:
    """A fingerprint of the folder, for the open app to notice a change made
    by another process (an AI client, a sync client, an editor) every couple
    of seconds. Stats every file and parses only the changed ones."""
    state = _scan(workspace)
    h = hashlib.sha1()
    for name in sorted(state["files"]):
        h.update(f"{name}:{state['files'][name][:3]};".encode("utf-8"))
    return f"{len(state['ids'])}:{h.hexdigest()[:16]}"


# --- moving out of applications.db -------------------------------------------

def _from_sqlite(row: dict) -> dict:
    rec = {k: v for k, v in row.items() if k in STORED}
    for k, empty in (("status_history", []), ("people", []), ("rounds", []), ("prep", None)):
        try:
            rec[k] = json.loads(rec.get(k) or "null") or empty
        except (TypeError, ValueError):
            rec[k] = empty
    return rec


def migrate(workspace: Path, overwrite: bool = False) -> int:
    """Move applications.db's applications into files, its trash into
    .trash/tracker, and the database itself into .trash. Returns how many
    applications were written.

    Only applications that have no file yet are written, so a second computer
    opening a synced workspace whose database it still has does not bring
    back what the first one deleted or put an old status over a new one.
    `overwrite` is for a restored backup, where the database is what was
    asked for.
    """
    import sqlite3
    db = workspace / LEGACY_DB
    with _locked(workspace):
        if not db.exists():
            return 0
        con = sqlite3.connect(db, timeout=15.0)
        con.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            rows = [dict(r) for r in con.execute("SELECT * FROM jobs")] if "jobs" in tables else []
            trashed = [dict(r) for r in con.execute("SELECT * FROM trash")] if "trash" in tables else []
        finally:
            con.close()
        state = _scan(workspace)
        binned = {t["id"] for t in _trash_files(workspace).values()}
        written = 0
        for row in rows:
            rec = _from_sqlite(row)
            name = state["ids"].get(rec["id"])
            if (name and not overwrite) or (rec["id"] in binned and not overwrite):
                continue
            _put(workspace, name or _new_name(workspace, rec), rec)
            written += 1
        for t in trashed:
            if t["id"] in binned or t["id"] in state["ids"]:
                continue
            try:
                rec = _from_sqlite(json.loads(t["data"]))
            except (TypeError, ValueError):
                continue
            rec["deleted_at"] = t["deleted_at"]
            _write(workspace / TRASH_DIR / _filename(rec), rec)
        bin_ = workspace / ".trash"
        bin_.mkdir(exist_ok=True)
        try:
            db.replace(bin_ / f"{time.strftime('%Y%m%d-%H%M%S')}-{LEGACY_DB}")
            for side in ("-wal", "-shm"):
                (workspace / (LEGACY_DB + side)).unlink(missing_ok=True)
        except OSError:
            pass        # still open somewhere; nothing is written twice next time
        return written


def set_company_logo(workspace: Path, company: str, logo: str) -> int:
    """Point every application to one company at the same logo.

    Applications are per-role but a logo belongs to the company, so this is
    matched on the name rather than set on one application.
    """
    want = company.strip().lower()
    n = 0
    with _locked(workspace):
        state = _scan(workspace)
        for job_id, name in list(state["ids"].items()):
            rec = state["files"][name][3]
            if str(rec.get("company") or "").lower() != want:
                continue
            n += 1
            if rec.get("logo") != logo:
                _put(workspace, name, dict(rec, logo=logo, updated_at=_now()))
    return n


def _row(rec: dict) -> dict:
    """A stored application as everything else reads it: every field there,
    empty ones as None, the lists as lists, and its own copies of them."""
    d = {k: copy.deepcopy(rec.get(k)) for k in STORED}
    d["status"] = d["status"] or "pending"
    d["salary_currency"] = d["salary_currency"] or "EUR"
    for k in ("status_history", "people", "rounds"):
        if not isinstance(d[k], list):
            d[k] = []
    if not isinstance(d["prep"], dict):
        d["prep"] = None
    # Before people, an application had one contact email. It is the first
    # person until someone is added.
    if not d["people"] and d.get("contact_email"):
        d["people"] = [{"id": "contact", "name": "", "role": "", "email": d["contact_email"],
                        "link": "", "last": ""}]
    # Before rounds, an application had one interview time. It is the first
    # round until the rounds are written.
    if not d["rounds"] and d.get("interview_at"):
        d["rounds"] = [{"id": "iv", "kind": "", "at": str(d["interview_at"])[:16],
                        "tz": d.get("interview_tz") or "", "with": "", "outcome": "", "note": ""}]
    return d


# One interview round: what kind, when (local time in `tz`, empty for "not
# scheduled yet"), with whom, and how it went ("" until you say).
ROUND_FIELDS = {"kind": 60, "at": 16, "tz": 64, "with": 120, "outcome": 8, "note": 400}
ROUND_OUTCOMES = ("", "passed", "failed")


def clean_rounds(rounds) -> list[dict]:
    """The rounds as they may be stored: known fields, a date and time or
    nothing, a real time zone, an outcome from the short list; twelve at most."""
    if not isinstance(rounds, list):
        raise ValueError("Rounds must be a list.")
    out = []
    for r in rounds[:12]:
        if not isinstance(r, dict):
            continue
        q = {k: str(r.get(k) or "").strip()[:n] for k, n in ROUND_FIELDS.items()}
        if q["at"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", q["at"]):
            raise ValueError(f"{q['at']} is not a date and time (YYYY-MM-DDTHH:MM).")
        if q["tz"]:
            valid_tz(q["tz"])
        if q["outcome"] not in ROUND_OUTCOMES:
            raise ValueError(f"Unknown outcome: {q['outcome']}. Use passed, failed or nothing.")
        q["id"] = str(r.get("id") or "") or uuid.uuid4().hex[:8]
        out.append(q)
    return out


def interview_of(rounds: list[dict]) -> tuple[str | None, str | None]:
    """The one interview time everything else reads (calendar, reminders, the
    countdown, an AI client): the first round not decided yet that has a time,
    else the last round that had one. Rounds happen in order, so the list
    order is the time order without converting any zones."""
    timed = [r for r in rounds if r.get("at")]
    nxt = next((r for r in timed if not r.get("outcome")), None) or (timed[-1] if timed else None)
    return (nxt["at"] + ":00", nxt.get("tz") or None) if nxt else (None, None)


# Interview prep: the likely questions (each with where it comes from and
# your notes), the stories that back the posting's asks, and the questions to
# ask them. Stored whole, as the app or an AI client last wrote it.
PREP_SOURCES = ("posting", "cv", "round", "you")


def clean_prep(prep) -> dict | None:
    if not isinstance(prep, dict):
        return None
    cut = lambda v, n: str(v or "").strip()[:n]
    qs = []
    for q in (prep.get("questions") or [])[:40]:
        if not isinstance(q, dict) or not cut(q.get("q"), 400):
            continue
        qs.append({"id": cut(q.get("id"), 16) or uuid.uuid4().hex[:8], "q": cut(q.get("q"), 400),
                   "src": q.get("src") if q.get("src") in PREP_SOURCES else "you",
                   "why": cut(q.get("why"), 400), "cv": cut(q.get("cv"), 400),
                   "note": cut(q.get("note"), 2000),
                   "state": q.get("state") if q.get("state") in ("work", "got") else ""})
    stories = []
    for st in (prep.get("stories") or [])[:20]:
        if isinstance(st, dict) and cut(st.get("req"), 200):
            stories.append({"req": cut(st.get("req"), 200), "proof": cut(st.get("proof"), 300),
                            "where": cut(st.get("where"), 80)})
    asks = []
    for a in (prep.get("asks") or [])[:12]:
        if isinstance(a, dict) and cut(a.get("q"), 300):
            asks.append({"id": cut(a.get("id"), 16) or uuid.uuid4().hex[:8], "q": cut(a.get("q"), 300),
                         "keep": bool(a.get("keep"))})
    return {"questions": qs, "stories": stories, "asks": asks, "by": cut(prep.get("by"), 60),
            "at": cut(prep.get("at"), 10), "cv_read": bool(prep.get("cv_read"))}


PERSON_FIELDS = {"name": 120, "role": 80, "email": 200, "link": 400, "last": 10}


def clean_people(people) -> list[dict]:
    """The people on an application as they may be stored: known fields only,
    trimmed, an email that looks like one, a web link, a date; nobody without
    a name or an email; twenty at most."""
    if not isinstance(people, list):
        raise ValueError("People must be a list.")
    out = []
    for p in people[:20]:
        if not isinstance(p, dict):
            continue
        q = {k: str(p.get(k) or "").strip()[:n] for k, n in PERSON_FIELDS.items()}
        if q["email"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", q["email"]):
            raise ValueError(f"{q['email']} is not an email address.")
        if q["link"] and not re.match(r"https?://", q["link"]):
            q["link"] = "https://" + q["link"]
        if q["last"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", q["last"]):
            q["last"] = ""
        if not (q["name"] or q["email"]):
            continue
        q["id"] = str(p.get("id") or "") or uuid.uuid4().hex[:8]
        out.append(q)
    return out


def list_jobs(workspace: Path, status: str | None = None, q: str | None = None,
              node: str | None = None) -> list[dict]:
    rows = [_row(r) for r in _records(workspace)]
    if node and node in NODE_STATUSES:
        rows = [r for r in rows if r["status"] in NODE_STATUSES[node]]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if q:
        needle = q.casefold()
        rows = [r for r in rows if any(needle in str(r.get(k) or "").casefold()
                                       for k in ("title", "company", "notes"))]
    rows.sort(key=lambda r: str(r.get("updated_at") or ""), reverse=True)
    return rows


def add_job(workspace: Path, data: dict) -> dict:
    if not (data.get("title") and data.get("company")):
        raise ValueError("A job needs at least a title and a company.")
    status = data.get("status") or "pending"
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    now = _now()
    job = {
        "id": uuid.uuid4().hex,
        "created_at": now,
        "updated_at": now,
        "status_history": [{"status": status, "at": now}],
    }
    for f in FIELDS:
        job[f] = data.get(f)
    job["interview_tz"] = valid_tz(job.get("interview_tz"))
    job["status"] = status
    job["salary_currency"] = data.get("salary_currency") or "EUR"
    # The language the posting is written in decides which base CV a
    # tailored copy starts from, so it is read off the posting when nobody
    # said. A wrong guess is one click to correct on the application.
    if not job.get("language"):
        try:
            import languages
            job["language"] = languages.detect(
                " ".join(str(data.get(k) or "") for k in ("title", "description")))
        except Exception:
            job["language"] = None

    with _locked(workspace):
        _put(workspace, _new_name(workspace, job), job)
    return _row(job)


def set_stored(workspace: Path, job_id: str, fields: dict) -> dict:
    """Set stored fields exactly as given: no history appended, no time
    stamped. For the sample data, which makes up a past."""
    with _locked(workspace):
        found = _find(workspace, job_id)
        if found is None:
            raise ValueError("No such job.")
        name, rec = found
        rec = dict(rec, **{k: v for k, v in fields.items() if k in STORED and k != "id"})
        _put(workspace, name, rec)
    return _row(rec)


def update_job(workspace: Path, job_id: str, data: dict) -> dict:
    """Change fields on one application, appending to its status history.

    `append_note` is not a field: it adds a dated line to `notes` rather than
    replacing them. That is what the MCP tools use, so a model can record why
    it changed something without being able to erase what the user typed.
    """
    if data.get("interview_tz"):
        valid_tz(data["interview_tz"])
    # The history append below is a read-modify-write, and an AI client is a
    # second writer on this folder. Without the lock two concurrent status
    # changes both read the same history and the second write silently drops
    # the first one's entry.
    with _locked(workspace):
        found = _find(workspace, job_id)
        if found is None:
            raise ValueError("No such job.")
        name, cur = found
        cur = copy.deepcopy(cur)
        new = copy.deepcopy(cur)
        touched: set[str] = set()

        def put(k, v):
            new[k] = v
            touched.add(k)

        for f in FIELDS:
            # Only fields that actually differ. A reconcile against a mailbox
            # re-derives the same interview time from the same event on every
            # run, and writing it back unchanged would bump updated_at, jump
            # the application to the top of a list sorted by it, and tell the
            # open app the jobs changed when nothing did.
            if f in data and not _same(data[f], cur.get(f)):
                put(f, data[f])
        if "prep" in data:
            prep = clean_prep(data["prep"]) or None
            if not _same(prep, cur.get("prep")):
                put("prep", prep)
        if "people" in data:
            people = clean_people(data["people"])
            if not _same(people, cur.get("people")):
                put("people", people)
                # contact_email is what the mail matching reads: keep it the
                # first person's.
                first = next((p["email"] for p in people if p["email"]), None)
                if not _same(first, cur.get("contact_email")) and "contact_email" not in touched:
                    put("contact_email", first)
        # Rounds carry the interview time: writing them moves interview_at to
        # the next one. Writing interview_at alone (the calendar, a mail
        # reconcile) moves that round, or adds one when none is waiting.
        rounds = None
        if "rounds" in data:
            rounds = clean_rounds(data["rounds"])
        elif "interview_at" in data and cur.get("rounds"):
            rounds = copy.deepcopy(cur["rounds"])
            at = str(data["interview_at"] or "")[:16]
            tz = data.get("interview_tz", cur.get("interview_tz")) or ""
            nxt = next((r for r in rounds if not r.get("outcome")), None)
            if nxt is not None:
                nxt.update(at=at, tz=tz)
            elif at:
                rounds.append({"id": uuid.uuid4().hex[:8], "kind": "", "at": at, "tz": tz,
                               "with": "", "outcome": "", "note": ""})
            rounds = clean_rounds(rounds)
        if rounds is not None:
            if not _same(rounds, cur.get("rounds")):
                put("rounds", rounds)
            if "rounds" in data:
                at, tz = interview_of(rounds)
                for col, val in (("interview_at", at), ("interview_tz", tz)):
                    if not _same(val, cur.get(col)) and col not in touched:
                        put(col, val)
        note = (data.get("append_note") or "").strip()
        if note:
            stamped = f"[{time.strftime('%Y-%m-%d')}] {note}"
            existing = (cur.get("notes") or "").rstrip()
            put("notes", f"{existing}\n{stamped}" if existing else stamped)
        # A status change appends to the history rather than overwriting it;
        # the history is the whole point of the funnel.
        new_status = data.get("status")
        if new_status and new_status != (cur.get("status") or "pending"):
            if new_status not in STATUSES:
                raise ValueError(f"Unknown status: {new_status}")
            hist = list(cur.get("status_history") or [])
            hist.append({"status": new_status, "at": _now()})
            put("status_history", hist)
        if not touched:
            return _row(cur)
        new["updated_at"] = _now()
        _put(workspace, name, new)
        return _row(new)


TRASH_DAYS = 30


def _trash_files(workspace: Path) -> dict[Path, dict]:
    out = {}
    d = workspace / TRASH_DIR
    if d.is_dir():
        for f in d.glob("*.y*ml"):
            if f.name.startswith("."):
                continue
            rec = _parse(f, f.stat().st_mtime)
            if rec:
                out[f] = rec
    return out


def delete_job(workspace: Path, job_id: str) -> None:
    """Move an application to the workspace's .trash folder. restore_job
    brings it back as it was; after TRASH_DAYS it is gone for good."""
    with _locked(workspace):
        found = _find(workspace, job_id)
        if found is None:
            return
        name, rec = found
        _write(workspace / TRASH_DIR / name, dict(rec, deleted_at=_now()))
        (tracker_dir(workspace) / name).unlink(missing_ok=True)
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - TRASH_DAYS * 86400))
        for f, t in _trash_files(workspace).items():
            if str(t.get("deleted_at") or "") < cutoff:
                f.unlink(missing_ok=True)


def restore_job(workspace: Path, job_id: str) -> dict:
    """Put a deleted application back, with the id and history it had."""
    with _locked(workspace):
        hit = next(((f, t) for f, t in _trash_files(workspace).items() if t["id"] == job_id), None)
        if hit is None:
            raise ValueError("That application is no longer in the trash.")
        f, rec = hit
        rec.pop("deleted_at", None)
        found = _find(workspace, job_id)
        _put(workspace, found[0] if found else f.name, rec)
        f.unlink(missing_ok=True)
    return _row(rec)


def list_trash(workspace: Path) -> list[dict]:
    """What is in the trash, newest first: id, company, title and when. One
    that is back in the list (a restored backup brought its file back) is not."""
    live = {r["id"] for r in _records(workspace)}
    out = [{"id": t["id"], "company": t.get("company"), "title": t.get("title"),
            "deleted_at": t.get("deleted_at")}
           for t in _trash_files(workspace).values() if t["id"] not in live]
    out.sort(key=lambda t: str(t["deleted_at"] or ""), reverse=True)
    return out


def _reply_days(history: list[dict]) -> int | None:
    """Days between applying and the first thing that happened next.

    Only a real answer counts: a job still sitting at `applied` has not had a
    reply yet, and folding those in as zero would flatter the median. Nor does
    being ghosted: marking one ghosted is you giving up on an answer, and
    counting the day you did as the day they replied pulled the median towards
    however long you usually wait before deciding that.
    """
    applied_at = None
    for event in history:
        at = event.get("at")
        if not at:
            continue
        if event.get("status") == "applied":
            applied_at = at
            continue
        if applied_at:
            if str(event.get("status", "")).startswith("ghosted"):
                return None
            try:
                a = time.strptime(applied_at[:19], "%Y-%m-%dT%H:%M:%S")
                b = time.strptime(at[:19], "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
            return max(0, round((time.mktime(b) - time.mktime(a)) / 86400))
    return None


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    values = sorted(values)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return round((values[mid - 1] + values[mid]) / 2)


def _days_since(stamp: str | None) -> int | None:
    """Whole days between an ISO stamp and now, or None if it will not parse.

    Tolerant about length so it takes both a date and a full timestamp, since
    followup_date is a date and interview_at is not.
    """
    if not stamp:
        return None
    text = str(stamp).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M",
                "%Y-%m-%d"):
        try:
            when = time.strptime(text[:len(time.strftime(fmt))], fmt)
        except ValueError:
            continue
        return round((time.time() - time.mktime(when)) / 86400)
    return None


def valid_tz(name: str | None) -> str | None:
    """An IANA zone name as given, or ValueError. Empty means the user's own."""
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
    except Exception:
        raise ValueError(f"Unknown time zone: {name!r}. Use an IANA name such as "
                         "Europe/London or America/Sao_Paulo.")
    return name


def interview_local(at: str | None, tz: str | None) -> str | None:
    """An interview's moment in this machine's local time.

    `interview_at` is the wall-clock time the invitation gave, in
    `interview_tz` when there is one: 10:00 in London stays 10:00 in the file,
    because that is what the email said and what a person re-reading it will
    check against. Anything comparing it with now needs it here instead.
    """
    if not at or not tz:
        return at
    try:
        import datetime as dt
        from zoneinfo import ZoneInfo
        wall = dt.datetime.fromisoformat(str(at)[:19])
        return wall.replace(tzinfo=ZoneInfo(tz)).astimezone().replace(
            tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return at


def _applied_at(history: list[dict]) -> str | None:
    for event in history:
        if event.get("status") == "applied":
            return event.get("at")
    return None


def alerts(workspace: Path) -> dict:
    """The three things about an application that are worth interrupting for.

    One function because the same answer is wanted in three places: the panel
    in the Jobs view, the digest an AI client reads out, and the desktop
    notification. Three copies of these rules would drift apart within a month.

    Nothing here reaches outside the workspace. The dates come from the mailbox
    originally, but by the time they are here they are ours.
    """
    today = time.strftime("%Y-%m-%d")
    out: dict[str, list] = {"followup_due": [], "interview_soon": [],
                            "interview_passed": [], "silent": []}

    for job in list_jobs(workspace):
        if job["status"] in TERMINAL:
            continue
        brief = {k: job[k] for k in ("id", "title", "company", "status")}

        if job.get("followup_date") and job["followup_date"] <= today:
            out["followup_due"].append(brief | {"followup_date": job["followup_date"]})

        age = _days_since(interview_local(job.get("interview_at"), job.get("interview_tz")))
        if age is not None:
            entry = brief | {"interview_at": job["interview_at"],
                             "interview_tz": job.get("interview_tz")}
            if -INTERVIEW_SOON_DAYS <= age <= 0:
                out["interview_soon"].append(entry)
            elif age > 0 and job["status"] == "interviewing":
                # The interview has been and gone and nothing was recorded.
                # Either it needs an outcome, or the date is stale because it
                # moved in a calendar nobody has re-read since.
                out["interview_passed"].append(entry)

        if job["status"] == "applied":
            # The later of the two, because an acknowledgement that changed no
            # status still means they are not silent.
            last = max(filter(None, [job.get("last_contact_at"),
                                     _applied_at(job.get("status_history") or [])]),
                       default=None)
            quiet = _days_since(last)
            if quiet is not None and quiet > SILENT_DAYS:
                out["silent"].append(brief | {"silent_days": quiet})

    out["counts"] = {k: len(v) for k, v in out.items()}
    out["total"] = sum(out["counts"].values())
    return out


def funnel(workspace: Path, since: str | None = None) -> dict:
    """Node counts and flow volumes for the application funnel.

    `since` is an ISO date; jobs created before it are left out, which is what
    the range control on the funnel screen selects.
    """
    rows = [_row(r) for r in _records(workspace)]
    if since:
        rows = [r for r in rows if str(r.get("created_at") or "") >= since]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    replies = [d for d in (_reply_days(r["status_history"]) for r in rows) if d is not None]

    total = sum(counts.values())
    nodes = [{"id": nid, "label": LABELS[nid],
              "count": sum(counts.get(s, 0) for s in statuses)}
             for nid, statuses in NODE_STATUSES.items()]
    links = []
    for src, dst, statuses in FLOWS:
        value = sum(counts.get(s, 0) for s in statuses)
        if value:
            links.append({"source": src, "target": dst, "value": value})

    applied = sum(counts.get(s, 0) for s in NODE_STATUSES["applied_s"])
    interviewed = sum(counts.get(s, 0) for s in NODE_STATUSES["interview_s"])
    offers = sum(counts.get(s, 0) for s in NODE_STATUSES["offer_s"])
    rate = lambda a, b: round(a / b * 100) if b else 0  # noqa: E731
    return {
        "nodes": nodes,
        "links": links,
        "totals": {
            "total": total, "applied": applied,
            "interviewed": interviewed, "offers": offers,
            "interview_rate": rate(interviewed, applied),
            "offer_rate": rate(offers, interviewed),
            "accept_rate": rate(counts.get("accepted", 0), offers),
            "median_reply_days": _median(replies),
            "replied": len(replies),
        },
        "by_status": counts,
        "since": since,
    }


def ics(workspace: Path, job_id: str | None = None) -> str:
    """Interviews and follow-ups as an iCalendar file, for any calendar app.

    Interviews carry their moment in UTC, worked out from the zone the
    invitation gave, so a calendar anywhere puts them at the right hour.
    Follow-ups are all-day. Nothing is sent anywhere: this is a file the
    person imports, or opens for a single interview.
    """
    import datetime as dt

    def esc(v: str) -> str:
        return (str(v or "").replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CV Studio//Calendar//EN",
           "CALSCALE:GREGORIAN", "X-WR-CALNAME:CV Studio"]
    for job in list_jobs(workspace):
        if job_id and job["id"] != job_id:
            continue
        what = f"{job['company']} · {job['title']}"
        where = job.get("location") or ""
        if job.get("interview_at"):
            try:
                wall = dt.datetime.fromisoformat(str(job["interview_at"])[:19])
                if job.get("interview_tz"):
                    from zoneinfo import ZoneInfo
                    at = wall.replace(tzinfo=ZoneInfo(job["interview_tz"]))
                else:
                    at = wall.astimezone()
                start = at.astimezone(dt.timezone.utc)
                out += ["BEGIN:VEVENT", f"UID:{job['id']}-interview@cv-studio",
                        f"DTSTAMP:{stamp}", f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
                        f"DTEND:{(start + dt.timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}",
                        f"SUMMARY:{esc('Interview · ' + job['company'])}",
                        f"DESCRIPTION:{esc(what + (chr(10) + job['notes'] if job.get('notes') else ''))}",
                        f"LOCATION:{esc(where)}", "END:VEVENT"]
            except (ValueError, KeyError, Exception):
                pass
        if job.get("followup_date") and job["status"] not in TERMINAL:
            try:
                day = dt.date.fromisoformat(str(job["followup_date"])[:10])
                out += ["BEGIN:VEVENT", f"UID:{job['id']}-followup@cv-studio",
                        f"DTSTAMP:{stamp}", f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
                        f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
                        f"SUMMARY:{esc('Follow up · ' + job['company'])}",
                        f"DESCRIPTION:{esc(what)}", "END:VEVENT"]
            except ValueError:
                pass
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def export(workspace: Path, fmt: str = "json") -> str:
    """Everything in one file, for a spreadsheet or another tracker."""
    rows = list_jobs(workspace)
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["id", *FIELDS, "people", "rounds", "created_at", "updated_at"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            # One cell a spreadsheet can show: "Name (Role) <email>; ...".
            r = dict(r, people="; ".join(
                " ".join(x for x in (p.get("name"), f"({p['role']})" if p.get("role") else "",
                                     f"<{p['email']}>" if p.get("email") else "") if x)
                for p in r.get("people") or []),
                rounds="; ".join(
                " ".join(x for x in (rd.get("kind") or "Interview", rd.get("at", "").replace("T", " "),
                                     f"({rd['outcome']})" if rd.get("outcome") else "") if x)
                for rd in r.get("rounds") or []))
            w.writerow(r)
        return buf.getvalue()
    return json.dumps(rows, indent=2)
