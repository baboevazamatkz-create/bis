"""Локальный запуск без Docker (Windows, macOS, Linux): `bis local`.

Одним процессом: встроенный PostgreSQL 16 с pgvector (пакет pgserver), миграции, API, веб-интерфейс,
фоновые задачи и Telegram-бот (если задан токен). Данные — в папке data рядом с настройками.
Настройки — файл .env в рабочей папке (те же переменные, что для сервера).
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

log = logging.getLogger("bis.local")


def load_env(path: Path) -> None:
    """KEY=VALUE из .env в окружение процесса (ключи Яндекса читаются из окружения)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def start_database(data_dir: Path) -> str:
    import pgserver

    pg_dir = data_dir / "pg"
    pg_dir.mkdir(parents=True, exist_ok=True)
    server = pgserver.get_server(str(pg_dir), cleanup_mode="stop")
    server.psql("CREATE EXTENSION IF NOT EXISTS vector;")
    uri = server.get_uri()
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def prepare(workdir: Path) -> Path:
    """Окружение, база и миграции. Возвращает папку данных."""
    os.chdir(workdir)
    load_env(workdir / ".env")
    data = Path(os.environ.get("BIS_DATA_DIR") or (workdir / "data"))
    os.environ["BIS_DATABASE_URL"] = start_database(data)
    os.environ.setdefault("BIS_STORAGE_DIR", str(data / "files"))
    web = workdir / "web"
    if web.exists():
        os.environ.setdefault("BIS_WEB_DIR", str(web))
    if not os.environ.get("BIS_FFMPEG"):
        try:
            import imageio_ffmpeg

            os.environ["BIS_FFMPEG"] = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:  # noqa: BLE001 — без ffmpeg не будет только распознавания голосовых
            pass
    from bis.config import get_settings

    get_settings.cache_clear()
    from bis.cli import _alembic_upgrade

    _alembic_upgrade()
    return data


def run(workdir: Path, host: str, port: int) -> None:
    import uvicorn

    prepare(workdir)
    from bis.config import get_settings
    from bis.db import SessionLocal
    from bis.jobs import load_all_jobs, worker

    load_all_jobs()
    threading.Thread(target=worker, args=(SessionLocal,), daemon=True, name="bis-worker").start()
    if get_settings().telegram_bot_token:
        from bis.connectors.telegram import poll

        threading.Thread(target=poll, daemon=True, name="bis-telegram").start()
    from bis.main import create_app

    print(f"\n  BIS работает: http://localhost:{port}   (остановить — закройте это окно или Ctrl+C)\n", flush=True)
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
