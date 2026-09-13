#!/usr/bin/env python3
"""Map the blocks of a rendered page back to the YAML they came from.

RenderCV writes the Typst source it compiled next to the PDF, and that source is
a faithful, ordered transcript of the document: one `= ` for the header, one
`== ` per section in YAML order, and one top-level block per entry. Injecting a
position probe before each of those and asking Typst where they landed gives an
exact page and y for every block, which is what turns the flat PNG in the
preview into something you can click.

The probes cost nothing at layout time. `place` with its default `alignment:
auto` sits at the current flow position without reserving space, and `metadata`
has no visible output, so the probed document renders byte-identically to the
real one -- verified against the compiled PNG, not assumed.

Sections are matched to the YAML by *order*, never by their heading text: the
Typst source carries the printed label ("Professional experience") while the
editor needs the key (`professional_experience`), and a theme is free to change
the former. Entry counts are checked against the YAML, and a section whose count
disagrees keeps only its heading probe -- a page that is clickable at section
granularity is fine, one that maps clicks to the wrong job is not.

Run it directly to see the map for a rendered CV:

    python cv_map.py "~/Documents/CV Studio/assets/my-cv/Your_Name_CV.typ"
"""

from __future__ import annotations

import json
import re
from pathlib import Path

LABEL = "cvsprobe"

# `/ 1pt` divides a length by a length, which yields a plain float -- lengths
# themselves are not JSON-serialisable.
HELPER = (
    f"#let {LABEL}(n) = place(context [#metadata((n: n, p: here().page(), "
    f"x: here().position().x / 1pt, "
    f"y: here().position().y / 1pt))<{LABEL}>])\n"
)

_fonts_cache: dict[str, object] = {}


def _fonts(font_dir: Path | None):
    """Typst's font set, built once per font folder.

    `typst.query` rescans every font folder it is given on each call, and that
    scan -- not the compile -- is the expensive part of a probe pass.
    """
    import rendercv_fonts
    import typst

    key = str(font_dir or "")
    if key not in _fonts_cache:
        paths = list(rendercv_fonts.paths_to_font_folders)
        if font_dir:
            paths.append(font_dir)
        _fonts_cache[key] = typst.Fonts(font_paths=paths)
    return _fonts_cache[key]


def _inject(source: str) -> tuple[str, list[dict]]:
    """Put a probe before every block, and describe what each one marks.

    Only column-0 lines are considered: RenderCV indents everything nested
    inside a call, so a line starting in the first column is either a top-level
    construct or the `)` closing one. That is a far smaller assumption than
    parsing Typst, and a theme that broke it would be caught by the entry-count
    check rather than mapping clicks to the wrong entry.
    """
    out: list[str] = [HELPER]
    probes: list[dict] = []
    depth = 0            # open parens across column-0 lines
    in_block = False     # inside a run of lines forming one block
    section = -1         # index into the YAML's section order
    entry = 0

    def probe(kind: str, **extra) -> None:
        out.append(f"#{LABEL}({len(probes)})")
        probes.append({"kind": kind, **extra})

    for line in source.splitlines():
        stripped = line.strip()
        indented = line[:1].isspace()

        if depth == 0 and not indented:
            if not stripped:
                in_block = False
            elif line.startswith("= "):
                probe("header")
                in_block = True
            elif line.startswith("== "):
                section += 1
                entry = 0
                probe("section", s=section)
                in_block = True
            elif stripped.startswith(("- ", "+ ")):
                # List entries sit flush together with no blank line between
                # them, so each marker starts a block of its own.
                if section >= 0:
                    probe("entry", s=section, i=entry)
                    entry += 1
                in_block = True
            elif not in_block:
                # Anything else at the top level is an entry -- unless we are
                # still above the first heading, where it belongs to the header.
                if section >= 0:
                    probe("entry", s=section, i=entry)
                    entry += 1
                in_block = True

        out.append(line)
        if not indented:
            depth = max(0, depth + line.count("(") - line.count(")"))

    return "\n".join(out) + "\n", probes


def _positions(typ_path: Path, probed: str, font_dir: Path | None) -> list[dict]:
    """Compile the probed copy and ask Typst where every probe ended up."""
    import typst
    from rendercv.renderer.pdf_png import get_package_path

    scratch = typ_path.parent / ".cvstudio-map.typ"
    try:
        scratch.write_text(probed, encoding="utf-8")
        raw = typst.query(
            input=scratch, selector=f"<{LABEL}>", field="value", format="json",
            root=typ_path.parent, font_paths=_fonts(font_dir),
            package_path=get_package_path(),
        )
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass
    return json.loads(raw)


# Typst page sizes, in points, for the sizes RenderCV offers.
PAGE_PT = {"a4": (595.276, 841.89), "us-letter": (612.0, 792.0)}
UNIT_PT = {"cm": 28.3465, "mm": 2.83465, "in": 72.0, "pt": 1.0, "em": 10.0}


def _length(raw: str) -> float | None:
    m = re.fullmatch(r"([0-9.]+)(cm|mm|in|pt|em)", raw.strip())
    return float(m.group(1)) * UNIT_PT[m.group(2)] if m else None


def _text_box(source: str, marks: list[dict]) -> dict | None:
    """The column the text actually occupies, in points.

    Without this a click target spans the whole sheet, margins included, which
    looks like a band across the paper rather than a highlight on the entry.
    The left edge is measured -- every probe reports where the flow began -- and
    only the right edge has to be read off the page setup.
    """
    size = re.search(r'page-size:\s*"([^"]+)"', source)
    right = re.search(r"page-right-margin:\s*([0-9.]+(?:cm|mm|in|pt|em))", source)
    if not size or size.group(1) not in PAGE_PT or not right:
        return None
    width = PAGE_PT[size.group(1)][0]
    margin = _length(right.group(1))
    lefts = [m["x"] for m in marks if isinstance(m.get("x"), (int, float))]
    if margin is None or not lefts:
        return None
    x0, x1 = min(lefts), width - margin
    return {"x0": x0, "x1": x1, "page_width": width} if x1 > x0 else None


def _bands(marks: list[dict]) -> list[dict]:
    """Turn probe points into the strips of page each block owns.

    A block occupies the flow between its own probe and the next one. When those
    two sit on different pages the block gets a band on each page in between:
    that is right whether the block genuinely spans the break or was pushed over
    it whole, which is what makes this need no guess about which happened. The
    leftover whitespace a pushed block leaves behind stays clickable as part of
    it, which is the forgiving answer anyway.

    `y1: None` means "to the bottom of the page" -- the client knows the page
    height from the image it is already showing, so it is never sent one.
    """
    marks.sort(key=lambda m: (m["page"], m["y"]))
    bands: list[dict] = []
    for i, m in enumerate(marks):
        nxt = marks[i + 1] if i + 1 < len(marks) else None
        block = {"k": m["kind"], "name": m.get("name"), "i": m.get("i")}
        # Only the first block of all reaches up into the top margin; every
        # other page's margin already belongs to whatever ran onto it.
        start = 0.0 if i == 0 else m["y"]
        if nxt is None or nxt["page"] == m["page"]:
            bands.append({**block, "page": m["page"], "y0": start,
                          "y1": nxt["y"] if nxt else None})
            continue
        bands.append({**block, "page": m["page"], "y0": start, "y1": None})
        for page in range(m["page"] + 1, nxt["page"]):
            bands.append({**block, "page": page, "y0": 0.0, "y1": None})
        bands.append({**block, "page": nxt["page"], "y0": 0.0, "y1": nxt["y"]})
    return bands


def build_map(typ_path: Path, outline: list[tuple[str, int]],
              font_dir: Path | None = None) -> dict | None:
    """Where every section and entry of `typ_path` landed on the page.

    `outline` is the document's sections as [(yaml key, entry count), ...] in
    YAML order. Returns {"bands": [...], "box": {...}} -- bands as
    {k, name, i, page, y0, y1} with y in points, and box being the column the
    text occupies -- or None if the source could not be matched to the outline
    confidently enough to be worth clicking.
    """
    typ_path = Path(typ_path)
    source = typ_path.read_text(encoding="utf-8")
    probed, probes = _inject(source)
    if not probes:
        return None

    found = sum(1 for p in probes if p["kind"] == "section")
    if found != len(outline):
        return None

    # Keep entry probes only where the count agrees with the YAML. A section
    # that disagrees stays clickable by its heading.
    counted: dict[int, int] = {}
    for p in probes:
        if p["kind"] == "entry":
            counted[p["s"]] = counted.get(p["s"], 0) + 1
    trusted = {s for s, (_, n) in enumerate(outline) if counted.get(s, 0) == n}

    values = _positions(typ_path, probed, font_dir)
    marks = []
    for v in values:
        p = probes[v["n"]]
        if p["kind"] == "entry" and p["s"] not in trusted:
            continue
        mark = {"kind": p["kind"], "page": v["p"], "y": v["y"], "x": v.get("x")}
        if "s" in p:
            mark["name"] = outline[p["s"]][0]
        if "i" in p:
            mark["i"] = p["i"]
        marks.append(mark)

    if not marks:
        return None
    return {"bands": _bands(marks), "box": _text_box(source, marks)}


if __name__ == "__main__":
    import sys

    path = Path(sys.argv[1]).expanduser()
    # Without the YAML to hand, trust whatever the source says it has.
    probed, probes = _inject(path.read_text(encoding="utf-8"))
    sections = [p for p in probes if p["kind"] == "section"]
    counts: dict[int, int] = {}
    for p in probes:
        if p["kind"] == "entry":
            counts[p["s"]] = counts.get(p["s"], 0) + 1
    guess = [(f"section_{i}", counts.get(i, 0)) for i in range(len(sections))]
    result = build_map(path, guess) or {"bands": [], "box": None}
    print("text column:", result["box"])
    for band in result["bands"]:
        print(f'{band["page"]}  {band["y0"]:7.1f} -> '
              f'{"bottom" if band["y1"] is None else format(band["y1"], "7.1f")}  '
              f'{band["k"]:<8} {band["name"] or ""} {band["i"] if band["i"] is not None else ""}')
