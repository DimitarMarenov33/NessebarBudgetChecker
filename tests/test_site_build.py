"""Tests for the static site generator (`nessebar_budget.web.build`).

Builds the real site (from the real local DB) into a pytest tmp_path, then
checks the output shape: required pages exist, the contracts CSV export has
one row per distinct `eop` procurement, and -- since the site will be
published under a GitHub Pages sub-path -- no link or asset reference is
absolute (starts with "/").
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Procurement
from nessebar_budget.web.build import DEFAULT_DB_PATH, build_site

pytestmark = pytest.mark.skipif(
    not DEFAULT_DB_PATH.exists(), reason="data/nessebar.db not present"
)

HREF_SRC_RE = re.compile(r'(?:href|src)="(?P<url>[^"]*)"')


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


def test_contracts_csv_row_count_matches_distinct_eop_contracts(built_site: Path) -> None:
    csv_path = built_site / "data" / "contracts.csv"
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    expected = _distinct_eop_contract_count()
    assert expected > 0
    assert len(rows) == expected

    source_ids = {row["source_id"] for row in rows}
    assert len(source_ids) == len(rows), "contracts.csv has duplicate source_id rows"


def test_no_absolute_links_or_assets(built_site: Path) -> None:
    offenders = []
    for html_path in built_site.rglob("*.html"):
        html = html_path.read_text(encoding="utf-8")
        for match in HREF_SRC_RE.finditer(html):
            url = match.group("url")
            if url.startswith("/") and not url.startswith("//"):
                offenders.append((str(html_path.relative_to(built_site)), url))
    assert not offenders, f"absolute href/src found: {offenders[:10]}"


def test_build_is_idempotent(built_site: Path, tmp_path_factory) -> None:
    """Building twice into the same directory should succeed and produce the
    same set of files (no stale leftovers, no crash on re-run)."""
    out_dir = tmp_path_factory.mktemp("site_rebuild")
    build_site(out_dir, db_url=f"sqlite:///{DEFAULT_DB_PATH}")
    first = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    build_site(out_dir, db_url=f"sqlite:///{DEFAULT_DB_PATH}")
    second = sorted(p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file())
    assert first == second


def test_flags_page_renders_without_flag_schema_columns(built_site: Path) -> None:
    """flags/index.html must render even though the live `flags` table may not
    yet have the newer optional columns (subject_type, law_ref, ...)."""
    html = (built_site / "flags" / "index.html").read_text(encoding="utf-8")
    assert "Сигнали" in html
