"""Build sofia-map.pmtiles (MVT, gzip) with layers `buildings` and `parcels`,
plus attribute CSVs, from AGKK open-data shapefiles (BGS2005).

Usage: python3 -I build_map.py <buildings_dir> <parcels_dir> <out_dir>
Each dir holds one sub-folder per district with the .shp/.dbf/.shx/.prj files.
Pure Python (no GIS libraries needed).
"""
import csv
import glob
import gzip
import json
import math
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shpio import read_dbf, read_polygons  # noqa: E402
import proj  # noqa: E402

BLD_DIR, PCL_DIR, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(OUT, exist_ok=True)

EKATTE_PREFIX = "68134."  # гр. София
EXTENT = 4096
MAXZOOM = 16


def num(v, cast=int):
    try:
        return cast(float(v))
    except (TypeError, ValueError):
        return 0


def bld_props(r):
    area = num(r["AREA"], float)
    small = {"id": r["cadnum"], "fc": num(r["funccode"]), "fl": num(r["flrcount"])}
    full = {"type": r["functype"], "addr": r["immaddr"], "area": round(area, 1), "units": num(r["appcount"])}
    row = {
        "cadnum": r["cadnum"], "region": r["regname"], "functype": r["functype"],
        "funccode": r["funccode"], "floors": r["flrcount"], "units": r["appcount"],
        "area_m2": round(area, 1), "address": r["immaddr"], "street": r["strename"],
        "strnum": r["strnum"], "quarter_name": r["quarname"], "oldident": r["oldident"],
        "proptype": r["proptype"],
    }
    return small, full, row, area


def pcl_props(r):
    area = num(r["AREA"], float)
    small = {"id": r["cadnum"], "uc": num(r["usecode"]), "pc": num(r["purpcode"])}
    full = {"use": r["usetype"], "addr": r["immaddr"], "area": round(area, 1),
            "kv": r["quarter"], "upi": r["parcel"]}
    row = {
        "cadnum": r["cadnum"], "region": r["regname"], "usetype": r["usetype"],
        "usecode": r["usecode"], "purptype": r["purptype"], "proptype": r["proptype"],
        "area_m2": round(area, 1), "address": r["immaddr"], "street": r["strename"],
        "strnum": r["strnum"], "quarter": r["quarter"], "upi": r["parcel"],
        "place": r["place"], "quarter_name": r["quarname"], "oldident": r["oldident"],
    }
    return small, full, row, area


LAYERS = [
    {"name": "parcels", "dir": PCL_DIR, "props": pcl_props, "minzoom": 14,
     "min_area": {14: 0, 15: 0, 16: 0}, "full_from": 15, "csv": "sofia-parcels-attributes.csv"},
    {"name": "buildings", "dir": BLD_DIR, "props": bld_props, "minzoom": 12,
     "min_area": {12: 150, 13: 40, 14: 0, 15: 0, 16: 0}, "full_from": 15,
     "csv": "sofia-buildings-attributes.csv"},
]


def merc(lon, lat):
    x = (lon + 180.0) / 360.0
    s = math.sin(math.radians(lat))
    return x, 0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)


bounds = [1e9, 1e9, -1e9, -1e9]

# ---------------------------------------------------------------- load
for L in LAYERS:
    feats, rows = [], []
    for dbf in sorted(glob.glob(os.path.join(L["dir"], "*", "*", "*.dbf"))):
        _, recs = read_dbf(dbf, encoding="utf-8")
        geoms = read_polygons(dbf[:-4] + ".shp")
        assert len(geoms) == len(recs), dbf
        for rings, r in zip(geoms, recs):
            if r["_deleted"] or not rings or not r["cadnum"].startswith(EKATTE_PREFIX):
                continue
            small, full, row, area = L["props"](r)
            wr = []
            for ring in rings:
                pts = []
                for x, y in ring:
                    lon, lat = proj.inverse(x, y)
                    bounds[0], bounds[1] = min(bounds[0], lon), min(bounds[1], lat)
                    bounds[2], bounds[3] = max(bounds[2], lon), max(bounds[3], lat)
                    pts.append(merc(lon, lat))
                wr.append(pts)
            xs = [p[0] for p in wr[0]]
            ys = [p[1] for p in wr[0]]
            feats.append((wr, small, full, area, (min(xs), min(ys), max(xs), max(ys))))
            rows.append(row)
    L["feats"] = feats
    print(L["name"], len(feats), flush=True)
    with open(os.path.join(OUT, L["csv"]), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


# ---------------------------------------------------------------- geometry helpers
def signed_area(ring):
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return s / 2


def point_in_ring(pt, ring):
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


# ---------------------------------------------------------------- protobuf / MVT
def varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def zz(n):
    return (n << 1) ^ (n >> 31)


def key(field, wt):
    return varint((field << 3) | wt)


def ld(field, payload):
    return key(field, 2) + varint(len(payload)) + payload


def packed(field, ints):
    return ld(field, b"".join(varint(i) for i in ints))


def encode_value(v):
    if isinstance(v, str):
        return ld(1, v.encode("utf-8"))
    if isinstance(v, float):
        return key(3, 1) + struct.pack("<d", v)
    if v >= 0:
        return key(5, 0) + varint(v)
    return key(6, 0) + varint(((v << 1) ^ (v >> 63)) & 0xFFFFFFFFFFFFFFFF)


def tile_geometry(wrings, z, tx, ty):
    scale = (1 << z) * EXTENT
    rings = []
    for ring in wrings:
        q = []
        for x, y in ring:
            p = (round(x * scale - tx * EXTENT), round(y * scale - ty * EXTENT))
            if not q or q[-1] != p:
                q.append(p)
        if len(q) > 1 and q[0] == q[-1]:
            q.pop()
        if len(q) < 3:
            continue
        a = signed_area(q + [q[0]])
        if a == 0:
            continue
        rings.append((q, a))
    if not rings:
        return None
    outers = [q for q, a in rings if a > 0]
    holes = [q for q, a in rings if a < 0]
    if not outers:
        q, a = max(rings, key=lambda r: abs(r[1]))
        outers, holes = [q[::-1]], []
    groups = [[o] for o in outers]
    for h in holes:
        for g in groups:
            if point_in_ring(h[0], g[0] + [g[0][0]]):
                g.append(h)
                break
    cmds, cx, cy = [], 0, 0
    for g in groups:
        for q in g:
            x0, y0 = q[0]
            cmds += [9, zz(x0 - cx), zz(y0 - cy)]
            cx, cy = x0, y0
            cmds.append(2 | ((len(q) - 1) << 3))
            for x, y in q[1:]:
                cmds += [zz(x - cx), zz(y - cy)]
                cx, cy = x, y
            cmds.append(15)
    return cmds


def encode_layer(name, z, tx, ty, feats, full_from):
    keys, kidx, vals, vidx, fbufs = [], {}, [], {}, []
    for fid, (wr, small, full, area, bb) in feats:
        geom = tile_geometry(wr, z, tx, ty)
        if geom is None:
            continue
        p = dict(small)
        if z >= full_from:
            p.update(full)
        tags = []
        for k, v in p.items():
            if v == "" or v is None:
                continue
            if k not in kidx:
                kidx[k] = len(keys)
                keys.append(k)
            vk = (type(v).__name__, v)
            if vk not in vidx:
                vidx[vk] = len(vals)
                vals.append(v)
            tags += [kidx[k], vidx[vk]]
        fb = key(1, 0) + varint(fid) + packed(2, tags) + key(3, 0) + varint(3) + packed(4, geom)
        fbufs.append(ld(2, fb))
    if not fbufs:
        return b""
    layer = ld(1, name.encode()) + b"".join(fbufs)
    layer += b"".join(ld(3, k.encode()) for k in keys)
    layer += b"".join(ld(4, encode_value(v)) for v in vals)
    layer += key(5, 0) + varint(EXTENT) + key(15, 0) + varint(2)
    return ld(3, layer)


# ---------------------------------------------------------------- PMTiles v3
def zxy_to_id(z, x, y):
    acc = ((1 << (2 * z)) - 1) // 3
    n = 1 << z
    d = 0
    s = n >> 1
    while s > 0:
        rx = 1 if (x & s) else 0
        ry = 1 if (y & s) else 0
        d += s * s * ((3 * rx) ^ ry)
        if ry == 0:
            if rx == 1:
                x, y = n - 1 - x, n - 1 - y
            x, y = y, x
        s >>= 1
    return acc + d


def serialize_dir(entries):
    b = bytearray(varint(len(entries)))
    last = 0
    for e in entries:
        b += varint(e[0] - last)
        last = e[0]
    for e in entries:
        b += varint(e[1])
    for e in entries:
        b += varint(e[3])
    for i, e in enumerate(entries):
        b += varint(0) if i > 0 and e[2] == entries[i - 1][2] + entries[i - 1][3] else varint(e[2] + 1)
    return gzip.compress(bytes(b), mtime=0)


# ---------------------------------------------------------------- build
minzoom = min(L["minzoom"] for L in LAYERS)
tiles = {}
for z in range(minzoom, MAXZOOM + 1):
    n = 1 << z
    per_tile = defaultdict(dict)  # (tx,ty) -> {layer: [feats]}
    buf = 64 / EXTENT
    for L in LAYERS:
        if z < L["minzoom"]:
            continue
        for fid, f in enumerate(L["feats"]):
            if f[3] < L["min_area"][z]:
                continue
            x0, y0, x1, y1 = f[4]
            for tx in range(int(x0 * n - buf), int(x1 * n + buf) + 1):
                for ty in range(int(y0 * n - buf), int(y1 * n + buf) + 1):
                    per_tile[(tx, ty)].setdefault(L["name"], []).append((fid + 1, f))
    sizes = []
    for (tx, ty), layers in per_tile.items():
        data = b""
        for L in LAYERS:  # parcels first, buildings on top
            if L["name"] in layers:
                data += encode_layer(L["name"], z, tx, ty, layers[L["name"]], L["full_from"])
        if not data:
            continue
        gz = gzip.compress(data, compresslevel=9, mtime=0)
        tiles[zxy_to_id(z, tx, ty)] = gz
        sizes.append(len(gz))
    print(f"z{z}: {len(sizes)} tiles, max {max(sizes)/1024:.0f} KB, total {sum(sizes)/1e6:.1f} MB", flush=True)

entries, blob, seen = [], bytearray(), {}
for tid in sorted(tiles):
    data = tiles[tid]
    if data in seen:
        off, ln = seen[data]
    else:
        off, ln = len(blob), len(data)
        blob += data
        seen[data] = (off, ln)
    if entries and entries[-1][0] + entries[-1][1] == tid and entries[-1][2] == off and entries[-1][3] == ln:
        entries[-1][1] += 1
    else:
        entries.append([tid, 1, off, ln])

root, leaves = serialize_dir(entries), b""
if 127 + len(root) > 16384:
    leaf_size = 4096
    while True:
        rootents, lb = [], bytearray()
        for i in range(0, len(entries), leaf_size):
            chunk = entries[i:i + leaf_size]
            s = serialize_dir(chunk)
            rootents.append([chunk[0][0], 0, len(lb), len(s)])
            lb += s
        root = serialize_dir(rootents)
        if 127 + len(root) <= 16384:
            leaves = bytes(lb)
            break
        leaf_size *= 2

meta = json.dumps({
    "name": "Сгради и поземлени имоти – гр. София (АГКК)",
    "attribution": "Сгради и имоти: © АГКК, отворени данни",
    "vector_layers": [
        {"id": "buildings", "minzoom": 12, "maxzoom": MAXZOOM,
         "fields": {"id": "String", "fc": "Number", "fl": "Number", "type": "String",
                    "addr": "String", "area": "Number", "units": "Number"}},
        {"id": "parcels", "minzoom": 14, "maxzoom": MAXZOOM,
         "fields": {"id": "String", "uc": "Number", "pc": "Number", "use": "String",
                    "addr": "String", "area": "Number", "kv": "String", "upi": "String"}},
    ],
}, ensure_ascii=False).encode("utf-8")
meta_gz = gzip.compress(meta, mtime=0)

e7 = lambda v: int(round(v * 1e7))  # noqa: E731
root_off = 127
meta_off = root_off + len(root)
leaf_off = meta_off + len(meta_gz)
data_off = leaf_off + len(leaves)
header = b"PMTiles" + bytes([3])
header += struct.pack("<11Q", root_off, len(root), meta_off, len(meta_gz), leaf_off, len(leaves),
                      data_off, len(blob), len(tiles), len(entries), len(seen))
header += bytes([1, 2, 2, 1, minzoom, MAXZOOM])
header += struct.pack("<iiii", *(e7(v) for v in bounds))
header += bytes([13]) + struct.pack("<ii", e7(23.3219), e7(42.6977))
assert len(header) == 127
out = os.path.join(OUT, "sofia-map.pmtiles")
with open(out, "wb") as f:
    f.write(header + root + meta_gz + leaves + bytes(blob))
print("bounds", bounds)
print("pmtiles", out, round(os.path.getsize(out) / 1e6, 1), "MB; tiles", len(tiles), "leaves", len(leaves))
