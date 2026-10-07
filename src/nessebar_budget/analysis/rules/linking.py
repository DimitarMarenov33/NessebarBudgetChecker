"""Cross-source context the dataset-level rules share: EOP <-> SIGMA "twin"
linking, tender grouping, estimated values, offer-phase dates, EU funding.

EOP and SIGMA publish largely the same contracts (~87% of EOP contracts have
a same-EIK-same-value SIGMA twin, see `docs/RULES.md`). SIGMA's `raw_json.unp`
is the same unique procurement number as EOP's
`raw_json.contract.TenderNumber` / `procedure.SpecialNumber`
(e.g. "00126-2020-0023"), so a twin is a SIGMA record with the same unp and
contractor EIK. Only SIGMA carries `bids_received` and `eu_funded`; only EOP
carries dates, estimated values and notice texts -- linking lets each rule use
both without double-counting.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from nessebar_budget.analysis.rules._common import (
    _clean_html,
    _local_date,
    _to_eur,
    contract_subject_id,
    eop_parts,
    is_signed,
)

#: Wording that marks a procurement as EU/programme-funded in notice text.
#: Deliberately excludes a bare "Европейския съюз": in this data it mostly
#: appears in insurance territory clauses ("... на територията на Република
#: България и Европейския съюз") and product-origin fields, not funding.
_EU_TEXT_RE = re.compile(
    r"оперативна програма|програма за развитие на селските райони|\bПРСР\b"
    r"|план(?:а)? за възстановяване и устойчивост|механизъм(?:а)? за възстановяване"
    r"|interreg|кохезионн|европейски(?:я)? (?:фонд|земеделски фонд|социален фонд)"
    r"|\bЕФРР\b|\bЕЗФРСР\b|безвъзмездна финансова помощ"
    r"|(?:съ)?финансиран\w*\s+(?:\S+\s+){0,4}(?:от|по)\s+(?:Европейския съюз|ЕС)\b",
    re.IGNORECASE,
)


def tender_number(record: dict[str, Any]) -> str | None:
    """The unique procurement number (УНП) for an EOP or SIGMA record."""
    raw = record.get("raw_json") or {}
    if record.get("source") == "sigma":
        return raw.get("unp")
    contract, procedure, detail = eop_parts(record)
    return contract.get("TenderNumber") or procedure.get("SpecialNumber") or detail.get(
        "SpecialNumber"
    )


def type_of_contract(record: dict[str, Any]) -> int | None:
    contract, procedure, detail = eop_parts(record)
    for source in (contract, procedure, detail):
        value = source.get("TypeOfContract")
        if value in (1, 2, 3):
            return value
    return None


def estimated_value_eur(record: dict[str, Any]) -> float | None:
    """Tender-level estimated value (без ДДС) in EUR, from the EOP payload."""
    _, procedure, detail = eop_parts(record)
    value = _to_eur(procedure.get("EstimatedValue"), procedure.get("Currency"))
    if value is None:
        value = _to_eur(detail.get("EstimatedValue"), detail.get("CurrencyType"))
    if value is None and record.get("estimated_value_eur"):
        value = float(record["estimated_value_eur"])
    return value if value and value > 0 else None


def notice_date(record: dict[str, Any]):
    """Local date the procedure was announced: the earliest of the offer-phase
    start (≈ the date the notice was sent) and the publication date, falling
    back to `published_at`. The earliest date gives the *longest* interval, so
    every "too short" conclusion built on it is conservative."""
    _, _, detail = eop_parts(record)
    candidates = [
        _local_date(detail.get("OfferPhaseStartDate")),
        _local_date(detail.get("PublicationDate")),
    ]
    candidates = [c for c in candidates if c is not None]
    if candidates:
        return min(candidates)
    return _local_date(record.get("published_at"))


def contract_local_date(record: dict[str, Any]):
    contract, _, _ = eop_parts(record)
    return _local_date(contract.get("ContractDate")) or _local_date(record.get("contract_date"))


def description_texts(record: dict[str, Any]) -> list[str]:
    """Every published description of the subject (not the title): the
    tender description, each notice's short/lot description, and the parsed
    `notice_text` -- HTML-cleaned."""
    _, _, detail = eop_parts(record)
    texts = [_clean_html(detail.get("TenderDescription")), _clean_html(detail.get("notice_text"))]
    for notice in detail.get("notices") or []:
        texts.append(_clean_html(notice.get("short_description")))
        texts.extend(_clean_html(t) for t in notice.get("lot_descriptions") or [])
    return [t for t in texts if t and t.strip()]


def eop_mentions_eu(record: dict[str, Any]) -> bool:
    _, procedure, detail = eop_parts(record)
    if procedure.get("IsEUFinanced") is True:
        return True
    text = " ".join(
        [str(detail.get("TenderName") or "")] + description_texts(record)
    )
    return bool(_EU_TEXT_RE.search(text))


@dataclass
class DatasetIndex:
    """Lookups shared by the dataset-level rules (built once per run)."""

    by_subject: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: EOP signed contracts grouped by tender number.
    eop_by_tender: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    #: subject id -> twin subject id (both directions).
    twin: dict[str, str] = field(default_factory=dict)
    #: tender numbers present in EOP (any record, signed or not).
    eop_tenders: set[str] = field(default_factory=set)

    def bids(self, record: dict[str, Any]) -> int | None:
        if record.get("bids_received") is not None:
            return record["bids_received"]
        twin = self.twin.get(contract_subject_id(record))
        if twin and twin in self.by_subject:
            return self.by_subject[twin].get("bids_received")
        return None

    def is_eu_funded(self, record: dict[str, Any]) -> bool:
        if _record_eu_flag(record):
            return True
        twin = self.twin.get(contract_subject_id(record))
        return bool(twin and twin in self.by_subject and _record_eu_flag(self.by_subject[twin]))

    def canonical(self, subject_id: str) -> str:
        """Prefer the EOP side of an EOP/SIGMA twin pair."""
        twin = self.twin.get(subject_id)
        if twin and subject_id.startswith("sigma:") and twin.startswith("eop:"):
            return twin
        return subject_id


def _record_eu_flag(record: dict[str, Any]) -> bool:
    if record.get("source") == "sigma":
        return bool((record.get("raw_json") or {}).get("eu_funded"))
    if record.get("source") == "eop":
        return eop_mentions_eu(record)
    return False


def build_index(contracts: list[dict[str, Any]]) -> DatasetIndex:
    index = DatasetIndex()
    sigma_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    eop_by_tender: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in contracts:
        index.by_subject[contract_subject_id(record)] = record
        number = tender_number(record)
        if record.get("source") == "sigma" and number and record.get("contractor_eik"):
            sigma_by_key[(number, str(record["contractor_eik"]))].append(record)
        elif record.get("source") == "eop" and number:
            index.eop_tenders.add(number)
            if is_signed(record):
                eop_by_tender[number].append(record)
    index.eop_by_tender = dict(eop_by_tender)

    for number, rows in eop_by_tender.items():
        for record in rows:
            eik = record.get("contractor_eik")
            if not eik:
                continue
            candidates = sigma_by_key.get((number, str(eik)), [])
            twin = _closest_by_value(record, candidates)
            if twin is not None:
                a, b = contract_subject_id(record), contract_subject_id(twin)
                index.twin.setdefault(a, b)
                index.twin.setdefault(b, a)
    return index


def _closest_by_value(record: dict[str, Any], candidates: list[dict[str, Any]]):
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    value = record.get("contract_value_eur") or 0
    best = min(candidates, key=lambda c: abs((c.get("contract_value_eur") or 0) - value))
    best_value = best.get("contract_value_eur") or 0
    if value and abs(best_value - value) <= 0.02 * value:
        return best
    return None
