"""Per-contract rules: missing value, late publication, annexes, single bidder,
contractor concentration, exceptional (no-notice) procedures, short offer
deadlines, and bids at the estimated-value ceiling.

Legal citations are verified against `docs/law/zop.txt`; the quotes live in
`analysis/thresholds.py` and `docs/RULES.md`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from typing import Any, Protocol

from nessebar_budget.analysis.rules._common import (
    EXCEPTIONAL_PROCEDURES,
    OFFER_PERIOD_PROCEDURES,
    TIER_OPACITY,
    TIER_SIGNAL,
    TIER_VIOLATION,
    _local_date,
    _now,
    _parse_net_date,
    _short_title,
    _to_eur,
    contract_subject_id,
    eop_parts,
    eur,
    is_signed,
    make_flag,
    severity_by_value,
)
from nessebar_budget.analysis.rules.linking import (
    DatasetIndex,
    build_index,
    contract_local_date,
    description_texts,
    estimated_value_eur,
    notice_date,
    tender_number,
)
from nessebar_budget.analysis.thresholds import Thresholds

# ---------------------------------------------------------------------------
# Legacy per-record rule shape (kept for `engine.run_rules` / pipeline.py)
# ---------------------------------------------------------------------------


class Rule(Protocol):
    """A per-record rule: inspects one record and yields zero or more flag dicts."""

    name: str
    severity: str

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]: ...


class MissingValueRule:
    """Flags procurement records whose contract value is missing or zero.

    Kept compatible with its original per-record interface (see
    `analysis.engine.run_rules`); `missing_value_flags` below wraps it with
    the subject/tier/explanation fields the full engine needs.
    """

    name = "missing_value"
    severity = "warning"

    def check(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        # Only signed contracts can be missing a value; open tenders without a
        # contractor/contract date are not yet obliged to show one.
        if not is_signed(record):
            return []
        eur_value = record.get("contract_value_eur")
        bgn_value = record.get("contract_value_bgn")
        if (eur_value is None or eur_value == 0) and (bgn_value is None or bgn_value == 0):
            title = _short_title(record.get("title"))
            return [
                {
                    "rule": self.name,
                    "severity": self.severity,
                    "message": (
                        f"Договорът „{title}“ е публикуван без стойност — не може да се "
                        "провери колко струва и дали е избран правилният ред за възлагане."
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
    """`missing_value` (opacity): `MissingValueRule`, enriched with the full
    flag contract. `records` should carry `source`."""
    rule = MissingValueRule()
    out: list[dict[str, Any]] = []
    for record in records:
        for hit in rule.check(record):
            subject_id = contract_subject_id(record)
            out.append(
                make_flag(
                    rule=hit["rule"],
                    tier=TIER_OPACITY,
                    severity=hit["severity"],
                    message=hit["message"],
                    explanation=(
                        "В регистъра за този договор не е посочена стойност. Без нея гражданите "
                        "не могат да проверят колко пари се харчат и дали стойността е под "
                        "праговете, които позволяват по-лек ред за възлагане. Най-често причината "
                        "е непълно попълнено обявление, но стойността следва да се поиска."
                    ),
                    documents=[
                        "договорът с всички приложения",
                        "обявление за възложена поръчка",
                        "фактури и платежни документи по договора",
                    ],
                    subject_type="contract",
                    subject_id=subject_id,
                    details={
                        "source": record.get("source", "?"),
                        "source_id": record.get("source_id", "?"),
                        "contract_value_eur": record.get("contract_value_eur"),
                        "contract_value_bgn": record.get("contract_value_bgn"),
                    },
                    law_ref=(
                        "чл. 36, ал. 1, т. 12 ЗОП (в регистъра се публикуват договорите с "
                        "приложенията към тях)"
                    ),
                    procurement_id=hit.get("procurement_id"),
                )
            )
    return out


# ---------------------------------------------------------------------------
# late_publication (violation)
# ---------------------------------------------------------------------------


def late_publication_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`late_publication` (violation): an EOP contract whose award notice
    (``TedPublishDate``) was published more than the legal deadline after the
    contract was signed (``ContractDate``).

    ЗОП чл. 26, ал. 1, т. 1: "Възложителите изпращат за публикуване обявление
    за възлагане на поръчка в срок до: 1. тридесет дни след сключване на
    договор за обществена поръчка или рамково споразумение." (30 days.)

    Only `source == "eop"` records carry `raw_json.contract.TedPublishDate`;
    SIGMA records are skipped (no equivalent field observed).
    """
    deadline = thresholds.late_publication_deadline_days
    out: list[dict[str, Any]] = []

    for record in contracts:
        if record.get("source") != "eop":
            continue
        contract, _, _ = eop_parts(record)
        if not contract:
            continue

        contract_date = _parse_net_date(contract.get("ContractDate"))
        ted_publish_date = _parse_net_date(contract.get("TedPublishDate"))
        if contract_date is None or ted_publish_date is None:
            continue

        total_days = (ted_publish_date - contract_date).days
        days_late = total_days - deadline
        if days_late <= thresholds.late_publication_grace_days:
            continue
        if days_late > thresholds.late_publication_high_days:
            severity = "high"
        elif days_late > thresholds.late_publication_warning_days:
            severity = "warning"
        else:
            severity = "info"

        source_id = record.get("source_id", "?")
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            make_flag(
                rule="late_publication",
                tier=TIER_VIOLATION,
                severity=severity,
                message=(
                    f"Обявлението за договора с {contractor} („{title}“) е публикувано "
                    f"{total_days} дни след подписването — с {days_late} дни след законовия "
                    f"{deadline}-дневен срок."
                ),
                explanation=(
                    f"Законът изисква обявлението за възложена поръчка да бъде изпратено за "
                    f"публикуване до {deadline} дни след подписването на договора (чл. 26, ал. 1, "
                    f"т. 1 ЗОП). Тук то е публикувано {total_days} дни след подписването, което "
                    "забавя обществения контрол върху договора и се наказва с глоба (чл. 256а "
                    "ЗОП). Закъснението е невинно само ако обявлението е изпратено навреме и "
                    "забавянето е в самото публикуване — това личи от датата на изпращане."
                ),
                documents=[
                    "обявление за възложена поръчка с датата на изпращане за публикуване",
                    "договорът с датата на подписване",
                ],
                subject_type="contract",
                subject_id=f"eop:{source_id}",
                details={
                    "source_id": source_id,
                    "contract_date": contract_date.isoformat(),
                    "ted_publish_date": ted_publish_date.isoformat(),
                    "deadline_days": deadline,
                    "days_late": days_late,
                },
                law_ref=thresholds.late_publication_law_ref,
                procurement_id=record.get("id"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# annex_growth (signal) / annex_over_cap (violation)
# ---------------------------------------------------------------------------


def _annex_values(
    record: dict[str, Any], thresholds: Thresholds
) -> tuple[float, float, bool] | None:
    """(original EUR, current EUR, same_currency) of an EOP contract, each value
    re-derived from its own currency code -- NOT the raw `...Euro` fields, which
    were found to go stale after an amendment (see docs/RULES.md). None when
    out of scope or when the growth is implausible (> `annex_max_plausible_ratio`,
    a data-entry artifact -- see thresholds.py)."""
    if record.get("source") != "eop":
        return None
    contract, _, _ = eop_parts(record)
    if not contract:
        return None
    original = _to_eur(contract.get("ContractValue"), contract.get("Currency"))
    current = _to_eur(
        contract.get("CurrentContractValue"), contract.get("CurrentContractCurrency")
    )
    if not original or not current or original <= 0:
        return None
    if current > original * thresholds.annex_max_plausible_ratio:
        return None
    same_currency = contract.get("Currency") == contract.get("CurrentContractCurrency")
    return original, current, same_currency


def _over_cap(values: tuple[float, float, bool], thresholds: Thresholds) -> bool:
    """`annex_over_cap` only claims a violation when both values carry the same
    currency code -- no conversion assumption sits between the data and the
    conclusion. Mixed-currency cases above the cap stay `annex_growth` signals."""
    original, current, same_currency = values
    return same_currency and current > original * thresholds.annex_cap_ratio


_ANNEX_DOCUMENTS = [
    "допълнителни споразумения (анекси) към договора",
    "мотиви/обосновка за всяко изменение",
    "обявления за изменение на договора",
    "първоначалната документация с предвидените опции",
]


def annex_growth_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`annex_growth` (signal): an EOP contract whose current value grew by more
    than `annex_growth_ratio` (+10%) but not beyond the legal 50% cap -- above
    the cap, `annex_over_cap` takes over (one flag per contract, never both),
    unless the two values carry different currency codes (then it stays here).
    Growth beyond `annex_max_plausible_ratio` is treated as a data error.
    """
    out: list[dict[str, Any]] = []
    for record in contracts:
        values = _annex_values(record, thresholds)
        if values is None:
            continue
        original_eur, current_eur, _ = values
        if current_eur <= original_eur * thresholds.annex_growth_ratio:
            continue
        if _over_cap(values, thresholds):
            continue  # annex_over_cap

        source_id = record.get("source_id", "?")
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        pct = (current_eur / original_eur - 1) * 100
        out.append(
            make_flag(
                rule="annex_growth",
                tier=TIER_SIGNAL,
                severity="warning",
                message=(
                    f"Стойността на договора с {contractor} („{title}“) е нараснала с "
                    f"{pct:.0f}% спрямо подписаната ({eur(original_eur)} → {eur(current_eur)}) "
                    "— увеличението изисква обяснение."
                ),
                explanation=(
                    "След подписването стойността на договора е увеличена с допълнителни "
                    "споразумения. Големи увеличения може да сочат, че е спечелена ниска "
                    "оферта, която после се „доплаща“ без нова конкуренция; законът позволява "
                    "най-много +50% (чл. 116, ал. 2 ЗОП). Невинно обяснение са непредвидени "
                    "обстоятелства или опции, предвидени още в документацията."
                ),
                documents=_ANNEX_DOCUMENTS,
                subject_type="contract",
                subject_id=f"eop:{source_id}",
                details={
                    "source_id": source_id,
                    "original_value_eur": round(original_eur, 2),
                    "current_value_eur": round(current_eur, 2),
                    "growth_pct": round(pct, 1),
                },
                law_ref=thresholds.annex_growth_law_ref,
                procurement_id=record.get("id"),
            )
        )
    return out


def annex_over_cap_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`annex_over_cap` (violation, high): current value > original × 1.5.

    ЗОП чл. 116, ал. 2: "ако се налага увеличение на цената, то не може да
    надхвърля с повече от 50 на сто стойността на основния договор ...
    Когато се правят последователни изменения, ограничението се прилага за
    общата стойност на измененията." Same value derivation as `annex_growth`;
    requires matching currency codes and a ratio <= `annex_max_plausible_ratio`.
    """
    out: list[dict[str, Any]] = []
    for record in contracts:
        values = _annex_values(record, thresholds)
        if values is None or not _over_cap(values, thresholds):
            continue
        original_eur, current_eur, _ = values

        source_id = record.get("source_id", "?")
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        pct = (current_eur / original_eur - 1) * 100
        out.append(
            make_flag(
                rule="annex_over_cap",
                tier=TIER_VIOLATION,
                severity="high",
                message=(
                    f"Стойността на договора с {contractor} („{title}“) е нараснала с "
                    f"{pct:.0f}% ({eur(original_eur)} → {eur(current_eur)}) — над законовия "
                    "таван от 50% за изменения."
                ),
                explanation=(
                    "Законът позволява увеличение на цената при непредвидени обстоятелства с "
                    "най-много 50% от стойността на основния договор, включително при "
                    f"последователни изменения (чл. 116, ал. 2 ЗОП). Тук увеличението е "
                    f"{pct:.0f}%, което може да означава, че реално е възложена нова поръчка без "
                    "процедура. Изключение има само ако увеличението идва от ясни опции, "
                    "предвидени в първоначалната документация (чл. 116, ал. 1, т. 1), или ако "
                    "стойностите в регистъра са въведени грешно."
                ),
                documents=_ANNEX_DOCUMENTS,
                subject_type="contract",
                subject_id=f"eop:{source_id}",
                details={
                    "source_id": source_id,
                    "original_value_eur": round(original_eur, 2),
                    "current_value_eur": round(current_eur, 2),
                    "growth_pct": round(pct, 1),
                    "cap_ratio": thresholds.annex_cap_ratio,
                },
                law_ref=thresholds.annex_over_cap_law_ref,
                procurement_id=record.get("id"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# single_bidder (signal)
# ---------------------------------------------------------------------------


def single_bidder_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`single_bidder` (signal): a contract awarded after exactly one bid,
    above a value threshold (warning >= 100k EUR, high >= 500k EUR).

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

        source = record.get("source", "?")
        source_id = record.get("source_id", "?")
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        out.append(
            make_flag(
                rule="single_bidder",
                tier=TIER_SIGNAL,
                severity="high" if value >= high_eur else "warning",
                message=(
                    f"Поръчката „{title}“ за {eur(value)} е възложена на {contractor} при само "
                    "една подадена оферта — липсата на конкуренция изисква обяснение."
                ),
                explanation=(
                    "За тази поръчка е постъпила само една оферта. Липсата на конкуренция може "
                    "да сочи към изисквания, написани така, че да отговарят на предварително "
                    "избран участник, което законът забранява (чл. 2, ал. 2 ЗОП). Невинно "
                    "обяснение е тесен пазар или специфичен предмет — това личи от техническата "
                    "спецификация и от изискванията към участниците."
                ),
                documents=[
                    "документация и техническа спецификация на поръчката",
                    "критерии за подбор и методика за оценка",
                    "протокол/доклад на комисията",
                    "решение за определяне на изпълнител",
                ],
                subject_type="contract",
                subject_id=f"{source}:{source_id}",
                details={
                    "source": source,
                    "source_id": source_id,
                    "contract_value_eur": value,
                    "bids_received": 1,
                },
                law_ref=(
                    "чл. 2, ал. 2 ЗОП (забрана за необосновано ограничаване на конкуренцията)"
                ),
                procurement_id=record.get("id"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# contractor_concentration (signal)
# ---------------------------------------------------------------------------


def contractor_concentration_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds, *, now: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """`contractor_concentration` (signal): a contractor with >= 3 contracts and
    >= 15% of total contracted EUR in the trailing 24 months.

    `contracts` should be a *de-duplicated* contract universe -- the engine
    passes only `source == "eop"` records here (EOP and SIGMA overlap ~87%;
    summing both would double-count most spending). See `docs/RULES.md`.
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
        out.append(
            make_flag(
                rule="contractor_concentration",
                tier=TIER_SIGNAL,
                severity="info",
                message=(
                    f"{name} има {n} договора с общината за общо {eur(contractor_eur)} за "
                    f"последните ~24 месеца — {share * 100:.0f}% от стойността на всички "
                    "договори за периода."
                ),
                explanation=(
                    "Голям дял от обществените поръчки на общината отиват при един изпълнител. "
                    "Такава концентрация може да сочи към благоприятстване на определена фирма "
                    "чрез изискванията или оценката. Тя е невинна, ако фирмата е печелила открити "
                    "процедури с много участници или е единствената с нужния капацитет в региона."
                ),
                documents=[
                    "списък на договорите с изпълнителя и броя оферти по всяка процедура",
                    "протоколи и доклади на комисиите",
                    "декларации за липса на конфликт на интереси на членовете на комисиите",
                ],
                subject_type="contractor",
                subject_id=str(key),
                details={
                    "contractor_name": name,
                    "contractor_eik": rows[0].get("contractor_eik"),
                    "contract_count": n,
                    "total_eur": round(contractor_eur, 2),
                    "window_total_eur": round(total_eur, 2),
                    "share": round(share, 4),
                    "window_days": thresholds.concentration_window_days,
                },
                law_ref="чл. 2, ал. 1, т. 1 и 2 ЗОП (равнопоставеност и свободна конкуренция)",
            )
        )
    return out


# ---------------------------------------------------------------------------
# exceptional_procedure (signal)
# ---------------------------------------------------------------------------

_COMMODITY_EXCHANGE_RE = re.compile(r"стокова\s+борса|стокови\s+борси", re.IGNORECASE)
#: Fuels (CPV 09000000 / 091xxxxx): bought by this municipality through the
#: commodity exchange -- confirmed by hand for EOP ids 644-646, whose Решение
#: (not stored in raw_json) cites ЗОП чл. 79, ал. 1, т. 7; see docs/RULES.md.
_FUEL_CPV_PREFIXES = ("09000000", "091")


def exceptional_procedure_flags(
    contracts: list[dict[str, Any]],
    thresholds: Thresholds,
    index: DatasetIndex | None = None,
) -> list[dict[str, Any]]:
    """`exceptional_procedure` (signal): a signed contract awarded through the
    negotiated / no-notice family (see `_common.EXCEPTIONAL_PROCEDURES` for the
    verified mapping of EOP enum strings and SIGMA labels).

    Severity by value: info < 100k, warning >= 100k, high >= 500k EUR -- except
    that a contract whose texts name a commodity exchange ("стокова борса", the
    ground in ЗОП чл. 79, ал. 1, т. 7 / чл. 191, ал. 1, т. 6), or whose main
    CPV is fuel (09000000 / 091*), is kept at `info`: the ground is visible and
    routinely valid. (Tuned after hand-checking the top-2 raw flags, EOP ids
    646/644 -- fuel bought on the exchange, see docs/RULES.md.)

    EOP is canonical; a SIGMA record is only considered when its УНП has no
    EOP record at all (otherwise it is the same contract twice).
    """
    index = index or build_index(contracts)
    out: list[dict[str, Any]] = []
    for record in contracts:
        if not is_signed(record):
            continue
        source = record.get("source")
        procedure_type = record.get("procedure_type")
        if procedure_type not in EXCEPTIONAL_PROCEDURES:
            continue
        if source == "sigma" and tender_number(record) in index.eop_tenders:
            continue
        if source not in ("eop", "sigma"):
            continue

        label, grounds, fine = EXCEPTIONAL_PROCEDURES[procedure_type]
        value = float(record.get("contract_value_eur") or 0)
        texts = " ".join([str(record.get("title") or "")] + description_texts(record))
        commodity_exchange = bool(_COMMODITY_EXCHANGE_RE.search(texts))
        fuel = str(record.get("cpv_code") or "").startswith(_FUEL_CPV_PREFIXES)
        severity = (
            "info"
            if commodity_exchange or fuel
            else severity_by_value(
                value, thresholds.exceptional_warning_eur, thresholds.exceptional_high_eur
            )
        )
        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        fine_sentence = (
            f" Неоснователното използване се наказва с глоба ({fine})."
            if fine
            else ""
        )
        if commodity_exchange:
            ground_hint = (
                " Текстовете на поръчката споменават стокова борса — основание, което законът "
                "допуска (чл. 79, ал. 1, т. 7 ЗОП), ако е мотивирано в решението."
            )
        elif fuel:
            ground_hint = (
                " Предметът са горива, които общината купува чрез стокова борса — основание, "
                "което законът допуска (чл. 79, ал. 1, т. 7 ЗОП); то следва да е мотивирано в "
                "решението."
            )
        else:
            ground_hint = " Ако основанието е налице и е мотивирано в решението, процедурата е законна."
        out.append(
            make_flag(
                rule="exceptional_procedure",
                tier=TIER_SIGNAL,
                severity=severity,
                message=(
                    f"Договорът с {contractor} за „{title}“ ({eur(value)}) е възложен чрез "
                    f"{label} — без публично обявление; основанието изисква проверка."
                ),
                explanation=(
                    "При тази процедура общината преговаря директно с избрани от нея фирми, "
                    "вместо да обяви поръчката публично. Законът допуска това само при изрично "
                    f"изброени основания ({grounds}), които трябва да са мотивирани в решението "
                    "за откриване — иначе конкуренцията се заобикаля." + fine_sentence + ground_hint
                ),
                documents=[
                    "решение за откриване на процедурата с мотивите за избраното основание",
                    "покана(и) до поканените лица",
                    "протокол от преговорите",
                    "доказателства за основанието (напр. неотложност, изключителни права)",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                details={
                    "source": source,
                    "source_id": record.get("source_id"),
                    "procedure_type": procedure_type,
                    "procedure_label": label,
                    "contract_value_eur": value,
                    "commodity_exchange_mentioned": commodity_exchange,
                    "fuel_cpv": fuel,
                },
                law_ref=grounds + (f"; {fine} (глоба)" if fine else ""),
                procurement_id=record.get("id"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# short_offer_deadline (signal / violation)
# ---------------------------------------------------------------------------


def short_offer_deadline_flags(
    contracts: list[dict[str, Any]], thresholds: Thresholds
) -> list[dict[str, Any]]:
    """`short_offer_deadline`: the procedure was announced too shortly before
    the contract was signed (or its offer period was shorter than the law's
    minimum).

    - legal minimum offer period: open 30 days (ЗОП чл. 74, ал. 1), публично
      състезание 20 (чл. 178, ал. 2), събиране на оферти с обява 10 (чл. 188,
      ал. 1); "bare" minimum after every permitted reduction: 15 / 10 / 10;
    - notice date: earliest of the offer-phase start and the publication date
      (≈ the sending date the law counts from), see `linking.notice_date`;
    - signal: notice→contract < legal minimum + `offer_evaluation_margin_days`,
      or the published offer period itself < legal minimum (a shortened
      period must be motivated, чл. 74, ал. 5 / чл. 178, ал. 5);
    - violation: notice→contract or the offer period < the bare minimum (the
      contract was signed before even the shortest lawful offer period could
      end). Обяви announced before 22.12.2023 (current чл. 188 wording) are
      never called a violation.
    """
    out: list[dict[str, Any]] = []
    margin = thresholds.offer_evaluation_margin_days
    for record in contracts:
        if record.get("source") != "eop" or not is_signed(record):
            continue
        spec = OFFER_PERIOD_PROCEDURES.get(record.get("procedure_type") or "")
        if spec is None:
            continue
        label, prefix, article = spec
        legal_min = getattr(thresholds, f"offer_min_days_{prefix}")
        bare_min = getattr(thresholds, f"offer_bare_min_days_{prefix}")

        announced = notice_date(record)
        signed = contract_local_date(record)
        if announced is None or signed is None:
            continue
        days_to_contract = (signed - announced).days
        _, _, detail = eop_parts(record)
        offer_end = _local_date(detail.get("OfferPhaseEndDate"))
        offer_days = (offer_end - announced).days if offer_end else None

        violation = days_to_contract < bare_min or (offer_days is not None and offer_days < bare_min)
        if (
            violation
            and prefix == "collecting_offers"
            and announced < thresholds.collecting_offers_rule_effective_from
        ):
            violation = False
        signal = days_to_contract < legal_min + margin or (
            offer_days is not None and offer_days < legal_min
        )
        if not (violation or signal):
            continue

        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        offer_part = f", срокът за оферти — {offer_days} дни" if offer_days is not None else ""
        if violation:
            tier, severity = TIER_VIOLATION, "high"
            explanation = (
                f"За {label} законът определя минимален срок за подаване на оферти от "
                f"{legal_min} дни ({article}), а дори при всички позволени съкращения — не по-малко "
                f"от {bare_min} дни. Тук от обявяването до подписването на договора са минали само "
                f"{days_to_contract} дни{offer_part}, т.е. фирмите не са имали законовото време да "
                "подадат оферти. Невинно обяснение би било само грешна дата в регистъра."
            )
        else:
            tier, severity = TIER_SIGNAL, "warning"
            explanation = (
                f"За {label} минималният срок за оферти е {legal_min} дни ({article}), след което "
                "комисията трябва да отвори и оцени офертите. Тук от обявяването до подписването са "
                f"минали {days_to_contract} дни{offer_part}, което оставя много малко време за "
                "подготовка на оферти и може да сочи, че изпълнителят е бил известен предварително. "
                "Невинно обяснение е мотивирана спешност или съкратен срок след предварително "
                "обявление."
            )
        out.append(
            make_flag(
                rule="short_offer_deadline",
                tier=tier,
                severity=severity,
                message=(
                    f"Договорът с {contractor} за „{title}“ е подписан {days_to_contract} дни след "
                    f"обявяването на поръчката ({label}, законов срок за оферти {legal_min} дни)."
                ),
                explanation=explanation,
                documents=[
                    "обявление/обява с датата на изпращане за публикуване",
                    "протокол от отварянето на офертите",
                    "протокол/доклад на комисията",
                    "решение за определяне на изпълнител и датата на уведомяване",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                details={
                    "source_id": record.get("source_id"),
                    "procedure_type": record.get("procedure_type"),
                    "notice_date": announced.isoformat(),
                    "contract_date": signed.isoformat(),
                    "days_to_contract": days_to_contract,
                    "offer_period_days": offer_days,
                    "legal_min_days": legal_min,
                    "bare_min_days": bare_min,
                    "evaluation_margin_days": margin,
                },
                law_ref=f"{article} (минимален срок за получаване на оферти)",
                procurement_id=record.get("id"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# bid_at_ceiling (signal)
# ---------------------------------------------------------------------------


def bid_at_ceiling_flags(
    contracts: list[dict[str, Any]],
    thresholds: Thresholds,
    index: DatasetIndex | None = None,
) -> list[dict[str, Any]]:
    """`bid_at_ceiling` (signal): contract value >= 98% of the estimated value
    (and <= 105%: further above it the data shows VAT/currency/lot mismatches,
    not price signals) AND one bid or an unknown number of bids.

    The estimated value is tender-level, so only tenders with exactly one
    signed contract are compared (a lot's value against the whole tender's
    estimate would be meaningless). `bids_received` comes from the SIGMA twin
    (see `linking`). warning >= 100k, high >= 500k EUR; below 100k not flagged.
    """
    index = index or build_index(contracts)
    out: list[dict[str, Any]] = []
    for record in contracts:
        if record.get("source") != "eop" or not is_signed(record):
            continue
        number = tender_number(record)
        if not number or len(index.eop_by_tender.get(number, [])) != 1:
            continue
        value = record.get("contract_value_eur")
        estimate = estimated_value_eur(record)
        if not value or not estimate:
            continue
        ratio = float(value) / estimate
        if ratio < thresholds.bid_ceiling_ratio or ratio > thresholds.bid_ceiling_max_ratio:
            continue
        if value < thresholds.bid_ceiling_warning_eur:
            continue
        bids = index.bids(record)
        if bids is not None and bids >= 2:
            continue

        title = _short_title(record.get("title"))
        contractor = record.get("contractor_name") or "изпълнителя"
        bids_text = "само една оферта" if bids == 1 else "неизвестен брой оферти"
        out.append(
            make_flag(
                rule="bid_at_ceiling",
                tier=TIER_SIGNAL,
                severity="high" if value >= thresholds.bid_ceiling_high_eur else "warning",
                message=(
                    f"Договорът с {contractor} за „{title}“ е за {eur(value)} — "
                    f"{ratio * 100:.1f}% от прогнозната стойност ({eur(estimate)}), при "
                    f"{bids_text}."
                ),
                explanation=(
                    "Цената по договора практически съвпада с максималната (прогнозна) стойност, "
                    "определена от самата община, а конкуренция няма или не е известна. Така "
                    "цената на практика е определена от възложителя, а не от пазара, и може да е "
                    "завишена. Това е невинно, ако прогнозната стойност е обоснована с реално "
                    "пазарно проучване или ако договорът е по единични цени с таван."
                ),
                documents=[
                    "обосновка на прогнозната стойност (пазарно проучване, получени оферти)",
                    "ценово предложение на изпълнителя",
                    "протокол на комисията с броя на подадените оферти",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                details={
                    "source_id": record.get("source_id"),
                    "tender_number": number,
                    "contract_value_eur": round(float(value), 2),
                    "estimated_value_eur": round(estimate, 2),
                    "ratio": round(ratio, 4),
                    "bids_received": bids,
                    "sigma_twin": index.twin.get(contract_subject_id(record)),
                },
                law_ref=(
                    "чл. 21, ал. 1-2 ЗОП (прогнозната стойност, вкл. чрез пазарни проучвания); "
                    "чл. 2, ал. 2 ЗОП (конкуренция)"
                ),
                procurement_id=record.get("id"),
            )
        )
    return out
