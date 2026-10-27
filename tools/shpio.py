"""Minimal pure-Python shapefile (Polygon) + DBF reader."""
import struct


def read_dbf(path, encoding="utf-8"):
    with open(path, "rb") as f:
        data = f.read()
    nrec, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while data[pos] != 0x0D:
        name = data[pos:pos + 11].split(b"\0")[0].decode("ascii", "replace")
        ftype = chr(data[pos + 11])
        flen = data[pos + 16]
        fields.append((name, ftype, flen))
        pos += 32
    recs = []
    off = hlen
    for _ in range(nrec):
        rec = data[off:off + rlen]
        off += rlen
        deleted = rec[0:1] == b"*"
        p = 1
        row = {}
        for name, ftype, flen in fields:
            raw = rec[p:p + flen]
            p += flen
            row[name] = raw.decode(encoding, "replace").strip()
        row["_deleted"] = deleted
        recs.append(row)
    return fields, recs


def read_polygons(path):
    """Yield list of rings (list of (x,y)) per record (None for null shapes)."""
    with open(path, "rb") as f:
        data = f.read()
    shptype = struct.unpack("<i", data[32:36])[0]
    assert shptype in (5, 15, 25), f"not polygon: {shptype}"
    off = 100
    out = []
    n = len(data)
    while off < n:
        _, clen = struct.unpack(">ii", data[off:off + 8])
        off += 8
        content = data[off:off + clen * 2]
        off += clen * 2
        st = struct.unpack("<i", content[:4])[0]
        if st == 0:
            out.append(None)
            continue
        nparts, npts = struct.unpack("<ii", content[36:44])
        parts = struct.unpack(f"<{nparts}i", content[44:44 + 4 * nparts])
        pstart = 44 + 4 * nparts
        coords = struct.unpack(f"<{2 * npts}d", content[pstart:pstart + 16 * npts])
        pts = list(zip(coords[0::2], coords[1::2]))
        rings = []
        for i, s in enumerate(parts):
            e = parts[i + 1] if i + 1 < nparts else npts
            rings.append(pts[s:e])
        out.append(rings)
    return out
