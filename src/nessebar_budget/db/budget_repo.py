"""Upsert helpers for budget-report records and their parsed line items.

Kept separate from `db.repo` (which owns `Procurement` upserts) to avoid
touching a file another workstream is actively editing.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from nessebar_budget.db.budget_models import BudgetLineItem, CashExecutionLine
from nessebar_budget.db.models import BudgetReport


def upsert_budget_report(session: Session, record: dict[str, Any]) -> BudgetReport:
    """Insert or update a `BudgetReport` row, keyed on its (unique) `url`.

    Does not commit -- the caller controls the transaction boundary.
    """
    url = record["url"]
    existing = session.scalars(select(BudgetReport).where(BudgetReport.url == url)).one_or_none()

    if existing is None:
        report = BudgetReport(
            period=record.get("period"),
            kind=record.get("kind"),
            url=url,
            file_path=record.get("file_path"),
            fetched_at=record.get("fetched_at"),
            parsed_json=record.get("parsed_json"),
        )
        session.add(report)
        session.flush()
        return report

    for field in ("period", "kind", "file_path", "fetched_at", "parsed_json"):
        if field in record and record[field] is not None:
            setattr(existing, field, record[field])
    session.flush()
    return existing


def upsert_budget_line_items(
    session: Session, report_id: int, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """Insert new `BudgetLineItem` rows / update existing ones.

    Keyed on (report_id, unit, function_code, paragraph, subparagraph,
    object_code, object_name) so subtotal rows that share a name under
    different paragraphs (e.g. 'Функция 01' under §51-00 and §52-00) stay distinct.

    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0

    for row in rows:
        unit = row.get("unit")
        object_code = row.get("object_code")
        object_name = row.get("object_name")

        existing = session.scalars(
            select(BudgetLineItem).where(
                BudgetLineItem.report_id == report_id,
                BudgetLineItem.unit == unit,
                BudgetLineItem.function_code == row.get("function_code"),
                BudgetLineItem.paragraph == row.get("paragraph"),
                BudgetLineItem.subparagraph == row.get("subparagraph"),
                BudgetLineItem.object_code == object_code,
                BudgetLineItem.object_name == object_name,
            )
        ).first()

        values = {
            "period": row.get("period"),
            "unit": unit,
            "function_code": row.get("function_code"),
            "paragraph": row.get("paragraph"),
            "subparagraph": row.get("subparagraph"),
            "object_code": object_code,
            "object_name": object_name,
            "years": row.get("years"),
            "estimated_total": row.get("estimated_total"),
            "spent_prior": row.get("spent_prior"),
            "plan_current": row.get("plan_current"),
            "spent_period": row.get("spent_period"),
            "currency": row.get("currency"),
            "extra_json": row.get("extra_json"),
        }

        if existing is None:
            session.add(BudgetLineItem(report_id=report_id, **values))
            inserted += 1
        else:
            for field, value in values.items():
                setattr(existing, field, value)
            updated += 1

    return inserted, updated


def upsert_cash_execution_lines(
    session: Session, report_id: int, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """Insert new `CashExecutionLine` rows / update existing ones.

    Keyed on (report_id, section, paragraph), per the task spec.

    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0

    for row in rows:
        section = row.get("section")
        paragraph = row.get("paragraph")

        existing = session.scalars(
            select(CashExecutionLine).where(
                CashExecutionLine.report_id == report_id,
                CashExecutionLine.section == section,
                CashExecutionLine.paragraph == paragraph,
            )
        ).first()

        values = {
            "period": row.get("period"),
            "section": section,
            "paragraph": paragraph,
            "name": row.get("name"),
            "plan_annual": row.get("plan_annual"),
            "plan_adjusted": row.get("plan_adjusted"),
            "actual_ytd": row.get("actual_ytd"),
            "extra_json": row.get("extra_json"),
        }

        if existing is None:
            session.add(CashExecutionLine(report_id=report_id, **values))
            inserted += 1
        else:
            for field, value in values.items():
                setattr(existing, field, value)
            updated += 1

    return inserted, updated
