"""What an applicant tracking system reads from a CV, and how it meets a posting.

An ATS does two things with a CV, and a checker can only honestly imitate
those two:

1. It parses the PDF. It pulls the text layer out and cuts it into fields --
   name, contact, employers, titles, dates, sections. Anything the text layer
   does not carry in a readable order is lost before a person ever sees it.
2. A recruiter searches what it parsed, or ranks it against the posting. A
   skill that is not written in the CV, in words the posting would use, is not
   found.

So the checks here read the rendered PDF itself -- the file an ATS actually
receives, not the YAML and not RenderCV's Markdown -- and the keyword match is
a list of what the posting asks for and whether the CV says it. There is no
universal ATS score, and this does not pretend to compute one: the match rate
is a count, labelled as one.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

try:
    import pypdf
except ImportError:  # the checks say so rather than failing
    pypdf = None


def pdf_text(pdf: Path) -> list[str]:
    """The text layer of each page, as a parser would extract it."""
    if pypdf is None:
        raise RuntimeError("pypdf is not installed, so the PDF cannot be read.")
    reader = pypdf.PdfReader(str(pdf))
    return [(page.extract_text() or "") for page in reader.pages]


# --------------------------------------------------------------------------
# Parsing checks
# --------------------------------------------------------------------------

# The headings parsers are trained on. A section called something else still
# reads fine to a person, and is often filed under "other" by a machine.
STANDARD_HEADINGS = {
    "summary", "profile", "professional summary", "about", "about me",
    "objective", "experience", "work experience", "professional experience",
    "employment", "employment history", "work history", "career history",
    "education", "skills", "technical skills", "core skills", "key skills",
    "projects", "selected projects", "certifications", "certificates",
    "licenses and certifications", "languages", "publications", "awards",
    "honors", "honours", "awards and honors", "volunteering",
    "volunteer experience", "interests", "hobbies", "references", "research",
    "teaching", "patents", "courses", "training", "leadership", "activities",
    "extracurricular activities",
    # French, since the tracker already speaks it (France Travail, Apec).
    "profil", "résumé", "expérience", "expériences", "expérience professionnelle",
    "expériences professionnelles", "parcours professionnel", "formation",
    "formations", "compétences", "compétences techniques", "langues", "projets",
    "certifications", "centres d'intérêt", "loisirs", "publications", "bénévolat",
}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE = re.compile(r"(?:\+?\d[\s().-]?){8,}")
PRIVATE_USE = re.compile("[\ue000-\uf8ff]")
LIGATURES = re.compile("[\ufb00-\ufb06]")


def _plain(s) -> str:
    return " ".join(str(s or "").split())


def _norm(s: str) -> str:
    """Text as a search box compares it: case, dashes and spacing folded."""
    s = s.lower().replace("\u2013", "-").replace("\u2014", "-").replace("\u00a0", " ")
    return " ".join(s.split())


def _entry_name(entry) -> str | None:
    if not isinstance(entry, dict):
        return None
    for k in ("company", "institution", "name", "title"):
        if entry.get(k):
            return _plain(entry[k])
    return None


def parse_checks(pages: list[str], data: dict) -> list[dict]:
    """What a parser would make of this PDF, one finding each.

    `level` is ok, warn or bad. `fix`, when present, names a one-click change
    in the design that removes the problem; the caller decides whether to
    offer it.
    """
    out: list[dict] = []
    text = "\n".join(pages)
    flat = _norm(text)
    cv = (data or {}).get("cv") or {}
    words = len(text.split())

    def add(level, id_, title, detail, fix=None):
        out.append({"id": id_, "level": level, "title": title, "detail": detail,
                    **({"fix": fix} if fix else {})})

    if words < 20:
        add("bad", "text", "No readable text",
            "The PDF has almost no text layer, so an ATS would see a blank "
            "page. This happens with CVs exported as images.")
        return out
    add("ok", "text", f"{words} words read from the PDF",
        "The text is real text, in a single column, which is what parsers "
        "handle best.")

    pua = PRIVATE_USE.findall(text)
    if pua:
        add("warn", "icons",
            f"{len(pua)} icon{'s' if len(pua) != 1 else ''} read as unreadable characters",
            "The icons beside your contact details are drawn from an icon "
            "font, and come out of the PDF as private-use characters. Most "
            "systems drop them; some print them as boxes or glue them to the "
            "next word, which can break the email or phone number they sit "
            "beside.", fix="icons")

    if LIGATURES.search(text):
        add("warn", "ligatures", "Some letter pairs are stored as ligatures",
            "Pairs like “fi” are stored as a single glyph, so a "
            "search for “profile” can miss the word. Changing the "
            "body font usually fixes it.")

    # A word broken at the end of a line ("Post-" / "greSQL") comes out of the
    # PDF as two pieces, and a keyword search misses both. Justified text
    # hyphenates; RenderCV can justify without it.
    split = re.findall(r"([A-Za-zÀ-ÿ]{2,}) ?-\s*\n\s*([a-zà-ÿ][A-Za-zÀ-ÿ]+)", text)
    typo = (((data or {}).get("design") or {}).get("typography") or {})
    if split and typo.get("alignment") != "justified-with-no-hyphenation":
        shown = ", ".join(f"“{a}-{b}”" for a, b in split[:3])
        add("warn", "hyphens",
            f"{len(split)} word{'s' if len(split) != 1 else ''} split across lines",
            "The page hyphenates long words at the end of a line, so the text an "
            "ATS reads has them in two pieces and a search for the whole word "
            f"misses them: {shown}. Justifying without hyphenation fixes it.",
            fix="hyphens")

    head = _norm(" ".join(pages[0].split()[:40])) if pages else ""
    name = _plain(cv.get("name"))
    if name and _norm(name) in head:
        add("ok", "name", "Your name is at the top", f"Read as “{name}”.")
    elif name:
        add("warn", "name", "Your name is not the first thing on the page",
            "Parsers take the name from the top of page one. It was not found "
            "there.")

    found_email = EMAIL.search(text)
    if found_email:
        add("ok", "email", "Email address found", found_email.group(0))
    else:
        add("bad", "email", "No email address found",
            "Without one, the ATS has no way to contact you and many will "
            "reject the file outright.")

    if cv.get("phone"):
        found_phone = PHONE.search(text)
        if found_phone:
            add("ok", "phone", "Phone number found", found_phone.group(0).strip())
        else:
            add("warn", "phone", "Phone number not found in the text",
                "It is in your CV, but not as digits a parser would recognise.")

    hidden = []
    for net in cv.get("social_networks") or []:
        if not isinstance(net, dict):
            continue
        network = _plain(net.get("network"))
        if network and _norm(network) not in flat:
            hidden.append(network)
    if hidden:
        add("warn", "urls",
            f"{', '.join(hidden)}: username only",
            "The PDF prints the handle without the site it belongs to, so a "
            "parser sees a bare word and cannot fill in the profile link. "
            "Showing the full address fixes it.", fix="urls")

    odd = []
    for key in (cv.get("sections") or {}):
        title = str(key).replace("_", " ").strip()
        if title.lower() not in STANDARD_HEADINGS:
            odd.append(title)
    if odd:
        add("warn", "headings", "Headings a parser may not recognise",
            ", ".join(f"“{t}”" for t in odd) + ". ATS software files "
            "content under standard headings like Experience, Education and "
            "Skills; a creative title can land in “other”.")
    elif cv.get("sections"):
        add("ok", "headings", "Section headings are standard",
            "Every section has a heading parsers are trained on.")

    missing, order = [], []
    for key, entries in (cv.get("sections") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            nm = _entry_name(entry)
            if not nm or len(nm) < 3:
                continue
            at = flat.find(_norm(nm))
            if at < 0:
                missing.append(nm)
            else:
                order.append((at, nm))
    if missing:
        add("bad", "entries", "Entries missing from the extracted text",
            ", ".join(missing[:6]) + (" and more" if len(missing) > 6 else "") +
            ". They are on the page but not in its text layer, so an ATS "
            "never sees them.")
    elif order and [a for a, _ in order] != sorted(a for a, _ in order):
        add("warn", "order", "Entries come out in a different order",
            "The text layer reads some entries out of the order they appear "
            "on the page, which is what happens with columns and sidebars. "
            "Dates and titles can end up attached to the wrong employer.")
    elif order:
        add("ok", "order", "Entries read in order",
            "Every employer, school and project comes out of the PDF in the "
            "order it is printed.")

    undated = []
    for key in ("experience", "education"):
        for entry in (cv.get("sections") or {}).get(key) or []:
            if isinstance(entry, dict) and not (entry.get("start_date") or entry.get("date")):
                undated.append(_entry_name(entry) or key)
    if undated:
        add("warn", "dates", "Entries without dates",
            ", ".join(undated[:5]) + ". Parsers work out years of experience "
            "from dates, and an undated role counts for nothing.")

    if len(pages) > 2:
        add("warn", "length", f"{len(pages)} pages",
            "Parsers read every page, but recruiters rarely do. Two pages is "
            "the usual ceiling.")
    return out


# --------------------------------------------------------------------------
# Keywords
# --------------------------------------------------------------------------

STOP = set("""
a about above across after again against all almost along also although always am
among an and another any anyone anything are around as at away back be because
been before being below best better between both but by can cannot could daily
day days did do does doing done down during each either else end enough etc even
ever every few first for from full further get gets getting give given go going
good great had has have having he help her here hers him his how however i if in
including into is it its itself just keep kind know large last least less like
likely long look lot made make makes making many may me might more most much must
my need needs new next no nor not now of off often on once one only or other our
ours out over own part per plus really role same see seek seeking several shall
she should show since so some something such sure take than that the their them
then there these they thing things think this those though through throughout
thus to together too toward towards under unique until up upon us use used using
very via want was way ways we well were what whatever when where whether which
while who whom whose why will with within without work works working would year
years yet you your yours yourself
ability able across advantage apply applicants applicant application benefits
bonus candidate candidates career careers company culture competitive
description dynamic employer employment environment equal excellent exciting
experience experienced expertise familiar familiarity field great growing growth
hands hire hiring ideal impact join joining knowledge level looking mission
nice offer office opportunity opportunities passion passionate people perks
plus position preferred proven qualifications qualified related relevant remote
required requirement requirements responsibilities responsibility responsible
salary skill skills solid strong success successful team teams understanding
value values we're you'll you're world-class years' environment based across
across hybrid onsite full-time part-time contract permanent days week weeks
month months including include includes ensure ensuring provide providing
support supporting within across etc across
le la les un une des du de d l et ou en au aux pour par sur dans avec sans
nous vous ils elles il elle est sont être avoir a ont qui que quoi dont où
ce cet cette ces son sa ses leur leurs notre nos votre vos plus très bien
tout tous toute toutes comme mais donc ainsi aussi afin chez entre vers
poste profil expérience expériences équipe équipes entreprise mission missions
candidat candidate recherche recherchons rejoindre maîtrise connaissance
connaissances capacité ans année années minimum idéalement souhaité souhaitée
build building built design designing own owning write writing improve
improving participate drive driving deliver delivering create creating points
bonus day-to-day hands-on track record fast-paced
""".split())

# Short names that are also ordinary words. Only counted when written the way
# the tool is: capitalised, and not just because they start a sentence.
SHORT_NAMES = {"Go", "R", "C", "Rust", "Swift", "Dart", "Elm", "Ruby"}

REQUIREMENT_CUES = re.compile(
    r"require|qualification|you have|you bring|must|looking for|what you|"
    r"about you|skills|experience with|nice to have|bonus|profil|compétences|"
    r"vous avez|requis|souhait", re.I)

TOKEN = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9]*(?:[+#]+|(?:[./&-][A-Za-zÀ-ÿ0-9]+)+)?")

# The same skill under the names people actually write it.
SYNONYMS = {
    "javascript": ["js"], "js": ["javascript"],
    "typescript": ["ts"], "kubernetes": ["k8s"], "k8s": ["kubernetes"],
    "postgresql": ["postgres"], "postgres": ["postgresql"],
    "golang": ["go"], "go": ["golang"],
    "ci/cd": ["ci", "continuous integration", "continuous delivery"],
    "machine learning": ["ml"], "ml": ["machine learning"],
    "aws": ["amazon web services"], "gcp": ["google cloud"],
    "google cloud": ["gcp"], "react": ["react.js", "reactjs"],
    "node.js": ["node", "nodejs"], "node": ["node.js", "nodejs"],
    "c#": ["csharp", ".net"], "user experience": ["ux"], "ux": ["user experience"],
}


def _techy(tok: str, sentence_start: bool) -> bool:
    """Whether a token looks like a name of something: a tool, a language, a
    method. Capitals mid-sentence, internal capitals, digits or symbols."""
    if re.search(r"[+#./&]|\d", tok):
        return True
    if len(tok) >= 2 and tok.isupper():
        return True
    if any(c.isupper() for c in tok[1:]):
        return True
    return tok[0].isupper() and not sentence_start


def keywords(posting: str, company: str | None = None, limit: int = 25) -> list[dict]:
    """The terms a posting asks for, most insisted-on first.

    A heuristic, and labelled as one where it is shown. Names of things
    (capitalised mid-sentence, or shaped like PostgreSQL, CI/CD, C++) count
    double; so does anything said in the requirements rather than the pitch;
    lower-case words and two-word phrases only count once they are repeated,
    which is what separates "distributed systems" from "our systems".
    """
    ignore = {w.lower() for w in TOKEN.findall(company or "")}
    uni: Counter = Counter()
    forms: dict[str, Counter] = {}
    techy: set[str] = set()
    req: set[str] = set()
    bi: Counter = Counter()
    in_req = False
    for line in posting.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if REQUIREMENT_CUES.search(stripped) and len(stripped) < 90:
            in_req = True
        bullet = bool(re.match(r"^[-*\u2022\u25cf\u25aa\u2013]|\d+[.)]\s", stripped))
        weighty = in_req or bullet
        for sentence in re.split(r"(?<=[.!?:;])\s+", stripped):
            prev = None
            for i, m in enumerate(TOKEN.finditer(sentence)):
                tok = m.group(0).rstrip(".")
                low = tok.lower()
                if tok in SHORT_NAMES and i > 0:
                    uni[low] += 1
                    forms.setdefault(low, Counter())[tok] += 1
                    techy.add(low)
                    if weighty:
                        req.add(low)
                    prev = None
                    continue
                if low in STOP or low in ignore or len(low) < 2:
                    prev = None
                    continue
                if len(low) == 2 and not tok.isupper() and low not in SYNONYMS:
                    prev = None
                    continue
                uni[low] += 1
                forms.setdefault(low, Counter())[tok] += 1
                if _techy(tok, i == 0):
                    techy.add(low)
                if weighty:
                    req.add(low)
                if prev:
                    bi[(prev, low)] += 1
                prev = low

    scored: dict[str, float] = {}
    # Phrases first, and what they account for comes off their words: the
    # "systems" in "distributed systems" is not a second request for systems.
    left = Counter(uni)
    for (a, b), n in bi.items():
        if n < 2:
            continue
        scored[f"{a} {b}"] = n * 2.5 * (1.5 if a in req or b in req else 1)
        left[a] -= n
        left[b] -= n
    for low, n in left.items():
        # A plain word has to be insisted on; a name of a thing only has to
        # be said.
        if n < (1 if low in techy else 3):
            continue
        scored[low] = n * (2 if low in techy else 1) * (1.5 if low in req else 1)

    def display(term: str) -> str:
        return " ".join(forms[w].most_common(1)[0][0] if w in forms else w
                        for w in term.split(" "))

    top = sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"term": display(t), "key": t, "weight": round(s, 1)} for t, s in top]


def _has(term: str, text: str) -> bool:
    for alt in [term] + SYNONYMS.get(term, []):
        pat = r"(?<![a-z0-9])" + re.escape(alt) + r"(?:s|es)?(?![a-z0-9])"
        if re.search(pat, text):
            return True
    return False


def match(terms: list[dict], cv_text: str) -> dict:
    text = _norm(cv_text)
    found, missing = [], []
    for t in terms:
        (found if _has(t["key"], text) else missing).append(t)
    total = len(terms)
    return {"found": found, "missing": missing, "total": total,
            "rate": round(100 * len(found) / total) if total else None}
