"""One CT slice (DICOM) as a windowed greyscale PNG, with Python's standard library alone (4 Nov 2026: CT head
from CQ500, whose slices the node fetches by byte range like any other dataset file).

Enough of DICOM for CT: little-endian, explicit or implicit VR, 16-bit pixels either uncompressed or as
lossless JPEG (process 14, which CQ500 uses: decoded here, below), sequences skipped. Hounsfield units from the rescale slope and intercept, then a window (centre, width): brain
40/80, subdural 75/215, bone 600/2800.
"""

import struct, zlib
from array import array

_LONG = {b"OB", b"OW", b"OF", b"SQ", b"UT", b"UN", b"OD", b"OL", b"OV", b"UC", b"UR"}
WANT = {(0x0020, 0x000D): "study", (0x0020, 0x000E): "series", (0x0008, 0x0018): "sop", (0x0008, 0x103E): "description",
        (0x0018, 0x0050): "thickness", (0x0020, 0x0032): "position", (0x0020, 0x0013): "instance",
        (0x0028, 0x0010): "rows", (0x0028, 0x0011): "cols", (0x0028, 0x0100): "bits", (0x0028, 0x0103): "signed",
        (0x0028, 0x1052): "intercept", (0x0028, 0x1053): "slope", (0x0028, 0x1050): "wc", (0x0028, 0x1051): "ww"}


def parse(b: bytes, pixels=True):
    """The tags above (and, with pixels, where the pixel data is). Stops at the pixel data."""
    p = 132 if b[128:132] == b"DICM" else 0
    out, explicit = {}, True
    while p + 8 <= len(b):
        g, e = struct.unpack_from("<HH", b, p)
        if g != 0x0002 and p and "ts" in out and out["ts"] == "1.2.840.10008.1.2":
            explicit = False
        if g == 0xFFFE:                                    # an item or delimiter at the top level: skip its header
            p += 8; continue
        vr = b[p + 4:p + 6]
        if explicit and vr.isalpha() and vr.isupper():
            if vr in _LONG:
                n, = struct.unpack_from("<I", b, p + 8); h = 12
            else:
                n, = struct.unpack_from("<H", b, p + 6); h = 8
        else:
            vr = None; n, = struct.unpack_from("<I", b, p + 4); h = 8
        v0 = p + h
        if (g, e) == (0x7FE0, 0x0010):
            out["pixel_at"], out["pixel_len"] = v0, n
            break
        if n == 0xFFFFFFFF:                                # undefined length (a sequence): find its end
            depth, q = 1, v0
            while q + 8 <= len(b) and depth:
                gg, ee = struct.unpack_from("<HH", b, q)
                if gg == 0xFFFE and ee == 0xE0DD: depth -= 1; q += 8; continue
                if gg == 0xFFFE: q += 8; continue
                vr2 = b[q + 4:q + 6]
                if explicit and vr2.isalpha() and vr2.isupper():
                    if vr2 in _LONG:
                        m, = struct.unpack_from("<I", b, q + 8); hh = 12
                    else:
                        m, = struct.unpack_from("<H", b, q + 6); hh = 8
                else:
                    m, = struct.unpack_from("<I", b, q + 4); hh = 8
                if m == 0xFFFFFFFF: depth += 1; q += hh; continue
                q += hh + m
            p = q; continue
        val = b[v0:v0 + n]
        if (g, e) == (0x0002, 0x0010):
            out["ts"] = val.decode("ascii", "replace").strip("\x00 ")
        k = WANT.get((g, e))
        if k:
            if k in ("rows", "cols", "bits", "signed"):
                out[k], = struct.unpack("<H", val[:2])
            else:
                out[k] = val.decode("latin-1").strip("\x00 ")
        p = v0 + n
    if not pixels:
        out.pop("pixel_at", None)
    return out


def _fragments(b, at):
    """Encapsulated pixel data: the items after the offset table, joined (one frame)."""
    q, items = at, []
    while q + 8 <= len(b):
        g, e, n = struct.unpack_from("<HHI", b, q)
        if (g, e) == (0xFFFE, 0xE0DD): break
        items.append(b[q + 8:q + 8 + n]); q += 8 + n
    return b"".join(items[1:])


def lossless_jpeg(j: bytes):
    """A one-component lossless JPEG (ITU T.81 process 14), as a flat list of samples, and its width."""
    p, tables, w = 2, {}, 0
    while True:
        while j[p] != 0xFF: p += 1
        m = j[p + 1]; p += 2
        if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7: continue
        n, = struct.unpack_from(">H", j, p); seg = j[p + 2:p + n]
        if m == 0xC3:
            prec, h, w, nc = seg[0], *struct.unpack(">HH", seg[1:5]), seg[5]
            if nc != 1: raise ValueError("more than one component")
        elif m == 0xC4:
            q = 0
            while q < len(seg):
                tid = seg[q] & 0x0F; counts = seg[q + 1:q + 17]; vals = seg[q + 17:q + 17 + sum(counts)]
                code, k, look = 0, 0, {}
                for length in range(1, 17):
                    for _ in range(counts[length - 1]):
                        look[(length, code)] = vals[k]; k += 1; code += 1
                    code <<= 1
                tables[tid] = look; q += 17 + sum(counts)
        elif m == 0xDD:
            raise ValueError("restart intervals aren't handled")
        elif m == 0xDA:
            td = seg[2] >> 4; sel, pt = seg[1 + 2 * seg[0]], seg[3 + 2 * seg[0]] & 0x0F
            p += n; break
        p += n
    # the entropy-coded data, unstuffed (FF 00 -> FF), up to the next marker
    end = len(j); data = bytearray(); q = p
    while q < end:
        c = j[q]
        if c == 0xFF:
            if j[q + 1] == 0x00: data.append(0xFF); q += 2; continue
            break
        data.append(c); q += 1
    look = tables[td]
    # lookup by (length, code) as one dict keyed on code with the length's bit: 1<<length | code
    fast = {(1 << L) | c: v for (L, c), v in look.items()}
    bits, nbits, pos, out = 0, 0, 0, [0] * (w * h)
    mask = (1 << prec) - 1
    first = 1 << (prec - pt - 1)
    dl = len(data)
    for i in range(w * h):
        code = 1
        while True:                                   # one bit at a time into the code, until it's a known one
            if nbits == 0:
                bits = data[pos] if pos < dl else 0; pos += 1; nbits = 8
            nbits -= 1; code = (code << 1) | ((bits >> nbits) & 1)
            v = fast.get(code)
            if v is not None: break
        if v == 0: diff = 0
        elif v == 16: diff = 32768
        else:
            d = 0
            for _ in range(v):
                if nbits == 0:
                    bits = data[pos] if pos < dl else 0; pos += 1; nbits = 8
                nbits -= 1; d = (d << 1) | ((bits >> nbits) & 1)
            diff = d if d >= 1 << (v - 1) else d - (1 << v) + 1
        r, c = divmod(i, w)
        if i == 0: pred = first
        elif r == 0: pred = out[i - 1]
        elif c == 0: pred = out[i - w]
        else:
            a, b_, cc = out[i - 1], out[i - w], out[i - w - 1]
            pred = (a, b_, cc, a + b_ - cc, a + ((b_ - cc) >> 1), b_ + ((a - cc) >> 1), (a + b_) >> 1)[sel - 1]
        out[i] = (pred + diff) & mask
    return out, w


def png(b: bytes, window=(40, 80)) -> bytes:
    """The slice, windowed, as an 8-bit greyscale PNG."""
    t = parse(b)
    rows, cols = t["rows"], t["cols"]
    if t.get("ts") in (None, "1.2.840.10008.1.2", "1.2.840.10008.1.2.1"):
        px = array("h" if t.get("signed") else "H")
        px.frombytes(b[t["pixel_at"]:t["pixel_at"] + rows * cols * 2])
    elif t.get("ts") in ("1.2.840.10008.1.2.4.70", "1.2.840.10008.1.2.4.57"):
        flat, _ = lossless_jpeg(_fragments(b, t["pixel_at"]))
        if t.get("signed"):                              # the stored bits are two's complement
            flat = [v - 65536 if v >= 32768 else v for v in flat]
        px = array("h" if t.get("signed") else "H", flat)
    else:
        raise ValueError(f"compressed DICOM ({t.get('ts')})")
    slope = float((t.get("slope") or "1").split("\\")[0]); icpt = float((t.get("intercept") or "0").split("\\")[0])
    c, w = window
    lo = c - w / 2
    # stored value s -> HU = s*slope + icpt -> grey = (HU - lo) / w * 255, as one lookup over every stored value
    a, k = slope * 255 / w, (icpt - lo) * 255 / w
    lut = bytes(max(0, min(255, int(s * a + k))) for s in (range(-32768, 32768) if t.get("signed") else range(65536)))
    off = 32768 if t.get("signed") else 0
    raw = bytearray()
    for r in range(rows):
        raw.append(0)
        raw += bytes(lut[v + off] for v in px[r * cols:(r + 1) * cols])

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", cols, rows, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))
