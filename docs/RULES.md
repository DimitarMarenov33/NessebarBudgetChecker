# Anomaly rules

This document explains what `nessebar-budget analyze` checks, why, and what
each flag means for a citizen reading it on the site. The rules live in
`src/nessebar_budget/analysis/rules.py`, thresholds (all overridable via
`RULES_*` env vars) in `analysis/thresholds.py`, the budget-object/contract
text matcher in `analysis/matching.py`, and the upsert/resolve bookkeeping in
`analysis/engine.py`.

**Tone.** Every flag's `message` is written to be neutral and non-accusatory
("изисква обяснение" / "requires an explanation") -- a flag means "this is
worth a look," never "this is wrong." Severities are `info` < `warning` <
`high`, roughly: "worth knowing" / "worth asking about" / "worth asking
about soon."

**Lifecycle.** `analyze` upserts flags keyed on `(rule, subject_key)`: a
subject seen again updates the existing row (`last_seen_at`, `details_json`,
`severity`, `message`) in place; a subject a rule no longer produces gets
`resolved_at` set (not deleted) -- the flag's history stays visible, it's
just marked as no longer current.

**Legal texts.** All law quotes below are literal excerpts from
`docs/law/*.txt` (lex.bg consolidated texts, extracted 2026-10-06; see
`docs/law/INDEX.md`). Bulgarian legal thresholds are stated in leva (лв.);
this project's data is in EUR, so BGN figures are converted using the fixed
peg rate `BGN_EUR_RATE = 1.95583` (`analysis/thresholds.py`).

## 1. LatePublicationRule (`late_publication`)

**Checks:** for each EOP contract, the gap between signing
(`raw_json.contract.ContractDate`) and the award notice being sent for
publication (`raw_json.contract.TedPublishDate`), against the legal
deadline.

**Legal basis:** ЗОП чл. 26, ал. 1, т. 1 -- *"Възложителите изпращат за
публикуване обявление за възлагане на поръчка в срок до: 1. тридесет дни
след сключване на договор за обществена поръчка или рамково споразумение."*
(30 days after signing.) The same figure is repeated for contracts at the
"пряко възлагане"-adjacent values of ЗОП чл. 20, ал. 3/7, at чл. 194, ал. 4:
*"В 30-дневен срок от сключването на договора възложителят изпраща за
публикуване в РОП обявление за възлагане на обществена поръчка..."* -- so one
30-day threshold covers every contract.

**Threshold:** `late_publication_deadline_days = 30`.

**Severity:** `high` (a hard legal deadline, clearly missed).

**Only applies to `source == "eop"`** -- SIGMA records don't carry an
equivalent publish-date field.

## 2. OverspendVsPlanRule (`overspend_vs_plan`)

**Checks**, at the latest reporting period, for each capital-ledger object:
- cumulative spending this year (`spent_period`) exceeds the current annual
  plan (`plan_current`) by more than 2% *and* more than 10,000 EUR; and/or
- cumulative spending (`spent_prior + spent_period`) exceeds the object's
  total estimated cost (`estimated_total`).

**Threshold:** `overspend_ratio = 1.02`, `overspend_abs_eur = 10_000`.

**Severity:** `warning`.

**Legal/administrative context:** ЗПФ чл. 140 governs the municipality's
annual budget-execution report and its public discussion/publication; this
rule is this project's early, month-by-month analogue of that annual check.

## 3. PlanJumpRule (`plan_jump`)

**Checks** each capital-ledger object's `plan_current` across consecutive
reporting periods for:
- a month-over-month increase of more than 50% **and** more than 100,000
  EUR; or
- a new object appearing mid-year (its first reported period isn't the
  dataset's first period) with an initial plan over 250,000 EUR.

**Thresholds:** `plan_jump_ratio = 1.5`, `plan_jump_abs_eur = 100_000`,
`new_object_min_eur = 250_000`.

**Severity:** `warning`.

**Real example found in `data/nessebar.db`:** object "Основен ремонт на
улица от о.т.307 Слънчев бряг до кръстовище с ул.Сатурн" jumps from a
109,160 EUR plan in 2026-02 to 424,218 EUR in 2026-03 (+289%, +315,058 EUR).

## 4. SingleBidderRule (`single_bidder`)

**Checks:** contracts awarded after exactly one bid/offer (`bids_received ==
1`), above a value threshold.

**Thresholds:** `single_bidder_warning_eur = 100_000` -> `warning`;
`single_bidder_high_eur = 500_000` -> `high`.

**Only applies where `bids_received` is populated** -- in this project's
data that is SIGMA records only (EOP's own export never carries this field;
see `db/models.py`'s `Procurement.bids_received` docstring).

## 5. ContractorConcentrationRule (`contractor_concentration`)

**Checks:** any contractor with >= 3 contracts **and** >= 15% of total
contracted EUR, within the trailing ~24 months.

**Thresholds:** `concentration_min_contracts = 3`,
`concentration_share = 0.15`, `concentration_window_days = 730`.

**Severity:** `info`.

**De-duplication note:** this rule runs over `source == "eop"` contracts
*only*, not EOP+SIGMA combined. EOP and SIGMA were found to both publish
largely the same underlying contracts -- ~87% of EOP contracts have a
same-EIK-same-value SIGMA "twin" -- so summing both would double most
spending and distort every contractor's share. EOP (ЦАИС ЕОП) is the
current, unified national register and has the complete/clean dates needed
for the rolling window, so it is used as the canonical contract universe for
this one rule. (Other rules either use only one source already for other
reasons -- `late_publication`/`annex_growth` need EOP's raw contract fields,
`single_bidder` needs SIGMA's `bids_received` -- or, for
`unmatched_spending`'s *matching* step, deliberately draw on both sources
since more candidates can only improve recall there, not distort a sum.)

## 6. UnmatchedSpendingRule (`unmatched_spending`)

**Checks:** each capital-ledger object with cumulative spending-to-date at
or above the ЗОП direct-award threshold, for whether automated text/value
matching (`analysis.matching.find_best_match`) finds *any* plausible
corresponding published contract (EOP or SIGMA).

**Legal basis (threshold):** ЗОП чл. 20, ал. 4 -- below these estimated
values a municipality may award **without any competitive procedure**:
- т. 1: *"80 000 лв. — при строителство"*
- т. 2: *"100 000 лв. — при услуги по приложение № 2"*
- т. 3: *"50 000 лв. — при доставки и услуги извън тези по т. 2"*

This rule uses т. 3's 50,000 лв. (≈25,565 EUR) -- the lowest, most general
figure -- since capital-ledger objects mix construction, design, and
equipment-supply spending, and the lower threshold only widens the candidate
pool that the separately-conservative matcher then has to fail to match
before anything is flagged.

**Severity:** `info` (the lowest-confidence rule here by design).

**Message is deliberately hedged:** *"не открихме публикуван договор за
този обект; може да е възложен под праговете или описан различно"* ("we
didn't find a published contract for this object; it may have been awarded
below the thresholds, or described differently") -- a "no match" means *this
script* found none, not that no contract exists.

### The matcher (`analysis/matching.py`)

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

## 7. AnnexGrowthRule (`annex_growth`)

**Checks:** EOP contracts where the current value
(`raw_json.contract.CurrentContractValue`, after any annexes/amendments)
exceeds the originally signed value (`ContractValue`) by more than 10%.

Both values are **re-derived to EUR from their own currency codes**
(`Currency`/`CurrentContractCurrency`: `1` = EUR, `3` = BGN, confirmed
against every sampled contract in `data/nessebar.db` -- see
`scrapers/eop.py`'s `CURRENCY_CODES` and `analysis/rules.py`'s
`_to_eur`) -- **not** the raw payload's own `...Euro` fields. Those were
found to sometimes go stale after an amendment: one real record's
`CurrentContractValueEuro` stayed numerically identical to its original
`ContractValueEuro` even though `CurrentContractValue` had clearly changed,
and another had a `CurrentContractValue` that, taken at face value with the
wrong currency assumption, implied a ~51x blow-up that the properly
currency-coded value shows never happened. Trusting the raw `...Euro` field
would have produced both false negatives and a false, alarming positive.

**Legal context:** ЗОП чл. 116, ал. 2 caps the **cumulative** increase from
amendments under чл. 116, ал. 1, т. 2/3 at 50% of the original contract
value: *"...то не може да надхвърля с повече от 50 на сто стойността на
основния договор или рамковото споразумение."* This rule's 10% warning
threshold is deliberately far below that statutory ceiling -- it is an early
transparency signal ("this contract's value has grown, worth a look"), not
a claim that the 50% legal cap was breached.

**Threshold:** `annex_growth_ratio = 1.10`.

**Severity:** `warning`.

## 8. MissingValueRule (`missing_value`)

Unchanged from the original implementation: flags a signed contract
(`contractor_name` or `contract_date` present) whose `contract_value_eur`
and `contract_value_bgn` are both `None`/`0`. Applies to signed contracts
only -- open tenders aren't yet obliged to show a value.

## 9. MissingQuantityRule (`missing_quantity`)

**The citizen-facing question this answers:** if a report says "we bought new
pens for the offices" or "new uniforms for staff" and the spend was 185,000
€, that alone proves nothing is wrong -- but it also gives no way to check
anything, since 4 pens and 1 t-shirt are consistent with that same invoice
unless the exact number of objects purchased is published somewhere. This
rule flags that specific gap, for (A) EOP supply contracts and (B) § 52
capital budget objects.

### Scope A: EOP supply ("доставки") contracts

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

### Scope B: § 52 capital budget objects

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

### Honest caveats / known limitations (both scopes)

- **A singular noun implies a quantity of one, and this rule doesn't know
  that.** E.g. "Доставка на фабрично нов, товарен автомобил с надстройка тип
  „Мултилифт“" (ids 561/427/698, ~250-300k €) is grammatically singular --
  almost certainly one truck -- but states no explicit "1 брой", so it gets
  flagged. This is a real but low-severity gap in this rule's precision, not
  a transparency problem with the underlying contract.
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

## ЗОП чл. 20 thresholds, for reference

Since several rules/discussions above touch it, the full picture of ЗОП чл.
20, ал. 4's direct-award ("пряко възлагане") ceilings, below which **no**
competitive procedure is legally required at all:

| Category | Threshold |
|---|---|
| Строителство (construction) | 80 000 лв. (≈40,903 EUR) |
| Услуги по приложение № 2 (Annex II services) | 100 000 лв. (≈51,129 EUR) |
| Доставки и услуги извън тези по т. 2 (other) | 50 000 лв. (≈25,565 EUR) |

## Known limitations / honest caveats

- **EOP/SIGMA overlap is not perfectly reconciled.** ~87% of EOP contracts
  have a same-EIK-same-value SIGMA counterpart; the remainder sometimes
  carry *different* recorded values for what looks like the same
  tender/lot. `ContractorConcentrationRule` sidesteps this by using EOP
  only; other rules either use one source for field-availability reasons or
  (matching) draw on both deliberately for recall, not summation.
- **`UnmatchedSpendingRule` will miss some real matches** where a budget
  object's generic description (e.g. "СУ гр.Несебър") and a contract's
  specific one (e.g. "СУ „Любен Каравелов”") refer to the same thing without
  enough shared vocabulary to clear the matcher's bar. This is treated as an
  acceptable false negative, not a bug to chase: the rule's hedged message
  is written to be true even then ("may be described differently"), and the
  alternative -- loosening the matcher until it also catches these -- was
  tried and made the matcher accept nearly everything (see above).
- **`PlanJumpRule` checks every historical month-over-month transition**,
  not just the latest one, so re-running `analyze` on unchanged historical
  data keeps "updating" (not newly creating) the same handful of past-jump
  flags indefinitely. This is intentional -- they're historical facts, not
  conditions that should silently disappear -- but it does mean they never
  auto-resolve.
