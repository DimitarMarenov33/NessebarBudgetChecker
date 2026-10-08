"""Tests for `nessebar_budget.web.locate` (pure text -> parcel/street
resolution) and `nessebar_budget.scrapers.cadastre.read_parcels` (reading
the tiny fixture shapefile under `data/samples/cadastre/`).

Every `extract_parcel_ids`/`extract_streets` test string below is either
taken verbatim from the task brief's own examples, or pulled from this
project's real scraped `object_name`/`title` data (see each test's comment).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from nessebar_budget.scrapers.cadastre import read_parcels
from nessebar_budget.web.locate import (
    Location,
    extract_parcel_ids,
    extract_streets,
    parcel_lookup_keys,
    resolve,
    street_key,
)

FIXTURE_ZIP = (
    Path(__file__).resolve().parents[1] / "data" / "samples" / "cadastre" / "ravda_sample_parcels.zip"
)


# ---------------------------------------------------------------------------
# extract_parcel_ids
# ---------------------------------------------------------------------------


def test_single_parcel_id() -> None:
    assert extract_parcel_ids("Метална ръкохватка в ПИ 51500.501.4 по КК на гр.Несебър") == [
        "51500.501.4"
    ]


def test_comma_separated_list_expands_from_the_full_id(
) -> None:
    # Real object_name (budget_line_items): "ПУП-ПРЗ в ПИ 53045.521.755,
    # 756,757,758,759,760 по КК на гр.Обзор, гр.Обзор".
    text = "ПУП-ПРЗ   в  ПИ 53045.521.755, 756,757,758,759,760 по КК на гр.Обзор, гр.Обзор"
    assert extract_parcel_ids(text) == [
        "53045.521.755",
        "53045.521.756",
        "53045.521.757",
        "53045.521.758",
        "53045.521.759",
        "53045.521.760",
    ]


def test_list_expansion_stops_at_non_digit_text() -> None:
    text = "ПИ 51500.501.4, 51500.501.116 по плана"
    # "51500.501.116" is itself a full id (3 digits after the second dot, >
    # the single comma-digit continuation this project's data ever uses) --
    # not absorbed into the first id's list; both resolve independently.
    assert extract_parcel_ids(text) == ["51500.501.4", "51500.501.116"]


def test_consecutive_full_ids_separated_only_by_a_comma_are_not_merged() -> None:
    # Real title: "Изграждане на ново улично осветление в ПИ 11538.502.273,
    # 11538.502.278,11538.502.281,11538.502.282, по КК на к.к Св. Влас" --
    # four *independent* full ids back-to-back, not a "755, 756" style list
    # (each one has its own ekatte.cadreg.cadimm, not just a trailing digit).
    text = (
        "Изграждане на ново улично осветление в ПИ 11538.502.273, "
        "11538.502.278,11538.502.281,11538.502.282, по КК на к.к Св. Влас"
    )
    assert extract_parcel_ids(text) == [
        "11538.502.273",
        "11538.502.278",
        "11538.502.281",
        "11538.502.282",
    ]


def test_four_part_building_id_keeps_original_and_adds_parent() -> None:
    # Real object_name: "Придобиване на сгради в Слънчев бряг с
    # инд.ПИ 51500.506.493.1 и 2, гр.Несебър".
    text = "Придобиване на сгради в Слънчев бряг с инд.ПИ 51500.506.493.1 и 2, гр.Несебър"
    ids = extract_parcel_ids(text)
    assert "51500.506.493.1" in ids
    assert "51500.506.493" in ids
    # "и 2" is not a comma/space-separated bare-digit list -- never expanded.
    assert "51500.506.493.2" not in ids
    assert parcel_lookup_keys(ids) == ["51500.506.493"]


def test_foreign_or_typo_prefix_is_ignored() -> None:
    # Real object_name: "...в ПИ 115384.4.66 от кръстовището на улица Юг до
    # югозападния ъгъл на ПИ 11538.4.6 гр.Свети Влас" -- "115384" is a typo'd
    # EKATTE (an extra stray digit); only the real "11538.4.6" id is found.
    text = (
        "Проектиране на ново улично осветление в ПИ 115384.4.66 от "
        "кръстовището на улица Юг до югозападния ъгъл на ПИ 11538.4.6 "
        "гр.Свети Влас"
    )
    assert extract_parcel_ids(text) == ["11538.4.6"]


def test_prefix_not_among_the_14_municipality_codes_is_dropped() -> None:
    assert extract_parcel_ids("ПИ 68134.10.5 в съседна община") == []


def test_no_ids_in_plain_text() -> None:
    assert extract_parcel_ids("Доставка на канцеларски материали за нуждите на Община Несебър") == []


def test_extract_parcel_ids_empty_input() -> None:
    assert extract_parcel_ids(None) == []
    assert extract_parcel_ids("") == []


# ---------------------------------------------------------------------------
# extract_streets
# ---------------------------------------------------------------------------


def test_two_streets_joined_by_i_ul_split_into_two_names() -> None:
    assert extract_streets("ул. Любен Каравелов и ул.Раковска гр.Несебър") == [
        "Любен Каравелов",
        "Раковска",
    ]


def test_street_stops_at_v_uchastak() -> None:
    assert extract_streets("ул. Първа в участък от о.т.83 до о.т.123") == ["Първа"]


def test_quoted_street_ascii_quotes() -> None:
    assert extract_streets('находяща се на ул."Еделвайс", гр. Несебър') == ["Еделвайс"]


def test_quoted_street_same_char_both_sides() -> None:
    # Real title uses the same right-double-quote character as both the
    # opening and closing mark (a common copy/typesetting artifact).
    assert extract_streets("находяща се на ул.“Еделвайс“ 10, гр. Несебър") == ["Еделвайс"]


def test_quoted_street_bulgarian_low_high_quotes() -> None:
    assert extract_streets(
        "път І-9 „Слънчев бряг – Бургас“ и ул. „Св. св. Кирил и Методий“, в землището на с. Равда"
    ) == ["Св. св. Кирил и Методий"]


def test_bare_ulitsata_is_not_a_false_positive() -> None:
    assert extract_streets("движение по улицата е ограничено") == []


def test_street_with_bul_marker() -> None:
    assert extract_streets("паркинг на бул. Хан Крум") == ["Хан Крум"]


def test_no_streets_in_plain_text() -> None:
    assert extract_streets("Доставка на канцеларски материали") == []


# ---------------------------------------------------------------------------
# street_key
# ---------------------------------------------------------------------------


def test_street_key_equivalences() -> None:
    expected = "свсвкирилиметодий"
    assert street_key("Св.Св. Кирил и Методий") == expected
    assert street_key("Св. св. Кирил и Методий") == expected
    assert street_key("Св.св.Кирил и Методий") == expected
    # Also applied directly to the cadastre's own strename field, with or
    # without its own leading marker.
    assert street_key("ул. Св.Св. Кирил и Методий") == expected


def test_street_key_empty() -> None:
    assert street_key(None) == ""
    assert street_key("") == ""


# ---------------------------------------------------------------------------
# read_parcels (fixture shapefile)
# ---------------------------------------------------------------------------


def test_fixture_zip_exists() -> None:
    assert FIXTURE_ZIP.exists(), f"missing fixture: {FIXTURE_ZIP}"


def test_read_parcels_check_value() -> None:
    rows = {p["cadnum"]: p for p in read_parcels(FIXTURE_ZIP)}
    assert "61056.502.526" in rows
    target = rows["61056.502.526"]

    lon, lat = target["rings_lonlat"][0][0]
    assert abs(lon - 27.67765) < 1e-5
    assert abs(lat - 42.64455) < 1e-5

    assert abs(target["AREA"] - 301.17) < 0.01
    assert target["proptype"] == "Общинска публична"
    assert target["usetype"] == "За второстепенна улица"
    assert target["immaddr"] == "с. Равда, ул. Св.Св. Кирил и Методий"

    clon, clat = target["centroid_lonlat"]
    assert 27.0 < clon < 28.0
    assert 42.0 < clat < 43.0

    assert target["geometry_geojson"]["type"] in ("Polygon", "MultiPolygon")


def test_read_parcels_yields_every_fixture_record() -> None:
    rows = list(read_parcels(FIXTURE_ZIP))
    assert len(rows) == 5
    cadnums = {r["cadnum"] for r in rows}
    assert "61056.502.526" in cadnums
    assert "61056.501.505" in cadnums


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def _db_shaped(parcel: dict) -> dict:
    """Mimic `db.repo.replace_cadastre_parcels`'s conversion from a
    `read_parcels()` dict to the `CadastreParcel`-column shape
    `locate.resolve()`/`web.geo` consume."""
    centroid = parcel.get("centroid_lonlat")
    return {
        "cadnum": parcel["cadnum"],
        "ekatte": parcel["ekatte"],
        "settlement": parcel.get("ekattefn"),
        "area_m2": parcel.get("AREA"),
        "address": parcel.get("immaddr"),
        "street": parcel.get("strename") or None,
        "street_number": parcel.get("strnum") or None,
        "proptype": parcel.get("proptype") or None,
        "purptype": parcel.get("purptype") or None,
        "usetype": parcel.get("usetype") or None,
        "quarter": parcel.get("quarter") or None,
        "centroid_lat": centroid[1] if centroid else None,
        "centroid_lon": centroid[0] if centroid else None,
        "geometry_geojson": parcel.get("geometry_geojson"),
        "source_path": "област Бургас/община Несебър/с. Равда (61056)/поземлени имоти.zip",
        "source_modified": dt.date(2026, 10, 8),
    }


def _fixture_parcels() -> list[dict]:
    return [_db_shaped(p) for p in read_parcels(FIXTURE_ZIP)]


def _street_index(parcels: list[dict]) -> dict[str, dict[str, list[dict]]]:
    index: dict[str, dict[str, list[dict]]] = {}
    for p in parcels:
        usetype = p.get("usetype") or ""
        street = p.get("street")
        if not street or not any(s in usetype for s in ("улица", "алея", "площад", "път")):
            continue
        index.setdefault(p["ekatte"], {}).setdefault(street_key(street), []).append(p)
    return index


def test_resolve_by_parcel_id() -> None:
    parcels = _fixture_parcels()
    by_cadnum = {p["cadnum"]: p for p in parcels}
    streets = _street_index(parcels)

    loc = resolve(
        "Текущ ремонт на площадка в ПИ 61056.502.526 по плана на с. Равда",
        ["61056"],
        by_cadnum,
        streets,
    )
    assert isinstance(loc, Location)
    assert loc.precision == "parcel"
    assert loc.ids == ["61056.502.526"]
    assert loc.point is not None
    assert abs(loc.point[0] - 27.677541) < 1e-5
    assert abs(loc.point[1] - 42.644551) < 1e-5
    assert loc.unresolved == []


def test_resolve_by_street_uses_only_street_type_parcels() -> None:
    parcels = _fixture_parcels()
    by_cadnum = {p["cadnum"]: p for p in parcels}
    streets = _street_index(parcels)

    loc = resolve(
        "Реконструкция на ул. Св.Св. Кирил и Методий, с.Равда",
        ["61056"],
        by_cadnum,
        streets,
    )
    assert loc is not None
    assert loc.precision == "street"
    assert loc.street == "Св.Св. Кирил и Методий"
    # Only the two "За второстепенна улица" parcels qualify as street
    # parcels -- the three building parcels on the fixture that happen to
    # share the same `strename` are never used for this.
    assert {p["cadnum"] for p in loc.parcels} == {"61056.502.526", "61056.501.505"}
    assert loc.point is not None


def test_resolve_street_requires_exactly_one_candidate_settlement_match() -> None:
    parcels = _fixture_parcels()
    by_cadnum = {p["cadnum"]: p for p in parcels}
    streets = _street_index(parcels)

    # The same street name "found" under two different candidate EKATTE
    # codes (the second, "53045", has no data at all in this fixture, but
    # the ambiguity rule only cares whether more than one *candidate*
    # resolves -- with only one real match here it still resolves).
    loc = resolve(
        "ул. Св.Св. Кирил и Методий",
        ["61056", "53045"],
        by_cadnum,
        streets,
    )
    assert loc is not None
    assert loc.precision == "street"


def test_resolve_returns_none_when_nothing_matches() -> None:
    parcels = _fixture_parcels()
    by_cadnum = {p["cadnum"]: p for p in parcels}
    streets = _street_index(parcels)

    assert resolve("Доставка на канцеларски материали", ["61056"], by_cadnum, streets) is None
    # A parcel id not present in our stored data.
    assert resolve("ПИ 61056.999.999", ["61056"], by_cadnum, streets) is None
    assert resolve(None, ["61056"], by_cadnum, streets) is None
    assert resolve("", ["61056"], by_cadnum, streets) is None


def test_resolve_reports_unresolved_street_alongside_a_resolved_one() -> None:
    parcels = _fixture_parcels()
    by_cadnum = {p["cadnum"]: p for p in parcels}
    streets = _street_index(parcels)

    loc = resolve(
        "ул. Св.Св. Кирил и Методий и ул.Несъществуваща",
        ["61056"],
        by_cadnum,
        streets,
    )
    assert loc is not None
    assert loc.precision == "street"
    assert "Несъществуваща" in loc.unresolved



def test_street_name_shared_by_separate_streets_is_not_placed() -> None:
    from nessebar_budget.web import locate

    def parcel(cad, lat, lon):
        return {"cadnum": cad, "centroid_lat": lat, "centroid_lon": lon, "street": "ул. Първа"}

    # Two segments of one street, 400 m apart: one street.
    one = [parcel("51500.1.1", 42.6600, 27.7100), parcel("51500.1.2", 42.6636, 27.7100)]
    assert not locate.street_is_split(one)
    # Same name 5 km apart (real case in землище Несебър): two streets.
    two = one + [parcel("51500.9.9", 42.7050, 27.7100)]
    assert locate.street_is_split(two)
    streets = {"51500": {locate.street_key("Първа"): two, locate.street_key("Изгрев"): one}}
    # The ambiguous name is skipped; a second, unambiguous street is used.
    assert locate.resolve("Светофар на ул. Първа", ["51500"], {}, streets) is None
    loc = locate.resolve("Светофар на ул. Първа и ул. Изгрев", ["51500"], {}, streets)
    assert loc is not None and loc.street == "Изгрев"
