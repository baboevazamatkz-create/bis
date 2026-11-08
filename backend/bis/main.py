"""Точка входа API: uvicorn bis.main:app"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from bis import __version__
from bis.api import (
    admin,
    ai,
    auth,
    brain,
    chat,
    control,
    documents,
    knowledge,
    registry,
    telegram,
    voice,
    works,
)
from bis.config import get_settings

log = logging.getLogger("bis.http")


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.env == "prod" and (settings.secret_key == "change-me-in-production" or len(settings.secret_key) < 32):
        raise RuntimeError("Задайте BIS_SECRET_KEY длиной не менее 32 символов")
    app = FastAPI(title="BIS API", version=__version__,
                  description="ИИ-платформа автоматизации строительных компаний")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=2048)  # предпросмотры схем (SVG) и большие списки — в 5–8 раз меньше

    @app.middleware("http")
    async def slow_log(request: Request, call_next):
        """Медленные и упавшие запросы — в журнал (видно в `bis doctor`)."""
        t = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("Ошибка запроса %s %s", request.method, request.url.path)
            raise
        took = time.perf_counter() - t
        if response.status_code >= 500:
            log.error("%s %s → %s за %.1f с", request.method, request.url.path, response.status_code, took)
        elif took > 8:
            log.warning("медленно: %s %s — %.1f с", request.method, request.url.path, took)
        return response

    api = APIRouter(prefix="/api")
    api.include_router(admin.router)
    api.include_router(brain.router)
    api.include_router(auth.router)
    api.include_router(chat.router)
    api.include_router(knowledge.router)
    api.include_router(documents.router)
    api.include_router(documents.templates_router)
    api.include_router(control.router)
    api.include_router(works.router)
    api.include_router(voice.router)
    api.include_router(ai.router)
    api.include_router(telegram.router)
    for r in registry.routers:
        api.include_router(r)
    app.include_router(api)

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def forbidden(_: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    if settings.web_dir and Path(settings.web_dir).is_dir():  # локальный запуск: веб-интерфейс отдаёт сам API
        web = Path(settings.web_dir)
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            f = web / path
            return FileResponse(f if path and f.is_file() and web in f.resolve().parents else web / "index.html")

    return app


app = create_app()
