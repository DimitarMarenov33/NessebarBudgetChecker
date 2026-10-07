# FETCH_LOG — raw attempts, 2026-10-06

All requests used UA `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36`, curl, max 1 req/sec per host, `--max-time 15-30`.

| URL | Method | HTTP | Notes |
|---|---|---|---|
| https://www.nesebar.bg | GET | 200 | Loads fine. Legacy site (same codebase as nesebarinfo.com) |
| https://nessebar.bg | GET | 200 | Resolves, same site |
| https://nesebar.bg (no www) | GET | 200 | Same site |
| https://www.nesebar.bg/robots.txt | GET | 200 | `Disallow: /admin/, /wp-admin/`, `Crawl-delay: 10`, else open |
| https://www.nesebar.bg/publicorders.php | GET | 200 | "Обществени поръчки" landing page, 21KB HTML, links out to imeon.bg profile |
| https://nesebar.bg/register.html | GET | 200 | "Публичен регистър" page, 55KB HTML, lists PDF/XLSX registries (construction permits etc.) |
| http://www.nesebarinfo.com/reports.html | GET | 301 → https://www.nesebar.bg/reports.html | Legacy domain redirects into main site |
| https://www.nesebar.bg/reports.html | GET (via -L) | 200 | 539KB HTML. "Отчет за касово изпълнение на бюджета" — monthly XLS/XLSX archive folder `03-2019/`, files from ~2019 through Aug 2026 |
| https://www.nesebar.bg/03-2019/august2026.xlsx | GET | 200 | 89,286 bytes, valid OOXML — downloaded as sample |
| https://www.nesebar.bg/03-2019/B1_2026_8_5206.xls | GET | 200 | 2,542,592 bytes, valid legacy XLS (Composite Document, "Economic Station" author) — downloaded as sample |
| http://www.nesebarinfo.com/profile.php | GET (via -L) | 404 | Redirects to nesebar.bg but page itself 404s; however HTML body still rendered site chrome with a live link to `https://nesebar.imeon.bg/frmAOP.aspx` |
| https://nesebar.imeon.bg/frmAOP.aspx | GET | 200 | Real "Профил на купувача" host — classic ASP.NET WebForms app (`__doPostBack`, ViewState), 47KB |
| https://app.eop.bg | GET | 200 | Angular SPA shell only (runtime/polyfills/scripts/main bundles), 7.5KB |
| https://app.eop.bg/api/organizations/search?name=... | GET | 200 | **False positive** — server returns the same Angular index.html for any path (SPA fallback), Content-Type text/html, not JSON |
| https://app.eop.bg/api/v1/organizations, /backend/api/search, /api/procurement-procedures | GET | 200 (all) | Same SPA fallback, not real endpoints |
| https://app.eop.bg/robots.txt | GET | 200 | Only blocks a fake `/nogooglebot/` path; otherwise `Allow: /` |
| https://service.eop.bg/NX1Service.svc/GetQuickSearchResult | POST, `{"SearchText":"Несебър"}` | 500 | Reachable WCF/JSON service, CORS `*`, returns `{"ErrorCode":4,...}` — wrong param shape, not blocked |
| https://service.eop.bg/NX1Service.svc/GetQuickSearchResult | POST, `{"request":{...}}` and `{"text":...,"pageIndex":0,"pageSize":10}` | 500 | Same — still wrong schema |
| https://service.eop.bg/NX1Service.svc/GetPublicBuyerProfileBasicInformation | POST, `{"Name":"Община Несебър"}` | 400 | Reachable, wrong schema |
| https://www.aop.bg | GET | 200 | Old РОП portal reachable |
| https://data.egov.bg | GET | 200 | Portal reachable |
| https://data.egov.bg/api/3/action/package_search?q=Несебър | GET | 404 | `{"success":false,...,"error":"Непознат метод"}` — this is NOT a CKAN instance (contrary to initial assumption) |
| https://data.egov.bg/api/3/action/site_read | GET | 404 | Same "unknown method" error |
| https://data.egov.bg/api/listDatasets | GET | 404 | Same error — guessed Laravel-style path also wrong; real method names/base unconfirmed |
| https://www.minfin.bg | GET | 403 | Cloudflare JS challenge ("Just a moment...") |
| https://www.minfin.bg/bg | GET (w/ Accept-Language) | 403 | Same Cloudflare block |
| https://www.minfin.bg/bg/810 | GET | 403 | Same — "Финансови показатели на общините" page blocked |
| http://www.minfin.bg | GET | 301 → https (still blocked after redirect, not followed further) | |
| https://www.bulnao.government.bg | GET (-L) | 200 (via 302 redirect to /bg/) | Fully open |
| https://www.bulnao.government.bg/bg/oditna-dejnost/dokladi-obshini/ | GET | 200 | 292KB listing page |
| https://www.bulnao.government.bg/bg/oditna-dejnost/finansovi-oditi-na-gfo/finansovi-oditi-na-gfo-za-2020-g-obshini/ | GET | 200 | 287KB; contains "Несебър" link row |
| https://www.bulnao.government.bg/bg/documents/12555/GFO_Nesebar_nemod_2020.pdf | GET | 200 | 1,054,898 bytes, valid PDF (16 pages) — **downloaded as sample** |
| https://os-nesebar.bg | GET | 000 (DNS fail) | Does not resolve — wrong guessed domain |
| https://os-nessebar.eu | GET | 200 | **Correct** Общински съвет Несебър domain; has /resheniya, /protokoli-ot-zasedaniya-na-obshtinski-savet, /osporeni-resheniya sections |
| https://sigma.bg | GET | 200 | Generic/unrelated site — NOT the government SIGMA platform |
| https://sigma.midt.bg | GET | 200 | **Confirmed real SIGMA** — "Платформа за прозрачност на обществените поръчки" by Ministry of Innovation & Digital Transformation, 71KB Next.js-style page, no-login per own description |
| https://tenders.guru/bg | GET | 000 (DNS/connect fail) | Not reachable from this network within timeout |
| https://portal.registryagency.bg | GET | 200 | Reachable (Търговски регистър) |
| https://acf.bg | GET | 200 | Антикорупционен фонд reachable |

Not attempted this session (time budget): obshtestveniporachki.bg, opentender.eu, papagal.bg, opencorporates.com, www2.aop.bg open-data CSV/XML export pages, aop.bg/case2.php sample fetch, os-nessebar.eu sub-page fetch, nesebar.bg/news wp-json endpoint probe, SIGMA's underlying API (needs browser devtools — no Playwright venv found at project `.venv`, so JS network-call discovery was not possible in this session).

## Browser pass 2026-10-06

Playwright 1.63 + Chromium (headless), project `.venv` Python, UA
`Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36`,
viewport 1400x1000-1200, locale bg-BG, ~1 action/sec. Full request/response captures in
`docs/sources/eop_api_capture.jsonl`, `docs/sources/sigma_api_capture.jsonl`, `docs/sources/egov_api_capture.jsonl`.

| URL | Method | HTTP | Notes |
|---|---|---|---|
| https://app.eop.bg/today | GET (browser) | 200 | Angular SPA renders "Регистър на обществените поръчки" (1503 open tenders). Fires `GetPublishedTendersBySpecified` etc. on `service.eop.bg` |
| https://app.eop.bg/today/reporting | GET (browser) | 200 | "Справки" tabs: Търсене, Възложители, Стопански субекти, Публикувани поръчки, Публикувани договори, Външни експерти, Бюлетин, Статистики, Отворени данни |
| https://service.eop.bg/NX1Service.svc/GetContractingAuthoritySearchResult | POST (browser-driven, then httpx replay) | 200 | Schema captured; `ContractingAuthorityName:"Несебър"` → OrganizationId 28046, Guid 6d677995-…, EIK 000057122. See EOP_API.md |
| https://service.eop.bg/NX1Service.svc/GetProcurementsByOrganization | POST (browser + httpx replay) | 200 | 410 procedures for Несебър. Replay saved `eop_procedures_nesebar_page1.json` |
| https://service.eop.bg/NX1Service.svc/GetContractsByOrganization | POST (browser + httpx replay) | 200 | 413 contracts for Несебър. Replay saved `eop_contracts_nesebar_page1.json` |
| https://service.eop.bg/NX1Service.svc/GetPublishedTenderDetails | POST (browser + httpx replay, tenderId=213020) | 200 but EMPTY body | Confirmed: route id ≠ API id when PublishedTenderId used instead of TenderId |
| https://service.eop.bg/NX1Service.svc/GetPublishedTenderDetails | POST (httpx replay, tenderId=595341) | 200 | Full detail, 456KB. Saved `eop_procedure_detail_595341.json` |
| https://service.eop.bg/NX1Service.svc/GetPublishedContractListItems | POST (browser + httpx replay, tenderId=595341) | 200 | Contract(s) tab of a procedure. Saved `eop_contract_detail_595341.json` |
| https://service.eop.bg/NX1Service.svc/GetPublishedTenderExportsByTenderId | POST (browser) | 200 | Lists a downloadable full-dossier ZIP (not downloaded) |
| https://app.eop.bg/today/605199 , /today/595341 | GET (browser) | 200 | Human-readable procedure pages render correctly; confirmed URL = `/today/<TenderId>` |
| https://sigma.midt.bg | GET (browser) | 200 | Home page SSR'd (React Router 7); only non-content XHRs are `__manifest` prefetch + static i18n JSON |
| https://sigma.midt.bg/authorities/000057122 | GET (browser, then plain httpx) | 200 | ОБЩИНА НЕСЕБЪР institution profile — direct hit keying off EIK, no search needed. Saved `data/samples/sigma_authority_nesebar.html` |
| https://sigma.midt.bg/contracts?authority=000057122 | GET (httpx) | 200 | Human filter page; revealed CSV export link |
| https://sigma.midt.bg/contracts.csv?authority=000057122 | GET (httpx) | 200 | **411-row full CSV** incl. `bids_received` per contract. Saved `data/samples/sigma_contracts_nesebar.csv` |
| https://sigma.midt.bg/contracts/e:00126-2026-0042:266822:1:eik:201800812:1 | GET (httpx) | 200 | One contract detail page. Saved `data/samples/sigma_contract_detail.html` |
| https://sigma.midt.bg/search?q=Несебър | GET (httpx) | 200 | Confirms `/search?q=` works standalone |
| https://sigma.midt.bg/methodology | GET (httpx) | 200 | States SIGMA does **not** flag companies/procedures as risky; explains single-bid stat; CC-BY 4.0 licence, source storage.eop.bg |
| https://www.minfin.bg/bg/810 | GET (Playwright, headless Chromium) | 403 | Cloudflare "Един момент..." challenge — **headless Chromium itself is blocked**, same as curl |
| scrapling StealthyFetcher → minfin.bg/bg/810 | — | ERROR | `ModuleNotFoundError: No module named 'curl_cffi'` — StealthyFetcher's dependency not installed in this venv; not pursued further per task rules |
| https://data.egov.bg | GET (browser) | 200 | Drupal/Laravel-style portal, not CKAN (confirmed again) |
| https://data.egov.bg/data?q=обществени+поръчки | GET (browser, extra wait) | 200 | 293 results; client-side rendered with several-second delay. Surfaced monthly АОП **OCDS bulk-export dataset** |
| https://data.egov.bg/data/view/30437d16-3fec-4fad-8ac8-1eddee200e7c | GET (httpx) | 200 | Dataset detail: "Автоматично генерирани данни... ЦАИС ЕОП... OCDS", one resource per day |
| https://data.egov.bg/data/resourceView/17936a0c-d6d6-414f-92ac-d535b7ee93ec | GET (httpx) | 200 | One daily resource; reveals `POST /resource/download` form (CSRF-token gated) |
| https://data.egov.bg/api-spetsifikatsiya?section=22 and &item=82 | GET (httpx + browser) | 200 | Mostly placeholder/"missing help page"; real content is a "СВАЛИ API" download button whose target was not resolved |
| https://nesebar.imeon.bg/frmAOP.aspx | GET (browser) | 200 | Buyer-profile filter form loads; clicking „Зареди" (`ctl00$ContentPlaceHolder1$btnLoad`) loads 403 results |
| https://nesebar.imeon.bg/frmAOP.aspx (after Зареди postback) | POST (browser) | 200 | Full listing captured, saved `data/samples/imeon_listing.html`; pagination confirmed as `__doPostBack('ctl00$ContentPlaceHolder1$gvAOP','Page$N')`; outbound AOP links confirmed as `aop.bg/case2.php?mode=show_case&case_id=...` |

## Sub-threshold search 2026-10-07

UA `Mozilla/5.0 (compatible; NessebarTransparencyProject/1.0; civic research; polite crawler)` for nesebar.bg (httpx, ~10s between requests per robots.txt `Crawl-delay: 10`); `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ... Chrome/128.0.0.0` for os-nessebar.eu and data.egov.bg (httpx + Playwright headless Chromium, ~1 req/s).

| URL | Method | HTTP | Notes |
|---|---|---|---|
| https://www.nesebar.bg/robots.txt | GET (httpx) | 200 | Re-confirmed: `Disallow: /admin/, /wp-admin/`, `Crawl-delay: 10` |
| https://www.nesebar.bg/sitemap.xml | GET (httpx) | 404 | No XML sitemap |
| https://www.nesebar.bg/ | GET (httpx) | 200 | Full nav link extraction — no "Декларации"/"Текущи ремонти"/"Договори" top-level item exists |
| https://www.nesebar.bg/sitemap.html | GET (httpx) | 200 | HTML sitemap; same nav set as homepage, nothing beyond it |
| https://www.nesebar.bg/register.html | GET (httpx) | 200 | Full registry list extracted (~18 register categories, ~90 individual file links) — see SUB_THRESHOLD.md for the complete breakdown; no contracts/expense/payments register among them |
| https://www.nesebar.bg/reports.html | GET (httpx) | 200 | 539KB; headings enumerated for all periods 2018-2026; confirmed a distinct annual package per year (`otcheti/1-N.pdf`...`6-N.pdf`: Одитен доклад/Касов отчет/Баланс/Отчет за приходи и разходи/Пояснителни сведения/Обяснителна записка), separate from the monthly `B1`/capital-ledger archive already catalogued |
| https://www.nesebar.bg/otcheti/6-25.pdf | GET (httpx) | 200 | Downloaded — "Обяснителна записка" 2025 annual report. 3pp, Konica Minolta scan, **zero extractable text** (pdftotext empty) |
| https://www.nesebar.bg/otcheti/2-25.pdf | GET (httpx) | 200 | Downloaded — "Касов отчет към 31.12.2025". 3pp, same scanner, zero extractable text |
| https://www.nesebar.bg/otcheti/5-25.pdf | GET (httpx) | 200 | Downloaded — "Пояснителни сведения" 2025 |
| https://www.nesebar.bg/otcheti/1-25.pdf | GET (httpx) | 200 | Downloaded — "Одитен доклад" 2025 (1.06MB) |
| https://www.nesebar.bg/otcheti/6-2024.pdf | GET (httpx) | 200 | Downloaded — "Обяснителна записка" 2024, same scanned/no-text-layer pattern |
| https://www.nesebar.bg/obstinski-predpriqtiq.html | GET (httpx) | 200 | Lists 5 municipal enterprises (БКСО, общински гори, управление на отпадъците, общинско пристанище, звено самоохрана), each linking to a plain description page |
| https://www.nesebar.bg/bks.php | GET (httpx) | 200 | ОП "БКСО" page — mission text only, no own procurement/tenders section, shares main-site nav (Профил на купувача/Търгове) |
| https://www.nesebar.bg/psno.php | GET (httpx) | 200 | ОП "Управление на отпадъците" page — same pattern, no own procurement section |
| https://os-nessebar.eu/robots.txt | GET (httpx) | 200 | `Disallow:` empty — fully open |
| https://os-nessebar.eu/resheniya | GET (httpx) | 200 | Decisions list, page 1 (protocols №20-29, Sep 2025-Sep 2026), paginated 11 pages, no site search endpoint found |
| https://os-nessebar.eu/resheniya?page=2 | GET (httpx) | 200 | Page 2 (protocols №10-19, Sep 2024-Jul 2025) |
| https://os-nessebar.eu/resheniya/resheniya-ot-protokol-2429012026-g | GET (httpx) | 200 | Protocol #24 (29.01.2026) — full text scanned for "бюджет"; only incidental mentions, not the budget-adoption decision; one unrelated PDF attachment (stray-dog program) |
| https://os-nessebar.eu/resheniya/resheniya-ot-protokol-2512032026-g | GET (httpx) | 200 | Protocol #25 (12.03.2026) — references 2026 state budget law *not yet adopted* as of this session date; one unrelated attachment (Приложение №1, РИЕ) |
| https://os-nessebar.eu/resheniya/resheniya-ot-protokol-1528032025-g | GET (httpx) | 200 | Protocol #15 (28.03.2025), sampled as a 2025-budget-adoption candidate — not confirmed as the budget decision; one unrelated attachment (РИЕ placement schemes) |
| https://os-nessebar.eu/search?q=бюджет | GET (httpx) | 404 | No search endpoint |
| https://os-nessebar.eu/search?search=бюджет | GET (httpx) | 404 | No search endpoint |
| https://data.egov.bg/data?q=Община+Несебър | GET (httpx + Playwright rendered) | 200 | "358 намерени" but **zero** occurrences of "Несебър" in the rendered results (only in the echoed search box) — portal falls back to an irrelevant generic list (Сапарева баня datasets) rather than reporting "no results" |
| https://data.egov.bg/data?q=Несебър | GET (Playwright rendered) | 200 | "3 намерени"; reveals organisation facet "Община Несебър (2)", org id `279`, and org profile link |
| https://data.egov.bg/organisation/profile/3f435dd4-818c-4517-b01d-8ae0246f8ef7 | GET (Playwright rendered) | 200 | Org profile page itself renders no dataset list client-side within the wait window |
| https://data.egov.bg/data?q=Несебър&org[0]=279 | GET (Playwright rendered) | 200 | **Confirmed: exactly 2 datasets** — "Регистър на ЮЛНЦ в които участва община Несебър" (`/data/view/b0443df7-43cb-47c2-8778-cc8ff6ad0d6f`) and "Подлежаща за публикуване информация по ЗПКОНПИ в община Несебър" (`/data/view/140e1743-b0f1-4cb5-8603-4f674b1f0fa2`) — neither procurement/budget/contracts-related |
| https://os-nessebar.eu/resheniya (pages 3-11) | not attempted | — | Time budget; would be needed to pinpoint exact 2024/2025/2026 budget-adoption protocol numbers by exhaustive paging since no search exists |
| https://nesebar.imeon.bg/frmAOS.aspx | not fetched | — | Linked from register.html as "Регистър на актове за общинска собственост" / "Регистър на разпоредителните сделки с имоти" — property-transactions register, out of scope for purchases but noted for completeness |
| registri/второстепенни разпоредители с бюджет.PDF | not fetched | — | Listed on register.html as list of second-level budget spending units; title suggests a name list only, not opened this session |

## Budget ordinance search 2026-10-07

Looking for the ЗПФ чл.82(1) municipal budget ordinance (public-information duty / чл.137(6), public hearing on annual report / чл.140(4), capital-programme publication). `.venv/bin/python` + httpx, UA `Mozilla/5.0 (compatible; NessebarTransparencyProject/1.0; civic research; polite crawler)` for nesebar.bg; WebSearch/WebFetch for discovery and os-nessebar.eu.

| URL | Method | HTTP | Notes |
|---|---|---|---|
| WebSearch "Наредба за условията и реда за съставяне приемане изпълнение и отчитане на бюджета Община Несебър" | — | — | Surfaced `https://nesebar.bg/files/Naredba%2012.pdf` as the top hit |
| https://nesebar.bg/files/Naredba%2012.pdf | GET (httpx) | 200 | 180,298 bytes, valid PDF, 19 pages. **Downloaded** as `data/samples/nesebar_naredba_budget.pdf`; text extracted cleanly with pdfplumber (25,929 chars, has a text layer — no OCR needed) |
| https://nesebar.bg/files/Naredba%2012.pdf | HEAD (httpx) | 200 | `Last-Modified: Mon, 14 Oct 2013 18:35:40 GMT` — file untouched since 2013 despite citing the pre-2013 ЗОБ (repealed by ЗПФ), not ЗПФ itself |
| https://os-nessebar.eu/naredbi | GET (WebFetch) | 200 | Page 1 of 4 (items 1-10 of 37): Наредби №11, №9, №3, №1, №6, №16, №18 + 2 municipal-enterprise rules. No budget ordinance listed |
| https://os-nessebar.eu/naredbi?page=2 | GET (WebFetch) | 200 | Items 11-20 of 37; no Наредба №12 |
| https://os-nessebar.eu/naredbi?page=3 | GET (WebFetch) | 200 | Items ~21-30 of 37 (roads, housing, culture fund, kindergarten registry, etc.); no Наредба №12 |
| https://os-nessebar.eu/naredbi?page=4 | GET (WebFetch) | 429 | Too Many Requests — not retried this session (politeness budget); items 31-37 unchecked |
| WebSearch "Наредба Несебър бюджетната прогноза три години ЗПФ чл.82" | — | — | No Nessebar-specific forecast ordinance surfaced directly; mostly other municipalities' equivalents |
| https://os-nessebar.eu/resheniya/resheniya-ot-protokol-629032024-g | GET (WebFetch) | 200 | Решение №105 (Протокол №6, 29.03.2024) approves the 2025-2027 three-year budget forecast citing "чл.83 ал.2 от Закона за публичните финанси и Наредбата за условията и реда за съставяне на бюджетната прогноза..." — implies a possibly separate, ЗПФ-based forecast ordinance; number/full text not located this session (open item) |

Deliverable written to `docs/sources/NAREDBA_BUDGET.md`.
