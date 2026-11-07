"""Вход без пароля при локальном запуске — только с этого же компьютера."""

from fastapi.testclient import TestClient


def test_auto_login_only_from_loopback(client, demo, monkeypatch):
    from bis.config import get_settings
    from bis.main import app

    assert client.get("/api/auth/me").status_code == 401
    monkeypatch.setattr(get_settings(), "auto_login", True)
    r = TestClient(app, client=("127.0.0.1", 50000)).get("/api/auth/me")
    assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
    assert TestClient(app, client=("192.168.1.20", 50000)).get("/api/auth/me").status_code == 401
