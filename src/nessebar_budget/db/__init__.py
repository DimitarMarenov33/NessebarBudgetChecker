"""Database package.

Importing `budget_models` here (for its side effect of registering tables on
the shared `Base.metadata`) ensures `db.session.init_db()` creates the
budget-line-item tables too, even though nothing else in the app imports
that module directly.
"""

from __future__ import annotations

from nessebar_budget.db import budget_models  # noqa: F401
