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
