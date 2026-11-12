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


def load_env(path: Path, *, prefer_file: bool = False) -> None:
    """KEY=VALUE из .env в окружение процесса (ключи Яндекса читаются из окружения)."""
    if not path.exists():
        return
    from dotenv import dotenv_values

    for key, value in dotenv_values(path, encoding='utf-8-sig').items():
        if value is None:
            continue
        if prefer_file and key != 'BIS_WEB_DIR':
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)


def _pg_bin(name: str) -> str:
    import pgserver

    exe = name + (".exe" if os.name == "nt" else "")
    return str(Path(pgserver.__file__).parent / "pginstall" / "bin" / exe)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _running_port(pg_dir: Path) -> int | None:
    """Порт уже запущенной базы (postmaster.pid, 4-я строка), если процесс жив."""
    import subprocess

    pid_file = pg_dir / "postmaster.pid"
    if not pid_file.exists():
        return None
    r = subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "status"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if r.returncode != 0:
        return None
    lines = pid_file.read_text(errors="ignore").splitlines()
    return int(lines[3]) if len(lines) > 3 and lines[3].strip().isdigit() else None


def start_database(data_dir: Path) -> str:
    """Встроенный PostgreSQL 16 (бинарники из пакета pgserver). Запуск без каналов вывода и с ожиданием до 3 минут:
    на Windows антивирус и восстановление после сбоя замедляют старт."""
    import atexit
    import subprocess
    import tempfile

    pg_dir = data_dir / "pg"
    log_file = data_dir / "postgres.log"  # вне папки данных: её файлы блокирует антивирус
    data_dir.mkdir(parents=True, exist_ok=True)
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if not (pg_dir / "PG_VERSION").exists():
        pg_dir.mkdir(parents=True, exist_ok=True)
        print("  Создаю базу данных…", flush=True)
        subprocess.run([_pg_bin("initdb"), "-D", str(pg_dir), "-U", "postgres", "--auth=trust", "--encoding=UTF8"],
                       check=True, **quiet)
    port = _running_port(pg_dir)
    if port is None:
        port = _free_port()
        opts = f'-h 127.0.0.1 -p {port}' + ("" if os.name == "nt" else f" -k {tempfile.gettempdir()}")
        print("  Запускаю базу данных (до 3 минут)…", flush=True)
        r = subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "-w", "-t", "180", "-o", opts, "-l", str(log_file),
                            "start"], timeout=200, **quiet)
        if r.returncode != 0:
            tail = log_file.read_text(errors="ignore")[-3000:] if log_file.exists() else ""
            raise RuntimeError(f"База данных не запустилась. Журнал ({log_file}):\n{tail}")
        atexit.register(lambda: subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "stop", "-m", "fast", "-w"], **quiet))
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    if os.environ.get("BIS_VECTOR_ENABLED", "false").lower() in ("1", "true", "yes"):
        import psycopg

        with psycopg.connect(url.replace("+psycopg", ""), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return url


def prepare(workdir: Path) -> Path:
    """Окружение, база и миграции. Возвращает папку данных."""
    os.chdir(workdir)
    load_env(workdir / ".env", prefer_file=True)
    os.environ['BIS_LOCAL_DIR'] = str(workdir)
    # pgvector из встроенной сборки падает на части процессоров — по умолчанию поиск полнотекстовый
    os.environ.setdefault("BIS_VECTOR_ENABLED", "false")
    # без формы входа на своём компьютере (BIS_AUTO_LOGIN=false в .env — вернуть вход)
    os.environ["BIS_AUTO_LOGIN"] = "true"  # local pilot deliberately has no login screen
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
    _repair()
    return data


def _repair() -> None:
    """Поправить загруженный раньше архив (объект, даты) — быстро и один раз для каждого документа."""
    try:
        from bis.db import SessionLocal
        from bis.services.archive import repair_archive

        with SessionLocal() as db:
            fixed = repair_archive(db)
            db.commit()
        if any(fixed.values()):
            print(f"  Архив: привязано к объекту {fixed['objects']}, даты уточнены у {fixed['dates']}, "
                  f"исходящих писем найдено {fixed['letters']}", flush=True)
    except Exception as e:  # noqa: BLE001 — запуску это не мешает
        print(f"  Архив: не удалось поправить ({e})", flush=True)


def setup_log(workdir: Path) -> Path:
    """Журнал предупреждений и ошибок: logs/bis.log (до 5 файлов по 2 МБ) — его читает `bis doctor`."""
    from logging.handlers import RotatingFileHandler

    path = workdir / "logs" / "bis.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "uvicorn.access", "multipart", "ezdxf", "fontTools"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return path


def run(workdir: Path, host: str, port: int) -> None:
    import uvicorn

    setup_log(workdir)
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
