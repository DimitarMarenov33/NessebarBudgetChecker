"""Tests for the static site generator (`nessebar_budget.web.build`).

Builds the real site (from the real local DB) into a pytest tmp_path, then
checks the output shape: required pages exist, the contracts CSV export has
one row per distinct `eop` procurement, and -- since the site will be
published under a GitHub Pages sub-path -- no link or asset reference is
absolute (starts with "/"). The number-formatting filters are unit-tested
without the database.
"""

from __future__ import annotations

import csv
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Procurement
from nessebar_budget.web.build import (
    DEFAULT_DB_PATH,
    _make_env,
    build_site,
    fmt_eur,
    fmt_num,
    fmt_pct,
    severity_key,
    site_path,
)

requires_db = pytest.mark.skipif(
    not DEFAULT_DB_PATH.exists(), reason="data/nessebar.db not present"
)

HREF_SRC_RE = re.compile(r'(?:href|src)="(?P<url>[^"]*)"')


# ---------------------------------------------------------------------------
# Formatting filters (no database needed)
# ---------------------------------------------------------------------------


def test_num_filter_uses_comma_thousands_and_no_decimals() -> None:
    assert fmt_num(12347905.4) == "12,347,905"
    assert fmt_num(12347905.6) == "12,347,906"
    assert fmt_num(999) == "999"
    assert fmt_num(0) == "0"
    assert fmt_num(-1234567) == "-1,234,567"
    assert fmt_num(Decimal("1000000.49")) == "1,000,000"
    assert fmt_num(None) == "—"


def test_eur_filter_appends_euro_sign() -> None:
    assert fmt_eur(12347905) == "12,347,905 €"
    assert fmt_eur(Decimal("220000.00")) == "220,000 €"
    assert fmt_eur(0.4) == "0 €"
    assert fmt_eur(None) == "—"


def test_pct_filter_uses_dot_decimal() -> None:
    assert fmt_pct(0.784) == "78.4%"
    assert fmt_pct(1.0) == "100.0%"
    assert fmt_pct(12.5) == "1,250.0%"
    assert fmt_pct(-0.105) == "-10.5%"
    assert fmt_pct(0.5, decimals=0) == "50%"
    assert fmt_pct(None) == "—"


def test_filters_are_registered_in_jinja_env() -> None:
    env = _make_env()
    tpl = env.from_string("{{ a | num }} | {{ a | eur }} | {{ b | pct }} | {{ s | sevkey }}")
    assert tpl.render(a=12347905, b=0.784, s="critical") == "12,347,905 | 12,347,905 € | 78.4% | high"


def test_severity_and_path_helpers() -> None:
    assert severity_key("HIGH") == "high"
    assert severity_key("serious") == "warning"
    assert severity_key("good") == "info"
    assert severity_key(None) == "warning"
    assert site_path("../contracts/1.html") == "contracts/1.html"
    assert site_path("flags/index.html") == "flags/index.html"
    assert site_path(None) == ""


# ---------------------------------------------------------------------------
# Full build against the real database
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built_site(tmp_path_factory) -> Path:
    out_dir = tmp_path_factory.mktemp("site")
    build_site(out_dir, db_url=f"sqlite:///{DEFAULT_DB_PATH}")
    return out_dir


def _distinct_eop_contract_count() -> int:
    engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    with Session(engine) as session:
        return session.scalar(
            select(func.count(func.distinct(Procurement.source_id))).where(
                Procurement.source == "eop"
            )
        )


@requires_db
def test_required_pages_exist(built_site: Path) -> None:
    for rel in [
        "index.html",
        "contracts/index.html",
        "contractors/index.html",
        "budget/index.html",
        "cash/index.html",
        "flags/index.html",
        "methodology.html",
        "data/index.html",
        "data/contracts.csv",
        "data/contracts.json",
        "data/budget_line_items.csv",
        "data/cash_execution.csv",
        "data/flags.json",
        "data/meta.json",
        "map.html",
        "settlements/index.html",
        "data/map.json",
        "data/settlements.csv",
        "static/style.css",
        "static/app.js",
    ]:
        assert (built_site / rel).exists(), f"missing {rel}"


@requires_db
def test_contracts_csv_row_count_matches_distinct_eop_contracts(built_site: Path) -> None:
    csv_path = built_site / "data" / "contracts.csv"
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    expected = _distinct_eop_contract_count()
    assert expected > 0
    assert len(rows) == expected

    source_ids = {row["source_id"] for row in rows}
    assert len(source_ids) == len(rows), "contracts.csv has duplicate source_id rows"


@requires_db
def test_no_absolute_links_or_assets(built_site: Path) -> None:
    offenders = []
    for html_path in built_site.rglob("*.html"):
        html = html_path.read_text(encoding="utf-8")
        for match in HREF_SRC_RE.finditer(html):
            url = match.group("url")
            if url.startswith("/") and not url.startswith("//"):
                offenders.append((str(html_path.relative_to(built_site)), url))
    assert not offenders, f"absolute href/src found: {offenders[:10]}"


@requires_db
def test_build_is_idempotent(built_site: Path, tmp_path_factory) -> None:
    """Building twice into the same directory should succeed and produce the
    same set of files (no stale leftovers, no crash on re-run)."""
    out_dir = tmp_path_factory.mktemp("site_rebuild")
    build_site(out_dir, db_url=f"sqlite:///{DEFAULT_DB_PATH}")
    first = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    build_site(out_dir, db_url=f"sqlite:///{DEFAULT_DB_PATH}")
    second = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    assert first == second


@requires_db
def test_flags_page_renders_without_flag_schema_columns(built_site: Path) -> None:
    """flags/index.html must render even though the live `flags` table may not
    yet have the newer optional columns (subject_type, law_ref, ...)."""
    html = (built_site / "flags" / "index.html").read_text(encoding="utf-8")
    assert "Сигнали" in html


@requires_db
def test_flags_index_renders_tier_filter(built_site: Path) -> None:
    """flags/index.html must offer the tier segmented control (Всички /
    Нарушения / Сигнали / Непрозрачност) alongside the severity/rule filters,
    and show a tier chip on each row -- this must hold even when the live
    `flags` table doesn't have a `tier` column yet (tier_key()'s fallback)."""
    html = (built_site / "flags" / "index.html").read_text(encoding="utf-8")
    assert 'data-tier-filter=""' in html
    assert 'data-tier-filter="violation"' in html
    assert 'data-tier-filter="signal"' in html
    assert 'data-tier-filter="opacity"' in html
    assert "Нарушения" in html and "Сигнали" in html and "Непрозрачност" in html
    assert "data-flag-row" in html
    assert 'data-tier="' in html


@requires_db
def test_flags_index_renders_year_filter(built_site: Path) -> None:
    """flags/index.html must offer a "Година" (event year) filter alongside
    severity/rule, with options = distinct `event_year`s descending plus
    "Всички години", and each row must carry a matching `data-year` so the
    generic app.js filter (see `enableFlagFilters`) picks it up and combines
    it with the other filters exactly like severity/rule already do."""
    html = (built_site / "flags" / "index.html").read_text(encoding="utf-8")
    assert 'data-table-filter="year"' in html
    assert "Всички години" in html
    assert 'data-year="' in html

    option_years = [
        int(y)
        for y in re.findall(r'<option value="(\d{4})">\d{4}</option>', html)
    ]
    assert option_years, "no year <option>s rendered"
    assert option_years == sorted(option_years, reverse=True), "years must be descending"

    row_years = {
        int(y) for y in re.findall(r'data-year="(\d{4})"', html)
    }
    assert row_years, "no row carried a non-empty data-year"
    assert row_years == set(option_years), "select options must match the years actually on rows"


@requires_db
def test_flag_permalink_page_exists_for_a_flag_in_the_db(built_site: Path) -> None:
    """Every flag in the DB gets its own permalink page flags/<id>.html, and
    that page renders the explanation/documents/how-to-request sections."""
    engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    with Session(engine) as session:
        flag_id = session.execute(text("SELECT id FROM flags LIMIT 1")).scalar_one()

    flag_page = built_site / "flags" / f"{flag_id}.html"
    assert flag_page.exists(), f"missing flags/{flag_id}.html"
    html = flag_page.read_text(encoding="utf-8")
    assert "Документи за изискване" in html
    assert "Как да поискате документите" in html
    assert "ЗДОИ" in html


def _provenance_site(tmp_path: Path) -> tuple[Path, Session]:
    """A tiny DB (one BGN capital-ledger row, one EU-funded EOP contract),
    analysed and built -- independent of what the real DB's flags carry."""
    import datetime as dt

    from nessebar_budget.analysis.engine import run_full_analysis
    from nessebar_budget.db.budget_models import BudgetLineItem
    from nessebar_budget.db.models import Base, BudgetReport

    engine = create_engine(f"sqlite:///{tmp_path / 'tiny.db'}")
    Base.metadata.create_all(engine)
    session = Session(engine)
    report = BudgetReport(
        period="2022-12",
        kind="capital_xlsx",
        url="https://www.nesebar.bg/03-2019/Месечен отчет за 2022 Декември 5206 Несебър.xlsx",
        file_path="data/cache/nesebar_site/2022/12/Месечен отчет за 2022 Декември 5206 Несебър.xlsx",
    )
    session.add(report)
    session.flush()
    session.add(
        BudgetLineItem(
            report_id=report.id, period="2022-12", unit="Общо", paragraph="5100",
            object_name="Реконструкция на училище", plan_current=52344.02,
            spent_period=321082.61, spent_prior=0, estimated_total=52663.06, currency="EUR",
            extra_json={"row_type": "object", "original_currency": "BGN",
                        "conversion_rate": 1.95583, "source_sheet": "Общо", "source_row": 19},
        )
    )
    session.add(
        Procurement(
            source="eop", source_id="77", title="Доставка", procedure_type="NegotiatedProcedure",
            contractor_name="X EOOD", contractor_eik="111", contract_value_eur=600_000,
            contract_date=dt.datetime(2025, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None), url="https://app.eop.bg/today/595341",
            raw_json={
                "contract": {"ContractNumber": "77", "TenderNumber": "U77", "TypeOfContract": 1,
                             "ContractValue": 600000, "Currency": 1},
                "procedure": {"IsEUFinanced": True, "SpecialNumber": "U77"},
                "tender_detail": {},
            },
        )
    )
    session.commit()
    run_full_analysis(session, now=dt.datetime(2026, 3, 10, tzinfo=dt.UTC).replace(tzinfo=None))
    session.commit()
    out = tmp_path / "site"
    build_site(out, db_url=f"sqlite:///{tmp_path / 'tiny.db'}")
    return out, session


def test_flag_permalink_shows_sources_box(tmp_path: Path) -> None:
    """Every flag's permalink (and its row on flags/index.html) carries a
    closed-by-default "Източници" <details> linking the published file/record,
    with the sheet/row, the leva figures and the conversion note."""
    from nessebar_budget.db.models import Flag

    site, session = _provenance_site(tmp_path)
    all_flags = session.scalars(select(Flag)).all()
    flags = {f.rule: f for f in all_flags}
    session.close()

    budget_html = (site / "flags" / f"{flags['unplanned_spending'].id}.html").read_text("utf-8")
    assert "Източници" in budget_html
    assert '<details class="sources">' in budget_html  # closed by default
    hrefs = [m.group("url") for m in HREF_SRC_RE.finditer(budget_html)]
    assert any(h.startswith("https://www.nesebar.bg/") for h in hrefs)
    # Cyrillic/space file names are percent-encoded in the href.
    assert not any(" " in h for h in hrefs)
    assert "лист „Общо“, ред 19" in budget_html
    assert "102,376 лв." in budget_html and "627,983 лв." in budget_html
    assert "1 € = 1,95583 лв." in budget_html
    assert "Как да проверите" in budget_html
    # (a) object name bold, before the sheet/row line, with the row number --
    # the beta-tester fix: the flag's numbers are on the *object* row, not
    # the paragraph-subtotal row a reader would otherwise land on.
    objname_pos = budget_html.find("source__objname")
    loc_pos = budget_html.find("лист „Общо“, ред 19")
    assert -1 < objname_pos < loc_pos
    assert "Реконструкция на училище" in budget_html
    assert "търсете този ред по името на обекта (ред 19)" in budget_html
    # (b) the "Параграф" item in the numbers grid gets a short tooltip note.
    assert 'title="категория (§), не редът на обекта"' in budget_html
    # (c) "Как да проверите" is now 3 short numbered steps for a budget file.
    assert "sources__how-steps" in budget_html
    assert "Excel/LibreOffice" in budget_html
    assert "Ctrl+F" in budget_html
    assert "Усвоено към отчетния период" in budget_html

    contract_html = (site / "flags" / f"{flags['exceptional_procedure'].id}.html").read_text("utf-8")
    assert 'href="https://app.eop.bg/today/595341"' in contract_html
    # Contract flags get ЦАИС ЕОП-flavoured steps instead of the budget ones.
    assert "ЦАИС ЕОП" in contract_html
    assert "sources__how-steps" in contract_html
    assert "Ctrl+F" not in contract_html

    meta_html = (site / "flags" / f"{flags['eu_funded_irregularity'].id}.html").read_text("utf-8")
    assert f'href="../flags/{flags["exceptional_procedure"].id}.html"' in meta_html

    index_html = (site / "flags" / "index.html").read_text("utf-8")
    assert index_html.count('<details class="sources">') == len(all_flags)

    exported = json.loads((site / "data" / "flags.json").read_text("utf-8"))
    assert all(f["sources"] for f in exported)

    # event_year: the budget object's own ledger period (2022-12 -> 2022),
    # vs. the matched contract's year (contract_date 2025-01-01 -> 2025) for
    # both a direct contract flag and the EU meta-flag over the same contract.
    by_rule = {f["rule"]: f for f in exported}
    assert "event_year" in by_rule["unplanned_spending"]
    assert by_rule["unplanned_spending"]["event_year"] == 2022
    assert by_rule["exceptional_procedure"]["event_year"] == 2025
    assert by_rule["eu_funded_irregularity"]["event_year"] == 2025


#: An amount grouped with spaces / thin spaces (the pre-redesign format),
#: e.g. "220 000 €" -- every rendered number must use commas now.
SPACE_GROUPED_RE = re.compile(r"\d[ \u2009\u202f\u00a0]\d{3}(?:\D|$)")


@requires_db
def test_rendered_amounts_use_comma_grouping(built_site: Path) -> None:
    offenders = []
    for rel in ["index.html", "contracts/index.html", "budget/index.html", "cash/index.html",
                "contractors/index.html", "data/index.html", "methodology.html"]:
        html = (built_site / rel).read_text(encoding="utf-8")
        for match in re.finditer(r"[\d,]+ €", html):
            if not re.fullmatch(r"\d{1,3}(?:,\d{3})* €", match.group(0)):
                offenders.append((rel, match.group(0)))
        if SPACE_GROUPED_RE.search(re.sub(r"<[^>]+>", "|", html)):
            offenders.append((rel, SPACE_GROUPED_RE.search(re.sub(r"<[^>]+>", "|", html)).group(0)))
    assert not offenders, f"badly grouped numbers: {offenders[:10]}"


@requires_db
def test_home_page_is_compact(built_site: Path) -> None:
    html = (built_site / "index.html").read_text(encoding="utf-8")
    assert html.count('class="issue"') <= 5
    assert html.count('class="stat"') == 4
    assert "Къде отиват парите" in html
    assert "fonts.googleapis.com/css2?family=Inter" in html
    assert 'href="static/style.css"' in html and 'src="static/app.js"' in html


@requires_db
def test_detail_and_period_pages_exist(built_site: Path) -> None:
    assert any((built_site / "contracts").glob("*.html"))
    assert len(list((built_site / "contracts").glob("*.html"))) > 1
    assert len(list((built_site / "contractors").glob("*.html"))) > 1
    periods = [p for p in (built_site / "budget").glob("*.html") if p.name != "index.html"]
    assert periods, "no budget/<period>.html pages"


@requires_db
def test_contractor_page_shows_trade_register_card(built_site: Path) -> None:
    """The contractor page for ЕИК 102981058 (ЕЛЕКТРИКАЛ ГРУП, managed by
    Йордан Пламенов Момчев per the real `companies`/`company_people` data)
    must carry a "Търговски регистър" card with the company's name and its
    current manager."""
    page = built_site / "contractors" / "102981058.html"
    assert page.exists(), "missing contractors/102981058.html"
    html = page.read_text(encoding="utf-8")
    assert "Търговски регистър" in html
    assert "ЕЛЕКТРИКАЛ ГРУП" in html
    assert "ЙОРДАН ПЛАМЕНОВ МОМЧЕВ" in html


def _companies_table_count() -> int:
    from nessebar_budget.db.models import Company

    engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(Company))


@requires_db
def test_companies_csv_row_count_matches_companies_table(built_site: Path) -> None:
    csv_path = built_site / "data" / "companies.csv"
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    expected = _companies_table_count()
    assert expected > 0
    assert len(rows) == expected


@requires_db
def test_settlement_detail_pages_exist_for_every_settlement(built_site: Path) -> None:
    from nessebar_budget.web.geo import SETTLEMENTS

    for s in SETTLEMENTS:
        page = built_site / "settlements" / f"{s['key']}.html"
        assert page.exists(), f"missing settlements/{s['key']}.html"


@requires_db
def test_map_json_is_valid_geojson_and_reconciles_with_budget_totals(built_site: Path) -> None:
    """data/map.json is a FeatureCollection of Point features, one per
    settlement, and -- for a year with a clean December snapshot -- the sum
    of every settlement's `spent` for that year plus `unassigned`'s `spent`
    for that year equals the budget page's own grand-total spent for that
    year's period (built from the very same object rows, just partitioned
    by settlement instead of by function -- see geo.build_map_data)."""
    from nessebar_budget.web.build import _load_budget

    data = json.loads((built_site / "data" / "map.json").read_text(encoding="utf-8"))
    assert data["type"] == "FeatureCollection"
    assert len(data["features"]) == 16  # 14 settlements + 2 resort areas

    for feature in data["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Point"
        lon, lat = feature["geometry"]["coordinates"]
        assert isinstance(lon, (int, float)) and isinstance(lat, (int, float))
        props = feature["properties"]
        assert {"key", "name", "kind", "href", "all_years", "years", "contracts_count", "flags_count"} <= props.keys()

    engine = create_engine(f"sqlite:///{DEFAULT_DB_PATH}")
    with Session(engine) as session:
        budget = _load_budget(session)

    # Pick a year whose representative period is an exact "<year>-12" report
    # (no fallback-month note), so the comparison is against a clean,
    # unambiguous full-year snapshot.
    candidate_years = [y for y in range(2021, 2027) if f"{y}-12" in budget["periods"]]
    assert candidate_years, "no year with a December budget report to check against"
    checked_year = candidate_years[-1]
    period = f"{checked_year}-12"

    grand_spent = budget["by_period"][period]["grand_total"]["spent_period"]

    settlements_spent = sum(
        f["properties"]["years"].get(str(checked_year), {}).get("spent", 0.0)
        for f in data["features"]
    )
    unassigned_spent = data["unassigned"]["years"].get(str(checked_year), {}).get("spent", 0.0)

    assert abs((settlements_spent + unassigned_spent) - grand_spent) < 1.0, (
        f"{period}: settlements ({settlements_spent}) + unassigned ({unassigned_spent}) "
        f"!= budget grand total ({grand_spent})"
    )


# ---------------------------------------------------------------------------
# Mobile layout (390x844, dark) — no page may scroll sideways.
# ---------------------------------------------------------------------------

try:
    from playwright.sync_api import sync_playwright

    _HAS_PLAYWRIGHT = True
except ImportError:  # pragma: no cover - environment without the optional dep
    _HAS_PLAYWRIGHT = False

requires_playwright = pytest.mark.skipif(
    not _HAS_PLAYWRIGHT, reason="playwright not installed"
)


def _first(paths) -> Path | None:
    for p in sorted(paths):
        if p.name != "index.html":
            return p
    return None


@requires_db
@requires_playwright
def test_pages_do_not_scroll_horizontally_on_phone(built_site: Path) -> None:
    """Every page must fit a 390px-wide viewport with no horizontal scroll,
    and must declare a mobile-friendly viewport."""
    contract_page = _first((built_site / "contracts").glob("*.html"))
    contractor_page = _first((built_site / "contractors").glob("*.html"))
    budget_period_page = _first((built_site / "budget").glob("*.html"))
    flag_page = _first((built_site / "flags").glob("*.html"))
    assert contract_page and contractor_page and budget_period_page and flag_page

    pages = [
        "index.html",
        "contracts/index.html",
        "contractors/index.html",
        contractor_page.relative_to(built_site).as_posix(),
        "budget/index.html",
        budget_period_page.relative_to(built_site).as_posix(),
        "cash/index.html",
        "flags/index.html",
        contract_page.relative_to(built_site).as_posix(),
        flag_page.relative_to(built_site).as_posix(),
        "methodology.html",
    ]

    offenders = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark")
        page = context.new_page()
        for rel in pages:
            page.goto((built_site / rel).as_uri(), wait_until="networkidle")

            viewport_content = page.get_attribute("meta[name='viewport']", "content")
            assert viewport_content and "width=device-width" in viewport_content, (
                f"{rel}: missing/incorrect viewport meta tag"
            )

            scroll_width = page.evaluate("document.documentElement.scrollWidth")
            client_width = page.evaluate("document.documentElement.clientWidth")
            if scroll_width > client_width:
                offenders.append((rel, scroll_width, client_width))
        browser.close()

    assert not offenders, f"pages scrolling sideways at 390px: {offenders}"


@requires_db
@requires_playwright
def test_text_size_adjust_is_set(built_site: Path) -> None:
    """iOS Safari must not be allowed to auto-resize text (it would make the
    stacked phone tables reflow oddly after a user pinch-zoom elsewhere)."""
    css = (built_site / "static" / "style.css").read_text(encoding="utf-8")
    assert "-webkit-text-size-adjust: 100%" in css
