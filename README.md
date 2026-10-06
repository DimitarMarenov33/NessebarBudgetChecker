# Nessebar Budget Monitor

A local, civic-tech tool for monitoring Nessebar Municipality's public
procurement and budget data: scraping public sources, parsing PDF/XLSX
reports, flagging anomalies with simple rules, and optionally notifying a
Telegram chat.

## Local-only

This project runs entirely on your own machine:

- No cloud services, no Vercel, no deployment target.
- No telemetry or analytics of any kind.
- The only outbound network calls are (a) scraping the public municipal /
  government data sources you point it at, and (b) sending Telegram
  messages, and only if you configure a bot token and chat id.
- All data is stored locally in a SQLite database (`data/nessebar.db` by
  default) and local files under `data/`.

## Setup

```bash
# Create the virtualenv with Python 3.12
/opt/homebrew/opt/python@3.12/bin/python3.12 -m venv .venv

# Install the project and its dev dependencies
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"

# Install the Chromium browser used by Playwright-based scraping
.venv/bin/playwright install chromium

# Configure secrets
cp .env.example .env
# then edit .env with your Telegram bot token / chat id if you want notifications
```

## Usage

```bash
.venv/bin/python -m nessebar_budget.cli --help
.venv/bin/python -m nessebar_budget.cli init-db
.venv/bin/python -m nessebar_budget.cli scrape eop
.venv/bin/python -m nessebar_budget.cli analyze
.venv/bin/python -m nessebar_budget.cli notify-pending
.venv/bin/python -m nessebar_budget.cli serve
```

## Tests & linting

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## Layout

```
src/nessebar_budget/
    config.py          # pydantic-settings Settings (.env-driven)
    cli.py              # typer CLI: init-db, scrape, analyze, notify-pending, serve
    db/
        models.py       # SQLAlchemy 2.0 models: Procurement, BudgetReport, Flag
        session.py      # engine/session factory, init_db()
    scrapers/
        base.py         # abstract Scraper class
        eop.py          # stub: Public Procurement Register (EOP)
        nesebar_site.py # stub: official municipality website
        minfin.py       # stub: Ministry of Finance municipal budget data
    parsers/
        pdf.py          # stub: PDF table extraction
        xlsx.py         # stub: XLSX table extraction
    analysis/
        rules.py        # Rule protocol + MissingValueRule, PricePerUnitRule (placeholder)
        engine.py        # runs rules over records, yields Flag objects
    notify/
        telegram.py     # async send_message() via python-telegram-bot
    web/
        app.py          # FastAPI app: GET /, GET /api/flags
        templates/
            index.html

docs/        # legal texts, sourced reference material (maintained separately)
data/        # local database, cached downloads, sample data (maintained separately)
tests/       # pytest smoke tests
```

## Status

This is a scaffold: scrapers and parsers are stubs (they raise
`NotImplementedError`) until wired up to real data sources. No placeholder
or fake data is used anywhere in the codebase.
