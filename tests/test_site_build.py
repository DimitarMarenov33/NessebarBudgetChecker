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
