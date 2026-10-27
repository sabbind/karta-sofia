"""Изтегля регистрите на НАГ (nag.sofia.bg):
  - разрешения за строеж (РС)                            -> data/nag_rs.jsonl
  - удостоверения за въвеждане в експлоатация (УВЕ)      -> data/nag_uve.jsonl
За УВЕ отваря и страницата с детайли, за да вземе към кое РС се отнася (бавно, с лимит на заявките).

НЕ пази полето „Възложител“ (съдържа имена на физически лица).

Usage:
  python agent/harvest_nag.py            # нови записи (спира, щом стигне до вече известни)
  python agent/harvest_nag.py --full     # обхожда целия регистър (обновява статуси)
  python agent/harvest_nag.py --details 3000   # колко страници с детайли на УВЕ да отвори максимум
"""
import argparse
import html
import json
import re
import sys
import os
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import date_from_ms, date_from_number, http, load_jsonl, save_jsonl  # noqa: E402

BASE = "https://nag.sofia.bg"
REGISTERS = {
    "rs": {"url": "/RegisterBuildingPermitsPortal/Read", "file": "nag_rs.jsonl"},
    "uve": {"url": "/RegisterCertificateForExploitationBuildings/Read", "file": "nag_uve.jsonl"},
}
PAGE = 200
XHR = {"X-Requested-With": "XMLHttpRequest"}


def read_page(path, page):
    raw = http(BASE + path, data={"sort": "", "page": page, "pageSize": PAGE, "group": "", "filter": ""},
               headers=XHR, delay=1.0)
    return json.loads(raw)


def to_record(kind, d):
    rec = {
        "key": f"nag-{kind}:{d['Hash']}",
        "src": f"nag-{kind}",
        "hash": d["Hash"],
        "number": (d.get("Number") or "").strip(),
        "date": date_from_number(d.get("Number")),
        "in_force": date_from_ms((d.get("TakeEffect") or {}).get("DateTime") or d.get("TakeEffectFilter")),
        "status": d.get("Status"),
        "doctype": d.get("DocumentTypeName"),
        "issuer": (d.get("Issuer") or "").strip(),
        "region": d.get("Region"),
        "object": re.sub(r"\s+", " ", d.get("Object") or "").strip(),
        "scope": re.sub(r"\s+", " ", d.get("Scope") or "").strip(),
        "url": f"{BASE}/RegisterInfo/Info?url={d['Hash']}",
    }
    if kind == "uve":
        rec["supervision"] = d.get("ConstructionalOversightName")
    return rec


def harvest(kind, full):
    reg = REGISTERS[kind]
    db = load_jsonl(reg["file"])
    page, total, new, changed, known_pages = 1, None, 0, 0, 0
    while True:
        j = read_page(reg["url"], page)
        total = j.get("Total", total)
        rows = j.get("Data") or []
        if not rows:
            break
        page_new = 0
        for d in rows:
            rec = to_record(kind, d)
            old = db.get(rec["key"])
            if old is None:
                new += 1
                page_new += 1
                db[rec["key"]] = rec
            else:
                keep = {k: old[k] for k in ("permit_ref", "details_checked") if k in old}
                if any(old.get(k) != rec[k] for k in ("status", "scope", "object", "in_force")):
                    changed += 1
                rec.update(keep)
                db[rec["key"]] = rec
        print(f"{kind} page {page}: {len(rows)} rows, {page_new} new (total {total})", flush=True)
        known_pages = known_pages + 1 if page_new == 0 else 0
        if not full and known_pages >= 2:
            break
        if page * PAGE >= (total or 0):
            break
        page += 1
    save_jsonl(reg["file"], db)
    print(f"{kind}: {len(db)} records, {new} new, {changed} changed", flush=True)
    return db


REF_CELL = re.compile(r"Към разрешение/я за строеж\s*</td>\s*<td>(.*?)</td>", re.S)
REF_LINK = re.compile(r'<a[^>]+href="/RegisterInfo/Info\?url=([^"&]+)"[^>]*>(.*?)</a>', re.S)


def parse_permit_refs(page_html):
    """From the УВЕ detail page: [{'hash','number','issuer'}] of the referenced building permits."""
    m = REF_CELL.search(page_html)
    if not m:
        return []
    refs = []
    for h, label in REF_LINK.findall(m.group(1)):
        text = html.unescape(re.sub(r"<[^>]+>", " ", label))
        text = re.sub(r"\s+", " ", text).strip()
        mm = re.match(r"^(.*?\d{1,2}\.\d{1,2}\.\d{4})\s*(?:г\.)?\s*,?\s*(?:издадено от\s*(.*))?$", text)
        refs.append({
            "hash": urllib.parse.unquote(h),
            "number": mm.group(1).strip() if mm else text,
            "issuer": (mm.group(2) or "").strip() if mm else "",
        })
    return refs


def fetch_uve_details(limit, minutes):
    import time
    db = load_jsonl(REGISTERS["uve"]["file"])
    todo = [k for k, r in db.items() if not r.get("details_checked")]
    todo.sort(key=lambda k: db[k].get("date") or "", reverse=True)  # първо най-новите
    print(f"uve details: {len(todo)} pending, fetching up to {limit} or {minutes} min", flush=True)
    done, stop_at = 0, time.time() + minutes * 60
    try:
        for k in todo[:limit]:
            if time.time() > stop_at:
                print("  time budget reached", flush=True)
                break
            r = db[k]
            page = http(r["url"], delay=0.7).decode("utf-8", "replace")
            r["permit_ref"] = parse_permit_refs(page)
            r["details_checked"] = True
            done += 1
            if done % 200 == 0:
                print(f"  {done} details", flush=True)
                save_jsonl(REGISTERS["uve"]["file"], db)
    finally:
        save_jsonl(REGISTERS["uve"]["file"], db)
    print(f"uve details: fetched {done}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--details", type=int, default=100000, help="максимален брой страници с детайли")
    ap.add_argument("--minutes", type=float, default=240, help="времеви лимит за детайлите (минути)")
    ap.add_argument("--only", choices=["rs", "uve"])
    a = ap.parse_args()
    for kind in (["rs", "uve"] if not a.only else [a.only]):
        harvest(kind, a.full)
    if a.only in (None, "uve") and a.details > 0:
        fetch_uve_details(a.details, a.minutes)
