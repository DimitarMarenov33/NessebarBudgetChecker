"""Unit tests for the EOP and SIGMA scrapers.

All HTTP calls are replaced with an in-process ``httpx.MockTransport`` fed from
the real captured samples in ``data/samples/`` -- no network access happens in
these tests. Each test points `DATA_DIR` at a pytest `tmp_path` so scraper
caches never touch the real `data/cache/` directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Base, Procurement
from nessebar_budget.db.repo import upsert_procurements
from nessebar_budget.scrapers.eop import EopScraper
from nessebar_budget.scrapers.sigma import SigmaScraper

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "data" / "samples"


def _load(name: str) -> Any:
    return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def eop_scraper(tmp_path, monkeypatch) -> EopScraper:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    org = _load("eop_org_nesebar.json")
    procedures_page1 = _load("eop_procedures_nesebar_page1.json")
    contracts_page1 = _load("eop_contracts_nesebar_page1.json")
    tender_detail_595341 = _load("eop_procedure_detail_595341.json")
    contract_items_595341 = _load("eop_contract_detail_595341.json")

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")

        if method == "GetContractingAuthoritySearchResult":
            return httpx.Response(200, json=org)

        if method == "GetProcurementsByOrganization":
            start = body["request"]["StartIndex"]
            if start == 1:
                return httpx.Response(200, json=procedures_page1)
            return httpx.Response(
                200,
                json={"CurrentPageResults": [], "ResultsCount": 410, "HasMoreResults": False},
            )

        if method == "GetContractsByOrganization":
            start = body["request"]["StartIndex"]
            if start == 1:
                return httpx.Response(200, json=contracts_page1)
            return httpx.Response(
                200,
                json={"CurrentPageResults": [], "ResultsCount": 413, "HasMoreResults": False},
            )

        if method == "GetPublishedTenderDetails":
            tender_id = body["tenderId"]
            if tender_id == 595341:
                return httpx.Response(200, json=tender_detail_595341)
            # Other tender ids in the fixture pages have no saved detail sample;
            # a minimal-but-real-shaped empty detail is a valid stand-in here
            # (test double), not fabricated production data.
            return httpx.Response(
                200, json={"TenderId": tender_id, "TenderName": None, "PublicationDate": None}
            )

        if method == "GetPublishedContractListItems":
            tender_id = body["tenderId"]
            if tender_id == 595341:
                return httpx.Response(200, json=contract_items_595341)
            return httpx.Response(200, json={"ContractListItems": [], "Lots": []})

        return httpx.Response(404, json={"error": f"unhandled method {method}"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://service.eop.bg/NX1Service.svc",
    )
    # delay=0: these are mocked, in-process calls -- the politeness delay only
    # matters against the real, live service.eop.bg.
    scraper = EopScraper(client=client, delay=0.0)
    yield scraper
    scraper.close()


@pytest.fixture
def sigma_scraper(tmp_path, monkeypatch) -> SigmaScraper:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    csv_bytes = (SAMPLES_DIR / "sigma_contracts_nesebar.csv").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/contracts.csv"
        assert request.url.params.get("authority") == "000057122"
        return httpx.Response(200, content=csv_bytes)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    scraper = SigmaScraper(client=client, delay=0.0)
    yield scraper
    scraper.close()


# ---------------------------------------------------------------------------
# EOP
# ---------------------------------------------------------------------------


def test_eop_resolves_nesebar_organization(eop_scraper: EopScraper) -> None:
    org_id, org_guid = eop_scraper._resolve_organization()
    assert org_id == 28046
    assert org_guid == "6d677995-126f-4bc1-bdec-4ba334fb6026"


def test_eop_fetch_normalizes_contracts_and_open_procedures(eop_scraper: EopScraper) -> None:
    records = eop_scraper.fetch()

    # 10 sample contracts + 10 sample procedures that have no matching contract
    # in this (disjoint-by-construction) fixture pair.
    assert len(records) == 20
    assert all(r["source"] == "eop" for r in records)

    by_id = {r["source_id"]: r for r in records}
    contract_266822 = by_id["266822"]
    assert contract_266822["contract_value_eur"] == 220000.0
    assert contract_266822["contract_value_bgn"] is None
    assert contract_266822["currency"] == "EUR"
    assert contract_266822["contractor_name"] == "СТАНДАРТ – ИНВЕСТ – ГРУП ЕООД"
    assert contract_266822["contractor_eik"] == "201800812"
    assert contract_266822["url"] == "https://app.eop.bg/today/595341"
    # PublicationDate "/Date(1783908619653)/" from the real detail sample.
    assert contract_266822["published_at"].year == 2026

    # A procedure with no signed contract yet should carry an estimate but no
    # contract value -- this is exactly the "missing contract value" case the
    # project's analysis rules are meant to flag.
    open_tender = by_id["605199"]
    assert open_tender["contract_value_eur"] is None
    assert open_tender["contract_value_bgn"] is None
    assert open_tender["estimated_value_eur"] == 127500.0
    assert open_tender["estimated_value_bgn"] is None


def test_eop_currency_code_1_means_eur(eop_scraper: EopScraper) -> None:
    """Every sampled EOP row ties Currency==1 to ContractValue==ContractValueEuro,
    which is only consistent with code 1 meaning EUR (see eop.py module docstring)."""
    contracts = _load("eop_contracts_nesebar_page1.json")["CurrentPageResults"]
    assert all(c["Currency"] == 1 for c in contracts)
    assert all(c["ContractValue"] == c["ContractValueEuro"] for c in contracts)


def test_eop_caches_raw_responses(eop_scraper: EopScraper, tmp_path: Path) -> None:
    eop_scraper.fetch()
    cache_dir = tmp_path / "cache" / "eop"
    cached_files = list(cache_dir.glob("*.json"))
    assert cached_files, "expected at least one cached raw EOP response"
    assert any(f.name.startswith("org_search_") for f in cached_files)
    assert any(f.name.startswith("tender_") for f in cached_files)


def test_eop_net_date_parsing() -> None:
    from nessebar_budget.scrapers.eop import _parse_net_date

    assert _parse_net_date(None) is None
    parsed = _parse_net_date("/Date(1789678800000+0300)/")
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.tzinfo is None  # naive, stored as UTC


# ---------------------------------------------------------------------------
# SIGMA
# ---------------------------------------------------------------------------


def test_sigma_fetch_normalizes_csv_rows(sigma_scraper: SigmaScraper) -> None:
    records = sigma_scraper.fetch()
    assert len(records) == 411
    assert all(r["source"] == "sigma" for r in records)

    first = records[0]
    assert first["source_id"] == "e:00126-2020-0023:348:2:eik:102811636:1"
    assert "Публикации" in first["title"]
    assert first["contract_value_eur"] == pytest.approx(10225.83762392437)
    assert first["contractor_name"] == "ЕТ МАРАМ - МАРА МОМЧИЛОВА"
    assert first["contractor_eik"] == "102811636"
    assert first["bids_received"] == 1
    assert first["contract_date"].isoformat().startswith("2020-10-22")
    assert first["raw_json"]["unp"] == "00126-2020-0023"
    assert first["raw_json"]["eu_funded"] is False
    assert first["raw_json"]["bids_received"] == 1


def test_sigma_handles_missing_contractor_eik(sigma_scraper: SigmaScraper) -> None:
    records = sigma_scraper.fetch()
    with_null_eik = [r for r in records if r["contractor_eik"] is None]
    # The sample CSV has 7 rows with a blank contractor_eik (per `df.isnull().sum()`
    # on data/samples/sigma_contracts_nesebar.csv).
    assert len(with_null_eik) == 7


def test_sigma_caches_csv(sigma_scraper: SigmaScraper, tmp_path: Path) -> None:
    sigma_scraper.fetch()
    cache_file = tmp_path / "cache" / "sigma" / "contracts_000057122.csv"
    assert cache_file.exists()


# ---------------------------------------------------------------------------
# Upsert idempotency
# ---------------------------------------------------------------------------


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_upsert_is_idempotent(db_session: Session, eop_scraper: EopScraper) -> None:
    records = eop_scraper.fetch()

    inserted1, updated1 = upsert_procurements(db_session, records)
    db_session.commit()
    assert inserted1 == len(records)
    assert updated1 == 0

    total_after_first = db_session.scalar(select(func.count()).select_from(Procurement))
    assert total_after_first == len(records)

    # Re-running with the exact same normalized records should insert nothing
    # new and report nothing as "changed".
    inserted2, updated2 = upsert_procurements(db_session, records)
    db_session.commit()
    assert inserted2 == 0
    assert updated2 == 0

    total_after_second = db_session.scalar(select(func.count()).select_from(Procurement))
    assert total_after_second == total_after_first


def test_upsert_updates_changed_field_and_preserves_created_at(db_session: Session) -> None:
    record = {
        "source": "eop",
        "source_id": "123",
        "title": "Original title",
        "contract_value_eur": 1000.0,
    }
    upsert_procurements(db_session, [record])
    db_session.commit()

    row = db_session.scalars(
        select(Procurement).where(Procurement.source_id == "123")
    ).one()
    created_at_before = row.created_at

    updated_record = dict(record, title="Changed title")
    inserted, updated = upsert_procurements(db_session, [updated_record])
    db_session.commit()
    assert inserted == 0
    assert updated == 1

    db_session.refresh(row)
    assert row.title == "Changed title"
    assert row.created_at == created_at_before


def test_upsert_combines_eop_and_sigma_records_without_collision(
    db_session: Session, eop_scraper: EopScraper, sigma_scraper: SigmaScraper
) -> None:
    eop_records = eop_scraper.fetch()
    sigma_records = sigma_scraper.fetch()

    upsert_procurements(db_session, eop_records)
    upsert_procurements(db_session, sigma_records)
    db_session.commit()

    total = db_session.scalar(select(func.count()).select_from(Procurement))
    assert total == len(eop_records) + len(sigma_records)
