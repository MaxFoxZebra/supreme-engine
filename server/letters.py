"""Cover letters: Markdown files laid out by CV Studio itself.

RenderCV is a CV engine -- sections, entries, dates -- and a letter has none of
those, which is why a letter written as a RenderCV document printed as a CV in
disguise. A letter here is a Markdown file with a short header:

    ---
    application: 3f2a...          # the application it belongs to, if any
    company: Northwind
    looks_like: profile/platform-engineer-northwind.yaml
    place: Lyon
    date: today
    subject: Application for Platform Engineer
    language: en
    ---
    Dear Hiring Team,

    Northwind moved its payments platform onto Kubernetes...

    Kind regards,

and it is laid out with Typst, which the app already ships, in the look of the
CV it names: that CV's name, headline and contact line, its fonts, colours,
margins and paper, read every time it renders. The body is the letter as you
would type it in an email; the formatting it prints is bold, italic, links and
bullet lists, and nothing else, so nothing on screen fails to reach the page.
"""

from __future__ import annotations

import datetime as _dt
import html
import io
import re
import zipfile
from pathlib import Path

try:
    from ruamel.yaml import YAML
    _yaml = YAML(typ="rt")
    _yaml.width = 4096
except Exception:  # pragma: no cover - ruamel ships with the app
    _yaml = None

EXT = ".md"
WORD_TARGET = 350

# What a new letter says before it is written, in the languages people apply
# in most. The greeting and closing are the language's own conventions, not a
# translation of the English ones.
PHRASES = {
    "en": ("Application for {role}", "Dear Hiring Team,", "Kind regards,"),
    "fr": ("Candidature au poste de {role}", "Madame, Monsieur,",
           "Je vous prie d'agréer, Madame, Monsieur, l'expression de mes "
           "salutations distinguées."),
    "de": ("Bewerbung als {role}", "Sehr geehrte Damen und Herren,",
           "Mit freundlichen Grüßen"),
    "es": ("Candidatura al puesto de {role}", "Estimado equipo de selección:",
           "Atentamente,"),
    "it": ("Candidatura per la posizione di {role}", "Gentile team di selezione,",
           "Cordiali saluti,"),
    "pt": ("Candidatura para {role}", "Prezada equipa de recrutamento,",
           "Com os melhores cumprimentos,"),
    "nl": ("Sollicitatie naar de functie van {role}", "Geachte heer, mevrouw,",
           "Met vriendelijke groet,"),
}
PROMPTS = {
    "en": ["Open with something only you could write about {company}: a problem "
           "their product implies, or a real connection to your work.",
           "Then your strongest matching evidence, with a number in it, and the "
           "part of the story the CV had no room for.",
           "Close with what you would like to happen next."],
    "fr": ["Commencez par ce que vous seul pouvez dire de {company} : un problème "
           "que leur produit laisse deviner, ou un vrai lien avec votre travail.",
           "Puis votre meilleure preuve, chiffrée, et ce que le CV n'avait pas la "
           "place de raconter.",
           "Terminez par ce que vous aimeriez qu'il se passe ensuite."],
}

# Where the place and date go, per language. {month} is lower case where the
# language writes it so.
DATE_FORMS = {
    "en": "{d} {Month} {y}", "fr": "le {d} {month} {y}", "de": "{d}. {Month} {y}",
    "es": "{d} de {month} de {y}", "it": "{d} {month} {y}",
    "pt": "{d} de {month} de {y}", "nl": "{d} {month} {y}",
}
EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]


def _months(lang: str) -> list[str]:
    if lang == "en":
        return EN_MONTHS
    try:
        import languages
        import rendercv.schema.models.locale as loc
        name = languages.LANGS[lang][0]
        f = Path(loc.__file__).parent / "other_locales" / f"{name}.yaml"
        data = YAML(typ="safe").load(f.read_text(encoding="utf-8"))
        names = data["locale"]["month_names"]
        if len(names) == 12:
            return [str(n) for n in names]
    except Exception:
        pass
    return EN_MONTHS


def date_line(meta: dict, today: _dt.date | None = None) -> str:
    """The place and date as the letter prints them."""
    lang = str(meta.get("language") or "en")
    raw = meta.get("date")
    when = None
    if raw in (None, "", "today"):
        when = today or _dt.date.today()
    elif isinstance(raw, _dt.date):
        when = raw
    else:
        try:
            when = _dt.date.fromisoformat(str(raw))
        except ValueError:
            text = str(raw)  # written out by hand: printed as written
            place = str(meta.get("place") or "").strip()
            return f"{place}, {text}" if place else text
    m = _months(lang)[when.month - 1]
    form = DATE_FORMS.get(lang, DATE_FORMS["en"])
    text = form.format(d=when.day, y=when.year, Month=m[:1].upper() + m[1:],
                       month=m.lower())
    place = str(meta.get("place") or "").strip()
    return f"{place}, {text}" if place else text


# --------------------------------------------------------------------------
# The file
# --------------------------------------------------------------------------

FRONT = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z", re.S)


def parse(text: str) -> tuple[dict, str]:
    """(header, body). A file with no header is all body."""
    m = FRONT.match(text or "")
    if not m:
        return {}, (text or "").strip("\n")
    meta = {}
    try:
        loaded = YAML(typ="safe").load(m.group(1)) or {}
        if isinstance(loaded, dict):
            meta = loaded
    except Exception:
        meta = {}
    return meta, m.group(2).strip("\n")


ORDER = ["application", "company", "looks_like", "to", "place", "date",
         "subject", "language"]


def dump(meta: dict, body: str) -> str:
    """Header then body. Keys in a fixed order so a model's write and the app's
    write of the same letter produce the same file."""
    keys = [k for k in ORDER if meta.get(k) not in (None, "", [])]
    keys += [k for k in meta if k not in ORDER and meta.get(k) not in (None, "", [])]
    buf = io.StringIO()
    y = YAML()
    y.width = 4096
    y.default_flow_style = False
    y.dump({k: meta[k] for k in keys}, buf)
    return "---\n" + buf.getvalue() + "---\n" + (body or "").strip("\n") + "\n"


# --------------------------------------------------------------------------
# The body: the Markdown a letter uses, and nothing more
# --------------------------------------------------------------------------

BULLET = re.compile(r"^\s*[-*•]\s+")
INLINE = re.compile(r"\*\*(.+?)\*\*|__(.+?)__|\*(.+?)\*|_(.+?)_|\[([^\]]+)\]\(([^)\s]+)\)")


def blocks(body: str) -> list[tuple[str, list[str] | str]]:
    """Paragraphs and lists: [("p", text), ("ul", [items])]."""
    out: list = []
    for chunk in re.split(r"\n\s*\n", (body or "").strip()):
        lines = [l for l in chunk.split("\n") if l.strip()]
        if not lines:
            continue
        if all(BULLET.match(l) for l in lines):
            out.append(("ul", [BULLET.sub("", l).strip() for l in lines]))
        else:
            out.append(("p", " ".join(l.strip() for l in lines)))
    return out


def _inline(text: str, bold, em, link, plain):
    pos, parts = 0, []
    for m in INLINE.finditer(text):
        parts.append(plain(text[pos:m.start()]))
        if m.group(1) or m.group(2):
            parts.append(bold(_inline(m.group(1) or m.group(2), bold, em, link, plain)))
        elif m.group(3) or m.group(4):
            parts.append(em(_inline(m.group(3) or m.group(4), bold, em, link, plain)))
        else:
            parts.append(link(_inline(m.group(5), bold, em, link, plain), m.group(6)))
        pos = m.end()
    parts.append(plain(text[pos:]))
    return "".join(parts)


def _typ_escape(s: str) -> str:
    return re.sub(r'([\\#$@*_<>\[\]`~=/"])', r"\\\1", s)


def _typ_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_typst(text: str) -> str:
    return _inline(text, lambda x: f"#strong[{x}]", lambda x: f"#emph[{x}]",
                   lambda x, u: f"#link({_typ_str(u)})[{x}]", _typ_escape)


def to_plain(text: str) -> str:
    return _inline(text, lambda x: x, lambda x: x, lambda x, u: f"{x} ({u})", lambda x: x)


def word_count(body: str) -> int:
    return len(re.findall(r"[\w'’-]+", to_plain(body or "")))


# --------------------------------------------------------------------------
# The look: taken from the CV the letter goes with
# --------------------------------------------------------------------------

def _get(d, *path, default=None):
    for k in path:
        if not isinstance(d, dict) or k not in d:
            return default
        d = d[k]
    return d


def _rgb(v, default="#000000") -> str:
    s = str(v or "").strip()
    m = re.match(r"rgb\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)", s)
    if m:
        return "#%02x%02x%02x" % tuple(int(x) for x in m.groups())
    return s if re.match(r"^#[0-9a-fA-F]{6}$", s) else default


def _font(v, part: str) -> str:
    if isinstance(v, dict):
        return str(v.get(part) or v.get("body") or "Source Sans 3")
    return str(v or "Source Sans 3")


NETWORK_URL = {"linkedin": "linkedin.com/in/{u}", "github": "github.com/{u}",
               "gitlab": "gitlab.com/{u}", "x": "x.com/{u}", "twitter": "x.com/{u}"}


def letterhead(cv_data: dict | None, defaults: dict | None = None) -> dict:
    """Everything the letter takes from its CV, resolved against the CV's
    theme defaults so an unset colour is the theme's colour, not black."""
    cv = (cv_data or {}).get("cv") or {}
    design = (cv_data or {}).get("design") or {}
    dflt = defaults or {}

    def pick(*path, default=None):
        v = _get(design, *path)
        return v if v is not None else dflt.get(".".join(path), default)

    contact = []
    for k in ("location", "email", "phone", "website"):
        if cv.get(k):
            contact.append(str(cv[k]).replace("https://", "").replace("http://", "").rstrip("/"))
    for sn in cv.get("social_networks") or []:
        if isinstance(sn, dict) and sn.get("username"):
            tmpl = NETWORK_URL.get(str(sn.get("network", "")).lower())
            contact.append(tmpl.format(u=sn["username"]) if tmpl else str(sn["username"]))
    fam = pick("typography", "font_family")
    return {
        "name": str(cv.get("name") or "Your Name"),
        "headline": str(cv.get("headline") or ""),
        "contact": contact,
        "font": _font(fam if fam is not None else dflt.get("typography.font_family.body"), "body"),
        "name_font": _font(fam if fam is not None else dflt.get("typography.font_family.name"), "name"),
        "name_bold": bool(pick("typography", "bold", "name", default=False)),
        "name_color": _rgb(pick("colors", "name"), "#000000"),
        "headline_color": _rgb(pick("colors", "headline"), "#555555"),
        "contact_color": _rgb(pick("colors", "connections"), "#555555"),
        "rule_color": _rgb(pick("colors", "section_titles"), "#000000"),
        "link_color": _rgb(pick("colors", "links"), "#000000"),
        "body_color": _rgb(pick("colors", "body"), "#000000"),
        "paper": str(pick("page", "size", default="a4")),
        "margins": {k: str(pick("page", f"{k}_margin", default="2cm"))
                    for k in ("top", "bottom", "left", "right")},
    }


# --------------------------------------------------------------------------
# Laying it out
# --------------------------------------------------------------------------

def typst_source(meta: dict, body: str, head: dict) -> str:
    m = head["margins"]
    contact = " #h(0.8em)#text(fill: grey)[•]#h(0.8em) ".join(
        _typ_escape(c) for c in head["contact"])
    to = meta.get("to")
    to_lines = to if isinstance(to, list) else [l for l in str(to or "").split("\n") if l.strip()]
    parts = []
    for kind, content in blocks(body):
        if kind == "ul":
            parts.append("\n".join("- " + to_typst(i) for i in content))
        else:
            parts.append(to_typst(content))
    lang = str(meta.get("language") or "en")
    return f'''#let accent = rgb("{head["name_color"]}")
#let grey = rgb("{head["contact_color"]}")
#set document(title: {_typ_str(str(meta.get("subject") or "Cover letter"))}, author: {_typ_str(head["name"])})
#set page(paper: {_typ_str(head["paper"])}, margin: (top: {m["top"]}, bottom: {m["bottom"]}, left: {m["left"]}, right: {m["right"]}))
#set text(font: {_typ_str(head["font"])}, size: 10.5pt, lang: {_typ_str(lang)}, fill: rgb("{head["body_color"]}"))
#set par(justify: true, leading: 0.72em, spacing: 1.15em)
#set list(indent: 0.6em, spacing: 0.7em, marker: [•])
#show link: it => underline(offset: 2pt, stroke: 0.5pt + rgb("{head["link_color"]}"), it)
#text(font: {_typ_str(head["name_font"])}, size: 24pt, weight: {'"bold"' if head["name_bold"] else '"regular"'}, fill: accent)[{_typ_escape(head["name"])}]
{'#linebreak()#v(-0.35em)#text(size: 11pt, fill: rgb("' + head["headline_color"] + '"))[' + _typ_escape(head["headline"]) + ']' if head["headline"] else ''}
#v(0.3em)
#text(size: 9.5pt, fill: grey)[{contact}]
#v(0.4em)
#line(length: 100%, stroke: 0.6pt + rgb("{head["rule_color"]}"))
#v(1.4em)
{("#block[" + "#linebreak()".join(_typ_escape(str(l)) for l in to_lines) + "]\n#v(0.6em)") if to_lines else ""}
#align(right)[#text(fill: grey)[{_typ_escape(date_line(meta))}]]
#v(1.6em)
{("#text(weight: \"bold\")[" + _typ_escape(str(meta.get("subject"))) + "]\n#v(0.9em)") if meta.get("subject") else ""}
{chr(10).join(p + chr(10) for p in parts)}
#v(2.2em)
#text(weight: "bold")[{_typ_escape(head["name"])}]
'''


def _font_paths(extra: list[Path]) -> list[str]:
    paths = []
    try:
        import rendercv_fonts
        paths.append(str(Path(rendercv_fonts.__file__).parent))
    except Exception:
        pass
    import cjkfonts
    if cjkfonts.cache_dir().is_dir():
        paths.append(str(cjkfonts.cache_dir()))
    return paths + [str(p) for p in extra if p.is_dir()]


def render(meta: dict, body: str, head: dict, out: Path, stem: str,
           font_dirs: list[Path] | None = None) -> dict:
    """Write <stem>.pdf and <stem>_N.png into `out`. Returns
    {ok, pdf, png_pages, pages, words} or {ok: False, log}."""
    import typst
    out.mkdir(parents=True, exist_ok=True)
    for old in list(out.glob("*.pdf")) + list(out.glob("*.png")) + list(out.glob("*.typ")):
        try:
            old.unlink()
        except OSError:
            pass
    src = out / f"{stem}.typ"
    src.write_text(typst_source(meta, body, head), encoding="utf-8")
    fonts = _font_paths(font_dirs or [])
    try:
        pdf = out / f"{stem}.pdf"
        typst.compile(str(src), output=str(pdf), font_paths=fonts)
        typst.compile(str(src), output=str(out / f"{stem}_{{p}}.png"), format="png",
                      ppi=150, font_paths=fonts)
    except Exception as exc:
        return {"ok": False, "log": str(exc)}
    pngs = sorted(out.glob(f"{stem}_*.png"),
                  key=lambda f: int(re.sub(r"\D", "", f.stem.rsplit("_", 1)[-1]) or 0))
    return {"ok": True, "pdf": str(pdf), "png_pages": [str(p) for p in pngs],
            "pages": len(pngs), "words": word_count(body)}


def file_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_") + "_Cover_Letter"


# --------------------------------------------------------------------------
# Other ways out: Word, and plain text for a form's box
# --------------------------------------------------------------------------

def plain_text(meta: dict, body: str, head: dict) -> str:
    out = []
    for kind, content in blocks(body):
        out.append("\n".join("• " + to_plain(i) for i in content) if kind == "ul"
                   else to_plain(content))
    return "\n\n".join(out + [head["name"]]) + "\n"


def _w_runs(text: str) -> str:
    """Inline Markdown as WordprocessingML runs."""
    runs = []

    def emit(t, b=False, i=False, u=False):
        if not t:
            return
        props = ("<w:b/>" if b else "") + ("<w:i/>" if i else "") + ('<w:u w:val="single"/>' if u else "")
        runs.append(f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}'
                    f'<w:t xml:space="preserve">{html.escape(t, quote=False)}</w:t></w:r>')

    pos = 0
    for m in INLINE.finditer(text):
        emit(text[pos:m.start()])
        if m.group(1) or m.group(2):
            emit(to_plain(m.group(1) or m.group(2)), b=True)
        elif m.group(3) or m.group(4):
            emit(to_plain(m.group(3) or m.group(4)), i=True)
        else:
            emit(to_plain(m.group(5)), u=True)
        pos = m.end()
    emit(text[pos:])
    return "".join(runs)


def docx(meta: dict, body: str, head: dict) -> bytes:
    """A plain .docx: the same parts in the same order, in the CV's font. Built
    by hand rather than with a library, because a letter needs six paragraph
    kinds and the app should not ship a dependency for them."""
    font = html.escape(head["font"])

    def para(runs, *, size=None, bold=False, align=None, space_after=160, indent=None, color=None):
        ppr = (f'<w:jc w:val="{align}"/>' if align else "") + \
              f'<w:spacing w:after="{space_after}"/>' + \
              (f'<w:ind w:left="{indent}" w:hanging="240"/>' if indent else "")
        rpr = (f'<w:sz w:val="{size}"/>' if size else "") + ("<w:b/>" if bold else "") + \
              (f'<w:color w:val="{color.lstrip("#")}"/>' if color else "")
        if rpr:
            runs = runs.replace("<w:r>", f"<w:r><w:rPr>{rpr}</w:rPr>", ).replace(
                "<w:rPr>" + rpr + "</w:rPr><w:rPr>", "<w:rPr>" + rpr)
        return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>"

    def t(s):
        return f'<w:r><w:t xml:space="preserve">{html.escape(s, quote=False)}</w:t></w:r>'

    ps = [para(t(head["name"]), size=44, bold=head["name_bold"] or True, space_after=0,
               color=head["name_color"])]
    if head["headline"]:
        ps.append(para(t(head["headline"]), size=22, space_after=60, color=head["headline_color"]))
    ps.append(para(t("   •   ".join(head["contact"])), size=18, space_after=360,
                   color=head["contact_color"]))
    to = meta.get("to")
    for line in (to if isinstance(to, list) else [l for l in str(to or "").split("\n") if l.strip()]):
        ps.append(para(t(str(line)), space_after=0))
    ps.append(para(t(date_line(meta)), align="right", space_after=360, color=head["contact_color"]))
    if meta.get("subject"):
        ps.append(para(t(str(meta["subject"])), bold=True, space_after=240))
    for kind, content in blocks(body):
        if kind == "ul":
            for item in content:
                ps.append(para(t("•\t") + _w_runs(item), indent=360, space_after=60))
        else:
            ps.append(para(_w_runs(content)))
    ps.append(para(t(head["name"]), bold=True, space_after=0).replace("<w:spacing", '<w:spacing w:before="480"', 1))
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(ps) +
        '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1134" w:right="1247" w:bottom="1134" w:left="1247"/></w:sectPr>'
        "</w:body></w:document>")
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}" '
        f'w:cs="{font}"/><w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults></w:styles>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


# --------------------------------------------------------------------------
# New letters, and letters from before
# --------------------------------------------------------------------------

def _elide(text: str, lang: str) -> str:
    """French and Italian drop the vowel before a vowel: « poste d'ingénieur »."""
    if lang in ("fr", "it"):
        return re.sub(r"\b(de|le|la) ([aeiouyhàâéèêëîïôûAEIOUYHÀÂÉÈÊËÎÏÔÛ])",
                      lambda m: m.group(1)[0] + "'" + m.group(2), text)
    return text


def scaffold(*, company: str = "", role: str = "", lang: str = "en", place: str = "",
             looks_like: str | None = None, application: str | None = None) -> str:
    subj, greet, close = PHRASES.get(lang, PHRASES["en"])
    prompts = PROMPTS.get(lang, PROMPTS["en"])
    meta = {"application": application, "company": company or None,
            "looks_like": looks_like, "place": place or None, "date": "today",
            "subject": _elide(subj.format(role=role), lang) if role else None,
            "language": lang}
    body = "\n\n".join([greet] + [p.format(company=company or "the company")
                                  for p in prompts] + [close])
    return dump(meta, body)


def from_legacy(data: dict) -> tuple[dict, str]:
    """A letter written as a RenderCV document -- one section whose title is the
    subject and whose entries are the paragraphs -- as a header and a body."""
    cv = (data or {}).get("cv") or {}
    secs = cv.get("sections") or {}
    subject, entries = "", []
    if isinstance(secs, dict) and secs:
        subject, entries = next(iter(secs.items()))
        subject = re.sub(r"^\s*re\s*:?\s+", "", str(subject), flags=re.I)
    paras = [str(e).strip() for e in (entries or []) if isinstance(e, (str, int, float))]
    # The signature is printed from the CV now, so a last line that is just
    # the name goes.
    if paras and paras[-1].strip() == str(cv.get("name") or "").strip():
        paras.pop()
    import languages
    lang = languages.code_of(((data or {}).get("locale") or {}).get("language"))
    loc = str(cv.get("location") or "")
    if loc.strip().lower() in ("city, country", "city"):  # the starter's placeholder
        loc = ""
    meta = {"place": loc.split(",")[0].strip() or None, "date": "today",
            "subject": subject or None, "language": lang}
    return meta, "\n\n".join(paras)
