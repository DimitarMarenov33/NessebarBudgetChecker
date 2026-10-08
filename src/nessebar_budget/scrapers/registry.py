"""Scraper for the Trade Register (Търговски регистър), portal.registryagency.bg.

Talks directly to the Registry Agency's own JSON backend at
``portal.registryagency.bg/CR/api/Deeds/{EIK}`` (no auth, plain GET, a
browser-like User-Agent is enough) -- see ``docs/sources/REGISTRY_API.md``,
captured by fetching ~110 real distinct ``contractor_eik`` values from
`procurements` (ООД/ЕООД/АД/ЕАД/ЕТ all seen) and inspecting every
``fieldIdent`` that came back.

**Rate limiting**: this endpoint throttles aggressively. A first exploratory
run at the "polite" 1 request/second pace documented for every other scraper
in this project got ``429 Too Many Requests`` on the *majority* of requests
(99/156 in one run), in bursts (a handful of requests succeed, then a batch
fails, repeating) -- consistent with a short-window token-bucket limiter, not
a hard IP ban (a single request immediately after the run still got a plain
``200``). Per this project's own data-collection rules, hitting 429 means
*stop and reassess*, not "retry harder blindly": `_fetch_live` below treats
429 as retryable (unlike a hard 4xx, which fails immediately) but backs off
far longer than the usual ``2**attempt`` seconds -- honoring a numeric
``Retry-After`` header if the server sends one, else a fixed, generous floor
-- and the scraper's own default `delay` between *successful* requests is
2 seconds, not 1. Callers doing a full 156-EIK run should still expect some
EIKs to fail even so; `RegistryScraper.failures` collects them (eik, reason)
instead of letting one persistently-throttled EIK abort the whole batch --
deliberately different from `scrapers.eop`'s "let it propagate" philosophy,
which only ever deals with one organization at a time.

**`deedStatus` (top-level int) is not a reliable liveness signal.** Both
values seen across the sample (``1`` and ``2``) occur for companies that are
obviously still trading (recent annual financial statements on file,
2022-2025) -- so ``status`` below is derived primarily from the *text* of a
company's own identity fields (legal form, name), not from this code.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from html import unescape
from pathlib import Path
from typing import Any

import httpx

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper

logger = logging.getLogger(__name__)

DEEDS_API_URL = "https://portal.registryagency.bg/CR/api/Deeds"
DEED_PAGE_URL = "https://portal.registryagency.bg/CR/Reports/ActiveConditionTabResult"

_UA = (
    "NessebarBudgetMonitor/0.1 (+local civic budget-monitoring project; "
    "contact: dev@gmu.online; httpx)"
)

#: Fixed BGN/EUR peg, used to convert a "... лв." capital figure to EUR when
#: the registry hasn't already rendered it in euro (see `_parse_capital`).
_BGN_PER_EUR = 1.95583

#: `legalForm` (top-level int) -> Bulgarian label, used only as a fallback
#: when field 00030 (the registry's own legal-form text) is missing. Confirmed
#: from ~110 real deeds fetched for this project (see docs/sources/REGISTRY_API.md);
#: other legalForm codes (e.g. кооперация, сдружение) were not observed among
#: this municipality's contractors and are left unmapped (None fallback stays None).
_LEGAL_FORM_FALLBACK: dict[int, str] = {
    1: "Едноличен търговец",
    4: "Дружество с ограничена отговорност",
    5: "Акционерно дружество",
    10: "Еднолично дружество с ограничена отговорност",
    11: "Еднолично акционерно дружество",
}

#: fieldIdent -> CompanyPerson.role, for every ident this project parses names
#: out of (see docs/sources/REGISTRY_API.md's ident table for the full set of
#: idents observed, including ones deliberately *not* parsed here -- e.g.
#: branch records 00510-00540, AML/UBO filings 05290-05501, merger/transform
#: records 07010-07030 -- because they either aren't about people at all or
#: don't fit this project's fixed role vocabulary cleanly enough to parse
#: without real risk of mislabeling).
_PEOPLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("00070", "manager"),  # управител(и)
    ("00180", "sole_owner"),  # the trader themselves (ЕТ -- едноличен търговец)
    ("00190", "partner"),  # съдружници (ООД)
    ("00230", "sole_owner"),  # едноличен собственик на капитала (ЕООД)
    ("00100", "board_member"),  # съвет на директорите (seen as a stale, superseded slot on one АД)
    ("00120", "board_member"),  # съвет на директорите (seen on an АД/ЕАД with a foreign board)
    ("00132", "board_member"),  # управителен съвет (two-tier АД)
    ("00140", "board_member"),  # надзорен съвет (two-tier АД, supervisory board)
)

#: Country names (as the registry prints them, upper-case Cyrillic) long
#: enough to disambiguate "NAME, Държава: COUNTRY" from the common case where
#: *several* people's records are concatenated in one field's text with no
#: other separator (see `_extract_people`'s docstring). Sorted longest-first
#: so a multi-word country (e.g. "ЧЕШКА РЕПУБЛИКА") is tried before a shorter
#: one that could otherwise swallow part of it. Deliberately biased towards
#: Bulgaria's immediate region plus every nationality actually observed in
#: this project's own samples -- not an exhaustive list of every country;
#: see the module/REGISTRY_API.md caveat about what happens for one that's
#: missing.
_KNOWN_COUNTRIES: tuple[str, ...] = (
    "ЧЕШКА РЕПУБЛИКА", "ОБЕДИНЕНИ АРАБСКИ ЕМИРСТВА", "РУСКА ФЕДЕРАЦИЯ",
    "РЕПУБЛИКА МАКЕДОНИЯ", "СЕВЕРНА МАКЕДОНИЯ", "ВЕЛИКА БРИТАНИЯ", "ЮЖНА АФРИКА",
    "НОВА ЗЕЛАНДИЯ", "САЩ", "БЪЛГАРИЯ", "РУСИЯ", "УКРАЙНА", "ГЪРЦИЯ", "РУМЪНИЯ",
    "СЪРБИЯ", "МАКЕДОНИЯ", "ТУРЦИЯ", "ГЕРМАНИЯ", "ИТАЛИЯ", "ФРАНЦИЯ", "ИСПАНИЯ",
    "ПОРТУГАЛИЯ", "АНГЛИЯ", "КАНАДА", "ЯПОНИЯ", "КИТАЙ", "ИЗРАЕЛ", "АВСТРИЯ",
    "ШВЕЙЦАРИЯ", "БЕЛГИЯ", "НИДЕРЛАНДИЯ", "ХОЛАНДИЯ", "ПОЛША", "ЧЕХИЯ",
    "СЛОВАКИЯ", "УНГАРИЯ", "ХЪРВАТИЯ", "СЛОВЕНИЯ", "АЛБАНИЯ", "КИПЪР", "МАЛТА",
    "ЛЮКСЕМБУРГ", "ИРЛАНДИЯ", "ДАНИЯ", "ШВЕЦИЯ", "НОРВЕГИЯ", "ФИНЛАНДИЯ",
    "ЕСТОНИЯ", "ЛАТВИЯ", "ЛИТВА", "АВСТРАЛИЯ", "БРАЗИЛИЯ", "ИНДИЯ", "ЕГИПЕТ",
    "МАРОКО", "ЛИВАН", "АНДОРА", "МОЛДОВА", "ГРУЗИЯ", "АРМЕНИЯ", "АЗЕРБАЙДЖАН",
    "КАЗАХСТАН", "БЕЛАРУС", "ЧЕРНА ГОРА", "БОСНА И ХЕРЦЕГОВИНА", "ЛИХТЕНЩАЙН",
    "МОНАКО", "СЛОНОВСКА КОСТ",
)
_COUNTRY_PATTERN = "|".join(
    re.escape(c) for c in sorted(_KNOWN_COUNTRIES, key=len, reverse=True)
)
_PERSON_RE = re.compile(
    rf"(?P<name>.+?),\s*Държава:\s*(?P<country>{_COUNTRY_PATTERN})\b"
    rf"(?:,?\s*Размер на дяловото участие:\s*(?P<share>[0-9][0-9.,]*\s*(?:лв\.|€)))?"
)
#: Stripped before `_PERSON_RE` runs so a board field's shared mandate-expiry
#: date (one date for the whole group, not per-person) doesn't get glued onto
#: the first person's name.
_MANDATE_DATE_RE = re.compile(r"Дата на изтичане на мандата:\s*[\d.]+\s*г\.\s*")

#: Matches a person/entity's own ЕИК/БУЛСТАТ, folded into the *name* text
#: when that person is itself a legal entity (e.g. a sole owner that is a
#: company, or a board member representing one) -- both "ЕИК/ПИК NNNNNNNNN"
#: and the plainer "ЕИК NNNNNNNNN" wordings are seen. Captured into
#: `CompanyPerson.person_eik` and stripped out of `name`.
_PERSON_EIK_RE = re.compile(r",?\s*ЕИК(?:/ПИК)?\s+(\d{6,13})\b")

_NKID_RE = re.compile(r"Група по НКИД:\s*([\d.]+)\s*Клас по НКИД:\s*(.+)", re.IGNORECASE)
_REPORT_YEAR_RE = re.compile(r"Година:\s*(\d{4})")
_CAPITAL_EUR_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*€")
_CAPITAL_BGN_RE = re.compile(r"([0-9]+(?:[.,][0-9]+)?)\s*лв")
#: Fields `_determine_status` scans for "залич.../ликвидация/несъстоятелност" --
#: deliberately narrow (the company's own name/legal-form text), *not* every
#: field, because broader fields legitimately mention those words without
#: describing *this* company's own status: e.g. a standard АД bylaws clause
#: about "right to a liquidation share" (00311, "ликвидационен дял" -- not a
#: substring of "ликвидация" so that specific phrase doesn't collide, but
#: other wordings might), or a "прехвърляне на предприятие" (business
#: transfer) field naming a *different*, unrelated company that happens to be
#: "... ЕАД - в ликвидация" (seen in a real sample, docs/sources/REGISTRY_API.md).
_STATUS_SCAN_IDENTS = ("00020", "00030", "00040")
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(html: str | None) -> str | None:
    """Tag-strip + unescape + whitespace-collapse one field's `htmlData`."""
    if not html:
        return None
    text = _TAG_RE.sub(" ", html)
    text = unescape(text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text or None


_EIK_SPLIT_RE = re.compile(r"[;,/\s]+")


def normalize_eiks(raw: str | None) -> list[str]:
    """Canonical Trade Register ЕИКs found in one `contractor_eik` value.

    ЦАИС ЕОП stores the code as typed by the municipality: sometimes with the
    leading zeros dropped ("646811" for 000646811), sometimes two codes for
    a consortium ("200948893; 204901777"), sometimes the placeholder
    "не се публикува". The registry itself keys deeds on the 9-digit
    (companies) or 13-digit (branches) form with leading zeros, so: split on
    separators, keep digit-only tokens, left-pad anything shorter than 9 to
    9 digits, drop everything else, dedupe preserving order.
    """
    if not raw:
        return []
    out: list[str] = []
    for token in _EIK_SPLIT_RE.split(raw.strip()):
        if not token.isdigit():
            continue
        eik = token.zfill(9) if len(token) < 9 else token
        if eik not in out:
            out.append(eik)
    return out


def normalize_name(name: str) -> str:
    """The one name-matching key for people in this project, stored as both
    `CompanyPerson.name_normalized` and `Official.name_normalized`:
    lower-case, every non-letter/digit character (quotes, periods, hyphens of
    double surnames, non-breaking spaces) turned into a space, whitespace
    collapsed. Keeps Cyrillic. "Петър Хрусафов-Тодоров" and "ПЕТЪР ХРУСАФОВ
    ТОДОРОВ" give the same key; so do "Христо Г.Яръмов" and "Христо Г. Яръмов".
    `scrapers.declarations` imports this function so the two sides can never
    drift apart."""
    text = name.lower().replace("\xa0", " ")
    text = re.sub(r"[^\w\s]|_", " ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _iter_fields(deed: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten `sections[].subDeeds[].groups[].fields[]` into one flat list
    of ``{"ident", "text", "op", "entry_date", "action_date", "min_action_date"}``
    dicts, in document order."""
    fields: list[dict[str, Any]] = []
    for section in deed.get("sections") or []:
        for sub_deed in section.get("subDeeds") or []:
            for group in sub_deed.get("groups") or []:
                for field in group.get("fields") or []:
                    ident = field.get("fieldIdent")
                    if not ident:
                        continue
                    fields.append(
                        {
                            "ident": ident,
                            "text": _strip_html(field.get("htmlData")),
                            "op": field.get("fieldOperation"),
                            "entry_date": field.get("fieldEntryDate"),
                            "action_date": field.get("fieldActionDate"),
                            "min_action_date": field.get("recordMinActionDate"),
                        }
                    )
    return fields


def _earliest_date(fields: list[dict[str, Any]]) -> dt.datetime | None:
    dates = [
        parsed
        for f in fields
        if (parsed := _parse_iso(f.get("min_action_date") or f.get("action_date"))) is not None
    ]
    return min(dates) if dates else None


def _parse_nkid(text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    match = _NKID_RE.search(text)
    if not match:
        return None, None
    return match.group(1).strip(), match.group(2).strip()


def _parse_capital(text: str | None) -> float | None:
    if not text:
        return None
    match = _CAPITAL_EUR_RE.search(text)
    if match:
        return float(match.group(1).replace(",", "."))
    match = _CAPITAL_BGN_RE.search(text)
    if match:
        return round(float(match.group(1).replace(",", ".")) / _BGN_PER_EUR, 2)
    return None


def _max_report_year(text: str | None) -> int | None:
    if not text:
        return None
    years = [int(y) for y in _REPORT_YEAR_RE.findall(text)]
    return max(years) if years else None


def _determine_status(deed: dict[str, Any], by_ident: dict[str, dict[str, Any]]) -> str:
    """'deregistered' / 'liquidation' / 'insolvency' / 'active', from the text
    of the company's own current (non-deleted) identity fields -- see the
    `_STATUS_SCAN_IDENTS` comment above for why the scan is scoped this
    narrowly. `deed["deedStatus"]` is *not* used: both values observed in
    this project's samples (1 and 2) occur on companies with recent annual
    filings, so neither reliably means "deregistered" (see module docstring).
    """
    scan_text = " ".join(
        by_ident[ident]["text"]
        for ident in _STATUS_SCAN_IDENTS
        if by_ident.get(ident) and by_ident[ident]["text"] and by_ident[ident]["op"] != 2
    )
    if re.search(r"несъстоятелност", scan_text, re.IGNORECASE):
        return "insolvency"
    if re.search(r"ликвидация", scan_text, re.IGNORECASE):
        return "liquidation"
    if re.search(r"залич", scan_text, re.IGNORECASE):
        return "deregistered"
    return "active"


def _split_person_eik(name: str) -> tuple[str, str | None]:
    """Pull a trailing/embedded "ЕИК/ПИК NNNNNNNNN" (or "ЕИК NNNNNNNNN") out of
    a person-field name -- seen when the "person" is itself a legal entity
    (a sole owner that's a company, a board member representing one, a
    government body) -- returning `(clean_name, person_eik)`."""
    match = _PERSON_EIK_RE.search(name)
    if not match:
        return name.strip(" ,"), None
    person_eik = match.group(1)
    cleaned = (name[: match.start()] + name[match.end() :]).strip(" ,")
    return cleaned, person_eik


def _extract_people(text: str, role: str, ident: str) -> list[dict[str, Any]]:
    """Parse one field's text into one dict per person: {"name",
    "name_normalized", "role", "share_text", "person_eik", "is_current",
    "field_ident"}.

    The registry concatenates several people's records in one field with no
    delimiter beyond the repeating "<NAME>, Държава: <COUNTRY>[, Размер на
    дяловото участие: <AMOUNT>]" shape -- and a bare "NAME, Държава:" would be
    genuinely ambiguous about where one person's record ends and the next
    begins if "country" were matched generically (a second person's
    ALL-CAPS name is not distinguishable from a country name by case alone;
    confirmed on a real sample where a typo'd/corrected manager name appears
    twice back-to-back). Anchoring the country capture to `_KNOWN_COUNTRIES`
    (a closed, finite list) resolves this for every country actually in that
    list: `re.finditer` naturally stops each match right after the matched
    country token (or its share-amount suffix), so the next match starts
    exactly where the next person's record begins. A country *not* in the
    list instead lets the non-greedy name group swallow extra text up to the
    next recognized country -- producing one garbled combined name instead of
    two clean ones. This degrades gracefully (a slightly wrong `name` string,
    never a crash or a silently dropped record) and is judged an acceptable
    residual risk given how long `_KNOWN_COUNTRIES` already is; see
    docs/sources/REGISTRY_API.md.

    **No "Държава:" at all**: some entries (often a sole owner, 00230) are a
    bare name with no country clause whatsoever -- e.g. "Сабри Исмаилов
    Алиев" -- or a bare legal-entity name plus its own ЕИК and *still* no
    country -- e.g. '"ДАРЗАЛА ХОЛДИНГ" АД, ЕИК/ПИК 205127270'. `_PERSON_RE`
    can't match these at all (nothing to anchor on), so when it finds zero
    matches the whole (mandate-date-stripped) text is treated as one person
    instead of silently producing no rows -- confirmed safe for this
    project's `_PEOPLE_FIELDS` idents specifically (already filtered to
    current, non-"Заличен..." text by the time this is called). A field with
    *several* such country-less entries concatenated is unrecoverable by any
    text-shape rule and comes out as one merged name -- same documented
    residual risk as above (see docs/sources/REGISTRY_API.md).
    """
    cleaned = _MANDATE_DATE_RE.sub("", text).strip()
    people: list[dict[str, Any]] = []
    matches = list(_PERSON_RE.finditer(cleaned))

    if not matches:
        name, person_eik = _split_person_eik(cleaned)
        if name:
            people.append(
                {
                    "name": name,
                    "name_normalized": normalize_name(name),
                    "role": role,
                    "share_text": None,
                    "person_eik": person_eik,
                    "is_current": True,
                    "field_ident": ident,
                }
            )
        return people

    for match in matches:
        name, person_eik = _split_person_eik(match.group("name"))
        if not name:
            continue
        people.append(
            {
                "name": name,
                "name_normalized": normalize_name(name),
                "role": role,
                "share_text": match.group("share"),
                "person_eik": person_eik,
                "is_current": True,
                "field_ident": ident,
            }
        )
    return people


#: Announced-acts ident for the one-off "no activity" declaration that
#: replaces the ГФО (ЗСч чл. 38, ал. 9, т. 2), e.g. "Декларация по чл.38,
#: ал.9, т.2 от ЗСч Година: 2021".
NO_ACTIVITY_DECLARATION_IDENT = "1001BI"


def no_activity_declaration_year(raw_json: dict[str, Any] | None) -> int | None:
    """Latest year covered by a no-activity declaration in a stored slim
    `Company.raw_json` ({ident: {"text", ...}}), or None."""
    field = (raw_json or {}).get(NO_ACTIVITY_DECLARATION_IDENT) or {}
    return _max_report_year(field.get("text") if isinstance(field, dict) else None)


def parse_deed(deed: dict[str, Any]) -> dict[str, Any]:
    """Parse one `/CR/api/Deeds/{EIK}` JSON payload into the `Company`
    columns plus a `people` list of `CompanyPerson`-shaped dicts (see
    `db.repo.upsert_company`). Never raises on a field it doesn't recognize
    or a shape it hasn't seen -- unrecognized idents simply aren't parsed
    into a column, but still land in `raw_json`.
    """
    fields = _iter_fields(deed)
    by_ident: dict[str, dict[str, Any]] = {f["ident"]: f for f in fields}

    def field_text(ident: str) -> str | None:
        field = by_ident.get(ident)
        return field["text"] if field else None

    eik = deed.get("uic")
    legal_form = field_text("00030") or _LEGAL_FORM_FALLBACK.get(deed.get("legalForm") or -1)
    nkid_code, nkid_label = _parse_nkid(field_text("00061"))
    capital_eur = _parse_capital(field_text("00310")) or _parse_capital(field_text("00320"))

    people: list[dict[str, Any]] = []
    for ident, role in _PEOPLE_FIELDS:
        field = by_ident.get(ident)
        if field is None or field["op"] == 2 or not field["text"]:
            continue
        if field["text"].strip().startswith("Заличен"):
            continue
        people.extend(_extract_people(field["text"], role=role, ident=ident))

    raw_json = {
        f["ident"]: {
            "text": f["text"],
            "entry_date": f["entry_date"],
            "action_date": f["action_date"],
        }
        for f in fields
    }

    return {
        "eik": eik,
        "name": deed.get("companyName"),
        "legal_form": legal_form,
        "status": _determine_status(deed, by_ident),
        "seat_address": field_text("00050"),
        "activity": field_text("00060"),
        "nkid_code": nkid_code,
        "nkid_label": nkid_label,
        "capital_eur": capital_eur,
        "registered_at": _earliest_date(fields),
        "last_annual_report_year": _max_report_year(field_text("10019B")),
        "source_url": f"{DEED_PAGE_URL}?uic={eik}",
        "raw_json": raw_json,
        "people": people,
    }


class RegistryScraper(Scraper):
    """Fetches Trade Register deeds for a list of contractor ЕИКs.

    Unlike every other scraper in this project (`EopScraper`, `SigmaScraper`,
    `NesebarSiteScraper`), this one has no fixed "the municipality" scope --
    `fetch()` takes the list of ЕИКs to look up (the CLI/pipeline resolve
    that list from `procurements.contractor_eik`). It also does not let one
    failing ЕИК abort the whole run (see module docstring): failures are
    collected in `self.failures` as `(eik, reason)` pairs instead.
    """

    name = "registry"

    def __init__(
        self,
        delay: float = 2.0,
        max_retries: int = 4,
        max_age_days: int = 7,
        client: httpx.Client | None = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self.max_age_days = max_age_days
        self.failures: list[tuple[str, str]] = []

        settings = get_settings()
        self.cache_dir = Path(settings.data_dir) / "cache" / "registry"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _cache_path(self, eik: str) -> Path:
        return self.cache_dir / f"{eik}.json"

    def _cache_is_fresh(self, cache_file: Path) -> bool:
        age_days = (time.time() - cache_file.stat().st_mtime) / 86400
        return age_days <= self.max_age_days

    def fetch_deed(self, eik: str) -> dict[str, Any]:
        """GET the raw deed JSON for one ЕИК, using the on-disk cache unless
        it's older than `max_age_days`. Raises on a persistent failure --
        callers doing a multi-ЕИК batch should use `fetch()`, which catches
        this per ЕИК instead of letting it abort the batch."""
        cache_file = self._cache_path(eik)
        if cache_file.exists() and self._cache_is_fresh(cache_file):
            return json.loads(cache_file.read_text(encoding="utf-8"))

        data = self._fetch_live(eik)
        cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data

    def _fetch_live(self, eik: str) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.get(f"{DEEDS_API_URL}/{eik}")
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else 15.0 * attempt
                    logger.warning(
                        "registry %s: 429 Too Many Requests (attempt %d/%d), "
                        "waiting %.0fs before retrying",
                        eik,
                        attempt,
                        self.max_retries,
                        wait,
                    )
                    last_exc = httpx.HTTPStatusError(
                        "429 Too Many Requests", request=response.request, response=response
                    )
                    if attempt < self.max_retries:
                        time.sleep(wait)
                    continue
                response.raise_for_status()
                data = response.json()
                time.sleep(self.delay)
                return data
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    # A definitive client error (404 not found, etc.) -- no
                    # amount of retrying will fix this, unlike a 5xx/timeout.
                    raise
                last_exc = exc
                logger.warning(
                    "registry %s attempt %d/%d failed: %s", eik, attempt, self.max_retries, exc
                )
                if attempt < self.max_retries:
                    time.sleep(2**attempt)
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "registry %s attempt %d/%d failed: %s", eik, attempt, self.max_retries, exc
                )
                if attempt < self.max_retries:
                    time.sleep(2**attempt)

        assert last_exc is not None
        raise last_exc

    def fetch(self, eiks: list[str]) -> list[dict[str, Any]]:
        """Fetch + parse every ЕИК in `eiks`. A persistently-failing ЕИК is
        logged and recorded in `self.failures`, not raised -- see module
        docstring for why this scraper's failure policy differs from
        `EopScraper`/`SigmaScraper`'s "let it propagate"."""
        self.failures = []
        records: list[dict[str, Any]] = []
        for eik in eiks:
            try:
                deed = self.fetch_deed(eik)
            except Exception as exc:  # noqa: BLE001 -- see docstring: isolate per-EIK
                logger.warning("registry %s: giving up -- %s", eik, exc)
                self.failures.append((eik, f"{type(exc).__name__}: {exc}"))
                continue
            parsed = parse_deed(deed)
            if not parsed.get("eik"):
                parsed["eik"] = eik
            records.append(parsed)
        return records
