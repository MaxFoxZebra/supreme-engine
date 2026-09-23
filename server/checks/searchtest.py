"""Search: what is inside the documents, found the way you would type it.

    python checks/searchtest.py
"""

from __future__ import annotations

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
    studio.open_sample()
    hits = studio.search_documents("kubernetes")
    check("words inside a CV are found", len(hits) > 0, str(len(hits)))
    check("with the line they are on", all("kubernetes" in h["line"].lower() for h in hits),
          repr(hits[0]["line"] if hits else None))
    check("a list item reads as its words, not its dash",
          all(not h["line"].startswith("- ") for h in hits))
    check("case does not matter", len(studio.search_documents("KUBERNETES")) == len(hits))
    fr = studio.search_documents("ingenieur plateforme")
    check("accents do not matter, and every word must be there",
          any(h["path"].endswith(".fr.yaml") for h in fr), str([h["path"] for h in fr]))
    check("a word that is nowhere finds nothing", studio.search_documents("zeppelin") == [])
    check("nothing typed, nothing found", studio.search_documents("   ") == [])
    check("never more than eight", len(studio.search_documents("e")) <= 8)
    letters = studio.search_documents("Dear Hiring Team")
    check("letters are searched too", any(h["path"].endswith(".md") for h in letters),
          str([h["path"] for h in letters]))
    studio.close_sample()
    print()
    print(f"{fails} failure(s)" if fails else "every search check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
