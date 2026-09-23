"""Sample data: built in its own folder, full, and the real workspace untouched.

    python checks/sampletest.py
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
    real = Path(tempfile.mkdtemp()) / "ws"
    studio.WORKSPACE = real
    studio.bootstrap(real)
    before = sorted(p.name for p in real.rglob("*"))
    r = studio.open_sample()
    check("switched to the sample folder", studio.in_sample() and studio.WORKSPACE != real)
    check("about sixty applications", r["applications"] >= 60, repr(r))
    jobs = studio.jobstore.list_jobs(studio.WORKSPACE)
    check("every status there is", {j["status"] for j in jobs} >= {
        "pending", "applied", "interviewing", "offer", "rejected", "ghosted"})
    check("Spanish and Portuguese applications", {"es", "pt"} <= {j["language"] for j in jobs})
    langs = {t["lang"] for t in studio.translations_of(studio.WORKSPACE / "profile/my-cv.yaml")}
    check("the base CV in fr, es and pt", langs == {"fr", "es", "pt"}, repr(langs))
    check("tailored CVs and letters", r["tailored"] >= 4 and r["letters"] >= 3, repr(r))
    bare = sorted({j["company"] for j in jobs if not studio.logo_url(j.get("logo"))})
    check("every company has its logo", not bare, ", ".join(bare))
    alerts = studio.jobstore.alerts(studio.WORKSPACE)["counts"]
    check("something needs attention", alerts["followup_due"] + alerts["interview_soon"] > 0)
    letter = next(p for p in (studio.WORKSPACE / "letters").glob("*.md"))
    check("a letter renders", studio.render_letter(letter).get("ok"))
    studio.open_sample()
    check("opening it again starts over", len(studio.jobstore.list_jobs(studio.WORKSPACE)) == len(jobs))
    studio.close_sample()
    check("back to the real workspace", studio.WORKSPACE == real)
    check("which was never written to", sorted(p.name for p in real.rglob("*")) == before)
    print()
    print(f"{fails} failure(s)" if fails else "every sample check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
