"""Unit tests for the `registry` (Търговски регистър) scraper.

`parse_deed`/`normalize_name` are tested against three real captured deeds
(data/samples/registry/) covering ООД, ЕООД and АД -- see
docs/sources/REGISTRY_API.md for how they were fetched and what every
`fieldIdent` means. `RegistryScraper` itself is tested with an in-process
``httpx.MockTransport`` (no network access) covering the on-disk cache and
the 429-retry/backoff path. The `upsert_company` test uses an in-memory
SQLite, as the project's other repo tests do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Base, Company, CompanyPerson
from nessebar_budget.db.repo import upsert_company
from nessebar_budget.scrapers.registry import RegistryScraper, normalize_name, parse_deed

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "data" / "samples" / "registry"


def _load(name: str) -> Any:
    return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# parse_deed -- ООД (102981058, ЕЛЕКТРИКАЛ ГРУП)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ood_parsed() -> dict[str, Any]:
    return parse_deed(_load("deed_102981058.json"))


def test_parse_deed_ood_identity(ood_parsed: dict[str, Any]) -> None:
    assert ood_parsed["eik"] == "102981058"
    assert ood_parsed["name"] == "ЕЛЕКТРИКАЛ ГРУП"
    assert ood_parsed["legal_form"] == "Дружество с ограничена отговорност"
    assert ood_parsed["status"] == "active"
    assert "Несебър" in ood_parsed["seat_address"]
    assert ood_parsed["source_url"] == (
        "https://portal.registryagency.bg/CR/Reports/ActiveConditionTabResult?uic=102981058"
    )


def test_parse_deed_ood_nkid(ood_parsed: dict[str, Any]) -> None:
    assert ood_parsed["nkid_code"] == "4521"
    # The registry's own text for this label mixes Cyrillic and Latin
    # look-alike letters (a real data-quality quirk, not a parsing bug) --
    # just check a non-empty label came through.
    assert ood_parsed["nkid_label"]
    assert "ИНСТАЛАЦИИ" in ood_parsed["nkid_label"]


def test_parse_deed_ood_capital(ood_parsed: dict[str, Any]) -> None:
    assert ood_parsed["capital_eur"] == pytest.approx(51384.83)


def test_parse_deed_ood_registered_at(ood_parsed: dict[str, Any]) -> None:
    assert ood_parsed["registered_at"] is not None
    assert ood_parsed["registered_at"].year <= 2009


def test_parse_deed_ood_last_annual_report_year(ood_parsed: dict[str, Any]) -> None:
    assert ood_parsed["last_annual_report_year"] == 2022


def test_parse_deed_ood_manager(ood_parsed: dict[str, Any]) -> None:
    managers = [p for p in ood_parsed["people"] if p["role"] == "manager"]
    assert len(managers) == 1
    manager = managers[0]
    assert manager["name"] == "ЙОРДАН ПЛАМЕНОВ МОМЧЕВ"
    assert manager["name_normalized"] == "йордан пламенов момчев"
    assert manager["field_ident"] == "00070"
    assert manager["is_current"] is True


def test_parse_deed_ood_two_partners_with_shares(ood_parsed: dict[str, Any]) -> None:
    partners = [p for p in ood_parsed["people"] if p["role"] == "partner"]
    assert len(partners) == 2
    names = {p["name"] for p in partners}
    assert names == {"МИХАИЛ ПЛАМЕНОВ МОМЧЕВ", "ЙОРДАН ПЛАМЕНОВ МОМЧЕВ"}
    for partner in partners:
        assert partner["share_text"] == "50250.00 лв."
        assert partner["field_ident"] == "00190"


def test_parse_deed_ood_raw_json_is_slim(ood_parsed: dict[str, Any]) -> None:
    raw = ood_parsed["raw_json"]
    assert "00070" in raw
    entry = raw["00070"]
    assert set(entry) == {"text", "entry_date", "action_date"}
    assert entry["text"] == "ЙОРДАН ПЛАМЕНОВ МОМЧЕВ, Държава: БЪЛГАРИЯ"


# ---------------------------------------------------------------------------
# parse_deed -- ЕООД (102078098, Актив-Р)
# ---------------------------------------------------------------------------


def test_parse_deed_eood_sole_owner_and_legal_form() -> None:
    parsed = parse_deed(_load("deed_102078098_eood.json"))
    assert parsed["legal_form"] == "Еднолично дружество с ограничена отговорност"

    owners = [p for p in parsed["people"] if p["role"] == "sole_owner"]
    assert len(owners) == 1
    assert owners[0]["name"] == "РУМЕН СТОЯНОВ ДИМОВ"
    assert owners[0]["field_ident"] == "00230"

    # A manager field with two concatenated historical entries (a typo'd name
    # corrected later, with no share-amount delimiter between them) -- both
    # still get extracted as separate people, not merged into one garbled name
    # (see `_extract_people`'s docstring on how the known-country anchor
    # resolves this).
    managers = [p for p in parsed["people"] if p["role"] == "manager"]
    assert len(managers) == 2
    assert {m["name"] for m in managers} == {"Румен Стоянов Дивов", "РУМЕН СТОЯНОВ ДИМОВ"}


# ---------------------------------------------------------------------------
# parse_deed -- АД (102003626, ТРАНССТРОЙ-БУРГАС)
# ---------------------------------------------------------------------------


def test_parse_deed_ad_board_members_and_nkid() -> None:
    parsed = _load("deed_102003626_ad.json")
    result = parse_deed(parsed)
    assert result["legal_form"] == "Акционерно дружество"
    assert result["nkid_code"] == "42.99"

    board = [p for p in result["people"] if p["role"] == "board_member"]
    names = {p["name"] for p in board}
    # The two-tier board's management-board members (ident 00132).
    assert "ВАЛЕНТИН СТОЙНЕВ БОРИСОВ" in names
    assert "НИКОЛАЙ МИЛЕВ МИЛЕВ" in names
    assert "Гергана Господинова Николова" in names
    # The supervisory board (00140) -- at least the cleanly-delimited member.
    assert "Николай Ангелов Георгиев" in names


def test_parse_deed_agro_sam_shape_person_with_no_country_clause() -> None:
    """Real shape from EIK 147043618 (АГРО-САМ, ЕООД): both 00070 (manager)
    and 00230 (sole_owner) are a bare name with no "Държава:" clause at all
    -- `_PERSON_RE` can't anchor on anything, so this must fall back to
    treating the whole field text as one person instead of silently
    producing zero people (the bug this test guards against)."""
    deed = {
        "uic": "147043618",
        "companyName": "АГРО-САМ",
        "legalForm": 10,
        "deedStatus": 2,
        "sections": [
            {
                "subDeeds": [
                    {
                        "groups": [
                            {
                                "fields": [
                                    {
                                        "fieldIdent": "00070",
                                        "htmlData": "<div><p>Сабри Исмаилов Алиев</p></div>",
                                        "fieldOperation": 3,
                                        "fieldEntryDate": "2015-01-01T00:00:00",
                                        "fieldActionDate": "2015-01-01T00:00:00",
                                        "recordMinActionDate": "2015-01-01T00:00:00",
                                    },
                                    {
                                        "fieldIdent": "00230",
                                        "htmlData": "<div><p>Сабри Исмаилов Алиев</p></div>",
                                        "fieldOperation": 3,
                                        "fieldEntryDate": "2015-01-01T00:00:00",
                                        "fieldActionDate": "2015-01-01T00:00:00",
                                        "recordMinActionDate": "2015-01-01T00:00:00",
                                    },
                                ]
                            }
                        ]
                    }
                ]
            }
        ],
    }
    result = parse_deed(deed)

    managers = [p for p in result["people"] if p["role"] == "manager"]
    owners = [p for p in result["people"] if p["role"] == "sole_owner"]
    assert len(managers) == 1
    assert len(owners) == 1
    assert managers[0]["name"] == "Сабри Исмаилов Алиев"
    assert owners[0]["name"] == "Сабри Исмаилов Алиев"
    # Same person under two different idents/roles is expected and fine --
    # the uniqueness constraint is (company_id, name_normalized, role,
    # field_ident), which keeps both rows distinct.
    assert managers[0]["name_normalized"] == owners[0]["name_normalized"]


def test_extract_people_person_eik_suffix_stripped_with_country() -> None:
    """Real shape from EIK 102045504: a partner that is itself a company,
    "СОФИЯ ФРАНС АУТО, ЕИК/ПИК 040823148, Държава: БЪЛГАРИЯ, Размер на
    дяловото участие: 488642.70 €" -- `ЕИК/ПИК NNNNNNNNN` must land in
    `person_eik`, not stay glued onto `name`."""
    from nessebar_budget.scrapers.registry import _extract_people

    text = (
        "ТОДОР СТОЯНОВ ДЕМИРКОВ, Държава: БЪЛГАРИЯ, "
        "Размер на дяловото участие: 9972.30 € "
        "СОФИЯ ФРАНС АУТО, ЕИК/ПИК 040823148, Държава: БЪЛГАРИЯ, "
        "Размер на дяловото участие: 488642.70 €"
    )
    people = _extract_people(text, role="partner", ident="00190")
    by_name = {p["name"]: p for p in people}
    assert "СОФИЯ ФРАНС АУТО" in by_name
    assert by_name["СОФИЯ ФРАНС АУТО"]["person_eik"] == "040823148"
    assert by_name["СОФИЯ ФРАНС АУТО"]["share_text"] == "488642.70 €"
    assert by_name["ТОДОР СТОЯНОВ ДЕМИРКОВ"]["person_eik"] is None


def test_extract_people_person_eik_suffix_stripped_without_country() -> None:
    """Real shape from EIK 130083729 (ИНЖКОНСУЛТ): a sole owner that is
    itself a company, with *no* "Държава:" clause at all -- both fixes
    apply together (the no-country fallback, then the ЕИК/ПИК strip)."""
    from nessebar_budget.scrapers.registry import _extract_people

    people = _extract_people('"ДАРЗАЛА ХОЛДИНГ" АД, ЕИК/ПИК 205127270', role="sole_owner", ident="00230")
    assert len(people) == 1
    assert people[0]["name"] == '"ДАРЗАЛА ХОЛДИНГ" АД'
    assert people[0]["person_eik"] == "205127270"


def test_split_person_eik() -> None:
    from nessebar_budget.scrapers.registry import _split_person_eik

    assert _split_person_eik("СТОЯН ИВАНОВ") == ("СТОЯН ИВАНОВ", None)
    assert _split_person_eik("ФИРМА ЕООД, ЕИК/ПИК 123456789") == ("ФИРМА ЕООД", "123456789")
    assert _split_person_eik("ФИРМА ЕООД, ЕИК 123456789") == ("ФИРМА ЕООД", "123456789")


def test_parse_deed_et_sole_trader_uses_00180() -> None:
    """ЕТ (едноличен търговец, `legalForm == 1`) deeds carry the trader's own
    name under a distinct ident, 00180 -- not 00070/00230 -- and the deed has
    no manager/partner/sole-owner fields at all otherwise (the trader *is*
    the business). Real shape from EIK 121305650 (АНИМАТ - АНГЕЛ АНГЕЛОВ)."""
    deed = {
        "uic": "121305650",
        "companyName": "АНИМАТ - АНГЕЛ АНГЕЛОВ",
        "legalForm": 1,
        "sections": [
            {
                "subDeeds": [
                    {
                        "groups": [
                            {
                                "fields": [
                                    {
                                        "fieldIdent": "00030",
                                        "htmlData": "<p>Едноличен търговец</p>",
                                        "fieldOperation": 3,
                                    },
                                    {
                                        "fieldIdent": "00180",
                                        "htmlData": "<p>АНГЕЛ МАТЕВ АНГЕЛОВ, Държава: БЪЛГАРИЯ</p>",
                                        "fieldOperation": 3,
                                    },
                                ]
                            }
                        ]
                    }
                ]
            }
        ],
    }
    result = parse_deed(deed)
    assert result["legal_form"] == "Едноличен търговец"
    assert len(result["people"]) == 1
    person = result["people"][0]
    assert person["name"] == "АНГЕЛ МАТЕВ АНГЕЛОВ"
    assert person["role"] == "sole_owner"
    assert person["field_ident"] == "00180"


def test_parse_deed_degrades_gracefully_on_empty_deed() -> None:
    """A deed with no `sections` at all (e.g. a minimal/unexpected payload)
    must not raise -- every field should come back `None`/empty."""
    result = parse_deed({"uic": "000000000", "companyName": "X"})
    assert result["eik"] == "000000000"
    assert result["name"] == "X"
    assert result["people"] == []
    assert result["registered_at"] is None
    assert result["status"] == "active"


# ---------------------------------------------------------------------------
# normalize_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ЙОРДАН ПЛАМЕНОВ МОМЧЕВ", "йордан пламенов момчев"),
        ("  Иван   Петров  ", "иван петров"),
        ('"МОТО-ПФОЕ" ЕООД', "мото пфое еоод"),
        ("Петър, Иванов.", "петър иванов"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


# ---------------------------------------------------------------------------
# upsert_company
# ---------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def test_upsert_company_inserts_new_row_with_people(session: Session) -> None:
    parsed = parse_deed(_load("deed_102981058.json"))
    company = upsert_company(session, parsed)
    session.commit()

    assert company.eik == "102981058"
    assert company.name == "ЕЛЕКТРИКАЛ ГРУП"
    assert company.fetched_at is not None

    people = session.scalars(
        select(CompanyPerson).where(CompanyPerson.company_id == company.id)
    ).all()
    assert len(people) == 3  # 1 manager + 2 partners


def test_upsert_company_reupsert_replaces_people_without_duplicates(session: Session) -> None:
    parsed = parse_deed(_load("deed_102981058.json"))

    upsert_company(session, parsed)
    session.commit()

    # Re-upsert the exact same parsed record.
    company = upsert_company(session, parsed)
    session.commit()

    companies = session.scalars(select(Company).where(Company.eik == "102981058")).all()
    assert len(companies) == 1  # no duplicate Company row

    people = session.scalars(
        select(CompanyPerson).where(CompanyPerson.company_id == company.id)
    ).all()
    assert len(people) == 3  # still 3, not 6 -- old rows were replaced, not appended


def test_upsert_company_reupsert_with_fewer_people_drops_stale_rows(session: Session) -> None:
    parsed = parse_deed(_load("deed_102981058.json"))
    company = upsert_company(session, parsed)
    session.commit()

    shrunk = dict(parsed)
    shrunk["people"] = parsed["people"][:1]
    upsert_company(session, shrunk)
    session.commit()

    people = session.scalars(
        select(CompanyPerson).where(CompanyPerson.company_id == company.id)
    ).all()
    assert len(people) == 1


# ---------------------------------------------------------------------------
# RegistryScraper -- cache + 429 handling (in-process MockTransport, no network)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_scraper(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    deed = _load("deed_102981058.json")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        eik = request.url.path.rsplit("/", 1)[-1]
        if eik == "102981058":
            return httpx.Response(200, json=deed)
        if eik == "429000000":
            return httpx.Response(429, text="Too Many Requests")
        if eik == "404000000":
            return httpx.Response(404, text="Not Found")
        return httpx.Response(404, json={"error": f"unhandled eik {eik}"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    scraper = RegistryScraper(client=client, delay=0.0, max_retries=2)
    scraper._calls = calls  # type: ignore[attr-defined]
    yield scraper
    scraper.close()


def test_registry_scraper_fetches_and_caches(registry_scraper: RegistryScraper, tmp_path) -> None:
    records = registry_scraper.fetch(["102981058"])
    assert len(records) == 1
    assert records[0]["eik"] == "102981058"
    assert not registry_scraper.failures

    cache_file = tmp_path / "cache" / "registry" / "102981058.json"
    assert cache_file.exists()

    # A second fetch for the same EIK must be served from the cache -- no
    # additional HTTP call.
    calls_before = registry_scraper._calls["count"]  # type: ignore[attr-defined]
    registry_scraper.fetch(["102981058"])
    assert registry_scraper._calls["count"] == calls_before  # type: ignore[attr-defined]


def test_registry_scraper_404_is_a_recorded_failure_not_a_crash(
    registry_scraper: RegistryScraper,
) -> None:
    records = registry_scraper.fetch(["404000000"])
    assert records == []
    assert len(registry_scraper.failures) == 1
    assert registry_scraper.failures[0][0] == "404000000"


def test_registry_scraper_429_retries_then_gives_up_gracefully(
    registry_scraper: RegistryScraper, monkeypatch
) -> None:
    # Avoid real sleeping during the backoff between retries.
    monkeypatch.setattr("nessebar_budget.scrapers.registry.time.sleep", lambda _s: None)

    records = registry_scraper.fetch(["429000000"])
    assert records == []
    assert len(registry_scraper.failures) == 1
    eik, reason = registry_scraper.failures[0]
    assert eik == "429000000"
    assert "429" in reason


def test_registry_scraper_isolates_one_failure_from_the_rest(
    registry_scraper: RegistryScraper, monkeypatch
) -> None:
    monkeypatch.setattr("nessebar_budget.scrapers.registry.time.sleep", lambda _s: None)

    records = registry_scraper.fetch(["102981058", "404000000"])
    assert len(records) == 1
    assert records[0]["eik"] == "102981058"
    assert len(registry_scraper.failures) == 1
    assert registry_scraper.failures[0][0] == "404000000"


def test_normalize_eiks_pads_splits_and_drops_placeholders() -> None:
    from nessebar_budget.scrapers.registry import normalize_eiks

    assert normalize_eiks("646811") == ["000646811"]
    assert normalize_eiks("200948893; 204901777") == ["200948893", "204901777"]
    assert normalize_eiks("не се публикува") == []
    assert normalize_eiks(None) == []
    assert normalize_eiks(" 102981058 ") == ["102981058"]
    assert normalize_eiks("1234567890123") == ["1234567890123"]


def test_normalize_name_is_shared_and_hyphen_insensitive() -> None:
    from nessebar_budget.scrapers import declarations, registry

    assert registry.normalize_name("Петър Хрусафов-Тодоров") == "петър хрусафов тодоров"
    assert registry.normalize_name("ПЕТЪР ХРУСАФОВ ТОДОРОВ") == "петър хрусафов тодоров"
    assert registry.normalize_name("Христо Г.Яръмов") == registry.normalize_name("Христо Г. Яръмов")
    assert declarations.normalize_name is registry.normalize_name
