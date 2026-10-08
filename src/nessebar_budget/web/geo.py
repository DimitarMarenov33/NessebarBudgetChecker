"""Settlement geography for "Карта на разходите" (the map page).

Deterministic, offline: `SETTLEMENTS` is loaded once from the static
`data/settlements.json` file next to this module (lat/lon fetched once from
OpenStreetMap Nominatim, EKATTE codes cross-checked against Wikipedia's
settlement infobox and against the KAIS cadastral-identifier prefixes seen in
this project's own scraped data -- see that file's "_readme" and each entry's
"source"). No function in this module ever makes a network call: the build
must work fully offline.

Settlement *detection* (`detect_settlement`/`detect_settlements`) is pure
text matching -- alias strings matched case-insensitively with word
boundaries, plus KAIS cadastral identifiers ("ПИ 51500.501.4") resolved via
their EKATTE prefix -- never a geocoding guess. When several settlements are
named in the same text, the one appearing earliest wins for
`detect_settlement`; `detect_settlements` returns the full ordered list so
callers can flag the text as ambiguous (`len(...) > 1`).

`build_map_data()` aggregates the budget ledger, contracts and flags by
settlement (plus curated pins from `data/locations/pins.csv`) into the dict
`map.html`/`settlements/*.html` render from, and that gets exported as
`data/map.json` (GeoJSON) and `data/settlements.csv`.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from nessebar_budget.web.locate import resolve, street_key

WEB_DIR = Path(__file__).resolve().parent
SETTLEMENTS_JSON_PATH = WEB_DIR / "data" / "settlements.json"
PINS_CSV_RELATIVE = Path("data") / "locations" / "pins.csv"
PROJECT_ROOT = WEB_DIR.parents[2]
PINS_CSV_PATH = PROJECT_ROOT / PINS_CSV_RELATIVE

PINS_CSV_FIELDS = [
    "object_name", "period", "lat", "lon", "cadastral_id",
    "verified_by", "source_note", "verified_at",
]

#: KAIS cadastral identifier, e.g. "51500.501.4" or "51500.506.493.1" --
#: EKATTE (5 digits), then 2-3 more dot-separated numeric segments.
CADASTRAL_ID_RE = re.compile(r"\b(\d{5})(?:\.\d+){2,3}\b")

#: The one real ambiguity in the alias list: "Несебър" is both a settlement
#: name and the municipality's own name, and "Община Несебър" / "общ.
#: Несебър" / "общ.Несебър" (the buyer/contracting-authority name, or "...
#: of Nessebar municipality") appears in the great majority of procurement
#: titles without describing *where* the object/contract is. Only the bare
#: "Несебър" alias is at risk of this false positive (every other nesebar
#: alias already carries a "гр." prefix that never follows "Община"/"общ."
#: in real text) -- see detect_settlements() below.
_NESEBAR_ORG_PRECEDING_RE = re.compile(r"(?:община|общ\.)\s*$", re.IGNORECASE)


def _load_settlements_raw() -> list[dict[str, Any]]:
    data = json.loads(SETTLEMENTS_JSON_PATH.read_text(encoding="utf-8"))
    return data["settlements"]


#: Loaded once at import time from the static JSON file -- never refetched,
#: never touches the network.
SETTLEMENTS: list[dict[str, Any]] = _load_settlements_raw()

SETTLEMENTS_BY_KEY: dict[str, dict[str, Any]] = {s["key"]: s for s in SETTLEMENTS}

#: EKATTE code -> settlement key, for the cadastral-identifier fallback.
#: Resort areas (no EKATTE of their own) are deliberately absent here --
#: their cadastral identifiers use their enclosing town's EKATTE, which
#: correctly resolves to that town, not to the resort.
EKATTE_TO_KEY: dict[str, str] = {
    s["ekatte"]: s["key"] for s in SETTLEMENTS if s.get("ekatte")
}

#: Settlement key -> EKATTE, for the two resort areas that have no EKATTE of
#: their own ("Точни места" needs a concrete 5-digit code to look up
#: `CadastreParcel`/street data against) -- they sit in their enclosing
#: town's землище (see `settlements.json`'s `ekatte_territory`).
_RESORT_EKATTE: dict[str, str] = {"slanchev-bryag": "51500", "elenite": "11538"}


def settlement_ekatte(key: str) -> str | None:
    """The EKATTE code to use for cadastral lookups for settlement `key` --
    the settlement's own EKATTE, or (for the two resort areas) their
    enclosing town's, via `_RESORT_EKATTE`."""
    settlement = SETTLEMENTS_BY_KEY.get(key)
    if settlement and settlement.get("ekatte"):
        return settlement["ekatte"]
    return _RESORT_EKATTE.get(key)


def ekatte_candidates(keys: list[str]) -> list[str]:
    """`keys` (settlement keys, in priority order, e.g. from
    `object_settlements`/`detect_settlements`) mapped to EKATTE codes,
    deduplicated, order preserved -- the `ekatte_candidates` argument
    `locate.resolve()` expects."""
    out: list[str] = []
    for key in keys:
        ekatte = settlement_ekatte(key)
        if ekatte and ekatte not in out:
            out.append(ekatte)
    return out


def _alias_regex_fragment(alias: str) -> str:
    """Turn a literal alias string into a regex fragment that also matches
    the common punctuation/spacing variants seen in this project's scraped
    text: a "." may or may not actually be present (optionally followed by
    whitespace), and any run of whitespace may be wider/narrower than
    written ("Св.Влас", "Св. Влас" and "Св.  Влас" all match the one alias
    "Св. Влас"). Every other character is matched literally."""
    parts: list[str] = []
    for ch in alias:
        if ch == ".":
            parts.append(r"\.?\s*")
        elif ch.isspace():
            parts.append(r"\s*")
        else:
            parts.append(re.escape(ch))
    return "".join(parts)


def _compile_alias(alias: str) -> re.Pattern[str]:
    return re.compile(r"\b" + _alias_regex_fragment(alias) + r"\b", re.IGNORECASE)


#: alias string -> compiled pattern, built once.
_ALIAS_PATTERNS: dict[str, re.Pattern[str]] = {
    alias: _compile_alias(alias) for s in SETTLEMENTS for alias in s["aliases"]
}


def detect_settlements(text: str | None) -> list[str]:
    """All settlement keys named in `text`, ordered by first occurrence.

    Deterministic text matching only -- no geocoding, no fuzzy matching:

    - every settlement's `aliases` (case-insensitive, word-boundary,
      punctuation/whitespace-flexible -- see `_alias_regex_fragment`), and
    - KAIS cadastral identifiers ("ПИ 51500.501.4") resolved via their
      5-digit EKATTE prefix (`EKATTE_TO_KEY`).

    The bare "Несебър" alias is suppressed when it's clearly just naming the
    *municipality* as the contracting authority ("Община Несебър", "общ.
    Несебър") rather than the town as a location -- see
    `_NESEBAR_ORG_PRECEDING_RE`. Every other alias for every other
    settlement is unambiguous in this project's data (see docs/SITE.md).
    """
    if not text:
        return []

    earliest: dict[str, int] = {}

    for settlement in SETTLEMENTS:
        key = settlement["key"]
        for alias in settlement["aliases"]:
            pattern = _ALIAS_PATTERNS[alias]
            for m in pattern.finditer(text):
                if (
                    key == "nesebar"
                    and alias == "Несебър"
                    and _NESEBAR_ORG_PRECEDING_RE.search(text[: m.start()])
                ):
                    continue
                pos = m.start()
                if key not in earliest or pos < earliest[key]:
                    earliest[key] = pos

    for m in CADASTRAL_ID_RE.finditer(text):
        key = EKATTE_TO_KEY.get(m.group(1))
        if key is None:
            continue
        pos = m.start()
        if key not in earliest or pos < earliest[key]:
            earliest[key] = pos

    return [key for key, _pos in sorted(earliest.items(), key=lambda kv: kv[1])]


def detect_settlement(text: str | None) -> str | None:
    """The first settlement named in `text`, or None. See
    `detect_settlements` for the full ordered list and the matching rules."""
    found = detect_settlements(text)
    return found[0] if found else None


def object_settlements(name: str | None) -> list[str]:
    """Settlements named by a capital-programme object, the official location
    first.

    Column B of the capital programme is "наименование, местонахождение и
    функционално предназначение": the municipality writes the object and
    then, after a final comma, where it is ("... Слънчев бряг - Тънково,
    с.Тънково"; "... мост между к.к. Слънчев бряг - с.Тънково, гр.Несебър").
    When the text after the last comma names exactly one settlement, that is
    the object's location and is returned first; places mentioned earlier
    in the name (a road's two ends, a neighbouring resort) follow. Without
    such a suffix this falls back to `detect_settlements` order.
    """
    found = detect_settlements(name)
    if name and "," in name:
        tail_keys = detect_settlements(name.rsplit(",", 1)[1])
        if len(tail_keys) == 1:
            key = tail_keys[0]
            return [key] + [k for k in found if k != key]
    return found


def cadastral_ids(text: str | None) -> list[str]:
    """Every KAIS cadastral identifier in `text` ("51500.501.4",
    "51500.506.493.1", ...), in order of first occurrence, deduplicated.
    Unlike `detect_settlements`, this is not restricted to this
    municipality's own EKATTE codes -- it's a generic extractor, used to
    show/copy identifiers regardless of whether they resolve to a settlement
    here."""
    if not text:
        return []
    full_re = re.compile(r"\b\d{5}(?:\.\d+){2,3}\b")
    return list(dict.fromkeys(m.group(0) for m in full_re.finditer(text)))


def kais_url(identifier: str | None = None) -> str:
    """Link to the official KAIS (АГКК) cadastral map.

    Verified 2026-10-08: https://kais.cadastre.bg/bg/Map is a client-rendered
    single-page app with no documented (or discoverable by probing query
    strings) deep-link format that opens a specific identifier directly --
    `?identifier=<id>` and similar return the same empty map shell as the
    bare URL. So this always returns the plain map page; callers should show
    `identifier` as plain text next to the link for the reader to paste into
    KAIS's own search box themselves. `identifier` is accepted (and ignored)
    so call sites read naturally and so a future verified deep-link format
    only needs to change this one function.
    """
    return "https://kais.cadastre.bg/bg/Map"


# ---------------------------------------------------------------------------
# Curated pins (data/locations/pins.csv)
# ---------------------------------------------------------------------------


def load_pins(path: Path | None = None) -> list[dict[str, Any]]:
    """Volunteer-curated exact pins (see data/locations/README.md). Returns
    [] if the file doesn't exist yet or has no data rows -- pins are a
    strictly optional second layer on top of the settlement-level map."""
    csv_path = path or PINS_CSV_PATH
    if not csv_path.exists():
        return []
    pins: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            lat_raw, lon_raw = (row.get("lat") or "").strip(), (row.get("lon") or "").strip()
            if not lat_raw or not lon_raw:
                continue
            try:
                lat, lon = float(lat_raw), float(lon_raw)
            except ValueError:
                continue
            pins.append(
                {
                    "object_name": (row.get("object_name") or "").strip(),
                    "period": (row.get("period") or "").strip() or None,
                    "lat": lat,
                    "lon": lon,
                    "cadastral_id": (row.get("cadastral_id") or "").strip() or None,
                    "verified_by": (row.get("verified_by") or "").strip() or None,
                    "source_note": (row.get("source_note") or "").strip() or None,
                    "verified_at": (row.get("verified_at") or "").strip() or None,
                }
            )
    return pins


def ensure_pins_scaffold(path: Path | None = None) -> None:
    """Create `data/locations/pins.csv` (header row only) and its README if
    they don't exist yet. Safe to call on every build -- never overwrites an
    existing pins.csv."""
    csv_path = path or PINS_CSV_PATH
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists():
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(PINS_CSV_FIELDS)
    readme_path = csv_path.parent / "README.md"
    if not readme_path.exists():
        readme_path.write_text(_PINS_README, encoding="utf-8")


_PINS_README = """\
# Точни координати на обекти (`pins.csv`)

Картата на сайта ("Карта на разходите") показва всяко населено място като
една точка, с общите суми за всичките му обекти -- тя е достатъчна за да се
види *накъде* отиват парите, но не показва точното местоположение на всеки
отделен обект (улица, детска площадка, сграда). `pins.csv` е доброволен,
проверен от човек слой върху тази карта: по един ред за всеки обект, чиито
истински координати някой е намерил и проверил.

## Как да добавите точен пин

1. Отворете `budget/<период>.html` на сайта (или `data/budget_line_items.csv`)
   и намерете обекта, чиито координати искате да добавите -- запишете точното
   му име (`object_name`) и месеца (`period`, формат `YYYY-MM`), точно както
   са показани там.
2. Потърсете местоположението в Кадастралната карта на АГКК (КАИС):
   https://kais.cadastre.bg/bg/Map -- ако обектът споменава идентификатор по
   кадастралната карта (например „ПИ 51500.501.4“), въведете го в търсачката
   на КАИС; иначе търсете по адрес/квартал/населено място. КАИС показва
   координатите на имота след като го намерите и го изберете на картата
   (обикновено в WGS84/EPSG:4326 -- това очаква и тази таблица).
3. Добавете нов ред в `pins.csv` със следните колони:

   | Колона | Съдържание |
   |---|---|
   | `object_name` | Точното име на обекта, както е в бюджета |
   | `period` | Месецът на справката, от която е взет обекта (`YYYY-MM`) |
   | `lat` | Географска ширина (десетична, WGS84) |
   | `lon` | Географска дължина (десетична, WGS84) |
   | `cadastral_id` | Идентификатор по кадастралната карта, ако е известен (напр. `51500.501.4`) |
   | `verified_by` | Вашето име/псевдоним (или "анонимен доброволец") |
   | `source_note` | Кратка бележка как сте намерили точката (напр. "КАИС, ПИ 51500.501.4, кв.65") |
   | `verified_at` | Дата на проверка, `YYYY-MM-DD` |

4. Качете файла / направете pull request. При следващото генериране на сайта
   („build-site“) пинът автоматично се появява като втори слой на картата.

Не се изисква запис за всеки обект -- само за тези, за които някой е
проверил и потвърдил точно местоположение. Обектите без пин продължават да
се броят в общата сума на населеното място.
"""


# ---------------------------------------------------------------------------
# Per-settlement aggregation
# ---------------------------------------------------------------------------


def _year_of_period(period: str) -> int | None:
    try:
        return int(period[:4])
    except (TypeError, ValueError):
        return None


def _representative_periods(all_periods: list[str]) -> dict[int, tuple[str, bool]]:
    """year -> (period, used_fallback). Prefers that year's December
    ("<year>-12") snapshot (the ledger is cumulative within a fiscal year, so
    December is that year's full-year total); when a year has no December
    report yet (current year in progress, or a gap in what's been scraped),
    falls back to that year's latest available period and flags it so
    callers can say so."""
    by_year: dict[int, list[str]] = defaultdict(list)
    for p in all_periods:
        y = _year_of_period(p)
        if y is not None:
            by_year[y].append(p)
    result: dict[int, tuple[str, bool]] = {}
    for y, periods in by_year.items():
        dec = f"{y}-12"
        if dec in periods:
            result[y] = (dec, False)
        else:
            result[y] = (max(periods), True)
    return result


def _empty_bucket() -> dict[str, Any]:
    return {"plan": 0.0, "spent": 0.0, "objects_count": 0}


def build_map_data(
    session: Any,
    contracts_view_list: list[Any],
    flags: list[dict[str, Any]] | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate the budget ledger, contracts and flags by settlement.

    `session`/`budget` give access to the capital-ledger rows exactly the
    way `build.py`'s own budget pages see them (same helpers, same
    `CONSOLIDATED_UNIT`/`row_type` filtering -- see the deferred import
    below), so settlement totals plus the `unassigned` bucket always add up
    to the same grand totals `budget/<period>.html` shows. `flags` should be
    the already-rendered `_flag_view()` dicts (optional -- flags_count is 0
    for every settlement without them).
    """
    # Deferred import: build.py imports this module, so importing build.py
    # back at module load time would be a circular import. By the time this
    # function actually runs (during build_site()), build.py is already
    # fully loaded, so a function-local import works fine. This is also
    # exactly what the task asked for: reuse build.py's own _load_budget/
    # _row_type instead of re-implementing the row_type/consolidated-unit
    # filtering here.
    from nessebar_budget.web.build import _load_budget

    if budget is None:
        budget = _load_budget(session)

    periods: list[str] = budget["periods"]
    by_period: dict[str, dict[str, Any]] = budget["by_period"]
    rep_periods = _representative_periods(periods)

    settlement_years: dict[str, dict[int, dict[str, Any]]] = {
        s["key"]: {} for s in SETTLEMENTS
    }
    settlement_objects: dict[str, list[dict[str, Any]]] = {s["key"]: [] for s in SETTLEMENTS}
    unassigned_years: dict[int, dict[str, Any]] = {}
    unassigned_objects: list[dict[str, Any]] = []
    notes: dict[int, str] = {}

    for year, (period, used_fallback) in sorted(rep_periods.items()):
        if used_fallback:
            notes[year] = (
                f"{year} няма декемврийски отчет -- показани са данните от "
                f"последния наличен месец на годината ({period})."
            )
        period_data = by_period.get(period)
        if not period_data:
            continue

        year_buckets: dict[str, dict[str, Any]] = defaultdict(_empty_bucket)
        unassigned_bucket = _empty_bucket()

        for obj in period_data["objects"]:
            name = obj["object_name"] or ""
            keys = object_settlements(name)
            ids = cadastral_ids(name)
            plan = obj["plan_current"] or 0.0
            spent = obj["spent_period"] or 0.0
            href = f"budget/{period}.html"
            row = {
                "name": name,
                "period": period,
                "plan": plan,
                "spent": spent,
                "cadastral_ids": ids,
                "href": href,
                "ambiguous": len(keys) > 1,
                "settlements": keys,
            }
            if not keys:
                bucket = unassigned_bucket
                bucket["plan"] += plan
                bucket["spent"] += spent
                bucket["objects_count"] += 1
                unassigned_objects.append(row)
                continue
            primary = keys[0]
            bucket = year_buckets[primary]
            bucket["plan"] += plan
            bucket["spent"] += spent
            bucket["objects_count"] += 1
            settlement_objects[primary].append(row)

        for key, bucket in year_buckets.items():
            settlement_years[key][year] = bucket
        unassigned_years[year] = unassigned_bucket

    # -- contracts: count/value by settlement, matched on the contract title.
    contract_counts: dict[str, int] = defaultdict(int)
    contract_totals: dict[str, float] = defaultdict(float)
    contract_items: dict[str, list[Any]] = defaultdict(list)
    for c in contracts_view_list:
        keys = detect_settlements(getattr(c, "title", None))
        value = getattr(c, "display_value_eur", None) or 0.0
        for key in keys:
            contract_counts[key] += 1
            contract_totals[key] += value
            contract_items[key].append(c)

    # -- flags: counted on details_json.object_name, falling back to the
    # linked subject's label (a contract's title, for contract-type flags).
    flags_count: dict[str, int] = defaultdict(int)
    flags_by_settlement: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in flags or []:
        details = f.get("details") or {}
        text = details.get("object_name") or f.get("subject_label") or ""
        for key in detect_settlements(text):
            flags_count[key] += 1
            flags_by_settlement[key].append(f)

    years_available = sorted(rep_periods.keys())

    settlements_out: list[dict[str, Any]] = []
    for s in SETTLEMENTS:
        key = s["key"]
        years = settlement_years[key]
        all_plan = sum(b["plan"] for b in years.values())
        all_spent = sum(b["spent"] for b in years.values())
        all_objects = sum(b["objects_count"] for b in years.values())
        settlements_out.append(
            {
                **s,
                "href": f"settlements/{key}.html",
                "years": years,
                "all_years": {
                    "plan": all_plan,
                    "spent": all_spent,
                    "objects_count": all_objects,
                },
                "objects": settlement_objects[key],
                "contracts": {
                    "count": contract_counts.get(key, 0),
                    "total_eur": contract_totals.get(key, 0.0),
                    "list": sorted(
                        contract_items.get(key, []),
                        key=lambda c: (
                            getattr(c, "contract_date", None)
                            or getattr(c, "published_at", None)
                            or dt.datetime.min  # noqa: DTZ901 -- naive sentinel, see build.py's _NAIVE_MIN
                        ),
                        reverse=True,
                    ),
                },
                "flags_count": flags_count.get(key, 0),
                "flags": flags_by_settlement.get(key, []),
            }
        )

    unassigned = {
        "years": unassigned_years,
        "all_years": {
            "plan": sum(b["plan"] for b in unassigned_years.values()),
            "spent": sum(b["spent"] for b in unassigned_years.values()),
            "objects_count": sum(b["objects_count"] for b in unassigned_years.values()),
        },
        "objects": unassigned_objects,
    }

    pins = load_pins()

    return {
        "settlements": settlements_out,
        "unassigned": unassigned,
        "years_available": years_available,
        "notes": notes,
        "pins": pins,
    }


def export_map_geojson(map_data: dict[str, Any]) -> dict[str, Any]:
    """`map_data` (from `build_map_data`) as a GeoJSON FeatureCollection --
    one Point feature per settlement, plus the `unassigned` totals and
    curated `pins` as extra top-level members (ignored by strict GeoJSON
    readers, consumed directly by `map.html`'s own script)."""
    features = []
    for s in map_data["settlements"]:
        years_props = {
            str(year): {
                "plan": round(bucket["plan"], 2),
                "spent": round(bucket["spent"], 2),
                "objects_count": bucket["objects_count"],
            }
            for year, bucket in sorted(s["years"].items())
        }
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [s["lon"], s["lat"]]},
                "properties": {
                    "key": s["key"],
                    "name": s["name"],
                    "kind": s["kind"],
                    "ekatte": s.get("ekatte"),
                    "href": s["href"],
                    "all_years": {
                        "plan": round(s["all_years"]["plan"], 2),
                        "spent": round(s["all_years"]["spent"], 2),
                        "objects_count": s["all_years"]["objects_count"],
                    },
                    "years": years_props,
                    "contracts_count": s["contracts"]["count"],
                    "contracts_total_eur": round(s["contracts"]["total_eur"], 2),
                    "flags_count": s["flags_count"],
                },
            }
        )

    unassigned = map_data["unassigned"]
    return {
        "type": "FeatureCollection",
        "features": features,
        "years_available": map_data["years_available"],
        "notes": map_data["notes"],
        "unassigned": {
            "all_years": {
                "plan": round(unassigned["all_years"]["plan"], 2),
                "spent": round(unassigned["all_years"]["spent"], 2),
                "objects_count": unassigned["all_years"]["objects_count"],
            },
            "years": {
                str(y): {
                    "plan": round(b["plan"], 2),
                    "spent": round(b["spent"], 2),
                    "objects_count": b["objects_count"],
                }
                for y, b in sorted(unassigned["years"].items())
            },
        },
        "pins": map_data["pins"],
    }


def export_settlements_csv(map_data: dict[str, Any], path: Path) -> None:
    fieldnames = [
        "key", "name", "kind", "ekatte", "lat", "lon",
        "all_years_plan_eur", "all_years_spent_eur", "objects_count",
        "contracts_count", "contracts_total_eur", "flags_count",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in map_data["settlements"]:
            writer.writerow(
                {
                    "key": s["key"],
                    "name": s["name"],
                    "kind": s["kind"],
                    "ekatte": s.get("ekatte") or "",
                    "lat": s["lat"],
                    "lon": s["lon"],
                    "all_years_plan_eur": round(s["all_years"]["plan"], 2),
                    "all_years_spent_eur": round(s["all_years"]["spent"], 2),
                    "objects_count": s["all_years"]["objects_count"],
                    "contracts_count": s["contracts"]["count"],
                    "contracts_total_eur": round(s["contracts"]["total_eur"], 2),
                    "flags_count": s["flags_count"],
                }
            )
        unassigned = map_data["unassigned"]
        writer.writerow(
            {
                "key": "unassigned",
                "name": "Без разпознато населено място",
                "kind": "",
                "ekatte": "",
                "lat": "",
                "lon": "",
                "all_years_plan_eur": round(unassigned["all_years"]["plan"], 2),
                "all_years_spent_eur": round(unassigned["all_years"]["spent"], 2),
                "objects_count": unassigned["all_years"]["objects_count"],
                "contracts_count": "",
                "contracts_total_eur": "",
                "flags_count": "",
            }
        )


# ---------------------------------------------------------------------------
# "Точни места" -- exact-location resolution for budget objects/contracts
# ---------------------------------------------------------------------------

#: `usetype` substrings that mark a parcel as a "street parcel" for street
#: matching (task-specified, verbatim): a parcel whose own `usetype` doesn't
#: contain one of these is never matched by street name, even if it happens
#: to carry a non-empty `strename` (e.g. a building parcel with a street
#: address is not itself "the street").
_STREET_USETYPE_SUBSTRINGS: tuple[str, ...] = ("улица", "алея", "площад", "път")


def _is_street_parcel(usetype: str | None, street: str | None) -> bool:
    if not street or not usetype:
        return False
    return any(s in usetype for s in _STREET_USETYPE_SUBSTRINGS)


def _attr(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


#: `CadastreParcel` columns copied into the plain dict `locate.resolve()`
#: works with. Accepts either ORM rows (production) or plain dicts with the
#: same keys (tests) -- see `_attr`.
_PARCEL_FIELDS: tuple[str, ...] = (
    "cadnum", "ekatte", "settlement", "area_m2", "address", "street",
    "street_number", "proptype", "purptype", "usetype", "quarter",
    "centroid_lat", "centroid_lon", "geometry_geojson", "source_path",
    "source_modified",
)


def _parcel_to_dict(row: Any) -> dict[str, Any]:
    return {field: _attr(row, field) for field in _PARCEL_FIELDS}


def _build_street_index(
    parcels: list[dict[str, Any]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    index: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    for p in parcels:
        if not _is_street_parcel(p.get("usetype"), p.get("street")):
            continue
        key = street_key(p.get("street"))
        if not key:
            continue
        index[p["ekatte"]][key].append(p)
    return index


def _location_id(kind: str, key: str) -> str:
    import hashlib

    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    return f"{kind}-{digest}"


def _source_label(parcels: list[dict[str, Any]]) -> str | None:
    if not parcels:
        return None
    p = parcels[0]
    path = p.get("source_path")
    if not path:
        return None
    modified = p.get("source_modified")
    modified_str = modified.isoformat() if hasattr(modified, "isoformat") else (modified or "?")
    return f"АГКК отворени данни, {path}, версия {modified_str}"


def _budget_object_groups(budget: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every distinct capital-programme object (consolidated `Общо` unit,
    row type `object`), keyed on its own name, with its plan/spent summed
    per year (same representative-period-per-year rule as
    `build_map_data`/`_representative_periods`) and the latest period it
    appears in (used for its `href`)."""
    periods: list[str] = budget["periods"]
    by_period: dict[str, dict[str, Any]] = budget["by_period"]
    rep_periods = _representative_periods(periods)

    groups: dict[str, dict[str, Any]] = {}
    for year, (period, _used_fallback) in sorted(rep_periods.items()):
        period_data = by_period.get(period)
        if not period_data:
            continue
        for obj in period_data["objects"]:
            name = obj["object_name"] or ""
            if not name:
                continue
            group = groups.setdefault(name, {"name": name, "years": {}, "latest_period": period})
            group["years"][year] = {
                "plan": obj["plan_current"] or 0.0,
                "spent": obj["spent_period"] or 0.0,
            }
            group["latest_period"] = max(group["latest_period"], period)
    return groups


def _flag_summary(flag_view: dict[str, Any]) -> dict[str, Any]:
    # `permalink_href` is built relative to a first-level page (e.g.
    # "../flags/1.html" -- see `build.py`'s `_flag_view`/`site_path`); every
    # `href` in a location feature (this module's own `budget/<period>.html`/
    # `contracts/<id>.html`) is instead already site-root-relative, so the
    # leading "../" is stripped here for consistency -- callers (map.html's
    # script, every template) always combine these with their *own* `root`.
    href = flag_view.get("permalink_href")
    if href:
        href = href.removeprefix("../")
    return {
        "id": flag_view.get("id"),
        "rule_label": flag_view.get("rule_label"),
        "tier": flag_view.get("tier_key") or flag_view.get("tier"),
        "href": href,
    }


def _build_feature(
    location_id: str,
    kind: str,
    title: str | None,
    settlement_key: str | None,
    loc: Any,
    amounts: dict[str, Any],
    href: str,
    flag_views: list[dict[str, Any]],
    year: int | None = None,
) -> dict[str, Any]:
    rep_parcel = loc.parcels[0] if loc.parcels else None
    settlement = SETTLEMENTS_BY_KEY[settlement_key]["name"] if settlement_key else None
    if kind == "contract":
        display_amount = amounts.get("value_eur") or 0.0
    else:
        display_amount = sum(b["spent"] for b in amounts.values())
    return {
        "id": location_id,
        "kind": kind,
        "title": title,
        "settlement": settlement,
        "settlement_key": settlement_key,
        "precision": loc.precision,
        "cadnums": list(loc.ids) if loc.precision == "parcel" else None,
        "street": loc.street if loc.precision == "street" else None,
        "address": rep_parcel.get("address") if rep_parcel else None,
        "area_m2": rep_parcel.get("area_m2") if rep_parcel else None,
        "proptype": rep_parcel.get("proptype") if rep_parcel else None,
        "usetype": rep_parcel.get("usetype") if rep_parcel else None,
        "amounts": amounts,
        "display_amount": display_amount,
        #: Contract year (Sofia time, same value `contracts/index.html`
        #: groups by) -- used by map.html's year filter for a contract
        #: marker; `None` for a budget object, which instead has one key per
        #: year directly inside `amounts`.
        "year": year,
        "href": href,
        "flags": [_flag_summary(f) for f in flag_views],
        "unresolved": list(loc.unresolved),
        "source": _source_label(loc.parcels),
        "point": loc.point,
        "parcels": loc.parcels,
    }


def build_locations(
    contracts_view_list: list[Any],
    flags: list[dict[str, Any]],
    budget: dict[str, Any],
    cadastre_rows: list[Any],
) -> dict[str, Any]:
    """Resolve every distinct budget object and every `eop` contract to an
    exact cadastral parcel or street, using the already-loaded
    `CadastreParcel` rows (`cadastre_rows` -- ORM rows or plain dicts with
    the same columns, see `_parcel_to_dict`).

    Returns `{"features": [...], "parcels_used": {cadnum: {"parcel":,
    "location_ids": [...]}}, "by_object_name": {name: location_id},
    "by_contract_id": {contract.id: location_id}}`. `features` is one dict
    per *located* item (budget objects/contracts that `locate.resolve()`
    returned None for simply aren't in it) -- see `export_locations_geojson`
    for the GeoJSON shape this is turned into, and `_build_feature` for the
    per-feature fields.
    """
    parcels = [_parcel_to_dict(row) for row in cadastre_rows]
    parcels_by_cadnum = {p["cadnum"]: p for p in parcels if p.get("cadnum")}
    street_index = _build_street_index(parcels)

    features: list[dict[str, Any]] = []
    parcels_used: dict[str, dict[str, Any]] = {}
    by_object_name: dict[str, str] = {}
    by_contract_id: dict[int, str] = {}

    def register_parcels(used_parcels: list[dict[str, Any]], location_id: str) -> None:
        for p in used_parcels:
            cadnum = p.get("cadnum")
            if not cadnum:
                continue
            entry = parcels_used.setdefault(cadnum, {"parcel": p, "location_ids": []})
            if location_id not in entry["location_ids"]:
                entry["location_ids"].append(location_id)

    object_groups = _budget_object_groups(budget)
    for name, group in sorted(object_groups.items()):
        keys = object_settlements(name)
        candidates = ekatte_candidates(keys)
        loc = resolve(name, candidates, parcels_by_cadnum, street_index)
        if loc is None:
            continue
        location_id = _location_id("obj", name)
        amounts = {
            str(year): {"plan": bucket["plan"], "spent": bucket["spent"]}
            for year, bucket in sorted(group["years"].items())
        }
        obj_flags = [
            f
            for f in flags or []
            if f.get("subject_type") == "budget_object"
            and (f.get("details") or {}).get("object_name") == name
        ]
        register_parcels(loc.parcels, location_id)
        by_object_name[name] = location_id
        features.append(
            _build_feature(
                location_id,
                "budget_object",
                name,
                keys[0] if keys else None,
                loc,
                amounts,
                f"budget/{group['latest_period']}.html",
                obj_flags,
            )
        )

    for c in contracts_view_list:
        keys = detect_settlements(getattr(c, "title", None))
        candidates = ekatte_candidates(keys)
        loc = resolve(getattr(c, "title", None), candidates, parcels_by_cadnum, street_index)
        if loc is None:
            continue
        location_id = f"contract-{c.id}"
        amounts = {
            "value_eur": getattr(c, "display_value_eur", None),
            "contract_date": getattr(c, "contract_date", None),
            "contractor": getattr(c, "contractor_name", None),
        }
        register_parcels(loc.parcels, location_id)
        by_contract_id[c.id] = location_id
        features.append(
            _build_feature(
                location_id,
                "contract",
                getattr(c, "title", None),
                keys[0] if keys else None,
                loc,
                amounts,
                f"contracts/{c.source_id}.html",
                getattr(c, "flags", None) or [],
                year=getattr(c, "year", None),
            )
        )

    return {
        "features": features,
        "parcels_used": parcels_used,
        "by_object_name": by_object_name,
        "by_contract_id": by_contract_id,
        "total_budget_objects": len(object_groups),
    }


def export_locations_geojson(locations: dict[str, Any]) -> dict[str, Any]:
    """`locations["features"]` (from `build_locations`) as a GeoJSON
    FeatureCollection of Point features -- `site/data/locations.geojson`."""
    features = []
    for f in locations["features"]:
        point = f.get("point")
        if point is None:
            continue
        amounts = f["amounts"]
        if f["kind"] == "contract":
            contract_date = amounts.get("contract_date")
            amounts = {
                "value_eur": round(amounts["value_eur"], 2) if amounts.get("value_eur") is not None else None,
                "contract_date": contract_date.isoformat() if hasattr(contract_date, "isoformat") else contract_date,
                "contractor": amounts.get("contractor"),
            }
        else:
            amounts = {
                year: {"plan": round(b["plan"], 2), "spent": round(b["spent"], 2)}
                for year, b in amounts.items()
            }
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(point[0], 6), round(point[1], 6)]},
                "properties": {
                    "id": f["id"],
                    "kind": f["kind"],
                    "title": f["title"],
                    "settlement": f["settlement"],
                    "settlement_key": f["settlement_key"],
                    "precision": f["precision"],
                    "cadnums": f["cadnums"],
                    "street": f["street"],
                    "address": f["address"],
                    "area_m2": round(f["area_m2"], 2) if f.get("area_m2") is not None else None,
                    "proptype": f["proptype"],
                    "usetype": f["usetype"],
                    "amounts": amounts,
                    "display_amount": round(f["display_amount"], 2) if f.get("display_amount") is not None else None,
                    "year": f.get("year"),
                    "href": f["href"],
                    "flags": f["flags"],
                    "unresolved": f["unresolved"],
                    "source": f["source"],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def export_location_parcels_geojson(locations: dict[str, Any]) -> dict[str, Any]:
    """The parcel polygons used by any located item -- `site/data/
    location_parcels.geojson` -- for drawing outlines on the map."""
    features = []
    for cadnum, entry in sorted(locations["parcels_used"].items()):
        parcel = entry["parcel"]
        geometry = parcel.get("geometry_geojson")
        if not geometry:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "cadnum": cadnum,
                    "location_ids": entry["location_ids"],
                    "address": parcel.get("address"),
                    "street": parcel.get("street"),
                    "proptype": parcel.get("proptype"),
                    "usetype": parcel.get("usetype"),
                    "area_m2": round(parcel["area_m2"], 2) if parcel.get("area_m2") is not None else None,
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}
