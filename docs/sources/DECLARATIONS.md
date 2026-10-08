# Anti-corruption declarations — os-nessebar.eu (fetched 2026-10-08)

Source: `https://os-nessebar.eu/deklaracii-po-zpk` (Общински съвет Несебър's "Декларации по ЗПК" section) and its КПКОНПИ archive, `https://os-nessebar.eu/deklaracii-po-kpkonpi-arhiv`. Scraper: `src/nessebar_budget/scrapers/declarations.py` (`DeclarationsScraper`). Table: `Official` (`src/nessebar_budget/db/models.py`).

`robots.txt` (`https://os-nessebar.eu/robots.txt`) is `User-agent: * / Disallow:` — nothing disallowed, no `Crawl-delay`. This scraper still applies a flat 2s delay between every request (`DeclarationsScraper(delay=2.0)` default).

## Pages found

The index page and the КПКОНПИ archive page each link a handful of register sub-pages (URL fragment `/publichen-registar-na-deklaraciite-...`); `list_register_pages()` discovered exactly 6, all reachable with plain `httpx.get` (server-rendered HTML, no JS needed):

| # | URL (host omitted) | Legal basis / mandate | Role(s) on page |
|---|---|---|---|
| 1 | `/deklaracii-po-zpk/...-za-obshtinski-savetnici-mandat-2023-2027` | ЗПК чл.49 ал.1 т.1/т.3 (несъвместимост), 2023-2027 | councillors |
| 2 | `/deklaracii-po-zpk/...-kmetove-na-kmetstva-mandat-2023-2027` (имущество и интереси) | ЗПК чл.49 ал.1 т.2/т.4, 2023-2027 | village mayors only |
| 3 | `/deklaracii-po-zpk/...-kmet-na-obshtina-kmetove-na-kmetstva-mandat-2023-2027` | ЗПК чл.49 ал.1 т.1/т.3 (несъвместимост), 2023-2027 | municipal mayor + village mayors |
| 4 | `/deklaracii-po-kpkonpi-arhiv/...-za-obshtinski-savetnici` | ЗПКОНПИ чл.35 ал.1 т.1/т.3, 2019-2023 | councillors |
| 5 | `/deklaracii-po-kpkonpi-arhiv/...-kmetove-na-kmetstva` (имущество и интереси) | ЗПКОНПИ чл.35 ал.1 т.2/т.4, 2019-2023 | village mayors only |
| 6 | `/deklaracii-po-kpkonpi-arhiv/...-kmet-na-obshtina-kmetove-na-kmetstva` | ЗПКОНПИ чл.35 ал.1 т.1/т.3, 2019-2023 | municipal mayor + village mayors |

Full URLs are in `DeclarationsScraper.list_register_pages()`'s source and in `data/cache/declarations/html/` (cached HTML of every page fetched, gitignored).

Each page's content is one `<p><strong>...</strong></p>` title (states the legal citation and mandate) followed by one `<p><a href=".../assets/upload/files/<transliterated-name>.pdf">Име Презиме [Фамилия]</a></p>` per person. **The Cyrillic name is always the link text, never the filename** (filenames are ad hoc transliterations, inconsistent in casing/suffixing between pages and mandates). Page 3/6 (the combined "кмет на община + кметове на кметства" pages) are the only ones where a row's link text also carries a role suffix ("... - Кмет на община Несебър" / "... - Кмет на кметство с.Х"); this is the only place role is determined per-row — everywhere else it is a page-level default inferred from the URL slug.

### Not scraped: `deklaracii-po-zpuki-arhiv`

The index page also links a much older archive, `https://os-nessebar.eu/deklaracii-po-zpuki-arhiv` ("Декларации по ЗПУКИ — архив", the pre-КПКОНПИ law, mandate "2015"). It was fetched and inspected, but deliberately **not** scraped into `officials`:

- It is a single flat page (no register sub-pages), with ~90 PDF links under two headings repeated twice ("ЗПУКИ - чл.12, т.1" / "т.2") that correspond to incompatibility vs. change-of-circumstances filings, not to a role split.
- There is no per-row or per-section signal that distinguishes councillors from (if any) mayors — every name sits in one undifferentiated list, including several entries that are clearly already-covered councillors re-filing a 2-page amendment ("Николай Димитров 2стр.", etc.).
- Guessing a role for ~90 names with no actual signal would violate this scraper's precision-over-recall stance (`role` would be fabricated, not inferred), so this page is out of scope. Its existence is recorded here for completeness; a future pass could scrape it with `role="other"` if that limitation is acceptable.

## Officials stored (real run, 2026-10-08)

`scrape declarations` fetched **72 person-rows** off the 6 register pages and upserted them into `Official`, keyed on `(name_normalized, role, mandate)`; **69 distinct officials** ended up in the table (3 raw rows collapsed into an already-seen identity — see "Known duplicates" below).

| role \ mandate | 2019-2023 | 2023-2027 | total |
|---|---:|---:|---:|
| councillor | 24 | 21 | 45 |
| mayor | 0 | 1 | 1 |
| village_mayor | 7 | 8 | 15 |
| other | 8 | 0 | 8 |
| **total** | **39** | **30** | **69** |

- **`mayor`** (1): the current municipal mayor (Николай Кирилов Димитров, 2023-2027) — his role was read off the per-row "- Кмет на община Несебър" suffix on register page 3.
- **`other`** (8, all 2019-2023): the 2019-2023 municipal-mayor-plus-village-mayors incompatibility register (page 6) has **no per-row suffix at all** (unlike its 2023-2027 counterpart) — its 8 rows (the mayor + 7 village mayors) carry no reliable per-row role signal, so the page-level default applies. Because the URL contains both `kmet-na-obshtina` and `kmetove-na-kmetstva`, `_default_role_from_url` can't safely pick `mayor` or `village_mayor` for the whole page either, so these 8 rows are `other` rather than a guessed role. The same 7 village mayors' *asset/interest* declarations (page 5, which is village-mayors-only) **are** correctly tagged `village_mayor` — see below.

## Known duplicates / upsert precedence

Two situations produce more than one register row for the same person:

1. **Same page, two filings.** Example: "Николай Илиев Пандазиев" (village mayor, с.Баня, mandate 2023-2027) has two PDF links on the asset/interest register (an original + an amendment). `parse_register_page` returns both rows; `upsert_official` applies the last one in page order, so the amendment's document wins.
2. **Same identity, two register types, same mandate.** The 2023-2027 village mayors are published on both the incompatibility register (page 3, mixed with the mayor) and the asset/interest register (page 2, village-mayors-only) with the *same* `role`/`mandate`, so they share one `Official` row. `list_register_pages()`'s processing order (`_register_sort_key`) puts "имущество и интереси" (asset/interest) pages *after* "несъвместимост" (incompatibility) ones within a mandate, specifically so the asset/interest document — the type that can actually carry company holdings — is the one left standing. Confirmed in the real run: Пандазиев's stored `document_url` is `.../deklaracii%20nesyvmestimost/8.pdf` (the asset/interest filing), not the incompatibility one.

This means **a person's `document_url` is not necessarily their only published declaration** — just the one judged more likely to carry parseable interests. Both of a person's source pages remain visible in `data/cache/declarations/html/` for manual cross-checking.

## PDF text vs. scanned

**69 PDFs fetched, 0 with a text layer, 69 `document_status="scanned"`.** Every individually signed declaration PDF sampled across both mandates and all three register types — including ones with distinct filename conventions (`name.pdf`, `Name_o.pdf`, `Name_k.pdf`, `img-*.pdf`) — came back as a scanned image with zero extractable text from both pdfplumber and the pypdf fallback. `declared_interests_json` is therefore `[]` for all 69 stored officials; **0 declared interests were parsed from real data.**

One *non-person* PDF on the archive pages (`KmetoveRegistar____.pdf`, a consolidated register listing, correctly excluded from `officials` by the "looks like a person row" filter) *does* have a text layer — it was useful for confirming pdfplumber works end-to-end against this site's PDFs, but it's a table of names/dates/filing types, not a filled declaration (no company/EIK content), so it wasn't usable to validate `parse_declaration_text` against a real example.

Because no real filled-and-digitized declaration was available, `parse_declaration_text` is exercised in `tests/test_declarations.py` against a **synthetic** fixture (`data/samples/declarations/declaration_text_sample.txt`) written in the standard form's own section wording (participation in commercial companies / debts over 5 000 BGN / contracts tied to decision areas). Three example extractions from that fixture:

```json
{"company_name": "Слънчев бряг турс ЕООД", "eik": "147025511",
 "relation": "едноличен собственик; управител",
 "raw": "1. „Слънчев бряг турс“ ЕООД, ЕИК 147025511 – едноличен собственик и управител"}

{"company_name": "Инвест Ко ООД", "eik": "1234567891234",
 "relation": "съдружник",
 "raw": "2. „Инвест Ко“ ООД, ЕИК 1234567891234 – съдружник"}

{"company_name": "Техно Билд ООД", "eik": "203456789",
 "relation": "договор с лице, свързано с вземаните решения",
 "raw": "Договор за консултантски услуги с „Техно Билд“ ООД, ЕИК 203456789, от 2022 г."}
```

`parse_declaration_text` only extracts a row from inside one of its three recognised form sections, and only when the line carries a solid signal (a 9/13-digit EIK, one of the form's own relation phrases, or — for the debts section — an amount in лв./EUR); plain section-header/instruction text yields nothing. This is deliberately precision-over-recall: if/when a municipality publishes a born-digital (non-scanned) declaration, or OCR is added upstream, the parser is ready, but it will not fabricate a hit out of boilerplate.

## Parsing caveats / uncertainties

- **Role for archive mayor+village-mayor rows (`other`, 8 rows)**: see above — a real limitation of the 2019-2023 incompatibility register's HTML, not a parsing bug.
- **`mandate` for anything outside the two `мандат YYYY(-YYYY)` pages**: not applicable here (both archive and current pages state their mandate in-page), but if a future register page omits it, `mandate` will be `None`.
- **Filename-vs-name**: confirmed on every page inspected that the link text (not the filename) carries the Cyrillic name; filenames are only used to build `document_url`/the PDF cache path.
- **`declared_interests_json` is empty for every real official** (see above) — this is a statement about what the municipality has published (scanned paper forms), not a gap in the parser.
- **OCR was explicitly out of scope** for this pass (task said: scanned -> `document_status='scanned'`, `declared_interests_json=[]`); revisiting with OCR (e.g. `pytesseract`) is the natural next step if interest data is wanted from these PDFs.
- **`deklaracii-po-zpuki-arhiv` (mandate "2015") is not scraped** — see "Not scraped" above.

## Full-name enrichment (added 2026-10-08, follow-up)

The declarations registers only ever print a **two-part** name ("Георги Георгиев"), which can never equal a Trade Register manager/partner's **full three-part** legal name ("Георги Димитров Георгиев") — so `analysis`'s related-party rule, which requires an exact full-name match, could never fire for any official. `DeclarationsScraper` now also fetches a small, hand-curated list of **composition pages** (`COMPOSITION_SOURCES` in `scrapers/declarations.py`) that print full names, and attaches one to an official when it can be matched unambiguously.

### Composition pages used

| Source | Mandate | Role | Full names found |
|---|---|---|---:|
| `https://os-nessebar.eu/sastav` | 2023-2027 | councillor | 20 |
| `https://os-nessebar.eu/predsedatel` | 2023-2027 | councillor | 1 (the chair) |
| `https://archive.os-nessebar.eu/състав-на-общински-съвет-несебър/` | 2019-2023 | councillor | 20 |
| `https://www.nesebar.bg/mayor.html` | 2023-2027 | mayor | 1 |

No page listing **full** names for village mayors (either mandate) or the 2019-2023 municipal mayor was found on either `nesebar.bg` or `os-nessebar.eu`: `nesebar.bg/structure.html` (admin org chart) and `nesebar.bg/contacts.html` (phone directory) only print role titles ("Кмет на кметство Обзор", ...), never a name next to them; no per-village page was discoverable either. Those officials' `name`/`name_normalized` are therefore left as scraped (their `short_name`/`name_source_url` stay unset) — this is the explicitly-permitted fallback, not a bug.

### Matching algorithm (`resolve_full_name`)

A short name's tokens are split into `(first, rest)` (`rest` is usually one token, but e.g. "Венета Танева-Кючукова" contributes two: "танева", "кючукова"). A composition-page candidate matches if its first token equals `first` *and* its own trailing `len(rest)` tokens equal `rest` — i.e. the middle patronymic(s) are ignored, and a shared surname is disambiguated by the first name (real case found: "Георги Димитров **Георгиев**" vs "Георги Манолов **Манолов**", vs short names "Георги Георгиев"/"Георги Манолов" — each resolves to exactly one, not the other). An official only gets enriched when **exactly one** candidate matches within its own `(mandate, role)` — zero or multiple candidates leaves it unchanged, never a guess. Matching is scoped to `role` as well as `mandate` so a village mayor can never be matched against a councillor candidate that happens to share a first+last name (councillors are the only role with composition pages for both mandates, so this is a real guard, not a theoretical one).

### Results (real run, 2026-10-08)

**35 officials were enriched this run** (`name_source_url` set: 21 of the 2023-2027 councillors via `/sastav` + the chair via `/predsedatel`; 14 of the 2019-2023 councillors via the archive composition page). Counting everyone whose stored `name` is now a clean three-part name (enriched, or already printed in full on the register itself):

| role | mandate | officials | three-part name now | how |
|---|---|---:|---:|---|
| councillor | 2023-2027 | 21 | 21 | enriched via `/sastav` + `/predsedatel` |
| mayor | 2023-2027 | 1 | 1 | already three-part on the register itself (no enrichment needed) |
| village_mayor | 2023-2027 | 8 | 8 | already three-part on the register itself (no enrichment needed) |
| councillor | 2019-2023 | 24 | 14 | enriched via the archive composition page |
| other | 2019-2023 | 8 | 0 | no composition page covers this (mandate, role) |
| village_mayor | 2019-2023 | 7 | 0 | no composition page covers this (mandate, role) |
| **total** | | **69** | **44** | |

**Unmatched** (10, all 2019-2023 councillors — present on the register but either absent from, or not confidently matched on, the archive composition page): Александър Стоянов, Васил Василев, Върбан Кръстев, Златко Димитров, Константин Лефтеров, Михаил Минчев, Румен Кулев - председател, Сотир Наумов, Недялко Йорданов, Виктор Паскалев. None of these were *ambiguous* (more than one candidate matching) — the composition page's 20 names (see table above) simply don't cover all 24 councillors the register lists for that mandate (the page is a later or different snapshot of the same council, e.g. after resignations/replacements); `resolve_full_name` correctly returned `None` for each rather than guessing. "Върбан Кръстев" is a near-miss worth flagging by hand: the composition page lists a "Върбан Валентинов **Христов**" (different surname) for that mandate — left unmatched rather than assumed to be the same person.

No ambiguous (multiple-candidate) matches occurred in this run at all.

### Schema / identity notes

- `Official.short_name` (new, nullable `TEXT`) keeps the original two-part register name once `name`/`name_normalized` are overwritten with the resolved full name; `Official.name_source_url` (new, nullable `TEXT`) records which composition page it came from. Both added via `db.migrate.migrate_officials_table` (idempotent `ALTER TABLE`, same pattern as `migrate_flags_table`, now sharing a small generic `_add_missing_columns` helper).
- `db.repo.upsert_official`'s identity lookup (nominally `(name_normalized, role, mandate)`) is resilient to `name` switching between short and full form across runs: a row matches if *either* its stored `name_normalized` *or* its stored `short_name` (normalized) equals the incoming record's short name. This is what lets a once-enriched row keep being found (and updated in place, not duplicated) on a later run — including one where the composition page is temporarily unreachable; such a run's unresolved short name is never allowed to downgrade an already-enriched full name back to its short form.

## Cache layout

`data/cache/declarations/` (gitignored):
- `html/<slug-prefix>__<sha1-of-url-10-hex>.html` — every register/index page fetched (the full slug is used where short enough to avoid exceeding the filesystem's 255-byte filename limit; the sha1 is for uniqueness, not security).
- `pdfs/<original-filename>.pdf` — every declaration PDF fetched, keyed on its original (already-unique) filename from the site.
