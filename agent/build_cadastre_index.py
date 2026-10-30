"""Компактен индекс на кадастъра за свързването (пуска се ръчно, когато АГКК обнови данните).

Usage: python agent/build_cadastre_index.py <sofia-buildings-attributes.csv> <sofia-parcels-attributes.csv>
-> data/cadastre_buildings.txt.gz   (идентификатор;код на предназначение;площ – по един на ред)
-> data/cadastre_parcels.csv.gz     (id, район, квартал, УПИ, стар идентификатор/пл. №, местност, адрес)
"""
import csv
import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import DATA  # noqa: E402

bld_csv, pcl_csv = sys.argv[1], sys.argv[2]
os.makedirs(DATA, exist_ok=True)

with open(bld_csv, encoding="utf-8-sig") as f, gzip.open(os.path.join(DATA, "cadastre_buildings.txt.gz"), "wt", encoding="utf-8") as o:
    n = 0
    for r in csv.DictReader(f):
        o.write(f'{r["cadnum"]};{r["funccode"]};{r["area_m2"]}\n')
        n += 1
print("buildings", n)

with open(pcl_csv, encoding="utf-8-sig") as f, gzip.open(os.path.join(DATA, "cadastre_parcels.csv.gz"), "wt", encoding="utf-8", newline="") as o:
    w = csv.writer(o)
    w.writerow(["id", "region", "kv", "upi", "oldident", "place", "quarter_name", "street", "strnum"])
    n = 0
    for r in csv.DictReader(f):
        w.writerow([r["cadnum"], r["region"].replace("район ", ""), r["quarter"], r["upi"], r["oldident"],
                    r["place"], r["quarter_name"], r["street"], r["strnum"]])
        n += 1
print("parcels", n)
