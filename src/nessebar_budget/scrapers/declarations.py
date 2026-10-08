"""Scraper for the Nessebar Municipal Council's anti-corruption declarations
registers (`https://os-nessebar.eu/deklaracii-po-zpk`), i.e. the public
registers of incompatibility / asset-and-interest declarations officials
file under ЗПК (today's law) and its predecessor КПКОНПИ (archived).

Site shape (verified 2026-10-08; see `docs/sources/DECLARATIONS.md`):

- The index page (`INDEX_URL`) and the КПКОНПИ archive page (`ARCHIVE_URL`)
  each link a handful of "register" sub-pages, one per (role group, register
  type, mandate) -- e.g. "... за общински съветници, мандат 2023-2027" or
  "... кметове на кметства, мандат 2019-2023". `list_register_pages`
  discovers these by their common URL fragment
  (`/publichen-registar-na-deklaraciite-`).
- Each register page is plain server-rendered HTML (no JS needed): a title
  paragraph states the register's legal basis and mandate, followed by one
  `<p><a href=".../assets/upload/files/<transliterated-name>.pdf">Иван
  Иванов</a></p>` per person -- the link text is the person's name in
  Cyrillic, never the filename. On the one page that mixes the municipal
  mayor with village mayors, each link's text also carries a role suffix
  ("... - Кмет на община Несебър" / "... - Кмет на кметство с.Х"), which is
  the only place role is determined per-row rather than per-page.
- `robots.txt` has no `Disallow` and no `Crawl-delay`; this scraper still
  applies a flat 2s delay between requests (see `run`'s `delay` parameter).

Known limitations (see docs/sources/DECLARATIONS.md for the full writeup):

- A much older, flat archive page (`deklaracii-po-zpuki-arhiv`, mandate
  "2015") lists names directly on the page with no sub-pages and no
  reliable per-row role signal (two undifferentiated batches of names under
  "ЗПУКИ - чл.12, т.1/т.2"); it is deliberately *not* scraped here, since
  guessing a role for it would violate this scraper's precision-over-recall
  stance. Its existence is just noted in the docs.
- If the same (name, role, mandate) identity is published on more than one
  register page (this happens for 2019-2023 village mayors, who have both
  an incompatibility and an asset/interest declaration listed), only the
  last-processed page's document survives the upsert (the unique key is
  `(name_normalized, role, mandate)`, one document per identity). Register
  pages are ordered so that an "имущество и интереси" (asset/interest)
  declaration -- the type that can actually carry company holdings -- wins
  over a "несъвместимост" (incompatibility) one for the same identity; see
  `_register_sort_key`.
- Every individually signed declaration PDF fetched while building this
  scraper was a scanned image (no text layer) -- `document_status` will
  almost certainly read "scanned" for most/all real officials. The parsing
  logic in `parse_declaration_text` is still implemented against the
  standard form's wording and exercised by a synthetic text fixture in
  `tests/test_declarations.py`, so it is ready for the rare PDF that *does*
  carry a text layer (e.g. a born-digital register summary).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper
from nessebar_budget.scrapers.registry import normalize_name as _registry_normalize_name

logger = logging.getLogger(__name__)

INDEX_URL = "https://os-nessebar.eu/deklaracii-po-zpk"
ARCHIVE_URL = "https://os-nessebar.eu/deklaracii-po-kpkonpi-arhiv"
BASE_URL = "https://os-nessebar.eu/"
DEFAULT_DELAY = 2.0

_UA = (
    "NessebarBudgetMonitor/0.1 (+local civic budget-monitoring project; "
    "contact: dev@gmu.online; httpx)"
)

#: Register sub-pages are linked from the index/archive pages with this URL
#: fragment in their path (both legal regimes use the same phrasing).
_REGISTER_PATH_RE = re.compile(r"/publichen-registar-na-deklaraciite-", re.IGNORECASE)

_MANDATE_RE = re.compile(r"мандат\s*[:\-]?\s*(\d{4})(?:\s*[-–]\s*(\d{4}))?", re.IGNORECASE)

#: A row's link text is rejected (not a person) if it looks like a document
#: title rather than a name -- the one observed case is a consolidated
#: register PDF whose link text restates the register's own legal citation.
_NOT_A_PERSON_RE = re.compile(r"^регистър\b|чл\.|мандат", re.IGNORECASE)

_SECRETARY_RE = re.compile(r"секретар", re.IGNORECASE)
_DEPUTY_MAYOR_RE = re.compile(r"зам[.\-\s]*[-\s]*кмет|заместник[\s-]*кмет", re.IGNORECASE)
#: Matches the "- Кмет на община Несебър" / "- Кмет на кметство с.Х" suffix
#: some rows append to the person's name; group(1) says which.
_MAYOR_SUFFIX_RE = re.compile(r"\s[-\s]*кмет\s+на\s+(община|кметство)\b", re.IGNORECASE)

_EIK_RE = re.compile(r"\b(\d{13}|\d{9})\b")
_QUOTE_PAIRS = (("„", "“"), ("“", "”"), ('"', '"'), ("'", "'"))
_LEGAL_FORM_RE = re.compile(r"^\s*(ЕООД|ООД|ЕАД|АД|КД|ДЗЗД|ЕТ)\b", re.IGNORECASE)

_SECTION_COMPANY_RE = re.compile(r"участие\s+в\s+търговски\s+дружества", re.IGNORECASE)
_SECTION_DEBT_RE = re.compile(r"задължени[яе]\s+над\s+5\s*0{3}", re.IGNORECASE)
_SECTION_CONTRACT_RE = re.compile(
    r"договори\s+с\s+лица.{0,60}извършват\s+дейност", re.IGNORECASE | re.DOTALL
)
#: Resets the current section when a *different* numbered "ЧАСТ ..." header
#: of the declaration form is reached (checked after the three regexes
#: above, so it only fires for parts we don't otherwise recognise).
_OTHER_PART_HEADER_RE = re.compile(r"^част\s+\S+\.?", re.IGNORECASE)

#: Phrase -> kept as-is (not slugified) in `relation`, so the JSON stays
#: close to the declaration's own wording; order controls match priority
#: when several phrases are present in the same line.
_COMPANY_RELATION_PHRASES = (
    "едноличен собственик",
    "съдружник",
    "акционер",
    "управител",
    "член на управителен орган",
    "член на контролен орган",
    "едноличен търговец",
)

_DEBT_RELATION = "задължение над 5 000 лв."
_CONTRACT_RELATION = "договор с лице, свързано с вземаните решения"


# One shared matching key with the Trade Register side (see its docstring).
normalize_name = _registry_normalize_name


def _register_sort_key(url: str) -> tuple[int, int, str]:
    """Processing order: current mandate before archived, and within a
    mandate, incompatibility ("несъвместимост") registers before
    asset/interest ("имущество и интереси") ones -- so that when the same
    identity is published in both (observed for 2019-2023 village mayors),
    the asset/interest document (the type that can carry company holdings)
    is the one left standing after the upsert."""
    archive = 1 if "kpkonpi-arhiv" in url else 0
    asset_interest = 1 if "imushtestvo" in url else 0
    return (archive, asset_interest, url)


def _default_role_from_url(url: str) -> str:
    """Page-level fallback role, used for rows whose link text carries no
    per-row role suffix (see `_MAYOR_SUFFIX_RE`)."""
    if "za-obshtinski-savetnici" in url:
        return "councillor"
    if "kmetove-na-kmetstva" in url and "kmet-na-obshtina" not in url:
        return "village_mayor"
    return "other"


def _detect_mandate(text: str) -> str | None:
    m = _MANDATE_RE.search(text)
    if not m:
        return None
    start, end = m.group(1), m.group(2)
    return f"{start}-{end}" if end else start


def _split_name_and_role(raw_text: str, default_role: str) -> tuple[str, str]:
    """Split a row's link text into (name, role). The role suffixes this
    site actually prints are "- Кмет на община ..." / "- Кмет на кметство
    ..."; secretary/deputy-mayor patterns are handled too in case a future
    register page uses them, though none observed so far did."""
    m = _DEPUTY_MAYOR_RE.search(raw_text)
    if m:
        return raw_text[: m.start()].strip(" -"), "deputy_mayor"
    m = _SECRETARY_RE.search(raw_text)
    if m:
        return raw_text[: m.start()].strip(" -"), "secretary"
    m = _MAYOR_SUFFIX_RE.search(raw_text)
    if m:
        role = "mayor" if m.group(1).lower() == "община" else "village_mayor"
        return raw_text[: m.start()].strip(" -"), role
    return raw_text, default_role


def parse_register_page(html: str, url: str) -> list[dict[str, Any]]:
    """Parse one register page into one dict per person: `{name, role,
    mandate, source_url, document_url}`. `source_url` is always `url`;
    `document_url` is the person's declaration PDF, resolved to an absolute
    URL. Rows are returned in page order; a person listed twice on the same
    page (observed for an original + amended filing) is returned twice --
    the caller/`upsert_official` resolve that with "last one wins"."""
    soup = BeautifulSoup(html, "lxml")
    container = (
        soup.find("div", class_=lambda c: bool(c) and "font-serif" in c)
        or soup.find("main")
        or soup
    )
    container_text = container.get_text(" ", strip=True).replace("\xa0", " ")
    mandate = _detect_mandate(container_text)
    default_role = _default_role_from_url(url)

    records: list[dict[str, Any]] = []
    for a in container.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".pdf"):
            continue
        raw_text = re.sub(r"\s+", " ", a.get_text(" ", strip=True).replace("\xa0", " ")).strip()
        if not raw_text or len(raw_text) > 150 or _NOT_A_PERSON_RE.search(raw_text):
            continue
        name, role = _split_name_and_role(raw_text, default_role)
        name = name.strip(" -\xa0")
        if not name:
            continue
        records.append(
            {
                "name": name,
                "role": role,
                "mandate": mandate,
                "source_url": url,
                "document_url": urljoin(url, href),
            }
        )
    return records


# -- full-name enrichment -----------------------------------------------------
#
# The declarations registers only ever print a two-part name ("Георги
# Георгиев"), which can never equal a Trade Register manager/partner's full
# three-part name, so the related-party rule can't match officials at all.
# A handful of "composition" pages elsewhere on the council/municipal sites
# print the full legal name instead; this section fetches those, extracts
# full-name candidates, and matches one to a short name by (first token,
# trailing token(s)) -- i.e. ignoring the middle patronymic -- within the
# same mandate. Only an exact, unambiguous (single-candidate) match is used;
# see `resolve_full_name`.

#: Composition pages known to print full (three-part) names, each tagged
#: with the (mandate, role) its names apply to -- matching is scoped to
#: both, not just mandate, so e.g. a village mayor can never be matched
#: against a councillor candidate that happens to share a first+last name.
#: Fetched through the same `DeclarationsScraper._get` (and its 2s delay)
#: as every other request this scraper makes; see `fetch_full_names`.
#: Deliberately small and hand-curated rather than auto-discovered: every
#: entry was individually checked for false positives (see
#: `docs/sources/DECLARATIONS.md`). No page listing full names for village
#: mayors (either mandate) or the 2019-2023 municipal mayor was found on
#: either site -- those officials' names are left as scraped.
COMPOSITION_SOURCES: tuple[dict[str, str], ...] = (
    {"url": "https://os-nessebar.eu/sastav", "mandate": "2023-2027", "role": "councillor"},
    {"url": "https://os-nessebar.eu/predsedatel", "mandate": "2023-2027", "role": "councillor"},
    {
        "url": (
            "https://archive.os-nessebar.eu/"
            "%D1%81%D1%8A%D1%81%D1%82%D0%B0%D0%B2-%D0%BD%D0%B0-%D0%BE%D0%B1%D1%89"
            "%D0%B8%D0%BD%D1%81%D0%BA%D0%B8-%D1%81%D1%8A%D0%B2%D0%B5%D1%82-"
            "%D0%BD%D0%B5%D1%81%D0%B5%D0%B1%D1%8A%D1%80/"
        ),
        "mandate": "2019-2023",
        "role": "councillor",
    },
    {"url": "https://www.nesebar.bg/mayor.html", "mandate": "2023-2027", "role": "mayor"},
)

#: A Title-Case Cyrillic token, optionally a hyphenated compound
#: ("Танева-Кючукова") once " - "/" – " spacing around the hyphen is
#: collapsed (see `_candidate_full_names`).
_NAME_TOKEN_RE = r"[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?"
#: A full line consisting of exactly 2-4 such tokens and nothing else.
_FULL_NAME_LINE_RE = re.compile(rf"^{_NAME_TOKEN_RE}(?:\s+{_NAME_TOKEN_RE}){{1,3}}$")
_HYPHEN_SPACING_RE = re.compile(r"\s*-\s*")


def _candidate_full_names(html: str) -> list[str]:
    """Extract full-name candidates off one composition page.

    Two page shapes are handled, both observed on the real sites: a modern
    `<ul><li><h3>Full Name</h3>...</li></ul>` roster (os-nessebar.eu) and an
    older WordPress one printing `<img .../>Full Name<br />Образование:
    ...` (archive.os-nessebar.eu), plus `<strong>Full Name</strong> е
    роден...`-style biography openers (nesebar.bg). `<script>`/`<style>`/
    `<nav>`/`<header>`/`<footer>` are dropped first so nav/footer links
    never leak in. Each candidate line must consist of *only* 2-4 Title-Case
    Cyrillic tokens (see `_FULL_NAME_LINE_RE`) -- this is deliberately
    strict: a short, curated list of pages (`COMPOSITION_SOURCES`) rather
    than a generic crawl is what keeps this precise.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()

    names: list[str] = []
    for tag in soup.find_all(["h3", "strong"]):
        text = tag.get_text(" ", strip=True).replace("\xa0", " ")
        text = _HYPHEN_SPACING_RE.sub("-", text)
        text = re.sub(r"\s+", " ", text).strip()
        if _FULL_NAME_LINE_RE.match(text):
            names.append(text)

    # The WordPress-era archive page prints the name as a bare text node
    # right after an <img ... /> and right before a <br />, not inside its
    # own tag -- matched directly on the (nav/header/footer-stripped) HTML.
    for m in re.finditer(
        rf"/>\s*({_NAME_TOKEN_RE}(?:\s+{_NAME_TOKEN_RE}){{1,3}})\s*<br",
        str(soup),
    ):
        names.append(m.group(1).strip())

    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def resolve_full_name(short_name: str, candidates: list[str]) -> str | None:
    """Return the one `candidates` entry whose first and last name
    (normalized) match `short_name`, or `None` if zero or more than one do.

    "Last name" is whichever trailing tokens `short_name` has beyond its
    first (usually one, but e.g. "Венета Танева-Кючукова" contributes two:
    "танева" and "кючукова") -- matched against the *same number* of
    trailing tokens of each candidate, so a candidate's middle
    patronymic(s) are ignored. Never guesses: a short name matching more
    than one candidate (ambiguous) or none (unmatched) is left alone by the
    caller.
    """
    short_tokens = normalize_name(short_name).split()
    if len(short_tokens) < 2:
        return None
    first, rest = short_tokens[0], short_tokens[1:]

    matches = []
    for candidate in candidates:
        candidate_tokens = normalize_name(candidate).split()
        if len(candidate_tokens) < len(short_tokens):
            continue
        if candidate_tokens[0] == first and candidate_tokens[-len(rest):] == rest:
            matches.append(candidate)

    if len(matches) == 1:
        return matches[0]
    return None


# -- declaration PDF text parsing --------------------------------------------


def _extract_quoted(line: str) -> str | None:
    for open_q, close_q in _QUOTE_PAIRS:
        start = line.find(open_q)
        if start == -1:
            continue
        end = line.find(close_q, start + 1)
        if end != -1:
            return line[start + 1 : end].strip()
    return None


def _extract_company_name(line: str) -> str:
    quoted = _extract_quoted(line)
    if quoted is not None:
        tail_start = line.find(quoted) + len(quoted)
        tail = line[tail_start : tail_start + 12].lstrip("„“”\"' ")
        m = _LEGAL_FORM_RE.match(tail)
        return f"{quoted} {m.group(1).upper()}" if m else quoted

    m = re.search(r"\bеик\b", line, re.IGNORECASE)
    eik_m = _EIK_RE.search(line)
    cutoff = m.start() if m else (eik_m.start() if eik_m else len(line))
    candidate = line[:cutoff]
    candidate = re.sub(r"^[\d.)\-–•\s]+", "", candidate)
    candidate = candidate.strip(" ,-–")
    return candidate or line.strip()


def _matched_relation_phrases(line: str) -> list[str]:
    lowered = line.lower()
    return [phrase for phrase in _COMPANY_RELATION_PHRASES if phrase in lowered]


def _extract_company_line(line: str) -> dict[str, Any] | None:
    eik_m = _EIK_RE.search(line)
    phrases = _matched_relation_phrases(line)
    if eik_m is None and not phrases:
        return None  # precision over recall: need at least one solid signal
    return {
        "company_name": _extract_company_name(line),
        "eik": eik_m.group(1) if eik_m else None,
        "relation": "; ".join(phrases) if phrases else "участие в търговско дружество",
        "raw": line,
    }


def _extract_debt_line(line: str) -> dict[str, Any] | None:
    if not re.search(r"лв\.?|eur|€", line, re.IGNORECASE):
        return None
    eik_m = _EIK_RE.search(line)
    return {
        "company_name": _extract_company_name(line),
        "eik": eik_m.group(1) if eik_m else None,
        "relation": _DEBT_RELATION,
        "raw": line,
    }


def _extract_contract_line(line: str) -> dict[str, Any] | None:
    eik_m = _EIK_RE.search(line)
    quoted = _extract_quoted(line)
    if eik_m is None and quoted is None:
        return None
    return {
        "company_name": _extract_company_name(line),
        "eik": eik_m.group(1) if eik_m else None,
        "relation": _CONTRACT_RELATION,
        "raw": line,
    }


def parse_declaration_text(text: str) -> list[dict[str, Any]]:
    """Extract declared commercial-company interests (plus declared debts
    over 5 000 BGN and contracts with parties doing business in areas tied
    to the official's decisions) from a declaration PDF's extracted text.

    Walks the text looking for the standard declaration form's own section
    headings (participation in commercial companies / debts over 5 000 BGN
    / relevant contracts) and only extracts a row from inside one of those
    sections, and only when the row carries a solid signal (an EIK, or one
    of the form's own relation phrases, or an amount for debts) -- empty
    boilerplate/instruction text yields nothing. Each returned dict is
    `{"company_name", "eik", "relation", "raw"}`; `raw` is the source
    line so a human can verify the extraction against the PDF.
    """
    section: str | None = None
    results: list[dict[str, Any]] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if _SECTION_COMPANY_RE.search(line):
            section = "company"
            continue
        if _SECTION_DEBT_RE.search(line):
            section = "debt"
            continue
        if _SECTION_CONTRACT_RE.search(line):
            section = "contract"
            continue
        if _OTHER_PART_HEADER_RE.match(line):
            section = None
            continue

        entry: dict[str, Any] | None = None
        if section == "company":
            entry = _extract_company_line(line)
        elif section == "debt":
            entry = _extract_debt_line(line)
        elif section == "contract":
            entry = _extract_contract_line(line)

        if entry is not None:
            results.append(entry)

    return results


def _extract_pdf_text(path: Path) -> str:
    """Extract text from a declaration PDF with pdfplumber, falling back to
    pypdf if pdfplumber yields nothing (or errors). Returns "" (not an
    error) for a scanned PDF with no text layer at all -- the caller treats
    that as `document_status="scanned"`."""
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        if text.strip():
            return text
    except Exception as exc:  # noqa: BLE001 -- any pdfplumber failure just falls back to pypdf
        logger.warning("declarations: pdfplumber failed on %s: %s", path, exc)

    try:
        import pypdf

        reader = pypdf.PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 -- a PDF neither library can read is just "scanned"
        logger.warning("declarations: pypdf fallback also failed on %s: %s", path, exc)
        return ""


class DeclarationsScraper(Scraper):
    """Fetches the municipal council's declarations registers and each
    person's declaration PDF, downloading/parsing into `Official`-shaped
    records (see module docstring for the site's shape and caveats)."""

    name = "declarations"

    def __init__(
        self,
        cache_dir: Path | None = None,
        delay: float = DEFAULT_DELAY,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries

        settings = get_settings()
        self.cache_dir = cache_dir or Path(settings.data_dir) / "cache" / "declarations"
        (self.cache_dir / "html").mkdir(parents=True, exist_ok=True)
        (self.cache_dir / "pdfs").mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=60.0)
        self._last_request_at: float | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- rate limiting --------------------------------------------------------

    def _throttle(self) -> None:
        if self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            remaining = self.delay - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "declarations: GET %s attempt %d/%d failed: %s",
                    url, attempt, self.max_retries, exc,
                )
        assert last_exc is not None
        raise last_exc

    # -- register page discovery ----------------------------------------------

    def _save_html_cache(self, url: str, html: str) -> None:
        # These URLs' own slugs can run past macOS's 255-byte filename limit
        # (the register pages' slugs restate most of the legal citation), so
        # the cache filename is a short, readable prefix of the slug plus a
        # hash of the full URL (for uniqueness, not security).
        slug = urlparse(url).path.strip("/").replace("/", "__") or "index"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        (self.cache_dir / "html" / f"{slug[:80]}__{digest}.html").write_text(
            html, encoding="utf-8"
        )

    def list_register_pages(self) -> list[str]:
        """Discover register sub-page URLs linked from the current-mandate
        index page and the КПКОНПИ archive page, in the processing order
        `_register_sort_key` defines."""
        urls: set[str] = set()
        for index_url in (INDEX_URL, ARCHIVE_URL):
            try:
                response = self._get(index_url)
            except httpx.HTTPError as exc:
                logger.warning("declarations: could not fetch %s: %s", index_url, exc)
                continue
            self._save_html_cache(index_url, response.text)
            soup = BeautifulSoup(response.text, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if _REGISTER_PATH_RE.search(href):
                    urls.add(urljoin(BASE_URL, href))
        return sorted(urls, key=_register_sort_key)

    # -- PDF download ----------------------------------------------------------

    def _pdf_cache_path(self, url: str) -> Path:
        filename = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "declaration.pdf"
        return self.cache_dir / "pdfs" / filename

    def fetch_pdf(self, url: str) -> Path:
        """Download (or reuse the cached copy of) one declaration PDF,
        returning its local path."""
        dest = self._pdf_cache_path(url)
        if dest.exists():
            return dest
        response = self._get(url)
        dest.write_bytes(response.content)
        return dest

    # -- full-name enrichment --------------------------------------------------

    def fetch_full_names(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        """Fetch every `COMPOSITION_SOURCES` page and extract full-name
        candidates, grouped by `(mandate, role)`: `{(mandate, role):
        [(full_name, source_url), ...]}`. A page that fails to fetch is
        skipped (logged, not fatal) -- enrichment for its (mandate, role)
        just won't happen this run, same as if it had no candidates at all.
        """
        by_key: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for source in COMPOSITION_SOURCES:
            url, mandate, role = source["url"], source["mandate"], source["role"]
            try:
                response = self._get(url)
            except httpx.HTTPError as exc:
                logger.warning("declarations: could not fetch composition page %s: %s", url, exc)
                continue
            self._save_html_cache(url, response.text)
            for candidate_name in _candidate_full_names(response.text):
                by_key.setdefault((mandate, role), []).append((candidate_name, url))
        return by_key

    # -- public API --------------------------------------------------------------

    def run(self, limit: int | None = None, delay: float = DEFAULT_DELAY) -> list[dict[str, Any]]:
        """List every register page, parse every person off of it, resolve
        full names off `COMPOSITION_SOURCES` where possible, then download
        and parse each person's declaration PDF.

        `limit` caps the number of *people* processed (PDF fetch + parse),
        for testing; register-page discovery, parsing, and full-name
        resolution always run in full. Returns one dict per person: the
        fields from `parse_register_page` (with `name` replaced by the
        resolved full name when one was found) plus `short_name` (always
        the original register name), `name_source_url` (set only when a
        full name was resolved), `declared_interests_json`,
        `document_status` ("text"/"scanned"/"missing"), and `fetched_at`.
        """
        self.delay = delay

        register_urls = self.list_register_pages()
        people: list[dict[str, Any]] = []
        for url in register_urls:
            try:
                response = self._get(url)
            except httpx.HTTPError as exc:
                logger.warning("declarations: could not fetch register page %s: %s", url, exc)
                continue
            self._save_html_cache(url, response.text)
            people.extend(parse_register_page(response.text, url))

        full_names = self.fetch_full_names()

        results: list[dict[str, Any]] = []
        for person in people:
            if limit is not None and len(results) >= limit:
                break

            record = dict(person)
            short_name = record["name"]
            record["short_name"] = short_name
            candidates = full_names.get((record.get("mandate"), record["role"]), [])
            matched = resolve_full_name(short_name, [name for name, _src in candidates])
            if matched is not None:
                record["name"] = matched
                record["name_source_url"] = next(
                    src for name, src in candidates if name == matched
                )

            try:
                pdf_path = self.fetch_pdf(person["document_url"])
            except httpx.HTTPError as exc:
                logger.warning(
                    "declarations: failed to download %s for %s: %s",
                    person["document_url"], person["name"], exc,
                )
                record["document_status"] = "missing"
                record["declared_interests_json"] = []
                record["fetched_at"] = dt.datetime.now(dt.UTC).replace(tzinfo=None)
                results.append(record)
                continue

            text = _extract_pdf_text(pdf_path)
            if text.strip():
                record["document_status"] = "text"
                record["declared_interests_json"] = parse_declaration_text(text)
            else:
                record["document_status"] = "scanned"
                record["declared_interests_json"] = []
            record["fetched_at"] = dt.datetime.now(dt.UTC).replace(tzinfo=None)
            results.append(record)

        return results

    def fetch(self) -> list[dict[str, Any]]:
        """`Scraper` interface: run with default options (every person)."""
        return self.run()
