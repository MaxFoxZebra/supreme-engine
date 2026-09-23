"""Cover letters, end to end, in a throwaway workspace.

    python checks/lettertest.py

Converts a letter written the old way (as a RenderCV document), writes a new
one for an application, renders both through Typst, and exports each as PDF,
Word and plain text. Text a person would plausibly type -- a C# job, a $ sign,
an email address, an underscore -- has to reach the page as typed, because
every one of those is markup to Typst.
"""

from __future__ import annotations

import datetime as dt
import io
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import letters  # noqa: E402
import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


CV = """cv:
  name: Alex Moreau
  headline: Staff Engineer
  location: Lyon, France
  email: alex@example.com
  social_networks:
    - network: LinkedIn
      username: alex-moreau
  sections:
    summary:
      - I build infrastructure.
design:
  theme: classic
  page:
    size: a4
"""

LEGACY = """cv:
  name: Alex Moreau
  sections:
    Re Platform Engineer at Northwind:
      - Dear Hiring Team,
      - I would like to apply.
      - Kind regards,
      - Alex Moreau
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        studio.WORKSPACE = ws
        (ws / "profile").mkdir()
        (ws / "letters").mkdir()
        (ws / "profile" / "my-cv.yaml").write_text(CV, encoding="utf-8")
        (ws / "letters" / "old.yaml").write_text(LEGACY, encoding="utf-8")
        studio.set_base_cv("profile/my-cv.yaml")

        print("A letter from before")
        done = studio.convert_legacy_letters()
        check("converted to Markdown", done == ["letters/old.md"], repr(done))
        check("the old file is kept beside it", (ws / "letters" / "old.yaml.bak").exists())
        meta, body = letters.parse((ws / "letters" / "old.md").read_text(encoding="utf-8"))
        check("the section title became the subject",
              meta.get("subject") == "Platform Engineer at Northwind", repr(meta.get("subject")))
        check("the name line went, the rest is the body",
              body == "Dear Hiring Team,\n\nI would like to apply.\n\nKind regards,", repr(body))
        check("it looks like the base CV", meta.get("looks_like") == "profile/my-cv.yaml")
        check("listed as a letter", any(d["path"] == "letters/old.md" and d.get("letter")
                                        for d in studio.list_documents()))

        print("Rendering")
        p = ws / "letters" / "old.md"
        p.write_text(letters.dump(meta, body + "\n\n- C# and **Go**, $120k, a_b@example.com #1\n"
                                  "- [a link](https://example.com)"), encoding="utf-8")
        r = studio.render_letter(p)
        check("Typst lays it out", r.get("ok"), r.get("error") or "")
        check("one page, a PNG of it", r.get("pages") == 1 and len(r.get("pngs") or []) == 1)
        try:
            import pypdf
            text = "\n".join(pg.extract_text() for pg in pypdf.PdfReader(ws / r["pdf"]).pages)
            for want in ("Alex Moreau", "Staff Engineer", "linkedin.com/in/alex-moreau",
                         "C#", "$120k", "a_b@example.com", "#1", "Platform Engineer at Northwind"):
                check(f"prints {want!r}", want in text)
            check("the markup itself does not print", "**" not in text and "](" not in text)
        except ImportError:
            print("  skip  pypdf is not installed")
        check("the thumbnail finds it", studio.thumb(p).get("png") is not None)

        print("Exports")
        data, ctype, name = studio.letter_export(p, "docx")
        z = zipfile.ZipFile(io.BytesIO(data))
        doc = z.read("word/document.xml").decode("utf-8")
        check("a .docx with the letter in it", "I would like to apply." in doc and name.endswith(".docx"))
        check("bold survives into Word", "<w:b/>" in doc)
        data, _, _ = studio.letter_export(p, "txt")
        txt = data.decode("utf-8")
        check("plain text, markup gone", "Go" in txt and "**" not in txt and txt.startswith("Dear"))
        data, ctype, _ = studio.letter_export(p, "pdf")
        check("the PDF", data[:4] == b"%PDF" and ctype == "application/pdf")

        print("Write one, for an application")
        if studio.jobstore is not None:
            job = studio.jobstore.add_job(ws, {
                "company": "Doctolib", "title": "Ingénieur plateforme", "status": "pending",
                "description": "Nous recherchons un ingénieur plateforme pour rejoindre notre "
                               "équipe à Paris. Vous serez responsable de la fiabilité des "
                               "services et de la chaîne de déploiement."})
            r = studio.new_letter(job["id"])
            meta, body = letters.parse(studio.safe_path(r["path"]).read_text(encoding="utf-8"))
            check("named after the company", r["path"] == "letters/cover-doctolib.md", r["path"])
            check("in the posting's language", meta.get("language") == "fr")
            check("with its subject and greeting", meta.get("subject") ==
                  "Candidature au poste d'Ingénieur plateforme" and body.startswith("Madame, Monsieur,"))
            check("written from the CV's town", meta.get("place") == "Lyon")
            linked = next(j for j in studio.jobstore.list_jobs(ws) if j["id"] == job["id"])
            check("linked to the application", linked.get("letter_path") == r["path"])
            check("it renders", studio.render_letter(studio.safe_path(r["path"])).get("ok"))

    print("Dates")
    day = dt.date(2026, 9, 23)
    for lang, want in (("en", "Lyon, 23 September 2026"), ("fr", "Lyon, le 23 septembre 2026"),
                       ("de", "Lyon, 23. September 2026"), ("es", "Lyon, 23 de septiembre de 2026")):
        got = letters.date_line({"place": "Lyon", "date": "today", "language": lang}, day)
        check(f"{lang}: {want}", got == want, got)
    check("a date written by hand prints as written",
          letters.date_line({"place": "Lyon", "date": "end of September"}) == "Lyon, end of September")

    print()
    print(f"{fails} failure(s)" if fails else "every letter check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
