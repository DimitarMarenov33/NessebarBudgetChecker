"""Tests for the budget-execution parsers, using the two real sample files
in `data/samples/` as fixtures (August 2026 capital ledger + B1 cash report).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nessebar_budget.parsers.budget_b1 import parse_budget_b1
from nessebar_budget.parsers.budget_capital import parse_budget_capital
from nessebar_budget.scrapers.nesebar_site import classify_filename

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
CAPITAL_XLSX = SAMPLES / "nesebar_budget_execution_aug2026.xlsx"
B1_XLS = SAMPLES / "nesebar_cash_execution_B1_2026_8.xls"


@pytest.fixture(scope="module")
def capital_result():
    return parse_budget_capital(CAPITAL_XLSX)


@pytest.fixture(scope="module")
def b1_result():
    return parse_budget_b1(B1_XLS)


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
