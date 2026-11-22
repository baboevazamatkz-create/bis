from __future__ import annotations

import os
import secrets
import tempfile

import pytest
from sqlalchemy import text

from bis.testing_database import UnsafeTestDatabase, install_guard, test_database_config

# Install the driver boundary before any application import or test collection.
try:
    test_db = test_database_config(os.environ)
except UnsafeTestDatabase as exc:
    raise pytest.UsageError(str(exc)) from None
remove_test_guard = install_guard(test_db)
os.environ["BIS_DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["BIS_ENV"] = "test"
os.environ["BIS_SECRET_KEY"] = secrets.token_hex(32)
os.environ["BIS_STORAGE_DIR"] = tempfile.mkdtemp(prefix="bis-test-")
os.environ.pop("BIS_LLM_PROVIDERS_JSON", None)
os.environ.pop("BIS_LLM_CONFIG_FILE", None)


def pytest_unconfigure():
    remove_test_guard()

TABLES = [
    "project_readings",
    "brain_investigations", "fact_decisions", "fact_conflicts", "project_facts", "fact_snapshots",
    "library_versions", "library_files", "library_connections",
    "knowledge_chunks", "knowledge_sources", "contract_obligations", "appointments", "chat_messages", "chat_sessions",
    "documents", "contracts", "persons", "construction_objects", "document_templates", "counters", "org_memory",
    "audit_log", "users", "counterparties", "organizations",
]


@pytest.fixture(scope="session")
def migrated():
    from bis.db import get_engine

    engine = get_engine()
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
