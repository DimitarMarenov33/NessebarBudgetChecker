"""Provenance ("Източници") for every flag: which published file or record,
which sheet and row, and which fields produced the numbers a flag quotes.

Every rule attaches a `sources` list to its flag dict (persisted as
`Flag.sources_json`); the site renders it as a collapsible "Източници" box so
a citizen can open the municipality's own file (or the ЦАИС ЕОП / SIGMA
record), find the row, and compare the figures. Each entry::

    {
      "kind":   "budget_file" | "eop_contract" | "eop_tender" | "sigma"
                | "reports_page" | "flag" | "registry" | "declaration",
      "label":  Bulgarian description, e.g.
                "Разчет за капиталовите разходи — декември 2022 г.",
      "url":    original public URL (or a site-relative "flags/<id>.html"
                for kind "flag"), or None,
      "file":   basename of the municipal file, or None,
      "sheet":  worksheet name, or None,
      "row":    1-based spreadsheet row, or None,
      "period": "YYYY-MM", or None,
      "fields": [{"name": label as in the source, "value": raw value as
                  published, "value_eur": number or None,
                  ["currency": "BGN" | "EUR"], ["label": Bulgarian gloss]}],
      "note":   str or None,
    }

Budget files before 2026 are published in leva; the parser stores EUR
(2 dp) and records `extra_json.original_currency`/`conversion_rate`, so the
leva figure shown here is *computed back* (EUR × 1.95583). The EUR value was
rounded to the cent, so the round trip can be off by up to ~1 стотинка; a
computed figure within that error of a whole lev is shown as that whole lev
(the municipality publishes whole leva -- e.g. 627 983, not 627 982.99).

Parsed line items carry `extra_json.source_sheet`/`source_row` once the
parse step records them; until then the sheet falls back to the row's
`unit` (the worksheet title) and the row number is None.
"""

from __future__ import annotations

import datetime as dt
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from nessebar_budget.analysis.thresholds import BGN_EUR_RATE

KIND_BUDGET_FILE = "budget_file"
KIND_EOP_CONTRACT = "eop_contract"
KIND_EOP_TENDER = "eop_tender"
KIND_SIGMA = "sigma"
KIND_REPORTS_PAGE = "reports_page"
KIND_FLAG = "flag"
#: Търговски регистър (Registry Agency) deed page for a `Company` -- used by
#: `rules.companies` (`related_party`, `person_concentration`, `young_company`,
#: `company_status`, `activity_mismatch`).
KIND_REGISTRY = "registry"
#: Регистър на декларациите -- an `Official`'s declaration of interests PDF,
#: used by `rules.companies.related_party_flags`.
KIND_DECLARATION = "declaration"
KINDS = (
    KIND_BUDGET_FILE,
    KIND_EOP_CONTRACT,
    KIND_EOP_TENDER,
    KIND_SIGMA,
    KIND_REPORTS_PAGE,
    KIND_FLAG,
    KIND_REGISTRY,
    KIND_DECLARATION,
)

#: Община Несебър's ЕИК as SIGMA spells it in the export URL.
SIGMA_AUTHORITY_EIK = "000057122"
SIGMA_CSV_URL = f"https://sigma.midt.bg/contracts.csv?authority={SIGMA_AUTHORITY_EIK}"
SIGMA_CSV_FILE = "contracts.csv"
#: The municipality's archive of budget-execution reports (scraped by
#: `scrapers/nesebar_site.py`).
REPORTS_PAGE_URL = "https://www.nesebar.bg/reports.html"
EOP_APP_URL = "https://app.eop.bg"

#: ЦАИС ЕОП API methods (see `scrapers/eop.py`) each raw_json part comes from.
EOP_API_CONTRACTS = "GetContractsByOrganization"
EOP_API_PROCUREMENTS = "GetProcurementsByOrganization"
EOP_API_TENDER_DETAILS = "GetPublishedTenderDetails"

_MONTHS_BG = (
    "януари", "февруари", "март", "април", "май", "юни",
    "юли", "август", "септември", "октомври", "ноември", "декември",
)

#: Capital-ledger field -> its column header in the municipality's
#: "Разчет за финансиране на капиталовите разходи" workbook (the parser
#: locates these columns by the same header text, see
#: `parsers/budget_capital.py`).
BUDGET_FIELD_LABELS: dict[str, str] = {
    "estimated_total": "Сметна стойност",
    "spent_prior": "Усвоено до края на предходната година",
    "plan_current": "Уточнен план",
    "spent_period": "Усвоено към отчетния период",
}
BUDGET_FIELDS = tuple(BUDGET_FIELD_LABELS)

#: `raw_json.contract` keys (GetContractsByOrganization) -> Bulgarian gloss.
EOP_CONTRACT_FIELD_LABELS: dict[str, str] = {
    "ContractNumber": "Номер на договора",
    "TenderNumber": "Уникален номер на поръчката",
    "ContractSubject": "Предмет на договора",
    "ContractDate": "Дата на сключване",
    "TedPublishDate": "Дата на публикуване на обявлението за възлагане",
    "ContractValue": "Стойност при сключване",
    "Currency": "Валута",
    "CurrentContractValue": "Текуща стойност",
    "CurrentContractCurrency": "Валута на текущата стойност",
    "SupplierName": "Изпълнител",
    "RegisterNumberList": "ЕИК на изпълнителя",
    "ProcedureType": "Вид процедура",
    "TypeOfContract": "Вид на поръчката",
}
#: `raw_json.procedure` (GetProcurementsByOrganization) and
#: `raw_json.tender_detail` (GetPublishedTenderDetails) keys -> gloss.
EOP_TENDER_FIELD_LABELS: dict[str, str] = {
    "SpecialNumber": "Уникален номер на поръчката",
    "TenderName": "Наименование на поръчката",
    "ProcedureType": "Вид процедура",
    "EstimatedValue": "Прогнозна стойност",
    "Currency": "Валута",
    "IsEUFinanced": "Финансиране от ЕС",
    "PublicationDate": "Дата на публикуване",
    "OfferPhaseStartDate": "Начало на срока за оферти",
    "OfferPhaseEndDate": "Край на срока за оферти",
    "TenderDescription": "Описание на поръчката",
    "TypeOfContract": "Вид на поръчката",
}
#: SIGMA CSV columns (see `scrapers/sigma.py`) -> gloss.
SIGMA_FIELD_LABELS: dict[str, str] = {
    "id": "Идентификатор на реда",
    "unp": "Уникален номер на поръчката",
    "subject": "Предмет",
    "contractor": "Изпълнител",
    "contractor_eik": "ЕИК на изпълнителя",
    "value_eur": "Стойност",
    "signed_at": "Дата на сключване",
    "procedure": "Вид процедура",
    "bids_received": "Брой получени оферти",
    "eu_funded": "Финансиране от ЕС",
}

DEFAULT_CONTRACT_FIELDS = (
    "ContractNumber",
    "TenderNumber",
    "ContractDate",
    "ContractValue",
    "Currency",
    "SupplierName",
    "RegisterNumberList",
)
DEFAULT_TENDER_FIELDS = ("SpecialNumber", "ProcedureType", "EstimatedValue", "Currency")
DEFAULT_SIGMA_FIELDS = ("id", "unp", "bids_received")

#: EOP integer currency codes (verified in `rules/_common.py`).
_EOP_CURRENCY = {1: "EUR", 3: "BGN"}
_EOP_MONEY_CURRENCY_KEY = {
    "ContractValue": "Currency",
    "CurrentContractValue": "CurrentContractCurrency",
    "EstimatedValue": "Currency",
}
_EOP_DATE_FIELDS = {
    "ContractDate",
    "TedPublishDate",
    "PublicationDate",
    "OfferPhaseStartDate",
    "OfferPhaseEndDate",
}
#: Long free-text fields are cut to this many characters.
_TEXT_LIMIT = 300


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute or key access, so helpers accept ORM rows and plain dicts."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _num(value: Any) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def period_label(period: str | None) -> str | None:
    """"2022-12" -> "декември 2022 г." (None/odd input passes through)."""
    if not period:
        return None
    try:
        year, month = int(period[:4]), int(period[5:7])
        return f"{_MONTHS_BG[month - 1]} {year} г."
    except (ValueError, IndexError):
        return period


def eur_to_bgn(value_eur: float | None, rate: float = BGN_EUR_RATE) -> float | None:
    """The leva figure a BGN file published, computed back from the stored
    EUR value (2 dp). A result within the round-trip error (~1 стотинка) of a
    whole lev is returned as that whole lev -- see the module docstring."""
    eur = _num(value_eur)
    if eur is None:
        return None
    bgn = eur * float(rate)
    if abs(bgn - round(bgn)) <= 0.01:
        return float(round(bgn))
    return round(bgn, 2)


def file_basename(file_path: str | None, url: str | None = None) -> str | None:
    """Basename of a cached file path, falling back to the URL's last segment."""
    for candidate in (file_path, urlsplit(url).path if url else None):
        if candidate:
            name = PurePosixPath(unquote(str(candidate)).replace("\\", "/")).name
            if name:
                return name
    return None


def _source(
    kind: str,
    label: str,
    *,
    url: str | None = None,
    file: str | None = None,
    sheet: str | None = None,
    row: int | None = None,
    period: str | None = None,
    fields: list[dict[str, Any]] | None = None,
    note: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kind": kind,
        "label": label,
        "url": url,
        "file": file,
        "sheet": sheet,
        "row": row,
        "period": period,
        "fields": list(fields or []),
        "note": note,
    }
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def _as_row(value: Any) -> int | None:
    try:
        row = int(value)
    except (TypeError, ValueError):
        return None
    return row if row > 0 else None


def _iso_local_date(value: Any) -> str | None:
    # Imported lazily: importing `rules._common` runs `rules/__init__`, whose
    # rule modules import this module -- a top-level import would be circular.
    from nessebar_budget.analysis.rules._common import _local_date

    date = _local_date(value)
    return date.isoformat() if date else None


# ---------------------------------------------------------------------------
# budget files
# ---------------------------------------------------------------------------


def budget_report_ref(report: Any) -> dict[str, Any] | None:
    """A plain-dict snapshot of a `BudgetReport` (or a dict with its keys)."""
    if report is None:
        return None
    return {
        "id": _get(report, "id"),
        "period": _get(report, "period"),
        "kind": _get(report, "kind"),
        "url": _get(report, "url"),
        "file_path": _get(report, "file_path"),
    }


def budget_report_label(kind: str | None, period: str | None) -> str:
    when = period_label(period)
    if kind and kind.upper().startswith(("B1", "B3", "IB1", "IB3")):
        base = "Отчет за касовото изпълнение на бюджета"
    else:
        base = "Разчет за капиталовите разходи"
    return f"{base} — {when}" if when else base


def budget_row_source(
    row: Any,
    report: Any,
    *,
    fields: tuple[str, ...] | list[str] = BUDGET_FIELDS,
    note: str | None = None,
) -> dict[str, Any]:
    """Source entry for one capital-ledger line item (`BudgetLineItem` or a
    dict with its keys, incl. `extra_json`) read from `report` (a
    `BudgetReport`, a dict from `budget_report_ref`, or None).

    Uses `report.url`/`file_path`/`period` and
    `extra_json.source_sheet`/`source_row`/`original_currency`/
    `conversion_rate`; every key may be missing (older parses) -- the sheet
    then falls back to the row's `unit` (= the worksheet title) and the row
    number to None.
    """
    extra = _get(row, "extra_json") or {}
    if not isinstance(extra, dict):
        extra = {}
    period = _get(row, "period") or _get(report, "period")
    currency = str(extra.get("original_currency") or "EUR").upper()
    rate = _num(extra.get("conversion_rate")) or BGN_EUR_RATE

    out_fields: list[dict[str, Any]] = []
    for name in fields:
        value_eur = _num(_get(row, name))
        entry: dict[str, Any] = {
            "name": BUDGET_FIELD_LABELS.get(name, name),
            "value": (
                eur_to_bgn(value_eur, rate)
                if currency == "BGN"
                else (round(value_eur, 2) if value_eur is not None else None)
            ),
            "value_eur": round(value_eur, 2) if value_eur is not None else None,
            "currency": currency,
        }
        out_fields.append(entry)

    object_name = _get(row, "object_name")
    paragraph = _get(row, "paragraph")
    notes = []
    if object_name:
        notes.append(
            f"Обект „{object_name}“" + (f" (§ {paragraph})" if paragraph else "") + "."
        )
    row_no = _as_row(extra.get("source_row"))
    if row_no is None and object_name:
        notes.append("Номерът на реда не е записан — потърсете наименованието на обекта в листа.")
    if note:
        notes.append(note)

    url = _get(report, "url")
    return _source(
        KIND_BUDGET_FILE,
        budget_report_label(_get(report, "kind"), period),
        url=url,
        file=file_basename(_get(report, "file_path"), url),
        sheet=extra.get("source_sheet") or _get(row, "unit"),
        row=row_no,
        period=period,
        fields=out_fields,
        note=" ".join(notes) or None,
    )


def budget_object_source(
    obj: dict[str, Any],
    fields: tuple[str, ...] | list[str] = BUDGET_FIELDS,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """`budget_row_source` for a rule's object dict: the engine attaches the
    line item's `extra_json`/`unit` and its report (`_report`) to each one."""
    return budget_row_source(obj, obj.get("_report"), fields=fields, note=note)


def budget_file_source(
    report: Any, period: str | None, *, note: str | None = None
) -> dict[str, Any]:
    """A whole budget file (no specific row), e.g. "the object is absent here"."""
    url = _get(report, "url")
    period = period or _get(report, "period")
    return _source(
        KIND_BUDGET_FILE,
        budget_report_label(_get(report, "kind"), period),
        url=url,
        file=file_basename(_get(report, "file_path"), url),
        sheet="Общо" if report is not None else None,
        period=period,
        note=note,
    )


# ---------------------------------------------------------------------------
# ЦАИС ЕОП / SIGMA records
# ---------------------------------------------------------------------------


def _eop_field(name: str, part: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    raw = part.get(name)
    entry: dict[str, Any] = {"name": name, "label": labels.get(name, name)}
    if name in _EOP_MONEY_CURRENCY_KEY:
        code = part.get(_EOP_MONEY_CURRENCY_KEY[name])
        currency = _EOP_CURRENCY.get(code)
        value = _num(raw)
        entry["value"] = value
        if currency:
            entry["currency"] = currency
        if value is not None and currency == "EUR":
            entry["value_eur"] = round(value, 2)
        elif value is not None and currency == "BGN":
            entry["value_eur"] = round(value / BGN_EUR_RATE, 2)
        else:
            entry["value_eur"] = None
        return entry
    if name in _EOP_DATE_FIELDS:
        entry["value"] = _iso_local_date(raw) if raw else None
    elif name.endswith("Currency"):
        entry["value"] = _EOP_CURRENCY.get(raw, raw)
    elif isinstance(raw, str):
        from nessebar_budget.analysis.rules._common import _clean_html

        text = " ".join(_clean_html(raw).split())
        entry["value"] = text if len(text) <= _TEXT_LIMIT else text[: _TEXT_LIMIT - 1] + "…"
    else:
        entry["value"] = raw
    entry["value_eur"] = None
    return entry


def _eop_url(record: dict[str, Any], *parts: dict[str, Any]) -> str | None:
    if record.get("url"):
        return record["url"]
    for part in parts:
        tender_id = part.get("TenderId")
        if tender_id:
            return f"{EOP_APP_URL}/today/{tender_id}"
    return None


def eop_contract_source(
    record: dict[str, Any],
    fields: tuple[str, ...] | list[str] = DEFAULT_CONTRACT_FIELDS,
    *,
    note: str | None = None,
) -> dict[str, Any] | None:
    raw = record.get("raw_json") or {}
    contract = raw.get("contract") or {}
    if not contract:
        return None
    number = contract.get("ContractNumber") or record.get("source_id")
    tender = contract.get("TenderNumber")
    label = f"ЦАИС ЕОП — договор № {number}" + (f" (поръчка {tender})" if tender else "")
    api_note = (
        f"Полетата са от API на ЦАИС ЕОП, метод {EOP_API_CONTRACTS}; страницата на поръчката "
        "в app.eop.bg (раздел „Договори“) показва същите данни."
    )
    return _source(
        KIND_EOP_CONTRACT,
        label,
        url=_eop_url(record, contract, raw.get("procedure") or {}),
        fields=[_eop_field(n, contract, EOP_CONTRACT_FIELD_LABELS) for n in fields if n in contract],
        note=" ".join(x for x in (note, api_note) if x),
        api=[EOP_API_CONTRACTS],
    )


def eop_tender_source(
    record: dict[str, Any],
    fields: tuple[str, ...] | list[str] = DEFAULT_TENDER_FIELDS,
    *,
    note: str | None = None,
) -> dict[str, Any] | None:
    raw = record.get("raw_json") or {}
    procedure = raw.get("procedure") or {}
    detail = raw.get("tender_detail") or {}
    if not procedure and not detail:
        return None
    out_fields = []
    apis: list[str] = []
    for name in fields:
        if name in procedure:
            out_fields.append(_eop_field(name, procedure, EOP_TENDER_FIELD_LABELS))
            if EOP_API_PROCUREMENTS not in apis:
                apis.append(EOP_API_PROCUREMENTS)
        elif name in detail:
            part = dict(detail)
            if name == "EstimatedValue":
                part.setdefault("Currency", detail.get("CurrencyType"))
            out_fields.append(_eop_field(name, part, EOP_TENDER_FIELD_LABELS))
            if EOP_API_TENDER_DETAILS not in apis:
                apis.append(EOP_API_TENDER_DETAILS)
    number = (
        procedure.get("SpecialNumber")
        or detail.get("SpecialNumber")
        or (raw.get("contract") or {}).get("TenderNumber")
    )
    label = "ЦАИС ЕОП — процедура" + (f" {number}" if number else "")
    api_note = (
        "Полетата са от API на ЦАИС ЕОП, "
        + ("метод " if len(apis) == 1 else "методи ")
        + " и ".join(apis or [EOP_API_PROCUREMENTS])
        + "; същите данни са на страницата на поръчката в app.eop.bg."
    )
    return _source(
        KIND_EOP_TENDER,
        label,
        url=_eop_url(record, procedure, detail),
        fields=out_fields,
        note=" ".join(x for x in (note, api_note) if x),
        api=apis or [EOP_API_PROCUREMENTS],
    )


def _sigma_value(record: dict[str, Any], name: str) -> Any:
    raw = record.get("raw_json") or {}
    if name == "id":
        return record.get("source_id")
    if name == "subject":
        return record.get("title")
    if name == "contractor":
        return record.get("contractor_name")
    if name == "contractor_eik":
        return record.get("contractor_eik")
    if name == "value_eur":
        return _num(record.get("contract_value_eur"))
    if name == "signed_at":
        value = record.get("contract_date")
        return value.date().isoformat() if isinstance(value, dt.datetime) else value
    if name == "bids_received":
        value = record.get("bids_received")
        return value if value is not None else raw.get("bids_received")
    return raw.get(name)


def sigma_source(
    record: dict[str, Any],
    fields: tuple[str, ...] | list[str] = DEFAULT_SIGMA_FIELDS,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """The SIGMA (МИДТ) CSV export row behind a SIGMA record."""
    out_fields = []
    for name in fields:
        value = _sigma_value(record, name)
        entry: dict[str, Any] = {
            "name": name,
            "label": SIGMA_FIELD_LABELS.get(name, name),
            "value": value,
            "value_eur": value if name == "value_eur" else None,
        }
        if name == "value_eur":
            entry["currency"] = "EUR"
        out_fields.append(entry)
    row_id = record.get("source_id")
    base_note = (
        f"Ред с id = {row_id} в CSV експорта на SIGMA за Община Несебър "
        f"(ЕИК {SIGMA_AUTHORITY_EIK})."
    )
    return _source(
        KIND_SIGMA,
        "SIGMA (МИДТ) — експорт на договорите на Община Несебър",
        url=SIGMA_CSV_URL,
        file=SIGMA_CSV_FILE,
        fields=out_fields,
        note=" ".join(x for x in (base_note, note) if x),
        record_id=row_id,
    )


def contract_sources(
    record: dict[str, Any],
    *,
    contract_fields: tuple[str, ...] | list[str] | None = DEFAULT_CONTRACT_FIELDS,
    tender_fields: tuple[str, ...] | list[str] | None = DEFAULT_TENDER_FIELDS,
    sigma: dict[str, Any] | None = None,
    sigma_fields: tuple[str, ...] | list[str] = DEFAULT_SIGMA_FIELDS,
    note: str | None = None,
) -> list[dict[str, Any]]:
    """Sources for one procurement record (`engine._procurement_to_dict`
    shape): the ЦАИС ЕОП contract (raw_json.contract, from
    GetContractsByOrganization) and procedure (raw_json.procedure /
    tender_detail, from GetProcurementsByOrganization /
    GetPublishedTenderDetails) for an EOP record; the SIGMA CSV row for a
    SIGMA record. Pass `sigma` (the EOP record's SIGMA twin) when SIGMA
    contributed a figure, e.g. `bids_received` -- it is added as a "sigma"
    source. An empty/None `contract_fields` or `tender_fields` leaves that
    part out.
    """
    out: list[dict[str, Any]] = []
    source = record.get("source")
    if source == "sigma":
        out.append(sigma_source(record, sigma_fields, note=note))
        return out
    if source == "eop":
        contract = (
            eop_contract_source(record, contract_fields, note=note) if contract_fields else None
        )
        if contract is not None:
            out.append(contract)
        if tender_fields:
            tender = eop_tender_source(
                record, tender_fields, note=None if contract is not None else note
            )
            if tender is not None and (tender["fields"] or contract is None):
                out.append(tender)
    if sigma is not None:
        out.append(sigma_source(sigma, sigma_fields))
    if not out:  # an unknown source: still say where the record came from
        out.append(
            _source(
                KIND_EOP_CONTRACT if source == "eop" else str(source or "?"),
                f"Запис {source or '?'}:{record.get('source_id') or '?'}",
                url=record.get("url"),
                note=note,
            )
        )
    return out


# ---------------------------------------------------------------------------
# report archive / other flags
# ---------------------------------------------------------------------------


def reports_page_source(
    period: str,
    files_checked: list[str] | None = None,
    *,
    kinds: tuple[str, ...] | list[str] = ("B1", "B3"),
    note: str | None = None,
) -> dict[str, Any]:
    """The municipality's report archive page, for a report we did *not*
    find: `files_checked` are the files of that period we do have (any
    kind), so a citizen can see what the scraper saw."""
    files = sorted({f for f in (files_checked or []) if f})
    kinds_text = ", ".join(kinds)
    parts = [
        (
            f"Проверихме архива „Отчети“ на сайта на общината за файл от вид {kinds_text} "
            f"за {period_label(period) or period} и не открихме такъв."
        )
    ]
    if files:
        parts.append("За този период намерихме само: " + ", ".join(files) + ".")
    else:
        parts.append("За този период не намерихме нито един файл.")
    if note:
        parts.append(note)
    return _source(
        KIND_REPORTS_PAGE,
        f"Архив „Отчети“ на nesebar.bg — {period_label(period) or period}",
        url=REPORTS_PAGE_URL,
        period=period,
        fields=[
            {
                "name": f"Намерени файлове {kinds_text}",
                "value": 0,
                "value_eur": None,
            }
        ],
        note=" ".join(parts),
        files_checked=files,
    )


def flag_source(rule: str, subject_key: str, label: str) -> dict[str, Any]:
    """Another flag this one is built on. `url` ("flags/<id>.html",
    site-relative) is filled in by the engine once the flag has an id."""
    return _source(KIND_FLAG, label, rule=rule, subject_key=subject_key)


# ---------------------------------------------------------------------------
# Trade Register / declarations of interest (rules.companies)
# ---------------------------------------------------------------------------


def registry_source(
    company: dict[str, Any] | Any,
    fields: list[dict[str, Any]],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Търговски регистър source: the company's registry deed page
    (`Company.source_url`), carrying whichever fields the calling rule
    actually read -- e.g. a matched person's name/role/share for
    `related_party`, or the company's own status/registration date/last ГФО
    year for `young_company`/`company_status`. `fields` is built by the
    caller (each `{"name", "label", "value", "value_eur"}`, same shape as
    every other source kind)."""
    name = _get(company, "name")
    eik = _get(company, "eik")
    label = f"Търговски регистър — {name or eik or '?'}"
    return _source(KIND_REGISTRY, label, url=_get(company, "source_url"), fields=fields, note=note)


def declaration_source(
    official: dict[str, Any] | Any,
    fields: list[dict[str, Any]],
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Регистър на декларациите source: the official's declaration of
    interests PDF (`Official.document_url`). `fields` is built by the
    caller: name/role/mandate always, plus the matched raw line from
    `declared_interests_json` when the flag's match basis is the
    declaration itself (vs. a bare name match, where there is no specific
    line to point at -- the note then says the declaration is unverified)."""
    name = _get(official, "name")
    label = f"Декларация за интереси — {name or '?'}"
    return _source(KIND_DECLARATION, label, url=_get(official, "document_url"), fields=fields, note=note)
