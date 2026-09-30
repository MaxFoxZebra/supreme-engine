"""Reviewing what an AI client changed, end to end, in a throwaway workspace.

    python checks/reviewtest.py

Writes to a letter and a CV the way the MCP tools do, watched the way the MCP
wrapper watches them, then keeps and undoes the changes one unit at a time and
all at once. What must hold: the first write's "before" survives later
writes, a kept change stops being shown, an undone one is back as it was and
nothing else moves, undoing everything gives back the file byte for byte, a
letter the model created can be taken back, and a request made against a
document that has since changed is refused.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import letters  # noqa: E402
import mcp_server  # noqa: E402
import review  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


CV = """cv:
  name: Alex Moreau
  headline: Staff Engineer   # the one I use on LinkedIn
  location: Lyon, France
  sections:
    experience:
      - company: Northwind
        position: Platform Engineer
        start_date: 2021-01
        highlights:
          - Moved payments onto Kubernetes
          - Cut deploy time from 40 to 6 minutes
      - company: Globex
        position: Backend Engineer
        start_date: 2018-01
        end_date: 2020-12
design:
  theme: classic
"""

LETTER = """---
company: Northwind
subject: Application for Platform Engineer
language: en
---
Dear Hiring Team,

Northwind moved its payments platform onto Kubernetes.

I would like to help.

Kind regards,
"""


def as_ai(tool: str, fn) -> None:
    """Run fn the way an MCP tool call runs: watched."""
    before = mcp_server._snapshot()
    fn()
    mcp_server._checkpoint(before, tool)


def ids(payload) -> list[str]:
    return [u["id"] for u in (payload or {}).get("units", [])]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        studio.WORKSPACE = ws
        studio.CLIENT_ID = "claude"
        (ws / "profile").mkdir()
        (ws / "letters").mkdir()
        cv = ws / "profile" / "my-cv.yaml"
        lt = ws / "letters" / "cover-northwind.md"
        cv.write_text(CV, encoding="utf-8")
        lt.write_text(LETTER, encoding="utf-8")

        print("A letter")
        check("nothing to review before any AI write", studio.review_payload(lt) is None)
        as_ai("write_letter", lambda: studio.save_letter(lt, {
            "meta": {"subject": "Platform Engineer, Northwind"},
            "body": "Dear Hiring Team,\n\nNorthwind moved its payments platform onto "
                    "Kubernetes, and I led that move.\n\nI would like to help.\n\n"
                    "- Six-minute deploys\n- On call for three years\n\nKind regards,"}))
        r = studio.review_payload(lt)
        check("the changes are waiting, by the client that made them",
              r is not None and r["by"] == "claude" and r["writes"] == 1, repr(r and r["by"]))
        kinds = sorted((u["id"].split(":")[0], u.get("tag")) for u in r["units"])
        check("the subject, an edited paragraph and an added list",
              kinds == [("body", "chg"), ("body", "ins"), ("meta", None)], repr(kinds))
        chg = next(u for u in r["units"] if u.get("tag") == "chg")
        check("the edited paragraph carries both versions",
              "I led that move" in chg["after"] and "I led" not in chg["before"])
        check("unchanged paragraphs are not units",
              not any("I would like to help" in str(u.get("after")) for u in r["units"]))

        as_ai("write_letter", lambda: studio.save_letter(lt, {"meta": {"place": "Lyon"}}))
        r = studio.review_payload(lt)
        check("a second write keeps the first one's before",
              r["writes"] == 2 and "meta:place" in ids(r) and "meta:subject" in ids(r),
              repr(ids(r)))

        studio.review_resolve(lt, ["meta:subject"], "keep", r["sig"])
        r = studio.review_payload(lt)
        check("a kept change stops being shown", "meta:subject" not in ids(r), repr(ids(r)))
        check("and the letter still has it",
              letters.parse(lt.read_text(encoding="utf-8"))[0]["subject"]
              == "Platform Engineer, Northwind")

        ins = next(u["id"] for u in r["units"] if u.get("tag") == "ins")
        studio.review_resolve(lt, [ins], "undo", r["sig"])
        body = letters.parse(lt.read_text(encoding="utf-8"))[1]
        check("an undone paragraph is gone", "Six-minute deploys" not in body)
        check("and the rest of what it wrote stays", "I led that move" in body)
        r = studio.review_payload(lt)
        check("the undone unit is no longer shown", ins not in ids(r), repr(ids(r)))

        try:
            studio.review_resolve(lt, ["*"], "undo", "stale0000000")
            check("a stale request is refused", False)
        except ValueError:
            check("a stale request is refused", True)

        studio.review_resolve(lt, ["*"], "undo", r["sig"])
        meta, body = letters.parse(lt.read_text(encoding="utf-8"))
        check("undo all goes back to the kept text, keeping what was kept",
              "I led that move" not in body and meta["subject"] == "Platform Engineer, Northwind"
              and meta.get("place") is None, repr(meta))
        check("and there is nothing left to review", studio.review_payload(lt) is None)

        print("A CV")
        as_ai("edit_cv_fields", lambda: studio.apply_patches(cv, [
            {"path": ["cv", "headline"], "value": "Principal Engineer"},
            {"path": ["cv", "sections", "experience", 0, "highlights", 1],
             "value": "Cut deploy time from 40 to 6 minutes across 30 services"}]))
        as_ai("write_cv", lambda: studio.apply_ops(cv, [
            {"op": "add_entry", "section": "experience", "at": 1,
             "value": {"company": "Initech", "position": "SRE", "start_date": "2020-01"}}]))
        r = studio.review_payload(cv)
        u = {x["id"]: x for x in r["units"]}
        tags = sorted((k.split(":")[0], x.get("tag")) for k, x in u.items())
        check("a header field, a changed entry and an added one",
              tags == [("cv", None), ("ent", "chg"), ("ent", "ins")], repr(tags))
        added = next(k for k, x in u.items() if x.get("tag") == "ins")
        check("the added entry is named as the outline names it",
              "Initech" in u[added]["where"], u[added]["where"])

        studio.review_resolve(cv, [added], "undo", r["sig"])
        text = cv.read_text(encoding="utf-8")
        check("an undone entry is gone, the others stay",
              "Initech" not in text and "30 services" in text and "Principal" in text)
        check("comments survive", "# the one I use on LinkedIn" in text)
        r = studio.review_payload(cv)
        studio.review_resolve(cv, ["cv:headline"], "keep", r["sig"])
        r = studio.review_payload(cv)
        check("kept header field is no longer shown", "cv:headline" not in ids(r), repr(ids(r)))
        studio.review_resolve(cv, ["*"], "undo", r["sig"])
        text = cv.read_text(encoding="utf-8")
        check("undo all keeps what was kept and undoes the rest",
              "Principal Engineer" in text and "30 services" not in text, text[:120])
        check("nothing left", studio.review_payload(cv) is None)

        held = cv.read_text(encoding="utf-8")
        as_ai("write_cv", lambda: studio.write_doc(cv, CV.replace("Lyon", "Paris"), "write_cv"))
        r = studio.review_payload(cv)
        studio.review_resolve(cv, ["*"], "undo", r["sig"])
        check("undo of a whole-file write gives the file back byte for byte",
              cv.read_text(encoding="utf-8") == held)

        print("A letter the model made, renamed, then taken back")
        new = ws / "letters" / "cover-globex.md"
        as_ai("create_letter", lambda: new.write_text(LETTER.replace("Northwind", "Globex"),
                                                      encoding="utf-8"))
        r = studio.review_payload(new)
        check("a new document is one unit", ids(r) == ["created"] and r["created"], repr(ids(r)))
        moved = studio.rename_document("letters/cover-globex.md", "globex")
        dest = ws / moved["path"]
        check("renaming carries the review", studio.review_payload(dest) is not None)
        out = studio.review_resolve(dest, ["*"], "undo", studio.review_payload(dest)["sig"])
        check("undoing it moves it to the trash", not dest.exists() and out.get("deleted"))
        check("and forgets it", review.pending(ws) == {}, repr(review.pending(ws)))

        print("Only the app's own process writes nothing")
        studio.save_letter(lt, {"meta": {"place": "Nantes"}})
        check("a save from the app is not an AI change", studio.review_payload(lt) is None)

    print("\nevery review check passes" if not fails else f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
