"""Runs analysis rules and persists their findings as `Flag` rows.

Two entry points coexist here, for the same reason `analysis.rules` keeps two
rule shapes (see that module's docstring):

- `run_rules`/`DEFAULT_RULES` is the original, minimal pipeline: it runs
  per-record rules (currently just `MissingValueRule`) over whatever flat
  dicts the caller hands it and yields bare `Flag` ORM objects (not
  persisted, no subject_key/upsert bookkeeping). This is kept exactly as-is
  because `pipeline.py`'s `_step_analyze` (owned by a different workstream)
  imports and calls it directly.

- `run_full_analysis` is the real engine for this project's anomaly rules:
  it loads procurements and the capital budget ledger from the DB, runs
  every rule in `analysis.rules`, and upserts the results into `Flag` keyed
  on `(rule, subject_key)` -- new subjects are inserted, previously-seen
  subjects are updated in place (severity/message/details refreshed,
  `last_seen_at` bumped), and subjects a rule no longer produces are marked
  `resolved_at`. Meta rules (`eu_funded_irregularity`) run last, over every
  other rule's output. This is what the `analyze` CLI command calls.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nessebar_budget.analysis.matching import MatchCandidate
from nessebar_budget.analysis.rules import (
    MissingValueRule,
    Rule,
    annex_growth_flags,
    annex_over_cap_flags,
    bid_at_ceiling_flags,
    build_index,
    contractor_concentration_flags,
    eu_funded_irregularity_flags,
    exceptional_procedure_flags,
    late_publication_flags,
    missing_quantity_flags,
    missing_report_flags,
    missing_value_flags,
    near_threshold_flags,
    overspend_vs_plan_flags,
    plan_jump_flags,
    price_unverifiable_flags,
    short_offer_deadline_flags,
    single_bidder_flags,
    splitting_flags,
    unmatched_spending_flags,
    unplanned_spending_flags,
)
from nessebar_budget.analysis.thresholds import Thresholds, get_thresholds
from nessebar_budget.db.budget_models import BudgetLineItem
from nessebar_budget.db.models import BudgetReport, Flag, Procurement

#: Rules considered stable enough to run by default in the *legacy* minimal
#: pipeline. PricePerUnitRule is deliberately excluded until reference price
#: data is available. See module docstring -- `run_full_analysis` below is
#: the real rule set for this project and does not use this list.
DEFAULT_RULES: list[Rule] = [MissingValueRule()]


def run_rules(
    records: Iterable[dict[str, Any]], rules: list[Rule] | None = None
) -> Iterator[Flag]:
    """Run each (legacy, per-record) rule over each record, yielding bare
    `Flag` objects for every hit. Kept for `pipeline.py`'s `_step_analyze`;
    see module docstring. Prefer `run_full_analysis` for new code.
    """
    active_rules = rules if rules is not None else DEFAULT_RULES
    for record in records:
        for rule in active_rules:
            for hit in rule.check(record):
                yield Flag(
                    procurement_id=hit.get("procurement_id"),
                    rule=hit["rule"],
                    severity=hit["severity"],
                    message=hit["message"],
                )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _procurement_to_dict(p: Procurement) -> dict[str, Any]:
    return {
        "id": p.id,
        "source": p.source,
        "source_id": p.source_id,
        "title": p.title,
        "procedure_type": p.procedure_type,
        "cpv_code": p.cpv_code,
        "contractor_name": p.contractor_name,
        "contractor_eik": p.contractor_eik,
        "contract_value_eur": _as_float(p.contract_value_eur),
        "contract_value_bgn": _as_float(p.contract_value_bgn),
        "estimated_value_eur": _as_float(p.estimated_value_eur),
        "bids_received": p.bids_received,
        "contract_date": p.contract_date,
        "published_at": p.published_at,
        "raw_json": p.raw_json,
    }


def _row_type(extra_json: dict[str, Any] | None) -> str | None:
    return (extra_json or {}).get("row_type")


def _budget_row_to_dict(r: BudgetLineItem) -> dict[str, Any]:
    return {
        "period": r.period,
        "paragraph": r.paragraph,
        "object_name": r.object_name,
        "plan_current": _as_float(r.plan_current),
        "spent_period": _as_float(r.spent_period),
        "spent_prior": _as_float(r.spent_prior),
        "estimated_total": _as_float(r.estimated_total),
    }


@dataclass
class RuleCounts:
    """Counts for one rule, for one `analyze` run: flags produced this run,
    and how many of those were new / updated rows, plus rows resolved."""

    new: int = 0
    updated: int = 0
    resolved: int = 0
    produced: int = 0
    #: tier -> flags produced this run (a rule can emit more than one tier).
    tiers: dict[str, int] = field(default_factory=dict)


@dataclass
class AnalysisSummary:
    counts: dict[str, RuleCounts] = field(default_factory=dict)
    flags_produced: int = 0
    #: tier -> flags produced this run, across all rules.
    tier_counts: dict[str, int] = field(default_factory=dict)

    def total_new(self) -> int:
        return sum(c.new for c in self.counts.values())

    def total_updated(self) -> int:
        return sum(c.updated for c in self.counts.values())

    def total_resolved(self) -> int:
        return sum(c.resolved for c in self.counts.values())


#: Every rule `run_full_analysis` runs, in execution order. Meta rules
#: (`eu_funded_irregularity`) come last: they read every other rule's output.
#: Also scopes which existing `Flag` rows are eligible to be auto-resolved
#: (only flags from a rule we actually ran this pass).
FULL_RULE_NAMES = (
    "missing_value",
    "late_publication",
    "annex_growth",
    "annex_over_cap",
    "single_bidder",
    "contractor_concentration",
    "exceptional_procedure",
    "short_offer_deadline",
    "bid_at_ceiling",
    "splitting",
    "near_threshold",
    "price_unverifiable",
    "missing_quantity",
    "unplanned_spending",
    "overspend_vs_plan",
    "plan_jump",
    "unmatched_spending",
    "missing_monthly_report",
    "missing_annual_report",
    "eu_funded_irregularity",
)


def _year_end_objects(
    objects: list[dict[str, Any]], periods: list[str]
) -> list[dict[str, Any]]:
    """Objects as of the latest reported period *of each year* in the data."""
    last_by_year: dict[str, str] = {}
    for period in periods:
        year = period[:4]
        if period > last_by_year.get(year, ""):
            last_by_year[year] = period
    year_end = set(last_by_year.values())
    return [o for o in objects if o.get("period") in year_end]


def _collect_flags(
    contracts: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    thresholds: Thresholds,
    now: dt.datetime,
    reports: list[tuple[str | None, str | None]] | None = None,
) -> list[dict[str, Any]]:
    eop_contracts = [c for c in contracts if c["source"] == "eop"]
    index = build_index(contracts)

    periods = sorted({o["period"] for o in objects if o.get("period")})
    objects_history: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for o in objects:
        name = o.get("object_name")
        if not name:
            continue
        objects_history[(o.get("paragraph"), name)].append(o)

    objects_latest: list[dict[str, Any]] = []
    dataset_first_period = periods[0] if periods else None
    if periods:
        latest_period = periods[-1]
        objects_latest = [o for o in objects if o.get("period") == latest_period]
    objects_year_end = _year_end_objects(objects, periods)

    match_candidates = [
        MatchCandidate(
            key=(c["source"], c["source_id"]),
            title=c.get("title"),
            value_eur=c.get("contract_value_eur"),
        )
        for c in contracts
        if c.get("contractor_name") and c.get("contract_value_eur")
    ]

    flags: list[dict[str, Any]] = []
    # --- contracts ---
    flags += missing_value_flags(contracts)
    flags += late_publication_flags(contracts, thresholds)
    flags += annex_growth_flags(contracts, thresholds)
    flags += annex_over_cap_flags(contracts, thresholds)
    flags += single_bidder_flags(contracts, thresholds)
    # Concentration is computed over a de-duplicated contract universe (EOP
    # only) -- see contractor_concentration_flags' docstring.
    flags += contractor_concentration_flags(eop_contracts, thresholds, now=now)
    flags += exceptional_procedure_flags(contracts, thresholds, index)
    flags += short_offer_deadline_flags(contracts, thresholds)
    flags += bid_at_ceiling_flags(contracts, thresholds, index)
    split = splitting_flags(contracts, thresholds)
    flags += split
    split_members = {m for f in split for m in f["details_json"]["member_ids"]}
    flags += near_threshold_flags(contracts, thresholds, splitting_member_ids=split_members)
    flags += price_unverifiable_flags(contracts, thresholds)
    flags += missing_quantity_flags(contracts, objects_latest, thresholds)
    # --- budget ledger ---
    flags += unplanned_spending_flags(objects_year_end, thresholds)
    flags += overspend_vs_plan_flags(objects_latest, thresholds)
    if dataset_first_period is not None:
        flags += plan_jump_flags(
            objects_history, thresholds, dataset_first_period=dataset_first_period
        )
    flags += unmatched_spending_flags(objects_latest, match_candidates, thresholds)
    # --- publication of budget reports ---
    # Only when at least one report is known: an empty `budget_reports` means
    # "not scraped yet", not "the municipality published nothing".
    if reports:
        flags += missing_report_flags(reports, thresholds, now=now)
    # --- meta rules: always last ---
    flags += eu_funded_irregularity_flags(flags, index)
    return flags


def _apply(flag: Flag, hit: dict[str, Any], now: dt.datetime) -> None:
    """Refresh every rule-owned column of `flag` from `hit`."""
    flag.severity = hit["severity"]
    flag.message = hit["message"]
    flag.tier = hit.get("tier")
    flag.explanation = hit.get("explanation")
    flag.documents_json = hit.get("documents_json")
    flag.subject_type = hit.get("subject_type")
    flag.subject_id = hit.get("subject_id")
    flag.procurement_id = hit.get("procurement_id")
    flag.details_json = hit.get("details_json")
    flag.law_ref = hit.get("law_ref")
    flag.last_seen_at = now


def _upsert_flags(
    session: Session, flags: list[dict[str, Any]], rules_run: Iterable[str], now: dt.datetime
) -> dict[str, RuleCounts]:
    """Insert/update/resolve `Flag` rows for `flags`, keyed on (rule, subject_key)."""
    rules_run = set(rules_run)
    counts: dict[str, RuleCounts] = {name: RuleCounts() for name in rules_run}

    existing = session.scalars(select(Flag).where(Flag.rule.in_(rules_run))).all()
    existing_by_key: dict[tuple[str, str], Flag] = {
        (f.rule, f.subject_key): f for f in existing if f.subject_key is not None
    }
    produced_keys: dict[str, set[str]] = defaultdict(set)

    for hit in flags:
        rule = hit["rule"]
        subject_key = hit["subject_key"]
        if subject_key in produced_keys[rule]:
            continue  # a rule emitting the same subject twice keeps the first
        produced_keys[rule].add(subject_key)
        rule_counts = counts.setdefault(rule, RuleCounts())
        rule_counts.produced += 1
        tier = hit.get("tier") or "?"
        rule_counts.tiers[tier] = rule_counts.tiers.get(tier, 0) + 1

        existing_flag = existing_by_key.get((rule, subject_key))
        if existing_flag is None:
            new_flag = Flag(
                rule=rule,
                subject_key=subject_key,
                created_at=now,
                first_seen_at=now,
            )
            _apply(new_flag, hit, now)
            session.add(new_flag)
            existing_by_key[(rule, subject_key)] = new_flag
            rule_counts.new += 1
        else:
            _apply(existing_flag, hit, now)
            existing_flag.resolved_at = None
            rule_counts.updated += 1

    for flag in existing:
        if flag.subject_key is None or flag.resolved_at is not None:
            continue
        if flag.subject_key not in produced_keys.get(flag.rule, set()):
            flag.resolved_at = now
            counts[flag.rule].resolved += 1

    return counts


def run_full_analysis(
    session: Session,
    *,
    thresholds: Thresholds | None = None,
    now: dt.datetime | None = None,
) -> AnalysisSummary:
    """Load current data, run every rule in `analysis.rules`, and upsert the
    resulting flags. Does not commit -- the caller controls the transaction.
    """
    thresholds = thresholds or get_thresholds()
    now = now or _now()

    procurement_rows = session.scalars(select(Procurement)).all()
    contracts = [_procurement_to_dict(p) for p in procurement_rows]

    budget_rows = session.scalars(
        select(BudgetLineItem).where(BudgetLineItem.unit == "Общо")
    ).all()
    objects = [
        {**_budget_row_to_dict(r), "_row_type": _row_type(r.extra_json)} for r in budget_rows
    ]
    objects = [o for o in objects if o.pop("_row_type") == "object"]

    reports = [tuple(row) for row in session.execute(select(BudgetReport.period, BudgetReport.kind))]

    flags = _collect_flags(contracts, objects, thresholds, now, reports=reports)
    counts = _upsert_flags(session, flags, FULL_RULE_NAMES, now)

    tier_counts: dict[str, int] = defaultdict(int)
    for rule_counts in counts.values():
        for tier, n in rule_counts.tiers.items():
            tier_counts[tier] += n
    return AnalysisSummary(
        counts=counts,
        flags_produced=sum(c.produced for c in counts.values()),
        tier_counts=dict(tier_counts),
    )
