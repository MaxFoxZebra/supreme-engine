"""What a tool asks the user directly, through a client that can show a form
(MCP elicitation): the question, and what each answer does. The form is
replaced by scripted answers; a client that cannot ask is checked too.

    python checks/elicittest.py
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
import mcp_server as M  # noqa: E402

fails = 0
asked: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def answer(reply):
    """Every form from here on gets `reply`: a callable of (message, schema)."""
    def fake(message, schema):
        asked.append(message)
        return reply(message, schema)
    M._elicit = fake


yes = lambda m, s: s(**({"confirm": True} if "confirm" in s.model_fields else {}))
no = lambda m, s: M.DECLINED
cannot = lambda m, s: None


def raises(fn, needle: str) -> bool:
    try:
        fn()
    except Exception as exc:
        return needle in str(exc)
    return False


def main() -> int:
    studio.WORKSPACE = Path(tempfile.mkdtemp())
    studio.bootstrap(studio.WORKSPACE)
    ws, J = studio.WORKSPACE, studio.jobstore

    answer(cannot)
    a = M.add_job(company="Acme", title="Engineer", status="applied")
    check("without a form, a likely duplicate is refused as before",
          raises(lambda: M.add_job(company="Acme", title="Analyst"), "confirmed_new=True"))
    answer(no)
    check("a duplicate the user rejects is not added",
          raises(lambda: M.add_job(company="Acme", title="Analyst"), "not a new application")
          and len(J.list_jobs(ws)) == 1)
    check("and they were shown what is already there", "Engineer (Awaiting reply)" in asked[-1], asked[-1])
    answer(yes)
    b = M.add_job(company="Acme", title="Analyst")
    check("one they confirm is added", len(J.list_jobs(ws)) == 2)

    answer(lambda m, s: s(choice="Analyst (Draft)"))
    got = M.find_job(company="Acme", about="Re: your application, 2 Oct")
    check("several matches: the user picks one, and it is the only candidate",
          got["confident"] and got["candidates"][0]["id"] == b["id"], str(got)[:120])
    check("they see what the mail was", "Re: your application" in asked[-1])
    answer(lambda m, s: s(choice="None of these"))
    got = M.find_job(company="Acme")
    check("'none of these' leaves no candidate", got["candidates"] == [] and not got["confident"])
    answer(cannot)
    check("without a form, both candidates come back as before",
          len(M.find_job(company="Acme")["candidates"]) == 2)

    M.update_job_tracking(job_id=a["id"], interview_at="2099-05-02T10:00:00")
    answer(no)
    check("moving an interview the user did not agree to changes nothing",
          raises(lambda: M.update_job_tracking(job_id=a["id"], interview_at="2099-05-03T10:00:00"),
                 "declined") and J.list_jobs(ws, q="Engineer")[0]["interview_at"] == "2099-05-02T10:00:00")
    check("and the question says from when to when", "2099-05-02 at 10:00" in asked[-1]
          and "2099-05-03 at 10:00" in asked[-1], asked[-1])
    check("clearing one is asked too",
          raises(lambda: M.update_job_tracking(job_id=a["id"], interview_at=""), "declined")
          and "Clear the interview" in asked[-1])
    answer(yes)
    M.update_job_tracking(job_id=a["id"], interview_at="2099-05-03T10:00:00")
    check("a move they confirm is made", J.list_jobs(ws, q="Engineer")[0]["interview_at"] == "2099-05-03T10:00:00")
    n = len(asked)
    M.update_job_tracking(job_id=b["id"], interview_at="2099-06-01T09:00:00")
    check("a first interview time is not asked about", len(asked) == n)
    answer(no)
    check("a bad date is refused before anyone is asked",
          raises(lambda: M.update_job_tracking(job_id=a["id"], interview_at="soon"), "ISO")
          and len(asked) == n)

    M._elicit = lambda m, s: None
    M.update_job_tracking(job_id=a["id"], description="The posting as it was saved, edited by hand.")
    answer(no)
    check("replacing a saved posting the user says no to keeps theirs",
          raises(lambda: M.update_job_tracking(job_id=a["id"], description="Another text entirely."),
                 "declined") and "edited by hand" in M.read_job(a["id"])["description"])
    answer(yes)
    M.update_job_tracking(job_id=a["id"], description="Another text entirely.")
    check("and with a yes it is replaced, with no flag needed",
          M.read_job(a["id"])["description"] == "Another text entirely.")
    answer(cannot)
    check("without a form, replacing still needs replace_posting",
          raises(lambda: M.update_job_tracking(job_id=a["id"], description="Third."), "replace_posting"))

    cv = "profile/my-cv.yaml"
    src = M.read_cv(cv)
    bare = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    answer(no)
    check("a whole-file write that drops comments is asked about, and a no keeps the file",
          raises(lambda: M.write_cv(cv, bare), "declined") and M.read_cv(cv) == src)
    check("the question quotes a comment that would go", "comment" in asked[-1], asked[-1][:120])
    n = len(asked)
    M.write_cv(cv, src.replace("Your Role", "Platform Engineer"))
    check("a write that keeps every comment is not asked about", len(asked) == n)

    def unreadable(url):
        raise ValueError("this board needs a sign-in")
    studio.posting.read = unreadable
    posting = "We are hiring a platform engineer. " * 30
    answer(lambda m, s: s(text=posting))
    c = M.add_job(company="Initech", title="Platform Engineer", url="https://www.linkedin.com/jobs/view/1")
    check("a posting the app cannot read is asked for, and what is pasted is saved",
          M.read_job(c["id"])["description"] == posting.strip()
          and "pasted" in (c.get("posting_note") or ""), c.get("posting_note"))
    answer(lambda m, s: s(text=""))
    d = M.add_job(company="Hooli", title="SRE", url="https://www.linkedin.com/jobs/view/2")
    check("left empty, the application is still added", bool(d.get("id")) and not M.read_job(d["id"]).get("description"))

    print(f"\n{fails} failure(s)" if fails else "\nevery elicitation check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
