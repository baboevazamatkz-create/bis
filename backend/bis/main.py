"""Точка входа API: uvicorn bis.main:app"""

from __future__ import annotations

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
    executive,
    facts,
    knowledge,
    libraries,
    projects,
    registry,
    telegram,
    voice,
    works,
    workspace,
)
from bis.config import get_settings
from bis.middleware import RequestLogMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.env == "prod" and (settings.secret_key == "change-me-in-production" or len(settings.secret_key) < 32):
        raise RuntimeError("Задайте BIS_SECRET_KEY длиной не менее 32 символов")
    app = FastAPI(title="BIS API", version=__version__,
                  description="ИИ-платформа автоматизации строительных компаний")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(GZipMiddleware, minimum_size=2048)  # предпросмотры схем (SVG) и большие списки — в 5–8 раз меньше

    app.add_middleware(RequestLogMiddleware)

    api = APIRouter(prefix="/api")
    api.include_router(admin.router)
    api.include_router(workspace.router)
    api.include_router(brain.router)
    api.include_router(facts.router)
    api.include_router(projects.router)
    api.include_router(auth.router)
    api.include_router(chat.router)
    api.include_router(knowledge.router)
    api.include_router(libraries.router)
    api.include_router(documents.router)
    api.include_router(executive.router)
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

    @app.exception_handler(FileNotFoundError)
    async def missing_file(_: Request, exc: FileNotFoundError):
        return JSONResponse(status_code=404, content={"detail": "Исходный файл недоступен. Проверьте папку хранения и подключение диска."})

    if settings.web_dir and Path(settings.web_dir).is_dir():  # локальный запуск: веб-интерфейс отдаёт сам API
        web = Path(settings.web_dir).resolve()
        app.mount("/assets", StaticFiles(directory=web / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str):
            if path == 'api' or path.startswith('api/'):
                return JSONResponse(status_code=404, content={'detail': 'API-метод не найден. Проверьте версию приложения.'})
            f = web / path
            return FileResponse(f if path and f.is_file() and web in f.resolve().parents else web / "index.html", headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
