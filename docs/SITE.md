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
| `index.html` | Headline numbers (contract count/value by year, latest capital-plan execution, cash YTD), latest flags, a short "how this works" explainer, footer timestamp. |
| `contracts/index.html` | Every ЦАИС ЕОП procurement (signed + open), client-side sortable/searchable/filterable table, per-year value chart. |
| `contracts/<source_id>.html` | One page per procurement: all fields, matched SIGMA enrichment (if any), source link, applicable flags. |
| `contractors/index.html` | Contractors aggregated from signed contracts: count, total value, share of total, single-bidder share. |
| `contractors/<slug>.html` | One contractor's detail + contract list. `<slug>` is a URL-safe key, not always the raw EIK — see "Contractor identity" below. |
| `budget/index.html` | Latest-month capital ledger (consolidated `Общо` unit): plan vs. spent by function, month-over-month plan changes (new/increased objects), full object table. |
| `budget/<period>.html` | Same breakdown for one historical month (e.g. `budget/2026-08.html`). |
| `cash/index.html` | Cash execution (B1) YTD by expenditure paragraph, biggest categories, optional Minfin quarterly comparison (Nessebar vs. median of all municipalities) when the quarterly workbook parses cleanly. |
| `flags/index.html` | All flags with severity, plain-language message, (when present) law reference and link to the flagged subject. Renders an empty state gracefully if no flags exist yet. |
| `methodology.html` | Sources, update cadence, the eop/SIGMA dedupe heuristic, a plain-language summary of each anomaly rule (full depth in `docs/RULES.md`), and the "flags are not proof of wrongdoing" disclaimer. |
| `data/index.html` | Links to every export below, plus a summary of current counts. |

## Data exports (`site/data/`)

| File | Contents |
|---|---|
| `contracts.csv` / `contracts.json` | One row per distinct `eop` procurement (514 at last build), with SIGMA enrichment columns (`bids_received`, `sigma_source_id`, `sigma_unp`, `sigma_eu_funded`) when a match was found. |
| `budget_line_items.csv` | Every `budget_line_items` row, all months and units (not just the consolidated `Общо` one shown in the HTML pages), with `row_type` (`object` / `paragraph_subtotal` / `function_subtotal` / `grand_total`) pulled out of `extra_json`. |
| `cash_execution.csv` | Every `cash_execution_lines` row, all months, both `приходи`/`разходи` sections. |
| `flags.json` | All flags, with whatever optional columns (`law_ref`, `details_json`, ...) the live `flags` table currently has — see "Flag schema" below. |
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

`src/nessebar_budget/db/models.py`'s `Flag` model is being extended (by a
concurrent workstream) with columns like `subject_type`, `subject_id`,
`subject_key`, `details_json`, `law_ref`. Until a given DB file has actually
been migrated to include them, `select(Flag)` (every *mapped* column) raises
`OperationalError: no such column`. `build.py`'s `_load_flags_safe()` works
around this by introspecting the live `flags` table and selecting only the
columns that actually exist there right now — so the build never breaks
whether it's running against the old or the new schema. Everywhere else,
optional fields are read with `getattr(flag, "field", None)` and the
templates render whatever is present (see `flags/index.html` and
`contract_detail.html`).

## Design

Mobile-first, deliberately plain: near-black text on off-white, one accent
colour (used for links, the active nav underline, and chart bars), muted
greys for secondary text, hairline table rules (no card boxes, no shadows,
no gradients), system font stack, light/dark via `prefers-color-scheme`
(see `static/style.css`'s custom properties). Charts are hand-written inline
SVG bars in Jinja (`templates/_macros.html`'s `vbar_chart`/`hbar_chart`) —
no Chart.js, no external request. EUR amounts use a thin-space thousands
separator and no decimals (`fmt_eur` in `build.py`).

## Not yet surfaced

- Per-unit (кметства/schools) capital-ledger breakdowns exist in
  `budget_line_items` (46 distinct `unit` values) but only the consolidated
  `Общо` rows are rendered as pages — out of scope for this pass; the full
  per-unit data is still in `data/budget_line_items.csv`.
- The Minfin quarterly comparison on `cash/index.html` is best-effort: it's
  skipped silently (no error) if the workbook's shape doesn't match what
  `_load_minfin_indicators()` expects.
