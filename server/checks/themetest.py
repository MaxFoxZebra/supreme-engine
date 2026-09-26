"""CV Studio's own themes, and its SVG contact icons, the way an ATS reads them.

    python checks/themetest.py

Every theme renders the sample CV. The text is then read two ways: in the
order it was written (pypdf, like most parsers) and by position on the page
(pdfminer, like some). A theme marked ATS-safe has to give its sections in
order both ways; and with icons on, no theme may put an icon character in
the text.
"""

from __future__ import annotations

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
ATS_SAFE = {"studio", "ledger"}


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def order(text: str) -> list[str]:
    low = text.lower()
    return [h for _, h in sorted((low.find(h), h) for h in SECTIONS if h in low)]


def main() -> int:
    y = YAML()
    cv = y.load(sample.BASE_CV)["cv"]
    check("the themes are offered first", studio.available_themes()[:3] == list(themes.DEFAULTS),
          ", ".join(studio.available_themes()[:4]))
    for name in themes.DEFAULTS:
        schema = studio.design_schema(name)
        colors = next((g for g in schema["groups"] if g["name"] == "colors"), {"fields": []})
        own = themes.DEFAULTS[name]["colors"]["section_titles"]
        check(f"{name}: Design shows its own defaults",
              any(f["path"] == ["colors", "section_titles"] and f["default"] == own
                  for f in colors["fields"]))
        with tempfile.TemporaryDirectory() as tmp:
            doc = Path(tmp) / "cv.yaml"
            y.dump({"cv": cv, "design": {"theme": name,
                                        "header": {"connections": {"show_icons": True}}}},
                   doc.open("w", encoding="utf-8"))
            r = render_file(doc, Path(tmp) / "out")
            check(f"{name}: renders", bool(r.get("ok")), "" if r.get("ok") else (r.get("log") or "")[-300:])
            if not r.get("ok"):
                continue
            text = "\n".join(p.extract_text() for p in pypdf.PdfReader(r["pdf"]).pages)
            check(f"{name}: icons leave no characters in the text",
                  not any(0xE000 <= ord(c) <= 0xF8FF for c in text))
            first = [ln for ln in text.splitlines() if ln.strip()][:1]
            check(f"{name}: the name comes first", bool(first) and cv["name"] in first[0], repr(first))
            if name in ATS_SAFE:
                present = [h for h in SECTIONS if h in text.lower()]
                check(f"{name}: sections in order, as written", order(text) == present, repr(order(text)))
                if extract_text:
                    pos = extract_text(r["pdf"], laparams=LAParams(boxes_flow=None))
                    check(f"{name}: sections in order, by position", order(pos) == present,
                          repr(order(pos)))
    print("\nthemes read as they should" if not fails else f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
