"""FastAPI app factory."""
from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from web.db import _ensure_engine
from web.routes import api, pages, ws


BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app() -> FastAPI:
    load_dotenv()
    _ensure_engine()

    app = FastAPI(title="PolitiCheck", docs_url="/docs", redoc_url=None)

    secret = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
    app.add_middleware(SessionMiddleware, secret_key=secret, same_site="lax")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["seconds_to_mmss"] = _seconds_to_mmss
    templates.env.filters["fmt_date"] = _fmt_upload_date
    app.state.templates = templates

    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")
    app.include_router(ws.router)

    return app


def _seconds_to_mmss(s) -> str:
    try:
        total = int(s or 0)
    except (TypeError, ValueError):
        return "--:--"
    m, sec = divmod(total, 60)
    return f"{m:02d}:{sec:02d}"


_MONTHS_ES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _fmt_upload_date(raw) -> str:
    if not raw:
        return ""
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(str(raw), "%Y%m%d")
        return f"{d.day} de {_MONTHS_ES[d.month - 1]} de {d.year}"
    except Exception:
        return str(raw)


app = create_app()
