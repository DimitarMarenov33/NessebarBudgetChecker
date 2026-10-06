# Budget-execution report forms — Община Несебър

What was actually learned from the two real sample files (`data/samples/`)
and cross-checked against the official MinFin макети
(`data/minfin/maketi/`, unzipped from `maketi_quarterly_III_2026_municipalities.zip`
and `maketi_monthly_2026.zip`). Nothing below is guessed.

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

**Quirk found in the live August 2026 file**: the per-параграф "Уточнен
план Общо" column (col 4) is 0 for *every* row in both the revenue and
expenditure tables, including the grand totals — only the ОТЧЕТ (actual)
columns carry real figures at this §§/под-§§ granularity. The real adjusted
plan total (e.g. the ~15.7M EUR figure for tax revenue) is only available
in the aggregate `Cash-Flow-DATA`/`OTCHET-agregirani pokazateli` sheets,
which are out of scope here. `plan_annual` is therefore always `None` and
`plan_adjusted` is frequently 0 for this municipality's exports; only
`actual_ytd` should be relied on from this parser.

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
кметства/schools/kindergartens), **identical fixed layout** on every sheet:
rows 1-9 are the header (title, ЕБК code, period, "Сумите са в EUR!",
column captions), row 10 is the sheet's "ОБЩО" grand total, row 11+ is data.

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

**Known limitation**: this classification can't distinguish a под-параграф
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
estimated_total matches the sum of its object rows (0 mismatches).

## 4. MinFin макети (`data/minfin/maketi/`, reference only)

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
