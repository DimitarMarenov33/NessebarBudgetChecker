"""Unit tests for `nessebar_budget.scrapers.declarations`.

No network access: `parse_register_page` and `parse_declaration_text` are
pure functions exercised against fixtures in `data/samples/declarations/`.
Every individual declaration PDF actually fetched while building this
scraper turned out to be a scanned image with no text layer (see
`docs/sources/DECLARATIONS.md`), so there is no real text-bearing PDF to use
as a fixture -- `declaration_text_sample.txt` is a synthetic sample written
in the standard declaration form's own wording, used to exercise
`parse_declaration_text`'s section-based extraction.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from nessebar_budget.db.models import Base, Official
from nessebar_budget.db.repo import upsert_official
from nessebar_budget.scrapers.declarations import (
    _candidate_full_names,
    normalize_name,
    parse_declaration_text,
    parse_register_page,
    resolve_full_name,
)

SAMPLES_DIR = Path(__file__).resolve().parent.parent / "data" / "samples" / "declarations"

REGISTER_URL = (
    "https://os-nessebar.eu/deklaracii-po-zpk/publichen-registar-na-deklaraciite-za-"
    "nesavmestimost-i-deklaraciite-za-promyana-na-deklariranite-obstoyatelstva-v-"
    "deklaraciite-za-nesavmestimost-sagl-zakona-za-protivodeystvie-na-korupciyata-"
    "deklaracii-po-chl49-al1-t1-i-t3-ot-zpk-kmet-na-obshtina-kmetove-na-kmetstva-"
    "mandat-2023-2027"
)


# -- parse_register_page -----------------------------------------------------


class TestParseRegisterPage:
    def _records(self) -> list[dict]:
        html = (SAMPLES_DIR / "register_kmet_obshtina.html").read_text(encoding="utf-8")
        return parse_register_page(html, REGISTER_URL)

    def test_extracts_one_record_per_person(self) -> None:
        records = self._records()
        assert len(records) == 4
        assert [r["name"] for r in records] == [
            "Николай Кирилов Димитров",
            "Валентин Григоров Станчев",
            "Златин Стойков Георгиев",
            "Жельо Кръстев Желев",
        ]

    def test_skips_the_non_pdf_register_summary_link(self) -> None:
        records = self._records()
        names = [r["name"] for r in records]
        assert not any("РЕГИСТЪР" in name for name in names)
        assert not any(name.startswith("РЕГИСТЪР") for name in names)

    def test_role_is_split_off_a_mayor_suffix(self) -> None:
        records = self._records()
        by_name = {r["name"]: r for r in records}
        assert by_name["Николай Кирилов Димитров"]["role"] == "mayor"

    def test_role_is_split_off_a_village_mayor_suffix(self) -> None:
        records = self._records()
        by_name = {r["name"]: r for r in records}
        assert by_name["Валентин Григоров Станчев"]["role"] == "village_mayor"
        assert by_name["Златин Стойков Георгиев"]["role"] == "village_mayor"

    def test_double_dash_suffix_is_still_split_correctly(self) -> None:
        """The real page prints "Желев - - Кмет на кметство ..." (two
        dashes); the name must not retain a trailing dash."""
        records = self._records()
        by_name = {r["name"]: r for r in records}
        assert "Жельо Кръстев Желев" in by_name
        assert by_name["Жельо Кръстев Желев"]["role"] == "village_mayor"

    def test_mandate_detected_from_page_text(self) -> None:
        records = self._records()
        assert all(r["mandate"] == "2023-2027" for r in records)

    def test_source_and_document_urls(self) -> None:
        records = self._records()
        by_name = {r["name"]: r for r in records}
        record = by_name["Николай Кирилов Димитров"]
        assert record["source_url"] == REGISTER_URL
        assert record["document_url"] == (
            "https://os-nessebar.eu/assets/upload/files/nikolaj_dimitrov%282%29.pdf"
        )

    def test_default_role_from_url_when_no_suffix(self) -> None:
        """A councillor register page has no per-row suffix; role falls
        back to the page-level default inferred from the URL slug."""
        html = """
        <div class="prose font-serif">
          <p><strong>... за общински съветници, мандат 2023-2027 ...</strong></p>
          <p><a href="/assets/upload/files/ivan_ivanov.pdf">Иван Иванов</a></p>
        </div>
        """
        url = (
            "https://os-nessebar.eu/deklaracii-po-zpk/publichen-registar-na-deklaraciite-"
            "za-nesavmestimost-...-za-obshtinski-savetnici-mandat-2023-2027"
        )
        records = parse_register_page(html, url)
        assert len(records) == 1
        assert records[0]["role"] == "councillor"
        assert records[0]["mandate"] == "2023-2027"


# -- parse_declaration_text --------------------------------------------------


class TestParseDeclarationText:
    def _entries(self) -> list[dict]:
        text = (SAMPLES_DIR / "declaration_text_sample.txt").read_text(encoding="utf-8")
        return parse_declaration_text(text)

    def test_extracts_four_entries(self) -> None:
        entries = self._entries()
        assert len(entries) == 4

    def test_company_entry_with_combined_relation_and_eik(self) -> None:
        entries = self._entries()
        entry = entries[0]
        assert entry["company_name"] == "Слънчев бряг турс ЕООД"
        assert entry["eik"] == "147025511"
        assert "едноличен собственик" in entry["relation"]
        assert "управител" in entry["relation"]
        assert "ЕИК 147025511" in entry["raw"]

    def test_company_entry_with_thirteen_digit_eik(self) -> None:
        entries = self._entries()
        entry = entries[1]
        assert entry["company_name"] == "Инвест Ко ООД"
        assert entry["eik"] == "1234567891234"
        assert entry["relation"] == "съдружник"

    def test_debt_entry_has_no_eik_but_keeps_raw_context(self) -> None:
        entries = self._entries()
        entry = entries[2]
        assert entry["company_name"] == "Банка ДСК ЕАД"
        assert entry["eik"] is None
        assert entry["relation"] == "задължение над 5 000 лв."
        assert "25 000 лв." in entry["raw"]

    def test_contract_entry(self) -> None:
        entries = self._entries()
        entry = entries[3]
        assert entry["company_name"] == "Техно Билд ООД"
        assert entry["eik"] == "203456789"
        assert "договор" in entry["relation"]

    def test_empty_text_yields_no_entries(self) -> None:
        assert parse_declaration_text("") == []

    def test_boilerplate_instruction_text_without_eik_or_keyword_is_ignored(self) -> None:
        text = (
            "ЧАСТ ВТОРА. УЧАСТИЕ В ТЪРГОВСКИ ДРУЖЕСТВА\n"
            "Попълва се само ако е приложимо.\n"
        )
        assert parse_declaration_text(text) == []

    def test_section_resets_on_an_unrelated_part_header(self) -> None:
        """A line with an EIK-looking number *after* the company section has
        ended (a new, unrelated "ЧАСТ" header was reached) is not picked up."""
        text = (
            "ЧАСТ ВТОРА. УЧАСТИЕ В ТЪРГОВСКИ ДРУЖЕСТВА\n"
            "„Алфа“ ЕООД, ЕИК 111111111 – съдружник\n"
            "ЧАСТ ПЕТА. ДРУГИ ОБСТОЯТЕЛСТВА\n"
            "Служебен номер 222222222, нищо общо с дружество.\n"
        )
        entries = parse_declaration_text(text)
        assert len(entries) == 1
        assert entries[0]["eik"] == "111111111"


# -- normalize_name -----------------------------------------------------------


class TestNormalizeName:
    def test_lowercases_and_collapses_whitespace(self) -> None:
        assert normalize_name("Александър  Стоянов") == "александър стоянов"

    def test_strips_hyphen_and_nbsp_between_name_parts(self) -> None:
        assert normalize_name("Венета Танева-Кючукова") == "венета танева кючукова"
        assert normalize_name("Александър\xa0 Стоянов") == "александър стоянов"

    def test_strips_quotes_and_punctuation(self) -> None:
        assert normalize_name('„Иван" Петров, Иванов.') == "иван петров иванов"

    def test_same_person_differently_spaced_normalizes_identically(self) -> None:
        assert normalize_name("Иван   Петров") == normalize_name("Иван Петров")


# -- full-name enrichment -----------------------------------------------------


class TestCandidateFullNames:
    def _names(self) -> list[str]:
        html = (SAMPLES_DIR / "sastav_trimmed.html").read_text(encoding="utf-8")
        return _candidate_full_names(html)

    def test_extracts_four_full_names(self) -> None:
        names = self._names()
        assert len(names) == 4

    def test_plain_three_part_name(self) -> None:
        assert "Александър Стоянов Стоянов" in self._names()

    def test_hyphenated_surname_with_spaced_dash_is_compacted(self) -> None:
        """The real page prints "Венета Танева Танева - Кючукова" (spaces
        around the dash); the candidate must read as a single compacted
        hyphenated surname, not be rejected or split oddly."""
        assert "Венета Танева Танева-Кючукова" in self._names()

    def test_does_not_pick_up_nav_or_footer_text(self) -> None:
        names = self._names()
        assert not any("Контакти" in n for n in names)
        assert not any("Съвет Несебър" in n for n in names)

    def test_empty_html_yields_no_candidates(self) -> None:
        assert _candidate_full_names("<html><body></body></html>") == []


class TestResolveFullName:
    def _names(self) -> list[str]:
        html = (SAMPLES_DIR / "sastav_trimmed.html").read_text(encoding="utf-8")
        return _candidate_full_names(html)

    def test_unique_first_and_last_name_match(self) -> None:
        assert resolve_full_name("Александър Стоянов", self._names()) == (
            "Александър Стоянов Стоянов"
        )

    def test_matches_ignore_the_middle_patronymic(self) -> None:
        # "Стоянов" (patronymic) and "Стоянов" (surname) both appear in the
        # candidate; only the trailing (surname) token needs to match.
        assert resolve_full_name("Александър Стоянов", self._names()) is not None

    def test_hyphenated_surname_matches_despite_duplicated_token(self) -> None:
        """"Венета Танева Танева-Кючукова" repeats "танева" as both
        patronymic and the first half of the surname; the short name's two
        trailing tokens ("танева", "кючукова") must still match the
        candidate's trailing two tokens, not get confused by the repeat."""
        assert resolve_full_name("Венета Танева-Кючукова", self._names()) == (
            "Венета Танева Танева-Кючукова"
        )

    def test_shared_surname_disambiguated_by_first_name(self) -> None:
        """Two different candidates share the surname "Манолов"/"Георгиев"
        pattern (Георги Димитров Георгиев vs Георги Манолов Манолов); the
        short name's first token must pick the right one, not just any
        candidate whose trailing token matches."""
        names = self._names()
        assert resolve_full_name("Георги Георгиев", names) == "Георги Димитров Георгиев"
        assert resolve_full_name("Георги Манолов", names) == "Георги Манолов Манолов"

    def test_no_candidate_matches_returns_none(self) -> None:
        assert resolve_full_name("Георги Непознатов", self._names()) is None

    def test_short_name_with_only_one_token_never_matches(self) -> None:
        assert resolve_full_name("Георги", self._names()) is None

    def test_ambiguous_match_returns_none(self) -> None:
        """Two candidates sharing the same first name *and* the same
        trailing (surname) token must not resolve to either -- this is the
        "never guess on ambiguous matches" case."""
        candidates = ["Иван Петров Иванов", "Иван Георгиев Иванов"]
        assert resolve_full_name("Иван Иванов", candidates) is None


# -- upsert_official (in-memory SQLite) --------------------------------------


class TestUpsertOfficial:
    def _session(self) -> Session:
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return Session(engine)

    def test_inserts_a_new_official(self) -> None:
        session = self._session()
        record = {
            "name": "Иван Петров",
            "role": "councillor",
            "mandate": "2023-2027",
            "source_url": "https://os-nessebar.eu/register",
            "document_url": "https://os-nessebar.eu/assets/upload/files/ivan_petrov.pdf",
            "document_status": "scanned",
            "declared_interests_json": [],
        }
        upsert_official(session, record)
        session.commit()

        officials = session.scalars(select(Official)).all()
        assert len(officials) == 1
        assert officials[0].name == "Иван Петров"
        assert officials[0].name_normalized == "иван петров"
        assert officials[0].role == "councillor"
        assert officials[0].mandate == "2023-2027"
        assert officials[0].document_status == "scanned"

    def test_upsert_same_identity_updates_in_place(self) -> None:
        session = self._session()
        base = {
            "name": "Иван Петров",
            "role": "councillor",
            "mandate": "2023-2027",
            "source_url": "https://os-nessebar.eu/register-a",
            "document_url": "https://os-nessebar.eu/assets/upload/files/ivan_petrov.pdf",
            "document_status": "scanned",
            "declared_interests_json": [],
        }
        upsert_official(session, base)
        session.commit()

        updated = dict(base)
        updated["document_url"] = "https://os-nessebar.eu/assets/upload/files/ivan_petrov_v2.pdf"
        updated["document_status"] = "text"
        updated["declared_interests_json"] = [
            {"company_name": "Тест ЕООД", "eik": "111111111", "relation": "управител", "raw": "x"}
        ]
        upsert_official(session, updated)
        session.commit()

        officials = session.scalars(select(Official)).all()
        assert len(officials) == 1
        assert officials[0].document_url.endswith("ivan_petrov_v2.pdf")
        assert officials[0].document_status == "text"
        assert len(officials[0].declared_interests_json) == 1

    def test_different_role_is_a_different_identity(self) -> None:
        session = self._session()
        shared = {
            "name": "Иван Петров",
            "mandate": "2023-2027",
            "source_url": "https://os-nessebar.eu/register",
            "document_url": "https://os-nessebar.eu/assets/upload/files/ivan_petrov.pdf",
            "document_status": "scanned",
            "declared_interests_json": [],
        }
        upsert_official(session, {**shared, "role": "councillor"})
        upsert_official(session, {**shared, "role": "mayor"})
        session.commit()

        officials = session.scalars(select(Official)).all()
        assert len(officials) == 2
        assert {o.role for o in officials} == {"councillor", "mayor"}

    def test_name_variants_normalize_to_the_same_identity(self) -> None:
        """Differing whitespace/nbsp in the raw name must not create a
        second row for the same (role, mandate) person."""
        session = self._session()
        upsert_official(
            session,
            {
                "name": "Венета Танева-Кючукова",
                "role": "councillor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/veneta.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        upsert_official(
            session,
            {
                "name": "Венета  Танева-Кючукова",  # double space
                "role": "councillor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/veneta_v2.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        session.commit()

        officials = session.scalars(select(Official)).all()
        assert len(officials) == 1
        assert officials[0].document_url.endswith("veneta_v2.pdf")

    def test_enrichment_stores_full_name_short_name_and_source_url(self) -> None:
        session = self._session()
        upsert_official(
            session,
            {
                "name": "Георги Димитров Георгиев",  # already resolved by the scraper
                "short_name": "Георги Георгиев",
                "name_source_url": "https://os-nessebar.eu/sastav",
                "role": "councillor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/georgi.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        session.commit()

        official = session.scalars(select(Official)).one()
        assert official.name == "Георги Димитров Георгиев"
        assert official.name_normalized == "георги димитров георгиев"
        assert official.short_name == "Георги Георгиев"
        assert official.name_source_url == "https://os-nessebar.eu/sastav"

    def test_a_later_run_without_enrichment_still_finds_the_enriched_row(self) -> None:
        """A row enriched on run 1 must still be found (by `short_name`) on
        a run where the composition page couldn't be fetched -- not
        duplicated into a second, short-named row."""
        session = self._session()
        upsert_official(
            session,
            {
                "name": "Георги Димитров Георгиев",
                "short_name": "Георги Георгиев",
                "name_source_url": "https://os-nessebar.eu/sastav",
                "role": "councillor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/georgi.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        session.commit()

        # Simulate a later run where the composition page was unreachable:
        # the scraper passes the short name again, with no name_source_url.
        upsert_official(
            session,
            {
                "name": "Георги Георгиев",
                "short_name": "Георги Георгиев",
                "role": "councillor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/georgi_v2.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        session.commit()

        officials = session.scalars(select(Official)).all()
        assert len(officials) == 1
        # The full name is NOT downgraded back to the short form...
        assert officials[0].name == "Георги Димитров Георгиев"
        assert officials[0].name_source_url == "https://os-nessebar.eu/sastav"
        # ...but the other, always-fresh fields still got updated.
        assert officials[0].document_url.endswith("georgi_v2.pdf")

    def test_never_enriched_row_has_no_source_url_and_short_name_equals_name(self) -> None:
        """When `parsed` carries no `short_name` (no composition page covers
        this role/mandate, e.g. village mayors), `short_name` still gets set
        -- equal to `name` -- so a later call can match this row the same
        way as any other; `name_source_url` stays unset either way."""
        session = self._session()
        upsert_official(
            session,
            {
                "name": "Николай Пандазиев",
                "role": "village_mayor",
                "mandate": "2023-2027",
                "source_url": "https://os-nessebar.eu/register",
                "document_url": "https://os-nessebar.eu/assets/upload/files/nikolaj.pdf",
                "document_status": "scanned",
                "declared_interests_json": [],
            },
        )
        session.commit()

        official = session.scalars(select(Official)).one()
        assert official.name == "Николай Пандазиев"
        assert official.short_name == "Николай Пандазиев"
        assert official.name_source_url is None
