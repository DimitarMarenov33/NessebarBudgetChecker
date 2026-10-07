"""Tiny, idempotent schema patch-up for the committed `data/nessebar.db`.

`Base.metadata.create_all()` (SQLAlchemy's usual migration-free story) only
creates tables that don't exist yet -- it never alters an *existing* table's
columns. `data/nessebar.db` is committed to the repo with an old, empty-ish
`flags` table (no `subject_type`/`subject_id`/`subject_key`/`details_json`/
`law_ref`/`first_seen_at`/`last_seen_at`/`resolved_at`, later no
`tier`/`explanation`/`documents_json`, and later still no `sources_json`
columns), predating those fields being
added to `db.models.Flag`. This module adds them with
plain `ALTER TABLE ... ADD COLUMN` statements, each guarded by a check
against `PRAGMA table_info`, so running it repeatedly (every `init_db()`
call) is a no-op once the columns exist.

SQLite's `ALTER TABLE` can't add a constraint to an existing table, so the
`(rule, subject_key)` uniqueness that `Flag.__table_args__` declares is
instead enforced with a separate `CREATE UNIQUE INDEX IF NOT EXISTS` --
equivalent for SQLite's purposes, and doesn't require rebuilding the table.
"""

from __future__ import annotations

import logging

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

#: (column name, SQL type) for every column `db.models.Flag` added beyond the
#: original committed `flags` table. Order doesn't matter for SQLite.
_NEW_FLAG_COLUMNS: tuple[tuple[str, str], ...] = (
    ("subject_type", "VARCHAR(32)"),
    ("subject_id", "VARCHAR(256)"),
    ("subject_key", "VARCHAR(256)"),
    ("details_json", "JSON"),
    ("law_ref", "TEXT"),
    ("first_seen_at", "DATETIME"),
    ("last_seen_at", "DATETIME"),
    ("resolved_at", "DATETIME"),
    # The shared tier/explanation/documents contract (2026-10-07).
    ("tier", "VARCHAR(16)"),
    ("explanation", "TEXT"),
    ("documents_json", "JSON"),
    # Provenance ("Източници") of every flag (2026-10-07).
    ("sources_json", "JSON"),
)

_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_flags_rule_subject_key "
    "ON flags (rule, subject_key)"
)


def migrate_flags_table(engine: Engine) -> None:
    """Add any missing `flags` columns (and the rule/subject_key unique
    index) to an already-existing SQLite database. Safe to call on every
    `init_db()`: does nothing once the schema is up to date, and does
    nothing at all for a non-SQLite engine or a freshly-created `flags`
    table (which already has every column via `Base.metadata.create_all`).
    """
    if engine.dialect.name != "sqlite":
        return

    with engine.connect() as conn:
        existing_tables = {
            row[0] for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        if "flags" not in existing_tables:
            return  # create_all() will create it fresh, with every column already.

        existing_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(flags)"))
        }
        for column, sql_type in _NEW_FLAG_COLUMNS:
            if column in existing_columns:
                continue
            logger.info("migrate_flags_table: adding flags.%s (%s)", column, sql_type)
            conn.execute(text(f"ALTER TABLE flags ADD COLUMN {column} {sql_type}"))

        conn.execute(text(_UNIQUE_INDEX_SQL))
        conn.commit()
