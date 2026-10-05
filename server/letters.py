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
would type it in an email, in Markdown: headings, lists, quotes, rules, code,
tables, bold, italic, strikethrough and links all print, and the page, the PDF,
Word and plain text are written from one parse, so nothing on screen fails to
reach the page.
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


ORDER = ["application", "company", "looks_like", "letterhead", "to", "place", "date",
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

# A letter is Markdown, all of it that a letter can use: headings (three
# levels; a fourth and deeper read as the third), paragraphs, hard line
# breaks, bullet and numbered lists nested as deep as you like, quotes, rules,
# code blocks and tables; and inside a line bold, italic, both, strikethrough,
# code, links and backslash escapes. The page, the PDF, Word and plain text
# are all written from the one parse below, so what shows is what prints.

LIST_ITEM = re.compile(r"^(\s*)([-*+•]|\d{1,9}[.)])\s+(.*)$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
FENCE = re.compile(r"^\s{0,3}(```|~~~)(.*)$")
QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")

_PROMPT_RES: list = []


def is_prompt(text: str) -> bool:
    """A paragraph that is still one of a new letter's writing prompts, as
    written, in any language: a note to the writer, not part of the letter."""
    if not _PROMPT_RES:
        for ps in PROMPTS.values():
            for p in ps:
                rx = re.escape(" ".join(p.split())).replace(re.escape("{company}"), ".+?")
                _PROMPT_RES.append(re.compile("^" + rx + "$"))
    flat = " ".join(str(text or "").split())
    return any(r.match(flat) for r in _PROMPT_RES)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s)]


def _starts_block(line: str, nxt: str | None) -> bool:
    return bool(HEADING.match(line) or RULE.match(line) or FENCE.match(line)
                or QUOTE.match(line) or LIST_ITEM.match(line)
                or ("|" in line and nxt is not None and TABLE_SEP.match(nxt) and "-" in nxt))


def _join(lines: list[str]) -> str:
    """Lines of one paragraph: a line ending in two spaces or a backslash
    breaks there; any other line break is a space."""
    out = ""
    for i, l in enumerate(lines):
        hard = l.endswith("  ") or l.rstrip(" ").endswith("\\")
        t = l.strip()
        if t.endswith("\\") and not t.endswith("\\\\"):
            t = t[:-1].rstrip()
        out += t + (("\n" if hard else " ") if i < len(lines) - 1 else "")
    return out


def _list(lines: list[str]) -> list[dict]:
    """Items and continuation lines as a tree, by how far each is indented."""
    items: list[list] = []            # [indent, ordered, number, text lines]
    for l in lines:
        m = LIST_ITEM.match(l)
        if m:
            ind = len(m.group(1).expandtabs(4))
            mark = m.group(2)
            items.append([ind, mark[0].isdigit(), int(mark[:-1]) if mark[0].isdigit() else 1,
                          [m.group(3)]])
        elif items:
            items[-1][3].append(l)

    def build(i: int, base: int) -> tuple[dict, int]:
        first = items[i]
        node = {"t": "ol" if first[1] else "ul", "start": first[2], "items": []}
        while i < len(items) and items[i][0] >= base:
            # A bullet after numbers, or numbers after bullets, at the same
            # depth: a new list.
            if items[i][0] == base and items[i][1] != first[1]:
                break
            if items[i][0] > base and node["items"]:
                child, i = build(i, items[i][0])
                node["items"][-1]["children"].append(child)
                continue
            node["items"].append({"text": _join(items[i][3]), "children": []})
            i += 1
        return node, i

    out, i = [], 0
    while i < len(items):
        node, i = build(i, items[i][0])
        out.append(node)
    return out


def parse_md(body: str) -> list[dict]:
    """The blocks of a letter's body, in order."""
    lines = (body or "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
    out: list[dict] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        nxt = lines[i + 1] if i + 1 < n else None
        if not line.strip():
            i += 1
            continue
        m = FENCE.match(line)
        if m:
            fence, code = m.group(1), []
            i += 1
            while i < n and not lines[i].strip().startswith(fence):
                code.append(lines[i])
                i += 1
            out.append({"t": "code", "text": "\n".join(code)})
            i += 1
            continue
        m = HEADING.match(line)
        if m:
            out.append({"t": "h", "level": min(3, len(m.group(1))), "text": m.group(2)})
            i += 1
            continue
        if RULE.match(line):
            out.append({"t": "hr"})
            i += 1
            continue
        if "|" in line and nxt is not None and TABLE_SEP.match(nxt) and "-" in nxt:
            head = _cells(line)
            aligns = []
            for c in _cells(nxt):
                aligns.append("center" if c.startswith(":") and c.endswith(":")
                              else "right" if c.endswith(":") else "left")
            rows = []
            i += 2
            while i < n and lines[i].strip() and "|" in lines[i]:
                rows.append(_cells(lines[i]))
                i += 1
            w = len(head)
            rows = [(r + [""] * w)[:w] for r in rows]
            out.append({"t": "table", "head": head, "rows": rows, "align": (aligns + ["left"] * w)[:w]})
            continue
        if QUOTE.match(line):
            q = []
            while i < n and lines[i].strip() and (QUOTE.match(lines[i]) or not _starts_block(lines[i], None)):
                m = QUOTE.match(lines[i])
                q.append(m.group(1) if m else lines[i])
                i += 1
            out.append({"t": "quote", "blocks": parse_md("\n".join(q))})
            continue
        if LIST_ITEM.match(line):
            block = []
            while i < n:
                l = lines[i]
                if not l.strip():
                    # A blank line inside a list ends it unless the list goes on.
                    j = i + 1
                    while j < n and not lines[j].strip():
                        j += 1
                    if j < n and (LIST_ITEM.match(lines[j]) or lines[j].startswith("  ")):
                        i = j
                        continue
                    break
                if block and not LIST_ITEM.match(l) and not l.startswith(" ") and _starts_block(l, lines[i + 1] if i + 1 < n else None):
                    break
                block.append(l)
                i += 1
            out += _list(block)
            continue
        para = []
        while i < n and lines[i].strip():
            if para and _starts_block(lines[i], lines[i + 1] if i + 1 < n else None):
                break
            para.append(lines[i])
            i += 1
        out.append({"t": "p", "text": _join(para)})
    return out


def blocks(body: str, prompts: bool = False) -> list[dict]:
    """The letter's blocks. A writing prompt nobody replaced is left out
    unless asked for: it is never printed, exported, or sent."""
    return [b for b in parse_md(body)
            if prompts or not (b["t"] == "p" and is_prompt(b["text"]))]


def prompts_left(body: str) -> list[str]:
    """The writing prompts still in a letter, as the page shows them."""
    return [b["text"] for b in parse_md(body) if b["t"] == "p" and is_prompt(b["text"])]


# Inside a line: each piece of text with what it is (b, i, s, code, br) and
# the link it belongs to, if any.
INLINE_TOK = re.compile(
    r"\\([\\`*_{}\[\]()#+\-.!~|>])"                 # 1 escaped character
    r"|`([^`]+)`"                                    # 2 code
    r"|\*\*\*(?!\s)(.+?)(?<!\s)\*\*\*"                # 3 bold italic
    r"|\*\*(?!\s)(.+?)(?<!\s)\*\*|(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)"   # 4, 5 bold
    r"|~~(?!\s)(.+?)(?<!\s)~~"                        # 6 strikethrough
    r"|\*(?!\s)(.+?)(?<!\s)\*|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)"         # 7, 8 italic
    r"|\[([^\]]+)\]\(([^)\s]+)\)"                    # 9, 10 link
    r"|(\n)")                                        # 11 hard break
INLINE = INLINE_TOK


def spans(text: str, st: frozenset = frozenset(), url: str | None = None) -> list:
    out: list = []
    pos = 0
    for m in INLINE_TOK.finditer(text or ""):
        if m.start() > pos:
            out.append((text[pos:m.start()], st, url))
        g = m.groups()
        if g[0] is not None:
            out.append((g[0], st, url))
        elif g[1] is not None:
            out.append((g[1], st | {"code"}, url))
        elif g[2] is not None:
            out += spans(g[2], st | {"b", "i"}, url)
        elif g[3] is not None or g[4] is not None:
            out += spans(g[3] if g[3] is not None else g[4], st | {"b"}, url)
        elif g[5] is not None:
            out += spans(g[5], st | {"s"}, url)
        elif g[6] is not None or g[7] is not None:
            out += spans(g[6] if g[6] is not None else g[7], st | {"i"}, url)
        elif g[8] is not None:
            out += spans(g[8], st, g[9])
        else:
            out.append(("\n", st | {"br"}, url))
        pos = m.end()
    if pos < len(text or ""):
        out.append((text[pos:], st, url))
    return out


def _typ_escape(s: str) -> str:
    return re.sub(r'([\\#$@*_<>\[\]`~=/"+-])', r"\\\1", s)


def _typ_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _groups(sp: list):
    """Consecutive spans that share a link, together."""
    run: list = []
    for x in sp:
        if run and x[2] != run[-1][2]:
            yield run[0][2], run
            run = []
        run.append(x)
    if run:
        yield run[0][2], run


def to_typst(text: str) -> str:
    out = []
    for url, run in _groups(spans(text)):
        parts = []
        for t, st, _ in run:
            if "br" in st:
                parts.append("#linebreak()")
                continue
            x = f"#raw({_typ_str(t)})" if "code" in st else _typ_escape(t)
            if "s" in st:
                x = f"#strike[{x}]"
            if "i" in st:
                x = f"#emph[{x}]"
            if "b" in st:
                x = f"#strong[{x}]"
            parts.append(x)
        body = "".join(parts)
        out.append(f"#link({_typ_str(url)})[{body}]" if url else body)
    return "".join(out)


def to_plain(text: str) -> str:
    out = []
    for url, run in _groups(spans(text)):
        body = "".join(t for t, _, _ in run)
        out.append(f"{body} ({url})" if url and url != body else body)
    return "".join(out)


def _walk_text(bl: list[dict]):
    for b in bl:
        if b["t"] in ("p", "h"):
            yield b["text"]
        elif b["t"] in ("ul", "ol"):
            stack = [b]
            while stack:
                node = stack.pop()
                for it in node["items"]:
                    yield it["text"]
                    stack += it["children"]
        elif b["t"] == "quote":
            yield from _walk_text(b["blocks"])
        elif b["t"] == "table":
            yield from b["head"]
            for r in b["rows"]:
                yield from r
        elif b["t"] == "code":
            yield b["text"]


def word_count(body: str) -> int:
    """Words of the letter itself: its writing prompts are not."""
    words = 0
    for t in _walk_text(blocks(body)):
        words += len(re.findall(r"[^\W_][\w'’-]*", to_plain(t)))
    return words


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


HEAD_KEYS = ("name", "headline", "contact", "signature")


def override_head(head: dict, over) -> dict:
    """The letterhead with what this letter says instead of its CV.

    A letter's header can hold `letterhead: {name, headline, contact,
    signature}`; each one set replaces what the CV gives, for this letter
    only, and an empty headline means none. What the CV says is kept beside
    it, so the page can offer to go back to it.
    """
    head = dict(head)
    head["from_cv"] = {"name": head["name"], "headline": head["headline"],
                       "contact": list(head["contact"]), "signature": head["name"]}
    over = over if isinstance(over, dict) else {}
    for k in ("name", "headline"):
        if over.get(k) is not None:
            head[k] = str(over[k]).strip()
    if over.get("contact") is not None:
        c = over["contact"]
        items = c if isinstance(c, list) else re.split(r"\s*[•·|]\s*", str(c))
        head["contact"] = [str(x).strip() for x in items if str(x).strip()]
    head["name"] = head["name"] or head["from_cv"]["name"]
    head["signature"] = str(over.get("signature") or "").strip() or head["name"]
    head["overridden"] = [k for k in HEAD_KEYS if over.get(k) is not None]
    return head


# --------------------------------------------------------------------------
# Laying it out
# --------------------------------------------------------------------------

def typst_source(meta: dict, body: str, head: dict, parts: list[str] | None = None,
                 end: str = "") -> str:
    m = head["margins"]
    contact = " #h(0.8em)#text(fill: grey)[•]#h(0.8em) ".join(
        _typ_escape(c) for c in head["contact"])
    to = meta.get("to")
    to_lines = to if isinstance(to, list) else [l for l in str(to or "").split("\n") if l.strip()]
    if parts is None:
        parts = [_typ_block(b, head) for b in blocks(body)]
    lang = str(meta.get("language") or "en")
    return f'''#let accent = rgb("{head["name_color"]}")
#let grey = rgb("{head["contact_color"]}")
#set document(title: {_typ_str(str(meta.get("subject") or "Cover letter"))}, author: {_typ_str(head["name"])})
#set page(paper: {_typ_str(head["paper"])}, margin: (top: {m["top"]}, bottom: {m["bottom"]}, left: {m["left"]}, right: {m["right"]}))
#set text(font: {_typ_str(head["font"])}, size: 10.5pt, lang: {_typ_str(lang)}, fill: rgb("{head["body_color"]}"))
#set par(justify: true, leading: 0.72em, spacing: 1.15em)
#set list(indent: 0.6em, spacing: 0.7em, marker: [•])
#show link: it => underline(offset: 2pt, stroke: 0.5pt + rgb("{head["link_color"]}"), it)
#show heading: set text(fill: rgb("{head["rule_color"]}"), weight: "bold")
#show heading: set block(above: 1.3em, below: 0.7em)
#show heading.where(level: 1): set text(size: 15pt)
#show heading.where(level: 2): set text(size: 12.5pt)
#show heading.where(level: 3): set text(size: 11pt)
#show raw: set text(font: ("DejaVu Sans Mono", "Liberation Mono", "Courier New"), size: 9.5pt)
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
{chr(10).join(p + chr(10) for p in parts)}{end}
#v(2.2em)
#text(weight: "bold")[{_typ_escape(head.get("signature") or head["name"])}]
'''



# Where each paragraph landed, for the page view: the same position probes
# the CV map uses (cv_map), one on its own line before each paragraph, which
# leaves the page byte-identical. A paragraph here is what the review calls
# one: the text between blank lines.

def chunks(body: str) -> list[str]:
    return [c.strip("\n") for c in re.split(r"\n\s*\n", (body or "").strip()) if c.strip()]


def page_map(meta: dict, body: str, head: dict, out: Path, font_dirs: list[Path] | None = None) -> dict | None:
    """{"bands": [{k: "header" | "para", i, page, y0, y1}], "box"} in points,
    or None when the paragraphs cannot be told apart reliably (a code block
    or a loose list with blank lines inside it)."""
    import json
    import typst
    import cv_map
    parts_of = [[_typ_block(b, head) for b in blocks(c)] for c in chunks(body)]
    if [b for c in chunks(body) for b in blocks(c)] != blocks(body):
        return None
    parts = []
    for i, ps in enumerate(parts_of):
        if ps:
            parts.append(f"#{cv_map.LABEL}({i})\n" + "\n\n".join(ps))
    if not parts:
        return None
    src = cv_map.HELPER + typst_source(meta, body, head, parts, end=f"#{cv_map.LABEL}(-1)\n")
    scratch = out / ".cvstudio-map.typ"
    try:
        out.mkdir(parents=True, exist_ok=True)
        scratch.write_text(src, encoding="utf-8")
        raw = typst.query(input=str(scratch), selector=f"<{cv_map.LABEL}>", field="value",
                          format="json", font_paths=_font_paths(font_dirs or []))
        values = json.loads(raw)
    except Exception:
        return None
    finally:
        try:
            scratch.unlink()
        except OSError:
            pass
    marks = [{"kind": "header", "page": 1, "y": 0.0}]
    for v in values:
        n = v.get("n")
        marks.append({"kind": "end" if n == -1 else "para", "i": None if n == -1 else n,
                      "page": v["p"], "y": v["y"], "x": v.get("x"), "h": v.get("h")})
    bands = [b for b in cv_map._bands(marks) if b["k"] != "end"]
    width = cv_map.PAGE_PT.get(str(head.get("paper") or "a4"))
    right = cv_map._length(str(head["margins"].get("right") or ""))
    lefts = [m["x"] for m in marks if isinstance(m.get("x"), (int, float))]
    heights = [m["h"] for m in marks if isinstance(m.get("h"), (int, float))]
    box = None
    if width and right is not None and lefts and width[0] - right > min(lefts):
        box = {"x0": min(lefts), "x1": width[0] - right, "page_width": width[0],
               "page_height": max(heights) if heights else width[1]}
    return {"bands": bands, "box": box}

def _typ_list(node: dict, depth: int = 0) -> str:
    pad = "  " * depth
    lines = []
    for k, it in enumerate(node["items"]):
        mark = f"{node['start'] + k}." if node["t"] == "ol" else "-"
        lines.append(f"{pad}{mark} {to_typst(it['text'])}")
        for ch in it["children"]:
            lines.append(_typ_list(ch, depth + 1))
    return "\n".join(lines)


def _typ_block(b: dict, head: dict) -> str:
    t = b["t"]
    if t == "p":
        return to_typst(b["text"])
    if t == "h":
        return f"#heading(level: {b['level']}, outlined: false)[{to_typst(b['text'])}]"
    if t in ("ul", "ol"):
        return _typ_list(b)
    if t == "hr":
        return f'#line(length: 100%, stroke: 0.5pt + rgb("{head["contact_color"]}"))'
    if t == "code":
        return ('#block(fill: luma(245), inset: 8pt, radius: 3pt, width: 100%)'
                f'[#raw(block: true, {_typ_str(b["text"])})]')
    if t == "quote":
        inner = "\n\n".join(_typ_block(x, head) for x in b["blocks"])
        return (f'#block(inset: (left: 11pt, y: 3pt), stroke: (left: 1.5pt + rgb("{head["rule_color"]}")))'
                f'[#set text(style: "italic", fill: rgb("{head["contact_color"]}"))\n{inner}]')
    if t == "table":
        n = len(b["head"])
        al = ", ".join(b["align"])
        cells = [f"[#strong[{to_typst(c)}]]" for c in b["head"]]
        for r in b["rows"]:
            cells += [f"[{to_typst(c)}]" for c in r]
        return (f'#table(columns: {n}, align: ({al},), inset: 6pt, '
                f'stroke: 0.5pt + rgb("{head["contact_color"]}"), ' + ", ".join(cells) + ")")
    return ""


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

def _plain_blocks(bl: list[dict], depth: int = 0) -> list[str]:
    out = []
    for b in bl:
        t = b["t"]
        if t in ("p", "h"):
            out.append(to_plain(b["text"]))
        elif t in ("ul", "ol"):
            def lines(node, d):
                for k, it in enumerate(node["items"]):
                    mark = f"{node['start'] + k}." if node["t"] == "ol" else "•"
                    yield "   " * d + mark + " " + to_plain(it["text"])
                    for ch in it["children"]:
                        yield from lines(ch, d + 1)
            out.append("\n".join(lines(b, 0)))
        elif t == "quote":
            out.append("\n".join("> " + l for l in "\n\n".join(_plain_blocks(b["blocks"])).split("\n")))
        elif t == "hr":
            out.append("———")
        elif t == "code":
            out.append(b["text"])
        elif t == "table":
            rows = [b["head"]] + b["rows"]
            out.append("\n".join(" | ".join(to_plain(c) for c in r) for r in rows))
    return out


def plain_text(meta: dict, body: str, head: dict) -> str:
    return "\n\n".join(_plain_blocks(blocks(body)) + [head.get("signature") or head["name"]]) + "\n"


def _hex(c) -> str:
    return str(c or "000000").lstrip("#")


def _w_runs(text: str, *, bold=False, italic=False, size=None, color=None, font=None) -> str:
    """Inline Markdown as WordprocessingML runs, over the paragraph's own look."""
    runs = []
    for t, st, url in spans(text):
        if "br" in st:
            runs.append("<w:r><w:br/></w:r>")
            continue
        props = (("<w:rFonts w:ascii=\"Consolas\" w:hAnsi=\"Consolas\"/>" if "code" in st
                  else f'<w:rFonts w:ascii="{html.escape(font)}" w:hAnsi="{html.escape(font)}"/>' if font else "")
                 + ("<w:b/>" if bold or "b" in st else "") + ("<w:i/>" if italic or "i" in st else "")
                 + ("<w:strike/>" if "s" in st else "")
                 + (f'<w:color w:val="{_hex(color)}"/>' if color else "")
                 + (f'<w:sz w:val="{size}"/>' if size else "")
                 + ('<w:u w:val="single"/>' if url else "")
                 + ('<w:shd w:val="clear" w:color="auto" w:fill="F2F2F2"/>' if "code" in st else ""))
        runs.append(f'<w:r>{"<w:rPr>" + props + "</w:rPr>" if props else ""}'
                    f'<w:t xml:space="preserve">{html.escape(t, quote=False)}</w:t></w:r>')
    return "".join(runs)


def _wp(runs: str, *, align=None, after=160, before=None, indent=None, hanging=None,
        left_border=None, bottom_border=None, shade=None, keep_next=False) -> str:
    ppr = ((f'<w:keepNext/>' if keep_next else "")
           + (f'<w:pBdr>' + (f'<w:left w:val="single" w:sz="12" w:space="8" w:color="{_hex(left_border)}"/>' if left_border else "")
              + (f'<w:bottom w:val="single" w:sz="6" w:space="1" w:color="{_hex(bottom_border)}"/>' if bottom_border else "")
              + '</w:pBdr>' if left_border or bottom_border else "")
           + (f'<w:shd w:val="clear" w:color="auto" w:fill="{shade}"/>' if shade else "")
           + f'<w:spacing w:after="{after}"' + (f' w:before="{before}"' if before is not None else "") + '/>'
           + (f'<w:ind w:left="{indent}"' + (f' w:hanging="{hanging}"' if hanging else "") + '/>' if indent else "")
           + (f'<w:jc w:val="{align}"/>' if align else ""))
    return f"<w:p><w:pPr>{ppr}</w:pPr>{runs}</w:p>"


def _w_blocks(bl: list[dict], head: dict, *, indent=0, quote=False) -> list[str]:
    out = []
    grey = head["contact_color"]
    for b in bl:
        t = b["t"]
        if t == "p":
            out.append(_wp(_w_runs(b["text"], italic=quote, color=grey if quote else None),
                           indent=indent or None, left_border=grey if quote else None))
        elif t == "h":
            size = {1: 30, 2: 25, 3: 22}[b["level"]]
            out.append(_wp(_w_runs(b["text"], bold=True, size=size, color=head["rule_color"]),
                           before=240, after=100, indent=indent or None, keep_next=True))
        elif t in ("ul", "ol"):
            def items(node, d):
                for k, it in enumerate(node["items"]):
                    mark = f"{node['start'] + k}.\t" if node["t"] == "ol" else "•\t"
                    out.append(_wp('<w:r><w:t xml:space="preserve">' + mark + "</w:t></w:r>" + _w_runs(it["text"], italic=quote),
                                   indent=indent + 360 * (d + 1), hanging=300, after=60))
                    for ch in it["children"]:
                        items(ch, d + 1)
            items(b, 0)
        elif t == "quote":
            out += _w_blocks(b["blocks"], head, indent=indent + 240, quote=True)
        elif t == "hr":
            out.append(_wp("", bottom_border=grey, after=200))
        elif t == "code":
            for line in b["text"].split("\n") or [""]:
                run = ('<w:r><w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas"/><w:sz w:val="19"/></w:rPr>'
                       f'<w:t xml:space="preserve">{html.escape(line, quote=False)}</w:t></w:r>') if line else ""
                out.append(_wp(run, shade="F4F4F4", after=0, indent=indent or None))
            out.append(_wp("", after=120))
        elif t == "table":
            border = f'w:val="single" w:sz="4" w:space="0" w:color="{_hex(grey)}"'
            rows = []
            for r_i, row in enumerate([b["head"]] + b["rows"]):
                cells = "".join(
                    f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                    f'<w:p><w:pPr><w:spacing w:after="0"/><w:jc w:val="{ {"left": "left", "center": "center", "right": "right"}[b["align"][c_i]] }"/></w:pPr>'
                    f'{_w_runs(c, bold=r_i == 0)}</w:p></w:tc>'
                    for c_i, c in enumerate(row))
                rows.append(f"<w:tr>{cells}</w:tr>")
            out.append('<w:tbl><w:tblPr><w:tblW w:w="0" w:type="auto"/><w:tblBorders>'
                       + "".join(f"<w:{e} {border}/>" for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
                       + '</w:tblBorders><w:tblCellMar><w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>'
                       + "</w:tblPr>" + "".join(rows) + "</w:tbl>")
            out.append(_wp("", after=120))
    return out


def docx(meta: dict, body: str, head: dict) -> bytes:
    """A plain .docx: the same parts in the same order, in the CV's font. Built
    by hand rather than with a library, because a letter needs a dozen
    paragraph kinds and the app should not ship a dependency for them."""
    font = html.escape(head["font"])

    def t(s):
        return f'<w:r><w:t xml:space="preserve">{html.escape(s, quote=False)}</w:t></w:r>'

    ps = [_wp(_w_runs(head["name"], bold=True, size=44, color=head["name_color"]), after=0)]
    if head["headline"]:
        ps.append(_wp(_w_runs(head["headline"], size=22, color=head["headline_color"]), after=60))
    ps.append(_wp(_w_runs("   •   ".join(head["contact"]), size=18, color=head["contact_color"]), after=360))
    to = meta.get("to")
    for line in (to if isinstance(to, list) else [l for l in str(to or "").split("\n") if l.strip()]):
        ps.append(_wp(t(str(line)), after=0))
    ps.append(_wp(_w_runs(date_line(meta), color=head["contact_color"]), align="right", after=360))
    if meta.get("subject"):
        ps.append(_wp(_w_runs(str(meta["subject"]), bold=True), after=240))
    ps += _w_blocks(blocks(body), head)
    ps.append(_wp(_w_runs(head.get("signature") or head["name"], bold=True), before=480, after=0))
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
