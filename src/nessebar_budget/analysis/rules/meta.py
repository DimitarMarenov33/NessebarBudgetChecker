"""Meta rules: run after every other rule, over their output.

`eu_funded_irregularity` (signal, info): a contract that already carries at
least one other flag in this run *and* is EU-funded -- SIGMA `eu_funded`, EOP
`procedure.IsEUFinanced`, or notice wording naming an operational programme /
EU fund (see `linking._EU_TEXT_RE`; a bare "Европейския съюз" is deliberately
not enough, it mostly appears in insurance-territory clauses).

Irregularities involving EU funds are reported to the programme's managing
authority and to OLAF; misuse of EU funds is a crime under чл. 248а of the
Наказателен кодекс (cited per the project brief -- the НК is NOT among the
texts in `docs/law/`, so this citation is not locally verified).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nessebar_budget.analysis.rules._common import TIER_SIGNAL, _short_title, eur, make_flag
from nessebar_budget.analysis.rules.linking import DatasetIndex

META_RULES = frozenset({"eu_funded_irregularity"})


def _flagged_contracts(flags: list[dict[str, Any]]) -> dict[str, set[str]]:
    """contract subject id -> names of the rules that flagged it (directly, or
    as a member of a group flag such as `splitting`)."""
    out: dict[str, set[str]] = defaultdict(set)
    for flag in flags:
        if flag["rule"] in META_RULES:
            continue
        if flag.get("subject_type") == "contract" and flag.get("subject_id"):
            out[flag["subject_id"]].add(flag["rule"])
        for member in (flag.get("details_json") or {}).get("member_ids") or []:
            out[member].add(flag["rule"])
    return out


def eu_funded_irregularity_flags(
    flags: list[dict[str, Any]], index: DatasetIndex
) -> list[dict[str, Any]]:
    flagged = _flagged_contracts(flags)
    merged: dict[str, set[str]] = defaultdict(set)
    for subject_id, rules in flagged.items():
        merged[index.canonical(subject_id)] |= rules

    out: list[dict[str, Any]] = []
    for subject_id, rules in sorted(merged.items()):
        record = index.by_subject.get(subject_id)
        if record is None or not index.is_eu_funded(record):
            continue
        rule_list = sorted(rules)
        title = _short_title(record.get("title"))
        value = record.get("contract_value_eur")
        value_part = f" ({eur(value)})" if value else ""
        out.append(
            make_flag(
                rule="eu_funded_irregularity",
                tier=TIER_SIGNAL,
                severity="info",
                message=(
                    f"Договорът „{title}“{value_part} е финансиран с европейски средства и вече има "
                    f"отбелязан риск ({', '.join(rule_list)})."
                ),
                explanation=(
                    "Този договор е финансиран (изцяло или частично) от Европейския съюз и има поне "
                    "един друг сигнал на този сайт. Нередностите при европейски средства се "
                    "докладват на управляващия орган на програмата и на Европейската служба за "
                    "борба с измамите (OLAF), а злоупотребата с такива средства е престъпление по "
                    "чл. 248а от Наказателния кодекс. Ако другите сигнали получат невинно "
                    "обяснение, отпада и този."
                ),
                documents=[
                    "договор за безвъзмездна финансова помощ",
                    "доклади от проверки на управляващия орган",
                    "междинни и окончателни отчети по проекта",
                    "документите, посочени в другите сигнали за договора",
                ],
                subject_type="contract",
                subject_id=subject_id,
                details={
                    "source": record.get("source"),
                    "source_id": record.get("source_id"),
                    "other_rules": rule_list,
                    "twin": index.twin.get(subject_id),
                },
                law_ref=(
                    "чл. 248а НК (не е сред текстовете в docs/law); докладване на нередности "
                    "пред управляващия орган и OLAF"
                ),
                procurement_id=record.get("id"),
            )
        )
    return out
