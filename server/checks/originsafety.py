"""Other websites cannot reach the app through your browser.

The server listens on your own machine, so the one way in from the outside is
a web page you have open: it can point its domain at 127.0.0.1 (DNS
rebinding) to read the app as its own, or send a form-style request that a
browser lets through without asking. Both are refused; the app's own pages,
the bookmark's window and scripts are not.

    python checks/originsafety.py
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ask(port: int, method: str, path: str, headers: dict, body: bytes | None = None) -> int:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        c.putheader(k, v)
    c.putheader("Content-Length", str(len(body or b"")))
    c.endheaders(body)
    r = c.getresponse()
    r.read()
    c.close()
    return r.status


def main() -> int:
    port, clip = free_port(), free_port()
    data = tempfile.mkdtemp()
    env = dict(os.environ, XDG_DATA_HOME=data, LOCALAPPDATA=data, CVSTUDIO_CLIP_PORT=str(clip))
    proc = subprocess.Popen([sys.executable, str(HERE / "studio.py"), "--port", str(port),
                             "--workspace", tempfile.mkdtemp()], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                if ask(port, "GET", "/api/state", {"Host": f"127.0.0.1:{port}"}) == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        here = f"127.0.0.1:{port}"
        pref = json.dumps({"set": {"x": 1}}).encode()
        J = {"Content-Type": "application/json"}

        check("the app's page opens", ask(port, "GET", "/", {"Host": here}) == 200)
        check("and by localhost", ask(port, "GET", "/", {"Host": f"localhost:{port}"}) == 200)
        check("a rebound domain gets nothing", ask(port, "GET", "/", {"Host": f"evil.example:{port}"}) == 404)
        check("nor the API", ask(port, "GET", "/api/state", {"Host": f"evil.example:{port}"}) == 404)
        check("nor the bookmark's window",
              ask(clip, "GET", "/clip", {"Host": f"attacker.example:{clip}"}) == 404)

        check("the app's own page can write",
              ask(port, "POST", "/api/prefs", {"Host": here, "Origin": f"http://{here}", **J}, pref) == 200)
        check("the bookmark's window can write",
              ask(clip, "POST", "/api/prefs", {"Host": f"127.0.0.1:{clip}",
                                               "Origin": f"http://127.0.0.1:{clip}", **J}, pref) == 200)
        check("a script can write", ask(port, "POST", "/api/prefs", {"Host": here, **J}, pref) == 200)
        check("another site's page cannot",
              ask(port, "POST", "/api/prefs", {"Host": here, "Origin": "https://evil.example", **J}, pref) == 404)
        check("nor another program's page on this machine",
              ask(port, "POST", "/api/prefs", {"Host": here, "Origin": "http://127.0.0.1:3000", **J}, pref) == 404)
        check("a form-style post is refused, whoever sends it",
              ask(port, "POST", "/api/prefs", {"Host": here, "Content-Type": "text/plain"}, pref) == 404)
        check("a sandboxed page (Origin: null) cannot",
              ask(port, "POST", "/api/prefs", {"Host": here, "Origin": "null", **J}, pref) == 404)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    print()
    print(f"{fails} failure(s)" if fails else "no other website can reach the app")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
