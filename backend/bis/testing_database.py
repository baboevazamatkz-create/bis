"""Opt-in PostgreSQL boundary for the destructive BIS test suite."""

import os
import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.engine import Engine, URL, make_url
from sqlalchemy.exc import ArgumentError


class UnsafeTestDatabase(RuntimeError):
    """Safe diagnostics: never include a DSN or a database exception."""


IDENTITY_SQL = """
SELECT current_database(), current_user, session_user,
       host(inet_server_addr()), inet_server_port(), marker.id::text,
       NOT (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls)
       AND NOT EXISTS (SELECT 1 FROM pg_auth_members WHERE member = r.oid)
       AND NOT EXISTS (
           SELECT 1 FROM pg_database d
           WHERE d.datname <> current_database() AND d.datallowconn
             AND has_database_privilege(r.oid, d.oid, 'CONNECT')
       )
       AND NOT has_schema_privilege(r.oid, 'bis_test_guard', 'CREATE')
       AND NOT has_table_privilege(r.oid, 'bis_test_guard.identity', 'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
       AND current_schema() = 'public'
FROM bis_test_guard.identity AS marker
JOIN pg_roles AS r ON r.rolname = current_user
JOIN pg_namespace AS ns ON ns.nspname = 'bis_test_guard'
JOIN pg_class AS tbl ON tbl.relnamespace = ns.oid AND tbl.relname = 'identity'
WHERE ns.nspowner <> r.oid AND tbl.relowner <> r.oid
"""


@dataclass(frozen=True)
class TestDatabaseConfig:
    url: URL
    identity: str

    def connect(self, dialect, args, params):
        # Check the driver's actual parameters, including any engine-level overrides.
        # Implicit libpq service settings and alternate transports are forbidden.
        expected = dict(host=self.url.host, port=self.url.port, dbname=self.url.database,
                        user=self.url.username, password=self.url.password)
        optional = {'connect_timeout', 'options', 'context'}  # SQLAlchemy's psycopg adapters
        options = params.get('options', '')
        if (args or any(key.startswith('PG') for key in os.environ)
                or set(params) - expected.keys() - optional
                or any(params.get(key) != value for key, value in expected.items())
                or (options and not re.fullmatch(r'(?:-c (?:lock_timeout|statement_timeout)=\d+ ?)+', options))):
            raise UnsafeTestDatabase('Connection parameters do not match the isolated BIS test database.')
        try:
            connection = dialect.connect(*args, **{**params, 'connect_timeout': 5})
        except Exception:
            raise UnsafeTestDatabase('Cannot connect to the explicitly configured BIS test database.') from None
        try:
            with connection.cursor() as cursor:
                cursor.execute(IDENTITY_SQL)
                result = cursor.fetchone()
            if result != (self.url.database, self.url.username, self.url.username,
                          self.url.host, self.url.port, self.identity, True):
                raise UnsafeTestDatabase('BIS test database identity or PostgreSQL permissions do not match.')
            connection.rollback()
        except Exception:
            connection.close()
            raise UnsafeTestDatabase('BIS test database identity or PostgreSQL permissions do not match.') from None
        return connection


def test_database_config(environ):
    """Reject missing configuration and competing production settings before dialing."""
    raw = environ.get('TEST_DATABASE_URL')
    if not raw or not environ.get('BIS_TEST_DB_ID'):
        raise UnsafeTestDatabase('Set TEST_DATABASE_URL and BIS_TEST_DB_ID for an isolated test database.')
    if (environ.get('BIS_DATABASE_URL', raw) != raw or any(key.startswith('PG') for key in environ)):
        raise UnsafeTestDatabase('Remove conflicting BIS_DATABASE_URL or libpq PG* environment variables.')
    try:
        url = make_url(raw)
        identity = UUID(environ['BIS_TEST_DB_ID'])
    except (ArgumentError, TypeError, ValueError):
        raise UnsafeTestDatabase('Invalid BIS test database configuration.') from None
    if (url.drivername != 'postgresql+psycopg' or url.host != '127.0.0.1' or not url.port
            or not 0 < url.port < 65536 or url.database != 'bis_test' or url.username != 'bis_test_runner'
            or not url.password or url.query or identity.version != 4):
        raise UnsafeTestDatabase('Use an explicit loopback port, bis_test, bis_test_runner and a UUIDv4 identity.')
    return TestDatabaseConfig(url, str(identity))


def install_guard(settings):
    """Protect all SQLAlchemy engines in pytest, including Alembic and application sessions."""
    def guarded_connect(dialect, record, args, params):
        del record
        if dialect.name != 'postgresql' or dialect.driver != 'psycopg' or dialect.is_async:
            raise UnsafeTestDatabase('Only the isolated PostgreSQL test connection is allowed.')
        return settings.connect(dialect, args, params)

    event.listen(Engine, 'do_connect', guarded_connect)
    return lambda: event.remove(Engine, 'do_connect', guarded_connect)
