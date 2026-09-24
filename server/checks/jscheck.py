"""Syntax-check every script in the served page with node, before anything runs.

    python checks/jscheck.py

The interface is one HTML string with the scripts inline, so a stray quote in
one of them leaves a page that loads and then does nothing, which no Python
check notices. node --check reads each script the way the browser would.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import studio  # noqa: E402


def main() -> int:
    page = studio.INDEX_HTML.replace("__PREFS__", "null").replace("__API_TOKEN__", '"t"')
    scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
    # Save to CV Studio's window, and the bookmark itself.
    clip = studio.CLIP_HTML.replace("__PREFS__", "null").replace("__API_TOKEN__", '"t"')
    scripts += re.findall(r"<script>(.*?)</script>", clip, re.S)
    scripts.append(studio.bookmarklet()[len("javascript:"):])
    bad = 0
    with tempfile.TemporaryDirectory() as tmp:
        for i, body in enumerate(scripts):
            f = Path(tmp) / f"script-{i}.js"
            f.write_text(body, encoding="utf-8")
            r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
            ok = r.returncode == 0
            print(f"  {'ok  ' if ok else 'FAIL'}  inline script {i + 1} of {len(scripts)}"
                  f" ({len(body) // 1024} KB)")
            if not ok:
                bad += 1
                print("        " + r.stderr.strip().replace("\n", "\n        ")[:1200])
    for f in sorted((HERE / "static").glob("*.js")):
        if f.name.endswith(".min.js"):
            continue
        r = subprocess.run(["node", "--check", str(f)], capture_output=True, text=True)
        print(f"  {'ok  ' if r.returncode == 0 else 'FAIL'}  static/{f.name}")
        bad += r.returncode != 0
    print()
    print(f"{bad} script(s) do not parse" if bad else "every script parses")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
