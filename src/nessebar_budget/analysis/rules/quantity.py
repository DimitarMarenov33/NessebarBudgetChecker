"""`missing_quantity` and `price_unverifiable` -- "can a citizen check the price?"

A citizen reading "we bought new pens for the offices" at a 185,000 EUR spend
has no way to check the unit price -- 4 pens and a single t-shirt are
consistent with that invoice unless an exact number of objects purchased is
published somewhere.

The two rules are split so a contract never gets both (documented in
`docs/RULES.md`):

- `price_unverifiable` (opacity) -- the broad case: a supply or works contract
  (TypeOfContract 2 or 3) >= 20,000 EUR with NO quantity, NO substantive
  technical description (every published description, minus the title, is
  shorter than 80 chars) and NO unit-price/framework wording. Only a title and
  a total sum are public.
- `missing_quantity` (opacity) -- the quantity-only case: a supply contract
  (TypeOfContract 2) that *does* publish a description, but no count/volume;
  plus Scope B, § 52 capital budget objects whose name states no count.
"""

from __future__ import annotations

import re
from typing import Any

from nessebar_budget.analysis.rules._common import (
    TIER_OPACITY,
    _clean_html,
    _norm_text,
    _short_title,
    contract_subject_id,
    eop_parts,
    eur,
    is_signed,
    make_flag,
)
from nessebar_budget.analysis.rules.linking import description_texts
from nessebar_budget.analysis.thresholds import Thresholds

#: `raw_json.contract.TypeOfContract` codes, verified against every sampled
#: EOP contract in `data/nessebar.db`: 1 = услуги, 2 = доставки,
#: 3 = строителство. See docs/RULES.md.
_MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT = 2
_PRICE_UNVERIFIABLE_TYPES = (2, 3)

#: § 52 "Придобиване на дълготрайни активи" -- the only paragraph Scope B of
#: `missing_quantity` looks at.
_MISSING_QUANTITY_BUDGET_PARAGRAPH = "5200"

#: A number immediately followed by a unit of count/volume/area/weight, e.g.
#: "20 броя", "15 бр.", "2 000 т", "500 кв.м", optionally with a spelled-out
#: number in parens/slashes in between ("10 /десет/ броя"). Deliberately
#: covers only units that denote *counted objects* (not money/time-of-day
#: words like "лева"/"часовник") -- see docs/RULES.md for the positive/
#: negative test cases this was tuned against.
_QUANTITY_UNIT_RE = (
    r"(?:"
    r"бр\.?|броя|брой"
    r"|компл(?:ект(?:а|и)?|\.)"
    r"|к-та"
    r"|тон(?:а|ове)?|т\.?"
    r"|кв\.?\s?м\.?"
    r"|куб\.?\s?м\.?"
    r"|лин\.?\s?м\.?"
    r"|м²|м³"
    r"|кг\.?"
    r"|литра|л\.?"
    r"|м\.?"
    r"|дка"
    r"|опаковк[аи]"
    r"|чифт(?:а|ове)?"
    r"|единиц[аи]"
    r"|час(?:а|ове)?"
    r")"
)
_QUANTITY_FILLER_RE = r"(?:\s*[(/][^)/\n]{0,20}[)/])?"
_QUANTITY_NUM_RE = r"(?:\d[\d\s .,]*\d|\d)"
_QUANTITY_RE = re.compile(
    rf"{_QUANTITY_NUM_RE}{_QUANTITY_FILLER_RE}\s*{_QUANTITY_UNIT_RE}(?![а-яА-Я])",
    re.IGNORECASE,
)
#: "20 х 30" / "х 20" style dimension/multiplication notation.
_QUANTITY_NXN_RE = re.compile(r"\d\s*[хx]\s*\d", re.IGNORECASE)

#: Phrases indicating quantities are *deliberately* left open (a framework
#: agreement / per-call-off ordering against written requests / unit-price
#: billing) rather than simply omitted -- these are not flagged, only noted.
_FRAMEWORK_RAMKOVO_RE = re.compile(r"рамков", re.IGNORECASE)
_FRAMEWORK_EDINICHNI_TSENI_RE = re.compile(r"единични\s*цени", re.IGNORECASE)
_FRAMEWORK_ZAYAVKA_RE = re.compile(r"заявк", re.IGNORECASE)
_FRAMEWORK_PERIODICHNO_RE = re.compile(r"периодично\s*възлагане", re.IGNORECASE)
_FRAMEWORK_PROGNOZEN_RE = re.compile(r"прогнозн", re.IGNORECASE)
_FRAMEWORK_ORIENTIR_RE = re.compile(r"ориентировъчн|индикативн", re.IGNORECASE)
_FRAMEWORK_KOLICHESTVO_RE = re.compile(r"количеств", re.IGNORECASE)


def _has_quantity(text: str) -> bool:
    """True if `text` states a number-of-objects quantity anywhere."""
    return bool(_QUANTITY_RE.search(text) or _QUANTITY_NXN_RE.search(text))


def _is_framework_defined(text: str) -> bool:
    """True if `text` says quantities are intentionally open-ended (framework
    agreement / per-call-off ordering / unit pricing) rather than omitted.
    """
    if _FRAMEWORK_RAMKOVO_RE.search(text) or _FRAMEWORK_EDINICHNI_TSENI_RE.search(text):
        return True
    if _FRAMEWORK_ZAYAVKA_RE.search(text) or _FRAMEWORK_PERIODICHNO_RE.search(text):
        return True
    return bool(_FRAMEWORK_KOLICHESTVO_RE.search(text)) and bool(
        _FRAMEWORK_PROGNOZEN_RE.search(text) or _FRAMEWORK_ORIENTIR_RE.search(text)
    )


def _missing_quantity_severity(value: float, thresholds: Thresholds) -> str:
    if value >= thresholds.missing_quantity_high_eur:
        return "high"
    if value >= thresholds.missing_quantity_warning_eur:
        return "warning"
    return "info"


def _substantive_description_chars(record: dict[str, Any]) -> int:
    """Length of the longest published description once the title (and the
    tender name) is removed from it -- a "description" that only repeats the
    title is no description."""
    contract, procedure, detail = eop_parts(record)
    titles = {
        _norm_text(t)
        for t in (
            record.get("title"),
            contract.get("ContractSubject"),
            procedure.get("TenderName"),
            detail.get("TenderName"),
        )
        if t
    }
    best = 0
    for text in description_texts(record):
        norm = _norm_text(text)
        for title in titles:
            if title:
                norm = norm.replace(title, " ")
        best = max(best, len(norm.strip(" .,;:-–\"„“”")))
    return best


def _assess_contract(record: dict[str, Any]) -> dict[str, Any] | None:
    """Quantity/description facts for a signed EOP contract, or None if the
    record is out of scope (not EOP, not signed, no contract sub-object)."""
    if record.get("source") != "eop" or not is_signed(record):
        return None
    contract, _, detail = eop_parts(record)
    if not contract:
        return None
    text_sources = (
        ("title", record.get("title")),
        ("tender_description", detail.get("TenderDescription")),
        ("notice_text", detail.get("notice_text")),
    )
    quantity_found_in = "none"
    for field_name, raw_text in text_sources:
        if raw_text and _has_quantity(_clean_html(raw_text)):
            quantity_found_in = field_name
            break
    combined = " ".join(_clean_html(t) for _, t in text_sources if t)
    return {
        "type_of_contract": contract.get("TypeOfContract"),
        "value": float(record.get("contract_value_eur") or 0),
        "quantity_found_in": quantity_found_in,
        "framework": _is_framework_defined(combined),
        "description_chars": _substantive_description_chars(record),
    }


def _is_price_unverifiable(facts: dict[str, Any], thresholds: Thresholds) -> bool:
    return (
        facts["type_of_contract"] in _PRICE_UNVERIFIABLE_TYPES
        and facts["value"] >= thresholds.missing_quantity_min_value_eur
        and facts["quantity_found_in"] == "none"
        and not facts["framework"]
        and facts["description_chars"] < thresholds.price_unverifiable_min_description_chars
    )


def price_unverifiable_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`price_unverifiable` (opacity): see module docstring. info < 100k,
    warning >= 100k EUR."""
    out: list[dict[str, Any]] = []
    for record in contracts:
        facts = _assess_contract(record)
        if facts is None or not _is_price_unverifiable(facts, thresholds):
            continue
        value = facts["value"]
        works = facts["type_of_contract"] == 3
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        documents = [
            "техническа спецификация",
            "ценово предложение на изпълнителя с единични цени",
            "фактури с количества и единични цени",
            "приемо-предавателни протоколи",
        ]
        if works:
            documents.insert(1, "количествено-стойностна сметка")
            documents.append("актове за установяване на извършените СМР")
        out.append(
            make_flag(
                rule="price_unverifiable",
                tier=TIER_OPACITY,
                severity=(
                    "warning" if value >= thresholds.price_unverifiable_warning_eur else "info"
                ),
                message=(
                    f"Тази покупка може да сочи към нередност: за договора с {contractor} "
                    f"„{title}“ ({eur(value)}) не са публикувани количества и спецификации, така "
                    "че не може да се прецени дали цената е обоснована."
                ),
                explanation=(
                    f"За {'това строителство' if works else 'тази доставка'} в публикуваните данни "
                    "има само заглавие и обща сума — без количества, без съдържателно техническо "
                    "описание и без единични цени. Законът изисква техническите спецификации да "
                    "позволяват точно определяне на предмета на поръчката (чл. 48, ал. 1 ЗОП); без "
                    "тях сумата не може да се сравни с пазарните цени и може да прикрива завишаване. "
                    "Възможно е документите да съществуват като приложения, които не обработваме — "
                    "затова следва да се поискат."
                ),
                documents=documents,
                subject_type="contract",
                subject_id=contract_subject_id(record),
                details={
                    "source_id": record.get("source_id"),
                    "contract_value_eur": value,
                    "type_of_contract": facts["type_of_contract"],
                    "description_chars": facts["description_chars"],
                    "min_description_chars": thresholds.price_unverifiable_min_description_chars,
                    "reasons": ["no_quantity", "no_technical_description", "no_unit_prices"],
                },
                law_ref=thresholds.price_unverifiable_law_ref,
                procurement_id=record.get("id"),
            )
        )
    return out


def _missing_quantity_contract_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """Scope A: a signed EOP supply contract (TypeOfContract == 2) >= the
    minimum value with no quantity in `title` / `TenderDescription` /
    `notice_text` (searched in that order; `quantity_found_in` records which),
    no framework/unit-price wording -- and NOT already `price_unverifiable`
    (i.e. a description is published; only the count is missing).
    """
    min_value = thresholds.missing_quantity_min_value_eur
    out: list[dict[str, Any]] = []

    for record in contracts:
        facts = _assess_contract(record)
        if facts is None:
            continue
        if facts["type_of_contract"] != _MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT:
            continue
        value = facts["value"]
        if not value or value < min_value:
            continue
        if facts["quantity_found_in"] != "none" or facts["framework"]:
            continue
        if _is_price_unverifiable(facts, thresholds):
            continue  # the broader rule covers it -- never both

        contract, _, _ = eop_parts(record)
        title = _short_title(record.get("title") or contract.get("ContractSubject"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            make_flag(
                rule="missing_quantity",
                tier=TIER_OPACITY,
                severity=_missing_quantity_severity(value, thresholds),
                message=(
                    f"Договорът с {contractor} за „{title}“ ({eur(value)}) не посочва "
                    "количество на закупеното — така не може да се прецени дали цената е "
                    "обоснована."
                ),
                explanation=(
                    "В публикуваните данни има описание на предмета, но липсва брой, обем или "
                    "количество. Без количество не може да се изчисли единичната цена и да се "
                    "сравни с пазарната — същата сума може да е справедлива или силно завишена. "
                    "Количеството често е в техническата спецификация или в ценовото предложение, "
                    "които не са в обработваните от нас данни."
                ),
                documents=[
                    "техническа спецификация",
                    "ценово предложение на изпълнителя с единични цени",
                    "фактури с количества и единични цени",
                    "приемо-предавателни протоколи",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                details={
                    "source_id": record.get("source_id", "?"),
                    "contract_value_eur": value,
                    "type_of_contract": _MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT,
                    "min_value_eur": round(min_value, 2),
                    "quantity_found_in": facts["quantity_found_in"],
                    "description_chars": facts["description_chars"],
                },
                law_ref=thresholds.missing_quantity_law_ref,
                procurement_id=record.get("id"),
            )
        )
    return out


def _missing_quantity_budget_flags(
    objects_latest: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """Scope B: a § 52 (придобиване на ДМА) capital budget object at the latest
    reporting period, with `plan_current` or `spent_period` >= the minimum,
    whose (short) `object_name` states no quantity. Much lower precision than
    Scope A -- ledger names essentially never state a count. See docs/RULES.md.
    """
    min_value = thresholds.missing_quantity_min_value_eur
    out: list[dict[str, Any]] = []

    for obj in objects_latest:
        if obj.get("paragraph") != _MISSING_QUANTITY_BUDGET_PARAGRAPH:
            continue
        plan = float(obj.get("plan_current") or 0)
        spent = float(obj.get("spent_period") or 0)
        value = max(plan, spent)
        if value < min_value:
            continue

        name = obj.get("object_name") or "обект"
        if _has_quantity(name) or _is_framework_defined(name):
            continue

        subject_id = f"{obj.get('paragraph') or '?'}:{name}"
        basis, basis_value = ("план", plan) if plan >= spent else ("разход", spent)
        out.append(
            make_flag(
                rule="missing_quantity",
                tier=TIER_OPACITY,
                severity=_missing_quantity_severity(value, thresholds),
                message=(
                    f"Бюджетният обект „{name}“ ({basis} {eur(basis_value)}) не посочва брой "
                    "или количество на придобиваните активи — така не може да се прецени дали "
                    "цената е обоснована."
                ),
                explanation=(
                    "В отчета за капиталовите разходи този обект е описан само с кратко име, без "
                    "брой или количество. Без тях не може да се изчисли цената за единица и да се "
                    "провери дали е разумна. Отчетът по закон съдържа само кратки наименования, "
                    "затова броят следва да се търси във фактурите и договора за доставка."
                ),
                documents=[
                    "фактури с количества и единични цени",
                    "договор за доставка",
                    "приемо-предавателни протоколи",
                ],
                subject_type="budget_object",
                subject_id=subject_id,
                details={
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "plan_current": plan,
                    "spent_period": spent,
                    "min_value_eur": round(min_value, 2),
                },
                law_ref=thresholds.missing_quantity_budget_law_ref,
            )
        )
    return out


def missing_quantity_flags(
    contracts: list[dict[str, Any]],
    objects_latest: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`missing_quantity` (opacity): Scope A (EOP supply contracts that publish
    a description but no count) + Scope B (§ 52 capital budget objects)."""
    return _missing_quantity_contract_flags(contracts, thresholds) + _missing_quantity_budget_flags(
        objects_latest, thresholds
    )
