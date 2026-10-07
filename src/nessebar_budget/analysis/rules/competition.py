"""Threshold-gaming rules: `splitting` (ЗОП чл. 21, ал. 15) and
`near_threshold` (values parked just under a ЗОП чл. 20 boundary).

Both work on *procurement units*, not raw contract rows: all lots of one
tender are one procurement (ЗОП чл. 21, ал. 4 -- "стойността на поръчката е
равна на сбора от стойностите на всички позиции"), so splitting is only ever
looked for *between* tenders, never between the lots of one tender.

ЗОП чл. 20 bands (verified values, in force since 01.01.2024; see
`analysis/thresholds.py`), as (boundary, regime required at/above it):

    works:              80 000 лв. -> обява (tier 1); 300 000 -> публично
                        състезание (tier 2); 10 526 116 -> open procedure (tier 3)
    supplies/services:  50 000 -> tier 1; 100 000 -> tier 2; 273 812 -> tier 3
    Annex 2 services:   100 000 -> tier 2; 1 466 850 -> tier 3

A procurement awarded under a regime of tier t is only lawful below the
boundary that requires tier t+1. Splitting = several such procurements, each
below that boundary, whose sum within 12 months crosses it.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any

from nessebar_budget.analysis.matching import distinctive_tokens, jaccard, tokenize
from nessebar_budget.analysis.rules._common import (
    PROCEDURE_REGIME_TIER,
    TIER_OPACITY,
    TIER_SIGNAL,
    _short_title,
    contract_subject_id,
    eur,
    is_signed,
    make_flag,
    procedure_label,
)
from nessebar_budget.analysis.rules.linking import (
    contract_local_date,
    estimated_value_eur,
    notice_date,
    tender_number,
    type_of_contract,
)
from nessebar_budget.analysis.thresholds import Thresholds, bgn_to_eur

_CATEGORY_LABEL = {
    "works": "строителство",
    "supplies": "доставки и услуги",
    "annex2": "услуги по приложение № 2",
}
_REGIME_LABEL = {
    1: "събиране на оферти с обява",
    2: "публично състезание",
    3: "открита процедура",
}


def _bands(category: str, thresholds: Thresholds) -> list[tuple[float, float, int]]:
    """[(boundary_bgn, boundary_eur, required_tier), ...] ascending."""
    if category == "works":
        bounds, tiers = thresholds.zop_works_bounds_bgn, (1, 2, 3)
    elif category == "annex2":
        bounds, tiers = thresholds.zop_annex2_bounds_bgn, (2, 3)
    else:
        bounds, tiers = thresholds.zop_supplies_bounds_bgn, (1, 2, 3)
    return [(float(b), bgn_to_eur(float(b)), t) for b, t in zip(bounds, tiers, strict=False)]


def category_of(record: dict[str, Any], thresholds: Thresholds) -> str | None:
    """works / supplies / annex2 from TypeOfContract (3 = строителство, 2 =
    доставки, 1 = услуги -- mapping verified in docs/RULES.md) + main CPV."""
    toc = type_of_contract(record)
    if toc == 3:
        return "works"
    if toc == 1:
        cpv = str(record.get("cpv_code") or "")
        if cpv and cpv.startswith(tuple(thresholds.annex2_cpv_prefixes)):
            return "annex2"
        return "supplies"
    if toc == 2:
        return "supplies"
    return None


def _same_object_tokens(title: str | None) -> frozenset[str]:
    """Street / cadastral-parcel tokens: what identifies one *строеж*. Place
    names alone are not enough (two streets in Равда are two строежа)."""
    return frozenset(t for t in distinctive_tokens(title) if t.startswith(("ST:", "CAD:")))


@dataclass
class _Unit:
    """One procurement (a tender, or a tender's share won by one contractor)."""

    key: str
    tender: str
    members: list[dict[str, Any]]
    value: float
    date: dt.date
    regime_tier: int | None
    procedure_type: str | None
    category: str
    cpv: str
    title: str
    eik: str | None
    contractor_name: str | None
    tokens: frozenset[str] = field(default_factory=frozenset)
    object_tokens: frozenset[str] = field(default_factory=frozenset)


def _build_units(
    contracts: list[dict[str, Any]], thresholds: Thresholds, *, per_contractor: bool
) -> list[_Unit]:
    grouped: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for record in contracts:
        if record.get("source") != "eop" or not is_signed(record):
            continue
        number = tender_number(record)
        if not number:
            continue
        key = (number, str(record.get("contractor_eik") or "")) if per_contractor else (number,)
        grouped[key].append(record)

    units: list[_Unit] = []
    for key, rows in grouped.items():
        first = rows[0]
        category = category_of(first, thresholds)
        dates = [d for d in (contract_local_date(r) for r in rows) if d is not None]
        if category is None or not dates:
            continue
        value = sum(float(r.get("contract_value_eur") or 0) for r in rows)
        title = first.get("title") or ""
        procedure_type = first.get("procedure_type")
        units.append(
            _Unit(
                key=":".join(key),
                tender=key[0],
                members=rows,
                value=value,
                date=min(dates),
                regime_tier=PROCEDURE_REGIME_TIER.get(procedure_type or ""),
                procedure_type=procedure_type,
                category=category,
                cpv=str(first.get("cpv_code") or ""),
                title=title,
                eik=str(first.get("contractor_eik") or "") or None,
                contractor_name=first.get("contractor_name"),
                tokens=tokenize(title),
                object_tokens=_same_object_tokens(title),
            )
        )
    units.sort(key=lambda u: (u.date, u.key))
    return units


def _find_groups(
    units: list[_Unit],
    thresholds: Thresholds,
    similar: Callable[[_Unit, _Unit], bool],
) -> list[tuple[tuple[float, float, int], list[_Unit]]]:
    """Rolling-12-month windows of lower-regime units whose sum crosses a band
    boundary. Higher boundaries first; each unit is used in at most one group
    (disjoint greedy, in date order) so a long chain doesn't spawn a flag per
    overlapping window."""
    if not units:
        return []
    window = dt.timedelta(days=thresholds.splitting_window_days)
    used: set[str] = set()
    out: list[tuple[tuple[float, float, int], list[_Unit]]] = []
    for band in reversed(_bands(units[0].category, thresholds)):
        _, boundary_eur, required_tier = band
        eligible = [
            u
            for u in units
            if u.key not in used
            and u.value < boundary_eur
            and u.regime_tier is not None
            and u.regime_tier < required_tier
        ]
        for anchor in eligible:
            if anchor.key in used:
                continue
            group = [
                u
                for u in eligible
                if u.key not in used
                and anchor.date <= u.date <= anchor.date + window
                and (u is anchor or similar(anchor, u))
            ]
            if len(group) < thresholds.splitting_min_members:
                continue
            if sum(u.value for u in group) < boundary_eur:
                continue
            if _looks_like_renewals(group, thresholds):
                continue
            out.append((band, group))
            used.update(u.key for u in group)
    return out


def _looks_like_renewals(group: list[_Unit], thresholds: Thresholds) -> bool:
    """True if every consecutive gap is >= `splitting_renewal_gap_days`: the
    pattern of successive annual contracts for a recurring need (ЗОП чл. 21,
    ал. 8), not of one need divided into parts."""
    dates = sorted(u.date for u in group)
    return all((b - a).days >= thresholds.splitting_renewal_gap_days for a, b in pairwise(dates))


def _contractor_similar(anchor: _Unit, other: _Unit) -> bool:
    if anchor.category == "works":
        # ЗОП чл. 21, ал. 16, т. 1: separate строежи are never "splitting".
        return bool(anchor.object_tokens & other.object_tokens)
    return anchor.cpv[:2] == other.cpv[:2]


def _cpv_similar_factory(min_jaccard: float) -> Callable[[_Unit, _Unit], bool]:
    def similar(anchor: _Unit, other: _Unit) -> bool:
        if jaccard(anchor.tokens, other.tokens) < min_jaccard:
            return False
        if anchor.category == "works":
            return bool(anchor.object_tokens & other.object_tokens)
        return True

    return similar


def splitting_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`splitting` (signal): ЗОП чл. 21, ал. 15 -- "Не се допуска разделяне на
    обществена поръчка на части с което се прилага ред за възлагане за
    по-ниски стойности".

    Two groupings of signed EOP procurements, each within a rolling 12-month
    window (чл. 21, ал. 16):

    1. by contractor EIK (+ same category; same CPV division for supplies/
       services; same street/parcel for works -- separate строежи are exempt
       under чл. 21, ал. 16, т. 1);
    2. by main-CPV prefix (5 digits) + similar title (Jaccard over
       `matching.tokenize` >= `splitting_title_jaccard`).

    A group needs >= 2 procurements each below a чл. 20 boundary *and*
    awarded under a regime only allowed below it (e.g. обява for
    supplies < 100 000 лв.), while their sum crosses it. Groups whose members
    are all >= `splitting_renewal_gap_days` apart (successive annual
    contracts, чл. 21, ал. 8) are skipped. severity warning;
    high when the sum exceeds 2x the boundary. CPV-path groups whose
    contracts are all already in a contractor-path group are dropped.
    """
    out: list[dict[str, Any]] = []
    contractor_member_sets: list[set[str]] = []

    # --- path 1: contractor --------------------------------------------------
    units = _build_units(contracts, thresholds, per_contractor=True)
    by_contractor: dict[tuple[str, str], list[_Unit]] = defaultdict(list)
    for u in units:
        if u.eik:
            by_contractor[(u.eik, u.category)].append(u)
    for (eik, category), group_units in sorted(by_contractor.items()):
        for band, group in _find_groups(group_units, thresholds, _contractor_similar):
            flag = _splitting_flag("eik", f"eik:{eik}:{category}", band, group, thresholds)
            contractor_member_sets.append(set(flag["details_json"]["member_ids"]))
            out.append(flag)

    # --- path 2: CPV prefix + similar title -----------------------------------
    units = _build_units(contracts, thresholds, per_contractor=False)
    by_cpv: dict[tuple[str, str], list[_Unit]] = defaultdict(list)
    for u in units:
        if len(u.cpv) >= 5:
            by_cpv[(u.cpv[:5], u.category)].append(u)
    similar = _cpv_similar_factory(thresholds.splitting_title_jaccard)
    for (cpv5, category), group_units in sorted(by_cpv.items()):
        for band, group in _find_groups(group_units, thresholds, similar):
            flag = _splitting_flag("cpv", f"cpv:{cpv5}:{category}", band, group, thresholds)
            members = set(flag["details_json"]["member_ids"])
            if any(members <= s for s in contractor_member_sets):
                continue
            out.append(flag)
    return out


def _splitting_flag(
    path: str,
    group_key: str,
    band: tuple[float, float, int],
    group: list[_Unit],
    thresholds: Thresholds,
) -> dict[str, Any]:
    boundary_bgn, boundary_eur, required_tier = band
    total = sum(u.value for u in group)
    members = [
        {
            "subject_id": contract_subject_id(r),
            "procurement_id": r.get("id"),
            "tender_number": u.tender,
            "title": r.get("title"),
            "contractor_name": r.get("contractor_name"),
            "contract_value_eur": round(float(r.get("contract_value_eur") or 0), 2),
            "contract_date": u.date.isoformat(),
            "procedure_type": r.get("procedure_type"),
        }
        for u in group
        for r in u.members
    ]
    member_ids = [m["subject_id"] for m in members]
    anchor = group[0]
    subject_id = f"split:{group_key}:{int(boundary_bgn)}:{contract_subject_id(anchor.members[0])}"
    severity = "high" if total > thresholds.splitting_high_multiple * boundary_eur else "warning"
    regimes = sorted({procedure_label(u.procedure_type) for u in group})
    first, last = group[0].date, group[-1].date
    required = _REGIME_LABEL.get(required_tier, "по-строга процедура")
    category_label = _CATEGORY_LABEL.get(anchor.category, anchor.category)
    if path == "eik":
        who = anchor.contractor_name or anchor.eik
        headline = f"{len(group)} поръчки за {category_label}, възложени на {who}"
        subject_type = "contractor"
    else:
        headline = f"{len(group)} сходни поръчки („{_short_title(anchor.title, 60)}“)"
        subject_type = "contract_group"
    message = (
        f"{headline} между {first:%d.%m.%Y} и {last:%d.%m.%Y}, са за общо {eur(total)} — всяка "
        f"е под прага от {eur(boundary_eur)} ({boundary_bgn:,.0f} лв.), а заедно го надхвърлят."
    )
    explanation = (
        f"Тези поръчки са възложени по по-лек ред ({', '.join(regimes)}), който законът допуска "
        f"само под {boundary_bgn:,.0f} лв., но общо за 12 месеца надхвърлят този праг, над който "
        f"се изисква {required} (чл. 20 ЗОП). Законът забранява разделянето на поръчка на части, "
        "за да се приложи ред за по-ниски стойности (чл. 21, ал. 15 ЗОП; глоба по чл. 247). "
        "Невинно обяснение е, ако нуждите са възникнали поотделно и не са били известни при "
        "първата поръчка (чл. 21, ал. 16, т. 2) или ако предметите реално са различни."
    )
    return make_flag(
        rule="splitting",
        tier=TIER_SIGNAL,
        severity=severity,
        message=message,
        explanation=explanation,
        documents=[
            "докладни записки/заявки за възникване на потребността по всяка поръчка",
            "обосновка на прогнозната стойност на всяка поръчка",
            "годишен план-график на обществените поръчки",
            "договорите и техническите спецификации",
        ],
        subject_type=subject_type,
        subject_id=subject_id,
        details={
            "path": path,
            "group_key": group_key,
            "category": anchor.category,
            "members": members,
            "member_ids": member_ids,
            "sum_eur": round(total, 2),
            "boundary_bgn": boundary_bgn,
            "boundary_eur": round(boundary_eur, 2),
            "required_regime": required,
            "window_days": thresholds.splitting_window_days,
            "first_date": first.isoformat(),
            "last_date": last.isoformat(),
        },
        law_ref=thresholds.splitting_law_ref,
        procurement_id=anchor.members[0].get("id"),
    )


# ---------------------------------------------------------------------------
# near_threshold
# ---------------------------------------------------------------------------


def near_threshold_flags(
    contracts: list[dict[str, Any]],
    thresholds: Thresholds,
    *,
    splitting_member_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """`near_threshold` (opacity; signal with splitting evidence): a procedure's
    estimated value (fallback: summed contract value) lies within
    `near_threshold_pct` (5%) *below* the чл. 20 boundary that its own regime
    must stay under -- e.g. an обява for supplies estimated at 98 000 лв.
    (boundary 100 000 лв.).

    ЗОП чл. 21, ал. 14: "Изборът на метод за изчисляване на прогнозната
    стойност ... не трябва да се използва за прилагане на ред за възлагане
    за по-ниски стойности." Only procedures announced on/after
    `zop_thresholds_effective_from` (2024-01-01, when the verified values took
    effect) are checked. One flag per tender (`eop-tender:<УНП>`); open
    tenders without a contract yet are included -- the estimate is known
    before the award.
    """
    splitting_member_ids = splitting_member_ids or set()
    by_tender: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in contracts:
        if record.get("source") != "eop":
            continue
        number = tender_number(record)
        if number:
            by_tender[number].append(record)

    out: list[dict[str, Any]] = []
    for number, rows in sorted(by_tender.items()):
        signed = [r for r in rows if is_signed(r)]
        rep = signed[0] if signed else rows[0]
        category = category_of(rep, thresholds)
        tier = PROCEDURE_REGIME_TIER.get(rep.get("procedure_type") or "")
        if category is None or tier is None:
            continue
        band = next((b for b in _bands(category, thresholds) if b[2] == tier + 1), None)
        if band is None:
            continue
        boundary_bgn, boundary_eur, required_tier = band
        announced = notice_date(rep) or contract_local_date(rep)
        if announced is None or announced < thresholds.zop_thresholds_effective_from:
            continue
        estimate = estimated_value_eur(rep)
        value = estimate if estimate else sum(float(r.get("contract_value_eur") or 0) for r in signed)
        basis = "прогнозната стойност" if estimate else "стойността на договорите"
        if not value:
            continue
        lower = boundary_eur * (1 - thresholds.near_threshold_pct)
        if not (lower <= value < boundary_eur):
            continue

        member_ids = {contract_subject_id(r) for r in signed}
        with_splitting = bool(member_ids & splitting_member_ids)
        gap_pct = (1 - value / boundary_eur) * 100
        title = _short_title(rep.get("title"))
        explanation = (
            f"{basis.capitalize()} е определена малко под границата от {boundary_bgn:,.0f} лв., "
            f"над която законът изисква {_REGIME_LABEL.get(required_tier, 'по-строга процедура')} "
            "(чл. 20 ЗОП). Това може да е съвпадение, но може и стойността да е „нагласена“, за да се "
            "избегне по-строгият ред, което чл. 21, ал. 14 ЗОП забранява. Обосновката на "
            "прогнозната стойност показва дали тя отразява реални пазарни цени."
        )
        if with_splitting:
            explanation += (
                " Освен това поръчката участва в група поръчки, които заедно надхвърлят прага "
                "(вижте сигнала за възможно разделяне)."
            )
        out.append(
            make_flag(
                rule="near_threshold",
                tier=TIER_SIGNAL if with_splitting else TIER_OPACITY,
                severity="warning" if with_splitting else "info",
                message=(
                    f"{basis.capitalize()} на поръчката „{title}“ е {eur(value)} — само "
                    f"{gap_pct:.1f}% под прага от {eur(boundary_eur)} ({boundary_bgn:,.0f} лв.) за "
                    f"{procedure_label(rep.get('procedure_type'))}."
                ),
                explanation=explanation,
                documents=[
                    "обосновка на прогнозната стойност (пазарно проучване, получени оферти)",
                    "годишен план-график на обществените поръчки",
                    "документация на поръчката",
                ],
                subject_type="procedure",
                subject_id=f"eop-tender:{number}",
                details={
                    "tender_number": number,
                    "procedure_type": rep.get("procedure_type"),
                    "category": category,
                    "value_eur": round(value, 2),
                    "value_basis": "estimated" if estimate else "contracts",
                    "boundary_bgn": boundary_bgn,
                    "boundary_eur": round(boundary_eur, 2),
                    "gap_pct": round(gap_pct, 2),
                    "notice_date": announced.isoformat(),
                    "member_ids": sorted(member_ids),
                    "with_splitting_evidence": with_splitting,
                },
                law_ref=thresholds.near_threshold_law_ref,
                procurement_id=rep.get("id"),
            )
        )
    return out
