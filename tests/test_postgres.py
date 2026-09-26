from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import BasketInput, CN, now
from quant_platform.jobs.analyze import alert
from quant_platform.providers import Deferred
from quant_platform.providers.tushare import Budget
from quant_platform.storage import LostLease
from quant_platform.storage.baskets import Conflict, save_basket

pytestmark = pytest.mark.postgres


def seed(db):
    day = now().astimezone(CN).date()
    with db.transaction() as conn:
        for i in range(-3, 4):
            for exchange in ("SSE", "SZSE"):
                conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, day + timedelta(days=i)))
        conn.execute(
            "INSERT INTO instruments VALUES('SH600895','Test','Main Board','SSE',%s,NULL,'L','{}',now())",
            (day - timedelta(days=500),),
        )
    return day


def enqueue(db):
    with db.transaction() as conn:
        return db.enqueue(conn, "test", "test", "unique")


def test_single_claim_fencing_atomic_publication(db):
    jid = enqueue(db)
    with ThreadPoolExecutor(max_workers=2) as executor:
        jobs = list(executor.map(lambda owner: db.claim("test", owner), ["a", "b"]))
    assert sum(j is not None for j in jobs) == 1
    old = next(j for j in jobs if j)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (jid,))
    new = db.claim("test", "replacement")
    assert new["fence"] > old["fence"]
    with pytest.raises(LostLease):
        with db.publication(old):
            pytest.fail("Expired owner must not publish")
    with pytest.raises(RuntimeError):
        with db.publication(new) as conn:
            db.set_setting(conn, "rollback", 1)
            raise RuntimeError("crash")
    assert db.setting("rollback") is None
    with db.publication(new) as conn:
        db.set_setting(conn, "published", 1)
        db.enqueue(conn, "child", "test", "downstream")
    assert db.setting("published") == 1
    assert db.rows("SELECT status FROM jobs WHERE id=%s", (jid,))[0]["status"] == "complete"
    assert enqueue(db) == jid


def test_data_revision_reversions_are_not_lost(db):
    with db.transaction() as conn:
        a = db.dataset(conn, "daily", "2026-01-01", [1], {})
        repeated = db.dataset(conn, "daily", "2026-01-01", [1], {})
        b = db.dataset(conn, "daily", "2026-01-01", [2], {})
        reverted = db.dataset(conn, "daily", "2026-01-01", [1], {})
    assert a == repeated
    assert a < b < reverted


def test_basket_effective_next_session_and_conflict(db):
    day = seed(db)
    value = BasketInput(name="basket", members={"SH600895": 1})
    with db.transaction() as conn:
        saved = save_basket(conn, value)
    assert saved["effective_day"] == day + timedelta(days=1)
    assert db.active_baskets(day) == []
    assert len(db.active_baskets(day + timedelta(days=1))) == 1
    with pytest.raises(Conflict):
        with db.transaction() as conn:
            save_basket(conn, value, UUID(saved["id"]))


def test_alert_duplicate_restart_missing_not_rearmed(db):
    at = now()
    with db.transaction() as conn:
        alert(conn, "rule", "first", 0.04, 0.03, 300, {}, at)
        alert(conn, "rule", "first", 0.04, 0.03, 300, {}, at)
        alert(conn, "rule", "missing", None, 0.03, 300, {}, at + timedelta(minutes=10))
        alert(conn, "rule", "sustained", 0.04, 0.03, 300, {}, at + timedelta(minutes=10))
    assert len(db.rows("SELECT * FROM alerts")) == 1
    with db.transaction() as conn:
        alert(conn, "rule", "exit", 0.01, 0.03, 300, {}, at + timedelta(minutes=11))
        alert(conn, "rule", "reenter", 0.04, 0.03, 300, {}, at + timedelta(minutes=12))
    assert len(db.rows("SELECT * FROM alerts")) == 2


def test_credential_budget_shared_between_workers(db, settings):
    settings.ordinary_rpm = 1
    one, two = Budget(db, settings), Budget(db, settings)
    one.acquire("daily")
    with pytest.raises(Deferred):
        two.acquire("adj_factor")
    two.acquire("rt_k")


def test_api_auth_and_versioned_mutations(db, settings):
    seed(db)
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/api/v1/baskets").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        assert client.post("/api/v1/baskets", json={"name": "Basket", "members": {"SH600895": 1}}).status_code == 410
        targets = {"name": "Model", "weights": {"SH600895": 0.05}, "cash_weight": 0.95}
        response = client.post("/api/v1/model-portfolios", json=targets)
        assert response.status_code == 201, response.text
        data = response.json()
        assert client.get("/api/v1/model-portfolios").json()[0]["revision"] == 1
        assert client.put("/api/v1/model-portfolios/" + data["id"], json=targets).status_code == 409
        assert (
            client.put("/api/v1/model-portfolios/" + data["id"], json={**targets, "expected_revision": 1}).status_code
            == 200
        )
        assert client.get("/api/v1/status").status_code == 200
        assert client.post("/api/v1/control", json={"action": "pause"}).status_code == 202
        assert db.setting("polling_paused") is True
