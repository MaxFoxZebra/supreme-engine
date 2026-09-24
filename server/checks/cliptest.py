"""Save to CV Studio: the bookmark finds the app at a fixed address, and says
so when it cannot.

    python checks/cliptest.py
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]
os.environ["CVSTUDIO_CLIP_PORT"] = "47899"

import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    bm = studio.bookmarklet()
    check("the bookmark is one line of script", bm.startswith("javascript:") and "\n" not in bm)
    check("with nothing a browser would cut it at", "#" not in bm and not re.search(r"%(?![0-9A-F]{2})", bm))
    check("pointing at the fixed port", "127.0.0.1:47899" in bm)
    check("reading the job the page describes", "JobPosting" in bm and "ld+json" in bm)
    studio.start_clip_listener()
    check("the app listens there", studio.CLIP_STATE["ok"], studio.CLIP_STATE["why"])
    page = urllib.request.urlopen("http://127.0.0.1:47899/clip", timeout=5).read().decode()
    check("and serves the bookmark's window", "Save to CV Studio" in page and "cvstudio-clip-ready" in page)
    check("with nothing left to fill in", "__API_TOKEN__" not in page and "__PREFS__" not in page)
    studio.start_clip_listener()
    check("a second copy says the port is taken", not studio.CLIP_STATE["ok"]
          and "in use" in studio.CLIP_STATE["why"], studio.CLIP_STATE["why"])
    print()
    print(f"{fails} failure(s)" if fails else "every bookmark check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
