# Budget-execution report forms — Община Несебър

What was actually learned from the sample files in `data/samples/` and
cross-checked against the official MinFin макети (`data/minfin/maketi/`,
unzipped from `maketi_quarterly_III_2026_municipalities.zip` and
`maketi_monthly_2026.zip`). Nothing below is guessed.

Originally written against the 2026 EUR-denominated forms only; extended
(§2, §3, §4) to cover the 2019-2025 BGN-denominated monthly archive once
`NesebarSiteScraper`'s background download reached those years. As of this
writing the download has reached 2019-2020 (full), 2021 (through July), and
2026 (full); 2021 (Aug-Dec) and 2022-2025 are still in progress -- see §4
for the quality-check run and what's still unverified.

## 1. `nesebar.bg/reports.html` — the archive page

Plain server-rendered HTML. Files live under `/03-2019/` (a folder name
fixed at the archive's 2019 start, unrelated to a file's actual date),
grouped under `<h4>` headings, almost all of the form:

    Отчети за касово изпълнение на бюджета към 31.08.2026 г.
    Тримесечен отчет за касово изпълнение на бюджета към 30.06.2026 г.

Each heading states the report's as-of date — this is a far more reliable
period signal than the filenames, and is what `NesebarSiteScraper` uses
primarily (`period_source="heading"`, confidence "high"); filename-based
inference (see below) is only a fallback for the rare link with no dated
heading above it.

Within one heading's group there are normally 7 files for a given month:
`B1_YYYY_M_5206.xls` + 5× `IB1_YYYY_M_5206_<SUFFIX>.xls` + one friendly-named
capital-ledger `.xlsx`. March and June additionally carry a `B3`/`IB3_*`
quarterly set alongside the monthly `B1`/`IB1_*` ones (quarter-end months);
these are folded into the same `B1`/`IB1_*` kinds (see §2) since they share
an identical schema — just a different reporting cadence.

Filename-based classification/period heuristics (`classify_filename` in
`scrapers/nesebar_site.py`):
- `B1_YYYY_M_5206.xls` / `B3_YYYY_M_5206.xls` → kind `B1`, (year, month)
  read straight out of the filename, confidence "high".
- `IB1_YYYY_M_5206_{DES,DMP,K33,KSF,RA}.xls` (or `IB3_...`) → kind
  `IB1_DES`/`IB1_DMP`/`IB1_K33`/`IB1_KSF`/`IB1_RA`, same extraction,
  confidence "high".
- Any other `.xlsx`: tried in order — (1) a Bulgarian month name
  (е.g. "Месечен отчет за 2026 Април 5206 Несебър.xlsx") plus a nearby
  4-digit year → "high"; (2) an English month name glued to a 2- or
  4-digit year (`august2026.xlsx`, `june26.xlsx`) → "medium"; (3) a
  trailing `MMYY` numeral (`otchet0726.xlsx`) → "low", only if the first
  two digits are a valid month 01-12. Otherwise kind stays `capital_xlsx`
  with no period (confidence "none") — this happens for a handful of old,
  irregularly-named files (`3mesechen-mart.xlsx`, `42022.xlsx`) that were
  deliberately not chased further given the task's scope.
- Everything else (PDF scans, `budget/Budget_YYYY_5206.xls` annual macro,
  energy-efficiency reports, ГФО audits, ...) → kind `other`. These are
  classified but **not downloaded** by `scrape nesebar_site` (only the
  7 budget-report kinds above are fetched), to avoid pulling the whole
  ~1,158-link, 2019-2026 archive on every run.

## 2. `B1`/`B3` cash-execution report (`*.xls`, old BIFF/xlrd format)

Sheet `OTCHET` is what matters; the workbook also has `Cash-Flow-DATA` and
`OTCHET-agregirani pokazateli` (aggregate plan/actual by a handful of
revenue groupings — not parsed) and `NALICHNOST`/`OCHAKVANO`/`list`/`INF`
(cash-balance/forecast/metadata sheets — not parsed).

`OTCHET` is **not one table**: it's a print-ready concatenation of many
report "pages" onto one sheet (49 of them, in the August 2026 sample — one
per funding-account/transfer/functional breakdown), each re-printing the
organisation header block before its own §§/под-§§ table. Every such table
shares one schema:

| col (0-idx) | meaning |
|---|---|
| 1 | `§§` — filled only on a paragraph-aggregate row (name then sits in col 2), or the literal string `"ВСИЧКО"` marking that table's grand-total row (pseudo-code "99-99" in col 2, name in col 3) |
| 2 | `под-§§` — filled only on a detail row (name then sits in col 3) |
| 4 | Уточнен план Общо (adjusted plan, total) |
| 5-7 | plan split by държавни/местни/дофинансиране дейности |
| 8-10 | ОТЧЕТ (actual) split by the same three |
| 11 | ОТЧЕТ Общо (actual, total) |

`parsers/budget_b1.py` locates every such header row (col1=="§§" AND
col2=="под-§§"), and parses **only the first two** — "I. ПРИХОДИ..."
(`section="приходи"`) and "II. РАЗХОДИ - РЕКАПИТУЛАЦИЯ..."
(`section="разходи"`) — per the task's "main expenditure-by-paragraph
table" scope. It records in its result's `skipped` list how many further
pages existed (transfers §§3000-6900, loans, EU-funds/trust-fund
sub-ledgers mirrored by the `IB1_*` siblings below, a functional/дейност
breakdown) and does not parse them.

**`B3` (quarterly)**: `B3_YYYY_Q_5206.xls` / `IB3_*` share the exact same
`OTCHET` schema as `B1`/`IB1_*` (confirmed on real 2019-2021 and 2026
files, not just the MinFin macro) — `classify_filename` already maps a
`B3`/`IB3` filename to the same `kind` ("B1" / "IB1_<SUFFIX>") as its
monthly sibling, so no special-casing was needed in either the scraper or
`parse_budget_b1`; the same function parses both. The one thing that *does*
differ is the header's "за периода от...до" date pair: `B1`'s covers one
month, but `B3`'s "от" is always 1 January of that year (quarterly reports
are year-to-date cumulative) and "до" is the quarter-end month -- so
`_infer_period`'s fallback (used only when no `period` is passed in) picks
the *later* of the two dates, not the first one found; naively using "от"
would misreport every `B3` as January.

**Quirk found in the live August 2026 `B1` file**: the per-параграф
"Уточнен план Общо" column (col 4) is 0 for *every* row in both the revenue
and expenditure tables, including the grand totals — only the ОТЧЕТ
(actual) columns carry real figures at this §§/под-§§ granularity. The real
adjusted plan total (e.g. the ~15.7M EUR figure for tax revenue) is only
available in the aggregate `Cash-Flow-DATA`/`OTCHET-agregirani pokazateli`
sheets, which are out of scope here. `plan_annual` is therefore always
`None`; `plan_adjusted` is frequently 0 for this municipality's monthly `B1`
exports (confirmed 2019-2026), so only `actual_ytd` should be relied on
from monthly files. This quirk does *not* hold for the quarterly `B3` form:
the 2019 Q1 sample has real (nonzero) "Уточнен план" figures, so
`plan_adjusted` is usable there.

**`IB1_DES`/`IB1_DMP`/`IB1_K33`/`IB1_KSF`/`IB1_RA`**: separate files, same
`OTCHET`/schema, each scoped to a different account instead of the main
budget (per their macro template's own title line):
- `DES`, `DMP`, `KSF`, `RA`: "ОТЧЕТНИ ДАННИ ПО ЕБК ЗА СМЕТКИТЕ ЗА СРЕДСТВАТА
  ОТ ЕВРОПЕЙСКИЯ СЪЮЗ" + the suffix — four different EU-funds sub-accounts.
- `K33`: "ОТЧЕТНИ ДАННИ ПО ЕБК ЗА СМЕТКИТЕ ЗА ЧУЖДИ СРЕДСТВА" — trust/foreign
  funds account (§33xx).

These are catalogued (`BudgetReport` rows, downloaded) but **not parsed**
into `CashExecutionLine` — only the main `B1`/`B3` file is, per the task.

## 3. Capital-expenditure ledger ("Разчет за финансиране на капиталовите
   разходи", the friendly-named `.xlsx`, e.g. `august2026.xlsx`)

One sheet per organisational unit ("Общо" = municipality-wide; the rest are
кметства/schools/kindergartens, varying in count year to year as units are
added/merged — 13 in Feb 2021, 18 in Aug 2026), the same general layout on
every sheet: a handful of title/period/currency rows, a row of column
captions (the "header row"), then data; row immediately after the header
is the sheet's "ОБЩО" grand total, further rows are data.

**`parsers/budget_capital.py` locates the header row and the four core
monetary columns (Сметна стойност / Усвоено до края на предходната година /
Уточнен план / Усвоено към отчетния период) by searching for that label
text** (`_find_core_columns`), rather than assuming a fixed row/column —
this is what lets the same parser read every filename/year variant below
without per-year branching. The search anchors on "Сметна стойност" (the
one label seen in every variant so far); the other three core columns and
the code/name/years columns are then matched by label within that same
row, falling back to position-relative-to-"Сметна стойност" (code=1,
name=2, years=3, the next three columns after it) if a label is missing or
reordered. A sheet with no "Сметна стойност" label anywhere in its first 15
rows raises `CapitalHeaderNotFound`, which `parse_budget_capital` catches
per-sheet; if *every* sheet in the workbook fails this way, the whole
result is marked `unsupported` (with the reason) instead of being forced
through — see "Known limitations" below for what's *not* re-derived by
label.

### Layout variants observed (filenames, sheets, currency)

| years | filename pattern(s) | sheets | currency marker | header row | notes |
|---|---|---|---|---|---|
| 2021 (Feb-May+, confirmed) | `Отчет за <year> <month> 5206 Несебър.xlsx`, `razchet-<month><year>.xlsx` | "Общо" + 13-16 кметства/schools/gardens | none found — BGN by period fallback | row 5 (1-indexed), identical column positions to 2026 (D=4..G=7, funding block H-W=8..23) | Same column layout as 2026 byte-for-byte except the missing currency marker and fewer org-unit sheets; `_find_core_columns`/`_find_data_start` land on the same rows as the 2026 sample. Validated: 0 validation mismatches, 0 parse failures across every 2021 file cached so far. |
| 2026 (Jan-Aug, confirmed) | `Месечен отчет за 2026 <month> 5206 Несебър.xlsx`, `<month><year>.xlsx`/`<month><yy>.xlsx`, `otchetMMYY.xlsx` | "Общо" + 18 кметства/schools/gardens | row 4, col B: `"Сумите са в EUR!"` | row 5 | The original sample layout this parser was first built on; see `nesebar_budget_execution_aug2026.xlsx`. |
| 2022-2025 | *(not yet in the local cache as of this writing — `NesebarSiteScraper`'s background download was still on 2021 when this doc was last updated)* | — | — | — | Task mentions ad hoc names `kr_2021_2_5206.xlsx`, "Месечен отчет за 2022 Май 5206 Несебър.xlsx", `ot4et-june.xlsx`, `mart2024.xlsx`, `2024Май5206Несебър.xlsx` for this range. The label-based header locator and BGN-by-period-fallback currency detection *should* handle these unchanged (2021's ad hoc names already exercise the same code paths), but this is unverified until the files are actually downloaded — re-run the quality-check script (see §5) once they land, and treat any `unsupported` result or validation mismatch as a real layout difference to investigate, not a parser bug to paper over. |

### Known limitations

- **Funding-source-group columns (H-W) are still positionally mapped, not
  label-derived.** The four core columns (D-G equivalents) are now
  label-located per the task's explicit ask; the five funding groups
  (`targeted_subsidies`, `carryover_targeted_subsidies`, `own_funds`,
  `other_sources`, `eu_funds`, each with plan/actual and sometimes a
  "в т.ч." memo sub-column — see below) were left at the 2026 sample's
  fixed offsets (8-23) because no pre-2026 workbook with a *different*
  funding-block layout was available to validate a label-based mapper
  against; a confidently-wrong label match would be worse than a documented
  fixed assumption. `_funding_extra` is defensive (a column past
  `ws.max_column` reads as `None` instead of raising), so a narrower sheet
  degrades gracefully rather than crashing. Re-derive this by label if/when
  a 2022-2025 sample shows the block has actually moved.
- The original fixed-position classification logic (КР-paragraph /
  функция / под-параграф / object, by code shape) is unchanged from the
  2026-only version — it held for every 2021 sheet checked.

Column A holds a code whose *shape* determines the row's level — there is
no separate "level" column, so the parser infers it:
- 4-digit code ending `"00"` (`"5100"`, `"5200"`) → a КР-paragraph subtotal
  (§51-00 Основен ремонт на ДМА, §52-00 Придобиване на ДМА, ...).
- literal `"Функция NN"` → a function subtotal within the current paragraph.
- 4-digit code sharing the current paragraph's first two digits but *not*
  ending `"00"` (`"5201".."5219"` under `"5200"`) → a КР-под-параграф
  subtotal (object-type breakdown: compute equipment, buildings, vehicles,
  machinery, inventory, other).
- any other 4-digit code → an actual object/дейност line — this is a
  leading "function number" digit followed by the 3-digit ЕБК "дейност"
  code (e.g. `"1122"` = function 1 + дейност 122 "Общинска
  администрация"; `"6606"` = function 6 + дейност 606 "улична мрежа").
  The same code legitimately repeats across several distinct objects.
- column A blank, column B one of `"Обект"`/`"ППР"`/`"ППР за сграда"`/
  `"инженеринг"`/`"сграда чрез изграждане"` → a cosmetic group-header row
  that duplicates the subtotal above it (no data of its own) — skipped,
  but kept as `extra_json["group_label"]` on the object rows that follow.

**Known limitation (row classification by code shape)**: this classification can't distinguish a под-параграф
row from an object row by code shape alone if an object's
`function-digit + дейност` code happened to share the paragraph's first two
digits (e.g. a hypothetical `"5299"` object under paragraph `"5200"`) — not
observed in the sample, and `validate_function_subtotals` would surface it
as a subtotal/object-sum mismatch rather than silently miscounting.

Columns D-G: Сметна стойност (estimated_total) / Усвоено до края на
предходната година (spent_prior) / Уточнен план (plan_current, =
I+N+P+S+V below) / Усвоено към отчетния период (spent_period, = K+O+Q+T+W
below) — both sum formulas are stated in the sheet's own header and were
verified against the "ОБЩО" row.

Columns H-W: five funding-source groups, each plan+actual (two with a
"в т.ч." memo sub-column, and a free-text annotation cell that some rows
use instead of/alongside the structured columns — kept verbatim in
`extra_json["funding"][group]["note"]`, not parsed further):
`targeted_subsidies`, `carryover_targeted_subsidies`, `own_funds`,
`other_sources`, `eu_funds`.

Validated against the August 2026 sample: the "Общо" sheet's grand total is
exactly 24,990,398 EUR (Сметна стойност), and every function subtotal's
estimated_total matches the sum of its object rows (0 mismatches). The same
check against the February 2021 BGN sample (converted to EUR) also comes
back with 0 mismatches.

## 4. Currency: BGN (2019-2025) vs. EUR (2026 on)

Bulgaria adopted the euro on 2026-01-01 at the fixed official rate
**1 EUR = 1.95583 BGN**; every report before that date is denominated in
leva, every report from 2026-01 on is denominated in euro. Both parsers
(`budget_b1.py`, `budget_capital.py`) now:

1. **Detect the original currency per workbook/sheet**, explicit marker
   first:
   - `B1`/`B3`: the literal string `"(в лева)"` / `"(в евро)"`, found at
     row 18 (1-indexed), column 12, in every file inspected across
     2019-2026 — a stable, cheap, exact signal (`_detect_original_currency`
     in `budget_b1.py` scans the first 25 rows for it rather than hardcoding
     that row/column, in case an older year moves it).
   - Capital ledger: `"Сумите са в EUR!"` (row 4, col B, 2026 on only); no
     marker was found in any 2021 file inspected, Cyrillic "лв"/"лева" is
     also checked for in case a future-discovered year spells it out.
   - **Fallback** (no marker found): period `< "2026-01"` → BGN, else EUR.
     (Both parsers log a warning and default to BGN if *neither* a marker
     nor a period is available at all — shouldn't happen in practice, since
     `cli.py` always passes the `BudgetReport.period` recorded by the
     scraper.)
2. **Convert every monetary value to EUR at parse time**, rounded to 2
   decimal places, at the fixed rate above. `currency` is always stored as
   `"EUR"` on the row (so rules/site code need no currency-awareness
   changes). `extra_json` additionally records:
   - `original_currency`: `"BGN"` or `"EUR"`.
   - `conversion_rate`: `1.95583`, present **only** when a conversion
     actually happened (i.e. `original_currency == "BGN"`); omitted for
     EUR-native rows since nothing was converted.
   - `original`: the pre-conversion figures, also **only** present for a
     BGN row — e.g. for the capital parser,
     `extra_json["original"] = {"estimated_total": ..., "spent_prior": ...,
     "plan_current": ..., "spent_period": ..., "funding": {...}}`; for the
     B1/B3 parser, `extra_json["original"]` holds the same 8 monetary keys
     as the top-level `extra_json` (`plan_total`, `plan_state`, ...,
     `actual_total`).

**Caveat for `CashExecutionLine`**: `db/budget_models.py`'s
`CashExecutionLine` table (unlike `BudgetLineItem`) has no `currency`
column, and `db/budget_repo.py`'s `upsert_cash_execution_lines` doesn't
read one off the row dict either — both are out of this task's editable-
files list. `parse_budget_b1`'s rows still carry `"currency": "EUR"` (for
any future caller/schema change), but today it's only actually *persisted*
via `extra_json` (which **is** read/stored as-is), not as a queryable
column, for cash-execution lines. Capital-ledger rows don't have this gap:
`BudgetLineItem.currency` exists and is populated normally.

### Quality-check results (scratch script, not the CLI)

Ran both parsers directly (no DB writes) over every `B1`/`B3`/`capital_xlsx`
file cached under `data/cache/nesebar_site/` at the time of writing — the
background scraper had reached 2019-2021 (complete) and 2026 (complete);
2022-2025 weren't downloaded yet:

| year | kind | files | rows | mismatches | failures | currencies |
|---|---|---|---|---|---|---|
| 2019 | B1 (incl. B3) | 16 | 4,162 | 0 | 0 | BGN |
| 2020 | B1 (incl. B3) | 16 | 4,160 | 0 | 0 | BGN |
| 2021 | B1 (incl. B3), through July | 10 | 2,600 | 0 | 0 | BGN |
| 2021 | capital_xlsx, Feb-May | 4 | 1,629 | 0 | 0 | BGN |
| 2026 | B1 (incl. B3) | 10 | 2,610 | 0 | 0 | EUR |
| 2026 | capital_xlsx | 7 | 2,478 | 0 | 0 | EUR |

63 files total, **0 parse failures, 0 `unsupported` workbooks, 0 validation
mismatches**. This should be re-run (the script lived in a scratch location,
not committed) once the scraper reaches 2022-2025, to confirm the ad hoc
capital-workbook names mentioned in the task (`kr_2021_2_5206.xlsx`,
`mart2024.xlsx`, `2024Май5206Несебър.xlsx`, ...) parse cleanly too.

### Test fixtures added (`data/samples/`)

- `nesebar_cash_execution_B1_2020_1_BGN.xls` — smallest available pre-2026
  monthly `B1` (2020-01, BGN, has its own "(в лева)" marker).
- `nesebar_cash_execution_B3_2019_1_BGN.xls` — quarterly `B3` (2019 Q1,
  BGN), for the B3-acceptance / quarter-end-period tests.
- `nesebar_budget_execution_feb2021_BGN.xlsx` — 2021 capital ledger (BGN,
  no currency marker, exercises the period-based fallback and confirms the
  label-based header locator on a non-2026 file).
- `nesebar_budget_execution_aug2026.xlsx` / `nesebar_cash_execution_B1_2026_8.xls`
  — pre-existing 2026 EUR samples, unchanged.
- A 2024 capital-ledger fixture was **not** added: no 2024 file had reached
  the local cache by the time this task's fixtures were finalized (see
  "Quality-check results" above) — add one (smallest available, <3 MB) once
  the scraper reaches 2024.

## 5. MinFin макети (`data/minfin/maketi/`, reference only)

Unzipped `maketi_quarterly_III_2026_municipalities.zip` →
`.../касови отчети/B3_2026_3_Mun.xls` + `IB3_2026_3_Mun_{DES,DMP,K33,KSF,RA}.xls`
and `maketi_monthly_2026.zip` → `.../B1_2026_01_PRB*.xls` (ministries'
monthly macro — municipalities only get a *quarterly* macro from MinFin;
the municipality's own *monthly* `B1` export on nesebar.bg uses the same
`OTCHET` schema as these macros, just with its own ЕБК code 5206 instead of
a `PRB` placeholder). Used only to confirm the `OTCHET` column layout above
is the standard form, not a Nessebar-specific one; no macro file was found
for the capital-expenditure ledger in §3 — the task's sample `.xlsx` is this
project's only ground truth for that form.
