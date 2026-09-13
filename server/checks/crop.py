"""Crop and zoom a PNG with nothing but the stdlib, so screenshots can be read
at a size where design decisions are actually visible."""
import struct, sys, zlib


def read(path):
    raw = open(path, "rb").read()
    pos, idat, w = 8, b"", None
    while pos < len(raw):
        ln = struct.unpack(">I", raw[pos:pos + 4])[0]
        typ = raw[pos + 4:pos + 8]
        body = raw[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, color = struct.unpack(">IIBB", body[:10])
        elif typ == b"IDAT":
            idat += body
        pos += 12 + ln
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color] * depth // 8
    data, stride, rows, prev, at = zlib.decompress(idat), w * bpp, [], bytearray(w * bpp), 0
    for _ in range(h):
        f = data[at]; at += 1
        line = bytearray(data[at:at + stride]); at += stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 1: line[i] = (line[i] + a) & 255
            elif f == 2: line[i] = (line[i] + b) & 255
            elif f == 3: line[i] = (line[i] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if (pa <= pb and pa <= pc) else b if pb <= pc else c)) & 255
        rows.append(bytes(line)); prev = line
    return w, h, bpp, rows


def write(path, w, h, bpp, rows):
    kind = {1: 0, 2: 4, 3: 2, 4: 6}[bpp]
    raw = b"".join(b"\x00" + r for r in rows)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    open(path, "wb").write(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, kind, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


if __name__ == "__main__":
    src, dst, x, y, cw, ch = sys.argv[1], sys.argv[2], *map(int, sys.argv[3:7])
    zoom = int(sys.argv[7]) if len(sys.argv) > 7 else 1
    w, h, bpp, rows = read(src)
    x, y = max(0, x), max(0, y)
    cw, ch = min(cw, w - x), min(ch, h - y)
    out = []
    for r in rows[y:y + ch]:
        line = r[x * bpp:(x + cw) * bpp]
        if zoom > 1:
            line = b"".join(line[i:i + bpp] * zoom for i in range(0, len(line), bpp))
        out += [line] * zoom
    write(dst, cw * zoom, ch * zoom, bpp, out)
    print(f"{dst}  {cw * zoom}x{ch * zoom}")
