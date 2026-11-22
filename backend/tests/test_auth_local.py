"""The personal pilot's password-free access is confined to its local listener."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def pilot(demo, monkeypatch):
    from bis.config import get_settings
    from bis.main import create_app

    monkeypatch.setenv("BIS_AUTO_LOGIN", "true")
    get_settings.cache_clear()
    try:
        yield create_app(local_bind="127.0.0.1")
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_pilot_accepts_local_browser_even_with_stale_token(pilot, host):
    client = TestClient(pilot, base_url="http://127.0.0.1:8081", client=("127.0.0.1", 1234))
    # Uvicorn exposes the socket address independently of the HTTP Host.
    response = client.get("/api/auth/me", headers={"Authorization": "Bearer stale", "Host": f"{host}:8081"})
    assert response.status_code == 200
    assert response.json()["user"]["role"] == "admin"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize(
    ("peer", "host", "headers"),
    [
        ("192.0.2.17", "127.0.0.1", {}),
        ("127.0.0.1", "127.0.0.1", {"Host": "example.test"}),
        ("127.0.0.1", "127.0.0.1", {"Origin": "http://example.test"}),
        ("127.0.0.1", "127.0.0.1", {"Forwarded": "for=192.0.2.17"}),
        ("127.0.0.1", "127.0.0.1", {"X-Forwarded-For": "127.0.0.1"}),
        ("127.0.0.1", "192.0.2.17", {"Host": "127.0.0.1:8081"}),
    ],
)
def test_pilot_does_not_grant_admin_outside_local_request(pilot, peer, host, headers):
    client = TestClient(pilot, base_url=f"http://{host}:8081", client=(peer, 1234))
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_network_api_requires_valid_token(client, auth, monkeypatch):
    from bis.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_login", True)
    assert client.get("/api/auth/me", headers=auth).status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer invalid"}).status_code == 401

    key = get_settings().secret_key
    payload = jwt.decode(auth["Authorization"].split()[1], key, algorithms=["HS256"])
    payload["exp"] = datetime.now(UTC) - timedelta(minutes=1)
    expired = jwt.encode(payload, key, algorithm="HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401
