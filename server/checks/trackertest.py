"""Applications as files: readable, editable by hand, safe in a synced folder.

    python checks/trackertest.py

One YAML file per application in tracker/. This checks what that promises:
the file reads like the application, an edit made outside the app shows up,
a sync client's conflicted copy does not show twice, two processes writing at
once lose nothing, the index outside the workspace is only ever a copy, and a
workspace that still has applications.db (this computer's, or a second one's
that has not synced yet) moves out of it without bringing back the deleted or
putting an old status over a new one.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import backups  # noqa: E402
import jobs  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def fresh() -> Path:
    ws = Path(tempfile.mkdtemp()) / "CV Studio"
    ws.mkdir()
    return ws


def tick() -> None:
    # Timestamps are to the second; a test that orders two writes waits one.
    time.sleep(1.05)


# The schema applications.db had, to make one the way the app used to.
OLD_SCHEMA = """
CREATE TABLE jobs (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT,
  country TEXT, description TEXT, url TEXT, source TEXT, score INTEGER,
  status TEXT NOT NULL DEFAULT 'pending', notes TEXT, followup_date TEXT,
  interview_at TEXT, interview_tz TEXT, contact_email TEXT, last_contact_at TEXT,
  salary_expected INTEGER, salary_offered INTEGER, salary_currency TEXT NOT NULL DEFAULT 'EUR',
  status_history TEXT NOT NULL DEFAULT '[]', cv_path TEXT, letter_path TEXT, logo TEXT,
  language TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  people TEXT, rounds TEXT, prep TEXT);
CREATE TABLE trash (id TEXT PRIMARY KEY, data TEXT NOT NULL, deleted_at TEXT NOT NULL);
"""


def old_db(ws: Path, rows: list[dict], trash: list[dict] = ()) -> None:
    con = sqlite3.connect(ws / "applications.db")
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(OLD_SCHEMA)
    for r in rows:
        con.execute(f"INSERT INTO jobs ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                    list(r.values()))
    for t in trash:
        con.execute("INSERT INTO trash VALUES (?,?,?)", (t["id"], json.dumps(t), t["deleted_at"]))
    con.commit()
    con.close()


def old_row(id_: str, company: str, status: str, updated: str, **kw) -> dict:
    return {"id": id_, "title": "Engineer", "company": company, "status": status,
            "status_history": json.dumps([{"status": "applied", "at": "2026-08-01T09:00:00"}]
                                         + ([{"status": status, "at": updated}] if status != "applied" else [])),
            "created_at": "2026-08-01T09:00:00", "updated_at": updated, **kw}


def file_format() -> None:
    print("The file")
    ws = fresh()
    notes = "  Indented first line\nRecruiter: Sarah # not a comment\n\nAsked about Go.\n"
    j = jobs.add_job(ws, {"company": "Société Générale", "title": "Backend Engineer",
                          "status": "applied", "notes": notes, "score": 4,
                          "followup_date": "2026-09-12", "url": "https://example.com/jobs/1"})
    files = list((ws / "tracker").glob("*.yaml"))
    check("an application is one file in tracker/", len(files) == 1, str(files))
    f = files[0]
    check("named after the company and the role", f.name.startswith("societe-generale-backend-engineer-"), f.name)
    text = f.read_text(encoding="utf-8")
    check("the company comes first", text.startswith("company: Société Générale\n"), text[:40])
    check("notes are a block, not one quoted line", "notes: |" in text)
    check("empty fields are left out", "salary_offered" not in text and "letter_path" not in text)
    back = jobs.list_jobs(ws)[0]
    check("it reads back as it was written",
          back["notes"] == notes and back["score"] == 4 and back["followup_date"] == "2026-09-12"
          and back["company"] == "Société Générale" and back["id"] == j["id"], repr(back["notes"]))
    check("every field is there, empty ones as None",
          all(k in back for k in jobs.FIELDS) and back["salary_offered"] is None)

    before = jobs.list_jobs(ws)[0]["updated_at"]
    tick()
    jobs.update_job(ws, j["id"], {"letter_path": "", "salary_offered": None, "score": 4})
    check("writing back nothing changes nothing", jobs.list_jobs(ws)[0]["updated_at"] == before)

    stamp = jobs.stamp(ws)
    tick()
    f.write_text(text.replace("status: applied", "status: interviewing")
                 .replace("followup_date: '2026-09-12'", "followup_date: 2026-09-20"), encoding="utf-8")
    after = jobs.list_jobs(ws)[0]
    check("an edit made in another editor shows up", after["status"] == "interviewing", after["status"])
    check("an unquoted date reads as the same text", after["followup_date"] == "2026-09-20",
          repr(after["followup_date"]))
    check("and the open app is told", jobs.stamp(ws) != stamp)

    (ws / "tracker" / "by-hand.yaml").write_text(
        "company: Acme\ntitle: SRE\nstatus: applied\n", encoding="utf-8")
    (ws / "tracker" / "broken.yaml").write_text("company: [unclosed\n", encoding="utf-8")
    rows = jobs.list_jobs(ws)
    hand = next((r for r in rows if r["company"] == "Acme"), None)
    check("a file written by hand is an application", hand is not None and hand["created_at"])
    check("a broken file is skipped, not fatal", len(rows) == 2, str(len(rows)))
    jobs.update_job(ws, hand["id"], {"status": "interviewing"})
    check("saving one written by hand keeps its file and gives it an id",
          f"id: {hand['id']}" in (ws / "tracker" / "by-hand.yaml").read_text(encoding="utf-8"))

    seen = sorted((r["id"], r["status"], r["updated_at"]) for r in jobs.list_jobs(ws))
    shutil.rmtree(jobs._index_dir())
    jobs._mem.clear()
    check("the index is only a copy: delete it and nothing is lost",
          sorted((r["id"], r["status"], r["updated_at"]) for r in jobs.list_jobs(ws)) == seen)


def sync_conflict() -> None:
    print("A sync client's conflicted copy")
    ws = fresh()
    j = jobs.add_job(ws, {"company": "Acme", "title": "SRE", "status": "applied"})
    f = next((ws / "tracker").glob("*.yaml"))
    copy = f.with_name(f.stem + " (Max's conflicted copy 2026-10-04).yaml")
    shutil.copy(f, copy)
    tick()
    jobs.update_job(ws, j["id"], {"status": "interviewing"})
    rows = jobs.list_jobs(ws)
    check("shows once", len(rows) == 1, str(len(rows)))
    check("as the newer of the two", rows[0]["status"] == "interviewing", rows[0]["status"])
    check("and is reported", jobs.conflicts(ws).get(j["id"]) == [copy.name], str(jobs.conflicts(ws)))


WORKER = """
import sys
sys.path.insert(0, {here!r})
import jobs
from pathlib import Path
for i in range({n}):
    jobs.update_job(Path({ws!r}), {id!r}, {{"append_note": "{tag} " + str(i), "status": "{status}" if i % 2 else "applied"}})
"""


def two_writers() -> None:
    print("Two processes writing at once")
    ws = fresh()
    j = jobs.add_job(ws, {"company": "Acme", "title": "SRE", "status": "applied"})
    n = 15
    procs = [subprocess.Popen([sys.executable, "-c", WORKER.format(
        here=str(HERE), n=n, ws=str(ws), id=j["id"], tag=tag, status=st)])
        for tag, st in (("app", "interviewing"), ("mcp", "offer"))]
    for p in procs:
        p.wait(timeout=120)
    row = jobs.list_jobs(ws)[0]
    lines = (row["notes"] or "").splitlines()
    check("every note from both is kept", len(lines) == 2 * n, f"{len(lines)} of {2 * n}")
    left = [p.name for p in (ws / "tracker").iterdir() if p.name.startswith(".")]
    check("no temporary files left behind", not left, str(left))


def migration() -> None:
    print("Moving out of applications.db")
    ws = fresh()
    old_db(ws, [old_row("a" * 32, "Acme", "interviewing", "2026-09-10T10:00:00",
                        people=json.dumps([{"id": "p1", "name": "Sarah", "role": "", "email": "s@acme.example",
                                            "link": "", "last": ""}]),
                        notes="Line one\nLine two"),
                old_row("b" * 32, "Globex", "applied", "2026-08-01T09:00:00",
                        contact_email="hr@globex.example")],
           trash=[{"id": "c" * 32, "title": "PM", "company": "Initech", "status": "applied",
                   "status_history": "[]", "created_at": "2026-07-01T09:00:00",
                   "updated_at": "2026-07-01T09:00:00", "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}])
    rows = {r["id"]: r for r in jobs.list_jobs(ws)}
    check("every application comes across", set(rows) == {"a" * 32, "b" * 32}, str(list(rows)))
    a = rows.get("a" * 32, {})
    check("with its history, people and notes",
          len(a.get("status_history") or []) == 2 and a["people"][0]["name"] == "Sarah"
          and a["notes"] == "Line one\nLine two")
    check("an old single contact still reads as a person",
          rows.get("b" * 32, {}).get("people", [{}])[0].get("email") == "hr@globex.example")
    check("the trash comes across", [t["id"] for t in jobs.list_trash(ws)] == ["c" * 32])
    check("the database goes to .trash",
          not (ws / "applications.db").exists() and any((ws / ".trash").glob("*-applications.db")))
    check("and its journal files with it",
          not (ws / "applications.db-wal").exists() and not (ws / "applications.db-shm").exists())

    print("A second computer that still has the database")
    ws2 = fresh()
    shutil.copytree(ws / "tracker", ws2 / "tracker")
    # This computer moved Acme on to an offer and deleted Globex...
    jobs.update_job(ws2, "a" * 32, {"status": "offer"})
    jobs.delete_job(ws2, "b" * 32)
    # ...then the other one, not yet synced, opens with its old database.
    old_db(ws2, [old_row("a" * 32, "Acme", "interviewing", "2026-09-10T10:00:00"),
                 old_row("b" * 32, "Globex", "applied", "2026-08-01T09:00:00"),
                 old_row("d" * 32, "Hooli", "applied", "2026-09-01T09:00:00")])
    rows = {r["id"]: r for r in jobs.list_jobs(ws2)}
    check("a newer status is not put back to the old one", rows.get("a" * 32, {}).get("status") == "offer",
          str(rows.get("a" * 32, {}).get("status")))
    check("a deleted application does not come back", "b" * 32 not in rows)
    check("one only the database had comes across", "d" * 32 in rows)

    print("Restoring a backup from before")
    ws3 = fresh()
    data = Path(os.environ["XDG_DATA_HOME"])
    old_db(ws3, [old_row("e" * 32, "Umbrella", "applied", "2026-09-01T09:00:00")])
    con = sqlite3.connect(ws3 / "applications.db")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    folder = backups.folder(data, ws3)
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / "20260101-000000000-daily.zip", "w") as z:
        z.write(ws3 / "applications.db", "applications.db")
    (ws3 / "applications.db").unlink()
    j = jobs.add_job(ws3, {"company": "Wayne", "title": "CTO"})
    jobs.list_jobs(ws3)
    # Umbrella was moved on since the backup, in a file of its own.
    jobs._put(ws3, "umbrella-engineer-eeeeeeee.yaml",
              jobs._from_sqlite(old_row("e" * 32, "Umbrella", "rejected", "2026-09-20T09:00:00")))
    backups.restore(data, ws3, "20260101-000000000-daily.zip")
    rows = {r["id"]: r for r in jobs.list_jobs(ws3)}
    check("the backup's applications are what it had", rows.get("e" * 32, {}).get("status") == "applied",
          str(rows.get("e" * 32, {}).get("status")))
    check("and one made since is left alone", j["id"] in rows)


def mcp_guard() -> None:
    print("An AI client")
    try:
        import mcp_server  # noqa: F401
    except Exception as exc:
        check("mcp_server imports", False, repr(exc))
        return
    import studio
    ws = fresh()
    studio.WORKSPACE = ws
    jobs.add_job(ws, {"company": "Acme", "title": "SRE"})
    f = next((ws / "tracker").glob("*.yaml"))
    try:
        mcp_server._doc_path(f"tracker/{f.name}")
        check("cannot write an application's file with the CV tools", False)
    except PermissionError:
        check("cannot write an application's file with the CV tools", True)
    try:
        mcp_server._doc_path("profile/my-cv.yaml")
        check("and can still write a CV", True)
    except PermissionError as exc:
        check("and can still write a CV", False, str(exc))


def main() -> int:
    file_format()
    sync_conflict()
    two_writers()
    migration()
    mcp_guard()
    print()
    print(f"{fails} failure(s)" if fails else "every tracker check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
