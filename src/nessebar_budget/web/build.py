"""Static site generator for "Бюджетен монитор Несебър".

Reads the local SQLite database (procurements, budget ledger, cash execution,
flags) and renders a fully static, dependency-free site to `out_dir` (default
`./site`) with Jinja2 templates. No FastAPI, no server, no JS framework: the
output is meant to be published as-is via GitHub Pages under a sub-path, so
every link/asset reference in the templates is relative.

Usage:
    python -m nessebar_budget.web.build --out site

Also exposes `build_site(out_dir, db_url=None)` for programmatic/test use.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from nessebar_budget.analysis.rules._common import _local_date
from nessebar_budget.db.budget_models import BudgetLineItem, CashExecutionLine
from nessebar_budget.db.models import CadastreParcel, Company, CompanyPerson, Flag, Procurement
from nessebar_budget.scrapers.registry import normalize_eiks
from nessebar_budget.web import geo

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "nessebar.db"
MINFIN_QUARTERLY_PATH = PROJECT_ROOT / "data" / "minfin" / "quarterly_Q2_2026.xlsx"
REPO_URL = "https://github.com/DimitarMarenov33/NessebarBudgetChecker"

#: EOP's own `contract.ProcedureType` field is an English enum for signed
#: contracts; open procedures (no contract yet) carry a Bulgarian label
#: straight from `procedure.ProcedureType` instead. This maps the English
#: codes to the same Bulgarian labels so the UI never shows raw enum names.
PROCEDURE_TYPE_BG = {
    "OpenProcedure": "Открита процедура",
    "CollectingOffersWithNotice": "Събиране на оферти с обява",
    "PublicCompetition": "Публично състезание",
    "NegotiatedProcedure": "Договаряне",
    "InvitationToSpecificEconomicOperators": "Покана до определени лица",
    "DirectNegotiation": "Пряко договаряне (без обявление)",
}

MONTH_NAMES_BG = {
    1: "януари", 2: "февруари", 3: "март", 4: "април", 5: "май", 6: "юни",
    7: "юли", 8: "август", 9: "септември", 10: "октомври", 11: "ноември", 12: "декември",
}

#: SIGMA matching tolerances for the eop<->sigma dedupe heuristic (see
#: docs/SITE.md "Дублиране и обогатяване" for the full writeup).
SIGMA_VALUE_TOLERANCE = 0.01
SIGMA_DATE_TOLERANCE_DAYS = 3

#: Sentinel used to sort rows with a missing date first/last. SQLite's
#: DATETIME columns round-trip as naive datetimes (no tzinfo survives the
#: TEXT storage), so every real value compared against this is naive too --
#: a naive sentinel is correct here, not an oversight.
_NAIVE_MIN = dt.datetime.min  # noqa: DTZ901


# --------------------------------------------------------------------------
# Formatting helpers (also registered as Jinja filters)
# --------------------------------------------------------------------------


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def fmt_num(value: Any) -> str:
    """Comma-grouped integer, no decimals (12347905.4 -> '12,347,905')."""
    f = _as_float(value)
    if f is None:
        return "—"
    return f"{round(f):,}"


def fmt_eur(value: Any) -> str:
    """Comma-grouped EUR amount, no decimals (e.g. '12,347,905 €')."""
    f = _as_float(value)
    if f is None:
        return "—"
    return f"{round(f):,} €"


def fmt_pct(value: Any, decimals: int = 1) -> str:
    """`value` is a fraction (0.784 -> '78.4%')."""
    f = _as_float(value)
    if f is None:
        return "—"
    return f"{f * 100:,.{decimals}f}%"


#: Canonical severity buckets. The rules engine emits `info` / `warning` /
#: `high`; older/other vocabularies (critical, serious, ...) fold into these
#: three so the UI (chips, filters, counts) only ever deals with one scale.
SEVERITY_ORDER = ("high", "warning", "info")
SEVERITY_LABELS = {"high": "Висока", "warning": "Средна", "info": "Ниска"}
_SEVERITY_ALIASES = {
    "critical": "high", "high": "high",
    "serious": "warning", "medium": "warning", "warning": "warning",
    "info": "info", "good": "info", "ok": "info", "low": "info",
}


def severity_key(severity: str | None) -> str:
    """Map any severity string onto one of SEVERITY_ORDER (default: warning)."""
    return _SEVERITY_ALIASES.get((severity or "").strip().lower(), "warning")


def severity_label(severity: str | None) -> str:
    return SEVERITY_LABELS[severity_key(severity)]


#: Citizen-facing "tier" of a flag -- a coarser, plain-language read of how
#: confident the flag is, independent of (though loosely correlated with)
#: `severity`. `violation`: a direct mismatch with the law, visible from the
#: data alone. `signal`: a pattern consistent with an irregularity that needs
#: documents to confirm one way or the other. `opacity`: legal as far as the
#: data shows, but not independently verifiable without more documents.
#: The `tier` column itself is being added to `Flag` by a concurrent
#: workstream; until it exists/is populated, `_tier_key()` below falls back
#: to a per-rule guess, then to severity -- see its docstring.
TIER_ORDER = ("violation", "signal", "opacity")
TIER_LABELS = {"violation": "Нарушение", "signal": "Сигнал", "opacity": "Непрозрачност"}
TIER_TOOLTIPS = {
    "violation": "Пряко несъответствие със закона, видимо от данните",
    "signal": "Модел, съвместим с нередност; нужни са документи",
    "opacity": "Законно, но непроверимо без допълнителни документи",
}

#: Best-effort tier for a flag whose `tier` column is missing/NULL (older DB
#: schema, or a rule the concurrent tier-tagging pass hasn't reached yet),
#: keyed by rule name. Covers both the rules live in this DB today and the
#: additional rule names named in the current task brief, so the UI reads
#: sensibly the moment those rules start producing flags too. A real `tier`
#: value on the row always wins over this guess.
_TIER_FALLBACK_BY_RULE: dict[str, str] = {
    "late_publication": "violation",
    "missing_value": "violation",
    "annex_over_cap": "violation",
    "missing_annual_report": "violation",
    "annex_growth": "signal",
    "plan_jump": "signal",
    "overspend_vs_plan": "signal",
    "single_bidder": "signal",
    "contractor_concentration": "signal",
    "splitting": "signal",
    "exceptional_procedure": "signal",
    "short_offer_deadline": "signal",
    "bid_at_ceiling": "signal",
    "near_threshold": "signal",
    "eu_funded_irregularity": "signal",
    "unmatched_spending": "opacity",
    "missing_quantity": "opacity",
    "price_unverifiable": "opacity",
    "unplanned_spending": "opacity",
    "missing_monthly_report": "opacity",
}
#: Severity -> tier, used only when neither a real `tier` value nor a
#: per-rule guess above is available (e.g. a brand-new rule name).
_TIER_FALLBACK_BY_SEVERITY = {"high": "violation", "warning": "signal", "info": "opacity"}


def tier_key(tier: str | None, rule: str | None = None, severity: str | None = None) -> str:
    """Resolve any (possibly missing) `tier` value to one of TIER_ORDER."""
    t = (tier or "").strip().lower()
    if t in TIER_ORDER:
        return t
    if rule and rule in _TIER_FALLBACK_BY_RULE:
        return _TIER_FALLBACK_BY_RULE[rule]
    return _TIER_FALLBACK_BY_SEVERITY[severity_key(severity)]


def tier_label(tier: str | None, rule: str | None = None, severity: str | None = None) -> str:
    return TIER_LABELS[tier_key(tier, rule, severity)]


def tier_tooltip(tier: str | None, rule: str | None = None, severity: str | None = None) -> str:
    return TIER_TOOLTIPS[tier_key(tier, rule, severity)]


def site_path(href: str | None) -> str:
    """Strip a leading '../' from a subject href built relative to a
    first-level page (e.g. '../contracts/1.html' -> 'contracts/1.html'), so
    templates can re-root it with `{{ root }}` from any depth."""
    if not href:
        return ""
    return href.removeprefix("../")


def fmt_date(value: Any) -> str:
    """dd.mm.yyyy in Sofia time. Datetimes in this project are naive UTC
    (ЦАИС ЕОП stores a contract signed on 20.10 as 19.10 21:00 UTC), so they
    are converted to the Sofia calendar date first; plain `date` values are
    already calendar dates and are printed as they are."""
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, dt.datetime):
        value = _local_date(value)
    return value.strftime("%d.%m.%Y")


def fmt_period(period: str | None) -> str:
    """'2026-08' -> 'август 2026'."""
    if not period:
        return "—"
    try:
        year_s, month_s = period.split("-")
        return f"{MONTH_NAMES_BG[int(month_s)]} {year_s}"
    except (ValueError, KeyError):
        return period


def fmt_quarter(label: str | None) -> str:
    """'2026 Q2' -> 'II тримесечие на 2026'."""
    if not label:
        return "—"
    try:
        year_s, q_s = label.split(" Q")
        roman = {"1": "I", "2": "II", "3": "III", "4": "IV"}[q_s]
        return f"{roman} тримесечие на {year_s}"
    except (ValueError, KeyError):
        return label


def iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return str(value)


# --------------------------------------------------------------------------
# Data classes for the "contracts" view
# --------------------------------------------------------------------------


@dataclass
class ContractView:
    """One row in the deduped contracts table: always sourced from an `eop`
    Procurement row, optionally enriched with a matched SIGMA row."""

    id: int
    source_id: str
    title: str | None
    contractor_name: str | None
    contractor_eik: str | None
    contractor_key: str | None
    contractor_slug: str | None
    is_signed: bool
    procedure_type: str | None
    contract_date: dt.datetime | None
    published_at: dt.datetime | None
    contract_value_eur: float | None
    estimated_value_eur: float | None
    url: str | None
    cpv_code: str | None
    year: int | None
    bids_received: int | None = None
    sigma_source_id: str | None = None
    sigma_procedure_type: str | None = None
    sigma_unp: str | None = None
    sigma_sector_code: Any = None
    sigma_eu_funded: bool | None = None
    flags: list[Any] = field(default_factory=list)
    #: "Точни места" feature dict (see `web.geo.build_locations`), attached
    #: once the map's exact-location data is built; `None` when this
    #: contract's title could not be placed at a parcel/street.
    location: dict[str, Any] | None = None

    @property
    def display_value_eur(self) -> float | None:
        return self.contract_value_eur if self.contract_value_eur is not None else self.estimated_value_eur

    @property
    def value_label(self) -> str:
        return "стойност по договор" if self.contract_value_eur is not None else "прогнозна стойност"


def _slugify(value: str) -> str:
    """URL/filename-safe slug. Falls back to a short hash for values with no
    ASCII alnum content at all (e.g. a Cyrillic-only string)."""
    safe = re.sub(r"[^0-9A-Za-z_-]+", "-", value.strip()).strip("-")
    if not safe:
        safe = "x" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return safe[:80]


#: EIK values the source data uses as a literal placeholder rather than a
#: real identifier (Bulgarian law lets a contracting individual's EIK/ЕГН go
#: unpublished). Grouping these together as "one contractor" would wrongly
#: merge unrelated people, so such rows fall back to grouping by name instead.
_EIK_PLACEHOLDER = "не се публикува"


def _contractor_group_key(eik: str | None, name: str | None, procurement_id: int) -> str:
    """Key used to group/aggregate contracts by contractor, and (slugified)
    to build the contractor's file name. Prefers a real EIK; falls back to
    the contractor name when the EIK is missing or a known placeholder; and,
    failing that, a per-procurement key so unrelated rows never merge."""
    if eik and eik.strip() and eik.strip() != _EIK_PLACEHOLDER:
        return eik.strip()
    if name and name.strip():
        return name.strip()
    return f"unknown-{procurement_id}"


def _procedure_label(raw: str | None) -> str | None:
    if raw is None:
        return None
    return PROCEDURE_TYPE_BG.get(raw, raw)


def _match_sigma(
    eop_rows: list[Procurement], sigma_rows: list[Procurement]
) -> tuple[dict[int, Procurement], dict[str, int]]:
    """Dedupe heuristic: for each signed `eop` contract, find the best SIGMA
    row for the *same* contractor+value+date, and use it only to enrich the
    eop record (bids_received, SIGMA's own procedure_type, raw_json extras)
    -- SIGMA rows are never shown as separate contracts.

    Match key: same `contractor_eik`, `contract_value_eur` within +-1%, and
    `contract_date` within +-3 days. Candidates are scored by (date
    difference, value difference) and assigned greedily, in eop-id order,
    without reusing a SIGMA row for two different eop contracts.
    """
    sigma_by_eik: dict[str, list[Procurement]] = defaultdict(list)
    for s in sigma_rows:
        if s.contractor_eik and s.contract_value_eur is not None and s.contract_date is not None:
            sigma_by_eik[s.contractor_eik].append(s)

    used_sigma_ids: set[int] = set()
    match_by_eop_id: dict[int, Procurement] = {}
    eligible = 0
    multi_candidate = 0

    for e in sorted(eop_rows, key=lambda r: r.id):
        if not e.contractor_eik or e.contract_value_eur is None or e.contract_date is None:
            continue
        eligible += 1
        ev = float(e.contract_value_eur)
        if ev == 0:
            continue
        candidates = []
        for s in sigma_by_eik.get(e.contractor_eik, []):
            if s.id in used_sigma_ids:
                continue
            sv = float(s.contract_value_eur)
            if abs(sv - ev) / ev > SIGMA_VALUE_TOLERANCE:
                continue
            date_diff = abs((s.contract_date - e.contract_date).days)
            if date_diff > SIGMA_DATE_TOLERANCE_DAYS:
                continue
            candidates.append((date_diff, abs(sv - ev), s))
        if not candidates:
            continue
        if len(candidates) > 1:
            multi_candidate += 1
        candidates.sort(key=lambda c: (c[0], c[1]))
        best = candidates[0][2]
        used_sigma_ids.add(best.id)
        match_by_eop_id[e.id] = best

    stats = {
        "eop_with_value_and_eik": eligible,
        "matched": len(match_by_eop_id),
        "unmatched": eligible - len(match_by_eop_id),
        "multi_candidate": multi_candidate,
        "sigma_total": len(sigma_rows),
        "sigma_matched": len(used_sigma_ids),
    }
    return match_by_eop_id, stats


def _build_contracts(
    session: Session, flag_rows: list[Any]
) -> tuple[list[ContractView], dict[str, int], dict[int, list[Any]], dict[int, Procurement]]:
    eop_rows = list(session.scalars(select(Procurement).where(Procurement.source == "eop")))
    sigma_rows = list(session.scalars(select(Procurement).where(Procurement.source == "sigma")))

    flags_by_proc_id: dict[int, list[Any]] = defaultdict(list)
    for flag in flag_rows:
        proc_id = getattr(flag, "procurement_id", None)
        if proc_id is not None:
            flags_by_proc_id[proc_id].append(flag)

    match_by_eop_id, stats = _match_sigma(eop_rows, sigma_rows)

    contracts: list[ContractView] = []
    for e in eop_rows:
        match = match_by_eop_id.get(e.id)
        year = None
        if e.contract_date is not None:
            year = _local_date(e.contract_date).year
        elif e.published_at is not None:
            year = _local_date(e.published_at).year

        contractor_key = (
            _contractor_group_key(e.contractor_eik, e.contractor_name, e.id)
            if (e.contractor_eik or e.contractor_name)
            else None
        )
        view = ContractView(
            id=e.id,
            source_id=e.source_id,
            title=e.title,
            contractor_name=e.contractor_name,
            contractor_eik=e.contractor_eik,
            contractor_key=contractor_key,
            contractor_slug=_slugify(contractor_key) if contractor_key else None,
            is_signed=e.contract_value_eur is not None,
            procedure_type=_procedure_label(e.procedure_type),
            contract_date=e.contract_date,
            published_at=e.published_at,
            contract_value_eur=_as_float(e.contract_value_eur),
            estimated_value_eur=_as_float(e.estimated_value_eur),
            url=e.url,
            cpv_code=e.cpv_code,
            year=year,
            flags=flags_by_proc_id.get(e.id, []),
        )
        if match is not None:
            match_raw = match.raw_json or {}
            view.bids_received = match.bids_received
            view.sigma_source_id = match.source_id
            view.sigma_procedure_type = match.procedure_type
            view.sigma_unp = match_raw.get("unp")
            view.sigma_sector_code = match_raw.get("sector_code")
            view.sigma_eu_funded = match_raw.get("eu_funded")
        contracts.append(view)

    contracts.sort(
        key=lambda c: (c.contract_date or c.published_at or _NAIVE_MIN),
        reverse=True,
    )
    return contracts, stats, flags_by_proc_id, match_by_eop_id


@dataclass
class ContractorView:
    slug: str
    eik_display: str
    eik_published: bool
    name: str
    contract_count: int
    total_value_eur: float
    share_of_total: float
    single_bidder_known: int
    single_bidder_count: int
    contracts: list[ContractView]
    #: Trade Register card(s) for this contractor's member company/companies
    #: (one per EIK found in `eik_display`; several for a consortium), plus a
    #: single aggregated status dot for the contractors index. See
    #: `_attach_company_views()`.
    tr_cards: list[CompanyCardView] = field(default_factory=list)
    tr_status_key: str = "none"
    tr_status_dot: str = "neutral"
    tr_status_label: str = "Няма данни"

    @property
    def single_bidder_share(self) -> float | None:
        if self.single_bidder_known == 0:
            return None
        return self.single_bidder_count / self.single_bidder_known


def _build_contractors(contracts: list[ContractView]) -> list[ContractorView]:
    signed = [c for c in contracts if c.is_signed and c.contractor_key]
    total_value = sum(c.contract_value_eur or 0.0 for c in signed)

    by_key: dict[str, list[ContractView]] = defaultdict(list)
    for c in signed:
        by_key[c.contractor_key].append(c)

    contractors = []
    for key, rows in by_key.items():
        value = sum(c.contract_value_eur or 0.0 for c in rows)
        known = [c for c in rows if c.bids_received is not None]
        single = [c for c in known if c.bids_received == 1]
        name = rows[0].contractor_name or key
        eik_published = bool(
            rows[0].contractor_eik and rows[0].contractor_eik.strip() != _EIK_PLACEHOLDER
        )
        contractors.append(
            ContractorView(
                slug=rows[0].contractor_slug,
                eik_display=rows[0].contractor_eik if eik_published else "не е публикуван",
                eik_published=eik_published,
                name=name,
                contract_count=len(rows),
                total_value_eur=value,
                share_of_total=(value / total_value) if total_value else 0.0,
                single_bidder_known=len(known),
                single_bidder_count=len(single),
                contracts=sorted(
                    rows, key=lambda c: (c.contract_date or _NAIVE_MIN), reverse=True
                ),
            )
        )
    contractors.sort(key=lambda c: c.total_value_eur, reverse=True)
    return contractors


# --------------------------------------------------------------------------
# Trade Register (Търговски регистър) — "companies"/"company_people" view
# --------------------------------------------------------------------------

#: `CompanyPerson.role` -> capitalized Bulgarian label for the registry card.
COMPANY_ROLE_LABELS: dict[str, str] = {
    "manager": "Управител",
    "partner": "Съдружник",
    "sole_owner": "Едноличен собственик",
    "board_member": "Член на съвета",
    "representative": "Представител",
    "liquidator": "Ликвидатор",
    "other": "Друго",
}

#: `Company.status` -> Bulgarian label. A missing/unrecognised status (incl.
#: the schema's own `unknown`) falls back to "Няма данни" -- the registry
#: simply hasn't resolved a definite state for this company.
COMPANY_STATUS_LABELS: dict[str, str] = {
    "active": "Активна",
    "liquidation": "В ликвидация",
    "insolvency": "В несъстоятелност",
    "deregistered": "Заличена",
}
#: `Company.status` -> dot tone (reuses the site's existing dot colours --
#: see `.dot--*` in style.css -- plus the one new `--status-active` green
#: added for this card).
_COMPANY_STATUS_DOT: dict[str, str] = {
    "active": "active",
    "liquidation": "warning",
    "insolvency": "high",
    "deregistered": "high",
}
#: Severity rank used to pick ONE status dot for a consortium contractor
#: (several member companies) on the contractors index -- the worst member
#: status wins, since that's the one worth a reader's attention.
_TR_STATUS_RANK: dict[str, int] = {"active": 1, "liquidation": 2, "insolvency": 3, "deregistered": 3}
#: Roles that make someone "stand behind" a company for the "също в"
#: cross-link (a former/"not current" or merely procedural role --
#: representative/liquidator/other -- does not count); mirrors
#: `analysis.rules.companies._CURRENT_ROLES`.
_TR_CROSS_LINK_ROLES = {"manager", "partner", "sole_owner", "board_member"}
#: A ГФО more than this many years old is flagged stale (current year - 2).
_TR_STALE_REPORT_YEARS = 2
_TR_SOLE_TRADER_FORM = "Едноличен търговец"
TR_STALE_REPORT_NOTE = (
    "Търговците обявяват ГФО до 30 септември на следващата година (чл. 38, ал. 1, т. 1 ЗСч)."
)
TR_NAMESAKE_NOTE = (
    "Съвпадението „също в“ е по пълно име (три или повече думи) — възможно е да става дума за "
    "различни хора с еднакво име (съименици)."
)


def _company_status_label(status: str | None) -> str:
    return COMPANY_STATUS_LABELS.get(status or "", "Няма данни")


def _company_status_dot(status: str | None) -> str:
    return _COMPANY_STATUS_DOT.get(status or "", "neutral")


@dataclass
class CompanyPersonView:
    name: str
    role_label: str
    share_text: str | None
    person_eik: str | None
    #: (contractor_slug, contractor_name) pairs for every OTHER contractor
    #: company where this same full name currently holds a qualifying role
    #: (see `_TR_CROSS_LINK_ROLES`) -- "също в" links. Deterministic exact
    #: match on `name_normalized`, 3+ word names only (see `docs/SITE.md`).
    also_at: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class CompanyCardView:
    eik: str
    name: str | None
    legal_form: str | None
    status_key: str
    status_label: str
    status_dot: str
    seat_address: str | None
    nkid_code: str | None
    nkid_label: str | None
    #: Free-text `activity`, truncated to ~200 chars (only set -- together
    #: with `activity_full` -- when there's no nkid_code/nkid_label to show
    #: instead).
    activity_short: str | None
    activity_full: str | None
    capital_eur: float | None
    #: "Вписана в ТР" normally; for an EIK not starting with "2" (a
    #: pre-2008 company re-registered into the unified registry, not newly
    #: founded) this is instead "В ТР от (пререгистрация от съдебния
    #: регистър)", since `registered_at` for those is the 2008-2011
    #: transfer date, not a founding date.
    registered_label: str
    registered_at: dt.date | None
    last_annual_report_year: int | None
    #: True when `last_annual_report_year` is more than `_TR_STALE_REPORT_YEARS`
    #: behind the current year (sole traders are exempt -- see law_ref).
    report_stale: bool
    source_url: str | None
    fetched_at: dt.datetime | None
    people: list[CompanyPersonView]
    has_cross_links: bool


def _company_activity(company: Company) -> tuple[str | None, str | None]:
    """(activity_short, activity_full) -- only used when the company has no
    НКИД code/label; `activity_full` is set (non-None) only when the text
    was actually truncated, so the template's "още" toggle only shows up
    when there's more to reveal."""
    text = (company.activity or "").strip()
    if not text:
        return None, None
    if len(text) <= 200:
        return text, None
    return text[:200].rstrip() + "…", text


_SHARE_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(лв\.?|€)")


def _fmt_share(text: str | None) -> str | None:
    """Registry share text ("50250.00 лв.") with this site's number style
    ("50,250 лв."); the currency is kept as the registry states it. The CSV
    export keeps the raw registry text."""
    if not text:
        return text

    def _one(m: re.Match[str]) -> str:
        amount = float(m.group(1).replace(",", "."))
        decimals = 0 if amount == int(amount) else 2
        unit = "лв." if m.group(2).startswith("лв") else "€"
        return f"{amount:,.{decimals}f} {unit}"

    return _SHARE_AMOUNT_RE.sub(_one, text)


def _build_company_card(
    company: Company,
    people: list[CompanyPerson],
    name_index: dict[str, list[tuple[int, set[str]]]],
    contractor_name_by_slug: dict[str, str],
    current_slug: str,
    current_year: int,
) -> CompanyCardView:
    activity_short, activity_full = (
        (None, None) if company.nkid_code else _company_activity(company)
    )
    report_stale = (
        company.last_annual_report_year is not None
        and company.last_annual_report_year < (current_year - _TR_STALE_REPORT_YEARS)
        and company.legal_form != _TR_SOLE_TRADER_FORM
    )

    people_views: list[CompanyPersonView] = []
    has_cross_links = False
    for p in people:
        if not p.is_current:
            continue
        also_at: list[tuple[str, str]] = []
        if len((p.name_normalized or "").split()) >= 3:
            slugs: set[str] = set()
            for comp_id, co_slugs in name_index.get(p.name_normalized, []):
                if comp_id != company.id:
                    slugs |= co_slugs
            slugs.discard(current_slug)
            also_at = sorted(
                ((s, contractor_name_by_slug.get(s, s)) for s in slugs),
                key=lambda t: t[1],
            )
        has_cross_links = has_cross_links or bool(also_at)
        people_views.append(
            CompanyPersonView(
                name=p.name,
                role_label=COMPANY_ROLE_LABELS.get(p.role, p.role),
                share_text=_fmt_share(p.share_text),
                person_eik=p.person_eik,
                also_at=also_at,
            )
        )

    return CompanyCardView(
        eik=company.eik,
        name=company.name,
        legal_form=company.legal_form,
        status_key=company.status or "unknown",
        status_label=_company_status_label(company.status),
        status_dot=_company_status_dot(company.status),
        seat_address=company.seat_address,
        nkid_code=company.nkid_code,
        nkid_label=company.nkid_label,
        activity_short=activity_short,
        activity_full=activity_full,
        capital_eur=_as_float(company.capital_eur),
        registered_label=(
            "Вписана в ТР"
            if (company.eik or "").startswith("2")
            else "В ТР от (пререгистрация от съдебния регистър)"
        ),
        registered_at=company.registered_at.date() if company.registered_at else None,
        last_annual_report_year=company.last_annual_report_year,
        report_stale=report_stale,
        source_url=company.source_url,
        fetched_at=company.fetched_at,
        people=people_views,
        has_cross_links=has_cross_links,
    )


def _contractor_tr_status(cards: list[CompanyCardView]) -> tuple[str, str, str]:
    """One (status_key, dot, label) for the contractors index -- the worst
    member company's status wins (see `_TR_STATUS_RANK`); "none"/grey when
    no member company was found in the Trade Register at all."""
    if not cards:
        return "none", "neutral", "Няма данни"
    best = max(cards, key=lambda c: _TR_STATUS_RANK.get(c.status_key, 0))
    if _TR_STATUS_RANK.get(best.status_key, 0) == 0:
        return "none", "neutral", "Няма данни"
    return best.status_key, best.status_dot, best.status_label


def _attach_company_views(
    contractors: list[ContractorView],
    companies: list[Company],
    people: list[CompanyPerson],
    current_year: int,
) -> None:
    """Populate every `ContractorView.tr_cards`/`tr_status_*` in place.

    A contractor's member company/companies are found by normalising
    `eik_display` (handles both a plain EIK and a consortium's "eik1; eik2")
    and looking each one up in `Company.eik` (already the registry's own
    canonical 9/13-digit, zero-padded form -- see `scrapers.registry`). A
    contractor with no eik_published, a placeholder EIK, or an EIK the
    registry never returned a deed for (e.g. a ДЗЗД/BULSTAT-only entity, or
    a fetch that failed -- see `docs/sources/REGISTRY_API.md`) simply gets
    no card and the "no data" grey dot.
    """
    companies_by_eik = {c.eik: c for c in companies}
    people_by_company: dict[int, list[CompanyPerson]] = defaultdict(list)
    for p in people:
        people_by_company[p.company_id].append(p)

    contractor_name_by_slug = {co.slug: co.name for co in contractors}

    # company_id -> every contractor slug it is linked to (a company can
    # appear under more than one slug: solo, and/or as a consortium member
    # grouped under a different raw "eik1; eik2" key).
    company_slugs: dict[int, set[str]] = defaultdict(set)
    matched_by_slug: dict[str, list[Company]] = {}
    for co in contractors:
        matched = [
            companies_by_eik[eik] for eik in normalize_eiks(co.eik_display) if eik in companies_by_eik
        ]
        if matched:
            matched_by_slug[co.slug] = matched
            for comp in matched:
                company_slugs[comp.id].add(co.slug)

    # name_normalized -> [(company_id, {contractor slugs}), ...] for every
    # CURRENT person in a qualifying role at a company that IS some
    # contractor on this site (otherwise there's nowhere to link "също в" to).
    name_index: dict[str, list[tuple[int, set[str]]]] = defaultdict(list)
    for comp_id, slugs in company_slugs.items():
        for p in people_by_company.get(comp_id, []):
            if not p.is_current or p.role not in _TR_CROSS_LINK_ROLES:
                continue
            if len((p.name_normalized or "").split()) < 3:
                continue
            name_index[p.name_normalized].append((comp_id, slugs))

    for co in contractors:
        cards = [
            _build_company_card(
                comp, people_by_company.get(comp.id, []), name_index,
                contractor_name_by_slug, co.slug, current_year,
            )
            for comp in matched_by_slug.get(co.slug, [])
        ]
        co.tr_cards = cards
        co.tr_status_key, co.tr_status_dot, co.tr_status_label = _contractor_tr_status(cards)


def _export_companies(companies: list[Company], data_dir: Path) -> None:
    fieldnames = [
        "eik", "name", "legal_form", "status", "seat_address", "nkid_code", "nkid_label",
        "capital_eur", "registered_at", "last_annual_report_year", "source_url", "fetched_at",
    ]
    with (data_dir / "companies.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in companies:
            writer.writerow(
                {
                    "eik": c.eik,
                    "name": c.name,
                    "legal_form": c.legal_form,
                    "status": c.status,
                    "seat_address": c.seat_address,
                    "nkid_code": c.nkid_code,
                    "nkid_label": c.nkid_label,
                    "capital_eur": _as_float(c.capital_eur),
                    "registered_at": iso(c.registered_at),
                    "last_annual_report_year": c.last_annual_report_year,
                    "source_url": c.source_url,
                    "fetched_at": iso(c.fetched_at),
                }
            )


def _export_company_people(
    people: list[CompanyPerson], companies_by_id: dict[int, Company], data_dir: Path
) -> None:
    fieldnames = ["eik", "name", "role", "share_text", "person_eik"]
    with (data_dir / "company_people.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in people:
            comp = companies_by_id.get(p.company_id)
            writer.writerow(
                {
                    "eik": comp.eik if comp else "",
                    "name": p.name,
                    "role": p.role,
                    "share_text": p.share_text,
                    "person_eik": p.person_eik,
                }
            )


# --------------------------------------------------------------------------
# Budget (capital ledger) view
# --------------------------------------------------------------------------

CONSOLIDATED_UNIT = "Общо"


def _row_type(row: BudgetLineItem) -> str:
    extra = row.extra_json or {}
    return extra.get("row_type", "")


def _load_budget(session: Session) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(BudgetLineItem).where(BudgetLineItem.unit == CONSOLIDATED_UNIT)
        )
    )
    other_units = {
        unit
        for unit in session.scalars(select(BudgetLineItem.unit).distinct())
        if unit and unit != CONSOLIDATED_UNIT
    }

    by_period: dict[str, list[BudgetLineItem]] = defaultdict(list)
    for r in rows:
        by_period[r.period].append(r)

    periods = sorted(by_period)

    function_names: dict[str, str] = {}
    for r in rows:
        if _row_type(r) == "function_subtotal" and r.function_code:
            function_names[r.function_code] = r.object_name or r.function_code

    period_data: dict[str, dict[str, Any]] = {}
    for period in periods:
        prows = by_period[period]
        grand = next((r for r in prows if _row_type(r) == "grand_total"), None)
        paragraph_totals = [r for r in prows if _row_type(r) == "paragraph_subtotal"]
        objects = [r for r in prows if _row_type(r) == "object"]

        func_totals: dict[str, dict[str, float]] = defaultdict(
            lambda: {"estimated_total": 0.0, "spent_prior": 0.0, "plan_current": 0.0, "spent_period": 0.0}
        )
        for o in objects:
            if not o.function_code:
                continue
            bucket = func_totals[o.function_code]
            bucket["estimated_total"] += _as_float(o.estimated_total) or 0.0
            bucket["spent_prior"] += _as_float(o.spent_prior) or 0.0
            bucket["plan_current"] += _as_float(o.plan_current) or 0.0
            bucket["spent_period"] += _as_float(o.spent_period) or 0.0

        function_rows = [
            {
                "code": code,
                "name": function_names.get(code, code),
                **totals,
                "pct_executed": (totals["spent_period"] / totals["plan_current"]) if totals["plan_current"] else None,
            }
            for code, totals in sorted(func_totals.items())
        ]

        object_rows = sorted(
            [
                {
                    "function_code": o.function_code,
                    "function_name": function_names.get(o.function_code, o.function_code),
                    "paragraph": o.paragraph,
                    "subparagraph": o.subparagraph,
                    "object_name": o.object_name,
                    "estimated_total": _as_float(o.estimated_total),
                    "spent_prior": _as_float(o.spent_prior),
                    "plan_current": _as_float(o.plan_current),
                    "spent_period": _as_float(o.spent_period),
                    "pct_executed": (
                        _as_float(o.spent_period) / _as_float(o.plan_current)
                        if o.plan_current else None
                    ),
                }
                for o in objects
            ],
            key=lambda d: (d["function_code"] or "", -(d["plan_current"] or 0)),
        )

        grand_plan = _as_float(grand.plan_current) if grand else None
        grand_spent = _as_float(grand.spent_period) if grand else None
        period_data[period] = {
            "period": period,
            "grand_total": {
                "estimated_total": _as_float(grand.estimated_total) if grand else None,
                "spent_prior": _as_float(grand.spent_prior) if grand else None,
                "plan_current": grand_plan,
                "spent_period": grand_spent,
                "pct_executed": (grand_spent / grand_plan) if grand_plan else None,
            },
            "paragraph_totals": [
                {
                    "paragraph": r.paragraph,
                    "name": r.object_name,
                    "estimated_total": _as_float(r.estimated_total),
                    "plan_current": _as_float(r.plan_current),
                    "spent_period": _as_float(r.spent_period),
                }
                for r in sorted(paragraph_totals, key=lambda r: r.paragraph or "")
            ],
            "function_totals": function_rows,
            "objects": object_rows,
            "object_count": len(object_rows),
        }

    return {
        "periods": periods,
        "by_period": period_data,
        "other_units_count": len(other_units),
    }


def _function_chart_items(period_data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not period_data:
        return []
    return [
        {"label": f["name"], "value": f["plan_current"], "value2": f["spent_period"]}
        for f in period_data["function_totals"]
    ]


def _object_key(obj: dict[str, Any]) -> tuple:
    return (obj["function_code"], obj["paragraph"], obj["subparagraph"], obj["object_name"])


def _compute_period_changes(
    periods: list[str], by_period: dict[str, dict[str, Any]]
) -> dict[str, dict[str, list]]:
    """For each period (after the first), list objects that are newly present
    vs. the previous period ("added") or whose current-year plan increased
    ("increased"). Keyed by object (function/paragraph/subparagraph/name)."""
    changes: dict[str, dict[str, list]] = {}
    for i, period in enumerate(periods):
        if i == 0:
            changes[period] = {"added": [], "increased": [], "previous": None}
            continue
        prev_period = periods[i - 1]
        prev_objs = {_object_key(o): o for o in by_period[prev_period]["objects"]}
        added = []
        increased = []
        for obj in by_period[period]["objects"]:
            key = _object_key(obj)
            prev = prev_objs.get(key)
            if prev is None:
                added.append(obj)
                continue
            delta = (obj["plan_current"] or 0) - (prev["plan_current"] or 0)
            if delta > 0.5:
                increased.append({**obj, "delta": delta, "previous_plan": prev["plan_current"]})
        changes[period] = {"added": added, "increased": increased, "previous": prev_period}
    return changes


# --------------------------------------------------------------------------
# Cash execution (B1) view
# --------------------------------------------------------------------------


def _load_cash(session: Session) -> dict[str, Any]:
    rows = list(session.scalars(select(CashExecutionLine)))
    by_period: dict[str, list[CashExecutionLine]] = defaultdict(list)
    for r in rows:
        by_period[r.period].append(r)
    periods = sorted(by_period)

    period_data: dict[str, dict[str, Any]] = {}
    for period in periods:
        prows = by_period[period]
        expense = [r for r in prows if r.section == "разходи"]
        income = [r for r in prows if r.section == "приходи"]

        def extra_row_type(r: CashExecutionLine) -> str:
            return (r.extra_json or {}).get("row_type", "")

        expense_total = next(
            (r for r in expense if extra_row_type(r) == "section_total"), None
        )
        income_total = next(
            (r for r in income if extra_row_type(r) == "section_total"), None
        )
        expense_paragraphs = sorted(
            (
                {"paragraph": r.paragraph, "name": r.name, "actual_ytd": _as_float(r.actual_ytd)}
                for r in expense
                if extra_row_type(r) == "paragraph"
            ),
            key=lambda d: -(d["actual_ytd"] or 0),
        )
        period_data[period] = {
            "period": period,
            "expense_total": _as_float(expense_total.actual_ytd) if expense_total else None,
            "income_total": _as_float(income_total.actual_ytd) if income_total else None,
            "expense_paragraphs": expense_paragraphs,
        }
    return {"periods": periods, "by_period": period_data}


# --------------------------------------------------------------------------
# Minfin quarterly indicators (optional, best-effort)
# --------------------------------------------------------------------------

MINFIN_WANTED_INDICATORS = [
    "1. Дял на приходите",
    "2. Покритие",
    "3. Бюджетно салдо",
    "8. Дял на капиталовите",
]
NESEBAR_MINFIN_CODE = 5206


def _load_minfin_indicators() -> list[dict[str, Any]] | None:
    """Best-effort read of the latest quarterly Minfin municipal-indicators
    workbook. Returns None (and the page simply omits the section) on any
    unexpected shape -- this is explicitly optional, see task brief."""
    if not MINFIN_QUARTERLY_PATH.exists():
        return None
    try:
        import openpyxl

        wb = openpyxl.load_workbook(MINFIN_QUARTERLY_PATH, data_only=True)
        ws = wb["показатели"]

        groups: list[tuple[str, list[int]]] = []
        current_title = None
        current_cols: list[int] = []
        for c in range(3, ws.max_column + 1):
            title = ws.cell(row=1, column=c).value
            if title:
                if current_cols:
                    groups.append((current_title, current_cols))
                current_title = title
                current_cols = [c]
            else:
                current_cols.append(c)
        if current_cols:
            groups.append((current_title, current_cols))

        results = []
        for wanted in MINFIN_WANTED_INDICATORS:
            match = next((g for g in groups if g[0] and g[0].startswith(wanted)), None)
            if match is None:
                continue
            title, cols = match
            latest_col = cols[-1]
            label = ws.cell(row=2, column=latest_col).value
            values = []
            nessebar_value = None
            for r in range(3, ws.max_row + 1):
                code = ws.cell(row=r, column=1).value
                value = ws.cell(row=r, column=latest_col).value
                if isinstance(code, (int, float)) and value is not None:
                    values.append(value)
                    if int(code) == NESEBAR_MINFIN_CODE:
                        nessebar_value = value
            if not values or nessebar_value is None:
                continue
            results.append(
                {
                    "title": title,
                    "period_label": fmt_quarter(label),
                    "nessebar": nessebar_value,
                    "median": statistics.median(values),
                    "n_municipalities": len(values),
                }
            )
        return results or None
    except Exception:  # noqa: BLE001 -- best-effort/optional section, see docstring
        return None


# --------------------------------------------------------------------------
# Flags view
# --------------------------------------------------------------------------


def _load_flags_safe(session: Session) -> list[Any]:
    """Select all `Flag` rows, but only for columns that actually exist on the
    live DB's `flags` table right now.

    The `Flag` model is being extended with extra columns (subject_type,
    subject_id, subject_key, details_json, law_ref, ...) by another concurrent
    workstream; until that migration has actually run against this DB file,
    `select(Flag)` (which selects every *mapped* column) raises
    `OperationalError: no such column`. Introspecting the live table and
    selecting only the intersection keeps this build working across both the
    old and the new schema, without ever importing/editing `db/models.py`.

    Returns a list of SQLAlchemy `Row` objects: attribute access (`row.rule`)
    works for any selected column, and plain `getattr(row, "law_ref", None)`
    safely returns `None` for a column this DB doesn't have yet.
    """
    try:
        existing_cols = {c["name"] for c in sa_inspect(session.get_bind()).get_columns("flags")}
    except Exception:  # noqa: BLE001 -- defensive: table/engine shape is never fatal here
        return []
    columns = [c for c in Flag.__table__.columns if c.name in existing_cols]
    if not columns:
        return []
    stmt = select(*columns)
    if "resolved_at" in existing_cols:
        stmt = stmt.where(Flag.resolved_at.is_(None))
    rows = list(session.execute(stmt))
    if "created_at" in existing_cols:
        rows.sort(key=lambda r: getattr(r, "created_at", None) or _NAIVE_MIN, reverse=True)
    return rows


#: Bulgarian label + value-formatting hint for the `details_json` keys this
#: project's rules are known to emit (see docs/RULES.md). Rendered as a small
#: definition list on the flags index/detail pages -- the raw JSON itself is
#: never shown to a reader. Any key not listed here still renders (with a
#: humanised fallback label), so a new rule's details never silently vanish.
_DETAIL_LABELS: dict[str, str] = {
    # -- Trade Register / declarations rules (rules.companies) --
    "contractor_eik": "ЕИК на изпълнителя",
    "contractor_name": "Изпълнител",
    "status": "Статус в Търговския регистър",
    "last_annual_report_year": "Последен обявен ГФО за година",
    "last_no_activity_declaration_year": "Декларация за липса на дейност за година",
    "latest_contract_year": "Година на последния договор",
    "contract_count": "Брой договори",
    "total_contract_count": "Брой договори общо",
    "total_eur": "Обща стойност на договорите",
    "status_reason": "Фирмата е в ликвидация, несъстоятелност или заличена",
    "stale_report_reason": "Липсват скорошни годишни отчети",
    "registered_at": "Вписана в Търговския регистър",
    "age_days": "Възраст на фирмата при подписването",
    "name": "Лице",
    "official_name": "Длъжностно лице",
    "official_role": "Длъжност",
    "official_mandate": "Мандат",
    "person_role": "Роля във фирмата",
    "match_basis": "Основание за съвпадението",
    "nkid_code": "Код по НКИД",
    "nkid_label": "Основна дейност по НКИД",
    "cpv_code": "Код по CPV",
    "mismatch": "Несъответствие",
    "contract_date": "Дата на договора",
    "ted_publish_date": "Дата на публикуване",
    "deadline_days": "Срок по закон",
    "days_late": "Закъснение",
    "paragraph": "Параграф",
    "period": "Период",
    "plan_current": "План",
    "spent_period": "Похарчено",
    "spent_prior": "Похарчено преди периода",
    "estimated_total": "Обща стойност на обекта",
    "over_plan": "Над плана",
    "over_estimate": "Над общата стойност",
    "from_period": "От период",
    "to_period": "До период",
    "plan_before": "План преди",
    "plan_after": "План след",
    "increase_eur": "Увеличение",
    "increase_pct": "Увеличение",
    "direct_award_threshold_eur": "Праг за пряко възлагане",
    "min_value_eur": "Минимален праг за проверка",
    "original_value_eur": "Първоначална стойност",
    "current_value_eur": "Текуща стойност",
    "growth_pct": "Ръст спрямо първоначалната стойност",
    "contract_value_eur": "Стойност на договора",
    "bids_received": "Брой подадени оферти",
    "quantity_found_in": "Количество е открито в",
    "first_period": "Първи отчетен период за обекта",
    "dataset_first_period": "Начало на наличните данни",
    "year_first_period": "Първи наличен отчет за годината",
    "initial_plan": "Начален план",
    # Rules added 2026-10-07 (splitting, annex_over_cap, exceptional_procedure,
    # short_offer_deadline, bid_at_ceiling, near_threshold, unplanned_spending,
    # missing_monthly_report, missing_annual_report, eu_funded_irregularity,
    # price_unverifiable) -- same "small labelled list" treatment.
    "tender_number": "Номер на процедурата",
    "procedure_label": "Вид процедура",
    "estimated_value_eur": "Прогнозна стойност",
    "ratio": "Съотношение цена/прогнозна стойност",
    "cap_ratio": "Дял от законовия таван (чл. 116, ал. 2)",
    "commodity_exchange_mentioned": "Стокова борса",
    "fuel_cpv": "CPV код за гориво",
    "year": "Година",
    "expected_period": "Очакван период",
    "due_date": "Краен срок",
    "kinds_checked": "Проверени видове отчети",
    "type_of_contract": "Вид поръчка",
    "description_chars": "Дължина на описанието",
    "min_description_chars": "Очаквана минимална дължина",
    "reasons": "Причини",
    "category": "Категория",
    "value_eur": "Стойност",
    "value_basis": "База на стойността",
    "boundary_eur": "Праг по закон",
    "gap_pct": "Разлика до прага",
    "notice_date": "Дата на обявлението",
    "with_splitting_evidence": "Данни за разделяне на поръчка",
    "sum_eur": "Обща сума на свързаните поръчки",
    "required_regime": "Изискван ред по закон",
    "window_days": "Прозорец на проверката",
    "first_date": "Първа дата",
    "last_date": "Последна дата",
    "over_plan_eur": "Над плана с",
    "other_rules": "Други сигнали за същия обект",
}
#: Detail keys whose values are EUR amounts but whose name doesn't end in
#: `_eur` (so the generic `key.endswith("_eur")` heuristic in
#: `_format_detail_value` would otherwise miss them).
_DETAIL_EUR_KEYS = {
    "initial_plan", "plan_current", "spent_period", "spent_prior",
    "estimated_total", "plan_before", "plan_after",
}
#: Keys that duplicate what's already shown elsewhere on the page (subject
#: label, source system, ...), or are internal bookkeeping (ids/grouping
#: keys/nested record lists) not meaningful read as a bare value -- skipped
#: so the numbers list never repeats itself or leaks a raw identifier.
_DETAIL_SKIP_KEYS = {
    "name_normalized", "companies",  # matching key / nested list, shown in the message
    "object_name", "source", "source_id", "subject", "quantity_found_in",
    "sigma_twin", "twin", "member_ids", "path", "group_key", "members",
    "procedure_type",  # procedure_label is this same field, already human-readable
    "boundary_bgn",  # boundary_eur is the same legal threshold, EUR-first project convention
}
_TYPE_OF_CONTRACT_LABELS = {1: "услуги", 2: "доставки", 3: "строителство"}
_CATEGORY_LABELS = {"supplies": "доставки", "works": "строителство", "services": "услуги"}
_VALUE_BASIS_LABELS = {"estimated": "прогнозна стойност", "contract": "стойност по договор"}
_REASON_LABELS = {
    "no_quantity": "без количество",
    "no_technical_description": "без съдържателно техническо описание",
    "no_unit_prices": "без единични цени",
    "over_estimate": "над общата прогнозна стойност",
    "over_plan": "над годишния план",
}


def _format_detail_value(key: str, value: Any) -> str | None:
    """Format one `details_json` value for the citizen-facing numbers list.
    Returns None for a value too structured to show as a single line (a
    nested list of records, a dict) -- the caller skips that key entirely
    rather than dumping raw JSON."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "не"
    if isinstance(value, dict):
        return None
    if isinstance(value, list):
        if not value:
            return None
        if any(isinstance(v, (dict, list)) for v in value):
            return None
        if key == "reasons":
            return ", ".join(_REASON_LABELS.get(str(v), str(v)) for v in value)
        if key == "other_rules":
            return ", ".join(RULE_LABELS.get(str(v), str(v)) for v in value)
        return ", ".join(str(v) for v in value)
    if key == "type_of_contract":
        try:
            return _TYPE_OF_CONTRACT_LABELS.get(int(value), str(value))
        except (TypeError, ValueError):
            return str(value)
    if key == "value_basis":
        return _VALUE_BASIS_LABELS.get(str(value), str(value))
    if key == "category":
        return _CATEGORY_LABELS.get(str(value), str(value))
    if key in ("ratio", "cap_ratio"):
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)
    if key.endswith("_pct"):
        try:
            return f"{float(value):,.1f}%"
        except (TypeError, ValueError):
            return str(value)
    if key.endswith("_eur") or key in _DETAIL_EUR_KEYS:
        return fmt_eur(value)
    if key.endswith("_bgn"):
        return f"{fmt_num(value)} лв."
    if key in (
        "period", "from_period", "to_period", "first_period", "dataset_first_period",
        "year_first_period", "expected_period",
    ):
        return fmt_period(value)
    if key in ("contract_date", "ted_publish_date", "due_date", "notice_date", "first_date", "last_date"):
        return fmt_date(value)
    if key.endswith("_days") or key == "window_days":
        return f"{fmt_num(value)} дни"
    if key == "status":
        return COMPANY_STATUS_LABELS.get(str(value), str(value))
    if key == "match_basis":
        return {"name": "еднакви три имена", "declaration": "декларация за интереси"}.get(
            str(value), str(value)
        )
    if key == "person_role":
        return COMPANY_ROLE_LABELS.get(str(value), str(value))
    if key == "registered_at":
        return fmt_date(dt.date.fromisoformat(str(value)[:10]))
    if key == "year" or key.endswith("_year"):
        try:
            return str(int(value))
        except (TypeError, ValueError):
            return str(value)
    if key in ("description_chars", "min_description_chars"):
        return f"{fmt_num(value)} знака"
    if isinstance(value, (int, float)):
        return fmt_num(value)
    return str(value)


def _detail_items(details: dict[str, Any] | None) -> list[tuple[str, str, str]]:
    """`details_json` -> a small, human-labelled (key, label, value) list for
    a definition list -- never the raw JSON blob. A value too structured to
    render as one line (a nested list of records, a dict) is skipped rather
    than dumped as raw JSON. The raw `key` is kept alongside the Bulgarian
    `label` so a template can single out a specific item (e.g. "paragraph",
    to attach the "категория (§), не редът на обекта" tooltip -- see
    docs/SITE.md's Provenance box section)."""
    if not isinstance(details, dict):
        return []
    items = []
    for key, value in details.items():
        if key in _DETAIL_SKIP_KEYS or value is None:
            continue
        formatted = _format_detail_value(key, value)
        if formatted is None:
            continue
        label = _DETAIL_LABELS.get(key, key.replace("_", " ").capitalize())
        items.append((key, label, formatted))
    return items


#: Fallback "which documents would settle this" list, used only when a flag's
#: own `documents_json` (added by a concurrent workstream) is missing/empty --
#: generic enough to be true for *any* flag of that subject type, specific
#: enough to be a useful starting point for a ЗДОИ request.
_DEFAULT_DOCUMENTS_BY_SUBJECT_TYPE: dict[str, list[str]] = {
    "contract": [
        "пълния текст на договора и всички анекси/допълнителни споразумения към него",
        "документацията по процедурата (решение, обявление, протокол на комисията, мотиви за избор на изпълнител)",
    ],
    "procedure": [
        "документацията по процедурата (решение, обявление, протокол на комисията)",
    ],
    "budget_object": [
        "техническата спецификация/количествено-стойностната сметка за обекта",
        "протокола от заседанието на Общинския съвет, с което е приет или изменен планът за обекта",
    ],
    "cash_paragraph": [
        "разшифровка на разхода по конкретни документи (фактури, договори) по този параграф",
    ],
    "report": [
        "пълния текст на съответния отчет/доклад и решението на Общинския съвет за приемането му",
    ],
}
_DEFAULT_DOCUMENTS_FALLBACK = ["документите, на които се основава този сигнал"]


# --------------------------------------------------------------------------
# Provenance ("Източници") box
# --------------------------------------------------------------------------

#: Kinds of `Flag.sources_json` entries (see `analysis/provenance.py`).
_SOURCE_KIND_LABELS = {
    "budget_file": "Файл на общината",
    "eop_contract": "ЦАИС ЕОП",
    "eop_tender": "ЦАИС ЕОП",
    "sigma": "SIGMA",
    "reports_page": "Сайт на общината",
    "flag": "Сигнал на този сайт",
    "registry": "Търговски регистър",
    "declaration": "Регистър на декларациите",
}
_TYPE_OF_CONTRACT_CODE_LABELS = {1: "услуги", 2: "доставки", 3: "строителство"}
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SOURCES_BGN_NOTE = (
    "Стойностите са публикувани в лева и са преобразувани по фиксирания курс 1 € = 1,95583 лв."
)


def external_href(url: str | None) -> str:
    """Percent-encode the path/query of an original public URL (municipal file
    names carry spaces and Cyrillic) so it is a valid href; already-encoded
    sequences are left alone."""
    if not url:
        return ""
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%:/?+,;@!$'()*-._~")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _fmt_amount(value: float) -> str:
    """Comma thousands, up to 2 decimals (dropped when whole): 627,983 / 321,082.61."""
    text = f"{value:,.2f}"
    return text.removesuffix(".00")


def _fmt_source_value(field: dict[str, Any]) -> tuple[str, bool]:
    """(display text, is_numeric) for one source field's raw value."""
    value = field.get("value")
    currency = field.get("currency")
    if value is None or value == "":
        return "—", False
    if isinstance(value, bool):
        return ("да" if value else "не"), False
    if isinstance(value, (int, float)):
        if field.get("name") == "TypeOfContract" and int(value) in _TYPE_OF_CONTRACT_CODE_LABELS:
            return f"{int(value)} ({_TYPE_OF_CONTRACT_CODE_LABELS[int(value)]})", False
        text = _fmt_amount(float(value))
        if currency == "BGN":
            return f"{text} лв.", True
        if currency == "EUR":
            return f"{text} €", True
        return text, True
    text = str(value)
    if _ISO_DATE_RE.match(text):
        return fmt_date(text), True
    return text, False


def _source_views(raw: Any) -> list[dict[str, Any]]:
    """`Flag.sources_json` -> display dicts for the "Източници" box (labels,
    hrefs, "лист X, ред Y", formatted field values). Tolerates a missing
    column, a JSON string, and entries missing any key."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if not isinstance(raw, list):
        return []
    views = []
    for src in raw:
        if not isinstance(src, dict):
            continue
        kind = src.get("kind") or ""
        url = src.get("url")
        external = bool(url) and kind != "flag" and str(url).startswith(("http://", "https://"))
        location_parts = []
        if src.get("sheet"):
            location_parts.append(f"лист „{src['sheet']}“")
        if src.get("row"):
            location_parts.append(f"ред {src['row']}")
        fields = []
        for fld in src.get("fields") or []:
            if not isinstance(fld, dict):
                continue
            raw_text, numeric = _fmt_source_value(fld)
            value_eur = fld.get("value_eur")
            fields.append(
                {
                    "name": fld.get("label") or fld.get("name") or "",
                    "key": fld.get("name") if fld.get("label") else None,
                    "raw": raw_text,
                    "numeric": numeric,
                    "eur": (
                        f"{_fmt_amount(float(value_eur))} €"
                        if isinstance(value_eur, (int, float)) and not isinstance(value_eur, bool)
                        else ""
                    ),
                    "is_bgn": fld.get("currency") == "BGN",
                }
            )
        views.append(
            {
                "kind": kind,
                "kind_label": _SOURCE_KIND_LABELS.get(kind, ""),
                "label": src.get("label") or _SOURCE_KIND_LABELS.get(kind, "Източник"),
                "href": external_href(url) if external else (url or ""),
                "external": external,
                "file": src.get("file"),
                "location": ", ".join(location_parts),
                "row": src.get("row"),
                "fields": fields,
                "has_eur": any(f["eur"] for f in fields),
                "all_numeric": bool(fields) and all(f["numeric"] for f in fields),
                "note": src.get("note"),
                "rule": src.get("rule"),
            }
        )
    return views


def _sources_how_to_steps(
    sources: list[dict[str, Any]], details: dict[str, Any] | None = None
) -> list[str]:
    """The "Как да проверите" box, as 2-3 short numbered steps tailored to
    this flag's dominant source kind -- replaces the old one-sentence
    version after a beta tester opened a budget file, went to the
    "5200 Придобиване на дълготрайни материални активи" *paragraph subtotal*
    row and saw different numbers than the flag's, because the flag's
    figures are on the *object* row, not the paragraph subtotal above it.
    The steps now say explicitly which row/field to look at and (for a
    budget file) point at the object's own name, not the paragraph."""
    kinds = {s["kind"] for s in sources}
    if "budget_file" in kinds:
        object_name = (details or {}).get("object_name") if isinstance(details, dict) else None
        row = next(
            (s["row"] for s in reversed(sources) if s.get("kind") == "budget_file" and s.get("row")),
            None,
        )
        find_step = "в лист „Общо“ натиснете Ctrl+F и потърсете "
        find_step += f"името на обекта „{object_name}“" if object_name else "името на обекта"
        if row:
            find_step += f" (или отидете на ред {row})"
        find_step += "."
        return [
            "отворете файла от връзката (Excel/LibreOffice).",
            find_step,
            (
                "сравнете колоните „Сметна стойност“, „Уточнен план“ и „Усвоено към отчетния "
                "период“ с таблицата тук — стойностите във файла са в лева, тук са и в евро."
            ),
        ]
    if kinds & {"eop_contract", "eop_tender", "sigma"}:
        field_names: list[str] = []
        for s in sources:
            if s.get("kind") not in {"eop_contract", "eop_tender", "sigma"}:
                continue
            for fld in s.get("fields") or []:
                name = fld.get("name")
                if name and name not in field_names:
                    field_names.append(name)
        fields_text = ", ".join(f"„{n}“" for n in field_names[:4]) or "посочените по-долу полета"
        return [
            "отворете поръчката/договора от връзката по-горе в ЦАИС ЕОП (или CSV файла на SIGMA).",
            f"потърсете полетата {fields_text} в записа.",
            "сравнете стойностите с тези в таблицата тук.",
        ]
    if "reports_page" in kinds:
        return [
            "отворете архива с отчети на сайта на общината (връзката по-горе).",
            "потърсете файла за посочения период.",
            (
                "ако липсва — това е самият сигнал: не открихме отчет, публикуван там, където го "
                "търсихме."
            ),
        ]
    return ["отворете свързаните сигнали и проверете техните източници."]


def _zdoi_template(
    rule_label: str,
    message: str,
    subject_line: str,
    documents: list[str],
) -> str:
    """Plain-text ЗДОИ (Закон за достъп до обществена информация) request
    template -- static text, no mailto/JS: the "Копирай текст за заявление"
    box on a flag's permalink page just shows this in a <pre>."""
    doc_lines = "\n".join(f"{i}. {d}" for i, d in enumerate(documents, start=1))
    return (
        "ДО\n"
        "КМЕТА НА ОБЩИНА НЕСЕБЪР\n\n"
        "ЗАЯВЛЕНИЕ\n"
        "за достъп до обществена информация по Закона за достъп до обществена\n"
        "информация (ЗДОИ)\n\n"
        "От: [Вашето име], [адрес/имейл за кореспонденция]\n\n"
        f"Относно: {subject_line}\n\n"
        "Уважаеми господин/госпожо Кмет,\n\n"
        "На основание чл. 24 и следващите от Закона за достъп до обществена "
        "информация, моля да ми бъде предоставен достъп (копие на хартиен "
        "носител / по електронен път / преглед на оригинала — моля отбележете "
        "предпочитание) до следните документи:\n\n"
        f"{doc_lines}\n\n"
        f"Искането е свързано със следната констатация на „Бюджетен монитор "
        f"Несебър“ ({rule_label}): {message}\n\n"
        "Моля да ми бъде отговорено в законоустановения 14-дневен срок по чл. 28 "
        "ЗДОИ.\n\n"
        "Дата: __________                                   Подпис: __________\n"
    )


RULE_LABELS = {
    "late_publication": "Късно публикуване",
    "overspend_vs_plan": "Разход над плана",
    "plan_jump": "Рязко увеличение на плана",
    "single_bidder": "Един участник",
    "contractor_concentration": "Концентрация при изпълнител",
    "unmatched_spending": "Разход без открит договор",
    "annex_growth": "Нарастване чрез анекси",
    "missing_value": "Липсваща стойност",
    "missing_quantity": "Липсващо количество",
    "splitting": "Възможно разделяне на поръчка",
    "annex_over_cap": "Анекси над законовия таван",
    "exceptional_procedure": "Възлагане без публично обявление",
    "short_offer_deadline": "Кратък срок за оферти",
    "bid_at_ceiling": "Цена на тавана без конкуренция",
    "near_threshold": "Стойност точно под прага",
    "unplanned_spending": "Разход извън бюджета",
    "missing_monthly_report": "Липсващ месечен отчет",
    "missing_annual_report": "Липсващ годишен отчет",
    "eu_funded_irregularity": "Европейски средства със сигнал",
    "price_unverifiable": "Цена, която не може да се провери",
    "related_party": "Свързано лице",
    "person_concentration": "Едно лице зад няколко изпълнители",
    "young_company": "Новоучредена фирма",
    "company_status": "Статус на фирмата",
    "activity_mismatch": "Несъответствие на дейността",
}

#: The methodology page's "Правила за сигнали" section, as data rather than
#: hardcoded prose: one entry per rule documented in docs/RULES.md, each with
#: what it detects, its threshold(s), its legal basis and the documents a
#: citizen would need to request to settle the question either way. `tier`
#: is resolved through `tier_key()` (same fallback table used for real flags)
#: so this list and the live flags page never disagree about a rule's tier.
METHODOLOGY_RULES: list[dict[str, Any]] = [
    # -- Violations: a clear legal breach on the face of the data ----------
    {
        "rule": "late_publication",
        "tier": "violation",
        "detects": "Обявлението за възложена поръчка е изпратено за публикуване твърде късно след "
        "подписването на договора.",
        "threshold": "Над 30 дни от подписването на договора до изпращането за публикуване; тежестта "
        "расте с закъснението (над 14 дни — средна, над 60 дни — висока).",
        "law_ref": "чл. 26, ал. 1, т. 1 ЗОП (обявлението за възлагане се изпраща до 30 дни след "
        "сключване на договора); чл. 256а ЗОП (глоба при неизпращане в срок).",
        "documents": [
            "обявление за възложена поръчка с датата на изпращане за публикуване",
            "договорът с датата на подписване",
        ],
    },
    {
        "rule": "annex_over_cap",
        "tier": "violation",
        "detects": "Текущата стойност на договор (след анекси) е нараснала с над 50% спрямо "
        "първоначално подписаната — над законовия таван за натрупано увеличение.",
        "threshold": "Ръст над 50% от първоначалната стойност; явни грешки в данните (напр. стойност, "
        "въведена в стотинки) се изключват отделно.",
        "law_ref": "чл. 116, ал. 2 ЗОП (увеличението на цената не може да надхвърля 50% от стойността "
        "на основния договор); чл. 255, ал. 3 ЗОП (глоба при изменение без основание).",
        "documents": [
            "допълнителни споразумения (анекси) към договора",
            "мотиви/обосновка за всяко изменение",
            "обявления за изменение на договора",
            "първоначалната документация с предвидените опции",
        ],
    },
    {
        "rule": "unplanned_spending",
        "tier": "violation",
        "detects": "По капиталов обект има плащания без одобрен годишен план, или надвишаващи общата "
        "прогнозна стойност на обекта.",
        "threshold": "Разход поне 10,000 € без план, или надвишаване на общата стойност с поне "
        "10,000 €.",
        "law_ref": "чл. 128, ал. 1 ЗПФ (забрана за разходи, непредвидени в годишния бюджет); чл. 102, "
        "ал. 1 ЗПФ; чл. 124, ал. 2 ЗПФ (промените се одобряват от общинския съвет).",
        "documents": [
            "решение на общинския съвет за промяна на бюджета (чл. 124 ЗПФ)",
            "фактури и платежни нареждания по обекта",
            "договор(и) за изпълнение на обекта",
            "актуализиран разчет за капиталовите разходи",
        ],
    },
    {
        "rule": "short_offer_deadline",
        "tier": "violation",
        "detects": "Срокът за подаване на оферти (или времето от изпращане на обявлението до "
        "подписването на договора) е по-кратък от законовия минимум за избраната процедура.",
        "threshold": "Под законовия минимум (напр. 30 дни при открита процедура, 20 при публично "
        "състезание, 10 при обява) — нарушение; под минимума плюс 5-дневен марж за оценка — сигнал. "
        "В тази база засега 0 отворени случая.",
        "law_ref": "чл. 74, чл. 178, чл. 188 ЗОП (минимални срокове за подаване на оферти по вид "
        "процедура).",
        "documents": [
            "обявлението с датата на изпращане/публикуване",
            "офертите с датите на получаването им",
            "мотиви за съкратен срок, ако има такъв",
        ],
    },
    # -- Signals: a pattern consistent with misconduct, needs documents ----
    {
        "rule": "splitting",
        "tier": "signal",
        "detects": "Две или повече поръчки на един изпълнител или с близък предмет, всяка под прага за "
        "по-строга процедура, но общо за 12 месеца го надхвърлят.",
        "threshold": "Сборът надхвърля прага по ЗОП чл. 20 за реда, при който поръчките реално са "
        "възложени; по-висока тежест при сума над 2× прага.",
        "law_ref": "чл. 21, ал. 15 ЗОП (забрана за разделяне на поръчка с цел по-лек ред); чл. 20 ЗОП "
        "(стойностни прагове); чл. 247, ал. 1 ЗОП (глоба).",
        "documents": [
            "докладни записки/заявки за възникване на потребността по всяка поръчка",
            "обосновка на прогнозната стойност на всяка поръчка",
            "годишен план-график на обществените поръчки",
            "договорите и техническите спецификации",
        ],
    },
    {
        "rule": "exceptional_procedure",
        "tier": "signal",
        "detects": "Поръчка е възложена чрез преговори без предварително обявление или покана до "
        "определени лица, вместо чрез публична процедура.",
        "threshold": "Тежест по стойност на договора (над 100,000 € — средна, над 500,000 € — висока); "
        "по-ниска, когато текстовете сочат доставка на гориво/стокова борса.",
        "law_ref": "чл. 18, ал. 1, т. 13 и чл. 182, ал. 1 и 2 ЗОП; чл. 79, ал. 1 и 6 ЗОП; чл. 250а ЗОП "
        "(глоба).",
        "documents": [
            "решение за откриване на процедурата с мотивите за избраното основание",
            "покана(и) до поканените лица",
            "протокол от преговорите",
            "доказателства за основанието (напр. неотложност, изключителни права)",
        ],
    },
    {
        "rule": "bid_at_ceiling",
        "tier": "signal",
        "detects": "Цената по договора практически съвпада с максималната (прогнозна) стойност, "
        "определена от самата община, при липсваща или неизвестна конкуренция.",
        "threshold": "Стойност между 98% и 105% от прогнозната, с точно една подадена оферта или "
        "неизвестен брой; над 100,000 € — средна тежест, над 500,000 € — висока.",
        "law_ref": "чл. 21, ал. 1-2 ЗОП (прогнозната стойност се определя вкл. чрез пазарни проучвания); "
        "чл. 2, ал. 2 ЗОП (забрана за ограничаване на конкуренцията).",
        "documents": [
            "обосновка на прогнозната стойност (пазарно проучване, получени оферти)",
            "ценово предложение на изпълнителя",
            "протокол на комисията с броя на подадените оферти",
        ],
    },
    {
        "rule": "single_bidder",
        "tier": "signal",
        "detects": "Договор е възложен след подадена само една оферта.",
        "threshold": "Над 100,000 € (средна тежест) или над 500,000 € (висока тежест). Изисква брой "
        "оферти от СИГМА — ЕОП не публикува това поле директно.",
        "law_ref": "чл. 2, ал. 2 ЗОП (забрана за необосновано ограничаване на конкуренцията).",
        "documents": [
            "документация и техническа спецификация на поръчката",
            "критерии за подбор и методика за оценка",
            "протокол/доклад на комисията",
            "решение за определяне на изпълнител",
        ],
    },
    {
        "rule": "contractor_concentration",
        "tier": "signal",
        "detects": "Един изпълнител концентрира голям дял от стойността на договорите на общината за "
        "кратък период.",
        "threshold": "3+ договора и 15%+ от общата стойност за последните ~24 месеца. В тази база "
        "засега 0 отворени случая.",
        "law_ref": "чл. 2, ал. 1, т. 1-2 ЗОП (равнопоставеност, свободна конкуренция).",
        "documents": [
            "списък на всички договори на общината с този изпълнител за периода",
            "протоколите от съответните процедури по избор на изпълнител",
        ],
    },
    {
        "rule": "annex_growth",
        "tier": "signal",
        "detects": "Текущата стойност на договор (след анекси/допълнителни споразумения) е нараснала "
        "спрямо първоначално подписаната.",
        "threshold": "Над 10% ръст, но под 50% (над него се сигнализира като annex_over_cap) — доста "
        "под законовия таван, затова е ранен сигнал, не твърдение за нарушение.",
        "law_ref": "чл. 116, ал. 2 ЗОП (законов таван на натрупаното увеличение: 50% от стойността на "
        "основния договор; тук се сигнализира много по-рано, при +10%).",
        "documents": [
            "допълнителни споразумения (анекси) към договора",
            "мотиви/обосновка за всяко изменение",
            "обявления за изменение на договора",
            "първоначалната документация с предвидените опции",
        ],
    },
    {
        "rule": "overspend_vs_plan",
        "tier": "signal",
        "detects": "Разходът по капиталов обект с одобрен план надвишава годишния план с повече от "
        "допустимото.",
        "threshold": "Над 2% и над 10,000 € над плана (обекти без никакъв план, или над общата им "
        "прогнозна стойност, се сигнализират отделно като unplanned_spending).",
        "law_ref": "чл. 124, ал. 2 ЗПФ (промените по общинския бюджет се одобряват от общинския "
        "съвет); чл. 125 ЗПФ (компенсирани промени).",
        "documents": [
            "решение на общинския съвет за промяна на бюджета (чл. 124 ЗПФ)",
            "заповеди на кмета за компенсирани промени (чл. 125 ЗПФ)",
            "фактури и платежни нареждания по обекта",
            "актуализиран разчет за капиталовите разходи",
        ],
    },
    {
        "rule": "plan_jump",
        "tier": "signal",
        "detects": "Планът за капиталов обект скача рязко спрямо предходния месец, или нов обект се "
        "появява „посред година“ с голям начален план.",
        "threshold": "Над 50% и над 100,000 € ръст спрямо предходния месец в същата година; нов "
        "обект с начален план над 250,000 €, който липсва в първия наличен отчет за годината. "
        "Декември не се сравнява със следващата година — тя има нов годишен бюджет.",
        "law_ref": "чл. 124, ал. 2 ЗПФ (промените по общинския бюджет се одобряват от общинския "
        "съвет); чл. 22, ал. 2 ЗМСМА (разгласяване на решенията в 7-дневен срок).",
        "documents": [
            "решение на общинския съвет за актуализация на бюджета",
            "докладна записка/мотиви към промяната",
            "разчет за капиталовите разходи преди и след промяната",
        ],
    },
    {
        "rule": "unmatched_spending",
        "tier": "signal",
        "detects": "Разход по капиталов обект над прага за пряко възлагане, за който не е открит "
        "съответстващ публикуван договор.",
        "threshold": "Разход от поне ≈25,565 € (50,000 лв., чл. 20, ал. 4, т. 3 ЗОП), без автоматично "
        "намерено съответствие в ЦАИС ЕОП/СИГМА.",
        "law_ref": "чл. 20, ал. 4, т. 3 ЗОП (праг за директно възлагане на доставки/услуги: 50,000 "
        "лв.).",
        "documents": [
            "договор(и) за изпълнение на обекта",
            "документ за избора на изпълнител (процедура или пряко възлагане)",
            "фактури и приемо-предавателни протоколи",
        ],
    },
    {
        "rule": "missing_annual_report",
        "tier": "signal",
        "detects": "За приключила година няма публикуван годишен отчет за изпълнението на бюджета. "
        "Задължен е кметът, като първостепенен разпоредител с бюджет на общината; общинската "
        "наредба за бюджета (Наредба № 12, чл. 37, ал. 1) изисква от него само да информира "
        "местната общност поне два пъти годишно на срещи и пресконференции — публикуването в "
        "интернет е отделно задължение по ЗПФ и ЗДОИ.",
        "threshold": "Повече от 3 месеца след края на годината (краен срок 31 март следващата) все "
        "още няма отчет.",
        "law_ref": "чл. 11, ал. 3 ЗПФ (кметът е първостепенен разпоредител с бюджет); чл. 133, ал. 1 "
        "и ал. 4 ЗПФ; чл. 140, ал. 5 и ал. 6 ЗПФ (годишният отчет се приема до 30 септември и се "
        "публикува на интернет страницата); чл. 173 ЗПФ (глоба).",
        "documents": [
            "отчет за касовото изпълнение на бюджета към 31.12. за годината",
            "годишен отчет за изпълнението на бюджета",
            "решение на общинския съвет за приемане на годишния отчет",
            "отчет за сметките за средства от Европейския съюз за годината",
        ],
    },
    {
        "rule": "eu_funded_irregularity",
        "tier": "signal",
        "detects": "Договор, финансиран изцяло или частично с европейски средства, за който вече има "
        "поне един друг сигнал на сайта.",
        "threshold": "Прилага се само ако договорът вече носи друг сигнал (пряко, или като член на "
        "група за разделяне на поръчка) — работи последна, върху резултата от всички други правила.",
        "law_ref": "чл. 248а НК (не е сред текстовете в docs/law, цитиран по задание); докладване на "
        "нередности пред управляващия орган на програмата и пред OLAF.",
        "documents": [
            "договор за безвъзмездна финансова помощ",
            "доклади от проверки на управляващия орган",
            "междинни и окончателни отчети по проекта",
            "документите, посочени в другите сигнали за договора",
        ],
    },
    # -- Companies: Trade Register <-> declarations of interest ------------
    {
        "rule": "related_party",
        "tier": "signal",
        "detects": "Текущ управител, съдружник, едноличен собственик или член на съвета на изпълнител "
        "носи същото (три имена) с общински съветник/кмет, или е посочен в декларацията за интереси "
        "на такова лице.",
        "threshold": "Съвпадение по пълно име (≥ 3 думи) или по декларирана връзка (ЕИК или "
        "наименование). Висока тежест при декларирана връзка или договори за ≥ 100,000 €, иначе "
        "средна.",
        "law_ref": "чл. 54, ал. 1, т. 7 и ал. 2 ЗОП (конфликт на интереси, който не може да бъде "
        "отстранен); чл. 37, ал. 1 ЗМСМА (общинският съветник не участва в решения по свои "
        "имуществени интереси).",
        "documents": [
            "актуална справка от Търговския регистър за фирмата",
            "декларацията за интереси на съответното лице от Регистъра на декларациите",
            "протоколи от заседания/решения, свързани с договорите на тази фирма",
        ],
    },
    {
        "rule": "person_concentration",
        "tier": "signal",
        "detects": "Едно и също лице (управител/съдружник/собственик/член на съвета) стои зад 2 или "
        "повече различни фирми-изпълнители на общината.",
        "threshold": "Фирмите заедно имат ≥ 3 договора или ≥ 200,000 € обща стойност с общината.",
        "law_ref": "чл. 2, ал. 1, т. 1 и 2 ЗОП (равнопоставеност и свободна конкуренция).",
        "documents": [
            "актуални справки от Търговския регистър за всяка от фирмите",
            "списък на всички договори на общината с тези фирми",
            "протоколите от съответните процедури по избор на изпълнител",
        ],
    },
    {
        "rule": "young_company",
        "tier": "signal",
        "detects": "Фирма е спечелила договор с общината малко след учредяването си в Търговския "
        "регистър.",
        "threshold": "Договор на стойност ≥ 50,000 € в рамките на 12 месеца след регистрацията; висока "
        "тежест при ≥ 200,000 € или в рамките на 6 месеца.",
        "law_ref": "чл. 2, ал. 1, т. 1 и 2 ЗОП (равнопоставеност и свободна конкуренция).",
        "documents": [
            "удостоверение за актуално състояние от Търговския регистър",
            "документация на поръчката с критериите за подбор (опит, капацитет)",
            "декларация за икономическо и финансово състояние на участника",
        ],
    },
    {
        "rule": "company_status",
        "tier": "signal",
        "detects": "Изпълнител е в ликвидация/несъстоятелност/заличен, докато държи скорошен договор с "
        "общината, или не е обявил годишен финансов отчет (ГФО) от години, а е получил значителни "
        "плащания.",
        "threshold": "Договор, подписан през последните 3 години, при статус ликвидация/"
        "несъстоятелност/заличена фирма; или ГФО, по-стар от (последна година на договор - 2), при "
        "договори за ≥ 100,000 €.",
        "law_ref": "чл. 2, ал. 1 ЗОП (принципи); чл. 55, ал. 1, т. 1 ЗОП (възложителят може да отстрани "
        "участник в несъстоятелност или ликвидация); чл. 38, ал. 1, т. 1 Закон за счетоводството "
        "(търговците обявяват ГФО в търговския регистър до 30 септември на следващата година; "
        "едноличните търговци без задължителен одит са освободени, а фирма без дейност подава "
        "декларация — чл. 38, ал. 9).",
        "documents": [
            "удостоверение за актуално състояние от Търговския регистър",
            "влязло в сила решение на съда за несъстоятелност/ликвидация (ако е приложимо)",
            "обявените годишни финансови отчети (ГФО) на фирмата в Търговския регистър",
            "приемо-предавателни протоколи по договора/договорите",
        ],
    },
    {
        "rule": "activity_mismatch",
        "tier": "signal",
        "detects": "Декларираната в Търговския регистър основна дейност (НКИД) на изпълнителя изглежда "
        "несъвместима с предмета на поръчката (CPV) — напр. хотелиерска фирма печели поръчка за "
        "строителство.",
        "threshold": "Договор ≥ 50,000 €, при изрично заложена като несъвместима двойка НКИД/CPV (не "
        "всяка комбинация, която просто липсва от таблицата, се сигнализира).",
        "law_ref": "чл. 2, ал. 1, т. 3 ЗОП (пропорционалност).",
        "documents": [
            "актуална справка от Търговския регистър за предмета на дейност на фирмата",
            "документация на поръчката с критериите за подбор (опит, технически възможности)",
            "декларация за подизпълнители (ако е приложимо)",
        ],
    },
    # -- Opacity: lawful as far as the data shows, but unverifiable --------
    {
        "rule": "price_unverifiable",
        "tier": "opacity",
        "detects": "За доставка или строителство над 20,000 € няма публикувано количество, "
        "съдържателно техническо описание, нито единични цени — само заглавие и обща сума.",
        "threshold": "Стойност над 20,000 €; над 100,000 € — средна тежест.",
        "law_ref": "чл. 48, ал. 1, т. 1 ЗОП (техническите спецификации позволяват точно определяне на "
        "параметрите на предмета на поръчката); чл. 36, ал. 1, т. 12 ЗОП (публикуване на договорите с "
        "приложенията към тях).",
        "documents": [
            "техническа спецификация",
            "ценово предложение на изпълнителя с единични цени",
            "фактури с количества и единични цени",
            "приемо-предавателни протоколи",
        ],
    },
    {
        "rule": "missing_quantity",
        "tier": "opacity",
        "detects": "За доставка с публикувано описание (или за капиталов обект) не е посочено "
        "количество, обем или брой — стойността не може да бъде проверена „на единица“.",
        "threshold": "Над 20,000 € стойност/разход, без разпознато количество в заглавието или "
        "описанието (освен при рамкови договори или доставки по заявка).",
        "law_ref": "чл. 2, ал. 2 ЗОП; Приложение № 4, част В, т. 6 ЗОП (количество или стойност в "
        "обявлението за възлагане).",
        "documents": [
            "техническа спецификация",
            "ценово предложение на изпълнителя с единични цени",
            "фактури с количества и единични цени",
            "приемо-предавателни протоколи",
        ],
    },
    {
        "rule": "missing_value",
        "tier": "opacity",
        "detects": "Подписан договор, за който не е посочена стойност нито в лева, нито в евро.",
        "threshold": "Договорът има изпълнител и/или дата на подписване, но стойността липсва или е "
        "нула. В тази база засега 0 отворени случая.",
        "law_ref": "чл. 36, ал. 1, т. 12 ЗОП (задължително съдържание на информацията за сключения "
        "договор).",
        "documents": [
            "текста на самия договор, в частта за цената",
            "обявлението за възлагане на поръчката",
        ],
    },
    {
        "rule": "near_threshold",
        "tier": "opacity",
        "detects": "Прогнозната стойност на поръчка е определена съвсем малко под прага, над който се "
        "изисква по-строга процедура.",
        "threshold": "До 5% под прага по ЗОП чл. 20; по-висока тежест (сигнал), ако поръчката е част "
        "и от група за разделяне (splitting).",
        "law_ref": "чл. 20, ал. 2-3 ЗОП (стойностни прагове); чл. 21, ал. 14 ЗОП (методът за "
        "прогнозната стойност не се използва за прилагане на ред за по-ниски стойности).",
        "documents": [
            "обосновка на прогнозната стойност (пазарно проучване, получени оферти)",
            "годишен план-график на обществените поръчки",
            "документация на поръчката",
        ],
    },
    {
        "rule": "missing_monthly_report",
        "tier": "opacity",
        "detects": "За даден месец няма публикуван месечен отчет за касовото изпълнение на бюджета. "
        "Задължен е кметът, като първостепенен разпоредител с бюджет на общината; общинската "
        "наредба за бюджета (Наредба № 12, чл. 37, ал. 1) изисква от него само да информира "
        "местната общност поне два пъти годишно на срещи и пресконференции — публикуването в "
        "интернет е отделно задължение по ЗПФ и ЗДОИ.",
        "threshold": "Всеки месец без открит Б1/Б3 отчет в наличните данни (от 2019-01 насам).",
        "law_ref": "чл. 11, ал. 3 ЗПФ (кметът е първостепенен разпоредител с бюджет); чл. 133, ал. 1 "
        "и ал. 4 ЗПФ (ежемесечни отчети, публикувани на интернет страницата); чл. 15, ал. 1, т. 7 и "
        "чл. 15а, ал. 4 ЗДОИ (публикуване до 3 работни дни); чл. 173 ЗПФ (глоба).",
        "documents": [
            "месечен отчет за касовото изпълнение на бюджета (Б1) за съответния месец",
        ],
    },
]
for _item in METHODOLOGY_RULES:
    _item["tier_label"] = TIER_LABELS[_item["tier"]]
del _item


def _event_year(
    subject_type: str | None,
    details: dict[str, Any] | None,
    contract: ContractView | None,
) -> int | None:
    """The year the flagged *event* happened ("година на възникване"), as
    opposed to `created_at`/`first_seen_at` (when *we* detected it):

    - budget-object flags: the ledger period recorded in `details_json` --
      `period` (most rules), `to_period` (plan_jump, the jump's later side)
      or `first_period`/`from_period` (plan_jump's "new object" case) --
      whichever key that rule populated, "YYYY-MM" -> its year;
    - report flags (missing_monthly_report/missing_annual_report):
      `details_json.year` (annual) or `period`/`expected_period` (monthly);
    - everything else -- contract/procedure/contractor-group flags,
      including the `eu_funded_irregularity` meta-flag, which all carry a
      `procurement_id` -- the matched contract's own `year` (same value
      `contracts/index.html` groups by: `contract_date`, falling back to
      `published_at`). None when no contract resolves (e.g.
      `contractor_concentration`, which spans many contracts and has no
      single event date).
    """
    if subject_type == "budget_object" and isinstance(details, dict):
        period = (
            details.get("period")
            or details.get("to_period")
            or details.get("first_period")
            or details.get("from_period")
        )
        if period and len(str(period)) >= 4:
            try:
                return int(str(period)[:4])
            except ValueError:
                return None
        return None
    if subject_type == "report" and isinstance(details, dict):
        year = details.get("year")
        if year is not None:
            try:
                return int(year)
            except (TypeError, ValueError):
                pass
        period = details.get("period") or details.get("expected_period")
        if period and len(str(period)) >= 4:
            try:
                return int(str(period)[:4])
            except ValueError:
                return None
        return None
    if contract is not None:
        return contract.year
    return None


def _flag_view(
    flag: Any,
    contract_by_proc_id: dict[int, ContractView],
    sigma_proc_id_to_contract: dict[int, ContractView] | None = None,
) -> dict[str, Any]:
    # Defensive: later columns (subject_key/subject_type/subject_id/details_json/
    # law_ref/tier/explanation/documents_json/first_seen_at/last_seen_at) may or
    # may not exist yet on Flag -- never hard-depend on them (see _load_flags_safe).
    law_ref = getattr(flag, "law_ref", None)
    details = getattr(flag, "details_json", None)
    if isinstance(details, str):
        try:
            details = json.loads(details)
        except ValueError:
            details = None
    subject_type = getattr(flag, "subject_type", None)
    subject_id = getattr(flag, "subject_id", None)

    subject_href = None
    subject_label = None
    contract = None
    if flag.procurement_id is not None:
        contract = contract_by_proc_id.get(flag.procurement_id)
        if contract is None and sigma_proc_id_to_contract:
            # The flag's procurement_id points at a SIGMA row (e.g.
            # single_bidder) rather than the eop row contracts/<id>.html is
            # keyed on -- fall back to that SIGMA row's matched eop contract.
            contract = sigma_proc_id_to_contract.get(flag.procurement_id)
        if contract is not None:
            subject_href = f"../contracts/{contract.source_id}.html"
            subject_label = contract.title or contract.source_id
    if subject_href is None and subject_type == "contractor" and subject_id:
        subject_href = f"../contractors/{subject_id}.html"
        subject_label = subject_id
    if subject_href is None and subject_type == "person":
        # No dedicated "people" page yet -- link to the contractors list,
        # where the companies named in this flag's details can be found.
        subject_href = "../contractors/index.html"
        subject_label = (details or {}).get("name") if isinstance(details, dict) else None
    if subject_href is None and subject_type == "budget_object" and isinstance(details, dict):
        period = details.get("period") or details.get("to_period") or details.get("first_period")
        if period:
            subject_href = f"../budget/{period}.html"
            subject_label = details.get("object_name") or subject_id

    rule_label = RULE_LABELS.get(flag.rule, flag.rule)
    severity = flag.severity
    tier_raw = getattr(flag, "tier", None)
    t_key = tier_key(tier_raw, flag.rule, severity)

    explanation = getattr(flag, "explanation", None) or flag.message

    documents_raw = getattr(flag, "documents_json", None)
    if isinstance(documents_raw, str):
        try:
            documents_raw = json.loads(documents_raw)
        except ValueError:
            documents_raw = None
    documents = (
        list(documents_raw)
        if documents_raw
        else _DEFAULT_DOCUMENTS_BY_SUBJECT_TYPE.get(subject_type or "", _DEFAULT_DOCUMENTS_FALLBACK)
    )

    if subject_type == "budget_object" and isinstance(details, dict) and details.get("period"):
        subject_line = f"{subject_label or rule_label}, период {fmt_period(details.get('period'))}"
    elif contract is not None:
        subject_line = (
            f"договор № {contract.source_id} от {fmt_date(contract.contract_date)} "
            f"с {contract.contractor_name or '—'}"
        )
    else:
        subject_line = subject_label or rule_label

    first_seen_at = getattr(flag, "first_seen_at", None) or flag.created_at
    last_seen_at = getattr(flag, "last_seen_at", None) or flag.created_at

    sources_raw = getattr(flag, "sources_json", None)
    if isinstance(sources_raw, str):
        try:
            sources_raw = json.loads(sources_raw)
        except ValueError:
            sources_raw = None
    sources = _source_views(sources_raw)
    if subject_type == "budget_object" and isinstance(details, dict) and details.get("object_name"):
        # So the "Източници" box can show the object's own name in bold,
        # above the sheet/row line, for a citizen to Ctrl+F for -- the row
        # itself may be the *paragraph subtotal* a reader would otherwise
        # land on by scrolling, not the object's own row.
        obj_name = details["object_name"]
        for src in sources:
            if src.get("kind") == "budget_file" and src.get("row"):
                src["object_name"] = obj_name

    event_year = _event_year(subject_type, details, contract)

    return {
        "id": flag.id,
        "rule": flag.rule,
        "rule_label": rule_label,
        "severity": severity,
        "tier": tier_raw,
        "tier_key": t_key,
        "tier_label": TIER_LABELS[t_key],
        "tier_tooltip": TIER_TOOLTIPS[t_key],
        "message": flag.message,
        "explanation": explanation,
        "law_ref": law_ref,
        "documents": documents,
        "details": details,
        "detail_items": _detail_items(details),
        "subject_type": subject_type,
        "subject_href": subject_href,
        "subject_label": subject_label,
        "procurement_id": flag.procurement_id,
        "created_at": flag.created_at,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "event_year": event_year,
        "permalink_href": f"../flags/{flag.id}.html",
        "zdoi_template": _zdoi_template(rule_label, flag.message, subject_line, documents),
        "sources": sources,
        "sources_raw": sources_raw if isinstance(sources_raw, list) else [],
        "sources_has_bgn": any(fld["is_bgn"] for src in sources for fld in src["fields"]),
        "sources_how_to_steps": _sources_how_to_steps(sources, details) if sources else [],
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["eur"] = fmt_eur
    env.filters["num"] = fmt_num
    env.filters["pct"] = fmt_pct
    env.filters["bgdate"] = fmt_date
    env.filters["period"] = fmt_period
    env.filters["quarter"] = fmt_quarter
    env.filters["sevkey"] = severity_key
    env.filters["sevlabel"] = severity_label
    env.filters["sitepath"] = site_path
    env.filters["tierkey"] = tier_key
    env.filters["tierlabel"] = tier_label
    env.filters["tiertip"] = tier_tooltip
    env.globals["fmt_eur"] = fmt_eur
    env.globals["fmt_num"] = fmt_num
    env.globals["fmt_pct"] = fmt_pct
    env.globals["severity_order"] = SEVERITY_ORDER
    env.globals["severity_labels"] = SEVERITY_LABELS
    env.globals["tier_order"] = TIER_ORDER
    env.globals["tier_labels"] = TIER_LABELS
    env.globals["tier_tooltips"] = TIER_TOOLTIPS
    env.globals["repo_url"] = REPO_URL
    # Cache-busting tag for static/style.css and static/app.js: a hash of
    # their contents, so a deploy that changes them is picked up at once
    # instead of after the browser's cached copy expires.
    env.globals["asset_version"] = hashlib.sha256(
        b"".join((STATIC_DIR / name).read_bytes() for name in ("style.css", "app.js"))
    ).hexdigest()[:10]
    env.globals["rule_labels"] = RULE_LABELS
    env.globals["sources_bgn_note"] = SOURCES_BGN_NOTE
    env.globals["tr_stale_report_note"] = TR_STALE_REPORT_NOTE
    env.globals["tr_namesake_note"] = TR_NAMESAKE_NOTE
    env.globals["kais_url"] = geo.kais_url
    return env


def _write(env: Environment, template_name: str, out_path: Path, context: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    html = env.get_template(template_name).render(**context)
    out_path.write_text(html, encoding="utf-8")


def _export_contracts(contracts: list[ContractView], data_dir: Path) -> None:
    fieldnames = [
        "source_id", "title", "is_signed", "contractor_name", "contractor_eik",
        "procedure_type", "contract_date", "published_at", "year",
        "contract_value_eur", "estimated_value_eur", "bids_received",
        "sigma_source_id", "sigma_unp", "sigma_eu_funded", "cpv_code", "url",
    ]
    rows = []
    for c in contracts:
        rows.append(
            {
                "source_id": c.source_id,
                "title": c.title,
                "is_signed": c.is_signed,
                "contractor_name": c.contractor_name,
                "contractor_eik": c.contractor_eik,
                "procedure_type": c.procedure_type,
                "contract_date": iso(_local_date(c.contract_date)),
                "published_at": iso(c.published_at),
                "year": c.year,
                "contract_value_eur": c.contract_value_eur,
                "estimated_value_eur": c.estimated_value_eur,
                "bids_received": c.bids_received,
                "sigma_source_id": c.sigma_source_id,
                "sigma_unp": c.sigma_unp,
                "sigma_eu_funded": c.sigma_eu_funded,
                "cpv_code": c.cpv_code,
                "url": c.url,
            }
        )

    with (data_dir / "contracts.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    (data_dir / "contracts.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _export_budget(session: Session, data_dir: Path) -> None:
    rows = list(session.scalars(select(BudgetLineItem)))
    fieldnames = [
        "period", "unit", "function_code", "paragraph", "subparagraph",
        "object_code", "object_name", "row_type", "estimated_total",
        "spent_prior", "plan_current", "spent_period",
    ]
    with (data_dir / "budget_line_items.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "period": r.period,
                    "unit": r.unit,
                    "function_code": r.function_code,
                    "paragraph": r.paragraph,
                    "subparagraph": r.subparagraph,
                    "object_code": r.object_code,
                    "object_name": r.object_name,
                    "row_type": _row_type(r),
                    "estimated_total": _as_float(r.estimated_total),
                    "spent_prior": _as_float(r.spent_prior),
                    "plan_current": _as_float(r.plan_current),
                    "spent_period": _as_float(r.spent_period),
                }
            )


def _export_cash(session: Session, data_dir: Path) -> None:
    rows = list(session.scalars(select(CashExecutionLine)))
    fieldnames = ["period", "section", "paragraph", "name", "row_type", "actual_ytd"]
    with (data_dir / "cash_execution.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow(
                {
                    "period": r.period,
                    "section": r.section,
                    "paragraph": r.paragraph,
                    "name": r.name,
                    "row_type": (r.extra_json or {}).get("row_type", ""),
                    "actual_ytd": _as_float(r.actual_ytd),
                }
            )


def _export_flags(flags: list[dict[str, Any]], data_dir: Path) -> None:
    serializable = [
        {
            "id": f["id"],
            "rule": f["rule"],
            "rule_label": f["rule_label"],
            "severity": f["severity"],
            "tier": f["tier_key"],
            "tier_label": f["tier_label"],
            "message": f["message"],
            "explanation": f["explanation"],
            "law_ref": f["law_ref"],
            "documents": f["documents"],
            "details": f["details"],
            "event_year": f["event_year"],
            "sources": f["sources_raw"],
            "subject_type": f["subject_type"],
            "subject_href": f["subject_href"],
            "subject_label": f["subject_label"],
            "permalink": f["permalink_href"].removeprefix("../"),
            "created_at": iso(f["created_at"]),
            "first_seen_at": iso(f["first_seen_at"]),
            "last_seen_at": iso(f["last_seen_at"]),
        }
        for f in flags
    ]
    (data_dir / "flags.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def build_site(out_dir: Path, db_url: str | None = None) -> None:
    """Build the full static site into `out_dir` (wiped and recreated each
    run, so builds are idempotent)."""
    out_dir = Path(out_dir)
    if db_url is None:
        db_url = f"sqlite:///{DEFAULT_DB_PATH}"

    engine = create_engine(db_url)
    env = _make_env()
    current_year = dt.datetime.now(dt.UTC).year

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True)
    shutil.copytree(STATIC_DIR, out_dir / "static")

    with Session(engine) as session:
        all_flags_raw = _load_flags_safe(session)
        contracts, sigma_stats, _flags_by_proc_id, match_by_eop_id = _build_contracts(
            session, all_flags_raw
        )
        contractors = _build_contractors(contracts)
        all_companies = list(session.scalars(select(Company)))
        all_company_people = list(session.scalars(select(CompanyPerson)))
        _attach_company_views(contractors, all_companies, all_company_people, current_year)
        budget = _load_budget(session)
        budget_changes = _compute_period_changes(budget["periods"], budget["by_period"])
        cash = _load_cash(session)
        minfin = _load_minfin_indicators()

        contract_by_proc_id = {c.id: c for c in contracts}
        # A flag's `procurement_id` may point at a *SIGMA* row (e.g.
        # single_bidder, which needs SIGMA's bids_received) rather than the
        # `eop` row contracts/<id>.html is built from. Since eop<->SIGMA
        # matches are already resolved for the contracts pages, reuse that
        # mapping so such a flag still links to its (matched) contract page
        # instead of showing no subject link at all.
        sigma_proc_id_to_contract = {
            sigma_proc.id: contract_by_proc_id[eop_id]
            for eop_id, sigma_proc in match_by_eop_id.items()
            if eop_id in contract_by_proc_id
        }
        flags = [
            _flag_view(f, contract_by_proc_id, sigma_proc_id_to_contract) for f in all_flags_raw
        ]

        # Re-attach the rendered flag *views* (tier/explanation/documents/
        # permalink, not just the raw DB row) to the contracts that carry
        # them, so contract_detail.html can show the same rich flag info as
        # the flags index/permalink pages instead of a second representation.
        flags_by_proc_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for f in flags:
            if f["procurement_id"] is not None:
                flags_by_proc_id[f["procurement_id"]].append(f)
        for c in contracts:
            c.flags = flags_by_proc_id.get(c.id, [])

        # Budget-object flags (overspend_vs_plan, plan_jump, unmatched_spending,
        # missing_quantity scope B, ...) aren't tied to a procurement_id -- they
        # key on `details_json.period` (or `to_period` for plan_jump) instead,
        # so budget/<period>.html can show "flags for this period's objects".
        budget_flags_by_period: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in flags:
            if f["subject_type"] != "budget_object":
                continue
            details = f["details"] or {}
            period = details.get("period") or details.get("to_period") or details.get("first_period")
            if period:
                budget_flags_by_period[period].append(f)

        _export_contracts(contracts, data_dir)
        _export_budget(session, data_dir)
        _export_cash(session, data_dir)
        _export_flags(flags, data_dir)
        _export_companies(all_companies, data_dir)
        _export_company_people(all_company_people, {c.id: c for c in all_companies}, data_dir)

        # -- map: settlement aggregation (budget ledger + contracts + flags) --
        geo.ensure_pins_scaffold()
        map_data = geo.build_map_data(session, contracts, flags=flags, budget=budget)
        geo.export_settlements_csv(map_data, data_dir / "settlements.csv")
        (data_dir / "map.json").write_text(
            json.dumps(geo.export_map_geojson(map_data), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # -- map: exact locations ("Точни места") --
        cadastre_rows = list(session.scalars(select(CadastreParcel)))
        locations = geo.build_locations(contracts, flags, budget, cadastre_rows)
        features_by_id = {f["id"]: f for f in locations["features"]}

        for c in contracts:
            loc_id = locations["by_contract_id"].get(c.id)
            c.location = features_by_id.get(loc_id) if loc_id else None

        for f in flags:
            loc_id = None
            if f["subject_type"] == "budget_object":
                obj_name = (f["details"] or {}).get("object_name")
                if obj_name:
                    loc_id = locations["by_object_name"].get(obj_name)
            elif f["procurement_id"] is not None:
                located_contract = contract_by_proc_id.get(
                    f["procurement_id"]
                ) or sigma_proc_id_to_contract.get(f["procurement_id"])
                if located_contract is not None:
                    loc_id = locations["by_contract_id"].get(located_contract.id)
            f["location"] = features_by_id.get(loc_id) if loc_id else None

        for s in map_data["settlements"]:
            s["locations"] = [f for f in locations["features"] if f["settlement_key"] == s["key"]]

        (data_dir / "locations.geojson").write_text(
            json.dumps(geo.export_locations_geojson(locations), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (data_dir / "location_parcels.geojson").write_text(
            json.dumps(geo.export_location_parcels_geojson(locations), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    generated_at = dt.datetime.now(dt.UTC).replace(microsecond=0)
    generated_at_label = generated_at.strftime("%d.%m.%Y %H:%M") + " UTC"

    signed = [c for c in contracts if c.is_signed]
    open_procs = [c for c in contracts if not c.is_signed]
    by_year: dict[int, dict[str, float]] = defaultdict(lambda: {"count": 0, "total_eur": 0.0})
    for c in signed:
        if c.year is None:
            continue
        by_year[c.year]["count"] += 1
        by_year[c.year]["total_eur"] += c.contract_value_eur or 0.0
    year_rows = [
        {"year": y, "count": d["count"], "total_eur": d["total_eur"]}
        for y, d in sorted(by_year.items())
    ]
    year_chart_items = [{"label": str(r["year"]), "value": r["total_eur"]} for r in year_rows]

    latest_budget_period = budget["periods"][-1] if budget["periods"] else None
    latest_budget = budget["by_period"].get(latest_budget_period) if latest_budget_period else None
    latest_cash_period = cash["periods"][-1] if cash["periods"] else None
    latest_cash = cash["by_period"].get(latest_cash_period) if latest_cash_period else None
    severity_rank = {key: i for i, key in enumerate(SEVERITY_ORDER)}
    tier_rank = {key: i for i, key in enumerate(TIER_ORDER)}
    # Violations first, then signals, then opacity flags; within a tier the
    # most severe first, then the most recent. Flags from the same rules run
    # share a timestamp, so recency is only the final tie-break.
    flags_ranked = sorted(
        flags,
        key=lambda f: (
            tier_rank[f["tier_key"]],
            severity_rank[severity_key(f["severity"])],
            -f["created_at"].timestamp() if f["created_at"] else float("inf"),
            -(f["id"] or 0),
        ),
    )
    # The home page preview shows at most 5 rows: one per rule first (so it
    # reads as a cross-section, not five copies of the same check), then
    # fills any remaining slots in ranked order.
    latest_flags: list[dict[str, Any]] = []
    seen_rules: set[str] = set()
    for f in flags_ranked:
        if f["rule"] not in seen_rules:
            seen_rules.add(f["rule"])
            latest_flags.append(f)
    latest_flags = latest_flags[:5]
    for f in flags_ranked:
        if len(latest_flags) >= 5:
            break
        if f not in latest_flags:
            latest_flags.append(f)
    latest_flags.sort(key=flags_ranked.index)
    flag_severity_counts = {key: 0 for key in SEVERITY_ORDER}
    flag_tier_counts = {key: 0 for key in TIER_ORDER}
    for f in flags:
        flag_severity_counts[severity_key(f["severity"])] += 1
        flag_tier_counts[f["tier_key"]] += 1

    meta = {
        "generated_at": generated_at.isoformat(),
        "counts": {
            "contracts_eop_total": len(contracts),
            "contracts_signed": len(signed),
            "contracts_open": len(open_procs),
            "contractors": len(contractors),
            "companies": len(all_companies),
            "flags": len(flags),
            "budget_periods": budget["periods"],
            "cash_periods": cash["periods"],
        },
        "sigma_dedupe": sigma_stats,
        "locations": {
            "located_budget_objects": sum(
                1 for f in locations["features"] if f["kind"] == "budget_object"
            ),
            "total_budget_objects": locations["total_budget_objects"],
            "located_contracts": sum(1 for f in locations["features"] if f["kind"] == "contract"),
            "total_contracts": len(contracts),
        },
    }
    (data_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    base_ctx = {"generated_at_label": generated_at_label}

    # -- index --------------------------------------------------------
    _write(
        env, "index.html", out_dir / "index.html",
        {
            **base_ctx, "root": "", "active": "home",
            "contracts_count": len(contracts),
            "signed_count": len(signed),
            "open_count": len(open_procs),
            "total_signed_value": sum(c.contract_value_eur or 0 for c in signed),
            "year_rows": year_rows,
            "year_chart_items": year_chart_items,
            "latest_budget_period": latest_budget_period,
            "latest_budget": latest_budget,
            "latest_cash_period": latest_cash_period,
            "latest_cash": latest_cash,
            "latest_flags": latest_flags,
            "flags_total": len(flags),
            "flag_severity_counts": flag_severity_counts,
            "flag_tier_counts": flag_tier_counts,
            "first_year": year_rows[0]["year"] if year_rows else None,
            "last_year": year_rows[-1]["year"] if year_rows else None,
            "sigma_stats": sigma_stats,
        },
    )

    # -- contracts ------------------------------------------------------
    _write(
        env, "contracts_index.html", out_dir / "contracts" / "index.html",
        {
            **base_ctx, "root": "../", "active": "contracts",
            "contracts": contracts,
            "year_rows": year_rows,
            "year_chart_items": year_chart_items,
            "sigma_stats": sigma_stats,
        },
    )
    for c in contracts:
        _write(
            env, "contract_detail.html", out_dir / "contracts" / f"{c.source_id}.html",
            {**base_ctx, "root": "../", "active": "contracts", "c": c},
        )

    # -- contractors ------------------------------------------------------
    _write(
        env, "contractors_index.html", out_dir / "contractors" / "index.html",
        {**base_ctx, "root": "../", "active": "contractors", "contractors": contractors},
    )
    for co in contractors:
        _write(
            env, "contractor_detail.html", out_dir / "contractors" / f"{co.slug}.html",
            {**base_ctx, "root": "../", "active": "contractors", "co": co},
        )

    # -- budget ------------------------------------------------------
    _write(
        env, "budget_index.html", out_dir / "budget" / "index.html",
        {
            **base_ctx, "root": "../", "active": "budget",
            "periods": budget["periods"],
            "latest_period": latest_budget_period,
            "latest": latest_budget,
            "changes": budget_changes.get(latest_budget_period, {}) if latest_budget_period else {},
            "other_units_count": budget["other_units_count"],
            "function_chart_items": _function_chart_items(latest_budget),
            "budget_flags": (
                budget_flags_by_period.get(latest_budget_period, []) if latest_budget_period else []
            ),
        },
    )
    for period in budget["periods"]:
        _write(
            env, "budget_period.html", out_dir / "budget" / f"{period}.html",
            {
                **base_ctx, "root": "../", "active": "budget",
                "period": period,
                "periods": budget["periods"],
                "data": budget["by_period"][period],
                "changes": budget_changes.get(period, {}),
                "function_chart_items": _function_chart_items(budget["by_period"][period]),
                "budget_flags": budget_flags_by_period.get(period, []),
            },
        )

    # -- cash ------------------------------------------------------
    cash_chart_items = []
    if latest_cash:
        top = latest_cash["expense_paragraphs"][:12]
        cash_chart_items = [
            {"label": r["name"], "value": r["actual_ytd"]} for r in top
        ]

    _write(
        env, "cash_index.html", out_dir / "cash" / "index.html",
        {
            **base_ctx, "root": "../", "active": "cash",
            "periods": cash["periods"],
            "latest_period": latest_cash_period,
            "latest": latest_cash,
            "minfin": minfin,
            "cash_chart_items": cash_chart_items,
        },
    )

    # -- flags ------------------------------------------------------
    _write(
        env, "flags_index.html", out_dir / "flags" / "index.html",
        {
            **base_ctx, "root": "../", "active": "flags", "flags": flags,
            "flag_severity_counts": flag_severity_counts,
            "flag_tier_counts": flag_tier_counts,
        },
    )
    for f in flags:
        _write(
            env, "flag_detail.html", out_dir / "flags" / f"{f['id']}.html",
            {**base_ctx, "root": "../", "active": "flags", "f": f},
        )

    # -- data ------------------------------------------------------
    _write(
        env, "data_index.html", out_dir / "data" / "index.html",
        {**base_ctx, "root": "../", "active": "data", "meta": meta},
    )

    # -- methodology ------------------------------------------------------
    _write(
        env, "methodology.html", out_dir / "methodology.html",
        {
            **base_ctx, "root": "", "active": "methodology", "sigma_stats": sigma_stats,
            "methodology_rules": METHODOLOGY_RULES,
        },
    )

    # -- map / settlements ------------------------------------------------
    settlements_with_data = sum(
        1 for s in map_data["settlements"] if s["all_years"]["objects_count"] > 0
    )
    total_spent = (
        sum(s["all_years"]["spent"] for s in map_data["settlements"])
        + map_data["unassigned"]["all_years"]["spent"]
    )
    total_objects = (
        sum(s["all_years"]["objects_count"] for s in map_data["settlements"])
        + map_data["unassigned"]["all_years"]["objects_count"]
    )
    flags_mapped = sum(s["flags_count"] for s in map_data["settlements"])
    last_years = sorted(map_data["years_available"])[-3:]

    _write(
        env, "map.html", out_dir / "map.html",
        {
            **base_ctx, "root": "", "active": "map",
            "settlements": map_data["settlements"],
            "unassigned": map_data["unassigned"],
            "years_available": map_data["years_available"],
            "notes": map_data["notes"],
            "settlements_with_data": settlements_with_data,
            "total_spent": total_spent,
            "total_objects": total_objects,
            "flags_mapped": flags_mapped,
            "flags_total": len(flags),
        },
    )
    _write(
        env, "settlements_index.html", out_dir / "settlements" / "index.html",
        {
            **base_ctx, "root": "../", "active": "map",
            "settlements": map_data["settlements"],
            "unassigned": map_data["unassigned"],
            "last_years": last_years,
        },
    )
    for s in map_data["settlements"]:
        _write(
            env, "settlement_detail.html", out_dir / "settlements" / f"{s['key']}.html",
            {**base_ctx, "root": "../", "active": "map", "s": s, "notes": map_data["notes"]},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Nessebar Budget Monitor static site.")
    parser.add_argument("--out", default="site", help="Output directory (default: ./site)")
    parser.add_argument("--db-url", default=None, help="SQLAlchemy DB URL (default: project data/nessebar.db)")
    args = parser.parse_args()
    build_site(Path(args.out), db_url=args.db_url)
    print(f"Built site into {Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
