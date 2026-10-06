"""Command-line interface for Nessebar Budget Monitor."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from nessebar_budget.analysis.engine import run_rules
from nessebar_budget.db.budget_repo import (
    upsert_budget_line_items,
    upsert_budget_report,
    upsert_cash_execution_lines,
)
from nessebar_budget.db.models import BudgetReport, Flag, Procurement
from nessebar_budget.db.repo import upsert_procurements
from nessebar_budget.db.session import get_session, init_db
from nessebar_budget.notify.telegram import send_message
from nessebar_budget.parsers.budget_b1 import parse_budget_b1
from nessebar_budget.parsers.budget_capital import parse_budget_capital
from nessebar_budget.scrapers.eop import EopScraper
from nessebar_budget.scrapers.minfin import MinfinScraper
from nessebar_budget.scrapers.nesebar_site import NesebarSiteScraper
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

ALL_SOURCES = ["eop", "sigma", "nesebar_site", *STUB_SCRAPERS]


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
        help="Cap the number of paginated list pages fetched (eop) or files downloaded "
        "(nesebar_site); for testing.",
    ),
    since: str = typer.Option(
        None, "--since", help="nesebar_site only: only fetch reports for period >= YYYY-MM."
    ),
    delay: float = typer.Option(
        None, "--delay", help="nesebar_site only: seconds between requests (default 10, "
        "per robots.txt Crawl-delay)."
    ),
) -> None:
    """Fetch records from SOURCE (one of: eop, sigma, nesebar_site, minfin).

    `eop`, `sigma`, and `nesebar_site` fetch live records and upsert them
    into the database, printing a summary. `minfin` is still an
    unimplemented stub.
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
        None, "--period", help="Only parse cached BudgetReport rows for this YYYY-MM period."
    ),
) -> None:
    """Parse cached budget-report files (kinds `B1` and `capital_xlsx`) into
    `BudgetLineItem`/`CashExecutionLine`, and print a summary."""
    init_db()

    with get_session() as session:
        query = select(BudgetReport).where(BudgetReport.kind.in_(["B1", "capital_xlsx"]))
        if period:
            query = query.where(BudgetReport.period == period)
        reports = session.scalars(query).all()

        files_parsed = 0
        line_items = 0
        cash_lines = 0
        mismatches = 0
        failures: list[tuple[str, str]] = []

        for report in reports:
            if not report.file_path or not Path(report.file_path).exists():
                failures.append((report.file_path or f"report #{report.id}", "file missing on disk"))
                continue
            try:
                if report.kind == "capital_xlsx":
                    result = parse_budget_capital(report.file_path, period=report.period)
                    inserted, updated = upsert_budget_line_items(
                        session, report.id, result["rows"]
                    )
                    line_items += inserted + updated
                    mismatches += len(result["mismatches"])
                    report.parsed_json = {
                        "row_count": len(result["rows"]),
                        "mismatches": result["mismatches"],
                    }
                else:  # "B1"
                    result = parse_budget_b1(report.file_path, period=report.period)
                    inserted, updated = upsert_cash_execution_lines(
                        session, report.id, result["rows"]
                    )
                    cash_lines += inserted + updated
                    report.parsed_json = {
                        "row_count": len(result["rows"]),
                        "skipped": result["skipped"],
                    }
                files_parsed += 1
            except Exception as exc:  # noqa: BLE001 -- keep ingesting the remaining files
                logger.warning("parse-budget: failed to parse %s: %s", report.file_path, exc)
                failures.append((report.file_path, str(exc)))

        session.commit()

    table = Table(title="parse-budget summary")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("Files parsed", str(files_parsed))
    table.add_row("Files failed", str(len(failures)))
    table.add_row("Budget line items (capital ledger)", str(line_items))
    table.add_row("Cash execution lines (B1)", str(cash_lines))
    table.add_row("Validation mismatches logged", str(mismatches))
    console.print(table)

    for file_path, reason in failures:
        console.print(f"[red]FAILED[/red] {file_path}: {reason}")


@app.command("analyze")
def analyze_command() -> None:
    """Run analysis rules over stored procurements and persist resulting flags."""
    with get_session() as session:
        procurements = session.scalars(select(Procurement)).all()
        records = [
            {
                "id": p.id,
                "source_id": p.source_id,
                "contract_value_bgn": p.contract_value_bgn,
            }
            for p in procurements
        ]
        flags = list(run_rules(records))
        for flag in flags:
            session.add(flag)
        session.commit()
        console.print(f"Generated {len(flags)} flag(s) from {len(records)} procurement(s).")


@app.command("notify-pending")
def notify_pending_command() -> None:
    """Send Telegram notifications for flags that haven't been notified yet."""
    with get_session() as session:
        pending = session.scalars(select(Flag).where(Flag.notified_at.is_(None))).all()
        if not pending:
            console.print("No pending flags.")
            return

        async def _send_all() -> None:
            for flag in pending:
                await send_message(f"[{flag.severity}] {flag.rule}: {flag.message}")

        asyncio.run(_send_all())
        console.print(f"Attempted to notify {len(pending)} pending flag(s).")


@app.command("serve")
def serve_command(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the local FastAPI web UI."""
    import uvicorn

    uvicorn.run("nessebar_budget.web.app:app", host=host, port=port)


if __name__ == "__main__":
    app()
