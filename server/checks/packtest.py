"""Application pack: the CV and the letter as one PDF, or a zip.

    python checks/packtest.py
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp()
os.environ["LOCALAPPDATA"] = os.environ["XDG_DATA_HOME"]

import studio  # noqa: E402
from pypdf import PdfReader  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def main() -> int:
    studio.open_sample()
    jobs = studio.jobstore.list_jobs(studio.WORKSPACE)
    both = next(j for j in jobs if j.get("cv_path") and j.get("letter_path"))
    info = studio.pack_info(both["id"])
    check("it knows there is a CV and a letter", info["cv"] and info["letter"])
    check("the name says whose, where and what",
          info["name"].startswith("Alex-Moreau-") and "Engineer" in info["name"], info["name"])

    data, ctype, fname = studio.application_pack(both["id"], "pdf")
    pages = len(PdfReader(io.BytesIO(data)).pages)
    cv_pages = len(PdfReader(str(studio.current_pdf(studio.safe_path(both["cv_path"]))[0])).pages)
    check("one PDF holds the CV and then the letter", ctype == "application/pdf" and pages == cv_pages + 1,
          f"{pages} pages, CV {cv_pages}")
    check("named for the application", fname == info["name"] + ".pdf", fname)

    data, ctype, fname = studio.application_pack(both["id"], "zip", "My file.zip", posting=True)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    check("a zip keeps them apart", ctype == "application/zip"
          and "Alex-Moreau-CV.pdf" in names and "Alex-Moreau-Cover-letter.pdf" in names, str(names))
    check("with the posting when asked and there is one",
          ("posting.md" in names) == info["posting"], str(names))
    check("a chosen name is kept, made safe", fname == "My-file.zip", fname)

    cv_only = next(j for j in jobs if j.get("cv_path") and not j.get("letter_path"))
    data, ctype, fname = studio.application_pack(cv_only["id"], "pdf")
    check("just a CV comes out as that CV", ctype == "application/pdf" and data[:4] == b"%PDF")

    bare = next(j for j in jobs if not j.get("cv_path") and not j.get("letter_path"))
    try:
        studio.application_pack(bare["id"])
        check("nothing to export is said so", False)
    except ValueError as exc:
        check("nothing to export is said so", "no CV or letter" in str(exc), str(exc))
    studio.close_sample()
    print()
    print(f"{fails} failure(s)" if fails else "every pack check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
