"""Upsert helpers for budget-report records and their parsed line items,
plus `parse_budget_reports` -- the `parse-budget` logic shared by the CLI
command and the weekly pipeline.

Kept separate from `db.repo` (which owns `Procurement` upserts) to avoid
touching a file another workstream is actively editing.

Period handling (see docs/sources/BUDGET_FORMS.md §6): the scraper records
the month a file was *published under* on nesebar.bg; `parse_budget_reports`
replaces it with the period the file *covers*, read from the file's own
header, keeps the scraper's guess as `parsed_json["heading_period"]`, and
parses only one file per (family, period) -- family "cash" = B1/B3,
"capital" = capital_xlsx -- marking the rest as skipped duplicates.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from nessebar_budget.db.budget_models import BudgetLineItem, CashExecutionLine
from nessebar_budget.db.models import BudgetReport
from nessebar_budget.parsers.budget_b1 import detect_period as detect_cash_period
from nessebar_budget.parsers.budget_b1 import parse_budget_b1
from nessebar_budget.parsers.budget_capital import detect_period as detect_capital_period
from nessebar_budget.parsers.budget_capital import parse_budget_capital
from nessebar_budget.scrapers.nesebar_site import classify_filename

logger = logging.getLogger(__name__)

#: Monthly (B1) and quarterly (B3) cash-execution reports: one "cash" family.
CASH_KINDS: tuple[str, ...] = ("B1", "B3")
CAPITAL_KIND = "capital_xlsx"
#: BudgetReport kinds `parse_budget_reports` parses.
PARSEABLE_KINDS: tuple[str, ...] = (*CASH_KINDS, CAPITAL_KIND)

FAMILY_CASH = "cash"
FAMILY_CAPITAL = "capital"

#: `parsed_json["period_source"]` / `["kind_source"]` values.
SOURCE_CONTENT = "content"
SOURCE_SCRAPER = "scraper"

_COPY_SUFFIX_RE = re.compile(r"\(\d+\)\s*$")
_QUARTERLY_MARKERS = ("тримес", "3mese")
_MONTHLY_MARKERS = ("месечен", "mesechen", "otchet", "ot4et")
#: `upsert_budget_line_items`' natural key (besides report_id).
_LINE_ITEM_KEY = ("unit", "function_code", "paragraph", "subparagraph", "object_code", "object_name")
_CACHE_PERIOD_RE = re.compile(r"(\d{4})[/\\](\d{2})[/\\][^/\\]+$")


def upsert_budget_report(session: Session, record: dict[str, Any]) -> BudgetReport:
    """Insert or update a `BudgetReport` row, keyed on its (unique) `url`.

    Does not commit -- the caller controls the transaction boundary.
    """
    url = record["url"]
    existing = session.scalars(select(BudgetReport).where(BudgetReport.url == url)).one_or_none()

    if existing is None:
        report = BudgetReport(
            period=record.get("period"),
            kind=record.get("kind"),
            url=url,
            file_path=record.get("file_path"),
            fetched_at=record.get("fetched_at"),
            parsed_json=record.get("parsed_json"),
        )
        session.add(report)
        session.flush()
        return report

    for name in ("file_path", "fetched_at"):
        if record.get(name) is not None:
            setattr(existing, name, record[name])

    previous = dict(existing.parsed_json or {})
    period_locked = previous.get("period_source") == SOURCE_CONTENT
    kind_locked = previous.get("kind_source") == SOURCE_CONTENT
    if not (period_locked or kind_locked):
        for name in ("period", "kind", "parsed_json"):
            if record.get(name) is not None:
                setattr(existing, name, record[name])
        session.flush()
        return existing

    # `parse-budget` already replaced the scraper's guesses with what the
    # file itself says (its period, and/or kind "other"): keep those and
    # only refresh the scraper-side metadata, so a re-scrape doesn't move a
    # quarterly file back to the month it was published under.
    if record.get("period") is not None:
        if period_locked:
            previous["heading_period"] = record["period"]
        else:
            existing.period = record["period"]
    if record.get("kind") is not None and not kind_locked:
        existing.kind = record["kind"]
    if record.get("parsed_json") is not None:
        previous["scrape"] = record["parsed_json"]
    existing.parsed_json = previous
    session.flush()
    return existing


def upsert_budget_line_items(
    session: Session, report_id: int, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """Insert new `BudgetLineItem` rows / update existing ones.

    Keyed on (report_id, unit, function_code, paragraph, subparagraph,
    object_code, object_name) so subtotal rows that share a name under
    different paragraphs (e.g. 'Функция 01' under §51-00 and §52-00) stay distinct.

    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0

    index = {
        tuple(getattr(item, name) for name in _LINE_ITEM_KEY): item
        for item in session.scalars(
            select(BudgetLineItem).where(BudgetLineItem.report_id == report_id)
        )
    }

    for row in rows:
        unit = row.get("unit")
        object_code = row.get("object_code")
        object_name = row.get("object_name")
        key = tuple(row.get(name) for name in _LINE_ITEM_KEY)
        existing = index.get(key)

        values = {
            "period": row.get("period"),
            "unit": unit,
            "function_code": row.get("function_code"),
            "paragraph": row.get("paragraph"),
            "subparagraph": row.get("subparagraph"),
            "object_code": object_code,
            "object_name": object_name,
            "years": row.get("years"),
            "estimated_total": row.get("estimated_total"),
            "spent_prior": row.get("spent_prior"),
            "plan_current": row.get("plan_current"),
            "spent_period": row.get("spent_period"),
            "currency": row.get("currency"),
            "extra_json": row.get("extra_json"),
        }

        if existing is None:
            item = BudgetLineItem(report_id=report_id, **values)
            session.add(item)
            index[key] = item
            inserted += 1
        else:
            for name, value in values.items():
                setattr(existing, name, value)
            updated += 1

    return inserted, updated


def upsert_cash_execution_lines(
    session: Session, report_id: int, rows: list[dict[str, Any]]
) -> tuple[int, int]:
    """Insert new `CashExecutionLine` rows / update existing ones.

    Keyed on (report_id, section, paragraph), per the task spec.

    Returns (inserted_count, updated_count).
    """
    inserted = 0
    updated = 0

    index = {
        (item.section, item.paragraph): item
        for item in session.scalars(
            select(CashExecutionLine).where(CashExecutionLine.report_id == report_id)
        )
    }

    for row in rows:
        section = row.get("section")
        paragraph = row.get("paragraph")
        existing = index.get((section, paragraph))

        values = {
            "period": row.get("period"),
            "section": section,
            "paragraph": paragraph,
            "name": row.get("name"),
            "plan_annual": row.get("plan_annual"),
            "plan_adjusted": row.get("plan_adjusted"),
            "actual_ytd": row.get("actual_ytd"),
            "extra_json": row.get("extra_json"),
        }

        if existing is None:
            item = CashExecutionLine(report_id=report_id, **values)
            session.add(item)
            index[(section, paragraph)] = item
            inserted += 1
        else:
            for name, value in values.items():
                setattr(existing, name, value)
            updated += 1

    return inserted, updated


# ---------------------------------------------------------------------------
# parse-budget: period from content, one file per (family, period)
# ---------------------------------------------------------------------------


def report_family(kind: str | None) -> str | None:
    """"cash" for B1/B3, "capital" for capital_xlsx, else None."""
    if kind in CASH_KINDS:
        return FAMILY_CASH
    if kind == CAPITAL_KIND:
        return FAMILY_CAPITAL
    return None


@dataclass
class ReportCandidate:
    """One parseable report file, with the period it was assigned to."""

    report_id: int
    kind: str
    file_path: str
    period: str | None
    mtime: float = 0.0

    @property
    def filename(self) -> str:
        return unicodedata.normalize("NFC", Path(self.file_path).name)

    @property
    def family(self) -> str | None:
        return report_family(self.kind)


def _capital_name_rank(filename: str) -> int:
    """0 = monthly-named ("Месечен", "otchet", "m-otchet", ...), 1 = neutral,
    2 = quarterly-named ("Тримесечен", "3mese..."). Quarterly is checked
    first because "тримесечен" itself contains "месечен"."""
    name = unicodedata.normalize("NFC", filename).casefold()
    if any(marker in name for marker in _QUARTERLY_MARKERS):
        return 2
    if any(marker in name for marker in _MONTHLY_MARKERS):
        return 0
    return 1


def _is_reupload_copy(filename: str) -> bool:
    """True for a browser-style duplicate name: "... Несебър(1).xlsx"."""
    return bool(_COPY_SUFFIX_RE.search(filename.rsplit(".", 1)[0]))


def preference_key(candidate: ReportCandidate) -> tuple[Any, ...]:
    """Sort key -- lowest is the file to parse when several share a period.

    cash: the quarterly B3 over the monthly B1 (the B3 is the official
    quarterly report and is not always identical to the same month's B1 --
    see BUDGET_FORMS.md §6). capital: a monthly-named file over a
    neutral one over a quarterly-named one. Then, for both: an original
    name over a "(1)" re-upload copy, the newest file (mtime), the newest
    report row.
    """
    if candidate.family == FAMILY_CASH:
        primary = 0 if candidate.kind == "B3" else 1
    else:
        primary = _capital_name_rank(candidate.filename)
    return (
        primary,
        1 if _is_reupload_copy(candidate.filename) else 0,
        -candidate.mtime,
        -candidate.report_id,
    )


def _preference_reason(winner: ReportCandidate, loser: ReportCandidate) -> str:
    """Human-readable reason `winner` was preferred over `loser`."""
    wk, lk = preference_key(winner), preference_key(loser)
    if wk[0] != lk[0]:
        if winner.family == FAMILY_CASH:
            return "the quarterly B3 (official quarterly report) is preferred over the monthly B1"
        return "a monthly-named file is preferred over a quarterly-named or unmarked one"
    if wk[1] != lk[1]:
        return 'the original file is preferred over a "(1)" re-upload copy'
    if wk[2] != lk[2]:
        return "the newest file (modification time) is preferred"
    return "the newest report row is preferred"


def group_candidates(
    candidates: Iterable[ReportCandidate],
) -> dict[tuple[str, str], list[ReportCandidate]]:
    """Group by (family, period), each group sorted best-first by
    `preference_key`. A candidate with no period at all can't be compared
    with anything and gets a group of its own ("?<report id>")."""
    groups: dict[tuple[str, str], list[ReportCandidate]] = defaultdict(list)
    for cand in candidates:
        family = cand.family
        if family is None:
            continue
        groups[(family, cand.period or f"?{cand.report_id}")].append(cand)
    return {key: sorted(group, key=preference_key) for key, group in groups.items()}


@dataclass
class ParseSummary:
    files_parsed: int = 0
    line_items: int = 0
    cash_lines: int = 0
    mismatches: int = 0
    skipped_duplicates: int = 0
    reclassified_other: int = 0
    kinds_refined: int = 0
    periods_changed: int = 0
    rows_deleted: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)
    others: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_parsed": self.files_parsed,
            "line_items": self.line_items,
            "cash_lines": self.cash_lines,
            "mismatches": self.mismatches,
            "skipped_duplicates": self.skipped_duplicates,
            "reclassified_other": self.reclassified_other,
            "kinds_refined": self.kinds_refined,
            "periods_changed": self.periods_changed,
            "rows_deleted": self.rows_deleted,
            "failures": len(self.failures),
        }


def _heading_period(report: BudgetReport) -> str | None:
    """The scraper's period guess (the nesebar.bg heading month)."""
    parsed = report.parsed_json or {}
    if parsed.get("heading_period"):
        return parsed["heading_period"]
    if parsed.get("period_source") == SOURCE_CONTENT:
        # Period already replaced but the guess wasn't kept: the cache
        # folder (<year>/<month>/) is named after the heading month.
        m = _CACHE_PERIOD_RE.search(report.file_path or "")
        if m:
            return f"{m.group(1)}-{m.group(2)}"
    return report.period


def _refine_legacy_kinds(session: Session) -> int:
    """Rows catalogued before B3/IB3 got their own kinds: B1 -> B3 and
    IB1_<X> -> IB3_<X> where the filename says it's a quarterly file."""
    changed = 0
    query = select(BudgetReport).where(
        or_(BudgetReport.kind == "B1", BudgetReport.kind.like("IB1%"))
    )
    for report in session.scalars(query):
        name = Path(report.file_path or report.url or "").name
        new_kind = classify_filename(name)["kind"]
        is_b3 = report.kind == "B1" and new_kind == "B3"
        is_ib3 = (report.kind or "").startswith("IB1_") and new_kind.startswith("IB3_")
        if is_b3 or is_ib3:
            report.kind = new_kind
            changed += 1
    return changed


def _table_for(family: str) -> type[BudgetLineItem | CashExecutionLine]:
    return CashExecutionLine if family == FAMILY_CASH else BudgetLineItem


def _delete(session: Session, table: Any, *conditions: Any) -> int:
    result = session.execute(
        delete(table).where(*conditions), execution_options={"synchronize_session": "fetch"}
    )
    return result.rowcount or 0


def parse_budget_reports(
    session: Session, *, period: str | None = None, rebuild: bool = False
) -> ParseSummary:
    """Parse cached B1/B3/capital_xlsx report files into line items.

    1. Each file's period is read from its own header (`detect_period`);
       the scraper's heading-month guess is used only if that fails
       (`parsed_json["period_source"]` = "content" / "scraper"), and is kept
       as `parsed_json["heading_period"]`. `BudgetReport.period` and every
       parsed row get the detected period.
    2. Files are grouped by (family, period); only the best one per group
       (`preference_key`) is parsed, the others get
       `parsed_json["skipped"] = {"duplicate_of": <id>, "reason": ...}`. A
       capital_xlsx whose content isn't a capital ledger becomes kind
       "other" (reason in `parsed_json["other_reason"]`) and the next file
       in its group, if any, is tried.
    3. Before upserting, rows that no longer belong are deleted: rows of
       skipped/"other" reports, a parsed report's rows under its old period,
       rows at a parsed (family, period) from any other report, and rows of
       reports that are no longer a parseable kind. `rebuild=True` first
       deletes every line item and re-parses everything from the cache.

    `period` limits the work to groups whose period is `period` (plus the
    new period of any report previously stored under `period`). `url` and
    `file_path` of every report are left untouched. Does not commit.
    """
    summary = ParseSummary()
    summary.kinds_refined = _refine_legacy_kinds(session)

    if rebuild:
        summary.rows_deleted += _delete(session, BudgetLineItem, BudgetLineItem.id.isnot(None))
        summary.rows_deleted += _delete(
            session, CashExecutionLine, CashExecutionLine.id.isnot(None)
        )

    # Rows whose report isn't (any longer) a kind of their table's family.
    cash_ids = select(BudgetReport.id).where(BudgetReport.kind.in_(CASH_KINDS))
    capital_ids = select(BudgetReport.id).where(BudgetReport.kind == CAPITAL_KIND)
    summary.rows_deleted += _delete(
        session, CashExecutionLine, CashExecutionLine.report_id.not_in(cash_ids)
    )
    summary.rows_deleted += _delete(
        session, BudgetLineItem, BudgetLineItem.report_id.not_in(capital_ids)
    )

    reports = session.scalars(
        select(BudgetReport).where(BudgetReport.kind.in_(PARSEABLE_KINDS)).order_by(BudgetReport.id)
    ).all()

    by_id: dict[int, BudgetReport] = {}
    meta: dict[int, dict[str, Any]] = {}
    old_period: dict[int, str | None] = {}
    candidates: list[ReportCandidate] = []
    for report in reports:
        by_id[report.id] = report
        old_period[report.id] = report.period
        heading = _heading_period(report)
        path = Path(report.file_path) if report.file_path else None
        if path is None or not path.exists():
            summary.failures.append(
                (report.file_path or f"report #{report.id}", "file missing on disk")
            )
            continue
        detector = detect_cash_period if report.kind in CASH_KINDS else detect_capital_period
        detected = detector(path)
        meta[report.id] = {
            "period_source": SOURCE_CONTENT if detected else SOURCE_SCRAPER,
            "heading_period": heading,
            "detected_period": detected,
        }
        candidates.append(
            ReportCandidate(
                report_id=report.id,
                kind=report.kind or "",
                file_path=str(report.file_path),
                period=detected or heading,
                mtime=path.stat().st_mtime,
            )
        )

    groups = group_candidates(candidates)
    if period:
        scope = {period} | {
            c.period for c in candidates if old_period[c.report_id] == period and c.period
        }
        groups = {key: group for key, group in groups.items() if key[1] in scope}

    for (family, _group_key), ranked in sorted(groups.items()):
        table = _table_for(family)
        winner: ReportCandidate | None = None
        for cand in ranked:
            report = by_id[cand.report_id]
            base = dict(meta[cand.report_id])
            if cand.period != report.period:
                summary.periods_changed += 1
            report.period = cand.period

            if winner is not None:
                reason = _preference_reason(winner, cand)
                report.parsed_json = {
                    **base,
                    "skipped": {
                        "duplicate_of": winner.report_id,
                        "duplicate_file": winner.filename,
                        "reason": f"same {family} period {cand.period} as report "
                        f"#{winner.report_id}; {reason}",
                    },
                }
                summary.rows_deleted += _delete(session, table, table.report_id == cand.report_id)
                summary.skipped_duplicates += 1
                summary.duplicates.append(
                    {
                        "family": family,
                        "period": cand.period,
                        "skipped_id": cand.report_id,
                        "skipped_file": cand.filename,
                        "kept_id": winner.report_id,
                        "kept_file": winner.filename,
                    }
                )
                continue

            try:
                if family == FAMILY_CAPITAL:
                    result = parse_budget_capital(cand.file_path, period=cand.period)
                else:
                    result = parse_budget_b1(cand.file_path, period=cand.period)
            except Exception as exc:  # noqa: BLE001 -- keep ingesting the remaining files
                logger.warning("parse-budget: failed to parse %s: %s", cand.file_path, exc)
                summary.failures.append((cand.file_path, str(exc)))
                report.parsed_json = {**base, "error": str(exc)}
                continue

            if family == FAMILY_CAPITAL and result.get("unsupported"):
                report.kind = "other"
                report.parsed_json = {
                    **base,
                    "kind_source": SOURCE_CONTENT,
                    "previous_kind": CAPITAL_KIND,
                    "other_reason": "not a capital-expenditure ledger: " + result["unsupported"],
                }
                summary.rows_deleted += _delete(session, table, table.report_id == cand.report_id)
                summary.reclassified_other += 1
                summary.others.append({"report_id": cand.report_id, "file": cand.filename})
                continue

            winner = cand
            # This (family, period) now belongs to this report alone, and
            # this report's rows only to this period.
            summary.rows_deleted += _delete(
                session, table, table.period == cand.period, table.report_id != cand.report_id
            )
            summary.rows_deleted += _delete(
                session,
                table,
                table.report_id == cand.report_id,
                or_(table.period != cand.period, table.period.is_(None)),
            )
            if family == FAMILY_CAPITAL:
                inserted, updated = upsert_budget_line_items(session, cand.report_id, result["rows"])
                summary.line_items += inserted + updated
                summary.mismatches += len(result["mismatches"])
                report.parsed_json = {
                    **base,
                    "row_count": len(result["rows"]),
                    "mismatches": result["mismatches"],
                }
            else:
                inserted, updated = upsert_cash_execution_lines(
                    session, cand.report_id, result["rows"]
                )
                summary.cash_lines += inserted + updated
                report.parsed_json = {
                    **base,
                    "row_count": len(result["rows"]),
                    "unparsed_pages": result["skipped"],
                }
            summary.files_parsed += 1

    session.flush()
    return summary
