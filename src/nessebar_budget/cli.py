"""Command-line interface for Nessebar Budget Monitor."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from nessebar_budget.analysis.engine import run_full_analysis
from nessebar_budget.config import get_settings
from nessebar_budget.db.budget_repo import parse_budget_reports, upsert_budget_report
from nessebar_budget.db.models import Flag
from nessebar_budget.db.repo import (
    distinct_contractor_eiks,
    upsert_company,
    upsert_official,
    upsert_procurements,
)
from nessebar_budget.db.session import get_session, init_db
from nessebar_budget.notify.telegram import send_flags_notification
from nessebar_budget.scrapers.declarations import DeclarationsScraper
from nessebar_budget.scrapers.eop import EopScraper
from nessebar_budget.scrapers.minfin import MinfinScraper
from nessebar_budget.scrapers.nesebar_site import NesebarSiteScraper
from nessebar_budget.scrapers.registry import RegistryScraper
from nessebar_budget.scrapers.sigma import SigmaScraper

logger = logging.getLogger(__name__)

app = typer.Typer(help="Nessebar Budget Monitor: local civic budget-monitoring CLI.")
console = Console()

#: Sources with only a stub fetch() (not yet upsert-wired); see `scrape_command`
#: for the "eop"/"sigma"/"nesebar_site" sources, which have a full
#: fetch+upsert pipeline.
STUB_SCRAPERS = {
    "minfin": MinfinScraper,
}

ALL_SOURCES = ["eop", "sigma", "nesebar_site", "declarations", "registry", *STUB_SCRAPERS]


@app.command("init-db")
def init_db_command() -> None:
    """Create database tables if they don't exist yet."""
    init_db()
    console.print("[green]Database initialized.[/green]")


@app.command("scrape")
def scrape_command(
    source: str,
    limit: int = typer.Option(
        None,
        "--limit",
        help="Cap the number of paginated list pages fetched (eop), files downloaded "
        "(nesebar_site), officials processed (declarations), or ЕИКs looked up "
        "(registry); for testing.",
    ),
    since: str = typer.Option(
        None, "--since", help="nesebar_site only: only fetch reports for period >= YYYY-MM."
    ),
    delay: float = typer.Option(
        None, "--delay", help="nesebar_site/declarations/registry only: seconds between "
        "requests (default 10 for nesebar_site per robots.txt Crawl-delay, 2 for "
        "declarations, 2 for registry)."
    ),
) -> None:
    """Fetch records from SOURCE (one of: eop, sigma, nesebar_site, declarations,
    registry, minfin).

    `eop`, `sigma`, `nesebar_site`, `declarations`, and `registry` fetch live
    records and upsert them into the database, printing a summary. `minfin`
    is still an unimplemented stub.
    """
    if source not in ALL_SOURCES:
        console.print(f"[red]Unknown source {source!r}. Choices: {', '.join(ALL_SOURCES)}[/red]")
        raise typer.Exit(code=1)

    if source == "nesebar_site":
        scraper = NesebarSiteScraper()
        init_db()
        try:
            records = scraper.run(
                since=since, limit=limit, delay=delay if delay is not None else scraper.delay
            )
        finally:
            scraper.close()

        with get_session() as session:
            for record in records:
                upsert_budget_report(session, record)
            session.commit()

        console.print(f"[green]nesebar_site[/green]: fetched {len(records)} report file(s).")
        return

    if source == "declarations":
        if since is not None:
            console.print("[yellow]--since is ignored for source 'declarations'.[/yellow]")
        scraper = DeclarationsScraper()
        init_db()
        try:
            records = scraper.run(limit=limit, delay=delay if delay is not None else scraper.delay)
        finally:
            scraper.close()

        with get_session() as session:
            for record in records:
                upsert_official(session, record)
            session.commit()

        console.print(f"[green]declarations[/green]: fetched {len(records)} official(s).")
        return

    if source == "registry":
        if since is not None:
            console.print("[yellow]--since is ignored for source 'registry'.[/yellow]")
        init_db()
        with get_session() as session:
            eiks = distinct_contractor_eiks(session)
        if limit is not None:
            eiks = eiks[:limit]

        scraper = RegistryScraper(delay=delay if delay is not None else 2.0)
        try:
            records = scraper.fetch(eiks)
        finally:
            scraper.close()

        with get_session() as session:
            for record in records:
                upsert_company(session, record)
            session.commit()

        console.print(
            f"[green]registry[/green]: {len(eiks)} ЕИК(s), fetched {len(records)}, "
            f"failed {len(scraper.failures)}."
        )
        for eik, reason in scraper.failures:
            console.print(f"[red]FAILED[/red] {eik}: {reason}")
        return

    if source in STUB_SCRAPERS:
        if limit is not None or since is not None or delay is not None:
            console.print(f"[yellow]--limit/--since/--delay are ignored for source {source!r}.[/yellow]")
        try:
            records = STUB_SCRAPERS[source]().fetch()
            console.print(f"Fetched {len(records)} records from {source}.")
        except NotImplementedError:
            console.print(f"[yellow]scrape {source}: not implemented[/yellow]")
        return

    if since is not None or delay is not None:
        console.print(f"[yellow]--since/--delay are ignored for source {source!r}.[/yellow]")

    scraper: EopScraper | SigmaScraper
    if source == "eop":
        scraper = EopScraper(max_pages=limit)
    else:
        if limit is not None:
            console.print("[yellow]--limit is ignored for sigma (single CSV download).[/yellow]")
        scraper = SigmaScraper()

    init_db()
    try:
        records = scraper.fetch()
    finally:
        scraper.close()

    with get_session() as session:
        inserted, updated = upsert_procurements(session, records)
        session.commit()

    console.print(
        f"[green]{source}[/green]: fetched {len(records)} record(s), "
        f"inserted {inserted}, updated {updated}."
    )


@app.command("parse-budget")
def parse_budget_command(
    period: str = typer.Option(
        None,
        "--period",
        help="Only (re)parse reports covering this YYYY-MM period (as stated in the file itself).",
    ),
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help="Delete every parsed line item first and re-parse all cached files from scratch.",
    ),
) -> None:
    """Parse cached budget-report files (kinds `B1`, `B3` and `capital_xlsx`)
    into `CashExecutionLine`/`BudgetLineItem`, and print a summary.

    Each file's period is read from its own header (not the nesebar.bg
    heading it was published under) and stored on the report and its rows.
    Only one file per period is parsed for each family (cash: B1/B3;
    capital: capital_xlsx) -- the others are marked as skipped duplicates.
    Rows of skipped reports, and rows left under a report's old period, are
    deleted first, so re-running is idempotent.
    """
    init_db()

    with get_session() as session:
        summary = parse_budget_reports(session, period=period, rebuild=rebuild)
        session.commit()

    table = Table(title="parse-budget summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Files parsed", str(summary.files_parsed))
    table.add_row("Files failed", str(len(summary.failures)))
    table.add_row("Duplicates skipped (same family and period)", str(summary.skipped_duplicates))
    table.add_row("Reclassified as 'other' (not a capital ledger)", str(summary.reclassified_other))
    table.add_row("Kinds refined (B1 -> B3, IB1 -> IB3)", str(summary.kinds_refined))
    table.add_row("Report periods changed", str(summary.periods_changed))
    table.add_row("Stale line items deleted", str(summary.rows_deleted))
    table.add_row("Budget line items (capital ledger)", str(summary.line_items))
    table.add_row("Cash execution lines (B1/B3)", str(summary.cash_lines))
    table.add_row("Validation mismatches logged", str(summary.mismatches))
    console.print(table)

    for dup in summary.duplicates:
        console.print(
            f"[yellow]SKIPPED[/yellow] {dup['family']} {dup['period']}: {dup['skipped_file']} "
            f"(#{dup['skipped_id']}) -- kept {dup['kept_file']} (#{dup['kept_id']})"
        )
    for other in summary.others:
        console.print(f"[yellow]OTHER[/yellow] {other['file']} (#{other['report_id']})")
    for file_path, reason in summary.failures:
        console.print(f"[red]FAILED[/red] {file_path}: {reason}")


@app.command("analyze")
def analyze_command() -> None:
    """Run every anomaly rule (analysis.rules) over stored procurements and
    the capital budget ledger, and upsert the resulting flags.

    Flags are upserted on (rule, subject_key): a subject seen again is
    updated in place; a subject a rule no longer produces is marked
    resolved rather than deleted. Prints new/updated/resolved counts per
    rule.
    """
    init_db()
    with get_session() as session:
        summary = run_full_analysis(session)
        session.commit()

    table = Table(title="analyze summary")
    table.add_column("rule")
    table.add_column("tier(s)")
    table.add_column("open", justify="right")
    table.add_column("new", justify="right")
    table.add_column("updated", justify="right")
    table.add_column("resolved", justify="right")
    for rule_name in sorted(summary.counts):
        counts = summary.counts[rule_name]
        tiers = ", ".join(f"{t} {n}" for t, n in sorted(counts.tiers.items())) or "-"
        table.add_row(
            rule_name,
            tiers,
            str(counts.produced),
            str(counts.new),
            str(counts.updated),
            str(counts.resolved),
        )
    console.print(table)

    tier_table = Table(title="open flags by tier")
    tier_table.add_column("tier")
    tier_table.add_column("open", justify="right")
    for tier_name in ("violation", "signal", "opacity"):
        tier_table.add_row(tier_name, str(summary.tier_counts.get(tier_name, 0)))
    console.print(tier_table)
    console.print(
        f"Total: {summary.total_new()} new, {summary.total_updated()} updated, "
        f"{summary.total_resolved()} resolved ({summary.flags_produced} flag(s) produced this run)."
    )


@app.command("notify-pending")
def notify_pending_command(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print the grouped notification message instead of sending it to Telegram.",
    ),
) -> None:
    """Send one grouped Telegram notification for flags not yet notified.

    Bundles up to ~10 flags in full (severity emoji, message, deep link)
    plus a per-severity summary of any remainder, in a single HTML message.
    Marks every included flag's `notified_at` once actually sent. No-ops
    with a logged warning if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not
    configured, unless `--dry-run` is given (which never requires them).
    """
    with get_session() as session:
        pending = session.scalars(select(Flag).where(Flag.notified_at.is_(None), Flag.resolved_at.is_(None))).all()
        if not pending:
            console.print("No pending flags.")
            return

        message = asyncio.run(send_flags_notification(pending, dry_run=dry_run))

        if dry_run:
            console.print(message)
            console.print(
                f"[yellow]--dry-run[/yellow]: {len(pending)} pending flag(s) not sent."
            )
            return

        if message is None:
            console.print(
                "[yellow]Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID "
                "missing); no notification sent.[/yellow]"
            )
            return

        now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        for flag in pending:
            flag.notified_at = now
        session.commit()
        console.print(f"Notified {len(pending)} pending flag(s) in one grouped message.")


_SITE_OUT_OPTION = typer.Option(Path("site"), "--out", help="Output directory for the static site.")


@app.command("build-site")
def build_site_command(
    out: Path = _SITE_OUT_OPTION,
) -> None:
    """Generate the static website (HTML, CSV/JSON exports) from the database."""
    from nessebar_budget.web.build import build_site

    build_site(out)
    console.print(f"Site built in {out}/")


@app.command("pipeline")
def pipeline_command(
    mode: str = typer.Argument("weekly", help="Only 'weekly' is supported for now."),
) -> None:
    """Run the full weekly pipeline: scrape, parse, analyze, notify, build site."""
    from nessebar_budget.pipeline import run_weekly

    if mode != "weekly":
        raise typer.BadParameter("only 'weekly' is supported")
    run_weekly(get_settings())

if __name__ == "__main__":
    app()
