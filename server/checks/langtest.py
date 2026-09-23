"""Translate a CV the way the app and an AI client do, and check every step.

    python checks/langtest.py

Works in a throwaway workspace: writes a CV, adds French to it, changes the
English one and checks the French one says what it is missing, changes the
design and checks both print alike, and renders the French one through
RenderCV, since a translation RenderCV refuses is worth nothing.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import languages  # noqa: E402
import studio  # noqa: E402
from cv_render import render_file  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


CV = """cv:
  name: Alex Moreau
  headline: Staff Engineer
  email: alex@example.com
  sections:
    summary:
      - I build infrastructure other engineers do not have to think about.
    experience:
      - company: Northwind
        position: Staff Engineer
        start_date: 2021-01
        end_date: present
        highlights:
          - Led the migration of 140 services to Kubernetes.
    Side quests:
      - label: Chess
        details: Club champion
design:
  theme: engineeringclassic
"""


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        studio.WORKSPACE = Path(tmp)
        (Path(tmp) / "profile").mkdir()
        src = Path(tmp) / "profile" / "my-cv.yaml"
        src.write_text(CV, encoding="utf-8")

        print("Adding French")
        r = studio.add_language(src, "fr")
        fr = studio.safe_path(r["path"])
        check("written beside the source as my-cv.fr.yaml", r["path"] == "profile/my-cv.fr.yaml")
        text = fr.read_text(encoding="utf-8")
        check("the locale says French", languages.file_language(text) == "fr")
        check("an ongoing date reads aujourd'hui", "present: aujourd'hui" in text)
        check("known section titles are translated",
              r["sections_titled"] == {"summary": "Résumé",
                                       "experience": "Expérience professionnelle"},
              repr(r["sections_titled"]))
        check("an unknown one is left for the translator",
              r["sections_to_title"] == ["Side quests"])
        check("contact details and dates are untouched",
              "alex@example.com" in text and "2021-01" in text)
        check("the document list knows its language and source",
              any(d["path"] == r["path"] and d["lang"] == "fr"
                  and d["translation_of"] == "profile/my-cv.yaml"
                  for d in studio.list_documents()))
        check("a second French version is refused",
              _raises(lambda: studio.add_language(src, "fr")))
        check("translating the translation translates the source",
              studio.add_language(fr, "de")["of"] == "profile/my-cv.yaml")
        fam = studio.language_family(fr)
        check("the family is English, French and German",
              sorted(m["lang"] for m in fam["members"]) == ["de", "en", "fr"])

        print("Keeping it in step")
        check("nothing is missing yet", studio.translation_drift(fr)["changes"] == [])
        studio.apply_patches(src, [{"path": ["cv", "sections", "experience", 0,
                                             "highlights", 0],
                                    "value": "Led the migration of 140 services "
                                             "to Kubernetes with no downtime."}])
        d = studio.translation_drift(fr)["changes"]
        check("the English change is listed", len(d) == 1, repr(d))
        check("at the French path", d and d[0]["key"] ==
              "cv.sections.Expérience professionnelle.0.highlights.0", d and d[0]["key"])
        studio.mark_translation_current(fr)
        check("marking it current clears the list",
              studio.translation_drift(fr)["changes"] == [])

        print("One design")
        studio.apply_patches(src, [{"path": ["design", "theme"], "value": "sb2nov"}])
        check("a theme change on the source reaches the translation",
              "theme: sb2nov" in fr.read_text(encoding="utf-8"))
        check("and leaves its locale alone",
              languages.file_language(fr.read_text(encoding="utf-8")) == "fr")

        print("Rendering")
        out = render_file(fr, Path(tmp) / "out")
        check("RenderCV renders the French CV", bool(out.get("ok")),
              "" if out.get("ok") else (out.get("log") or "")[-300:])

    print("Postings")
    check("a French posting reads as French", languages.detect(
        "Nous recherchons un ingénieur plateforme pour rejoindre notre équipe à Paris. "
        "Vous serez responsable de la fiabilité des services et de la chaîne de "
        "déploiement.") == "fr")
    check("an English one as English", languages.detect(
        "We are looking for a platform engineer to join our team in London. You will "
        "own the reliability of our services and the deployment pipeline.") == "en")
    check("a list of tools is left alone", languages.detect("Kubernetes Terraform AWS") is None)

    print()
    print(f"{fails} failure(s)" if fails else "every language check passes")
    return 1 if fails else 0


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
