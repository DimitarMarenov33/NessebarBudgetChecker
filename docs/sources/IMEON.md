# nesebar.imeon.bg/frmAOP.aspx — "Профил на купувача" (captured via Playwright, 2026-10-06)

Classic ASP.NET WebForms app, confirmed working with headless Chromium (no bot-blocking encountered).

## Flow
1. `GET https://nesebar.imeon.bg/frmAOP.aspx` — loads an empty filter form (Номер поръчка, От дата, До дата,
   Вид процедура dropdown, Статус dropdown) plus a `Зареди` ("Load") submit button named
   `ctl00$ContentPlaceHolder1$btnLoad`. No results are shown until this button is pressed (it's a normal
   ASP.NET `<input type=submit>` postback, not a `__doPostBack` JS call — a plain form POST with the standard
   `__VIEWSTATE`/`__VIEWSTATEGENERATOR`/`__EVENTVALIDATION` hidden fields plus the button's `name=value` works).
2. Clicking `Зареди` with all filters empty returns the **full unfiltered list — 403 results** for Община
   Несебър, paginated 10/page via an ASP.NET GridView (`ctl00$ContentPlaceHolder1$gvAOP`). Saved one full
   listing page: `data/samples/imeon_listing.html` (89KB, page 1 of ~41).
3. **Pagination**: later pages are `javascript:__doPostBack('ctl00$ContentPlaceHolder1$gvAOP','Page$<N>')` —
   to automate, POST the form with `__EVENTTARGET=ctl00$ContentPlaceHolder1$gvAOP`,
   `__EVENTARGUMENT=Page$<N>`, plus the current `__VIEWSTATE`/`__EVENTVALIDATION` harvested from the previous
   response (standard WebForms postback pattern — ViewState is not static, must be re-extracted after every
   request).
4. **Per-row detail links** inside the grid, also `__doPostBack` targets scoped per row (e.g.
   `ctl00$ContentPlaceHolder1$gvAOP$ctl02$btnFiles`, `...$btnGaranciiPlashtania`) — "Файлове" (documents) and
   "Гаранции/Плащания" (guarantees/payments) popups per procedure, same postback mechanics as pagination.
5. Each row also carries a **direct outbound link to the official AOP case page**, not a postback — plain
   `<a href="https://www.aop.bg/case2.php?mode=show_case&case_id=<id>" target="_blank">Тук</a>`, confirming
   and sharpening the pattern already noted in `INVENTORY.md` §4 (`case2.php?case_id=...`).

## Listing columns (own, independent source — good cross-check against ЦАИС ЕОП/SIGMA)
`Поръчка №` (case number, e.g. `00126-2011-0003`, matches the `00126-...` AOP buyer-code pattern seen in
`service.eop.bg` data), `Дата`, `Предмет` (subject, free text), `Сума(без ДДС)` (value excl. VAT),
`ДДС` (VAT amount), `Сума общо` (total incl. VAT — **note: this is not present as a split field in the
ЦАИС ЕОП API, useful addition**), `Срок за подаване на оферти` (bid deadline, often "---" for closed/old
cases), `Връзка към сайта на АОП` (the `case2.php` link above), `Статус` (Възложена/Отворена/Затворена =
Awarded/Open/Closed), `Файлове`, `Гаранции/Плащания`.

## Not done / open questions
- Did not replay the postback flow with plain httpx (would need ViewState scraping — mechanically
  straightforward given the above, not executed this session due to time budget).
- Did not open a `Файлове` or `Гаранции/Плащания` popup to see their payload shape.
- Did not test whether `case_id` on the outbound AOP links can be bulk-enumerated/cross-referenced against
  the `TenderId`/`SpecialNumber` values from `service.eop.bg` (would be valuable: this site's case numbers
  going back to at least 2008-2011, well before ЦАИС ЕОП existed, i.e. it covers a longer history than the
  central system).
