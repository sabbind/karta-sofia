import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
from common import find_ids, parse_scope, parse_upi, norm_kv, OTHER_PLACES  # noqa: E402
from harvest_nag import parse_permit_refs  # noqa: E402
from harvest_dnsk import parse_year_page, decode_js_string  # noqa: E402

ok = 0


def eq(a, b, msg=""):
    global ok
    assert a == b, f"{msg}\n  got:      {a!r}\n  expected: {b!r}"
    ok += 1


# --- identifiers
eq(find_ids("Идентификатор КККР (сграда/СОС): 68134.704.184.1.4, 68134.704.184.1.5 и 68134.704.184.1.6"),
   ["68134.704.184.1.4", "68134.704.184.1.5", "68134.704.184.1.6"])
eq(find_ids("68134.204.171.1; 68134.204.171.1.52; 68134.204.171.1.57"),
   ["68134.204.171.1", "68134.204.171.1.52", "68134.204.171.1.57"])
eq(find_ids("ПИ С ИДЕНТИФИКАТОР 68134.1607. 7193 ПО КККР"), ["68134.1607.7193"])
eq(find_ids("68134.406.19. 2"), ["68134.406.19.2"])
eq(find_ids("80409.5903.35, 80409.5903.8, 80409.5903.9,80409.5903.7"),
   ["80409.5903.35", "80409.5903.8", "80409.5903.9", "80409.5903.7"])
eq(find_ids("к.к. +15.00 м = 554.17 м"), [])

# --- scope
sc = parse_scope('Местност: КРЪСТОВА ВАДА И М. ЮЖЕН ПАРК - IV ЧАСТ, Квартал по регулация: 9, УПИ: I - 3264,3289, '
                 'Идентификатор КККР (имот): 68134.904.3350, Идентификатор КККР (сграда/СОС): 68134.904.3350.2.151, '
                 'Планоснимачен район: Кръстова вада, Планоснимачен № 1382,2571, Адрес: гр. София, ул. Филип Кутев № 2Б')
eq(sc["Квартал по регулация"], "9")
eq(sc["УПИ"], "I - 3264,3289")
eq(sc["Идентификатор КККР (сграда/СОС)"], "68134.904.3350.2.151")
eq(sc["Планоснимачен №"], "1382,2571")
eq(sc["Адрес"], "гр. София, ул. Филип Кутев № 2Б")
sc = parse_scope("Местност: м. Гео Милев, Квартал по регулация: , УПИ: , Адрес: гр. София, ул. АНРИ БАРБЮС № 6, бл. 118, вх. А, Б")
eq(sc.get("Квартал по регулация"), None)
eq(sc["Адрес"], "гр. София, ул. АНРИ БАРБЮС № 6, бл. 118, вх. А, Б")

# --- УПИ / квартал
eq(parse_upi("III - 398"), ("III", ["398"]))
eq(parse_upi("XIV4230,4231"), ("XIV", ["4230", "4231"]))
eq(parse_upi('I – "за болничен комплекс"'), ("I", []))
eq(parse_upi('ІІ-824-"за производствени и складови дейности"'), ("II", ["824"]))
eq(parse_upi("ХХІХ-376,за складове и офиси"), ("XXIX", ["376"]))
eq(parse_upi('VIII - 278,279 "за жс"'), ("VIII", ["278", "279"]))
eq(parse_upi("XVIIa - 164"), ("XVII", ["164"]))
eq(parse_upi("II"), ("II", []))
eq(parse_upi(""), (None, []))
eq(norm_kv("33Б"), "33Б")
eq(norm_kv("12а"), "12А")
eq(norm_kv("387A"), "387А")
eq(norm_kv(" 81 "), "81")

# --- territory
eq(bool(OTHER_PLACES.search("Местност: С. ЛОЗЕН, Квартал по регулация: 62")), True)
eq(bool(OTHER_PLACES.search("Адрес: гр. Нови Искър, ул. Първа шопска")), True)
eq(bool(OTHER_PLACES.search("Местност: КВ. ОВЧА КУПЕЛ, Адрес: гр. София, ул. ЗАГРЕБСКА")), False)
eq(bool(OTHER_PLACES.search("Местност: ХР. СМИРНЕНСКИ-СЛАТИНА")), False)

# --- УВЕ detail page -> referenced РС
html_snip = '''<td>Към разрешение/я за строеж</td>
   <td>
      <a href="/RegisterInfo/Info?url=kXWtq2_4ITk%3D"
         target="_blank">
          <i class="infoicon" aria-hidden="true"></i>82/18.09.2019 г., издадено от Главен архитект на СО - р-н &quot;Банкя&quot;
      </a>
      <br />
   </td>'''
eq(parse_permit_refs(html_snip), [{"hash": "kXWtq2_4ITk=", "number": "82/18.09.2019",
                                    "issuer": 'Главен архитект на СО - р-н "Банкя"'}])

# --- DNSK year page (структура като на dnsk.bg)
rows = [["", "ДНСК26-РП-527", '<a href="https://dnsk.bg/wp-content/uploads/2026/10/dnsk26-rp-527.pdf" target="_blank">PDF файл</a>',
         "27.8.2026", "Началник на Дирекция национален строителен контрол (ДНСК)", "„ВЪВЕЖДАНЕ НА ЕНЕРГОЕФЕКТИВНИ МЕРКИ“<br />",
         "68134.406.19. 2", "V - „ЗА АДМИНИСТРАТИВНА СГРАДА, НСИ”", "„ЛИЛЯНА ДИМИТРОВА”",
         "РАЙОН „ОБОРИЩЕ”, ГР. СОФИЯ, УЛ. „ПАНАЙОТ ВОЛОВ” №2", "535", "СОФИЯ", "София", "София", "ТАЙНО ИМЕ",
         "Разрешение за ползване", "Издаден", "ДНСК", "Физическо лице", "1234567890", "СОФИЯ", ""]]
# както TablePress: JSON с \\uXXXX, вграден в JS низ с единични кавички (\\ -> \\\\, ' -> \\')
json_text = json.dumps(rows, ensure_ascii=True).replace("/", "\\/")
js_literal = json_text.replace("\\", "\\\\").replace("'", "\\'")
eq(decode_js_string(js_literal), json_text)
# реален откъс от dnsk.bg
eq(json.loads(decode_js_string('[["","\\u0414\\u041d\\u0421\\u041a26-\\u0420\\u041f-532","\\u003Ca href=\\"https:\\/\\/dnsk.bg\\/x.pdf\\"\\u003EPDF\\u003C\\/a\\u003E"]]')),
   [["", "ДНСК26-РП-532", '<a href="https://dnsk.bg/x.pdf">PDF</a>']])
page = ('<table id="tablepress-44" class="tablepress"><thead><tr class="row-1">'
        '<th class="column-1">Виж <br />\nинфо</th><th class="column-2">№ <br />\nна акта</th><th class="column-3">Разрешение <br />\nза ползване</th>'
        '<th class="column-4">Дата <br />\nна акта</th><th class="column-5">Административен <br />\nорган</th><th class="column-6">Описание/<br />\nНаименование <br />\nна обект </th>'
        '<th class="column-7">Кадастрален идентификатор</th><th class="column-8">УПИ</th><th class="column-9">Местност</th>'
        '<th class="column-10">Населено място <br />\nРайон (обект)</th><th class="column-11">Квартал</th><th class="column-12">Област <br />\n(обект)</th>'
        '<th class="column-13">Област <br />\n(адм.)</th><th class="column-14">Район <br />\n(адм.)</th><th class="column-15">Наименование ЮЛ/<br />\nИмена ФЛ</th>'
        '<th class="column-16">Тип</th><th class="column-17">Състояние</th><th class="column-18">Администрация</th><th class="column-19">Вид на <br />\nвъзложителя</th>'
        '<th class="column-20">ЕГН/Булстат/ЕИК</th><th class="column-21">Община <br />\n(обект)</th><td class="column-22"></td></tr></thead><tbody></tbody></table>'
        "<script>window.DT_TP_data=window.DT_TP_data||{}; window.DT_TP_data['44']=JSON.parse('" + js_literal + "');</script>")
recs, heads = parse_year_page(page)
r = recs[0]
eq(r["number"], "ДНСК26-РП-527")
eq(r["pdf"], "https://dnsk.bg/wp-content/uploads/2026/10/dnsk26-rp-527.pdf")
eq(r["cad_id"], "68134.406.19. 2")
eq(r["kv"], "535")
eq(r["municipality"], "СОФИЯ")
eq(r["object"], "„ВЪВЕЖДАНЕ НА ЕНЕРГОЕФЕКТИВНИ МЕРКИ“")
eq("ТАЙНО ИМЕ" in json.dumps(r, ensure_ascii=False), False, "personal name must not be stored")
eq("1234567890" in json.dumps(r, ensure_ascii=False), False, "ЕГН must not be stored")

print(f"all {ok} checks passed")
