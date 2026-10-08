"""Weekly end-to-end pipeline: scrape -> parse -> analyze -> notify -> build site.

This is what GitHub Actions' `weekly.yml` workflow runs (via
`python -m nessebar_budget.cli pipeline weekly`, wired up by the CLI owner).
It is intentionally a thin orchestration layer: every step re-uses the same
scraper/parser/repo/rule functions that the CLI's individual commands call,
so behaviour matches running `scrape`, `parse-budget`, `analyze`, and
`notify-pending` by hand.

Design notes for `run_weekly`:

- Each step is a small `_step_*(settings) -> dict` function kept at module
  level (not nested) specifically so tests can monkeypatch them individually
  (e.g. `monkeypatch.setattr(pipeline, "_step_scrape_eop", fake)`) to assert
  ordering and failure isolation without touching the network or a real DB.
- `run_weekly` runs every step in order inside its own try/except: a failing
  step is recorded (with its error) but does not stop later steps from
  running. If *any* step failed, `run_weekly` raises `SystemExit(1)` after
  all steps have run; otherwise it returns the summary dict.
- The last step (`_step_build_site`) imports `nessebar_budget.web.build` lazily and
  swallows `ImportError` as a soft "skip" (not a failure) -- that module is
  owned by a different workstream and may not exist yet / may not be
  installed in every environment.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from nessebar_budget.config import Settings, get_settings
from nessebar_budget.db.budget_repo import (
    PARSEABLE_KINDS,
    parse_budget_reports,
    upsert_budget_report,
)
from nessebar_budget.db.models import Flag
from nessebar_budget.db.repo import (
    distinct_contractor_eiks,
    upsert_company,
    upsert_official,
    upsert_procurements,
)
from nessebar_budget.db.session import get_session, init_db
from nessebar_budget.scrapers.declarations import DeclarationsScraper
from nessebar_budget.scrapers.eop import EopScraper
from nessebar_budget.scrapers.nesebar_site import NesebarSiteScraper
from nessebar_budget.scrapers.registry import RegistryScraper
from nessebar_budget.scrapers.sigma import SigmaScraper

logger = logging.getLogger(__name__)

#: BudgetReport kinds that `parse-budget` knows how to parse.
_PARSEABLE_KINDS = list(PARSEABLE_KINDS)

#: Ordered (name, function) pairs `run_weekly` executes. Built as a module
#: constant of *names*, resolved to the current (possibly monkeypatched)
#: module-level function at call time inside `run_weekly` -- see its
#: docstring/comment below for why that matters for tests.
_STEP_NAMES: tuple[str, ...] = (
    "init_db",
    "scrape_eop",
    "scrape_sigma",
    "scrape_registry",
    "scrape_declarations",
    "scrape_nesebar_site",
    "parse_budget",
    "analyze",
    "notify_pending",
    "build_site",
)


def _previous_period(reference: dt.date | None = None) -> str:
    """Return the "YYYY-MM" period for the month before `reference` (default: today).

    Used as the `--since` floor for the `nesebar_site` scrape: budget-execution
    reports for a given month are typically published early in the *next*
    month, so a weekly run only needs to look back one period to pick up
    anything new (already-downloaded files are skipped via the scraper's own
    on-disk cache regardless).
    """
    today = reference or dt.datetime.now(dt.UTC).date()
    first_of_this_month = today.replace(day=1)
    last_day_prev_month = first_of_this_month - dt.timedelta(days=1)
    return f"{last_day_prev_month.year:04d}-{last_day_prev_month.month:02d}"


# ---------------------------------------------------------------------------
# Individual steps. Each takes the Settings instance (even if unused) so they
# share one signature, and returns a small JSON-able info dict.
# ---------------------------------------------------------------------------


def _step_init_db(settings: Settings) -> dict[str, Any]:
    init_db()
    return {}


def _step_scrape_eop(settings: Settings) -> dict[str, Any]:
    # Polite delay of 1s/request (not the scraper's own default) -- ~440 calls
    # is ~8 minutes, which is acceptable for a weekly CI run.
    scraper = EopScraper(delay=1.0)
    try:
        records = scraper.fetch()
    finally:
        scraper.close()

    with get_session() as session:
        inserted, updated = upsert_procurements(session, records)
        session.commit()

    return {"fetched": len(records), "inserted": inserted, "updated": updated}


def _step_scrape_sigma(settings: Settings) -> dict[str, Any]:
    scraper = SigmaScraper()
    try:
        records = scraper.fetch()
    finally:
        scraper.close()

    with get_session() as session:
        inserted, updated = upsert_procurements(session, records)
        session.commit()

    return {"fetched": len(records), "inserted": inserted, "updated": updated}


def _step_scrape_registry(settings: Settings) -> dict[str, Any]:
    """Fetch Trade Register deeds (`scrapers.registry`) for every distinct
    `contractor_eik` in `procurements`, and upsert them into
    `companies`/`company_people`. A persistently-failing ЕИК (e.g. one still
    429-rate-limited past its own retries) is skipped, not fatal to the
    step -- see `RegistryScraper`'s module docstring -- and listed in the
    returned info dict instead."""
    with get_session() as session:
        eiks = distinct_contractor_eiks(session)

    scraper = RegistryScraper(delay=2.0)
    try:
        records = scraper.fetch(eiks)
    finally:
        scraper.close()

    with get_session() as session:
        for record in records:
            upsert_company(session, record)
        session.commit()

    return {
        "eiks": len(eiks),
        "fetched": len(records),
        "failed": len(scraper.failures),
        "failures": [eik for eik, _ in scraper.failures],
    }


def _step_scrape_declarations(settings: Settings) -> dict[str, Any]:
    """Fetch the municipal council's declarations registers and each
    official's declaration PDF (see `scrapers.declarations`), and upsert
    them keyed on (name_normalized, role, mandate)."""
    scraper = DeclarationsScraper()
    try:
        records = scraper.run(delay=2.0)
    finally:
        scraper.close()

    with get_session() as session:
        for record in records:
            upsert_official(session, record)
        session.commit()

    return {"fetched": len(records)}


def _step_scrape_nesebar_site(settings: Settings) -> dict[str, Any]:
    since = _previous_period()
    scraper = NesebarSiteScraper()
    try:
        # robots.txt Crawl-delay is 10s; be explicit rather than relying on
        # the scraper's own default.
        records = scraper.run(since=since, delay=10.0)
    finally:
        scraper.close()

    with get_session() as session:
        for record in records:
            upsert_budget_report(session, record)
        session.commit()

    return {"since": since, "fetched": len(records)}


def _step_parse_budget(settings: Settings) -> dict[str, Any]:
    """Parse cached `BudgetReport` files into line items -- the same
    `parse_budget_reports` that `cli.parse_budget_command` runs (period from
    each file's own header, one file per family and period)."""
    with get_session() as session:
        summary = parse_budget_reports(session)
        session.commit()
    return summary.as_dict()


def _step_analyze(settings: Settings) -> dict[str, Any]:
    from nessebar_budget.analysis.engine import run_full_analysis

    with get_session() as session:
        summary = run_full_analysis(session)
        session.commit()
    return {
        "flags_produced": summary.flags_produced,
        "new": summary.total_new(),
        "updated": summary.total_updated(),
        "resolved": summary.total_resolved(),
    }


def _step_notify_pending(settings: Settings) -> dict[str, Any]:
    """Send one grouped Telegram message for flags not yet notified.

    No-op (beyond a logged warning) when TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID
    aren't configured -- see `notify.telegram.send_flags_notification`."""
    from nessebar_budget.notify.telegram import send_flags_notification

    with get_session() as session:
        pending = session.scalars(select(Flag).where(Flag.notified_at.is_(None), Flag.resolved_at.is_(None))).all()
        if not pending:
            return {"pending": 0, "sent": False}
        message = asyncio.run(send_flags_notification(pending, dry_run=False))
        if message is None:
            return {"pending": len(pending), "sent": False}
        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        for flag in pending:
            flag.notified_at = now
        session.commit()
    return {"pending": len(pending), "sent": True}


def _step_build_site(settings: Settings) -> dict[str, Any]:
    try:
        from nessebar_budget.web.build import build_site
    except ImportError as exc:
        logger.info("pipeline build_site: nessebar_budget.web.build not available yet (%s); skipping.", exc)
        return {"skipped": True, "reason": str(exc)}

    build_site(Path("site"))
    return {"skipped": False, "out_dir": "site"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_weekly(settings: Settings | None = None) -> dict[str, Any]:
    """Run the full weekly pipeline (scrape -> parse -> analyze -> notify -> build site).

    Steps run in a fixed order; a failing step is recorded (with its
    exception) but does not prevent later steps from running. Returns a
    summary dict (per-step name/ok/elapsed_seconds/info-or-error, plus an
    overall `ok` flag and timestamps) when every step succeeded. If one or
    more steps failed, the summary is logged and `SystemExit(1)` is raised
    instead of returning, so this function is safe to call directly from a
    CLI command and get the right process exit code for free.
    """
    settings = settings or get_settings()

    # Resolved by *name* against this module's current globals on every call
    # (not captured at import time) so tests can monkeypatch
    # `nessebar_budget.pipeline._step_xxx` and have `run_weekly` pick up the
    # replacement.
    module_globals = globals()

    started_at = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    overall_start = time.monotonic()
    steps: list[dict[str, Any]] = []
    any_failed = False

    for name in _STEP_NAMES:
        fn: Callable[[Settings], dict[str, Any]] = module_globals[f"_step_{name}"]
        step_start = time.monotonic()
        entry: dict[str, Any] = {"name": name}
        try:
            entry["info"] = fn(settings)
            entry["ok"] = True
        except Exception as exc:
            logger.exception("pipeline weekly: step %r failed", name)
            entry["ok"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            any_failed = True
        entry["elapsed_seconds"] = round(time.monotonic() - step_start, 3)
        logger.info(
            "pipeline weekly: step %r %s in %.2fs",
            name,
            "ok" if entry["ok"] else "FAILED",
            entry["elapsed_seconds"],
        )
        steps.append(entry)

    summary = {
        "ok": not any_failed,
        "started_at": started_at.isoformat(),
        "finished_at": dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat(),
        "total_elapsed_seconds": round(time.monotonic() - overall_start, 3),
        "steps": steps,
    }
    logger.info("pipeline weekly: summary=%s", summary)

    if any_failed:
        raise SystemExit(1)

    return summary
