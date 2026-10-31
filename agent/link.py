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
        buildings = {}  # id -> (код на предназначение, площ)
        for l in f:
            parts = l.strip().split(";")
            if parts[0]:
                fc = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                try:
                    area = float(parts[2]) if len(parts) > 2 else 100.0
                except ValueError:
                    area = 100.0
                buildings[parts[0]] = (fc, area)
    parcels, by_kv, by_addr = {}, defaultdict(list), defaultdict(list)
    with gzip.open(os.path.join(DATA, "cadastre_parcels.csv.gz"), "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            parcels[r["id"]] = r
            kv = norm_kv(r["kv"])
            roman, _ = parse_upi(r["upi"])
            if kv and roman:
                by_kv[(r["region"], kv, roman)].append(r)
            sk, num = street_key(r["street"]), norm_num(r["strnum"])
            r["_street"] = sk
            if sk and num:
                by_addr[(r["region"], sk, num)].append(r)
    return buildings, parcels, by_kv, by_addr


def area_key(s):
    """'ж.к. Младост 4' / 'кв. МЛАДОСТ 4' / 'ж.к. „Люлин“ - 6' -> 'младост 4'."""
    k = re.sub(r"[\"„“”'«»]", " ", (s or "").lower())
    k = re.sub(r"^\s*(?:ж\.?\s*к\.?|кв\.?|к-с|комплекс|жк\b|м\.|местност\b)\s*", "", k)
    k = re.sub(r"[.,\-–/()]", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def norm_block(b):
    m = re.match(r"^\s*(\d+)\s*([А-Яа-яA-Za-z]?)\b", b or "")
    return (m.group(1) + m.group(2).upper()) if m else ""


def load_addresses():
    """Адресните точки на ГИС – София (data/addresses.csv.gz), вързани с имот и сграда."""
    by_street, by_block = defaultdict(list), defaultdict(list)
    path = os.path.join(DATA, "addresses.csv.gz")
    if not os.path.exists(path):
        return by_street, by_block
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if r["settlement"] != "гр. София" or not r["parcel"]:
                continue
            rec = (r["region"], r["parcel"], r["building"])
            sk, num = street_key(r["street"]), norm_num(r["num"])
            if sk and num:
                by_street[(sk, num)].append(rec)
            ak, bl = area_key(r["area"]), norm_block(r["block"])
            if ak and bl:
                by_block[(ak, bl)].append(rec)
    return by_street, by_block


BLOCK_RE = re.compile(r"(?:ж\.?\s*к\.?|\bкв\.?|к-с|комплекс)\s*[\"„“]?([А-Яа-я][А-Яа-я0-9 \-.]*?)[\"“”]?\s*[,;]?\s*"
                      r"бл(?:ок)?\.?\s*№?\s*(\d+\s*[А-Яа-я]?)\b", re.I)


def parse_block(text):
    m = BLOCK_RE.search(text or "")
    return (area_key(m.group(1)), norm_block(m.group(2))) if m else (None, None)


def link_by_address_points(district, sk, num, ak, bl, by_street, by_block):
    """-> (targets, how, note). Блок -> сградата; улица и номер -> имотът."""
    def pick(cands):
        if district:
            cands = [c for c in cands if c[0] == district]  # друг район -> не е същият адрес
        return cands
    if ak and bl:
        cands = pick(by_block.get((ak, bl), []))
        blds = {c[2] for c in cands if c[2]}
        if len(blds) == 1:
            return [("b", blds.pop())], "блок", None
        if len(blds) > 1:
            return None, None, f"{len(blds)} сгради за {ak} бл. {bl}"
    if sk and num:
        cands = pick(by_street.get((sk, num), []))
        pcls = {c[1] for c in cands}
        if len(pcls) == 1:
            return [("p", pcls.pop())], "адресна точка", None
        if len(pcls) > 1:
            return None, None, f"{len(pcls)} имота с адрес {sk} {num}"
    return None, None, None


# ---------------------------------------------------------------- addresses
_PREFIX = re.compile(r"^(?:ул|бул|пл|ж\.?\s*к|кв|м|в\.?\s*з|площад|булевард|улица)\b\.?\s*", re.I)


def street_key(s):
    """'ул. "Княз Ал.Дондуков-Корсаков"' -> 'княз ал дондуков корсаков'."""
    if not s:
        return ""
    k = re.sub(r"[\"„“”'«»]", " ", s.lower())
    for _ in range(2):
        k = _PREFIX.sub("", k.strip())
    k = re.sub(r"[.,\-–/()]", " ", k)
    return re.sub(r"\s+", " ", k).strip()


def norm_num(n):
    if not n:
        return ""
    m = re.match(r"^\s*(\d+)\s*([А-Яа-яA-Za-z]?)\b", n.strip())
    return (m.group(1) + m.group(2).upper()) if m else ""


ADDR_RE = re.compile(r"(?:ул|бул|пл)\.?\s*(?:ул\.?\s*)?[\"„“]?([^№,;\"„“”]+?)[\"“”]?\s*№\s*(\d+\s*[А-Яа-я]?)", re.I)


def parse_address(text):
    """-> (street_key, number) from 'гр. София, ул. АНРИ БАРБЮС № 6, бл. 118' or (None, None)."""
    m = ADDR_RE.search(text or "")
    if not m:
        return None, None
    return street_key(m.group(1)), norm_num(m.group(2))


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
    # dnsk (нов формат: отделни колони; стар формат: всичко е в „Местонахождение“)
    settle = doc.get("settlement") or ""
    ids = find_ids(doc.get("cad_id")) or find_ids(settle) or find_ids(doc.get("object"))
    kv = doc.get("kv")
    upi = doc.get("upi")
    if not kv:
        m = re.search(r"\bкв\.?\s*(\d+\s*[А-Яа-я]?)\b(?!\s*\.)", settle)
        kv = m.group(1) if m else None
    if not upi:
        m = re.search(r"УПИ\s*([IVXLСІХ]+[^,;]*)", settle)
        upi = m.group(1) if m else None
    return (ids, district_from(settle) or district_from(doc.get("place")), kv, upi,
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


def loc_key(s):
    """Местност -> множество от думи: 'м. Лозенец ІІ-ра част' ~ 'ЛОЗЕНЕЦ - II ЧАСТ'."""
    k = (s or "").lower().translate(str.maketrans({"і": "i", "х": "x"}))
    k = re.sub(r"\b(?:м|местност|жил|гр|ж|к|кв|с|и)\.|\bместност\b|\bчаст\b", " ", k)
    k = re.sub(r"(\b[ivx]+|\d+)\s*-?\s*(?:ва|ра|та|ма)\b", r"\1", k)
    k = re.sub(r"[\"„“”'«».,\-–/()]", " ", k)
    return {w for w in k.split() if len(w) > 1 or w in "ivx" or w.isdigit()}


def loc_similar(a, b):
    a, b = loc_key(a), loc_key(b)
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= 0.6


def load_upi():
    """Слоят с УПИ (data/upi.csv.gz) -> (квартал, УПИ) -> [запис]; запис = dict с номера, местност, райони, имоти, сгради."""
    idx = defaultdict(list)
    path = os.path.join(DATA, "upi.csv.gz")
    if not os.path.exists(path):
        return idx
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            if not r["parcels"]:
                continue
            kv = norm_kv(r["kv"])
            roman, nums = parse_upi(r["upi"])
            if not (kv and roman):
                continue
            idx[(kv, roman)].append({
                "id": r["id"], "nums": set(nums), "locality": r["locality"],
                "regions": {x.strip() for x in r["region"].split(",")},
                "parcels": [(p, float(v)) for p, v in (x.split(":") for x in r["parcels"].split("|"))],
                "buildings": [b for b in (r.get("buildings") or "").split("|") if b]})
    return idx


def link_by_upi_layer(district, kv, upi, locality, upi_idx, parcels, buildings):
    """-> (targets, note). Местност (план) + квартал + УПИ (+ отреждане) -> УПИ от регулацията ->
    по местоположение към имотите под него и към сградата в него (ако е една основна)."""
    kvn = norm_kv(kv)
    roman, nums = parse_upi(upi)
    if not (kvn and roman):
        return [], None
    cands = upi_idx.get((kvn, roman), [])
    if not cands:
        return [], None
    if locality:
        same = [c for c in cands if loc_similar(c["locality"], locality)]
        if same:
            cands = same
        elif district:
            cands = [c for c in cands if district in c["regions"]]
    elif district:
        cands = [c for c in cands if district in c["regions"]]
    if nums and len(cands) > 1:
        conf = [c for c in cands if c["nums"] & set(nums)]
        cands = conf or cands
    if not cands:
        return [], None
    sets = {tuple(p for p, sh in c["parcels"] if sh >= 0.3) or (c["parcels"][0][0],) for c in cands}
    if len(sets) > 1:
        return [], f"{len(sets)} възможни УПИ {roman} в кв. {kvn}" + (f", м. {locality}" if locality else "")
    pids = [p for p in sets.pop() if p in parcels]
    if not pids:
        return [], None
    targets = [("p", p) for p in pids]
    blds = {b for c in cands for b in c["buildings"] if b in buildings and not is_aux(buildings[b])}
    if len(blds) == 1:
        targets.append(("b", blds.pop()))
    return targets, None


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
# етапи: 0 няма, 1 РС, 2 въведена, 3 други документи,
#        4 в имота има РС, 5 в имота има въвеждане – но имотът има няколко сгради и не е ясно за коя е
AUX_FC = {180, 410, 475, 480}  # гаражи, допълващо застрояване, трафопостове/енергийни, други инфраструктурни


def is_aux(info):
    fc, area = info
    return fc in AUX_FC or area < 25

# ---------------------------------------------------------------- категории документи
# 'm' основни (строеж/пристройка/надстройка на сграда и въвеждането им)
# 'p' преустройства и промени в сградата
# 'u' техническа инфраструктура (ел., топло, газ, ВиК, кабели, ТП, улици, мобилни оператори, ФЕЦ)
# 'o' други (огради, гаражи, допълващо застрояване, басейни, временни обекти…)
TELECOM = re.compile(r"базов[аи]\s+станци|приемо\s*[-–]?\s*предавателн|приемно\s*[-–]?\s*предавателн|\bgsm\b|\bumts\b|\blte\b|"
                     r"\b[345]g\b|антен|радиомониторинг|радиорелейн|мобилн\w*\s+оператор|теленор|виваком|мтел|мобилтел|глобул|"
                     r"а1\s+българия|йеттел|vivacom|telenor|yettel|оптичн|телекомуникац|\bбтк\b", re.I)
UTILITY = re.compile(
    r"електрозахранване|ел\.?\s*захранване|електропровод|кабел|\bб?м?к?тп\b|трафопост|трансформатор|\bсрн\b|\bнн\b|"
    r"топлозахранване|топлопровод|абонатна\s+станция|газ(?:ова|ификация|опровод|оснабд|ово\s+отклонение)|газопровод|"
    r"водопровод|водоснабд|канализац|\bвик\b|сградн\w+\s+отклонени|пречиствател|"
    r"\bулица\b|улици\b|улично\s+осветление|тротоар|пътн\w+\s+връзк|кръстовище|"
    r"фотоволт|\bф?в?ец\b|соларн|слънчев\w*\s+(?:колектор|панел|систем)|зарядн\w*\s+станци", re.I)
REBUILD_START = re.compile(
    r"^\W*(?:\d+\.\s*)?(?:вътрешно\s+|частично\s+|основно\s+|цялостно\s+|конструктивно\s+)?(?:преустройство|преустрийство|промяна|смяна|"
    r"монтаж|демонтаж|остъкляване|реконструкция|ремонт|основен\s+ремонт|подмяна|обновяване|саниране|въвеждане\s+на\s+мерки|"
    r"енергийн|разделяне|обединяване|укрепване|усилване|разширение\s+на\s+(?:ап|обект|магазин|офис|помещ)|оборудване|"
    r"изграждане\s+на\s+(?:вход|асансьор|стълб))", re.I)
REBUILD_ANY = re.compile(r"промяна\s+(?:на\s+)?предназначени|смяна\s+(?:на\s+)?предназначени|без\s+смр|"
                         r"подемн\w*\s+платформ|платформ\w*\s+за\s+(?:хора|лица|инвалид)|асансьор|рампа\s+за", re.I)
OTHER_ANY = re.compile(
    r"ограда|паркинг|детска\s+площадка|подпорна\s+стена|вертикална\s+планировка|реклам|навес|козирка|климатич|въздуховод|комин|"
    r"допълващо\s+застрояване|лятна\s+кухня|гараж|изгребна\s+яма|сондаж|кладенец|оранжерия|барбекю|беседка|басейн|временн|"
    r"кабина|павилион|преместваем|остъкляване|благоустрояване|озеленяване|разполагане|поставяне", re.I)
MAIN_ANY = re.compile(
    r"сград|къщ|жилищна\s+група|жилищен\s+комплекс|\bблок\b|хотел|офис|административ|магазин|търговски\s+център|"
    r"склад|цех|производствен|училищ|детска\s+градина|ясла|болниц|клиника|поликлиника|храм|църква|читалище|"
    r"спортна\s+зала|завод|пристройка|надстройка|\bжс\b|\bбаза\b|бензиностанция|заведение|ресторант|кафе|сервиз|"
    r"работилниц|музей|театър|кино|общежитие|дом\s+за|център|болнич|лаборатори|ателие|салон|аптека|банка|хале|хангар|ферма", re.I)
HEAD = 45  # „главата“ на описанието – какво е основното действие/обект


def doc_kind(obj):
    """'m' основен, 'p' преустройство/промяна в сградата, 'u' техническа инфраструктура, 'o' друго."""
    o = re.sub(r"\s+", " ", (obj or "")).strip()
    if not o:
        return "o"
    # „главата“: основният обект, преди добавките („… с гаражи“, „… и изгребна яма“, „…, трафопост“)
    head = re.split(r"\s(?:с|със|и|във?|в\s+УПИ)\s|,|\s[-–]\s|/", o, maxsplit=1, flags=re.I)[0][:HEAD + 20]
    if TELECOM.search(o) or UTILITY.search(head) or re.match(r"^\W*(?:външн|вътрешн\w*\s+газ|сградн\w*\s+газ)", o, re.I):
        return "u"
    if REBUILD_START.search(o) or REBUILD_ANY.search(o):
        return "p"
    if re.search(r"пристройка|надстройка", head, re.I):
        return "m"
    if OTHER_ANY.search(head):
        return "o"
    if MAIN_ANY.search(o):
        return "m"
    return "o"
    if TELECOM.search(o):
        return "t"
    if OTHER_START.search(o):
        return "o"
    if OTHER_EVERYWHERE.search(o):
        return "o"
    # „главата“: основният обект, преди добавките („… с гаражи“, „… и изгребна яма“, „…, трафопост“)
    head = re.split(r"\s(?:с|със|и|във?|в\s+УПИ)\s|,|\s[-–]\s|/", o, maxsplit=1, flags=re.I)[0][:HEAD + 20]
    if re.search(r"пристройка|надстройка", head, re.I):
        return "m"
    if OTHER_ANY.search(head):
        return "o"
    if MAIN_ANY.search(o):
        return "m"
    return "o"
    if OTHER_START.search(o):
        return "o"
    if re.search(r"пристройка|надстройка", o, re.I):
        return "m"
    if MAIN_ANY.search(o) and not (OTHER_ANY.search(o) and not re.search(r"сград|къщ|блок", o, re.I)):
        return "m"
    return "o"


def short_issuer(s):
    s = s or ""
    s = re.sub(r"^\s*(Вр\.\s*и\.?\s*д\.?\s*)?", "", s)
    s = s.replace("Главен архитект на СО - р-н", "Гл. арх. р-н").replace("Главен архитект на Столична община", "Гл. арх. на СО")
    s = s.replace("Началник на Дирекция национален строителен контрол (ДНСК)", "ДНСК")
    return re.sub(r"\s+", " ", s).strip()


DNSK_INDEX = "https://dnsk.bg/registri/publichen-registar-na-razresheniyata-za-polzvane-izdadeni-ot-dnsk/"


def dnsk_year_page(d):
    """Без PDF в регистъра на ДНСК -> линк към страницата на регистъра за годината."""
    return f"{DNSK_INDEX}{d['year']}-g/" if d.get("src") == "dnsk-rp" and d.get("year") else None


GRID_DX, GRID_DY = 0.003, 0.00225  # ~245 x 250 м


def write_grid(stage_b):
    """Обзорна мрежа за малките мащаби: брой сгради по етап във всяка клетка ~250 м."""
    iddir = os.path.join(SITE, "idx", "id")
    if not os.path.isdir(iddir):
        return
    x0, y0 = 23.17, 42.55
    cells = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    for fn in os.listdir(iddir):
        reg = fn[:-5]
        with open(os.path.join(iddir, fn), encoding="utf-8") as f:
            shard = json.load(f)
        for rest, (bb, kind) in shard.items():
            if kind != "b":
                continue
            cx, cy = (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2
            c = cells[(int((cx - x0) / GRID_DX), int((cy - y0) / GRID_DY))]
            c[stage_b.get(f"68134.{reg}.{rest}", 0)] += 1
    out = {"x0": x0, "y0": y0, "dx": GRID_DX, "dy": GRID_DY,
           "c": [[i, j] + v for (i, j), v in sorted(cells.items())]}  # [i, j, без, РС, въведени, други, имот с РС, имот с въвеждане]
    with open(os.path.join(SITE, "idx", "grid.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    print("grid cells:", len(cells), flush=True)


def main():
    buildings, parcels, by_kv, by_addr = load_cadastre()
    addr_street, addr_block = load_addresses()
    upi_idx = load_upi()
    print("УПИ (регулация):", sum(map(len, upi_idx.values())), flush=True)
    print("address points:", sum(map(len, addr_street.values())), "street,", len(addr_block), "blocks", flush=True)
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
        if not place and d["src"] == "dnsk-rp":
            m = re.search(r"\bм(?:естност)?\.?\s*[\"„“]?([^,;\"“”]+)", d.get("settlement") or "")
            place = m.group(1) if m else None
        t, note_u = link_by_upi_layer(district, kv, upi, place, upi_idx, parcels, buildings)
        if t:
            links[key] = (t, "упи")
            continue
        t, note = link_by_kv_upi(district, kv, upi, by_kv)
        if t:
            links[key] = (t, "kvupi")
            continue
        note = "; ".join(filter(None, [note_u, note]))
        addr_text = addr if d["src"].startswith("nag") else (d.get("settlement") or "")
        sk, num = parse_address(addr_text)
        if note and note[0].isdigit() and sk:
            # няколко имота с този кв. + УПИ: избери този на същата улица
            cands = [c for c in by_kv.get((district, norm_kv(kv), parse_upi(upi)[0]), []) if c["_street"] == sk]
            if len(cands) == 1:
                links[key] = ([("p", cands[0]["id"])], "kvupi+адрес")
                continue
        ak, bl = parse_block(addr_text)
        t2, how2, note2 = link_by_address_points(district, sk, num, ak, bl, addr_street, addr_block)
        if t2:
            # блокът е и в имот -> документът е и за имота
            if t2[0][0] == "b":
                pid = ".".join(t2[0][1].split(".")[:3])
                if pid in parcels:
                    t2 = t2 + [("p", pid)]
            links[key] = (t2, how2)
            continue
        note = "; ".join(filter(None, [note, note2]))
        if sk and num and district:
            cands = by_addr.get((district, sk, num), [])
            if len(cands) == 1:
                links[key] = ([("p", cands[0]["id"])], "адрес")
                continue
            if len(cands) > 1:
                note = "; ".join(filter(None, [note, f"{len(cands)} имота с адрес {sk} {num}"]))
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

    # РС е само към имот, а неговото УВЕ сочи сграда в същия имот -> РС се закача и към тази сграда
    for key, d in docs.items():
        if d["src"] != "nag-uve" or key not in links:
            continue
        uve_buildings = [cid for kind, cid in links[key][0] if kind == "b"]
        if not uve_buildings:
            continue
        for ref in d.get("permit_ref") or []:
            rk = rs_by_hash.get(ref.get("hash"))
            if not rk or rk not in links:
                continue
            targets, how = links[rk]
            rs_parcels = {cid for kind, cid in targets if kind == "p"}
            if any(kind == "b" for kind, _ in targets) or not rs_parcels:
                continue
            add = [("b", b) for b in uve_buildings if ".".join(b.split(".")[:3]) in rs_parcels]
            if add:
                links[rk] = (targets + add, how)

    # ---------------- връзките УВЕ <-> РС (от страницата на УВЕ в НАГ) – за показване и за проверка
    NAG_INFO = "https://nag.sofia.bg/RegisterInfo/Info?url="
    refs = defaultdict(list)
    mismatches = []
    pset = lambda k: {(c if kd == "p" else ".".join(c.split(".")[:3])) for kd, c in links[k][0]}  # noqa: E731
    for key, d in docs.items():
        if d["src"] != "nag-uve":
            continue
        for ref in d.get("permit_ref") or []:
            if not ref.get("hash"):
                continue
            refs[key].append(["РС " + (ref.get("number") or ""), NAG_INFO + ref["hash"]])
            rk = rs_by_hash.get(ref["hash"])
            if rk:
                refs[rk].append(["УВЕ " + (d.get("number") or ""), d.get("url")])
                if key in links and rk in links and not (pset(key) & pset(rk)):
                    mismatches.append([d.get("number"), d.get("url"), ", ".join(sorted(pset(key))), links[key][1],
                                       docs[rk].get("number"), docs[rk].get("url"), ", ".join(sorted(pset(rk))),
                                       links[rk][1], (d.get("object") or "")[:160]])

    # ---------------- outputs for the map
    per_reg = defaultdict(lambda: defaultdict(list))
    parcels_bld_docs = set()
    stage_b, stage_p = {}, {}
    year_b, year_p = {}, {}  # година на най-новия валиден документ (за филтъра по година)
    for key, (targets, how) in links.items():
        d = docs[key]
        cancelled = "отмен" in (d.get("status") or "").lower()
        kind_doc = doc_kind(d.get("object"))
        entry = [d["src"], d.get("number"), d.get("date"), d.get("status"), short_issuer(d.get("issuer")),
                 (d.get("object") or "")[:240], d.get("url") or d.get("pdf") or dnsk_year_page(d), how, kind_doc,
                 refs.get(key) or None]
        for kind, cid in targets:
            p = cid.split(".")
            per_reg[p[1]][".".join(p[2:])].append(entry)
            target = stage_b if kind == "b" else stage_p
            yt = year_b if kind == "b" else year_p
            if not cancelled and (d.get("date") or "")[:4].isdigit():
                yt[cid] = max(yt.get(cid, 0), int(d["date"][:4]))
            if kind == "p" and not cancelled and kind_doc in ("m", "p"):
                parcels_bld_docs.add(cid)  # документи за сграда в имота (а не само за имота/мрежите)
            if not cancelled and kind_doc == "m":  # етапът на сградата – само от основните документи
                st = STAGE[d["src"]]
                target[cid] = max(target.get(cid, 0) if target.get(cid, 0) != 3 else 0, st)
            elif not cancelled and target.get(cid, 0) == 0:
                target[cid] = 3  # само други документи (преустройства, ремонти, станции…)
    # документи само към имот: ако в имота има точно една сграда, тя взима етапа на имота
    bld_by_parcel = defaultdict(list)
    for b in buildings:
        bld_by_parcel[".".join(b.split(".")[:3])].append(b)
    inherited = 0
    single_bld = {}  # имот -> единствената (основна) сграда в него
    for pid, st in stage_p.items():
        if pid not in parcels_bld_docs:
            continue  # само техническа инфраструктура / други към имота – не оцветяват сградите
        blds = bld_by_parcel.get(pid, [])
        main_blds = [b for b in blds if not is_aux(buildings[b])]
        if len(blds) > 1 and len(main_blds) == 1:
            # една основна сграда + гаражи/трафопостове -> документът е за основната
            for b in blds:
                if b != main_blds[0] and stage_b.get(b, 0) == 0:
                    stage_b[b] = 3
            blds = main_blds
        if len(blds) > 1:
            # няколко основни сгради: не знаем за коя е документът
            amb = {1: 4, 2: 5}.get(st, 3)
            for b in blds:
                cur = stage_b.get(b, 0)
                new_st = (amb if b in main_blds else 3) if cur in (0, 3, 4, 5) else cur
                if cur in (4, 5) and new_st in (4, 5):
                    new_st = max(cur, new_st)
                elif cur in (4, 5):
                    new_st = cur
                if new_st != cur:
                    stage_b[b] = new_st
                    inherited += 1
                if pid in year_p:
                    year_b[b] = max(year_b.get(b, 0), year_p[pid])
            continue
        if len(blds) != 1:
            continue
        b = blds[0]
        single_bld[pid] = b
        if pid in year_p:
            year_b[b] = max(year_b.get(b, 0), year_p[pid])
        cur = stage_b.get(b, 0)
        if cur in (1, 2):
            new_st = max(cur, st) if st in (1, 2) else cur
        else:
            new_st = st if st in (1, 2) else 3
        if new_st != cur:
            stage_b[b] = new_st
            inherited += 1
    print("buildings inheriting parcel stage:", inherited, flush=True)
    # основните документи и преустройствата към имот с една сграда се показват при сградата
    moved = 0
    for pid, b in single_bld.items():
        reg, prest = pid.split(".")[1], pid.split(".")[2]
        brest = ".".join(b.split(".")[2:])
        items = per_reg[reg]
        keep = []
        have = {(e[0], e[1]) for e in items.get(brest, [])}
        for e in items.get(prest, []):
            if e[8] in ("m", "p"):
                if (e[0], e[1]) not in have:
                    items[brest].append(e[:10] + ["имот"])  # [10]: вписан с имота
                    moved += 1
            else:
                keep.append(e)
        if keep:
            items[prest] = keep
        else:
            items.pop(prest, None)
    print("documents shown at the only building of their parcel:", moved, flush=True)

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
        json.dump({"b": stage_b, "p": stage_p, "yb": year_b, "yp": year_p}, f, separators=(",", ":"))
    write_grid(stage_b)

    # ---------------- unlinked table (за ръчно свързване)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "razminavania_uve_rs.csv"), "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["УВЕ №", "УВЕ линк", "УВЕ – имот(и)", "УВЕ – как", "РС №", "РС линк", "РС – имот(и)",
                    "РС – как", "обект"])
        w.writerows(mismatches)
    print("УВЕ и РС на различни места:", len(mismatches), flush=True)
    cols = ["key", "регистър", "номер", "дата", "издател", "район", "обект", "местност", "квартал", "УПИ",
            "адрес/обхват", "бележка", "линк", "identifier"]
    src_name = {"nag-rs": "НАГ – РС", "nag-uve": "НАГ – УВЕ", "dnsk-rp": "ДНСК – РП"}
    unl = [k for k in notes]
    unl.sort(key=lambda k: (docs[k]["src"], docs[k].get("date") or ""), reverse=True)
    # документи с идентификатор, който още го няма в нашето копие на кадастъра (АГКК публикува на ~3 месеца):
    # ще се свържат сами след следващото обновяване -> отделна таблица, не за ръчно свързване
    waiting = [k for k in unl if notes[k].startswith("идентификатор(и) не са в кадастъра")]
    manual_todo = [k for k in unl if k not in set(waiting)]
    for fname, keys in (("nesvarzani.csv", manual_todo), ("chakat_kadastar.csv", waiting)):
        with open(os.path.join(OUT, fname), "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            w.writerow(cols)
            for k in keys:
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
        "unlinked_for_manual": len(manual_todo), "waiting_for_cadastre_update": len(waiting),
        "buildings_with_docs": len(stage_b), "parcels_with_docs": len(stage_p),
    }
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
