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
_windows_shutdown_callback = None


def load_env(path: Path, *, prefer_file: bool = False) -> None:
    """KEY=VALUE из .env в окружение процесса (ключи Яндекса читаются из окружения)."""
    if not path.exists():
        return
    from dotenv import dotenv_values

    auto_login_values = []
    for key, value in dotenv_values(path, encoding='utf-8-sig').items():
        if value is None:
            continue
        if prefer_file and key.upper() == "BIS_AUTO_LOGIN":
            auto_login_values.append(value)
        elif prefer_file and key != 'BIS_WEB_DIR':
            os.environ[key] = value
        else:
            os.environ.setdefault(key, value)
    if auto_login_values:
        from pydantic import TypeAdapter

        aliases = [key for key in os.environ if key.upper() == "BIS_AUTO_LOGIN"]
        boolean = TypeAdapter(bool)
        values = auto_login_values + [os.environ[key] for key in aliases]
        enabled_values = [boolean.validate_python(value) for value in values]
        for key in aliases:
            del os.environ[key]
        os.environ["BIS_AUTO_LOGIN"] = "true" if all(enabled_values) else "false"


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
    if os.name == "nt":
        quiet["creationflags"] = subprocess.CREATE_NO_WINDOW
    if not (pg_dir / "PG_VERSION").exists():
        pg_dir.mkdir(parents=True, exist_ok=True)
        print("  Создаю базу данных…", flush=True)
        subprocess.run([_pg_bin("initdb"), "-D", str(pg_dir), "-U", "postgres", "--auth=trust", "--encoding=UTF8"],
                       check=True, **quiet)
    port = _running_port(pg_dir)
    owns_database = port is None
    if port is None:
        port = _free_port()
        opts = f'-h 127.0.0.1 -p {port}' + ("" if os.name == "nt" else f" -k {tempfile.gettempdir()}")
        print("  Запускаю базу данных (до 3 минут)…", flush=True)
        r = subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "-w", "-t", "180", "-o", opts, "-l", str(log_file),
                            "start"], timeout=200, **quiet)
        if r.returncode != 0:
            tail = log_file.read_text(errors="ignore")[-3000:] if log_file.exists() else ""
            raise RuntimeError(f"База данных не запустилась. Журнал ({log_file}):\n{tail}")
    def stop_database() -> None:
        status = subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "status"], **quiet)
        if status.returncode == 0:
            subprocess.run([_pg_bin("pg_ctl"), "-D", str(pg_dir), "stop", "-m", "fast", "-w"], **quiet)

    if owns_database:
        atexit.register(stop_database)
    if owns_database and os.name == "nt":
        import ctypes
        from ctypes import wintypes

        console_handler = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def on_console_close(event):
            if event == 2:  # CTRL_CLOSE_EVENT: stop PostgreSQL before Windows tears down the console.
                try:
                    stop_database()
                except Exception:  # noqa: BLE001 — allow the rest of the process tree to exit
                    log.exception("Не удалось штатно остановить локальную базу при закрытии BIS")
            return False

        global _windows_shutdown_callback
        _windows_shutdown_callback = console_handler(on_console_close)
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(_windows_shutdown_callback, True):
            raise ctypes.WinError()
    url = f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    if os.environ.get("BIS_VECTOR_ENABLED", "false").lower() in ("1", "true", "yes"):
        import psycopg

        with psycopg.connect(url.replace("+psycopg", ""), autocommit=True) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return url


def prepare(workdir: Path, *, server_host: str | None = None) -> Path:
    """Окружение, база и миграции. Возвращает папку данных."""
    from bis.config import get_settings, validate_auto_login

    os.chdir(workdir)
    load_env(workdir / ".env", prefer_file=True)
    if server_host is not None:
        get_settings.cache_clear()
        validate_auto_login(server_host)
    os.environ['BIS_LOCAL_DIR'] = str(workdir)
    # pgvector из встроенной сборки падает на части процессоров — по умолчанию поиск полнотекстовый
    os.environ.setdefault("BIS_VECTOR_ENABLED", "false")
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
            print(f"  Архив: даты уточнены у {fixed['dates']}, "
                  f"исходящих писем найдено {fixed['letters']}, договоров уточнено {fixed['contracts']}", flush=True)
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
    prepare(workdir, server_host=host)
    from bis.app import create_app
    from bis.config import get_settings
    from bis.db import SessionLocal
    from bis.jobs import load_all_jobs, worker

    load_all_jobs()
    threading.Thread(target=worker, args=(SessionLocal,), daemon=True, name="bis-worker").start()
    if get_settings().telegram_bot_token:
        from bis.connectors.telegram import poll

        threading.Thread(target=poll, daemon=True, name="bis-telegram").start()

    print(f"\n  BIS работает: http://localhost:{port}   (остановить — закройте это окно или Ctrl+C)\n", flush=True)
    if os.environ.get("BIS_OPEN_BROWSER", "false").lower() in ("1", "true", "yes"):
        import socket
        import time
        import webbrowser

        def open_when_ready():
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((host, port), timeout=1):
                        webbrowser.open(f"http://localhost:{port}")
                        return
                except OSError:
                    time.sleep(0.25)

        threading.Thread(target=open_when_ready, daemon=True, name="bis-open-browser").start()
    app = create_app(local_bind=host)
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=not app.state.local_auto_login)
