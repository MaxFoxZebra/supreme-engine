"""Interview rounds: stored, checked, and kept in step with the one
interview time the calendar, the reminders and an AI client read.

    python checks/roundstest.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import jobs  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    ws = Path(tempfile.mkdtemp())
    j = jobs.add_job(ws, {"company": "Acme", "title": "SRE", "status": "applied"})
    check("a new application has no rounds", j["rounds"] == [])

    j = jobs.update_job(ws, j["id"], {"interview_at": "2026-10-01T10:00:00", "interview_tz": "Europe/London"})
    check("an interview time alone reads as one round",
          len(j["rounds"]) == 1 and j["rounds"][0]["at"] == "2026-10-01T10:00", repr(j["rounds"]))

    rounds = [{"kind": "Recruiter screen", "at": "2026-09-20T15:00", "outcome": "passed"},
              {"kind": "Technical", "at": "2026-10-01T10:00", "tz": "Europe/London", "with": "Tom"},
              {"kind": "Team fit"}]
    j = jobs.update_job(ws, j["id"], {"rounds": rounds})
    check("three rounds are kept in order", [r["kind"] for r in j["rounds"]] ==
          ["Recruiter screen", "Technical", "Team fit"])
    check("each has an id", all(r["id"] for r in j["rounds"]))
    check("the interview time is the next undecided round",
          j["interview_at"] == "2026-10-01T10:00:00" and j["interview_tz"] == "Europe/London",
          f"{j['interview_at']} {j['interview_tz']}")

    rs = [dict(r) for r in j["rounds"]]
    rs[1]["outcome"] = "passed"
    rs[2].update(at="2026-10-08T14:00", tz="")
    j = jobs.update_job(ws, j["id"], {"rounds": rs})
    check("passing a round moves the interview time on",
          j["interview_at"] == "2026-10-08T14:00:00" and j["interview_tz"] is None,
          f"{j['interview_at']} {j['interview_tz']}")

    j = jobs.update_job(ws, j["id"], {"interview_at": "2026-10-09T09:00:00"})
    check("moving the interview time moves that round", j["rounds"][2]["at"] == "2026-10-09T09:00")

    rs = [dict(r, outcome="passed") for r in j["rounds"]]
    j = jobs.update_job(ws, j["id"], {"rounds": rs})
    check("with every round decided, the last one stays the interview time",
          j["interview_at"] == "2026-10-09T09:00:00")
    j = jobs.update_job(ws, j["id"], {"interview_at": "2026-10-20T11:00:00"})
    check("a new interview time after that adds a round",
          len(j["rounds"]) == 4 and j["rounds"][3]["at"] == "2026-10-20T11:00")

    for bad, why in (([{"at": "tomorrow"}], "a date that is not one"),
                     ([{"tz": "Mars/Olympus"}], "a time zone that is not one"),
                     ([{"outcome": "maybe"}], "an outcome not on the list"),
                     ("three rounds", "something that is not a list")):
        try:
            jobs.update_job(ws, j["id"], {"rounds": bad})
            check(f"refuses {why}", False)
        except ValueError:
            check(f"refuses {why}", True)

    csv = jobs.export(ws, "csv")
    check("the CSV export lists the rounds", "Recruiter screen 2026-09-20 15:00 (passed)" in csv)
    print()
    print(f"{fails} failure(s)" if fails else "every rounds check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
