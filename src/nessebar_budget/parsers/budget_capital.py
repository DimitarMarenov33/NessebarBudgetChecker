"""Parser for Nessebar's "Разчет за финансиране на капиталовите разходи"
(capital-expenditure financing ledger) monthly xlsx report.

Layout, originally learned from `data/samples/nesebar_budget_execution_aug2026.xlsx`
(2026, EUR-denominated) and extended to cover the 2019-2025 BGN-denominated
workbooks published under various ad hoc filenames (`kr_2021_2_5206.xlsx`,
`razchet-mai2021.xlsx`, "Месечен отчет за 2022 Май 5206 Несебър.xlsx",
`mart2024.xlsx`, ...) -- see `docs/sources/BUDGET_FORMS.md` for the full
writeup and the per-year variant notes.

Each sheet (one per organisational unit: "Общо" is the municipality-wide
total, the rest are individual кметства/schools/kindergartens) shares the
same general header shape (a handful of title/period rows, then a row of
column captions, then data) followed by data rows:

- row 10 (2026 sample): the sheet's grand total ("ОБЩО").
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

Columns for the four core monetary fields (Сметна стойност / Усвоено до
края на предходната година / Уточнен план / Усвоено към отчетния период)
are located every parse by searching the sheet's own header row for that
label text, rather than assumed at fixed D-G positions -- this is what
lets the same parser read workbooks whose column layout shifts slightly
across years. The five funding-source-group columns (H-W: each plan+actual,
two with a "в т.ч." memo sub-column, and a free-text annotation cell some
rows use instead of/alongside the structured columns) are *not* currently
re-derived by label per year -- no pre-2026 ground truth for that block was
available while writing this; extraction is defensive (out-of-range columns
read as `None` instead of raising) but still assumes the 2026 sample's
column positions. See BUDGET_FORMS.md "Known limitations".

Currency: every monetary figure is converted to EUR at parse time (2 dp),
using the fixed official rate (1 EUR = 1.95583 BGN, Bulgaria's euro-adoption
rate on 2026-01-01). The workbook's own currency marker is used when present
("Сумите са в EUR!" from 2026 on; older workbooks are expected to say
"лв."/"лева" or nothing); when no marker is found, the period decides
(< 2026-01 -> BGN, >= 2026-01 -> EUR). `currency` is always stored as
"EUR"; `extra_json` additionally records `original_currency`,
`conversion_rate`, and (when a conversion actually happened) the
the conversion rate under `extra_json["conversion_rate"]` (raw values are not duplicated).

Provenance: every row's `extra_json` carries `source_sheet` (worksheet name)
and `source_row` (1-based Excel row number) of the line it was read from.

Period: `detect_period(path)` reads the period the workbook *covers* from its
own "план/отчет за периода: YYYY <месец>" header cell. `parse-budget` uses
that, not the month the file was published under on nesebar.bg (quarterly
files and re-uploads are often posted under a later month's heading).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

import openpyxl

logger = logging.getLogger(__name__)

#: 1 EUR = 1.95583 BGN -- Bulgaria's official euro-adoption rate (2026-01-01).
BGN_PER_EUR = 1.95583

#: the five funding-source groups: (label, legal-basis/text col, plan col,
#: actual col, optional "в т.ч." plan-subset col, optional actual-subset col)
#: -- fixed positions, not label-derived; see module docstring.
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

#: label substrings (normalized: casefolded, whitespace collapsed) used to
#: locate the four core monetary columns in the sheet's own header row.
_LABEL_ESTIMATED_TOTAL = "сметна стойност"
_LABEL_SPENT_PRIOR = "усвоено до"
_LABEL_PLAN_CURRENT = "уточнен план"
_LABEL_SPENT_PERIOD = "усвоено към"
_LABEL_NAME_COL = "информация за наименованието"
_LABEL_YEARS_COL = "година начало"

_CURRENCY_EUR_RE = re.compile(r"\beur\b|евро", re.IGNORECASE)
_CURRENCY_BGN_RE = re.compile(r"\bлв\b|лев[а-я]*", re.IGNORECASE)


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


def _norm(value: Any) -> str:
    """Casefold + collapse whitespace, for label matching that should be
    resilient to the odd extra space/line-break in a merged header cell."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def _convert(value: float | None, original_currency: str) -> float | None:
    """Convert a monetary value from `original_currency` to EUR (2 dp)."""
    if value is None:
        return None
    if original_currency == "EUR":
        return round(value, 2)
    return round(value / BGN_PER_EUR, 2)


class CapitalHeaderNotFound(ValueError):
    """Raised when a sheet's core monetary-column header can't be located --
    signals the sheet (or whole workbook) is not this report's layout."""


def _find_core_columns(ws) -> dict[str, int]:
    """Locate the header row and the core columns (code/name/years + the
    four monetary fields) by their label text, scanning the first ~15 rows.

    Returns a dict with keys `header_row`, `code_col`, `name_col`,
    `years_col`, `estimated_total_col`, `spent_prior_col`,
    `plan_current_col`, `spent_period_col`.

    Raises `CapitalHeaderNotFound` if "Сметна стойност" (the one label
    assumed stable across every layout variant seen) isn't found anywhere
    in that scan window -- this is how a sheet that isn't this report form
    at all (e.g. a misfiled B1 copy, or a text report) is told apart from a
    merely-reflowed variant of it.
    """
    max_col = min(ws.max_column or 1, 30)
    max_row = min(ws.max_row or 1, 15)

    header_row: int | None = None
    estimated_total_col: int | None = None
    for r in range(1, max_row + 1):
        for c in range(1, max_col + 1):
            if _LABEL_ESTIMATED_TOTAL in _norm(ws.cell(row=r, column=c).value):
                header_row, estimated_total_col = r, c
                break
        if header_row is not None:
            break

    if header_row is None:
        raise CapitalHeaderNotFound(
            f'label "Сметна стойност" not found in the first {max_row} rows'
        )

    cols: dict[str, int] = {
        "header_row": header_row,
        "estimated_total_col": estimated_total_col,
        "code_col": 1,
        "name_col": 2,
        "years_col": 3,
        "spent_prior_col": estimated_total_col + 1,
        "plan_current_col": estimated_total_col + 2,
        "spent_period_col": estimated_total_col + 3,
    }

    row_cells = [(c, _norm(ws.cell(row=header_row, column=c).value)) for c in range(1, max_col + 1)]

    for c, norm_text in row_cells:
        if not norm_text:
            continue
        if norm_text == "§":
            cols["code_col"] = c
        elif _LABEL_NAME_COL in norm_text:
            cols["name_col"] = c
        elif _LABEL_YEARS_COL in norm_text:
            cols["years_col"] = c

    # Re-derive the three monetary columns after estimated_total by label
    # too, falling back to the "+1/+2/+3" guess above if a label is missing
    # or reordered (keeps this tolerant of a shifted/merged column layout).
    spent_prior_col = next(
        (c for c, t in row_cells if c > estimated_total_col and _LABEL_SPENT_PRIOR in t), None
    )
    plan_current_col = next(
        (c for c, t in row_cells if c > estimated_total_col and _LABEL_PLAN_CURRENT in t), None
    )
    spent_period_col = next(
        (c for c, t in row_cells if c > estimated_total_col and _LABEL_SPENT_PERIOD in t), None
    )
    if spent_prior_col is not None:
        cols["spent_prior_col"] = spent_prior_col
    if plan_current_col is not None:
        cols["plan_current_col"] = plan_current_col
    if spent_period_col is not None:
        cols["spent_period_col"] = spent_period_col

    return cols


def _detect_original_currency(ws, period: str | None) -> str:
    """Detect the workbook's original currency: an explicit marker first
    ("Сумите са в EUR!" from 2026 on; "лв."/"лева" in older workbooks),
    falling back to the period (< 2026-01 -> BGN, >= 2026-01 -> EUR) when no
    marker is found.
    """
    for row in ws.iter_rows(min_row=1, max_row=8, max_col=6, values_only=True):
        for cell in row:
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
    logger.warning(
        "budget_capital: no currency marker and no period given for sheet %r; defaulting to BGN",
        ws.title,
    )
    return "BGN"


def _find_data_start(ws, cols: dict[str, int]) -> int:
    """Return the 1-indexed row of the sheet's "ОБЩО" grand-total row."""
    code_col, name_col = cols["code_col"], cols["name_col"]
    start = cols["header_row"] + 1
    for r in range(start, min(ws.max_row, start + 20) + 1):
        code = ws.cell(row=r, column=code_col).value
        name = ws.cell(row=r, column=name_col).value
        if code is None and isinstance(name, str) and name.strip() == _HEADER_DATA_START_HINT:
            return r
    raise ValueError('could not locate the "ОБЩО" grand-total row after the header')


def _funding_extra(ws, row: int, original_currency: str) -> dict[str, Any]:
    """Read the five funding-source-group columns for one object row.

    Defensive against a narrower sheet than the 2026 sample (a column past
    `ws.max_column` reads as `None` rather than raising).
    """
    max_col = ws.max_column or 0
    funding: dict[str, Any] = {}
    funding_raw: dict[str, Any] = {}
    for group in FUNDING_GROUPS:

        def _cell(col_key: str, group: dict[str, Any] = group) -> Any:
            col = group.get(col_key)
            if col is None or col > max_col:
                return None
            return ws.cell(row=row, column=col).value

        raw_plan = _num(_cell("plan_col"))
        raw_actual = _num(_cell("actual_col"))
        raw_plan_subset = _num(_cell("plan_subset_col")) if "plan_subset_col" in group else None
        raw_actual_subset = (
            _num(_cell("actual_subset_col")) if "actual_subset_col" in group else None
        )

        entry: dict[str, Any] = {
            "plan": _convert(raw_plan, original_currency),
            "actual": _convert(raw_actual, original_currency),
        }
        raw_entry: dict[str, Any] = {"plan": raw_plan, "actual": raw_actual}
        if "plan_subset_col" in group:
            entry["plan_subset"] = _convert(raw_plan_subset, original_currency)
            raw_entry["plan_subset"] = raw_plan_subset
        if "actual_subset_col" in group:
            entry["actual_subset"] = _convert(raw_actual_subset, original_currency)
            raw_entry["actual_subset"] = raw_actual_subset
        if "note_col" in group:
            note = _text(_cell("note_col"))
            if note:
                entry["note"] = note
        funding[group["key"]] = entry
        funding_raw[group["key"]] = raw_entry

    return {"funding": funding, "funding_raw": funding_raw}


def parse_sheet(ws, period: str, original_currency: str) -> list[dict[str, Any]]:
    """Parse a single sheet of the capital-expenditure workbook into rows.

    Raises `CapitalHeaderNotFound` (caught by the caller) if the sheet isn't
    this report's layout at all.
    """
    unit = ws.title
    rows: list[dict[str, Any]] = []

    cols = _find_core_columns(ws)
    start_row = _find_data_start(ws, cols)
    rate = BGN_PER_EUR

    current_paragraph: str | None = None
    current_paragraph_name: str | None = None
    current_function: str | None = None
    current_function_name: str | None = None
    current_subparagraph: str | None = None
    current_subparagraph_name: str | None = None
    current_group_label: str | None = None

    def base_row(row_type: str) -> dict[str, Any]:
        raw_estimated = _num(ws.cell(row=r, column=cols["estimated_total_col"]).value)
        raw_spent_prior = _num(ws.cell(row=r, column=cols["spent_prior_col"]).value)
        raw_plan_current = _num(ws.cell(row=r, column=cols["plan_current_col"]).value)
        raw_spent_period = _num(ws.cell(row=r, column=cols["spent_period_col"]).value)

        extra: dict[str, Any] = {
            "row_type": row_type,
            "original_currency": original_currency,
            # Provenance: where in the workbook this line came from (1-based
            # Excel row), so a flag can point a reader at the exact cell.
            "source_sheet": ws.title,
            "source_row": r,
        }
        if original_currency != "EUR":
            # Raw BGN figures are not stored: they equal the EUR value × rate.
            extra["conversion_rate"] = rate

        return {
            "period": period,
            "unit": unit,
            "function_code": current_function,
            "paragraph": current_paragraph,
            "subparagraph": current_subparagraph,
            "years": _text(ws.cell(row=r, column=cols["years_col"]).value),
            "estimated_total": _convert(raw_estimated, original_currency),
            "spent_prior": _convert(raw_spent_prior, original_currency),
            "plan_current": _convert(raw_plan_current, original_currency),
            "spent_period": _convert(raw_spent_period, original_currency),
            "currency": "EUR",
            "extra_json": extra,
        }

    for r in range(start_row, ws.max_row + 1):
        code_raw = ws.cell(row=r, column=cols["code_col"]).value
        name_raw = ws.cell(row=r, column=cols["name_col"]).value
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
        funding = _funding_extra(ws, r, original_currency)
        row["extra_json"]["funding"] = funding["funding"]
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

    `period` overrides the period inferred from each sheet's own
    "план/отчет за периода" header text when given (`parse-budget` passes
    the workbook's `detect_period` result, or the scraper's guess when that
    fails).

    Returns `{"rows": [...], "mismatches": [...], "unsupported": str | None}`.
    `unsupported` is set (and `rows`/`mismatches` are empty) when the
    workbook doesn't match this report's layout at all -- e.g. a sheet with
    no "Сметна стойност" label anywhere, such as a misfiled B1 copy or a
    text report -- rather than forcing a parse onto the wrong document.
    """
    path = Path(path)
    try:
        wb = openpyxl.load_workbook(path, data_only=True)
    except Exception as exc:  # noqa: BLE001 -- any file this brittle format rejects
        return {"rows": [], "mismatches": [], "unsupported": f"could not open workbook: {exc}"}

    rows: list[dict[str, Any]] = []
    sheet_errors: dict[str, str] = {}
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        sheet_period = period or _infer_period(ws) or ""
        original_currency = _detect_original_currency(ws, sheet_period or period)
        try:
            rows.extend(parse_sheet(ws, sheet_period, original_currency))
        except CapitalHeaderNotFound as exc:
            sheet_errors[sheet_name] = str(exc)
            logger.warning("budget_capital: sheet %r is not this report's layout: %s", sheet_name, exc)
        except ValueError as exc:
            sheet_errors[sheet_name] = str(exc)
            logger.warning("budget_capital: could not parse sheet %r: %s", sheet_name, exc)

    unsupported = None
    if not rows and sheet_errors:
        reasons = "; ".join(f"{name}: {err}" for name, err in sheet_errors.items())
        unsupported = f"no sheet matched this report's layout ({reasons})"

    mismatches = validate_function_subtotals(rows)
    for message in mismatches:
        logger.warning("budget_capital: function-subtotal mismatch: %s", message)

    return {"rows": rows, "mismatches": mismatches, "unsupported": unsupported}


_BG_MONTHS = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}
_MONTH_ALT = "|".join(_BG_MONTHS)
#: "2022 Юни" (the form every workbook seen so far uses) ...
_YEAR_MONTH_RE = re.compile(rf"(?<!\d)(\d{{4}})\s*({_MONTH_ALT})(?![а-я])", re.IGNORECASE)
#: ... or "Юни 2022", in case a future workbook flips the order.
_MONTH_YEAR_RE = re.compile(rf"(?<![а-я])({_MONTH_ALT})\s*(\d{{4}})(?!\d)", re.IGNORECASE)
_PERIOD_LABEL = "за периода"
#: sheet that carries the municipality-wide header; checked first.
_PRIMARY_SHEET = "Общо"


def _period_from_text(value: Any) -> str | None:
    """"план/отчет за периода:  2022 Юни" -> "2022-06" (case- and
    whitespace-insensitive; year-month or month-year order)."""
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", unicodedata.normalize("NFC", value)).strip()
    m = _YEAR_MONTH_RE.search(text)
    if m:
        year, month = int(m.group(1)), _BG_MONTHS[m.group(2).casefold()]
    else:
        m = _MONTH_YEAR_RE.search(text)
        if not m:
            return None
        year, month = int(m.group(2)), _BG_MONTHS[m.group(1).casefold()]
    if not 1990 <= year <= 2100:
        return None
    return f"{year:04d}-{month:02d}"


def _period_from_rows(rows: list[tuple[Any, ...]]) -> str | None:
    """Find the report period in a sheet's first rows.

    Preferred: the "план/отчет за периода: YYYY <месец>" cell (the label and
    the period share one cell in every workbook seen, 2021-2026). If the
    label cell holds no period, a date cell to its right on the same row is
    used (the latest one -- the 2016 `kp01.01.16.xlsx` capital programme
    writes "за периода: | от | 2016-01-01"). Last resort: any header cell
    that reads like "YYYY <месец>".
    """
    for row in rows:
        for idx, cell in enumerate(row):
            if isinstance(cell, str) and _PERIOD_LABEL in _norm(cell):
                found = _period_from_text(cell)
                if found:
                    return found
                for right in row[idx + 1 :]:
                    found = _period_from_text(right)
                    if found:
                        return found
                dates = [c for c in row[idx + 1 :] if hasattr(c, "year") and hasattr(c, "month")]
                if dates:
                    latest = max(dates)
                    return f"{latest.year:04d}-{latest.month:02d}"
    for row in rows:
        for cell in row:
            found = _period_from_text(cell)
            if found:
                return found
    return None


def _infer_period(ws) -> str | None:
    """Period stated in one worksheet's own header (first 10 rows)."""
    rows = list(ws.iter_rows(min_row=1, max_row=10, max_col=12, values_only=True))
    return _period_from_rows(rows)


def detect_period(path: str | Path) -> str | None:
    """Return the period ("YYYY-MM") a capital-ledger workbook *covers*, as
    stated in its own "план/отчет за периода: YYYY <месец>" header cell --
    not the month it happened to be published under on nesebar.bg (a
    quarterly file is often posted a month later, and re-uploads land under
    yet another heading).

    The "Общо" sheet is read first, then the others, stopping at the first
    sheet whose header states a period. Returns `None` if the workbook can't
    be opened or no sheet states one (e.g. a file that isn't this report).
    """
    try:
        wb = openpyxl.load_workbook(Path(path), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 -- any unreadable file just has no period
        logger.warning("budget_capital: could not open %s to detect its period: %s", path, exc)
        return None
    try:
        names = list(wb.sheetnames)
        if _PRIMARY_SHEET in names:
            names.remove(_PRIMARY_SHEET)
            names.insert(0, _PRIMARY_SHEET)
        for name in names:
            found = _infer_period(wb[name])
            if found:
                return found
        return None
    finally:
        wb.close()
