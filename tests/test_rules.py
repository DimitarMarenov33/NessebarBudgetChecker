"""Unit tests for `analysis.matching`, `analysis.rules`, and `analysis.engine`.

Several fixtures below are lightly adapted from *real* titles/values seen in
`data/nessebar.db` while building the matcher (see `docs/RULES.md`), so the
Jaccard/distinctive-token thresholds here are exercised against the same kind
of text they'll see for real, not just toy examples.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nessebar_budget.analysis import matching, rules
from nessebar_budget.analysis.engine import run_full_analysis
from nessebar_budget.analysis.matching import MatchCandidate, find_best_match
from nessebar_budget.analysis.thresholds import Thresholds
from nessebar_budget.db.budget_models import BudgetLineItem
from nessebar_budget.db.models import Base, Flag, Procurement


def _dt(*args, **kwargs) -> dt.datetime:
    """Naive datetime fixture helper (ruff DTZ001-safe: built via a
    tz-aware call, then stripped, matching this project's convention of
    naive-but-UTC datetimes -- see e.g. `db/repo.py`'s `_now()`)."""
    return dt.datetime(*args, tzinfo=dt.UTC, **kwargs).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# analysis.matching
# ---------------------------------------------------------------------------


def test_tokenize_strips_stopwords_and_keeps_cadastre_whole() -> None:
    tokens = matching.tokenize("Основен ремонт на улица от о.т.307 в ПИ 51500.506.679, гр.Несебър")
    assert "CAD:51500.506.679" in tokens
    # generic boilerplate words should be gone
    assert "основен" not in tokens
    assert "ремонт" not in tokens
    assert "гр" not in tokens


def test_distinctive_tokens_excludes_nesebar_itself() -> None:
    # "Несебър"/"община"/"град" are in virtually every title and must not,
    # on their own, be treated as a distinguishing signal.
    assert matching.distinctive_tokens("Нещо си, гр.Несебър, община Несебър") == frozenset()


def test_distinctive_tokens_finds_settlement_and_street() -> None:
    tokens = matching.distinctive_tokens('Проект за ул. "Сатурн" в с.Равда')
    assert "равда" in tokens
    assert "ST:сатурн" in tokens


def test_distinctive_tokens_does_not_mistake_street_adjective_for_a_name() -> None:
    # Regression: "улично осветление" ("street lighting") is an adjective,
    # not "ул. <name>" -- an earlier version of the street regex matched the
    # bare "ул" prefix and captured a meaningless "ично" fragment from it,
    # which then spuriously "matched" unrelated objects/contracts that
    # merely both mentioned street lighting.
    tokens = matching.distinctive_tokens("Улично осветление на паркинги и тротоари")
    assert not any(t.startswith("ST:") for t in tokens)

    # But a real street name after the full word "улица" (not abbreviated)
    # must still be found.
    tokens = matching.distinctive_tokens("Паркинги по улица Първа в к.к. Слънчев бряг")
    assert "ST:първа" in tokens


def test_find_best_match_real_pair_above_jaccard_threshold() -> None:
    # Adapted from a real (budget object, eop contract) pair.
    candidates = [
        MatchCandidate(
            key=("eop", "595341"),
            title=(
                'ОБСЛУЖВАЩА УЛИЦА О.Т.800-О.Т.501 В ПИ 51500.506.679 В '
                'К.К."СЛЪНЧЕВ БРЯГ-ЗАПАД", ГР.НЕСЕБЪР, ОБЩИНА НЕСЕБЪР'
            ),
            value_eur=71_315.0,
        ),
    ]
    match = find_best_match(
        "Проектиране на обслужваща улица от о.т.800 до о.т.801 в ПИ 51500.506.679 в "
        "Слънчев бряг, гр.Несебър",
        45_790.0,
        candidates,
    )
    assert match is not None
    assert match.candidate.key == ("eop", "595341")
    assert match.jaccard_score >= 0.35 or match.shared_distinctive


def test_find_best_match_rejects_value_outside_ratio_window() -> None:
    # Same text overlap as above, but the value is 10x off -- must not match.
    candidates = [
        MatchCandidate(key=("eop", "1"), title="ОБСЛУЖВАЩА УЛИЦА О.Т.800 В ПИ 51500.506.679", value_eur=710_000.0)
    ]
    match = find_best_match("Обслужваща улица о.т.800 ПИ 51500.506.679", 45_790.0, candidates)
    assert match is None


def test_find_best_match_rejects_unrelated_title_despite_shared_boilerplate() -> None:
    # Both mention generic "доставка"/"община Несебър" boilerplate, nothing else.
    candidates = [
        MatchCandidate(key=("eop", "1"), title="Доставка на електромобил за нуждите на община Несебър", value_eur=19_335.0)
    ]
    match = find_best_match(
        "Доставка на компютърна техника за нуждите на община Несебър", 19_000.0, candidates
    )
    assert match is None


def test_find_best_match_empty_candidates_or_value() -> None:
    assert find_best_match("Нещо", 1000.0, []) is None
    assert find_best_match("Нещо", 0, [MatchCandidate(key=1, title="Нещо", value_eur=1000.0)]) is None


# ---------------------------------------------------------------------------
# analysis.rules
# ---------------------------------------------------------------------------


def _net_date(d: dt.datetime) -> str:
    millis = int(d.replace(tzinfo=dt.UTC).timestamp() * 1000)
    return f"/Date({millis}+0200)/"


def test_missing_value_rule_unchanged_interface() -> None:
    fake_record = {"id": 1, "source_id": "TEST-1", "contractor_name": "X", "contract_value_bgn": 0}
    hits = rules.MissingValueRule().check(fake_record)
    assert len(hits) == 1
    assert hits[0]["rule"] == "missing_value"


def test_missing_value_flags_adds_subject_fields() -> None:
    records = [
        {
            "id": 1,
            "source": "eop",
            "source_id": "123",
            "contractor_name": "X",
            "contract_value_eur": None,
            "contract_value_bgn": None,
        }
    ]
    flags = rules.missing_value_flags(records)
    assert len(flags) == 1
    assert flags[0]["subject_type"] == "contract"
    assert flags[0]["subject_key"] == "eop:123"


def test_late_publication_rule_flags_after_deadline() -> None:
    thresholds = Thresholds()
    contract_date = _dt(2026, 1, 1)
    ted_publish_date = contract_date + dt.timedelta(days=45)
    record = {
        "source": "eop",
        "source_id": "999",
        "title": "Тестова поръчка",
        "contractor_name": "Тест ЕООД",
        "raw_json": {
            "contract": {
                "ContractDate": _net_date(contract_date),
                "TedPublishDate": _net_date(ted_publish_date),
            }
        },
    }
    flags = rules.late_publication_flags([record], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["severity"] == "warning"  # 15 days over: beyond grace, below the 60-day "high" tier
    assert flag["details_json"]["days_late"] == 15  # 45 days actual - 30 day deadline
    assert "чл. 26" in flag["law_ref"]


def test_late_publication_rule_silent_within_deadline() -> None:
    thresholds = Thresholds()
    contract_date = _dt(2026, 1, 1)
    ted_publish_date = contract_date + dt.timedelta(days=10)
    record = {
        "source": "eop",
        "source_id": "999",
        "raw_json": {
            "contract": {
                "ContractDate": _net_date(contract_date),
                "TedPublishDate": _net_date(ted_publish_date),
            }
        },
    }
    assert rules.late_publication_flags([record], thresholds) == []


def test_late_publication_rule_ignores_sigma_records() -> None:
    thresholds = Thresholds()
    record = {"source": "sigma", "source_id": "1", "raw_json": {"contract": {}}}
    assert rules.late_publication_flags([record], thresholds) == []


def test_annex_growth_rule_flags_growth_above_ratio() -> None:
    thresholds = Thresholds()
    record = {
        "source": "eop",
        "source_id": "429",
        "title": "Нещо",
        "contractor_name": "X",
        "raw_json": {
            "contract": {
                "ContractValue": 357_120.06,
                "Currency": 1,  # EUR
                "CurrentContractValue": 425_474.07,
                "CurrentContractCurrency": 1,
            }
        },
    }
    flags = rules.annex_growth_flags([record], thresholds)
    assert len(flags) == 1
    assert flags[0]["severity"] == "warning"
    assert flags[0]["details_json"]["growth_pct"] > 10


def test_annex_growth_rule_uses_currency_codes_not_raw_euro_fields() -> None:
    # Reproduces a real data-quality trap: `CurrentContractValueEuro` in the
    # raw payload can be stale (still equal to the original ContractValueEuro)
    # even though `CurrentContractValue` changed -- and vice versa, a raw
    # `CurrentContractValue` can be bogus if its currency code isn't honored.
    # This case (modeled on a real record) must NOT be flagged once currency
    # codes are respected: original 190350.56 BGN (~97324.70 EUR) vs a
    # corrupted current value that is nominally "EUR" already.
    thresholds = Thresholds()
    record = {
        "source": "eop",
        "source_id": "462",
        "raw_json": {
            "contract": {
                "ContractValue": 190_350.56,
                "Currency": 3,  # BGN
                "ContractValueEuro": 97_324.70,
                "CurrentContractValue": 97_324.70,  # already-EUR, ~unchanged
                "CurrentContractCurrency": 1,
                "CurrentContractValueEuro": 97_324.70,
            }
        },
    }
    assert rules.annex_growth_flags([record], thresholds) == []


def test_single_bidder_rule_severity_by_value() -> None:
    thresholds = Thresholds()
    low = {"source": "sigma", "source_id": "1", "title": "A", "bids_received": 1, "contract_value_eur": 50_000}
    warn = {"source": "sigma", "source_id": "2", "title": "B", "bids_received": 1, "contract_value_eur": 150_000}
    high = {"source": "sigma", "source_id": "3", "title": "C", "bids_received": 1, "contract_value_eur": 600_000}
    multi = {"source": "sigma", "source_id": "4", "title": "D", "bids_received": 3, "contract_value_eur": 600_000}

    flags = rules.single_bidder_flags([low, warn, high, multi], thresholds)
    by_id = {f["details_json"]["source_id"]: f for f in flags}
    assert "1" not in by_id  # below warning threshold
    assert "4" not in by_id  # not single-bidder
    assert by_id["2"]["severity"] == "warning"
    assert by_id["3"]["severity"] == "high"


def test_contractor_concentration_rule_flags_dominant_contractor() -> None:
    thresholds = Thresholds()
    now = _dt(2026, 10, 6)
    recent = now - dt.timedelta(days=30)
    contracts = [
        {"contractor_eik": "1", "contractor_name": "Dominant OOD", "contract_value_eur": 100_000, "contract_date": recent}
        for _ in range(3)
    ] + [
        {"contractor_eik": "2", "contractor_name": "Other OOD", "contract_value_eur": 400_000, "contract_date": recent}
    ]
    flags = rules.contractor_concentration_flags(contracts, thresholds, now=now)
    assert len(flags) == 1
    assert flags[0]["subject_id"] == "1"
    assert flags[0]["severity"] == "info"


def test_contractor_concentration_rule_ignores_old_contracts() -> None:
    thresholds = Thresholds()
    now = _dt(2026, 10, 6)
    old = now - dt.timedelta(days=800)
    contracts = [
        {"contractor_eik": "1", "contractor_name": "X", "contract_value_eur": 100_000, "contract_date": old}
        for _ in range(5)
    ]
    assert rules.contractor_concentration_flags(contracts, thresholds, now=now) == []


def test_overspend_vs_plan_rule_flags_over_plan_and_over_estimate() -> None:
    thresholds = Thresholds()
    objects = [
        {
            "paragraph": "5200",
            "object_name": "Климатици",
            "period": "2026-08",
            "plan_current": 0,
            "spent_period": 95_735,
            "spent_prior": 0,
            "estimated_total": 20_000,
        },
        {
            "paragraph": "5200",
            "object_name": "Спокоен обект",
            "period": "2026-08",
            "plan_current": 100_000,
            "spent_period": 50_000,
            "spent_prior": 0,
            "estimated_total": 200_000,
        },
    ]
    flags = rules.overspend_vs_plan_flags(objects, thresholds)
    assert len(flags) == 1
    assert flags[0]["details_json"]["object_name"] == "Климатици"
    assert flags[0]["details_json"]["over_plan"] is True
    assert flags[0]["details_json"]["over_estimate"] is True


def test_plan_jump_rule_flags_large_mom_increase_and_new_object() -> None:
    thresholds = Thresholds()
    history = {
        ("5100", "Улица А"): [
            {"period": "2026-02", "plan_current": 109_160},
            {"period": "2026-03", "plan_current": 424_218},
        ],
        ("5200", "Нов обект"): [
            {"period": "2026-04", "plan_current": 620_000},
        ],
    }
    flags = rules.plan_jump_flags(history, thresholds, dataset_first_period="2026-02")
    rule_subjects = {f["subject_id"] for f in flags}
    assert "5100:Улица А:2026-02->2026-03" in rule_subjects
    assert "5200:Нов обект:new" in rule_subjects


def test_plan_jump_rule_ignores_small_or_early_changes() -> None:
    thresholds = Thresholds()
    history = {
        ("5100", "Стабилен"): [
            {"period": "2026-02", "plan_current": 100_000},
            {"period": "2026-03", "plan_current": 110_000},  # +10%, below ratio
        ],
        ("5200", "От началото"): [
            {"period": "2026-02", "plan_current": 300_000},  # first period == dataset first period
        ],
    }
    assert rules.plan_jump_flags(history, thresholds, dataset_first_period="2026-02") == []


def test_unmatched_spending_rule_flags_when_no_contract_matches() -> None:
    thresholds = Thresholds()
    objects = [
        {
            "paragraph": "5200",
            "object_name": "Напълно уникален обект без аналог никъде",
            "period": "2026-08",
            "spent_period": 60_000,
        }
    ]
    candidates = [MatchCandidate(key=("eop", "1"), title="Съвсем различна поръчка", value_eur=61_000)]
    flags = rules.unmatched_spending_flags(objects, candidates, thresholds)
    assert len(flags) == 1
    assert flags[0]["severity"] == "info"
    assert "не открихме публикуван договор" in flags[0]["message"]


def test_unmatched_spending_rule_silent_when_below_threshold_or_matched() -> None:
    thresholds = Thresholds()
    small = {"paragraph": "5200", "object_name": "Малък обект", "period": "2026-08", "spent_period": 1_000}
    matched = {
        "paragraph": "5200",
        "object_name": 'Ремонт на ул."Сатурн" от о.т.307 в к.к.Слънчев бряг',
        "period": "2026-08",
        "spent_period": 50_000,
    }
    candidates = [
        MatchCandidate(key=("eop", "1"), title='Ремонт на ул."Сатурн" от о.т.307 в к.к.Слънчев бряг', value_eur=55_000)
    ]
    assert rules.unmatched_spending_flags([small], candidates, thresholds) == []
    assert rules.unmatched_spending_flags([matched], candidates, thresholds) == []


# ---------------------------------------------------------------------------
# analysis.engine: upsert / new-updated-resolved bookkeeping
# ---------------------------------------------------------------------------


def _memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_run_full_analysis_upserts_new_then_updates_then_resolves() -> None:
    session = _memory_session()
    session.add(
        Procurement(
            source="sigma",
            source_id="1",
            title="Поръчка с 1 оферта",
            contractor_name="X EOOD",
            contractor_eik="111",
            contract_value_eur=200_000,
            bids_received=1,
            contract_date=_dt(2026, 1, 1),
        )
    )
    session.commit()

    now1 = _dt(2026, 6, 1)
    summary1 = run_full_analysis(session, now=now1)
    session.commit()
    assert summary1.counts["single_bidder"].new == 1
    assert summary1.counts["single_bidder"].updated == 0

    flags = session.scalars(select(Flag).where(Flag.rule == "single_bidder")).all()
    assert len(flags) == 1
    assert flags[0].first_seen_at == now1
    assert flags[0].last_seen_at == now1
    assert flags[0].resolved_at is None

    # Second run, same data: should update (not duplicate) the same subject_key.
    now2 = _dt(2026, 6, 8)
    summary2 = run_full_analysis(session, now=now2)
    session.commit()
    assert summary2.counts["single_bidder"].new == 0
    assert summary2.counts["single_bidder"].updated == 1
    flags = session.scalars(select(Flag).where(Flag.rule == "single_bidder")).all()
    assert len(flags) == 1
    assert flags[0].last_seen_at == now2

    # Third run, data no longer qualifies: should resolve, not delete.
    proc = session.scalars(select(Procurement)).one()
    proc.bids_received = 3
    session.commit()
    now3 = _dt(2026, 6, 15)
    summary3 = run_full_analysis(session, now=now3)
    session.commit()
    assert summary3.counts["single_bidder"].resolved == 1
    flags = session.scalars(select(Flag).where(Flag.rule == "single_bidder")).all()
    assert len(flags) == 1
    assert flags[0].resolved_at == now3

    session.close()


def test_run_full_analysis_handles_empty_db() -> None:
    session = _memory_session()
    summary = run_full_analysis(session, now=_dt(2026, 1, 1))
    assert summary.flags_produced == 0
    session.close()


def test_run_full_analysis_budget_object_rules() -> None:
    session = _memory_session()
    common = {
        "unit": "Общо",
        "paragraph": "5200",
        "object_name": "Тестов обект",
        "extra_json": {"row_type": "object"},
        "currency": "EUR",
        "estimated_total": 200_000,
    }
    session.add(BudgetLineItem(period="2026-02", plan_current=100_000, spent_period=1_000, **common))
    session.add(BudgetLineItem(period="2026-03", plan_current=300_000, spent_period=250_000, **common))
    session.commit()

    summary = run_full_analysis(session, now=_dt(2026, 4, 1))
    session.commit()
    assert summary.counts["plan_jump"].new >= 1
    assert summary.counts["overspend_vs_plan"].new >= 1

    session.close()
