"""Simulated job pages and job boards for clipflow.js: CV Studio on a scratch
workspace, the boards' public feeds answered from here, and a server of job
pages as the sites draw them, served as the browser's proxy so each page
keeps its real address. Prints one line of JSON (the ports and key)
when ready, then serves until killed.

    python checks/clipsim.py
"""

from __future__ import annotations

import html
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]


def _free_port() -> int:
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        return so.getsockname()[1]


# Ports of its own, so a copy of the app (or an earlier run) is left alone.
os.environ.setdefault("CVSTUDIO_CLIP_PORT", str(_free_port()))

import posting  # noqa: E402
import studio  # noqa: E402

# --- The VTEX posting, as Greenhouse's feed returns it -------------------------
VTEX_HTML = (
    "<p>VTEX is a global leader in digital commerce technology, providing innovative solutions for "
    "businesses seeking excellence in their online ventures. As an integral part of the Global "
    "Strategic Alliances team, you will serve as the primary technical point of contact for our most "
    "strategic VTEX partners.</p><p><strong>Key Responsibilities:</strong></p><ul>"
    "<li><strong>Technical Advisory:</strong> Serve as the lead technical authority for key partners, "
    "providing expert guidance on platform architecture, best practices and scalability.</li>"
    "<li><strong>Technical Governance:</strong> Oversee the technical health of partner projects, "
    "identifying potential risks and ensuring alignment with performance standards and security "
    "protocols.</li><li><strong>Enablement &amp; Coaching:</strong> Conduct workshops and technical "
    "training sessions so partner developers are proficient in VTEX foundations.</li>"
    "<li><strong>Payment Provider Homologation Process:</strong> Own the payment connectors "
    "homologation process, managing the assessment and prioritization of incoming requests.</li></ul>"
    "<p><strong>Requirements:</strong></p><ul><li>Bachelor's degree in Engineering, Computer Science "
    "or related fields.</li><li>Proven experience with the VTEX platform, especially the Payment "
    "Provider Protocol and Marketplace integrations.</li><li>Expertise in GraphQL and RESTful APIs, "
    "architecting complex integrations between commerce platforms and third-party systems.</li>"
    "<li>Advanced English and Portuguese; Spanish is a plus.</li></ul>")
LEVER_BODY = ("<div>Scaleway builds a sovereign European cloud. " + "We ship infrastructure people trust. " * 30
              + "</div>")
ASHBY_BODY = ("<h2>About the role</h2><p>" + "You will own the developer platform end to end. " * 25 + "</p>"
              "<h2>What you bring</h2><ul><li>Five years with Kubernetes</li><li>Go or Rust</li></ul>")

FEEDS = {
    "https://boards-api.greenhouse.io/v1/boards/vtex/jobs/5856357004": {
        "title": "Partner Technical Account Engineer", "company_name": "VTEX",
        "location": {"name": "São Paulo"}, "content": html.escape(VTEX_HTML)},
    "https://api.lever.co/v0/postings/scaleway/e21a04e5-bad7-4077-9d82-c1b58a8bd4ee": {
        "text": "Internal AI Lead", "categories": {"location": "Paris"}, "description": LEVER_BODY,
        "lists": [{"text": "Requirements", "content": "<li>MCP and n8n</li><li>AI evangelist mindset</li>"}],
        "additional": ""},
    "https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [{
        "id": "0f7c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b", "title": "Staff Platform Engineer",
        "location": "Remote, Europe", "descriptionHtml": ASHBY_BODY}]},
}


def fake_get(url: str, limit: int = 0) -> bytes:
    if url in FEEDS:
        return json.dumps(FEEDS[url]).encode()
    raise OSError(f"no route for {url}")


posting._get = fake_get

# --- Job pages, as the sites draw them ------------------------------------------
LONG = " ".join(["Design, build and run the services our customers rely on every day."] * 20)
APPLY_FORM = ("<form><h2>Apply for this job</h2><label>First Name*</label><input><label>Last Name*"
              "</label><input><label>Resume/CV*</label><button>Attach</button><p>Do you have any "
              "family members currently working here?*</p><p>Privacy Notice: the personal data provided "
              "during this selection process will be used exclusively for evaluating candidates.</p></form>")


def ld(obj) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


PAGES = {
    # Greenhouse's new boards: the posting is drawn by script, no job data.
    ("job-boards.greenhouse.io", "/vtex/jobs/5856357004"):
        "<html><head><title>Job Application for Partner Technical Account Engineer at VTEX</title>"
        "</head><body><div id=app><h1>Partner Technical Account Engineer</h1><p>São Paulo</p>"
        "<div class=job__description>(drawn by script)</div>" + APPLY_FORM + "</div></body></html>",
    # A company site describing its job for search engines, in full.
    ("careers.example.com", "/jobs/chef-de-projet"):
        "<html><head><title>Chef de projet | Example</title>" + ld({"@context": "https://schema.org",
            "@graph": [{"@type": "WebPage"}, {"@type": "JobPosting", "title": "Chef de Projet Digital (H/F)",
            "hiringOrganization": {"@type": "Organization", "name": "Example SA"},
            "jobLocation": {"@type": "Place", "address": {"addressLocality": "Lyon", "addressCountry": "FR"}},
            "baseSalary": {"@type": "MonetaryAmount", "currency": "EUR", "value": {
                "@type": "QuantitativeValue", "minValue": 45000, "maxValue": 55000, "unitText": "YEAR"}},
            "description": "<h2>Vos missions</h2><p>" + LONG + "</p><ul><li>Piloter les projets</li>"
                           "<li>Animer les ateliers</li></ul>"}]}) + "</head><body>Example careers</body></html>",
    # Job data that only carries the first lines, pointing at a Lever posting.
    ("jobs.lever.co", "/scaleway/e21a04e5-bad7-4077-9d82-c1b58a8bd4ee"):
        "<html><head><title>Scaleway - Internal AI Lead</title>" + ld({"@type": "JobPosting",
            "title": "Internal AI Lead", "hiringOrganization": {"name": "Scaleway"},
            "description": "<p>Scaleway builds a sovereign European cloud.</p>"}) +
        "</head><body></body></html>",
    # Ashby: no job data on the page.
    ("jobs.ashbyhq.com", "/acme/0f7c1a2b-3c4d-4e5f-8a9b-0c1d2e3f4a5b"):
        "<html><head><title>Staff Platform Engineer @ Acme</title></head><body><div id=root></div></body></html>",
    # Indeed: read from the page itself.
    ("fr.indeed.com", "/viewjob"):
        "<html><head><title>Data Engineer - Lille - Indeed</title></head><body>"
        '<h1 data-testid="jobsearch-JobInfoHeader-title">Data Engineer - job post</h1>'
        '<div data-testid="inlineHeader-companyName">Decathlon</div>'
        '<div data-testid="inlineHeader-companyLocation">Lille (59)</div>'
        '<div id="jobDescriptionText"><h2>Missions</h2><p>' + LONG + "</p><ul><li>Spark</li><li>dbt</li></ul>"
        "</div></body></html>",
    # LinkedIn: read from the page itself.
    ("www.linkedin.com", "/jobs/view/4012345678/"):
        "<html><head><title>Solutions Architect | Returnista | LinkedIn</title></head><body>"
        '<h1 class="top-card-layout__title">Solutions Architect</h1>'
        '<a class="topcard__org-name-link">Returnista</a>'
        '<span class="topcard__flavor--bullet">Amsterdam, North Holland, Netherlands</span>'
        '<div class="show-more-less-html__markup"><p>' + LONG + "</p></div></body></html>",
    # A page with nothing to read: only what you select.
    ("jobs.example.org", "/opening/42"):
        "<html><head><title>Opening 42</title></head><body><main><h1>Support Engineer</h1><p id=body>"
        + LONG + "</p></main></body></html>",
}


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        # As the browser's proxy, the request line carries the whole address.
        from urllib.parse import urlsplit
        u = urlsplit(self.path)
        host = u.hostname or (self.headers.get("Host") or "").split(":")[0]
        page = PAGES.get((host, u.path))
        body = (page or "<html><body>not found</body></html>").encode()
        self.send_response(200 if page else 404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    pages = ThreadingHTTPServer(("127.0.0.1", 0), Pages)
    threading.Thread(target=pages.serve_forever, daemon=True).start()
    api_port = _free_port()
    sys.argv = ["studio.py", "--port", str(api_port), "--token", "t", "--workspace", tempfile.mkdtemp()]
    threading.Thread(target=studio.main, daemon=True).start()
    import time
    import urllib.request
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{studio.CLIP_PORT}/clip", timeout=1).read()
            break
        except Exception:
            time.sleep(0.1)
    print(json.dumps({"pages": pages.server_address[1], "api": api_port, "clip": studio.CLIP_PORT,
                      "token": "t", "hosts": sorted({h for h, _ in PAGES})}), flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
