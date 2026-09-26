"""Bring a CV you already have into CV Studio.

Three sources, read on this machine and nowhere else:

- LinkedIn's data archive (Settings > Data privacy > Get a copy of your data).
  It is a zip of CSV files -- Profile, Positions, Education, Skills and so on --
  so every field comes across as the field it is, with nothing to guess.
- A PDF: a CV somebody made elsewhere, or a LinkedIn profile saved with
  More > Save to PDF. Only the text layer is available, so contact details are
  matched exactly and the rest is sorted into sections by their headings and
  dated lines. What could not be placed is reported rather than dropped.
- A JSON export from another builder: Reactive Resume (rxresu.me) or the
  JSON Resume standard. Fields map to fields, like the LinkedIn archive.

The result is RenderCV's own `cv` block plus a summary of what was found, for
the caller to preview and, once the person agrees, write. Nothing here signs
in anywhere or writes a file.
"""

from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile

try:
    import pypdf
except ImportError:  # pragma: no cover
    pypdf = None
try:
    import phonenumbers
except ImportError:  # pragma: no cover
    phonenumbers = None

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


class ImportError_(ValueError):
    """A file that cannot be imported, with a sentence saying why."""


def _date(s: str | None) -> str | None:
    """'Jan 2020', 'January 2020', '01/2020', '2020-01', '2020' -> RenderCV's form."""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"(?i)present|current|now|today|aujourd'hui|actuel", s):
        return "present"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})[/.](\d{4})", s)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([A-Za-z]{3,})\.?\s+(\d{4})", s)
    if m and m.group(1)[:3].lower() in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1)[:3].lower()]:02d}"
    m = re.fullmatch(r"(\d{4})", s)
    if m:
        return m.group(1)
    return None


def _phone(cv: dict, notes: list[str]) -> None:
    """Keep a phone number only in a form RenderCV accepts.

    RenderCV validates it as an international number, so one written the
    national way (06 12 34 56 78) would stop the whole CV rendering. Which
    country it belongs to cannot be known from the digits, so it is left out
    and said so, rather than guessed.
    """
    raw = cv.get("phone")
    if not raw:
        return
    try:
        num = phonenumbers.parse(raw, None) if phonenumbers else None
        if num is not None and phonenumbers.is_valid_number(num):
            cv["phone"] = phonenumbers.format_number(
                num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            return
    except Exception:
        num = None
    del cv["phone"]
    if num is not None:
        # It has its country code, but the numbering plan RenderCV checks
        # against does not know the range. Printed as text it still reaches
        # the page, just without the validation.
        shown = phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        cv.setdefault("custom_connections", []).insert(
            0, {"placeholder": shown, "url": None, "fontawesome_icon": "phone"})
        notes.append(f"The phone number {shown} is not in the numbering plan RenderCV checks "
                     "against, so it is shown as plain text in the header instead.")
        return
    notes.append(f"The phone number {raw} has no country code, so it was left out. "
                 "Add it with its code, like +33 6 12 34 56 78.")


def _found(cv: dict, source: str, notes: list[str]) -> dict:
    """What came across, counted, for the person to check before it is written."""
    _phone(cv, notes)
    # A year alone has to reach RenderCV as a number: written as text,
    # "2013" prints as "Jan 2013".
    for items in (cv.get("sections") or {}).values():
        for e in items if isinstance(items, list) else []:
            if isinstance(e, dict):
                for k in ("start_date", "end_date", "date"):
                    if isinstance(e.get(k), str) and re.fullmatch(r"\d{4}", e[k]):
                        e[k] = int(e[k])
    sec = cv.get("sections") or {}
    contact = [k for k in ("name", "headline", "location", "email", "phone", "website")
               if cv.get(k)] + (["social networks"] if cv.get("social_networks") else [])
    found = [{"what": "Name and contact details",
              "count": f"{len(contact)} field{'s' if len(contact) != 1 else ''}"}]
    labels = {"summary": "Summary", "experience": "Experience", "education": "Education",
              "skills": "Skills", "languages": "Languages", "certifications": "Certifications",
              "projects": "Projects"}
    for key, items in sec.items():
        n = len(items)
        if key == "experience":
            hl = sum(len(e.get("highlights") or []) for e in items if isinstance(e, dict))
            count = f"{n} role{'s' if n != 1 else ''}" + (f", {hl} highlights" if hl else "")
        elif key == "education":
            count = f"{n} entr{'ies' if n != 1 else 'y'}"
        elif key == "summary":
            count = f"{n} paragraph{'s' if n != 1 else ''}"
        else:
            count = f"{n} item{'s' if n != 1 else ''}"
        found.append({"what": labels.get(key, key.replace("_", " ").capitalize()), "count": count})
    return {"ok": True, "source": source, "cv": cv, "found": found, "notes": notes}


# ------------------------------------------------------------------ LinkedIn
def _csv_rows(z: zipfile.ZipFile, name: str) -> list[dict]:
    """Rows of the first file in the archive whose name matches, case-blind.

    LinkedIn nests some files in folders and has changed its capitalisation
    before, so the match is on the file name alone.
    """
    for info in z.infolist():
        if info.filename.rsplit("/", 1)[-1].lower() == name.lower():
            text = z.read(info).decode("utf-8-sig", errors="replace")
            # Some exports open with a "Notes:" paragraph and a blank line
            # before the header row. A one-column file has no comma in its
            # header, so the preamble is recognised by its word, not by that.
            lines = text.splitlines()
            if lines and lines[0].lstrip('"').lower().startswith("notes"):
                while lines and lines[0].strip():
                    lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
            return [{(k or "").strip(): (v or "").strip() for k, v in row.items()}
                    for row in csv.DictReader(io.StringIO("\n".join(lines)))]
    return []


def from_linkedin_zip(data: bytes) -> dict:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ImportError_("That zip could not be opened.") from exc
    names = {i.filename.rsplit("/", 1)[-1].lower() for i in z.infolist()}
    if "profile.csv" not in names and "positions.csv" not in names:
        raise ImportError_("This does not look like a LinkedIn data archive: "
                           "it has no Profile.csv or Positions.csv.")
    notes: list[str] = []
    cv: dict = {}
    prof = (_csv_rows(z, "Profile.csv") or [{}])[0]
    name = " ".join(x for x in (prof.get("First Name"), prof.get("Last Name")) if x)
    if name:
        cv["name"] = name
    if prof.get("Headline"):
        cv["headline"] = prof["Headline"]
    if prof.get("Geo Location"):
        cv["location"] = prof["Geo Location"]
    emails = _csv_rows(z, "Email Addresses.csv")
    primary = next((e for e in emails if e.get("Primary", "").lower() == "yes"),
                   emails[0] if emails else None)
    if primary and primary.get("Email Address"):
        cv["email"] = primary["Email Address"]
    phones = _csv_rows(z, "PhoneNumbers.csv")
    if phones and phones[0].get("Number"):
        cv["phone"] = phones[0]["Number"]
    if prof.get("Websites"):
        site = re.search(r"https?://\S+", prof["Websites"])
        if site:
            cv["website"] = site.group(0).rstrip("]")

    sections: dict = {}
    if prof.get("Summary"):
        sections["summary"] = [p.strip() for p in re.split(r"\n\s*\n", prof["Summary"]) if p.strip()]
    exp = []
    for p in _csv_rows(z, "Positions.csv"):
        if not (p.get("Company Name") or p.get("Title")):
            continue
        entry = {"company": p.get("Company Name") or "", "position": p.get("Title") or ""}
        if p.get("Location"):
            entry["location"] = p["Location"]
        start, end = _date(p.get("Started On")), _date(p.get("Finished On"))
        if start:
            entry["start_date"] = start
            entry["end_date"] = end or "present"
        desc = [ln.strip(" •-*\t") for ln in (p.get("Description") or "").splitlines()]
        desc = [ln for ln in desc if ln]
        if len(desc) > 1:
            entry["highlights"] = desc
        elif desc:
            entry["summary"] = desc[0]
        exp.append(entry)
    if exp:
        sections["experience"] = exp
    edu = []
    for e in _csv_rows(z, "Education.csv"):
        if not e.get("School Name"):
            continue
        # LinkedIn keeps "Master, Computer Science" in one field; RenderCV
        # prints the degree and the subject apart.
        entry = {"institution": e["School Name"], "area": _clean(e.get("Degree Name") or "")}
        m = DEGREE.match(entry["area"])
        if m:
            rest = _clean(entry["area"][m.end():].lstrip(" ,–—-"))
            if rest:
                entry["degree"], entry["area"] = m.group(0).strip(" ."), rest
        start, end = _date(e.get("Start Date")), _date(e.get("End Date"))
        if start:
            entry["start_date"] = start
        if end:
            entry["end_date"] = end
        if e.get("Notes"):
            entry["summary"] = e["Notes"]
        if not entry["area"]:
            notes.append(f"{entry['institution']} has no degree name in the archive; "
                         "the area is left blank.")
        edu.append(entry)
    if edu:
        sections["education"] = edu
    skills = [s.get("Name") for s in _csv_rows(z, "Skills.csv") if s.get("Name")]
    if skills:
        sections["skills"] = [{"label": "Skills", "details": ", ".join(skills)}]
    langs = [l for l in _csv_rows(z, "Languages.csv") if l.get("Name")]
    if langs:
        sections["languages"] = [{"label": l["Name"],
                                  "details": l.get("Proficiency") or ""} for l in langs]
    certs = [c for c in _csv_rows(z, "Certifications.csv") if c.get("Name")]
    if certs:
        sections["certifications"] = [
            " — ".join(x for x in (c["Name"], c.get("Authority")) if x) for c in certs]
    if sections:
        cv["sections"] = sections
    if not cv.get("name"):
        notes.append("No name was found in Profile.csv.")
    return _found(cv, "LinkedIn data archive", notes)


# ----------------------------------------------------------------------- PDF
HEADINGS = {
    "summary": r"summary|profile|about( me)?|professional summary|objective|profil|résumé|a propos|à propos",
    "experience": r"(work |professional )?experience|employment( history)?|work history|career|expérience(s)?( professionnelle(s)?)?",
    "education": r"education|academic background|studies|formation(s)?|éducation",
    "skills": r"(top |key |technical )?skills|competences|compétences|expertise|technologies",
    "languages": r"languages|langues",
    "certifications": r"certifications?|licenses?( & certifications)?|certificats?",
    "projects": r"projects|projets",
    "contact": r"contact|coordonnées",
    "honors": r"honors?(-| & )awards|awards|distinctions",
    "publications": r"publications",
}
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(?<!\d)(\+?\d[\d\s().-]{7,}\d)(?!\d)")
LINKEDIN = re.compile(r"(?:https?://)?(?:[\w-]+\.)?linkedin\.com/in/([\w\-%]+)", re.I)
URL = re.compile(r"(?:https?://|www\.)[^\s,;]+", re.I)
MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|janv|févr|fevr|mars|avr|mai|juin|juil|août|aout|déc)[a-zéû]*\.?"
DATE_TOKEN = rf"(?:{MON}\s+\d{{4}}|\d{{1,2}}[/.]\d{{4}}|\d{{4}}-\d{{2}}|\d{{4}})"
RANGE = re.compile(
    rf"(?i)({DATE_TOKEN})\s*(?:-|–|—|to|à|au)\s*({DATE_TOKEN}|present|current|now|today|aujourd'hui|actuel)")
BULLET = re.compile(r"^\s*[•●◦▪‣◆◇■□►▸➢✓✔\-*–]\s+")


def _heading(line: str) -> str | None:
    t = line.strip().strip(":").strip()
    if not t or len(t) > 40:
        return None
    for key, pat in HEADINGS.items():
        if re.fullmatch(rf"(?i){pat}", t):
            return key
    return None


def _pdf_lines(data: bytes) -> list[str]:
    if pypdf is None:
        raise ImportError_("pypdf is not installed, so the PDF cannot be read.")
    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:  # a damaged or encrypted PDF
        raise ImportError_(f"That PDF could not be read ({type(exc).__name__}).") from exc
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln and not re.fullmatch(r"(?i)page \d+ of \d+", ln)]
    if sum(len(ln) for ln in lines) < 40:
        raise ImportError_("That PDF has no text to read. It may be a scan; export it "
                           "from the program that made it, or import your LinkedIn "
                           "archive instead.")
    return lines


def _to_date(tok: str) -> str | None:
    tok = tok.strip().rstrip(".")
    if re.fullmatch(r"(?i)present|current|now|today|aujourd'hui|actuel", tok):
        return "present"
    m = re.fullmatch(rf"(?i)({MON})\s+(\d{{4}})", tok)
    if m:
        mon = m.group(1).lower().replace("é", "e").replace("û", "u")[:3]
        fr = {"fev": "feb", "avr": "apr", "mai": "may", "jui": "jun", "aou": "aug", "dec": "dec"}
        mon = fr.get(mon, mon)
        if mon == "jui" or m.group(1).lower().startswith("juil"):
            mon = "jul"
        if mon in MONTHS:
            return f"{m.group(2)}-{MONTHS[mon]:02d}"
    return _date(tok)


ROLE_WORDS = re.compile(
    r"(?i)\b(engineer|developer|manager|director|lead|head|intern|analyst|designer|"
    r"consultant|officer|scientist|specialist|architect|founder|owner|associate|assistant|"
    r"coordinator|administrator|advisor|adviser|researcher|teacher|professor|lecturer|"
    r"product|marketing|sales|executive|president|vp|cto|ceo|cfo|coo|partner|"
    r"technician|writer|editor|nurse|accountant|recruiter|trainee|apprentice|"
    r"stagiaire|ingénieur|développeur|chef|responsable|directeur|directrice|title|role)\b")
LOCATION = re.compile(r"[A-ZÀ-Ý][\w .'’-]*\s*,\s*[A-ZÀ-Ý][\w .'’-]*(\s*,\s*[A-ZÀ-Ý][\w .'’-]*)?")
DURATION = re.compile(r"(?i)^\(?\s*(\d+\s*(years?|yrs?|ans?|mos?|months?|mois)\s*)+\)?$")
DEGREE = re.compile(r"(?i)^(MSc|MS|MA|MBA|BSc|BS|BA|BEng|MEng|PhD|DPhil|MPhil|LLM|LLB|MD|"
                    r"Master(?:['’]s)?(?: degree)?|Bachelor(?:['’]s)?(?: degree)?|Licence|"
                    r"Doctorat|Diplôme|DUT|BTS)\b\.?")


def _clean(s: str) -> str:
    return re.sub(r"\s+,", ",", re.sub(r"\s+", " ", s)).strip(" ,|–—-·")


def _take_location(text: str) -> tuple[str, str | None]:
    """'Company – City, Country' -> ('Company', 'City, Country')."""
    m = re.search(r"\s*[–—|·-]\s*(" + LOCATION.pattern + r")\s*$", text)
    if m:
        return text[:m.start()], _clean(m.group(1))
    return text, None


def _role_and_company(parts: list[str]) -> tuple[str, str, bool]:
    """Which part is the job and which the employer. Returns (position,
    company, sure). Role words decide it; with none, the first part is taken
    as the job, which is the commoner order, and it is reported as a guess."""
    parts = [_clean(p) for p in parts if _clean(p)]
    if not parts:
        return "", "", False
    if len(parts) == 1:
        one = parts[0]
        m = re.match(r"(.+?)\s+(?:at|@|chez)\s+(.+)", one)
        if m:
            return _clean(m.group(1)), _clean(m.group(2)), True
        pieces = re.split(r"\s*,\s*|\s+[–—|]\s+", one, maxsplit=1)
        if len(pieces) == 1:
            return one, "", False
        parts = pieces
    a, b = parts[0], parts[1]
    ra, rb = bool(ROLE_WORDS.search(a)), bool(ROLE_WORDS.search(b))
    if rb and not ra:
        return b, a, True
    return a, b, ra and not rb


def _entries(body: list[str]) -> tuple[list[dict], list[str]]:
    """Cut a section into entries. An entry is a header (title, employer or
    school), a date range, perhaps a location, and bullets -- in whatever order
    the layout printed them: date first, date last, date on a line after the
    bullets, or LinkedIn's employer and title on lines of their own."""
    entries: list[dict] = []
    pending: list[str] = []
    cur: dict | None = None

    def new(header: list[str]) -> dict:
        e = {"header": [h for h in header if h], "start": None, "end": None,
             "location": None, "bullets": []}
        entries.append(e)
        return e

    for ln in body:
        rng = RANGE.search(ln)
        if BULLET.match(ln):
            # Lines between an entry's header and its first bullet are more of
            # the header (Ink puts the title under the employer line); lines
            # after an entry's bullets start the next one.
            if pending and (cur is None or cur["bullets"]):
                cur = new(pending)
                pending = []
            elif pending:
                cur["header"] += pending
                pending = []
            if cur is None:
                cur = new([])
            cur["bullets"].append(BULLET.sub("", ln))
            continue
        if rng:
            rest = ln[:rng.start()] + " " + ln[rng.end():]
            # LinkedIn follows the dates with how long that was, and a year
            # range in brackets leaves the brackets behind.
            rest = re.sub(r"\(\s*(\d+\s*(years?|yrs?|mos?|months?|ans?|mois)\s*)*\)", "", rest)
            rest = _clean(re.sub(r"\s*·\s*$", "", rest.strip()))
            if cur is not None and not cur["start"]:
                cur["header"] += pending + ([rest] if rest else [])
            elif cur is not None and not cur["bullets"] and rest and pending:
                # A dated line with its own header text starts the next entry;
                # the lines before it finish the previous one's header (a
                # degree under its school).
                cur["header"] += pending
                cur = new([rest])
            else:
                if cur is not None and len(pending) > 2:
                    cur["bullets"] += pending[:-2]
                    pending = pending[-2:]
                cur = new(pending + ([rest] if rest else []))
            pending = []
            cur["start"], cur["end"] = _to_date(rng.group(1)), _to_date(rng.group(2))
            continue
        if DURATION.match(ln):
            continue
        if cur is not None and not cur["location"] and LOCATION.fullmatch(ln.strip()):
            cur["location"] = _clean(ln)
            continue
        if cur is not None and cur["bullets"] and ln[:1].islower():
            cur["bullets"][-1] += " " + ln          # a wrapped bullet
            continue
        pending.append(ln)
    loose: list[str] = []
    if pending:
        if cur is not None:
            # What is left after the last entry is description if it reads like
            # a sentence, and more of the header (a degree line) if not.
            for ln in pending:
                prose = len(ln) > 60 or ln.rstrip().endswith(".")
                (cur["bullets"] if prose or cur["bullets"] else cur["header"]).append(ln)
        else:
            loose = pending
    return entries, loose


def from_pdf(data: bytes) -> dict:
    lines = _pdf_lines(data)
    notes: list[str] = []
    blob = "\n".join(lines)
    cv: dict = {}

    # Contact details: exact matches wherever they are on the page.
    email = EMAIL.search(blob)
    if email:
        cv["email"] = email.group(0).rstrip(".")
    li = LINKEDIN.search(blob)
    if li:
        cv["social_networks"] = [{"network": "LinkedIn", "username": li.group(1).strip("/")}]
    for m in PHONE.finditer(blob):
        digits = re.sub(r"\D", "", m.group(1))
        if 8 <= len(digits) <= 15 and not RANGE.search(m.group(1)):
            cv["phone"] = re.sub(r"\s+", " ", m.group(1)).strip()
            break
    for m in URL.finditer(blob):
        if "linkedin.com" not in m.group(0).lower():
            site = m.group(0).rstrip(".,)")
            cv["website"] = site if site.startswith("http") else "https://" + site
            break

    # Sections by their headings. Everything before the first heading is the
    # header: the first line that is not contact detail is the name, the next
    # the headline.
    blocks: dict[str, list[str]] = {}
    order: list[str] = []
    current = "_head"
    blocks[current] = []
    for ln in lines:
        key = _heading(ln)
        if key:
            current = key
            if key not in blocks:
                blocks[key] = []
                order.append(key)
            continue
        blocks[current].append(ln)

    def is_contact(ln: str) -> bool:
        return bool(EMAIL.search(ln) or LINKEDIN.search(ln) or URL.search(ln)
                    or (PHONE.search(ln) and len(re.sub(r"\D", "", ln)) >= 8))

    head = [ln for ln in blocks["_head"] if not is_contact(ln)]
    # LinkedIn's own PDF opens with a sidebar -- Contact, Top Skills,
    # Languages -- and the name comes after it, at the foot of whichever
    # sidebar block is last. The profile address spells the name, so the line
    # that matches it is the name, and the lines after it are the headline and
    # the location; they come out of the block they were read into.
    if li and not head:
        words = [w for w in re.split(r"[-_]", li.group(1).lower()) if len(w) > 2 and w.isalpha()]
        for key in [k for k in order if k != "_head"]:
            body = blocks[key]
            hit = next((i for i, ln in enumerate(body)
                        if words and sum(w in ln.lower() for w in words) >= min(2, len(words))
                        and not is_contact(ln)), None)
            if hit is not None:
                head = body[hit:]
                blocks[key] = body[:hit]
                break
    # A LinkedIn PDF puts Contact, Top Skills and Languages in a sidebar before
    # the name; the name is then the first line of the main column.
    if not head and blocks.get("contact"):
        head = [ln for ln in blocks["contact"] if not is_contact(ln)]
    if head:
        cv["name"] = head[0]
        if len(head) > 1 and len(head[1]) < 120:
            cv["headline"] = head[1]
        loc = next((ln for ln in head[1:4] if re.fullmatch(
            r"[A-ZÀ-Ý][\w .'-]+,\s*[A-ZÀ-Ý][\w .'-]+(,\s*[A-ZÀ-Ý][\w .'-]+)?", ln)), None)
        if loc:
            cv["location"] = loc

    sections: dict = {}
    if blocks.get("summary"):
        text = " ".join(blocks["summary"]).strip()
        if text:
            sections["summary"] = [text]

    if blocks.get("experience"):
        entries, loose = _entries(blocks["experience"])
        exp, guessed = [], 0
        for e in entries:
            header = e["header"]
            if len(header) == 1:
                text, loc = _take_location(header[0])
                position, company, sure = _role_and_company([text])
            else:
                # LinkedIn prints the employer, then the title, on lines of their own.
                texts = []
                loc = None
                for h in header[:2]:
                    t, l = _take_location(h)
                    texts.append(t)
                    loc = loc or l
                position, company, sure = _role_and_company(list(reversed(texts)))
            entry = {"company": company, "position": position}
            if loc or e["location"]:
                entry["location"] = e["location"] or loc
            if e["start"]:
                entry["start_date"] = e["start"]
                entry["end_date"] = e["end"] or "present"
            if e["bullets"]:
                entry["highlights"] = e["bullets"]
            guessed += (not sure) and bool(company)
            if not company:
                notes.append(f'"{position}" has no employer that could be told apart from '
                             "the title. Check it on the page.")
            exp.append(entry)
        if exp:
            sections["experience"] = exp
        if guessed:
            notes.append(f"For {guessed} role{'s' if guessed != 1 else ''}, which part is the "
                         "job title and which the employer was a guess. Check them on the page.")
        if loose:
            notes.append(f"{len(loose)} line{'s' if len(loose) != 1 else ''} under Experience "
                         "had no dates and were not placed.")

    if blocks.get("education"):
        entries, loose = _entries(blocks["education"])
        edu = []
        for e in entries:
            loc = None
            bits = []
            for h in e["header"]:
                t, l = _take_location(h)
                bits.append(t)
                loc = loc or l
            text = " – ".join(b for b in bits if _clean(b))
            degree = None
            m = DEGREE.match(text)
            if m:
                degree = m.group(0).strip(" .")
                text = text[m.end():]
            parts = [_clean(p) for p in re.split(r"\s*,\s*|\s+[–—]\s+", text, maxsplit=1)]
            institution = parts[0] if parts else ""
            area = parts[1] if len(parts) > 1 else ""
            if degree and area.lower().startswith(degree.lower() + " "):
                area = area[len(degree) + 1:]
            m = DEGREE.match(area)
            if not degree and m:
                degree = m.group(0).strip(" .")
                area = _clean(area[m.end():])
            if area.lower().startswith("in "):
                area = area[3:]
            entry = {"institution": institution, "area": area}
            if degree:
                entry["degree"] = degree
            if loc or e["location"]:
                entry["location"] = e["location"] or loc
            if e["start"]:
                entry["start_date"] = e["start"]
            if e["end"]:
                entry["end_date"] = e["end"]
            if e["bullets"]:
                entry["highlights"] = e["bullets"]
            edu.append(entry)
        if not entries and loose:
            for i in range(0, len(loose), 2):
                edu.append({"institution": loose[i],
                            "area": loose[i + 1] if i + 1 < len(loose) else ""})
            notes.append("Education had no dates; each school came in without them.")
        if edu:
            sections["education"] = edu

    for key, label in (("skills", "Skills"), ("languages", "Languages")):
        body = blocks.get(key) or []
        if not body:
            continue
        items = []
        for ln in body:
            m = re.match(r"([^:]{2,40}):\s*(.+)", ln)
            if m:
                items.append({"label": m.group(1).strip(), "details": m.group(2).strip()})
            else:
                items.append({"label": label, "details": BULLET.sub("", ln)})
        # Loose single-word lines (a LinkedIn sidebar) become one line of them.
        plain = [i for i in items if i["label"] == label]
        if len(plain) > 1:
            items = [i for i in items if i["label"] != label] + [
                {"label": label, "details": ", ".join(i["details"] for i in plain)}]
            notes.append(f'"{label}" was read as one list; split it into groups if you like.')
        sections[key] = items

    for key in ("certifications", "projects", "honors", "publications"):
        body = [BULLET.sub("", ln) for ln in blocks.get(key) or []]
        if body:
            sections[key] = body

    if sections:
        cv["sections"] = sections
    if not cv.get("name"):
        notes.append("No name could be found at the top of the PDF.")
    if not sections:
        notes.append("No section headings were recognised, so only the contact details "
                     "came across. A connected AI client can structure the rest.")
    return _found(cv, "PDF", notes)


# ---------------------------------------------------------------------- JSON
def _html_text(fragment: str | None) -> tuple[list[str], list[str]]:
    """(paragraphs, bullets) out of a rich-text field: list items become
    highlights, paragraphs stay text, bold stays bold."""
    frag = fragment or ""
    if "<" not in frag:
        lines = [ln.strip(" \t•-*") for ln in html.unescape(frag).splitlines()]
        lines = [ln for ln in lines if ln]
        return (lines, []) if len(lines) < 2 else ([], lines)
    frag = re.sub(r"(?is)<(strong|b)>(.*?)</\1>", r"**\2**", frag)
    bullets = [re.sub(r"<[^>]+>", " ", li) for li in re.findall(r"(?is)<li[^>]*>(.*?)</li>", frag)]
    rest = re.sub(r"(?is)<(ul|ol)[^>]*>.*?</\1>", "\n", frag)
    paras = [re.sub(r"<[^>]+>", " ", p) for p in re.split(r"(?i)</p>|<br\s*/?>|</div>", rest)]
    tidy = lambda t: re.sub(r"\s+", " ", html.unescape(t)).strip()
    return [t for t in map(tidy, paras) if t], [t for t in map(tidy, bullets) if t]


def _period(s: str | None) -> tuple[str | None, str | None]:
    """'April 24 - Present', 'Nov 22 - Sept 24', '2013-2018', 'Jan 2020 – Mar 2021'."""
    s = (s or "").strip()
    if not s:
        return None, None
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+|(?<=\d)[-–—](?=\d)|\s+to\s+", s) if p.strip()]

    def one(p: str) -> str | None:
        got = _date(p)
        if got:
            return got
        m = re.fullmatch(r"([A-Za-z]{3,})\.?\s+'?(\d{2})", p)
        if m and m.group(1)[:3].lower() in MONTHS:
            return f"20{m.group(2)}-{MONTHS[m.group(1)[:3].lower()]:02d}"
        return None
    start = one(parts[0]) if parts else None
    end = one(parts[1]) if len(parts) > 1 else None
    return start, end


def _dated(entry: dict, start: str | None, end: str | None, raw: str | None, notes: list[str],
           what: str) -> None:
    if start:
        entry["start_date"] = start
        if end:
            entry["end_date"] = end
    elif raw:
        notes.append(f"The dates of {what} ({raw}) could not be read; add them in the editor.")


def _described(entry: dict, fragment: str | None) -> None:
    paras, bullets = _html_text(fragment)
    if paras:
        entry["summary"] = " ".join(paras)
    if bullets:
        entry["highlights"] = bullets


def _network(url: str, label: str = "") -> dict | None:
    for net, pat in (("LinkedIn", r"linkedin\.com/in/([^/?#]+)"), ("GitHub", r"github\.com/([^/?#]+)"),
                     ("GitLab", r"gitlab\.com/([^/?#]+)"), ("X", r"(?:twitter|x)\.com/([^/?#]+)")):
        m = re.search(pat, url or "", re.I)
        if m:
            return {"network": net, "username": m.group(1)}
    return None


def from_reactive_resume(d: dict) -> dict:
    notes: list[str] = []
    b = d.get("basics") or {}
    cv: dict = {k: str(b[k]).strip() for k in ("name", "headline", "email", "phone", "location")
                if str(b.get(k) or "").strip()}
    site = b.get("website") or b.get("url") or {}
    site = site.get("url") or site.get("href") if isinstance(site, dict) else site
    if site:
        cv["website"] = site if str(site).startswith("http") else "https://" + site
    sec_all = d.get("sections") or {}
    nets = []
    for f in b.get("customFields") or []:
        n = _network(f.get("link") or f.get("text") or "")
        if n:
            nets.append(n)
    prof = (sec_all.get("profiles") or {})
    for it in prof.get("items") or []:
        if it.get("hidden"):
            continue
        url = (it.get("url") or {}).get("href") if isinstance(it.get("url"), dict) else it.get("url")
        n = _network(url or "") or ({"network": it.get("network"), "username": it.get("username")}
                                    if it.get("network") and it.get("username") else None)
        if n and n not in nets:
            nets.append(n)
    if nets:
        cv["social_networks"] = nets

    sections: dict = {}
    summ = d.get("summary") or sec_all.get("summary") or {}
    if not summ.get("hidden"):
        paras, bullets = _html_text(summ.get("content"))
        if paras or bullets:
            sections["summary"] = paras + bullets

    def items(key: str) -> list[dict]:
        s = sec_all.get(key) or {}
        if s.get("hidden") or s.get("visible") is False:
            return []
        return [i for i in s.get("items") or [] if not i.get("hidden") and i.get("visible", True)]

    exp = []
    for it in items("experience"):
        entry = {"company": it.get("company") or "", "position": it.get("position") or ""}
        if it.get("location"):
            entry["location"] = it["location"]
        raw = it.get("period") or it.get("date")
        _dated(entry, *_period(raw), raw, notes, entry["company"] or "a role")
        _described(entry, it.get("description") or it.get("summary"))
        exp.append(entry)
    if exp:
        sections["experience"] = exp

    edu = []
    for it in items("education"):
        entry = {"institution": it.get("school") or it.get("institution") or "",
                 "area": it.get("area") or it.get("studyType") or ""}
        if it.get("degree"):
            entry["degree"] = it["degree"]
        if it.get("location"):
            entry["location"] = it["location"]
        raw = it.get("period") or it.get("date")
        _dated(entry, *_period(raw), raw, notes, entry["institution"] or "a school")
        _described(entry, it.get("description") or it.get("summary"))
        if it.get("grade") or it.get("score"):
            entry.setdefault("highlights", []).append(f"Grade: {it.get('grade') or it.get('score')}")
        edu.append(entry)
    if edu:
        sections["education"] = edu

    for key, title in (("projects", "projects"), ("volunteer", "volunteering"),
                       ("awards", "awards"), ("publications", "publications")):
        out = []
        for it in items(key):
            name = it.get("name") or it.get("title") or it.get("organization") or ""
            if not name:
                continue
            entry = {"name": name}
            by = it.get("awarder") or it.get("publisher") or it.get("position")
            raw = it.get("period") or it.get("date")
            start, end = _period(raw)
            if start and end:
                entry["start_date"], entry["end_date"] = start, end
            elif start:
                entry["date"] = start
            _described(entry, it.get("description") or it.get("summary"))
            if by:
                entry["summary"] = by + (". " + entry["summary"] if entry.get("summary") else "")
            out.append(entry)
        if out:
            sections[title] = out

    skills = [{"label": it.get("name") or "",
               "details": ", ".join(it.get("keywords") or []) or it.get("description") or it.get("proficiency") or ""}
              for it in items("skills") if it.get("name")]
    skills = [s for s in skills if s["details"]] + [s for s in skills if not s["details"]]
    if skills:
        sections["skills"] = [s if s["details"] else s["label"] for s in skills]
    langs = [{"label": it.get("language") or it.get("name") or "",
              "details": it.get("fluency") or it.get("description") or ""}
             for it in items("languages") if it.get("language") or it.get("name")]
    if langs:
        sections["languages"] = [l if l["details"] else l["label"] for l in langs]
    certs = []
    for it in items("certifications"):
        name = it.get("title") or it.get("name")
        if not name:
            continue
        entry = {"name": name + (f", {it['issuer']}" if it.get("issuer") else "")}
        start, _ = _period(it.get("date"))
        if start:
            entry["date"] = start
        certs.append(entry)
    if certs:
        sections["certifications"] = certs
    interests = []
    for it in items("interests"):
        kw = ", ".join(it.get("keywords") or [])
        if it.get("name"):
            interests.append(f"{it['name']}: {kw}" if kw else it["name"])
    if interests:
        sections["interests"] = interests
    for cs in d.get("customSections") or []:
        if cs.get("hidden") or not cs.get("items"):
            continue
        notes.append(f"The custom section “{cs.get('title') or 'Custom'}” was left out; "
                     "copy it over in the editor if you need it.")
    if sections:
        cv["sections"] = sections
    pic = d.get("picture") or b.get("picture") or {}
    if isinstance(pic, dict) and pic.get("url") and not pic.get("hidden"):
        notes.append("Your photo stays on Reactive Resume's site. Add it under Design > Photo; "
                     "CV Studio keeps it on this computer.")
    if not cv.get("name"):
        notes.append("No name was found in the file.")
    return _found(cv, "Reactive Resume", notes)


def from_json_resume(d: dict) -> dict:
    notes: list[str] = []
    b = d.get("basics") or {}
    cv: dict = {}
    for src, dst in (("name", "name"), ("label", "headline"), ("email", "email"),
                     ("phone", "phone")):
        if str(b.get(src) or "").strip():
            cv[dst] = str(b[src]).strip()
    loc = b.get("location") or {}
    place = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("countryCode")) if x)
    if place:
        cv["location"] = place
    if b.get("url"):
        cv["website"] = b["url"]
    nets = []
    for p in b.get("profiles") or []:
        n = _network(p.get("url") or "") or ({"network": p["network"], "username": p["username"]}
                                            if p.get("network") and p.get("username") else None)
        if n:
            nets.append(n)
    if nets:
        cv["social_networks"] = nets
    sections: dict = {}
    if b.get("summary"):
        sections["summary"] = [p.strip() for p in re.split(r"\n\s*\n", b["summary"]) if p.strip()]
    exp = []
    for w in d.get("work") or []:
        entry = {"company": w.get("name") or w.get("company") or "", "position": w.get("position") or ""}
        if w.get("location"):
            entry["location"] = w["location"]
        start, end = _date(w.get("startDate")), _date(w.get("endDate"))
        if start:
            entry["start_date"], entry["end_date"] = start, end or "present"
        if w.get("summary"):
            entry["summary"] = w["summary"]
        if w.get("highlights"):
            entry["highlights"] = list(w["highlights"])
        exp.append(entry)
    if exp:
        sections["experience"] = exp
    edu = []
    for e in d.get("education") or []:
        entry = {"institution": e.get("institution") or "", "area": e.get("area") or ""}
        if e.get("studyType"):
            entry["degree"] = e["studyType"]
        start, end = _date(e.get("startDate")), _date(e.get("endDate"))
        if start:
            entry["start_date"] = start
        if end:
            entry["end_date"] = end
        if e.get("courses"):
            entry["highlights"] = list(e["courses"])
        edu.append(entry)
    if edu:
        sections["education"] = edu
    proj = []
    for p in d.get("projects") or []:
        if not p.get("name"):
            continue
        entry = {"name": p["name"]}
        if p.get("description"):
            entry["summary"] = p["description"]
        if p.get("highlights"):
            entry["highlights"] = list(p["highlights"])
        start, end = _date(p.get("startDate")), _date(p.get("endDate"))
        if start and end:
            entry["start_date"], entry["end_date"] = start, end
        elif start:
            entry["date"] = start
        proj.append(entry)
    if proj:
        sections["projects"] = proj
    skills = [{"label": s["name"], "details": ", ".join(s.get("keywords") or []) or s.get("level") or ""}
              for s in d.get("skills") or [] if s.get("name")]
    if skills:
        sections["skills"] = [s if s["details"] else s["label"] for s in skills]
    langs = [{"label": l["language"], "details": l.get("fluency") or ""}
             for l in d.get("languages") or [] if l.get("language")]
    if langs:
        sections["languages"] = [l if l["details"] else l["label"] for l in langs]
    certs = []
    for c in d.get("certificates") or []:
        if not c.get("name"):
            continue
        entry = {"name": c["name"] + (f", {c['issuer']}" if c.get("issuer") else "")}
        if _date(c.get("date")):
            entry["date"] = _date(c.get("date"))
        certs.append(entry)
    if certs:
        sections["certifications"] = certs
    interests = [i["name"] + (": " + ", ".join(i["keywords"]) if i.get("keywords") else "")
                 for i in d.get("interests") or [] if i.get("name")]
    if interests:
        sections["interests"] = interests
    if sections:
        cv["sections"] = sections
    if not cv.get("name"):
        notes.append("No name was found in basics.")
    return _found(cv, "JSON Resume", notes)


def from_json(data: bytes) -> dict:
    try:
        d = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ImportError_("That file is not valid JSON.") from exc
    if not isinstance(d, dict) or not isinstance(d.get("basics"), dict):
        raise ImportError_("This JSON is neither a Reactive Resume export nor a JSON Resume "
                           "file: it has no “basics”.")
    if isinstance(d.get("sections"), dict):
        return from_reactive_resume(d)
    return from_json_resume(d)


def import_file(filename: str, data: bytes) -> dict:
    """Route a file to the reader for its kind."""
    name = (filename or "").lower()
    if name.endswith(".zip") or data[:2] == b"PK":
        return from_linkedin_zip(data)
    if name.endswith(".pdf") or data[:4] == b"%PDF":
        return from_pdf(data)
    if name.endswith(".json") or data.lstrip()[:1] == b"{":
        return from_json(data)
    raise ImportError_("CV Studio can import a PDF, LinkedIn's data archive (.zip), "
                       "or a Reactive Resume or JSON Resume file (.json).")
