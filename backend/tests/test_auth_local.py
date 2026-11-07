"""Вход без пароля на время разработки (BIS_AUTO_LOGIN)."""

from fastapi.testclient import TestClient


def test_auto_login_for_everyone_even_with_stale_token(client, demo, monkeypatch):
    from bis.config import get_settings
    from bis.main import app

    assert client.get("/api/auth/me").status_code == 401
    monkeypatch.setattr(get_settings(), "auto_login", True)
    for c in (TestClient(app, client=("127.0.0.1", 1)), TestClient(app, client=("192.168.1.20", 1))):
        assert c.get("/api/auth/me").json()["user"]["role"] == "admin"
        r = c.get("/api/auth/me", headers={"Authorization": "Bearer stale-token"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "admin"
