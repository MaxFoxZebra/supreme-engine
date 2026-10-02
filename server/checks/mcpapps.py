"""MCP Apps, from the host's side: the views it reads, the tools that name
them, and what the views call. Speaks the handshake-era protocol by hand, as
a host that declares the io.modelcontextprotocol/ui extension does.

    python checks/mcpapps.py [python] [workspace]
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
fails = 0


def check(name: str, ok: bool, detail="") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + str(detail) if detail else ''}")
    fails += not ok


def main(py: str, ws: str) -> int:
    p = subprocess.Popen([py, "server_main.py", "--mcp", "--workspace", ws], cwd=HERE,
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
    q: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [q.put(json.loads(line)) for line in p.stdout if line.strip()],
                     daemon=True).start()
    n = 0

    def send(m):
        p.stdin.write((json.dumps(m) + "\n").encode())
        p.stdin.flush()

    def call(method, params=None):
        nonlocal n
        n += 1
        send({"jsonrpc": "2.0", "id": n, "method": method, "params": params or {}})
        while True:
            m = q.get(timeout=240)
            if m.get("id") == n:
                return m

    def tool(name, args=None):
        return call("tools/call", {"name": name, "arguments": args or {}})["result"]

    try:
        call("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "host", "version": "1"},
                            "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {
                                "mimeTypes": ["text/html;profile=mcp-app"]}}}})
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        tools = {t["name"]: t for t in call("tools/list")["result"]["tools"]}
        ui = {n: (t.get("_meta") or {}).get("ui") for n, t in tools.items() if (t.get("_meta") or {}).get("ui")}
        check("render_cv, show_themes and review_changes name their views",
              {k: v.get("resourceUri") for k, v in ui.items() if v.get("resourceUri")} == {
                  "render_cv": "ui://cv-studio/page.html", "show_themes": "ui://cv-studio/themes.html",
                  "review_changes": "ui://cv-studio/review.html"}, ui)
        check("the views' own tools are offered to a host with views, for the views only",
              ui.get("preview_image") == {"visibility": ["app"]} and ui.get("resolve_change") == {"visibility": ["app"]})

        for view in ("page", "themes", "review"):
            c = call("resources/read", {"uri": f"ui://cv-studio/{view}.html"})["result"]["contents"][0]
            html = c.get("text") or ""
            check(f"the {view} view is served as an MCP App", c.get("mimeType") == "text/html;profile=mcp-app"
                  and "{{SHARED}}" not in html and "ui/initialize" in html and "<script src" not in html)

        tool("render_cv", {"path": "profile/my-cv.yaml"})
        r = tool("preview_image", {"path": "profile/my-cv.yaml", "page": 1})
        check("a rendered page comes back for the page view",
              not r.get("isError") and r["structuredContent"]["png"].startswith("iVBOR"))

        r = tool("show_themes", {"path": "profile/my-cv.yaml", "themes": ["bold", "airy"]})
        tiles = r["structuredContent"]["themes"]
        check("show_themes renders each theme from a copy", [t["ok"] for t in tiles] == [True, True]
              and r["structuredContent"]["current"] != "bold", tiles)
        check("and carries no pictures to the model", "iVBOR" not in json.dumps(r))
        r = tool("preview_image", {"path": "profile/my-cv.yaml", "theme": "bold"})
        check("the view fetches each theme's page itself", r["structuredContent"]["png"].startswith("iVBOR"))

        tool("add_job", {"company": "Acme", "title": "Engineer", "status": "applied"})
        tool("edit_cv_fields", {"path": "profile/my-cv.yaml",
                                "edits": [{"path": ["cv", "headline"], "value": "Changed by AI"}]})
        r = tool("review_changes")["structuredContent"]
        check("review_changes lists the document and the application",
              [d["path"] for d in r["documents"]] == ["profile/my-cv.yaml"]
              and [a["kind"] for a in r["applications"]] == ["add"], r["summary"])
        tool("resolve_change", {"kind": "document", "id": "profile/my-cv.yaml", "action": "undo"})
        tool("resolve_change", {"kind": "application", "id": r["applications"][0]["id"], "action": "keep"})
        after = tool("review_changes")["structuredContent"]
        cv = tool("read_cv", {"path": "profile/my-cv.yaml"})["content"][0]["text"]
        check("Undo from the view puts the document back", "Changed by AI" not in cv)
        check("and leaves nothing waiting, not even a record of the user's own decision",
              not after["documents"] and not after["applications"], after["summary"])
    finally:
        p.kill()
    print(f"\n{fails} failure(s)" if fails else "\nevery MCP Apps check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else sys.executable,
                  sys.argv[2] if len(sys.argv) > 2 else tempfile.mkdtemp()))
