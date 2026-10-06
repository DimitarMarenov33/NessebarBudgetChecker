"""Scraper for ЦАИС ЕОП (service.eop.bg) — Bulgaria's public procurement register.

Talks directly to the JSON-RPC-ish backend at ``service.eop.bg/NX1Service.svc``
(no auth, wide-open CORS — see ``docs/sources/EOP_API.md``, captured by replaying
the real app.eop.bg Angular SPA's XHRs with Playwright). This module re-implements
that capture as a small, polite, cached httpx client.

Currency-code assumption (see ``CURRENCY_CODES`` below): confirmed from
``data/samples/eop_contracts_nesebar_page1.json`` and
``data/samples/eop_procedures_nesebar_page1.json`` -- every sampled row has
``Currency == 1``, and for every contract row ``ContractValue == ContractValueEuro``
*exactly* (not a currency-converted value). That is only consistent with code
``1`` meaning EUR: if it meant BGN, ``ContractValueEuro`` would have to be
``ContractValue / 1.95583`` (the fixed BGN/EUR peg), which it is not. Codes
2 (USD) and 3 (BGN) follow the ordering implied by the EOP_API.md doc's
``RetrieveCurrencies`` note; no sample exercises them, so they are carried
through best-effort (the raw numeric code is also kept in ``currency``... no,
in ``raw_json``) rather than asserted.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper

logger = logging.getLogger(__name__)

EOP_SERVICE_URL = "https://service.eop.bg/NX1Service.svc"
EOP_APP_URL = "https://app.eop.bg"

#: Default search term / known identity for Община Несебър (docs/sources/EOP_API.md).
DEFAULT_ORG_NAME = "Несебър"
NESSEBAR_EIK = "000057122"

#: int currency code -> ISO label. 1=EUR confirmed (see module docstring); 2/3 are
#: carried over from EOP_API.md's RetrieveCurrencies note but unconfirmed by any
#: sample seen so far.
CURRENCY_CODES = {1: "EUR", 2: "USD", 3: "BGN"}

_NET_DATE_RE = re.compile(r"/Date\((-?\d+)(?:[+-]\d{4})?\)/")

_UA = (
    "NessebarBudgetMonitor/0.1 (+local civic budget-monitoring project; "
    "contact: dev@gmu.online; httpx)"
)


def _parse_net_date(value: str | None) -> dt.datetime | None:
    """Parse a .NET JSON date like ``/Date(1789678800000+0300)/`` into a naive UTC datetime.

    The millisecond count is already UTC-based (``DateTime.ToUniversalTime()``
    ticks); the trailing ``+HHMM``/``-HHMM`` is cosmetic metadata about the
    serializing server's local offset and is not applied again.
    """
    if not value:
        return None
    match = _NET_DATE_RE.match(value)
    if not match:
        return None
    millis = int(match.group(1))
    return dt.datetime.fromtimestamp(millis / 1000, tz=dt.UTC).replace(tzinfo=None)


def _value_by_currency(
    value: float | None, currency_code: int | None
) -> tuple[float | None, float | None]:
    """Split a single (value, currency_code) pair into (value_eur, value_bgn)."""
    if value is None:
        return None, None
    currency = CURRENCY_CODES.get(currency_code)
    if currency == "EUR":
        return value, None
    if currency == "BGN":
        return None, value
    # USD or an unrecognized code: don't guess which bucket it belongs in.
    return None, None


def _slim(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep only scalar fields of a tender-detail payload; full copies live in the cache."""
    if not detail:
        return detail
    return {k: v for k, v in detail.items() if not isinstance(v, (dict, list))}


def _safe_filename_fragment(text: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-яЁё]+", "_", text).strip("_") or "x"


class EopScraper(Scraper):
    """Fetches Nessebar Municipality procurement/contract data from ЦАИС ЕОП."""

    name = "eop"

    def __init__(
        self,
        org_name: str = DEFAULT_ORG_NAME,
        organization_id: int | None = None,
        organization_guid: str | None = None,
        page_size: int = 50,
        delay: float = 1.0,
        max_pages: int | None = None,
        max_retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.org_name = org_name
        self._organization_id = organization_id
        self._organization_guid = organization_guid
        self.page_size = page_size
        self.delay = delay
        self.max_pages = max_pages
        self.max_retries = max_retries

        settings = get_settings()
        self.cache_dir = Path(settings.data_dir) / "cache" / "eop"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=EOP_SERVICE_URL,
            headers={
                "Content-Type": "application/json",
                "Origin": EOP_APP_URL,
                "Referer": f"{EOP_APP_URL}/",
                "User-Agent": _UA,
            },
            timeout=30.0,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------
    # Low-level transport: POST with cache + retry/backoff + polite delay.
    # ------------------------------------------------------------------
    def _post(
        self, method: str, body: dict[str, Any], cache_key: str | None = None
    ) -> dict[str, Any]:
        cache_file = self.cache_dir / f"{cache_key}.json" if cache_key else None
        if cache_file is not None and cache_file.exists():
            return json.loads(cache_file.read_text(encoding="utf-8"))

        last_exc: Exception | None = None
        data: dict[str, Any] | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.post(f"/{method}", json=body)
                response.raise_for_status()
                data = response.json()
                last_exc = None
                break
            except (httpx.HTTPError, ValueError) as exc:
                last_exc = exc
                logger.warning(
                    "EOP %s attempt %d/%d failed: %s", method, attempt, self.max_retries, exc
                )
                if attempt < self.max_retries:
                    time.sleep(2**attempt)

        if last_exc is not None:
            raise last_exc

        if cache_file is not None:
            cache_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        time.sleep(self.delay)
        return data  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public API methods (docs/sources/EOP_API.md methods 1-5).
    # ------------------------------------------------------------------
    def search_organization(self, name: str) -> dict[str, Any]:
        """GetContractingAuthoritySearchResult: buyer/organization lookup by name."""
        body = {
            "request": {
                "ActivityTypeGroup": None,
                "ContractingAuthorityTypeId": None,
                "ContractingAuthorityName": name,
                "BatchNumber": None,
                "Status": "2",
                "ActivityTypeId": None,
                "HierarchyType": None,
                "ExecutionRegion": None,
                "NutsCode": None,
                "RegistryNumber": None,
                "BulgarianBudgetCode": None,
                "StartIndex": 1,
                "EndIndex": 10,
                "OrderColumn": "BatchNumber",
                "OrderAscending": True,
            }
        }
        cache_key = f"org_search_{_safe_filename_fragment(name)}"
        return self._post("GetContractingAuthoritySearchResult", body, cache_key=cache_key)

    def list_procurements(self, org_id: int, page_size: int = 50) -> list[dict[str, Any]]:
        """GetProcurementsByOrganization, paginated via StartIndex/EndIndex."""
        return self._paginate(
            method="GetProcurementsByOrganization",
            make_body=lambda start, end: {
                "request": {"OrganizationId": org_id, "StartIndex": start, "EndIndex": end}
            },
            cache_prefix=f"procurements_org{org_id}",
            page_size=page_size,
        )

    def list_contracts(self, org_guid: str, page_size: int = 50) -> list[dict[str, Any]]:
        """GetContractsByOrganization, paginated via StartIndex/EndIndex."""
        return self._paginate(
            method="GetContractsByOrganization",
            make_body=lambda start, end: {
                "request": {
                    "ParticipantGuid": org_guid,
                    "StartIndex": start,
                    "EndIndex": end,
                },
                "role": 1,
            },
            cache_prefix=f"contracts_org{_safe_filename_fragment(org_guid)[:8]}",
            page_size=page_size,
        )

    def tender_details(self, tender_id: int) -> dict[str, Any]:
        """GetPublishedTenderDetails: one procedure's full detail."""
        body = {"tenderId": tender_id, "ianaTimeZone": "Europe/Sofia"}
        return self._post("GetPublishedTenderDetails", body, cache_key=f"tender_{tender_id}")

    def contract_items(self, tender_id: int) -> dict[str, Any]:
        """GetPublishedContractListItems: a procedure's 'Договори' tab."""
        body = {"tenderId": tender_id, "ianaTimeZone": "Europe/Sofia"}
        return self._post(
            "GetPublishedContractListItems", body, cache_key=f"contract_items_{tender_id}"
        )

    def _paginate(
        self,
        method: str,
        make_body: Any,
        cache_prefix: str,
        page_size: int,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        start = 1
        page_num = 0
        while True:
            page_num += 1
            if self.max_pages is not None and page_num > self.max_pages:
                break
            end = start + page_size - 1
            body = make_body(start, end)
            data = self._post(method, body, cache_key=f"{cache_prefix}_{start}_{end}")
            page_results = data.get("CurrentPageResults") or []
            results.extend(page_results)

            has_more = data.get("HasMoreResults")
            results_count = data.get("ResultsCount")
            if has_more is False:
                break
            if not page_results:
                break
            if has_more is None:
                # Observed null in every sample response -- fall back to ResultsCount
                # (when present) and otherwise to "this page wasn't full" as the stop
                # condition.
                if results_count is not None and len(results) >= results_count:
                    break
                if len(page_results) < page_size:
                    break
            start = end + 1
        return results

    # ------------------------------------------------------------------
    # Normalization / fetch()
    # ------------------------------------------------------------------
    def _resolve_organization(self) -> tuple[int, str]:
        if self._organization_id is not None and self._organization_guid is not None:
            return self._organization_id, self._organization_guid

        data = self.search_organization(self.org_name)
        results = data.get("CurrentPageResults") or []
        match = next((r for r in results if r.get("RegistryNumber") == NESSEBAR_EIK), None)
        if match is None and results:
            match = results[0]
        if match is None:
            raise ValueError(f"EOP: no contracting authority found for name={self.org_name!r}")
        return match["OrganizationId"], match["OrganizationGuid"]

    def fetch(self) -> list[dict[str, Any]]:
        """Fetch and normalize all procurement/contract records for the configured org.

        Produces one record per signed contract (richest data: value, contractor,
        dates) plus one record per procedure that has *no* signed contract yet
        (e.g. still-open or unawarded tenders) -- so that "missing contract value"
        analysis has something real to flag instead of silently dropping those rows.
        """
        org_id, org_guid = self._resolve_organization()

        procedures = self.list_procurements(org_id, page_size=self.page_size)
        contracts = self.list_contracts(org_guid, page_size=self.page_size)

        procedures_by_tender = {
            p["TenderId"]: p for p in procedures if p.get("TenderId") is not None
        }
        contract_tender_ids = {c["TenderId"] for c in contracts if c.get("TenderId") is not None}

        unique_tender_ids = sorted(set(procedures_by_tender) | contract_tender_ids)
        tender_detail_by_id: dict[int, dict[str, Any]] = {}
        for tender_id in unique_tender_ids:
            # Let failures propagate after the retry/backoff in _post is exhausted --
            # per project rules, a persistently-failing live call stops the run rather
            # than silently producing partial/fabricated data.
            tender_detail_by_id[tender_id] = self.tender_details(tender_id)

        records: list[dict[str, Any]] = []
        for contract in contracts:
            tender_id = contract.get("TenderId")
            procedure = procedures_by_tender.get(tender_id)
            detail = tender_detail_by_id.get(tender_id) if tender_id is not None else None
            records.append(self._normalize_contract(contract, procedure, detail))

        for tender_id, procedure in procedures_by_tender.items():
            if tender_id in contract_tender_ids:
                continue
            detail = tender_detail_by_id.get(tender_id)
            records.append(self._normalize_procedure(procedure, detail))

        return records

    @staticmethod
    def _normalize_contract(
        contract: dict[str, Any],
        procedure: dict[str, Any] | None,
        detail: dict[str, Any] | None,
    ) -> dict[str, Any]:
        tender_id = contract.get("TenderId")
        currency_code = contract.get("Currency")
        # The API always supplies an EUR figure (``*ValueEuro``) regardless of the
        # contract's original currency; pre-2026 contracts are in BGN (code 3) and
        # the ``Current*`` fields are the EUR-converted amounts (CurrentContractCurrency 1).
        contract_eur = contract.get("CurrentContractValueEuro")
        if contract_eur is None:
            contract_eur = contract.get("ContractValueEuro")
        if contract_eur is None:
            contract_eur, _ = _value_by_currency(contract.get("CurrentContractValue"), contract.get("CurrentContractCurrency"))
        contract_bgn = None
        if CURRENCY_CODES.get(currency_code) == "BGN":
            contract_bgn = contract.get("ContractValue")
        elif CURRENCY_CODES.get(contract.get("CurrentContractCurrency")) == "BGN":
            contract_bgn = contract.get("CurrentContractValue")

        est_eur = est_bgn = None
        title = contract.get("ContractSubject")
        if procedure:
            est_eur, est_bgn = _value_by_currency(
                procedure.get("EstimatedValue"), procedure.get("Currency")
            )
            title = title or procedure.get("TenderName")
        if detail:
            title = title or detail.get("TenderName")

        published_at = _parse_net_date(detail.get("PublicationDate")) if detail else None

        return {
            "source": "eop",
            "source_id": str(contract.get("ContractNumber")),
            "title": title,
            "cpv_code": None,
            "procedure_type": contract.get("ProcedureType"),
            "estimated_value_eur": est_eur,
            "estimated_value_bgn": est_bgn,
            "contract_value_eur": contract_eur,
            "contract_value_bgn": contract_bgn,
            "currency": CURRENCY_CODES.get(currency_code),
            "contractor_name": contract.get("SupplierName"),
            "contractor_eik": contract.get("RegisterNumberList"),
            "published_at": published_at,
            "contract_date": _parse_net_date(contract.get("ContractDate")),
            "url": f"{EOP_APP_URL}/today/{tender_id}" if tender_id else None,
            "raw_json": {"contract": contract, "procedure": procedure, "tender_detail": _slim(detail)},
        }

    @staticmethod
    def _normalize_procedure(
        procedure: dict[str, Any], detail: dict[str, Any] | None
    ) -> dict[str, Any]:
        tender_id = procedure.get("TenderId")
        est_eur, est_bgn = _value_by_currency(procedure.get("EstimatedValue"), procedure.get("Currency"))
        title = procedure.get("TenderName") or (detail.get("TenderName") if detail else None)
        published_at = _parse_net_date(detail.get("PublicationDate")) if detail else None

        return {
            "source": "eop",
            "source_id": str(tender_id),
            "title": title,
            "cpv_code": None,
            "procedure_type": procedure.get("ProcedureType"),
            "estimated_value_eur": est_eur,
            "estimated_value_bgn": est_bgn,
            "contract_value_eur": None,
            "contract_value_bgn": None,
            "currency": CURRENCY_CODES.get(procedure.get("Currency")),
            "contractor_name": None,
            "contractor_eik": None,
            "published_at": published_at,
            "contract_date": None,
            "url": f"{EOP_APP_URL}/today/{tender_id}" if tender_id else None,
            "raw_json": {"procedure": procedure, "tender_detail": _slim(detail)},
        }
