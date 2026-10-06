"""Engine/session factory for the local SQLite (or other SQLAlchemy-supported) database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from nessebar_budget.config import get_settings
from nessebar_budget.db.migrate import migrate_flags_table
from nessebar_budget.db.models import Base

_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine, created lazily from settings."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        if settings.database_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}
        _engine = create_engine(settings.database_url, connect_args=connect_args)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return a process-wide sessionmaker bound to the shared engine."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _SessionFactory


@contextmanager
def get_session() -> Iterator[Session]:
    """Context manager yielding a SQLAlchemy session."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """Create all tables that don't exist yet, then patch up any that
    pre-date later model changes (see `db.migrate.migrate_flags_table`)."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    migrate_flags_table(engine)
