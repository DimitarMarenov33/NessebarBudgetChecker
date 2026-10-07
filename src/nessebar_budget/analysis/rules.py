"""Analysis rules: each rule inspects procurement/budget data and yields flag dicts.

Two shapes of rule coexist here, for historical/compatibility reasons:

- `MissingValueRule` is the original, *per-record* rule (a `Rule` in the
  `Protocol` sense below): `.check(record)` inspects a single flat dict and
  returns 0+ flag dicts. This shape is kept exactly as-is because
  `analysis.engine.run_rules`/`DEFAULT_RULES` (the legacy minimal pipeline
  still used by `pipeline.py`'s `_step_analyze`) and `tests/test_smoke.py`
  both depend on it.

- Every other rule below is a plain module-level function operating on a
  *dataset* (all contracts, or all budget-ledger objects across periods),
  since they need cross-record context a single record can't provide
  (contractor concentration across many contracts, a budget object's
  month-over-month history, matching a budget object against the whole
  contract list, ...). `analysis.engine.run_full_analysis` is what drives
  these; see that module for the upsert/resolve bookkeeping.

Every flag dict has at least: `rule`, `severity` (info/warning/high),
`message` (Bulgarian, neutral/non-accusatory, for citizens), `subject_type`,
`subject_id`, `subject_key`, `details_json`. Most also set `law_ref`.

Legal citations are summarized in each rule's docstring; full quotes and
article numbers are in `analysis/thresholds.py` and `docs/RULES.md`, sourced
from `docs/law/*.txt` (see `docs/law/INDEX.md`).
"""

from __future__ import annotations

import datetime as dt
import html
import re
from collections import defaultdict
from itertools import pairwise
from typing import Any, Protocol

from nessebar_budget.analysis.matching import MatchCandidate, find_best_match
from nessebar_budget.analysis.thresholds import BGN_EUR_RATE, Thresholds

#: int currency code -> ISO label, as independently confirmed in
#: `scrapers/eop.py` (`CURRENCY_CODES`) and re-verified here against the
#: committed `data/nessebar.db`: every `Currency == 3` contract satisfies
#: `ContractValue / BGN_EUR_RATE == ContractValueEuro` to the cent, and every
#: `Currency == 1` contract has `ContractValue == ContractValueEuro` exactly.
_CURRENCY_EUR = 1
_CURRENCY_BGN = 3

_NET_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")


def _parse_net_date(value: str | None) -> dt.datetime | None:
    """Parse a .NET JSON date like ``/Date(1789678800000+0300)/`` into a naive
    UTC datetime. Duplicated (deliberately, in miniature) from
    `scrapers/eop.py`'s private `_parse_net_date` rather than imported, to
    avoid coupling this module to a concurrently-edited scraper module for
    what is a ~5-line, stable piece of logic.
    """
    if not value:
        return None
    match = _NET_DATE_RE.match(value)
    if not match:
        return None
    millis = int(match.group(1))
    return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC).replace(tzinfo=None)


def _to_eur(value: float | None, currency_code: int | None) -> float | None:
    """Convert a raw EOP contract-dict value to EUR using its own currency code."""
    if value is None:
        return None
    if currency_code == _CURRENCY_EUR:
        return float(value)
    if currency_code == _CURRENCY_BGN:
        return float(value) / BGN_EUR_RATE
    return None  # unrecognized/unconfirmed code (e.g. USD): don't guess


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Rule(Protocol):
    """A per-record rule: inspects one record and yields zero or more flag dicts."""

    name: str
    severity: str

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]: ...


class MissingValueRule:
    """Flags procurement records whose contract value is missing or zero.

    Kept byte-for-byte compatible with its original per-record interface
    (see module docstring); `missing_value_flags` below wraps it with the
    subject_type/subject_id/subject_key fields the new engine needs.
    """

    name = "missing_value"
    severity = "warning"

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        # Only signed contracts can be missing a value; open tenders without a
        # contractor/contract date are not yet obliged to show one.
        is_contract = bool(record.get("contractor_name") or record.get("contract_date"))
        if not is_contract:
            return []
        eur = record.get("contract_value_eur")
        bgn = record.get("contract_value_bgn")
        if (eur is None or eur == 0) and (bgn is None or bgn == 0):
            return [
                {
                    "rule": self.name,
                    "severity": self.severity,
                    "message": (
                        f"Procurement {record.get('source_id', '?')!r} has no "
                        "recorded contract value (None or 0)."
                    ),
                    "procurement_id": record.get("id"),
                }
            ]
        return []


class PricePerUnitRule:
    """Placeholder: flag unusually high price-per-unit vs. a reference dataset.

    TODO: this requires a reference price dataset (e.g. per CPV code / unit)
    that does not exist yet. Until that reference data is wired in, this
    rule does not pretend to evaluate anything.
    """

    name = "price_per_unit"
    severity = "warning"

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "PricePerUnitRule requires reference price data that is not available yet"
        )


def missing_value_flags(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`MissingValueRule`, enriched with subject_type/subject_id/subject_key
    for `analysis.engine.run_full_analysis`. `records` should carry `source`.
    """
    rule = MissingValueRule()
    out: list[dict[str, Any]] = []
    for record in records:
        for hit in rule.check(record):
            source = record.get("source", "?")
            source_id = record.get("source_id", "?")
            subject_id = f"{source}:{source_id}"
            out.append(
                {
                    **hit,
                    "subject_type": "contract",
                    "subject_id": subject_id,
                    "subject_key": subject_id,
                    "details_json": {
                        "source": source,
                        "source_id": source_id,
                        "contract_value_eur": record.get("contract_value_eur"),
                        "contract_value_bgn": record.get("contract_value_bgn"),
                    },
                    "law_ref": None,
                }
            )
    return out



def _short_title(title: str | None, limit: int = 90) -> str:
    """Trim a long procurement subject for citizen-facing messages."""
    text = (title or "обществена поръчка").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def late_publication_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """LatePublicationRule: flags an EOP contract whose award notice
    (``TedPublishDate``) was published more than the legal deadline after the
    contract was signed (``ContractDate``).

    ЗОП чл. 26, ал. 1, т. 1: "Възложителите изпращат за публикуване обявление
    за възлагане на поръчка в срок до: 1. тридесет дни след сключване на
    договор за обществена поръчка или рамково споразумение." (30 days.)

    Only `source == "eop"` records carry `raw_json.contract.TedPublishDate`;
    SIGMA records are skipped (no equivalent field observed).
    """
    deadline = thresholds.late_publication_deadline_days
    law_ref = thresholds.late_publication_law_ref
    out: list[dict[str, Any]] = []

    for record in contracts:
        if record.get("source") != "eop":
            continue
        raw = record.get("raw_json") or {}
        contract = raw.get("contract")
        if not contract:
            continue

        contract_date = _parse_net_date(contract.get("ContractDate"))
        ted_publish_date = _parse_net_date(contract.get("TedPublishDate"))
        if contract_date is None or ted_publish_date is None:
            continue

        days_late = (ted_publish_date - contract_date).days - deadline
        if days_late <= thresholds.late_publication_grace_days:
            continue
        if days_late > thresholds.late_publication_high_days:
            severity = "high"
        elif days_late > thresholds.late_publication_warning_days:
            severity = "warning"
        else:
            severity = "info"

        source_id = record.get("source_id", "?")
        subject_id = f"eop:{source_id}"
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            {
                "rule": "late_publication",
                "severity": severity,
                "message": (
                    f"Обявлението за възлагане по договор с {contractor} "
                    f"(„{title}“) е публикувано {days_late} дни след законовия "
                    f"{deadline}-дневен срок от подписването на договора — изисква обяснение."
                ),
                "subject_type": "contract",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "source_id": source_id,
                    "contract_date": contract_date.isoformat(),
                    "ted_publish_date": ted_publish_date.isoformat(),
                    "deadline_days": deadline,
                    "days_late": days_late,
                },
                "law_ref": law_ref,
                "procurement_id": record.get("id"),
            }
        )
    return out


def annex_growth_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """AnnexGrowthRule: flags an EOP contract whose current value
    (``CurrentContractValue``, i.e. after any annexes/amendments) exceeds its
    originally signed value (``ContractValue``) by more than the configured
    ratio (default +10%).

    Both values are re-derived to EUR from their own currency codes (not the
    raw_json's own `...Euro` fields, which were found to sometimes go stale
    after an amendment -- see `thresholds.py`/`docs/RULES.md`).

    ЗОП чл. 116, ал. 2 caps the *cumulative* increase at 50% of the original
    value; this rule's lower warning threshold is an early signal, not a
    claim that the legal cap was breached.
    """
    ratio = thresholds.annex_growth_ratio
    law_ref = thresholds.annex_growth_law_ref
    out: list[dict[str, Any]] = []

    for record in contracts:
        if record.get("source") != "eop":
            continue
        raw = record.get("raw_json") or {}
        contract = raw.get("contract")
        if not contract:
            continue

        original_eur = _to_eur(contract.get("ContractValue"), contract.get("Currency"))
        current_eur = _to_eur(
            contract.get("CurrentContractValue"), contract.get("CurrentContractCurrency")
        )
        if not original_eur or not current_eur or original_eur <= 0:
            continue

        if current_eur <= original_eur * ratio:
            continue

        source_id = record.get("source_id", "?")
        subject_id = f"eop:{source_id}"
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        pct = (current_eur / original_eur - 1) * 100
        out.append(
            {
                "rule": "annex_growth",
                "severity": "warning",
                "message": (
                    f"Стойността на договора с {contractor} („{title}“) е нараснала "
                    f"с {pct:.0f}% спрямо първоначално сключената — изисква обяснение "
                    "(напр. допълнителни споразумения)."
                ),
                "subject_type": "contract",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "source_id": source_id,
                    "original_value_eur": round(original_eur, 2),
                    "current_value_eur": round(current_eur, 2),
                    "growth_pct": round(pct, 1),
                },
                "law_ref": law_ref,
                "procurement_id": record.get("id"),
            }
        )
    return out


def single_bidder_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """SingleBidderRule: flags a contract awarded after receiving exactly one
    bid/offer, above a value threshold (warning >= 100k EUR, high >= 500k EUR).

    Only SIGMA records carry `bids_received` (EOP's is always null here --
    see `db/models.py`'s `Procurement.bids_received` docstring).
    """
    warn_eur = thresholds.single_bidder_warning_eur
    high_eur = thresholds.single_bidder_high_eur
    out: list[dict[str, Any]] = []

    for record in contracts:
        if record.get("bids_received") != 1:
            continue
        value = record.get("contract_value_eur")
        if not value or value < warn_eur:
            continue

        severity = "high" if value >= high_eur else "warning"
        source = record.get("source", "?")
        source_id = record.get("source_id", "?")
        subject_id = f"{source}:{source_id}"
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            {
                "rule": "single_bidder",
                "severity": severity,
                "message": (
                    f"Поръчка „{title}“ на стойност {value:,.0f} € е възложена на "
                    f"{contractor} при подадена само 1 оферта — изисква обяснение защо "
                    "конкуренцията е била толкова ограничена."
                ),
                "subject_type": "contract",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "source": source,
                    "source_id": source_id,
                    "contract_value_eur": value,
                    "bids_received": 1,
                },
                "law_ref": None,
                "procurement_id": record.get("id"),
            }
        )
    return out


def contractor_concentration_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds, *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """ContractorConcentrationRule: flags a contractor with >= 3 contracts and
    >= 15% of total contracted EUR in the trailing 24 months.

    `contracts` should be a *de-duplicated* contract universe -- the engine
    passes only `source == "eop"` records here. EOP and SIGMA were found to
    both publish largely the same underlying contracts (~87% of EOP contracts
    have a same-EIK-same-value SIGMA twin); summing both would double-count
    most spending and distort every contractor's share. EOP is the current,
    unified national register (ЦАИС ЕОП) and has the cleaner/complete dates,
    so it is used as the canonical set for this rule. See `docs/RULES.md`.
    """
    now = now or _now()
    window_start = now - dt.timedelta(days=thresholds.concentration_window_days)

    recent = [
        r
        for r in contracts
        if r.get("contract_date")
        and r.get("contract_value_eur")
        and r["contract_date"] >= window_start
    ]
    total_eur = sum(float(r["contract_value_eur"]) for r in recent)
    if total_eur <= 0:
        return []

    by_contractor: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in recent:
        key = r.get("contractor_eik") or r.get("contractor_name") or "?"
        by_contractor[key].append(r)

    out: list[dict[str, Any]] = []
    for key, rows in by_contractor.items():
        n = len(rows)
        contractor_eur = sum(float(r["contract_value_eur"]) for r in rows)
        share = contractor_eur / total_eur
        if n < thresholds.concentration_min_contracts or share < thresholds.concentration_share:
            continue

        name = rows[0].get("contractor_name") or key
        subject_id = str(key)
        out.append(
            {
                "rule": "contractor_concentration",
                "severity": "info",
                "message": (
                    f"{name} е сключил(а) {n} договора за общо {contractor_eur:,.0f} € "
                    f"с общината през последните ~24 месеца — {share * 100:.0f}% от общата "
                    "стойност на договорите за периода — изисква преглед за концентрация на "
                    "възложените поръчки."
                ),
                "subject_type": "contract",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "contractor_name": name,
                    "contractor_eik": rows[0].get("contractor_eik"),
                    "contract_count": n,
                    "total_eur": round(contractor_eur, 2),
                    "window_total_eur": round(total_eur, 2),
                    "share": round(share, 4),
                    "window_days": thresholds.concentration_window_days,
                },
                "law_ref": None,
            }
        )
    return out


def overspend_vs_plan_flags(
    objects_latest: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """OverspendVsPlanRule: for each capital budget object, at the latest
    reporting period, flags:

    - cumulative spending so far this year (`spent_period`) exceeding the
      current annual plan (`plan_current`) by more than 2% AND more than
      10,000 EUR, and/or
    - cumulative spending (`spent_prior` + `spent_period`) exceeding the
      object's total estimated cost (`estimated_total`).

    `objects_latest` is one dict per (paragraph, object_name) at its most
    recent period: `paragraph`, `object_name`, `plan_current`, `spent_period`,
    `spent_prior`, `estimated_total`, `period`.
    """
    ratio = thresholds.overspend_ratio
    abs_eur = thresholds.overspend_abs_eur
    out: list[dict[str, Any]] = []

    for obj in objects_latest:
        plan = float(obj.get("plan_current") or 0)
        spent = float(obj.get("spent_period") or 0)
        spent_prior = float(obj.get("spent_prior") or 0)
        estimated_total = obj.get("estimated_total")
        name = obj.get("object_name") or "обект"

        over_plan = spent - plan * ratio
        over_plan_flag = over_plan > abs_eur

        cumulative = spent_prior + spent
        over_estimate_flag = bool(estimated_total) and cumulative > float(estimated_total)

        if not over_plan_flag and not over_estimate_flag:
            continue

        subject_id = f"{obj.get('paragraph') or '?'}:{name}"
        reasons = []
        if over_plan_flag:
            reasons.append(
                f"разход {spent:,.0f} € при план {plan:,.0f} € за годината "
                f"(с {over_plan:,.0f} € над допустимото отклонение)"
            )
        if over_estimate_flag:
            reasons.append(
                f"общо усвоени {cumulative:,.0f} € при пълна прогнозна стойност на обекта "
                f"{float(estimated_total):,.0f} €"
            )
        message = (
            f"Обект „{name}“: " + "; ".join(reasons) + " — изисква обяснение."
        )
        out.append(
            {
                "rule": "overspend_vs_plan",
                "severity": "warning",
                "message": message,
                "subject_type": "budget_object",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "plan_current": plan,
                    "spent_period": spent,
                    "spent_prior": spent_prior,
                    "estimated_total": float(estimated_total) if estimated_total else None,
                    "over_plan": over_plan_flag,
                    "over_estimate": over_estimate_flag,
                },
                "law_ref": None,
            }
        )
    return out


def plan_jump_flags(
    objects_history: dict[tuple[str | None, str], list[dict[str, Any]]],
    thresholds: Thresholds,
    *,
    dataset_first_period: str,
) -> list[dict[str, Any]]:
    """PlanJumpRule: flags a capital budget object whose current-year plan
    (`plan_current`) jumped month-over-month by more than 50% AND more than
    100,000 EUR, or a new object appearing mid-year (its first reported
    period is not the dataset's first period) with an initial plan over
    250,000 EUR.

    `objects_history` maps (paragraph, object_name) -> that object's rows
    (one per period, each with `period`/`plan_current`), in any order.
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

        # Month-over-month jump.
        for prev, cur in pairwise(ordered):
            prev_plan = float(prev.get("plan_current") or 0)
            cur_plan = float(cur.get("plan_current") or 0)
            if prev_plan <= 0:
                continue
            diff = cur_plan - prev_plan
            if diff <= 0 or cur_plan < prev_plan * ratio or diff <= abs_eur:
                continue
            subject_id = f"{subject_base}:{prev['period']}->{cur['period']}"
            out.append(
                {
                    "rule": "plan_jump",
                    "severity": "warning",
                    "message": (
                        f"Планът за обект „{name}“ скача от {prev_plan:,.0f} € "
                        f"({prev['period']}) на {cur_plan:,.0f} € ({cur['period']}) — "
                        "изисква обяснение за внезапното преразпределение на средства."
                    ),
                    "subject_type": "budget_object",
                    "subject_id": subject_id,
                    "subject_key": subject_id,
                    "details_json": {
                        "object_name": name,
                        "paragraph": paragraph,
                        "from_period": prev["period"],
                        "to_period": cur["period"],
                        "plan_before": prev_plan,
                        "plan_after": cur_plan,
                        "increase_eur": diff,
                        "increase_pct": round((cur_plan / prev_plan - 1) * 100, 1),
                    },
                    "law_ref": None,
                }
            )

        # New mid-year object with a large initial plan.
        first_row = ordered[0]
        first_plan = float(first_row.get("plan_current") or 0)
        if first_row["period"] != dataset_first_period and first_plan > new_object_min:
            subject_id = f"{subject_base}:new"
            out.append(
                {
                    "rule": "plan_jump",
                    "severity": "warning",
                    "message": (
                        f"Обект „{name}“ се появява за пръв път в бюджетния отчет за "
                        f"{first_row['period']} (не от началото на годината) с план "
                        f"{first_plan:,.0f} € — изисква обяснение за произхода на средствата."
                    ),
                    "subject_type": "budget_object",
                    "subject_id": subject_id,
                    "subject_key": subject_id,
                    "details_json": {
                        "object_name": name,
                        "paragraph": paragraph,
                        "first_period": first_row["period"],
                        "dataset_first_period": dataset_first_period,
                        "initial_plan": first_plan,
                    },
                    "law_ref": None,
                }
            )

    return out


def unmatched_spending_flags(
    objects_latest: list[dict[str, Any]],
    contract_candidates: list[MatchCandidate],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """UnmatchedSpendingRule: for each capital budget object with cumulative
    spending-to-date at or above the ЗОП direct-award threshold, flags it if
    automated matching (`analysis.matching.find_best_match`) finds no
    plausible corresponding published contract.

    This is intentionally the lowest-confidence rule here (`info` severity,
    hedged wording): a "no match" means *this script* could not find one, not
    that no contract exists -- see `analysis/matching.py`.
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

        subject_id = f"{obj.get('paragraph') or '?'}:{name}"
        out.append(
            {
                "rule": "unmatched_spending",
                "severity": "info",
                "message": (
                    f"За обект „{name}“ с разход {spent:,.0f} € не открихме публикуван "
                    "договор за този обект; може да е възложен под праговете или описан "
                    "различно."
                ),
                "subject_type": "budget_object",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "spent_period": spent,
                    "direct_award_threshold_eur": round(threshold_eur, 2),
                },
                "law_ref": thresholds.direct_award_law_ref,
            }
        )
    return out


# ---------------------------------------------------------------------------
# MissingQuantityRule (`missing_quantity`)
# ---------------------------------------------------------------------------
#
# A citizen reading "we bought new pens for the offices" / "new uniforms for
# staff" at a 185,000 EUR spend has no way to check the unit price -- 4 pens
# and a single t-shirt are consistent with that invoice unless an exact
# number of objects purchased is published somewhere. This rule flags that
# specific gap for (A) EOP supply contracts and (B) § 52 capital budget
# objects. See `docs/RULES.md` for the TypeOfContract mapping verification,
# the regex's test cases, and honest caveats (including real cases found
# where the quantity exists but isn't in a field this project stores).

#: `raw_json.contract.TypeOfContract` code confirmed (against every sampled
#: EOP contract in `data/nessebar.db`) to mean "доставки" (supplies/goods):
#: TOC==1 is services (e.g. застраховка, строителен надзор, repair/maintenance
#: *services*), TOC==2 is supplies (e.g. доставка на материали/автомobili/
#: горива), TOC==3 is construction (СМР/реконструкция). See docs/RULES.md.
_MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT = 2

#: § 52 "Придобиване на дълготрайни активи" -- the only paragraph this rule's
#: Scope B looks at (subparagraphs 52-01..52-19: computers, buildings,
#: machinery/equipment, vehicles, inventory, infrastructure, other -- see
#: `docs/RULES.md` for the subparagraph labels found in the data).
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
_QUANTITY_NUM_RE = r"(?:\d[\d\s .,]*\d|\d)"
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

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _clean_html(text: str | None) -> str:
    """Strip HTML tags and unescape entities from an EOP `tender_detail`
    scalar field (`TenderDescription` carries ``<span>``/``&nbsp;`` markup).
    """
    if not text:
        return ""
    return html.unescape(_HTML_TAG_RE.sub(" ", text))


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


def _missing_quantity_contract_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """MissingQuantityRule, Scope A: a signed EOP supply contract
    (`TypeOfContract == 2`, see `_MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT`) at
    or above `missing_quantity_min_value_eur` is flagged if no quantity
    appears in, in order, `title` (already a `ContractSubject`-or-
    `TenderName` fallback chain -- see `scrapers/eop.py`'s
    `_normalize_contract`), `tender_detail.TenderDescription` (a short,
    sometimes mid-sentence-truncated scalar straight from the API -- see
    docs/RULES.md's honest caveat about ids 644-646), or
    `tender_detail.notice_text` (the richer text parsed from the full
    published-notice HTML by `scrapers/eop.py`'s `extract_notice`/
    `_build_notices` -- short description + per-lot descriptions, deduplicated)
    -- unless that same text (all three fields combined) says quantities are
    intentionally left open (see `_is_framework_defined`), in which case it is
    silently skipped, not flagged. `details_json["quantity_found_in"]` records
    which of the three fields (or `"none"`) the quantity was actually found
    in, for transparency about how much of this is still "our own scrape
    didn't look far enough" (see docs/RULES.md).

    Only `source == "eop"` records carry this raw_json shape; SIGMA records
    have no `contract`/`tender_detail` sub-objects (see docs/RULES.md).
    """
    min_value = thresholds.missing_quantity_min_value_eur
    law_ref = thresholds.missing_quantity_law_ref
    out: list[dict[str, Any]] = []

    for record in contracts:
        if record.get("source") != "eop":
            continue
        raw = record.get("raw_json") or {}
        contract = raw.get("contract")
        if not contract:
            continue
        if contract.get("TypeOfContract") != _MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT:
            continue
        is_contract = bool(record.get("contractor_name") or record.get("contract_date"))
        if not is_contract:
            continue

        value = record.get("contract_value_eur")
        if not value or value < min_value:
            continue

        tender_detail = raw.get("tender_detail") or {}
        text_sources = (
            ("title", record.get("title")),
            ("tender_description", tender_detail.get("TenderDescription")),
            ("notice_text", tender_detail.get("notice_text")),
        )
        # `quantity_found_in` is "none" for every flag actually emitted below
        # (a match on any field short-circuits to `continue`, same as before
        # this per-field search existed) -- it is recorded anyway so
        # `details_json` documents *that* all three ordered fields were
        # checked, not just that the combined text had no match.
        quantity_found_in = "none"
        for field_name, raw_text in text_sources:
            if raw_text and _has_quantity(_clean_html(raw_text)):
                quantity_found_in = field_name
                break
        if quantity_found_in != "none":
            continue

        combined_text = " ".join(_clean_html(raw_text) for _, raw_text in text_sources if raw_text)
        if _is_framework_defined(combined_text):
            continue

        source_id = record.get("source_id", "?")
        subject_id = f"eop:{source_id}"
        title = _short_title(record.get("title") or contract.get("ContractSubject"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            {
                "rule": "missing_quantity",
                "severity": _missing_quantity_severity(value, thresholds),
                "message": (
                    f"Договорът с {contractor} за „{title}“ на стойност {value:,.0f} € "
                    "не посочва количество (брой/обем) на закупеното — без него не може "
                    "да се провери цената за единица. Изисква обяснение."
                ),
                "subject_type": "contract",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "source_id": source_id,
                    "contract_value_eur": value,
                    "type_of_contract": _MISSING_QUANTITY_EOP_TYPE_OF_CONTRACT,
                    "min_value_eur": round(min_value, 2),
                    "quantity_found_in": quantity_found_in,
                },
                "law_ref": law_ref,
                "procurement_id": record.get("id"),
            }
        )
    return out


def _missing_quantity_budget_flags(
    objects_latest: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """MissingQuantityRule, Scope B: a § 52 (придобиване на ДМА) capital
    budget object at the latest reporting period, with `plan_current` or
    `spent_period` at or above `missing_quantity_min_value_eur`, is flagged
    if its (short) `object_name` states no quantity.

    Budget-ledger rows only carry a short object name, not a full technical
    description -- this scope has much lower precision than Scope A, almost
    every object here lacks an explicit count regardless of whether one
    exists in the underlying project documentation. See docs/RULES.md.
    """
    min_value = thresholds.missing_quantity_min_value_eur
    law_ref = thresholds.missing_quantity_budget_law_ref
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
            {
                "rule": "missing_quantity",
                "severity": _missing_quantity_severity(value, thresholds),
                "message": (
                    f"Бюджетният обект „{name}“ с {basis} {basis_value:,.0f} € не "
                    "посочва брой/количество на придобиваните активи — без него не "
                    "може да се провери цената за единица. Изисква обяснение."
                ),
                "subject_type": "budget_object",
                "subject_id": subject_id,
                "subject_key": subject_id,
                "details_json": {
                    "object_name": name,
                    "paragraph": obj.get("paragraph"),
                    "period": obj.get("period"),
                    "plan_current": plan,
                    "spent_period": spent,
                    "min_value_eur": round(min_value, 2),
                },
                "law_ref": law_ref,
            }
        )
    return out


def missing_quantity_flags(
    contracts: list[dict[str, Any]],
    objects_latest: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """MissingQuantityRule (`missing_quantity`): combines Scope A
    (`_missing_quantity_contract_flags`, EOP supply contracts) and Scope B
    (`_missing_quantity_budget_flags`, § 52 capital budget objects). See
    docs/RULES.md for the full writeup, legal basis, and caveats.
    """
    return _missing_quantity_contract_flags(contracts, thresholds) + _missing_quantity_budget_flags(
        objects_latest, thresholds
    )
