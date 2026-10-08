"""Deterministic text -> cadastral-parcel/street resolution ("Точни места").

Pure functions only: no DB session, no network, no AI/LLM, nothing fuzzy.
Everything here either matches a literal pattern (a KAIS cadastral
identifier) or a normalized-string equality (a street name, via
`street_key`). An item whose text names nothing we can match stays
unresolved -- it is *never* geocoded, guessed, or placed "close enough".

Two extraction primitives:

- `extract_parcel_ids(text)`: every KAIS cadastral identifier
  ("61056.502.526", optionally a 4th building/unit segment), expanding a
  short comma-separated list that directly follows a full identifier
  ("ПИ 53045.521.755, 756,757" -> .755/.756/.757 -- confirmed against this
  project's own scraped budget-object names, see tests).
- `extract_streets(text)`: every street name following "ул."/"улица"/
  "бул."/"булевард", quoted or not.
- `street_key(name)`: a normalized matching key, lower-case with every
  non-letter/non-digit character stripped (so punctuation/spacing variants
  of the same name collapse to one key) -- used on *both* sides of a street
  match: the text's own extracted name and the cadastre's own `strename`
  field.

`resolve(...)` combines both against a settlement's already-loaded parcel
data: a parcel identifier match wins outright (it is self-describing -- its
own 5-digit EKATTE prefix already says which settlement); failing that, a
street name is matched only within the item's own candidate settlement(s).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Parcel identifiers
# ---------------------------------------------------------------------------

#: A KAIS cadastral identifier: 5-digit EKATTE, then parcel-register and
#: parcel-number segments, optionally a 4th building/unit segment. Given
#: verbatim by the task brief -- do not loosen it (a malformed/typo'd id,
#: e.g. a 6-digit EKATTE from a stray extra digit, must NOT match).
_FULL_ID_RE = re.compile(r"\b(\d{5})\.(\d{1,4})\.(\d{1,5})(?:\.(\d{1,4}))?\b")

#: A bare number directly following a full id inside a comma-separated list
#: ("ПИ 53045.521.755, 756,757,758,759,760") -- anchored to the exact
#: position right after the previous match/number via `Pattern.match(text,
#: pos)` (which anchors at `pos`, not just at the string start), so a list
#: only continues while commas/bare-digit tokens keep appearing immediately;
#: anything else (a word, "по КК на...") stops it. The trailing `(?!\.\d)`
#: keeps this from misreading a *second, independent* full id that happens
#: to follow right after a comma with no descriptive text in between (real
#: data: "ПИ 11538.502.273, 11538.502.278,11538.502.281,11538.502.282, по
#: КК...") -- without it, the first 1-5 digits of that second id's own
#: EKATTE would look just like a bare continuation number.
_LIST_ITEM_RE = re.compile(r"\s*,\s*(\d{1,5})(?!\.\d)\b")

#: The 14 EKATTE codes of Община Несебър's own settlements (see
#: `web.geo.EKATTE_TO_KEY`, duplicated here as a plain literal set so this
#: module stays import-free of `web.geo` and fully standalone/pure).
VALID_EKATTE: frozenset[str] = frozenset(
    {
        "51500", "53045", "11538", "61056", "39164", "53822", "73571",
        "18469", "02703", "58431", "55350", "62102", "27454", "37825",
    }
)


def _expand_list(text: str, ekatte: str, cadreg: str, start_pos: int) -> tuple[list[str], int]:
    """From `start_pos` (right after a resolved full id), consume a chain of
    ", <digits>" tokens and turn each into a sibling id sharing the same
    `ekatte.cadreg` prefix. Returns (new_ids, end_pos)."""
    ids: list[str] = []
    pos = start_pos
    while True:
        m = _LIST_ITEM_RE.match(text, pos)
        if not m:
            break
        ids.append(f"{ekatte}.{cadreg}.{m.group(1)}")
        pos = m.end()
    return ids, pos


def extract_parcel_ids(text: str | None) -> list[str]:
    """Every KAIS cadastral identifier named in `text`, deduplicated, in
    order of first occurrence.

    A 4-part id (a building/unit on a parcel) contributes *both* its literal
    form (kept for display -- this project only stores land parcels, so a
    building/unit id never itself appears as a key in `parcels_by_cadnum`)
    and its parent 3-part parcel id (what actually gets looked up). Callers
    doing the actual lookup should collapse each returned id to its first
    three dot-separated segments before checking a parcel map -- `resolve()`
    does this.

    Only ids whose 5-digit EKATTE prefix is one of this municipality's own
    14 codes are returned -- a foreign/typo'd prefix (e.g. a stray extra
    digit turning "11538" into "115384") is silently dropped by the pattern
    itself (the fixed-width `\\d{5}` plus the `\\b` boundary either side
    never matches inside a longer digit run) or, if it did match a 5-digit
    run, is filtered out explicitly here.
    """
    if not text:
        return []

    ids: list[str] = []
    pos = 0
    for m in _FULL_ID_RE.finditer(text):
        if m.start() < pos:
            continue  # inside a just-expanded list; don't re-match it
        ekatte, cadreg, cadimm, unit = m.group(1), m.group(2), m.group(3), m.group(4)
        if ekatte not in VALID_EKATTE:
            pos = m.end()
            continue

        full_id = f"{ekatte}.{cadreg}.{cadimm}" + (f".{unit}" if unit else "")
        if full_id not in ids:
            ids.append(full_id)
        if unit:
            parent = f"{ekatte}.{cadreg}.{cadimm}"
            if parent not in ids:
                ids.append(parent)
            pos = m.end()
            continue

        extra_ids, end_pos = _expand_list(text, ekatte, cadreg, m.end())
        for extra in extra_ids:
            if extra not in ids:
                ids.append(extra)
        pos = end_pos

    return ids


def parcel_lookup_keys(ids: list[str]) -> list[str]:
    """Collapse `extract_parcel_ids()`'s output to the 3-part keys actually
    used by `parcels_by_cadnum` (a 4-part id's parent), deduplicated,
    preserving order."""
    keys: list[str] = []
    for rid in ids:
        parts = rid.split(".")
        key = rid if len(parts) == 3 else ".".join(parts[:3])
        if key not in keys:
            keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Streets
# ---------------------------------------------------------------------------

#: "булевард"/"улица" need an explicit trailing `\b` -- without it, the
#: alternation would also match the first 5/8 letters of an unrelated longer
#: word ("улицата", "булевардът" -- generic nouns, not markers introducing a
#: name). "бул."/"ул." don't need (and must NOT get) one: they already end
#: on the non-word period, and the common "ул. Иван" spelling has a space
#: right after it -- a `\b` *there* would fail (period and space are both
#: non-word, so there is no word/non-word transition at that position).
_STREET_MARKER_RE = re.compile(r"\b(?:булевард\b|улица\b|бул\.|ул\.)\s*", re.IGNORECASE)

#: Leading marker stripped by `street_key` -- applied to both an extracted
#: name (which already had its marker consumed by `_STREET_MARKER_RE`, so
#: this is normally a no-op there) and the cadastre's own `strename` field
#: (which sometimes spells it out inline, e.g. "ул. Св.Св. Кирил и Методий").
_STREET_PREFIX_RE = re.compile(r"^\s*(?:булевард\b|улица\b|бул\.?|ул\.?)\s*", re.IGNORECASE)

#: Non-word (and underscore) characters stripped by `street_key`, so e.g.
#: "Св.Св. Кирил и Методий" and "Св. св.Кирил и Методий" give the same key.
_NON_ALNUM_RE = re.compile(r"[^\w]|_")

#: opening-quote -> closing-quote. "“" is listed as its own pair too: this
#: project's real source text sometimes uses the same right-double-quote
#: character for both open and close (a common copy/typesetting artifact),
#: e.g. 'ул.“Еделвайс“'.
_QUOTE_PAIRS: dict[str, str] = {
    "„": "“",
    "“": "“",
    '"': '"',
    "'": "'",
    "‘": "’",
    "«": "»",
}

#: Stop substrings for an *unquoted* street name -- the earliest-occurring
#: one (by position, not list order) ends the name. Given verbatim by the
#: task brief.
_STOP_TOKENS: tuple[str, ...] = (
    ",", ";", ")", " в участък", " от о.т.", " между", " до ",
    " гр.", " с.", " к.к.", " и ул.",
)

_TRIM_CHARS = " \t\n\"'„“”’‘«»"


def _scan_unquoted(text: str, start: int) -> str:
    cut = len(text)
    for token in _STOP_TOKENS:
        idx = text.find(token, start)
        if idx != -1 and idx < cut:
            cut = idx
    return text[start:cut]


def extract_streets(text: str | None) -> list[str]:
    """Every street name named in `text` after a "ул."/"улица"/"бул."/
    "булевард" marker, in order of appearance (not deduplicated -- a street
    mentioned twice is returned twice; callers that only care about distinct
    names can dedupe via `street_key`).

    A quoted name ("ул. „Св. св. Кирил и Методий“", 'ул."Еделвайс"') is taken
    verbatim between the quotes, whatever punctuation it contains. An
    unquoted name stops at the first of a fixed set of separators (comma,
    semicolon, closing paren, or one of a few Bulgarian phrases that
    typically follow a street name in this project's source text -- see
    `_STOP_TOKENS`), so "ул. Любен Каравелов и ул.Раковска гр.Несебър" splits
    into two names via two independent marker matches, and "ул. Първа в
    участък от о.т.83 до о.т.123" stops at "Първа".
    """
    if not text:
        return []

    results: list[str] = []
    for m in _STREET_MARKER_RE.finditer(text):
        start = m.end()
        if start >= len(text):
            continue
        ch = text[start]
        close = _QUOTE_PAIRS.get(ch)
        if close is not None:
            end = text.find(close, start + 1)
            name = text[start + 1 : end] if end != -1 else _scan_unquoted(text, start + 1)
        else:
            name = _scan_unquoted(text, start)
        name = name.strip(_TRIM_CHARS)
        if name:
            results.append(name)
    return results


def street_key(name: str | None) -> str:
    """Normalized matching key for a street name: drop a leading marker
    (if any), lower-case, strip every non-letter/non-digit character. Used
    identically on an `extract_streets()` result and on the cadastre's own
    `strename` field, so punctuation/spacing variants of the same name
    (and a marker present on one side but not the other) always agree."""
    if not name:
        return ""
    text = _STREET_PREFIX_RE.sub("", name.strip())
    text = text.lower()
    return _NON_ALNUM_RE.sub("", text)


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


@dataclass
class Location:
    """One resolved location for a budget object or a contract."""

    #: 'parcel' | 'street'
    precision: str
    #: Resolved parcel cadnums (precision == 'parcel'); [] for a street.
    ids: list[str] = field(default_factory=list)
    #: The matched street name, as named in the text (precision == 'street').
    street: str | None = None
    #: The parcel dict(s) backing this location (one for 'parcel', every
    #: parcel of the matched street for 'street') -- same dict shape
    #: `parcels_by_cadnum` values use (at least: cadnum, ekatte, address,
    #: street, street_number, proptype, purptype, usetype, area_m2,
    #: centroid_lat, centroid_lon, geometry_geojson).
    parcels: list[dict[str, Any]] = field(default_factory=list)
    #: Representative (lon, lat) point.
    point: tuple[float, float] | None = None
    #: Ids/street names named in the text that this item could not resolve
    #: (e.g. a second street mentioned but not matched, or a parcel id not
    #: present in our stored data).
    unresolved: list[str] = field(default_factory=list)


def _feature_collection(parcels: list[dict[str, Any]]) -> dict[str, Any]:
    features = []
    for p in parcels:
        geometry = p.get("geometry_geojson")
        if not geometry:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "cadnum": p.get("cadnum"),
                    "address": p.get("address"),
                    "street": p.get("street"),
                    "proptype": p.get("proptype"),
                    "usetype": p.get("usetype"),
                    "area_m2": p.get("area_m2"),
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _mean_point(parcels: list[dict[str, Any]]) -> tuple[float, float] | None:
    pts = [
        (p["centroid_lon"], p["centroid_lat"])
        for p in parcels
        if p.get("centroid_lon") is not None and p.get("centroid_lat") is not None
    ]
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _closest_to(point: tuple[float, float], parcels: list[dict[str, Any]]) -> dict[str, Any]:
    def dist(p: dict[str, Any]) -> float:
        dx = (p.get("centroid_lon") or 0.0) - point[0]
        dy = (p.get("centroid_lat") or 0.0) - point[1]
        return dx * dx + dy * dy

    return min(parcels, key=dist)


#: Parcels of one real street are contiguous, so the centroids of
#: neighbouring segments sit well within this distance. Street parcels of
#: one name that split into groups further apart than this are different
#: streets that share a name (e.g. several "ул. Първа" inside землище
#: Несебър, up to 5 km apart) -- such a name is ambiguous and never placed.
STREET_SPLIT_GAP_KM = 1.0


def _km(a: dict[str, Any], b: dict[str, Any]) -> float:
    dy = (a["centroid_lat"] - b["centroid_lat"]) * 111.2
    dx = (a["centroid_lon"] - b["centroid_lon"]) * 111.2 * math.cos(math.radians(a["centroid_lat"]))
    return math.hypot(dx, dy)


def street_is_split(parcels: list[dict[str, Any]], gap_km: float = STREET_SPLIT_GAP_KM) -> bool:
    """True when `parcels` (all parcels of one street name in one
    settlement) form more than one group under single-linkage clustering
    with `gap_km`, i.e. the name belongs to two or more separate streets."""
    pts = [p for p in parcels if p.get("centroid_lat") is not None and p.get("centroid_lon") is not None]
    if len(pts) < 2:
        return False
    reached = {0}
    frontier = [0]
    while frontier:
        i = frontier.pop()
        for j in range(len(pts)):
            if j not in reached and _km(pts[i], pts[j]) <= gap_km:
                reached.add(j)
                frontier.append(j)
    return len(reached) < len(pts)


def resolve(
    text: str | None,
    ekatte_candidates: list[str],
    parcels_by_cadnum: dict[str, dict[str, Any]],
    street_parcels_by_ekatte_key: dict[str, dict[str, list[dict[str, Any]]]],
) -> Location | None:
    """Resolve `text` (a budget-object name or a contract title) to a
    cadastral parcel or street, or None if nothing in it can be matched.

    Rules (see module docstring / docs/sources/CADASTRE.md):

    1. Parcel ids first. Every id `extract_parcel_ids()` finds is looked up
       directly in `parcels_by_cadnum` (a cadastral id is self-describing --
       its own EKATTE prefix already says which settlement, so no
       restriction to `ekatte_candidates` applies here). If at least one
       resolves, `precision = 'parcel'`.
    2. Otherwise, streets. Every name `extract_streets()` finds is matched
       (via `street_key`) against each candidate settlement's own street
       parcels, in `ekatte_candidates` order. A match is only accepted when
       *exactly one* candidate settlement yields any match at all -- an
       ambiguous title naming streets in what could be two different
       settlements resolves to neither. A street name whose parcels split
       into separate groups (`street_is_split`: two different streets with
       the same name in one settlement) is skipped, and the next named
       street is tried. `precision = 'street'`.
    3. If neither resolves anything at all, return None.
    """
    if not text:
        return None

    raw_ids = extract_parcel_ids(text)
    lookup_keys = parcel_lookup_keys(raw_ids)

    resolved_parcels: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for key in lookup_keys:
        parcel = parcels_by_cadnum.get(key)
        if parcel is not None:
            resolved_parcels.append(parcel)
        else:
            unresolved.append(key)

    if resolved_parcels:
        first = resolved_parcels[0]
        point = (first["centroid_lon"], first["centroid_lat"])
        return Location(
            precision="parcel",
            ids=[p["cadnum"] for p in resolved_parcels],
            parcels=resolved_parcels,
            point=point,
            unresolved=unresolved,
        )

    street_names = extract_streets(text)
    if not street_names:
        return None

    candidate_matches: dict[str, list[tuple[str, str]]] = {}
    for ekatte in ekatte_candidates:
        street_map = street_parcels_by_ekatte_key.get(ekatte) or {}
        found = []
        for name in street_names:
            key = street_key(name)
            if key and key in street_map:
                found.append((name, key))
        if found:
            candidate_matches[ekatte] = found

    if len(candidate_matches) != 1:
        return None

    (ekatte, matches) = next(iter(candidate_matches.items()))
    street_map = street_parcels_by_ekatte_key[ekatte]
    usable = [(n, k) for n, k in matches if not street_is_split(street_map[k])]
    if not usable:
        return None
    chosen_name, chosen_key = usable[0]
    street_parcels = street_map[chosen_key]

    mean_point = _mean_point(street_parcels)
    if mean_point is None:
        return None
    representative = _closest_to(mean_point, street_parcels)
    point = (representative["centroid_lon"], representative["centroid_lat"])
    # The representative parcel goes first so callers that just want "the"
    # parcel for this location (e.g. its own address/proptype/usetype to
    # show next to the point) can always use `parcels[0]`, for either
    # precision -- the full list (every parcel of the street) stays intact
    # right after it, for drawing the complete set of outlines.
    street_parcels = [representative] + [p for p in street_parcels if p is not representative]

    matched_names = {n for n, _k in matches}
    remaining_unresolved = list(unresolved)
    for name in street_names:
        if name not in matched_names:
            remaining_unresolved.append(name)

    return Location(
        precision="street",
        street=chosen_name,
        parcels=street_parcels,
        point=point,
        unresolved=remaining_unresolved,
    )


def location_geometry(location: Location) -> dict[str, Any]:
    """`FeatureCollection` of `location.parcels`' own polygons -- for a
    'parcel' location this is just that one parcel; for a 'street' location,
    every parcel making up that street."""
    return _feature_collection(location.parcels)
