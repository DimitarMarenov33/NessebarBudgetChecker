"""Numeric thresholds used by `analysis.rules`, with the legal basis for each.

All thresholds are overridable via environment variables (prefix `RULES_`,
e.g. `RULES_LATE_PUBLICATION_DEADLINE_DAYS=45`) or a local `.env` file, same
mechanism as `nessebar_budget.config.Settings`.

Legal grounding (see `docs/law/INDEX.md` and `docs/RULES.md` for the full
quotes/citations; texts extracted from lex.bg into `docs/law/*.txt`):

- Publication deadline for a contract/award notice after signing: ЗОП
  чл. 26, ал. 1, т. 1 -- "Възложителите изпращат за публикуване обявление за
  възлагане на поръчка в срок до: 1. тридесет дни след сключване на договор
  за обществена поръчка или рамково споразумение." The same 30-day figure is
  repeated for sub-threshold ("пряко възлагане"-adjacent) contracts at ЗОП
  чл. 194, ал. 4 ("В 30-дневен срок от сключването на договора възложителят
  изпраща за публикуване в РОП обявление за възлагане на обществена поръчка
  на стойност по чл. 20, ал. 3 или 7."). Both route to the same 30-day
  figure, so a single threshold covers every contract in `procurements`.

- Direct-award ("пряко възлагане") thresholds: ЗОП чл. 20, ал. 4 -- below
  these estimated values a municipality may award *without* any competitive
  procedure at all:
    т.1  80 000 лв. -- строителство (construction/works)
    т.2 100 000 лв. -- услуги по приложение № 2 (Annex II services)
    т.3  50 000 лв. -- доставки и услуги извън тези по т. 2 (other supplies/services)
  `UnmatchedSpendingRule` uses т.3's 50 000 лв. (the lowest/most general
  figure) as its trigger level: capital-ledger objects mix construction,
  design, and equipment-supply spending, and using the lowest threshold is
  the more conservative choice for *when to even look* for a contract (it
  only widens the candidate pool that the -- separately conservative --
  matcher then has to fail to match before anything is flagged).

- Contract-value growth via amendments: ЗОП чл. 116, ал. 2 caps the
  *cumulative* price increase from an amendment under чл. 116, ал. 1, т. 2/3
  at 50% of the original contract value ("не може да надхвърля с повече от
  50 на сто стойността на основния договор"). `AnnexGrowthRule`'s 10%
  warning level is deliberately far below that statutory ceiling: it is an
  early transparency signal ("this contract's value has grown and may be
  worth a look"), not a claim that the 50% legal limit has been breached.

- BGN/EUR thresholds: ЗОП still states its thresholds in leva. Bulgaria's
  euro adoption carried over the long-standing currency-board peg as the
  fixed conversion rate, so BGN-denominated legal thresholds are converted
  to EUR via that same fixed rate for comparison against this project's
  EUR-denominated data.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Fixed BGN/EUR conversion rate (currency-board peg, carried over as the
#: fixed rate for euro adoption). Confirmed against this project's own data:
#: every `procurements.raw_json.contract` row with `Currency == 3` (BGN)
#: satisfies `ContractValue / BGN_EUR_RATE == ContractValueEuro` to the cent
#: (see `scrapers/eop.py`'s `CURRENCY_CODES`).
BGN_EUR_RATE = 1.95583


class Thresholds(BaseSettings):
    """Rule thresholds. See module docstring for the legal basis of each."""

    model_config = SettingsConfigDict(
        env_prefix="RULES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LatePublicationRule --- ЗОП чл. 26, ал. 1, т. 1 (и чл. 194, ал. 4)
    late_publication_deadline_days: int = 30
    # ЗОП чл. 26 sets a 30-day deadline to *send* the award notice; the registry
    # publishes it some days later, so a short grace period avoids flagging
    # ordinary processing lag. Severity escalates with the delay.
    late_publication_grace_days: int = 7
    late_publication_warning_days: int = 14
    late_publication_high_days: int = 60
    late_publication_law_ref: str = "чл. 26, ал. 1, т. 1 ЗОП (срок 30 дни след сключване на договора)"

    # --- OverspendVsPlanRule ---
    overspend_ratio: float = 1.02
    overspend_abs_eur: float = 10_000.0

    # --- PlanJumpRule ---
    plan_jump_ratio: float = 1.5  # i.e. +50%
    plan_jump_abs_eur: float = 100_000.0
    new_object_min_eur: float = 250_000.0

    # --- SingleBidderRule ---
    single_bidder_warning_eur: float = 100_000.0
    single_bidder_high_eur: float = 500_000.0

    # --- ContractorConcentrationRule ---
    concentration_min_contracts: int = 3
    concentration_share: float = 0.15
    concentration_window_days: int = 730  # ~24 months

    # --- UnmatchedSpendingRule --- ЗОП чл. 20, ал. 4, т. 3
    direct_award_threshold_bgn: float = 50_000.0
    direct_award_law_ref: str = (
        "чл. 20, ал. 4, т. 3 ЗОП (праг за директно възлагане на доставки/услуги: 50 000 лв.)"
    )
    match_jaccard_threshold: float = 0.35
    match_value_ratio_low: float = 0.5
    match_value_ratio_high: float = 2.0

    # --- AnnexGrowthRule --- ЗОП чл. 116, ал. 2 (виж бележката по-горе)
    annex_growth_ratio: float = 1.10
    annex_growth_law_ref: str = (
        "чл. 116, ал. 2 ЗОП (законов таван на натрупаното увеличение: 50% "
        "от стойността на основния договор; тук се сигнализира много по-рано, при +10%)"
    )

    @property
    def direct_award_threshold_eur(self) -> float:
        return self.direct_award_threshold_bgn / BGN_EUR_RATE


def get_thresholds() -> Thresholds:
    """Return a freshly loaded Thresholds instance (reads env/`.env` each call)."""
    return Thresholds()
