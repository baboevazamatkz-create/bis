"""Exercise the guard and PostgreSQL grants against the disposable CI service."""

from dataclasses import replace
from os import environ
from uuid import uuid4

import pytest
from sqlalchemy.exc import ProgrammingError

from bis.config import get_settings
from bis.db import get_engine
from bis.testing_database import UnsafeTestDatabase
from bis.testing_database import test_database_config as parse_test_database_config


def test_real_postgres_rejects_a_different_identity(migrated):
    wrong_identity = replace(parse_test_database_config(environ), identity=str(uuid4()))
    dialect = get_engine().dialect
    args, params = dialect.create_connect_args(wrong_identity.url)
    with pytest.raises(UnsafeTestDatabase, match='identity or PostgreSQL permissions'):
        wrong_identity.connect(dialect, args, params)


def test_runner_cannot_write_marker_or_connect_to_other_databases(migrated):
    engine = get_engine()
    with engine.connect() as conn:
        assert conn.exec_driver_sql('SELECT current_user').scalar_one() == 'bis_test_runner'
        assert conn.exec_driver_sql("""
            SELECT count(*) FROM pg_database
            WHERE datname <> current_database() AND datallowconn
              AND has_database_privilege(current_user, oid, 'CONNECT')
        """).scalar_one() == 0
        with pytest.raises(ProgrammingError) as failure:
            # Check UPDATE permission without ever selecting a row to change.
            conn.exec_driver_sql('UPDATE bis_test_guard.identity SET id = id WHERE false')
        assert failure.value.orig.sqlstate == '42501'


def test_pgvector_survives_reset_when_enabled(migrated):
    if not get_settings().vector_enabled:
        pytest.skip('pgvector is disabled on this local test instance')
    with get_engine().connect() as conn:
        assert conn.exec_driver_sql("SELECT vector_dims('[1,2]'::vector)").scalar_one() == 2
