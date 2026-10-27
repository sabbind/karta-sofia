"""Build static search indexes for the map (served next to index.html).

Usage: python3 -I build_search.py <buildings_dir> <parcels_dir> <site_dir>
Writes:
  <site>/idx/addr.json        addresses (street + number) and streets
  <site>/idx/id/<reg>.json    bbox per cadastral id, one file per cadastral region
"""
import glob
import json
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shpio import read_dbf, read_polygons  # noqa: E402
import proj  # noqa: E402

BLD, PCL, SITE = sys.argv[1], sys.argv[2], sys.argv[3]
IDX = os.path.join(SITE, "idx")
os.makedirs(os.path.join(IDX, "id"), exist_ok=True)


def r6(v):
    return round(v, 6)


def union(a, b):
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


ids = defaultdict(dict)          # reg -> {rest: [bbox, layer]}
addrs = {}                       # norm key -> {"s","n","r","bb"}
streets = {}                     # norm key -> {"s","r","bb"}


def norm_street(s):
    s = re.sub(r"[\"„“”']", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


PREFIX = re.compile(r"^(ул|бул|пл|ж\.?\s?к|кв|м|в\.?\s?з)(\.\s*|\s+)", re.I)


def street_key(s):
    k = PREFIX.sub("", s.lower())
    k = re.sub(r"[.,\-]", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def better(a, b):
    """True if label b is nicer than a (has a type prefix, not all caps)."""
    score = lambda t: (bool(PREFIX.match(t)), t != t.upper())  # noqa: E731
    return score(b) > score(a)


for kind, root in (("b", BLD), ("p", PCL)):
    for dbf in sorted(glob.glob(os.path.join(root, "*", "*", "*.dbf"))):
        _, recs = read_dbf(dbf)
        geoms = read_polygons(dbf[:-4] + ".shp")
        for rings, r in zip(geoms, recs):
            cid = r["cadnum"]
            if r["_deleted"] or not rings or not cid.startswith("68134."):
                continue
            xs, ys = zip(*rings[0])
            (lo0, la0), (lo1, la1) = proj.inverse(min(xs), min(ys)), proj.inverse(max(xs), max(ys))
            # LCC is conformal & nearly axis-aligned over Sofia: corner transform is accurate enough for a bbox
            lo_a, la_a = proj.inverse(min(xs), max(ys))
            lo_b, la_b = proj.inverse(max(xs), min(ys))
            bb = [r6(min(lo0, lo_a, lo1, lo_b)), r6(min(la0, la_a, la1, la_b)),
                  r6(max(lo0, lo_a, lo1, lo_b)), r6(max(la0, la_a, la1, la_b))]
            parts = cid.split(".")
            reg, rest = parts[1], ".".join(parts[2:])
            prev = ids[reg].get(rest)
            ids[reg][rest] = [union(prev[0], bb) if prev else bb, kind]

            st = norm_street(r["strename"])
            if len(re.findall(r"[А-Яа-яA-Za-z]", PREFIX.sub("", st))) < 2:
                continue  # празни или безсмислени имена ("....", "643")
            region = r["regname"].replace("район ", "")
            sk = (street_key(st), region)
            if sk in streets:
                streets[sk]["bb"] = union(streets[sk]["bb"], bb)
                if better(streets[sk]["s"], st):
                    streets[sk]["s"] = st
            else:
                streets[sk] = {"s": st, "r": region, "bb": bb}
            num = r["strnum"].strip().upper().replace(" ", "")
            if num:
                ak = (sk[0], num, region)
                if ak in addrs:
                    # prefer the building's footprint over the whole parcel when both exist
                    if kind == "b" and addrs[ak].get("k") == "p":
                        addrs[ak].update(bb=bb, k="b")
                    elif kind == addrs[ak]["k"]:
                        addrs[ak]["bb"] = union(addrs[ak]["bb"], bb)
                else:
                    addrs[ak] = {"s": st, "n": num, "r": region, "bb": bb, "k": kind}
    print(kind, "done", flush=True)

for reg, d in ids.items():
    with open(os.path.join(IDX, "id", f"{reg}.json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))

for (sk0, num, region), v in addrs.items():
    v["s"] = streets[(sk0, region)]["s"]

# compact: [street, number, region, minx, miny, maxx, maxy]; streets have number ""
out = [[v["s"], "", v["r"], *v["bb"]] for v in streets.values()]
out += [[v["s"], v["n"], v["r"], *v["bb"]] for v in addrs.values()]
with open(os.path.join(IDX, "addr.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
print("regions", len(ids), "streets", len(streets), "addresses", len(addrs),
      "addr.json", round(os.path.getsize(os.path.join(IDX, "addr.json")) / 1e6, 2), "MB")
