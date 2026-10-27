"""Shared helpers: polite HTTP, JSONL storage, parsing of cadastral identifiers / УПИ / квартал."""
import gzip
import json
import os
import re
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SITE = os.path.join(ROOT, "site")
UA = "karta-sofia-agent/1.0 (+https://karta-sofia.pages.dev)"

SOFIA = "68134"  # ЕКАТТЕ на гр. София


# ---------------------------------------------------------------- HTTP
_last = [0.0]


def http(url, data=None, headers=None, delay=1.0, retries=4, timeout=60):
    """GET (or POST when data is a dict) with a fixed delay between requests and retries."""
    h = {"User-Agent": UA}
    h.update(headers or {})
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
    for attempt in range(retries):
        wait = _last[0] + delay - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1} for {url[:80]}: {e}", flush=True)
            time.sleep(5 * (attempt + 1))


# ---------------------------------------------------------------- storage
def load_jsonl(name):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return {}
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[r["key"]] = r
    return out


def save_jsonl(name, records):
    os.makedirs(DATA, exist_ok=True)
    path = os.path.join(DATA, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for k in sorted(records, key=lambda k: (records[k].get("date") or "", k), reverse=True):
            f.write(json.dumps(records[k], ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_json(name, default):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, obj):
    os.makedirs(DATA, exist_ok=True)
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)


def open_gz_text(path):
    return gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")


# ---------------------------------------------------------------- dates
def date_from_number(number):
    """'46/08.10.2026' -> '2026-10-08' (дата на издаване от номера на акта)."""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", number or "")
    if not m:
        return None
    d, mo, y = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d}"


def date_from_ms(s):
    """'/Date(1792965600000)/' -> 'YYYY-MM-DD' (Europe/Sofia, approx via +3h)."""
    m = re.search(r"\((-?\d+)\)", s or "")
    if not m:
        return None
    t = int(m.group(1)) / 1000 + 3 * 3600
    return time.strftime("%Y-%m-%d", time.gmtime(t))


# ---------------------------------------------------------------- identifiers
# 68134.4092.9996[.1[.2]] ; tolerate spaces around dots ("68134.1607. 7193")
ID_RE = re.compile(r"(?<![\d.])(\d{5})\s*\.\s*(\d{1,5})\s*\.\s*(\d{1,5})((?:\s*\.\s*\d{1,5}){0,2})(?![\d])")


def find_ids(text):
    """All cadastral identifiers in text, normalised ('68134.704.184.1.4')."""
    out = []
    for m in ID_RE.finditer(text or ""):
        tail = re.sub(r"\s+", "", m.group(4))
        cid = f"{m.group(1)}.{m.group(2)}.{m.group(3)}{tail}"
        if cid not in out:
            out.append(cid)
    return out


def id_kind(cid):
    n = cid.count(".") + 1
    return {3: "parcel", 4: "building", 5: "unit"}.get(n)


def building_of(cid):
    """building id for a building or unit (СОС) id."""
    p = cid.split(".")
    return ".".join(p[:4]) if len(p) >= 4 else None


def parcel_of(cid):
    return ".".join(cid.split(".")[:3])


# ---------------------------------------------------------------- scope parsing (НАГ)
SCOPE_KEYS = ["Местност", "Квартал по регулация", "УПИ", "Идентификатор КККР (имот)",
              "Идентификатор КККР (сграда/СОС)", "Планоснимачен район", "Планоснимачен №", "Адрес"]
_KEY_RE = re.compile("(" + "|".join(re.escape(k) for k in SCOPE_KEYS) + r")\s*:?\s*")


def parse_scope(scope):
    """'Местност: X, Квартал по регулация: 22, УПИ: III - 398, ...' -> dict."""
    out = {}
    s = scope or ""
    marks = [(m.start(), m.end(), m.group(1)) for m in _KEY_RE.finditer(s)]
    for i, (st, en, k) in enumerate(marks):
        stop = marks[i + 1][0] if i + 1 < len(marks) else len(s)
        v = s[en:stop].strip().rstrip(",").strip()
        if v:
            out[k] = v
    return out


ROMAN = re.compile(r"^[IVXLCІХМ]+", re.I)  # латински и кирилски „І“, „Х“
_CYR2LAT = str.maketrans({"І": "I", "Х": "X", "і": "I", "х": "X", "М": "M", "С": "C", "Л": "L", "В": "V"})


def parse_upi(upi):
    """'XIV - 4230,4231 "за ЖС"' -> ('XIV', ['4230','4231']); 'II' -> ('II', [])."""
    if not upi:
        return None, []
    s = upi.strip().strip('"„“”').strip()
    m = re.match(r"^([IVXLCІХ]+)\s*[a-zа-я]?\s*(?:[-–—]\s*)?(.*)$", s, re.I)
    if not m:
        return None, []
    roman = m.group(1).translate(_CYR2LAT).upper()
    rest = m.group(2)
    rest = re.split(r'["„“”]|за\s', rest, maxsplit=1)[0]       # отрежи „за ЖС…“
    nums = re.findall(r"\d+(?:\.\d+)?", rest)
    return roman, nums


def norm_kv(kv):
    """'33Б' / '33 б' / '33b' -> '33Б'."""
    if not kv:
        return None
    s = kv.strip().upper().replace(" ", "")
    s = s.translate(str.maketrans({"A": "А", "B": "Б", "V": "В", "G": "Г"}))
    m = re.match(r"^(\d+[А-Я]?)$", s)
    return m.group(1) if m else None


# Столична община – райони, чиито територии в землище 68134 покриваме
SOFIA_DISTRICTS = ["Средец", "Красно село", "Възраждане", "Оборище", "Сердика", "Подуяне", "Слатина",
                   "Изгрев", "Лозенец", "Триадица", "Красна поляна", "Илинден", "Надежда", "Искър",
                   "Младост", "Студентски", "Витоша", "Овча купел", "Люлин", "Връбница", "Нови Искър",
                   "Кремиковци", "Панчарево", "Банкя"]

# населени места извън гр. София – ако се споменават без идентификатор, записът не е за града
OTHER_PLACES = re.compile(r"(?<![А-Яа-я])(?:с\.\s*[А-Яа-я]|село\s|гр\.\s*(?:Банкя|Нови\s+Искър|Бухово))", re.I)
