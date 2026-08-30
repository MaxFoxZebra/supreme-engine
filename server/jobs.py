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
    "rejected":    ["rejected", "rejected_interviewing"],
    "ghosted":     ["ghosted", "ghosted_interviewing"],
}

LABELS = {
    "all": "All jobs", "pending": "Draft", "applied_s": "Applied",
    "awaiting": "Awaiting", "interview_s": "Interviewing",
    "still_iv": "Still interviewing", "offer_s": "Offer", "deciding": "Deciding",
    "accepted": "Accepted", "refused": "Declined", "rejected": "Rejected",
    "ghosted": "Ghosted",
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
    ("interview_s", "rejected", ["rejected_interviewing"]),
    ("interview_s", "ghosted", ["ghosted_interviewing"]),
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
  salary_expected  INTEGER,
  salary_offered   INTEGER,
  salary_currency  TEXT NOT NULL DEFAULT 'EUR',
  status_history   TEXT NOT NULL DEFAULT '[]',
  cv_path          TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_company ON jobs(company);
"""

FIELDS = [
    "title", "company", "location", "country", "description", "url", "source",
    "score", "status", "notes", "followup_date", "salary_expected",
    "salary_offered", "salary_currency", "cv_path",
]


def db_path(workspace: Path) -> Path:
    return workspace / "applications.db"


def connect(workspace: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path(workspace))
    con.row_factory = sqlite3.Row
    # WAL keeps reads from blocking on writes, which matters because the UI
    # polls while a status is being written.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    return con


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["status_history"] = json.loads(d.get("status_history") or "[]")
    except json.JSONDecodeError:
        d["status_history"] = []
    return d


def list_jobs(workspace: Path, status: str | None = None, q: str | None = None) -> list[dict]:
    con = connect(workspace)
    sql = "SELECT * FROM jobs"
    args: list = []
    where = []
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
    job["status"] = status
    job["salary_currency"] = data.get("salary_currency") or "EUR"

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
    con = connect(workspace)
    try:
        cur = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if cur is None:
            raise ValueError("No such job.")
        sets, args = [], []
        for f in FIELDS:
            if f in data:
                sets.append(f"{f}=?")
                args.append(data[f])
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
            return _row(cur)
        sets.append("updated_at=?")
        args.append(_now())
        args.append(job_id)
        con.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", args)
        con.commit()
        return _row(con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        con.close()


def delete_job(workspace: Path, job_id: str) -> None:
    con = connect(workspace)
    try:
        con.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        con.commit()
    finally:
        con.close()


def funnel(workspace: Path) -> dict:
    """Node counts and flow volumes for the application funnel."""
    con = connect(workspace)
    try:
        counts = {r["status"]: r["n"] for r in
                  con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    finally:
        con.close()

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
        },
        "by_status": counts,
    }


def export(workspace: Path, fmt: str = "json") -> str:
    """Everything back out as text, so the database is never a lock-in."""
    rows = list_jobs(workspace)
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["id", *FIELDS, "created_at", "updated_at"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return buf.getvalue()
    return json.dumps(rows, indent=2)
