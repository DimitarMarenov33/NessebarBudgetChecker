"""Numeric thresholds used by `analysis.rules`, with the legal basis for each.

All thresholds are overridable via environment variables (prefix `RULES_`,
e.g. `RULES_LATE_PUBLICATION_DEADLINE_DAYS=45`) or a local `.env` file, same
mechanism as `nessebar_budget.config.Settings`. Tuple-valued fields take a
JSON list (e.g. `RULES_ZOP_SUPPLIES_BOUNDS_BGN='[50000, 100000, 273812]'`).

Every article cited below was checked against the local consolidated texts
in `docs/law/` (`grep "^Чл\\. N\\."` in zop.txt / zpf.txt / zmsma.txt / zdoi.txt,
lex.bg, extracted 2026-10-06; see `docs/law/INDEX.md`). The quoted fragment
is the key sentence, verbatim. Two sources are cited but are NOT in
`docs/law/`: the Наказателен кодекс (чл. 248а, cited by the project brief for
EU-funds fraud) and the Закон за счетоводството (cited without an article
number). They are marked as such wherever they appear.

ЗОП (Закон за обществените поръчки, zop.txt)
---------------------------------------------
- чл. 2, ал. 2 -- "възложителите нямат право да ограничават конкуренцията
  чрез включване на условия или изисквания, които дават необосновано
  предимство или необосновано ограничават участието на стопански субекти".
- чл. 18, ал. 1 -- the procedure catalogue: т. 8 "договаряне без
  предварително обявление", т. 12 "публично състезание", т. 13 "пряко
  договаряне".
- чл. 20 -- value thresholds (all in leva, all "изм. - ДВ, бр. 88 от 2023 г.,
  в сила от 01.01.2024 г."):
    ал. 1, т. 1 "а) 10 526 116 лв. - за строителство; б) 273 812 лв. - за
      доставки и услуги; в) 1 466 850 лв. - за услуги по приложение № 2"
    ал. 2 "т. 1 при строителство - от 300 000 лв. до 10 526 116 лв.;
      т. 2 при доставки и услуги ... - от 100 000 лв. до съответния праг по ал. 1"
    ал. 3 "т. 1 при строителство - от 80 000 лв. до 300 000 лв.;
      т. 2 при доставки и услуги, с изключение на услугите по приложение № 2 -
      от 50 000 лв. до 100 000 лв."
    ал. 4 "т. 1 80 000 лв. - при строителство; т. 2 100 000 лв. - при услуги по
      приложение № 2; т. 3 50 000 лв. - при доставки и услуги извън тези по т. 2"
  The pre-2024 values are NOT in our local text (it only carries the current
  wording plus "изм." notes). `splitting` therefore applies the current
  boundaries to all years -- which can only under-detect if, as generally
  reported, the earlier boundaries were lower (unverified locally) -- and
  `near_threshold` only looks at procedures opened on/after 2024-01-01.
- чл. 21, ал. 4 "Когато обществената поръчка включва няколко обособени
  позиции ... стойността на поръчката е равна на сбора от стойностите на
  всички позиции." (=> lots of one tender are one unit, never "splitting").
- чл. 21, ал. 8 -- regular/renewable supplies and services are valued on
  "действителната обща стойност на поръчките от същия вид, които са възложени
  през предходните 12 месеца" (=> successive annual renewals are separate
  12-month procurements, see `splitting_renewal_gap_days`).
- чл. 21, ал. 14 "Изборът на метод за изчисляване на прогнозната стойност ...
  не трябва да се използва за прилагане на ред за възлагане за по-ниски
  стойности."
- чл. 21, ал. 15 "Не се допуска разделяне на обществена поръчка на части с
  което се прилага ред за възлагане за по-ниски стойности, освен в случаите
  по ал. 6."
- чл. 21, ал. 16 "Не се смята за разделяне възлагането в рамките на 12
  месеца на две или повече поръчки: 1. с обект изпълнение на строеж ...;
  2. с идентичен или сходен предмет, които не са били известни на
  възложителя ..." (=> the 12-month window; works are only grouped when they
  concern the same object).
- чл. 26, ал. 1, т. 1 -- "Възложителите изпращат за публикуване обявление за
  възлагане на поръчка в срок до: 1. тридесет дни след сключване на договор
  за обществена поръчка или рамково споразумение." The same 30 days are
  repeated for публично състезание at чл. 185, т. 1 ("Възложителят изпраща
  за публикуване обявление по образец в срок 30 дни от: 1. подписване на
  договора") and for чл. 20, ал. 3 contracts at чл. 194, ал. 4 ("В 30-дневен
  срок от сключването на договора възложителят изпраща за публикуване в РОП
  обявление за възлагане на обществена поръчка на стойност по чл. 20, ал. 3
  или 7.").
- чл. 36, ал. 1, т. 12 -- the register publishes "договорите за обществени
  поръчки и рамковите споразумения, както и приложенията към тях".
- чл. 48, ал. 1, т. 1 -- technical specifications "позволяват точно
  определяне на параметрите на предмета на поръчката".
- чл. 74, ал. 1 "Минималният срок за получаване на оферти в открита
  процедура е 30 дни от датата на изпращане на обявлението"; ал. 2 and ал. 4
  allow shortening, "не по-кратък от 15 дни" (prior-information notice /
  urgency, which ал. 5 says must be motivated in the notice).
- чл. 79, ал. 1 "Публичните възложители могат да прилагат процедура на
  договаряне без предварително обявление само в следните случаи"; ал. 6 "С
  решението за откриване на процедурата възложителят мотивира приложимото
  основание по ал. 1." (т. 7: goods traded on a commodity exchange.)
- чл. 112, ал. 6 -- the contract is signed "не преди изтичане на 14-дневен
  срок от уведомяването ... за решението за определяне на изпълнител"
  (ал. 7, т. 2 waives this when the winner is the only participant -- which
  is why the evaluation margin below deliberately does NOT add 14 days).
- чл. 116, ал. 2 "ако се налага увеличение на цената, то не може да
  надхвърля с повече от 50 на сто стойността на основния договор ...
  Когато се правят последователни изменения, ограничението се прилага за
  общата стойност на измененията."
- чл. 178, ал. 2 (публично състезание) "Срокът не може да бъде по-кратък от
  20 дни и започва да тече от изпращането на обявлението за публикуване."
  ал. 3/ал. 4 allow shortening, "не по-кратък от 10 дни".
- чл. 182, ал. 1 "Възложителят може да проведе пряко договаряне с определени
  лица при наличие на някое от основанията по чл. 79, ал. 1, т. 3 и т. 5 - 9
  или когато: ..."; ал. 2 "С решението за откриване на процедурата
  възложителят мотивира приложимото основание по ал. 1."
- чл. 188, ал. 1 (събиране на оферти с обява) "Срокът за получаване на
  оферти ... не може да бъде по-кратък от 10 дни, от публикуването на
  обявата." (wording in force since 22.12.2023 -- older обяви only get the
  softer "signal" tier, never "violation").
- чл. 191, ал. 1 -- a "покана до определени лица" instead of a public обява
  is allowed only "когато е налице някое от следните основания".
- Fines: чл. 247, ал. 1 (breach of чл. 2, ал. 2 or чл. 21, ал. 15: "глоба в
  размер 2 на сто от стойността на сключения договор"); чл. 250а
  ("сключи договор в резултат на процедура по чл. 18, ал. 1, т. 8 - 10 или
  т. 13, без да са налице условията за прилагането и"); чл. 255, ал. 3
  ("измени договор ... без да са налице основанията по чл. 116, ал. 1");
  чл. 256а ("не изпрати в срок информацията, подлежаща на публикуване").
- Приложение № 2 -- the "социални и други специфични услуги" (CPV lists for
  health/social, education, culture, hotel/restaurant, legal, security,
  postal ...); approximated by `annex2_cpv_prefixes` below.
- Приложение № 4, част В, т. 6 -- award notice: "естество и количество или
  стойност на доставките" (a disjunction -- see `missing_quantity`).

ЗПФ (Закон за публичните финанси, zpf.txt)
-----------------------------------------
- чл. 11, ал. 3 -- "по бюджета на община - кметът на общината" is the
  първостепенен разпоредител с бюджет.
- чл. 102, ал. 1 "Първостепенните разпоредители с бюджет не могат да
  извършват разходи и да поемат задължения за разходи за текущата година,
  надхвърлящи общия размер на утвърдените разходи".
- чл. 124, ал. 2 "Промените по общинския бюджет, извън тези по чл. 56, ал. 2,
  се одобряват от общинския съвет."
- чл. 125, ал. 1 -- the council may authorise the mayor to make
  "компенсирани промени" within/between activities.
- чл. 128, ал. 1 "Не се допуска извършването на разходи, натрупването на нови
  задължения за разходи и/или поемането на ангажименти за разходи, както и
  започването на програми или проекти, които не са предвидени в годишния
  бюджет на общината."
- чл. 133, ал. 1 "Първостепенните разпоредители с бюджет представят в
  Министерството на финансите ежемесечно и на тримесечие отчети за
  изпълнението на бюджетите"; ал. 4 "Отчетите по ал. 1 и 3 ... се публикуват
  на интернет страниците на съответните първостепенни разпоредители".
- чл. 140, ал. 5 -- the council adopts the annual report "не по-късно от 30
  септември на годината, следваща отчетната година"; ал. 6 "Приетият отчет
  ... се публикуват на интернет страницата на общината."
- чл. 173 "За неизпълнение на задължение за публикуване на информация или на
  документи на интернет страница, предвидено в този закон ... виновното
  длъжностно лице се наказва с глоба от 100 до 500 лв."

ЗДОИ (Закон за достъп до обществена информация, zdoi.txt)
---------------------------------------------------------
- чл. 15, ал. 1, т. 7 "информация за бюджета и финансовите отчети на
  администрацията, която се публикува съгласно Закона за публичните финанси".
- чл. 15а, ал. 4 "Информацията по чл. 15 се публикува, съответно се обновява,
  в срок до три работни дни от приемането на съответния акт или от
  създаването на съответната информация".

ЗМСМА (zmsma.txt)
-----------------
- чл. 22, ал. 2 "Актовете на общинския съвет се разгласяват на населението на
  общината в срока по ал. 1 [7 дни] ... чрез интернет страницата на общинския
  съвет или на общината".

BGN/EUR: ЗОП still states its thresholds in leva. Bulgaria's euro adoption
carried over the long-standing currency-board peg as the fixed conversion
rate, so BGN-denominated legal thresholds are converted to EUR via that same
fixed rate for comparison against this project's EUR-denominated data.
"""

from __future__ import annotations

import datetime as dt

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fixed BGN/EUR conversion rate (currency-board peg, carried over as the
#: fixed rate for euro adoption). Confirmed against this project's own data:
#: every `procurements.raw_json.contract` row with `Currency == 3` (BGN)
#: satisfies `ContractValue / BGN_EUR_RATE == ContractValueEuro` to the cent
#: (see `scrapers/eop.py`'s `CURRENCY_CODES`).
BGN_EUR_RATE = 1.95583


def bgn_to_eur(value_bgn: float) -> float:
    """Convert a leva amount (e.g. a ЗОП чл. 20 threshold) to EUR at the peg."""
    return value_bgn / BGN_EUR_RATE


class Thresholds(BaseSettings):
    """Rule thresholds. See module docstring for the legal basis of each."""

    model_config = SettingsConfigDict(
        env_prefix="RULES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- late_publication --- ЗОП чл. 26, ал. 1, т. 1 (и чл. 185, т. 1; чл. 194, ал. 4)
    late_publication_deadline_days: int = 30
    # ЗОП чл. 26 sets a 30-day deadline to *send* the award notice; the registry
    # publishes it some days later, so a short grace period avoids flagging
    # ordinary processing lag. Severity escalates with the delay.
    late_publication_grace_days: int = 7
    late_publication_warning_days: int = 14
    late_publication_high_days: int = 60
    late_publication_law_ref: str = (
        "чл. 26, ал. 1, т. 1 ЗОП (обявлението за възлагане се изпраща до 30 дни след "
        "сключване на договора); чл. 256а ЗОП (глоба при неизпращане в срок)"
    )

    # --- overspend_vs_plan --- (softer case: a plan exists but is exceeded)
    overspend_ratio: float = 1.02
    overspend_abs_eur: float = 10_000.0

    # --- unplanned_spending --- ЗПФ чл. 128, ал. 1 / чл. 102, ал. 1 / чл. 124, ал. 2
    #: Spending on an object with no annual plan at all is only flagged from
    #: this amount (tiny 600 € air-conditioners with plan 0 are ledger noise).
    unplanned_min_spent_eur: float = 10_000.0
    #: Cumulative spending above the object's total estimated cost is only
    #: flagged when the excess itself is at least this large.
    unplanned_min_excess_eur: float = 10_000.0

    # --- plan_jump ---
    plan_jump_ratio: float = 1.5  # i.e. +50%
    plan_jump_abs_eur: float = 100_000.0
    new_object_min_eur: float = 250_000.0

    # --- single_bidder ---
    single_bidder_warning_eur: float = 100_000.0
    single_bidder_high_eur: float = 500_000.0

    # --- contractor_concentration ---
    concentration_min_contracts: int = 3
    concentration_share: float = 0.15
    concentration_window_days: int = 730  # ~24 months

    # --- unmatched_spending --- ЗОП чл. 20, ал. 4, т. 3
    direct_award_threshold_bgn: float = 50_000.0
    direct_award_law_ref: str = (
        "чл. 20, ал. 4, т. 3 ЗОП (праг за директно възлагане на доставки/услуги: 50 000 лв.)"
    )
    match_jaccard_threshold: float = 0.35
    match_value_ratio_low: float = 0.5
    match_value_ratio_high: float = 2.0

    # --- annex_growth (signal) / annex_over_cap (violation) --- ЗОП чл. 116, ал. 2
    annex_growth_ratio: float = 1.10
    #: чл. 116, ал. 2: cumulative increase "не може да надхвърля с повече от 50
    #: на сто стойността на основния договор" -> current > original * 1.5.
    annex_cap_ratio: float = 1.50
    #: Above this ratio the register data itself is implausible and both annex
    #: rules stay silent: hand-checking data/nessebar.db (2026-10-07) found every
    #: such case to be a data-entry artifact -- CurrentContractValue entered in
    #: stotinki/cents (exactly +9,900%), or a unit-price contract whose
    #: "ContractValue" is the sum of unit prices (0.59 €, 541 €, 2,023 € against
    #: six-figure estimates).
    annex_max_plausible_ratio: float = 5.0
    annex_growth_law_ref: str = (
        "чл. 116, ал. 2 ЗОП (законов таван на натрупаното увеличение: 50% "
        "от стойността на основния договор; тук се сигнализира много по-рано, при +10%)"
    )
    annex_over_cap_law_ref: str = (
        "чл. 116, ал. 2 ЗОП (увеличението на цената не може да надхвърля 50% от "
        "стойността на основния договор); чл. 255, ал. 3 ЗОП (глоба при изменение без основание)"
    )

    # --- missing_quantity / price_unverifiable --- ЗОП чл. 2, ал. 2; чл. 48, ал. 1;
    # Приложение № 4, част В, т. 6
    missing_quantity_min_value_eur: float = 20_000.0
    missing_quantity_warning_eur: float = 100_000.0
    missing_quantity_high_eur: float = 500_000.0
    #: `price_unverifiable`: a description shorter than this (after HTML
    #: cleanup, and not just the title repeated) counts as "no technical
    #: description published".
    price_unverifiable_min_description_chars: int = 80
    price_unverifiable_warning_eur: float = 100_000.0
    # Scope A (EOP supply contracts, TypeOfContract == 2): the award notice's
    # minimum legal content (quantity OR value) is already satisfied by the
    # value alone, so this is phrased as a transparency signal, not a breach.
    missing_quantity_law_ref: str = (
        "чл. 2, ал. 2 ЗОП (съответствие с количеството/обема на поръчката); "
        "Приложение № 4, част В, т. 6 ЗОП (обявлението за възлагане посочва "
        "естество и количество или стойност на доставките)"
    )
    price_unverifiable_law_ref: str = (
        "чл. 48, ал. 1, т. 1 ЗОП (техническите спецификации позволяват точно определяне "
        "на параметрите на предмета на поръчката); чл. 36, ал. 1, т. 12 ЗОП (публикуване "
        "на договорите с приложенията към тях)"
    )
    # Scope B (§ 52 capital budget objects): the capital ledger is a ЗПФ
    # budget-execution document, not a ЗОП procurement notice -- there is no
    # equivalent direct legal requirement to itemize quantities here, only the
    # general public-finance transparency principle.
    missing_quantity_budget_law_ref: str = (
        "принцип на прозрачност, чл. 20, т. 7 ЗПФ (не е пряко правно "
        "изискване за посочване на брой/количество в разчета за капиталови разходи)"
    )

    # --- splitting / near_threshold --- ЗОП чл. 20 (ДВ бр. 88/2023, в сила от 01.01.2024)
    #: Band boundaries in leva, ascending: [чл. 20, ал. 4 (direct) | ал. 3 upper
    #: (обява/покана) | ал. 1 (EU-level, open procedure)].
    zop_works_bounds_bgn: tuple[float, ...] = (80_000.0, 300_000.0, 10_526_116.0)
    zop_supplies_bounds_bgn: tuple[float, ...] = (50_000.0, 100_000.0, 273_812.0)
    #: Приложение № 2 services have no ал. 3 band: direct below 100 000 лв.
    #: (ал. 4, т. 2), ал. 2 regime up to 1 466 850 лв. (ал. 1, т. 1, буква "в").
    zop_annex2_bounds_bgn: tuple[float, ...] = (100_000.0, 1_466_850.0)
    #: Main-CPV prefixes treated as Приложение № 2 services -- an approximation
    #: of the annex's CPV list (zop.txt): government/social 75, education 80
    #: (80000000-80660000), health/social 85 (85000000-85323000), culture 92
    #: (92000000-92700000), hotel/restaurant 55, legal 791 (79100000-79140000),
    #: security 797 (79700000-79721000), events 7995, staffing 7961/7962, postal
    #: 641, and only the *listed* 98 codes (98000000, 98120000, 9813xxxx,
    #: 98200000, 98500000, 98513000-98514000, 98900000/98910000) -- not e.g.
    #: 98351110 (parking enforcement), which is an ordinary service.
    annex2_cpv_prefixes: tuple[str, ...] = (
        "75", "80", "85", "92", "55", "791", "797", "7995", "7961", "7962", "641",
        "9800", "9812", "9813", "9820", "9850", "98513", "98514", "989",
    )
    #: Date from which the чл. 20 values above are in force (per zop.txt).
    zop_thresholds_effective_from: dt.date = dt.date(2024, 1, 1)
    #: чл. 21, ал. 16: "в рамките на 12 месеца".
    splitting_window_days: int = 365
    splitting_min_members: int = 2
    #: Members of a group that are all at least this far apart look like
    #: successive annual contracts for a recurring need (a yearly telecom or
    #: maintenance subscription renewed every ~12 months), which ЗОП чл. 21,
    #: ал. 8 values per 12-month period -- not splitting. Such groups are
    #: skipped. Hand-check 2026-10-07: 3 of the 6 raw groups were exactly this
    #: (331, 340 and 365 days apart).
    splitting_renewal_gap_days: int = 300
    #: CPV-path title similarity (Jaccard over `matching.tokenize`).
    splitting_title_jaccard: float = 0.5
    #: `high` when the group's sum exceeds this multiple of the boundary.
    splitting_high_multiple: float = 2.0
    splitting_law_ref: str = (
        "чл. 21, ал. 15 ЗОП (забрана за разделяне на поръчка с цел по-лек ред); "
        "чл. 20 ЗОП (стойностни прагове); чл. 247, ал. 1 ЗОП (глоба)"
    )
    #: "within 5% below a boundary".
    near_threshold_pct: float = 0.05
    near_threshold_law_ref: str = (
        "чл. 20, ал. 2-3 ЗОП (стойностни прагове); чл. 21, ал. 14 ЗОП (методът за "
        "прогнозната стойност не се използва за прилагане на ред за по-ниски стойности)"
    )

    # --- exceptional_procedure --- ЗОП чл. 79 / чл. 182 / чл. 191; глоба чл. 250а
    exceptional_warning_eur: float = 100_000.0
    exceptional_high_eur: float = 500_000.0

    # --- short_offer_deadline --- ЗОП чл. 74, чл. 178, чл. 188
    offer_min_days_open: int = 30  # чл. 74, ал. 1
    offer_bare_min_days_open: int = 15  # чл. 74, ал. 2 и 4
    offer_min_days_public_competition: int = 20  # чл. 178, ал. 2
    offer_bare_min_days_public_competition: int = 10  # чл. 178, ал. 3 и 4
    offer_min_days_collecting_offers: int = 10  # чл. 188, ал. 1
    offer_bare_min_days_collecting_offers: int = 10  # чл. 188, ал. 1 (no reduction)
    #: чл. 188, ал. 1's current wording ("изм. и доп. - ДВ, бр. 88 от 2023 г.,
    #: в сила от 22.12.2023 г.") -- older обяви are never called a violation.
    collecting_offers_rule_effective_from: dt.date = dt.date(2023, 12, 22)
    #: Realistic minimum time between the offer deadline and signing: opening,
    #: evaluation, decision, notification. Deliberately excludes чл. 112,
    #: ал. 6's 14-day standstill, which ал. 7, т. 2 waives for a sole bidder.
    offer_evaluation_margin_days: int = 5

    # --- bid_at_ceiling --- ЗОП чл. 21, ал. 1-2; чл. 2, ал. 2
    bid_ceiling_ratio: float = 0.98
    #: Contract values more than 5% ABOVE the estimate are not "at the ceiling"
    #: -- in this data they are VAT/currency/lot mismatches, not price signals.
    bid_ceiling_max_ratio: float = 1.05
    bid_ceiling_warning_eur: float = 100_000.0
    bid_ceiling_high_eur: float = 500_000.0

    # --- missing_monthly_report / missing_annual_report --- ЗПФ чл. 133, 140, 173; ЗДОИ чл. 15а
    reports_first_period: str = "2019-01"
    #: Budget-report kinds that count as "the monthly execution report".
    report_kinds: tuple[str, ...] = ("B1", "B3")
    #: missing_annual_report: the 12/Y report is expected by this month/day of Y+1.
    annual_report_due_month: int = 3
    annual_report_due_day: int = 31

    @property
    def direct_award_threshold_eur(self) -> float:
        return bgn_to_eur(self.direct_award_threshold_bgn)


def get_thresholds() -> Thresholds:
    """Return a freshly loaded Thresholds instance (reads env/`.env` each call)."""
    return Thresholds()
