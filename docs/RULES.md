# Anomaly rules

This document explains what `nessebar-budget analyze` checks, why, and what
each flag means for a citizen reading it on the site. The rules live in the
`src/nessebar_budget/analysis/rules/` package (`contracts.py`,
`competition.py`, `quantity.py`, `budget.py`, `reports.py`, `meta.py`, plus
`linking.py` for EOP/SIGMA cross-referencing; every public name is
re-exported from `nessebar_budget.analysis.rules`), thresholds (all
overridable via `RULES_*` env vars) in `analysis/thresholds.py`, the
budget-object/contract text matcher in `analysis/matching.py`, and the
upsert/resolve bookkeeping in `analysis/engine.py`.

## The flag contract

Every flag row (`db.models.Flag`) carries:

| Field | Meaning |
|---|---|
| `tier` | **`violation`** -- a clear legal breach on the face of the data; **`signal`** -- a pattern consistent with misconduct that needs documents to confirm or dismiss; **`opacity`** -- lawful, but unverifiable from what is published, so documents should be requested. |
| `severity` | `info` < `warning` < `high`: "worth knowing" / "worth asking about" / "worth asking about soon". Independent of tier (a big `signal` can be `high`, a small `violation` `info`). |
| `message` | The 1-2 sentence Bulgarian headline. |
| `explanation` | 2-4 plain Bulgarian sentences: what we see, why it *may* point to misconduct, and what would make it innocent. |
| `documents_json` | Bulgarian names of the documents a citizen should request under ЗДОИ to settle the question (e.g. "техническа спецификация", "приемо-предавателни протоколи", "решение на общинския съвет за промяна на бюджета (чл. 124 ЗПФ)"). |
| `law_ref` | The legal citation, verified against `docs/law/*.txt` (see below). |
| `subject_type` / `subject_id` / `subject_key` | What the flag is about (`contract`, `contractor`, `contract_group`, `procedure`, `budget_object`, `report`) and its upsert key. |
| `details_json` | The supporting numbers (dates, amounts, members of a group, ...). |

**Tone.** Neutral, never accusatory: "може да сочи към нередност",
"изисква обяснение", "следва да се поиска". A flag means "this is worth a
look," never "this is wrong." Example (`price_unverifiable`): *"Тази покупка
може да сочи към нередност: за договора с … не са публикувани количества и
спецификации, така че не може да се прецени дали цената е обоснована."*

**Lifecycle.** `analyze` upserts flags keyed on `(rule, subject_key)`: a
subject seen again updates the existing row (every rule-owned column:
tier/severity/message/explanation/documents/details/law_ref/subject, plus
`last_seen_at`) in place; a subject a rule no longer produces gets
`resolved_at` set (not deleted). Meta rules (`eu_funded_irregularity`) run
**last**, over every other rule's output. `analyze` prints, per rule, the
open/new/updated/resolved counts and the tiers emitted, then the open
total per tier.

**Schema.** `tier`, `explanation`, `documents_json` were added 2026-10-07;
`db/migrate.py` adds them to an existing SQLite `flags` table with guarded,
idempotent `ALTER TABLE … ADD COLUMN` statements on every `init_db()`.

**Legal texts.** Every article cited by a rule was checked with
`grep "^Чл\. N\."` in `docs/law/zop.txt`, `zpf.txt`, `zmsma.txt`,
`zdoi.txt` (lex.bg consolidated texts, extracted 2026-10-06, see
`docs/law/INDEX.md`); the key sentence is quoted verbatim in
`analysis/thresholds.py`'s docstring. Two sources are cited but **not** in
`docs/law/`: the Наказателен кодекс (чл. 248а, EU-funds fraud, cited per the
project brief) and the Закон за счетоводството (cited only as "реквизити на
първичните счетоводни документи", no article number). ЗОП states thresholds
in leva; data is in EUR, converted at the fixed peg `BGN_EUR_RATE =
1.95583`.

## Summary (run on `data/nessebar.db`, 2026-10-07)

411 signed EOP contracts, 411 SIGMA records, capital ledger 2021-02..2022-02
and 2026-02..2026-08, budget reports 2019-01..2022-03 and 2026-01..2026-08.

| Rule | Tier | Severity | Legal basis | Open |
|---|---|---|---|---|
| `late_publication` | violation | info/warning/high by delay | ЗОП чл. 26, ал. 1, т. 1; чл. 256а | 5 |
| `annex_over_cap` | violation | high | ЗОП чл. 116, ал. 2; чл. 255, ал. 3 | 3 |
| `unplanned_spending` | violation | warning | ЗПФ чл. 128, ал. 1; чл. 102, ал. 1; чл. 124, ал. 2 | 16 |
| `short_offer_deadline` | violation / signal | high / warning | ЗОП чл. 74, чл. 178, чл. 188 | 0 |
| `splitting` | signal | warning; high > 2x boundary | ЗОП чл. 21, ал. 15-16; чл. 20; чл. 247 | 3 |
| `exceptional_procedure` | signal | by value (fuel/exchange: info) | ЗОП чл. 79, чл. 182, чл. 191; чл. 250а | 30 |
| `bid_at_ceiling` | signal | warning >= 100k; high >= 500k | ЗОП чл. 21, ал. 1-2; чл. 2, ал. 2 | 18 |
| `single_bidder` | signal | warning >= 100k; high >= 500k | ЗОП чл. 2, ал. 2 | 31 |
| `contractor_concentration` | signal | info | ЗОП чл. 2, ал. 1 | 0 |
| `annex_growth` | signal | warning | ЗОП чл. 116, ал. 2 | 31 |
| `overspend_vs_plan` | signal | warning | ЗПФ чл. 124, ал. 2; чл. 125 | 1 |
| `plan_jump` | signal | warning | ЗПФ чл. 124, ал. 2; ЗМСМА чл. 22, ал. 2 | 14 |
| `unmatched_spending` | signal | info | ЗОП чл. 20, ал. 4, т. 3 | 13 |
| `missing_annual_report` | signal | warning | ЗПФ чл. 11, ал. 3; чл. 133, ал. 4; чл. 140, ал. 5-6; чл. 173 | 4 |
| `eu_funded_irregularity` | signal (meta) | info | НК чл. 248а (not in docs/law); OLAF | 10 |
| `price_unverifiable` | opacity | info; warning >= 100k | ЗОП чл. 48, ал. 1, т. 1; чл. 36, ал. 1, т. 12 | 2 |
| `missing_quantity` | opacity | info/warning/high by value | ЗОП чл. 2, ал. 2; Прил. № 4, ч. В, т. 6 | 75 |
| `missing_value` | opacity | warning | ЗОП чл. 36, ал. 1, т. 12 | 0 |
| `near_threshold` | opacity (signal with splitting) | info (warning) | ЗОП чл. 20, ал. 2-3; чл. 21, ал. 14 | 4 |
| `missing_monthly_report` | opacity | info | ЗПФ чл. 11, ал. 3; чл. 133, ал. 1 и 4; ЗДОИ чл. 15а, ал. 4 | 41 |

Totals: 301 open -- 24 violation, 155 signal, 122 opacity. No
contract-level rule fires on more than ~9% of the 411 signed contracts
(`missing_quantity` Scope A: 38; `exceptional_procedure`/`annex_growth`:
30-31).

## Shared reference data

### Procedure types (verified in `data/nessebar.db`)

EOP signed contracts carry the ЦАИС ЕОП enum (`raw_json.contract.ProcedureType`),
EOP open tenders the Bulgarian label (`raw_json.procedure.ProcedureType`),
SIGMA its own short label. Counts match one-to-one across sources:

| EOP enum | EOP label | SIGMA label | ЗОП | Regime tier | Exceptional? |
|---|---|---|---|---|---|
| `OpenProcedure` (183) | Открита процедура | Открита (182) | чл. 18, ал. 1, т. 1 | 3 (ал. 1) | no |
| `PublicCompetition` (95) | Публично състезание | Състезание (95) | т. 12 | 2 (ал. 2) | no |
| `CollectingOffersWithNotice` (104) | Събиране на оферти с обява | Събиране на оферти (104) | чл. 187 | 1 (ал. 3) | no |
| `NegotiatedProcedure` (19) | Договаряне без предварително обявление | Пряко / без обявление (23, together with ↓) | т. 8, чл. 79 | 3 | **yes** -- fine чл. 250а |
| `DirectNegotiation` (4) | Пряко договаряне | ↑ | т. 13, чл. 182 | 2 | **yes** -- fine чл. 250а |
| `InvitationToSpecificEconomicOperators` (7) | Покана до определени лица | Договаряне с покана (7) | чл. 191 | 1 | **yes** |

Every EOP `NegotiatedProcedure` contract's `raw_json.procedure.ProcedureType`
reads "Договаряне без предварително обявление" and every `DirectNegotiation`
reads "Пряко договаряне", which fixes the mapping. Mapping lives in
`rules/_common.py` (`PROCEDURE_REGIME_TIER`, `EXCEPTIONAL_PROCEDURES`).

### EOP <-> SIGMA twins (`rules/linking.py`)

SIGMA's `raw_json.unp` is the same УНП as EOP's `contract.TenderNumber`; a
SIGMA record with the same УНП and contractor EIK (closest value if several)
is the EOP contract's twin. Only SIGMA has `bids_received`/`eu_funded`;
only EOP has dates, estimates and notice texts. Rules use the twin for
those fields instead of summing both sources.

### ЗОП чл. 20 thresholds (in force since 01.01.2024, ДВ бр. 88/2023)

| Category | Direct award below (ал. 4) | Обява/покана (ал. 3) | Публично състезание / пряко договаряне (ал. 2) | EU-level, open procedure (ал. 1) |
|---|---|---|---|---|
| Строителство | 80 000 лв. (40,903 €) | 80 000 - 300 000 лв. | 300 000 - 10 526 116 лв. (153,388 € - 5,381,936 €) | >= 10 526 116 лв. |
| Доставки и услуги | 50 000 лв. (25,565 €) | 50 000 - 100 000 лв. (to 51,129 €) | 100 000 - 273 812 лв. (to 139,998 €) | >= 273 812 лв. |
| Услуги по Прил. № 2 | 100 000 лв. (51,129 €) | -- (ал. 3 excludes them) | 100 000 - 1 466 850 лв. (to 749,999 €) | >= 1 466 850 лв. |

`category_of()` maps `TypeOfContract` 3 -> works, 2 -> supplies, 1 ->
services (Прил. № 2 when the main CPV matches `annex2_cpv_prefixes`, an
approximation of the annex's CPV list; e.g. 98351110 parking enforcement is
*not* in it). The pre-2024 values are **not** in our local law text (only
the current wording plus "изм." notes), so: `splitting` applies these
values to all years (it can only under-detect if, as generally reported,
earlier boundaries were lower -- unverified locally); `near_threshold` only
looks at procedures announced on/after 2024-01-01.

---

## Violations

### `late_publication`

**Checks:** for each EOP contract, the gap between signing
(`raw_json.contract.ContractDate`) and the award notice
(`raw_json.contract.TedPublishDate`).

**Legal basis:** ЗОП чл. 26, ал. 1, т. 1 -- *"Възложителите изпращат за
публикуване обявление за възлагане на поръчка в срок до: 1. тридесет дни
след сключване на договор за обществена поръчка или рамково споразумение."*
Repeated for публично състезание at чл. 185, т. 1 and for чл. 20, ал. 3
contracts at чл. 194, ал. 4 (*"В 30-дневен срок от сключването на договора
възложителят изпраща за публикуване в РОП обявление…"*). Fine: чл. 256а.

**Thresholds:** 30 days + 7-day grace (sending vs. publishing lag); severity
info, warning > 14 days late, high > 60 days late. EOP only.

### `annex_over_cap`

**Checks:** EOP contracts whose `CurrentContractValue` exceeds
`ContractValue` x 1.5. Values are re-derived to EUR from their own currency
codes (`Currency`/`CurrentContractCurrency`: 1 = EUR, 3 = BGN) -- **not**
the raw `...Euro` fields, which go stale after an amendment (one record's
`CurrentContractValueEuro` stayed equal to the original although
`CurrentContractValue` changed).

**Legal basis:** ЗОП чл. 116, ал. 2 -- *"ако се налага увеличение на
цената, то не може да надхвърля с повече от 50 на сто стойността на
основния договор … Когато се правят последователни изменения,
ограничението се прилага за общата стойност на измененията."* Fine: чл.
255, ал. 3. The explanation names the one lawful exception (options
foreseen in the original documentation, чл. 116, ал. 1, т. 1) and the
possibility of a register data error.

**Data-quality guards (tuned 2026-10-07):** the first run produced 20
flags; 16 were artifacts -- `CurrentContractValue` entered in stotinki/cents
(exactly +9,900%), or unit-price contracts whose "ContractValue" is the sum
of unit prices (0.59 €, 541 €, 2,023 € against six-figure estimates). Hence:
growth above `annex_max_plausible_ratio = 5.0` silences both annex rules,
and the violation tier requires **matching currency codes** on both values
(one mixed-currency case stays an `annex_growth` signal). Remaining 3: EOP
149721 (+198%), 137216 (+80%), 145407 (+76%) -- all "текущ ремонт" street/
pavement maintenance contracts, BGN on both sides; worth a document request.

### `unplanned_spending`

**Checks**, for every capital-ledger object at the latest reported period
**of each year** (so a plan added later that year resolves the flag):
- `no_plan`: annual plan (`plan_current`) 0/None while `spent_period` >=
  10,000 €; and/or
- `over_estimate`: cumulative spending (`spent_prior + spent_period`)
  exceeds the object's total estimated cost by >= 10,000 €.

**Legal basis:** ЗПФ чл. 128, ал. 1 -- *"Не се допуска извършването на
разходи … както и започването на програми или проекти, които не са
предвидени в годишния бюджет на общината."*; чл. 102, ал. 1. The
explanation says the spending is lawful if the council approved a budget
change under чл. 124, ал. 2 (*"Промените по общинския бюджет … се одобряват
от общинския съвет."*), and that decision is the first document listed.

**Thresholds:** `unplanned_min_spent_eur = 10,000`,
`unplanned_min_excess_eur = 10,000` (2026-08 has 46 zero-plan objects, most
of them 600-5,000 € items; the minimum keeps the 9 material ones). Subject
key: `<paragraph>:<object>:<year>`.

**Caveat:** in the 2026 ledger, own-funds objects such as "Товарни
автомобили за ОП Управление на отпадъците" (358,927 € spent) carry 0 in
every plan column, including the per-source funding breakdown, while
`estimated_total` equals the amount spent -- the zero is in the
municipality's own file, not a parsing artifact, but the ledger's
conventions are not documented anywhere we could find.

### `short_offer_deadline` (violation variant -- see Signals)

---

## Signals

### `splitting`

**Legal basis:** ЗОП чл. 21, ал. 15 -- *"Не се допуска разделяне на
обществена поръчка на части с което се прилага ред за възлагане за по-ниски
стойности"*; fine чл. 247, ал. 1. ал. 16 limits it: *"Не се смята за
разделяне възлагането в рамките на 12 месеца на две или повече поръчки: 1.
с обект изпълнение на строеж …; 2. с идентичен или сходен предмет, които не
са били известни на възложителя …"*; ал. 4: lots of one tender are one
procurement.

**Unit of analysis:** a *tender* (all its lots summed, чл. 21, ал. 4) -- or,
for the contractor path, a tender's share won by one contractor. Lots of one
tender are never "split".

**Groups** (signed EOP contracts, rolling 12-month window from an anchor):
1. **by contractor EIK** + category; same CPV division for supplies/
   services; for works, the same street or cadastral parcel (`ST:`/`CAD:`
   tokens from `matching.distinctive_tokens`) -- separate строежи are exempt
   (ал. 16, т. 1);
2. **by main CPV prefix (5 digits) + similar title** (Jaccard over
   `matching.tokenize` >= 0.5; works also need a shared street/parcel).

**Condition:** >= 2 units, each below a чл. 20 boundary **and** awarded under
a regime only allowed below it (обява/покана below the ал. 2 boundary;
публично състезание/пряко договаряне below the ал. 1 boundary), while their
sum crosses it. Open procedures never count (top regime: nothing gained by
dividing). Highest boundary first; each unit joins at most one group;
CPV-path groups already covered by a contractor-path group are dropped.
Severity warning; high when the sum > 2x the boundary. `details_json`:
`members` (subject, УНП, date, value, procedure), `sum_eur`,
`boundary_bgn`/`boundary_eur`, `required_regime`.

**Renewal exclusion (tuned 2026-10-07):** the first run produced 6 groups;
3 were successive annual contracts for a recurring need (БТК telecom
15.09.2021 -> 15.09.2022; irrigation maintenance; food for Домашен
социален патронаж -- 331-365 days apart), which ЗОП чл. 21, ал. 8 values per
12-month period. Groups whose members are all >= 300 days apart
(`splitting_renewal_gap_days`) are skipped. Remaining 3 (hand-checked):
- ДИ ЕНД ЕМ КОНСУЛТ -- two обяви signed the **same day** (22.06.2023),
  consecutive УНП 00126-2023-0030/0031, 26.3k € each, both consultancy for
  the same heating-appliance replacement project: the textbook pattern.
- ЕСПИ ИНВЕСТ -- two обяви 2 months apart on the **same parcel**
  (53045.502.222, a school in Обзор): heating installation + retaining wall,
  sum 183,950 € > 153,388 €. Could be two строежи (ал. 16, т. 1) -- the
  explanation says so.
- ЛИНК Мобилити -- two обяви 6 months apart for the parking payment
  system/intermediary, 35,790 € each (= 70,000 лв.).

### `exceptional_procedure`

**Checks:** signed contracts awarded through the negotiated / no-notice
family (table above). EOP is canonical; a SIGMA record counts only when its
УНП has no EOP record at all.

**Legal basis:** ЗОП чл. 79, ал. 1 -- *"Публичните възложители могат да
прилагат процедура на договаряне без предварително обявление само в
следните случаи"*, ал. 6 *"С решението за откриване на процедурата
възложителят мотивира приложимото основание по ал. 1."*; чл. 182, ал. 1-2
(пряко договаряне, same duty to motivate); чл. 191, ал. 1 (покана до
определени лица "когато е налице някое от следните основания"). Fine: чл.
250а for чл. 18, ал. 1, т. 8-10 and 13.

**Severity:** info < 100k, warning >= 100k, high >= 500k € -- except
**info** when the texts mention a commodity exchange ("стокова борса", чл.
79, ал. 1, т. 7) or the main CPV is fuel (09000000/091*). **Tuned
2026-10-07:** the top-2 raw flags were EOP ids 646/644 (1.48M €, 978k €
fuel); their Решение -- not stored in `raw_json` -- cites чл. 79, ал. 1, т.
7 (see `missing_quantity`'s caveats below). New top-2, hand-checked as
right: EOP 188598 (140k €, EU-funded ПОС 2021-2027 stove replacement,
no ground in any stored text) and EOP 170371 (135.7k € пряко договаряне for
a school playground repair, 11.5% above its own estimate, no ground stated).

### `short_offer_deadline`

**Legal minimum offer periods** (verified): open procedure 30 days from
sending the notice (ЗОП чл. 74, ал. 1), shortenable to no less than 15 (ал.
2 prior-information notice, ал. 4 urgency, motivated per ал. 5); публично
състезание 20 days (чл. 178, ал. 2), no less than 10 (ал. 3/4); събиране на
оферти с обява 10 days from publication (чл. 188, ал. 1, wording in force
since 22.12.2023). Покана до определени лица and negotiations have no
statutory offer period and are skipped.

**Notice date:** `notices[]` in `raw_json.tender_detail` carry no dates, so
the earliest of `OfferPhaseStartDate` (≈ the sending date) and
`PublicationDate` is used, falling back to `published_at` -- the earliest
date gives the longest interval, so every "too short" is conservative. Days
are counted on Sofia calendar dates (EOP stores a contract signed "18.09" as
17.09 21:00 UTC).

**Tiers:** *violation* (high) when notice->contract or the offer period
(`OfferPhaseEndDate` - notice) is below the bare minimum; *signal*
(warning) when notice->contract < legal minimum + 5-day evaluation margin,
or the offer period < the legal minimum (a shortened period must be
motivated). The margin deliberately excludes чл. 112, ал. 6's 14-day
standstill, which ал. 7, т. 2 waives for a sole participant. Обяви before
22.12.2023 are never called a violation.

**Result:** 0 flags. The shortest offer periods in the data are exactly the
legal minimums and never below (open 30 days, публично състезание 20,
обява 10, on Sofia dates from the offer-phase start), and the shortest
notice->contract gaps are 45 (open), 47 (публично състезание) and 18 days
(обява) -- all above minimum + margin (35 / 25 / 15). The rule is kept as a
guard for future data, not tuned down to produce hits.

### `bid_at_ceiling`

**Checks:** EOP contracts whose tender has exactly one signed contract
(a lot against the whole tender's estimate would be meaningless), with
`contract_value >= 0.98 x estimated value` (and <= 1.05 -- further above it
the data shows VAT/currency/lot mismatches, e.g. one contract at 1.94x its
estimate), and one bid **or an unknown number** (SIGMA twin's
`bids_received`). warning >= 100k, high >= 500k €. Estimated value:
`raw_json.procedure.EstimatedValue` with its currency code.

**Legal basis:** ЗОП чл. 21, ал. 1-2 (the estimate is set by the buyer,
*"в резултат на проведени пазарни проучвания или консултации"*); чл. 2,
ал. 2. The innocent reading (unit-price contract with a cap; a genuine
market study behind the estimate) is in the explanation.

**Hand-check:** top-2 look right -- EOP 120105, ЕКОБУЛСОРТ waste
pre-treatment, 3,579,043 € = 100.0% of the estimate with 1 bid (SIGMA);
EOP 162336, closed swimming pool, 2,303,191 € = 104.8% of the estimate, bids
unknown. 18 flags = 6.8% of the 266 single-contract tenders (126 of 266 are
>= 0.98 before the value/bids filters -- contract value = estimate is common
for capped service contracts).

### `single_bidder`

Contracts with `bids_received == 1` (SIGMA only) above 100k € (warning) /
500k € (high). Legal basis: ЗОП чл. 2, ал. 2 -- *"възложителите нямат право
да ограничават конкуренцията чрез включване на условия или изисквания,
които дават необосновано предимство или необосновано ограничават
участието"*.

### `contractor_concentration`

A contractor with >= 3 contracts **and** >= 15% of total contracted EUR in
the trailing ~24 months (`subject_type = "contractor"`, so the site links
to the contractor page). Runs over EOP only: ~87% of EOP contracts have a
same-EIK-same-value SIGMA twin, so summing both would double most spending.
Legal anchor: ЗОП чл. 2, ал. 1, т. 1-2 (равнопоставеност, свободна
конкуренция). 0 open flags in the current window.

### `annex_growth`

Current value > original x 1.10, up to the 50% cap (above it:
`annex_over_cap`, never both -- except mixed-currency cases, which stay
here). Same value derivation and plausibility guard as `annex_over_cap`.
The 10% level is an early transparency signal, far below ЗОП чл. 116, ал.
2's ceiling.

### `overspend_vs_plan` (the softer case)

Objects **with** an annual plan whose cumulative spending this year exceeds
it by > 2% and > 10,000 €, at the latest period. Objects that
`unplanned_spending` covers (no plan / over the total estimate) are skipped,
so one object never gets both -- which is why this rule dropped from 28 to
1 open flag on 2026-10-07. Innocent reading: an approved budget change
(чл. 124, ал. 2) or a mayor's compensated change (чл. 125 ЗПФ) not yet in
the ledger.

### `plan_jump`

An object's `plan_current` rising month-over-month by > 50% **and** >
100,000 €, or a new object appearing mid-year with an initial plan >
250,000 €. Example: "Основен ремонт на улица от о.т.307 Слънчев бряг до
кръстовище с ул.Сатурн" 109,160 € (2026-02) -> 424,218 € (2026-03). Every
historical transition is checked, so past jumps never auto-resolve (they
are facts). Legal anchor: ЗПФ чл. 124, ал. 2; ЗМСМА чл. 22, ал. 2 (council
acts published within 7 days).

### `unmatched_spending`

Capital-ledger objects with spending >= the direct-award threshold (ЗОП чл.
20, ал. 4, т. 3: 50 000 лв. = 25,565 €, the lowest and most general figure)
for which `matching.find_best_match` finds **no** plausible EOP/SIGMA
contract. Lowest-confidence rule (info, hedged wording: *"не открихме
публикуван договор за този обект; може да е възложен под праговете или
описан различно"*).

#### The matcher (`analysis/matching.py`)

A budget object and a contract are considered a match if **both**:

1. **Value ratio** -- `object_spend / contract_value_eur` (or its
   reciprocal) falls within `[0.5, 2.0]`.
2. **Text overlap** -- either:
   - Jaccard similarity of their (stop-word-filtered) token sets is >= 0.35,
     or
   - they share at least one "distinctive" token: a full cadastral parcel
     id (e.g. `51500.506.679`, kept whole rather than split on its dots), a
     named settlement/resort area (e.g. "Равда", "Гюльовца", "Слънчев
     бряг"), or a street name (`ул. X`).

Generic words -- "Несебър", "община", "град", "основен ремонт",
"изграждане", "доставка", "обособена позиция", and similar
procurement/budget boilerplate -- are stripped before comparison. They
appear in virtually every title in this dataset, so leaving them in would
make almost any two records "match" on bureaucratic phrasing alone. This was
tuned by hand against real object/contract title pairs from
`data/nessebar.db` (see `tests/test_rules.py`); an early looser version
matched *every* candidate purely because nearly all titles mention
"Несебър"/a shared cadastral-region prefix -- a false-positive trap this
project specifically wants to avoid overclaiming into.

This is intentionally the **lowest-recall, highest-precision** gate in this
project: when in doubt, `find_best_match` returns no match, and the rule's
own hedged wording absorbs that uncertainty rather than the matcher
overreaching into a false "these definitely don't correspond" claim in
either direction.

### `missing_annual_report`

A completed year Y with no B1/B3 cash-execution report for 12/Y in
`budget_reports` once 31 March of Y+1 has passed. Severity warning. Legal
basis: ЗПФ чл. 133, ал. 1 and 4 (*"Първостепенните разпоредители с бюджет
представят в Министерството на финансите ежемесечно и на тримесечие
отчети"*, *"се публикуват на интернет страниците"* -- the mayor is the
municipality's първостепенен разпоредител, чл. 11, ал. 3); чл. 140, ал. 5-6
(annual report adopted by 30 September, *"Приетият отчет … се публикуват на
интернет страницата на общината"*); fine чл. 173. The municipality's own
Наредба № 12 за общинския бюджет (2004, still citing the repealed Закон за
общинските бюджети, never amended to ЗПФ) sets a lighter *local* duty --
чл. 37, ал. 1 only requires the mayor to inform the community *"не по-малко
от два пъти годишно"*, in person (срещи, пресконференции, кръгли маси); it
says nothing about the website. The website-publication duty this rule
actually checks comes from ЗПФ/ЗДОИ, not from the local ordinance. December
of such a year gets this flag only, not also a monthly one. Subject
`annual:YYYY`. Current: 2022, 2023, 2024, 2025 -- the same gap as the
monthly reports below (the scraper's archive coverage, a background
download is under way).

### `eu_funded_irregularity` (meta rule -- runs last)

A contract that already carries at least one other flag in this run
(directly, or as a member of a `splitting` group) **and** is EU-funded:
SIGMA `eu_funded`, EOP `procedure.IsEUFinanced`, or notice wording naming an
operational programme / EU fund / ПРСР / План за възстановяване /
"безвъзмездна финансова помощ". A bare "Европейския съюз" is deliberately
not enough: in this data it mostly appears in insurance-territory clauses
("… на територията на Република България и Европейския съюз") and product
origin fields (9 of the 12 hits of a naive "Европейски…" search). EOP/SIGMA twins collapse onto the EOP
subject. The explanation says irregularities on EU funds are reported to
the programme's managing authority and to OLAF, and that misuse is a crime
under чл. 248а НК (**not** in `docs/law/`; cited per the project brief).
Documents: the grant contract (договор за безвъзмездна финансова помощ),
the managing authority's verification reports, project reports.

---

## Opacity

### `price_unverifiable` and `missing_quantity` -- how they split

Both answer "can a citizen check the price?". Rather than two flags on one
contract, they are split (`rules/quantity.py`):

- **`price_unverifiable`** -- the broad case: a signed supply **or works**
  contract (`TypeOfContract` 2 or 3) >= 20,000 € with **no** quantity
  anywhere, **no** substantive technical description (every published
  description -- `TenderDescription`, `notice_text`, each notice's short
  and lot descriptions -- is shorter than 80 characters once the title is
  removed from it), and **no** unit-price/framework wording. Only a title and
  a total sum are public. info; warning >= 100k €. Legal anchor: ЗОП чл. 48,
  ал. 1, т. 1 (specifications *"позволяват точно определяне на параметрите
  на предмета на поръчката"*) and чл. 36, ал. 1, т. 12 (contracts are
  published *"както и приложенията към тях"*). Works contracts also list
  "количествено-стойностна сметка" and "актове за установяване на
  извършените СМР" among the documents. Current: 2 (EOP 201397 parking
  equipment 152,848 €; EOP 62013 multilift truck 201,960 €).
- **`missing_quantity`** -- the quantity-only case (below): a supply
  contract that *does* publish a description, but no count; plus Scope B
  (capital budget objects). A contract that is `price_unverifiable` is never
  also `missing_quantity`.

### `missing_quantity`

**The citizen-facing question this answers:** if a report says "we bought new
pens for the offices" or "new uniforms for staff" and the spend was 185,000
€, that alone proves nothing is wrong -- but it also gives no way to check
anything, since 4 pens and 1 t-shirt are consistent with that same invoice
unless the exact number of objects purchased is published somewhere. This
rule flags that specific gap, for (A) EOP supply contracts and (B) § 52
capital budget objects. Since 2026-10-07 Scope A only fires when some
description *is* published (otherwise `price_unverifiable` covers the
contract); `details_json["description_chars"]` records how long it is.

#### Scope A: EOP supply ("доставки") contracts

**TypeOfContract mapping (verified against every sampled contract in
`data/nessebar.db`'s `raw_json.contract.TypeOfContract`):**

| Code | Meaning | Evidence |
|---|---|---|
| 1 | Услуги (services) | Titles matching "строител\|смр\|ремонт" under TOC==1 turned out, on inspection, to all be *services about* construction/repair -- "Упражняване на строителен надзор" (construction supervision), "Извършване на ... ремонти на компактираща инсталация" (maintenance service), "Техническо поддържане и ремонт на леки автомобили" (vehicle service contract) -- not construction itself. |
| 2 | Доставки (supplies) | 122/124 (98%) of TOC==2 titles contain "доставка"; the 2 exceptions are a "Публикации на обяви..." (publication/printing) pair, a borderline supply-adjacent service. Clear examples: "Доставка на спомагателно-хигиенни материали" (220,000 €), "Доставка на 3 броя фабрично нови автомобили". |
| 3 | Строителство (construction) | 109/146 (75%) of titles contain "строител\|смр\|ремонт\|изгражд\|реконструкц" directly (СМР, "Реконструкция и изграждане на улици..."). |
| None | No `contract` sub-object at all (open tender, not yet signed) | All 102 `TypeOfContract is None` rows have `raw_json.contract is None`; none has a `contractor_name`/`contract_date`, so the existing `is_contract` guard already excludes them. |

"закупуване" and "придобиване" (the user's other suggested keywords) appear
almost nowhere in EOP titles in this dataset -- they're budget-ledger
vocabulary (see Scope B), not EOP procurement-notice vocabulary. "доставка"
alone is a reliable, independently-verified signal for TOC==2.

**Text inspected for a quantity statement (checked in this order, and
recorded in `details_json["quantity_found_in"]` as whichever one matched, or
`"none"`):**

1. `title` (already a `ContractSubject`-or-`TenderName` fallback chain --
   see `scrapers/eop.py`'s `_normalize_contract`).
2. `tender_detail.TenderDescription` -- a short scalar straight from
   `GetPublishedTenderDetails`, HTML-stripped/entity-unescaped (it carries
   `<span>`/`&nbsp;`/`&bdquo;` markup in the raw data). This field is
   sometimes truncated mid-sentence by the API itself, not by this project's
   scrape (confirmed for ids 644-646, see the caveat below).
3. `tender_detail.notice_text` -- the richer text `scrapers/eop.py`'s
   `extract_notice`/`_build_notices` parse out of the *full* published-notice
   HTML (`TenderPublicationDetails[].HtmlPreview`, ~55-250KB per notice):
   each notice's short description (II.1.4 / eForms "Описание(BT-24-
   Procedure)") plus every обособена позиция's own description (II.2.4 /
   "Описание(BT-24-Lot)"), deduplicated across all of a tender's notices and
   capped at 8,000 chars. This is the fix for the "our own scrape is
   sometimes the one missing the number" gap noted below -- added
   2026-10-07, see docs/sources/EOP_API.md for the HTML's field structure.

SIGMA records carry none of this raw_json shape (`contract`/`tender_detail`
keys simply don't exist there), so Scope A only runs over `source == "eop"`.

**Quantity pattern:** a number immediately followed by a unit of count,
volume, area, or weight -- бр./броя/брой, компл./комплект(а/и), к-та,
тон(а/ове)/т., кв.м, куб.м, лин.м, м²/м³, кг, литра/л., м, дка,
опаковк(а/и), чифт(а/ove), единиц(а/и), час(а/ove) -- optionally with a
spelled-out number in parens/slashes in between (e.g. "10 /десет/ броя"),
plus "20 х 30"-style multiplication/dimension notation. Deliberately
excludes bare prefixes of unrelated words ("20 лева", "20 часовник", "20
тонаж" do **not** match -- see `tests/test_rules.py`'s guard cases).

**Framework/call-off exception (not flagged, only skipped quietly):** if no
quantity is found but the text says "рамково" (framework agreement), "по
единични цени" (unit pricing), "заявк-" (ordering per written request --
covers "периодично възлагане ... въз основа на писмени заявки"), or both a
"количеств-" and a "прогнозн-"/"ориентировъчн-"/"индикативн-" stem appear
anywhere (covers both word orders -- "прогнозни количества" and "количествата
... са прогнозни"), the object is treated as "quantity intentionally left
open for per-call-off ordering," not omitted, and is not flagged.

**Threshold:** `missing_quantity_min_value_eur = 20,000` (both scopes).

**Severity by contract/object value:** `info` below 100,000 €;
`warning` from 100,000 €; `high` from 500,000 €
(`missing_quantity_warning_eur` / `missing_quantity_high_eur`).

**Legal basis -- stated honestly.** ЗОП чл. 2, ал. 2 lists "количеството или
обема" (the quantity or volume) of a procurement, alongside its
subject/value/complexity, as one of the things requirements must be
proportionate to. More concretely, Приложение № 4 (towards чл. 23, ал. 5, т.
2, буква "а") ЧАСТ В, т. 6 -- the minimum required content of an **award
notice** -- states: *"Описание на поръчката: ... естество и количество или
стойност на доставките..."* (for a supply contract: nature AND
quantity-**or**-value of the goods). This is a disjunction: a notice that
states only the value (which every contract in this dataset already does,
since contract value is a separately-stored, always-present field) already
satisfies this specific provision. **A missing quantity alone is therefore
not, by itself, a proven legal violation** -- this rule's `law_ref` cites
чл. 2 + that notice-content annex as the grounding for *why quantity matters*
to a transparency check, not as a claim that its absence breaks the law.
`law_ref`: *"чл. 2, ал. 2 ЗОП ...; Приложение № 4, част В, т. 6 ЗОП ..."*
(full text in `analysis/thresholds.py`).

For Scope B, there is no ЗОП notice at all -- a capital budget ledger is a
ЗПФ budget-execution document. `law_ref` there is the general public-finance
transparency principle, ЗПФ чл. 20, т. 7, explicitly noted as *not* a direct
requirement to itemize quantities in that specific document.

#### Scope B: § 52 capital budget objects

Filters `budget_line_items` rows with `unit == "Общо"`, `paragraph == "5200"`
("Придобиване на дълготрайни активи"), at the latest reporting period, same
as the other budget-object rules. The subparagraphs found under it in
`data/nessebar.db`: 52-01 придобиване на компютри и хардуер, 52-02
придобиване на сгради, 52-03 придобиване на друго оборудване/машини и
съоръжения, 52-04 придобиване на транспортни средства, 52-05 придобиване на
стопански инвентар, 52-06 изграждане на инфраструктурни обекти, 52-19
придобиване на други ДМА.

A row is flagged if `max(plan_current, spent_period) >= 20,000 €` and its
`object_name` (the only text a budget-ledger row carries -- no full technical
description) has no quantity pattern.

**This scope is markedly lower-precision than Scope A.** Budget-ledger
object names are short labels ("Компютри за СУ Несебър STEM", "Проект и
изграждане на нови класни стаи ...") that essentially never state a count,
whether or not one exists in the underlying project file -- so most § 52
objects above the threshold get flagged regardless of whether the
municipality actually failed to specify a quantity anywhere. Treat Scope B
flags as "the budget ledger itself doesn't tell you the count" (true by
construction) rather than "no quantity exists anywhere" (unverified).

#### Honest caveats / known limitations (both scopes)

- **A singular noun implies a quantity of one, and this rule doesn't know
  that.** E.g. "Доставка на фабрично нов, товарен автомобил с надстройка тип
  „Мултилифт“" (ids 561/427/698, ~250-300k €) is grammatically singular --
  almost certainly one truck -- but states no explicit "1 брой", so it gets
  flagged. This is a real but low-severity gap in this rule's precision, not
  a transparency problem with the underlying contract. (One of them, EOP
  62013, publishes no description at all and is now a `price_unverifiable`
  flag instead.)
- **Ids 646/644/645 ("Доставка на горива...", up to 1,478,239 €) were
  re-checked after `notice_text` was added (2026-10-07) and are still
  flagged -- this time confirmed against the *full* published-notice HTML,
  not just the truncated `TenderDescription` scalar.** The original finding
  (by hand) was that `tender_detail.TenderDescription` is truncated at
  exactly 212 characters, mid-sentence, ending "...по обособени позиции:",
  while sibling fuel contracts (ids 647/648) have full 4,000-7,000 character
  `TenderDescription`s that *do* state exact liter volumes. That made it
  look like this project's own scrape was simply missing a number the
  notice stated. Parsing these three tenders' full `HtmlPreview` (one
  "Решение" + 4 per-lot "Обявление за възложена поръчка" notices) shows
  otherwise: every section (II.1.4/II.2.4 legacy and the "Решение"'s own
  IV.4 multi-lot description) repeats only the lot's *name* ("Доставка на
  Газьол за промишлени и комунални цели", "Доставка на Дизелово гориво Б6,
  за нуждите на ОП..."), never a liter volume -- this is a fuel-bought-via-
  commodity-exchange procedure (ЗОП чл. 79, ал. 1, т. 7, "Договаряне без
  предварително обявление", see the Решение's own мотиви section), where
  quantities evidently live in the technical specification attachment, not
  the notice text itself. So for these three, the gap really is "not stated
  in the published notice" (not just "not in the fields this project
  scrapes") -- though see the next caveat for the same attachment-based
  limit applying here too.
- **The quantity can exist in a field this project doesn't store at all --
  re-confirmed with the richer `notice_text`, not resolved by it.** EOP id
  450 ("Доставка, монтаж, демонтаж и баланс на гуми...", 332,340 €) is still
  flagged after the 2026-10-07 `notice_text` fix: its own
  `short_description` (eForms "Описание(BT-24-Procedure)") literally says
  *"Видът и количествата автомобилни гуми са посочени в Таблица, неразделна
  част към техническата спецификация"* ("the type and quantities of tires
  are stated in a Table, part of the technical specification") -- i.e. the
  quantity is real and published, just inside an attached document (likely
  only retrievable from the tender's ZIP export of attachments), which this
  project does not download or parse. A `missing_quantity` flag here means
  "not in the fields we scrape, confirmed not in the notice text either,"
  not "nowhere."
- **Expanding the searched text to the full notice surfaces technical
  specifications, not just purchase counts -- a new, concretely observed
  false-negative-of-flag (wrongly resolved) risk.** Of the 9 contracts
  `notice_text` newly resolved a pre-existing flag for in the 2026-10-07
  re-run, manual inspection of all 9 found every one matched a *technical
  spec number*, not "how many units were bought": id 787 ("...тип „Шредер“")
  matched "18 тона /час" (a throughput rate); id 814 ("два броя автобуси")
  matched "60 броя седящи места" (seat count, not bus count); ids
  816/817/818/820 (sanitation trucks) all matched the same "50 куб.м."
  (the truck body's load volume, not the number of trucks); ids
  31429/31438/31443 ("Tематичен онлайн портал...") matched "1.2 м" (a 3D
  scanner's working-distance spec). None of these states how many units of
  the actual procured item were bought -- the rule's quantity pattern (by
  design, see above) matches *any* number-plus-unit, and a long, detailed
  technical-specification section (which the full notice text now includes,
  unlike the old short `title`/`TenderDescription` search) is much more
  likely to incidentally contain one than a one-line contract title was.
  This was judged an acceptable trade-off (the alternative -- not reading
  the fuller text at all -- reintroduces the 644-646-style gap above) but is
  a real, now-measured precision cost: treat a `missing_quantity` *absence*
  for a technical-equipment contract as weaker evidence of "quantity was
  actually disclosed" than for a plainer supply contract.
- **Scope A's framework/call-off exception trades recall for honesty, not
  the other way around** -- e.g. any mention of "заявк-" (ordering by
  request) suppresses a flag even though a handful of such contracts (ids
  449/797/448/796/447, resp.-parts-and-repairs contracts up to 1,175,971 €)
  could still be read as under-specified. These were judged closer to
  "genuinely open-ended, ordered as needed" than "quantity omitted," given
  their own description explicitly frames deliveries as per-request.

### `missing_value`

A signed contract (`contractor_name` or `contract_date` present) whose
`contract_value_eur` and `contract_value_bgn` are both None/0. Open tenders
are not yet obliged to show a value. Legal anchor: ЗОП чл. 36, ал. 1, т. 12
(the register publishes the contracts with their annexes). 0 open flags.

### `near_threshold`

**Checks:** one flag per EOP tender (`eop-tender:<УНП>`, open tenders
included -- the estimate is known before the award): the estimated value
(fallback: summed contract values) lies within 5% **below** the чл. 20
boundary that the tender's own regime must stay under (e.g. an обява for
supplies, boundary 100 000 лв. = 51,129 €). Open procedures and the
negotiated family are skipped (no boundary to game). Only procedures
announced on/after 2024-01-01 (when the verified values took effect).

**Tier:** opacity / info by default (lawful, but the estimate's
justification should be requested); **signal / warning** when one of the
tender's contracts is also a member of a `splitting` group -- the
explanation then says so. Legal anchor: ЗОП чл. 20, ал. 2-3; чл. 21, ал. 14
(*"Изборът на метод за изчисляване на прогнозната стойност … не трябва да
се използва за прилагане на ред за възлагане за по-ниски стойности."*).

**Current:** 4 -- two works обяви at 148,275 € (= 290,000 лв., 3.3% under
300,000 лв.), and two обяви for "5 броя паркомати" at 51,000 € (0.3% under
100,000 лв.; the first, 00126-2026-0035, has no contract and was evidently
relaunched as 00126-2026-0040).

### `missing_monthly_report`

Every month from 2019-01 to the month before last (relative to the run
date) with no B1/B3 cash-execution report in `budget_reports` -> opacity /
info, subject `report:YYYY-MM`. Wording is *"Не открихме публикуван месечен
отчет…"*: the gap may be the scraper's, not the municipality's. Legal basis:
ЗПФ чл. 11, ал. 3 (the mayor is the municipality's първостепенен
разпоредител с бюджет); чл. 133, ал. 1 and 4; ЗДОИ чл. 15, ал. 1, т. 7
(*"информация за бюджета и финансовите отчети на администрацията"*) and
чл. 15а, ал. 4 (*"се публикува … в срок до три работни дни от …
създаването на съответната информация"*); fine ЗПФ чл. 173. The
municipality's own Наредба № 12 за общинския бюджет (2004, not updated to
ЗПФ) only commits the mayor to informing the community in person at least
twice a year (чл. 37, ал. 1) -- the website-publication duty comes from
ЗПФ/ЗДОИ, not the ordinance. When `budget_reports` is empty the report
rules do not run at all ("not scraped yet" is not "not published").
Current: 41 (2022-04 .. 2025-12, minus the four Decembers that carry
`missing_annual_report`).

---

## Known limitations / honest caveats

- **Pre-2024 ЗОП thresholds are not in our local law text.** `splitting`
  applies the current (2024) boundaries to every year; `near_threshold`
  skips pre-2024 procedures. If the earlier boundaries were lower (as
  generally reported, unverified locally), `splitting` under-detects for
  2020-2023; it cannot over-detect for that reason.
- **Annex 2 services are approximated by CPV prefix.** A mis-classified
  service changes which boundaries apply.
- **Contract duration is not in the data**, so `splitting` cannot tell a
  12-month renewal from a divided need directly; the 300-day renewal gap is a
  heuristic (documented above). A need divided into parts signed > 300 days
  apart is missed.
- **Register data errors exist** (values in stotinki, unit-price sums as
  "contract value", currency codes that disagree). `annex_*` has explicit
  guards; `bid_at_ceiling` caps the ratio at 1.05; other rules may still
  inherit such errors.
- **Grounds for exceptional procedures are not stored** -- the Решение's
  motives section is not in `raw_json`, so `exceptional_procedure` cannot
  tell a well-motivated negotiation from a baseless one (except the
  commodity-exchange/fuel heuristic). That is exactly why it is a *signal*.
- **EOP/SIGMA overlap is reconciled only by УНП + EIK.** Twins with a
  missing EIK are not linked; `bids_received` is then "unknown".
- **`unmatched_spending` will miss some real matches** where a budget
  object's generic description and a contract's specific one refer to the
  same thing without enough shared vocabulary -- an accepted false negative
  (the matcher was tuned for precision; loosening it made it accept nearly
  everything).
- **`plan_jump` checks every historical transition**, so its flags never
  auto-resolve -- they are historical facts.
- **Budget-report gaps are partly our coverage**: 2022-04..2025-12 are
  missing from `budget_reports` while a background download of the
  municipality's archive is still running; re-run `analyze` afterwards and
  the corresponding `missing_*_report` flags resolve automatically.
