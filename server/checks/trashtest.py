"""Deleting, restoring and renaming: nothing is lost by a click.

    python checks/trashtest.py
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
    ws, J = studio.WORKSPACE, studio.jobstore
    jobs = J.list_jobs(ws)
    j = next(x for x in jobs if x.get("cv_path") and len(x["status_history"]) > 1)
    J.delete_job(ws, j["id"])
    check("a deleted application leaves the list", all(x["id"] != j["id"] for x in J.list_jobs(ws)))
    check("and is in the trash", any(t["id"] == j["id"] for t in J.list_trash(ws)))
    back = J.restore_job(ws, j["id"])
    check("Undo brings it back with its id, history and CV",
          back["id"] == j["id"] and back["status_history"] == j["status_history"]
          and back["cv_path"] == j["cv_path"])
    check("and empties the trash", not J.list_trash(ws))

    cv = j["cv_path"]
    r = studio.rename_document(cv, "renamed cv")
    new = r["path"]
    check("renaming keeps the language suffix of a translation-style name or none",
          new.endswith(".yaml") and "renamed cv" in new, new)
    after = next(x for x in J.list_jobs(ws) if x["id"] == j["id"])
    check("the application follows the rename", after["cv_path"] == new, str(after["cv_path"]))
    tr = next(d for d in studio.list_documents() if d.get("translation_of"))
    r2 = studio.rename_document(tr["path"], "french copy")
    check("a translation keeps its language suffix", r2["path"].endswith("." + tr["lang"] + ".yaml"), r2["path"])
    check("and stays linked to its source",
          any(d["path"] == r2["path"] and d.get("translation_of") == tr["translation_of"]
              for d in studio.list_documents()))
    base = studio.base_cv()["path"]
    try:
        studio.delete_document(base)
        check("the base CV cannot be deleted", False)
    except ValueError:
        check("the base CV cannot be deleted", True)
    r3 = studio.delete_document(new)
    check("a document goes to .trash, not away", (ws / r3["trashed"]).exists(), r3["trashed"])
    after = next(x for x in J.list_jobs(ws) if x["id"] == j["id"])
    check("and its application no longer points at it", after["cv_path"] is None)
    studio.close_sample()
    print()
    print(f"{fails} failure(s)" if fails else "every trash and rename check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
