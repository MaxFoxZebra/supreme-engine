"""A stand-in MCP Apps host, to see CV Studio's views working in a browser.

It does what a client such as Claude Desktop does with a tool that names a
view: start the CV Studio MCP server, call the tool, read the ui:// view, show
it in a sandboxed frame, speak MCP Apps with it over postMessage, and relay
what the view calls (tools/call, resources/read) to the server.

    python checks/appshost.py --workspace DIR [--port 8790]
    then open http://127.0.0.1:8790/?tool=render_cv&args={"path":"profile/my-cv.yaml"}

window.HOST in the page records what the view sent the host (model context,
downloads), for a test to read. The frame is given allow-same-origin so a test
can click inside it; a real host would not.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

PAGE = r"""<!doctype html><meta charset="utf-8"><title>Host</title>
<style>body{margin:0;padding:16px;background:#ece9e1;font:13px system-ui}
iframe{width:760px;border:1px solid #ccc;border-radius:10px;background:#fff;display:block}</style>
<div id="where"></div><iframe id="f" sandbox="allow-scripts allow-same-origin"></iframe>
<script>
const P = new URLSearchParams(location.search);
const TOOL = P.get("tool"), ARGS = JSON.parse(P.get("args") || "{}");
window.HOST = { context: [], downloads: [], calls: [], ready: false };
const rpc = (method, params) => fetch("/rpc", { method: "POST", body: JSON.stringify({ method, params }) }).then(r => r.json());
const f = document.getElementById("f");
let result = null;
const toView = m => f.contentWindow.postMessage({ jsonrpc: "2.0", ...m }, "*");
window.addEventListener("message", async e => {
  if (e.source !== f.contentWindow) return;
  const m = e.data;
  if (m.method === "ui/initialize") {
    toView({ id: m.id, result: { protocolVersion: "2026-01-26", hostInfo: { name: "test-host", version: "1" },
      hostCapabilities: { serverTools: {}, serverResources: {}, downloadFile: {}, openLinks: {} },
      hostContext: { theme: P.get("theme") || "light", displayMode: "inline" } } });
  } else if (m.method === "ui/notifications/initialized") {
    toView({ method: "ui/notifications/tool-input", params: { arguments: ARGS } });
    toView({ method: "ui/notifications/tool-result", params: result });
    HOST.ready = true;
  } else if (m.method === "ui/notifications/size-changed") {
    f.style.height = Math.min(m.params.height + 4, 1400) + "px";
  } else if (m.method === "ui/update-model-context") {
    HOST.context.push(m.params); toView({ id: m.id, result: {} });
  } else if (m.method === "ui/download-file") {
    HOST.downloads.push(m.params); toView({ id: m.id, result: {} });
  } else if (m.method === "tools/call" || m.method === "resources/read") {
    HOST.calls.push(m.params.name || m.params.uri);
    const r = await rpc(m.method, m.params);
    toView(r.error ? { id: m.id, error: r.error } : { id: m.id, result: r.result });
  } else if (m.id != null) {
    toView({ id: m.id, result: {} });
  }
});
(async () => {
  const tools = (await rpc("tools/list", {})).result.tools;
  const uri = ((tools.find(t => t.name === TOOL) || {})._meta || {}).ui.resourceUri;
  document.getElementById("where").textContent = TOOL + " → " + uri;
  result = (await rpc("tools/call", { name: TOOL, arguments: ARGS })).result;
  f.srcdoc = (await rpc("resources/read", { uri })).result.contents[0].text;
})();
</script>"""


class Server:
    def __init__(self, py: str, workspace: str):
        self.p = subprocess.Popen([py, "server_main.py", "--mcp", "--workspace", workspace], cwd=HERE,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.lock, self.n = threading.Lock(), 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test-host", "version": "1"},
                                    "capabilities": {"extensions": {"io.modelcontextprotocol/ui": {
                                        "mimeTypes": ["text/html;profile=mcp-app"]}}}})
        self._write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _write(self, m):
        self.p.stdin.write((json.dumps(m) + "\n").encode())
        self.p.stdin.flush()

    def request(self, method, params):
        with self.lock:
            self.n += 1
            self._write({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params})
            for line in self.p.stdout:
                if line.strip():
                    m = json.loads(line)
                    if m.get("id") == self.n:
                        return m


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--port", type=int, default=8790)
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()
    mcp = Server(a.python, a.workspace)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(PAGE.encode(), "text/html; charset=utf-8")

        def do_POST(self):
            m = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self._send(json.dumps(mcp.request(m["method"], m.get("params") or {})).encode(), "application/json")

    ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
