"""Изтегля публичния регистър на разрешенията за ползване на ДНСК (dnsk.bg), по години.
-> data/dnsk_rp.jsonl

Пази само записите на територията на Столична община.
НЕ чете и НЕ пази колоните „Наименование ЮЛ/Имена ФЛ“ и „ЕГН/Булстат/ЕИК“.

Usage:
  python agent/harvest_dnsk.py              # текущата и предходната година
  python agent/harvest_dnsk.py --all        # всички години (2000 – днес)
"""
import argparse
import html
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import http, load_jsonl, save_jsonl  # noqa: E402

INDEX = "https://dnsk.bg/registri/publichen-registar-na-razresheniyata-za-polzvane-izdadeni-ot-dnsk/"

# заглавие на колоната (нормализирано) -> наше поле; колоните с лични данни нарочно ги няма
COLS = {
    "№ на акта": "number",
    "Разрешение за ползване": "pdf",
    "Дата на акта": "date_raw",
    "Административен орган": "issuer",
    "Описание/ Наименование на обект": "object",
    "Кадастрален идентификатор": "cad_id",
    "УПИ": "upi",
    "Местност": "place",
    "Населено място Район (обект)": "settlement",
    "Квартал": "kv",
    "Област (обект)": "province",
    "Тип": "doctype",
    "Състояние": "status",
    "Администрация": "administration",
    "Община (обект)": "municipality",
    # стар формат (2000–2025)
    "РП №/дата": "number_date",
    "Наименование на строежа": "object",
    "Местонахождение": "settlement",
}
SKIP = {"Наименование ЮЛ/ Имена ФЛ", "ЕГН/Булстат/ЕИК", "Вид на възложителя"}


def clean(s):
    s = html.unescape(re.sub(r"<br\s*/?>", " ", s or ""))
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_head(s):
    return re.sub(r"\s+", " ", clean(s)).replace("/ ", "/ ").strip()


def year_links():
    try:
        page = http(INDEX, delay=2).decode("utf-8", "replace")
        links = sorted(set(re.findall(r"publichen-registar-na-razresheniyata-za-polzvane-izdadeni-ot-dnsk/(\d{4})-g/", page)),
                       reverse=True)
    except Exception as e:  # noqa: BLE001
        print(f"dnsk index: ГРЕШКА {type(e).__name__}: {e}", flush=True)
        links = []
    if not links:  # резервно: годините по шаблона
        links = [str(y) for y in range(time.gmtime().tm_year, 1999, -1)]
    return [(int(y), f"{INDEX}{y}-g/") for y in links]


_JS_ESC = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.S)
_SIMPLE = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def decode_js_string(s):
    r"""Content of a JS single-quoted string literal -> Python str (handles \\ \' \" \/ \uXXXX \xXX)."""
    def rep(m):
        e = m.group(1)
        if e[0] == "u" and len(e) == 5:
            return chr(int(e[1:], 16))
        if e[0] == "x" and len(e) == 3:
            return chr(int(e[1:], 16))
        return _SIMPLE.get(e, e)
    return _JS_ESC.sub(rep, s)


def parse_year_page(page):
    m = re.search(r"<table id=\"tablepress-(\d+)\".*?<thead>(.*?)</thead>", page, re.S)
    if not m:
        raise ValueError("no tablepress table")
    tid, thead = m.groups()
    heads = [norm_head(h) for h in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", thead, re.S)]
    dm = re.search(r"DT_TP_data\['" + tid + r"'\]\s*=\s*JSON\.parse\('(.*?)'\);", page, re.S)
    if dm:
        rows = json.loads(decode_js_string(dm.group(1)))
    else:  # малки таблици може да са само в HTML
        body = page[m.end():page.index("</table>", m.end())]
        rows = [re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S) for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S)]
    out = []
    for row in rows:
        rec = {}
        for i, h in enumerate(heads):
            if i >= len(row) or h in SKIP:
                continue
            field = COLS.get(h)
            if not field:
                continue
            cell = row[i] or ""
            if field == "pdf":
                pm = re.search(r'href=\\?"([^"\\]+\.pdf)', cell)
                rec["pdf"] = pm.group(1) if pm else None
            else:
                rec[field] = clean(cell)
        out.append(rec)
    return out, heads


def is_sofia_municipality(r):
    txt = " ".join(r.get(k) or "" for k in ("municipality", "settlement", "cad_id", "province"))
    if re.search(r"СТОЛИЧНА|68134\.|гр\.\s*София\b|София\s*[-–]?\s*град|\bСО\b", txt, re.I):
        return True
    return (r.get("municipality") or "").strip().lower() in ("софия", "столична")


def split_number_date(v):
    """'ДНСК-РП-123/12.03.2015 г.' -> ('ДНСК-РП-123', '12.03.2015')."""
    v = (v or "").strip()
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", v)
    date = m.group(1) if m else None
    num = re.split(r"\s*/\s*|\s+от\s+|\s*,\s*", v, maxsplit=1)[0].strip() if v else ""
    if date and num == date:
        num = ""
    return num, date


def to_date(raw):
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", raw or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def harvest(all_years):
    db = load_jsonl("dnsk_rp.jsonl")
    years = year_links()
    if not years:
        raise SystemExit("no year links found on " + INDEX)
    if not all_years:
        years = years[:2]
    errors = 0
    for y, url in years:
        t0 = time.time()
        try:
            page = http(url, delay=3, timeout=180).decode("utf-8", "replace")
            rows, heads = parse_year_page(page)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"dnsk {y}: ГРЕШКА {type(e).__name__}: {e}", flush=True)
            continue
        unknown = [h for h in heads if h and h not in COLS and h not in SKIP and h not in ("Виж инфо",)]
        kept = 0
        for r in rows:
            if r.get("number_date") and not r.get("number"):
                r["number"], d = split_number_date(r.pop("number_date"))
                r.setdefault("date_raw", d)
            r.pop("number_date", None)
            if not r.get("number") or not is_sofia_municipality(r):
                continue
            key = "dnsk-rp:" + re.sub(r"\s+", "", r["number"])
            r.update({"key": key, "src": "dnsk-rp", "year": y, "date": to_date(r.get("date_raw"))})
            r.pop("date_raw", None)
            db[key] = r
            kept += 1
        print(f"dnsk {y}: {len(rows)} rows, {kept} in Столична община ({time.time() - t0:.0f}s)"
              + (f"; unknown columns: {unknown}" if unknown else ""), flush=True)
        save_jsonl("dnsk_rp.jsonl", db)
    save_jsonl("dnsk_rp.jsonl", db)
    print(f"dnsk: {len(db)} records, {errors} years with errors", flush=True)
    if errors == len(years):
        raise SystemExit("dnsk: всички години са неуспешни")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    harvest(ap.parse_args().all)
