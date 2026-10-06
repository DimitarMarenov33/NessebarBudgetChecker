"""SQLAlchemy models for parsed budget-execution line items.

These extend the shared `Base` declared in `db.models` (which also defines
`BudgetReport`, the row recording a single fetched report file). Importing
this module registers the tables below on `Base.metadata`, so they are
created by `db.session.init_db()` as long as this module has been imported
at least once before `init_db()` runs -- see `db/__init__.py`.
"""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nessebar_budget.db.models import Base


class BudgetLineItem(Base):
    """A single row from a capital-expenditure ledger ("Разчет за финансиране
    на капиталовите разходи"), as parsed by `parsers.budget_capital`.

    Rows are denormalized: every object-level row carries its own
    `function_code`/`paragraph`/`subparagraph` context, and aggregate rows
    (paragraph subtotals, function subtotals, the sheet's grand "ОБЩО" row)
    are also stored, distinguished via `extra_json["row_type"]`.
    """

    __tablename__ = "budget_line_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    report_id: Mapped[int | None] = mapped_column(ForeignKey("budget_reports.id"), default=None)

    period: Mapped[str | None] = mapped_column(String(32), default=None)
    unit: Mapped[str | None] = mapped_column(String(128), default=None)

    function_code: Mapped[str | None] = mapped_column(String(16), default=None)
    paragraph: Mapped[str | None] = mapped_column(String(16), default=None)
    subparagraph: Mapped[str | None] = mapped_column(String(16), default=None)

    object_code: Mapped[str | None] = mapped_column(String(32), default=None)
    object_name: Mapped[str | None] = mapped_column(Text, default=None)
    years: Mapped[str | None] = mapped_column(String(32), default=None)

    estimated_total: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    spent_prior: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    plan_current: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    spent_period: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)

    currency: Mapped[str | None] = mapped_column(String(8), default=None)
    extra_json: Mapped[dict | None] = mapped_column(JSON, default=None)


class CashExecutionLine(Base):
    """A single row from a "B1" cash-execution-by-paragraph report, as parsed
    by `parsers.budget_b1`.
    """

    __tablename__ = "cash_execution_lines"

    id: Mapped[int] = mapped_column(primary_key=True)

    report_id: Mapped[int | None] = mapped_column(ForeignKey("budget_reports.id"), default=None)

    period: Mapped[str | None] = mapped_column(String(32), default=None)
    section: Mapped[str | None] = mapped_column(String(32), default=None)

    paragraph: Mapped[str | None] = mapped_column(String(16), default=None)
    name: Mapped[str | None] = mapped_column(Text, default=None)

    plan_annual: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    plan_adjusted: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)
    actual_ytd: Mapped[float | None] = mapped_column(Numeric(18, 2), default=None)

    extra_json: Mapped[dict | None] = mapped_column(JSON, default=None)
