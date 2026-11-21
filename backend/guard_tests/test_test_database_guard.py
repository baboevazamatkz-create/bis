"""No real connection is needed to exercise the failure paths."""

import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import create_engine

from bis.testing_database import UnsafeTestDatabase, install_guard
from bis.testing_database import test_database_config as parse_config

IDENTITY = str(uuid.uuid4())
URL = 'postgresql+psycopg://bis_test_runner:fixture@127.0.0.1:54532/bis_test'


def config(**overrides):
    return parse_config({'TEST_DATABASE_URL': URL, 'BIS_TEST_DB_ID': IDENTITY, **overrides})


@pytest.mark.parametrize('environ', [
    {},
    {'TESTING': 'true'},
    {'TEST_DATABASE_URL': URL},
    {'TEST_DATABASE_URL': URL, 'BIS_TEST_DB_ID': 'invalid'},
    {'TEST_DATABASE_URL': URL.replace('/bis_test', '/bis'), 'BIS_TEST_DB_ID': IDENTITY},
    {'TEST_DATABASE_URL': URL.replace('bis_test_runner', 'postgres'), 'BIS_TEST_DB_ID': IDENTITY},
    {'TEST_DATABASE_URL': URL.replace('127.0.0.1', 'production'), 'BIS_TEST_DB_ID': IDENTITY},
    {'TEST_DATABASE_URL': URL + '?hostaddr=192.0.2.1', 'BIS_TEST_DB_ID': IDENTITY},
    {'TEST_DATABASE_URL': URL, 'BIS_TEST_DB_ID': IDENTITY, 'BIS_DATABASE_URL': 'production'},
    {'TEST_DATABASE_URL': URL, 'BIS_TEST_DB_ID': IDENTITY, 'PGSERVICE': 'production'},
])
def test_configuration_fails_before_any_connection(environ):
    with pytest.raises(UnsafeTestDatabase):
        parse_config(environ)


def test_other_engine_cannot_reach_driver(monkeypatch):
    engine = create_engine(URL.replace('/bis_test', '/bis'))
    dial = Mock()
    monkeypatch.setattr(engine.dialect, 'connect', dial)
    uninstall = install_guard(config())
    try:
        with pytest.raises(UnsafeTestDatabase):
            engine.connect()
        dial.assert_not_called()
    finally:
        uninstall()


@pytest.mark.parametrize('overrides', [{'hostaddr': '192.0.2.1'}, {'port': 5432}, {'user': 'postgres'}])
def test_engine_connect_args_cannot_redirect_the_connection(monkeypatch, overrides):
    engine = create_engine(URL, connect_args=overrides)
    dial = Mock()
    monkeypatch.setattr(engine.dialect, 'connect', dial)
    uninstall = install_guard(config())
    try:
        with pytest.raises(UnsafeTestDatabase):
            engine.connect()
        dial.assert_not_called()
    finally:
        uninstall()


@pytest.mark.parametrize(('row', 'accepted'), [
    (('bis_test', 'bis_test_runner', 'bis_test_runner', '127.0.0.1', 54532, IDENTITY, True), True),
    (('bis', 'bis_test_runner', 'bis_test_runner', '127.0.0.1', 54532, IDENTITY, True), False),
    (('bis_test', 'bis_test_runner', 'postgres', '127.0.0.1', 54532, IDENTITY, True), False),
    (('bis_test', 'bis_test_runner', 'bis_test_runner', '127.0.0.1', 54532, 'other', True), False),
    (('bis_test', 'bis_test_runner', 'bis_test_runner', '127.0.0.1', 54532, IDENTITY, False), False),
    (None, False),
])
def test_identity_verified_using_only_reads(row, accepted):
    raw = MagicMock()
    cursor = raw.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = row
    dialect = SimpleNamespace(connect=Mock(return_value=raw))
    settings = config()
    args, params = create_engine(URL).dialect.create_connect_args(settings.url)
    if accepted:
        assert settings.connect(dialect, args, params) is raw
        raw.rollback.assert_called_once()
    else:
        with pytest.raises(UnsafeTestDatabase):
            settings.connect(dialect, args, params)
        raw.close.assert_called_once()
    cursor.execute.assert_called_once()
    assert cursor.execute.call_args.args[0].lstrip().startswith('SELECT')
