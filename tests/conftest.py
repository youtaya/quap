import os
from datetime import date, timedelta
from urllib.parse import urlsplit

import pytest
from psycopg import sql

from quant_platform.config import Settings
from quant_platform.storage import Database


@pytest.fixture(autouse=True)
def no_external_http(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Tests cannot use external HTTP; inject a mock transport.")

    monkeypatch.setattr("httpx.HTTPTransport.handle_request", fail)


@pytest.fixture
def settings():
    return Settings(api_token="a" * 32, tushare_token="test-only-fixture", environment="test")


@pytest.fixture(scope="session")
def postgres_url():
    dsn = os.environ.get("QUANT_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("QUANT_TEST_DATABASE_URL not set; real PostgreSQL integration not executed")
    if not urlsplit(dsn).path.lstrip("/").startswith("quant_test"):
        pytest.fail("Integration tests require an isolated quant_test* database.")
    from quant_platform.cli import migrate

    migrate(Settings(database_url=dsn))
    return dsn


@pytest.fixture
def db(postgres_url):
    database = Database(postgres_url)
    with database.transaction() as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables t WHERE schemaname='public' AND tablename!='alembic_version' "
            "AND NOT EXISTS(SELECT 1 FROM pg_inherits i WHERE i.inhrelid=('public.' || t.tablename)::regclass)"
        ).fetchall()
        conn.execute(
            sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                sql.SQL(",").join(sql.Identifier(r["tablename"]) for r in tables)
            )
        )
    yield database
    database.close()


@pytest.fixture
def history_rows():
    start = date(2025, 1, 1)
    return [
        {
            "day": start + timedelta(days=i),
            "dataset_id": i + 1,
            "factor_dataset_id": i + 2,
            "factor": 1,
            "data": {
                "open": 10 + i / 100,
                "close": 10 + i / 100,
                "high": 11 + i / 100,
                "low": 9 + i / 100,
                "volume": 1000000,
                "turnover": 30000000,
            },
        }
        for i in range(140)
    ]
