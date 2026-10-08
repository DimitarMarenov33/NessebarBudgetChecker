"""Repository functions: upsert logic for normalized scraper records."""

from __future__ import annotations

import datetime as dt
import decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Company, CompanyPerson, Official, Procurement
from nessebar_budget.scrapers.declarations import normalize_name

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


#: Official fields a normalized `declarations` scraper record may set (beyond
#: the identity key fields `name`/`role`/`mandate`, handled separately below).
_OFFICIAL_UPSERTABLE_FIELDS = (
    "source_url",
    "document_url",
    "declared_interests_json",
    "document_status",
    "fetched_at",
)


def upsert_official(session: Session, parsed: dict[str, Any]) -> Official:
    """Insert a new `Official` row / update an existing one, keyed on
    `(name_normalized, role, mandate)` (see `Official.__table_args__`).

    `parsed` is one record from `scrapers.declarations.DeclarationsScraper.run`
    (or `parse_register_page` plus the PDF-parsing fields): `name`, `role`,
    `mandate`, optionally `short_name`/`name_source_url` (full-name
    enrichment, see `scrapers.declarations.resolve_full_name`), and the
    fields in `_OFFICIAL_UPSERTABLE_FIELDS`. `name_normalized` is computed
    here (not expected on `parsed`). Does not commit -- the caller controls
    the transaction boundary. Returns the inserted/updated `Official`.

    Identity lookup is resilient to `name` switching between a short
    (register-printed) and full (enriched) form across runs: a row matches
    if either its stored `name_normalized` *or* its stored `short_name`
    (normalized) equals the incoming record's short name -- `short_name`
    defaults to `name` when enrichment found nothing, so a never-enriched
    row (where `name` already *is* the short form) still matches on the
    first condition alone. This also means an already-enriched row's full
    `name` is never downgraded back to a short one just because enrichment
    didn't run (or found nothing) on a later call -- see `resolved_this_run`
    below.
    """
    name = parsed["name"]
    role = parsed["role"]
    mandate = parsed.get("mandate")
    short_name = parsed.get("short_name") or name
    name_normalized = normalize_name(name)
    short_name_normalized = normalize_name(short_name)
    resolved_this_run = name_normalized != short_name_normalized

    existing = None
    for candidate in session.scalars(
        select(Official).where(Official.role == role, Official.mandate == mandate)
    ):
        candidate_short_normalized = (
            normalize_name(candidate.short_name) if candidate.short_name else candidate.name_normalized
        )
        if candidate.name_normalized == name_normalized or candidate_short_normalized == short_name_normalized:
            existing = candidate
            break

    if existing is None:
        values = {field: parsed.get(field) for field in _OFFICIAL_UPSERTABLE_FIELDS}
        official = Official(
            name=name,
            name_normalized=name_normalized,
            short_name=short_name,
            name_source_url=parsed.get("name_source_url") if resolved_this_run else None,
            role=role,
            mandate=mandate,
            **values,
        )
        session.add(official)
        return official

    # Never overwrite an already-enriched full name with a short one just
    # because this run's composition-page fetch failed/found nothing --
    # only replace name/name_normalized when this run resolved one (or the
    # row was never enriched to begin with, i.e. name == its own short form).
    already_enriched = existing.name_normalized != normalize_name(
        existing.short_name or existing.name
    )
    if resolved_this_run or not already_enriched:
        existing.name = name
        existing.name_normalized = name_normalized
        if resolved_this_run:
            existing.name_source_url = parsed.get("name_source_url")
    existing.short_name = short_name

    for field in _OFFICIAL_UPSERTABLE_FIELDS:
        if field in parsed:
            setattr(existing, field, parsed[field])
    return existing


#: Company fields a `registry` scraper record may set (beyond the identity
#: key `eik`).
_COMPANY_UPSERTABLE_FIELDS = (
    "name",
    "legal_form",
    "status",
    "seat_address",
    "activity",
    "nkid_code",
    "nkid_label",
    "capital_eur",
    "registered_at",
    "last_annual_report_year",
    "source_url",
    "raw_json",
)

#: CompanyPerson fields copied verbatim from each `parsed["people"]` dict
#: (see `scrapers.registry.parse_deed`/`_extract_people`).
_COMPANY_PERSON_FIELDS = (
    "name",
    "name_normalized",
    "role",
    "share_text",
    "person_eik",
    "is_current",
    "field_ident",
)


def distinct_contractor_eiks(session: Session) -> list[str]:
    """Every distinct non-empty `Procurement.contractor_eik`, sorted.

    Used by the `registry` scraper's CLI/pipeline wiring to discover which
    ЕИКs to look up in the Trade Register -- `registry` has no fixed scope
    of its own (unlike `eop`/`sigma`, which always mean "Община Несебър").
    """
    from nessebar_budget.scrapers.registry import (
        normalize_eiks,  # local: avoid scraper import at DB import time
    )

    raw_values = session.scalars(select(Procurement.contractor_eik).distinct()).all()
    return sorted({eik for raw in raw_values for eik in normalize_eiks(raw)})


def upsert_company(session: Session, parsed: dict[str, Any]) -> Company:
    """Insert a new `Company` row / update an existing one, keyed on `eik`.

    `parsed` is one record from `scrapers.registry.parse_deed`: `eik` plus
    the fields in `_COMPANY_UPSERTABLE_FIELDS`, plus `people` (a list of
    `CompanyPerson`-shaped dicts). Sets `fetched_at` to now.

    The company's `company_people` rows are replaced atomically on every
    call -- every existing row for this company is deleted and the parsed
    `people` list is inserted fresh -- so a re-upsert never leaves
    stale/duplicate rows around even though which idents a person shows up
    under (or whether they're still a current manager/partner/etc. at all)
    can change between runs. Does not commit -- the caller controls the
    transaction boundary.
    """
    eik = parsed["eik"]
    now = _now()

    existing = session.scalars(select(Company).where(Company.eik == eik)).one_or_none()

    if existing is None:
        values = {field: parsed.get(field) for field in _COMPANY_UPSERTABLE_FIELDS}
        company = Company(eik=eik, fetched_at=now, **values)
        session.add(company)
    else:
        company = existing
        for field in _COMPANY_UPSERTABLE_FIELDS:
            if field in parsed:
                setattr(company, field, parsed[field])
        company.fetched_at = now

    # Flush so `company.id` exists (new row) / stays valid (existing row)
    # before it's used as the FK for the delete+reinsert below.
    session.flush()
    session.execute(delete(CompanyPerson).where(CompanyPerson.company_id == company.id))

    for person in parsed.get("people") or []:
        values = {field: person.get(field) for field in _COMPANY_PERSON_FIELDS}
        session.add(CompanyPerson(company_id=company.id, **values))

    return company
