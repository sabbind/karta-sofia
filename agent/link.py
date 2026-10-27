"""Свързва документите от регистрите със сградите и имотите от кадастъра.

Входове (data/):  nag_rs.jsonl, nag_uve.jsonl, dnsk_rp.jsonl, manual_links.csv,
                  cadastre_buildings.txt.gz, cadastre_parcels.csv.gz
Изходи:
  site/idx/docs/<кадастрален район>.json   документите по сграда/имот (за панела на картата)
  site/idx/status.json                       етап на всяка сграда/имот (за оцветяването)
  out/nesvarzani.csv                         непривързаните документи – за ръчно попълване
  out/report.json                            статистика

Начини на свързване (поле "how"):
  id      – идентификатор от самия документ
  kvupi   – район + квартал + УПИ (+ планоснимачен номер) срещу кадастъра
  rs      – УВЕ взима мястото на своето разрешение за строеж (по линка в НАГ)
  uve     – РС взима мястото от УВЕ, което го посочва
  manual  – ръчно свързване от data/manual_links.csv
"""
import csv
import gzip
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (DATA, OTHER_PLACES, ROOT, SITE, SOFIA, building_of, find_ids, load_jsonl,  # noqa: E402
                    norm_kv, parse_scope, parse_upi)

OUT = os.path.join(ROOT, "out")


# ---------------------------------------------------------------- cadastre
def load_cadastre():
    with gzip.open(os.path.join(DATA, "cadastre_buildings.txt.gz"), "rt", encoding="utf-8") as f:
        buildings = {l.strip() for l in f if l.strip()}
    parcels, by_kv = {}, defaultdict(list)
    with gzip.open(os.path.join(DATA, "cadastre_parcels.csv.gz"), "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            parcels[r["id"]] = r
            kv = norm_kv(r["kv"])
            roman, _ = parse_upi(r["upi"])
            if kv and roman:
                by_kv[(r["region"], kv, roman)].append(r)
    return buildings, parcels, by_kv


# ---------------------------------------------------------------- per-source field extraction
def district_from(text):
    if not text:
        return None
    t = text.lower()
    for d in ("Красно село", "Красна поляна", "Овча купел", "Нови Искър", "Средец", "Възраждане", "Оборище",
              "Сердика", "Подуяне", "Слатина", "Изгрев", "Лозенец", "Триадица", "Илинден", "Надежда",
              "Искър", "Младост", "Студентски", "Витоша", "Люлин", "Връбница", "Кремиковци", "Панчарево", "Банкя"):
        if d.lower() in t:
            return d
    return None


def fields(doc):
    """-> (ids, district, kv, upi_raw, place, address, text_for_territory)"""
    if doc["src"].startswith("nag"):
        sc = parse_scope(doc.get("scope"))
        ids = find_ids(sc.get("Идентификатор КККР (сграда/СОС)", "") + " ; " + sc.get("Идентификатор КККР (имот)", ""))
        if not ids:
            ids = find_ids(doc.get("scope"))
        return (ids, doc.get("region"), sc.get("Квартал по регулация"), sc.get("УПИ"), sc.get("Местност"),
                sc.get("Адрес"), doc.get("scope") or "")
    # dnsk
    ids = find_ids(doc.get("cad_id"))
    settle = doc.get("settlement") or ""
    return (ids, district_from(settle) or district_from(doc.get("place")), doc.get("kv"), doc.get("upi"),
            doc.get("place"), settle, settle + " " + (doc.get("municipality") or ""))


def in_sofia_city(ids, district, text):
    if ids:
        return any(i.startswith(SOFIA + ".") for i in ids)
    if district == "Банкя":
        return False
    return not OTHER_PLACES.search(text or "")


# ---------------------------------------------------------------- linking
def link_by_ids(ids, buildings, parcels):
    targets, unknown = [], []
    for cid in ids:
        if not cid.startswith(SOFIA + "."):
            continue
        n = cid.count(".") + 1
        if n >= 4:
            b = building_of(cid)
            if b in buildings:
                targets.append(("b", b))
                continue
            p = ".".join(cid.split(".")[:3])
            if p in parcels:
                targets.append(("p", p))
                continue
        elif n == 3 and cid in parcels:
            targets.append(("p", cid))
            continue
        unknown.append(cid)
    return list(dict.fromkeys(targets)), unknown


def link_by_kv_upi(district, kv, upi, by_kv):
    kvn = norm_kv(kv)
    roman, nums = parse_upi(upi)
    if not (district and kvn and roman):
        return [], None
    cands = by_kv.get((district, kvn, roman), [])
    if nums and len(cands) > 1:
        conf = [c for c in cands if set(re.findall(r"\d+", c["oldident"] or "")) & set(nums)]
        if conf:
            cands = conf
    if len(cands) == 1:
        return [("p", cands[0]["id"])], None
    note = f"{len(cands)} имота с кв. {kvn}, УПИ {roman}" if cands else f"няма имот с кв. {kvn}, УПИ {roman} в р-н {district}"
    return [], note


def load_manual():
    path = os.path.join(DATA, "manual_links.csv")
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f, delimiter=";"):
            key = (r.get("key") or "").strip()
            ids = find_ids(r.get("identifier") or "")
            if key and ids:
                out[key] = ids
    return out


STAGE = {"nag-rs": 1, "nag-uve": 2, "dnsk-rp": 2}


def short_issuer(s):
    s = s or ""
    s = re.sub(r"^\s*(Вр\.\s*и\.?\s*д\.?\s*)?", "", s)
    s = s.replace("Главен архитект на СО - р-н", "Гл. арх. р-н").replace("Главен архитект на Столична община", "Гл. арх. на СО")
    s = s.replace("Началник на Дирекция национален строителен контрол (ДНСК)", "ДНСК")
    return re.sub(r"\s+", " ", s).strip()


def main():
    buildings, parcels, by_kv = load_cadastre()
    manual = load_manual()
    docs = {}
    for name in ("nag_rs.jsonl", "nag_uve.jsonl", "dnsk_rp.jsonl"):
        docs.update(load_jsonl(name))
    print("documents", len(docs), "manual links", len(manual), flush=True)

    links, notes, skipped = {}, {}, Counter()
    for key, d in docs.items():
        ids, district, kv, upi, place, addr, text = fields(d)
        manual_note = None
        if key in manual:
            t, unk = link_by_ids(manual[key], buildings, parcels)
            if t:
                links[key] = (t, "manual")
                continue
            manual_note = "ръчният идентификатор не е в кадастъра: " + ", ".join(unk)
        if not in_sofia_city(ids, district, text):
            skipped["извън гр. София"] += 1
            continue
        t, unknown = link_by_ids(ids, buildings, parcels)
        if t:
            links[key] = (t, "id")
            continue
        t, note = link_by_kv_upi(district, kv, upi, by_kv)
        if t:
            links[key] = (t, "kvupi")
            continue
        notes[key] = "; ".join(filter(None, [
            manual_note, ("идентификатор(и) не са в кадастъра: " + ", ".join(unknown)) if unknown else None, note]))

    # УВЕ <-> РС по линка в НАГ
    rs_by_hash = {d["hash"]: k for k, d in docs.items() if d["src"] == "nag-rs"}
    for key, d in docs.items():
        if d["src"] != "nag-uve":
            continue
        for ref in d.get("permit_ref") or []:
            rk = rs_by_hash.get(ref.get("hash"))
            if not rk:
                continue
            if key not in links and rk in links:
                links[key] = (links[rk][0], "rs")
                notes.pop(key, None)
            elif rk not in links and key in links:
                links[rk] = (links[key][0], "uve")
                notes.pop(rk, None)

    # ---------------- outputs for the map
    per_reg = defaultdict(lambda: defaultdict(list))
    stage_b, stage_p = {}, {}
    for key, (targets, how) in links.items():
        d = docs[key]
        cancelled = "отмен" in (d.get("status") or "").lower()
        entry = [d["src"], d.get("number"), d.get("date"), d.get("status"), short_issuer(d.get("issuer")),
                 (d.get("object") or "")[:240], d.get("url") or d.get("pdf"), how]
        for kind, cid in targets:
            p = cid.split(".")
            per_reg[p[1]][".".join(p[2:])].append(entry)
            if not cancelled:
                st = STAGE[d["src"]]
                target = stage_b if kind == "b" else stage_p
                target[cid] = max(target.get(cid, 0), st)
    ddir = os.path.join(SITE, "idx", "docs")
    os.makedirs(ddir, exist_ok=True)
    for f in os.listdir(ddir):
        os.remove(os.path.join(ddir, f))
    for reg, items in per_reg.items():
        for lst in items.values():
            lst.sort(key=lambda e: e[2] or "", reverse=True)
        with open(os.path.join(ddir, f"{reg}.json"), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(SITE, "idx", "status.json"), "w", encoding="utf-8") as f:
        json.dump({"b": stage_b, "p": stage_p}, f, separators=(",", ":"))

    # ---------------- unlinked table (за ръчно свързване)
    os.makedirs(OUT, exist_ok=True)
    cols = ["key", "регистър", "номер", "дата", "издател", "район", "обект", "местност", "квартал", "УПИ",
            "адрес/обхват", "бележка", "линк", "identifier"]
    src_name = {"nag-rs": "НАГ – РС", "nag-uve": "НАГ – УВЕ", "dnsk-rp": "ДНСК – РП"}
    unl = [k for k in notes]
    unl.sort(key=lambda k: (docs[k]["src"], docs[k].get("date") or ""), reverse=True)
    with open(os.path.join(OUT, "nesvarzani.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(cols)
        for k in unl:
            d = docs[k]
            ids, district, kv, upi, place, addr, text = fields(d)
            w.writerow([k, src_name[d["src"]], d.get("number"), d.get("date"), d.get("issuer"), district or "",
                        (d.get("object") or "")[:300], place or "", kv or "", upi or "",
                        (addr or d.get("scope") or "")[:300], notes[k], d.get("url") or d.get("pdf") or "", ""])

    by_src = Counter(docs[k]["src"] for k in docs)
    linked_src = Counter((docs[k]["src"], links[k][1]) for k in links)
    unl_src = Counter(docs[k]["src"] for k in notes)
    report = {
        "documents": dict(by_src), "skipped": dict(skipped),
        "linked": {f"{s} / {h}": n for (s, h), n in sorted(linked_src.items())},
        "unlinked": dict(unl_src),
        "buildings_with_docs": len(stage_b), "parcels_with_docs": len(stage_p),
    }
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
