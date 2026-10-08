"""Unit tests for `nessebar_budget.pipeline.run_weekly`.

All ten steps are monkeypatched to fakes -- no network access, no real
database -- so these tests only exercise `run_weekly`'s own orchestration:
step ordering, failure isolation (a failing step doesn't stop later steps),
the returned summary dict on success, and the non-zero exit on failure.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

from nessebar_budget import pipeline
from nessebar_budget.config import Settings

ALL_STEP_NAMES = [
    "init_db",
    "scrape_eop",
    "scrape_sigma",
    "scrape_registry",
    "scrape_declarations",
    "scrape_nesebar_site",
    "parse_budget",
    "analyze",
    "notify_pending",
    "build_site",
]


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


def _patch_all_steps(monkeypatch, calls: list[str], failing: set[str] = frozenset()) -> None:
    """Monkeypatch every `_step_*` function to a fake that records its name
    (in call order) and either returns a small info dict or raises, for
    names in `failing`.
    """
    for name in ALL_STEP_NAMES:

        def make_fake(step_name: str):
            def _fake(settings: Settings) -> dict:
                calls.append(step_name)
                if step_name in failing:
                    raise RuntimeError(f"{step_name} exploded")
                return {"name": step_name}

            return _fake

        monkeypatch.setattr(pipeline, f"_step_{name}", make_fake(name))


def test_run_weekly_runs_all_steps_in_order_and_returns_summary(monkeypatch, settings) -> None:
    calls: list[str] = []
    _patch_all_steps(monkeypatch, calls)

    summary = pipeline.run_weekly(settings)

    assert calls == ALL_STEP_NAMES
    assert summary["ok"] is True
    assert [s["name"] for s in summary["steps"]] == ALL_STEP_NAMES
    assert all(s["ok"] for s in summary["steps"])
    assert all("elapsed_seconds" in s for s in summary["steps"])
    assert "started_at" in summary and "finished_at" in summary


def test_run_weekly_isolates_a_failing_step_and_still_runs_later_steps(
    monkeypatch, settings
) -> None:
    calls: list[str] = []
    _patch_all_steps(monkeypatch, calls, failing={"scrape_sigma"})

    with pytest.raises(SystemExit) as exc_info:
        pipeline.run_weekly(settings)

    # A SystemExit with a non-zero code is the whole point of the "exit
    # non-zero at the end if any step failed" requirement.
    assert exc_info.value.code != 0

    # Despite scrape_sigma raising, every later step still ran, in order.
    assert calls == ALL_STEP_NAMES


def test_run_weekly_records_failure_details_before_exiting(monkeypatch, settings) -> None:
    calls: list[str] = []
    captured: dict = {}

    # Patch logger.info to capture the summary that's logged right before
    # run_weekly raises SystemExit (the return value itself is unreachable
    # from the caller once it raises).
    original_info = pipeline.logger.info

    def spy_info(msg, *args, **kwargs):
        if args and isinstance(args[-1], dict):
            captured["summary"] = args[-1]
        return original_info(msg, *args, **kwargs)

    monkeypatch.setattr(pipeline.logger, "info", spy_info)
    _patch_all_steps(monkeypatch, calls, failing={"analyze"})

    with pytest.raises(SystemExit):
        pipeline.run_weekly(settings)

    summary = captured["summary"]
    assert summary["ok"] is False
    by_name = {s["name"]: s for s in summary["steps"]}
    assert by_name["analyze"]["ok"] is False
    assert "RuntimeError" in by_name["analyze"]["error"]
    # Steps after the failing one still ran and succeeded.
    assert by_name["notify_pending"]["ok"] is True
    assert by_name["build_site"]["ok"] is True


def test_run_weekly_defaults_settings_when_none_given(monkeypatch) -> None:
    calls: list[str] = []
    _patch_all_steps(monkeypatch, calls)

    summary = pipeline.run_weekly()

    assert summary["ok"] is True
    assert calls == ALL_STEP_NAMES


def test_step_build_site_import_error_is_a_soft_skip_not_a_failure(monkeypatch, settings) -> None:
    # web.build is owned by a concurrent workstream and may or may not exist
    # yet in any given checkout; force the "not available" case explicitly
    # (sys.modules[name] = None makes the import system raise ImportError,
    # regardless of whether the real module is actually importable here) so
    # this test is deterministic either way.
    monkeypatch.setitem(sys.modules, "nessebar_budget.web.build", None)

    info = pipeline._step_build_site(settings)

    assert info["skipped"] is True
    assert "reason" in info


class TestPreviousPeriod:
    def test_mid_month(self) -> None:
        assert pipeline._previous_period(dt.date(2026, 10, 6)) == "2026-09"

    def test_january_rolls_back_to_prior_year_december(self) -> None:
        assert pipeline._previous_period(dt.date(2026, 1, 15)) == "2025-12"

    def test_first_of_month(self) -> None:
        assert pipeline._previous_period(dt.date(2026, 3, 1)) == "2026-02"
