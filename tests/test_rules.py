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

from nessebar_budget.analysis import matching, provenance, rules
from nessebar_budget.analysis.engine import run_full_analysis
from nessebar_budget.analysis.matching import MatchCandidate, find_best_match
from nessebar_budget.analysis.thresholds import Thresholds
from nessebar_budget.db.budget_models import BudgetLineItem
from nessebar_budget.db.models import Base, BudgetReport, Flag, Procurement


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


def test_overspend_vs_plan_rule_flags_only_the_softer_case() -> None:
    """overspend_vs_plan keeps the softer case (a plan exists but is exceeded);
    no plan at all / over the total estimate moved to unplanned_spending."""
    thresholds = Thresholds()
    unplanned = {
        "paragraph": "5200",
        "object_name": "Климатици",
        "period": "2026-08",
        "plan_current": 0,
        "spent_period": 95_735,
        "spent_prior": 0,
        "estimated_total": 20_000,
    }
    over_plan = {
        "paragraph": "5200",
        "object_name": "Обект над плана",
        "period": "2026-08",
        "plan_current": 100_000,
        "spent_period": 150_000,
        "spent_prior": 0,
        "estimated_total": 300_000,
    }
    calm = {
        "paragraph": "5200",
        "object_name": "Спокоен обект",
        "period": "2026-08",
        "plan_current": 100_000,
        "spent_period": 50_000,
        "spent_prior": 0,
        "estimated_total": 200_000,
    }
    flags = rules.overspend_vs_plan_flags([unplanned, over_plan, calm], thresholds)
    assert [f["details_json"]["object_name"] for f in flags] == ["Обект над плана"]
    assert flags[0]["tier"] == "signal"
    assert "решение на общинския съвет" in " ".join(flags[0]["documents_json"])

    unplanned_flags = rules.unplanned_spending_flags([unplanned, over_plan, calm], thresholds)
    assert [f["details_json"]["object_name"] for f in unplanned_flags] == ["Климатици"]
    assert unplanned_flags[0]["details_json"]["reasons"] == ["no_plan", "over_estimate"]


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


def test_quantity_regex_positive_cases() -> None:
    for text in (
        "Доставка на 20 броя компютри",
        "15 бр. климатици",
        "2 000 т асфалт",
        "500 кв.м",
        "10 /десет/ броя контейнери",
        "15 х 20 панела",
    ):
        assert rules._has_quantity(text), text


def test_quantity_regex_negative_cases() -> None:
    for text in (
        "Доставка на канцеларски материали",
        "Униформено облекло за служители",
    ):
        assert not rules._has_quantity(text), text


def test_quantity_regex_does_not_match_unrelated_words_with_shared_prefix() -> None:
    # "лева" (currency), "тонаж" (a property, not "X тона"), "часовник" (a
    # noun) must not be mistaken for a bare "л"/"тон"/"час" unit.
    assert not rules._has_quantity("20 лева за бройка")
    assert not rules._has_quantity("20 тонаж на превозното средство")
    assert not rules._has_quantity("20 часовник")


def test_is_framework_defined_true_for_call_off_and_unit_price_language() -> None:
    assert rules._is_framework_defined("доставка по рамково споразумение")
    assert rules._is_framework_defined("заплащане по единични цени")
    assert rules._is_framework_defined("доставка въз основа на писмени заявки от възложителя")
    assert rules._is_framework_defined(
        "Посочените количества са прогнозни и в тях може да настъпи промяна"
    )


def test_is_framework_defined_false_for_plain_text() -> None:
    assert not rules._is_framework_defined("Доставка на посадъчен материал за озеленяване")


def _eop_contract_record(
    *,
    source_id: str,
    value_eur: float,
    subject: str,
    type_of_contract: int = 2,
    description: str | None = None,
    notice_text: str | None = None,
) -> dict:
    return {
        "source": "eop",
        "source_id": source_id,
        "title": subject,
        "contractor_name": "Доставчик ЕООД",
        "contract_date": _dt(2026, 1, 1),
        "contract_value_eur": value_eur,
        "raw_json": {
            "contract": {"ContractSubject": subject, "TypeOfContract": type_of_contract},
            "tender_detail": {
                "TenderName": subject,
                "TenderDescription": description or "",
                "notice_text": notice_text or "",
            },
        },
    }


#: A substantive (> 80 chars) technical description that states no count.
_DESCRIPTION_NO_QUANTITY = (
    "Посадъчният материал трябва да бъде здрав, добре развит, без механични повреди, "
    "болести и неприятели, с добре оформена коренова система и съответстващ на сорта."
)


def test_missing_quantity_rule_flags_supply_contract_without_quantity() -> None:
    thresholds = Thresholds()
    record = _eop_contract_record(
        source_id="413",
        value_eur=385_000,
        subject="Избор на изпълнител за доставка на посадъчен материал – цветя и храсти",
        description=_DESCRIPTION_NO_QUANTITY,
    )
    flags = rules.missing_quantity_flags([record], [], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["severity"] == "warning"  # >= 100k and < 500k
    assert flag["subject_type"] == "contract"
    assert flag["subject_id"] == "eop:413"
    assert "не посочва количество" in flag["message"]
    assert "€" in flag["message"]
    assert "ЗОП" in flag["law_ref"]
    # None of title/TenderDescription/notice_text stated a quantity here.
    assert flag["details_json"]["quantity_found_in"] == "none"
    assert flag["tier"] == "opacity"
    assert "техническа спецификация" in flag["documents_json"]
    # A description exists, so this is the quantity-only case: never also
    # price_unverifiable.
    assert rules.price_unverifiable_flags([record], thresholds) == []


def test_missing_quantity_rule_finds_quantity_in_notice_text() -> None:
    """The quantity-text search now reaches `tender_detail.notice_text` --
    the richer text `scrapers/eop.py`'s `extract_notice`/`_build_notices`
    parse from the full published-notice HTML -- not just `title`/
    `TenderDescription`. This is the fix for EOP ids like 644-646 (see
    docs/RULES.md): a contract whose `title` and (truncated)
    `TenderDescription` both lack a quantity must no longer be flagged if
    the full notice text states one.
    """
    thresholds = Thresholds()
    record = _eop_contract_record(
        source_id="644",
        value_eur=1_478_239,
        subject="Доставка на горива за нуждите на Общинска администрация Несебър",
        description="Доставка на горива за нуждите на Общинска администрация Несебър по обособени позиции:",
        notice_text="Доставка на 150 000 литра дизелово гориво Б6 за нуждите на ОП БКСО - Несебър",
    )
    assert rules.missing_quantity_flags([record], [], thresholds) == []


def test_missing_quantity_rule_severity_tiers_by_value() -> None:
    thresholds = Thresholds()
    d = _DESCRIPTION_NO_QUANTITY
    info = _eop_contract_record(
        source_id="1", value_eur=50_000, subject="Доставка на материали", description=d
    )
    warn = _eop_contract_record(
        source_id="2", value_eur=150_000, subject="Доставка на материали", description=d
    )
    high = _eop_contract_record(
        source_id="3", value_eur=600_000, subject="Доставка на материали", description=d
    )
    flags = rules.missing_quantity_flags([info, warn, high], [], thresholds)
    by_id = {f["details_json"]["source_id"]: f for f in flags}
    assert by_id["1"]["severity"] == "info"
    assert by_id["2"]["severity"] == "warning"
    assert by_id["3"]["severity"] == "high"


def test_missing_quantity_rule_silent_when_quantity_stated_or_below_threshold_or_wrong_type() -> None:
    thresholds = Thresholds()
    has_quantity = _eop_contract_record(
        source_id="1", value_eur=200_000, subject="Доставка на 20 броя компютри"
    )
    below_threshold = _eop_contract_record(
        source_id="2", value_eur=10_000, subject="Доставка на материали"
    )
    not_a_supply = _eop_contract_record(
        source_id="3", value_eur=500_000, subject="СМР по ремонт на улица", type_of_contract=3
    )
    assert rules.missing_quantity_flags([has_quantity], [], thresholds) == []
    assert rules.missing_quantity_flags([below_threshold], [], thresholds) == []
    assert rules.missing_quantity_flags([not_a_supply], [], thresholds) == []


def test_missing_quantity_rule_silent_when_framework_defined() -> None:
    thresholds = Thresholds()
    record = _eop_contract_record(
        source_id="412",
        value_eur=220_000,
        subject="Доставка на спомагателно-хигиенни материали",
        description=(
            "чрез периодично възлагане на доставки въз основа на писмени заявки "
            "от възложителя, по прогнозни количества"
        ),
    )
    assert rules.missing_quantity_flags([record], [], thresholds) == []


def test_missing_quantity_rule_ignores_sigma_and_open_tenders() -> None:
    thresholds = Thresholds()
    sigma_record = {
        "source": "sigma",
        "source_id": "1",
        "contractor_name": "X",
        "contract_date": _dt(2026, 1, 1),
        "contract_value_eur": 500_000,
        "raw_json": {},
    }
    open_tender = {
        "source": "eop",
        "source_id": "2",
        "contract_value_eur": 500_000,
        "raw_json": {"contract": {"ContractSubject": "Доставка на материали", "TypeOfContract": 2}},
    }
    assert rules.missing_quantity_flags([sigma_record, open_tender], [], thresholds) == []


def test_missing_quantity_rule_flags_budget_object_without_quantity() -> None:
    thresholds = Thresholds()
    objects = [
        {
            "paragraph": "5200",
            "object_name": "Компютри за СУ Несебър STEM",
            "period": "2026-08",
            "plan_current": 75_195,
            "spent_period": 75_195,
        }
    ]
    flags = rules.missing_quantity_flags([], objects, thresholds)
    assert len(flags) == 1
    flag = flags[0]
    assert flag["subject_type"] == "budget_object"
    assert flag["subject_id"] == "5200:Компютри за СУ Несебър STEM"
    assert "€" in flag["message"]


def test_missing_quantity_rule_budget_object_silent_outside_section_52_or_with_quantity() -> None:
    thresholds = Thresholds()
    other_paragraph = {
        "paragraph": "5100",
        "object_name": "Основен ремонт на улица",
        "period": "2026-08",
        "plan_current": 300_000,
        "spent_period": 300_000,
    }
    has_quantity = {
        "paragraph": "5200",
        "object_name": "15 бр. климатици за общински сгради",
        "period": "2026-08",
        "plan_current": 0,
        "spent_period": 95_735,
    }
    below_threshold = {
        "paragraph": "5200",
        "object_name": "Дребно оборудване",
        "period": "2026-08",
        "plan_current": 5_000,
        "spent_period": 1_000,
    }
    assert rules.missing_quantity_flags([], [other_paragraph], thresholds) == []
    assert rules.missing_quantity_flags([], [has_quantity], thresholds) == []
    assert rules.missing_quantity_flags([], [below_threshold], thresholds) == []


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
    # 250k spent vs a 200k total estimate (plan 300k covers it this year):
    # the "over estimate" case now belongs to unplanned_spending.
    assert summary.counts["unplanned_spending"].new == 1
    assert summary.counts["overspend_vs_plan"].new == 0
    assert summary.tier_counts.get("violation") == 1

    session.close()


# ---------------------------------------------------------------------------
# New rules (2026-10-07): synthetic EOP/SIGMA records
# ---------------------------------------------------------------------------


def _eop(
    source_id: str,
    *,
    value: float,
    title: str = "Доставка на канцеларски материали",
    procedure: str = "CollectingOffersWithNotice",
    toc: int = 2,
    cpv: str = "30190000",
    eik: str = "111111111",
    contractor: str = "Доставчик ЕООД",
    signed: dt.datetime | None = None,
    notice: dt.datetime | None = None,
    offer_end: dt.datetime | None = None,
    tender: str | None = None,
    estimate: float | None = None,
    eu: bool = False,
    original: float | None = None,
    current: float | None = None,
    currency: int = 1,
    current_currency: int = 1,
    description: str = "",
) -> dict:
    """A signed EOP contract record shaped like `engine._procurement_to_dict`."""
    signed = signed or _dt(2025, 3, 1)
    notice = notice or (signed - dt.timedelta(days=60))
    offer_end = offer_end or (notice + dt.timedelta(days=30))
    return {
        "id": int(source_id) if source_id.isdigit() else None,
        "source": "eop",
        "source_id": source_id,
        "title": title,
        "procedure_type": procedure,
        "cpv_code": cpv,
        "contractor_name": contractor,
        "contractor_eik": eik,
        "contract_value_eur": value,
        "contract_value_bgn": None,
        "estimated_value_eur": None,
        "bids_received": None,
        "contract_date": signed,
        "published_at": notice,
        "raw_json": {
            "contract": {
                "ContractDate": _net_date(signed),
                "TenderNumber": tender or f"00126-T-{source_id}",
                "TypeOfContract": toc,
                "ContractSubject": title,
                "ContractValue": original if original is not None else value,
                "Currency": currency,
                "CurrentContractValue": current if current is not None else value,
                "CurrentContractCurrency": current_currency,
            },
            "procedure": {
                "EstimatedValue": estimate,
                "Currency": 1,
                "IsEUFinanced": eu,
                "TypeOfContract": toc,
            },
            "tender_detail": {
                "TenderName": title,
                "TenderDescription": description,
                "OfferPhaseStartDate": _net_date(notice),
                "PublicationDate": _net_date(notice),
                "OfferPhaseEndDate": _net_date(offer_end),
                "notices": [],
            },
        },
    }


def _sigma(source_id: str, *, unp: str, eik: str, value: float, bids=None, eu=False, procedure="Открита") -> dict:
    return {
        "id": None,
        "source": "sigma",
        "source_id": source_id,
        "title": "SIGMA запис",
        "procedure_type": procedure,
        "cpv_code": None,
        "contractor_name": "Доставчик ЕООД",
        "contractor_eik": eik,
        "contract_value_eur": value,
        "bids_received": bids,
        "contract_date": _dt(2025, 3, 1),
        "published_at": None,
        "raw_json": {"unp": unp, "eu_funded": eu, "procedure": procedure},
    }


def _assert_contract(flag: dict) -> None:
    """Every flag honors the shared tier/explanation/documents contract."""
    assert flag["tier"] in rules.TIERS
    assert flag["explanation"] and len(flag["explanation"]) > 80
    assert isinstance(flag["documents_json"], list) and flag["documents_json"]
    assert flag["message"] and flag["subject_key"]


# --- splitting ---------------------------------------------------------------


def test_splitting_flags_same_contractor_lower_regime_sum_over_boundary() -> None:
    thresholds = Thresholds()
    # Two обяви for supplies (each < 100,000 лв. = 51,129 €), 3 months apart,
    # summing to 60,000 € -> should have been a публично състезание.
    a = _eop("1", value=30_000, signed=_dt(2025, 1, 10))
    b = _eop("2", value=30_000, signed=_dt(2025, 4, 10), title="Доставка на тонер касети")
    flags = rules.splitting_flags([a, b], thresholds)
    assert len(flags) == 1
    flag = flags[0]
    _assert_contract(flag)
    assert flag["rule"] == "splitting"
    assert flag["tier"] == "signal"
    assert flag["severity"] == "warning"
    assert flag["details_json"]["boundary_bgn"] == 100_000
    assert flag["details_json"]["sum_eur"] == 60_000
    assert sorted(flag["details_json"]["member_ids"]) == ["eop:1", "eop:2"]
    assert "чл. 21, ал. 15" in flag["law_ref"]


def test_splitting_high_when_sum_exceeds_twice_the_boundary() -> None:
    thresholds = Thresholds()
    contracts = [_eop(str(i), value=40_000, signed=_dt(2025, 1, 1 + i)) for i in range(1, 4)]
    flags = rules.splitting_flags(contracts, thresholds)
    assert len(flags) == 1
    assert flags[0]["severity"] == "high"  # 120,000 € > 2 x 51,129 €


def test_splitting_silent_for_open_procedures_lots_renewals_and_small_sums() -> None:
    thresholds = Thresholds()
    # Open procedures: the top regime, so dividing gains nothing.
    open_a = _eop("1", value=40_000, procedure="OpenProcedure", signed=_dt(2025, 1, 1))
    open_b = _eop("2", value=40_000, procedure="OpenProcedure", signed=_dt(2025, 2, 1))
    assert rules.splitting_flags([open_a, open_b], thresholds) == []
    # Lots of ONE tender are one procurement (ЗОП чл. 21, ал. 4).
    lot_a = _eop("3", value=30_000, tender="00126-2025-0001")
    lot_b = _eop("4", value=30_000, tender="00126-2025-0001")
    assert rules.splitting_flags([lot_a, lot_b], thresholds) == []
    # Successive annual contracts (>= 300 days apart) for a recurring need.
    year1 = _eop("5", value=35_790, signed=_dt(2021, 9, 15))
    year2 = _eop("6", value=35_790, signed=_dt(2022, 9, 15))
    assert rules.splitting_flags([year1, year2], thresholds) == []
    # Sum below the boundary.
    small_a = _eop("7", value=20_000, signed=_dt(2025, 1, 1))
    small_b = _eop("8", value=20_000, signed=_dt(2025, 2, 1))
    assert rules.splitting_flags([small_a, small_b], thresholds) == []
    # Outside the 12-month window.
    far_a = _eop("9", value=30_000, signed=_dt(2023, 1, 1))
    far_b = _eop("10", value=30_000, signed=_dt(2024, 6, 1))
    assert rules.splitting_flags([far_a, far_b], thresholds) == []


def test_splitting_works_only_grouped_for_the_same_object() -> None:
    """ЗОП чл. 21, ал. 16, т. 1: separate строежи are never splitting."""
    thresholds = Thresholds()
    street_a = _eop(
        "1", value=100_000, toc=3, cpv="45233000", title='Ремонт на ул. "Искър" с.Равда',
        signed=_dt(2025, 1, 1),
    )
    street_b = _eop(
        "2", value=100_000, toc=3, cpv="45233000", title='Ремонт на ул. "Дунав" с.Равда',
        signed=_dt(2025, 2, 1),
    )
    assert rules.splitting_flags([street_a, street_b], thresholds) == []
    same_parcel = _eop(
        "3", value=100_000, toc=3, cpv="45000000",
        title="Подпорна стена в УПИ I (ПИ 53045.502.222) гр.Обзор", signed=_dt(2025, 3, 1),
    )
    same_parcel_2 = _eop(
        "4", value=100_000, toc=3, cpv="45000000",
        title="Ремонт на отоплителна инсталация, ПИ 53045.502.222, гр.Обзор", signed=_dt(2025, 4, 1),
    )
    flags = rules.splitting_flags([same_parcel, same_parcel_2], thresholds)
    assert len(flags) == 1
    assert flags[0]["details_json"]["boundary_bgn"] == 300_000


def test_splitting_cpv_path_groups_similar_titles_across_contractors() -> None:
    thresholds = Thresholds()
    title = "Абонаментна поддръжка на автоматизирани напоителни системи на територията"
    a = _eop("1", value=30_000, title=title, cpv="77314000", eik="1", signed=_dt(2025, 1, 1))
    b = _eop("2", value=30_000, title=title + " на общината", cpv="77314100", eik="2",
             signed=_dt(2025, 5, 1))
    flags = rules.splitting_flags([a, b], thresholds)
    assert len(flags) == 1
    assert flags[0]["details_json"]["path"] == "cpv"
    assert flags[0]["subject_type"] == "contract_group"


# --- annex_over_cap / annex_growth --------------------------------------------


def test_annex_over_cap_is_a_violation_and_replaces_annex_growth() -> None:
    thresholds = Thresholds()
    record = _eop("1", value=100_000, original=100_000, current=180_000)
    over = rules.annex_over_cap_flags([record], thresholds)
    assert len(over) == 1
    _assert_contract(over[0])
    assert over[0]["tier"] == "violation"
    assert over[0]["severity"] == "high"
    assert over[0]["details_json"]["growth_pct"] == 80.0
    assert "чл. 116, ал. 2" in over[0]["law_ref"]
    assert rules.annex_growth_flags([record], thresholds) == []


def test_annex_growth_below_cap_and_data_errors_ignored() -> None:
    thresholds = Thresholds()
    moderate = _eop("1", value=100_000, original=100_000, current=130_000)
    assert rules.annex_over_cap_flags([moderate], thresholds) == []
    growth = rules.annex_growth_flags([moderate], thresholds)
    assert len(growth) == 1 and growth[0]["tier"] == "signal"
    # x100 "stotinki" entry error (+9,900%): implausible, neither rule fires.
    cents = _eop("2", value=48_785.61, original=48_785.61, current=4_878_561.0)
    assert rules.annex_over_cap_flags([cents], thresholds) == []
    assert rules.annex_growth_flags([cents], thresholds) == []
    # Mixed currency codes above the cap: no violation claimed, signal only.
    mixed = _eop(
        "3", value=613_550, original=1_200_000, currency=3, current=1_033_627.9, current_currency=1
    )
    assert rules.annex_over_cap_flags([mixed], thresholds) == []
    assert len(rules.annex_growth_flags([mixed], thresholds)) == 1


# --- exceptional_procedure -----------------------------------------------------


def test_exceptional_procedure_severity_by_value_and_mapping() -> None:
    thresholds = Thresholds()
    big = _eop("1", value=600_000, procedure="NegotiatedProcedure", cpv="90510000")
    mid = _eop("2", value=150_000, procedure="DirectNegotiation", cpv="45000000", toc=3)
    small = _eop("3", value=9_000, procedure="InvitationToSpecificEconomicOperators", toc=1)
    flags = {f["subject_id"]: f for f in rules.exceptional_procedure_flags([big, mid, small], thresholds)}
    assert flags["eop:1"]["severity"] == "high"
    assert flags["eop:2"]["severity"] == "warning"
    assert flags["eop:3"]["severity"] == "info"
    for flag in flags.values():
        _assert_contract(flag)
        assert flag["tier"] == "signal"
    assert "чл. 79" in flags["eop:1"]["law_ref"] and "чл. 250а" in flags["eop:1"]["law_ref"]
    assert "чл. 182" in flags["eop:2"]["law_ref"]
    assert "чл. 191" in flags["eop:3"]["law_ref"]


def test_exceptional_procedure_commodity_exchange_fuel_is_info() -> None:
    thresholds = Thresholds()
    fuel = _eop("1", value=1_478_239, procedure="NegotiatedProcedure", cpv="09000000",
                title="Доставка на горива")
    exchange = _eop("2", value=543_309, procedure="NegotiatedProcedure", cpv="34000000",
                    description="Сделката се сключва на стокова борса по правилата на борсата.")
    flags = rules.exceptional_procedure_flags([fuel, exchange], thresholds)
    assert {f["severity"] for f in flags} == {"info"}
    assert "стокова борса" in flags[0]["explanation"]


def test_exceptional_procedure_silent_for_open_and_sigma_twins() -> None:
    thresholds = Thresholds()
    open_proc = _eop("1", value=900_000, procedure="OpenProcedure")
    negotiated = _eop("2", value=50_000, procedure="NegotiatedProcedure", tender="UNP-2")
    twin = _sigma("s2", unp="UNP-2", eik="111111111", value=50_000, procedure="Пряко / без обявление")
    sigma_only = _sigma("s3", unp="UNP-3", eik="9", value=200_000, procedure="Пряко / без обявление")
    flags = rules.exceptional_procedure_flags([open_proc, negotiated, twin, sigma_only], thresholds)
    assert sorted(f["subject_id"] for f in flags) == ["eop:2", "sigma:s3"]


# --- short_offer_deadline -----------------------------------------------------


def test_short_offer_deadline_violation_signal_and_silence() -> None:
    thresholds = Thresholds()
    notice = _dt(2025, 3, 1, 9)
    # Signed 10 days after an open-procedure notice: below even the 15-day
    # shortened minimum (ЗОП чл. 74, ал. 2/4) -> violation.
    too_fast = _eop("1", value=500_000, procedure="OpenProcedure", notice=notice,
                    offer_end=notice + dt.timedelta(days=8), signed=notice + dt.timedelta(days=10))
    # Signed 32 days after: offers barely closed, no time to evaluate -> signal.
    rushed = _eop("2", value=500_000, procedure="OpenProcedure", notice=notice,
                  offer_end=notice + dt.timedelta(days=30), signed=notice + dt.timedelta(days=32))
    # Offer period itself only 20 days (shortened, must be motivated) -> signal.
    shortened = _eop("3", value=500_000, procedure="OpenProcedure", notice=notice,
                     offer_end=notice + dt.timedelta(days=20), signed=notice + dt.timedelta(days=60))
    normal = _eop("4", value=500_000, procedure="OpenProcedure", notice=notice,
                  offer_end=notice + dt.timedelta(days=30), signed=notice + dt.timedelta(days=60))
    flags = {f["subject_id"]: f for f in rules.short_offer_deadline_flags(
        [too_fast, rushed, shortened, normal], thresholds)}
    assert flags["eop:1"]["tier"] == "violation" and flags["eop:1"]["severity"] == "high"
    assert flags["eop:2"]["tier"] == "signal"
    assert flags["eop:3"]["tier"] == "signal"
    assert "eop:4" not in flags
    _assert_contract(flags["eop:1"])
    assert "чл. 74" in flags["eop:1"]["law_ref"]


def test_short_offer_deadline_old_obyava_never_a_violation() -> None:
    """чл. 188, ал. 1's 10-day wording is in force from 22.12.2023 only."""
    thresholds = Thresholds()
    notice = _dt(2023, 6, 1, 9)
    old = _eop("1", value=40_000, procedure="CollectingOffersWithNotice", notice=notice,
               offer_end=notice + dt.timedelta(days=5), signed=notice + dt.timedelta(days=7))
    flags = rules.short_offer_deadline_flags([old], thresholds)
    assert len(flags) == 1 and flags[0]["tier"] == "signal"


# --- bid_at_ceiling -----------------------------------------------------------


def test_bid_at_ceiling_flags_single_or_unknown_bids() -> None:
    thresholds = Thresholds()
    unknown = _eop("1", value=200_000, estimate=201_000, procedure="OpenProcedure", tender="U1")
    single = _eop("2", value=600_000, estimate=600_000, procedure="OpenProcedure", tender="U2",
                  eik="2")
    twin = _sigma("s2", unp="U2", eik="2", value=600_000, bids=1)
    flags = {f["subject_id"]: f for f in rules.bid_at_ceiling_flags([unknown, single, twin], thresholds)}
    assert flags["eop:1"]["severity"] == "warning"
    assert flags["eop:1"]["details_json"]["bids_received"] is None
    assert flags["eop:2"]["severity"] == "high"
    assert flags["eop:2"]["details_json"]["bids_received"] == 1
    assert "само една оферта" in flags["eop:2"]["message"]
    _assert_contract(flags["eop:2"])


def test_bid_at_ceiling_silent_with_competition_low_ratio_or_lots() -> None:
    thresholds = Thresholds()
    competed = _eop("1", value=200_000, estimate=200_000, tender="U1", eik="1")
    twin = _sigma("s1", unp="U1", eik="1", value=200_000, bids=4)
    below = _eop("2", value=150_000, estimate=200_000, tender="U2")
    lot_a = _eop("3", value=200_000, estimate=200_000, tender="U3", eik="3")
    lot_b = _eop("4", value=100_000, estimate=200_000, tender="U3", eik="4")
    small = _eop("5", value=50_000, estimate=50_000, tender="U5")
    way_above = _eop("6", value=300_000, estimate=200_000, tender="U6")
    assert rules.bid_at_ceiling_flags(
        [competed, twin, below, lot_a, lot_b, small, way_above], thresholds
    ) == []


# --- near_threshold -----------------------------------------------------------


def test_near_threshold_flags_estimate_just_under_the_regime_boundary() -> None:
    thresholds = Thresholds()
    # обява for supplies: must stay under 100,000 лв. (51,129 €).
    near = _eop("1", value=49_900, estimate=51_000, notice=_dt(2026, 6, 1),
                signed=_dt(2026, 7, 1), tender="N1")
    far = _eop("2", value=30_000, estimate=30_000, notice=_dt(2026, 6, 1),
               signed=_dt(2026, 7, 1), tender="N2")
    old = _eop("3", value=49_900, estimate=51_000, notice=_dt(2023, 6, 1),
               signed=_dt(2023, 7, 1), tender="N3")
    top = _eop("4", value=139_000, estimate=139_500, procedure="OpenProcedure",
               notice=_dt(2026, 6, 1), signed=_dt(2026, 8, 1), tender="N4")
    flags = rules.near_threshold_flags([near, far, old, top], thresholds)
    assert [f["subject_id"] for f in flags] == ["eop-tender:N1"]
    flag = flags[0]
    _assert_contract(flag)
    assert flag["tier"] == "opacity" and flag["severity"] == "info"
    assert flag["details_json"]["boundary_bgn"] == 100_000
    # With splitting evidence for the same contract it becomes a signal.
    combined = rules.near_threshold_flags([near], thresholds, splitting_member_ids={"eop:1"})
    assert combined[0]["tier"] == "signal" and combined[0]["severity"] == "warning"
    assert "разделяне" in combined[0]["explanation"]


# --- unplanned_spending -------------------------------------------------------


def test_unplanned_spending_flags_no_plan_and_over_estimate() -> None:
    thresholds = Thresholds()
    no_plan = {"paragraph": "5200", "object_name": "Товарни автомобили", "period": "2026-08",
               "plan_current": 0, "spent_period": 358_927, "spent_prior": 0,
               "estimated_total": 358_927}
    over_est = {"paragraph": "5100", "object_name": "Саниране на читалище", "period": "2026-08",
                "plan_current": 98_108, "spent_period": 392_005, "spent_prior": 0,
                "estimated_total": 118_459}
    tiny = {"paragraph": "5200", "object_name": "Климатик", "period": "2026-08",
            "plan_current": 0, "spent_period": 601, "spent_prior": 0, "estimated_total": 601}
    fine = {"paragraph": "5200", "object_name": "Наред", "period": "2026-08",
            "plan_current": 50_000, "spent_period": 40_000, "spent_prior": 0,
            "estimated_total": 60_000}
    flags = {f["details_json"]["object_name"]: f for f in rules.unplanned_spending_flags(
        [no_plan, over_est, tiny, fine], thresholds)}
    assert set(flags) == {"Товарни автомобили", "Саниране на читалище"}
    flag = flags["Товарни автомобили"]
    _assert_contract(flag)
    assert flag["tier"] == "violation" and flag["severity"] == "warning"
    assert flag["subject_id"].endswith(":2026")
    assert "чл. 128" in flag["law_ref"]
    assert any("чл. 124" in d for d in flag["documents_json"])
    assert flags["Саниране на читалище"]["details_json"]["reasons"] == ["over_estimate"]
    # ... and overspend_vs_plan does not duplicate the over-estimate object.
    assert rules.overspend_vs_plan_flags([over_est], thresholds) == []


# --- missing_monthly_report / missing_annual_report ---------------------------


def test_missing_report_flags_monthly_and_annual() -> None:
    thresholds = Thresholds(reports_first_period="2024-11")
    now = _dt(2026, 2, 10)
    # Present: 2024-11, 2025-01..2025-11 (B1). Missing: 2024-12 (annual, since
    # 31.03.2025 has passed) and 2025-12 (annual not yet due -> monthly).
    reports = [("2024-11", "B1")] + [(f"2025-{m:02d}", "B1") for m in range(1, 12)]
    reports += [("2025-12", "IB1_DES")]  # an annex is not the B1 report itself
    flags = rules.missing_report_flags(reports, thresholds, now=now)
    keys = {(f["rule"], f["subject_key"]) for f in flags}
    assert keys == {
        ("missing_annual_report", "annual:2024"),
        ("missing_monthly_report", "report:2025-12"),
    }
    for flag in flags:
        _assert_contract(flag)
    annual = next(f for f in flags if f["rule"] == "missing_annual_report")
    assert annual["tier"] == "signal"
    monthly = next(f for f in flags if f["rule"] == "missing_monthly_report")
    assert monthly["tier"] == "opacity" and monthly["severity"] == "info"
    assert "чл. 15а, ал. 4 ЗДОИ" in monthly["law_ref"]


def test_missing_report_flags_silent_when_complete() -> None:
    thresholds = Thresholds(reports_first_period="2025-01")
    reports = [(f"2025-{m:02d}", "B1") for m in range(1, 13)] + [("2026-01", "B3")]
    assert rules.missing_report_flags(reports, thresholds, now=_dt(2026, 3, 5)) == []


# --- eu_funded_irregularity ----------------------------------------------------


def test_eu_funded_irregularity_needs_eu_funding_and_another_flag() -> None:
    eu_flagged = _eop("1", value=600_000, procedure="NegotiatedProcedure", eu=True)
    eu_clean = _eop("2", value=600_000, procedure="OpenProcedure", eu=True)
    flagged_not_eu = _eop("3", value=600_000, procedure="NegotiatedProcedure")
    sigma_eu_twin = _sigma("s3", unp="00126-T-3", eik="111111111", value=600_000, eu=True)
    insurance = _eop("4", value=600_000, procedure="NegotiatedProcedure",
                     description="Застраховката покрива събития на територията на Република "
                     "България и Европейския съюз.")
    contracts = [eu_flagged, eu_clean, flagged_not_eu, sigma_eu_twin, insurance]
    thresholds = Thresholds()
    index = rules.build_index(contracts)
    base = rules.exceptional_procedure_flags(contracts, thresholds, index)
    flags = rules.eu_funded_irregularity_flags(base, index)
    by_id = {f["subject_id"]: f for f in flags}
    # eop:1 (IsEUFinanced) and eop:3 (EU-funded per its SIGMA twin) only;
    # "Европейския съюз" in an insurance clause is not EU funding.
    assert set(by_id) == {"eop:1", "eop:3"}
    _assert_contract(by_id["eop:1"])
    assert by_id["eop:1"]["details_json"]["other_rules"] == ["exceptional_procedure"]
    assert "OLAF" in by_id["eop:1"]["explanation"]
    assert "248а" in by_id["eop:1"]["law_ref"]


# --- price_unverifiable -------------------------------------------------------


def test_price_unverifiable_flags_title_only_supply_or_works() -> None:
    thresholds = Thresholds()
    works = _eop("1", value=150_000, toc=3, cpv="45000000", procedure="PublicCompetition",
                 title="Ремонт на сграда")
    supply = _eop("2", value=30_000, title="Доставка на обзавеждане")
    flags = {f["subject_id"]: f for f in rules.price_unverifiable_flags([works, supply], thresholds)}
    assert flags["eop:1"]["severity"] == "warning"
    assert flags["eop:2"]["severity"] == "info"
    assert flags["eop:1"]["tier"] == "opacity"
    assert "не може да се прецени дали цената е обоснована" in flags["eop:1"]["message"]
    assert "количествено-стойностна сметка" in flags["eop:1"]["documents_json"]
    _assert_contract(flags["eop:1"])
    # The broader rule wins: missing_quantity never duplicates it.
    assert rules.missing_quantity_flags([supply], [], thresholds) == []


def test_price_unverifiable_silent_with_description_quantity_services_or_unit_prices() -> None:
    thresholds = Thresholds()
    described = _eop("1", value=150_000, description=_DESCRIPTION_NO_QUANTITY)
    counted = _eop("2", value=150_000, title="Доставка на 20 броя компютри")
    services = _eop("3", value=150_000, toc=1, title="Застраховка на автомобили")
    unit_priced = _eop("4", value=150_000, title="Доставка по единични цени на материали")
    repeated_title = _eop("5", value=150_000, title="Доставка на обзавеждане за детска градина",
                          description="Доставка на обзавеждане за детска градина")
    assert rules.price_unverifiable_flags(
        [described, counted, services, unit_priced], thresholds
    ) == []
    # A "description" that only repeats the title is no description.
    assert len(rules.price_unverifiable_flags([repeated_title], thresholds)) == 1


# --- engine: tiers persisted, meta last, migration ------------------------------


def test_run_full_analysis_persists_tier_explanation_documents() -> None:
    session = _memory_session()
    session.add(
        Procurement(
            source="eop",
            source_id="77",
            title="Доставка на горива",
            procedure_type="NegotiatedProcedure",
            cpv_code="45000000",
            contractor_name="X EOOD",
            contractor_eik="111",
            contract_value_eur=600_000,
            contract_date=_dt(2025, 1, 1),
            raw_json={
                "contract": {"TenderNumber": "U77", "TypeOfContract": 1},
                "procedure": {"IsEUFinanced": True},
                "tender_detail": {},
            },
        )
    )
    session.add(BudgetReport(period="2026-01", kind="B1"))
    session.commit()

    summary = run_full_analysis(session, now=_dt(2026, 3, 10))
    session.commit()
    assert summary.counts["exceptional_procedure"].produced == 1
    assert summary.counts["eu_funded_irregularity"].produced == 1
    assert summary.counts["missing_monthly_report"].produced > 0
    assert summary.tier_counts["signal"] >= 2

    flags = session.scalars(select(Flag)).all()
    for flag in flags:
        assert flag.tier in rules.TIERS
        assert flag.explanation
        assert flag.documents_json
    meta = next(f for f in flags if f.rule == "eu_funded_irregularity")
    assert meta.subject_key == "eop:77"
    session.close()


def test_migrate_flags_table_adds_contract_columns_idempotently(tmp_path) -> None:
    from sqlalchemy import text

    from nessebar_budget.db.migrate import migrate_flags_table

    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE flags (id INTEGER PRIMARY KEY, procurement_id INTEGER, rule VARCHAR(128) "
            "NOT NULL, severity VARCHAR(16) NOT NULL, message TEXT NOT NULL, created_at DATETIME "
            "NOT NULL, notified_at DATETIME)"
        ))
    migrate_flags_table(engine)
    migrate_flags_table(engine)  # second run is a no-op
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(flags)"))}
    assert {
        "tier", "explanation", "documents_json", "subject_key", "law_ref", "sources_json"
    } <= columns


# ---------------------------------------------------------------------------
# analysis.provenance: the "Източници" of every flag
# ---------------------------------------------------------------------------

_DEC_2022_REPORT = {
    "id": 472,
    "period": "2022-12",
    "kind": "capital_xlsx",
    "url": "https://www.nesebar.bg/03-2019/otchet2022dec5206.xlsx",
    "file_path": "data/cache/nesebar_site/2022/12/otchet2022dec5206.xlsx",
}


def _karavelov_row(**extra) -> dict:
    """The real Dec 2022 line item (BGN file: plan 102 376 лв., spent 627 983 лв.)."""
    return {
        "period": "2022-12",
        "unit": "Общо",
        "paragraph": "5100",
        "object_name": 'Реконструкция на СУ "Л.Каравелов" - филиал в Несебър-стар град',
        "estimated_total": 52663.06,
        "spent_prior": 0.0,
        "plan_current": 52344.02,
        "spent_period": 321082.61,
        "extra_json": {
            "row_type": "object",
            "original_currency": "BGN",
            "conversion_rate": 1.95583,
            **extra,
        },
    }


def test_budget_row_source_with_sheet_and_row() -> None:
    src = provenance.budget_row_source(
        _karavelov_row(source_sheet="Общо", source_row=19), _DEC_2022_REPORT
    )
    assert src["kind"] == "budget_file"
    assert src["url"] == _DEC_2022_REPORT["url"]
    assert src["file"] == "otchet2022dec5206.xlsx"
    assert (src["sheet"], src["row"], src["period"]) == ("Общо", 19, "2022-12")
    assert "декември 2022" in src["label"]
    fields = {f["name"]: f for f in src["fields"]}
    # Leva figures computed back from EUR (x 1.95583) land on the published whole leva.
    assert fields["Уточнен план"]["value"] == 102376
    assert fields["Уточнен план"]["value_eur"] == 52344.02
    assert fields["Усвоено към отчетния период"]["value"] == 627983
    assert fields["Сметна стойност"]["value"] == 103000
    assert {f["currency"] for f in src["fields"]} == {"BGN"}
    assert "Номерът на реда" not in (src["note"] or "")


def test_budget_row_source_without_source_row_falls_back_to_unit_sheet() -> None:
    src = provenance.budget_row_source(_karavelov_row(), _DEC_2022_REPORT, fields=("plan_current",))
    assert src["sheet"] == "Общо"  # the worksheet title, stored as `unit`
    assert src["row"] is None
    assert [f["name"] for f in src["fields"]] == ["Уточнен план"]
    assert "Номерът на реда не е записан" in src["note"]
    # No report known (e.g. an orphan line item) and an EUR-native 2026 row.
    eur_row = {**_karavelov_row(), "period": "2026-03", "extra_json": {"row_type": "object"}}
    src = provenance.budget_row_source(eur_row, None)
    assert src["url"] is None and src["file"] is None
    plan = next(f for f in src["fields"] if f["name"] == "Уточнен план")
    assert plan["value"] == plan["value_eur"] == 52344.02
    assert plan["currency"] == "EUR"


def test_eur_to_bgn_keeps_cents_when_not_a_whole_lev() -> None:
    assert provenance.eur_to_bgn(321082.61) == 627983
    assert provenance.eur_to_bgn(100.0) == 195.58
    assert provenance.eur_to_bgn(None) is None


def test_contract_sources_eop_without_sigma() -> None:
    record = _eop("5", value=184065.08, original=360000, currency=3, estimate=360000,
                  tender="00126-2023-0042")
    record["url"] = "https://app.eop.bg/today/299573"
    record["raw_json"]["contract"].update(
        {"ContractNumber": "5", "SupplierName": "Доставчик ЕООД", "RegisterNumberList": "111"}
    )
    record["raw_json"]["procedure"]["Currency"] = 3
    sources = provenance.contract_sources(record)
    assert [s["kind"] for s in sources] == ["eop_contract", "eop_tender"]
    contract, tender = sources
    assert contract["url"] == "https://app.eop.bg/today/299573"
    assert "00126-2023-0042" in contract["label"]
    assert "GetContractsByOrganization" in contract["note"]
    fields = {f["name"]: f for f in contract["fields"]}
    assert fields["ContractValue"]["value"] == 360000
    assert fields["ContractValue"]["currency"] == "BGN"
    assert fields["ContractValue"]["value_eur"] == 184065.08
    assert fields["ContractDate"]["value"] == "2025-03-01"
    assert fields["SupplierName"]["value"] == "Доставчик ЕООД"
    estimate = next(f for f in tender["fields"] if f["name"] == "EstimatedValue")
    assert estimate["value"] == 360000 and estimate["value_eur"] == 184065.08
    assert "GetProcurementsByOrganization" in tender["note"]


def test_contract_sources_with_sigma_bids() -> None:
    thresholds = Thresholds()
    eop = _eop("2", value=600_000, estimate=600_000, procedure="OpenProcedure", tender="U2", eik="2")
    twin = _sigma("s2", unp="U2", eik="2", value=600_000, bids=1)
    sources = provenance.contract_sources(eop, sigma=twin)
    sigma = sources[-1]
    assert sigma["kind"] == "sigma"
    assert sigma["url"] == "https://sigma.midt.bg/contracts.csv?authority=000057122"
    assert "s2" in sigma["note"]
    assert {f["name"]: f["value"] for f in sigma["fields"]} == {"id": "s2", "unp": "U2", "bids_received": 1}
    # The rule cites the SIGMA twin because SIGMA supplied the bid count.
    flag = rules.bid_at_ceiling_flags([eop, twin], thresholds)[0]
    assert [s["kind"] for s in flag["sources"]] == ["eop_contract", "eop_tender", "sigma"]
    # A SIGMA-only record is its own (single) source.
    only = provenance.contract_sources(twin, sigma_fields=("id", "bids_received"))
    assert [s["kind"] for s in only] == ["sigma"]


def test_every_rule_attaches_sources() -> None:
    thresholds = Thresholds()
    reports = [("2025-12", "B1")]
    flags = rules.missing_report_flags(
        reports, thresholds, now=_dt(2026, 3, 10), files_by_period={"2026-01": ["kr.xlsx"]}
    )
    january = next(f for f in flags if f["subject_id"] == "report:2026-01")
    (page,) = january["sources"]
    assert page["kind"] == "reports_page"
    assert page["url"] == "https://www.nesebar.bg/reports.html"
    assert "kr.xlsx" in page["note"]
    jumps = rules.plan_jump_flags(
        {("5200", "Обект"): [
            {"period": "2026-02", "plan_current": 100_000, "object_name": "Обект"},
            {"period": "2026-03", "plan_current": 300_000, "object_name": "Обект"},
        ]},
        thresholds,
        dataset_first_period="2026-01",
    )
    jump = next(f for f in jumps if "->" in f["subject_id"])
    assert [s["period"] for s in jump["sources"]] == ["2026-02", "2026-03"]


def test_run_full_analysis_persists_sources_json() -> None:
    session = _memory_session()
    report = BudgetReport(**{k: v for k, v in _DEC_2022_REPORT.items() if k != "id"})
    session.add(report)
    session.flush()
    row = _karavelov_row(source_sheet="Общо", source_row=19)
    session.add(BudgetLineItem(report_id=report.id, currency="EUR", **row))
    session.add(
        Procurement(
            source="eop",
            source_id="77",
            title="Доставка",
            procedure_type="NegotiatedProcedure",
            contractor_name="X EOOD",
            contractor_eik="111",
            contract_value_eur=600_000,
            contract_date=_dt(2025, 1, 1),
            url="https://app.eop.bg/today/595341",
            raw_json={
                "contract": {"ContractNumber": "77", "TenderNumber": "U77", "TypeOfContract": 1},
                "procedure": {"IsEUFinanced": True, "SpecialNumber": "U77"},
                "tender_detail": {},
            },
        )
    )
    session.commit()

    run_full_analysis(session, now=_dt(2026, 3, 10))
    session.commit()
    flags = session.scalars(select(Flag)).all()
    assert flags and all(f.sources_json for f in flags)

    unplanned = next(f for f in flags if f.rule == "unplanned_spending")
    (src,) = unplanned.sources_json
    assert src["url"] == _DEC_2022_REPORT["url"]
    assert (src["sheet"], src["row"]) == ("Общо", 19)
    values = {f["name"]: f["value"] for f in src["fields"]}
    assert values["Уточнен план"] == 102376 and values["Усвоено към отчетния период"] == 627983

    exceptional = next(f for f in flags if f.rule == "exceptional_procedure")
    assert exceptional.sources_json[0]["url"] == "https://app.eop.bg/today/595341"
    # The meta rule cites the flag it is built on, by permalink.
    meta = next(f for f in flags if f.rule == "eu_funded_irregularity")
    linked = [s for s in meta.sources_json if s["kind"] == "flag"]
    assert linked and linked[0]["url"] == f"flags/{exceptional.id}.html"
    assert any(
        f["name"] == "IsEUFinanced" for s in meta.sources_json for f in s.get("fields", [])
    )
    session.close()


def test_plan_jump_never_compares_across_years_or_flags_a_year_s_first_file() -> None:
    """Each year's capital ledger is a new annual budget: Dec 2022 -> Mar 2023
    is not a mid-year jump, and an object first seen in the first available
    file of a year (here 2023-03: Jan/Feb 2023 not published) is part of
    that year's initial plan. Within one year both cases still flag."""
    thresholds = Thresholds()

    def row(period: str, plan: float, name: str) -> dict:
        return {"period": period, "plan_current": plan, "object_name": name, "paragraph": "5100"}

    history = {
        # 100k in Dec 2022, 600k in the 2023 budget: new year, not a jump.
        ("5100", "Пренесен обект"): [row("2022-12", 100_000, "Пренесен обект"),
                                     row("2023-03", 600_000, "Пренесен обект")],
        # First seen in 2023's first available file with a big plan: not mid-year.
        ("5100", "Обект от началото на 2023"): [row("2023-03", 900_000, "Обект от началото на 2023")],
        # Same-year jump and a genuine mid-year newcomer: still flagged.
        ("5100", "Скок"): [row("2023-03", 100_000, "Скок"), row("2023-04", 600_000, "Скок")],
        ("5100", "Нов през годината"): [row("2023-05", 900_000, "Нов през годината")],
        ("5100", "Друг"): [row("2022-11", 1_000, "Друг"), row("2022-12", 1_000, "Друг"),
                           row("2023-04", 1_000, "Друг")],
    }
    flags = rules.plan_jump_flags(history, thresholds, dataset_first_period="2022-11")
    keys = {f["subject_key"] for f in flags}
    assert keys == {
        "5100:Скок:2023-03->2023-04",
        "5100:Нов през годината:new",
    }
    new = next(f for f in flags if f["subject_key"].endswith(":new"))
    assert new["details_json"]["year_first_period"] == "2023-03"
