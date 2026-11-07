"""Точка входа API: uvicorn bis.main:app"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from bis import __version__
from bis.api import admin, auth, chat, control, documents, knowledge, registry, works
from bis.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    if settings.env == "prod" and (settings.secret_key == "change-me-in-production" or len(settings.secret_key) < 32):
        raise RuntimeError("Задайте BIS_SECRET_KEY длиной не менее 32 символов")
    app = FastAPI(title="BIS API", version=__version__,
                  description="ИИ-платформа автоматизации строительных компаний")
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    api = APIRouter(prefix="/api")
    api.include_router(admin.router)
    api.include_router(auth.router)
    api.include_router(chat.router)
    api.include_router(knowledge.router)
    api.include_router(documents.router)
    api.include_router(documents.templates_router)
    api.include_router(control.router)
    api.include_router(works.router)
    for r in registry.routers:
        api.include_router(r)
    app.include_router(api)

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(PermissionError)
    async def forbidden(_: Request, exc: PermissionError):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return app


app = create_app()
