"""Repository functions: upsert logic for normalized scraper records."""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Procurement

#: Procurement fields that a normalized scraper record may set. Anything else
#: present in a record dict (e.g. bookkeeping keys a scraper carries internally)
#: is simply ignored here rather than raising.
UPSERTABLE_FIELDS = (
    "title",
    "cpv_code",
    "procedure_type",
    "estimated_value_bgn",
    "estimated_value_eur",
    "contract_value_bgn",
    "contract_value_eur",
    "currency",
    "contractor_name",
    "contractor_eik",
    "bids_received",
    "published_at",
    "contract_date",
    "url",
    "raw_json",
)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _unchanged(old_value: Any, new_value: Any) -> bool:
    """True if `old_value` (read back from the DB) already equals `new_value`.

    Numeric(16, 2) columns come back from SQLAlchemy as `decimal.Decimal`, while
    scraper records carry plain floats. `Decimal('787335.92') == 787335.92` is
    *False* (Decimal compares against the float's exact, unrounded binary value),
    even though they represent the same number -- so Decimal/float pairs are
    compared as floats instead.
    """
    if isinstance(old_value, decimal.Decimal) or isinstance(new_value, decimal.Decimal):
        if old_value is None or new_value is None:
            return old_value == new_value
        return float(old_value) == float(new_value)
    return old_value == new_value


def upsert_procurements(session: Session, records: list[dict[str, Any]]) -> tuple[int, int]:
    """Insert new Procurement rows / update existing ones, keyed on (source, source_id).

    Existing rows keep their original `created_at`; `updated_at` is only bumped
    when a field actually changes. Does not commit -- the caller controls the
    transaction boundary.

    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0
    now = _now()

    for record in records:
        source = record["source"]
        source_id = str(record["source_id"])

        existing = session.scalars(
            select(Procurement).where(
                Procurement.source == source, Procurement.source_id == source_id
            )
        ).one_or_none()

        if existing is None:
            values = {field: record.get(field) for field in UPSERTABLE_FIELDS}
            session.add(
                Procurement(
                    source=source,
                    source_id=source_id,
                    created_at=now,
                    updated_at=now,
                    **values,
                )
            )
            inserted += 1
            continue

        changed = False
        for field in UPSERTABLE_FIELDS:
            if field not in record:
                continue
            new_value = record[field]
            old_value = getattr(existing, field)
            if not _unchanged(old_value, new_value):
                setattr(existing, field, new_value)
                changed = True
        if changed:
            existing.updated_at = now
            updated += 1

    return inserted, updated
