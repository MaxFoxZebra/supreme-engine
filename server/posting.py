"""A job posting, read from its link on this machine.

The job boards most companies hire through publish each posting as data as
well as a page: Lever, Greenhouse, Ashby and SmartRecruiters have public
feeds, and most other pages describe the job for search engines
(schema.org JobPosting). Reading that, rather than a summary of the page,
gives the title and the text exactly as the company wrote them.

Nothing here needs a key or an account, and the request goes to the board or
the company's site and nowhere else.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import urllib.request
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

UA = "Mozilla/5.0 (compatible; CV Studio posting reader)"
LIMIT = 3 * 1024 * 1024


def _get(url: str, limit: int = LIMIT) -> bytes:
    if urlparse(url).scheme not in ("http", "https"):
        raise ValueError("not http")
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json, text/html;q=0.9, */*;q=0.5"})
    with urllib.request.urlopen(req, timeout=12) as r:  # noqa: S310 -- scheme checked
        body = r.read(limit + 1)
    if len(body) > limit:
        raise ValueError("too large")
    return body


def _json(url: str):
    return json.loads(_get(url).decode("utf-8", errors="replace"))


# --- HTML to Markdown ---------------------------------------------------------

class _Md(HTMLParser):
    """Headings, paragraphs, lists, bold and links; everything else as text."""
    BLOCK = {"p", "div", "section", "article", "br", "tr", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.depth = 0
        self.skip = 0
        self.href: list[str | None] = []

    def _nl(self, n=2):
        text = "".join(self.out)
        have = len(text) - len(text.rstrip("\n"))
        if text.strip():
            self.out.append("\n" * max(0, n - have))

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self._nl()
            self.out.append("## ")
        elif tag in ("ul", "ol"):
            self.depth += 1
            self._nl(1 if self.depth > 1 else 2)
        elif tag == "li":
            self._nl(1)
            self.out.append("  " * max(0, self.depth - 1) + "- ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "a":
            self.href.append(dict(attrs).get("href"))
            self.out.append("[")
        elif tag == "br":
            self._nl(1)
        elif tag in self.BLOCK:
            self._nl()

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag):
            self._nl()
        elif tag in ("ul", "ol"):
            self.depth = max(0, self.depth - 1)
            self._nl()
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag == "a":
            href = self.href.pop() if self.href else None
            self.out.append(f"]({href})" if href and href.startswith("http") else "]")
        elif tag in self.BLOCK - {"br"}:
            self._nl()

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def to_markdown(fragment: str) -> str:
    """A posting's HTML as the Markdown the app stores."""
    p = _Md()
    p.feed(fragment or "")
    p.close()
    md = "".join(p.out)
    md = re.sub(r"\*\*\s*\*\*", "", md)
    md = re.sub(r"\[([^\]]*)\](?!\()", r"\1", md)
    md = re.sub(r"(?m)^## \*\*(.+?)\*\*\s*$", r"## \1", md)
    md = re.sub(r"(?m)^(- |## )\s+", r"\1", md)
    md = re.sub(r"(?m)[ \t]+$", "", md)
    md = re.sub(r"(?m)^(##|-)\s*$\n?", "", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# --- The boards ---------------------------------------------------------------

def _lever(u) -> dict | None:
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", u.path)
    if not (u.hostname or "").endswith("lever.co") or not m:
        return None
    api = "api.eu.lever.co" if ".eu." in (u.hostname or "") else "api.lever.co"
    d = _json(f"https://{api}/v0/postings/{m.group(1)}/{m.group(2)}")
    parts = [d.get("description") or ""]
    for block in d.get("lists") or []:
        parts.append(f"<h3>{htmllib.escape(block.get('text') or '')}</h3><ul>{block.get('content') or ''}</ul>")
    parts.append(d.get("additional") or "")
    cat = d.get("categories") or {}
    return {"title": d.get("text"), "company": None, "location": cat.get("location"),
            "description": to_markdown("".join(parts)), "via": "Lever",
            "workplace": d.get("workplaceType")}


def _greenhouse_job(board: str, job_id: str) -> dict:
    d = _json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}")
    return {"title": d.get("title"), "company": d.get("company_name"),
            "location": (d.get("location") or {}).get("name"),
            "description": to_markdown(htmllib.unescape(d.get("content") or "")),
            "via": "Greenhouse"}


def _greenhouse(u) -> dict | None:
    host = u.hostname or ""
    if not host.endswith("greenhouse.io"):
        return None
    m = re.match(r"/([^/]+)/jobs/(\d+)", u.path)
    if m:
        return _greenhouse_job(m.group(1), m.group(2))
    q = parse_qs(u.query)
    if q.get("for") and q.get("token"):
        return _greenhouse_job(q["for"][0], q["token"][0])
    return None


def _ashby(u) -> dict | None:
    m = re.match(r"/([^/]+)/([0-9a-f-]{36})", u.path)
    if u.hostname != "jobs.ashbyhq.com" or not m:
        return None
    d = _json(f"https://api.ashbyhq.com/posting-api/job-board/{m.group(1)}")
    job = next((j for j in d.get("jobs") or [] if j.get("id") == m.group(2)), None)
    if not job:
        raise ValueError("This posting is no longer on the company's Ashby board.")
    return {"title": job.get("title"), "company": None, "location": job.get("location"),
            "description": to_markdown(job.get("descriptionHtml") or ""), "via": "Ashby"}


def _smartrecruiters(u) -> dict | None:
    m = re.match(r"/([^/]+)/(\d+)", u.path)
    if u.hostname != "jobs.smartrecruiters.com" or not m:
        return None
    d = _json(f"https://api.smartrecruiters.com/v1/companies/{m.group(1)}/postings/{m.group(2)}")
    sec = ((d.get("jobAd") or {}).get("sections") or {})
    body = "".join(f"<h2>{htmllib.escape(s.get('title') or '')}</h2>{s.get('text') or ''}"
                   for s in sec.values() if isinstance(s, dict))
    loc = d.get("location") or {}
    return {"title": d.get("name"), "company": (d.get("company") or {}).get("name"),
            "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
            "description": to_markdown(body), "via": "SmartRecruiters"}


def _find_jobposting(o):
    if isinstance(o, list):
        for x in o:
            r = _find_jobposting(x)
            if r:
                return r
        return None
    if not isinstance(o, dict):
        return None
    if o.get("@graph"):
        return _find_jobposting(o["@graph"])
    types = o.get("@type")
    return o if "JobPosting" in (types if isinstance(types, list) else [types]) else None


def _place(j: dict) -> str | None:
    locs = j.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs] if locs else []
    out = []
    for loc in locs:
        a = (loc or {}).get("address") if isinstance(loc, dict) else loc
        if isinstance(a, str):
            out.append(a)
        elif isinstance(a, dict):
            out.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry"))
                                 if isinstance(x, str) and x))
    out = [x for x in dict.fromkeys(out) if x]
    return " · ".join(out) or None


def _page(url: str) -> dict:
    """Any other page: the job it describes for search engines, or, on a
    company site that embeds a Greenhouse board, that board's own record."""
    page = _get(url).decode("utf-8", errors="replace")
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        try:
            j = _find_jobposting(json.loads(block.strip()))
        except ValueError:
            continue
        if j and j.get("title"):
            org = j.get("hiringOrganization")
            return {"title": htmllib.unescape(str(j["title"])),
                    "company": org.get("name") if isinstance(org, dict) else org,
                    "location": _place(j),
                    "description": to_markdown(htmllib.unescape(str(j.get("description") or ""))),
                    "via": "the page's job data"}
    gh = re.search(r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)([\w-]+)", page)
    job_id = parse_qs(urlparse(url).query).get("gh_jid", [None])[0] or \
        (re.search(r"/(\d{7,})(?:/|$)", urlparse(url).path) or [None, None])[1]
    if gh and job_id:
        return _greenhouse_job(gh.group(1), job_id)
    raise ValueError("This page does not describe its job as data, so the title and text "
                     "cannot be read exactly. Ask the user to paste the posting, or to save "
                     "it with the Save to CV Studio bookmark.")


def read(url: str) -> dict:
    """{title, company, location, description, via, url}: the posting at `url`,
    as its board or page publishes it."""
    url = (url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("A posting's link starts with http:// or https://.")
    out = None
    try:
        for reader in (_lever, _greenhouse, _ashby, _smartrecruiters):
            out = reader(u)
            if out:
                break
        if not out:
            out = _page(url)
    except ValueError:
        raise
    except Exception as exc:  # the network, a 404, a board that changed shape
        code = getattr(exc, "code", None)
        raise ValueError(f"Could not read the posting from {u.hostname}: " +
                         ("the board no longer has it (404)." if code == 404 else
                          f"{type(exc).__name__}: {exc}")) from None
    if not (out.get("title") or "").strip():
        raise ValueError("The posting was found but has no title.")
    # "(H/F)", "m/w/d" and the like say who may apply, not what the job is:
    # the app leaves them out of titles wherever it reads one.
    out["title"] = re.sub(r"\s*[([]?\b(?:[hfmwdx](?:\s*/\s*[hfmwdx]){1,2}|all genders)\b[)\]]?", "",
                          re.sub(r"\s+", " ", out["title"]), flags=re.I).strip(" -–|,")
    out["url"] = url
    return out
