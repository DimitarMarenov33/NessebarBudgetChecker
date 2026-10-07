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
