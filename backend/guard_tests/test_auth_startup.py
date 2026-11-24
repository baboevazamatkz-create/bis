"""Exercise launch policy without opening a database or listening socket."""

import os
import secrets
import subprocess
import sys

import pytest

from bis import local
from bis.cli import main
from bis.config import get_settings


@pytest.fixture(autouse=True)
def reset_local_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BIS_AUTO_LOGIN", os.environ.get("BIS_AUTO_LOGIN", ""))
    monkeypatch.delenv("BIS_AUTO_LOGIN")
    monkeypatch.setenv("BIS_ENV", "prod")
    monkeypatch.setenv("BIS_SECRET_KEY", secrets.token_hex(32))
    for key in ("BIS_LOCAL_DIR", "BIS_DATABASE_URL", "BIS_STORAGE_DIR", "BIS_FFMPEG", "BIS_OPEN_BROWSER",
                "BIS_VECTOR_ENABLED"):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    monkeypatch.setenv("BIS_FFMPEG", "unused")
    monkeypatch.setenv("BIS_VECTOR_ENABLED", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("file_value", "process_value", "enabled"),
    [("true", None, True), ("false", None, False), (None, None, False),
     ("true", "false", False), ("false", "true", False)],
)
@pytest.mark.parametrize("host_args", [[], ["--host", "127.0.0.1"]])
def test_windows_pilot_launcher_reads_setting_and_binds_loopback(
    tmp_path, monkeypatch, file_value, process_value, enabled, host_args,
):
    import uvicorn

    if process_value is not None:
        monkeypatch.setenv("BIS_AUTO_LOGIN", process_value)
    if file_value is not None:
        # The installer writes a Windows .env and passes an explicit loopback address.
        key = "bis_auto_login" if process_value is not None else "BIS_AUTO_LOGIN"
        (tmp_path / ".env").write_bytes(f"{key}={file_value}\r\n".encode())
    monkeypatch.setattr(local, "start_database", lambda _: "postgresql+psycopg://unused")
    monkeypatch.setattr("bis.cli._alembic_upgrade", lambda: None)
    monkeypatch.setattr(local, "_repair", lambda: None)
    monkeypatch.setattr(local, "setup_log", lambda _: None)
    monkeypatch.setattr(local.threading.Thread, "start", lambda _: None)
    launched = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: launched.append((app, kwargs)))

    assert main(["local", "--dir", str(tmp_path), "--port", "8081", *host_args]) == 0
    [(app, kwargs)] = launched
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["proxy_headers"] is (not enabled)
    assert app.state.local_auto_login is enabled


def test_local_network_launch_rejects_autologin_before_database(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text("BIS_AUTO_LOGIN=true\n", encoding="utf-8")

    def unexpected_db(_):
        pytest.fail("invalid configuration reached the database")

    monkeypatch.setattr(local, "start_database", unexpected_db)
    assert main(["local", "--dir", str(tmp_path), "--host", "0.0.0.0"]) == 1
    assert "BIS_AUTO_LOGIN" in capsys.readouterr().err


def test_serve_rejects_autologin_before_uvicorn(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setenv("BIS_AUTO_LOGIN", "true")
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("Uvicorn was started"))
    assert main(["serve", "--host", "127.0.0.1"]) == 1
    assert "BIS_AUTO_LOGIN" in capsys.readouterr().err


def test_direct_asgi_import_rejects_autologin():
    run = subprocess.run([sys.executable, "-c", "from bis.main import app"], capture_output=True, text=True,
                         env={**os.environ, "BIS_AUTO_LOGIN": "true", "BIS_LOCAL_DIR": "."}, timeout=20)
    assert run.returncode != 0
    assert "BIS_AUTO_LOGIN" in run.stderr


@pytest.mark.parametrize("environment", ["dev", "prod", "test"])
@pytest.mark.parametrize("key", [None, "synthetic-signing-key-for-startup-tests", "too-short"])
def test_network_application_rejects_insecure_signing_key(monkeypatch, environment, key):
    from bis.app import create_app

    monkeypatch.setenv("BIS_ENV", environment)
    if key is None:
        monkeypatch.delenv("BIS_SECRET_KEY")
    else:
        monkeypatch.setenv("BIS_SECRET_KEY", key)
    with pytest.raises(RuntimeError, match="BIS_SECRET_KEY"):
        create_app()


def test_dev_pilot_can_keep_its_loopback_only_defaults(monkeypatch):
    from bis.app import create_app

    monkeypatch.setenv("BIS_ENV", "dev")
    monkeypatch.delenv("BIS_SECRET_KEY")
    monkeypatch.setenv("BIS_AUTO_LOGIN", "true")
    assert create_app(local_bind="127.0.0.1").state.local_auto_login


def test_network_local_rejects_unsafe_key_before_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BIS_ENV", "dev")
    monkeypatch.delenv("BIS_SECRET_KEY")
    monkeypatch.setattr(local, "setup_log", lambda _: None)

    def unexpected_db(_):
        pytest.fail("invalid configuration reached the database")

    monkeypatch.setattr(local, "start_database", unexpected_db)
    assert main(["local", "--dir", str(tmp_path), "--host", "0.0.0.0"]) == 1
    assert "BIS_SECRET_KEY" in capsys.readouterr().err


def test_serve_rejects_unsafe_key_before_uvicorn(monkeypatch, capsys):
    import uvicorn

    monkeypatch.setenv("BIS_ENV", "dev")
    monkeypatch.delenv("BIS_SECRET_KEY")
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: pytest.fail("Uvicorn was started"))
    assert main(["serve"]) == 1
    assert "BIS_SECRET_KEY" in capsys.readouterr().err


def test_direct_asgi_rejects_unsafe_key():
    run = subprocess.run(
        [sys.executable, "-c", "from bis.main import app"], capture_output=True, text=True,
        env={**os.environ, "BIS_ENV": "dev", "BIS_SECRET_KEY": "synthetic-signing-key-for-startup-tests"}, timeout=20,
    )
    assert run.returncode != 0
    assert "BIS_SECRET_KEY" in run.stderr


def test_pilot_frame_protection_allows_disconnect_before_headers(monkeypatch):
    import asyncio

    from bis.app import create_app

    monkeypatch.setenv("BIS_AUTO_LOGIN", "true")
    app = create_app(local_bind="127.0.0.1")

    async def finish_on_disconnect(scope, receive, send):
        assert (await receive())["type"] == "http.disconnect"

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        pytest.fail("A disconnected client must not receive an HTTP response")

    app.mount("/disconnect", finish_on_disconnect)
    scope = {"type": "http", "method": "GET", "path": "/disconnect/", "headers": [], "query_string": b""}
    asyncio.run(app(scope, receive, send))
