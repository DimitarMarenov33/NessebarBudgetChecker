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
  `resolved_at`. This is what the `analyze` CLI command calls.
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
    contractor_concentration_flags,
    late_publication_flags,
    missing_quantity_flags,
    missing_value_flags,
    overspend_vs_plan_flags,
    plan_jump_flags,
    single_bidder_flags,
    unmatched_spending_flags,
)
from nessebar_budget.analysis.thresholds import Thresholds, get_thresholds
from nessebar_budget.db.budget_models import BudgetLineItem
from nessebar_budget.db.models import Flag, Procurement

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
    """new/updated/resolved flag counts for one rule, for one `analyze` run."""

    new: int = 0
    updated: int = 0
    resolved: int = 0


@dataclass
class AnalysisSummary:
    counts: dict[str, RuleCounts] = field(default_factory=dict)
    flags_produced: int = 0

    def total_new(self) -> int:
        return sum(c.new for c in self.counts.values())

    def total_updated(self) -> int:
        return sum(c.updated for c in self.counts.values())

    def total_resolved(self) -> int:
        return sum(c.resolved for c in self.counts.values())


#: Every rule `run_full_analysis` runs, in the order they're executed. Used
#: to scope which existing `Flag` rows are eligible to be auto-resolved (only
#: flags from a rule we actually ran this pass, never flags from some other/
#: future rule not part of this run).
FULL_RULE_NAMES = (
    "missing_value",
    "late_publication",
    "annex_growth",
    "single_bidder",
    "contractor_concentration",
    "overspend_vs_plan",
    "plan_jump",
    "unmatched_spending",
    "missing_quantity",
)


def _collect_flags(
    contracts: list[dict[str, Any]],
    objects: list[dict[str, Any]],
    thresholds: Thresholds,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    eop_contracts = [c for c in contracts if c["source"] == "eop"]

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
    flags += missing_value_flags(contracts)
    flags += late_publication_flags(contracts, thresholds)
    flags += annex_growth_flags(contracts, thresholds)
    flags += single_bidder_flags(contracts, thresholds)
    # Concentration is computed over a de-duplicated contract universe (EOP
    # only) -- see contractor_concentration_flags' docstring.
    flags += contractor_concentration_flags(eop_contracts, thresholds, now=now)
    flags += overspend_vs_plan_flags(objects_latest, thresholds)
    if dataset_first_period is not None:
        flags += plan_jump_flags(
            objects_history, thresholds, dataset_first_period=dataset_first_period
        )
    flags += unmatched_spending_flags(objects_latest, match_candidates, thresholds)
    flags += missing_quantity_flags(contracts, objects_latest, thresholds)
    return flags


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
        produced_keys[rule].add(subject_key)

        existing_flag = existing_by_key.get((rule, subject_key))
        if existing_flag is None:
            session.add(
                Flag(
                    rule=rule,
                    severity=hit["severity"],
                    message=hit["message"],
                    procurement_id=hit.get("procurement_id"),
                    subject_type=hit.get("subject_type"),
                    subject_id=hit.get("subject_id"),
                    subject_key=subject_key,
                    details_json=hit.get("details_json"),
                    law_ref=hit.get("law_ref"),
                    created_at=now,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            counts[rule].new += 1
        else:
            existing_flag.severity = hit["severity"]
            existing_flag.message = hit["message"]
            existing_flag.details_json = hit.get("details_json")
            existing_flag.law_ref = hit.get("law_ref")
            existing_flag.last_seen_at = now
            existing_flag.resolved_at = None
            counts[rule].updated += 1

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

    flags = _collect_flags(contracts, objects, thresholds, now)
    counts = _upsert_flags(session, flags, FULL_RULE_NAMES, now)

    return AnalysisSummary(counts=counts, flags_produced=len(flags))
