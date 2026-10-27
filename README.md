# Карта на сградите в София с разрешенията им

Онлайн карта (https://karta-sofia.pages.dev) на сградите и поземлените имоти в гр. София (отворени данни на АГКК)
с документите за тях от публичните регистри:

| Регистър | Източник |
|---|---|
| Разрешения за строеж | НАГ – nag.sofia.bg/RegisterBuildingPermitsPortal |
| Удостоверения за въвеждане в експлоатация | НАГ – nag.sofia.bg/RegisterCertificateForExploitationBuildings |
| Разрешения за ползване | ДНСК – dnsk.bg (по години) |

## Как работи

Два пъти седмично (понеделник и четвъртък) GitHub Actions (`.github/workflows/update.yml`):
1. `agent/harvest_nag.py`, `agent/harvest_dnsk.py` – теглят само новите записи (НАГ – докато стигне до вече известни;
   ДНСК – текущата и предходната година). Целите регистри се минават само при първото пускане или ръчно
   (Actions → Run workflow → „Пълно обхождане“);
2. `agent/link.py` – свързва ги със сградите/имотите: по идентификатор, по квартал + УПИ (+ планоснимачен №),
   УВЕ ↔ РС по линка в НАГ, и ръчно от `data/manual_links.csv`;
3. записва `site/idx/docs/*.json` и `site/idx/status.json` и публикува `site/` в Cloudflare Pages;
4. `out/nesvarzani.csv` – документите, които не са свързани автоматично.

Пазим минимум данни: номер, дата, статус, издател, обект, обхват и линк към документа при източника.
**Не се пазят** възложители (имена на физически лица) и ЕГН/Булстат.

## Ръчно свързване

Отвори `out/nesvarzani.csv`, попълни колоната `identifier` (един или няколко кадастрални идентификатора)
и добави редовете (`key;identifier`) в `data/manual_links.csv`.

## Сгради и имоти (АГКК)

Файлът `sofia-map.pmtiles` е в Cloudflare R2. При ново изтегляне от АГКК:
```
python3 tools/build_map.py <сгради> <имоти> <изход>       # pmtiles + CSV с атрибути
python3 tools/build_search.py <сгради> <имоти> site        # индекс за търсене по идентификатор
python3 agent/build_cadastre_index.py <изход>/sofia-buildings-attributes.csv <изход>/sofia-parcels-attributes.csv
```

## Настройки (GitHub → Settings → Secrets and variables → Actions)

- `CLOUDFLARE_API_TOKEN` – токен с право „Cloudflare Pages: Edit“
- `CLOUDFLARE_ACCOUNT_ID` – идентификаторът на Cloudflare акаунта
