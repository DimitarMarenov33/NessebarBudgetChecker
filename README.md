# Nessebar Budget Monitor

**Nessebar Budget Checker** / **Несебър Бюджет Чекър**

## Цел (BG)

Граждански инструмент за наблюдение на бюджета и обществените поръчки на
Община Несебър: автоматично извлича публични данни (ЦАИС ЕОП, СИГМА,
официалния сайт на общината), ги съпоставя, прилага прости правила за
откриване на аномалии (напр. липсваща стойност на договор) и публикува
резултата като статичен сайт, като при нужда известява и в Telegram.
Проектът **не твърди** злоупотреба — маркира само статистически необичайни
записи, за по-нататъшна проверка от хора.

## Purpose (EN)

A civic-tech tool that monitors Nessebar Municipality's public procurement
and budget-execution data: it scrapes public sources (ЦАИС ЕОП, SIGMA, the
municipality's own website), parses the budget reports, flags statistical
anomalies with a small rule engine, publishes the result as a static
website, and can optionally notify a Telegram chat. It does **not** accuse
anyone of wrongdoing — see the [Disclaimer](#disclaimer) below.

## How this project is built (AI disclosure)

**The running system uses no AI.** Every number, signal and text on the
site comes from deterministic code. Scrapers download the official files,
parsers read them row by row, fixed rules with published thresholds decide
what becomes a signal, and the explanations are pre-written templates
filled in with figures from the data. No language model, neural network or
machine-learning model is called at any step, and the same input always
produces the same output.

**The source code was written with AI assistance.** Most of the code, tests
and documentation in this repository were generated with Claude Code,
Anthropic's AI coding assistant, working under the direction of the
maintainer. The maintainer set the goals and requirements, chose the data
sources, made the design and legal decisions (a lawyer reviewed the legal
wording), and reviewed the results. Beta testers check published signals
against the original source files. AI assistance is still used, and
commits do not always say so, so treat any commit as possibly AI-assisted.

**Correctness does not rest on trusting the AI.** An automated test suite
checks the parsers and rules, including against real source files. Every
signal on the site lists the exact file, sheet, row and field its numbers
come from, so anyone can check it by hand. Legal citations are quoted from
the law texts stored in `docs/law/`.

**Contributing with AI tools.** AI-assisted contributions are welcome if
you understand and can explain what you submit, you have checked that it
may be published under this project's licence, and you say in the pull
request which tool you used and for what.

**На български.** Работещата система не използва изкуствен интелект:
числата и сигналите се изчисляват с фиксирани, публични правила върху
официални данни. Програмният код е писан с помощта на Claude Code (ИИ
асистент за програмиране) под ръководството на поддържащия проекта, който
определя целите, източниците и правните решения и проверява резултатите.
Всеки сигнал посочва файла, листа и реда, от които идват числата му.

## Architecture

```
                 ┌─────────────────────────────┐
  Monday 05:00   │   GitHub Actions: weekly.yml │
  UTC (cron) ───▶│   "pipeline weekly":         │
  or manual      │   scrape → parse → analyze   │
                 │   → notify → build site      │
                 └───────────┬─────────┬─────────┘
                             │         │
              commits data/  │         │  uploads site/
              nessebar.db    │         │  as a Pages artifact
              (if changed)   ▼         ▼
                 ┌─────────────┐  ┌──────────────────┐
                 │ SQLite DB,  │  │  GitHub Pages     │
                 │ in the repo │  │  (static HTML)    │
                 │ (data/)     │  │  <user>.github.io │
                 └─────────────┘  └──────────────────┘
                             │
                             │ pending Flag rows
                             ▼
                 ┌─────────────────────┐
                 │   Telegram chat      │
                 │ (optional, via       │
                 │  TELEGRAM_BOT_TOKEN) │
                 └─────────────────────┘
```

- **No server to run or pay for.** Everything happens inside free GitHub
  Actions minutes (public repos get those for free) and free GitHub Pages
  hosting. See `docs/DEPLOY.md` for the one-time setup.
- **The database is the repo.** `data/nessebar.db` is committed by the
  weekly bot job itself (author `nessebar-bot`), so the full history of
  every weekly run is just `git log -- data/nessebar.db`.
- **The site is rebuilt, not hand-edited.** `src/nessebar_budget/web/`
  generates static HTML from whatever's currently in the DB; pushing a
  template/code change there (or a direct `data/` update) redeploys it on
  its own via `pages.yml`, without re-scraping anything.
- A local FastAPI app (`nessebar serve`) also exists for interactively
  browsing the same data on your own machine.

## Local setup

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
# then edit .env with your Telegram bot token / chat id, and (optionally)
# SITE_BASE_URL, if you want to build the site locally
```

## Commands

All commands are available both as `.venv/bin/python -m nessebar_budget.cli
<command>` and, once installed, as the `nessebar <command>` console script
(see `pyproject.toml`'s `[project.scripts]`).

```bash
.venv/bin/python -m nessebar_budget.cli --help

.venv/bin/python -m nessebar_budget.cli init-db
.venv/bin/python -m nessebar_budget.cli scrape eop
.venv/bin/python -m nessebar_budget.cli scrape sigma
.venv/bin/python -m nessebar_budget.cli scrape nesebar_site --since 2026-01
.venv/bin/python -m nessebar_budget.cli parse-budget
.venv/bin/python -m nessebar_budget.cli analyze
.venv/bin/python -m nessebar_budget.cli notify-pending
.venv/bin/python -m nessebar_budget.cli build-site --out site
.venv/bin/python -m nessebar_budget.cli pipeline weekly   # runs all of the above, in order
.venv/bin/python -m nessebar_budget.cli serve             # local FastAPI UI at :8000
```

`pipeline weekly` is what `.github/workflows/weekly.yml` runs in CI; see
`src/nessebar_budget/pipeline.py` for exactly what each of its 8 steps does
and how failures are isolated (one failing step doesn't stop the rest, but
the command exits non-zero if anything failed).

## Tests & linting

```bash
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
```

## Layout

```
src/nessebar_budget/
    config.py          # pydantic-settings Settings (.env-driven)
    cli.py              # typer CLI: init-db, scrape, parse-budget, analyze,
                        #   notify-pending, build-site, pipeline, serve
    pipeline.py         # run_weekly(): the 8-step weekly CI pipeline
    db/
        models.py       # SQLAlchemy 2.0 models: Procurement, BudgetReport,
                        #   BudgetLineItem, CashExecutionLine, Flag
        session.py      # engine/session factory, init_db()
        repo.py         # Procurement upserts
        budget_repo.py  # BudgetReport/BudgetLineItem/CashExecutionLine upserts
    scrapers/
        base.py         # abstract Scraper class
        eop.py          # ЦАИС ЕОП (service.eop.bg) — procurement/contracts
        sigma.py        # SIGMA (sigma.midt.bg) — procurement CSV export
        nesebar_site.py # official municipality site's monthly report archive
        minfin.py       # Ministry of Finance municipal data (stub)
    parsers/
        budget_b1.py    # "B1" cash-execution report parser
        budget_capital.py  # capital-expenditure ledger (XLSX) parser
    analysis/
        rules.py        # Rule protocol + MissingValueRule, PricePerUnitRule (placeholder)
        engine.py        # runs rules over records, yields Flag objects
    notify/
        telegram.py     # async send_message() via python-telegram-bot
    web/
        app.py          # FastAPI app for local browsing: GET /, GET /api/flags
        build.py        # static site generator: build_site(out_dir) for GitHub Pages
        templates/

.github/workflows/
    ci.yml              # push/PR to main: ruff + pytest
    weekly.yml          # Monday cron + manual: full pipeline, commits data/, deploys Pages
    pages.yml           # push to main (web/** or data/** changed) + manual: rebuild+redeploy only

docs/
    DEPLOY.md           # step-by-step GitHub Actions + Pages setup for the repo owner
    law/INDEX.md        # legal basis: which laws justify publishing this analysis
    sources/            # reverse-engineered API/format notes for each data source
data/        # local database, cached downloads, sample data (maintained separately)
tests/       # pytest unit/smoke tests
```

## Data sources

| Source | What it gives us | Notes |
|---|---|---|
| ЦАИС ЕОП (`service.eop.bg`) | Procurement procedures + signed contracts, values, contractors | Bulgaria's national procurement register; see `docs/sources/EOP_API.md` |
| SIGMA (`sigma.midt.bg`) | Same underlying ЦАИС ЕОП/АОП data, pre-cleaned CSV per authority | See `docs/sources/SIGMA_API.md` |
| `nesebar.bg/reports.html` | Monthly budget-execution reports (B1 cash execution, capital-expenditure ledger), back to March 2019 | See `docs/sources/BUDGET_FORMS.md`, `docs/sources/INVENTORY.md` |
| Ministry of Finance (minfin.bg) | Municipal financial-condition indicators | Not yet wired up (`scrapers/minfin.py` is a stub) |

Full reconnaissance notes (what's confirmed vs. still a guess, scrapability
per source) live in `docs/sources/INVENTORY.md`.

## Legal basis

This project only aggregates and re-presents data that Bulgarian law already
requires these institutions to publish proactively (obligations under ЗОП,
ЗПФ, ЗДОИ, ЗМСМА, among others). See **`docs/law/INDEX.md`** for the index
of source laws, with citations, and `docs/law/*.txt` for the extracted
texts themselves.

## Disclaimer

**Flags are anomalies, not proof.** The rules in
`src/nessebar_budget/analysis/rules.py` (e.g. a contract with no recorded
value) surface statistically unusual records so a human can look closer —
they are not an accusation of wrongdoing, and a flagged record may well
turn out to have an entirely ordinary explanation (a data-entry gap, a
multi-year framework contract, a redacted classified amount, etc.). Always
verify against the original source document (linked from every flagged
record) before drawing any conclusion.

## License

The source code is licensed under the **European Union Public Licence
v. 1.2 (EUPL-1.2)**; the full text is in [`LICENSE`](LICENSE). Copyright ©
2026 the Nessebar Budget Monitor contributors. In short: anyone may use,
study, change and share the code, and anyone who distributes a changed
version, or runs it as a service for others, must publish their changes
under the same licence. The EUPL has official, equally valid texts in all
EU languages, including Bulgarian:
https://interoperable-europe.ec.europa.eu/collection/eupl/eupl-text-eupl-12

Data from the sources listed above keeps its own terms (for example the
Trade Register's open data is CC0 and SIGMA's data is CC BY); the map
tiles are © OpenStreetMap contributors.

## Local-only data handling

- No telemetry or analytics of any kind.
- The only outbound network calls this project makes on its own initiative
  are (a) scraping the public municipal/government sources above, and (b)
  sending Telegram messages, only if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
  are configured.
- All data is stored in a SQLite database (`data/nessebar.db` by default,
  committed to this repo by the weekly CI job) and local files under
  `data/`.
