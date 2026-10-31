"""Слоят с УПИ на ГИС – София (shp, BGS2005, cp1251) -> кои кадастрални имоти покрива всяко УПИ.

Usage: python tools/upi_join.py <upi.shp> <папка с имотите>
-> data/upi.csv.gz
   region; kv; upi; locality; purpose; parcels  (parcels = "68134.x.y:дял|…", дял от площта на УПИ)

Дяловете се смятат с равномерна мрежа от точки вътре в УПИ (до ~60 точки).
"""
import csv
import gzip
import math
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from address_join import load_layer, locate, pip  # noqa: E402
from shpio import read_polygons  # noqa: E402
import struct  # noqa: E402


def iter_dbf(path, encoding="cp1251"):
    with open(path, "rb") as f:
        h = f.read(32)
        n, hl, rl = struct.unpack("<IHH", h[4:12])
        fields, pos = [], 1
        while True:
            d = f.read(32)
            if d[0] == 0x0D:
                break
            fields.append((d[:11].split(b"\0")[0].decode("latin1"), pos, d[16]))
            pos += d[16]
        f.seek(hl)
        for _ in range(n):
            r = f.read(rl)
            yield {nm: r[a:a + ln].decode(encoding, "replace").strip() for nm, a, ln in fields}


def samples(rings, target=60):
    xs = [p[0] for r in rings for p in r]
    ys = [p[1] for r in rings for p in r]
    x0, y0, x1, y1 = min(xs), min(ys), max(xs), max(ys)
    step = max(math.sqrt((x1 - x0) * (y1 - y0) / target), 0.5)
    pts = []
    y = y0 + step / 2
    while y < y1:
        x = x0 + step / 2
        while x < x1:
            if pip(x, y, rings):
                pts.append((x, y))
            x += step
        y += step
    if not pts:  # тесни УПИ: средата на най-дългата отсечка
        pts = [((xs[0] + xs[len(xs) // 2]) / 2, (ys[0] + ys[len(ys) // 2]) / 2)]
    return pts


def main():
    shp, pdir = sys.argv[1:3]
    parcels = load_layer(pdir)
    geoms = read_polygons(shp)
    out = os.path.join(HERE, "..", "data", "upi.csv.gz")
    n = with_p = 0
    with gzip.open(out, "wt", encoding="utf-8", newline="") as o:
        w = csv.writer(o, delimiter=";")
        w.writerow(["region", "kv", "upi", "locality", "purpose", "parcels"])
        for r, g in zip(iter_dbf(shp[:-4] + ".dbf"), geoms):
            if not g or not r["UPI"] or not r["QUARTER"]:
                continue
            pts = samples(g)
            c = Counter(locate(x, y, parcels) for x, y in pts)
            tot = sum(c.values())
            shares = sorted(((k, v / tot) for k, v in c.items() if k), key=lambda t: -t[1])
            pstr = "|".join(f"{k}:{s:.2f}" for k, s in shares if s >= 0.05)
            region = ",".join(x.strip().strip('"') for x in r["REGIONNAME"].split(","))
            w.writerow([region, r["QUARTER"], r["UPI"], r["N_PLACEID"], r["REGPURPOSE"], pstr])
            n += 1
            with_p += bool(pstr)
            if n % 20000 == 0:
                print(n, flush=True)
    print("УПИ:", n, "с имоти:", with_p)


if __name__ == "__main__":
    main()
