"""Helpers shared by every rule module: date/currency parsing, the flag-dict
builder (which enforces the shared Flag contract), and procedure-type maps.

The flag contract (see `db.models.Flag` and `docs/RULES.md`):

- `tier`: "violation" (clear legal breach on the face of the data),
  "signal" (pattern consistent with misconduct, needs documents) or
  "opacity" (lawful but unverifiable -- documents should be requested);
- `message`: the 1-2 sentence Bulgarian headline;
- `explanation`: 2-4 plain Bulgarian sentences for citizens -- what we see,
  why it may point to misconduct, what would make it innocent;
- `documents_json`: Bulgarian names of the documents to request under ЗДОИ;
- `law_ref`: the verified citation (see `analysis/thresholds.py`);
- `sources`: provenance -- which published files/records, sheets, rows and
  fields produced the numbers (see `analysis/provenance.py`); persisted as
  `Flag.sources_json` and shown on the site as the "Източници" box.
"""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

from nessebar_budget.analysis.thresholds import BGN_EUR_RATE

try:  # pragma: no cover - depends on the host's tz database
    from zoneinfo import ZoneInfo

    _SOFIA: dt.tzinfo | None = ZoneInfo("Europe/Sofia")
except Exception:  # noqa: BLE001 -- any tz failure falls back to a fixed offset
    _SOFIA = None

TIER_VIOLATION = "violation"
TIER_SIGNAL = "signal"
TIER_OPACITY = "opacity"
TIERS = (TIER_VIOLATION, TIER_SIGNAL, TIER_OPACITY)

#: int currency code -> ISO label, as independently confirmed in
#: `scrapers/eop.py` (`CURRENCY_CODES`) and re-verified here against the
#: committed `data/nessebar.db`: every `Currency == 3` contract satisfies
#: `ContractValue / BGN_EUR_RATE == ContractValueEuro` to the cent, and every
#: `Currency == 1` contract has `ContractValue == ContractValueEuro` exactly.
_CURRENCY_EUR = 1
_CURRENCY_BGN = 3

_NET_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _parse_net_date(value: str | None) -> dt.datetime | None:
    """Parse a .NET JSON date like ``/Date(1789678800000+0300)/`` into a naive
    UTC datetime. Duplicated (deliberately, in miniature) from
    `scrapers/eop.py`'s private `_parse_net_date` rather than imported, to
    avoid coupling this module to a concurrently-edited scraper module for
    what is a ~5-line, stable piece of logic.
    """
    if not value:
        return None
    match = _NET_DATE_RE.match(str(value))
    if not match:
        return None
    millis = int(match.group(1))
    return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC).replace(tzinfo=None)


def _local_date(value: Any) -> dt.date | None:
    """Calendar date in Sofia for a .NET date string or a naive-UTC datetime.

    Day counts (offer periods, notice-to-contract gaps) must be computed on
    local calendar dates: EOP stores a contract signed "on 18.09" as
    ``/Date(...+0300)/`` = 17.09 21:00 UTC, which a naive UTC `.date()` would
    misread as the 17th.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        utc = value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value
    elif isinstance(value, dt.date):
        return value
    else:
        naive = _parse_net_date(str(value))
        if naive is None:
            return None
        utc = naive.replace(tzinfo=dt.UTC)
    if _SOFIA is not None:
        return utc.astimezone(_SOFIA).date()
    return (utc + dt.timedelta(hours=2)).date()


def _to_eur(value: float | None, currency_code: int | None) -> float | None:
    """Convert a raw EOP value to EUR using its own currency code."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if currency_code == _CURRENCY_EUR:
        return number
    if currency_code == _CURRENCY_BGN:
        return number / BGN_EUR_RATE
    return None  # unrecognized/unconfirmed code (e.g. USD): don't guess


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def _clean_html(text: str | None) -> str:
    """Strip HTML tags and unescape entities from an EOP `tender_detail`
    scalar field (`TenderDescription` carries ``<span>``/``&nbsp;`` markup).
    """
    if not text:
        return ""
    return html.unescape(_HTML_TAG_RE.sub(" ", str(text)))


def _norm_text(text: str | None) -> str:
    """Whitespace-collapsed, lowercased plain text (for length/equality checks)."""
    return _WS_RE.sub(" ", _clean_html(text)).strip().lower()


def _short_title(title: str | None, limit: int = 90) -> str:
    """Trim a long procurement subject for citizen-facing messages."""
    text = _WS_RE.sub(" ", (title or "обществена поръчка")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def eur(value: float | None) -> str:
    """Citizen-facing EUR amount: ``1,234,567 €`` (the site's comma format)."""
    return f"{float(value or 0):,.0f} €"


def contract_subject_id(record: dict[str, Any]) -> str:
    """The canonical subject id of a contract record, e.g. ``eop:266822``."""
    return f"{record.get('source', '?')}:{record.get('source_id', '?')}"


def is_signed(record: dict[str, Any]) -> bool:
    """A signed contract (vs. an open tender not yet awarded)."""
    return bool(record.get("contractor_name") or record.get("contract_date"))


def eop_parts(record: dict[str, Any]) -> tuple[dict, dict, dict]:
    """(contract, procedure, tender_detail) sub-dicts of an EOP raw_json."""
    raw = record.get("raw_json") or {}
    return (raw.get("contract") or {}, raw.get("procedure") or {}, raw.get("tender_detail") or {})


def make_flag(
    *,
    rule: str,
    tier: str,
    severity: str,
    message: str,
    explanation: str,
    documents: list[str],
    subject_type: str,
    subject_id: str,
    details: dict[str, Any],
    law_ref: str | None,
    procurement_id: int | None = None,
    subject_key: str | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a flag dict honoring the shared Flag contract (see module docstring)."""
    if tier not in TIERS:  # pragma: no cover - programming error guard
        raise ValueError(f"unknown tier {tier!r}")
    return {
        "rule": rule,
        "tier": tier,
        "severity": severity,
        "message": message,
        "explanation": explanation,
        "documents_json": list(documents),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "subject_key": subject_key or subject_id,
        "details_json": details,
        "law_ref": law_ref,
        "procurement_id": procurement_id,
        "sources": [src for src in (sources or []) if src],
    }


def severity_by_value(value: float, warning_eur: float, high_eur: float) -> str:
    if value >= high_eur:
        return "high"
    if value >= warning_eur:
        return "warning"
    return "info"


# ---------------------------------------------------------------------------
# Procedure types
# ---------------------------------------------------------------------------
#
# Distinct `procurements.procedure_type` values in data/nessebar.db
# (2026-10-07): EOP signed contracts carry the ЦАИС ЕОП enum
# (`raw_json.contract.ProcedureType`); EOP open tenders carry the Bulgarian
# label (`raw_json.procedure.ProcedureType`); SIGMA carries its own short
# Bulgarian label. Counts matched one-to-one across sources:
#   OpenProcedure (183)                         ~ "Открита процедура" ~ SIGMA "Открита" (182)
#   PublicCompetition (95)                      ~ "Публично състезание" ~ SIGMA "Състезание" (95)
#   CollectingOffersWithNotice (104)            ~ "Събиране на оферти с обява" ~ SIGMA "Събиране на оферти" (104)
#   NegotiatedProcedure (19) + DirectNegotiation (4)  ~ SIGMA "Пряко / без обявление" (23)
#   InvitationToSpecificEconomicOperators (7)   ~ "Покана до определени лица" ~ SIGMA "Договаряне с покана" (7)
# Every EOP NegotiatedProcedure contract's `raw_json.procedure.ProcedureType`
# reads "Договаряне без предварително обявление" (ЗОП чл. 18, ал. 1, т. 8),
# and every DirectNegotiation reads "Пряко договаряне" (т. 13).

#: Regime "tier" a procedure type belongs to, i.e. the ЗОП чл. 20 band it is
#: the ordinary route for: 1 = ал. 3 (обява / покана), 2 = ал. 2 (публично
#: състезание / пряко договаряне), 3 = ал. 1 (open procedure and the other
#: чл. 18, ал. 1, т. 1-11 procedures). None = unknown/ambiguous.
PROCEDURE_REGIME_TIER: dict[str, int | None] = {
    "CollectingOffersWithNotice": 1,
    "Събиране на оферти с обява": 1,
    "Събиране на оферти": 1,
    "InvitationToSpecificEconomicOperators": 1,
    "Покана до определени лица": 1,
    "Договаряне с покана": 1,
    "PublicCompetition": 2,
    "Публично състезание": 2,
    "Състезание": 2,
    "DirectNegotiation": 2,
    "Пряко договаряне": 2,
    "OpenProcedure": 3,
    "Открита процедура": 3,
    "Открита": 3,
    "NegotiatedProcedure": 3,
    "Договаряне без предварително обявление": 3,
    "Конкурс за проект - открит": 3,
    "Пряко / без обявление": None,  # SIGMA lumps т. 8 and т. 13 together
}

#: The negotiated / no-notice family: (Bulgarian label, legal grounds, fine).
EXCEPTIONAL_PROCEDURES: dict[str, tuple[str, str, str | None]] = {
    "NegotiatedProcedure": (
        "договаряне без предварително обявление",
        "чл. 18, ал. 1, т. 8 и чл. 79, ал. 1 и 6 ЗОП",
        "чл. 250а ЗОП",
    ),
    "Договаряне без предварително обявление": (
        "договаряне без предварително обявление",
        "чл. 18, ал. 1, т. 8 и чл. 79, ал. 1 и 6 ЗОП",
        "чл. 250а ЗОП",
    ),
    "DirectNegotiation": (
        "пряко договаряне",
        "чл. 18, ал. 1, т. 13 и чл. 182, ал. 1 и 2 ЗОП",
        "чл. 250а ЗОП",
    ),
    "Пряко договаряне": (
        "пряко договаряне",
        "чл. 18, ал. 1, т. 13 и чл. 182, ал. 1 и 2 ЗОП",
        "чл. 250а ЗОП",
    ),
    "Пряко / без обявление": (
        "пряко договаряне / договаряне без обявление",
        "чл. 79, ал. 1 и чл. 182, ал. 1 ЗОП",
        "чл. 250а ЗОП",
    ),
    "InvitationToSpecificEconomicOperators": (
        "покана до определени лица",
        "чл. 191, ал. 1 ЗОП",
        None,
    ),
    "Покана до определени лица": ("покана до определени лица", "чл. 191, ал. 1 ЗОП", None),
    "Договаряне с покана": ("покана до определени лица", "чл. 191, ал. 1 ЗОП", None),
}

#: Procedure types with a statutory minimum offer period:
#: key -> (Bulgarian label, threshold field prefix, legal article).
OFFER_PERIOD_PROCEDURES: dict[str, tuple[str, str, str]] = {
    "OpenProcedure": ("открита процедура", "open", "чл. 74, ал. 1 ЗОП"),
    "Открита процедура": ("открита процедура", "open", "чл. 74, ал. 1 ЗОП"),
    "PublicCompetition": ("публично състезание", "public_competition", "чл. 178, ал. 2 ЗОП"),
    "Публично състезание": ("публично състезание", "public_competition", "чл. 178, ал. 2 ЗОП"),
    "CollectingOffersWithNotice": (
        "събиране на оферти с обява",
        "collecting_offers",
        "чл. 188, ал. 1 ЗОП",
    ),
    "Събиране на оферти с обява": (
        "събиране на оферти с обява",
        "collecting_offers",
        "чл. 188, ал. 1 ЗОП",
    ),
}

#: Bulgarian label for the procedure, for messages.
PROCEDURE_LABEL: dict[str, str] = {
    "CollectingOffersWithNotice": "събиране на оферти с обява",
    "InvitationToSpecificEconomicOperators": "покана до определени лица",
    "PublicCompetition": "публично състезание",
    "DirectNegotiation": "пряко договаряне",
    "OpenProcedure": "открита процедура",
    "NegotiatedProcedure": "договаряне без предварително обявление",
}


def procedure_label(procedure_type: str | None) -> str:
    if not procedure_type:
        return "неизвестна процедура"
    return PROCEDURE_LABEL.get(procedure_type, procedure_type.lower())
