"""The match: how well the base CV fits a posting, scored by an AI client.

    python checks/matchtest.py

The rules first (every criterion rated, the score worked out here, the
bands); then stored on an application and read back, out of date once the
base CV or the posting moves on; then a job a scan found, scored before it is
an application and carried onto it; then through the MCP server as a client.
"""

from __future__ import annotations

import csv
import io
import json
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

import jobs as J  # noqa: E402
import scans  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


def rated(*ratings) -> list[dict]:
    return [{"key": k, "rating": r, "why": f"{k} because"} for k, r in zip(J.MATCH_CRITERIA, ratings)]


def refused(data) -> bool:
    try:
        J.clean_match(data)
    except ValueError:
        return True
    return False


POSTING = ("## What you will do\n\n- Run our Kubernetes clusters across two regions\n"
           "- Own on-call for the payment systems\n")


def the_rules() -> None:
    print("The rules")
    check("the weights add up to 100", sum(J.MATCH_CRITERIA.values()) == 100)
    check("all fives is 10", J.match_score(rated(5, 5, 5, 5, 5)) == 10.0)
    check("all fours is 9, a top match", J.match_score(rated(4, 4, 4, 4, 4)) == 9.0
          and J.match_band(9.0) == "top")
    m = J.clean_match({"criteria": rated(5, 4, 4, 3, 3), "summary": "Close", "strengths": ["a", "", "b"],
                       "gaps": "not a list", "score": 9.9})
    check("the score is worked out here, not taken", m["score"] == 9.0, m["score"])
    check("in the criteria's own order", [c["key"] for c in m["criteria"]] == list(J.MATCH_CRITERIA))
    check("empty lines and a list that is not one are dropped", m["strengths"] == ["a", "b"] and m["gaps"] == [])
    check("bands start at 9, 7, 5 and 3",
          [J.match_band(x) for x in (9.0, 8.9, 7.0, 6.9, 5.0, 4.9, 3.0, 2.9)] == ["top", "strong", "strong", "good", "good", "fair", "fair", "weak"])
    check("every criterion has to be rated", refused({"criteria": rated(4, 4, 4, 4)}))
    check("from 0 to 5", refused({"criteria": rated(4, 4, 4, 4, 6)}))
    check("in whole numbers", refused({"criteria": [*rated(4, 4, 4, 4), {"key": "practical", "rating": "high"}]}))
    check("and only these criteria", refused({"criteria": [*rated(4, 4, 4, 4, 4), {"key": "vibes", "rating": 5}]}))
    check("nothing clears it", J.clean_match(None) is None and J.clean_match({}) is None)


def on_an_application() -> None:
    print("On an application")
    ws = Path(tempfile.mkdtemp()) / "CV Studio"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)
    bare = J.add_job(ws, {"company": "Initech", "title": "SRE"})
    try:
        studio.save_match(bare["id"], {"criteria": rated(4, 4, 4, 4, 4)})
        check("a posting is needed to score against", False)
    except ValueError as exc:
        check("a posting is needed to score against", "posting" in str(exc), exc)
    job = J.add_job(ws, {"company": "Acme", "title": "SRE", "description": POSTING})
    before = J.list_jobs(ws)[0]["updated_at"]
    m = studio.save_match(job["id"], {"criteria": rated(5, 4, 4, 3, 3), "summary": "Close"}, by="claude")
    check("it is stored with who scored it and against which CV",
          m["score"] == 9.0 and m["by"] == "claude" and m["cv"] == "profile/my-cv.yaml" and m["stale"] == [], m)
    row = next(x for x in J.list_jobs(ws) if x["id"] == job["id"])
    check("and read back with the application", row["match"]["score"] == 9.0 and row["match"]["summary"] == "Close")
    text = next((ws / "tracker").glob("acme-*.yaml")).read_text(encoding="utf-8")
    check("in the application's own file", "match:" in text and "requirements" in text)
    base = studio._base_sig()
    check("current while nothing changed", studio.match_stale(row["match"], row, base) == [])
    cv = ws / "profile" / "my-cv.yaml"
    cv.write_text(cv.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    check("out of date once the base CV is edited",
          studio.match_stale(row["match"], row, studio._base_sig()) == ["cv"])
    J.update_job(ws, job["id"], {"description": POSTING + "- And on weekends\n"})
    row = next(x for x in J.list_jobs(ws) if x["id"] == job["id"])
    check("and once the posting changes",
          studio.match_stale(row["match"], row, studio._base_sig()) == ["cv", "posting"])
    m = studio.save_match(job["id"], {"criteria": rated(3, 3, 3, 3, 3)}, by="claude")
    check("scoring again replaces it, current again", m["score"] == 7.2 and m["stale"] == [], m)
    out = list(csv.DictReader(io.StringIO(J.export(ws, "csv"))))
    check("the export carries the score", {r["company"]: r["match"] for r in out} == {"Acme": "7.2", "Initech": ""},
          [(r["company"], r["match"]) for r in out])
    J.update_job(ws, job["id"], {"match": None})
    check("and it can be cleared", next(x for x in J.list_jobs(ws) if x["id"] == job["id"])["match"] is None)
    check("the user's own fit is a separate thing", "score" in J.FIELDS and before)


def from_a_scan() -> None:
    print("From a scan")
    ws = Path(tempfile.mkdtemp()) / "CV Studio"
    studio.WORKSPACE = ws
    studio.bootstrap(ws)
    r = studio.scan_record([
        {"company": "Acme", "role": "Platform Engineer", "link": "http://127.0.0.1:9/acme",
         "description": POSTING, "match": {"criteria": rated(5, 5, 4, 4, 4), "summary": "Close",
                                           "strengths": ["Kubernetes"], "gaps": ["Payments"]}},
        {"company": "Globex", "role": "TAM", "link": "http://127.0.0.1:9/globex"},
    ], scan="Morning search", by="claude")
    check("a scored job comes back with its score and key",
          [(x["company"], x["score"]) for x in r["scored"]] == [("Acme", 9.6)], r["scored"])
    sheet = {x["company"]: x for x in scans.read_file(studio.scan_folder() / scans.FILE_NAME)}
    check("the score is a column of the spreadsheet", sheet["Acme"]["extra"].get("Score") == "9.6"
          and not sheet["Globex"]["extra"].get("Score"), sheet["Acme"]["extra"])
    check("and the reasons are not", "Close" not in json.dumps(sheet["Acme"]))
    st = {x["company"]: x for x in studio.scans_state()["new"]}
    check("its card shows the match, not the column",
          st["Acme"]["match"]["score"] == 9.6 and "Score" not in st["Acme"]["details"]
          and "match" not in st["Globex"], st["Acme"])
    added = studio.scan_add(st["Acme"]["key"])["job"]
    check("a yes carries the match onto the application",
          (added.get("match") or {}).get("score") == 9.6 and added["match"]["by"] == "claude"
          and added["match"]["stale"] == [], added.get("match"))
    stored = next(x for x in J.list_jobs(ws) if x["id"] == added["id"])
    check("where it stays", stored["match"]["gaps"] == ["Payments"])
    try:
        studio.scan_record([{"company": "Bad", "role": "X", "link": "http://127.0.0.1:9/bad",
                             "match": {"criteria": rated(9, 9, 9, 9, 9)}}])
        check("a match that does not add up is refused", False)
    except ValueError:
        check("a match that does not add up is refused", True)


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
        check("score_job is listed, and is not read-only", "score_job" in tools
              and not (tools["score_job"].get("annotations") or {}).get("readOnlyHint"))
        call = lambda name, args: c.send("tools/call", {"name": name, "arguments": args})["result"]
        text = lambda r: r["content"][0]["text"]
        made = json.loads(text(call("add_job", {"company": "Acme", "title": "SRE", "description": POSTING,
                                                "confirmed_new": True})))
        check("add_job says to score it next", "score_job" in made.get("next", ""), made.get("next"))
        listed = call("list_jobs", {})["structuredContent"]["result"]
        check("list_jobs shows it has no match yet", listed[0].get("match") is None, listed[0])
        r = call("score_job", {"job_id": made["id"], "criteria": rated(4, 4, 4, 4, 5),
                               "summary": "A close fit.", "strengths": ["Kubernetes"], "gaps": ["Payments"]})
        m = json.loads(text(r))
        check("score_job scores it out of 10 and names the band",
              not r.get("isError") and m["score"] == 9.1 and m["band"] == "top", text(r)[:200])
        listed = call("list_jobs", {})["structuredContent"]["result"]
        check("and list_jobs shows the score, current",
              listed[0].get("match") == {"score": 9.1, "stale": False}, listed[0].get("match"))
        row = json.loads(text(call("read_job", {"job_id": made["id"]})))
        check("read_job has the reasons", row["match"]["gaps"] == ["Payments"] and row["match"]["stale"] == [])
        r = call("score_job", {"job_id": made["id"], "criteria": rated(4, 4), "summary": "?"})
        check("half a rating is refused", bool(r.get("isError")), text(r)[:160])
        r = call("record_job_scan", {"jobs": [{"company": "Globex", "role": "SRE", "link": "https://globex.example/1",
                                                "match": {"criteria": rated(5, 4, 4, 4, 4), "summary": "Close"}}]})
        check("a scan's score comes back with the key to add it by",
              re.search(r"Globex, SRE: 9\.3 \(key [^)]+\)", text(r)) is not None, text(r))
    finally:
        c.close()


if __name__ == "__main__":
    the_rules()
    on_an_application()
    from_a_scan()
    through_mcp()
    print()
    print("every match check passes" if not fails else f"{fails} match check(s) failed")
    sys.exit(1 if fails else 0)
