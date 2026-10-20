"""Job scans: what a scheduled search found, waiting for a yes or a no.

    python checks/scanstest.py

The spreadsheet first (a sheet written by someone else, the one CV Studio
writes, a file open in Excel); then a yes and a no in the process, the way the
app's cards reach them; then through the MCP server as a client, the way a
morning search hands its jobs over.
"""

from __future__ import annotations

import csv
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "checks"))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import scans  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


# The columns a scheduled search writes to a Google Sheet, exported as CSV.
SHEET = [
    ["Date", "Scan", "Company", "Role", "Location", "Sector", "Top pick", "Brazil office", "Posted",
     "Why it fits", "Link", "My status"],
    ["2026-10-14", "Media, AI & cloud", "Dailymotion", "Solutions Engineer", "Paris", "Video & media", "", "",
     "date unknown", "Video delivery", "https://www.welcometothejungle.com/x/dm?utm_source=scan", "To review"],
    ["2026-10-14", "Media, AI & cloud", "Scaleway", "AI Solutions Architect", "Paris", "Cloud & data", "Yes", "",
     "2026-10-11", "Cloud", "https://jobs.lever.co/scaleway/1", "To review"],
    ["2026-10-14", "Retail, e-commerce & payments", "Adyen", "Solutions Engineer", "Paris", "Payments", "", "Yes",
     "2026-10-09", "Payments", "https://careers.adyen.com/1", "Not interested"],
    ["2026-10-13", "Media, AI & cloud", "Mistral AI", "Forward Deployed Engineer", "Paris", "AI labs", "", "",
     "2026-10-12", "LLM", "https://jobs.ashbyhq.com/mistral/1", "Applied"],
]


def the_spreadsheet() -> None:
    print("A sheet someone else wrote")
    folder = Path(tempfile.mkdtemp()) / "claude crons"
    folder.mkdir()
    with open(folder / "scans.csv", "w", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows(SHEET)
    st = scans.state(folder, [])
    new = {r["company"]: r for r in st["new"]}
    check("its columns are read by name", set(new) == {"Dailymotion", "Scaleway"}, list(new))
    check("the scan is the scan, and the sector a column of its own",
          new["Scaleway"]["scan"] == "Media, AI & cloud" and new["Scaleway"]["details"].get("Sector") == "Cloud & data",
          new["Scaleway"])
    check("a status in words is understood", st["counts"] == {"new": 2, "added": 1, "skipped": 1}, st["counts"])
    check("a top pick comes first in its day", st["new"][0]["company"] == "Scaleway", [r["company"] for r in st["new"]])
    check("a link is the same job without its tracking",
          scans.key_of({"link": "https://www.welcometothejungle.com/x/dm/?utm_source=other#top"})
          == new["Dailymotion"]["key"])

    print("The one CV Studio writes")
    before = scans.stamp(folder)
    r = scans.record(folder, [
        {"company": "Dust", "title": "Solutions Engineer", "url": "https://jobs.ashbyhq.com/dust/9", "why": "Agents",
         "details": {"Top pick": True, "Sector": "AI labs", "Location": "ignored"}},
        {"company": "Scaleway", "role": "AI Solutions Architect", "link": "https://jobs.lever.co/scaleway/1/"},
        {"company": "Acme", "role": "Platform Engineer", "link": "https://acme.example/jobs/2"},
    ], [{"company": "Acme", "title": "Platform Engineer", "url": ""}], scan="Media, AI & cloud")
    check("a job already there is left out, one already applied to is not offered",
          (r["added"], r["already"], r["tracked"]) == (1, 1, 1), r)
    check("it is a real spreadsheet", (folder / scans.FILE_NAME).is_file()
          and scans.read_file(folder / scans.FILE_NAME)[0]["company"])
    dust = next(x for x in scans.read_file(folder / scans.FILE_NAME) if x["company"] == "Dust")
    check("with what the search said beyond the columns", dust["extra"] == {"Top pick": "Yes", "Sector": "AI labs"},
          dust["extra"])
    check("the app can tell it changed", scans.stamp(folder) != before)
    keys = {x["company"]: x["key"] for x in scans.state(folder, [])["new"]}
    scans.set_status(folder, [keys["Dailymotion"]], scans.SKIPPED, [])
    again = scans.record(folder, [{"company": "Dailymotion", "role": "Solutions Engineer",
                                   "link": "https://www.welcometothejungle.com/x/dm"}], [])
    check("a job passed on is not offered again", again["added"] == 0
          and "Dailymotion" not in [x["company"] for x in scans.state(folder, [])["new"]], again)
    check("and the sheet it came from is not rewritten",
          (folder / "scans.csv").read_text(encoding="utf-8").count("To review") == 2)

    print("Open in Excel")
    real = scans.write_xlsx

    def locked(*a, **k):
        raise PermissionError("in use")
    scans.write_xlsx = locked
    try:
        r = scans.record(folder, [{"company": "Qonto", "role": "Solutions Engineer",
                                   "link": "https://qonto.example/1"}], [])
        check("a job waits beside the file", not r["written"] and (folder / scans.PENDING).is_file(), r)
        check("and is shown already", "Qonto" in [x["company"] for x in scans.state(folder, [])["new"]])
    finally:
        scans.write_xlsx = real
    scans.set_status(folder, [keys["Scaleway"]], scans.SKIPPED, [])
    check("and is written with the next change", not (folder / scans.PENDING).exists()
          and any(x["company"] == "Qonto" for x in scans.read_file(folder / scans.FILE_NAME)))

    print("An empty file made for it")
    other = Path(tempfile.mkdtemp())
    (other / "jobs.xlsx").write_bytes(b"")
    check("is the one it writes", scans.own_file(other).name == "jobs.xlsx")
    scans.record(other, [{"company": "Acme", "role": "SRE", "link": "https://acme.example/sre"}], [])
    check("and becomes the spreadsheet", scans.read_file(other / "jobs.xlsx")[0]["company"] == "Acme")


def yes_and_no() -> None:
    print("Yes and no, in the app")
    ws = Path(tempfile.mkdtemp()) / "CV Studio"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)
    (ws / "claude crons").mkdir()
    check("the scans folder can be any folder of the workspace but CV Studio's own",
          studio.scan_folder_choices() == ["claude crons"], studio.scan_folder_choices())
    studio.save_prefs({"scan_folder": "claude crons"})
    check("and is the one chosen", studio.scan_folder() == ws / "claude crons")
    studio.scan_record([
        {"company": "Acme", "role": "Solutions Engineer", "link": "http://127.0.0.1:9/acme",
         "why": "The integrations work", "notes": "Seen twice"},
        {"company": "Globex", "role": "TAM", "link": "http://127.0.0.1:9/globex"},
    ], scan="Morning search")
    st = studio.scans_state()
    k = {r["company"]: r["key"] for r in st["new"]}
    r = studio.scan_add(k["Acme"])
    job = r["job"]
    check("a yes is an application", job["company"] == "Acme" and job["title"] == "Solutions Engineer"
          and job["url"] == "http://127.0.0.1:9/acme", job)
    check("with the search's reason in its notes, and the search as its source",
          "The integrations work" in (job.get("notes") or "") and job.get("source") == "Morning search", job)
    check("and says when its posting could not be read", r["posting"] == "unread", r["posting"])
    st = studio.scans_state()
    check("it is not waiting any more", [x["company"] for x in st["new"]] == ["Globex"]
          and st["counts"]["added"] == 1, st["counts"])
    again = studio.scan_add(k["Acme"])
    check("a second yes adds nothing", again["already"] and len(studio._all_jobs()) == 1, again)
    studio.scan_undo(k["Acme"], job["id"])
    check("taking it back moves the application to the trash",
          not studio._all_jobs() and any(t["id"] == job["id"] for t in studio.jobstore.list_trash(ws)))
    check("and the job is waiting again", "Acme" in [x["company"] for x in studio.scans_state()["new"]])
    studio.scan_decide([k["Globex"]], "Skipped")
    check("a no is remembered", studio.scans_state()["counts"]["skipped"] == 1)
    studio.scan_undo(k["Globex"])
    check("and can be taken back", "Globex" in [x["company"] for x in studio.scans_state()["new"]])
    try:
        studio.scan_add("https://nowhere.example/x")
        check("a job not in the folder is refused", False)
    except ValueError:
        check("a job not in the folder is refused", True)
    try:
        studio.scan_decide([k["Globex"]], "Maybe")
        check("a status that is not one is refused", False)
    except ValueError:
        check("a status that is not one is refused", True)


def through_mcp() -> None:
    from mcpclient import Client
    print("Through an AI client")
    ws = Path(tempfile.mkdtemp()) / "ws"
    c = Client(os.environ.get("PYTHON") or sys.executable,
               [str(HERE / "server_main.py"), "--mcp", "--workspace", str(ws), "--client", "claude"])
    try:
        c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "claude-ai", "version": "1"}})
        c.send("notifications/initialized", {}, notify=True)
        tools = {t["name"]: t for t in c.send("tools/list", {})["result"]["tools"]}
        check("the scan tools are listed", {"job_scans", "record_job_scan"} <= set(tools))
        check("job_scans only reads", (tools["job_scans"].get("annotations") or {}).get("readOnlyHint") is True)
        call = lambda name, args: c.send("tools/call", {"name": name, "arguments": args})["result"]
        jobs = [{"company": "Scaleway", "role": "AI Solutions Architect", "link": "https://jobs.lever.co/scaleway/1",
                 "why": "Cloud", "details": {"Top pick": True}},
                {"company": "Datadog", "role": "Sales Engineer", "link": "https://careers.datadoghq.com/1"}]
        r = call("record_job_scan", {"jobs": jobs, "scan": "Media, AI & cloud"})
        text = r["content"][0]["text"]
        check("a search hands its jobs over", not r.get("isError") and text.startswith("2 new job"), text)
        r = call("record_job_scan", {"jobs": jobs, "scan": "Media, AI & cloud"})
        check("and handing them over again is harmless", r["content"][0]["text"].startswith("0 new job"),
              r["content"][0]["text"])
        listing = call("job_scans", {})["content"][0]["text"]
        check("what is waiting can be read", "Waiting: 2" in listing and "Scaleway" in listing, listing)
        check("nothing became an application", not list((ws / "tracker").glob("*.yaml")))
        # The user's yes or no, told to Claude rather than swiped in the app.
        call("record_job_scan", {"jobs": [{"company": "Qonto", "role": "Solutions Engineer", "why": "Fintech"}]})
        listing = call("job_scans", {})["content"][0]["text"]
        key = lambda company: next((m.group(1) for line in listing.splitlines() if company in line
                                    for m in [re.search(r"\(key ([^)]+)\)$", line)] if m), None)
        check("each waiting job is listed with the key to decide it by",
              all(key(n) for n in ("Scaleway", "Datadog", "Qonto")), listing)
        r = call("decide_job_scan", {"key": key("Datadog"), "decision": "no"})
        check("a no passes on it", not r.get("isError") and "Waiting: 2" in call("job_scans", {})["content"][0]["text"],
              r["content"][0]["text"])
        r = call("decide_job_scan", {"key": key("Qonto"), "decision": "yes"})
        made = list((ws / "tracker").glob("*.yaml"))
        check("a yes makes it an application, and says what comes next",
              not r.get("isError") and len(made) == 1 and "tailored" in r["content"][0]["text"], r["content"][0]["text"])
        r = call("decide_job_scan", {"key": key("Scaleway"), "decision": "maybe"})
        check("and nothing but yes or no is taken", bool(r.get("isError")))
        r = call("record_job_scan", {"jobs": []})
        check("no jobs is an error", bool(r.get("isError")), r)
    finally:
        c.close()


if __name__ == "__main__":
    the_spreadsheet()
    yes_and_no()
    through_mcp()
    print()
    print("every scan check passes" if not fails else f"{fails} scan check(s) failed")
    sys.exit(1 if fails else 0)
