"""Provision a freshly created, disposable PostgreSQL instance for BIS tests."""

import argparse
import os
from uuid import UUID

import psycopg
from psycopg import sql


class ClusterIdentityMismatch(RuntimeError):
    """The endpoint is not the disposable cluster identified out of band."""


def provision(port, admin_password, runner_password, identity, vector, cluster_id):
    with psycopg.connect(host='127.0.0.1', port=port, dbname='bis_test', user='bis_test_admin',
                         password=admin_password, connect_timeout=5) as admin:
        actual_id = admin.execute('SELECT system_identifier FROM pg_catalog.pg_control_system()').fetchone()[0]
        if actual_id != cluster_id:
            raise ClusterIdentityMismatch
        databases = {name for (name,) in admin.execute('SELECT datname FROM pg_database')}
        if databases != {'bis_test', 'postgres', 'template0', 'template1'}:
            raise RuntimeError('Provisioning requires a fresh dedicated test cluster.')
        if admin.execute("""
            SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = 'bis_test_runner')
                OR EXISTS (SELECT FROM pg_namespace WHERE nspname = 'bis_test_guard')
                OR EXISTS (SELECT FROM pg_class WHERE relnamespace = 'public'::regnamespace)
        """).fetchone()[0]:
            raise RuntimeError('Test cluster is not empty or already provisioned.')
        admin.execute(sql.SQL('CREATE ROLE bis_test_runner LOGIN PASSWORD {}').format(sql.Literal(runner_password)))
        for db in sorted(databases):
            admin.execute(sql.SQL('REVOKE CONNECT, TEMPORARY ON DATABASE {} FROM PUBLIC').format(sql.Identifier(db)))
        admin.execute('GRANT CONNECT, CREATE ON DATABASE bis_test TO bis_test_runner')
        admin.execute('ALTER SCHEMA public OWNER TO bis_test_runner')
        admin.execute('CREATE SCHEMA bis_test_guard AUTHORIZATION bis_test_admin')
        admin.execute('CREATE TABLE bis_test_guard.identity (id uuid PRIMARY KEY, server_address inet NOT NULL)')
        # The client dials loopback. Docker's port forwarding can give PostgreSQL a bridge address.
        local_endpoint = admin.execute("SELECT inet_server_addr() = '127.0.0.1'::inet").fetchone()[0]
        print('PostgreSQL endpoint recorded:', 'loopback' if local_endpoint else 'forwarded', flush=True)
        admin.execute('INSERT INTO bis_test_guard.identity VALUES (%s, inet_server_addr())', (identity,))
        admin.execute('REVOKE ALL ON bis_test_guard.identity FROM PUBLIC')
        admin.execute('GRANT USAGE ON SCHEMA bis_test_guard TO bis_test_runner')
        admin.execute('GRANT SELECT ON bis_test_guard.identity TO bis_test_runner')
        if vector:
            admin.execute('CREATE SCHEMA bis_test_extensions AUTHORIZATION bis_test_admin')
            admin.execute('CREATE EXTENSION vector WITH SCHEMA bis_test_extensions')
            admin.execute('GRANT USAGE ON SCHEMA bis_test_extensions TO bis_test_runner')
            admin.execute('ALTER ROLE bis_test_runner IN DATABASE bis_test SET search_path TO public, bis_test_extensions')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--without-vector', action='store_true')
    args = parser.parse_args()
    try:
        identity = UUID(os.environ['BIS_TEST_DB_ID'])
        cluster_id = int(os.environ['BIS_TEST_CLUSTER_ID'])
        admin_password = os.environ['BIS_TEST_ADMIN_PASSWORD']
        runner_password = os.environ['BIS_TEST_RUNNER_PASSWORD']
        if (identity.version != 4 or cluster_id <= 0 or not admin_password or not runner_password
                or admin_password == runner_password
                or any(key.startswith('PG') for key in os.environ)):
            raise ValueError
        provision(args.port, admin_password, runner_password, identity, not args.without_vector, cluster_id)
    except (KeyError, ValueError, RuntimeError, psycopg.Error) as exc:
        parser.exit(1, f'Disposable PostgreSQL provisioning failed ({type(exc).__name__}).\n')
