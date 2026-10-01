"""Themes of your own: saved from a CV, read from the workspace, rendered on
the theme they are based on, with their own templates, and refused clearly
when they cannot be used.

    python checks/customthemetest.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import studio  # noqa: E402
import themes  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


def text_of(r: dict) -> str:
    import pypdf
    return "".join(p.extract_text() for p in pypdf.PdfReader(str(studio.WORKSPACE / r["pdf"])).pages)


def main() -> int:
    studio.open_sample()
    ws = studio.WORKSPACE
    cv = next(d["path"] for d in studio.list_documents() if d["path"].startswith("profile/"))
    p = studio.safe_path(cv)
    original = p.read_text(encoding="utf-8")

    def use(theme: str) -> dict:
        p.write_text(re.sub(r"(\n  theme: )\S+", r"\g<1>" + theme, original, count=1), encoding="utf-8")
        return studio.render(p)

    use("sidebar")
    studio.apply_patches(p, [{"path": ["design", "colors", "section_titles"], "value": "rgb(200, 60, 20)"}])
    design = studio.to_plain(studio.yaml_rt.load(p.read_text(encoding="utf-8")))["design"]
    saved = themes.save_custom(ws, "teal_side", "Teal sidebar", design["theme"], design)
    body = (ws / "themes" / "teal_side" / "theme.yaml").read_text(encoding="utf-8")
    check("a CV's design saves as a theme, based on its theme", "based_on: sidebar" in body
          and "rgb(200, 60, 20)" in body, body.splitlines()[2:5])
    check("only what differs from the base is written", "font_family" not in body, len(body.splitlines()))
    check("it is offered first, under its label", studio.available_themes()[0] == "teal_side"
          and themes.custom_list()[0]["label"] == "Teal sidebar")

    r = use("teal_side")
    check("it renders", r.get("ok"), (r.get("hint") or r.get("error") or "")[-300:])
    check("and keeps its base's sidebar", themes.base_of("teal_side") == "sidebar"
          and "sidebar_sections" in themes.theme_classes()["teal_side"].model_fields)

    (ws / "themes" / "plain_mine").mkdir(parents=True)
    (ws / "themes" / "plain_mine" / "theme.yaml").write_text(
        "label: Plain\nbased_on: classic\ndesign:\n  typography:\n    font_family:\n      body: XCharter\n",
        encoding="utf-8")
    r = use("plain_mine")
    check("a theme written by hand on classic renders", r.get("ok"), (r.get("hint") or "")[:120])

    # The starter CV is on one of RenderCV's own themes: its look has to save too.
    use("engineeringclassic")
    studio.apply_patches(p, [{"path": ["design", "colors", "name"], "value": "rgb(120, 20, 90)"}])
    design = studio.to_plain(studio.yaml_rt.load(p.read_text(encoding="utf-8")))["design"]
    themes.save_custom(ws, "plum_eng", "", design["theme"], design)
    body = (ws / "themes" / "plum_eng" / "theme.yaml").read_text(encoding="utf-8")
    check("a CV on one of RenderCV's themes saves as a theme too", "based_on: engineeringclassic" in body
          and "rgb(120, 20, 90)" in body and "section_titles" not in body, body.splitlines()[2:6])
    r = use("plum_eng")
    check("and renders", r.get("ok"), (r.get("hint") or "")[:120])

    stamped = ws / "themes" / "stamped"
    stamped.mkdir()
    (stamped / "theme.yaml").write_text("based_on: crisp\n", encoding="utf-8")
    (stamped / "SectionBeginning.j2.typ").write_text(
        "#text(fill: red)[STAMPED]\n" + (HERE / "themes" / "crisp" / "SectionBeginning.j2.typ").read_text(),
        encoding="utf-8")
    r = use("stamped")
    check("its own template replaces the base's", r.get("ok") and "STAMPED" in text_of(r))

    bad = ws / "themes" / "classic"
    bad.mkdir()
    (bad / "theme.yaml").write_text("based_on: crisp\n", encoding="utf-8")
    worse = ws / "themes" / "wrongbase"
    worse.mkdir()
    (worse / "theme.yaml").write_text("based_on: teal_side\n", encoding="utf-8")
    themes.refresh(ws)
    errs = {c["name"]: c["error"] for c in themes.custom_list() if c["error"]}
    check("a theme named like a shipped one is refused, saying so", "already a theme" in (errs.get("classic") or ""))
    check("so is one based on a theme it cannot extend", "based_on" in (errs.get("wrongbase") or ""))
    check("and neither is offered", "wrongbase" not in studio.available_themes())

    import shutil
    shutil.rmtree(ws / "themes" / "plain_mine")
    check("a theme folder deleted is forgotten", "plain_mine" not in studio.available_themes())
    for bad_name in ("Crisp", "x", "vivid"):
        try:
            themes.save_custom(ws, bad_name, "", "crisp", {})
            ok = bad_name == "Crisp"     # folded to crisp, which is taken
        except ValueError:
            ok = True
        check(f"saving as {bad_name!r} is refused", ok)

    print(f"\n{fails} failure(s)" if fails else "\nevery custom theme check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
