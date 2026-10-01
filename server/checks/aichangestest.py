"""What an AI client changes on an application is kept for review: shown
field by field, kept or undone, and never undone over a later change.

    python checks/aichangestest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import studio  # noqa: E402
import mcp_server  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    studio.WORKSPACE = Path(tempfile.mkdtemp())
    studio.bootstrap(studio.WORKSPACE)
    ws, J = studio.WORKSPACE, studio.jobstore
    studio.CLIENT_ID, studio.CLIENT_AGENT = "claude", "Claude Desktop"

    job = mcp_server.add_job(company="Acme", title="Engineer", status="applied")
    ch = J.ai_changes(ws)
    check("adding an application is recorded", len(ch) == 1 and ch[0]["kind"] == "add",
          str([c["kind"] for c in ch]))
    J.keep_ai_change(ws, ch[0]["id"])
    check("keeping one clears it", J.ai_changes_pending(ws) == 0)

    mcp_server.set_job_status(job_id=job["id"], status="Interviewing", append_note="Invite came in.")
    ch = J.ai_changes(ws)
    fields = {c["field"]: c for c in ch[0]["changes"]} if ch else {}
    check("a status change is recorded, by the client that made it",
          len(ch) == 1 and ch[0]["by"] == "claude" and ch[0]["tool"] == "set_job_status")
    check("as status codes, for the app to translate",
          fields.get("status", {}).get("from") == "applied"
          and fields.get("status", {}).get("to") == "interviewing", str(fields.get("status")))
    check("a note shows only what was added", fields.get("notes", {}).get("to") == "Invite came in.",
          str(fields.get("notes")))
    check("the history is not listed as a field of its own", "status_history" not in fields)

    r = J.undo_ai_change(ws, ch[0]["id"])
    back = r.get("job") or {}
    check("undo puts the status back", back.get("status") == "applied", back.get("status"))
    check("and the history with it", [h["status"] for h in back.get("status_history", [])] == ["applied"])
    check("and the note", not (back.get("notes") or ""))

    mcp_server.update_job_tracking(job_id=job["id"], followup_date="2099-01-05")
    first = J.ai_changes(ws)[0]
    J.update_job(ws, job["id"], {"followup_date": "2099-02-01"})        # the user, afterwards
    try:
        J.undo_ai_change(ws, first["id"])
        check("undo refuses over a later change", False)
    except ValueError as exc:
        check("undo refuses over a later change", "changed again" in str(exc), str(exc))
    check("and leaves the later value", J.list_jobs(ws)[0]["followup_date"] == "2099-02-01")

    mcp_server.set_company_logo(company="Acme", image_path=str(HERE / "static" / "brand-mark.png"))
    check("a logo alone is not a change to review",
          all(c["tool"] != "set_company_logo" for c in J.ai_changes(ws)))

    other = mcp_server.add_job(company="Globex", title="Analyst", confirmed_new=True)
    mcp_server.set_job_status(job_id=other["id"], status="interviewing")
    mcp_server.update_job_tracking(job_id=other["id"], followup_date="2099-03-01")
    mine = [c for c in J.ai_changes(ws) if c["job_id"] == other["id"]]
    check("changes made in the same second are listed newest first",
          [c["tool"] for c in mine] == ["update_job_tracking", "set_job_status", "add_job"],
          str([c["tool"] for c in mine]))
    J.undo_ai_change(ws, mine[-1]["id"])
    check("undoing an addition moves it to the trash",
          all(j["id"] != other["id"] for j in J.list_jobs(ws)))
    check("and takes its later changes off the list",
          not [c for c in J.ai_changes(ws) if c["job_id"] == other["id"]])

    # A client that can ask the user: a "no" changes nothing.
    mcp_server.ask_user = lambda message: False
    try:
        mcp_server.set_job_status(job_id=job["id"], status="offer")
        check("a declined confirmation changes nothing", False)
    except Exception as exc:
        check("a declined confirmation changes nothing",
              "declined" in str(exc) and J.list_jobs(ws, q="Acme")[0]["status"] == "applied", str(exc))

    print(f"\n{fails} failure(s)" if fails else "\nevery AI change check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
