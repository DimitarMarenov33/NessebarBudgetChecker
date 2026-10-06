"""Scraper stub for the Ministry of Finance (minfin.bg) municipal budget data.

TODO: implement fetch() to pull Nessebar Municipality budget execution
reports published by the Ministry of Finance, parse them into plain dicts
matching the BudgetReport model fields, and return them. No implementation
yet.
"""

from __future__ import annotations

from typing import Any

from nessebar_budget.scrapers.base import Scraper


class MinfinScraper(Scraper):
    name = "minfin"

    def fetch(self) -> list[dict[str, Any]]:
        raise NotImplementedError("MinfinScraper.fetch is not implemented yet")
