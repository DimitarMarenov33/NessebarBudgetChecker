"""Tests for `nessebar_budget.web.geo` -- settlement detection and the
static settlement dataset.

`detect_settlement`/`detect_settlements` are exercised against real
`object_name`/`title` strings pulled from `data/nessebar.db` (see each
case's comment for where it came from), covering the tricky patterns called
out in the task: abbreviations with/without a period and with/without a
space ("Св.Влас", "Св. Влас"), settlement-prefixed forms that must NOT also
match as a bare common noun ("с.Баня" but never bare "баня"), a cadastral
identifier with no settlement name in the text at all, two settlements named
in one string, ALL-CAPS text, and the one real ambiguity in the whole
alias list: "Несебър" is also the municipality's own name, so "Община
Несебър"/"общ. Несебър" (naming the contracting authority, not a location)
must not count as a mention of the town.
"""

from __future__ import annotations

from nessebar_budget.web import geo


def test_settlements_dataset_has_all_14_settlements_plus_2_resorts() -> None:
    assert len(geo.SETTLEMENTS) == 16
    ekatte_codes = {s["ekatte"] for s in geo.SETTLEMENTS if s.get("ekatte")}
    assert ekatte_codes == {
        "51500", "53045", "11538", "61056", "39164", "53822", "73571",
        "18469", "02703", "58431", "55350", "62102", "27454", "37825",
    }
    resorts = {s["key"] for s in geo.SETTLEMENTS if s["kind"] == "курорт"}
    assert resorts == {"slanchev-bryag", "elenite"}
    for s in geo.SETTLEMENTS:
        assert isinstance(s["lat"], float) and isinstance(s["lon"], float)
        assert s["source"]["date"] == "2026-10-08"


# ---------------------------------------------------------------------------
# detect_settlement / detect_settlements on real object_name/title strings
# ---------------------------------------------------------------------------


def test_plain_settlement_suffix_nesebar() -> None:
    # budget_line_items.object_name
    text = 'Реконструкция на корпус към СУ "Л.Каравелов"  Несебър-стар град, гр.Несебър'
    assert geo.detect_settlement(text) == "nesebar"


def test_village_prefix_ravda() -> None:
    # budget_line_items.object_name
    text = 'Реконструкция на улично осветление на ул. Св.Св. "Кирил и Методий"  с.Равда, с.Равда'
    assert geo.detect_settlement(text) == "ravda"


def test_town_prefix_obzor() -> None:
    # budget_line_items.object_name
    text = "Основен ремонт на главен канализационен колектор Ф 500 на крайбрежна алея гр.Обзор, гр.Обзор"
    assert geo.detect_settlement(text) == "obzor"


def test_sveti_vlas_full_name() -> None:
    # budget_line_items.object_name
    text = "Стопански инвентар за Кметство Свети Влас, гр.Свети Влас"
    assert geo.detect_settlement(text) == "sveti-vlas"


def test_sveti_vlas_abbreviation_with_cadastral_id_and_excluded_org_name() -> None:
    # procurements.title -- "гр.Св.Влас" (abbreviated, no space) must match,
    # and the trailing "община Несебър" must NOT also register as "nesebar".
    text = (
        "Благоустрояване и озеленяване , изграждане на детска площадка и зона за рекреация в "
        "ПИ 11538.501.357 по плана на гр.Св.Влас, община Несебър"
    )
    assert geo.detect_settlement(text) == "sveti-vlas"
    assert geo.detect_settlements(text) == ["sveti-vlas"]


def test_two_settlements_in_one_name_slanchev_bryag_and_kosharitsa() -> None:
    # procurements.title -- "Сл.бряг" (abbreviated resort) appears before
    # "с.Кошарица" in the text, so it wins detect_settlement(); both are
    # returned, in that order, by detect_settlements().
    text = "Изграждане на велоалея на път Сл.бряг - с.Кошарица, с.Кошарица"
    assert geo.detect_settlement(text) == "slanchev-bryag"
    assert geo.detect_settlements(text) == ["slanchev-bryag", "kosharitsa"]


def test_banya_prefixed_form_matches_but_bare_noun_would_not() -> None:
    # budget_line_items.object_name -- "с.Баня" must match...
    text = "Изграждане на многофункционална сграда с.Баня, с.Баня"
    assert geo.detect_settlement(text) == "banya"
    # ...but "банята"/bare "баня" (the common noun "bath/spa") must not be
    # an alias at all -- banya's aliases are all "с."-prefixed on purpose.
    assert all(alias.lower().startswith(("с.", "с ")) for alias in geo.SETTLEMENTS_BY_KEY["banya"]["aliases"])
    assert geo.detect_settlement("Нова обществена баня в центъра") is None


def test_neighbourhood_qualifier_after_settlement_still_detected() -> None:
    # budget_line_items.object_name -- "гр.Несебър" is matched even though a
    # (non-settlement) neighbourhood name ("ж.к.Черно море") follows it.
    text = 'Компютри ДГ "Моряче" гр.Несебър, ж.к.Черно море'
    assert geo.detect_settlement(text) == "nesebar"


def test_cadastral_id_only_no_settlement_word_in_text() -> None:
    # procurements.title -- names no settlement at all, only a cadastral
    # identifier whose EKATTE prefix (53045) is Обзор's.
    text = (
        "Авариен основен ремонт на главен канализационен колектор Ф500 в съществуваща алея, "
        "реконструкция на КПС - 1 и прилежащата техническа инфраструктура, находящи се в "
        "ПИ 53045.503.323"
    )
    assert geo.detect_settlement(text) == "obzor"


def test_orizare_with_cadastral_id_confirming_same_settlement_and_excluded_municipality() -> None:
    # procurements.title (near-verbatim) -- "с. Оризаре" appears before the
    # excluded "общ. Несебър", and the cadastral id (53822 = Оризаре) agrees.
    text = (
        'Авариен ремонт на отоплителна инсталация с котелно помещение и нафтено стопанство на ОУ '
        '"Г.С.Раковски" в УПИ IV, кв. 36, с. Оризаре общ. Несебър /ПИ с идент.53822.501.340 по КК/'
    )
    assert geo.detect_settlement(text) == "orizare"
    assert geo.detect_settlements(text) == ["orizare"]


def test_tankovo() -> None:
    # budget_line_items.object_name
    text = "Компютри за ОУ с.Тънково, с.Тънково"
    assert geo.detect_settlement(text) == "tankovo"


def test_all_caps_title_gyulyovtsa_with_excluded_municipality() -> None:
    # procurements.title, ALL CAPS -- case-insensitivity, plus "ОБЩИНА
    # НЕСЕБЪР" at the end must not register as a second settlement.
    text = 'ИЗГРАЖДАНЕ НА УЛ."ДУНАВ" В ПИ 18469.501.321, С.ГЮЛЬОВЦА, ОБЩИНА НЕСЕБЪР'
    assert geo.detect_settlement(text) == "gyulyovtsa"
    assert geo.detect_settlements(text) == ["gyulyovtsa"]


def test_many_settlements_in_one_title_is_ambiguous() -> None:
    # procurements.title (near-verbatim, a multi-lot road-works tender) --
    # four settlements plus two (excluded) municipality self-references.
    text = (
        "СМР по текущ ремонт на улична мрежа, общинска пътна мрежа и паркинги на територията на "
        "Община Несебър по обособени позиции: ОП 1 –гр. Свети Влас общ. Несебър , ОП 2- с. Кошарица, "
        "с. Оризаре, с. Тънково и с. Гюльовца общ. Несебър"
    )
    found = geo.detect_settlements(text)
    assert found == ["sveti-vlas", "kosharitsa", "orizare", "tankovo", "gyulyovtsa"]
    assert len(found) > 1  # ambiguous -- caller records this, see build_map_data
    assert geo.detect_settlement(text) == "sveti-vlas"


def test_pure_municipality_wide_contract_has_no_settlement() -> None:
    # procurements.title -- names only "Община Несебър" (the contracting
    # authority), no specific settlement anywhere.
    text = (
        "Доставка на спомагателно-хигиенни материали за нуждите на общинската администрация и "
        "второстепенните разпоредители на Община Несебър"
    )
    assert geo.detect_settlement(text) is None
    assert geo.detect_settlements(text) == []


def test_cadastral_id_before_literal_name_wins_by_text_position() -> None:
    # procurements.title (near-verbatim) -- the cadastral id (51500 =
    # Несебър) appears *before* the literal "СЛЪНЧЕВ БРЯГ" phrase, so
    # nesebar legitimately wins detect_settlement() by text order; both are
    # in detect_settlements(), in that order.
    text = (
        'ОБСЛУЖВАЩА УЛИЦА О.Т.800-О.Т.501 В ПИ 51500.506.679 В К.К."СЛЪНЧЕВ БРЯГ-ЗАПАД", '
        "ГР.НЕСЕБЪР, ОБЩИНА НЕСЕБЪР"
    )
    assert geo.detect_settlement(text) == "nesebar"
    assert geo.detect_settlements(text) == ["nesebar", "slanchev-bryag"]


def test_none_and_empty_text() -> None:
    assert geo.detect_settlement(None) is None
    assert geo.detect_settlement("") is None
    assert geo.detect_settlements(None) == []


# ---------------------------------------------------------------------------
# cadastral_ids() / kais_url()
# ---------------------------------------------------------------------------


def test_cadastral_ids_extracts_3_and_4_segment_identifiers_in_order() -> None:
    text = "ПИ 51500.506.303 И ПИ 51500.506.302, а и 51500.506.493.1 за пълнота"
    assert geo.cadastral_ids(text) == ["51500.506.303", "51500.506.302", "51500.506.493.1"]


def test_cadastral_ids_deduplicates_repeated_identifiers() -> None:
    text = "ПИ 61056.65.17 ... отново ПИ 61056.65.17"
    assert geo.cadastral_ids(text) == ["61056.65.17"]


def test_cadastral_ids_empty_for_no_match() -> None:
    assert geo.cadastral_ids("Няма идентификатори тук") == []
    assert geo.cadastral_ids(None) == []


def test_kais_url_is_the_map_page_not_a_fabricated_deep_link() -> None:
    # Verified 2026-10-08: KAIS has no working deep-link query format, so
    # every identifier gets the same plain map-page URL (see geo.kais_url's
    # docstring) -- the identifier is shown as text for the reader to paste.
    assert geo.kais_url("51500.501.4") == "https://kais.cadastre.bg/bg/Map"
    assert geo.kais_url() == "https://kais.cadastre.bg/bg/Map"


def test_object_location_is_the_official_trailing_place() -> None:
    # Real 2024-12 capital-programme rows (Общо, rows 248 and later): the
    # municipality writes the location after the last comma.
    assert geo.object_settlements(
        "Изграждане на велоалея южно от третокласен път Слънчев бряг - Тънково, с.Тънково"
    )[0] == "tankovo"
    assert geo.object_settlements(
        "Направа на мост при км+7.080 южно от третокласен път III - 9061 между "
        "к.к. Слънчев бряг - с.Тънково, гр.Несебър"
    )[0] == "nesebar"
    assert geo.object_settlements(
        "Изграждане на велоалея на път Сл.бряг - с.Кошарица, с.Кошарица"
    ) == ["kosharitsa", "slanchev-bryag"]


def test_object_without_place_suffix_falls_back_to_first_named() -> None:
    assert geo.object_settlements('Компютри ДГ "Моряче" гр.Несебър, ж.к.Черно море') == ["nesebar"]
