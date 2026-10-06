"""Scraper for SIGMA (sigma.midt.bg) — public-procurement transparency platform.

SIGMA re-publishes АОП/ЦАИС ЕОП open data (CC-BY 4.0, sourced from storage.eop.bg)
through a clean per-authority CSV export -- see ``docs/sources/SIGMA_API.md``.
There is no separate JSON API; the CSV endpoint is the documented, cleanest
integration point, so this scraper just downloads and parses that.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper

logger = logging.getLogger(__name__)

SIGMA_CSV_URL = "https://sigma.midt.bg/contracts.csv"
DEFAULT_AUTHORITY_EIK = "000057122"  # Община Несебър

_UA = (
    "NessebarBudgetMonitor/0.1 (+local civic budget-monitoring project; "
    "contact: dev@gmu.online; httpx)"
)


def _clean(value: Any) -> Any:
    """Normalize pandas NaN/NaT to plain None; pass everything else through."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


class SigmaScraper(Scraper):
    """Downloads Nessebar Municipality's contracts CSV from sigma.midt.bg."""

    name = "sigma"

    def __init__(
        self,
        authority_eik: str = DEFAULT_AUTHORITY_EIK,
        delay: float = 1.0,
        max_retries: int = 3,
        force_refresh: bool = False,
        client: httpx.Client | None = None,
    ) -> None:
        self.authority_eik = authority_eik
        self.delay = delay
        self.max_retries = max_retries
        self.force_refresh = force_refresh

        settings = get_settings()
        self.cache_dir = Path(settings.data_dir) / "cache" / "sigma"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(headers={"User-Agent": _UA}, timeout=30.0)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _cache_path(self) -> Path:
        return self.cache_dir / f"contracts_{self.authority_eik}.csv"

    def download_csv(self) -> Path:
        """Download (or reuse a cached copy of) the authority's contracts CSV.

        Returns the local path to the CSV file.
        """
        cache_file = self._cache_path()
        if cache_file.exists() and not self.force_refresh:
            return cache_file

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.get(
                    SIGMA_CSV_URL, params={"authority": self.authority_eik}
                )
                response.raise_for_status()
                cache_file.write_bytes(response.content)
                time.sleep(self.delay)
                return cache_file
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "SIGMA CSV download attempt %d/%d failed: %s",
                    attempt,
                    self.max_retries,
                    exc,
                )
                if attempt < self.max_retries:
                    time.sleep(2**attempt)

        assert last_exc is not None
        raise last_exc

    def fetch(self) -> list[dict[str, Any]]:
        csv_path = self.download_csv()
        df = pd.read_csv(csv_path)
        return [self._normalize_row(row) for row in df.to_dict(orient="records")]

    @staticmethod
    def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
        contractor_eik = _clean(row.get("contractor_eik"))
        if contractor_eik is not None:
            # pandas reads this column as float64 because of NaNs in other rows;
            # restore the integer-looking EIK string (102811636.0 -> "102811636").
            contractor_eik = str(int(contractor_eik))

        bids_received = _clean(row.get("bids_received"))
        if bids_received is not None:
            bids_received = int(bids_received)

        signed_at = _clean(row.get("signed_at"))
        contract_date = (
            dt.datetime.combine(dt.date.fromisoformat(str(signed_at)), dt.time.min)
            if signed_at
            else None
        )

        eu_funded = _clean(row.get("eu_funded"))
        procedure = _clean(row.get("procedure"))
        row_id = row.get("id")

        return {
            "source": "sigma",
            "source_id": str(row_id),
            "title": _clean(row.get("subject")),
            "cpv_code": None,
            "procedure_type": procedure,
            "estimated_value_eur": None,
            "estimated_value_bgn": None,
            "contract_value_eur": _clean(row.get("value_eur")),
            "contract_value_bgn": None,
            "currency": "EUR",
            "contractor_name": _clean(row.get("contractor")),
            "contractor_eik": contractor_eik,
            "bids_received": bids_received,
            "published_at": None,
            "contract_date": contract_date,
            "url": f"https://sigma.midt.bg/contracts/{row_id}",
            "raw_json": {
                "unp": _clean(row.get("unp")),
                "authority": _clean(row.get("authority")),
                "authority_eik": _clean(row.get("authority_eik")),
                "kind": _clean(row.get("kind")),
                "sector_code": _clean(row.get("sector_code")),
                "procedure": procedure,
                "bids_received": bids_received,
                "eu_funded": bool(eu_funded) if eu_funded is not None else None,
            },
        }
