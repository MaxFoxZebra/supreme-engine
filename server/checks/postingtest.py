"""Reading a posting from its link: each board's own record, shaped as that
board publishes it, and the job a page describes for search engines. No
network: the requests are answered from here.

    python checks/postingtest.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import posting  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


LEVER = {
    "id": "e21a04e5-bad7-4077-9d82-c1b58a8bd4ee",
    "text": "Internal AI Lead",
    "categories": {"location": "Paris", "commitment": "Permanent", "team": "IT"},
    "workplaceType": "hybrid",
    "description": "<div><b>About Scaleway</b></div><div>We build a <b>sovereign</b> cloud.</div>",
    "lists": [{"text": "What you will do",
               "content": "<li>Lead AI adoption across teams</li><li>Build agents with MCP and n8n</li>"},
              {"text": "Requirements",
               "content": "<li>A strong AI evangelist mindset</li><li>Vibe coding and AI-assisted development</li>"}],
    "additional": "<div>A background check is part of SecNumCloud.</div>",
}
GREENHOUSE = {
    "id": 6148352004, "title": "Technical Account Manager - France",
    "company_name": "Dataiku", "location": {"name": "Paris, France"},
    "content": "&lt;h2&gt;&lt;strong&gt;The role&lt;/strong&gt;&lt;/h2&gt;&lt;p&gt;Help customers "
               "succeed with &lt;a href=&quot;https://www.dataiku.com&quot;&gt;Dataiku&lt;/a&gt;.&lt;/p&gt;"
               "&lt;ul&gt;&lt;li&gt;Own the relationship&lt;/li&gt;&lt;/ul&gt;",
}
PAGE_LD = """<html><head><title>Chef de projet | Valtech</title>
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage"},
{"@type":"JobPosting","title":"Chef de Projet (H/F)","hiringOrganization":{"@type":"Organization","name":"Valtech"},
"jobLocation":{"@type":"Place","address":{"addressLocality":"Paris","addressCountry":"FR"}},
"description":"&lt;p&gt;Vous piloterez des projets &lt;strong&gt;digitaux&lt;/strong&gt;.&lt;/p&gt;"}]}</script>
</head><body>AI evangelist mindset appears in the text, not the title.</body></html>"""
PAGE_EMBED = """<html><body><div id="grnhse_app"></div>
<script src="https://boards.greenhouse.io/embed/job_board/js?for=valtech"></script></body></html>"""
PAGE_NOTHING = "<html><head><title>Careers</title></head><body>Join us</body></html>"

ROUTES = {
    "https://api.lever.co/v0/postings/scaleway/e21a04e5-bad7-4077-9d82-c1b58a8bd4ee": json.dumps(LEVER),
    "https://boards-api.greenhouse.io/v1/boards/dataiku/jobs/6148352004": json.dumps(GREENHOUSE),
    "https://boards-api.greenhouse.io/v1/boards/valtech/jobs/4765692101":
        json.dumps({**GREENHOUSE, "title": "Retail IT Delivery Domain Manager", "company_name": "Valtech"}),
    "https://www.valtech.com/fr-fr/carrieres/4670848101/": PAGE_LD,
    "https://www.valtech.com/career/jobs/4765692101/": PAGE_EMBED,
    "https://example.com/careers/1": PAGE_NOTHING,
}
asked: list[str] = []


def fake_get(url: str, limit: int = 0) -> bytes:
    asked.append(url)
    if url not in ROUTES:
        raise OSError(f"no route for {url}")
    return ROUTES[url].encode()


def main() -> int:
    posting._get = fake_get

    p = posting.read("https://jobs.lever.co/scaleway/e21a04e5-bad7-4077-9d82-c1b58a8bd4ee")
    check("Lever: the title the company gave", p["title"] == "Internal AI Lead", p["title"])
    check("Lever: read from its public feed", asked[-1].startswith("https://api.lever.co/v0/postings/"))
    check("Lever: every list kept, under its heading",
          "## What you will do" in p["description"] and "- Build agents with MCP and n8n" in p["description"]
          and "## Requirements" in p["description"], p["description"][:120].replace("\n", " | "))
    check("Lever: the closing text too", "SecNumCloud" in p["description"])
    check("Lever: bold kept", "**sovereign**" in p["description"])
    check("Lever: location", p["location"] == "Paris", str(p["location"]))

    p = posting.read("https://job-boards.greenhouse.io/dataiku/jobs/6148352004")
    check("Greenhouse: title and company", (p["title"], p["company"]) ==
          ("Technical Account Manager - France", "Dataiku"), f"{p['title']} / {p['company']}")
    check("Greenhouse: escaped HTML read as Markdown",
          p["description"].startswith("## The role") and "[Dataiku](https://www.dataiku.com)" in p["description"]
          and "- Own the relationship" in p["description"], p["description"][:100].replace("\n", " | "))

    p = posting.read("https://www.valtech.com/fr-fr/carrieres/4670848101/")
    check("a company page: the JobPosting it describes", p["title"] == "Chef de Projet"
          and p["company"] == "Valtech" and p["location"] == "Paris, FR", f"{p['title']} / {p['location']}")
    check("who may apply is left out of the title, as everywhere else", "H/F" not in p["title"])
    check("a company page: its description as Markdown", "**digitaux**" in p["description"],
          p["description"])

    p = posting.read("https://www.valtech.com/career/jobs/4765692101/")
    check("a page embedding a Greenhouse board: that board's record",
          p["title"] == "Retail IT Delivery Domain Manager" and p["via"] == "Greenhouse", p["title"])

    for bad, why in (("https://example.com/careers/1", "a page with no job data"),
                     ("ftp://example.com/x", "a link that is not the web"),
                     ("https://jobs.lever.co/scaleway/00000000-0000-0000-0000-000000000000", "a posting taken down")):
        try:
            posting.read(bad)
            check(f"refused: {why}", False, "it returned something")
        except Exception as exc:  # noqa: BLE001
            check(f"refused: {why}", True, str(exc)[:70])

    md = posting.to_markdown("<p>One</p><p></p><ul><li><p>Two</p></li></ul><script>x()</script>")
    check("empty blocks and scripts leave nothing behind", md == "One\n\n- Two", repr(md))

    print("\nposting reader works" if not fails else f"\n{fails} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
