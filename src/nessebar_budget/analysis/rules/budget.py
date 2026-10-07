"""Capital-budget-ledger rules: `unplanned_spending` (violation),
`overspend_vs_plan`, `plan_jump`, `unmatched_spending` (signals).

Inputs are capital-ledger objects ("Разчет за финансиране на капиталовите
разходи", `db.budget_models.BudgetLineItem`, unit == "Общо", row_type ==
"object"), as flat dicts with `period`, `paragraph`, `object_name`,
`plan_current`, `spent_period` (cumulative this year), `spent_prior`,
`estimated_total`.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from nessebar_budget.analysis.matching import MatchCandidate, find_best_match
from nessebar_budget.analysis.rules._common import (
    TIER_SIGNAL,
    TIER_VIOLATION,
    eur,
    make_flag,
)
from nessebar_budget.analysis.thresholds import Thresholds

_BUDGET_CHANGE_DOCUMENTS = [
    "решение на общинския съвет за промяна на бюджета (чл. 124 ЗПФ)",
    "заповеди на кмета за компенсирани промени (чл. 125 ЗПФ)",
    "фактури и платежни нареждания по обекта",
    "актуализиран разчет за капиталовите разходи",
]


def _object_subject(obj: dict[str, Any]) -> str:
    return f"{obj.get('paragraph') or '?'}:{obj.get('object_name') or 'обект'}"


# ---------------------------------------------------------------------------
# unplanned_spending (violation)
# ---------------------------------------------------------------------------


def _unplanned_reasons(obj: dict[str, Any], thresholds: Thresholds) -> list[str]:
    """Why an object's spending looks unplanned: "no_plan" (spent with a zero/
    missing annual plan) and/or "over_estimate" (cumulative spending above the
    object's total estimated cost)."""
    plan = float(obj.get("plan_current") or 0)
    spent = float(obj.get("spent_period") or 0)
    cumulative = float(obj.get("spent_prior") or 0) + spent
    estimated_total = float(obj.get("estimated_total") or 0)
    reasons: list[str] = []
    if plan <= 0 and spent >= thresholds.unplanned_min_spent_eur:
        reasons.append("no_plan")
    if estimated_total > 0 and cumulative - estimated_total >= thresholds.unplanned_min_excess_eur:
        reasons.append("over_estimate")
    return reasons


def unplanned_spending_flags(
    objects_year_end: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`unplanned_spending` (violation candidate, warning): for each object at
    the latest reported period *of each year*, spending with no annual plan
    (`plan_current` 0/None, spent >= `unplanned_min_spent_eur`) and/or
    cumulative spending above `estimated_total` (by >= `unplanned_min_excess_eur`).

    ЗПФ чл. 128, ал. 1: "Не се допуска извършването на разходи ... както и
    започването на програми или проекти, които не са предвидени в годишния
    бюджет на общината."; чл. 102, ал. 1 (no spending above the approved
    budget). It becomes lawful with a council decision under чл. 124, ал. 2 --
    which is the first document to request. Using each year's *last* report
    means a plan added later in the year (the normal fix) resolves the flag.
    Subject key is per object per year.
    """
    out: list[dict[str, Any]] = []
    for obj in objects_year_end:
        reasons = _unplanned_reasons(obj, thresholds)
        if not reasons:
            continue
        name = obj.get("object_name") or "обект"
        period = obj.get("period") or "?"
        year = period[:4]
        plan = float(obj.get("plan_current") or 0)
        spent = float(obj.get("spent_period") or 0)
        cumulative = float(obj.get("spent_prior") or 0) + spent
        estimated_total = float(obj.get("estimated_total") or 0)
        parts = []
        if "no_plan" in reasons:
            parts.append(f"изразходвани {eur(spent)} през {year} г. при годишен план 0 €")
        if "over_estimate" in reasons:
            parts.append(
                f"общо усвоени {eur(cumulative)} при обща прогнозна стойност на обекта "
                f"{eur(estimated_total)}"
            )
        subject_id = f"{_object_subject(obj)}:{year}"
        out.append(
            make_flag(
                rule="unplanned_spending",
                tier=TIER_VIOLATION,
                severity="warning",
                message=(
                    f"Обект „{name}“: " + "; ".join(parts) + " — разход извън утвърдения "
                    "бюджет, който изисква обяснение."
                ),
                explanation=(
                    "По този обект има плащания, които не са предвидени в годишния план на "
                    "общината или надхвърлят общата му прогнозна стойност (отчет за "
                    f"{period}). Законът забранява разходи и проекти, които не са предвидени в "
                    "годишния бюджет (чл. 128, ал. 1 ЗПФ). Разходът е законен, ако общинският "
                    "съвет е одобрил промяна на бюджета по чл. 124 ЗПФ преди плащането — това "
                    "решение следва да се поиска."
                ),
                documents=_BUDGET_CHANGE_DOCUMENTS[:1]
                + [
                    "фактури и платежни нареждания по обекта",
                    "договор(и) за изпълнение на обекта",
                    "актуализиран разчет за капиталовите разходи",
                ],
                subject_type="budget_object",
                subject_id=subject_id,
                details={
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": period,
                    "year": year,
                    "plan_current": plan,
                    "spent_period": spent,
                    "spent_prior": float(obj.get("spent_prior") or 0),
                    "estimated_total": estimated_total or None,
                    "reasons": reasons,
                },
                law_ref=(
                    "чл. 128, ал. 1 ЗПФ (забрана за разходи, непредвидени в годишния бюджет); "
                    "чл. 102, ал. 1 ЗПФ; чл. 124, ал. 2 ЗПФ (промените се одобряват от "
                    "общинския съвет)"
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# overspend_vs_plan (signal)
# ---------------------------------------------------------------------------


def overspend_vs_plan_flags(
    objects_latest: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`overspend_vs_plan` (signal) -- the softer case: an object WITH an annual
    plan whose cumulative spending this year exceeds it by more than 2% and
    10,000 EUR, at the latest reporting period. Objects that
    `unplanned_spending` already covers (no plan at all / over the total
    estimate) are skipped so one object never gets both.
    """
    ratio = thresholds.overspend_ratio
    abs_eur = thresholds.overspend_abs_eur
    out: list[dict[str, Any]] = []

    for obj in objects_latest:
        plan = float(obj.get("plan_current") or 0)
        spent = float(obj.get("spent_period") or 0)
        if plan <= 0 or _unplanned_reasons(obj, thresholds):
            continue
        over_plan = spent - plan * ratio
        if over_plan <= abs_eur:
            continue

        name = obj.get("object_name") or "обект"
        subject_id = _object_subject(obj)
        out.append(
            make_flag(
                rule="overspend_vs_plan",
                tier=TIER_SIGNAL,
                severity="warning",
                message=(
                    f"Обект „{name}“: изразходвани {eur(spent)} при план за годината "
                    f"{eur(plan)} — разходът надвишава плана и изисква обяснение."
                ),
                explanation=(
                    "Според отчета за капиталовите разходи по този обект е платено повече, "
                    "отколкото е предвидено в годишния план. Това може да сочи към поемане на "
                    "задължения без бюджетно покритие. Невинно обяснение е одобрена промяна на "
                    "бюджета (решение на общинския съвет или компенсирана промяна от кмета по "
                    "чл. 125 ЗПФ), която още не е отразена в отчета."
                ),
                documents=_BUDGET_CHANGE_DOCUMENTS,
                subject_type="budget_object",
                subject_id=subject_id,
                details={
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "plan_current": plan,
                    "spent_period": spent,
                    "spent_prior": float(obj.get("spent_prior") or 0),
                    "estimated_total": (
                        float(obj["estimated_total"]) if obj.get("estimated_total") else None
                    ),
                    "over_plan_eur": round(over_plan, 2),
                },
                law_ref=(
                    "чл. 124, ал. 2 ЗПФ (промените по общинския бюджет се одобряват от "
                    "общинския съвет); чл. 125 ЗПФ (компенсирани промени)"
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# plan_jump (signal)
# ---------------------------------------------------------------------------

_PLAN_JUMP_DOCUMENTS = [
    "решение на общинския съвет за актуализация на бюджета",
    "докладна записка/мотиви към промяната",
    "разчет за капиталовите разходи преди и след промяната",
]
_PLAN_JUMP_LAW_REF = (
    "чл. 124, ал. 2 ЗПФ (промените по общинския бюджет се одобряват от общинския съвет); "
    "чл. 22, ал. 2 ЗМСМА (разгласяване на решенията в 7-дневен срок)"
)


def plan_jump_flags(
    objects_history: dict[tuple[str | None, str], list[dict[str, Any]]],
    thresholds: Thresholds,
    *,
    dataset_first_period: str,
) -> list[dict[str, Any]]:
    """`plan_jump` (signal): a capital budget object whose plan jumped
    month-over-month by more than 50% AND more than 100,000 EUR, or a new
    object appearing mid-year with an initial plan over 250,000 EUR.

    `objects_history` maps (paragraph, object_name) -> that object's rows.
    """
    ratio = thresholds.plan_jump_ratio
    abs_eur = thresholds.plan_jump_abs_eur
    new_object_min = thresholds.new_object_min_eur
    out: list[dict[str, Any]] = []

    for (paragraph, name), rows in objects_history.items():
        ordered = sorted(rows, key=lambda r: r["period"])
        if not ordered:
            continue
        subject_base = f"{paragraph or '?'}:{name}"

        for prev, cur in pairwise(ordered):
            prev_plan = float(prev.get("plan_current") or 0)
            cur_plan = float(cur.get("plan_current") or 0)
            if prev_plan <= 0:
                continue
            diff = cur_plan - prev_plan
            if diff <= 0 or cur_plan < prev_plan * ratio or diff <= abs_eur:
                continue
            out.append(
                make_flag(
                    rule="plan_jump",
                    tier=TIER_SIGNAL,
                    severity="warning",
                    message=(
                        f"Планът за обект „{name}“ скача от {eur(prev_plan)} ({prev['period']}) "
                        f"на {eur(cur_plan)} ({cur['period']}) — внезапното преразпределение "
                        "изисква обяснение."
                    ),
                    explanation=(
                        "Между два последователни месечни отчета планираните средства за обекта "
                        "са увеличени рязко. Големи преразпределения в средата на годината може да "
                        "сочат към предварително уговорени разходи или към пренасочване на средства "
                        "без публично обсъждане. Нормалното обяснение е решение на общинския съвет "
                        "за актуализация на бюджета, в което увеличението е мотивирано."
                    ),
                    documents=_PLAN_JUMP_DOCUMENTS,
                    subject_type="budget_object",
                    subject_id=f"{subject_base}:{prev['period']}->{cur['period']}",
                    details={
                        "object_name": name,
                        "paragraph": paragraph,
                        "from_period": prev["period"],
                        "to_period": cur["period"],
                        "plan_before": prev_plan,
                        "plan_after": cur_plan,
                        "increase_eur": diff,
                        "increase_pct": round((cur_plan / prev_plan - 1) * 100, 1),
                    },
                    law_ref=_PLAN_JUMP_LAW_REF,
                )
            )

        first_row = ordered[0]
        first_plan = float(first_row.get("plan_current") or 0)
        if first_row["period"] != dataset_first_period and first_plan > new_object_min:
            out.append(
                make_flag(
                    rule="plan_jump",
                    tier=TIER_SIGNAL,
                    severity="warning",
                    message=(
                        f"Обект „{name}“ се появява за пръв път в отчета за {first_row['period']} "
                        f"(не от началото на годината) с план {eur(first_plan)} — произходът на "
                        "средствата изисква обяснение."
                    ),
                    explanation=(
                        "Обектът липсва в по-ранните отчети и се появява в средата на годината с "
                        "голям план. Нов голям разход извън първоначалния бюджет може да сочи към "
                        "решение, взето без достатъчна публичност. Невинно обяснение е решение на "
                        "общинския съвет за включване на обекта, например при получено външно "
                        "финансиране."
                    ),
                    documents=_PLAN_JUMP_DOCUMENTS,
                    subject_type="budget_object",
                    subject_id=f"{subject_base}:new",
                    details={
                        "object_name": name,
                        "paragraph": paragraph,
                        "first_period": first_row["period"],
                        "dataset_first_period": dataset_first_period,
                        "initial_plan": first_plan,
                    },
                    law_ref=_PLAN_JUMP_LAW_REF,
                )
            )

    return out


# ---------------------------------------------------------------------------
# unmatched_spending (signal)
# ---------------------------------------------------------------------------


def unmatched_spending_flags(
    objects_latest: list[dict[str, Any]],
    contract_candidates: list[MatchCandidate],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`unmatched_spending` (signal, info): a capital budget object with
    spending-to-date at or above the ЗОП direct-award threshold for which
    automated matching (`analysis.matching.find_best_match`) finds no
    plausible published contract. The lowest-confidence rule here (hedged
    wording): "no match" means *this script* found none.
    """
    threshold_eur = thresholds.direct_award_threshold_eur
    out: list[dict[str, Any]] = []

    for obj in objects_latest:
        spent = float(obj.get("spent_period") or 0)
        if spent < threshold_eur:
            continue
        name = obj.get("object_name") or "обект"

        match = find_best_match(
            name,
            spent,
            contract_candidates,
            jaccard_threshold=thresholds.match_jaccard_threshold,
            value_ratio_low=thresholds.match_value_ratio_low,
            value_ratio_high=thresholds.match_value_ratio_high,
        )
        if match is not None:
            continue

        out.append(
            make_flag(
                rule="unmatched_spending",
                tier=TIER_SIGNAL,
                severity="info",
                message=(
                    f"За обект „{name}“ с разход {eur(spent)} не открихме публикуван договор за "
                    "този обект; може да е възложен под праговете или описан различно."
                ),
                explanation=(
                    "Разходът по обекта е над прага, под който поръчка може да се възложи пряко, "
                    "но не открихме съответстващ договор в Регистъра на обществените поръчки. Това "
                    "може да означава разход без задължителната процедура. По-вероятното невинно "
                    "обяснение е, че договорът е описан с други думи, разделен е на части или е "
                    "сключен преди периода на нашите данни."
                ),
                documents=[
                    "договор(и) за изпълнение на обекта",
                    "документ за избора на изпълнител (процедура или пряко възлагане)",
                    "фактури и приемо-предавателни протоколи",
                ],
                subject_type="budget_object",
                subject_id=_object_subject(obj),
                details={
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "spent_period": spent,
                    "direct_award_threshold_eur": round(threshold_eur, 2),
                },
                law_ref=thresholds.direct_award_law_ref,
            )
        )
    return out
