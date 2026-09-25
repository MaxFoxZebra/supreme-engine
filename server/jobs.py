"""Job application records, stored in SQLite.

Why a database here when CVs are plain files: CVs are *documents*. You read
them, diff them, and they should outlive this program, so they stay as YAML.
Job applications are *records* -- dozens to hundreds of them, filtered, sorted,
aggregated, and appended to every time a status changes. One YAML file per job
would be parsed in full on every list and rewritten on every status change.

sqlite3 is in the Python standard library, so this costs no dependency, and the
whole store is one file in the workspace that can be copied or deleted. Export
to JSON and CSV is provided so nothing is locked in.

The schema follows the one from the job tracker this was ported from, including
`status_history`, which is what makes the funnel chart meaningful over time.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import time
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id               TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  company          TEXT NOT NULL,
  location         TEXT,
  country          TEXT,
  description      TEXT,
  url              TEXT,
  source           TEXT,
  score            INTEGER,
  status           TEXT NOT NULL DEFAULT 'pending',
  notes            TEXT,
  followup_date    TEXT,
  interview_at     TEXT,
  interview_tz     TEXT,
  contact_email    TEXT,
  last_contact_at  TEXT,
  salary_expected  INTEGER,
  salary_offered   INTEGER,
  salary_currency  TEXT NOT NULL DEFAULT 'EUR',
  status_history   TEXT NOT NULL DEFAULT '[]',
  cv_path          TEXT,
  letter_path      TEXT,
  logo             TEXT,
  language         TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
-- A deleted application, whole, for thirty days: Undo puts it back with its
-- id and its history rather than as a new row.
CREATE TABLE IF NOT EXISTS trash (
  id          TEXT PRIMARY KEY,
  data        TEXT NOT NULL,
  deleted_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
"""

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



def set_company_logo(workspace: Path, company: str, logo: str) -> int:
    """Point every application to one company at the same logo.

    Applications are per-role but a logo belongs to the company, so this is
    matched on the name rather than set on one row.
    """
    con = connect(workspace)
    try:
        cur = con.execute(
            "UPDATE jobs SET logo = ?, updated_at = ? WHERE lower(company) = ?",
            (logo, _now(), company.strip().lower()))
        con.commit()
        return cur.rowcount
    finally:
        con.close()

def db_path(workspace: Path) -> Path:
    return workspace / "applications.db"


def connect(workspace: Path) -> sqlite3.Connection:
    # An AI client writing statuses is a second process on this file, so a
    # contended write is now routine rather than theoretical. The default
    # five seconds is generous for the writes here, all of which are a single
    # small row, but it is left explicit so it reads as a decision.
    con = sqlite3.connect(db_path(workspace), timeout=15.0)
    con.row_factory = sqlite3.Row
    # WAL keeps reads from blocking on writes, which matters because the UI
    # polls while a status is being written.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=15000")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(jobs)")}
    for col, decl in (("cv_path", "TEXT"), ("letter_path", "TEXT"),
                      ("logo", "TEXT"), ("interview_at", "TEXT"),
                      ("contact_email", "TEXT"), ("last_contact_at", "TEXT"),
                      ("language", "TEXT"), ("interview_tz", "TEXT"),
                      ("people", "TEXT"), ("rounds", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    con.commit()
    return con


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["status_history"] = json.loads(d.get("status_history") or "[]")
    except json.JSONDecodeError:
        d["status_history"] = []
    try:
        d["people"] = json.loads(d.get("people") or "[]")
    except json.JSONDecodeError:
        d["people"] = []
    # Before people, an application had one contact email. It is the first
    # person until someone is added.
    if not d["people"] and d.get("contact_email"):
        d["people"] = [{"id": "contact", "name": "", "role": "", "email": d["contact_email"],
                        "link": "", "last": ""}]
    try:
        d["rounds"] = json.loads(d.get("rounds") or "[]")
    except json.JSONDecodeError:
        d["rounds"] = []
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
    con = connect(workspace)
    sql = "SELECT * FROM jobs"
    args: list = []
    where = []
    if node and node in NODE_STATUSES:
        wanted = NODE_STATUSES[node]
        where.append(f"status IN ({', '.join('?' * len(wanted))})")
        args += wanted
    if status:
        where.append("status = ?")
        args.append(status)
    if q:
        where.append("(title LIKE ? OR company LIKE ? OR notes LIKE ?)")
        args += [f"%{q}%"] * 3
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY datetime(updated_at) DESC"
    try:
        return [_row(r) for r in con.execute(sql, args)]
    finally:
        con.close()


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
        "status_history": json.dumps([{"status": status, "at": now}]),
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

    cols = ", ".join(job)
    con = connect(workspace)
    try:
        con.execute(f"INSERT INTO jobs ({cols}) VALUES ({', '.join('?' * len(job))})",
                    list(job.values()))
        con.commit()
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
    finally:
        con.close()


def update_job(workspace: Path, job_id: str, data: dict) -> dict:
    """Change fields on one application, appending to its status history.

    `append_note` is not a column: it adds a dated line to `notes` rather than
    replacing them. That is what the MCP tools use, so a model can record why
    it changed something without being able to erase what the user typed.
    """
    if data.get("interview_tz"):
        valid_tz(data["interview_tz"])
    con = connect(workspace)
    try:
        # The history append below is a read-modify-write, and an AI client is
        # now a second writer on this file. Without an immediate transaction
        # two concurrent status changes both read the same history and the
        # second write silently drops the first one's entry.
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if cur is None:
            raise ValueError("No such job.")
        sets, args = [], []
        for f in FIELDS:
            # Only fields that actually differ. A reconcile against a mailbox
            # re-derives the same interview time from the same event on every
            # run, and writing it back unchanged would bump updated_at, jump
            # the row to the top of a table sorted by it, and tell the open app
            # the jobs changed when nothing did.
            if f in data and data[f] != cur[f]:
                sets.append(f"{f}=?")
                args.append(data[f])
        if "people" in data:
            people = clean_people(data["people"])
            stored = json.dumps(people, ensure_ascii=False)
            if stored != (cur["people"] or "[]"):
                sets.append("people=?")
                args.append(stored)
                # contact_email is what the mail matching reads: keep it the
                # first person's.
                first = next((p["email"] for p in people if p["email"]), None)
                if first != cur["contact_email"] and "contact_email=?" not in sets:
                    sets.append("contact_email=?")
                    args.append(first)
        # Rounds carry the interview time: writing them moves interview_at to
        # the next one. Writing interview_at alone (the calendar, a mail
        # reconcile) moves that round, or adds one when none is waiting.
        rounds = None
        if "rounds" in data:
            rounds = clean_rounds(data["rounds"])
        elif "interview_at" in data and cur["rounds"]:
            rounds = json.loads(cur["rounds"] or "[]")
            at = str(data["interview_at"] or "")[:16]
            tz = data.get("interview_tz", cur["interview_tz"]) or ""
            nxt = next((r for r in rounds if not r.get("outcome")), None)
            if nxt is not None:
                nxt.update(at=at, tz=tz)
            elif at:
                rounds.append({"id": uuid.uuid4().hex[:8], "kind": "", "at": at, "tz": tz,
                               "with": "", "outcome": "", "note": ""})
            rounds = clean_rounds(rounds)
        if rounds is not None:
            stored = json.dumps(rounds, ensure_ascii=False)
            if stored != (cur["rounds"] or "[]"):
                sets.append("rounds=?")
                args.append(stored)
            if "rounds" in data:
                at, tz = interview_of(rounds)
                for col, val in (("interview_at", at), ("interview_tz", tz)):
                    if val != cur[col] and f"{col}=?" not in sets:
                        sets.append(f"{col}=?")
                        args.append(val)
        note = (data.get("append_note") or "").strip()
        if note:
            stamped = f"[{time.strftime('%Y-%m-%d')}] {note}"
            existing = (cur["notes"] or "").rstrip()
            sets.append("notes=?")
            args.append(f"{existing}\n{stamped}" if existing else stamped)
        # A status change appends to the history rather than overwriting it;
        # the history is the whole point of the funnel.
        new_status = data.get("status")
        if new_status and new_status != cur["status"]:
            if new_status not in STATUSES:
                raise ValueError(f"Unknown status: {new_status}")
            hist = json.loads(cur["status_history"] or "[]")
            hist.append({"status": new_status, "at": _now()})
            sets.append("status_history=?")
            args.append(json.dumps(hist))
        if not sets:
            con.rollback()
            return _row(cur)
        sets.append("updated_at=?")
        args.append(_now())
        args.append(job_id)
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
        con.commit()
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        con.close()


TRASH_DAYS = 30


def delete_job(workspace: Path, job_id: str) -> None:
    """Move an application to the trash. restore_job brings it back as it
    was; after TRASH_DAYS it is gone for good."""
    con = connect(workspace)
    try:
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        con.execute("INSERT OR REPLACE INTO trash (id, data, deleted_at) VALUES (?,?,?)",
                    (job_id, json.dumps(dict(row), ensure_ascii=False), _now()))
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S",
                               time.localtime(time.time() - TRASH_DAYS * 86400))
        con.execute("DELETE FROM trash WHERE deleted_at < ?", (cutoff,))
        con.commit()
    finally:
        con.close()


def restore_job(workspace: Path, job_id: str) -> dict:
    """Put a deleted application back, with the id and history it had."""
    con = connect(workspace)
    try:
        row = con.execute("SELECT data FROM trash WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise ValueError("That application is no longer in the trash.")
        data = json.loads(row["data"])
        cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]
        keep = [c for c in cols if c in data]
        con.execute(f"INSERT OR REPLACE INTO jobs ({', '.join(keep)}) VALUES "
                    f"({', '.join('?' for _ in keep)})", [data[c] for c in keep])
        con.execute("DELETE FROM trash WHERE id=?", (job_id,))
        con.commit()
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        con.close()


def list_trash(workspace: Path) -> list[dict]:
    """What is in the trash, newest first: id, company, title and when."""
    con = connect(workspace)
    try:
        out = []
        for r in con.execute("SELECT id, data, deleted_at FROM trash ORDER BY deleted_at DESC"):
            d = json.loads(r["data"])
            out.append({"id": r["id"], "company": d.get("company"), "title": d.get("title"),
                        "deleted_at": r["deleted_at"]})
        return out
    finally:
        con.close()


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
    con = connect(workspace)
    where, args = "", []
    if since:
        where, args = " WHERE created_at >= ?", [since]
    try:
        counts = {r["status"]: r["n"] for r in con.execute(
            "SELECT status, COUNT(*) n FROM jobs" + where + " GROUP BY status", args)}
        histories = [r["status_history"] for r in con.execute(
            "SELECT status_history FROM jobs" + where, args)]
    finally:
        con.close()

    replies = []
    for raw in histories:
        try:
            days = _reply_days(json.loads(raw or "[]"))
        except json.JSONDecodeError:
            days = None
        if days is not None:
            replies.append(days)

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
    """Everything back out as text, so the database is never a lock-in."""
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
