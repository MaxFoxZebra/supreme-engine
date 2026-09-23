"""Backups: a consistent zip of what cannot be made again, and a way back.

    python checks/backuptest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import backups  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    ws = Path(tempfile.mkdtemp()) / "My CVs"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)
    studio.jobstore.add_job(ws, {"company": "Acme", "title": "Engineer"})
    (ws / "assets" / "render-cache").mkdir(parents=True, exist_ok=True)
    (ws / "assets" / "render-cache" / "page.png").write_bytes(b"x" * 100)
    data = Path(os.environ["XDG_DATA_HOME"])
    check("a new workspace is due", backups.due(data, ws))
    b = backups.make(data, ws)
    names = zipfile.ZipFile(backups.folder(data, ws) / b["name"]).namelist()
    check("the database is in it", "applications.db" in names, ",".join(names))
    check("the CVs are in it", any(n.startswith("profile/") for n in names))
    check("rendered pages are not", not any(n.startswith("assets/render-cache") for n in names))
    check("and it is not due again today", not backups.due(data, ws))

    cv = next((ws / "profile").glob("*.yaml"))
    original = cv.read_text(encoding="utf-8")
    cv.write_text("broken: [", encoding="utf-8")
    studio.jobstore.delete_job(ws, studio.jobstore.list_jobs(ws)[0]["id"])
    r = backups.restore(data, ws, b["name"])
    check("restoring brings the CV back", cv.read_text(encoding="utf-8") == original)
    check("and the applications", len(studio.jobstore.list_jobs(ws)) == 1)
    check("what was there is backed up first", "before-restore" in r["before"])

    for _ in range(backups.KEEP + 3):
        backups.make(data, ws, "manual")
    check(f"only the last {backups.KEEP} are kept", len(backups.listing(data, ws)) == backups.KEEP,
          str(len(backups.listing(data, ws))))
    try:
        backups.restore(data, ws, "../../etc/passwd")
        check("a name outside the backups is refused", False)
    except FileNotFoundError:
        check("a name outside the backups is refused", True)
    print()
    print(f"{fails} failure(s)" if fails else "every backup check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
