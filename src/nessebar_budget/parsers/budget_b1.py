"""Parser for the standard MinFin "B1" (monthly) and "B3" (quarterly)
municipal cash-execution report xls.

Layout, learned from `data/samples/nesebar_cash_execution_B1_2026_8.xls` and
cross-checked against the official MinFin макети (`data/minfin/maketi/`,
`.../касови отчети/B3_2026_3_Mun.xls` and its `IB3_*` siblings) and against
real `B1`/`B3` files cached from 2019 on: see `docs/sources/BUDGET_FORMS.md`.
`B3` quarterly workbooks share `B1`'s exact `OTCHET` schema (same column
layout, same §§/под-§§ header shape) -- the only difference is the reporting
cadence (quarter-end cumulative instead of month-end cumulative) and, for
quarters before 2026, the period's "от"/"до" header dates spanning the whole
year-to-date range rather than a single month -- so this parser accepts
both without any kind-specific branching.

This workbook concatenates *several* logical report pages onto one `OTCHET`
sheet, each re-printing the organisation header block before its own table:
"I. ПРИХОДИ..." (revenue by §§/под-§§), "II. РАЗХОДИ - РЕКАПИТУЛАЦИЯ..."
(expenditure by §§/под-§§), then further pages for transfers, loans,
а functional/дейност breakdown, etc. This parser only extracts the first
two tables (revenue, expenditure by economic-classification параграф),
which is what the task calls for ("the main expenditure-by-paragraph
table"); everything after the expenditure table's "ВСИЧКО РАЗХОДИ" total row
is intentionally skipped -- see BUDGET_FORMS.md for exactly what that
leaves out.

Within each table, every row has (after the fixed `(a)` sort-key column in
position 0):
- col 1 (§§): filled only on a paragraph-aggregate row (its name then sits
  in col 2), OR the literal string "ВСИЧКО" marking the table's grand-total
  row (name in col 3, pseudo-code in col 2, e.g. "99-99").
- col 2 (под-§§): filled only on a subparagraph/detail row (its name then
  sits in col 3).
- cols 4-11: Уточнен план Общо, план by (държавни/местни/дофинансиране)
  дейности, ОТЧЕТ by the same three, ОТЧЕТ Общо.

Currency: the workbook carries an explicit "(в лева)" / "(в евро)" marker
(row 18, 1-indexed, in every file inspected so far, 2019-2026) just above
the header rows; this is checked first. When no marker is found, the period
decides (< 2026-01 -> BGN, >= 2026-01 -> EUR). Every monetary value is
converted to EUR at parse time (2 dp, at the fixed 1 EUR = 1.95583 BGN
rate); `extra_json` additionally records `original_currency`,
`conversion_rate`, and (only when a conversion actually happened) the
the conversion rate under `extra_json["conversion_rate"]`. `currency` is also
set on every row for forward-compatibility, even though the current
`CashExecutionLine` table/`upsert_cash_execution_lines` helper has no
`currency` column to persist it to (out of scope here -- see
`db/budget_models.py`); it is still recorded in `extra_json`, which *is*
persisted.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

SHEET_NAME = "OTCHET"

COL_SORT = 0
COL_MAJOR = 1
COL_SUB = 2
COL_NAME_IF_MAJOR = 2
COL_NAME_IF_SUB = 3
COL_PLAN_TOTAL = 4
COL_PLAN_STATE = 5
COL_PLAN_LOCAL = 6
COL_PLAN_COFINANCE = 7
COL_ACTUAL_STATE = 8
COL_ACTUAL_LOCAL = 9
COL_ACTUAL_COFINANCE = 10
COL_ACTUAL_TOTAL = 11

_MONEY_FIELDS = (
    "plan_total",
    "plan_state",
    "plan_local",
    "plan_cofinance",
    "actual_state",
    "actual_local",
    "actual_cofinance",
    "actual_total",
)

_SECTIONS_IN_ORDER = ("приходи", "разходи")

#: 1 EUR = 1.95583 BGN -- Bulgaria's official euro-adoption rate (2026-01-01).
BGN_PER_EUR = 1.95583

_CURRENCY_EUR_RE = re.compile(r"\beur\b|евро", re.IGNORECASE)
_CURRENCY_BGN_RE = re.compile(r"\bлв\b|лев[а-я]*", re.IGNORECASE)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    return text or None


def _code_str(value: Any) -> str | None:
    """Render a §§/под-§§ code cell as a plain string ("100", not "100.0")."""
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
        if isinstance(value, (int, float)):
            return str(int(value))
    except (TypeError, ValueError):
        pass
    return str(value).strip() or None


def _convert(value: float | None, original_currency: str) -> float | None:
    """Convert a monetary value from `original_currency` to EUR (2 dp)."""
    if value is None:
        return None
    if original_currency == "EUR":
        return round(value, 2)
    return round(value / BGN_PER_EUR, 2)


def _detect_original_currency(df: pd.DataFrame, period: str | None) -> str:
    """Detect the workbook's original currency from its own "(в лева)" /
    "(в евро)" marker (seen at row 18, 1-indexed, in every file inspected,
    2019-2026), falling back to the period (< 2026-01 -> BGN, >= 2026-01 ->
    EUR) when no marker is found.
    """
    for i in range(min(len(df), 25)):
        for cell in df.iloc[i]:
            if not isinstance(cell, str):
                continue
            if _CURRENCY_EUR_RE.search(cell):
                return "EUR"
            if _CURRENCY_BGN_RE.search(cell):
                return "BGN"

    if period and period < "2026-01":
        return "BGN"
    if period:
        return "EUR"
    logger.warning("budget_b1: no currency marker and no period given; defaulting to BGN")
    return "BGN"


def _find_header_rows(df: pd.DataFrame) -> list[int]:
    """Return the row indices of every "§§ / под-§§ / НАИМЕНОВАНИЕ" header."""
    header_rows = []
    for i in range(len(df)):
        row = df.iloc[i]
        if _text(row[COL_MAJOR]) == "§§" and _text(row[COL_SUB]) == "под-§§":
            header_rows.append(i)
    return header_rows


def _row_values(row: pd.Series, original_currency: str) -> dict[str, float | None]:
    raw = {
        "plan_total": _num(row[COL_PLAN_TOTAL]),
        "plan_state": _num(row[COL_PLAN_STATE]),
        "plan_local": _num(row[COL_PLAN_LOCAL]),
        "plan_cofinance": _num(row[COL_PLAN_COFINANCE]),
        "actual_state": _num(row[COL_ACTUAL_STATE]),
        "actual_local": _num(row[COL_ACTUAL_LOCAL]),
        "actual_cofinance": _num(row[COL_ACTUAL_COFINANCE]),
        "actual_total": _num(row[COL_ACTUAL_TOTAL]),
    }
    converted = {key: _convert(value, original_currency) for key, value in raw.items()}
    return {"converted": converted, "raw": raw}


def _currency_extra(original_currency: str, raw: dict[str, float | None]) -> dict[str, Any]:
    extra: dict[str, Any] = {"original_currency": original_currency}
    if original_currency != "EUR":
        extra["conversion_rate"] = BGN_PER_EUR  # raw BGN = EUR value × rate
    return extra


def _parse_block(
    df: pd.DataFrame, header_row: int, section: str, period: str, original_currency: str
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current_major: str | None = None

    r = header_row + 2  # skip the "(1) (2) ..." column-position row
    while r < len(df):
        row = df.iloc[r]
        major_raw = row[COL_MAJOR]
        major = _text(major_raw) if isinstance(major_raw, str) else _code_str(major_raw)
        sub = _code_str(row[COL_SUB])

        if major is None and sub is None:
            r += 1
            continue  # blank spacer or a footnote/"в т.ч." memo row with no code

        values = _row_values(row, original_currency)
        extra_base = _currency_extra(original_currency, values["raw"])

        if isinstance(major_raw, str) and major_raw.strip().upper() == "ВСИЧКО":
            rows.append(
                {
                    "period": period,
                    "section": section,
                    "paragraph": _code_str(row[COL_SUB]) or "ВСИЧКО",
                    "name": _text(row[COL_NAME_IF_SUB]),
                    "plan_adjusted": values["converted"]["plan_total"],
                    "actual_ytd": values["converted"]["actual_total"],
                    "plan_annual": None,
                    "currency": "EUR",
                    "extra_json": {"row_type": "section_total", **values["converted"], **extra_base},
                }
            )
            break  # end of this block

        if major is not None:
            current_major = major
            rows.append(
                {
                    "period": period,
                    "section": section,
                    "paragraph": major,
                    "name": _text(row[COL_NAME_IF_MAJOR]),
                    "plan_adjusted": values["converted"]["plan_total"],
                    "actual_ytd": values["converted"]["actual_total"],
                    "plan_annual": None,
                    "currency": "EUR",
                    "extra_json": {"row_type": "paragraph", **values["converted"], **extra_base},
                }
            )
        elif sub is not None:
            rows.append(
                {
                    "period": period,
                    "section": section,
                    "paragraph": sub,
                    "name": _text(row[COL_NAME_IF_SUB]),
                    "plan_adjusted": values["converted"]["plan_total"],
                    "actual_ytd": values["converted"]["actual_total"],
                    "plan_annual": None,
                    "currency": "EUR",
                    "extra_json": {
                        "row_type": "subparagraph",
                        "parent_paragraph": current_major,
                        **values["converted"],
                        **extra_base,
                    },
                }
            )
        r += 1

    return rows


def parse_budget_b1(path: str | Path, period: str | None = None) -> dict[str, Any]:
    """Parse the revenue and expenditure-by-paragraph tables of a B1/B3 xls.

    Returns `{"rows": [...], "skipped": [...]}`, where `skipped` documents
    the report pages after the expenditure table that were not parsed.
    """
    path = Path(path)
    xl = pd.ExcelFile(path, engine="xlrd")
    if SHEET_NAME not in xl.sheet_names:
        logger.warning("budget_b1: %s has no %r sheet; sheets=%s", path, SHEET_NAME, xl.sheet_names)
        return {"rows": [], "skipped": [f"no {SHEET_NAME!r} sheet"]}

    df = xl.parse(SHEET_NAME, header=None)

    header_rows = _find_header_rows(df)
    if not header_rows:
        logger.warning("budget_b1: no §§/под-§§ header row found in %s", path)
        return {"rows": [], "skipped": ["no header row found"]}

    inferred_period = period or _infer_period(df) or ""
    original_currency = _detect_original_currency(df, inferred_period)

    rows: list[dict[str, Any]] = []
    for section, header_row in zip(_SECTIONS_IN_ORDER, header_rows):
        rows.extend(_parse_block(df, header_row, section, inferred_period, original_currency))

    skipped = []
    if len(header_rows) > len(_SECTIONS_IN_ORDER):
        skipped.append(
            f"{len(header_rows) - len(_SECTIONS_IN_ORDER)} further report page(s) on this sheet "
            "(transfers/loans, EU-funds sub-ledgers, functional/дейност breakdown, ...) "
            "were not parsed"
        )
    if len(header_rows) < len(_SECTIONS_IN_ORDER):
        skipped.append(
            f"expected {len(_SECTIONS_IN_ORDER)} report pages (приходи, разходи) but only "
            f"found {len(header_rows)}"
        )

    return {"rows": rows, "skipped": skipped}


def _infer_period(df: pd.DataFrame) -> str | None:
    """Best-effort period inference from the "за периода от...до" header.

    Prefers the *later* ("до") of the two dates on the header's date row:
    for a monthly `B1` this is the same month as "от" anyway, but for a
    quarterly `B3` (whose "от" is always the calendar year's 1 January)
    using the earlier date would misreport a Q4 cumulative report as
    January.
    """
    best: Any = None
    for i in range(min(len(df), 20)):
        row = df.iloc[i]
        for cell in row:
            if hasattr(cell, "year") and hasattr(cell, "month") and (best is None or cell > best):
                best = cell
        if best is not None:
            break
    if best is None:
        return None
    return f"{best.year:04d}-{best.month:02d}"
