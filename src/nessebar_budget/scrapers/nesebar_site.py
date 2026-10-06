"""Scraper for the official Nessebar Municipality website's budget-execution
report archive (`https://www.nesebar.bg/reports.html`).

The page is a plain, server-rendered HTML list of monthly report files under
`/03-2019/` (a folder name fixed since the archive's start in 2019,
regardless of how far it actually reaches), grouped under `<h4>` headings
such as "Отчети за касово изпълнение на бюджета към 31.08.2026 г." -- see
`docs/sources/BUDGET_FORMS.md` and `docs/sources/INVENTORY.md` §1.

Each heading reliably states the report's as-of date, so that is used as
the primary source of a file's (year, month); the filename itself (the
standard `B1_YYYY_M_5206.xls` / `IB1_YYYY_M_5206_<SUFFIX>.xls` pattern, or a
friendly name like `august2026.xlsx`) is classified into a `kind` and used
as a *fallback* period source (with a lower `confidence`) for the rare link
that isn't under a dated heading.

robots.txt sets `Crawl-delay: 10` for this host; `delay` (default 10s)
is enforced between every outgoing HTTP request this scraper makes.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper

logger = logging.getLogger(__name__)

REPORTS_URL = "https://www.nesebar.bg/reports.html"
BASE_URL = "https://www.nesebar.bg/"
DEFAULT_CRAWL_DELAY = 10.0

_UA = (
    "NessebarBudgetMonitor/0.1 (+local civic budget-monitoring project; "
    "contact: dev@gmu.online; httpx)"
)

#: kinds this scraper recognises and will actually download/catalog as
#: BudgetReport rows; anything else is classified but left alone (the page
#: also links PDFs of scanned audits, energy-efficiency reports, etc.).
BUDGET_REPORT_KINDS = {
    "B1",
    "IB1_DES",
    "IB1_DMP",
    "IB1_K33",
    "IB1_KSF",
    "IB1_RA",
    "capital_xlsx",
}

_B1_RE = re.compile(r"(?:^|_)i?b[13]_(\d{4})_(\d{1,2})_(\d+)", re.IGNORECASE)
_SUFFIX_RE = re.compile(r"_(des|dmp|k33|ksf|ra)$", re.IGNORECASE)
_HEADING_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")

_MONTHS_BG = {
    "януари": 1, "февруари": 2, "март": 3, "април": 4, "май": 5, "юни": 6,
    "юли": 7, "август": 8, "септември": 9, "октомври": 10, "ноември": 11, "декември": 12,
}
_MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def classify_filename(filename: str) -> dict[str, Any]:
    """Classify a report filename into a `kind`, with a best-effort
    (year, month) and a `confidence` ("high"/"medium"/"low"/"none").

    `B1_YYYY_M_5206.xls` / `IB1_YYYY_M_5206_<SUFFIX>.xls` (and the quarterly
    `B3`/`IB3` siblings, which share the same naming scheme and schema) are
    parsed exactly, at "high" confidence. Friendly-named `.xlsx` files (the
    capital-expenditure ledger) are matched against a Bulgarian or English
    month name, then a trailing `MMYY` numeral, in decreasing order of
    confidence. Anything else is `kind="other"`.
    """
    lower = filename.lower()
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename

    m = _B1_RE.search(lower)
    if m:
        year, month, ebk_code = int(m.group(1)), int(m.group(2)), m.group(3)
        if lower.startswith("ib"):
            sm = _SUFFIX_RE.search(stem.lower())
            kind = f"IB1_{sm.group(1).upper()}" if sm else "other"
        else:
            kind = "B1"
        return {
            "kind": kind,
            "year": year,
            "month": month,
            "ebk_code": ebk_code,
            "confidence": "high",
        }

    if lower.endswith(".xlsx"):
        for name, num in _MONTHS_BG.items():
            if name in filename.lower():
                ym = re.search(r"(20\d{2})", filename)
                if ym:
                    return {
                        "kind": "capital_xlsx",
                        "year": int(ym.group(1)),
                        "month": num,
                        "confidence": "high",
                    }
        for name, num in sorted(_MONTHS_EN.items(), key=lambda kv: -len(kv[0])):
            em = re.search(rf"{name}[_\-]?(\d{{4}}|\d{{2}})(?!\d)", lower)
            if em:
                raw_year = em.group(1)
                year = int(raw_year) if len(raw_year) == 4 else 2000 + int(raw_year)
                return {"kind": "capital_xlsx", "year": year, "month": num, "confidence": "medium"}
        nm = re.search(r"(\d{2})(\d{2})$", stem)
        if nm:
            mm, yy = int(nm.group(1)), int(nm.group(2))
            if 1 <= mm <= 12:
                return {
                    "kind": "capital_xlsx",
                    "year": 2000 + yy,
                    "month": mm,
                    "confidence": "low",
                }
        return {"kind": "capital_xlsx", "year": None, "month": None, "confidence": "none"}

    return {"kind": "other", "year": None, "month": None, "confidence": "none"}


class NesebarSiteScraper(Scraper):
    """Fetches and classifies the monthly budget-report archive linked from
    `nesebar.bg/reports.html`, downloading recognised budget-report kinds to
    a local cache directory.
    """

    name = "nesebar_site"

    def __init__(
        self,
        cache_dir: Path | None = None,
        delay: float = DEFAULT_CRAWL_DELAY,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries

        settings = get_settings()
        self.cache_dir = cache_dir or Path(settings.data_dir) / "cache" / "nesebar_site"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=60.0)
        self._last_request_at: float | None = None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- rate limiting -----------------------------------------------------

    def _throttle(self) -> None:
        """Block until at least `self.delay` seconds have passed since the
        previous request this scraper made (robots.txt Crawl-delay: 10)."""
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
                    "nesebar_site: GET %s attempt %d/%d failed: %s", url, attempt, self.max_retries, exc
                )
        assert last_exc is not None
        raise last_exc

    # -- listing -------------------------------------------------------------

    def list_reports(self) -> list[dict[str, Any]]:
        """Fetch and parse the reports page into one record per file link,
        in document order, without downloading anything.
        """
        response = self._get(REPORTS_URL)
        soup = BeautifulSoup(response.text, "lxml")

        records: list[dict[str, Any]] = []
        current_period: str | None = None
        current_heading: str | None = None

        for el in soup.find_all(["h4", "a"]):
            if el.name == "h4":
                text = el.get_text(" ", strip=True)
                dm = _HEADING_DATE_RE.search(text)
                if dm:
                    _day, month, year = dm.groups()
                    current_period = f"{int(year):04d}-{int(month):02d}"
                    current_heading = text
                continue

            href = el.get("href")
            if not href or href.startswith("#"):
                continue
            filename = href.rsplit("/", 1)[-1]
            if "." not in filename:
                continue  # not a file link (e.g. in-page nav anchor)

            info = classify_filename(filename)
            if current_period is not None:
                year, month = (int(p) for p in current_period.split("-"))
                period = current_period
                confidence = "high"
                period_source = "heading"
            else:
                year, month = info["year"], info["month"]
                period = f"{year:04d}-{month:02d}" if year and month else None
                confidence = info["confidence"]
                period_source = "filename"

            records.append(
                {
                    "url": urljoin(BASE_URL, href),
                    "filename": filename,
                    "kind": info["kind"],
                    "period": period,
                    "year": year,
                    "month": month,
                    "confidence": confidence,
                    "period_source": period_source,
                    "heading": current_heading,
                }
            )

        return records

    # -- download ------------------------------------------------------------

    def _cache_path(self, record: dict[str, Any]) -> Path:
        year = record["year"] or "unknown-year"
        month = f"{record['month']:02d}" if record["month"] else "unknown-month"
        return self.cache_dir / str(year) / str(month) / record["filename"]

    def download(self, record: dict[str, Any]) -> Path:
        """Download one report file (or reuse the cached copy), returning
        its local path. Idempotent: skips the HTTP request entirely if the
        file is already cached.
        """
        dest = self._cache_path(record)
        if dest.exists():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = self._get(record["url"])
        dest.write_bytes(response.content)
        return dest

    # -- public API ------------------------------------------------------------

    def run(
        self,
        since: str | None = None,
        limit: int | None = None,
        delay: float | None = None,
    ) -> list[dict[str, Any]]:
        """List, filter, and download recognised budget-report files.

        `since` is an inclusive "YYYY-MM" floor on period; files with an
        unknown period are skipped (we can't compare them). `limit` caps the
        number of files downloaded (for testing). Already-cached files are
        skipped without any network request, so repeated runs are cheap.

        Returns one dict per downloaded/cached file: `{period, kind, url,
        file_path, fetched_at, parsed_json}`, where `parsed_json` carries the
        classification metadata (confidence, how the period was determined,
        the source heading) -- `parse-budget` overwrites it with parsing
        stats once the file is actually parsed.
        """
        if delay is not None:
            self.delay = delay

        all_records = self.list_reports()
        candidates = [r for r in all_records if r["kind"] in BUDGET_REPORT_KINDS and r["period"]]
        if since:
            candidates = [r for r in candidates if r["period"] >= since]
        candidates.sort(key=lambda r: (r["period"], r["kind"], r["filename"]))

        results: list[dict[str, Any]] = []
        for record in candidates:
            if limit is not None and len(results) >= limit:
                break
            was_cached = self._cache_path(record).exists()
            try:
                path = self.download(record)
            except httpx.HTTPError as exc:
                logger.warning("nesebar_site: failed to download %s: %s", record["url"], exc)
                continue
            fetched_at = (
                dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC).replace(tzinfo=None)
                if was_cached
                else dt.datetime.now(dt.UTC).replace(tzinfo=None)
            )
            results.append(
                {
                    "period": record["period"],
                    "kind": record["kind"],
                    "url": record["url"],
                    "file_path": str(path),
                    "fetched_at": fetched_at,
                    "parsed_json": {
                        "confidence": record["confidence"],
                        "period_source": record["period_source"],
                        "heading": record["heading"],
                        "was_cached": was_cached,
                    },
                }
            )

        return results

    def fetch(self) -> list[dict[str, Any]]:
        """`Scraper` interface: run with default options (all periods)."""
        return self.run()
