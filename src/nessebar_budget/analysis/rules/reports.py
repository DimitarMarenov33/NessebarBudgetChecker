"""Publication-gap rules over `budget_reports`: `missing_monthly_report`
(opacity) and `missing_annual_report` (signal).

The municipality (whose mayor is the първостепенен разпоредител с бюджет, ЗПФ
чл. 11, ал. 3) must report budget execution to the Ministry of Finance monthly
and publish those reports on its website: ЗПФ чл. 133, ал. 1 ("ежемесечно и на
тримесечие отчети за изпълнението на бюджетите") and ал. 4 ("се публикуват на
интернет страниците"); ЗДОИ чл. 15, ал. 1, т. 7 + чл. 15а, ал. 4 (within three
working days); fine: ЗПФ чл. 173.

"Missing" means *not in our database*: the scraper may not have found a file
the municipality did publish (e.g. under a different URL pattern). Messages
are worded accordingly ("не открихме").
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from nessebar_budget.analysis.rules._common import (
    TIER_OPACITY,
    TIER_SIGNAL,
    _now,
    make_flag,
)
from nessebar_budget.analysis.thresholds import Thresholds

_MONTHS_BG = (
    "януари", "февруари", "март", "април", "май", "юни",
    "юли", "август", "септември", "октомври", "ноември", "декември",
)

_MONTHLY_LAW_REF = (
    "чл. 133, ал. 1 и ал. 4 ЗПФ (ежемесечни отчети, публикувани на интернет страницата); "
    "чл. 15, ал. 1, т. 7 и чл. 15а, ал. 4 ЗДОИ (публикуване до 3 работни дни); "
    "чл. 173 ЗПФ (глоба)"
)
_ANNUAL_LAW_REF = (
    "чл. 133, ал. 1 и ал. 4 ЗПФ; чл. 140, ал. 5 и ал. 6 ЗПФ (годишният отчет се приема до "
    "30 септември и се публикува на интернет страницата); чл. 173 ЗПФ (глоба)"
)


def _month_range(first: str, last: str) -> list[str]:
    y, m = int(first[:4]), int(first[5:7])
    ly, lm = int(last[:4]), int(last[5:7])
    out = []
    while (y, m) <= (ly, lm):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _month_before_last(now: dt.datetime) -> str:
    y, m = now.year, now.month - 2
    if m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def _month_label(period: str) -> str:
    return f"{_MONTHS_BG[int(period[5:7]) - 1]} {period[:4]} г."


def missing_report_flags(
    reports: Iterable[tuple[str | None, str | None]],
    thresholds: Thresholds,
    *,
    now: dt.datetime | None = None,
) -> list[dict]:
    """`missing_monthly_report` + `missing_annual_report`.

    `reports` are (period "YYYY-MM", kind) pairs from `budget_reports`; only
    kinds in `thresholds.report_kinds` (B1/B3, the cash-execution report)
    count.

    - monthly (opacity, info): every month from `reports_first_period` to the
      month before last (relative to `now`) without such a file;
    - annual (signal, warning): a completed year Y with no 12/Y file once
      `annual_report_due_month/day` of Y+1 (31 March) has passed. That month
      then gets only the annual flag, not also a monthly one.

    Subject keys: `report:YYYY-MM` and `annual:YYYY`.
    """
    now = now or _now()
    have = {
        period
        for period, kind in reports
        if period and kind and kind.upper() in {k.upper() for k in thresholds.report_kinds}
    }
    last = _month_before_last(now)
    months = _month_range(thresholds.reports_first_period, last)

    out: list[dict] = []
    annual_years: set[int] = set()
    first_year = int(thresholds.reports_first_period[:4])
    for year in range(first_year, now.year):
        due = dt.date(
            year + 1, thresholds.annual_report_due_month, thresholds.annual_report_due_day
        )
        december = f"{year:04d}-12"
        if now.date() <= due or december in have:
            continue
        annual_years.add(year)
        out.append(
            make_flag(
                rule="missing_annual_report",
                tier=TIER_SIGNAL,
                severity="warning",
                message=(
                    f"Не открихме публикуван отчет за изпълнението на бюджета към 31 декември "
                    f"{year} г., въпреки че от края на годината са минали повече от 3 месеца."
                ),
                explanation=(
                    "Отчетът към края на годината показва как реално е изпълнен бюджетът. Той се "
                    "представя на Министерството на финансите и се публикува на сайта на общината "
                    "(чл. 133 ЗПФ), а годишният отчет се приема от общинския съвет до 30 септември "
                    "и също се публикува (чл. 140, ал. 5-6 ЗПФ). Липсата му затруднява контрола и "
                    "може да прикрива проблеми; възможно е и отчетът да е публикуван на място или "
                    "под име, което не разпознаваме."
                ),
                documents=[
                    f"отчет за касовото изпълнение на бюджета към 31.12.{year} г.",
                    f"годишен отчет за изпълнението на бюджета за {year} г.",
                    "решение на общинския съвет за приемане на годишния отчет",
                    f"отчет за сметките за средства от Европейския съюз за {year} г.",
                ],
                subject_type="report",
                subject_id=f"annual:{year}",
                details={
                    "year": year,
                    "expected_period": december,
                    "due_date": due.isoformat(),
                    "kinds_checked": list(thresholds.report_kinds),
                },
                law_ref=_ANNUAL_LAW_REF,
            )
        )

    for period in months:
        if period in have:
            continue
        if period.endswith("-12") and int(period[:4]) in annual_years:
            continue
        label = _month_label(period)
        out.append(
            make_flag(
                rule="missing_monthly_report",
                tier=TIER_OPACITY,
                severity="info",
                message=f"Не открихме публикуван месечен отчет за изпълнението на бюджета за {label}",
                explanation=(
                    "Общината е длъжна да изготвя месечни отчети за изпълнението на бюджета и да "
                    "ги публикува на сайта си (чл. 133, ал. 1 и 4 ЗПФ; чл. 15а, ал. 4 ЗДОИ — до 3 "
                    "работни дни). Без тях гражданите не могат да следят как се харчат парите през "
                    "годината. Възможно е отчетът да е публикуван на място, което нашият робот не "
                    "обхожда."
                ),
                documents=[f"месечен отчет за касовото изпълнение на бюджета (B1) за {label}"],
                subject_type="report",
                subject_id=f"report:{period}",
                details={"period": period, "kinds_checked": list(thresholds.report_kinds)},
                law_ref=_MONTHLY_LAW_REF,
            )
        )
    return out
