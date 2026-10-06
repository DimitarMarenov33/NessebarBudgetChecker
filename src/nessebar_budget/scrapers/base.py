"""Abstract base class for data-source scrapers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Scraper(ABC):
    """A scraper fetches raw records from a single public data source.

    Implementations should not perform any network calls at import time,
    and should not depend on any cloud/remote infrastructure: all fetching
    happens locally against public municipal/government sites.
    """

    #: short machine-readable name for this source, e.g. "eop", "minfin"
    name: str

    @abstractmethod
    def fetch(self) -> list[dict[str, Any]]:
        """Fetch and return a list of raw records (as plain dicts)."""
        raise NotImplementedError
