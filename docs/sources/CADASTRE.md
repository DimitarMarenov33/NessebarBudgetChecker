# АГКК/КАИС — cadastral-map open data (verified 2026-10-08)

The one and only source for "Точни места" (`src/nessebar_budget/web/locate.py`,
`src/nessebar_budget/scrapers/cadastre.py`): placing a budget object or
ЦАИС ЕОП contract at an exact point on `map.html` when its text names a
cadastral parcel or street, instead of only its settlement. Deterministic
only — no geocoding, no fuzzy matching, no AI/LLM.

## Endpoint

```
GET https://kais.cadastre.bg/bg/OpenData/Download?path=<urlencoded path>
```

No session, no auth, no query signing — a plain GET with a browser-like
`User-Agent` (`Mozilla/5.0 ... Chrome/120.0.0.0 Safari/537.36`) is enough;
confirmed with a bare `curl`. The response is `Content-Type: application/zip`;
**no `Last-Modified` header is sent** (checked by inspecting real response
headers for the с. Равда zip) — `CadastreScraper`/`CadastreParcel.
source_modified` therefore always records the *download* date, not a
true "last published" date from the server.

`path` is `"област Бургас/община Несебър/<label>/поземлени имоти.zip"`, one
file per settlement. The 14 labels (EKATTE in parens, matching
`web/data/settlements.json`):

| EKATTE | Label |
|---|---|
| 51500 | гр. Несебър (51500) |
| 53045 | гр. Обзор (53045) |
| 11538 | гр. Свети Влас (11538) |
| 02703 | с. Баня (02703) |
| 18469 | с. Гюльовца (18469) |
| 27454 | с. Емона (27454) |
| 37825 | с. Козница (37825) |
| 39164 | с. Кошарица (39164) |
| 53822 | с. Оризаре (53822) |
| 55350 | с. Паницово (55350) |
| 58431 | с. Приселци (58431) |
| 61056 | с. Равда (61056) |
| 62102 | с. Раковсково (62102) |
| 73571 | с. Тънково (73571) |

АГКК refreshes these roughly monthly (last seen 22.09.2026 at capture time);
Несебър's own zip (the town) is ~2.3 MB compressed. `CadastreScraper` caches
each settlement's zip at `data/cache/cadastre/<ekatte>_parcels.zip`
(gitignored) and reuses it if younger than 25 days.

**Only the "поземлени имоти" (land parcels) file is ever requested.** АГКК's
same folder also publishes "собственост ПИ/сгради/СОС" files (owners'
personal data) — this project never downloads those.

## Zip contents

One shapefile, "Имоти (Polygon)", as `.shp`/`.shx`/`.dbf`/`.prj`/`.cpg`.
Member names inside the zip are documented by АГКК as potentially
cp866-mangled; in practice they came back as plain UTF-8 (verified against a
real zip), but `read_parcels()` picks members by **file extension**, never
by an expected literal name, so either way works. `.cpg` = `utf-8` (the
`.dbf`'s text encoding). `cattype`/`place`/`parcel`/`quarname`/`regname`
fields are frequently empty strings — treat as "no value", not an error.

**CRS**: `BGS2005`, a Lambert Conformal Conic projection (read from the
`.prj`'s WKT with `pyproj.CRS.from_wkt`; transformed to WGS84/EPSG:4326 with
`pyproj.Transformer.from_crs(..., always_xy=True)`).

**Fields** (verbatim from the `.dbf`): `AREA`, `PERIM`, `cadimm`, `cadnum`
(e.g. `"61056.502.526"`), `cadreg`, `cattype`, `ekatte`, `ekattefn` (e.g.
`"с. Равда"`), `immaddr` (e.g. `"с. Равда, ул. Св.Св. Кирил и Методий"`),
`oldident` (an old-format code like `" 001001"` — **not** a previous
`cadnum`), `parcel`, `place`, `propcode`, `proptype` (e.g. `"Общинска
публична"`, `"Частна"`), `purpcode`, `purptype` (e.g. `"Урбанизирана"`),
`quarname`, `quarter`, `regname`, `strename` (e.g. `"ул. Св.Св. Кирил и
Методий"` — sometimes with no `"ул."` prefix at all, e.g. just `"СВОБОДА"`),
`strnum`, `usecode`, `usetype` (e.g. `"За второстепенна улица"`, `"За алея"`,
`"За площад"`, `"За местен път"`, `"Ниско застрояване (до 10 m)"`, `"Нива"`,
...), `validate`.

`read_parcels()` additionally computes, per parcel:
- `rings_lonlat`: every ring of the polygon, `[lon, lat]` pairs rounded to
  6 decimals, in shapefile vertex order (so `rings_lonlat[0][0]` is the
  polygon's first vertex).
- `centroid_lonlat`: area-weighted centroid, computed with the standard
  shoelace-based polygon-centroid formula **in the projected CRS**, then
  transformed to lon/lat. For a multipart shape (several disjoint rings
  under one `cadnum`), only the single **largest ring by area** is used —
  a deliberate simplification (good enough for a representative point; the
  full geometry, every ring, is still kept in `rings_lonlat`/
  `geometry_geojson`).
- `geometry_geojson`: a GeoJSON `Polygon`/`MultiPolygon` geometry dict in
  lon/lat — ring grouping (which rings are holes of which exterior ring)
  comes from `pyshp`'s own `shape.__geo_interface__`, not reimplemented here.

### Verified check value (с. Равда, EKATTE 61056)

Parcel `"61056.502.526"`: first vertex `(678556.323, 4725533.024)` in the
projected CRS transforms to **lon 27.67765, lat 42.64455**; `AREA` =
301.17 m²; `immaddr` = `"с. Равда, ул. Св.Св. Кирил и Методий"`; `proptype`
= `"Общинска публична"`; `usetype` = `"За второстепенна улица"`. Confirmed
against a real download on 2026-10-08 (`.venv/bin/python` + `pyshp` +
`pyproj`, no wrapper code).

## What this project stores

Only the parcels this project's own scraped data actually references —
**not** every parcel of every settlement (keeps the committed
`data/nessebar.db` small): every cadastral identifier named by a distinct
capital-programme object name (consolidated `Общо` unit, row type `object`)
or a distinct `eop` procurement title, **plus** the "street parcels" (the
cadastre's own `usetype` contains "улица"/"алея"/"площад"/"път", and
`strename` is non-empty) of any street named the same way. See
`scrapers.cadastre.sync_cadastre`/`referenced_parcels_and_streets` for the
exact collection logic, and `web.locate` for the text-matching rules
(regex for a cadastral identifier, street-name extraction, `street_key`
normalization). `CadastreParcel` rows are replaced wholesale per settlement
(`ekatte`) on every `scrape cadastre` run — a parcel no longer referenced by
anything simply disappears on the next run, rather than lingering.

At the 2026-10-08 real run: 1,022 distinct budget-object names + 425
distinct `eop` titles scanned → 119 distinct parcel ids + 118 distinct
street references found → 196 parcels actually stored (committed-DB size
delta: +672 KiB). See the build report in the PR/commit this file ships
with for the live located/unresolved counts.

## Legal basis for reuse

Published by АГКК as open data (`OpenData` in the endpoint's own path).
Covered by ЗДОИ глава IV„а" — public-sector information for reuse
(чл. 41а and following; see `docs/law/zdoi.txt`). Attribution shown on
every page with a located item: **"© АГКК — кадастрална карта, отворени
данни (версия от &lt;датата на изтегляне&gt;)"**.

## Not done / open questions

- No programmatic way was found (or needed) to ask АГКК "has this zip
  changed since &lt;date&gt;" ahead of downloading it — `source_modified`
  is always the download date, as noted above (no `Last-Modified` header).
- Building/unit-level identifiers (a 4th dot-separated segment, e.g.
  `"51500.506.493.1"`) are never looked up directly — АГКК's land-parcels
  layer only ever contains 3-part parcel ids; `web.locate.
  extract_parcel_ids`/`parcel_lookup_keys` collapse a 4-part id to its
  parent 3-part parcel before any lookup.


## Same street name, different streets

Several separate streets can share a name inside one землище (verified
2026-10-08: six "ПЪРВА" street parcels in 51500, in six groups up to 5.2 km
apart). `locate.street_is_split` clusters a name's street parcels with a
1 km single-linkage gap; more than one group means the name is ambiguous,
so it is skipped and the next street named in the text is tried. Effect on
the 2026-10-08 data: 203 -> 189 placed items (14 "ул. Първа" items moved
back to settlement level).
