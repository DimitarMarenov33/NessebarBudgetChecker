"""Tests for the budget-execution parsers, using real sample files in
`data/samples/` as fixtures:

- `nesebar_budget_execution_aug2026.xlsx` / `nesebar_cash_execution_B1_2026_8.xls`:
  2026, EUR-denominated (the forms the parsers were originally built on).
- `nesebar_budget_execution_feb2021_BGN.xlsx`: 2021 capital ledger, BGN,
  no explicit currency marker (period-based fallback).
- `nesebar_cash_execution_B1_2020_1_BGN.xls`: 2020 monthly B1, BGN, with
  its own "(в лева)" marker.
- `nesebar_cash_execution_B3_2019_1_BGN.xls`: 2019 Q1 quarterly B3 (same
  `OTCHET` schema as B1, cumulative Jan-Mar), BGN.

Conversion rate: 1 EUR = 1.95583 BGN (Bulgaria's official euro-adoption
rate, 2026-01-01).
"""

from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from nessebar_budget.parsers.budget_b1 import BGN_PER_EUR as B1_BGN_PER_EUR
from nessebar_budget.parsers.budget_b1 import parse_budget_b1
from nessebar_budget.parsers.budget_capital import BGN_PER_EUR as CAPITAL_BGN_PER_EUR
from nessebar_budget.parsers.budget_capital import parse_budget_capital
from nessebar_budget.scrapers.nesebar_site import classify_filename

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
CAPITAL_XLSX = SAMPLES / "nesebar_budget_execution_aug2026.xlsx"
CAPITAL_XLSX_2021_BGN = SAMPLES / "nesebar_budget_execution_feb2021_BGN.xlsx"
B1_XLS = SAMPLES / "nesebar_cash_execution_B1_2026_8.xls"
B1_XLS_2020_BGN = SAMPLES / "nesebar_cash_execution_B1_2020_1_BGN.xls"
B3_XLS_2019_BGN = SAMPLES / "nesebar_cash_execution_B3_2019_1_BGN.xls"

RATE = 1.95583


def eur(bgn: float) -> float:
    return round(bgn / RATE, 2)


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def capital_result():
    return parse_budget_capital(CAPITAL_XLSX)


@pytest.fixture(scope="module")
def capital_result_2021_bgn():
    return parse_budget_capital(CAPITAL_XLSX_2021_BGN, period="2021-02")


@pytest.fixture(scope="module")
def b1_result():
    return parse_budget_b1(B1_XLS)


@pytest.fixture(scope="module")
def b1_result_2020_bgn():
    return parse_budget_b1(B1_XLS_2020_BGN)


@pytest.fixture(scope="module")
def b3_result_2019_bgn():
    return parse_budget_b1(B3_XLS_2019_BGN)


# -- capital ledger: 2026 EUR sample (original layout) ------------------------


def test_capital_ledger_grand_total(capital_result):
    """The "Общо" sheet's ОБЩО row should carry the municipality-wide total
    Сметна стойност (estimated_total) of 24,990,398 EUR."""
    rows = capital_result["rows"]
    totals = [
        r
        for r in rows
        if r["unit"] == "Общо" and r["object_code"] == "ОБЩО" and r["extra_json"]["row_type"] == "grand_total"
    ]
    assert len(totals) == 1
    assert totals[0]["estimated_total"] == pytest.approx(24990398)
    assert totals[0]["currency"] == "EUR"


def test_capital_ledger_captures_named_object(capital_result):
    """A specific object row ("Леки автомобили за Общинска администрация")
    with a planned/estimated cost of 100,000 EUR should be present."""
    rows = capital_result["rows"]
    matches = [
        r
        for r in rows
        if r["object_name"] and "Леки автомобили за Общинска администрация" in r["object_name"]
    ]
    assert len(matches) == 1
    row = matches[0]
    assert row["unit"] == "Общо"
    assert row["estimated_total"] == pytest.approx(100000)
    assert row["object_code"] == "1122"


def test_capital_ledger_no_validation_mismatches(capital_result):
    """Function subtotals should reconcile with the sum of their objects
    (the sample file is well-formed)."""
    assert capital_result["mismatches"] == []


def test_capital_ledger_parses_every_sheet(capital_result):
    units = {r["unit"] for r in capital_result["rows"]}
    assert "Общо" in units
    assert len(units) > 1  # кметства/schools sheets also parsed


def test_capital_ledger_not_unsupported(capital_result):
    assert capital_result["unsupported"] is None


def test_capital_ledger_eur_marker_no_original_block(capital_result):
    """2026 sample has its own "Сумите са в EUR!" marker: no conversion
    happens, and no `extra_json["original"]` block is recorded."""
    totals = [
        r
        for r in capital_result["rows"]
        if r["unit"] == "Общо" and r["object_code"] == "ОБЩО"
    ]
    extra = totals[0]["extra_json"]
    assert extra["original_currency"] == "EUR"
    assert "original" not in extra
    assert "conversion_rate" not in extra


# -- capital ledger: 2021 BGN sample (currency fallback + header detection) --


def test_capital_ledger_2021_bgn_currency_fallback(capital_result_2021_bgn):
    """The 2021 workbook has no explicit currency marker; the period
    (2021-02, < 2026-01) should drive the BGN fallback."""
    assert capital_result_2021_bgn["unsupported"] is None
    totals = [
        r
        for r in capital_result_2021_bgn["rows"]
        if r["unit"] == "Общо" and r["object_code"] == "ОБЩО"
    ]
    assert len(totals) == 1
    extra = totals[0]["extra_json"]
    assert extra["original_currency"] == "BGN"
    assert extra["conversion_rate"] == pytest.approx(CAPITAL_BGN_PER_EUR)


def test_capital_ledger_2021_bgn_conversion_known_cell(capital_result_2021_bgn):
    """The "Общо" grand total's Сметна стойност is 39,380,680 BGN on the raw
    sheet; the parsed row should carry that converted to EUR (2 dp), with
    the original BGN figure preserved in extra_json."""
    totals = [
        r
        for r in capital_result_2021_bgn["rows"]
        if r["unit"] == "Общо" and r["object_code"] == "ОБЩО"
    ]
    row = totals[0]
    assert row["currency"] == "EUR"
    assert row["extra_json"]["original"]["estimated_total"] == pytest.approx(39380680)
    assert row["estimated_total"] == pytest.approx(eur(39380680))


def test_capital_ledger_2021_header_detected_by_label(capital_result_2021_bgn):
    """Sanity check that the label-based header locator (not a fixed row
    number) found real data: paragraph subtotal §51-00 should be present
    with a nonzero estimated_total."""
    rows = capital_result_2021_bgn["rows"]
    para = [
        r
        for r in rows
        if r["unit"] == "Общо" and r["paragraph"] == "5100" and r["extra_json"]["row_type"] == "paragraph_subtotal"
    ]
    assert len(para) == 1
    assert para[0]["estimated_total"] and para[0]["estimated_total"] > 0


# -- capital ledger: synthetic shifted-column workbook (layout variant) ------


def _write_shifted_capital_workbook(path: Path) -> None:
    """Build a minimal capital-ledger workbook whose core columns are
    shifted one column to the right of the 2026/2021 layout, and whose
    currency marker says "лева" instead of "EUR" -- a stand-in for an
    unseen older-year layout variant, to prove the header/column locator
    works off label text rather than fixed positions.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Общо"

    ws.cell(row=2, column=2, value="ОБЩИНА Несебър")
    ws.cell(row=3, column=3, value="план/отчет за периода:  2022 Март")
    ws.cell(row=4, column=2, value="Сумите са в лева")

    # Header row, shifted +1 column vs. the real-world layout.
    header_row = 6
    ws.cell(row=header_row, column=2, value="§")
    ws.cell(row=header_row, column=3, value="Информация за наименованието на обекта")
    ws.cell(row=header_row, column=4, value="Година начало - година край")
    ws.cell(row=header_row, column=5, value="Сметна стойност")
    ws.cell(row=header_row, column=6, value="Усвоено до края на предходната година")
    ws.cell(row=header_row, column=7, value="Уточнен план")
    ws.cell(row=header_row, column=8, value="Усвоено към отчетния период")

    ws.cell(row=header_row + 2, column=3, value="ОБЩО")
    ws.cell(row=header_row + 2, column=5, value=195583)  # = 100,000 EUR
    ws.cell(row=header_row + 2, column=6, value=0)
    ws.cell(row=header_row + 2, column=7, value=0)
    ws.cell(row=header_row + 2, column=8, value=0)

    ws.cell(row=header_row + 3, column=2, value="5100")
    ws.cell(row=header_row + 3, column=3, value="Основен ремонт на дълготрайни материални активи")
    ws.cell(row=header_row + 3, column=5, value=195583)
    ws.cell(row=header_row + 3, column=6, value=0)
    ws.cell(row=header_row + 3, column=7, value=0)
    ws.cell(row=header_row + 3, column=8, value=0)

    wb.save(path)


def test_capital_ledger_header_detection_handles_shifted_columns(tmp_path):
    path = tmp_path / "shifted_capital.xlsx"
    _write_shifted_capital_workbook(path)

    result = parse_budget_capital(path, period="2022-03")
    assert result["unsupported"] is None

    totals = [r for r in result["rows"] if r["object_code"] == "ОБЩО"]
    assert len(totals) == 1
    assert totals[0]["estimated_total"] == pytest.approx(100000)
    assert totals[0]["extra_json"]["original_currency"] == "BGN"

    para = [r for r in result["rows"] if r["paragraph"] == "5100"]
    assert len(para) == 1
    assert para[0]["estimated_total"] == pytest.approx(100000)


def test_capital_ledger_unsupported_when_no_header_label_found(tmp_path):
    """A workbook with no "Сметна стойност" label anywhere (e.g. a misfiled
    report of a different kind) should be classified as unsupported, not
    silently mis-parsed."""
    path = tmp_path / "not_a_capital_ledger.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.cell(row=1, column=1, value="this is some other kind of report")
    wb.save(path)

    result = parse_budget_capital(path, period="2022-03")
    assert result["rows"] == []
    assert result["unsupported"] is not None


# -- B1: 2026 EUR sample (original layout) ------------------------------------


def test_b1_expenditure_table_nonempty_and_numeric(b1_result):
    """The B1 parser should return a non-empty expenditure-by-paragraph
    table with numeric plan/actual values."""
    rows = b1_result["rows"]
    expenditure = [r for r in rows if r["section"] == "разходи"]
    assert len(expenditure) > 0

    numeric_rows = [r for r in expenditure if isinstance(r["actual_ytd"], float)]
    assert len(numeric_rows) > 0
    assert any(r["actual_ytd"] and r["actual_ytd"] > 0 for r in numeric_rows)

    salaries = [r for r in expenditure if r["paragraph"] == "100"]
    assert len(salaries) == 1
    assert salaries[0]["actual_ytd"] > 0


def test_b1_revenue_table_nonempty(b1_result):
    revenue = [r for r in b1_result["rows"] if r["section"] == "приходи"]
    assert len(revenue) > 0


def test_b1_documents_skipped_pages(b1_result):
    """The sheet has many more report pages than revenue+expenditure; the
    parser should say so rather than silently dropping them."""
    assert b1_result["skipped"]


def test_b1_eur_marker_no_original_block(b1_result):
    expenditure = [r for r in b1_result["rows"] if r["section"] == "разходи" and r["paragraph"] == "100"]
    extra = expenditure[0]["extra_json"]
    assert extra["original_currency"] == "EUR"
    assert "original" not in extra


# -- B1: 2020 BGN sample (currency marker + conversion) -----------------------


def test_b1_2020_bgn_marker_detected(b1_result_2020_bgn):
    expenditure = [r for r in b1_result_2020_bgn["rows"] if r["section"] == "разходи" and r["paragraph"] == "100"]
    assert len(expenditure) == 1
    extra = expenditure[0]["extra_json"]
    assert extra["original_currency"] == "BGN"
    assert extra["conversion_rate"] == pytest.approx(B1_BGN_PER_EUR)


def test_b1_2020_bgn_conversion_known_cell(b1_result_2020_bgn):
    """§100 (salaries) actual_total on the raw BGN sheet should convert to
    EUR (2 dp), with the raw BGN figure preserved in extra_json."""
    expenditure = [r for r in b1_result_2020_bgn["rows"] if r["section"] == "разходи" and r["paragraph"] == "100"]
    row = expenditure[0]
    raw_bgn = row["extra_json"]["original"]["actual_total"]
    assert raw_bgn and raw_bgn > 0
    assert row["actual_ytd"] == pytest.approx(eur(raw_bgn))
    assert row["currency"] == "EUR"


def test_b1_2020_period_is_pre2026(b1_result_2020_bgn):
    assert b1_result_2020_bgn["rows"][0]["period"] == "2020-01"


# -- B3: quarterly report accepted by the same B1 parser ---------------------


def test_b3_quarterly_parses_same_schema(b3_result_2019_bgn):
    """A B3 (quarterly) workbook shares B1's exact `OTCHET` schema and
    should be parsed by the same function with no special-casing."""
    rows = b3_result_2019_bgn["rows"]
    assert rows
    expenditure = [r for r in rows if r["section"] == "разходи" and r["paragraph"] == "100"]
    assert len(expenditure) == 1
    assert expenditure[0]["actual_ytd"] and expenditure[0]["actual_ytd"] > 0


def test_b3_quarterly_period_uses_quarter_end_not_year_start(b3_result_2019_bgn):
    """`_infer_period` must pick the quarter-end ("до") date, not the
    year-start ("от") date that a naive first-date-found scan would pick
    (Q1 2019's "от" is 2019-01-01, which would misreport as January)."""
    assert b3_result_2019_bgn["rows"][0]["period"] == "2019-03"


def test_b3_quarterly_bgn_currency_detected(b3_result_2019_bgn):
    expenditure = [r for r in b3_result_2019_bgn["rows"] if r["section"] == "разходи" and r["paragraph"] == "100"]
    assert expenditure[0]["extra_json"]["original_currency"] == "BGN"


def test_classify_filename_b3_maps_to_b1_kind():
    """B3/IB3 (quarterly) filenames share B1/IB1's schema and are classified
    into the same `kind` so they're routed through the same parser."""
    info = classify_filename("B3_2019_1_5206.xls")
    assert info["kind"] == "B1"
    assert (info["year"], info["month"]) == (2019, 1)

    info = classify_filename("IB3_2019_1_5206_K33.xls")
    assert info["kind"] == "IB1_K33"


# -- filename classification (unchanged behaviour) ----------------------------


@pytest.mark.parametrize(
    ("filename", "expected_kind", "expected_year", "expected_month"),
    [
        ("B1_2026_8_5206.xls", "B1", 2026, 8),
        ("IB1_2026_8_5206_DES.xls", "IB1_DES", 2026, 8),
        ("IB1_2026_8_5206_K33.xls", "IB1_K33", 2026, 8),
        ("august2026.xlsx", "capital_xlsx", 2026, 8),
        ("june26.xlsx", "capital_xlsx", 2026, 6),
    ],
)
def test_classify_filename(filename, expected_kind, expected_year, expected_month):
    info = classify_filename(filename)
    assert info["kind"] == expected_kind
    assert info["year"] == expected_year
    assert info["month"] == expected_month


def test_classify_filename_unknown_falls_back_to_other():
    info = classify_filename("regUV2019.pdf")
    assert info["kind"] == "other"
