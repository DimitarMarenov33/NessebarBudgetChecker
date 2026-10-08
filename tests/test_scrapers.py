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


def test_eop_fetch_sets_cpv_code_and_notice_text_from_tender_detail(
    eop_scraper: EopScraper,
) -> None:
    """The mocked `GetPublishedTenderDetails` response for tender 595341 (see
    the `eop_scraper` fixture) is a *real* captured payload with a full
    `TenderPublicationDetails[].HtmlPreview` -- confirming `cpv_code` and
    `raw_json.tender_detail.notice_text` get populated end-to-end via
    `EopScraper.fetch()`, not just via `extract_notice` in isolation.
    """
    records = eop_scraper.fetch()
    by_id = {r["source_id"]: r for r in records}

    contract_266822 = by_id["266822"]
    assert contract_266822["cpv_code"] == "33700000"
    tender_detail = contract_266822["raw_json"]["tender_detail"]
    assert "notices" in tender_detail
    assert tender_detail["notice_text"]
    assert "спомагателно-хигиенни материали" in tender_detail["notice_text"]
    # full_text must never land in the DB-bound raw_json (see `_build_notices`).
    assert all("full_text" not in n for n in tender_detail["notices"])
    assert all("HtmlPreview" not in n for n in tender_detail["notices"])

    # Tender ids with no real cached detail (the fixture's minimal stand-in,
    # see the `eop_scraper` handler) have no publications to parse -- this
    # must degrade to no cpv_code/notices, not raise.
    open_tender = by_id["605199"]
    assert open_tender["cpv_code"] is None
    assert "notices" not in (open_tender["raw_json"].get("tender_detail") or {})


# ---------------------------------------------------------------------------
# EOP notice-HTML parsing (`extract_notice` / `_build_notices`)
# ---------------------------------------------------------------------------


def test_extract_notice_parses_real_legacy_multilot_sample() -> None:
    """`data/samples/eop_notice_sample.html` is a trimmed-down *real*
    `HtmlPreview` (legacy/ЗОП form, from a real Несебър supervision-services
    procedure) -- kept small (see docs/sources/EOP_API.md) but with its
    buyer section, CPV, short description, and one full multi-paragraph lot
    description intact.
    """
    from nessebar_budget.scrapers.eop import extract_notice

    html = (SAMPLES_DIR / "eop_notice_sample.html").read_text(encoding="utf-8")
    parsed = extract_notice(html)

    assert parsed["notice_type"] == "Обявление за поръчка"
    assert parsed["cpv_main"] == "71521000"
    assert parsed["cpv_codes"] == ["71521000", "71520000"]
    assert parsed["short_description"] is not None
    assert parsed["short_description"].startswith("ОП №1")
    # The real lot description spans many sibling `div.name` paragraphs in
    # the source HTML -- naively taking only the first would truncate it.
    assert len(parsed["lot_descriptions"]) == 1
    lot_description = parsed["lot_descriptions"][0]
    assert lot_description.startswith("Упражняване на строителен надзор")
    assert len(lot_description) > 1000
    assert parsed["estimated_value"] == "72450 BGN"
    assert 0 < len(parsed["full_text"]) <= 30_000
    assert "Несебър" in parsed["full_text"]


def test_extract_notice_parses_eforms_fields_by_label() -> None:
    """The newer EU eForms notice generation (`PublicationFormType` 54/65/...)
    reuses the same row/div markup but folds the field code into the label
    text itself (e.g. ``Описание(BT-24-Procedure)``) and never writes the
    literal string "CPV" anywhere -- the CPV field is instead labeled
    "Основна класификация(BT-262-...)" with a "<code> - <description>" value.
    This is a hand-written but structurally faithful minimal eForms snippet
    (see docs/sources/EOP_API.md for the real-sample fields it mirrors).
    """
    from nessebar_budget.scrapers.eop import extract_notice

    html = """
    <html><body>
    <div style="text-align:right">
        <p class="header__form">Обявление за поръчка &#8211; Общата директива, стандартен режим</p>
    </div>
    <table class="maintable">
        <tr><td class="first__coll"></td><td><div class="section__name">Описание</div></td></tr>
        <tr><td style="width: 70px;"></td><td class="td__border">
            <div class="label__name">Описание(BT-24-Procedure)</div>
            <div class="name">В предмета на поръчката са включени следните дейности: доставка на гуми.</div>
        </td></tr>
        <tr><td class="first__coll"></td><td><div class="section__name">Основна класификация</div></td></tr>
        <tr><td style="width: 70px;"></td><td class="td__border">
            <div class="label__name">Основна класификация(BT-262-Procedure)</div>
            <div class="name">34350000 - Външни гуми с лек и тежък режим на експлоатация</div>
        </td></tr>
        <tr><td class="first__coll"></td><td><div class="section__name">Обхват на поръчката</div></td></tr>
        <tr><td style="width: 70px;"></td><td class="td__border">
            <div class="label__name">Прогнозна стойност, без да се включва ДДС(BT-27-Procedure)</div>
            <div class="name">650000.00 BGN</div>
        </td></tr>
    </table>
    </body></html>
    """
    parsed = extract_notice(html)

    assert parsed["notice_type"] == "Обявление за поръчка – Общата директива, стандартен режим"
    assert parsed["cpv_main"] == "34350000"
    assert parsed["cpv_codes"] == ["34350000"]
    assert parsed["short_description"] == (
        "В предмета на поръчката са включени следните дейности: доставка на гуми."
    )
    assert parsed["lot_descriptions"] == []
    assert parsed["estimated_value"] == "650000.00 BGN"


def test_extract_notice_joins_multiparagraph_value_without_truncation() -> None:
    from nessebar_budget.scrapers.eop import extract_notice

    html = """
    <table class="maintable">
        <tr><td class="first__coll">II.2.4)</td>
            <td><div class="section__name">Описание на обществената поръчка</div></td></tr>
        <tr><td style="width: 70px;"></td><td class="pdffirst__row td__border">
            <div class="name">Първи параграф.</div>
            <div class="name">Втори параграф.</div>
        </td></tr>
    </table>
    """
    parsed = extract_notice(html)
    assert parsed["lot_descriptions"] == ["Първи параграф. Втори параграф."]


def test_extract_notice_degrades_gracefully_on_empty_or_unrecognized_html() -> None:
    from nessebar_budget.scrapers.eop import extract_notice

    for html in ("", "<html><body>not a notice</body></html>"):
        parsed = extract_notice(html)
        assert parsed["notice_type"] is None
        assert parsed["cpv_main"] is None
        assert parsed["cpv_codes"] == []
        assert parsed["short_description"] is None
        assert parsed["lot_descriptions"] == []
        assert parsed["estimated_value"] is None


def test_build_notices_strips_full_text_and_dicts_but_keeps_scalars() -> None:
    from nessebar_budget.scrapers.eop import _build_notices, _tender_detail_with_notices

    html = (SAMPLES_DIR / "eop_notice_sample.html").read_text(encoding="utf-8")
    detail = {
        "TenderName": "СМР на обект",
        "TenderPublicationDetails": [
            {
                "PublicationFormType": 2,
                "TenderPublicationId": 426210,
                # A nested dict/list, like the real
                # `TenderPublicationAuthorityResultCollection` -- must be
                # dropped from the notice entry, same as `_slim`.
                "SomeNestedThing": {"a": 1},
                "HtmlPreview": html,
            }
        ],
    }

    notices, notice_text = _build_notices(detail)
    assert len(notices) == 1
    notice = notices[0]
    assert notice["TenderPublicationId"] == 426210
    assert "HtmlPreview" not in notice
    assert "full_text" not in notice
    assert "SomeNestedThing" not in notice
    assert notice["cpv_main"] == "71521000"
    assert notice_text  # non-empty: short_description + lot_descriptions
    assert len(notice_text) <= 8_000

    slim_detail, cpv_code = _tender_detail_with_notices(detail)
    assert cpv_code == "71521000"
    assert slim_detail["TenderName"] == "СМР на обект"
    assert slim_detail["notices"] == notices
    assert slim_detail["notice_text"] == notice_text


def test_tender_detail_with_notices_handles_missing_or_empty_detail() -> None:
    from nessebar_budget.scrapers.eop import _tender_detail_with_notices

    assert _tender_detail_with_notices(None) == (None, None)
    assert _tender_detail_with_notices({}) == ({}, None)


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


# -- nesebar_site: kinds and downloads -----------------------------------------

_NESEBAR_REPORTS_HTML = """
<html><body>
<h4>Отчети за касово изпълнение на бюджета към 31.01.2019 г.</h4>
<a href="/03-2019/B1_2019_1_5206.xls">B1</a>
<a href="/03-2019/B3_2018_4_5206.xls">B3</a>
<a href="/03-2019/IB3_2018_4_5206_DES.xls">IB3</a>
<h4>Отчети за касово изпълнение на бюджета към 29.02.2024 г.</h4>
<a href="/03-2019/Budget_2018_5206.xlsx">annual budget</a>
<a href="/03-2019/feb2024.xlsx">capital ledger</a>
<a href="/03-2019/regUV2019.pdf">scan</a>
</body></html>
"""


def test_nesebar_site_classifies_b3_and_non_ledger_but_downloads_them(tmp_path) -> None:
    from nessebar_budget.scrapers.nesebar_site import NesebarSiteScraper

    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path.endswith("reports.html"):
            return httpx.Response(200, text=_NESEBAR_REPORTS_HTML)
        return httpx.Response(200, content=b"file-bytes")

    scraper = NesebarSiteScraper(
        cache_dir=tmp_path, delay=0.0, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    try:
        records = {r["file_path"].rsplit("/", 1)[-1]: r for r in scraper.run()}
    finally:
        scraper.close()

    assert records["B3_2018_4_5206.xls"]["kind"] == "B3"
    assert records["IB3_2018_4_5206_DES.xls"]["kind"] == "IB3_DES"
    assert records["B1_2019_1_5206.xls"]["kind"] == "B1"
    assert records["feb2024.xlsx"]["kind"] == "capital_xlsx"
    # Non-ledger spreadsheet: "other", but still fetched and catalogued.
    assert records["Budget_2018_5206.xlsx"]["kind"] == "other"
    assert (tmp_path / "2024" / "02" / "Budget_2018_5206.xlsx").exists()
    # Period at download time stays the heading month (only a guess now).
    assert records["B3_2018_4_5206.xls"]["period"] == "2019-01"
    # PDFs are still not downloaded.
    assert "regUV2019.pdf" not in records
    assert not any(path.endswith(".pdf") for path in requested)


def test_nesebar_site_picks_up_late_reports_for_older_months(tmp_path) -> None:
    """The weekly run only looks at recent months, but a file whose URL was
    never seen before is downloaded whatever month it covers -- so a report
    the municipality posts late still reaches the database."""
    from nessebar_budget.scrapers.nesebar_site import NesebarSiteScraper

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("reports.html"):
            return httpx.Response(200, text=_NESEBAR_REPORTS_HTML)
        return httpx.Response(200, content=b"file-bytes")

    def run(**kwargs) -> set[str]:
        scraper = NesebarSiteScraper(
            cache_dir=tmp_path / str(len(kwargs)), delay=0.0,
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )
        try:
            return {r["url"].rsplit("/", 1)[-1] for r in scraper.run(**kwargs)}
        finally:
            scraper.close()

    # Without known URLs, `since` alone keeps only the 2024 heading's files.
    assert run(since="2024-01") == {"Budget_2018_5206.xlsx", "feb2024.xlsx"}
    # With known URLs, the never-seen 2019 files are fetched too, and the
    # already-stored one is not.
    base = "https://www.nesebar.bg/03-2019/"
    known = {base + "B1_2019_1_5206.xls"}
    got = run(since="2024-01", known_urls=known)
    assert "B3_2018_4_5206.xls" in got and "IB3_2018_4_5206_DES.xls" in got
    assert "B1_2019_1_5206.xls" not in got



def test_upsert_ignores_float_noise_and_volatile_ids_but_not_real_changes(db_session: Session) -> None:
    base = {
        "source": "eop", "source_id": "777", "title": "Ремонт",
        "contract_value_eur": 2812.1053465792015,
        "raw_json": {"contract": {"ContractReportId": 1, "CurrentContractValueEuro": 2812.11}},
    }
    upsert_procurements(db_session, [base])
    db_session.commit()

    # Same contract, re-fetched: last-bit float noise and a regenerated id.
    refetched = {
        **base,
        "contract_value_eur": 2812.105346579201,
        "raw_json": {"contract": {"ContractReportId": 2, "CurrentContractValueEuro": 2812.11}},
    }
    assert upsert_procurements(db_session, [refetched]) == (0, 0)
    db_session.commit()
    row = db_session.scalars(select(Procurement).where(Procurement.source_id == "777")).one()
    assert row.raw_json["contract"]["ContractReportId"] == 2  # payload kept current

    # A real price change (an annex) is still detected.
    annexed = {
        **refetched,
        "contract_value_eur": 3100.0,
        "raw_json": {"contract": {"ContractReportId": 3, "CurrentContractValueEuro": 3100.0}},
    }
    assert upsert_procurements(db_session, [annexed]) == (0, 1)
