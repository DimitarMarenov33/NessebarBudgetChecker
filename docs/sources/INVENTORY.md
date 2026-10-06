# Data source inventory — Община Несебър spending transparency

Compiled 2026-10-06 by automated recon (curl + web search only; no browser automation was available — `.venv` with Playwright was not present at `<project>/.venv` at the time of this check, so anything marked "needs browser" was not inspected via DevTools/network tab and should be re-checked once that environment exists). Everything below marked "confirmed" was actually fetched; everything marked "guess"/"unconfirmed" was not and should not be treated as fact.

---

## 1. Official municipal site — nesebar.bg / nessebar.bg / nesebar.bg (bare)
- **URLs**: https://www.nesebar.bg, https://nessebar.bg, https://nesebar.bg — all three resolve to the same site (200 OK). Legacy domain `nesebarinfo.com` 301-redirects into it.
- **Format**: plain server-rendered HTML (old Bootstrap theme, no JS rendering needed) for the municipal pages, **plus** a separate WordPress blog at `/news/` (has a `wp-json` REST API, not yet probed).
- **Auth**: none.
- **robots.txt**: `Disallow: /admin/, /wp-admin/`, `Crawl-delay: 10` — otherwise allowed. Respect the 10s crawl delay.
- **Key pages found**:
  - `/reports.html` — **"Отчет за касово изпълнение на бюджета"**: a monthly archive folder (`03-2019/...`) of real budget-execution spreadsheets (`B1_YYYY_M_5206.xls`, `IB1_..._DES/DMP/K33/KSF/RA.xls` sub-breakdowns, plus friendlier-named files like `august2026.xlsx`, `june26.xlsx`, `otchet0726.xlsx`). Range: at least 2019 → August 2026, i.e. monthly cadence, ~7 years of history. This is the single best source for "budget vs. actual" numbers.
  - `/register.html` — "Публичен регистър": PDFs/XLSX of registries (e.g. construction-permit/commissioning registers `regUV*`, `reg-obekti-eksploatacia2025.xlsx`). Useful for cross-checking capital/construction spend but not a contracts register per se.
  - `/publicorders.php` — "Обществени поръчки" landing page; links out to the real buyer profile at **nesebar.imeon.bg**.
  - `/rules-public-information.html` — "Достъп до обществена информация" (ДОИ) rules/contact for FOI-style requests.
  - `/news/...` — WordPress news, includes budget-adoption announcements (e.g. "ПРИЕХА БЮДЖЕТ 2025") but the announcement page itself did not link a budget PDF directly in this check.
- **Scrapability: EASY.** Plain HTML, directory-style file links, no auth, generous crawl-delay only restriction.
- **Working curl**:
  ```
  curl -sL -A "$UA" https://www.nesebar.bg/reports.html -o reports.html
  curl -sL -A "$UA" https://www.nesebar.bg/03-2019/august2026.xlsx -o budget_aug2026.xlsx
  ```

## 2. Профил на купувача — nesebar.imeon.bg
- **URL**: https://nesebar.imeon.bg/frmAOP.aspx (confirmed 200, 47KB).
- **What it is**: the municipality's real e-procurement buyer profile, hosted on "imeon.bg" — a shared ASP.NET WebForms CMS used by multiple BG municipalities (not Nessebar-specific infra).
- **Format**: server-rendered ASP.NET WebForms with `__doPostBack`/ViewState navigation — not a SPA, but pagination/filtering happens via postbacks, not plain links.
- **Auth**: public browsing; a "вход"/login exists for internal users only.
- **Scrapability: MEDIUM.** No JS rendering needed (no browser required), but a scraper must replay ASP.NET postbacks (capture `__VIEWSTATE`/`__EVENTVALIDATION` and POST) to page through records — more fragile than plain HTML, more friendly than a true JS SPA.

## 3. ЦАИС ЕОП — app.eop.bg (SPA) + service.eop.bg (real backend)
- **app.eop.bg**: confirmed Angular SPA (bundle files `runtime.*.js`, `polyfills.*.js`, `scripts.*.js`, `main.*.js`). The server returns the **same SPA shell HTML** for every path guessed (`/api/...`, `/backend/...`) — i.e. there is no discoverable REST surface directly on this host via path guessing; it needs a real browser/DevTools network capture to see which XHRs the Angular app makes (not possible this session — no Playwright env found).
- **service.eop.bg** — **the real backend**, found via public documentation/community sources (not independently re-derived by us): a WCF/JSON-style service at `https://service.eop.bg/NX1Service.svc/` with methods including `GetQuickSearchResult`, `GetPublishedTendersAdvancedSearchResult`, `GetContractsAdvancedSearchResult`, `GetPublishedTenderDetails`, `GetPublicBuyerProfileBasicInformation`. **Confirmed reachable** in this session: POSTing JSON to these URLs returns structured JSON errors (`{"ActivityId":...,"ErrorCode":4,"FaultMessage":null}`) rather than being blocked, and the response carries `Access-Control-Allow-Origin: *`, confirming it's meant to be called cross-origin from the SPA, no auth challenge seen. **Not yet confirmed**: the exact request-body shape — three guessed payloads (`{"SearchText":...}`, `{"request":{...}}`, `{"text":...,"pageIndex":...}`) all failed with ErrorCode 4 / HTTP 400-500. This needs one browser session with DevTools open on app.eop.bg to copy the real request body, after which it should be a clean, no-auth, no-key JSON API covering стойност/изпълнител/CPV/дата for every tender and contract, filterable by възложител (Община Несебър).
- **Scrapability: MEDIUM today, EASY once the payload schema is captured.** This is the highest-value target for both overpriced-purchase detection and missing-cost-figure detection once unlocked.

## 4. АОП / old РОП — aop.bg, www2.aop.bg
- **www.aop.bg**: 200 OK, old Регистър на обществените поръчки (pre-ЦАИС-ЕОП procedures). Individual case pages follow pattern `https://aop.bg/case2.php?case_id=<id>` (pattern found via search, e.g. `case_id=359928`); search UI at `ssearch.php?word=...`.
- **www2.aop.bg/e-uslugi/otvoreni-danni-ot-rop/**: AOP's own open-data export page for РОП (CSV/XML bulk, per AOP's public announcements covering ~2015-2019 data) — URL noted but **not fetched** this session (time budget).
- **Scrapability: MEDIUM.** Old-style PHP, plain HTML, no JS, but structure/IDs need enumeration (no municipality-level listing endpoint confirmed yet); the open-data export page should be checked first since it may give a direct CSV/XML dump instead of page-by-page scraping.

## 5. Портал за отворени данни — data.egov.bg
- **URL**: https://data.egov.bg — 200 OK.
- **Important correction**: this is **not a CKAN instance** despite being commonly described that way — `/api/3/action/*` calls (CKAN convention) returned `{"success":false,...,"error":"Непознат метод"}` (unknown method). Third-party documentation (api-evangelist's independent profile) describes it instead as a custom Laravel app with its own REST surface (`listDatasets`, `getDatasetDetails`, `listResources`, `getResourceData`, `listOrganisations`, etc., base `https://data.egov.bg/api`), but our one guessed call (`/api/listDatasets`) also 404'd, so the exact base path/method names are **unconfirmed** — needs the portal's own front-end JS network calls inspected.
- **Scrapability: MEDIUM, API shape unconfirmed.** AOP does publish a "обществени поръчки" dataset here per the AOP announcement found; worth another pass once API base is confirmed.

## 6. Министерство на финансите — minfin.bg
- **URL**: https://www.minfin.bg — **blocked**: HTTP 403, Cloudflare JS challenge page ("Just a moment...") on root, `/bg`, and `/bg/810` ("Финансови показатели на общините"). Confirmed via curl with browser-like headers — still blocked.
- **Scrapability: HARD without a real browser.** Needs Playwright/headless Chrome to pass the Cloudflare challenge (cookies + JS execution). Not possible this session (no `.venv`/Playwright found). Once unblocked, the monthly municipal financial-condition reports and "Макети на отчети" documents (XLSX/PDF) are the target — URL index pages were located (`/bg/810`, `/bg/1745`) but contents not yet seen.

## 7. Сметна палата — bulnao.government.bg
- **URL**: https://www.bulnao.government.bg — 200 OK (one redirect to `/bg/`), fully open, plain HTML, direct PDF links, no auth.
- **Confirmed content**: `/bg/oditna-dejnost/finansovi-oditi-na-gfo/finansovi-oditi-na-gfo-za-2020-g-obshini/` lists a 2020 consolidated financial-report audit for Несебър, linking directly to `/bg/documents/12555/GFO_Nesebar_nemod_2020.pdf` — **downloaded** (1.05MB, 16-page PDF). Web search also surfaced references to GFO_Nesebar audits for 2019/2021/2022 years (and press coverage noting "qualified opinion" findings in 2021/2023/2024, and a 6-year gap in comprehensive audits) — these specific report URLs were **not individually fetched** this session.
- **Scrapability: EASY.** Static HTML + PDFs, year-by-year listing pages, no auth, no JS.

## 8. SIGMA — sigma.midt.bg (not sigma.bg)
- **Important correction**: `sigma.bg` (200 OK) is an unrelated site. The real government platform the user meant is **https://sigma.midt.bg** — "СИГМА — Платформа за прозрачност на обществените поръчки", built by the Ministry of Innovation and Digital Transformation, publicly launched per multiple news outlets (offnews.bg, marica.bg, mig.government.bg). Per its own meta description: aggregates АОП/ЦАИС-ЕОП public-procurement data, filterable by institution/municipality/contractor/CPV, shows estimated vs. final contract value plus corrections, and explicitly aims to auto-flag **favoritism (single-bidder wins) and inflated prices** — i.e. this is conceptually the closest existing tool to what this project wants to build.
- **Confirmed**: page loads (200, 71KB, looks like a Next.js app via its meta tags).
- **Unconfirmed**: its backend API — not discovered this session (would need DevTools network capture; no browser available). Very likely it is itself a wrapper over the same ЦАИС ЕОП/service.eop.bg data described in §3.
- **Scrapability: UNKNOWN/needs browser.** High priority to revisit with Playwright — if it exposes a clean JSON API with pre-computed "red flags," it could shortcut much of this project's own anomaly-detection work for the procurement side.

## 9. Общински съвет Несебър — os-nessebar.eu
- **Correction**: the guessed `os-nesebar.bg` does **not resolve** (DNS failure). The real domain is **os-nessebar.eu** (200 OK).
- **Content**: `/resheniya` (council decisions), `/protokoli-ot-zasedaniya-na-obshtinski-savet` (session protocols/minutes), `/osporeni-resheniya` (contested/appealed decisions) — each with dated sub-pages (e.g. `.../protokol-2924092026-g`). Budget adoption and amendment decisions should live here as PDFs attached to specific protocols; not individually verified this session.
- **Scrapability: EASY (likely).** Plain HTML with dated permalink pattern, no auth seen; worth a dedicated pass to enumerate protocol/decision PDFs.

## 10. Aggregators & registries — mixed results
- **tenders.guru/bg**: DNS/connection failure (HTTP 000) — not reachable from this network at check time. Re-test later; do not assume it's down permanently.
- **portal.registryagency.bg** (Търговски регистър): 200 OK, reachable. Known from general knowledge (not independently re-verified here) that bulk/free programmatic access is restricted — paid or registered API only for structured data; free lookups are per-company web forms. Alternatives (papagal.bg, opencorporates.com) were **not checked** this session.
- **acf.bg** (Антикорупционен фонд): 200 OK, reachable; **not explored further** this session — worth a follow-up pass for any existing Nessebar-specific investigations/datasets.
- **opentender.eu, obshtestveniporachki.bg**: **not attempted** this session (time budget).

---

## Samples downloaded (`data/samples/`)
| File | Source | Size | Notes |
|---|---|---|---|
| `bulnao_audit_nesebar_gfo_2020.pdf` | bulnao.government.bg | 1,054,898 B | Сметна палата financial audit of Nessebar's 2020 consolidated annual financial report (16 pp) |
| `nesebar_budget_execution_aug2026.xlsx` | nesebar.bg/03-2019/ | 89,286 B | Monthly cash budget-execution report, August 2026 |
| `nesebar_cash_execution_B1_2026_8.xls` | nesebar.bg/03-2019/ | 2,542,592 B | Detailed "B1" cash-execution breakdown, August 2026, legacy XLS format |

Note: no ЦАИС ЕОП contract JSON/HTML, no MinFin municipal report, and no additional budget-decision PDF from os-nessebar.eu were captured — each is blocked or needs more time/browser access, as detailed above and in FETCH_LOG.md. These are the clear next steps, not silent gaps.

---

## Recommended ingestion order

**For detecting overpriced purchases (needs: contract value + CPV/category + contractor, ideally with peer-comparison):**
1. **service.eop.bg (ЦАИС ЕОП backend)** — richest structured data (value, contractor, CPV, dates) once the request schema is captured via one browser DevTools session. Highest leverage, currently blocked only by an unknown JSON schema, not by access control.
2. **sigma.midt.bg** — may already compute "inflated price" / "favoritism" flags; worth reverse-engineering before building your own detector, since it targets exactly this use case.
3. **aop.bg old РОП + data.egov.bg open-data export** — fills in pre-2020 procedures that never reached ЦАИС ЕОП.
4. **nesebar.imeon.bg buyer profile** — municipality's own copy of record; useful as a cross-check/fallback when central systems are incomplete, medium scraping effort (ASP.NET postbacks).

**For detecting missing/unpublished cost figures (needs: municipal-side records to diff against the above):**
1. **nesebar.bg/reports.html** (budget execution XLS/XLSX archive) — easiest, richest, longest-running numeric series available right now; start here today, no blockers.
2. **bulnao.government.bg audit PDFs** — independent narrative confirmation of where Nessebar's own reporting has gaps/violations (explicitly documents a multi-year audit gap and "qualified opinion" findings) — good ground truth for what "suspicious/missing" looks like.
3. **os-nessebar.eu council decisions/protocols** — budget adoption/amendment trail; cross-reference decision dates against execution-report changes.
4. **minfin.bg municipal reports** — valuable comparison baseline across all municipalities, but currently Cloudflare-blocked; defer until a Playwright-capable environment is available.

**Overall first move**: start ingesting `nesebar.bg/reports.html` (zero blockers, structured, long history) while, in parallel, spending one browser-based session to unlock `service.eop.bg` request schema and `sigma.midt.bg`'s API — those two unlock the procurement side.
