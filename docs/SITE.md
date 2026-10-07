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
| `flags/index.html` | All flags: a tier filter (segmented control: Всички / Нарушения / Сигнали / Непрозрачност) alongside severity/rule filters and search, tier+severity+rule chips per row, counts per tier and per severity. Each row is a native `<details>`/`<summary>` — no JS needed to expand it — revealing the explanation, legal basis, documents to request (ЗДОИ), and a small numbers list parsed out of `details_json` (never the raw JSON), then a closed-by-default "Източници" box (see "Provenance box" below). Renders an empty state gracefully if no flags exist yet. |
| `flags/<id>.html` | One permalink page per flag: title = rule label, tier + severity chips, message, explanation, legal basis, documents list, numbers, an "Източници" section (collapsible, closed by default), subject link, first/last-seen dates, a 3-step "Как да поискате документите" box, and a "Копирай текст за заявление" `<details>` with a static, pre-filled plain-text ЗДОИ request template (copy button via `static/app.js`). Linked from every flag row and from Telegram deep links `flags/#<id>` (the bare numeric `id` is the row's anchor on the index page too). |
| `methodology.html` | Sources, update cadence, the eop/SIGMA dedupe heuristic, a "За какви нарушения следим" section (what misconduct the three tiers cover, in plain Bulgarian, incl. which Наказателен кодекс offences a *signal* may relate to once documents confirm it), a data-driven "Правила за сигнали" section (one entry per rule — label, tier, what it detects, thresholds, legal basis, suggested documents — built from `build.py`'s `METHODOLOGY_RULES`, content sourced from `docs/RULES.md`), "Какво можете да направите" (the ЗДОИ route and which bodies to signal — АДФИ, АОП, Сметна палата, managing authority/OLAF, prosecution only with evidence), and the "flags are not proof of wrongdoing" disclaimer. |
| `data/index.html` | Links to every export below, plus a summary of current counts. |

## Data exports (`site/data/`)

| File | Contents |
|---|---|
| `contracts.csv` / `contracts.json` | One row per distinct `eop` procurement (514 at last build), with SIGMA enrichment columns (`bids_received`, `sigma_source_id`, `sigma_unp`, `sigma_eu_funded`) when a match was found. |
| `budget_line_items.csv` | Every `budget_line_items` row, all months and units (not just the consolidated `Общо` one shown in the HTML pages), with `row_type` (`object` / `paragraph_subtotal` / `function_subtotal` / `grand_total`) pulled out of `extra_json`. |
| `cash_execution.csv` | Every `cash_execution_lines` row, all months, both `приходи`/`разходи` sections. |
| `flags.json` | All flags: `tier`, `tier_label`, `severity`, `message`, `explanation`, `documents` (list), `law_ref`, `details`, `sources` (the raw `sources_json` provenance list, see docs/RULES.md), `subject_href`/`subject_label`, `permalink`, and the usual timestamps — whatever optional columns the live `flags` table currently has, see "Flag schema" below. |
| `meta.json` | Build timestamp, headline counts, and the SIGMA-match statistics also quoted in `methodology.html`. |

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

### Provenance box ("Източници")

`_macros.html`'s `sources_box(f, root)` renders `_flag_view()`'s `sources`
(built by `_source_views()` from `Flag.sources_json`) as a native
`<details class="sources">`, closed by default, in the flag row panel on
`flags/index.html` and on every `flags/<id>.html`. Per source: the label as
a link to the original public URL (external arrow icon, `target="_blank"`;
`external_href()` percent-encodes the Cyrillic/space file names), the file
name in mono, "лист X, ред Y" when known, a compact Поле / В източника /
В евро table (raw value as published -- leva for pre-2026 budget files --
and the EUR value; API keys shown under their Bulgarian gloss), and the
note. A `flag`-kind source links to that flag's permalink. If any value is
in leva, one line says "Стойностите са публикувани в лева и са преобразувани
по фиксирания курс 1 € = 1,95583 лв."; the box ends with a one-sentence
"Как да проверите" chosen by source kind (file -> sheet/row; ЦАИС ЕОП/SIGMA
record -> fields; report archive -> period). Not shown on the home page or
contract/budget pages.

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
