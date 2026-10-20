"""Job scans: what a scheduled search found, as a spreadsheet in the workspace.

A daily search for postings (an AI client's scheduled task, or a person with a
browser) leaves one row per job it found in `Job scans/Job scans.xlsx`: the
day it was found, which scan found it, the company, the role, where, the link,
why it fits, when it was posted, and a Status that says what became of it:
New until it is added to the tracker or skipped. The app lists the New rows
that are not tracked yet, one click to add or skip each; an AI client reads
the same list with `job_scans` and writes to it with `record_job_scan`.

It is a spreadsheet because that is the one file everyone can open, sort and
annotate without this app, and a scheduled task can write one with any tool.
So this reads what a person or another program put there, whatever its
columns are called, in English or French: any .xlsx, .csv or .tsv in the
folder counts. It writes only its own file, and keeps columns it does not know.

The .xlsx reading and writing below uses the standard library only: the format
is a zip of XML parts, and the few parts a table needs are short to write.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import threading
import unicodedata
import zipfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

DEFAULT_FOLDER = "Job scans"
FILE_NAME = "Job scans.xlsx"
SHEET = "Job scans"
# The file's columns, in order. Status is the bookkeeping: New, Added, Skipped.
COLUMNS = ["Found", "Scan", "Company", "Role", "Location", "Link", "Why it fits",
           "Posted", "Status", "Notes", "Description"]
FIELD_OF = {"Found": "found", "Scan": "scan", "Company": "company", "Role": "role",
            "Location": "location", "Link": "link", "Why it fits": "why",
            "Posted": "posted", "Status": "status", "Notes": "notes",
            "Description": "description"}
NEW, ADDED, SKIPPED = "New", "Added", "Skipped"
STATUSES = (NEW, ADDED, SKIPPED)
# What a person or another program may have written in a status column. Any
# other value ("To review", "À voir", empty) is still to look at.
STATUS_WORDS = {
    ADDED: ["added", "applied", "imported", "tracked", "in cv studio", "ajoute", "ajoutee",
            "postule", "postulee", "candidature envoyee", "agregado", "aplicado",
            "adicionado", "candidatado"],
    SKIPPED: ["skipped", "skip", "not interested", "no", "pass", "passed", "rejected",
              "declined", "ignored", "nope", "not for me", "pas interesse", "pas interessee",
              "non", "ignore", "refuse", "no me interesa", "descartado", "nao", "sem interesse"],
}
# What a sheet written somewhere else may call each column. Exact names first;
# then a header containing one of these as a word (so "Date posted" is
# posted, not found: posted is tried before found).
SYNONYMS = {
    "link": ["link", "url", "job link", "job url", "posting", "posting link", "lien",
             "lien de l'annonce", "annonce", "offre", "apply", "application link"],
    "role": ["role", "title", "job title", "job", "position", "poste", "intitule",
             "intitule du poste", "titre", "role title"],
    "company": ["company", "employer", "entreprise", "societe", "organisation",
                "organization", "company name", "employeur"],
    "location": ["location", "lieu", "ville", "city", "place", "localisation", "where",
                 "remote"],
    "posted": ["posted", "posted on", "date posted", "publication", "publiee le",
               "date de publication", "published", "age"],
    "found": ["found", "found on", "date found", "date", "day", "jour", "trouve le",
              "date trouvee", "scan date", "run date", "first seen"],
    "why": ["why it fits", "why", "fit", "reason", "pourquoi", "match", "why it matches"],
    "status": ["status", "statut", "state", "etat"],
    "notes": ["notes", "note", "comment", "comments", "commentaire", "commentaires",
              "remarks"],
    "scan": ["scan", "search", "sector", "secteur", "source", "board", "via", "site"],
    "description": ["description", "posting text", "job description", "details", "texte"],
}
ORDER = ["link", "role", "company", "location", "posted", "found", "why", "status",
         "notes", "scan", "description"]
PENDING = ".cvstudio-pending.json"
READABLE = (".xlsx", ".csv", ".tsv")
MAX_ROWS = 5000
MAX_PART = 32 * 1024 * 1024
URL = re.compile(r"https?://[^\s\"'<>]+")

M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


# --- keys ---------------------------------------------------------------------

def _fold(s) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()


TRACKING = re.compile(r"^(utm_.*|gclid|fbclid|mc_[a-z]+|ref|refid|trk|trackingid|src)$", re.I)


def norm_link(url: str | None) -> str:
    """A link as the same posting: scheme, case, www., a trailing slash, the
    fragment and tracking parameters do not make it another job."""
    url = (url or "").strip()
    if not url:
        return ""
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return ""
    host = u.hostname.casefold().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                       if not TRACKING.match(k)])
    return urlunparse(("https", host, u.path.rstrip("/") or "/", "", query, ""))


def key_of(row: dict) -> str:
    link = norm_link(row.get("link"))
    if link:
        return link
    co, role = _fold(row.get("company")), _fold(row.get("role"))
    return f"job:{co}|{role}" if co or role else ""


def tracked_index(jobs: list[dict]) -> tuple[set, set]:
    """The links and company-and-role pairs already in the tracker."""
    links = {norm_link(j.get("url")) for j in jobs} - {""}
    pairs = {(_fold(j.get("company")), _fold(j.get("title"))) for j in jobs}
    return links, pairs


def is_tracked(row: dict, index: tuple[set, set]) -> bool:
    links, pairs = index
    link = norm_link(row.get("link"))
    if link and link in links:
        return True
    co, role = _fold(row.get("company")), _fold(row.get("role"))
    return bool(co and role) and (co, role) in pairs


# --- reading ------------------------------------------------------------------

def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _letters(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _part(z: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART:
        raise ValueError(f"{name} is too large to read")
    return z.read(info)


def _rels(z: zipfile.ZipFile, name: str) -> dict[str, str]:
    raw = _part(z, name)
    if not raw:
        return {}
    return {r.get("Id"): r.get("Target") for r in ET.fromstring(raw).iter(f"{{{PR}}}Relationship")}


def _text(el) -> str:
    return "".join(t.text or "" for t in el.iter(f"{{{M}}}t"))


def _xlsx(path: Path) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    """Each sheet as (name, rows); a cell is (text, link)."""
    out = []
    with zipfile.ZipFile(path) as z:
        wb = ET.fromstring(_part(z, "xl/workbook.xml") or b"<x/>")
        rels = _rels(z, "xl/_rels/workbook.xml.rels")
        ss = _part(z, "xl/sharedStrings.xml")
        shared = [_text(si) for si in ET.fromstring(ss).iter(f"{{{M}}}si")] if ss else []
        for sh in wb.iter(f"{{{M}}}sheet"):
            target = rels.get(sh.get(f"{{{R}}}id"), "")
            name = target.lstrip("/") if target.startswith("/") else "xl/" + target
            raw = _part(z, name)
            if not raw:
                continue
            base, file = name.rsplit("/", 1)
            links = _rels(z, f"{base}/_rels/{file}.rels")
            root = ET.fromstring(raw)
            hyper = {}
            for h in root.iter(f"{{{M}}}hyperlink"):
                to = links.get(h.get(f"{{{R}}}id"))
                if to and h.get("ref"):
                    hyper[h.get("ref").split(":")[0]] = to
            rows = []
            for r in root.iter(f"{{{M}}}row"):
                cells: list[tuple[str, str]] = []
                for c in r.iter(f"{{{M}}}c"):
                    ref, kind = c.get("r") or "", c.get("t")
                    v, f = c.find(f"{{{M}}}v"), c.find(f"{{{M}}}f")
                    if kind == "s" and v is not None and (v.text or "").isdigit():
                        i = int(v.text)
                        text = shared[i] if i < len(shared) else ""
                    elif kind == "inlineStr":
                        text = _text(c.find(f"{{{M}}}is")) if c.find(f"{{{M}}}is") is not None else ""
                    elif kind == "b":
                        text = "TRUE" if (v is not None and v.text == "1") else "FALSE"
                    else:
                        text = (v.text or "") if v is not None else ""
                    link = hyper.get(ref, "")
                    if not link and f is not None and f.text:
                        m = re.search(r'HYPERLINK\(\s*"([^"]+)"', f.text, re.I)
                        link = m.group(1) if m else ""
                    i = _col(ref) if ref else len(cells)
                    while len(cells) < i:
                        cells.append(("", ""))
                    cells.append((text, link))
                rows.append(cells)
                if len(rows) >= MAX_ROWS:
                    break
            out.append((sh.get("name") or "", rows))
    return out


def _delimited(path: Path) -> list[tuple[str, list[list[tuple[str, str]]]]]:
    raw = path.read_bytes()[:MAX_PART]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    delim = "\t" if path.suffix.lower() == ".tsv" else None
    if delim is None:
        try:
            delim = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
        except csv.Error:
            delim = ","
    rows = [[(c, "") for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
    return [(path.stem, rows[:MAX_ROWS])]


def _field(header: str) -> str | None:
    h = _fold(header)
    if not h:
        return None
    for f in ORDER:
        if h in (_fold(s) for s in SYNONYMS[f]):
            return f
    for f in ORDER:
        for s in SYNONYMS[f]:
            s = _fold(s)
            if len(s) >= 3 and re.search(rf"\b{re.escape(s)}\b", h):
                return f
    return None


def _status(v: str) -> str:
    f = _fold(v)
    for status, words in STATUS_WORDS.items():
        if f == _fold(status) or f in (_fold(w) for w in words):
            return status
    return NEW


def _date(v: str) -> str:
    """Excel's day numbers as dates; anything else as written."""
    v = (v or "").strip()
    if re.fullmatch(r"\d{5}(\.\d+)?", v) and 20000 < float(v) < 80000:
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(float(v)))).isoformat()
    return v


def _columns(cells: list[tuple[str, str]]) -> dict[int, str]:
    """Which column holds which field: an exact name first, then a name that
    contains one, and each field once, so "Scan" and "Sector" side by side
    are the scan and a column of its own."""
    exact, loose = {}, {}
    for j, (t, _) in enumerate(cells):
        h = _fold(t)
        if not h:
            continue
        for f in ORDER:
            if h in (_fold(s) for s in SYNONYMS[f]):
                exact[j] = f
                break
        else:
            got = _field(t)
            if got:
                loose[j] = got
    out, taken = {}, set()
    for group in (exact, loose):
        for j, f in sorted(group.items()):
            if f not in taken:
                out[j] = f
                taken.add(f)
    return out


def _records(sheet: str, rows: list[list[tuple[str, str]]]) -> list[dict]:
    head, fields = None, {}
    for i, r in enumerate(rows[:10]):
        found = _columns(r)
        if len(found) >= 2 and set(found.values()) & {"link", "role", "company"}:
            head, fields = i, found
            break
    out = []
    if head is None:
        # No header anyone could read: a row is a job if it has a link.
        for r in rows:
            for t, link in r:
                m = link or (URL.search(t).group(0) if URL.search(t or "") else "")
                if m:
                    out.append({"link": m, "scan": sheet})
                    break
        return out
    names = {j: t for j, (t, _) in enumerate(rows[head])}
    for r in rows[head + 1:]:
        rec: dict = {"extra": {}}
        for j, (t, link) in enumerate(r):
            t = (t or "").strip()
            f = fields.get(j)
            if f:
                if f == "link":
                    rec["link"] = link or (URL.search(t).group(0) if URL.search(t) else t)
                else:
                    rec[f] = _date(t) if f in ("found", "posted") else t
                    # A role or company linked to the posting is the link too.
                    if link and not rec.get("link") and f in ("role", "company"):
                        rec["link"] = link
            elif t and names.get(j):
                rec["extra"][names[j]] = t
        if not any(rec.get(k) for k in ("link", "role", "company")):
            continue
        rec.setdefault("scan", sheet if sheet and sheet.casefold() != SHEET.casefold() else "")
        rec["status"] = _status(rec.get("status") or "")
        out.append(rec)
    return out


def read_file(path: Path) -> list[dict]:
    sheets = _xlsx(path) if path.suffix.lower() == ".xlsx" else _delimited(path)
    out = []
    for name, rows in sheets:
        for rec in _records(name, rows):
            rec["file"] = path.name
            out.append(rec)
    return out


# --- writing ------------------------------------------------------------------

def _cell(ref: str, value: str, style: int = 0) -> str:
    s = f' s="{style}"' if style else ""
    if value == "":
        return f'<c r="{ref}"{s}/>'
    return (f'<c r="{ref}"{s} t="inlineStr"><is><t xml:space="preserve">'
            f'{escape(_clean(value))}</t></is></c>')


def _clean(v) -> str:
    # XML 1.0 cannot carry most control characters; a cell holds 32767 at most.
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v if v is not None else ""))[:32000]


WIDTHS = {"Found": 12, "Scan": 22, "Company": 22, "Role": 40, "Location": 22, "Link": 46,
          "Why it fits": 60, "Posted": 12, "Status": 10, "Notes": 40, "Description": 60}


def write_xlsx(path: Path, rows: list[dict], extra: list[str]) -> None:
    """The table as a workbook of one sheet: a bold header row that stays put,
    a filter on every column, and each link clickable."""
    cols = COLUMNS + [e for e in extra if e not in COLUMNS]
    body, links = [], []
    body.append('<row r="1">' + "".join(_cell(f"{_letters(i)}1", c, 1) for i, c in enumerate(cols)) + "</row>")
    for n, row in enumerate(rows, start=2):
        cells = []
        for i, c in enumerate(cols):
            f = FIELD_OF.get(c)
            v = row.get(f, "") if f else row.get("extra", {}).get(c, "")
            ref = f"{_letters(i)}{n}"
            is_link = f == "link" and norm_link(v)
            cells.append(_cell(ref, v or "", 2 if is_link else (3 if f in ("why", "notes") else 0)))
            if is_link:
                links.append((ref, v))
        body.append(f'<row r="{n}">' + "".join(cells) + "</row>")
    last = f"{_letters(len(cols) - 1)}{max(1, len(rows) + 1)}"
    widths = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{WIDTHS.get(c, 20)}" customWidth="1"/>'
                     for i, c in enumerate(cols))
    hyper = "".join(f'<hyperlink ref="{ref}" r:id="rId{k + 1}"/>' for k, (ref, _) in enumerate(links))
    sheet = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             f'<worksheet xmlns="{M}" xmlns:r="{R}">'
             f'<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             f'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
             f'<cols>{widths}</cols><sheetData>{"".join(body)}</sheetData>'
             f'<autoFilter ref="A1:{last}"/>'
             + (f"<hyperlinks>{hyper}</hyperlinks>" if links else "") + "</worksheet>")
    sheet_rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PR}">'
                  + "".join(f'<Relationship Id="rId{k + 1}" Type="{R}/hyperlink" '
                            f'Target="{escape(_clean(url), {chr(34): "&quot;"})}" TargetMode="External"/>'
                            for k, (_, url) in enumerate(links)) + "</Relationships>")
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{M}">'
              '<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font>'
              '<font><u/><sz val="11"/><color rgb="FF0563C1"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
              '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
              '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
              '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1">'
              '<alignment wrapText="1" vertical="top"/></xf></cellXfs>'
              '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
              '</styleSheet>')
    parts = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PR}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml":
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{M}" xmlns:r="{R}">'
            f'<sheets><sheet name="{SHEET}" sheetId="1" r:id="rId1"/></sheets>'
            f'<definedNames><definedName name="_xlnm._FilterDatabase" localSheetId="0" hidden="1">'
            f"'{SHEET}'!$A$1:${last.rstrip('0123456789')}${max(1, len(rows) + 1)}</definedName></definedNames>"
            f'</workbook>',
        "xl/_rels/workbook.xml.rels":
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{PR}">'
            f'<Relationship Id="rId1" Type="{R}/worksheet" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{R}/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": styles,
        "xl/worksheets/sheet1.xml": sheet,
    }
    if links:
        parts["xl/worksheets/_rels/sheet1.xml.rels"] = sheet_rels
    # Its own name per writer: the app and the connector can both save the
    # sheet, and two zips written into one file make neither.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in parts.items():
            z.writestr(name, text)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# --- the folder ---------------------------------------------------------------

def folder_for(workspace: Path, setting: str | None) -> Path:
    """The scans folder: the setting, relative to the workspace unless it is a
    full path, else `Job scans` in the workspace."""
    s = (setting or "").strip()
    p = Path(s).expanduser() if s else Path(DEFAULT_FOLDER)
    return p if p.is_absolute() else workspace / p


def _readable(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and p.suffix.lower() in READABLE and not p.name.startswith((".", "~$")))


def stamp(folder: Path) -> str:
    """Each readable file's name, size and time, and the queue's: changes
    whenever anything in the folder that counts does."""
    parts = []
    for p in _readable(folder) + [folder / PENDING]:
        try:
            st = p.stat()
        except OSError:
            continue
        parts.append(f"{p.name}:{st.st_size}:{st.st_mtime_ns}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def own_file(folder: Path) -> Path:
    """The file CV Studio writes: `Job scans.xlsx`, or the only .xlsx in the
    folder when someone made one for it (an empty one included)."""
    mine = folder / FILE_NAME
    if mine.exists():
        return mine
    xlsx = [p for p in _readable(folder) if p.suffix.lower() == ".xlsx"]
    return xlsx[0] if len(xlsx) == 1 else mine


def _load_pending(folder: Path) -> list[dict]:
    try:
        data = json.loads((folder / PENDING).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _apply(rows: list[dict], ops: list[dict]) -> list[dict]:
    by_key = {key_of(r): r for r in rows if key_of(r)}
    for op in ops:
        if op.get("op") == "add":
            for r in op.get("rows") or []:
                k = key_of(r)
                if k and k not in by_key:
                    by_key[k] = r
                    rows.append(r)
        elif op.get("op") == "status":
            for k in op.get("keys") or []:
                if k in by_key:
                    by_key[k]["status"] = op.get("status") or NEW
                elif op.get("rows"):
                    for r in op["rows"]:
                        if key_of(r) == k:
                            r = dict(r, status=op.get("status") or NEW)
                            by_key[k] = r
                            rows.append(r)
    return rows


def load(folder: Path) -> dict:
    """Every row in the folder, own file first; the same job from two files
    once, with the own file's status. {rows, own, files, problems}."""
    own = own_file(folder)
    problems, files, rows, seen = [], [], [], {}
    paths = _readable(folder)
    paths.sort(key=lambda p: p != own)
    for p in paths:
        try:
            got = [] if p.stat().st_size == 0 else read_file(p)
        except (OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as exc:
            problems.append(f"{p.name}: could not be read ({type(exc).__name__}).")
            continue
        files.append({"name": p.name, "rows": len(got), "own": p == own})
        for r in got:
            k = key_of(r)
            if not k:
                continue
            if k in seen:
                # A job seen in the own file keeps its status; another file
                # may fill in what it left empty.
                have = seen[k]
                for f in ("role", "company", "location", "why", "posted", "found", "description"):
                    if not have.get(f) and r.get(f):
                        have[f] = r[f]
                continue
            r["own"] = p == own
            seen[k] = r
            rows.append(r)
    for p in folder.glob("*.xls") if folder.is_dir() else []:
        if p.stat().st_size:        # an empty one made for it has nothing to read
            problems.append(f"{p.name} is in the old Excel format. Save it as .xlsx (or .csv) to have it read.")
    rows = _apply(rows, _load_pending(folder))
    for r in rows:
        r["key"] = key_of(r)
    return {"rows": rows, "own": own, "files": files, "problems": problems}


def save(folder: Path, rows: list[dict]) -> bool:
    """Write the own file with every row, newest day first. False when the
    file cannot be replaced (open in Excel, most likely): the change waits in
    a small queue beside it and is written next time."""
    own = own_file(folder)
    folder.mkdir(parents=True, exist_ok=True)
    keep = [r for r in rows if r.get("own") or r.get("status") != NEW or r.get("recorded")]
    keep.sort(key=lambda r: str(r.get("found") or ""), reverse=True)
    extra: list[str] = []
    for r in keep:
        for k in r.get("extra") or {}:
            if k not in extra:
                extra.append(k)
    try:
        write_xlsx(own, keep, extra)
    except PermissionError:
        return False
    try:
        (folder / PENDING).unlink()
    except OSError:
        pass
    return True


def _queue(folder: Path, op: dict) -> None:
    ops = _load_pending(folder) + [op]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / PENDING).write_text(json.dumps(ops, ensure_ascii=False, indent=1), encoding="utf-8")


def _row_out(r: dict) -> dict:
    out = {k: r.get(k) or "" for k in ("key", "found", "scan", "company", "role", "location",
                                      "link", "why", "posted", "status", "notes", "file")}
    out["details"] = {k: v for k, v in (r.get("extra") or {}).items() if v}
    out["has_description"] = bool((r.get("description") or "").strip())
    return out


YES = re.compile(r"^(yes|y|true|oui|si|sim|x|1)$", re.I)


def _order(r: dict) -> tuple:
    """Newest day first; within a day, a search's top picks first, then the
    most recently posted."""
    top = any(YES.match(str(v).strip()) for k, v in (r.get("extra") or {}).items()
              if re.search(r"\btop\b|priorit", _fold(k)))
    posted = str(r.get("posted") or "")
    return (str(r.get("found") or ""), top, posted if re.match(r"\d{4}-\d\d-\d\d", posted) else "")


def state(folder: Path, jobs: list[dict]) -> dict:
    """What the folder holds, and the jobs in it still to look at: New, not in
    the tracker. Reads only; statuses catch up on the next write."""
    data = load(folder)
    index = tracked_index(jobs)
    new = [r for r in data["rows"] if r.get("status") == NEW and not is_tracked(r, index)]
    new.sort(key=_order, reverse=True)
    counts = {NEW: len(new), ADDED: 0, SKIPPED: 0}
    for r in data["rows"]:
        st = ADDED if r.get("status") == NEW and is_tracked(r, index) else r.get("status")
        if st in (ADDED, SKIPPED):
            counts[st] += 1
    scans: dict[str, int] = {}
    for r in new:
        name = r.get("scan") or ""
        scans[name] = scans.get(name, 0) + 1
    return {"folder": str(folder), "exists": folder.is_dir(), "file": data["own"].name,
            "file_exists": data["own"].exists(), "files": data["files"],
            "problems": data["problems"], "rows": len(data["rows"]),
            "new": [_row_out(r) for r in new],
            "counts": {"new": counts[NEW], "added": counts[ADDED], "skipped": counts[SKIPPED]},
            "scans": [{"name": k, "new": v} for k, v in scans.items()],
            "latest": max((str(r.get("found") or "") for r in new), default=""),
            "pending": bool(_load_pending(folder))}


def _settle(rows: list[dict], jobs: list[dict]) -> None:
    """Rows whose job is in the tracker now say Added, however it got there."""
    index = tracked_index(jobs)
    for r in rows:
        if r.get("status") == NEW and is_tracked(r, index):
            r["status"] = ADDED


def record(folder: Path, jobs_found: list[dict], jobs: list[dict], scan: str | None = None,
           found: str | None = None) -> dict:
    """Add what a scan found: one row per job not already in the sheet.
    {added, already, tracked, file, written}."""
    data = load(folder)
    rows, have = data["rows"], {r["key"] for r in data["rows"]}
    index = tracked_index(jobs)
    day = (found or dt.date.today().isoformat())[:10]
    new_rows, already, tracked = [], 0, 0
    for j in jobs_found:
        txt = lambda *ks: next((str(j[k]).strip() for k in ks if j.get(k) not in (None, "")), "")
        r = {"found": (txt("found") or day)[:10], "scan": txt("scan") or (scan or "").strip(),
             "company": txt("company"), "role": txt("role", "title"),
             "location": txt("location"), "link": txt("link", "url"),
             "why": txt("why", "fit", "why_it_fits"), "posted": txt("posted"),
             "notes": txt("notes"), "description": txt("description"),
             "extra": _details(j.get("details")),
             "status": NEW, "own": True, "recorded": True}
        k = key_of(r)
        if not k or not (r["company"] or r["role"] or r["link"]):
            continue
        if k in have:
            already += 1
            continue
        if is_tracked(r, index):
            r["status"] = ADDED
            tracked += 1
        have.add(k)
        r["key"] = k
        new_rows.append(r)
    rows.extend(new_rows)
    _settle(rows, jobs)
    written = save(folder, rows) if new_rows else True
    if not written:
        _queue(folder, {"op": "add", "rows": [{k: v for k, v in r.items() if k != "key"} for r in new_rows]})
    return {"added": len(new_rows) - tracked, "already": already, "tracked": tracked,
            "file": str(own_file(folder)), "written": written}


def _details(d) -> dict:
    """What a scan says beyond the columns (a sector, a top pick): a column
    each, a few short ones."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        k = re.sub(r"\s+", " ", str(k or "")).strip()[:40]
        if not k or k.casefold() in (c.casefold() for c in COLUMNS) or v in (None, "", False):
            continue
        out[k] = "Yes" if v is True else str(v).strip()[:500]
        if len(out) == 8:
            break
    return out


def set_status(folder: Path, keys: list[str], status: str, jobs: list[dict]) -> dict:
    """Mark rows Added or Skipped. A row from another file is copied into the
    own file with its new status, so the other file is never rewritten."""
    if status not in STATUSES:
        raise ValueError("A job a scan found is New, Added or Skipped.")
    data = load(folder)
    rows, by_key = data["rows"], {r["key"]: r for r in data["rows"]}
    wanted = [k for k in keys if k in by_key]
    if not wanted:
        raise ValueError("None of those rows is in the scans folder.")
    for k in wanted:
        by_key[k]["status"] = status
    _settle(rows, jobs)
    written = save(folder, rows)
    if not written:
        _queue(folder, {"op": "status", "keys": wanted, "status": status,
                        "rows": [{k: v for k, v in by_key[x].items() if k != "key"} for x in wanted]})
    return {"changed": len(wanted), "written": written, "file": str(own_file(folder))}


def find(folder: Path, key: str) -> dict | None:
    for r in load(folder)["rows"]:
        if r["key"] == key:
            return r
    return None


def create(folder: Path) -> Path:
    """The own file, with its header row, if it does not exist yet (or is the
    empty file someone made for it)."""
    own = own_file(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if not own.exists() or own.stat().st_size == 0:
        write_xlsx(own, [], [])
    return own
