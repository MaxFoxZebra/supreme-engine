"""CV Studio's own themes, and its SVG contact icons, the way an ATS reads them.

    python checks/themetest.py

Every theme renders the sample CV with icons on, and the cases that broke
them before: a photo, a CV with nothing but a name, twenty jobs, and a
right-to-left language. A theme marked ATS ✓ must then read in order in
three readers -- pypdf (the order the text was written), pdfminer's layout
analysis, and pdfminer by position alone -- down to each job: its company,
then its bullet points, then the next company. And a pale accent must still
give text that can be read.
"""

from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pypdf  # noqa: E402
from ruamel.yaml import YAML  # noqa: E402

import sample  # noqa: E402
import studio  # noqa: E402
import themes  # noqa: E402
from cv_render import render_file  # noqa: E402

try:
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams
except ImportError:  # pragma: no cover
    extract_text = None

fails = 0
SECTIONS = ["summary", "experience", "education", "skills", "languages"]
# What the Design panel labels ATS ✓; the others say ATS ~.
ATS_SAFE = {"vivid", "swiss", "crisp", "aurora", "editorial", "timeline", "studio"}


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def order(text: str, keys: list[str]) -> list[str]:
    low = text.lower()
    return [k for _, k in sorted((low.find(k.lower()), k) for k in keys if k.lower() in low)]


def readers(pdf: str) -> dict[str, str]:
    out = {"pypdf": "\n".join(p.extract_text() for p in pypdf.PdfReader(pdf).pages)}
    if extract_text:
        out["pdfminer"] = extract_text(pdf)
        out["pdfminer by position"] = extract_text(pdf, laparams=LAParams(boxes_flow=None))
    return out


def render(tmp: Path, name: str, cv: dict, design: dict, locale: dict | None = None):
    doc = tmp / f"{name}.yaml"
    body = {"cv": cv, "design": {"theme": name, **design}}
    if locale:
        body["locale"] = locale
    YAML().dump(body, doc.open("w", encoding="utf-8"))
    return render_file(doc, tmp / f"out-{name}")


def main() -> int:
    base = YAML().load(sample.BASE_CV)["cv"]
    base = copy.deepcopy(dict(base))
    icons = {"header": {"connections": {"show_icons": True}}}
    shown = [t for t in themes.DEFAULTS if t not in themes.HIDDEN]
    check("the themes are offered first, hidden ones left out",
          studio.available_themes()[:len(shown)] == shown
          and not (themes.HIDDEN & set(studio.available_themes())),
          ", ".join(studio.available_themes()[:len(shown) + 1]))

    yellow = "rgb(250, 204, 21)"
    check("a pale accent gets dark text on its band",
          themes.band_ink(themes._rgb((250, 204, 21))) != "rgb(255, 255, 255)")
    check("a pale accent is darkened for titles on white",
          themes._contrast(tuple(int(x) for x in themes.readable((250, 204, 21))[4:-1].split(",")),
                           (255, 255, 255)) >= 3.2)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        try:
            from PIL import Image
            Image.new("RGB", (240, 240), (120, 140, 170)).save(tmp / "photo.jpg")
            photo = True
        except ImportError:  # pragma: no cover
            photo = False
        jobs = [{"company": f"Company {i}", "position": f"Engineer {i}", "start_date": "2010-01",
                 "end_date": "2011-01", "highlights": [f"Shipped project {i} alpha, a long line of text "
                                                        "about what changed and how it was measured.",
                                                        f"Led project {i} beta."]}
                for i in range(20)]
        long_cv = {**base, "sections": {"summary": base["sections"]["summary"], "experience": jobs}}

        for name in themes.DEFAULTS:
            schema = studio.design_schema(name)
            colors = next((g for g in schema["groups"] if g["name"] == "colors"), {"fields": []})
            own = themes.DEFAULTS[name]["colors"]["section_titles"]
            check(f"{name}: Design shows its own defaults",
                  any(f["path"] == ["colors", "section_titles"] and f["default"] == own
                      for f in colors["fields"]))

            r = render(tmp, name, base, icons)
            check(f"{name}: renders", bool(r.get("ok")), "" if r.get("ok") else (r.get("log") or "")[-300:])
            if r.get("ok"):
                texts = readers(r["pdf"])
                check(f"{name}: icons leave no characters in the text",
                      not any(0xE000 <= ord(c) <= 0xF8FF for c in "".join(texts.values())))
                first = [ln for ln in texts["pypdf"].splitlines() if ln.strip()][:1]
                check(f"{name}: the name comes first", bool(first) and base["name"] in first[0], repr(first))
                if name in ATS_SAFE:
                    present = [h for h in SECTIONS if h in texts["pypdf"].lower()]
                    companies = [e["company"] for e in base["sections"]["experience"]]
                    for reader, text in texts.items():
                        check(f"{name}: sections in order ({reader})",
                              order(text, SECTIONS) == present, repr(order(text, SECTIONS)))
                        keys = []
                        for e in base["sections"]["experience"]:
                            keys += [e["company"], e["highlights"][0][:30]]
                        got = order(text, keys)
                        check(f"{name}: each job before its bullets, before the next ({reader})",
                              got == [k for k in keys if k.lower() in text.lower()],
                              repr([g[:14] for g in got]) if got != keys else "")

            if photo:
                r = render(tmp, name, {**base, "photo": str(tmp / "photo.jpg")}, {})
                check(f"{name}: renders with a photo", bool(r.get("ok")),
                      "" if r.get("ok") else (r.get("log") or "")[-200:])

            r = render(tmp, name, {"name": base["name"], "sections": base["sections"]}, {})
            check(f"{name}: renders with only a name above the sections", bool(r.get("ok")))

            r = render(tmp, name, long_cv, {})
            if r.get("ok"):
                pages = pypdf.PdfReader(r["pdf"]).pages
                p1 = len(pages[0].extract_text())
                text = "\n".join(p.extract_text() for p in pages)
                check(f"{name}: twenty jobs flow across pages, page one full",
                      len(pages) >= 2 and p1 > (1200 if name in themes.SIDE_THEMES else 1500) and "Company 19" in text,
                      f"{len(pages)} pages, {p1} characters on page one")
            else:
                check(f"{name}: twenty jobs render", False, (r.get("log") or "")[-200:])

            if name in themes.SIDE_THEMES:
                many = [{"label": f"Skill {i}", "details": "Kubernetes, Terraform, AWS, GCP, Prometheus, "
                         "Grafana and more"} for i in range(45)]
                r = render(tmp, name, {**base, "sections": {**base["sections"], "skills": many}}, {})
                if r.get("ok"):
                    pages = [p.extract_text() for p in pypdf.PdfReader(r["pdf"]).pages]
                    text = "\n".join(pages)
                    check(f"{name}: a column longer than the page carries on in the main one",
                          all(f"Skill {i}:" in text for i in range(45)) and "Skill 0" in pages[0]
                          and "fluent" in text, f"{len(pages)} pages")
                else:
                    check(f"{name}: a long column renders", False, (r.get("log") or "")[-200:])

            r = render(tmp, name, base, {}, {"language": "hebrew"})
            check(f"{name}: renders right to left", bool(r.get("ok")),
                  "" if r.get("ok") else (r.get("log") or "")[-200:])

            if name in ("aurora", "studio"):
                r = render(tmp, name, base, {"colors": {"section_titles": yellow}})
                typ = next(Path(tmp / f"out-{name}").glob("*.typ"), None)
                check(f"{name}: a yellow band gets dark text", bool(r.get("ok")) and typ is not None
                      and "colors-name: rgb(24, 24, 27)," in typ.read_text(encoding="utf-8"))

    print("\nthemes read as they should" if not fails else f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
