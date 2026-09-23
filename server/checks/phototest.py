"""Add a photo, show it on a CV, render it, and take it away again.

    python checks/phototest.py

In a throwaway workspace. RenderCV refuses a CV whose photo path does not
exist, so the part worth checking is the round trip: the path each CV writes,
the render with the photo, and every CV still rendering after it is removed.
"""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import studio  # noqa: E402
from cv_render import render_file  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


CV = """cv:
  name: Alex Moreau
  email: alex@example.com
  sections:
    summary:
      - I build infrastructure other engineers do not have to think about.
design:
  theme: classic
"""


def jpeg() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (150, 130, 110)).save(buf, "JPEG", quality=80)
    return buf.getvalue()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        studio.WORKSPACE = Path(tmp)
        (Path(tmp) / "profile").mkdir()
        cv = Path(tmp) / "profile" / "my-cv.yaml"
        cv.write_text(CV, encoding="utf-8")

        print("Saving it")
        check("nothing before there is one", studio.photo_info() is None)
        check("anything but a JPEG is refused", _raises(lambda: studio.save_photo(b"GIF89a")))
        info = studio.save_photo(jpeg())
        check("saved at the workspace root", info and info["path"] == "photo.jpg")
        ref = studio.photo_ref(cv)
        check("a CV in profile/ points at ../photo.jpg", ref == "../photo.jpg", ref)

        print("Showing it")
        studio.apply_patches(cv, [{"path": ["cv", "photo"], "value": ref}])
        out = render_file(cv, Path(tmp) / "out")
        check("RenderCV renders the CV with the photo", bool(out.get("ok")),
              "" if out.get("ok") else (out.get("log") or "")[-300:])

        print("Removing it")
        r = studio.remove_photo()
        check("it comes off the CV that showed it", r["cleared"] == ["profile/my-cv.yaml"])
        check("the file is gone", not (Path(tmp) / "photo.jpg").exists())
        out = render_file(cv, Path(tmp) / "out2")
        check("and the CV still renders", bool(out.get("ok")),
              "" if out.get("ok") else (out.get("log") or "")[-300:])

    print("Where CVs usually go without one")
    for where, want in (("London, UK", True), ("Austin, TX, USA", True), ("Dublin", True),
                        ("Paris, France", False), ("Berlin", False)):
        check(f"{where}: {'noted' if want else 'nothing said'}",
              studio.photo_unusual({"location": where}) == want)

    print()
    print(f"{fails} failure(s)" if fails else "every photo check passes")
    return 1 if fails else 0


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
