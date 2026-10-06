"""Smoke test: package imports, in-memory DB init, and a rule produces a flag."""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import nessebar_budget
from nessebar_budget.analysis.rules import MissingValueRule
from nessebar_budget.db.models import Base


def test_package_imports() -> None:
    assert nessebar_budget.__version__


def test_init_db_in_memory() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        assert session is not None


def test_missing_value_rule_flags_zero_value() -> None:
    fake_record = {"id": 1, "source_id": "TEST-1", "contractor_name": "X", "contract_value_bgn": 0}
    hits = MissingValueRule().check(fake_record)
    assert len(hits) == 1
    assert hits[0]["rule"] == "missing_value"
