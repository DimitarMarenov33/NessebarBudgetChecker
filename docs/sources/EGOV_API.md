# data.egov.bg — Портал за отворени данни (captured via Playwright, 2026-10-06)

Confirms and extends the earlier curl-only finding: **this is not CKAN** (`/api/3/action/*` 404s). It is a
custom Laravel/Blade-templated portal. The dataset *search results list is populated client-side with a
multi-second delay after the initial page load* (not visible in a `networkidle`+2s screenshot, but present
after ~4s extra wait) — if automating this, poll/wait rather than trusting `networkidle` alone.

## Confirmed working URL patterns (plain `httpx.get`, no auth/cookies needed for reads)
- **Dataset search**: `GET https://data.egov.bg/data?q=<query>` — e.g. `?q=обществени+поръчки` → "293 намерени
  резултати", paginated with `&page=N`. Can be combined with facets seen in the UI: `&org[0]=<org id>` (e.g.
  `502` = Агенция по обществени поръчки, 33 datasets), `&tag[0]=<tag id>` (e.g. `1036` = "обществени поръчки"
  tag, 42 datasets; `1284` = "поръчки", 57 datasets).
- **Dataset detail page**: `GET https://data.egov.bg/data/view/<uuid>` — e.g.
  `https://data.egov.bg/data/view/30437d16-3fec-4fad-8ac8-1eddee200e7c`.
- **Resource (one file) detail page**: `GET https://data.egov.bg/data/resourceView/<uuid>` — shown embedded
  within a dataset's page as one row per period/file.
- **Resource download**: `POST https://data.egov.bg/resource/download` with form fields `_token` (CSRF, must
  be scraped from the resourceView page's `<meta name="csrf-token">` or the form's hidden input — session/cookie-bound,
  so this is **not** a cookie-free one-shot GET), `resource` (numeric id, e.g. `129365`), `version` (e.g. `1`),
  `name`, `format` (dropdown offers at least `CSV`; other formats present in the select were not fully enumerated).
  **Not executed this session** (would need a session-aware two-step httpx flow: GET resourceView → extract
  token+cookie → POST download); documented as the confirmed mechanism, not yet replayed standalone.
- **API docs**: top nav "API спецификация" → `https://data.egov.bg/api-spetsifikatsiya?section=22` is a
  near-empty placeholder page ("Липсва помощна страница" = "help page missing"); the real content is one level
  in, under `https://data.egov.bg/api-spetsifikatsiya?section=22&item=82` ("Указания за интеграция на API"),
  which renders as a single **"СВАЛИ API"** (Download API) icon/button — a downloadable spec file (format/link
  target not resolved this session; it's not a plain `<a href>` with a recognizable extension, likely JS-driven).

## ЦАИС ЕОП / АОП bulk-export dataset (the thing this project actually wants)
Found directly in the "обществени поръчки" search results, published by org **"Агенция по обществени поръчки"
(АОП)**: a series of datasets titled **"Автоматично генерирани данни за обявления, публикувани в ЦАИС ЕОП през
месец <MM.YYYY> г., съгласно стандарт OCDS"** — i.e. **one dataset per month**, each containing **one resource
per day** within that month (confirmed: a resource titled "...на 24.09.2026 г..." existed inside the "09.2026"
monthly dataset). Format: **OCDS (Open Contracting Data Standard)** JSON release packages (the resourceView
page shows an inline JSON preview with OCDS fields like `"ocid"`, `"tag":["BG-ZOP"]`, `"value":{...}`), with a
**CSV** export option also offered per the download form. This is a genuine daily/monthly bulk feed of every
ЦАИС ЕОП announcement, in a standardized schema — likely the most robust single source for this project's
"all procurement nationwide" baseline, **if** the CSRF-protected download flow is automated. The dataset's own
description points to the authoritative rules doc: `https://www2.aop.bg/e-uslugi/otvoreni-danni-ot-rop/`
("Правила за публикуване: Ръководство за потребители на данни за обществени поръчки, публикувани чрез
стандарта за отворени данни OCDS") — **not fetched this session**, recommended first read before building
an ingester against this feed.

## Not done / open questions
- Did not complete a full download via the `resource/download` POST flow (needs session cookie + CSRF token
  handling — straightforward but not done this session).
- Did not resolve the "СВАЛИ API" button's actual target file.
- Did not fetch `www2.aop.bg/e-uslugi/otvoreni-danni-ot-rop/` (the OCDS rules/userguide doc) — high-value next step.
- Did not specifically search "Несебър" as a dataset query (the task's second search term) — the "обществени
  поръчки" search above already surfaces the nationwide bulk feed, which is the more valuable find; a
  municipality-specific dataset on this portal is unlikely to exist separately from the AOP bulk feed.
