"""Parser for Nessebar's "Разчет за финансиране на капиталовите разходи"
(capital-expenditure financing ledger) monthly xlsx report.

Layout, learned from `data/samples/nesebar_budget_execution_aug2026.xlsx`
(see `docs/sources/BUDGET_FORMS.md` for the full writeup):

Each sheet (one per organisational unit: "Общо" is the municipality-wide
total, the rest are individual кметства/schools/kindergartens) shares the
same fixed header (rows 1-9, 1-indexed) followed by data rows:

- row 10: the sheet's grand total ("ОБЩО").
- a row whose column A is a 4-digit code ending in "00" (e.g. "5100",
  "5200") is a КР-paragraph subtotal (§51-00 Основен ремонт на ДМА,
  §52-00 Придобиване на ДМА, ...).
- a row whose column A is the literal string "Функция NN" is a function
  subtotal within the current paragraph.
- a row whose column A is a 4-digit code sharing the current paragraph's
  first two digits (e.g. "5201".."5219" under paragraph "5200") is a КР
  под-параграф subtotal (object-type breakdown: compute equipment,
  buildings, vehicles, ...).
- any other row with a 4-digit code in column A is an actual object/дейност
  line (the task's "object code", e.g. "1122", "3322", "6606" -- a leading
  "function number" digit followed by the 3-digit ЕБК "дейност" code; the
  same code can repeat across several distinct objects).
- rows with column A blank and column B one of a handful of literal labels
  ("Обект", "ППР", "инженеринг", "сграда чрез изграждане", "ППР за сграда",
  ...) are cosmetic group headers with no data of their own (they duplicate
  the subtotal above them) -- skipped, but their text is kept as
  `extra_json["group_label"]` on the object rows that follow, until the next
  label/subtotal row.

Columns D-G are estimated_total / spent_prior / plan_current / spent_period.
Columns H-W are five funding-source groups (each plan+actual, two with
"в т.ч." memo sub-columns and free-text annotation cells); captured whole
into `extra_json["funding"]`.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import openpyxl

logger = logging.getLogger(__name__)

#: spreadsheet columns (1-indexed) for the fixed left-hand block.
COL_CODE = 1
COL_NAME = 2
COL_YEARS = 3
COL_ESTIMATED_TOTAL = 4
COL_SPENT_PRIOR = 5
COL_PLAN_CURRENT = 6
COL_SPENT_PERIOD = 7

#: the five funding-source groups: (label, legal-basis/text col, plan col,
#: actual col, optional "в т.ч." plan-subset col, optional actual-subset col)
FUNDING_GROUPS: list[dict[str, Any]] = [
    {
        "key": "targeted_subsidies",
        "note_col": 8,
        "plan_col": 9,
        "plan_subset_col": 10,
        "actual_col": 11,
        "actual_subset_col": 12,
    },
    {
        "key": "carryover_targeted_subsidies",
        "note_col": 13,
        "plan_col": 14,
        "actual_col": 15,
    },
    {
        "key": "own_funds",
        "plan_col": 16,
        "actual_col": 17,
    },
    {
        "key": "other_sources",
        "note_col": 18,
        "plan_col": 19,
        "actual_col": 20,
    },
    {
        "key": "eu_funds",
        "note_col": 21,
        "plan_col": 22,
        "actual_col": 23,
    },
]

_FUNCTION_RE = re.compile(r"^Функция\s*(\d+)$", re.IGNORECASE)
_CODE4_RE = re.compile(r"^\d{4}$")
_GROUP_LABELS = {"Обект", "ППР", "ППР за сграда", "инженеринг", "сграда чрез изграждане"}
_HEADER_DATA_START_HINT = "ОБЩО"


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _detect_currency(ws) -> str:
    for row in ws.iter_rows(min_row=1, max_row=6, max_col=4, values_only=True):
        for cell in row:
            if isinstance(cell, str) and "EUR" in cell.upper():
                return "EUR"
    return "EUR"  # the form is EUR-denominated for every month seen so far


def _find_data_start(ws) -> int:
    """Return the 1-indexed row of the sheet's "ОБЩО" grand-total row."""
    for r in range(1, min(ws.max_row, 20) + 1):
        code = ws.cell(row=r, column=COL_CODE).value
        name = ws.cell(row=r, column=COL_NAME).value
        if code is None and isinstance(name, str) and name.strip() == _HEADER_DATA_START_HINT:
            return r
    raise ValueError('could not locate the "ОБЩО" header row in the first 20 rows')


def _funding_extra(ws, row: int) -> dict[str, Any]:
    funding: dict[str, Any] = {}
    for group in FUNDING_GROUPS:
        entry: dict[str, Any] = {
            "plan": _num(ws.cell(row=row, column=group["plan_col"]).value),
            "actual": _num(ws.cell(row=row, column=group["actual_col"]).value),
        }
        if "plan_subset_col" in group:
            entry["plan_subset"] = _num(ws.cell(row=row, column=group["plan_subset_col"]).value)
        if "actual_subset_col" in group:
            entry["actual_subset"] = _num(
                ws.cell(row=row, column=group["actual_subset_col"]).value
            )
        if "note_col" in group:
            note = _text(ws.cell(row=row, column=group["note_col"]).value)
            if note:
                entry["note"] = note
        funding[group["key"]] = entry
    return funding


def parse_sheet(ws, period: str, currency: str) -> list[dict[str, Any]]:
    """Parse a single sheet of the capital-expenditure workbook into rows."""
    unit = ws.title
    rows: list[dict[str, Any]] = []

    start_row = _find_data_start(ws)

    current_paragraph: str | None = None
    current_paragraph_name: str | None = None
    current_function: str | None = None
    current_function_name: str | None = None
    current_subparagraph: str | None = None
    current_subparagraph_name: str | None = None
    current_group_label: str | None = None

    def base_row(row_type: str) -> dict[str, Any]:
        return {
            "period": period,
            "unit": unit,
            "function_code": current_function,
            "paragraph": current_paragraph,
            "subparagraph": current_subparagraph,
            "years": _text(ws.cell(row=r, column=COL_YEARS).value),
            "estimated_total": _num(ws.cell(row=r, column=COL_ESTIMATED_TOTAL).value),
            "spent_prior": _num(ws.cell(row=r, column=COL_SPENT_PRIOR).value),
            "plan_current": _num(ws.cell(row=r, column=COL_PLAN_CURRENT).value),
            "spent_period": _num(ws.cell(row=r, column=COL_SPENT_PERIOD).value),
            "currency": currency,
            "extra_json": {"row_type": row_type},
        }

    for r in range(start_row, ws.max_row + 1):
        code_raw = ws.cell(row=r, column=COL_CODE).value
        name_raw = ws.cell(row=r, column=COL_NAME).value
        code = _text(code_raw) if isinstance(code_raw, str) else code_raw
        name = _text(name_raw)

        if code is None and name is None:
            continue  # fully blank spacer row

        # Grand total for the sheet.
        if code is None and name == "ОБЩО":
            row = base_row("grand_total")
            row["object_code"] = "ОБЩО"
            row["object_name"] = "ОБЩО"
            row["paragraph"] = None
            row["function_code"] = None
            row["subparagraph"] = None
            rows.append(row)
            continue

        # КР-paragraph subtotal: 4-digit code ending "00".
        if isinstance(code, str) and _CODE4_RE.match(code) and code.endswith("00"):
            current_paragraph = code
            current_paragraph_name = name
            current_function = None
            current_function_name = None
            current_subparagraph = None
            current_subparagraph_name = None
            current_group_label = None
            row = base_row("paragraph_subtotal")
            row["paragraph"] = current_paragraph
            row["object_code"] = None
            row["object_name"] = current_paragraph_name
            rows.append(row)
            continue

        # Function subtotal: "Функция NN".
        if isinstance(code, str) and (m := _FUNCTION_RE.match(code)):
            current_function = m.group(1)
            current_function_name = name
            current_subparagraph = None
            current_subparagraph_name = None
            current_group_label = None
            row = base_row("function_subtotal")
            row["object_code"] = None
            row["object_name"] = current_function_name
            rows.append(row)
            continue

        # Cosmetic group-header row ("Обект", "ППР", ...): no data of its
        # own; remember the label for the object rows that follow.
        if code is None and name in _GROUP_LABELS:
            current_group_label = name
            continue

        if code is None:
            # Unrecognised blank-code row (e.g. a stray note); skip but log.
            logger.warning("budget_capital: skipping unrecognised row %s in %r: %r", r, unit, name)
            continue

        if not (isinstance(code, str) and _CODE4_RE.match(code)):
            logger.warning(
                "budget_capital: skipping row %s in %r with unexpected code %r", r, unit, code
            )
            continue

        # КР под-параграф subtotal: shares the current paragraph's first two
        # digits but doesn't end in "00" itself (e.g. "5201".."5219" under
        # paragraph "5200").
        if (
            current_paragraph is not None
            and code[:2] == current_paragraph[:2]
            and not code.endswith("00")
        ):
            current_subparagraph = code
            current_subparagraph_name = name
            current_group_label = None
            row = base_row("subparagraph_subtotal")
            row["subparagraph"] = current_subparagraph
            row["object_code"] = None
            row["object_name"] = current_subparagraph_name
            rows.append(row)
            continue

        # Otherwise: an actual object/дейност line.
        row = base_row("object")
        row["object_code"] = code
        row["object_name"] = name
        if current_group_label:
            row["extra_json"]["group_label"] = current_group_label
        row["extra_json"]["funding"] = _funding_extra(ws, r)
        rows.append(row)

    return rows


def validate_function_subtotals(rows: list[dict[str, Any]]) -> list[str]:
    """Check that each function subtotal's estimated_total roughly equals the
    sum of the object rows filed under it (same unit/paragraph/function).

    Returns a list of human-readable mismatch descriptions (empty if none).
    """
    mismatches: list[str] = []

    subtotals = [r for r in rows if r["extra_json"].get("row_type") == "function_subtotal"]
    objects = [r for r in rows if r["extra_json"].get("row_type") == "object"]

    for sub in subtotals:
        key = (sub["unit"], sub["paragraph"], sub["function_code"])
        total = sum(
            o["estimated_total"] or 0
            for o in objects
            if (o["unit"], o["paragraph"], o["function_code"]) == key
        )
        expected = sub["estimated_total"] or 0
        if abs(total - expected) > 1.0:  # 1 EUR tolerance for rounding
            mismatches.append(
                f"{sub['unit']} / paragraph {sub['paragraph']} / function {sub['function_code']} "
                f"({sub['object_name']}): subtotal={expected} but objects sum to {total}"
            )

    return mismatches


def parse_budget_capital(path: str | Path, period: str | None = None) -> dict[str, Any]:
    """Parse all sheets of a capital-expenditure workbook.

    `period` overrides the period inferred from the workbook's own
    "план/отчет за периода" header text when given (callers normally pass
    the period already recorded for the source `BudgetReport`).

    Returns `{"rows": [...], "mismatches": [...]}`.
    """
    wb = openpyxl.load_workbook(path, data_only=True)

    rows: list[dict[str, Any]] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        currency = _detect_currency(ws)
        sheet_period = period or _infer_period(ws) or ""
        try:
            rows.extend(parse_sheet(ws, sheet_period, currency))
        except ValueError as exc:
            logger.warning("budget_capital: could not parse sheet %r: %s", sheet_name, exc)

    mismatches = validate_function_subtotals(rows)
    for message in mismatches:
        logger.warning("budget_capital: function-subtotal mismatch: %s", message)

    return {"rows": rows, "mismatches": mismatches}


_PERIOD_RE = re.compile(r"(\d{4})\s*(Януари|Февруари|Март|Април|Май|Юни|Юли|Август|"
                        r"Септември|Октомври|Ноември|Декември)", re.IGNORECASE)
_BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}


def _infer_period(ws) -> str | None:
    """Best-effort period inference from the "план/отчет за периода" header
    cell (row 3, col C in the sample), e.g. "план/отчет за периода:  2026
    Август" -> "2026-08".
    """
    for row in ws.iter_rows(min_row=1, max_row=4, max_col=4, values_only=True):
        for cell in row:
            if not isinstance(cell, str):
                continue
            m = _PERIOD_RE.search(cell)
            if m:
                year = int(m.group(1))
                month = _BG_MONTHS[m.group(2).lower()]
                return f"{year:04d}-{month:02d}"
    return None
