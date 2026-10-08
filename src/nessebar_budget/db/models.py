"""SQLAlchemy 2.0 declarative models for Nessebar Budget Monitor."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""


class Procurement(Base):
    """A single procurement record, as published by a source (e.g. EOP, Minfin)."""

    __tablename__ = "procurements"

    id: Mapped[int] = mapped_column(primary_key=True)

    source: Mapped[str] = mapped_column(String(64))
    source_id: Mapped[str] = mapped_column(String(128))

    title: Mapped[str | None] = mapped_column(Text, default=None)
    cpv_code: Mapped[str | None] = mapped_column(String(32), default=None)
    procedure_type: Mapped[str | None] = mapped_column(String(64), default=None)

    estimated_value_bgn: Mapped[float | None] = mapped_column(Numeric(16, 2), default=None)
    estimated_value_eur: Mapped[float | None] = mapped_column(Numeric(16, 2), default=None)
    contract_value_bgn: Mapped[float | None] = mapped_column(Numeric(16, 2), default=None)
    contract_value_eur: Mapped[float | None] = mapped_column(Numeric(16, 2), default=None)
    currency: Mapped[str | None] = mapped_column(String(8), default=None)

    contractor_name: Mapped[str | None] = mapped_column(Text, default=None)
    contractor_eik: Mapped[str | None] = mapped_column(String(32), default=None)

    #: number of bids/offers received, when known (SIGMA's own competition signal;
    #: EOP records leave this null since no EOP endpoint inspected exposes it).
    bids_received: Mapped[int | None] = mapped_column(Integer, default=None)

    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    contract_date: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)

    url: Mapped[str | None] = mapped_column(Text, default=None)
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)

    created_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None)
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime,
        default=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None),
        onupdate=lambda: dt.datetime.now(dt.UTC).replace(tzinfo=None),
    )


class BudgetReport(Base):
    """A budget execution report document fetched from a source."""

    __tablename__ = "budget_reports"

    id: Mapped[int] = mapped_column(primary_key=True)

    period: Mapped[str | None] = mapped_column(String(32), default=None)
    kind: Mapped[str | None] = mapped_column(String(64), default=None)

    url: Mapped[str | None] = mapped_column(Text, default=None)
    file_path: Mapped[str | None] = mapped_column(Text, default=None)

    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    parsed_json: Mapped[dict | None] = mapped_column(JSON, default=None)


class Flag(Base):
    """An analysis-rule finding, optionally attached to a Procurement.

    Rows are upserted by `analysis.engine.run_full_analysis`, keyed on
    `(rule, subject_key)` (see `uq_flags_rule_subject_key` below): a subject
    seen again updates `last_seen_at`/`details_json` in place, and a subject
    a rule stops producing gets `resolved_at` set rather than being deleted.
    `subject_key` is nullable to stay compatible with the older, minimal
    `analysis.engine.run_rules` path (`pipeline.py`'s `_step_analyze`), which
    does not set it.
    """

    __tablename__ = "flags"
    __table_args__ = (
        UniqueConstraint("rule", "subject_key", name="uq_flags_rule_subject_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    procurement_id: Mapped[int | None] = mapped_column(
        ForeignKey("procurements.id"), default=None
    )

    rule: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)

    #: How to read this flag (the shared contract with the site):
    #: 'violation' = clear legal breach on the face of the data;
    #: 'signal' = pattern consistent with misconduct, needs documents;
    #: 'opacity' = lawful but unverifiable -- documents should be requested.
    tier: Mapped[str | None] = mapped_column(String(16), default=None)
    #: 2-4 plain Bulgarian sentences for citizens: what we see, why it may
    #: point to misconduct, what would make it innocent. `message` stays the
    #: 1-2 sentence headline.
    explanation: Mapped[str | None] = mapped_column(Text, default=None)
    #: JSON list of Bulgarian document names to request under ЗДОИ (e.g.
    #: "техническа спецификация", "приемо-предавателни протоколи").
    documents_json: Mapped[list | None] = mapped_column(JSON, default=None)

    #: What real-world thing this flag is about: 'contract' | 'contractor' |
    #: 'contract_group' | 'procedure' | 'budget_object' | 'cash_paragraph' |
    #: 'report'.
    subject_type: Mapped[str | None] = mapped_column(String(32), default=None)
    #: The subject's natural identifier (e.g. "eop:266822", or a budget
    #: object's paragraph+name).
    subject_id: Mapped[str | None] = mapped_column(String(256), default=None)
    #: The upsert key -- unique together with `rule` (see `__table_args__`).
    #: Usually equal to `subject_id`; kept as a separate column in case a
    #: rule ever needs a key distinct from the subject's plain identifier.
    subject_key: Mapped[str | None] = mapped_column(String(256), default=None)
    #: Rule-specific supporting numbers (dates, amounts, ratios, ...).
    details_json: Mapped[dict | None] = mapped_column(JSON, default=None)
    #: Citation of the legal article this flag is grounded in, if any.
    law_ref: Mapped[str | None] = mapped_column(Text, default=None)
    #: Provenance ("Източници"): JSON list of the published files/records the
    #: flag's numbers come from -- each {"kind", "label", "url", "file",
    #: "sheet", "row", "period", "fields": [{"name", "value", "value_eur",
    #: ...}], "note"}. Built by `analysis.provenance`; see docs/RULES.md.
    sources_json: Mapped[list | None] = mapped_column(JSON, default=None)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )
    #: When this subject was first flagged by this rule.
    first_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    #: When this subject was last (re-)produced by this rule.
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    #: Set once the rule stops producing this subject; cleared again if it reappears.
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)


class Company(Base):
    """A contractor's current state in the Trade Register (Търговски регистър),
    fetched by ЕИК from the Registry Agency portal's JSON backend
    (`scrapers.registry`). One row per ЕИК; refreshed in place."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    eik: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str | None] = mapped_column(Text, default=None)
    #: As printed by the registry, e.g. 'Дружество с ограничена отговорност'.
    legal_form: Mapped[str | None] = mapped_column(String(128), default=None)
    #: 'active' | 'liquidation' | 'insolvency' | 'deregistered' | 'unknown'
    status: Mapped[str | None] = mapped_column(String(32), default=None)
    seat_address: Mapped[str | None] = mapped_column(Text, default=None)
    activity: Mapped[str | None] = mapped_column(Text, default=None)
    nkid_code: Mapped[str | None] = mapped_column(String(16), default=None)
    nkid_label: Mapped[str | None] = mapped_column(Text, default=None)
    capital_eur: Mapped[float | None] = mapped_column(Numeric(16, 2), default=None)
    #: First registry entry for this deed (field 00010's earliest action date).
    registered_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    #: Latest financial year with an announced annual financial statement (ГФО).
    last_annual_report_year: Mapped[int | None] = mapped_column(Integer, default=None)
    #: Human-readable deed page on portal.registryagency.bg.
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
    #: Slim snapshot: {fieldIdent: {"text", "entry_date", "action_date"}, ...}.
    raw_json: Mapped[dict | None] = mapped_column(JSON, default=None)


class CompanyPerson(Base):
    """A natural or legal person named in a company's registry deed
    (manager, partner, sole owner, board member, representative)."""

    __tablename__ = "company_people"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "name_normalized", "role", "field_ident",
            name="uq_company_people_identity",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    #: Exactly as printed by the registry (usually upper-case).
    name: Mapped[str] = mapped_column(Text)
    #: Lower-case, whitespace-collapsed, no punctuation -- the matching key.
    name_normalized: Mapped[str] = mapped_column(String(256))
    #: 'manager' | 'partner' | 'sole_owner' | 'board_member' |
    #: 'representative' | 'liquidator' | 'other'
    role: Mapped[str] = mapped_column(String(32))
    #: e.g. 'Размер на дяловото участие: 50250.00 лв.'
    share_text: Mapped[str | None] = mapped_column(Text, default=None)
    #: ЕИК when the person is itself a company, else None.
    person_eik: Mapped[str | None] = mapped_column(String(32), default=None)
    #: False when the registry marks the field 'Заличено обстоятелство'.
    is_current: Mapped[bool] = mapped_column(default=True)
    #: Registry field the name came from (e.g. '00070' managers, '00190' partners).
    field_ident: Mapped[str | None] = mapped_column(String(16), default=None)


class Official(Base):
    """A person holding a municipal public office whose declaration of
    assets and interests is published by the municipality
    (`scrapers.declarations`)."""

    __tablename__ = "officials"
    __table_args__ = (
        UniqueConstraint("name_normalized", "role", "mandate", name="uq_officials_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: The declaration register's printed name, enriched to a full
    #: three-part name when `scrapers.declarations` found exactly one
    #: unambiguous match on a composition page (see `short_name` below).
    name: Mapped[str] = mapped_column(Text)
    name_normalized: Mapped[str] = mapped_column(String(256))
    #: The two-part name as originally printed on the declarations register
    #: (e.g. "Георги Георгиев"), kept once `name`/`name_normalized` have been
    #: enriched with a full name (e.g. "Георги Димитров Георгиев") off a
    #: composition page. Null for an official enrichment never ran for
    #: (e.g. no composition page covers their role) -- `name` is then still
    #: the short form. Also used by `db.repo.upsert_official` to keep
    #: matching the same person across runs after enrichment changes
    #: `name_normalized`.
    short_name: Mapped[str | None] = mapped_column(Text, default=None)
    #: 'councillor' | 'mayor' | 'deputy_mayor' | 'village_mayor' |
    #: 'secretary' | 'other'
    role: Mapped[str] = mapped_column(String(64))
    #: e.g. '2023-2027'
    mandate: Mapped[str | None] = mapped_column(String(16), default=None)
    #: The register page the name was taken from.
    source_url: Mapped[str | None] = mapped_column(Text, default=None)
    #: The composition page `name` was resolved from (e.g.
    #: `https://os-nessebar.eu/sastav`), when `short_name` was successfully
    #: enriched to a full name. Null otherwise.
    name_source_url: Mapped[str | None] = mapped_column(Text, default=None)
    #: The person's declaration PDF.
    document_url: Mapped[str | None] = mapped_column(Text, default=None)
    #: Interests parsed from the declaration: list of {"company_name",
    #: "eik", "relation", "raw"}; empty list when the PDF yields no text.
    declared_interests_json: Mapped[list | None] = mapped_column(JSON, default=None)
    #: 'text' | 'scanned' | 'missing' -- how much the PDF could be read.
    document_status: Mapped[str | None] = mapped_column(String(16), default=None)
    fetched_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
