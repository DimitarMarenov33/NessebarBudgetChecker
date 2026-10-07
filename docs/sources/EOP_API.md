# ЦАИС ЕОП — service.eop.bg API (captured via Playwright, 2026-10-06)

Captured by driving the real UI at `https://app.eop.bg/today` and `https://app.eop.bg/today/reporting`
with Playwright + Chromium (headless) and recording every XHR to/from `service.eop.bg`. All methods below
were **observed firing from the live UI**, then **replayed standalone with `httpx`** (no cookies, no
browser) — see `data/samples/eop_*.json` for the full saved responses. Raw capture (request+response,
first ~2-8KB of body) is in `docs/sources/eop_api_capture.jsonl` (282 lines).

General transport notes:
- All methods are `POST https://service.eop.bg/NX1Service.svc/<MethodName>`, `Content-Type: application/json; charset=UTF-8`.
- No auth header, no cookie required for any read-only/public method tested. CORS is wide open (`Access-Control-Allow-Origin: *`).
- Useful but non-essential headers the Angular app sends: `X-Requested-With: XMLHttpRequest`, `Referer: https://app.eop.bg/`, `Accept-Language: bg-BG`. None of these were required for the httpx replay to succeed — a plain `User-Agent` + `Content-Type` was sufficient.
- Dates are .NET JSON dates: `"/Date(1789678800000+0300)/"` (epoch ms, optional UTC-offset suffix).
- No rate limiting observed: ~15-20 calls over a few minutes from one IP, no 429s, no slowdown. Still, be polite (the task's own ≤1 req/sec guidance was followed).

## Methods

### 1. `GetContractingAuthoritySearchResult` — buyer/organization lookup
Request:
```json
{"request":{"ActivityTypeGroup":null,"ContractingAuthorityTypeId":null,
  "ContractingAuthorityName":"Несебър","BatchNumber":null,"Status":"2",
  "ActivityTypeId":null,"HierarchyType":null,"ExecutionRegion":null,"NutsCode":null,
  "RegistryNumber":null,"BulgarianBudgetCode":null,
  "StartIndex":1,"EndIndex":10,"OrderColumn":"BatchNumber","OrderAscending":true}}
```
- `ContractingAuthorityName` is a **single-token substring/contains match**, not a phrase match — searching
  `"Община Несебър"` (two tokens) returned 274 unrelated "ОБЩИНА *" results (looks like only the first/most
  common token was effectively used), while searching `"Несебър"` alone correctly narrowed to 3 results.
  **Lesson: search by the distinguishing place name only, not the full legal form.**
- `Status:"2"` = "Активен" (active) filter as set by the UI default; meaning of other values unconfirmed (guess: "1"=all/inactive, untested).
- Pagination: `StartIndex`/`EndIndex`, 1-based, page size = `EndIndex-StartIndex+1`.
- Response: `{"CurrentPageResults":[...], "ResultsCount": N, "HasMoreResults": null}`. Each result:
  `BatchNumber` (buyer's "партида" number), `City`, `ContractCount`, `TenderCount`, `PreliminaryNoticeCount`,
  `PublishedNoticeCount`, `OrganizationGuid`, `OrganizationId`, `OrganizationName`, `RegistryNumber` (EIK/БУЛСТАТ).
- **Община Несебър** result: `OrganizationId=28046`, `OrganizationGuid="6d677995-126f-4bc1-bdec-4ba334fb6026"`,
  `RegistryNumber="000057122"` (EIK), `BatchNumber=126`, `ContractCount=413`, `TenderCount=410`, `PublishedNoticeCount=123`.
  Saved: `data/samples/eop_org_nesebar.json`.

### 2. `GetProcurementsByOrganization` — procedures/tenders list for one buyer
Request: `{"request":{"OrganizationId":28046,"StartIndex":1,"EndIndex":10}}` (uses **OrganizationId**, the integer, not the GUID).
Response fields per item: `TenderId`, `PublishedTenderId`, `SpecialNumber` (e.g. `00126-2026-0057`, AOP-style
case number), `TenderName`, `ProcedureType` (Bulgarian label, e.g. "Открита процедура"), `TypeOfContract`
(1=services?/2=supplies?/3=works? — inferred from co-occurring subjects, not confirmed from an enum call),
`EstimatedValue`, `EstimatedValueMin/Max`, `Currency` (int code, see RetrieveCurrencies: 1=EUR, 2=USD, 3=BGN…),
`IsEUFinanced`. No pagination total count was inspected (would be `ResultsCount` by analogy). Saved:
`data/samples/eop_procedures_nesebar_page1.json`.

### 3. `GetContractsByOrganization` — signed-contracts list for one buyer
Request: `{"request":{"ParticipantGuid":"6d677995-126f-4bc1-bdec-4ba334fb6026","StartIndex":1,"EndIndex":10},"role":1}`
(uses **ParticipantGuid** = the org's `OrganizationGuid`, NOT its numeric id; `role:1` = buyer-side role, untested with other values).
Response: `{"CurrentPageResults":[...], "ResultsCount":413, "HasMoreResults":null}`. Each item is one signed
contract: `ContractNumber`, `ContractDate`, `ContractValue`, `ContractValueEuro`, `CurrentContractValue`
(post-annex value, same as ContractValue if no annex), `Currency` (int), `ContractSubject`, `SupplierName`,
`RegisterNumberList` (**contractor's EIK/БУЛСТАТ** — confirmed matches the supplier across endpoints),
`TenderId`, `TenderNumber`, `TenderPublicationId`, `ContractReportId`, `ProcedureType` (English enum name
here, e.g. `"OpenProcedure"`, `"PublicCompetition"`, `"CollectingOffersWithNotice"`), `TypeOfContract` (int:
1/2/3 seen), `IsFrameworkAgreement`, `AwardedToGroup`. **No CPV field.** Saved (10 of 413 rows, 42 pages
total at page size 10): `data/samples/eop_contracts_nesebar_page1.json`.

### 4. `GetPublishedTenderDetails` — one procedure's full detail
Request: `{"tenderId":<TenderId>,"ianaTimeZone":"Europe/Kiev"}` — **the key must be the numeric `TenderId`
field (e.g. 595341), NOT the `PublishedTenderId`** (e.g. 213020) that appears in the same list rows and in
the `/today/<id>` route for *currently-open* tenders — calling with a `PublishedTenderId` silently returns
an **empty 200 response** (confirmed: both via browser capture and via a direct httpx call). The Angular
route `https://app.eop.bg/today/<TenderId>` (e.g. `https://app.eop.bg/today/605199`) is the human-readable
page and happens to also pass that same numeric id straight through as `tenderId`, so route id and API id
coincide — just make sure whichever id you harvest from a list is the `TenderId` field, not `PublishedTenderId`.
Response (456KB for a real case): no `EstimatedValue`→ yes `EstimatedValue`/`EstimatedValueMin/Max`,
`CurrencyType` (int), `OrganizationId`/`OrganizationName`, `SpecialNumber`, `TenderName`, `TenderDescription`
(a short, sometimes truncated-mid-sentence summary — e.g. tenders 331423 (ids 644-646) all stop at exactly
212 characters, ending "...по обособени позиции:"; this is the raw API field's own shape, not a capture
artifact of this project), `ProcedureType` (int code here, differs from the string enum used elsewhere),
`TypeOfContract`, `PublicationDate`, `ContactPersonDisplayName/Email/Phone`, `TenderDescriptionDocuments`
(attachment list w/ `DocumentId` for downloads), `TenderPublicationDetails[]` (one per official notice --
решение / обявление за поръчка / обявление за възложена поръчка / ... -- each with an `HtmlPreview` field
containing the rendered Bulgarian-language notice as a full, self-contained HTML page, ~55-250KB).

**Update (2026-10-07): CPV *is* present in `HtmlPreview`, correcting the original note below that it
"did not appear at all" in tender 595341.** That original finding was from eyeballing one entity-escaped
HTML dump and missed it; `scrapers/eop.py`'s `extract_notice()` (added this session) now parses it
reliably. See "Notice-HTML structure" below for the field layout, and `docs/RULES.md`'s MissingQuantityRule
section for how this is used. Saved: `data/samples/eop_procedure_detail_595341.json`.

#### Notice-HTML structure (`TenderPublicationDetails[].HtmlPreview`)

Two distinct markup generations were found across the ~410 cached tenders (`PublicationFormType` on the
publication entry tells them apart):

- **Legacy / ЗОП forms** (`PublicationFormType` 2/3/14/30/32/33/34/35/36/37/39/40/41/42 -- the Bulgarian-only
  forms, e.g. 2="Обявление за поръчка", 3="Обявление за възложена поръчка", 32="Решение по чл. 22, ал.1 от
  ЗОП"). Laid out as a sequence of `<tr>` rows: a "header" row has a `<td class="first__coll">` holding the
  field's roman/arabic code (e.g. "II.1.4)") whose sibling `<td>` holds a `<div class="section__name">` (the
  field's Bulgarian label, e.g. "Кратко описание", "Основен CPV код", "Описание на обществената поръчка").
  The row(s) immediately after, up to the next header row, hold the value(s): either one or more sibling
  `<div class="name">` elements in one `<td>` (a free-text value, sometimes split across several divs --
  one per paragraph/bullet -- for a long multi-paragraph description; naively reading only the first
  `div.name` silently truncates it), or a `<div class="label__name">`+`<div class="name">` pair (a named
  sub-field, e.g. "Стойност, без да се включва ДДС:" / "7330000" then "Валута:" / "BGN"). A multi-lot
  procedure repeats the whole "II.2) Информация за ОП (N)" block once per обособена позиция (lot) --
  confirmed up to 7 lots in one notice (tender 266101).
- **eForms / EU-standard forms** (`PublicationFormType` 54/60/65/72/74/76/78 -- the newer BT-coded forms,
  e.g. 54="Обявление за поръчка – Общата директива, стандартен режим", 65="Обявление за възложена поръчка –
  Общата директива, стандартен режим"). Reuses the *same* row/div markup, but every field is a labeled
  sub-field with its BT code folded into the label text itself, e.g. `<div class="label__name">Описание
  (BT-24-Procedure)</div>`. There is **no literal "CPV" text anywhere** in this generation; the CPV field is
  instead "Основна класификация(BT-262-...)" / "Допълнителна класификация(BT-263-...)", whose value is
  `"<8-digit code> - <description>"` (e.g. "34350000 - Външни гуми с лек и тежък режим на експлоатация").
  Multi-lot procedures repeat the procedure-level fields once more per lot, suffixed `-Lot` instead of
  `-Procedure` (e.g. "Описание(BT-24-Lot)").

Both generations share: the notice's own heading/title (e.g. "Обявление за възложена поръчка") is a single
`<p class="header__form">` near the top of the page. Key fields and their (generation-specific) labels:

| Field | Legacy label (code) | eForms label |
|---|---|---|
| Notice type | `p.header__form` text | same |
| Main CPV | "Основен CPV код" (II.1.2 / IV.2) | "Основна класификация(BT-262-Procedure)" |
| Additional CPV | "Допълнителни CPV кодове" (II.2.2, per lot) | "Основна класификация(BT-262-Lot)" / "Допълнителна класификация(BT-263-...)" |
| Short description | "Кратко описание" (II.1.4) | "Описание(BT-24-Procedure)" |
| Per-lot description | "Описание на обществената поръчка" (II.2.4, repeats per lot) | "Описание(BT-24-Lot)" (repeats per lot) |
| Estimated value | "Прогнозна обща стойност" (II.1.5) / "Прогнозна стойност" (II.2.6, per lot) -- split into a "Стойност.../Валута" sub-pair | "Прогнозна стойност, без да се включва ДДС(BT-27-Procedure/-Lot)" -- one combined value |
| Awarded contractor (award notices only) | "Наименование и адрес на изпълнителя" (V.2.3), incl. EIK | (not parsed by this project -- `GetContractsByOrganization`/`GetPublishedContractListItems` already give the contractor+EIK directly) |
| Awarded value (award notices only) | "Информация относно стойността на поръчката/обособената позиция" (V.2.4) | (same -- not parsed; redundant with `ContractValue`) |

`scrapers/eop.py`'s `extract_notice(html)` parses both generations label-by-label (not CSS position) into
`{notice_type, cpv_main, cpv_codes, short_description, lot_descriptions, estimated_value, full_text}`; see
its module-level docstring for the full extraction algorithm. `_build_notices()`/`_tender_detail_with_notices()`
turn a tender's full `TenderPublicationDetails[]` into the `notices`/`notice_text` fields stored in
`raw_json.tender_detail` (full_text itself is never persisted — see docs/RULES.md).

**Honest caveat found while building this:** not every notice states a quantity even once the full text is
read -- e.g. tenders 331423 (ids 644-646, fuel bought via commodity exchange) and 526402 (id 450, tires)
both genuinely lack an explicit purchased quantity anywhere in their notice text (the former only ever
repeats the lot's fuel-type name; the latter's own text says the quantities are "в Таблица, неразделна част
към техническата спецификация" -- in an attached technical-specification table this project doesn't
download). See `docs/RULES.md`'s MissingQuantityRule section for the full writeup, including a newly
observed precision cost: the fuller text also surfaces technical *specifications* (seat counts, tank/body
volumes, throughput rates, equipment dimensions) that the rule's number+unit pattern can't distinguish from
a genuine purchased-quantity statement.

### 5. `GetPublishedContractListItems` — the procedure's "Договори" (contracts) tab
Request: `{"tenderId":<TenderId>,"ianaTimeZone":"Europe/Kiev"}`. Response: `{"ContractListItems":[...],"Lots":[]}`.
Each contract item: `Id` (=contract number, matches `ContractNumber` from method 3), `MainTenderId`, `Subject`,
`Value`, `CurrentContractValue`, `Currency`, `StartDate`/`EndDate`/`CurrentStartDate`/`CurrentEndDate` (often
null if not separately tracked), `StatusChangedDate`, `ExportDocumentId`, `ContractSuppliers[]` → each has
`OrganizationName` and `RegistryNumber` (**contractor EIK**), `ContractId`. `Annexes[]` would list contract
amendments (empty in the sample). Saved: `data/samples/eop_contract_detail_595341.json`.

### 6. `GetPublishedTenderExportsByTenderId` — full-dossier ZIP export
Request: `{"tenderId":<TenderId>,"ianaTimeZone":"Europe/Kiev"}`. Response lists downloadable export packages,
e.g. `{"Id":494459,"Name":"T595341-Експорт-20260713.zip","DocumentId":57856870,"IsFullExport":true,...}`.
**Not downloaded this session** (time budget). CPV no longer needs this -- it's reliably readable straight
out of `TenderPublicationDetails[].HtmlPreview` (see method 4's "Notice-HTML structure" above) -- but this
ZIP remains the only known place to find quantities that a notice itself defers to an attached technical
specification (confirmed for EOP id 450, see docs/RULES.md), since this project doesn't parse ZIP contents.

### 7. `GetPublishedTendersBySpecified` — the public "Регистър на обществените поръчки" listing (today/open tenders), not org-filtered
Request shape (from the landing page's default "отворени за участие" filter): `{"searchParameters":{"StartIndex":1,"EndIndex":10,"PropertyFilters":[],"SearchText":"","SearchProperty":{"PropertyDisplayName":"str_Today_opened","PropertyName":"Status","PropertyValue":"1"},"OrderAscending":false,"OrderColumn":"PublicationDate","Keywords":[]}}`.
This is the generic "all tenders" feed (1503 open tenders site-wide at capture time) and does **not** take an
organization filter directly in the parameters observed — organization-scoped results come from methods 2/3
above instead (reached via the Справки → Възложители search UI, not the home register page).

## How to filter by organization, end to end
1. `GetContractingAuthoritySearchResult` with `ContractingAuthorityName:"Несебър"` → get `OrganizationId=28046` and `OrganizationGuid`.
2. `GetProcurementsByOrganization` with that `OrganizationId` → paginate through all 410 procedures (`EstimatedValue`, `CPV` absent).
3. `GetContractsByOrganization` with that `OrganizationGuid` → paginate through all 413 signed contracts (`ContractValue`, `SupplierName`, `RegisterNumberList`=EIK, dates).
4. For any interesting `TenderId` from step 2/3, call `GetPublishedTenderDetails` (full text/description/documents) and `GetPublishedContractListItems` (contract value + contractor EIK again, cross-checkable against step 3).

## Field → meaning quick table
| Field | Meaning |
|---|---|
| `ContractValue` / `Value` | Contract value **excl. currency conversion drift**, in the currency given by `Currency` |
| `CurrentContractValue` | Value after annexes/corrections (same as above if none) |
| `Currency` | int code: 1=EUR, 2=USD, 3=BGN (from `RetrieveCurrencies`) |
| `EstimatedValue` | Pre-award estimated/budget value of the procedure |
| `RegisterNumberList` / `RegistryNumber` on a supplier | Contractor's EIK/БУЛСТАТ |
| `SupplierName` / `ContractSuppliers[].OrganizationName` | Contractor's registered name |
| `SpecialNumber` / `TenderNumber` | AOP-style case number, e.g. `00126-2026-0057` (`00126` = Несебър's permanent AOP buyer code) |
| `ContractDate` / `PublicationDate` / `StatusChangedDate` | .NET epoch-ms dates |
| CPV | **Not present as a top-level JSON field anywhere.** Reliably readable from `TenderPublicationDetails[].HtmlPreview` (method 4's "Notice-HTML structure") via `scrapers/eop.py`'s `extract_notice()` -- corrects this doc's original claim (below the 2026-10-07 note in method 4) that it "did not appear at all" in the one notice first inspected by eye. |

## Human-readable page
`https://app.eop.bg/today/<TenderId>` — e.g. https://app.eop.bg/today/605199 (open procedure) and
https://app.eop.bg/today/595341 (awarded, with a signed contract) both render correctly client-side.

## Not done / open questions
- Did not confirm the enum meaning of `TypeOfContract` (1/2/3) or the int `ProcedureType` codes used in
  `GetPublishedTenderDetails` (a different code space than the string `ProcedureType` in method 3) — would
  need a `RetrieveXxxTypes`-style lookup call, not located this session.
  the `GetContractingAuthoritySearchResult`'s `Status` field values beyond `"2"` are guesses, not tested.
- Did not download a `GetPublishedTenderExportsByTenderId` ZIP -- no longer needed to verify CPV presence
  (resolved, see method 4), but still open as the only known way to reach a quantity a notice defers to an
  attached technical specification (e.g. EOP id 450).
