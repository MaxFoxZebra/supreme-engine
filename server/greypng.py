"""A page image for a model, small enough to pass through its client.

The page render_cv shows the model was Typst's PNG: four bytes a pixel, in
colour, about 145,000 characters of base64 for one dense page at 96 ppi.
Claude passes a tool result over about 150,000 characters on only as a file
when code execution is on, and a view bound to that tool then never gets its
data: the CV page never appeared in the chat, only the image's thumbnail.

The same page in sixteen levels of grey is about a quarter of the size, and
reads the same: a page is black text on white, and its colours are accents.
Standard library only (zlib), since the app ships without an image library.
"""

from __future__ import annotations

import struct
import zlib

_SIG = b"\x89PNG\r\n\x1a\n"


def _chunks(png: bytes):
    pos = len(_SIG)
    while pos + 8 <= len(png):
        n, = struct.unpack(">I", png[pos:pos + 4])
        yield png[pos + 4:pos + 8], png[pos + 8:pos + 8 + n]
        pos += 12 + n


def _add(x: int, y: int, lo: int, hi: int) -> int:
    """Byte-wise addition modulo 256 of two rows held as big integers."""
    return ((x & lo) + (y & lo)) ^ ((x ^ y) & hi)


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[bytes]:
    """The scanlines of an 8-bit PNG, filters undone.

    Up and Sub, which are most of a page, run on whole rows as big integers;
    Average and Paeth, byte by byte.
    """
    stride = width * bpp
    lo = int.from_bytes(b"\x7f" * stride, "big")
    hi = int.from_bytes(b"\x80" * stride, "big")
    prev = bytes(stride)
    rows = []
    pos = 0
    for _ in range(height):
        f = raw[pos]
        cur = raw[pos + 1:pos + 1 + stride]
        pos += 1 + stride
        if f == 0:
            row = cur
        elif f == 2:
            row = _add(int.from_bytes(cur, "big"), int.from_bytes(prev, "big"), lo, hi).to_bytes(stride, "big")
        elif f == 1:
            # Each byte adds the decoded one a pixel before it: a running sum
            # per channel, done by doubling shifts.
            x, shift = int.from_bytes(cur, "big"), bpp * 8
            while shift < stride * 8:
                x = _add(x, x >> shift, lo, hi)
                shift *= 2
            row = x.to_bytes(stride, "big")
        elif f == 3:
            out = bytearray(cur)
            for i in range(stride):
                left = out[i - bpp] if i >= bpp else 0
                out[i] = (out[i] + ((left + prev[i]) >> 1)) & 255
            row = bytes(out)
        elif f == 4:
            out = bytearray(cur)
            for i in range(bpp):
                out[i] = (out[i] + prev[i]) & 255
            for i in range(bpp, stride):
                a, b, c = out[i - bpp], prev[i], prev[i - bpp]
                pa, pb = b - c, a - c
                pc = pa + pb
                pa, pb, pc = pa if pa >= 0 else -pa, pb if pb >= 0 else -pb, pc if pc >= 0 else -pc
                out[i] = (out[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            row = bytes(out)
        else:
            raise ValueError(f"unknown PNG filter {f}")
        rows.append(row)
        prev = row
    return rows


def grey16(png: bytes) -> bytes | None:
    """`png` as a 4-bit greyscale PNG, on white; None if it is not an 8-bit
    RGB or RGBA PNG without interlacing, the only kind Typst writes."""
    if not png.startswith(_SIG):
        return None
    head, idat = None, []
    for kind, body in _chunks(png):
        if kind == b"IHDR":
            head = struct.unpack(">IIBBBBB", body[:13])
        elif kind == b"IDAT":
            idat.append(body)
    if not head:
        return None
    width, height, depth, colour, _, _, interlace = head
    if depth != 8 or colour not in (2, 6) or interlace:
        return None
    bpp = 4 if colour == 6 else 3
    try:
        rows = _unfilter(zlib.decompress(b"".join(idat)), width, height, bpp)
    except (zlib.error, ValueError, IndexError):
        return None
    def level(p: bytes) -> int:
        """Luma from 0..255 to 0..15 for (r, g, b[, a]), composited on white."""
        y = (299 * p[0] + 587 * p[1] + 114 * p[2]) // 1000
        if bpp == 4:
            y = (y * p[3] + 255 * (255 - p[3])) // 255
        return (y * 15 + 127) // 255

    # Two pixels make a byte, looked up by their bytes rather than computed: a
    # page has few distinct colours. A blank row, most of a page, is one test.
    white = b"\xff" * bpp
    span = 2 * bpp
    stride = width * bpp
    if width % 2:
        stride += bpp
    blank = b"\xff" * stride
    blank_line = b"\x00" + b"\xff" * (stride // span)
    pairs: dict[bytes, int] = {}
    packed = bytearray()
    for row in rows:
        if width % 2:
            row += white
        if row == blank:
            packed += blank_line
            continue
        line = bytearray(1 + stride // span)
        for j, i in enumerate(range(0, stride, span), 1):
            key = row[i:i + span]
            b = pairs.get(key)
            if b is None:
                b = pairs[key] = level(key[:bpp]) << 4 | level(key[bpp:])
            line[j] = b
        packed += line

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

    return (_SIG + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 4, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(packed), 9)) + chunk(b"IEND", b""))
