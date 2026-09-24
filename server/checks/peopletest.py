"""People on an application: kept clean, mirrored for mail matching, and
added by an AI client without duplicates.

    python checks/peopletest.py
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
    studio.open_sample()
    ws, J = studio.WORKSPACE, studio.jobstore
    monzo = next(j for j in J.list_jobs(ws) if j["company"] == "Monzo" and j.get("people"))
    check("the sample has people", len(monzo["people"]) == 2, str(monzo["people"]))
    check("their first email is the contact the mail matching reads",
          monzo["contact_email"] == monzo["people"][0]["email"])

    j = next(x for x in J.list_jobs(ws) if not x.get("people") and not x.get("contact_email"))
    got = J.update_job(ws, j["id"], {"people": [
        {"name": "  Camille Laurent ", "role": "Recruiter", "email": "camille@acme.example", "link": "acme.example/c"},
        {"name": "", "email": ""}, {"junk": 1}]})
    ps = got["people"]
    check("names are trimmed, the empty are dropped", len(ps) == 1 and ps[0]["name"] == "Camille Laurent", str(ps))
    check("a bare link becomes a web link", ps[0]["link"] == "https://acme.example/c", ps[0]["link"])
    check("each gets an id", bool(ps[0]["id"]))
    check("and the contact email follows", got["contact_email"] == "camille@acme.example")
    try:
        J.update_job(ws, j["id"], {"people": [{"name": "X", "email": "not an email"}]})
        check("a broken email is refused", False)
    except ValueError as exc:
        check("a broken email is refused", "not an email address" in str(exc), str(exc))

    mcp_server.save_person(j["id"], email="CAMILLE@acme.example", role="Hiring manager")
    after = next(x for x in J.list_jobs(ws) if x["id"] == j["id"])["people"]
    check("an AI client completes the same person by email, not a second one",
          len(after) == 1 and after[0]["role"] == "Hiring manager" and after[0]["name"] == "Camille Laurent",
          str(after))

    legacy = next(x for x in J.list_jobs(ws) if not x.get("people"))
    J.update_job(ws, legacy["id"], {"contact_email": "old@firm.example"})
    back = next(x for x in J.list_jobs(ws) if x["id"] == legacy["id"])
    check("an old single contact shows as a person",
          len(back["people"]) == 1 and back["people"][0]["email"] == "old@firm.example", str(back["people"]))

    ctx = studio.draft_context(monzo["id"])
    check("a draft knows whose name to sign", ctx["you"] == "Alex Moreau", str(ctx))
    check("and when you applied and met", bool(ctx["applied"]) and bool(ctx["interviewed"]), str(ctx))
    studio.close_sample()
    print()
    print(f"{fails} failure(s)" if fails else "every people check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
