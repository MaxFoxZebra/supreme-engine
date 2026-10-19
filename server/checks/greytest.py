"""The grey page image the model gets, against Pillow's reading of the same.

    python checks/greytest.py

greypng undoes PNG's filters itself, so a slip there is a garbled page the
model reads as the CV, with no error anywhere. Each of the five filters on
colour and on alpha, odd widths, and what it must refuse.
"""

from __future__ import annotations

import io
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import greypng  # noqa: E402

fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global fails
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}{'  -> ' + detail if detail else ''}")
    fails += not ok


def _png(img, filters: list[int]) -> bytes:
    """`img` as a PNG with each row under the filter given for it."""
    import struct
    import zlib
    bpp = len(img.mode)
    w, h = img.size
    data = img.tobytes()
    stride = w * bpp
    prev = bytes(stride)
    out = bytearray()
    for y in range(h):
        cur = data[y * stride:(y + 1) * stride]
        f = filters[y]
        row = bytearray(stride)
        for i in range(stride):
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 0:
                pred = 0
            elif f == 1:
                pred = a
            elif f == 2:
                pred = b
            elif f == 3:
                pred = (a + b) // 2
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
            row[i] = (cur[i] - pred) & 255
        out += bytes([f]) + row
        prev = cur

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6 if bpp == 4 else 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(out))) + chunk(b"IEND", b""))


def main() -> int:
    from PIL import Image

    def grey(img):
        img = img.convert("RGBA")
        page = Image.new("RGBA", img.size, (255, 255, 255, 255))
        page.alpha_composite(img)
        return list(page.convert("L").tobytes())

    rng = random.Random(1)
    for mode in ("RGB", "RGBA"):
        for w, h in ((1, 1), (7, 5), (64, 33), (301, 97)):
            img = Image.new(mode, (w, h), (255,) * len(mode))
            px = img.load()
            for _ in range(w * h // 2):
                px[rng.randrange(w), rng.randrange(h)] = tuple(rng.randrange(256) for _ in mode)
            for x in range(w):
                for y in range(0, h, 3):
                    px[x, y] = tuple((x * 3 + y * 5 + k * 40) % 256 for k in range(len(mode)))
            # Each row under a filter of its own, all five in turn: Pillow
            # writes only None and Sub.
            got = greypng.grey16(_png(img, [y % 5 for y in range(h)]))
            if got is None:
                check(f"{mode} {w}x{h}: made", False)
                continue
            got = Image.open(io.BytesIO(got))
            # One step of sixteen is 17 of 255; rounding takes it to 10.
            worst = max(abs(a - b) for a, b in zip(grey(img), got.convert("L").tobytes()))
            check(f"{mode} {w}x{h}, every filter: the same page in sixteen greys",
                  got.size == (w, h) and worst <= 10, f"off by {worst} at most")

    try:
        import typst
    except ImportError:  # pragma: no cover
        typst = None
    if typst is not None:
        import tempfile
        with tempfile.TemporaryDirectory() as t:
            doc = Path(t) / "page.typ"
            doc.write_text('#set text(fill: rgb("#1f4e9c"))\n= Heading\n' + "Some words on a page. " * 200,
                           encoding="utf-8")
            png = typst.compile(str(doc), format="png", ppi=96)
            png = png if isinstance(png, bytes) else png[0]
        got = greypng.grey16(png)
        worst = 255 if got is None else max(
            abs(a - b) for a, b in zip(grey(Image.open(io.BytesIO(png))),
                                       Image.open(io.BytesIO(got)).convert("L").tobytes()))
        check("a page Typst wrote, a quarter of its size or less",
              got is not None and worst <= 10 and len(got) * 4 <= len(png),
              f"{len(png)} -> {len(got or b'')} bytes, off by {worst} at most")

    buf = io.BytesIO()
    Image.new("L", (10, 10)).save(buf, "PNG")
    check("refuses what it does not read, for the caller to send as it is",
          greypng.grey16(buf.getvalue()) is None and greypng.grey16(b"not a png") is None)
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), (10, 20, 30)).save(buf, "PNG")
    check("and a cut-off file", greypng.grey16(buf.getvalue()[:60]) is None)

    print()
    print(f"{fails} failure(s)" if fails else "every grey page check passes")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
