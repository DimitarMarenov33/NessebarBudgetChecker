"""Dataset-level rules linking contractors to the Trade Register
(Търговски регистър, `db.models.Company`/`CompanyPerson`) and to municipal
officials' declarations of interest (`db.models.Official`).

All five rules degrade to `[]` when `companies`/`officials` is empty (the
scrapers that populate those tables may not have run yet) -- every function
below checks this first. Legal citations are verified against
`docs/law/zop.txt`/`zmsma.txt`; the quotes live in `analysis/thresholds.py`
and `docs/RULES.md`. `docs/RULES.md` also carries the one unverified
citation used here (Закон за счетоводството, чл. 38 -- not in `docs/law/`).

Two input shapes recur in every function here (built by `analysis.engine`
from the ORM rows -- see `run_full_analysis`):

- a *company* dict: `{"eik", "name", "status", "nkid_code", "nkid_label",
  "registered_at", "last_annual_report_year", "source_url", "people"}`,
  where `people` is a list of `{"name", "name_normalized", "role",
  "share_text", "person_eik", "is_current", "field_ident"}` (one per
  `CompanyPerson` row for that company);
- an *official* dict: `{"name", "name_normalized", "role", "mandate",
  "source_url", "document_url", "declared_interests_json"}`, where
  `declared_interests_json` is a list of `{"company_name", "eik",
  "relation", "raw"}`.

`contracts` is always the de-duplicated EOP contract universe (same
`source == "eop"` list `contractor_concentration_flags` uses -- see its
docstring): EOP and SIGMA overlap ~87%, so summing both would double-count
most spending.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from nessebar_budget.analysis.provenance import (
    contract_sources,
    declaration_source,
    registry_source,
)
from nessebar_budget.analysis.rules._common import (
    _WS_RE,
    TIER_SIGNAL,
    _local_date,
    _now,
    contract_subject_id,
    eur,
    make_flag,
)
from nessebar_budget.analysis.rules.contracts import contractor_key
from nessebar_budget.analysis.thresholds import ACTIVITY_INCOMPATIBLE_PAIRS, Thresholds
from nessebar_budget.scrapers.registry import normalize_eiks

#: `CompanyPerson.role` values that make someone "stand behind" a company for
#: `related_party`/`person_concentration` -- a former ('is_current' False)
#: or merely procedural role ('representative', 'liquidator', 'other') does
#: not count.
_CURRENT_ROLES = ("manager", "partner", "sole_owner", "board_member")

_COMPANY_ROLE_LABELS_BG = {
    "manager": "управител",
    "partner": "съдружник",
    "sole_owner": "едноличен собственик на капитала",
    "board_member": "член на съвета",
    "representative": "представител",
    "liquidator": "ликвидатор",
    "other": "свързано лице",
}

_OFFICIAL_ROLE_LABELS_BG = {
    "councillor": "общински съветник",
    "mayor": "кмет",
    "deputy_mayor": "заместник-кмет",
    "village_mayor": "кметски наместник",
    "secretary": "секретар на общината",
    "other": "длъжностно лице",
}

_STATUS_LABELS_BG = {
    "liquidation": "производство по ликвидация",
    "insolvency": "производство по несъстоятелност",
    "deregistered": "заличена от Търговския регистър",
}

#: Registry fields the site shows for a Company ЕИК the data uses as a
#: placeholder (an individual's EIK/ЕГН is legally allowed to stay
#: unpublished) -- see `web.build._EIK_PLACEHOLDER`. Duplicated here (a
#: one-line string constant) rather than imported, to keep this analysis
#: module independent of the web layer.
_EIK_PLACEHOLDER = "не се публикува"

_COMPANY_CONTRACT_FIELDS = (
    "ContractNumber", "TenderNumber", "ContractDate", "ContractValue", "Currency", "SupplierName",
)
_SIGMA_CONTRACT_FIELDS = ("id", "unp", "subject", "contractor", "value_eur")

_COMPANY_DOCUMENTS = [
    "актуална справка от Търговския регистър за фирмата",
    "декларацията за интереси на съответното лице от Регистъра на декларациите",
]


def _role_label(role: str | None) -> str:
    return _COMPANY_ROLE_LABELS_BG.get(role or "", role or "свързано лице")


def _official_role_label(role: str | None) -> str:
    return _OFFICIAL_ROLE_LABELS_BG.get(role or "", role or "длъжностно лице")


def _normalize_company_name(text: str | None) -> str:
    """Lower-case, letters/digits/spaces only, collapsed whitespace -- the
    same convention the Trade Register scraper uses for `name_normalized`
    (see `db.models.CompanyPerson`/`Official`), applied here to
    `Company.name` (which has no precomputed normalized column) and to
    `declared_interests_json[].company_name`, so the two can be compared."""
    if not text:
        return ""
    cleaned = "".join(ch if (ch.isalnum() or ch.isspace()) else " " for ch in text.lower())
    return _WS_RE.sub(" ", cleaned).strip()


def _current_people(company: dict[str, Any]) -> list[dict[str, Any]]:
    """Current managers/partners/sole owners/board members of `company`."""
    return [
        p
        for p in company.get("people") or []
        if p.get("is_current", True) and p.get("role") in _CURRENT_ROLES
    ]


def _company_eik(company: dict[str, Any]) -> str:
    """The company's ЕИК in the same canonical form `_contracts_by_eik` keys
    on (9-digit, zero-padded), or "" when the row has no usable code."""
    codes = normalize_eiks(company.get("eik"))
    return codes[0] if codes else ""


def _contract_eik_pairs(contracts: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """(contract, ЕИК) for every canonical ЕИК a contract names -- one pair
    per member for a consortium, none for the "не се публикува" placeholder."""
    return [(record, eik) for record in contracts for eik in normalize_eiks(record.get("contractor_eik"))]


def _page_subject_id(eik: str, rows: list[dict[str, Any]]) -> str:
    """The contractor key the site uses for this company's page
    (`contractors/<key>.html` is keyed on the EOP row's raw ЕИК). Prefers a
    contract the company won alone, so a company that also took part in a
    consortium ("200948893; 204901777") still links to its own page."""
    for record in rows:
        if normalize_eiks(record.get("contractor_eik")) == [eik]:
            return contractor_key(record)
    return contractor_key(rows[0])


def _contracts_by_eik(contracts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """EOP contracts grouped by `contractor_eik`, skipping rows without a
    real EIK (missing, or the "не се публикува" placeholder)."""
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in contracts:
        # One contract can name two codes (a consortium) -- it then counts
        # for each member company; codes are padded to the registry's
        # 9-digit form so "646811" meets the deed stored as 000646811.
        for eik in normalize_eiks(record.get("contractor_eik")):
            out[eik].append(record)
    return out


def _contract_totals(rows: list[dict[str, Any]]) -> tuple[int, float]:
    count = len(rows)
    total = sum(float(r["contract_value_eur"]) for r in rows if r.get("contract_value_eur"))
    return count, total


#: Sort fallback for a (rare) contract row with no `contract_date` -- a
#: naive-UTC epoch, matching this project's naive-but-UTC datetime
#: convention (see e.g. `db.repo.py`'s `_now()`), so it never fails to
#: compare against the real (naive) `contract_date` values.
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC).replace(tzinfo=None)


def _contract_sources_for(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        src
        for r in sorted(rows, key=lambda r: r.get("contract_date") or _EPOCH)
        for src in contract_sources(
            r,
            contract_fields=_COMPANY_CONTRACT_FIELDS,
            tender_fields=None,
            sigma_fields=_SIGMA_CONTRACT_FIELDS,
        )
    ]


def _registry_field(name: str, label: str, value: Any) -> dict[str, Any]:
    return {"name": name, "label": label, "value": value, "value_eur": None}


# ---------------------------------------------------------------------------
# related_party (signal)
# ---------------------------------------------------------------------------


def related_party_flags(
    contracts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    officials: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`related_party` (signal): a current manager/partner/sole_owner/
    board_member of a contractor shares a (>= 3-token) full name with an
    `Official`, or an official's declared interests name the contractor
    (same EIK, or the same normalized company name). One flag per
    (contractor, official) pair that matches on either basis -- a
    declaration match on its own is kept over a mere name match for the
    same pair (stronger evidence, see the loop below).

    ЗОП чл. 54, ал. 1, т. 7 (и ал. 2): the contracting authority must
    exclude a bidder with an unremovable conflict of interest, extended to
    the people representing it and its governing/supervisory bodies. ЗМСМА
    чл. 37, ал. 1 additionally bars a *councillor* from taking part in
    decisions touching their own property interests.
    """
    if not companies or not officials:
        return []
    contracts_by_eik = _contracts_by_eik(contracts)

    officials_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for official in officials:
        name_norm = (official.get("name_normalized") or "").strip()
        if name_norm:
            officials_by_name[name_norm].append(official)

    out: list[dict[str, Any]] = []
    for company in companies:
        eik = _company_eik(company)
        if not eik or eik == _EIK_PLACEHOLDER:
            continue
        rows = contracts_by_eik.get(eik, [])
        if not rows:
            continue
        contract_count, total_eur = _contract_totals(rows)
        subject_id = _page_subject_id(eik, rows)
        company_name = company.get("name") or eik
        company_name_norm = _normalize_company_name(company.get("name"))

        # official.name_normalized -> (official, basis, person|None, interest|None)
        matches: dict[str, tuple[dict[str, Any], str, dict[str, Any] | None, dict[str, Any] | None]] = {}

        for person in _current_people(company):
            name_norm = (person.get("name_normalized") or "").strip()
            if len(name_norm.split()) < 3:
                continue  # at least three tokens: first/patronymic/family name
            for official in officials_by_name.get(name_norm, []):
                key = f"{official.get('name_normalized', '')}|{official.get('mandate') or ''}"
                matches.setdefault(key, (official, "name", person, None))

        for official in officials:
            for interest in official.get("declared_interests_json") or []:
                if not isinstance(interest, dict):
                    continue
                interest_eik = (interest.get("eik") or "").strip()
                interest_name_norm = _normalize_company_name(interest.get("company_name"))
                matched_eik = bool(interest_eik) and interest_eik == eik
                matched_name = bool(interest_name_norm) and interest_name_norm == company_name_norm
                if matched_eik or matched_name:
                    key = f"{official.get('name_normalized', '')}|{official.get('mandate') or ''}"
                    # A declaration is stronger evidence than a bare name match
                    # for the same (contractor, official) pair -- keep it.
                    matches[key] = (official, "declaration", None, interest)

        for official, basis, person, interest in matches.values():
            severity = "high" if basis == "declaration" or total_eur >= thresholds.related_party_high_eur else "warning"
            official_name = official.get("name") or "длъжностно лице"
            official_role = _official_role_label(official.get("role"))
            mandate = official.get("mandate")
            mandate_part = f", мандат {mandate}" if mandate else ""

            if basis == "name":
                role_label = _role_label(person.get("role")) if person else "свързано лице"
                message = (
                    f"{official_name} ({official_role}{mandate_part}) носи същото име като "
                    f"{role_label} на „{company_name}“ — изпълнител с {contract_count} договор(и) "
                    f"за общо {eur(total_eur)} с общината."
                )
                explanation = (
                    f"В Търговския регистър „{company_name}“ има {role_label} на име „{person.get('name')}“ "
                    f"— същото (три имена) като {official_name}, {official_role} в община Несебър. "
                    "Съвпадение на имена не доказва нищо само по себе си — може да става дума за "
                    "съвпадение между различни лица (namesake). Ако все пак е едно и също лице, е "
                    "налице основание по чл. 54, ал. 1, т. 7 ЗОП за отстраняване на изпълнителя. За "
                    "да се провери, трябва да се разгледа декларацията за интереси на лицето."
                )
            else:
                relation = (interest or {}).get("relation") or "декларирана връзка"
                message = (
                    f"{official_name} ({official_role}{mandate_part}) е декларирал/а {relation} с "
                    f"„{company_name}“ — изпълнител с {contract_count} договор(и) за общо "
                    f"{eur(total_eur)} с общината."
                )
                explanation = (
                    f"{official_name}, {official_role} в община Несебър, е декларирал/а в декларацията "
                    f"си за интереси връзка с „{company_name}“ ({relation}), а тази фирма е изпълнител "
                    f"на {contract_count} договор(и) с общината за общо {eur(total_eur)}. Декларацията "
                    "е самото доказателство за връзката, затова случаят е по-сигурен от съвпадение на "
                    "имена — остава да се провери дали лицето е участвало в решения, свързани с тази "
                    "фирма (чл. 37, ал. 1 ЗМСМА за общински съветник), или дали фирмата е следвало да "
                    "бъде отстранена при възлагането (чл. 54, ал. 1, т. 7 ЗОП)."
                )

            law_ref = "чл. 54, ал. 1, т. 7 и ал. 2 ЗОП (конфликт на интереси, който не може да бъде отстранен)"
            if official.get("role") == "councillor":
                law_ref += (
                    "; чл. 37, ал. 1 ЗМСМА (общинският съветник не участва при вземане на решения, "
                    "отнасящи се до негови имуществени интереси)"
                )

            registry_fields = [_registry_field("name", "Наименование", company_name)]
            if person is not None:
                registry_fields = [
                    _registry_field("name", "Лице в регистъра", person.get("name")),
                    _registry_field("role", "Роля", _role_label(person.get("role"))),
                ]
                if person.get("share_text"):
                    registry_fields.append(_registry_field("share", "Дял", person["share_text"]))

            declaration_fields = [
                _registry_field("name", "Име", official.get("name")),
                _registry_field("role", "Длъжност", official_role),
            ]
            if mandate:
                declaration_fields.append(_registry_field("mandate", "Мандат", mandate))
            declaration_note = None
            if interest is not None and interest.get("raw"):
                declaration_fields.append(_registry_field("raw", "Декларирано", interest["raw"]))
            elif basis == "name":
                declaration_note = (
                    "Все още не е открита декларирана връзка с тази фирма в декларацията — "
                    "съвпадението е само по име и трябва да се провери в самия документ."
                )

            sources = [
                registry_source(company, registry_fields),
                declaration_source(official, declaration_fields, note=declaration_note),
                *_contract_sources_for(rows),
            ]

            out.append(
                make_flag(
                    rule="related_party",
                    tier=TIER_SIGNAL,
                    severity=severity,
                    message=message,
                    explanation=explanation,
                    documents=[
                        *_COMPANY_DOCUMENTS,
                        "протоколи от заседания/решения, свързани с договорите на тази фирма",
                    ],
                    subject_type="contractor",
                    subject_id=subject_id,
                    subject_key=f"{subject_id}:{official.get('name_normalized', '')}:{mandate or ''}",
                    details={
                        "contractor_eik": eik,
                        "contractor_name": company_name,
                        "official_name": official.get("name"),
                        "official_role": official.get("role"),
                        "official_mandate": mandate,
                        "person_role": person.get("role") if person else None,
                        "match_basis": basis,
                        "contract_count": contract_count,
                        "total_eur": round(total_eur, 2),
                    },
                    law_ref=law_ref,
                    sources=sources,
                )
            )
    return out


# ---------------------------------------------------------------------------
# person_concentration (signal)
# ---------------------------------------------------------------------------


def person_concentration_flags(
    contracts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`person_concentration` (signal): one person (by `name_normalized`,
    current roles only) stands behind 2+ distinct contractor companies that
    TOGETHER won >= `person_concentration_min_contracts` contracts or
    >= `person_concentration_min_eur` from the municipality.

    Legal anchor: ЗОП чл. 2, ал. 1, т. 1-2 (равнопоставеност, свободна
    конкуренция) -- the same one `contractor_concentration` uses, for the
    same reason: splitting business across several formally separate
    companies run by the same person defeats the point of measuring
    concentration per legal entity.
    """
    if not companies:
        return []
    contracts_by_eik = _contracts_by_eik(contracts)

    # name_normalized -> eik -> (company, [persons])
    by_name: dict[str, dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]] = defaultdict(dict)
    for company in companies:
        eik = _company_eik(company)
        if not eik or eik == _EIK_PLACEHOLDER:
            continue
        for person in _current_people(company):
            name_norm = (person.get("name_normalized") or "").strip()
            if not name_norm:
                continue
            entry = by_name[name_norm].setdefault(eik, (company, []))
            entry[1].append(person)

    out: list[dict[str, Any]] = []
    for name_norm, by_eik in by_name.items():
        if len(by_eik) < 2:
            continue
        company_entries = []
        for eik, (company, persons) in by_eik.items():
            rows = contracts_by_eik.get(eik, [])
            if not rows:
                continue
            count, total = _contract_totals(rows)
            company_entries.append((company, persons, rows, count, total))
        if len(company_entries) < 2:
            continue

        total_contracts = sum(e[3] for e in company_entries)
        total_eur = sum(e[4] for e in company_entries)
        if (
            total_contracts < thresholds.person_concentration_min_contracts
            and total_eur < thresholds.person_concentration_min_eur
        ):
            continue

        display_name = company_entries[0][1][0].get("name") or name_norm
        severity = (
            "high"
            if total_contracts >= thresholds.person_concentration_high_contracts
            or total_eur >= thresholds.person_concentration_high_eur
            else "warning"
        )

        companies_detail = [
            {
                "eik": company.get("eik"),
                "name": company.get("name"),
                "roles": sorted({p.get("role") for p in persons if p.get("role")}),
                "contract_count": count,
                "total_eur": round(total, 2),
            }
            for company, persons, _rows, count, total in company_entries
        ]
        names_list = "; ".join(f"„{d['name']}“ ({d['contract_count']} дог.)" for d in companies_detail)

        message = (
            f"{display_name} стои зад {len(company_entries)} различни изпълнители на общината "
            f"({names_list}) — заедно с {total_contracts} договор(и) за общо {eur(total_eur)}."
        )
        explanation = (
            f"Едно и също лице, {display_name}, е вписано в Търговския регистър като управител, "
            "съдружник, едноличен собственик или член на съвета на няколко отделни фирми, които "
            "поотделно изглеждат като различни изпълнители, но заедно печелят голям дял от "
            "обществените поръчки на общината. Това може да сочи към изкуствено разпределяне на "
            "поръчки между свързани фирми с цел заобикаляне на правилата за концентрация или "
            "конкуренция. Невинно обяснение е, че фирмите работят в различни, несвързани дейности и "
            "са спечелили отделни процедури с реална конкуренция."
        )

        sources: list[dict[str, Any]] = []
        for company, persons, rows, _count, _total in company_entries:
            fields = [_registry_field("name", "Наименование", company.get("name"))]
            for person in persons:
                fields.append(_registry_field("role", "Роля", _role_label(person.get("role"))))
            sources.append(registry_source(company, fields))
            sources.extend(_contract_sources_for(rows))

        out.append(
            make_flag(
                rule="person_concentration",
                tier=TIER_SIGNAL,
                severity=severity,
                message=message,
                explanation=explanation,
                documents=[
                    "актуални справки от Търговския регистър за всяка от изброените фирми",
                    "списък на всички договори на общината с тези фирми",
                    "протоколите от съответните процедури по избор на изпълнител",
                ],
                subject_type="person",
                subject_id=f"person:{name_norm}",
                details={
                    "name": display_name,
                    "name_normalized": name_norm,
                    "companies": companies_detail,
                    "total_contract_count": total_contracts,
                    "total_eur": round(total_eur, 2),
                },
                law_ref="чл. 2, ал. 1, т. 1 и 2 ЗОП (равнопоставеност и свободна конкуренция)",
                sources=sources,
            )
        )
    return out


# ---------------------------------------------------------------------------
# young_company (signal)
# ---------------------------------------------------------------------------


def young_company_flags(
    contracts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`young_company` (signal): a contract signed within 12 months of the
    contractor's registration (`Company.registered_at`), worth >= 50,000 €.

    Legal anchor: ЗОП чл. 2, ал. 1, т. 1-2 (равнопоставеност, свободна
    конкуренция) -- a brand-new company winning a sizeable contract is not
    unlawful by itself, but is a pattern worth asking documents about
    (experience/capacity requirements, how it was found).
    """
    if not companies:
        return []
    companies_by_eik = {_company_eik(c): c for c in companies if _company_eik(c)}

    out: list[dict[str, Any]] = []
    for record, eik in _contract_eik_pairs(contracts):
        if record.get("source") != "eop":
            continue
        company = companies_by_eik.get(eik)
        if company is None or not company.get("registered_at"):
            continue
        if not eik.startswith("2"):
            # Only ЕИКs issued by the Trade Register itself (2xxxxxxxx, from
            # 2008) date the company's founding. Older companies were
            # re-registered from the court registers in 2008-2011, so their
            # earliest registry date is the transfer, not the founding.
            continue
        contract_date = record.get("contract_date")
        value_eur = record.get("contract_value_eur")
        if not contract_date or not value_eur:
            continue
        value_eur = float(value_eur)
        if value_eur < thresholds.young_company_min_eur:
            continue
        # EOP stores contract dates as naive UTC (signed "20.10" = 19.10 21:00
        # UTC); compare Sofia calendar dates, as the contract pages show them.
        local_contract_date = _local_date(contract_date)
        age_days = (local_contract_date - company["registered_at"].date()).days
        if age_days < 0 or age_days > thresholds.young_company_window_days:
            continue

        severity = (
            "high"
            if value_eur >= thresholds.young_company_high_eur
            or age_days <= thresholds.young_company_high_window_days
            else "warning"
        )
        age_months = round(age_days / 30.44, 1)
        company_name = company.get("name") or eik
        contractor_name = record.get("contractor_name") or company_name

        message = (
            f"{contractor_name} е спечелила договор за {eur(value_eur)} само {age_months} месеца "
            "след регистрацията си в Търговския регистър."
        )
        explanation = (
            f"„{company_name}“ е вписана в Търговския регистър на "
            f"{company['registered_at'].date().isoformat()}, а договорът с общината е подписан "
            f"{age_months} месеца по-късно, на стойност {eur(value_eur)}. Толкова скоро след "
            "създаването си фирма обикновено няма дълга история и опит, по които да се съди "
            "капацитетът ѝ. Невинно обяснение е, че е продължение на съществуващ бизнес (напр. "
            "преобразуване на едноличен търговец) или че критериите за подбор не изискват такъв опит."
        )

        sources = [
            registry_source(
                company,
                [
                    _registry_field("name", "Наименование", company_name),
                    _registry_field(
                        "registered_at", "Дата на регистрация", company["registered_at"].date().isoformat()
                    ),
                ],
            ),
            *contract_sources(
                record,
                contract_fields=_COMPANY_CONTRACT_FIELDS,
                tender_fields=None,
                sigma_fields=_SIGMA_CONTRACT_FIELDS,
            ),
        ]

        out.append(
            make_flag(
                rule="young_company",
                tier=TIER_SIGNAL,
                severity=severity,
                message=message,
                explanation=explanation,
                documents=[
                    "удостоверение за актуално състояние от Търговския регистър",
                    "документация на поръчката с критериите за подбор (опит, капацитет)",
                    "декларация за икономическо и финансово състояние на участника",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                subject_key=f"{contract_subject_id(record)}:{eik}",
                details={
                    "contractor_eik": eik,
                    "contractor_name": contractor_name,
                    "registered_at": company["registered_at"].date().isoformat(),
                    "contract_date": local_contract_date.isoformat(),
                    "age_days": age_days,
                    "contract_value_eur": round(value_eur, 2),
                },
                law_ref="чл. 2, ал. 1, т. 1 и 2 ЗОП (равнопоставеност и свободна конкуренция)",
                procurement_id=record.get("id"),
                sources=sources,
            )
        )
    return out


# ---------------------------------------------------------------------------
# company_status (signal)
# ---------------------------------------------------------------------------


def company_status_flags(
    contracts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    thresholds: Thresholds,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """`company_status` (signal): a contractor in liquidation/insolvency/
    deregistered while holding a contract signed in the last
    `company_status_recent_years` years, OR `last_annual_report_year` older
    than (latest contract year - `company_status_report_lag_years`) for a
    company with >= `company_status_min_eur_for_report` of contracted
    value. Both reasons can fire for the same company -- one flag either way.
    """
    if not companies:
        return []
    now = now or _now()
    contracts_by_eik = _contracts_by_eik(contracts)
    recent_cutoff = now - dt.timedelta(days=365 * thresholds.company_status_recent_years)

    out: list[dict[str, Any]] = []
    for company in companies:
        eik = _company_eik(company)
        if not eik or eik == _EIK_PLACEHOLDER:
            continue
        rows = contracts_by_eik.get(eik, [])
        if not rows:
            continue
        contract_count, total_eur = _contract_totals(rows)
        years = [_local_date(r["contract_date"]).year for r in rows if r.get("contract_date")]
        latest_year = max(years) if years else None
        recent_rows = [r for r in rows if r.get("contract_date") and r["contract_date"] >= recent_cutoff]

        status = company.get("status")
        last_report_year = company.get("last_annual_report_year")
        declaration_year = company.get("last_no_activity_declaration_year")
        # Sole traders without a mandatory audit do not publish a ГФО at all
        # (ЗСч чл. 38, ал. 9, т. 1), so "no ГФО" is no signal for them.
        is_sole_trader = (company.get("legal_form") or "").strip().lower() == "едноличен търговец"
        # A company that did no business declares that once instead of
        # filing a ГФО (ЗСч чл. 38, ал. 9, т. 2) -- count it as a filing.
        last_filing_year = max(
            (y for y in (last_report_year, declaration_year) if y is not None), default=None
        )

        status_reason = status in ("liquidation", "insolvency", "deregistered") and bool(recent_rows)
        stale_reason = (
            not is_sole_trader
            and latest_year is not None
            and total_eur >= thresholds.company_status_min_eur_for_report
            and (
                last_filing_year is None
                or last_filing_year < latest_year - thresholds.company_status_report_lag_years
            )
        )
        if not status_reason and not stale_reason:
            continue

        company_name = company.get("name") or eik
        sentences_msg = []
        sentences_exp = []
        severity = "warning"
        if status_reason:
            status_label = _STATUS_LABELS_BG.get(status, status)
            sentences_msg.append(
                f"„{company_name}“ е в {status_label}, но държи {len(recent_rows)} договор(и) с "
                f"общината, подписан(и) през последните {thresholds.company_status_recent_years} "
                "години."
            )
            sentences_exp.append(
                f"Според Търговския регистър „{company_name}“ е в {status_label}, а общината има с "
                f"нея {len(recent_rows)} договор(и), подписан(и) през последните "
                f"{thresholds.company_status_recent_years} години. Фирма в такова състояние може да "
                "няма реален капацитет да изпълни поръчката или да не отговаря на изискванията за "
                "допустимост. Невинно обяснение е, че производството е образувано след сключването на "
                "договора и изпълнението не е засегнато, или че статусът в регистъра е остарял."
            )
            if status in ("insolvency", "deregistered"):
                severity = "high"
        if stale_reason:
            report_text = f"{last_report_year} г." if last_report_year else "нито една година"
            first_missing = (last_filing_year + 1) if last_filing_year else None
            deadline_text = (
                f"Срокът за ГФО за {first_missing} г. е изтекъл на 30 септември {first_missing + 1} г. "
                if first_missing
                else ""
            )
            declaration_text = (
                f" Фирмата е подала декларация за липса на дейност за {declaration_year} г."
                if declaration_year
                else ""
            )
            sentences_msg.append(
                f"Последният обявен годишен финансов отчет (ГФО) е за {report_text}, докато "
                f"последният договор с общината е от {latest_year} г., за общо {eur(total_eur)}."
            )
            sentences_exp.append(
                f"„{company_name}“ е изпълнител с договори за общо {eur(total_eur)}, но в "
                f"Търговския регистър последният обявен ГФО е за {report_text}, а последният договор "
                f"с общината е от {latest_year} г.{declaration_text} Търговците трябва да обявят ГФО "
                "до 30 септември на следващата година. " + deadline_text + "Без актуални отчети "
                "не може да се провери финансовият капацитет на фирмата към момента на изпълнението. "
                "Невинно обяснение е закъснение при обявяването, което фирмата тепърва ще поправи; "
                "това е нарушение на фирмата, а не на общината."
            )

        law_ref = "чл. 2, ал. 1 ЗОП (равнопоставеност, пропорционалност, публичност и прозрачност)"
        if status_reason:
            law_ref += (
                "; чл. 55, ал. 1, т. 1 ЗОП (възложителят може да отстрани участник, обявен в "
                "несъстоятелност, в производство по несъстоятелност или в процедура по ликвидация)"
            )
        if stale_reason:
            law_ref += (
                "; чл. 38, ал. 1, т. 1 Закон за счетоводството (търговците обявяват ГФО в търговския "
                "регистър в срок до 30 септември на следващата година)"
            )

        # What the municipality holds and can be asked for under ЗДОИ. The
        # registry facts themselves are already public on the portal.
        documents: list[str] = []
        if status_reason:
            documents += [
                (
                    "документите, с които общината е проверила при възлагането, че фирмата не е в "
                    "ликвидация или несъстоятелност (ЕЕДОП и удостоверения)"
                ),
                "кореспонденция с ликвидатора/синдика по изпълнението на договора",
            ]
        if stale_reason:
            documents.append(
                "ЕЕДОП и документите за икономическо и финансово състояние, представени от "
                "фирмата в процедурата"
            )
        documents.append("приемо-предавателни протоколи и платежни документи по договора/договорите")

        registry_fields = [_registry_field("name", "Наименование", company_name)]
        if status:
            registry_fields.append(_registry_field("status", "Статус", _STATUS_LABELS_BG.get(status, status)))
        registry_fields.append(
            _registry_field("last_annual_report_year", "Последен обявен ГФО", last_report_year)
        )
        if declaration_year:
            registry_fields.append(
                _registry_field(
                    "last_no_activity_declaration_year",
                    "Декларация за липса на дейност",
                    declaration_year,
                )
            )

        out.append(
            make_flag(
                rule="company_status",
                tier=TIER_SIGNAL,
                severity=severity,
                message=" ".join(sentences_msg),
                explanation=" ".join(sentences_exp),
                documents=documents,
                subject_type="contractor",
                subject_id=_page_subject_id(eik, rows),
                subject_key=f"company:{eik}",
                details={
                    "contractor_eik": eik,
                    "contractor_name": company_name,
                    "status": status,
                    "last_annual_report_year": last_report_year,
                    "last_no_activity_declaration_year": declaration_year,
                    "latest_contract_year": latest_year,
                    "contract_count": contract_count,
                    "total_eur": round(total_eur, 2),
                    "status_reason": status_reason,
                    "stale_report_reason": stale_reason,
                },
                law_ref=law_ref,
                sources=[registry_source(company, registry_fields), *_contract_sources_for(rows)],
            )
        )
    return out


# ---------------------------------------------------------------------------
# activity_mismatch (signal)
# ---------------------------------------------------------------------------


def activity_mismatch_flags(
    contracts: list[dict[str, Any]],
    companies: list[dict[str, Any]],
    thresholds: Thresholds,
) -> list[dict[str, Any]]:
    """`activity_mismatch` (signal): the contractor's declared НКИД class
    (first 2 digits of `Company.nkid_code`) is one of a small, explicit set
    of known-incompatible pairs against the contract's CPV division (first
    2 digits), for contracts >= 50,000 €. Only the pairs enumerated in
    `thresholds.ACTIVITY_INCOMPATIBLE_PAIRS` are ever flagged -- an
    NKID/CPV combination simply absent from that table is NEVER a reason to
    flag (see that constant's docstring).
    """
    if not companies:
        return []
    companies_by_eik = {_company_eik(c): c for c in companies if _company_eik(c)}

    out: list[dict[str, Any]] = []
    for record, eik in _contract_eik_pairs(contracts):
        if record.get("source") != "eop":
            continue
        value_eur = record.get("contract_value_eur")
        if not value_eur or float(value_eur) < thresholds.activity_mismatch_min_eur:
            continue
        company = companies_by_eik.get(eik)
        cpv = record.get("cpv_code")
        nkid = company.get("nkid_code") if company else None
        if not company or not cpv or not nkid:
            continue
        nkid_division = str(nkid)[:2]
        cpv_division = str(cpv)[:2]

        description = None
        for nkid_divisions, bad_cpv_division, pair_description in ACTIVITY_INCOMPATIBLE_PAIRS:
            if nkid_division in nkid_divisions and cpv_division == bad_cpv_division:
                description = pair_description
                break
        if description is None:
            continue

        value_eur = float(value_eur)
        company_name = company.get("name") or eik
        contractor_name = record.get("contractor_name") or company_name
        nkid_label = company.get("nkid_label") or nkid

        message = (
            f"{contractor_name} е с деклариран предмет на дейност „{nkid_label}“ (НКИД {nkid}), но е "
            f"спечелила поръчка за {eur(value_eur)}, при която дейността изглежда несъвместима: "
            f"{description}."
        )
        explanation = (
            f"Търговският регистър сочи основна дейност на „{company_name}“ по НКИД {nkid} "
            f"(„{nkid_label}“), а поръчката е по CPV код {cpv} -- {description}. Това може да значи, "
            "че фирмата е подизпълнител на реалния изпълнител, че регистърът не е актуализиран след "
            "смяна на дейността, или че критериите за подбор не са изисквали опит в тази дейност. "
            "Невинно обяснение е разширена многопосочна дейност, която не е отразена като основен "
            "код в регистъра."
        )

        out.append(
            make_flag(
                rule="activity_mismatch",
                tier=TIER_SIGNAL,
                severity="warning",
                message=message,
                explanation=explanation,
                documents=[
                    "актуална справка от Търговския регистър за предмета на дейност на фирмата",
                    "документация на поръчката с критериите за подбор (опит, технически възможности)",
                    "декларация за подизпълнители (ако е приложимо)",
                ],
                subject_type="contract",
                subject_id=contract_subject_id(record),
                subject_key=f"{contract_subject_id(record)}:{eik}",
                details={
                    "contractor_eik": eik,
                    "contractor_name": contractor_name,
                    "nkid_code": nkid,
                    "nkid_label": company.get("nkid_label"),
                    "cpv_code": cpv,
                    "mismatch": description,
                    "contract_value_eur": round(value_eur, 2),
                },
                law_ref="чл. 2, ал. 1, т. 3 ЗОП (пропорционалност)",
                procurement_id=record.get("id"),
                sources=[
                    registry_source(
                        company,
                        [
                            _registry_field("name", "Наименование", company_name),
                            _registry_field("nkid_code", "Код по НКИД", nkid),
                            _registry_field("nkid_label", "Основна дейност", company.get("nkid_label")),
                        ],
                    ),
                    *contract_sources(
                        record,
                        contract_fields=_COMPANY_CONTRACT_FIELDS,
                        tender_fields=None,
                        sigma_fields=_SIGMA_CONTRACT_FIELDS,
                    ),
                ],
            )
        )
    return out
