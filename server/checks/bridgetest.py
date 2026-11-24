"""The Desktop <-> MCP bridge: in-process embedded Studio server and actor isolation.

    python checks/bridgetest.py

Verifies that:
  1. `open_studio` starts CV Studio's HTTP interface in-process on a daemon
     thread (milliseconds, no duplicate Python subprocess), behind a per-server
     key that never reaches the model.
  2. The modular static assets (static/css/* and static/js/*) are served cleanly
     and unauthenticated /api/* calls are refused with 401.
  3. Actor identity is strictly isolated via contextvars: an MCP tool edit is
     attributed to the AI client ("claude"), while an edit made through the
     embedded HTTP server in the same process is attributed to "you".
  4. Closing the MCP connector immediately stops the embedded HTTP server.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "checks"))
from mcpclient import Client  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def http_req(url: str, *, method: str = "GET", headers: dict | None = None,
             body: dict | None = None) -> tuple[int, bytes]:
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bridge-test-"))
    ws = tmp / "ws"
    ws.mkdir()
    os.environ["CVSTUDIO_DATA_DIR"] = str(tmp / "appdata")

    c = Client(sys.executable, [
        str(HERE / "server_main.py"), "--mcp",
        "--workspace", str(ws), "--client", "claude",
    ])
    try:
        # Handshake advertising MCP Apps UI extension so open_studio activates.
        init = c.send("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}},
            "clientInfo": {"name": "claude-ai", "version": "1.0"},
        })
        check("handshake succeeds with MCP Apps UI extension", "result" in init)
        c.send("notifications/initialized", {}, notify=True)

        # Verify studio resource declares its CSP frame domain.
        res = c.send("resources/read", {"uri": "ui://cv-studio/studio.html"})["result"]
        csp = ((res["contents"][0].get("_meta") or {}).get("ui") or {}).get("csp") or {}
        frame_domains = csp.get("frameDomains") or []
        check("studio view declares one loopback frame domain", len(frame_domains) == 1, str(frame_domains))

        # Call open_studio and time it (in-process daemon thread -> fast!).
        t0 = time.monotonic()
        r = c.send("tools/call", {"name": "open_studio", "arguments": {"path": "profile/my-cv.yaml"}})["result"]
        dt_ms = (time.monotonic() - t0) * 1000
        sc = r.get("structuredContent") or {}
        url = sc.get("url") or ""
        check("open_studio succeeds in-process", not r.get("isError") and bool(url), f"{dt_ms:.0f}ms")
        check("token stays out of model text", not any("token=" in (x.get("text") or "") for x in r.get("content") or []))

        parsed = urllib.parse.urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        token = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        check("served origin matches declared CSP frame domain", origin == frame_domains[0], origin)

        # Auth & modular static assets.
        st_unauth, _ = http_req(origin + "/api/state")
        check("unauthenticated /api/state is refused (401)", st_unauth == 401, str(st_unauth))

        st_page, html = http_req(url)
        html_text = html.decode("utf-8", errors="replace")
        check("index.html served with modular CSS & JS",
              st_page == 200 and "/static/css/tokens.css" in html_text and "/static/js/core.js" in html_text)

        st_css, _ = http_req(origin + "/static/css/tokens.css")
        st_js, _ = http_req(origin + "/static/js/core.js")
        check("modular CSS and JS assets return 200", st_css == 200 and st_js == 200, f"css={st_css} js={st_js}")

        # Actor isolation: 1) AI edit via MCP tool -> attributed to "claude"
        ai_res = c.send("tools/call", {
            "name": "edit_cv_fields",
            "arguments": {
                "path": "profile/my-cv.yaml",
                "edits": [{"path": ["cv", "headline"], "value": "Staff Distributed Systems Engineer"}],
            },
        })["result"]
        check("MCP edit_cv_fields succeeds", not ai_res.get("isError"))

        # Actor isolation: 2) User edit via embedded HTTP server -> attributed to "you"!
        st_save, save_raw = http_req(
            origin + "/api/save",
            method="POST",
            headers={"X-API-Key": token, "Origin": origin},
            body={
                "path": "profile/my-cv.yaml",
                "patches": [{"path": ["cv", "location"], "value": "Paris, France"}],
            },
        )
        check("embedded HTTP /api/save succeeds", st_save == 200, str(st_save))

        # Inspect provenance recorded in .cvstudio-edits.json
        edits = json.loads((ws / ".cvstudio-edits.json").read_text(encoding="utf-8"))
        doc_edits = (edits.get("docs") or {}).get("profile/my-cv.yaml", {}).get("edits") or []
        by_field = {".".join(map(str, e["field"])): e["by"] for e in doc_edits}
        check("MCP edit is attributed to claude", by_field.get("cv.headline") == "claude", str(by_field))
        check("embedded HTTP edit is attributed to you (not claude!)",
              by_field.get("cv.location") == "you", str(by_field))

        # Inspect history attribution too
        st_hist, hist_raw = http_req(
            origin + "/api/history?path=profile/my-cv.yaml",
            headers={"X-API-Key": token},
        )
        hist = json.loads(hist_raw)
        whos = [v.get("who") for v in hist.get("versions") or []]
        check("history records both claude and you separately",
              st_hist == 200 and "claude" in whos and "you" in whos, str(whos))
    finally:
        c.close()

    # After MCP exits, the daemon embedded server must stop immediately.
    gone = False
    for _ in range(20):
        try:
            http_req(origin + "/api/pulse", headers={"X-API-Key": token})
            time.sleep(0.1)
        except OSError:
            gone = True
            break
    check("embedded HTTP server exits immediately with MCP connector", gone)

    print()
    print(f"{fails} failure(s)" if fails else "every desktop-MCP bridge check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
