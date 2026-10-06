# Deploying Nessebar Budget Monitor (GitHub Actions + GitHub Pages)

This project ships with three workflows under `.github/workflows/`:

| Workflow | Trigger | What it does |
|---|---|---|
| `ci.yml` | push/PR to `main` | `ruff check` + `pytest -q`. No data, no deploy. |
| `weekly.yml` | Monday 05:00 UTC (cron), or manual | Scrapes all sources, parses/analyzes, sends pending Telegram alerts, commits `data/nessebar.db` if it changed, builds the static site, deploys it to GitHub Pages. |
| `pages.yml` | push to `main` touching `src/nessebar_budget/web/**` or `data/**`, or manual | Rebuilds + redeploys the static site only (no scraping) — e.g. after a template/CSS change. |

Everything below is a one-time setup you (the repo owner) need to do in the
GitHub UI. None of it is done automatically, and nothing here changes the
repo's visibility or pushes anything on its own.

## 1. Make the repository public

**GitHub Pages' free tier only serves public repositories.** This repo is
currently private, so none of the Pages-deploying steps above will work
until you flip it:

`Settings` → scroll to **Danger Zone** → **Change repository visibility** →
**Change to public**.

(There is a paid path — GitHub Pages for private repos is available on
GitHub Enterprise/Pro plans with certain conditions — but the "free CI/CD"
goal of this project assumes the public-repo route.)

## 2. Turn on GitHub Pages

`Settings` → `Pages` → under **Build and deployment**, set **Source** to
**GitHub Actions** (not "Deploy from a branch"). You don't need to pick a
specific workflow here — `weekly.yml` and `pages.yml` both already use
`actions/upload-pages-artifact` + `actions/deploy-pages`, which register
themselves as the Pages source once this is set.

After the first successful `deploy` job, the live URL appears both on this
Pages settings page and as the `deploy` job's "Deploy to GitHub Pages"
step's environment URL in the Actions run.

## 3. Add the Telegram secrets

The weekly pipeline's `notify-pending` step is a silent no-op until these
are set (see `nessebar_budget/notify/telegram.py`) — nothing breaks if you
skip this, you just won't get Telegram alerts.

`Settings` → `Secrets and variables` → `Actions` → `Secrets` tab → **New
repository secret**, add both:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

### Creating a Telegram bot (via @BotFather)

1. Open Telegram, start a chat with **@BotFather**.
2. Send `/newbot`, follow the prompts (pick a name and a unique
   `..._bot`-suffixed username).
3. BotFather replies with an HTTP API token, e.g.
   `123456789:AAFz...` — this is `TELEGRAM_BOT_TOKEN`.

### Getting a chat id

Pick whichever is easiest for how you want to receive alerts:

- **DM to yourself**: send any message to your new bot, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser (substitute
  your real token) and read `result[0].message.chat.id` from the JSON —
  that's `TELEGRAM_CHAT_ID` (a plain user chat id is a positive integer).
- **A group chat**: add the bot to the group, send a message in the group
  that mentions/replies to the bot (or just any message), then hit the same
  `getUpdates` URL — group chat ids are negative integers.
- **A channel**: add the bot as an admin of the channel, post anything,
  then use the same `getUpdates` URL, or use the channel's `@username` as
  `TELEGRAM_CHAT_ID` directly if it's public.

## 4. (Optional) Set SITE_BASE_URL

`Settings` → `Secrets and variables` → `Actions` → **Variables** tab → **New
repository variable**:

- Name: `SITE_BASE_URL`
- Value: your Pages URL, e.g. `https://dimitarmarenov33.github.io/NessebarBudgetChecker`

Both workflows fall back to that same URL by default if the variable isn't
set, so this step is only needed if you use a custom domain or rename the
repo.

## 5. Trigger a manual run

`Actions` tab → select **Weekly data pipeline** in the left sidebar →
**Run workflow** (top right) → choose the `main` branch → **Run workflow**.

This is the same job the Monday cron runs — useful for a first deploy
without waiting for next Monday, or for re-running after fixing a secret.

To redeploy *just* the site (no scraping) after a template change, use
**Deploy site** the same way, or just push to `main`; the `paths:` filter
on `pages.yml` picks it up automatically for changes under
`src/nessebar_budget/web/` or `data/`.

## 6. Where to see logs

`Actions` tab → click the workflow run → click a job (`update`, `build`,
`deploy`, `test`) → expand a step. The `pipeline weekly` step logs each of
its 8 sub-steps (`init_db`, `scrape_eop`, `scrape_sigma`,
`scrape_nesebar_site`, `parse_budget`, `analyze`, `notify_pending`,
`build_site`) with per-step timing and ok/FAILED status, plus a final JSON
summary line — search the step's log for `pipeline weekly: summary=` to
jump straight to it. A failed step is recorded but does not stop later
steps from running; the job itself only fails (red ✗, non-zero exit) if at
least one step failed.

## 7. Backfilling 2019–2025 locally

The weekly pipeline only scrapes `nesebar_site` since the previous month's
period (recent data) — that's deliberate, so a routine Monday run stays
fast. To pull the *entire* historical archive (back to the site's earliest
available reports, March 2019) once, run this **locally**, not in CI:

```bash
.venv/bin/python -m nessebar_budget.cli scrape nesebar_site --since 2019-01
```

This respects `robots.txt`'s `Crawl-delay: 10` (10 seconds between every
request) by default, and there are hundreds of monthly report files across
~7 years of archive, so **budget about 3 hours** for this to finish. It's
safe to re-run / interrupt and resume: already-downloaded files are cached
under `data/cache/nesebar_site/` (gitignored) and skipped without a new
request.

Once it's done, parse and commit the result:

```bash
.venv/bin/python -m nessebar_budget.cli parse-budget
.venv/bin/python -m nessebar_budget.cli analyze
git add data/nessebar.db
git commit -m "data: backfill nesebar_site reports 2019-01..present"
git push
```

Pushing this to `main` will trigger `pages.yml` (the `data/**` path filter)
and rebuild/redeploy the site with the full history, without needing to
wait for or re-run the full weekly scrape.

## Notes on the weekly job's running time

`scrape_eop` alone is roughly 440 HTTP calls at a 1-second delay between
each (~8 minutes); `scrape_nesebar_site` adds a handful more at the
10-second `Crawl-delay` its `robots.txt` requires. Expect the full `pipeline
weekly` run to take somewhere around 10-15 minutes end to end — comfortably
inside GitHub Actions' free-tier minutes for a once-a-week job on a public
repo (public repos get unlimited free Actions minutes on standard runners;
there is nothing to pay for here as long as the repo stays public).
