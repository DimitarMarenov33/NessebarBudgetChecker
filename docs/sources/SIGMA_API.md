# SIGMA — sigma.midt.bg (captured via Playwright, 2026-10-06)

SIGMA is a **server-rendered React Router 7 (Remix-successor) app**, not a classic JSON-API SPA. Driving it
with Playwright + network listeners (filtered to any XHR/fetch and to the `__manifest`/`.data` routes)
showed that **page content is not fetched via separate JSON calls** — it's embedded directly in each page's
initial HTML (in a `window.__reactRouterContext.streamController.enqueue(...)` payload using RR7's
custom "turbo-stream" indexed-reference encoding, not plain JSON). The only true XHR/fetch calls observed
were: `/assets/accessibility/translations.json` (static i18n strings) and `/__manifest?paths=...` (a
route-manifest prefetch for client-side navigation, not page data). Raw capture: `docs/sources/sigma_api_capture.jsonl` (32 lines).

**Practical consequence**: there is no separate JSON API to call. The working integration pattern is to
**GET the human page itself** (no cookies/JS required — confirmed via a plain `httpx.get`, see below) and
either (a) parse the visible HTML tables, or (b) use the dedicated **CSV export endpoints**, which is by far
the cleanest option and is documented below.

## Confirmed working URLs (all plain `httpx.get`, no auth, no cookies)
- **Institution/buyer profile**: `GET https://sigma.midt.bg/authorities/<EIK>` — e.g.
  `https://sigma.midt.bg/authorities/000057122` = ОБЩИНА НЕСЕБЪР. Route keys off the **padded national EIK/BULSTAT**
  (not a SIGMA-internal id), so this is directly guessable from any other source that has the EIK — no search needed.
- **Company/contractor profile**: `GET https://sigma.midt.bg/companies/<EIK>`.
- **Contract detail**: `GET https://sigma.midt.bg/contracts/<key>` where `<key>` is a composite id of the form
  `e:<UNP>:<internal-id>:<lot-or-_>:eik:<contractor-EIK>:<seq>` (or `name:<urlencoded name>` instead of `eik:...`
  when the contractor has no EIK on file) — e.g.
  `https://sigma.midt.bg/contracts/e:00126-2026-0042:266822:1:eik:201800812:1`. These keys are only discoverable
  by first loading a listing page (authority/company/contracts) and reading the `href`s — no independent way to
  construct them was found.
- **Contracts list/filter (human page)**: `GET https://sigma.midt.bg/contracts?authority=<EIK>` (also accepts
  `&sector=<2-digit CPV group>`, seen values `09,34,39,45,71,90` for Несебър).
- **Contracts list/filter (CSV export)**: `GET https://sigma.midt.bg/contracts.csv?authority=<EIK>` — **this is
  the single best integration point**: returns the full unfiltered dataset as clean CSV, no pagination needed,
  columns: `id, unp, subject, authority, authority_eik, contractor, contractor_eik, kind, sector_code, procedure,
  signed_at, value_eur, eu_funded, bids_received`. Saved: `data/samples/sigma_contracts_nesebar.csv` (411 rows,
  Несебър's full 2020–2026 contract history, 201KB).
- **Free-text search**: `GET https://sigma.midt.bg/search?q=<text>` (also accepts `single-offer` as a form field
  name seen in the markup, purpose not tested) — confirmed `?q=Несебър` returns 200 with results.
- Saved full page samples: `data/samples/sigma_authority_nesebar.html`, `data/samples/sigma_contract_detail.html`.

## Институция Несебър — key facts shown (from the authority page)
`spentEur: 77,876,484.83` (общо 2020–2026) · `contracts: 411` · `suppliers: 154` (distinct contractors) ·
`avgEur: 189,480.50` · `euSharePct: 10.3%` · `avgBids: 2.6` (average number of offers per tender) ·
`period: 2020-10-22 → 2026-09-18` · top CPV sector by spend: CPV 45 (строителни и монтажни работи), 61.6% of spend.

## Risk / "red flag" indicators — what SIGMA actually exposes
**Important correction to the earlier (curl-only) assumption that SIGMA "auto-flags favoritism/inflated
prices."** Its own methodology page (`https://sigma.midt.bg/methodology`, static server-rendered text,
fetched directly) explicitly states:
> "СИГМА е изцяло само за четене: не въвежда нови данни, **не оценява процедурите и не маркира фирми като
> рискови**... Не замества правен или одиторски анализ." (*"SIGMA is read-only: it does not add new data,
> does not evaluate procedures, and does not flag companies as risky... It does not replace legal or audit
> analysis."*)

So there is **no per-institution or per-company "risk score" or named flag** in the UI or in the embedded
data (an unexplained `"suspect":0` integer field *was* found in the raw `__reactRouterContext` payload for
Несебър, but given the methodology's explicit denial of flagging, this looks like an unused/internal schema
field rather than a published indicator — treat as unconfirmed, do not rely on it).

What SIGMA **does** expose, at the aggregate level, is a **single-bid-share statistic**: the home page states
"32,2% от стойността на всички поръчки са с договори с една оферта" (32.2% of total contract value by count
had only one bidder), with a "Поръчки с една оферта" table of the most recent/largest single-bid contracts
site-wide. At the row level, the **CSV export's `bids_received` column is the actual underlying signal** —
any contract with `bids_received == 1` is a single-bid ("no price competition") case; this is the most useful
field for this project's own favoritism/overpricing detection, straight from SIGMA's own cleaned CSV, no
scraping required.

## Licence / terms of use
Footer on every page: **"Източник (CC-BY 4.0): АОП / ЦАИС ЕОП — отворени данни (storage.eop.bg)"** — i.e.
SIGMA's own underlying data is itself sourced from **`storage.eop.bg`**, described as the open-data layer of
АОП/ЦАИС ЕОП (distinct from the `service.eop.bg` JSON-RPC backend documented in `EOP_API.md`) and re-published
under **CC-BY 4.0**. SIGMA itself also links "Отворен код" (open source) at the bottom of every page — the
SIGMA codebase itself appears to be open-source (link not followed this session; worth checking later for a
GitHub repo that may document the data pipeline / turbo-stream format precisely).

## Not done / open questions
- Did not resolve what `storage.eop.bg` itself serves (direct bulk CSV/JSON dumps?) — high-value follow-up,
  likely the actual bulk-export mechanism for all of ЦАИС ЕОП, not just Несебър.
- Did not fetch the "Отворен код" (source code) link target.
- Did not decode the full RR7 turbo-stream format generically (only read it positionally); for anything beyond
  Несебър's one profile, the CSV export is the recommended integration path instead.
