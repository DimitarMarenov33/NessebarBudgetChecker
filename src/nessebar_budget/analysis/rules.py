"""Analysis rules: each rule inspects a record and may yield flag data.

Rules are intentionally simple and honest: they only implement checks that
are actually meaningful today. Rules that would require more context (e.g.
market-price reference data) are left as explicit placeholders that raise
NotImplementedError rather than pretending to produce a real result.
"""

from __future__ import annotations

from typing import Any, Protocol


class Rule(Protocol):
    """A rule inspects one record and yields zero or more flag dicts.

    A flag dict has keys: rule, severity, message, and optionally
    procurement_id.
    """

    name: str
    severity: str

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        ...


class MissingValueRule:
    """Flags procurement records whose contract value is missing or zero."""

    name = "missing_value"
    severity = "warning"

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        # Only signed contracts can be missing a value; open tenders without a
        # contractor/contract date are not yet obliged to show one.
        is_contract = bool(record.get("contractor_name") or record.get("contract_date"))
        if not is_contract:
            return []
        eur = record.get("contract_value_eur")
        bgn = record.get("contract_value_bgn")
        if (eur is None or eur == 0) and (bgn is None or bgn == 0):
            return [
                {
                    "rule": self.name,
                    "severity": self.severity,
                    "message": (
                        f"Procurement {record.get('source_id', '?')!r} has no "
                        "recorded contract value (None or 0)."
                    ),
                    "procurement_id": record.get("id"),
                }
            ]
        return []


class PricePerUnitRule:
    """Placeholder: flag unusually high price-per-unit vs. a reference dataset.

    TODO: this requires a reference price dataset (e.g. per CPV code / unit)
    that does not exist yet. Until that reference data is wired in, this
    rule does not pretend to evaluate anything.
    """

    name = "price_per_unit"
    severity = "warning"

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "PricePerUnitRule requires reference price data that is not available yet"
        )
