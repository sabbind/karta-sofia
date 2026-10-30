"""Адресните точки на ГИС – София (shp, BGS2005) -> имот и сграда от кадастъра (точка в полигон).

Usage: python tools/address_join.py <addresses.shp> <папка с имотите> <папка със сградите>
-> data/addresses.csv.gz
   id; вид; населено място; район; ж.к./местност; улица; номер; блок; вход; имот; сграда; lon; lat
"""
import csv
import gzip
import os
import struct
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from proj import inverse  # noqa: E402
from shpio import read_dbf, read_polygons  # noqa: E402

CELL = 100.0


def read_points(path):
    with open(path, "rb") as f:
        data = f.read()
    off, out = 100, []
    while off < len(data):
        _, clen = struct.unpack(">ii", data[off:off + 8])
        off += 8
        c = data[off:off + clen * 2]
        off += clen * 2
        st = struct.unpack("<i", c[:4])[0]
        out.append(struct.unpack("<2d", c[4:20]) if st in (1, 11, 21) else None)
    return out


def iter_dbf(path, want):
    with open(path, "rb") as f:
        h = f.read(32)
        n, hl, rl = struct.unpack("<IHH", h[4:12])
        fields, pos = [], 1
        while True:
            d = f.read(32)
            if d[0] == 0x0D:
                break
            name, ln = d[:11].split(b"\0")[0].decode("latin1"), d[16]
            fields.append((name, pos, ln))
            pos += ln
        f.seek(hl)
        sel = [x for x in fields if x[0] in want]
        for _ in range(n):
            r = f.read(rl)
            yield {nm: r[a:a + ln].decode("utf-8", "replace").strip() for nm, a, ln in sel}


def find_shps(root):
    for dp, _, fs in os.walk(root):
        for fn in fs:
            if fn.lower().endswith(".shp"):
                yield os.path.join(dp, fn)


def pip(x, y, rings):
    inside = False
    for ring in rings:
        n = len(ring)
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                inside = not inside
            j = i
    return inside


def load_layer(root):
    polys, index = [], defaultdict(list)
    for shp in find_shps(root):
        _, recs = read_dbf(shp[:-4] + ".dbf")
        geoms = read_polygons(shp)
        for r, g in zip(recs, geoms):
            if not g or r["_deleted"]:
                continue
            xs = [p[0] for ring in g for p in ring]
            ys = [p[1] for ring in g for p in ring]
            bb = (min(xs), min(ys), max(xs), max(ys))
            k = len(polys)
            polys.append((r.get("cadnum") or r.get("CADNUM") or "", bb, g))
            for i in range(int(bb[0] // CELL), int(bb[2] // CELL) + 1):
                for j in range(int(bb[1] // CELL), int(bb[3] // CELL) + 1):
                    index[(i, j)].append(k)
    print(root, len(polys), flush=True)
    return polys, index


def locate(x, y, layer):
    polys, index = layer
    for k in index.get((int(x // CELL), int(y // CELL)), ()):
        cid, bb, g = polys[k]
        if bb[0] <= x <= bb[2] and bb[1] <= y <= bb[3] and pip(x, y, g):
            return cid
    return ""


def nearest(x, y, layer, within, maxd=25.0):
    """Най-близката сграда (по ръба) в същия имот, ако точката не е върху сграда."""
    polys, index = layer
    best, bd = "", maxd * maxd
    ci, cj = int(x // CELL), int(y // CELL)
    for i in (ci - 1, ci, ci + 1):
        for j in (cj - 1, cj, cj + 1):
            for k in index.get((i, j), ()):
                cid, bb, g = polys[k]
                if within and not cid.startswith(within + "."):
                    continue
                for ring in g:
                    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
                        dx, dy = x2 - x1, y2 - y1
                        t = max(0, min(1, ((x - x1) * dx + (y - y1) * dy) / (dx * dx + dy * dy or 1)))
                        d = (x1 + t * dx - x) ** 2 + (y1 + t * dy - y) ** 2
                        if d < bd:
                            best, bd = cid, d
    return best


def main():
    shp, pdir, bdir = sys.argv[1:4]
    parcels, buildings = load_layer(pdir), load_layer(bdir)
    pts = read_points(shp)
    want = {"primKey", "n_offic_9", "n_settl_3", "n_region", "n_lArea_4", "Str_name", "streetNum", "blockNum",
            "entrance", "deleted"}
    out = os.path.join(HERE, "..", "data", "addresses.csv.gz")
    stats = defaultdict(int)
    with gzip.open(out, "wt", encoding="utf-8", newline="") as o:
        w = csv.writer(o, delimiter=";")
        w.writerow(["id", "kind", "settlement", "region", "area", "street", "num", "block", "entr",
                    "parcel", "building", "lon", "lat"])
        for r, pt in zip(iter_dbf(shp[:-4] + ".dbf", want), pts):
            if r["deleted"] == "T" or not pt:
                stats["skipped"] += 1
                continue
            x, y = pt
            pcl = locate(x, y, parcels)
            bld = locate(x, y, buildings)
            how = "on"
            if not bld and pcl:
                bld, how = nearest(x, y, buildings, pcl), "near"
            stats["parcel" if pcl else "no_parcel"] += 1
            stats[("building_" + how) if bld else "no_building"] += 1
            lon, lat = inverse(x, y)
            reg = r["n_region"].split(" (")[0]
            area = r["n_lArea_4"].split(" (")[0]
            w.writerow([r["primKey"], r["n_offic_9"], r["n_settl_3"], reg, area, r["Str_name"], r["streetNum"],
                        r["blockNum"], r["entrance"], pcl, bld, f"{lon:.6f}", f"{lat:.6f}"])
    print(dict(stats))


if __name__ == "__main__":
    main()
