from __future__ import annotations

import os
import tempfile

os.environ.setdefault("BIS_DATABASE_URL", os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://bis:bis@localhost:5432/bis_test"))
os.environ["BIS_ENV"] = "test"
os.environ["BIS_STORAGE_DIR"] = tempfile.mkdtemp(prefix="bis-test-")
os.environ.pop("BIS_LLM_PROVIDERS_JSON", None)
os.environ.pop("BIS_LLM_CONFIG_FILE", None)

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402

from bis.config import get_settings  # noqa: E402

TABLES = [
    "knowledge_chunks", "knowledge_sources", "contract_obligations", "appointments", "chat_messages", "chat_sessions",
    "documents", "contracts", "persons", "construction_objects", "document_templates", "counters", "org_memory",
    "audit_log", "users", "counterparties", "organizations",
]


def _db_available() -> bool:
    try:
        with create_engine(get_settings().database_url).connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False


DB_AVAILABLE = _db_available()


@pytest.fixture(scope="session")
def migrated():
    if not DB_AVAILABLE:
        pytest.skip("PostgreSQL недоступна (TEST_DATABASE_URL)")
    engine = create_engine(get_settings().database_url)
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    from bis.cli import _alembic_upgrade

    _alembic_upgrade()
    yield
    engine.dispose()


@pytest.fixture
def db(migrated):
    from bis.db import SessionLocal, get_engine

    session = SessionLocal()
    yield session
    session.close()
    with get_engine().begin() as c:
        c.execute(text("TRUNCATE " + ", ".join(TABLES) + " CASCADE"))


@pytest.fixture(autouse=True)
def reset_singletons():
    from bis.llm.gateway import set_gateway
    from bis.rag.embeddings import set_embedder

    yield
    set_gateway(None)
    set_embedder(None)


@pytest.fixture
def demo(db):
    from bis.demo import seed_demo
    from bis.rag.embeddings import get_embedder
    from bis.services.storage import get_storage

    org = seed_demo(db, get_storage(), get_embedder())
    db.commit()
    return org


@pytest.fixture
def client(migrated):
    from fastapi.testclient import TestClient

    from bis.main import app

    return TestClient(app)


def login(client, email: str = "admin@demo.bis", password: str = "demo12345") -> dict[str, str]:
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture
def auth(client, demo):
    return login(client)
