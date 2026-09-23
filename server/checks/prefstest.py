"""Preferences: kept in a file beside the app, and handed to the page.

    python checks/prefstest.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import studio  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    check("nothing saved yet", studio.load_prefs() is None)
    studio.save_prefs({"appearance": "dark", "tz": "Europe/Paris"})
    studio.save_prefs({"notify": True})
    p = studio.load_prefs()
    check("changes merge", p == {"appearance": "dark", "tz": "Europe/Paris", "notify": True}, repr(p))
    studio.save_prefs({"ui_lang": "fr"}, replace=True)
    check("replace swaps them all", studio.load_prefs() == {"ui_lang": "fr"})
    check("the file is outside any workspace", studio.prefs_path().parent.name.lower().startswith("cv"))
    raw = json.dumps(studio.load_prefs()).replace("</", "<\\/")
    page = studio.INDEX_HTML.replace("__PREFS__", raw)
    check("the page carries them", 'var _p={"ui_lang": "fr"};' in page)
    studio.save_prefs({"x": "</script><b>"})
    raw = json.dumps(studio.load_prefs()).replace("</", "<\\/")
    check("a value cannot close the script", "</script><b>" not in raw)
    print()
    print(f"{fails} failure(s)" if fails else "every preference check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
