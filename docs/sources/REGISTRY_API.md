# Trade Register (Търговски регистър) — portal.registryagency.bg API (captured 2026-10-08)

Captured by fetching 107 of the 156 distinct real `contractor_eik` values already in this project's
`procurements` table (plain `httpx.get`, no auth, a browser-like `User-Agent` header is enough) and
inspecting every `fieldIdent` that came back. Covers legal forms ООД, ЕООД, АД, ЕАД, and ЕТ (едноличен
търговец, `legalForm == 1`) — see ident `00180` below for ЕТ's own person field. No кооперация/сдружение
contractor was found. Raw cached payloads: `data/cache/registry/{EIK}.json` (gitignored, reused as the
scraper's own cache). Three representative deeds are committed as test fixtures:
`data/samples/registry/deed_102981058.json` (ООД), `deed_102078098_eood.json` (ЕООД),
`deed_102003626_ad.json` (АД) — the ЕТ shape (`00180`) is covered by an inline synthetic fixture in
`tests/test_registry.py` instead (`test_parse_deed_et_sole_trader_uses_00180`), modeled on the real
EIK 121305650 (АНИМАТ - АНГЕЛ АНГЕЛОВ).

## ⚠️ Rate limiting — read before running a bulk fetch

A first exploratory run at the "1 request/second" pace this project uses for every other scraper got
**`429 Too Many Requests` on 99 of 156 requests**, in bursts (a handful succeed, then a batch fails,
repeating) — consistent with a short-window token-bucket limiter, not a hard/permanent IP ban (a single
plain request immediately after the run still got `200`). This matches the task's own explicit "stop and
report if the portal starts returning 403/429" instruction, flagged here accordingly. `RegistryScraper`
(`scrapers/registry.py`) responds to this by:
- defaulting to **2 seconds** between successful requests, not 1;
- treating `429` as retryable (unlike a definitive 4xx, e.g. 404) but backing off much longer than the
  usual `2**attempt` seconds — honoring a numeric `Retry-After` header if the server sends one, else
  `15 * attempt` seconds;
- **not** letting one persistently-throttled ЕИК abort the whole batch — `RegistryScraper.fetch()` catches
  per-ЕИК and records `(eik, reason)` in `self.failures` instead (deliberately different from
  `EopScraper`/`SigmaScraper`'s "let a failure propagate", which only ever deal with one organization).

Even with this, expect some ЕИКs to fail on a full 156-ЕИК run if the throttle window hasn't fully
reset — this is expected, not a bug, and failures are reported rather than silently dropped.

**Production run (2026-10-08, `scrape registry` over all 156 distinct `contractor_eik` values):** 107
deeds fetched and cached, 49 `contractor_eik` values failed:
- **41 `ConnectError: nodename nor servname provided`** (local DNS resolution failures) in one contiguous
  burst partway through the run — confirmed **transient**, not a sustained block: a plain `curl`/`nslookup`
  against the host immediately after the run both succeeded normally. Per the coordinator's instruction,
  these were **not** retried in the same session (the DB was instead rebuilt from the existing 107-deed
  cache without further network calls) — a future run should recover most of them.
- **7 `JSONDecodeError`** ("Expecting value: line 1 column 1" — a 2xx response with an empty/non-JSON
  body, so `raise_for_status()` doesn't catch it; possibly itself a softer rate-limit symptom, not
  confirmed), splitting into three distinct causes:
  - 3 are **not real distinct companies** — `30269049`/`646811`/`694286` are the same three ЕИКs already
    successfully fetched under their correctly zero-padded forms `030269049`/`000646811`/`000694286`
    (`procurements.contractor_eik` holds both a correctly zero-padded and a stripped-leading-zero string
    for at least 3 companies — likely an upstream `eop`/`sigma` formatting inconsistency, not a
    `registry`-scraper bug; the stripped form simply isn't a resolvable ЕИК).
  - 1 is **not a single ЕИК at all** — `"120564924; 200195680"` is a **consortium/joint-venture
    `contractor_eik`**: `procurements` row 591 (source `eop`, `source_id` 157662) literally stores two
    companies' names and ЕИКs joined with `"; "` in one field (`contractor_name = "АРТСТРОЙ ООД; ЕР ДЖИ
    КОНСУЛТИНГ ООД"`) for a jointly-awarded contract. A second such row exists too (`procurements` id 453,
    `"200948893; 204901777"`, `"ДИ ЕНД ЕМ КОНСУЛТ ЕООД; еКЛИМА ЕООД"`) — it happened to fail with
    `ConnectError` instead, during the DNS outage, so it's counted above, not here. Querying the Deeds API
    with a compound string obviously 404s/400s; `distinct_contractor_eiks` doesn't split these (out of this
    task's scope — would need a real decision about how to represent a joint contractor_eik, better made
    where `eop`'s own normalization lives). One of this pair's two members, `120564924` (АРТСТРОЙ), is
    separately already cached and correct; its partner `200195680` is not (see the DNS-outage list above —
    it also appears there as its own standalone failure).
  - 3 are genuinely unexplained: `102819095`, `177476575`, `177489733` — plausible, correctly-formatted
    9-digit ЕИКs that simply got an empty response; worth a plain retry, not investigated further.
- **1 `HTTPStatusError: 429 Too Many Requests`** that exhausted all 4 retries — for `"не се публикува"`
  ("not published"), a **literal placeholder string** sitting in `contractor_eik` for at least one
  procurement row instead of a real ЕИК (another upstream data-quality issue, not fixable here — and not a
  real failure to "fix" by retrying harder, since it was never a real ЕИК to begin with).

Net: of the 49, **2 aren't real/distinct ЕИКs to begin with** (the two consortium strings, one in each
bucket above), **3 are duplicates of an already-cached company** (the zero-stripped trio), and **1 is a
placeholder, not a ЕИК** (`"не се публикува"`) — **43 genuinely distinct, plausibly real companies remain
unfetched** (40 from the DNS outage + the 3 unexplained empty responses), none of them due to the portal's
own rate limiting holding past the end of the run.

## The endpoint

`GET https://portal.registryagency.bg/CR/api/Deeds/{EIK}` → JSON. Top-level:
- `companyName` (str) — clean, no quotes/legal-form suffix, e.g. `"ЕЛЕКТРИКАЛ ГРУП"`.
- `uic` (str) — the ЕИК, echoed back.
- `legalForm` (int) — confirmed from 107 real deeds: `1` = ЕТ, `4` = ООД, `5` = АД, `10` = ЕООД, `11` = ЕАД.
  No other value observed (кооперация, сдружение, etc. are unconfirmed).
- `fullName` (str) — `"<companyName>" <legal form abbreviation>`, e.g. `"ЕЛЕКТРИКАЛ ГРУП" ООД`.
- `deedStatus` (int) — **observed values `1` and `2`, uncorrelated with `legalForm` and, importantly,
  uncorrelated with whether the company is actually still trading**: companies with both values have
  recent (2022–2025) annual financial statements on file. `parse_deed`/`_determine_status` therefore does
  **not** use this field to decide `Company.status` — see below.
- `hasInstructions`/`hasAssignments`/`hasCompanyCasees`/`hasLegalFormChange`/`hasNotifications`/
  `hasForeignParentCompany`/`hasForeignBranches` (bool) — flags for other tabs on the human portal page;
  `hasCompanyCasees` (any attached court case) is true for almost every company and is not a useful
  "trouble" signal by itself.
- `sections[].subDeeds[].groups[].fields[]` — the actual data, as described in the task: each field has
  `fieldIdent`, `htmlData` (an HTML snippet; `_strip_html` tag-strips + unescapes + whitespace-collapses
  it), `fieldEntryDate`, `fieldActionDate`, `recordMinActionDate` (ISO `YYYY-MM-DDTHH:MM:SS`, no timezone
  suffix), `fieldOperation` (int).

**`fieldOperation` meaning (inferred, not documented anywhere official):** confirmed from every sample —
`fieldOperation == 2` always pairs with either empty `htmlData` or literal placeholder text starting with
"Заличен..." (`"Заличено обстоятелство."` / `"Заличена прокура"`); `1` and `3` both carry real, current
content (the distinction between them — amended vs. unchanged-since-registration — doesn't matter for this
project). **Practical rule used throughout `parse_deed`: a field with `fieldOperation == 2` (or whose text
starts with "Заличен") is not current — skip it.** No counter-example exists in the 107-deed sample for the
idents this project actually parses people out of.

## Field ident table

Idents this project's `parse_deed` reads into a `Company`/`CompanyPerson` column, plus every other ident
observed (for completeness — not parsed, but still captured verbatim in `raw_json`).

### Parsed into `Company` columns

| Ident | Meaning | Column | Notes |
|---|---|---|---|
| `00020` | Company name | (via top-level `companyName`, not this field) | Used only as a `_determine_status` scan target. |
| `00030` | Legal form, Bulgarian text | `legal_form` | e.g. `"Дружество с ограничена отговорност"`. Preferred over the `legalForm` int map (int map is only a fallback). |
| `00040` | Latin-alphabet name | — (status scan target only) | e.g. `"ELEKTRICAL GROUP"`. |
| `00050` | Seat/registered address | `seat_address` | `"Държава: БЪЛГАРИЯ Област: ... Община: ... Населено място: ..."`. |
| `00051` | Secondary address (seen on an АД — correspondence address, phone/fax/email/website folded in) | — | Not parsed into a column; informational only. |
| `00060` | Activity (free text, предмет на дейност) | `activity` | Often very long (multi-clause). |
| `00061` | НКИД classification | `nkid_code`, `nkid_label` | `"Група по НКИД: <code> Клас по НКИД: <label>"`. `<code>` is either old-style 4-digit (`4521`) or new-style dotted (`42.99`) — both fit `String(16)`. **Data-quality quirk**: some labels mix real Cyrillic with Latin look-alike letters (e.g. `ИЗГPAЖДAHE` — the `P`/`A` are Latin, not Cyrillic Р/А) — a genuine artifact of the source data, not a parsing bug. |
| `00310` / `00320` | Registered capital | `capital_eur` | Identical in every sample seen (both already in `"NN.NN €"`); `_parse_capital` tries `00310` first. A `"NN.NN лв."` form (pre-euro filings not yet redisplayed in €) is handled defensively — divide by 1.95583 — though no sample actually exercised it (Bulgaria's own euro changeover means essentially everything the live portal serves now reads in €). |
| `10019B` | Announced annual financial statements (ГФО) log | `last_annual_report_year` | Cumulative text, one "Годишен финансов отчет ... Дата на обявяване: ..." entry per filing/year, going back to the company's first filing. `last_annual_report_year` = `max` of every `"Година: NNNN"` match — reliably present for every filing from roughly 2010 onward; a few companies' *oldest* 1–2 entries instead read `"... отчет за NNNNг. Дата на обявяване: ..."` with no `"Година:"` label at all (pre-dating that labelling convention) and are simply not matched — harmless since only the *max* year matters and the max is always a recent, `"Година:"`-labelled entry in every sample seen. |
| every field | `fieldActionDate`/`recordMinActionDate` | `registered_at` | `min()` of `recordMinActionDate` (preferred) or `fieldActionDate` across **every** field in the deed, not just `00010` — in practice `00010` (ЕИК + фирмено дело number) is always among the earliest, but taking the true min across all fields is simpler and never wrong. |

### Parsed into `CompanyPerson` rows (`_PEOPLE_FIELDS` in `scrapers/registry.py`)

| Ident | Role | Legal form(s) seen on | Notes |
|---|---|---|---|
| `00070` | `manager` (управител) | ООД, ЕООД | `"ИМЕ ИМЕ ИМЕ, Държава: БЪЛГАРИЯ"`, possibly several concatenated with no delimiter (see caveat below). |
| `00180` | `sole_owner` (the trader themselves) | ЕТ | ЕТ (едноличен търговец) has no manager/partner/sole-owner-of-capital field at all — the trader *is* the business, named once here instead, e.g. `"АНГЕЛ МАТЕВ АНГЕЛОВ, Държава: БЪЛГАРИЯ"`. Sometimes has no `"Държава:"` clause at all (e.g. `"КОЛЮ ГЕОРГИЕВ БОЖКОВ"` alone) — handled by the no-country fallback below. |
| `00190` | `partner` (съдружник) | ООД | `"ИМЕ, Държава: БЪЛГАРИЯ, Размер на дяловото участие: NNNNN.NN лв."`, repeated per partner — the share-amount text is a clean, unambiguous delimiter between consecutive partners. |
| `00230` | `sole_owner` (едноличен собственик на капитала) | ЕООД | Single name, same `"ИМЕ, Държава: ..."` shape. On an ООД that used to be an ЕООД, this ident is present but `fieldOperation == 2` / `"Заличено обстоятелство."` (correctly skipped — see the `102981058` fixture, which was an ЕООД before becoming an ООД with two partners). |
| `00100` | `board_member` (съвет на директорите) | АД | Seen on one sample as a stale, superseded slot (real names, `fieldOperation == 1`) after the company restructured into the two-tier `00132`/`00140` form below — still parsed (it's current-enough per the `fieldOperation` rule) but will duplicate names already found under `00132`. Harmless (both rows are kept, same `name_normalized`, different `field_ident` — the unique constraint is `(company_id, name_normalized, role, field_ident)` precisely to allow this). |
| `00120` | `board_member` | АД/ЕАД | Seen with a `"Начин, по който се определя мандатът: ..."` free-text prefix before the name list (foreign board, Czech/Serbian nationals) — stripped on a best-effort basis only for the `00132`/`00140` mandate-date prefix (see caveat); `00120`'s differently-worded prefix is **not** specifically stripped, so its first name can come out with leading junk attached. Low-impact (rare ident: 10/107 deeds) and not covered by a required test. |
| `00132` | `board_member` (управителен съвет) | АД (two-tier) | `"Дата на изтичане на мандата: DD.MM.YYYY г. ИМЕ1, Държава: ... ИМЕ2, Държава: ..."` — the shared mandate-expiry-date prefix is stripped by `_MANDATE_DATE_RE` before name-splitting. |
| `00140` | `board_member` (надзорен съвет, supervisory board) | АД (two-tier) | Same shape as `00132`. |

**Person is itself a legal entity — `person_eik` (fixed 2026-10-08):** a sole owner, partner, or board
member can itself be a company/cooperative/government body rather than a natural person, in which case its
own ЕИК/БУЛСТАТ is folded straight into the name text: `"СОФИЯ ФРАНС АУТО, ЕИК/ПИК 040823148, Държава:
БЪЛГАРИЯ, Размер на дяловото участие: 488642.70 €"` (EIK 102045504), `'"ДАРЗАЛА ХОЛДИНГ" АД, ЕИК/ПИК
205127270'` (EIK 130083729, no country clause at all — see below), `"КООПЕРАЦИЯ \"ПАНДА\", ЕИК/ПИК
000885099"` (EIK 131230324), `"МИНИСТЪРА НА РЕГИОНАЛНОТО РАЗВИТИЕ И БЛАГОУСТРОЙСТВОТО, ЕИК/ПИК 831661388,
Държава: БЪЛГАРИЯ"` (EIK 813152902, sole owner is a government ministry). `_split_person_eik` matches
`,?\s*ЕИК(?:/ПИК)?\s+(\d{6,13})` (both the `"ЕИК/ПИК NNN"` and plainer `"ЕИК NNN"` wordings), captures the
digits into `CompanyPerson.person_eik`, and strips the matched substring back out of `name`.

**No "Държава:" at all — single person (fixed 2026-10-08):** some entries are a bare name (or bare
legal-entity name + its own ЕИК) with **no country clause whatsoever** — e.g. EIK 147043618 (АГРО-САМ,
ЕООД): both `00070` (manager) and `00230` (sole_owner) are literally just `"Сабри Исмаилов Алиев"`; EIK
102612748 (ДЕДАЛ, ЕООД): both are `"НАДРИЯНА ГЕОРГИЕВА ВАСИЛЕВА"`. `_PERSON_RE` can't match these at all
(nothing to anchor the country capture on) — **before the fix this silently produced zero
`CompanyPerson` rows**, found by auditing the 107-deed cache after the first production run: 18 companies
(mostly ЕООД) had no people at all despite clearly having a manager/sole owner. `_extract_people` now
falls back to treating the whole (mandate-date-stripped) field text as one person when `_PERSON_RE` finds
zero matches, still running it through `_split_person_eik` first (both fixes compose: the ИНЖКОНСУЛТ
example above has neither a country clause nor a share amount). The same name appearing under two
different idents/roles (e.g. АГРО-САМ's "Сабри Исмаилов Алиев" as both `manager` and `sole_owner`) is
expected and correct, not a bug — the `company_people` uniqueness constraint is `(company_id,
name_normalized, role, field_ident)`, which keeps both rows distinct.

**Concatenation-disambiguation caveat (`_extract_people`'s main residual risk):** the registry has no
delimiter between consecutive people's records beyond the repeating `"NAME, Държава: COUNTRY[, Размер
...]"` shape. When a record is a same-ident historical correction (a real sample: a manager's name entered
once with a typo, `"Румен Стоянов Дивов"`, then re-entered correctly, `"РУМЕН СТОЯНОВ ДИМОВ"`, both still
shown concatenated with no separator) a generic "capture the country" regex is genuinely ambiguous — an
ALL-CAPS corrected name is not distinguishable from a country name by case alone. This project resolves
it by anchoring the country capture to a closed, finite list of ~65 known Bulgarian-language country names
(`_KNOWN_COUNTRIES`) rather than a generic pattern — `re.finditer` then naturally stops each match right
after the recognized country token, so the next match starts exactly where the next person's record
begins. This resolves every case actually observed in this project's samples. **Residual risk (still
open, not fixed):** when **two or more** people are concatenated in one field and **not every one of them**
has its own `"Държава:"` clause, the fallback above only helps when there's *no* country clause anywhere in
the field — if at least one person in the group *does* have one, `_PERSON_RE` finds a match and the
no-match fallback never triggers, so the country-less person(s) before it get swallowed into one merged
name. Two real examples: EIK 000646811 (МОТО-ПФОЕ)'s `00110` representation-manner field — not actually a
`_PEOPLE_FIELDS` ident, so not parsed into people at all, but illustrative of the same underlying shape —
and its `00070` manager field, `"АТАНАС ИВАНОВ ФУРНАДЖИЕВ САТОШИ ИСОГАЙ, Държава: ЯПОНИЯ"`: the first
manager (Bulgarian, no country stated) and the second (Japanese, `Държава: ЯПОНИЯ`) merge into one
`manager` row named `"АТАНАС ИВАНОВ ФУРНАДЖИЕВ САТОШИ ИСОГАЙ"` — accepted as a known limitation, not fixed.
Similarly, one real АД sample (`00140` on `102003626`) has a board member with no `"Държава:"` clause
immediately followed by one that does: `"МИЛЧО СТОЙКОВ КИРЯКОВ НИНА ДОБРЕВА МИГАРОВА, Държава: БЪЛГАРИЯ
Николай Ангелов Георгиев, ..."` merges the first two names. Both degrade to a wrong (merged) `name`
string, never a crash or a silently dropped record, and neither is covered by a required test.

**Status (`Company.status`) is derived from text, not `deedStatus`** (see above) — `_determine_status`
scans only `00020`/`00030`/`00040` (the company's own name/legal-form text) for `несъстоятелност` →
`'insolvency'`, `ликвидация` → `'liquidation'`, `залич` → `'deregistered'`, else `'active'`. Scanning is
deliberately narrow: a broader scan hits real false positives in this project's own samples — e.g. a
standard АД bylaws clause about shareholders' "право на ... ликвидационен дял" (liquidation *share*, a
routine legal term, not a substring of `ликвидация`... but other wordings might collide), and a "прехвърляне
на търговското предприятие" (business transfer) field that names a *different*, unrelated company as
`"... ЕАД - в ликвидация"` (real sample, ident `06010` on EIK `030269049`/ДЖЕНЕРАЛИ ЗАСТРАХОВАНЕ — that
company itself is clearly active). **No real `deregistered`/`liquidation`/`insolvency` company was found
among this municipality's 156 contractors**, so this logic is exercised only defensively, not confirmed
against a real positive case — flagged as an open uncertainty.

### Observed but not parsed (captured verbatim in `raw_json` only)

| Ident(s) | Meaning (best-effort, from context) |
|---|---|
| `00010` | ЕИК + "Фирмено дело" (the old paper-register case number) — used only for `registered_at`. |
| `00012` | Older paper-register filing reference ("номер ..., книга ..., фолио ..."). |
| `00100`/`00110` (when `fieldOperation == 2`) | Historical/superseded board or representation-manner slot. |
| `00110` (when current) | "Начин на представляване" (manner of representation) — free text, e.g. `"по друг начин: <name> ще представлява сам, а <name2> заедно със <name>"`. Not in the structured `"NAME, Държава:"` shape, so not parsed into `CompanyPerson` rows — the task's own instructions only flag `00110` as a possible "Заличено обстоятелство" sentinel, not as a name-list field. |
| `00160` | "Начин, по който се определя срокът" (duration-of-existence clause). |
| `00170` | Public-company notice (ЗППЦК). |
| `00240` | Present but always empty (`None`) in every sample — purpose unconfirmed. |
| `00310`/`00320` sibling `00311` | Share-class rights text (АД) — boilerplate, contains "ликвидационен дял" (see status caveat above). |
| `00330` | "Непарична вноска" (in-kind/apport capital contribution description). |
| `00410`/`00420`/`00430`/`00440` | Procura (прокура, commercial power of attorney) grant/scope/cancellation. |
| `00510`–`00540` | **Branch (клон) record** — a deed for a registered branch, not the parent company: seat (`00510`), branch name (`00511`), branch number (`00512`), activity (`00520`), branch manager (`00530`), representation power (`00540`). Seen on a branch physically located in Несебър (`"ХОУМ СЪРВИС ЕООД – клон Несебър"`). |
| `03000`–`03260` | Insurance/bank-sector-specific disclosure fields (reserve thresholds, license references) — seen only on insurance-sector АД/ЕАД contractors (e.g. ДЖЕНЕРАЛИ ЗАСТРАХОВАНЕ, БУЛСТРАД). Mostly empty/zeroed in the samples seen. |
| `04000`–`04080` | Enforcement/lien actions (съд/съдебен изпълнител, "Вдигнат" = lifted) — also insurance-sector-heavy in the sample, may not be sector-specific in general. |
| `05290`–`05380` | AML/UBO filings under Закона за мерките срещу изпиране на пари (ЗМИП): legal citation, sequence number, and — when the ultimate owner is itself a foreign legal entity — that entity's own name/form/seat. |
| `05500`/`05501` | AML/UBO filing for a **natural-person** ultimate beneficial owner (name, nationality, country of residence, nature of control). Deliberately not mapped to a `CompanyPerson` role — the task's fixed role vocabulary (`manager`/`partner`/`sole_owner`/`board_member`/`representative`/`liquidator`/`other`) has no clean fit, and misclassifying a UBO filing as e.g. a manager would be actively misleading. |
| `06000`–`06020` | "Прехвърляне на търговското предприятие" (transfer of the commercial enterprise) — names the other company in the transfer. |
| `07010`–`07060` | Merger/transformation (вливане/преобразуване) — names the merging/surviving companies and the court filing reference. |
| `10019C`/`E`/`J`/`K`/`L`/`O`/`S`/`Z`, `1001AC`/`AI`/`BH`/`BI`/`BV` | Other announced documents (AGM notices, auditor's reports, management-board reports, capital-reduction resolutions, annual activity reports, merger/conversion reports, etc.) — same "cumulative log of filings" shape as `10019B`, not parsed. |
| `1001AJ` | Current founding act/bylaws document (дружествен договор/учредителен акт/устав) — present in every sample. |
| `X0110` | Cross-reference to a related court filing (входящ/изходящ номер). |

## Other endpoints (reference only, not used by `RegistryScraper`)

- Name search: `GET /CR/api/Deeds/Summary?page=1&pageSize=10&count=1&entryType=0&name=<urlencoded>&includeHistory=false&selectedSearchFilter=1` — not needed here since every contractor's ЕИК is already known from `procurements`.
- Human deed page (stored as `Company.source_url`): `https://portal.registryagency.bg/CR/Reports/ActiveConditionTabResult?uic={EIK}`.

## Production run results (after the `_extract_people`/`person_eik`/`00180` fixes, 2026-10-08)

Re-parsing all 107 cached deeds (no further network calls) with the fixed parser: **107 companies, 343
`company_people` rows, status 100% `active`** (expected — no deregistered/liquidated/insolvent company was
fetched; see below). Legal-form breakdown: 51 ЕООД, 36 ООД, 11 АД, 5 ЕАД, 4 ЕТ. **Every company has at
least one person row** — the earlier "4 ЕТ companies with zero people" finding (from auditing the first
production run, before `00180` was recognized as a people-bearing ident) is resolved: all 4
(`102811636`/МАРАМ, `121305650`/АНИМАТ, `812211358`/БОШКОВ, `830162763`/АНРИ-64) do have a `00180` field —
it just wasn't in `_PEOPLE_FIELDS` yet.

## Open questions / not done

- No actually-deregistered/liquidated/insolvent company among the 156 — `_determine_status`'s non-`active`
  branches are implemented defensively (per the task's own description of the signal) but not verified
  against a real positive case.
- `deedStatus`'s real meaning is unconfirmed (see above) — documented as explicitly *not* a liveness signal
  based on the evidence in hand, but what it *does* encode is still unknown.
- The rate-limit's exact window/threshold wasn't characterized (how many requests, over what period,
  before it clears) — `RegistryScraper`'s 2s delay + long 429 backoff is a reasonable, polite response to
  the evidence gathered, not a precisely-tuned fit to a known limit.
