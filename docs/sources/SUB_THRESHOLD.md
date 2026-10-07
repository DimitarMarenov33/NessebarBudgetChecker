# Sub-threshold / unpublished-purchase search — Община Несебър

Compiled 2026-10-07. Scope: purchases that never reach ЦАИС ЕОП — чл. 20, ал. 4 direct
awards, contracts/expense/payments registers, and the annual budget-execution report's
capital/repair appendices — searched for line-level (quantity + price) records. Everything
marked "confirmed" was fetched this session (see `FETCH_LOG.md`, "Sub-threshold search
2026-10-07"); everything marked "guess" was not independently verified.

---

## 1. What EXISTS

### 1.1 ЦАИС ЕОП coverage of "събиране на оферти с обява" — CONFIRMED since 2020
ЗОП чл. 187(1): "Възложителите откриват възлагането на поръчка на стойност по чл. 20,
ал. 3 с публикуване в РОП на обява за събиране на оферти..." РОП is part of the central
ЦАИС ЕОП platform (чл. 36(1): "Регистърът на обществените поръчки (РОП)... част от
платформата по чл. 39а, ал. 1"). This chain (`чл. 18, ал.1, т.12`/обява, moved into the
central register from 01.11.2019/01.01.2020 per the 2018/2019 ЗОП amendments) confirms the
task's premise: **"събиране на оферти с обява" has been in ЦАИС ЕОП since 2020** — this
band (50,000–100,000 лв. доставки/услуги; 80,000–300,000 лв. строителство per чл. 20,
ал. 3) is NOT the gap. The gap is the band below it.

### 1.2 nesebar.bg/register.html — full registry list (confirmed, fetched in full)
~18 register categories, ~90 individual file links (PDF/XLS/XLSX/DOC), covering: обекти
въведени в експлоатация, разрешителни за строеж, разрешения за поставяне, технически
паспорти на сгради, **регистър на актове за общинска собственост** и **разпоредителни
сделки с имоти** (both → `nesebar.imeon.bg/frmAOS.aspx`, property transactions, not
procurement), общински предприятия, търговски дружества с общинско участие, сдружения с
нестопанска цел, туризъм, ПУП, такси/транспорт, рекламно-информационни елементи, отпадъци,
**регистър на даренията** (donation contracts, 2019-2025, `.xls`/`.xlsx`), заявления по
ЗДОИ, кучета, незаконни строежи, списък на второстепенните разпоредители с бюджет (names
only, not opened), списък на приватизационните сделки. **None of these is a general
contracts register, an expense register, or a payments-to-suppliers register.** The closest
analogue — "Регистър на даренията" — covers gifts/donations *received*, not purchases made.

### 1.3 nesebar.bg/reports.html — two distinct report families (confirmed)
- **Monthly cash-execution archive** (already catalogued in `INVENTORY.md`/`BUDGET_FORMS.md`):
  `B1`/`IB1_*` `.xls` + the "Разчет за финансиране на капиталовите разходи" `.xlsx`, 2019
  → Aug 2026. The capital-ledger `.xlsx` is **object-level** (one row per construction/repair
  object) but carries **only monetary columns** (Сметна стойност, spent_prior, plan_current,
  spent_period, funding-source split) — confirmed in `BUDGET_FORMS.md` §3: no quantity, no
  unit price, no supplier/contractor name. This is the single most granular public capital/
  repair breakdown that exists, and it structurally cannot carry "quantity of items bought."
- **Annual report package, one per year 2019-2025** (new finding this session):
  `otcheti/1-N.pdf` (Одитен доклад), `2-N.pdf` (Касов отчет), `3-N.pdf` (Баланс), `4-N.pdf`
  (Отчет за приходи и разходи), `5-N.pdf` (Пояснителни сведения), `6-N.pdf` (Обяснителна
  записка). Downloaded and inspected 2025's `1-25.pdf`, `2-25.pdf`, `5-25.pdf`, `6-25.pdf`
  and 2024's `6-2024.pdf`: **all are flatbed scans (Konica Minolta bizhub C361i) with no text
  layer** (`pdftotext` returns 0 lines). The short ones (`2-25.pdf`, `6-25.pdf`) are 3 pages —
  consistent with narrative/aggregate content, not a line-item register. This is almost
  certainly the ЗПФ чл. 140 annual-execution-report-to-council package (kmet must submit it
  by 31 August of the following year — matches the file-group's position in the reports.html
  timeline). Not OCR'd this session (sample files saved to `data/samples/otchet_*.pdf`).

### 1.4 nesebar.imeon.bg/frmAOP.aspx — buyer profile (confirmed, `IMEON.md`)
403 listed procedures with Сума(без ДДС)/ДДС/Сума общо, Статус, per-row "Файлове" and
"Гаранции/Плащания" popups. This only lists ЗОП-regulated procedures (above the чл. 20,
ал. 3 floor) — no "Директно възлагане" or standalone "Плащания" section was found in the
filter form (Вид процедура/Статус/Номер/Дати only). Confirms: the municipality's own buyer
profile does not surface sub-чл.20-ал.4 direct awards either.

### 1.5 Municipal enterprises — quick check (confirmed)
`bks.php` (ОП БКСО) and `psno.php` (ОП Управление на отпадъците) are plain mission-statement
pages; both share the main site's nav (Профил на купувача/Търгове point to the same central
profile). No separate procurement/contracts page per enterprise.

### 1.6 data.egov.bg — Nessebar's organisation profile (confirmed, new finding)
Org id `279`, profile `https://data.egov.bg/organisation/profile/3f435dd4-818c-4517-b01d-8ae0246f8ef7`,
exactly **2 datasets**: "Регистър на ЮЛНЦ в които участва община Несебър" and "Подлежаща за
публикуване информация по ЗПКОНПИ в община Несебър." Neither is procurement/budget/contracts
data. A broader query "Община Несебър" returns the portal's irrelevant fallback list (datasets
from an unrelated municipality, Сапарева баня) rather than "no results" — confirmed by zero
occurrences of "Несебър" in that rendered page outside the echoed search box.

### 1.7 os-nessebar.eu — council decisions (partially checked, inconclusive)
Decisions publish per-protocol with inline text + occasional PDF attachments; no site search
exists. Sampled 3 protocols (№15/28.03.2025, №24/29.01.2026, №25/12.03.2026) looking for the
budget-adoption decision and a capital-program-by-object attachment — **did not find either**;
protocol №25's text shows the **2026 state budget law itself was still not adopted** as of
12.03.2026, so the municipal 2026 budget-adoption decision is later still. Pinpointing the
exact protocol for 2024/2025/2026 budget adoption and the 2024/2025 execution-report
acceptance would need exhaustive paging through ~29+ protocols (11 pages) — not done this
session (time budget). **This is a gap in our search, not a confirmed absence.**

---

## 2. What does NOT exist (searched, not found)

- No "Регистър на договорите" (general contracts register) anywhere on nesebar.bg, in the
  18-category register.html list, or in the imeon.bg buyer profile.
- No "Регистър на разходите" / payments-to-suppliers register. The old ЗОП-2014 чл. 22б
  "информация за извършени плащания" duty the task asks about is **confirmed repealed**: the
  current чл. 36а, ал. 1, т. 2 line that used to require this was "(отм. - ДВ, бр. 107 от
  2020 г., в сила от 01.01.2021 г.)" — abolished nationally from 2021, not merely unused by
  Nessebar.
  No standalone "Текущи ремонти" list page — repairs appear only as aggregate-value rows
  inside the monthly capital-expenditure ledger (§1.3), with no itemization below the
  object level.
- No Nessebar-specific procurement/budget/contracts dataset on data.egov.bg (confirmed: only
  2 unrelated datasets exist for org id 279).
- No separate itemized "capital/repair appendix" distinct from (a) the monthly capital
  ledger .xlsx (value-only, no quantity) or (b) the scanned annual report package (no text
  layer, narrative-length).

---

## 3. Legal position

**Is non-publication of чл. 20, ал. 4 direct awards lawful?** Yes, under current law.

- **ЗОП чл. 20, ал. 4**: municipalities may award directly, without any procedure, contracts
  below 80,000 лв. (строителство), 100,000 лв. (приложение № 2 услуги), 50,000 лв. (other
  доставки/услуги).
- **ЗОП чл. 20, ал. 5**: for ал. 4, т. 2 и 3 (доставки/услуги), "възложителите могат да
  доказват разхода само с първични платежни документи, без да е необходимо сключването на
  писмен договор" — the law explicitly does not even require a written contract for these,
  let alone its publication.
- **ЗОП чл. 36, ал. 1** lists everything that must reach РОП/ЦАИС ЕОП (20 numbered items):
  решения, обявления, обяви за събиране на оферти (чл. 20, ал. 3/7), документации, протоколи,
  договори за обществени поръчки, допълнителни споразумения, etc. **Чл. 20, ал. 4 awards are
  not in this list.** The repealed чл. 42 (pre-2018 version) might once have addressed lower
  tiers but carries no substantive text today ("Отм. - ДВ, бр. 86 от 2018 г.").
- **ЗДОИ чл. 15, ал. 1, т. 7 и 8**: obliges publication of "информация за бюджета и
  финансовите отчети... съгласно ЗПФ" and "информация за провеждани обществени поръчки...
  съгласно ЗОП" — i.e. ЗДОИ is a *pointer*, not an independent substantive duty; it requires
  exactly what ЗПФ/ЗОП already require and no more. Since ЗОП doesn't require publishing
  ал. 4 awards, ЗДОИ doesn't either.
- **ЗПФ**: чл. 140 (annual execution report to the съвет by 31 Aug), чл. 133(1)/(4) (monthly/
  quarterly execution reports published on the website — matches §1.3's monthly archive),
  чл. 93 (publish approved budgets). None of these provisions require a line-item register of
  individual purchases, suppliers, quantities, or unit prices — only aggregate
  revenue/expenditure-by-§§ figures and, for capital spending, by-object totals (§1.3).
- **ЗПФ чл. 173**: failure to publish information the law *does* require carries a 100-500 лв.
  fine (doubled on repeat) for the responsible official — but this cannot apply to чл. 20,
  ал. 4 purchase data, because no provision requires its publication in the first place.
- **New, not yet tested**: ЗОП чл. 230а (in force 07.08.2026, two months before this search)
  requires an annual *indicative plan-schedule* of procurements the buyer intends to run "по
  реда на този закон" published on the buyer profile by 30 Nov each year. Whether this is read
  to include ал. 4 direct awards (which are not run "by the order of this law" in the
  procedural sense) is **unclear/guess** — worth re-checking once a 2027 plan-schedule is
  published, but it would at most show planned categories/estimates, not completed purchases
  with quantities.

**Conclusion**: non-publication of чл. 20, ал. 4 direct-award line items (quantity + price) is
not a compliance gap by Nessebar specifically — it is a structural feature of Bulgarian
public-procurement law. No statute requires publishing these at all, and ал. 5 explicitly
permits skipping even a written contract for part of this band.

---

## 4. Recommendation

1. **Primary ingestion target for anything above the чл. 20, ал. 3 floor**: `service.eop.bg`
   (ЦАИС ЕОП backend, already unlocked — see `EOP_API.md`) cross-checked against
   `nesebar.imeon.bg/frmAOP.aspx`. This is contract-level, not individual-item-level, but it's
   the best value+contractor+CPV data available.
2. **Best available proxy for capital/repair "purchases"**: the monthly "Разчет за
   финансиране на капиталовите разходи" `.xlsx` on `reports.html` (already parsed per
   `BUDGET_FORMS.md` §3). Because this schema *never* carries a quantity or unit-price column
   by design, **every row should be auto-flagged "quantity not stated"** by the rule engine —
   not as an anomaly to detect, but as a structural default, since no public Nessebar source
   is built to carry that field.
3. **The true sub-чл.20-ал.4 layer (below even "събиране на оферти")**: no public, scrapable
   source exists, and under ал. 5 may legally have no written contract at all. This cannot be
   "discovered" by scraping; the only lever is a formal ЗДОИ information request for the
   primary payment documents themselves. Recommend the rule engine treat "sub-threshold direct
   award, zero public trace" as its own maximum-severity flag category, distinct from
   "quantity not stated," since the two have different legal roots (structural data-model gap
   vs. a lawful total absence of any record).
4. **Annual report PDFs** (§1.3): low priority for ingestion as structured data (scanned, no
   text layer, narrative-length) but worth OCR'ing once for the "Обяснителна записка"/"Одитен
   доклад" text, since Сметна палата audit findings (already noted in `INVENTORY.md` §7 —
   qualified opinions, multi-year audit gaps) are independent corroboration of reporting
   weaknesses, useful as narrative context alongside the numeric rule flags.
5. **os-nessebar.eu budget-adoption decisions**: worth a follow-up pass with full pagination
   (11 pages, no search) to pin exact protocol numbers and check for capital-program-by-object
   attachments — not completed this session; treat current absence as "not yet found," not
   "confirmed absent."
