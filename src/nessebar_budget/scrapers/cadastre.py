"""Scraper for АГКК's (Агенция по геодезия, картография и кадастър) official
cadastral-map open data -- the one and only source for "Точни места"
(placing a budget object/contract at an exact point on the map).

Endpoint (verified 2026-10-08, see docs/sources/CADASTRE.md):

    GET https://kais.cadastre.bg/bg/OpenData/Download?path=<urlencoded path>
    path = "област Бургас/община Несебър/<label>/поземлени имоти.zip"

No session/auth needed, no query signing, no Last-Modified header on the
response (confirmed by inspecting real response headers -- see
`CadastreScraper.download`'s docstring), just a plain GET with a
browser-like User-Agent. Each zip holds one shapefile ("Имоти (Polygon)"
.shp/.shx/.dbf/.prj/.cpg) -- never the "собственост" (ownership/personal
data) files, which this module never requests.

Only the "поземлени имоти" (land parcels) layer is used. Fields, CRS and a
verified check value are documented in `read_parcels()` and in
docs/sources/CADASTRE.md.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import time
import zipfile
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pyproj
import shapefile

from nessebar_budget.config import get_settings
from nessebar_budget.scrapers.base import Scraper

logger = logging.getLogger(__name__)

DOWNLOAD_URL = "https://kais.cadastre.bg/bg/OpenData/Download"

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: (EKATTE, path label) for every one of Община Несебър's 14 settlements,
#: exactly as АГКК's open-data file tree spells them. Order matches the task
#: brief / `web.data.settlements.json`.
LABELS: tuple[tuple[str, str], ...] = (
    ("51500", "гр. Несебър (51500)"),
    ("53045", "гр. Обзор (53045)"),
    ("11538", "гр. Свети Влас (11538)"),
    ("02703", "с. Баня (02703)"),
    ("18469", "с. Гюльовца (18469)"),
    ("27454", "с. Емона (27454)"),
    ("37825", "с. Козница (37825)"),
    ("39164", "с. Кошарица (39164)"),
    ("53822", "с. Оризаре (53822)"),
    ("55350", "с. Паницово (55350)"),
    ("58431", "с. Приселци (58431)"),
    ("61056", "с. Равда (61056)"),
    ("62102", "с. Раковсково (62102)"),
    ("73571", "с. Тънково (73571)"),
)
LABEL_BY_EKATTE: dict[str, str] = dict(LABELS)

#: Fields carried straight through from the shapefile's `.dbf` into each
#: `read_parcels()` record, exactly as АГКК names them.
_DBF_FIELDS: tuple[str, ...] = (
    "AREA", "PERIM", "cadimm", "cadnum", "cadreg", "cattype", "ekatte",
    "ekattefn", "immaddr", "oldident", "parcel", "place", "propcode",
    "proptype", "purpcode", "purptype", "quarname", "quarter", "regname",
    "strename", "strnum", "usecode", "usetype", "validate",
)


def source_path(ekatte: str) -> str:
    """The АГКК open-data path for one settlement's land-parcels zip (the
    literal string this project cites as its source, and the value stored
    in `CadastreParcel.source_path`)."""
    label = LABEL_BY_EKATTE[ekatte]
    return f"област Бургас/община Несебър/{label}/поземлени имоти.zip"


def download_url(ekatte: str) -> str:
    return f"{DOWNLOAD_URL}?path={quote(source_path(ekatte))}"


# ---------------------------------------------------------------------------
# Shapefile geometry helpers (pure -- no network/DB)
# ---------------------------------------------------------------------------


def _find_member(names: list[str], suffix: str) -> str:
    for name in names:
        if name.lower().endswith(suffix):
            return name
    raise FileNotFoundError(f"no *{suffix} member in zip (have: {names})")


def _rings_from_shape(shape: Any) -> list[list[tuple[float, float]]]:
    points: list[tuple[float, float]] = list(shape.points)
    if not points:
        return []
    parts = list(shape.parts) + [len(points)]
    return [points[parts[i] : parts[i + 1]] for i in range(len(parts) - 1)]


def _ring_area_and_centroid(
    ring: list[tuple[float, float]],
) -> tuple[float, tuple[float, float] | None]:
    """Signed shoelace area and area-weighted centroid of one ring, in
    whatever (projected) units `ring`'s coordinates are in. Degenerate rings
    (zero signed area, e.g. a 2-3 point sliver) fall back to a plain average
    of the vertices rather than dividing by zero."""
    n = len(ring)
    if n < 3:
        return 0.0, (ring[0] if ring else None)

    signed_area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x0, y0 = ring[i]
        x1, y1 = ring[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        signed_area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    signed_area *= 0.5

    if signed_area == 0:
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return 0.0, (sum(xs) / n, sum(ys) / n)

    return signed_area, (cx / (6 * signed_area), cy / (6 * signed_area))


def _largest_part_centroid(
    rings: list[list[tuple[float, float]]],
) -> tuple[float, float] | None:
    """The area-weighted centroid of `rings`' single largest-by-area ring
    (task-sanctioned simplification for multipart polygons -- see
    docs/sources/CADASTRE.md)."""
    best_centroid: tuple[float, float] | None = None
    best_abs_area = -1.0
    for ring in rings:
        area, centroid = _ring_area_and_centroid(ring)
        if centroid is None:
            continue
        if abs(area) > best_abs_area:
            best_abs_area = abs(area)
            best_centroid = centroid
    return best_centroid


def _transform_coords(coords: Any, transformer: pyproj.Transformer) -> Any:
    if not coords:
        return coords
    if isinstance(coords[0], int | float):
        lon, lat = transformer.transform(coords[0], coords[1])
        return [round(lon, 6), round(lat, 6)]
    return [_transform_coords(c, transformer) for c in coords]


def _transform_geometry(shape: Any, transformer: pyproj.Transformer) -> dict[str, Any] | None:
    gi = shape.__geo_interface__
    coords = gi.get("coordinates")
    if not coords:
        return None
    return {"type": gi["type"], "coordinates": _transform_coords(list(coords), transformer)}


def read_parcels(zip_path: Path) -> Iterator[dict[str, Any]]:
    """Yield one dict per land parcel in a settlement's "поземлени имоти.zip"
    (as downloaded by `CadastreScraper`).

    Each dict has every field in `_DBF_FIELDS` (verbatim from the shapefile's
    `.dbf`) plus:

    - `rings_lonlat`: every ring of the parcel's polygon, each a list of
      `[lon, lat]` pairs rounded to 6 decimals, transformed from the
      shapefile's own projected CRS (read from its `.prj`) straight to
      WGS84/EPSG:4326 -- in shapefile vertex order, so `rings_lonlat[0][0]`
      is the polygon's first vertex.
    - `centroid_lonlat`: `[lon, lat]` area-weighted centroid, computed in
      the projected CRS then transformed (for a multipart polygon, the
      largest ring by area is used -- see `_largest_part_centroid`).
    - `geometry_geojson`: a GeoJSON Polygon/MultiPolygon geometry dict in
      lon/lat (ring grouping -- which rings are holes of which exterior --
      comes from `shape.__geo_interface__`, i.e. pyshp's own winding-order
      analysis).

    Verified check value (с. Равда, EKATTE 61056): parcel "61056.502.526"'s
    first vertex transforms to lon=27.67765, lat=42.64455 (both within
    1e-5 of the raw projected-to-geographic transform); `AREA` 301.17 m²;
    `immaddr` "с. Равда, ул. Св.Св. Кирил и Методий"; `proptype` "Общинска
    публична"; `usetype` "За второстепенна улица".

    Member file names inside the zip may be encoded unpredictably (seen as
    plain UTF-8 in practice, but documented by АГКК as potentially
    cp866-mangled) -- members are therefore picked by file *extension*
    (`_find_member`), never by an expected literal name.
    """
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        shp_name = _find_member(names, ".shp")
        shx_name = _find_member(names, ".shx")
        dbf_name = _find_member(names, ".dbf")
        prj_name = _find_member(names, ".prj")

        prj_wkt = zf.read(prj_name).decode("utf-8")
        crs = pyproj.CRS.from_wkt(prj_wkt)
        transformer = pyproj.Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

        reader = shapefile.Reader(
            shp=io.BytesIO(zf.read(shp_name)),
            shx=io.BytesIO(zf.read(shx_name)),
            dbf=io.BytesIO(zf.read(dbf_name)),
            encoding="utf-8",
        )

        for shape_record in reader.iterShapeRecords():
            rec = shape_record.record.as_dict()
            shape = shape_record.shape

            rings = _rings_from_shape(shape)
            rings_lonlat = [
                [
                    [round(lon, 6), round(lat, 6)]
                    for lon, lat in (transformer.transform(x, y) for x, y in ring)
                ]
                for ring in rings
            ]
            centroid_xy = _largest_part_centroid(rings)
            centroid_lonlat = None
            if centroid_xy is not None:
                clon, clat = transformer.transform(*centroid_xy)
                centroid_lonlat = [round(clon, 6), round(clat, 6)]

            out: dict[str, Any] = {field: rec.get(field) for field in _DBF_FIELDS}
            out["rings_lonlat"] = rings_lonlat
            out["centroid_lonlat"] = centroid_lonlat
            out["geometry_geojson"] = _transform_geometry(shape, transformer)
            yield out


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class CadastreScraper(Scraper):
    """Downloads (or reuses a cached copy of) every settlement's land-parcel
    open-data zip. `fetch()` only downloads -- the actual parcel rows come
    from calling `read_parcels()` on each returned `zip_path` separately
    (kept apart so the pipeline step can filter to referenced parcels/streets
    before ever constructing DB rows)."""

    name = "cadastre"

    def __init__(
        self,
        delay: float = 3.0,
        max_retries: int = 3,
        max_age_days: int = 25,
        client: httpx.Client | None = None,
    ) -> None:
        self.delay = delay
        self.max_retries = max_retries
        self.max_age_days = max_age_days

        settings = get_settings()
        self.cache_dir = Path(settings.data_dir) / "cache" / "cadastre"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

        self._owns_client = client is None
        self._client = client or httpx.Client(
            headers={"User-Agent": _UA}, timeout=60.0, follow_redirects=True
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _cache_path(self, ekatte: str) -> Path:
        return self.cache_dir / f"{ekatte}_parcels.zip"

    def _cache_is_fresh(self, cache_file: Path) -> bool:
        age_days = (time.time() - cache_file.stat().st_mtime) / 86400
        return age_days <= self.max_age_days

    def download(self, ekatte: str) -> tuple[Path, dt.date]:
        """Download (or reuse a fresh cached copy of) one settlement's
        land-parcels zip. Returns `(zip_path, source_modified)`.

        АГКК's response carries no `Last-Modified` header (confirmed by
        inspecting real response headers against this endpoint,
        2026-10-08) -- `source_modified` is therefore always *today*
        (the download date) for a freshly-downloaded file, or the cached
        file's own mtime-derived date when reused from cache.
        """
        cache_file = self._cache_path(ekatte)
        if cache_file.exists() and self._cache_is_fresh(cache_file):
            modified = dt.datetime.fromtimestamp(cache_file.stat().st_mtime, tz=dt.UTC).date()
            return cache_file, modified

        last_exc: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = self._client.get(download_url(ekatte))
                response.raise_for_status()
                cache_file.write_bytes(response.content)
                time.sleep(self.delay)
                last_modified = response.headers.get("Last-Modified")
                modified = (
                    parsedate_to_datetime(last_modified).date()
                    if last_modified
                    else dt.datetime.now(tz=dt.UTC).date()
                )
                return cache_file, modified
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "cadastre %s download attempt %d/%d failed: %s",
                    ekatte, attempt, self.max_retries, exc,
                )
                if attempt < self.max_retries:
                    time.sleep(2**attempt)

        assert last_exc is not None
        raise last_exc

    def fetch(self) -> list[dict[str, Any]]:
        """Download every settlement's zip; return one summary dict per
        settlement: `{"ekatte", "label", "path", "zip_path",
        "source_modified"}`. Call `read_parcels(zip_path)` separately to
        actually read parcel rows out of it."""
        records: list[dict[str, Any]] = []
        for ekatte, label in LABELS:
            zip_path, modified = self.download(ekatte)
            records.append(
                {
                    "ekatte": ekatte,
                    "label": label,
                    "path": source_path(ekatte),
                    "zip_path": str(zip_path),
                    "source_modified": modified,
                }
            )
        return records


# ---------------------------------------------------------------------------
# Reference collection: which parcels/streets does this project's own data
# actually name? (keeps the committed data/nessebar.db small -- see module
# docstring and docs/sources/CADASTRE.md)
# ---------------------------------------------------------------------------

#: Mirrors `web.build.CONSOLIDATED_UNIT`/`_row_type` without importing that
#: (heavier, jinja2-dependent) module from a scraper -- the capital ledger's
#: consolidated unit name and the `extra_json["row_type"]` value marking an
#: actual object row (not a subtotal/grand-total row).
_CONSOLIDATED_UNIT = "Общо"
_OBJECT_ROW_TYPE = "object"


def collect_reference_corpus(session: Any) -> tuple[list[str], list[str]]:
    """Distinct capital-programme object names (consolidated `Общо` unit,
    row type `object`) and distinct `eop` procurement titles -- the text
    this project's own data actually contains, i.e. everything a budget
    object/contract page could ever need a cadastral parcel/street for."""
    from sqlalchemy import select

    from nessebar_budget.db.budget_models import BudgetLineItem
    from nessebar_budget.db.models import Procurement

    object_names: set[str] = set()
    rows = session.execute(
        select(BudgetLineItem.object_name, BudgetLineItem.extra_json).where(
            BudgetLineItem.unit == _CONSOLIDATED_UNIT
        )
    )
    for name, extra_json in rows:
        if not name:
            continue
        row_type = (extra_json or {}).get("row_type")
        if row_type == _OBJECT_ROW_TYPE:
            object_names.add(name)

    titles = {
        t
        for t in session.scalars(
            select(Procurement.title).where(Procurement.source == "eop").distinct()
        )
        if t
    }
    return sorted(object_names), sorted(titles)


def referenced_parcels_and_streets(
    object_names: list[str], titles: list[str]
) -> tuple[set[str], dict[str, set[str]]]:
    """-> (parcel_lookup_keys, street_keys_by_ekatte).

    `parcel_lookup_keys` is every parcel id (always collapsed to its 3-part
    parent) any budget object or contract names. `street_keys_by_ekatte` is,
    for every settlement a text could plausibly be in (its full ordered
    candidate list -- see `web.geo.ekatte_candidates`), every street
    `street_key()` that text names; stored for *every* candidate rather than
    only the eventually-unique one so the real tie-break (`locate.resolve`'s
    "exactly one settlement matches") has real data to run against at build
    time -- this only means a few extra street parcels get stored for a
    settlement that ultimately isn't the match, not a correctness issue.
    """
    from nessebar_budget.web import geo
    from nessebar_budget.web.locate import (
        extract_parcel_ids,
        extract_streets,
        parcel_lookup_keys,
        street_key,
    )

    parcel_keys: set[str] = set()
    streets_by_ekatte: dict[str, set[str]] = {}

    def _add_streets(text: str, candidates: list[str]) -> None:
        for name in extract_streets(text):
            key = street_key(name)
            if not key:
                continue
            for ekatte in candidates:
                streets_by_ekatte.setdefault(ekatte, set()).add(key)

    for name in object_names:
        parcel_keys.update(parcel_lookup_keys(extract_parcel_ids(name)))
        candidates = geo.ekatte_candidates(geo.object_settlements(name))
        _add_streets(name, candidates)

    for title in titles:
        parcel_keys.update(parcel_lookup_keys(extract_parcel_ids(title)))
        candidates = geo.ekatte_candidates(geo.detect_settlements(title))
        _add_streets(title, candidates)

    return parcel_keys, streets_by_ekatte


def sync_cadastre(session: Any, scraper: CadastreScraper | None = None) -> dict[str, Any]:
    """Download every settlement's open-data zip, keep only the parcels this
    project's own data references (plus the street parcels of a referenced
    street), and replace each settlement's `CadastreParcel` rows. Does not
    commit -- the caller controls the transaction boundary.

    Returns a summary dict: per-settlement parcel counts and totals, used by
    both the `scrape cadastre` CLI command and the pipeline step.
    """
    from nessebar_budget.db.repo import replace_cadastre_parcels
    from nessebar_budget.web.locate import street_key

    scraper = scraper or CadastreScraper()
    object_names, titles = collect_reference_corpus(session)
    parcel_keys, streets_by_ekatte = referenced_parcels_and_streets(object_names, titles)

    per_settlement: list[dict[str, Any]] = []
    total_stored = 0
    for ekatte, label in LABELS:
        zip_path, modified = scraper.download(ekatte)
        street_keys = streets_by_ekatte.get(ekatte, set())

        kept: list[dict[str, Any]] = []
        for parcel in read_parcels(zip_path):
            cadnum = parcel.get("cadnum")
            is_referenced_id = cadnum in parcel_keys
            strename = parcel.get("strename")
            is_street_match = bool(
                strename
                and street_keys
                and street_key(strename) in street_keys
                and _is_street_usetype(parcel.get("usetype"))
            )
            if is_referenced_id or is_street_match:
                kept.append(parcel)

        stored = replace_cadastre_parcels(session, ekatte, kept, source_path(ekatte), modified)
        total_stored += stored
        per_settlement.append(
            {
                "ekatte": ekatte,
                "label": label,
                "path": source_path(ekatte),
                "source_modified": modified.isoformat(),
                "stored": stored,
            }
        )

    return {
        "object_names": len(object_names),
        "titles": len(titles),
        "referenced_parcel_ids": len(parcel_keys),
        "referenced_street_keys": sum(len(v) for v in streets_by_ekatte.values()),
        "total_parcels_stored": total_stored,
        "per_settlement": per_settlement,
    }


#: Mirrors `web.geo._STREET_USETYPE_SUBSTRINGS` (kept as its own small local
#: copy rather than an import -- both are plain literal tuples, and this one
#: is consulted while deciding what to *store*, independent of how
#: `web.geo`/`locate.resolve` later decide what to *match*).
_STREET_USETYPE_SUBSTRINGS: tuple[str, ...] = ("улица", "алея", "площад", "път")


def _is_street_usetype(usetype: str | None) -> bool:
    if not usetype:
        return False
    return any(s in usetype for s in _STREET_USETYPE_SUBSTRINGS)
