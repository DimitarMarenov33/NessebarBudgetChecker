"""FastAPI web app for Nessebar Budget Monitor.

Runs purely locally (e.g. via `uvicorn nessebar_budget.web.app:app`). No
external services, no telemetry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from nessebar_budget.db.models import Flag
from nessebar_budget.db.session import get_session

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Nessebar Budget Monitor")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/flags")
def api_flags() -> list[dict[str, Any]]:
    with get_session() as session:
        flags = session.scalars(select(Flag).order_by(Flag.created_at.desc())).all()
        return [
            {
                "id": flag.id,
                "procurement_id": flag.procurement_id,
                "rule": flag.rule,
                "severity": flag.severity,
                "message": flag.message,
                "created_at": flag.created_at.isoformat() if flag.created_at else None,
                "notified_at": flag.notified_at.isoformat() if flag.notified_at else None,
            }
            for flag in flags
        ]
