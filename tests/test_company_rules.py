"""Unit tests for `analysis.rules.companies` (Trade Register <-> officials'
declarations of interest) and its wiring into `analysis.engine`.

Fixture style matches `tests/test_rules.py`: plain dicts for the pure rule
functions, an in-memory SQLite session for the `run_full_analysis` wiring
tests.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from nessebar_budget.analysis import rules
from nessebar_budget.analysis.engine import run_full_analysis
from nessebar_budget.analysis.thresholds import Thresholds
from nessebar_budget.db.models import Base, Company, CompanyPerson, Official, Procurement


def _dt(*args, **kwargs) -> dt.datetime:
    """Naive datetime fixture helper (ruff DTZ001-safe), same convention as
    `tests/test_rules.py`."""
    return dt.datetime(*args, tzinfo=dt.UTC, **kwargs).replace(tzinfo=None)


def _memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _contract(
    *,
    id: int = 1,  # mirrors the `_procurement_to_dict` shape
    source: str = "eop",
    source_id: str = "1",
    contractor_name: str | None,
    contractor_eik: str | None,
    contract_value_eur: float | None,
    contract_date: dt.datetime | None,
    cpv_code: str | None = None,
    title: str = "Тестова поръчка",
) -> dict:
    """A contract dict in `engine._procurement_to_dict`'s shape -- the
    companies rules only read the fields set here."""
    return {
        "id": id,
        "source": source,
        "source_id": source_id,
        "title": title,
        "procedure_type": None,
        "cpv_code": cpv_code,
        "contractor_name": contractor_name,
        "contractor_eik": contractor_eik,
        "contract_value_eur": contract_value_eur,
        "contract_value_bgn": None,
        "estimated_value_eur": None,
        "bids_received": None,
        "contract_date": contract_date,
        "published_at": None,
        "url": None,
        "raw_json": {},
    }


def _company(
    *,
    eik: str,
    name: str,
    status: str | None = "active",
    nkid_code: str | None = None,
    nkid_label: str | None = None,
    registered_at: dt.datetime | None = None,
    last_annual_report_year: int | None = 2025,
    people: list[dict] | None = None,
    legal_form: str | None = "Еднолично дружество с ограничена отговорност",
    last_no_activity_declaration_year: int | None = None,
) -> dict:
    return {
        "eik": eik,
        "name": name,
        "status": status,
        "nkid_code": nkid_code,
        "nkid_label": nkid_label,
        "registered_at": registered_at,
        "last_annual_report_year": last_annual_report_year,
        "legal_form": legal_form,
        "last_no_activity_declaration_year": last_no_activity_declaration_year,
        "source_url": f"https://portal.registryagency.bg/company/{eik}",
        "people": people or [],
    }


def _person(name: str, role: str = "manager", *, is_current: bool = True, share_text: str | None = None) -> dict:
    return {
        "name": name,
        "name_normalized": name.lower(),
        "role": role,
        "share_text": share_text,
        "person_eik": None,
        "is_current": is_current,
        "field_ident": "00070",
    }


def _official(
    *,
    name: str,
    role: str = "councillor",
    mandate: str | None = "2023-2027",
    declared_interests: list[dict] | None = None,
) -> dict:
    return {
        "name": name,
        "name_normalized": name.lower(),
        "role": role,
        "mandate": mandate,
        "source_url": "https://nesebar.bg/council/councillors.html",
        "document_url": f"https://nesebar.bg/declarations/{name.replace(' ', '_').lower()}.pdf",
        "declared_interests_json": declared_interests or [],
    }


# ---------------------------------------------------------------------------
# related_party
# ---------------------------------------------------------------------------


def test_related_party_name_match_warns_about_namesakes() -> None:
    thresholds = Thresholds()
    company = _company(
        eik="111222333",
        name="СТРОЙ ЕООД",
        people=[_person("Иван Петров Иванов", role="manager")],
    )
    official = _official(name="Иван Петров Иванов", role="councillor")
    contracts = [
        _contract(
            contractor_name="СТРОЙ ЕООД",
            contractor_eik="111222333",
            contract_value_eur=80_000,
            contract_date=_dt(2026, 1, 1),
        )
    ]

    flags = rules.related_party_flags(contracts, [company], [official], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["rule"] == "related_party"
    assert flag["tier"] == "signal"
    assert flag["subject_type"] == "contractor"
    assert flag["subject_id"] == "111222333"
    assert flag["details_json"]["match_basis"] == "name"
    # Below the high-value threshold and a bare name match -> warning, not high.
    assert flag["severity"] == "warning"
    assert "съвпадение" in flag["explanation"]
    assert "декларац" in flag["explanation"]
    assert "чл. 54, ал. 1, т. 7" in flag["law_ref"]
    assert "чл. 37" in flag["law_ref"]  # councillor -> ЗМСМА чл. 37 also cited

    kinds = {s["kind"] for s in flag["sources"]}
    assert {"registry", "declaration", "eop_contract"} <= kinds


def test_related_party_declaration_match_is_always_high_severity() -> None:
    thresholds = Thresholds()
    # No matching person in the registry -- only the declaration ties the
    # official to the company, so a name-based match cannot fire here.
    company = _company(eik="111222333", name="Строй ЕООД", people=[])
    official = _official(
        name="Петър Георгиев",
        role="mayor",
        mandate=None,
        declared_interests=[
            {
                "company_name": "Строй ЕООД",
                "eik": "111222333",
                "relation": "съдружник",
                "raw": "Съдружник в Строй ЕООД, ЕИК 111222333, дял 50%.",
            }
        ],
    )
    contracts = [
        _contract(
            contractor_name="Строй ЕООД",
            contractor_eik="111222333",
            contract_value_eur=10_000,  # well below related_party_high_eur
            contract_date=_dt(2026, 1, 1),
        )
    ]

    flags = rules.related_party_flags(contracts, [company], [official], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["details_json"]["match_basis"] == "declaration"
    assert flag["severity"] == "high"  # declaration match is always high
    # No councillor role here, so ЗМСМА чл. 37 should not be cited.
    assert "чл. 37" not in flag["law_ref"]

    declaration_src = next(s for s in flag["sources"] if s["kind"] == "declaration")
    raw_values = [f["value"] for f in declaration_src["fields"] if f["name"] == "raw"]
    assert raw_values and "Съдружник в Строй ЕООД" in raw_values[0]


def test_related_party_empty_companies_or_officials_returns_empty() -> None:
    thresholds = Thresholds()
    contracts = [
        _contract(
            contractor_name="Строй ЕООД",
            contractor_eik="111222333",
            contract_value_eur=80_000,
            contract_date=_dt(2026, 1, 1),
        )
    ]
    assert rules.related_party_flags(contracts, [], [], thresholds) == []
    assert rules.related_party_flags(contracts, [_company(eik="1", name="X")], [], thresholds) == []


# ---------------------------------------------------------------------------
# person_concentration
# ---------------------------------------------------------------------------


def test_person_concentration_one_person_behind_two_companies() -> None:
    thresholds = Thresholds()
    shared_manager = "Петър Георгиев Петров"
    company_a = _company(
        eik="100000001", name="Алфа ЕООД", people=[_person(shared_manager, role="manager")]
    )
    company_b = _company(
        eik="100000002", name="Бета ЕООД", people=[_person(shared_manager, role="partner")]
    )
    contracts = [
        _contract(
            id=1,
            source_id="1",
            contractor_name="Алфа ЕООД",
            contractor_eik="100000001",
            contract_value_eur=120_000,
            contract_date=_dt(2025, 6, 1),
        ),
        _contract(
            id=2,
            source_id="2",
            contractor_name="Бета ЕООД",
            contractor_eik="100000002",
            contract_value_eur=100_000,
            contract_date=_dt(2025, 9, 1),
        ),
    ]

    flags = rules.person_concentration_flags(contracts, [company_a, company_b], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["rule"] == "person_concentration"
    assert flag["subject_type"] == "person"
    assert flag["subject_id"] == f"person:{shared_manager.lower()}"
    assert flag["details_json"]["total_contract_count"] == 2
    assert flag["details_json"]["total_eur"] == 220_000.0
    assert {c["eik"] for c in flag["details_json"]["companies"]} == {"100000001", "100000002"}


def test_person_concentration_single_company_does_not_fire() -> None:
    thresholds = Thresholds()
    company = _company(eik="1", name="Солo ЕООД", people=[_person("Мария Иванова Колева")])
    contracts = [
        _contract(
            contractor_name="Солo ЕООД",
            contractor_eik="1",
            contract_value_eur=500_000,
            contract_date=_dt(2025, 1, 1),
        )
    ]
    assert rules.person_concentration_flags(contracts, [company], thresholds) == []


# ---------------------------------------------------------------------------
# young_company
# ---------------------------------------------------------------------------


def test_young_company_within_six_months_is_high_severity() -> None:
    thresholds = Thresholds()
    company = _company(eik="206942872", name="Нова Строй ЕООД", registered_at=_dt(2026, 1, 1))
    contracts = [
        _contract(
            id=42,
            source_id="42",
            contractor_name="Нова Строй ЕООД",
            contractor_eik="206942872",
            contract_value_eur=60_000,
            contract_date=_dt(2026, 3, 1),  # 59 days after registration
        )
    ]

    flags = rules.young_company_flags(contracts, [company], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["rule"] == "young_company"
    assert flag["subject_type"] == "contract"
    assert flag["subject_id"] == "eop:42"
    assert flag["severity"] == "high"  # within the 6-month high window
    assert flag["details_json"]["age_days"] == 59


def test_young_company_below_value_threshold_is_silent() -> None:
    thresholds = Thresholds()
    company = _company(eik="206942872", name="Нова Строй ЕООД", registered_at=_dt(2026, 1, 1))
    contracts = [
        _contract(
            contractor_name="Нова Строй ЕООД",
            contractor_eik="206942872",
            contract_value_eur=10_000,  # below young_company_min_eur
            contract_date=_dt(2026, 3, 1),
        )
    ]
    assert rules.young_company_flags(contracts, [company], thresholds) == []


# ---------------------------------------------------------------------------
# company_status
# ---------------------------------------------------------------------------


def test_company_status_liquidation_with_recent_contract() -> None:
    thresholds = Thresholds()
    company = _company(
        eik="1", name="Ликвидация ЕООД", status="liquidation", last_annual_report_year=2025
    )
    contracts = [
        _contract(
            contractor_name="Ликвидация ЕООД",
            contractor_eik="1",
            contract_value_eur=70_000,
            contract_date=_dt(2025, 11, 1),
        )
    ]
    now = _dt(2026, 6, 1)

    flags = rules.company_status_flags(contracts, [company], thresholds, now=now)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["rule"] == "company_status"
    assert flag["subject_type"] == "contractor"
    assert flag["subject_id"] == "1"
    assert flag["details_json"]["status_reason"] is True
    assert flag["severity"] == "warning"  # liquidation (not insolvency/deregistered) -> warning
    assert "ликвидация" in flag["message"]


def test_company_status_insolvency_is_high_severity() -> None:
    thresholds = Thresholds()
    company = _company(eik="1", name="Фалит ЕООД", status="insolvency")
    contracts = [
        _contract(
            contractor_name="Фалит ЕООД",
            contractor_eik="1",
            contract_value_eur=70_000,
            contract_date=_dt(2025, 11, 1),
        )
    ]
    flags = rules.company_status_flags(contracts, [company], thresholds, now=_dt(2026, 6, 1))
    assert len(flags) == 1
    assert flags[0]["severity"] == "high"


def test_company_status_active_company_does_not_fire() -> None:
    thresholds = Thresholds()
    company = _company(eik="1", name="Активна ЕООД", status="active", last_annual_report_year=2025)
    contracts = [
        _contract(
            contractor_name="Активна ЕООД",
            contractor_eik="1",
            contract_value_eur=70_000,
            contract_date=_dt(2025, 11, 1),
        )
    ]
    assert rules.company_status_flags(contracts, [company], thresholds, now=_dt(2026, 6, 1)) == []


# ---------------------------------------------------------------------------
# activity_mismatch
# ---------------------------------------------------------------------------


def test_activity_mismatch_hospitality_company_winning_works() -> None:
    thresholds = Thresholds()
    company = _company(
        eik="1",
        name="Хотел Несебър ЕООД",
        nkid_code="5510",
        nkid_label="Хотели и подобни средства за настаняване",
    )
    contracts = [
        _contract(
            contractor_name="Хотел Несебър ЕООД",
            contractor_eik="1",
            contract_value_eur=80_000,
            contract_date=_dt(2026, 1, 1),
            cpv_code="45000000",  # строителни работи
        )
    ]

    flags = rules.activity_mismatch_flags(contracts, [company], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["rule"] == "activity_mismatch"
    assert flag["tier"] == "signal"
    assert flag["severity"] == "warning"
    assert flag["subject_type"] == "contract"
    assert "строителство" in flag["details_json"]["mismatch"]


def test_activity_mismatch_unlisted_pair_is_never_flagged() -> None:
    """A trading company (НКИД 46/47) winning almost anything is NOT in
    `ACTIVITY_INCOMPATIBLE_PAIRS` -- absence from the table must never be a
    reason to flag (see that constant's docstring)."""
    thresholds = Thresholds()
    company = _company(eik="1", name="Търговия ЕООД", nkid_code="4690", nkid_label="Търговия на едро")
    contracts = [
        _contract(
            contractor_name="Търговия ЕООД",
            contractor_eik="1",
            contract_value_eur=80_000,
            contract_date=_dt(2026, 1, 1),
            cpv_code="45000000",
        )
    ]
    assert rules.activity_mismatch_flags(contracts, [company], thresholds) == []


# ---------------------------------------------------------------------------
# analysis.engine wiring
# ---------------------------------------------------------------------------


def test_run_full_analysis_handles_empty_companies_and_officials_tables() -> None:
    """Procurement data exists, but `companies`/`officials` are still empty
    (the Trade Register / declarations scrapers may not have run yet) --
    `run_full_analysis` must still run cleanly and produce no company flags."""
    session = _memory_session()
    session.add(
        Procurement(
            source="eop",
            source_id="1",
            title="Строителство на обект",
            contractor_name="Строй ЕООД",
            contractor_eik="111222333",
            contract_value_eur=80_000,
            contract_date=_dt(2026, 1, 1),
        )
    )
    session.commit()

    summary = run_full_analysis(session, now=_dt(2026, 6, 1))
    session.commit()

    for rule_name in (
        "related_party",
        "person_concentration",
        "young_company",
        "company_status",
        "activity_mismatch",
    ):
        assert summary.counts[rule_name].produced == 0
    session.close()


def test_run_full_analysis_related_party_end_to_end() -> None:
    """The engine correctly loads `Company`/`CompanyPerson`/`Official` ORM
    rows and hands them to `rules.companies` in the shape it expects."""
    session = _memory_session()
    session.add(
        Procurement(
            source="eop",
            source_id="1",
            title="Строителство на обект",
            contractor_name="Строй ЕООД",
            contractor_eik="111222333",
            contract_value_eur=150_000,
            contract_date=_dt(2026, 1, 1),
        )
    )
    company = Company(eik="111222333", name="Строй ЕООД", status="active", source_url="https://x")
    session.add(company)
    session.flush()
    session.add(
        CompanyPerson(
            company_id=company.id,
            name="ИВАН ПЕТРОВ ИВАНОВ",
            name_normalized="иван петров иванов",
            role="manager",
            is_current=True,
            field_ident="00070",
        )
    )
    session.add(
        Official(
            name="Иван Петров Иванов",
            name_normalized="иван петров иванов",
            role="councillor",
            mandate="2023-2027",
            document_url="https://nesebar.bg/declarations/ivanov.pdf",
            declared_interests_json=[],
        )
    )
    session.commit()

    summary = run_full_analysis(session, now=_dt(2026, 6, 1))
    session.commit()
    assert summary.counts["related_party"].new == 1
    session.close()


# ---------------------------------------------------------------------------
# Registry edge cases: ЗСч чл. 38, ал. 9; ЕИК formats; consortia
# ---------------------------------------------------------------------------


def _stale_contract(eik: str, value: float = 150_000, year: int = 2026) -> dict:
    return _contract(
        contractor_name="Фирма", contractor_eik=eik, contract_value_eur=value,
        contract_date=_dt(year, 3, 1),
    )


def test_company_status_stale_report_flags_with_verified_deadline() -> None:
    company = _company(eik="200000001", name="Късна ЕООД", last_annual_report_year=2022)
    flags = rules.company_status_flags(
        [_stale_contract("200000001")], [company], Thresholds(), now=_dt(2026, 10, 8)
    )
    assert len(flags) == 1
    flag = flags[0]
    assert flag["details_json"]["stale_report_reason"] is True
    assert "30 септември 2024" in flag["explanation"]  # ГФО 2023 was due 30.09.2024
    assert "чл. 38, ал. 1, т. 1 Закон за счетоводството" in flag["law_ref"]
    assert flag["subject_key"] == "company:200000001"


def test_company_status_sole_trader_is_exempt_from_annual_report() -> None:
    # ЗСч чл. 38, ал. 9, т. 1: ЕТ without a mandatory audit publish no ГФО.
    company = _company(
        eik="200000002", name="ЕТ Иван", last_annual_report_year=None,
        legal_form="Едноличен търговец",
    )
    flags = rules.company_status_flags(
        [_stale_contract("200000002")], [company], Thresholds(), now=_dt(2026, 10, 8)
    )
    assert flags == []


def test_company_status_counts_no_activity_declaration_as_filing() -> None:
    # ЗСч чл. 38, ал. 9, т. 2: an inactive company declares that instead.
    company = _company(
        eik="200000003", name="Тиха ЕООД", last_annual_report_year=2020,
        last_no_activity_declaration_year=2025,
    )
    flags = rules.company_status_flags(
        [_stale_contract("200000003")], [company], Thresholds(), now=_dt(2026, 10, 8)
    )
    assert flags == []


def test_company_rules_match_zero_padded_and_consortium_eiks() -> None:
    # ЦАИС ЕОП sometimes drops leading zeros ("646811") or lists a consortium.
    padded = _company(eik="000646811", name="Падната ЕООД", last_annual_report_year=2020)
    member = _company(eik="200948893", name="Член ЕООД", last_annual_report_year=2020)
    contracts = [
        _contract(contractor_name="Падната", contractor_eik="646811",
                  contract_value_eur=150_000, contract_date=_dt(2026, 3, 1), source_id="1"),
        _contract(contractor_name="ДЗЗД", contractor_eik="200948893; 204901777",
                  contract_value_eur=150_000, contract_date=_dt(2026, 3, 1), source_id="2"),
        _contract(contractor_name="Член ЕООД", contractor_eik="200948893",
                  contract_value_eur=10_000, contract_date=_dt(2025, 3, 1), source_id="3"),
    ]
    flags = rules.company_status_flags(contracts, [padded, member], Thresholds(), now=_dt(2026, 10, 8))
    by_key = {f["subject_key"]: f for f in flags}
    assert set(by_key) == {"company:000646811", "company:200948893"}
    # Links go to the contractor page keyed on the raw ЕОП code...
    assert by_key["company:000646811"]["subject_id"] == "646811"
    # ...and prefer the company's own page over the consortium's.
    assert by_key["company:200948893"]["subject_id"] == "200948893"


def test_young_company_skips_pre_trade_register_eiks() -> None:
    # 1xxxxxxxx companies were re-registered from the courts in 2008-2011;
    # their earliest registry date is not their founding date.
    company = _company(eik="102981058", name="Стара ООД", registered_at=_dt(2026, 1, 1))
    contracts = [
        _contract(contractor_name="Стара ООД", contractor_eik="102981058",
                  contract_value_eur=300_000, contract_date=_dt(2026, 3, 1))
    ]
    assert rules.young_company_flags(contracts, [company], Thresholds()) == []
