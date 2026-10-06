"""Runs a set of rules over records and yields Flag ORM objects (not yet persisted)."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from nessebar_budget.analysis.rules import MissingValueRule, Rule
from nessebar_budget.db.models import Flag

#: Rules considered stable enough to run by default. PricePerUnitRule is
#: deliberately excluded until reference price data is available.
DEFAULT_RULES: list[Rule] = [MissingValueRule()]


def run_rules(
    records: Iterable[dict[str, Any]], rules: list[Rule] | None = None
) -> Iterator[Flag]:
    """Run each rule over each record, yielding Flag objects for every hit."""
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
