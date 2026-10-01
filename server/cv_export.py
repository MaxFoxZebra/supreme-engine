"""A CV as a Word file or as plain text, from its YAML.

The PDF is the CV: RenderCV lays it out and that is what to attach. These are
for the two places a PDF will not go. Recruitment agencies ask for Word, to put
their own header on it; application forms want text to paste, section by
section. Neither tries to look like the PDF. Both carry every word of it, in
its order, with the name, the contact line, and the section titles as
headings, so what a recruiter or a form receives is the same CV.

Built from the CV's own data rather than from the PDF's text layer, which
loses the structure. The Word file reuses the letter's WordprocessingML
helpers, so the app still ships no library for it.
"""

from __future__ import annotations

import re

import letters

ENTRY_TITLE = ("company", "institution", "name", "title", "label")
ENTRY_SUB = ("position", "degree", "area", "journal")


def _section_title(key: str) -> str:
    """What RenderCV prints for a section key: underscores as spaces, and a
    key written in lower case capitalised word by word."""
    text = str(key).replace("_", " ")
    return text.title() if text == text.lower() else text


def _date(value, months: list[str], present: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() == "present":
        return present
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if m:
        return f"{months[int(m.group(2)) - 1][:3]} {m.group(1)}"
    return text


def _language(data: dict) -> tuple[list[str], str]:
    import languages
    code = languages.code_of(((data or {}).get("locale") or {}).get("language"))
    months = letters._months(code)
    present = ((data or {}).get("locale") or {}).get("present") or languages.PRESENT.get(code) or "present"
    return months, str(present)


def _when(e: dict, months, present) -> str:
    if e.get("date"):
        return _date(e["date"], months, present)
    start, end = _date(e.get("start_date"), months, present), _date(e.get("end_date"), months, present)
    return f"{start} – {end}" if start and end else start or end


def entries(data: dict) -> list[tuple[str, list[dict]]]:
    """Each section as (title, entries), every entry flattened to
    {title, sub, meta, text, bullets} so both formats read one shape."""
    cv = (data or {}).get("cv") or {}
    months, present = _language(data)
    out = []
    for key, items in (cv.get("sections") or {}).items():
        flat = []
        for e in items or []:
            if not isinstance(e, dict):
                flat.append({"text": str(e)})
                continue
            if "bullet" in e and len(e) == 1:
                flat.append({"bullets": [str(e["bullet"])]})
                continue
            if "label" in e and "details" in e:
                flat.append({"text": f"**{e['label']}**: {e['details']}"})
                continue
            title = next((str(e[k]) for k in ENTRY_TITLE if e.get(k)), "")
            sub = ", ".join(str(e[k]) for k in ENTRY_SUB if e.get(k))
            meta = " · ".join(x for x in (str(e.get("location") or ""), _when(e, months, present)) if x)
            if e.get("authors"):
                sub = ", ".join(map(str, e["authors"])) + (f". {sub}" if sub else "")
            flat.append({"title": title, "sub": sub, "meta": meta,
                         "text": str(e.get("summary") or ""),
                         "bullets": [str(h) for h in e.get("highlights") or []]})
        out.append((_section_title(key), flat))
    return out


def plain_text(data: dict, head: dict) -> str:
    """The CV as text to paste: headings in capitals, bullets as dashes."""
    strip = lambda s: re.sub(r"\*\*|__|(?<!\w)[*_](?!\s)|(?<!\s)[*_](?!\w)", "", s)
    lines = [head["name"]]
    if head.get("headline"):
        lines.append(head["headline"])
    if head.get("contact"):
        lines.append(" | ".join(head["contact"]))
    for title, items in entries(data):
        lines += ["", title.upper()]
        for e in items:
            top = " — ".join(x for x in (e.get("title"), e.get("sub")) if x)
            if top:
                lines.append("")
                lines.append(top + (f" ({e['meta']})" if e.get("meta") else ""))
            elif e.get("meta"):
                lines.append(e["meta"])
            if e.get("text"):
                lines.append(strip(e["text"]))
            lines += [f"- {strip(b)}" for b in e.get("bullets") or []]
    return "\n".join(lines).strip() + "\n"


def docx(data: dict, head: dict) -> bytes:
    """The CV as a plain Word document in its own font and colours."""
    R, P = letters._w_runs, letters._wp
    ps = [P(R(head["name"], bold=True, size=40, color=head["name_color"]), after=0)]
    if head.get("headline"):
        ps.append(P(R(head["headline"], size=22, color=head["headline_color"]), after=60))
    if head.get("contact"):
        ps.append(P(R("   •   ".join(head["contact"]), size=18, color=head["contact_color"]), after=240))
    for title, items in entries(data):
        ps.append(P(R(title, bold=True, size=24, color=head["rule_color"]), before=240, after=100,
                    bottom_border=head["rule_color"], keep_next=True))
        for e in items:
            top = R(e["title"], bold=True) if e.get("title") else ""
            if e.get("sub"):
                top += R(("  —  " if top else "") + e["sub"])
            if top:
                ps.append(P(top, after=0 if e.get("meta") or e.get("text") or e.get("bullets") else 120,
                            keep_next=True, before=80))
            if e.get("meta"):
                ps.append(P(R(e["meta"], size=18, color=head["contact_color"]), after=40, keep_next=True))
            if e.get("text"):
                ps.append(P(R(e["text"]), after=80))
            for b in e.get("bullets") or []:
                ps.append(P('<w:r><w:t xml:space="preserve">•\t</w:t></w:r>' + R(b),
                            indent=360, hanging=300, after=40))
    return letters.package_docx(ps, head["font"])
