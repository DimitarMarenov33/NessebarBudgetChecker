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
                    f"Поръчка „{title}“ на стойност {value:,.0f} EUR е възложена на "
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
                    f"{name} е сключил(а) {n} договора за общо {contractor_eur:,.0f} EUR "
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
                f"разход {spent:,.0f} EUR при план {plan:,.0f} EUR за годината "
                f"(с {over_plan:,.0f} EUR над допустимото отклонение)"
            )
        if over_estimate_flag:
            reasons.append(
                f"общо усвоени {cumulative:,.0f} EUR при пълна прогнозна стойност на обекта "
                f"{float(estimated_total):,.0f} EUR"
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
                        f"Планът за обект „{name}“ скача от {prev_plan:,.0f} EUR "
                        f"({prev['period']}) на {cur_plan:,.0f} EUR ({cur['period']}) — "
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
                        f"{first_plan:,.0f} EUR — изисква обяснение за произхода на средствата."
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
                    f"За обект „{name}“ с разход {spent:,.0f} EUR не открихме публикуван "
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
