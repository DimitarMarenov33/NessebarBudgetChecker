"""Analysis rules: each rule inspects procurement/budget data and yields flag dicts.

Split into modules by subject (the old single `rules.py` grew past 2,000
lines); every public name -- and the private helpers the tests use
(`_has_quantity`, `_is_framework_defined`, ...) -- stays importable from
`nessebar_budget.analysis.rules`:

- `contracts`    -- missing_value, late_publication, annex_growth,
                    annex_over_cap, single_bidder, contractor_concentration,
                    exceptional_procedure, short_offer_deadline, bid_at_ceiling
- `competition`  -- splitting, near_threshold
- `quantity`     -- missing_quantity, price_unverifiable
- `budget`       -- unplanned_spending, overspend_vs_plan, plan_jump,
                    unmatched_spending
- `reports`      -- missing_monthly_report, missing_annual_report
- `meta`         -- eu_funded_irregularity (runs last, over the others' output)
- `linking`      -- EOP <-> SIGMA twins, tender grouping, EU-funding detection

Two rule shapes coexist: `MissingValueRule` keeps the original per-record
`Rule` protocol (`.check(record)`, used by `engine.run_rules` / pipeline.py
and tests/test_smoke.py); everything else is a dataset-level function driven
by `engine.run_full_analysis`.

Every flag dict follows the shared Flag contract built by
`_common.make_flag`: `rule`, `tier` (violation/signal/opacity), `severity`
(info/warning/high), `message` (1-2 sentence Bulgarian headline),
`explanation` (2-4 plain Bulgarian sentences), `documents_json` (documents to
request under ЗДОИ), `subject_type`, `subject_id`, `subject_key`,
`details_json`, `law_ref`. Legal citations are verified against
`docs/law/*.txt`; quotes in `analysis/thresholds.py` and `docs/RULES.md`.
"""

from __future__ import annotations

from nessebar_budget.analysis.rules._common import (
    EXCEPTIONAL_PROCEDURES,
    PROCEDURE_REGIME_TIER,
    TIER_OPACITY,
    TIER_SIGNAL,
    TIER_VIOLATION,
    TIERS,
    _clean_html,
    _local_date,
    _now,
    _parse_net_date,
    _short_title,
    _to_eur,
)
from nessebar_budget.analysis.rules.budget import (
    _unplanned_reasons,
    overspend_vs_plan_flags,
    plan_jump_flags,
    unmatched_spending_flags,
    unplanned_spending_flags,
)
from nessebar_budget.analysis.rules.competition import (
    category_of,
    near_threshold_flags,
    splitting_flags,
)
from nessebar_budget.analysis.rules.contracts import (
    MissingValueRule,
    PricePerUnitRule,
    Rule,
    annex_growth_flags,
    annex_over_cap_flags,
    bid_at_ceiling_flags,
    contractor_concentration_flags,
    exceptional_procedure_flags,
    late_publication_flags,
    missing_value_flags,
    short_offer_deadline_flags,
    single_bidder_flags,
)
from nessebar_budget.analysis.rules.linking import DatasetIndex, build_index
from nessebar_budget.analysis.rules.meta import META_RULES, eu_funded_irregularity_flags
from nessebar_budget.analysis.rules.quantity import (
    _has_quantity,
    _is_framework_defined,
    missing_quantity_flags,
    price_unverifiable_flags,
)
from nessebar_budget.analysis.rules.reports import missing_report_flags

#: Default tier of each rule (a few rules can also emit another tier per flag:
#: `short_offer_deadline` signal/violation, `near_threshold` opacity/signal).
RULE_TIERS: dict[str, str] = {
    "late_publication": TIER_VIOLATION,
    "annex_over_cap": TIER_VIOLATION,
    "unplanned_spending": TIER_VIOLATION,
    "short_offer_deadline": TIER_SIGNAL,
    "single_bidder": TIER_SIGNAL,
    "contractor_concentration": TIER_SIGNAL,
    "plan_jump": TIER_SIGNAL,
    "overspend_vs_plan": TIER_SIGNAL,
    "unmatched_spending": TIER_SIGNAL,
    "annex_growth": TIER_SIGNAL,
    "splitting": TIER_SIGNAL,
    "exceptional_procedure": TIER_SIGNAL,
    "bid_at_ceiling": TIER_SIGNAL,
    "missing_annual_report": TIER_SIGNAL,
    "eu_funded_irregularity": TIER_SIGNAL,
    "missing_quantity": TIER_OPACITY,
    "missing_value": TIER_OPACITY,
    "price_unverifiable": TIER_OPACITY,
    "near_threshold": TIER_OPACITY,
    "missing_monthly_report": TIER_OPACITY,
}

__all__ = [
    "EXCEPTIONAL_PROCEDURES",
    "META_RULES",
    "PROCEDURE_REGIME_TIER",
    "RULE_TIERS",
    "TIERS",
    "TIER_OPACITY",
    "TIER_SIGNAL",
    "TIER_VIOLATION",
    "DatasetIndex",
    "MissingValueRule",
    "PricePerUnitRule",
    "Rule",
    "_clean_html",
    "_has_quantity",
    "_is_framework_defined",
    "_local_date",
    "_now",
    "_parse_net_date",
    "_short_title",
    "_to_eur",
    "_unplanned_reasons",
    "annex_growth_flags",
    "annex_over_cap_flags",
    "bid_at_ceiling_flags",
    "build_index",
    "category_of",
    "contractor_concentration_flags",
    "eu_funded_irregularity_flags",
    "exceptional_procedure_flags",
    "late_publication_flags",
    "missing_quantity_flags",
    "missing_report_flags",
    "missing_value_flags",
    "near_threshold_flags",
    "overspend_vs_plan_flags",
    "plan_jump_flags",
    "price_unverifiable_flags",
    "short_offer_deadline_flags",
    "single_bidder_flags",
    "splitting_flags",
    "unmatched_spending_flags",
    "unplanned_spending_flags",
]
