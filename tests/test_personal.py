"""PostgreSQL coverage for personal roles, alert delivery and the research worker."""

from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import now
from quant_platform.jobs.analyze import alert
from quant_platform.jobs.notify import deliver
from quant_platform.observations import count_lines
from quant_platform.operations import data_quality
from quant_platform.storage import Database
from test_recovery import job, seed_analysis

pytestmark = pytest.mark.postgres


def test_read_role_cannot_write_and_worker_role_can(postgres_url):
    reader = Database(postgres_url, role="quant_read")
    writer = Database(postgres_url, role="quant_worker")
    try:
        assert reader.role == "quant_read" and writer.role == "quant_worker"
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with reader.transaction() as conn:
                conn.execute("INSERT INTO incidents VALUES('role-read','{}',now())")
        with writer.transaction() as conn:
            conn.execute("INSERT INTO incidents VALUES('role-write','{}',now())")
        assert writer.rows("SELECT key FROM incidents WHERE key='role-write'")
    finally:
        reader.close()
        writer.close()


def test_alert_outbox_survives_failed_delivery(db, settings):
    at = now()
    with db.transaction() as conn:
        alert(conn, "rule", "first", 0.05, 0.03, 300, {"target": "SH600895"}, at)
    assert db.rows("SELECT event_key FROM alert_outbox")
    item = job(db, "notify", queue="notify")

    def fail(payload):
        raise RuntimeError("channel down")

    deliver(db, settings, item, send=fail)
    row = db.rows("SELECT * FROM alert_outbox")[0]
    assert row["delivered_at"] is None and row["attempts"] == 1 and row["last_error"] == "RuntimeError"
    assert db.rows("SELECT * FROM alerts")
    item = job(db, "notify", queue="notify")
    delivered = []
    deliver(db, settings, item, send=lambda payload: delivered.append(payload["event_key"]))
    assert delivered
    assert db.rows("SELECT delivered_at FROM alert_outbox")[0]["delivered_at"] is not None


def test_research_worker_withholds_orders_and_reuses_hash(db, settings, history_rows):
    from quant_platform.jobs.research import run

    target, _rows = seed_analysis(db, history_rows)
    item = job(db, "research", queue="research", payload={"day": str(target)})
    run(db, item, settings)
    saved = db.rows("SELECT data FROM reports WHERE kind='backtest'")[0]["data"]
    assert saved["execution"] == "withheld_limit_unknown"
    assert saved["research_value"] is None
    assert saved["automatic_orders"] is False
    assert "nav" not in saved
    again = job(db, "research", queue="research", payload={"day": str(target)})
    run(db, again, settings)
    second = db.rows("SELECT data FROM reports WHERE kind='backtest' ORDER BY id DESC")[0]["data"]
    assert second["research_hash"] == saved["research_hash"]


def test_data_quality_and_persisted_api_events(db, settings, tmp_path):
    settings.observation_root = tmp_path
    quality = data_quality(db)
    assert "container" not in quality
    assert quality["note"].startswith("Data quality")
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        body = client.get("/api/v1/status").json()
    assert body["data_quality"]["note"] == quality["note"]
    assert "services" in body and "data_quality" in body
    assert count_lines(tmp_path, "api") >= 1
    assert body["db_roles"] == {"read": "shared", "write": "shared"}


def test_scheduler_enqueues_personal_workers(db, settings):
    from datetime import datetime

    from quant_platform.domain import CN
    from quant_platform.jobs.scheduler import tick
    from test_postgres import seed

    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    with db.transaction() as conn:
        db.set_setting(conn, "directory", {"fetched_at": (at - timedelta(hours=1)).isoformat()})
    tick(db, settings, at)
    assert db.rows("SELECT 1 FROM jobs WHERE kind='notify'")
