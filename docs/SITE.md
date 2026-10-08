# Static site — Бюджетен монитор Несебър

Everything under `src/nessebar_budget/web/` builds a fully static,
dependency-free site from the local database. No server, no FastAPI, no JS
framework or build step — just Jinja2 templates rendered once at build time,
plus one CSS file and one small vanilla-JS file for client-side table
sort/search/filter (progressive enhancement: tables are fully readable with
JS off).

The site is designed to be published via **GitHub Pages under a sub-path**
(e.g. `https://<user>.github.io/NessebarBudgetChecker/`), so every link and
asset reference in every template is **relative** (`../contracts/...`,
`static/style.css`, never `/contracts/...`). `tests/test_site_build.py`
asserts this holds for the built output.

## Build / preview

```bash
.venv/bin/python -m nessebar_budget.web.build --out site   # builds ./site (gitignored)
cd site && python3 -m http.server 8000                     # preview at http://localhost:8000/
```

`build_site(out_dir: Path, db_url: str | None = None)` is also importable
directly for scripting/tests. Build is idempotent (wipes and recreates
`out_dir` each run) and takes well under a second against the current
database — the 30s budget in the task brief is not a concern at this data
volume.

For CI/CD (GitHub Actions building and deploying this to Pages), see
`docs/DEPLOY.md`.

## Pages

| Page | Content |
|---|---|
| `index.html` | Deliberately short (~3–4 desktop screens): hero, a 4-tile stat strip (contracts total, capital plan spent vs. plan with progress bar, cash YTD, open flags by severity), max 5 latest flags (one per rule first), one "contracts by year" chart, a 3-column "how it works", footer. |
| `contracts/index.html` | Every ЦАИС ЕОП procurement (signed + open), client-side sortable/searchable/filterable table, per-year value chart. |
| `contracts/<source_id>.html` | One page per procurement: all fields, matched SIGMA enrichment (if any), source link, applicable flags. |
| `contractors/index.html` | Contractors aggregated from signed contracts: count, total value, share of total, single-bidder share. |
| `contractors/<slug>.html` | One contractor's detail + contract list. `<slug>` is a URL-safe key, not always the raw EIK — see "Contractor identity" below. |
| `budget/index.html` | Latest-month capital ledger (consolidated `Общо` unit): plan vs. spent by function, month-over-month plan changes (new/increased objects), full object table. |
| `budget/<period>.html` | Same breakdown for one historical month (e.g. `budget/2026-08.html`). |
| `cash/index.html` | Cash execution (B1) YTD by expenditure paragraph, biggest categories, optional Minfin quarterly comparison (Nessebar vs. median of all municipalities) when the quarterly workbook parses cleanly. |
| `flags/index.html` | All flags: a tier filter (segmented control: Всички / Нарушения / Сигнали / Непрозрачност) alongside severity/rule/**година** filters and search (all combine), tier+severity+rule chips per row, counts per tier and per severity. The row's date shows both the event year and the detection date (see "Event year" below) — a small bold year above the muted detection date, right-aligned next to the chips. Each row is a native `<details>`/`<summary>` — no JS needed to expand it — revealing the explanation, legal basis, documents to request (ЗДОИ), and a small numbers list parsed out of `details_json` (never the raw JSON), then a closed-by-default "Източници" box (see "Provenance box" below). Renders an empty state gracefully if no flags exist yet. |
| `flags/<id>.html` | One permalink page per flag: title = rule label, tier + severity chips, message, explanation, legal basis, documents list, numbers, an "Източници" section (collapsible, closed by default), subject link, first/last-seen dates, a 3-step "Как да поискате документите" box, and a "Копирай текст за заявление" `<details>` with a static, pre-filled plain-text ЗДОИ request template (copy button via `static/app.js`). Linked from every flag row and from Telegram deep links `flags/#<id>` (the bare numeric `id` is the row's anchor on the index page too). |
| `methodology.html` | Sources, update cadence, the eop/SIGMA dedupe heuristic, a "За какви нарушения следим" section (what misconduct the three tiers cover, in plain Bulgarian, incl. which Наказателен кодекс offences a *signal* may relate to once documents confirm it), a data-driven "Правила за сигнали" section (one entry per rule — label, tier, what it detects, thresholds, legal basis, suggested documents — built from `build.py`'s `METHODOLOGY_RULES`, content sourced from `docs/RULES.md`), "Какво можете да направите" (the ЗДОИ route and which bodies to signal — АДФИ, АОП, Сметна палата, managing authority/OLAF, prosecution only with evidence), and the "flags are not proof of wrongdoing" disclaimer. |
| `map.html` | "Карта на разходите" — a Leaflet map, one circle marker per settlement of Община Несебър (14 towns/villages + the resort areas Слънчев бряг and Елените), sized by sqrt-scaled all-years (or selected-year) spent, popup with plan/spent/objects/contracts/flags and a link to the settlement page; a year selector (segmented control); a toggle that adds a live АГКК/КАИС cadastre overlay (a plain Leaflet image overlay); a curated-pins layer; a ranking table below the map. See "Карта по населени места" below. |
| `settlements/index.html` | Ranking table of all 16 settlements: all-years spent, last-3-years columns, objects, contracts, flags, plus an "unassigned" row so the table's numbers are traceable against the budget pages. |
| `settlements/<key>.html` | One settlement's detail: header (name/kind/EKATTE), 4 KPI cards, a per-year table (plan/spent/objects/change vs. previous year, with a note on any year using a fallback month), a sortable objects table (name/period/plan/spent/cadastral identifiers linked to KAIS), a contracts list, a flags list, and a "Как е изчислено" box naming exactly which rows/aliases/cadastral prefix produced the numbers. |
| `data/index.html` | Links to every export below, plus a summary of current counts. |

## Data exports (`site/data/`)

| File | Contents |
|---|---|
| `contracts.csv` / `contracts.json` | One row per distinct `eop` procurement (514 at last build), with SIGMA enrichment columns (`bids_received`, `sigma_source_id`, `sigma_unp`, `sigma_eu_funded`) when a match was found. |
| `budget_line_items.csv` | Every `budget_line_items` row, all months and units (not just the consolidated `Общо` one shown in the HTML pages), with `row_type` (`object` / `paragraph_subtotal` / `function_subtotal` / `grand_total`) pulled out of `extra_json`. |
| `cash_execution.csv` | Every `cash_execution_lines` row, all months, both `приходи`/`разходи` sections. |
| `flags.json` | All flags: `tier`, `tier_label`, `severity`, `message`, `explanation`, `documents` (list), `law_ref`, `details`, `event_year` (int or null, see "Event year" below), `sources` (the raw `sources_json` provenance list, see docs/RULES.md), `subject_href`/`subject_label`, `permalink`, and the usual timestamps — whatever optional columns the live `flags` table currently has, see "Flag schema" below. |
| `map.json` | GeoJSON `FeatureCollection`, one Point feature per settlement (`properties`: key/name/kind/ekatte/href, `all_years` and per-year `plan`/`spent`/`objects_count`, `contracts_count`/`contracts_total_eur`, `flags_count`), plus top-level `unassigned` (same shape, for objects with no recognised settlement), `years_available`, `notes` (which years used a fallback month instead of December) and `pins` (curated exact coordinates, see below). Consumed client-side by `map.html`'s own script — never embedded inline in the HTML. |
| `settlements.csv` | The same per-settlement totals as `map.json`, flattened to one row per settlement (plus an `unassigned` row), for spreadsheet use. |
| `meta.json` | Build timestamp, headline counts, and the SIGMA-match statistics also quoted in `methodology.html`. |

## Карта по населени места (the map feature)

`src/nessebar_budget/web/geo.py` turns the budget ledger, contracts and flags
into "where does the money go, by settlement" — deterministically, by text
matching, never by geocoding or guessing.

**Settlement data.** `src/nessebar_budget/web/data/settlements.json` is a
static, hand-verified list of the municipality's 14 EKATTE-coded settlements
plus the two resort areas (Слънчев бряг, in Несебър's own землище; Елените,
in Свети Влас's) that have no EKATTE of their own. For each: `key` (slug),
`name`, `kind` (`град`/`село`/`курорт`), `ekatte`, `lat`/`lon`, `aliases` (the
literal strings matched in text) and a `source` (URL + 2026-10-08). EKATTE
codes were cross-checked two ways — against bg.wikipedia.org's "Селище в
България" infobox for each settlement, and against the KAIS cadastral-id
prefixes already present in this project's own scraped `object_name`/`title`
text (e.g. "ПИ 61056.501.505, с.Равда" confirms EKATTE 61056 = Равда).
Coordinates are each settlement's top OpenStreetMap Nominatim result. The
build **never** calls the network — this file is read once, at import time.

**Detection (`detect_settlement`/`detect_settlements`).** Every alias is
matched case-insensitively, at a word boundary, with periods/whitespace
treated as flexible ("Св.Влас", "Св. Влас" and "Свети Влас" all match the
same settlement). Cadastral identifiers ("ПИ 51500.501.4") are also
resolved, via their 5-digit EKATTE prefix. When several settlements are
named in one string, `detect_settlement` returns the one appearing
**first in the text**; `detect_settlements` returns the full ordered list, so
callers can flag the text as ambiguous (`len(...) > 1`). Capital-programme
objects use `object_settlements` instead: column B of the report is
"наименование, местонахождение и функционално предназначение", and the
municipality writes the location after the final comma ("... път Слънчев
бряг - Тънково, с.Тънково"), so when that tail names exactly one settlement
it wins over places mentioned earlier in the name (verified against the
2024-12 report, sheet "Общо", row 248). The one deliberate
exception: the bare alias "Несебър" does **not** match when it's clearly
naming the *municipality* as contracting authority ("Община Несебър", "общ.
Несебър") rather than the town as a location — otherwise nearly every
procurement title (which almost all say "за нуждите на Община Несебър"
somewhere) would wrongly look like it's "in" the town of Несебър. Every
other alias, for every other settlement, has no such ambiguity in this
project's data (see `tests/test_geo.py` for the real DB strings this was
verified against, including the "Баня" case: only the `с.`-prefixed forms
are aliases, since bare "баня" is also the ordinary Bulgarian noun for
"bath/spa").

**Aggregation (`build_map_data`).** Reuses `build.py`'s own `_load_budget`/
`_row_type` (via a function-local import — `geo.py` can't import `build.py`
at module level, since `build.py` imports `geo`), so it only counts real
`object`-type rows of the consolidated `Общо` unit, exactly like
`budget/<period>.html` does. For each year, the **December** report is used
as that year's full-year snapshot; when a year has no December report yet
(in progress, or a scraping gap — 2023 and 2026 at the time of writing), the
latest available month is used instead and the year is annotated with a
`note` saying so, both in the GeoJSON and on each settlement's own page. An
object naming several settlements is counted only under the **first** one
(so settlement totals plus `unassigned` always sum to exactly the same
grand total `budget/<period>.html` shows — `tests/test_site_build.py`
checks this for one clean December year); a contract naming several
settlements, by contrast, is counted in **full** under every one of them
(there being no reconciliation requirement for contracts the way there is
for the budget ledger), so the sum of settlements' contract totals can
exceed the sitewide contracts total. Flags are matched on
`details_json.object_name`, falling back to the linked contract's title.

**Cadastre overlay caveat.** `map.html`'s "Кадастрална карта (АГКК)" toggle
adds a plain `L.imageOverlay` whose picture is re-requested from the public
АГКК/КАИС ArcGIS server's `export` endpoint
(`arcgisnopki/rest/services/InternalKais/CmcrPublic/MapServer/export`,
layers 1/2/15/17, Web Mercator bbox = the current viewport) on every
move/zoom, from zoom 17 up (below that the server draws almost nothing).
No ArcGIS client library is used on purpose: the server sends no CORS
headers and its service-metadata endpoint is blocked by browsers (ORB), so
Esri Leaflet never gets past initialisation; a bare `<img src>` works
because the browser's own `Referer` header is what the server requires
(verified in Chrome on 2026-10-08). It is a visual reference only, not part
of this site's own data, and it may be temporarily unavailable (it's someone
else's server).
KAIS's own map page (`kais.cadastre.bg/bg/Map`) has no verified deep-link
format for opening one specific parcel by identifier (probed with query
strings on 2026-10-08 — it's a client-rendered SPA that ignores them), so
every cadastral-identifier link on this site opens the plain map page and
shows the identifier as text to paste into KAIS's own search box —
`geo.kais_url()`.

**Curated pins (`data/locations/pins.csv`).** The map's settlement markers
are one point per settlement, not per object. A volunteer can add an exact,
verified coordinate for one specific object (found in KAIS) as a row in
`data/locations/pins.csv` (columns: `object_name`, `period`, `lat`, `lon`,
`cadastral_id`, `verified_by`, `source_note`, `verified_at`) — see
`data/locations/README.md` for the step-by-step. `geo.ensure_pins_scaffold()`
creates that CSV (header row only) and its README on first build if they
don't exist yet, and `geo.load_pins()` reads whatever rows are there into
`map.json`'s `pins` array, rendered as a second Leaflet layer. No pin is
required for a settlement's own totals to show up — pins are purely an
optional, human-verified precision layer on top.

## Dedupe & enrichment: ЦАИС ЕОП + SIGMA

ЦАИС ЕОП and SIGMA largely describe the *same* contracts, so the contracts
pages show **only the `eop` record**, enriched with SIGMA's `bids_received`
(and a few extra fields) when a match is found — never both as separate
rows. See `build.py`'s `_match_sigma()` and `methodology.html#dedupe` for the
full writeup; in short:

- same `contractor_eik`,
- `contract_value_eur` within **±1%**,
- `contract_date` within **±3 days**,
- ties broken by closest date then closest value, each SIGMA row used at most once.

At the last build: of 411 signed `eop` contracts with both an EIK and a
value, **374 matched** a SIGMA row, 37 did not, and 6 had more than one
candidate (resolved by the tie-break above). These exact numbers are
recomputed on every build and written to `data/meta.json`'s `sigma_dedupe` key
— they will drift slightly as new contracts are scraped.

## Contractor identity (a data-quality note)

A handful of `contractor_eik` values in the source data aren't real EIKs:
four rows carry the literal placeholder `"не се публикува"` ("not
published" — Bulgarian law lets an individual's EIK/EGN go unpublished), and
two rows carry **two** EIKs joined with `"; "` (a joint-venture/consortium
award). Grouping by the raw `contractor_eik` string would either wrongly
merge unrelated individuals under one fake "contractor", or produce a URL
with a space/semicolon in it. `_contractor_group_key()` in `build.py` falls
back to the contractor *name* when the EIK is missing/placeholder (so e.g.
two contracts by the same named individual still aggregate together, but
contracts by two different individuals don't), and `_slugify()` turns
whatever key results into a clean, ASCII, filesystem/URL-safe path segment
for `contractors/<slug>.html`. The contractor page then shows "ЕИК не е
публикуван" instead of fabricating one.

## Flag schema (forward-compatible by design)

`src/nessebar_budget/db/models.py`'s `Flag` model carries (among others)
`subject_type`, `subject_id`, `subject_key`, `details_json`, `law_ref`,
`tier`, `explanation`, `documents_json`. Until a given DB file has actually
been migrated to include a given column, `select(Flag)` (every *mapped*
column) raises `OperationalError: no such column`. `build.py`'s
`_load_flags_safe()` works around this by introspecting the live `flags`
table and selecting only the columns that actually exist there right now —
so the build never breaks whether it's running against an old or a new
schema. Everywhere else, optional fields are read with
`getattr(flag, "field", None)` and `_flag_view()` renders gracefully around
whatever is missing:

- **`tier`** missing/NULL: `tier_key()` falls back to a per-rule guess
  (`_TIER_FALLBACK_BY_RULE`), then to severity — the tier filter/chips never
  show an "unknown" bucket, but a real `tier` value always wins once present.
- **`explanation`** missing/NULL: the flag's existing `message` is reused as
  the explanation body.
- **`documents_json`** missing/empty: a generic, subject-type-keyed document
  list (`_DEFAULT_DOCUMENTS_BY_SUBJECT_TYPE`) stands in, specific enough to
  be a reasonable ЗДОИ starting point.
- **`details_json`** values are never dumped as raw JSON: `_detail_items()`
  maps each key to a Bulgarian label and a formatted value (EUR/BGN/%/date/
  period/day-count/boolean/known-code lookups in `_format_detail_value()`),
  skipping bookkeeping keys and anything too structured (a nested list of
  records, a dict) to show as one line.

- **`sources_json`** missing/empty: the "Източници" box is simply not
  rendered (every flag gets sources on the next `analyze`).

### Event year ("година на възникване")

`_event_year()` in `build.py` resolves, for every flag, the year the
underlying *event* happened — as opposed to `created_at`/`first_seen_at`
(when our scraper/analyzer *detected* it), which is what the row used to
show alone:

- **budget-object flags**: the ledger period in `details_json` — `period`
  (most rules), `to_period` (plan_jump's later side) or
  `first_period`/`from_period` (plan_jump's "new object" case) — whichever
  key that rule populated, "YYYY-MM" -> its year;
- **report flags** (`missing_monthly_report`/`missing_annual_report`):
  `details_json.year` (annual) or `period`/`expected_period` (monthly);
- **everything else** — contract/procedure/contractor-group flags,
  including the `eu_funded_irregularity` meta-flag (it inherits its
  subject contract's `procurement_id`) — the matched contract's own `year`
  (same value `contracts/index.html` groups by: `contract_date`, falling
  back to `published_at`). `None` when no contract resolves, e.g.
  `contractor_concentration`, which spans many contracts and has no single
  event date — such flags simply don't match any specific year in the
  filter (they still show under "Всички години").

`flags/index.html`'s "Година" `<select>` (`data-table-filter="year"`,
options = distinct `event_year` values present, descending, plus "Всички
години") combines with the existing search/tier/severity/rule filters the
same way the severity/rule `<select>`s already do (a `data-year` attribute
per row, matched by the generic filter in `app.js`). No `?year=` URL-param
preselect yet — none of the existing filters support URL params either, so
this was left for a later pass rather than added inconsistently for one
filter only.

### Provenance box ("Източници")

`_macros.html`'s `sources_box(f, root)` renders `_flag_view()`'s `sources`
(built by `_source_views()` from `Flag.sources_json`) as a native
`<details class="sources">`, closed by default, in the flag row panel on
`flags/index.html` and on every `flags/<id>.html`. Per source: the label as
a link to the original public URL (external arrow icon, `target="_blank"`;
`external_href()` percent-encodes the Cyrillic/space file names), the file
name in mono; for a `budget_file` source **with a known row**, a bold
"Обект: **<name>** — търсете този ред по името на обекта (ред N), не по
параграфа" line *before* the "лист X, ред Y" line (a beta tester opened the
file, read the "5200 Придобиване на дълготрайни материални активи"
*paragraph subtotal* row and saw different numbers — the flag's figures are
on the *object* row, not that subtotal; `_flag_view()` attaches
`source.object_name` from the flag's own `details_json.object_name` for
exactly this case); then "лист X, ред Y" when known, a compact Поле /
В източника / В евро table (raw value as published -- leva for pre-2026
budget files -- and the EUR value; API keys shown under their Bulgarian
gloss; a "Параграф" row in the *numbers* grid above this box carries a
"категория (§), не редът на обекта" tooltip for the same reason), and the
note. A `flag`-kind source links to that flag's permalink. If any value is
in leva, one line says "Стойностите са публикувани в лева и са преобразувани
по фиксирания курс 1 € = 1,95583 лв."; the box ends with "Как да проверите"
as 2-3 short numbered steps from `_sources_how_to_steps()`, chosen by source
kind: a budget file gets "отворете файла (Excel/LibreOffice)" / "Ctrl+F за
името на обекта (или отидете на ред N)" / "сравнете тези три колони,
стойностите тук са и в евро"; a ЦАИС ЕОП/SIGMA record gets "отворете
поръчката/договора" / "потърсете тези полета" (named from the source's own
field list) / "сравнете стойностите"; a report-archive source keeps a
period-based 3-step version. Not shown on the home page or contract/budget
pages.

Every flag-rendering template (`flags_index.html`, `flag_detail.html`,
`contract_detail.html`, `_budget_body.html`, the home page's `flag_row`
macro) consumes the *same* `_flag_view()`-shaped dict, so tier chips,
messages and permalinks are consistent everywhere a flag appears.

## Design

Linear-inspired, dark by default (`prefers-color-scheme: light` swaps the
same tokens for an off-white theme). Everything is driven by the custom
properties at the top of `static/style.css`: near-black canvas (`#08090a`,
surfaces `#0f1011`/`#141516`), primary text `#f7f8f8`, secondary `#8a8f98`,
hairline borders `rgba(255,255,255,0.08)`, one indigo accent (`#5e6ad2`) used
only for the primary button, progress bars, the latest chart bar and focus
rings. Spacing scale 4/8/12/16/24/32/48/64/96/128; 8px radii; no shadows and
no gradients except the faint radial glow behind the home hero. Severity is
shown as a coloured dot + text (red/orange/grey), never a filled badge.
Tier reuses the same pattern with its own three colours (`violation`/`signal`
share the high/warning severity colours, `opacity` gets its own muted
blue-grey `--tier-opacity`) — see `tier_key`/`tier_label`/`tier_tooltip` in
`build.py` and the `.dot--violation`/`.dot--signal`/`.dot--opacity` rules in
`static/style.css`.

Type is Inter (Google Fonts, `system-ui` fallback) with tight tracking on
headlines; every number uses `font-variant-numeric: tabular-nums`.

**Numbers.** All rendered numbers go through three Jinja filters defined in
`build.py`: `num` (`12,347,905`), `eur` (`12,347,905 €`) and `pct` (a
fraction: `0.784` → `78.4%`). English-style comma thousands separators, no
decimals for amounts. CSV/JSON exports stay raw numeric. `sevkey`/`sevlabel`
normalise any severity string to `high`/`warning`/`info`.

**Layout.** `base.html` holds the sticky 56px nav (collapses to a
horizontally scrollable link row on phones — no JS menu — with a `mask-image`
fade on the right edge hinting there's more, and a tiny inline script that
scrolls the active link into view on load), and the footer. `_macros.html`
holds the shared pieces: `page_head`, `figure` (big number with a smaller
unit), `meter`, `chip`/`sev_chip`, `flag_row`, `search`, `vbar_chart`, and
`cell_label` (a muted field label shown only above a value in the phone
stacked-table layout, via the `.cell-label` class — hidden on desktop, where
the column header already says it). `_budget_body.html` is shared by
`budget/index.html` and `budget/<period>.html`. Tables: hairline rows,
right-aligned tabular numbers, header row sticky under the nav on ≥1000px;
on phones (≤719px) every data table (`.table--stack`) turns into stacked
card-rows instead of scrolling sideways — primary text on top, the rest as a
2-column label/value grid underneath (labels from `cell_label` or from
context, per `.table--<name>` overrides next to the `.table--stack` rules in
`style.css`). Budget-by-function and cash-by-paragraph keep their inline
meters, which become full-width rows under the label on phones instead of a
separate chart + duplicate table. Touch targets (nav links, buttons, filter
selects, table row links) are ≥44px and inputs use 16px text on phones
(≤719px) only — the ≥1000px desktop layout is untouched by any of this.

**Charts** are hand-written inline SVG (no library, no external request).
`vbar_chart` uses an SVG without a `viewBox`: x/width in percent so it fills
any container, y/height in pixels so text never scales. Bars are muted,
the latest year is the one accent bar, only the max and latest bars get a
value label, every bar has a native `<title>` tooltip.

`static/app.js` (sort/search/filter, plus `/` to focus search) is loaded on
every page from `base.html`; tables remain fully readable with JS off.

## Not yet surfaced

- Per-unit (кметства/schools) capital-ledger breakdowns exist in
  `budget_line_items` (46 distinct `unit` values) but only the consolidated
  `Общо` rows are rendered as pages — out of scope for this pass; the full
  per-unit data is still in `data/budget_line_items.csv`.
- The Minfin quarterly comparison on `cash/index.html` is best-effort: it's
  skipped silently (no error) if the workbook's shape doesn't match what
  `_load_minfin_indicators()` expects.
