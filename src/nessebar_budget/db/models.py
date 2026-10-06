"""SQLAlchemy 2.0 declarative models for Nessebar Budget Monitor."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, Text
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
    """An analysis-rule finding, optionally attached to a Procurement."""

    __tablename__ = "flags"

    id: Mapped[int] = mapped_column(primary_key=True)

    procurement_id: Mapped[int | None] = mapped_column(
        ForeignKey("procurements.id"), default=None
    )

    rule: Mapped[str] = mapped_column(String(128))
    severity: Mapped[str] = mapped_column(String(16))
    message: Mapped[str] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=lambda: dt.datetime.now(dt.UTC)
    )
    notified_at: Mapped[dt.datetime | None] = mapped_column(DateTime, default=None)
