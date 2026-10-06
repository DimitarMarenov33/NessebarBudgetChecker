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

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import create_engine, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session

from nessebar_budget.db.budget_models import BudgetLineItem, CashExecutionLine
from nessebar_budget.db.models import Flag, Procurement

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "nessebar.db"
MINFIN_QUARTERLY_PATH = PROJECT_ROOT / "data" / "minfin" / "quarterly_Q2_2026.xlsx"

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


def _truncate(label: str | None, max_len: int = 30) -> str:
    if not label:
        return ""
    label = label.strip()
    if len(label) <= max_len:
        return label
    return label[: max_len - 1].rstrip() + "…"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def fmt_eur(value: Any) -> str:
    """Thin-space-grouped EUR amount, no decimals (e.g. '220 000 €')."""
    f = _as_float(value)
    if f is None:
        return "—"
    n = round(f)
    return f"{n:,}".replace(",", " ") + " €"


def fmt_num(value: Any) -> str:
    """Thin-space-grouped plain integer, no currency."""
    f = _as_float(value)
    if f is None:
        return "—"
    n = round(f)
    return f"{n:,}".replace(",", " ")


def fmt_pct(value: Any, decimals: int = 1) -> str:
    """`value` is a fraction (0.784 -> '78.4%')."""
    f = _as_float(value)
    if f is None:
        return "—"
    return f"{f * 100:.{decimals}f}%".replace(".", ",")


def fmt_date(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return value
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
) -> tuple[list[ContractView], dict[str, int], dict[int, list[Any]]]:
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
            year = e.contract_date.year
        elif e.published_at is not None:
            year = e.published_at.year

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
    return contracts, stats, flags_by_proc_id


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
        {"label": _truncate(f["name"], 26), "value": f["plan_current"], "value2": f["spent_period"]}
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


RULE_LABELS = {
    "late_publication": "Късно публикуване",
    "overspend_vs_plan": "Разход над плана",
    "plan_jump": "Рязко увеличение на плана",
    "single_bidder": "Един участник",
    "contractor_concentration": "Концентрация при изпълнител",
    "unmatched_spending": "Разход без открит договор",
    "annex_growth": "Нарастване чрез анекси",
    "missing_value": "Липсваща стойност",
}


def _flag_view(flag: Any, contract_by_proc_id: dict[int, ContractView]) -> dict[str, Any]:
    subject_href = None
    subject_label = None
    if flag.procurement_id is not None:
        contract = contract_by_proc_id.get(flag.procurement_id)
        if contract is not None:
            subject_href = f"../contracts/{contract.source_id}.html"
            subject_label = contract.title or contract.source_id

    # Defensive: later columns (subject_key/subject_type/subject_id/details_json/
    # law_ref) may or may not exist yet on Flag -- never hard-depend on them.
    law_ref = getattr(flag, "law_ref", None)
    details_json = getattr(flag, "details_json", None)
    subject_type = getattr(flag, "subject_type", None)
    subject_id = getattr(flag, "subject_id", None)
    if subject_href is None and subject_type == "contractor" and subject_id:
        subject_href = f"../contractors/{subject_id}.html"
        subject_label = subject_id

    return {
        "id": flag.id,
        "rule": flag.rule,
        "rule_label": RULE_LABELS.get(flag.rule, flag.rule),
        "severity": flag.severity,
        "message": flag.message,
        "created_at": flag.created_at,
        "law_ref": law_ref,
        "details": details_json,
        "subject_href": subject_href,
        "subject_label": subject_label,
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
    env.globals["fmt_eur"] = fmt_eur
    env.globals["fmt_num"] = fmt_num
    env.globals["fmt_pct"] = fmt_pct
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
                "contract_date": iso(c.contract_date),
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
    serializable = []
    for f in flags:
        serializable.append({**f, "created_at": iso(f["created_at"])})
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

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    data_dir = out_dir / "data"
    data_dir.mkdir(parents=True)
    shutil.copytree(STATIC_DIR, out_dir / "static")

    with Session(engine) as session:
        all_flags_raw = _load_flags_safe(session)
        contracts, sigma_stats, _flags_by_proc_id = _build_contracts(session, all_flags_raw)
        contractors = _build_contractors(contracts)
        budget = _load_budget(session)
        budget_changes = _compute_period_changes(budget["periods"], budget["by_period"])
        cash = _load_cash(session)
        minfin = _load_minfin_indicators()

        contract_by_proc_id = {c.id: c for c in contracts}
        flags = [_flag_view(f, contract_by_proc_id) for f in all_flags_raw]

        _export_contracts(contracts, data_dir)
        _export_budget(session, data_dir)
        _export_cash(session, data_dir)
        _export_flags(flags, data_dir)

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
    latest_flags = sorted(flags, key=lambda f: f["created_at"] or _NAIVE_MIN, reverse=True)[:8]

    meta = {
        "generated_at": generated_at.isoformat(),
        "counts": {
            "contracts_eop_total": len(contracts),
            "contracts_signed": len(signed),
            "contracts_open": len(open_procs),
            "contractors": len(contractors),
            "flags": len(flags),
            "budget_periods": budget["periods"],
            "cash_periods": cash["periods"],
        },
        "sigma_dedupe": sigma_stats,
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
            },
        )

    # -- cash ------------------------------------------------------
    cash_chart_items = []
    if latest_cash:
        top = latest_cash["expense_paragraphs"][:12]
        cash_chart_items = [
            {"label": _truncate(r["name"], 26), "value": r["actual_ytd"]} for r in top
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
        {**base_ctx, "root": "../", "active": "flags", "flags": flags},
    )

    # -- data ------------------------------------------------------
    _write(
        env, "data_index.html", out_dir / "data" / "index.html",
        {**base_ctx, "root": "../", "active": "data", "meta": meta},
    )

    # -- methodology ------------------------------------------------------
    _write(
        env, "methodology.html", out_dir / "methodology.html",
        {**base_ctx, "root": "", "active": "methodology", "sigma_stats": sigma_stats},
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
