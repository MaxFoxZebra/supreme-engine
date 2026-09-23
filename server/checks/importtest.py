"""Import a CV the ways a person would, and check RenderCV accepts the result.

    python checks/importtest.py            # the built-in fixtures
    python checks/importtest.py some.pdf   # also any PDF or LinkedIn .zip you pass

The fixtures are a LinkedIn data archive written here, row for row in
LinkedIn's own column names, and a PDF laid out the way LinkedIn's Save to PDF
lays a profile out -- sidebar first, employer and title on separate lines,
durations after the dates -- compiled with the Typst the app already bundles.
An import that RenderCV then refuses is worse than no import, so every result
is rendered, not just parsed.
"""

from __future__ import annotations

import csv
import io
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import importer  # noqa: E402
from cv_render import render_file  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def linkedin_zip() -> bytes:
    def table(rows: list[dict]) -> str:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue()
    files = {
        "Profile.csv": [{"First Name": "Alex", "Last Name": "Moreau", "Maiden Name": "",
                         "Headline": "Staff Engineer at Northwind",
                         "Summary": "I build infrastructure other engineers do not have to think about.",
                         "Geo Location": "Lyon, France", "Websites": ""}],
        "Email Addresses.csv": [{"Email Address": "alex@example.com", "Confirmed": "Yes",
                                 "Primary": "Yes", "Updated On": ""}],
        "PhoneNumbers.csv": [{"Extension": "", "Number": "+33 6 12 34 56 78", "Type": "Mobile"}],
        "Positions.csv": [
            {"Company Name": "Northwind", "Title": "Staff Engineer",
             "Description": "Led the Kubernetes migration.\nCut deploy time from 40 to 6 minutes.",
             "Location": "Lyon, France", "Started On": "Jan 2021", "Finished On": ""},
            {"Company Name": "Contoso", "Title": "Senior Software Engineer",
             "Description": "Built the deployment pipeline.", "Location": "Paris, France",
             "Started On": "Mar 2017", "Finished On": "Dec 2020"}],
        "Education.csv": [{"School Name": "Université Lyon 1", "Start Date": "2012",
                           "End Date": "2014", "Notes": "", "Degree Name": "Master, Computer Science",
                           "Activities": ""}],
        "Skills.csv": [{"Name": "Kubernetes"}, {"Name": "Python"}],
        "Languages.csv": [{"Name": "French", "Proficiency": "Native or bilingual proficiency"}],
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, rows in files.items():
            z.writestr(name, table(rows))
    return out.getvalue()


LINKEDIN_PDF = """#set page(width: 21cm, height: 29.7cm, margin: 1.5cm)
#set text(size: 10pt)
Contact

alex\\@example.com

linkedin.com/in/alex-moreau-dev

Top Skills

Kubernetes

Python

Alex Moreau

Staff Engineer at Northwind

Lyon, France

Summary

I build infrastructure other engineers do not have to think about.

Experience

Northwind

Staff Engineer

January 2021 - Present (4 years 9 months)

Lyon, France

Led the migration of 140 services to Kubernetes with no downtime.

Contoso

Senior Software Engineer

March 2017 - December 2020 (3 years 10 months)

Paris, France

Built the deployment pipeline used by 300 engineers.

Education

Université Claude Bernard Lyon 1

Master's degree, Computer Science · (2012 - 2014)
"""


def renders(cv: dict) -> tuple[bool, str]:
    """Write the imported block into a minimal document and render it."""
    from ruamel.yaml import YAML
    with tempfile.TemporaryDirectory() as tmp:
        doc = Path(tmp) / "imported.yaml"
        y = YAML()
        with doc.open("w", encoding="utf-8") as f:
            y.dump({"cv": cv, "design": {"theme": "engineeringclassic"}}, f)
        r = render_file(doc, Path(tmp) / "out")
        return bool(r.get("ok")), (r.get("log") or "")[-400:]


def main() -> int:
    print("LinkedIn data archive")
    r = importer.import_file("Basic_LinkedInDataExport.zip", linkedin_zip())
    cv = r["cv"]
    check("name, headline, location, email and phone come across",
          (cv.get("name"), cv.get("headline"), cv.get("location"), cv.get("email"),
           cv.get("phone")) == ("Alex Moreau", "Staff Engineer at Northwind", "Lyon, France",
                                "alex@example.com", "+33 6 12 34 56 78"))
    exp = cv["sections"]["experience"]
    check("both positions, current one open-ended",
          [(e["position"], e["company"], e["start_date"], e["end_date"]) for e in exp] ==
          [("Staff Engineer", "Northwind", "2021-01", "present"),
           ("Senior Software Engineer", "Contoso", "2017-03", "2020-12")])
    check("a multi-line description becomes highlights", len(exp[0].get("highlights", [])) == 2)
    edu = cv["sections"]["education"][0]
    check("the degree is split from the subject",
          (edu.get("degree"), edu["area"]) == ("Master", "Computer Science"), repr(edu))
    check("skills and languages come across",
          "Kubernetes" in cv["sections"]["skills"][0]["details"]
          and cv["sections"]["languages"][0]["label"] == "French")
    ok, log = renders(cv)
    check("RenderCV renders the imported CV", ok, "" if ok else log)

    print("LinkedIn's Save to PDF")
    try:
        import typst
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "li.typ"
            src.write_text(LINKEDIN_PDF, encoding="utf-8")
            pdf = typst.compile(str(src))
        r = importer.import_file("Profile.pdf", pdf)
        cv = r["cv"]
        check("the name is found after the sidebar", cv.get("name") == "Alex Moreau", cv.get("name"))
        check("headline and location follow it",
              (cv.get("headline"), cv.get("location")) == ("Staff Engineer at Northwind", "Lyon, France"))
        check("the LinkedIn profile is kept", (cv.get("social_networks") or [{}])[0].get("username")
              == "alex-moreau-dev")
        exp = cv["sections"]["experience"]
        check("employer and title on their own lines, durations dropped",
              [(e["position"], e["company"], e.get("location")) for e in exp] ==
              [("Staff Engineer", "Northwind", "Lyon, France"),
               ("Senior Software Engineer", "Contoso", "Paris, France")],
              repr([(e["position"], e["company"]) for e in exp]))
        check("each description stays with its role",
              [len(e.get("highlights", [])) for e in exp] == [1, 1])
        edu = cv["sections"]["education"][0]
        check("the degree is split from the subject",
              (edu.get("degree"), edu["area"], edu.get("start_date")) ==
              ("Master's degree".replace("'", "’"), "Computer Science", "2012")
              or (edu.get("degree"), edu["area"]) == ("Master's degree", "Computer Science"),
              repr(edu))
        ok, log = renders(cv)
        check("RenderCV renders it", ok, "" if ok else log)
    except ImportError:
        print("  skip  typst is not installed, so the PDF fixture cannot be built")

    print("Refusals")
    for name, data, why in (("notes.txt", b"hello", "an unsupported file"),
                            ("x.zip", linkedin_zip()[:40], "a broken zip"),
                            ("other.zip", _other_zip(), "a zip that is not LinkedIn's")):
        try:
            importer.import_file(name, data)
            check(f"{why} is refused", False)
        except importer.ImportError_ as exc:
            check(f"{why} is refused", True, str(exc))

    for extra in sys.argv[1:]:
        p = Path(extra)
        print(p.name)
        r = importer.import_file(p.name, p.read_bytes())
        for f in r["found"]:
            print(f"        {f['what']}: {f['count']}")
        for n in r["notes"]:
            print(f"        note: {n}")
        ok, log = renders(r["cv"])
        check("RenderCV renders it", ok, "" if ok else log)

    print()
    print(f"{fails} failure(s)" if fails else "every import renders")
    return 1 if fails else 0


def _other_zip() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("readme.txt", "not an archive from LinkedIn")
    return out.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
