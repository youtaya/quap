"""Real PostgreSQL regressions for recovery, durable controls and data gates."""

from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import CN, BasketInput, now
from quant_platform.jobs.scheduler import tick
from quant_platform.jobs.worker import Worker
from quant_platform.operations import board_status, doctor, maintenance, qualification
from quant_platform.providers import Deferred, PermissionDenied
from quant_platform.providers.tushare import Tushare
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict, save_basket
from test_postgres import seed
from test_provider import row

pytestmark = pytest.mark.postgres


def job(db, kind, queue="test", payload=None):
    with db.transaction() as conn:
        jid = db.enqueue(conn, kind, queue, f"{kind}:{now().isoformat()}", payload)
    return db.claim(queue, "test-owner")


def test_quota_deferrals_do_not_exhaust_failure_budget(db):
    item = job(db, "daily")
    for _ in range(12):
        db.defer(item, "quota", delay=0, transient=True)
        item = db.claim("test", "test-owner")
        assert item and item["failures"] == 0
    assert item["attempts"] == 13
    for i in range(10):
        db.defer(item, "failure", delay=0)
        item = db.claim("test", "test-owner")
        assert (item is None) == (i == 9)
    assert db.rows("SELECT failures,status FROM jobs")[0] == {"failures": 10, "status": "failed"}


def test_heartbeat_completed_publication_and_shutdown_drain(db, settings):
    worker = Worker(db, settings, "test")
    worker.job = job(db, "test")
    worker.beat_once()
    assert not worker.stop.is_set()
    with db.publication(worker.job):
        pass
    worker.beat_once()
    assert not worker.stop.is_set(), "Normal completion must not look like lost ownership"
    worker.job = job(db, "next")
    worker.stop.set()
    worker.beat_once()
    assert db.renew(worker.job), "SIGTERM stops claims but renews bounded in-flight work"


def test_heartbeat_reads_one_stable_job_snapshot(settings):
    db = Mock()
    worker = Worker(db, settings, "quotes")
    item = {"id": 1}
    worker.job = item
    db.heartbeat.side_effect = lambda *args: setattr(worker, "job", None)
    worker.beat_once()
    db.renew.assert_called_once_with(item)
    assert not worker.stop.is_set()


def test_scheduler_terminal_quotes_do_not_stop_next_cycle(db, settings):
    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    with db.transaction() as conn:
        db.set_setting(conn, "directory", {"fetched_at": at.isoformat()})
        jid = db.enqueue(conn, "quotes", "quotes", "failed-quote")
        conn.execute("UPDATE jobs SET status='failed' WHERE id=%s", (jid,))
    tick(db, settings, at)
    tick(db, settings, at)
    assert len(db.rows("SELECT * FROM jobs WHERE queue='quotes' AND status='pending'")) == 1
    daily = db.rows("SELECT payload FROM jobs WHERE kind='daily'")
    assert all(r["payload"]["day"] < str(day) for r in daily), "No unfinished current-day history"


def test_blocked_capability_prevents_new_quote_work(db, settings):
    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    with db.transaction() as conn:
        db.set_setting(conn, "directory", {"fetched_at": at.isoformat()})
    db.capability("rt_k", "blocked", {})
    tick(db, settings, at)
    assert not db.rows("SELECT * FROM jobs WHERE kind='quotes'")


def test_cap_split_resumes_after_quota_wait(db, settings, monkeypatch):
    item = job(db, "daily")
    feed = Tushare(settings, db)
    calls = []

    def request(endpoint, params):
        calls.append(params.get("ts_code"))
        if "ts_code" not in params:
            return [row()] * 6000
        if params["ts_code"] == "000001.SZ":
            raise Deferred("quota")
        return [row(params["ts_code"])]

    monkeypatch.setattr(feed, "request", request)
    day = datetime(2026, 9, 21).date()
    with pytest.raises(Deferred):
        feed.daily_partition("daily", day, {"SH600895", "SZ000001"}, job=item)
    assert calls == [None, "600895.SH", "000001.SZ"]
    calls.clear()

    def resumed(endpoint, params):
        calls.append(params["ts_code"])
        return [row(params["ts_code"])]

    monkeypatch.setattr(feed, "request", resumed)
    result = feed.daily_partition("daily", day, {"SH600895", "SZ000001"}, job=item)
    assert len(result) == 2 and calls == ["000001.SZ"]
    feed.close()


def test_unknown_intervening_calendar_blocks_activation(db):
    day = seed(db)
    with db.transaction() as conn:
        conn.execute("DELETE FROM calendars WHERE day=%s", (day + timedelta(days=1),))
    with pytest.raises(Conflict, match="Calendar gaps"):
        with db.transaction() as conn:
            save_basket(conn, BasketInput(name="test", members={"SH600895": 1}))


def test_api_refresh_settings_control_conflicts_and_retry(db, settings):
    seed(db)
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        response = client.post("/api/v1/control", json={"action": "refresh"})
        jid = response.json()["job_id"]
        assert db.rows("SELECT kind FROM jobs WHERE id=%s", (jid,))[0]["kind"] == "refresh"
        assert client.put("/api/v1/settings", json={"quote_seconds": 45}).status_code == 200
        assert client.get("/api/v1/settings").json()["quote_seconds"] == 45
        assert client.put("/api/v1/settings", json={"quote_seconds": 60}).status_code == 409
        basket = client.post("/api/v1/baskets", json={"name": "one", "members": {"SH600895": 1}}).json()
        path = f"/api/v1/baskets/{basket['id']}/control"
        assert client.post(path, json={"action": "pause", "expected_revision": 1}).status_code == 200
        assert client.post(path, json={"action": "resume", "expected_revision": 1}).status_code == 409
        assert (
            client.post(
                path, json={"action": "archive", "expected_revision": 1, "expected_control_revision": 1}
            ).status_code
            == 200
        )
        assert (
            client.post(
                path, json={"action": "resume", "expected_revision": 1, "expected_control_revision": 2}
            ).status_code
            == 409
        )
        assert client.post(f"/api/v1/jobs/{jid}/retry").status_code == 409
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET status='failed',failures=10 WHERE id=%s", (jid,))
        assert client.post(f"/api/v1/jobs/{jid}/retry").status_code == 202
        assert client.get("/api/v1/boards").status_code == 200


def test_board_coverage_does_not_hide_unverified_references(db):
    seed(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO latest_quotes VALUES('SH600895',1,%s)",
            (jsonb({"source_time": now().isoformat(), "close": 11, "pre_close": 10, "reference_verified": False}),),
        )
    main = next(b for b in board_status(db) if b["board"] == "Main Board")
    assert main["fresh"] == 1 and main["coverage"] == 0 and main["daily_return"] is None


def test_doctor_only_unblocks_revalidated_dependencies(db, settings):
    seed(db)
    with db.transaction() as conn:
        for kind in ("directory", "daily", "quotes", "qlib_export"):
            jid = db.enqueue(conn, kind, "test", kind)
            conn.execute("UPDATE jobs SET status='blocked' WHERE id=%s", (jid,))
    feed = Mock()

    def request(endpoint, params, probe=False):
        if endpoint != "stock_basic":
            raise PermissionDenied("not entitled")
        return [{"ts_code": "600895.SH"}]

    feed.request.side_effect = request
    doctor(db, settings, feed)
    states = {r["kind"]: r["status"] for r in db.rows("SELECT kind,status FROM jobs")}
    assert states == {"directory": "pending", "daily": "blocked", "quotes": "blocked", "qlib_export": "blocked"}


def test_retention_preserves_recent_rows_and_records_incidents(db, settings, tmp_path):
    settings.artifact_root = tmp_path
    with db.transaction() as conn:
        conn.execute("INSERT INTO alert_keys VALUES('old',now()-interval '400 days'),('recent',now())")
    item = job(db, "maintenance")
    maintenance(db, settings, item)
    assert [r["event_key"] for r in db.rows("SELECT * FROM alert_keys")] == ["recent"]
    assert db.rows("SELECT data FROM incidents WHERE key='backup_overdue'")[0]["data"]["active"]


def test_qualification_cannot_backfill_missed_live_minutes(db, settings, monkeypatch):
    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    monkeypatch.setattr("quant_platform.operations.now", lambda: at)
    item = job(db, "qualification", payload={"at": (at - timedelta(minutes=5)).isoformat()})
    qualification(db, settings, item)
    assert db.rows("SELECT * FROM qualification_samples") == []
    assert db.setting("qualification")["status"] == "pending"


def test_backup_connection_options_and_ambient_isolation(monkeypatch):
    from quant_platform.operations import pg_environment

    monkeypatch.setenv("PGSERVICE", "unrelated-service")
    monkeypatch.setenv("PGSSLMODE", "disable")
    env = pg_environment(
        "postgresql://operator:fixture%40only@db.example/quant%5Ftest"
        "?sslmode=verify-full&sslrootcert=%2Fcerts%2Fca.pem&target_session_attrs=read-write&connect_timeout=9"
    )
    assert env["PGDATABASE"] == "quant_test"
    assert env["PGPASSWORD"] == "fixture@only"
    assert env["PGSSLMODE"] == "verify-full" and env["PGSSLROOTCERT"] == "/certs/ca.pem"
    assert env["PGTARGETSESSIONATTRS"] == "read-write" and env["PGCONNECT_TIMEOUT"] == "9"
    assert "PGSERVICE" not in env
    with pytest.raises(ValueError, match="explicit database"):
        pg_environment("host=localhost")


def test_gapped_stock_and_benchmark_metrics_are_unavailable(history_rows):
    from quant_platform.jobs.analyze import calendar_indicators

    target = history_rows[-1]["day"]
    calendar = {str(r["day"]): True for r in history_rows}
    complete = calendar_indicators(history_rows, target, calendar)
    assert complete["status"] == "complete" and complete["return20"] is not None
    for rows, dates in (
        (history_rows[:70] + history_rows[71:], calendar),
        (history_rows, {k: v for k, v in calendar.items() if k != str(history_rows[70]["day"])}),
        (history_rows, {**calendar, str(history_rows[70]["day"]): False}),
    ):
        result = calendar_indicators(rows, target, dates)
        assert result["status"] == "calendar_gaps"
        assert all(result.get(key) is None for key in ("return20", "rsi14", "sma60", "volatility20"))
        assert result["data_versions"]


def seed_analysis(db, history_rows):
    today = seed(db)
    with db.transaction() as conn:
        conn.execute("UPDATE instruments SET name='Fixture'")
    target = today - timedelta(days=1)
    rows = [{**r, "day": target - timedelta(days=len(history_rows) - 1 - i)} for i, r in enumerate(history_rows)]
    with db.transaction() as conn:
        for row in rows:
            for exchange in ("SSE", "SZSE"):
                conn.execute(
                    "INSERT INTO calendars VALUES(%s,%s,true,now()) ON CONFLICT DO NOTHING", (exchange, row["day"])
                )
        did = db.dataset(conn, "daily", "fixture-history", rows[:-1], {})
        fid = db.dataset(conn, "adj_factor", "fixture-history", rows, {})
        for row in rows:
            conn.execute("INSERT INTO factors VALUES('SH600895',%s,%s,1)", (row["day"], fid))
        for row in rows[:-1]:
            conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (row["day"], did, jsonb(row["data"])))
    return target, rows


def test_collection_analysis_manual_candidate_approval(db, settings, history_rows):
    from quant_platform.jobs.analyze import daily
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    feed = Mock()
    feed.daily_partition.side_effect = lambda endpoint, day, codes, job: (
        [{"symbol": "SH600895", "factor": 1}]
        if endpoint == "adj_factor"
        else [{"symbol": "SH600895", "day": day, **rows[-1]["data"]}]
    )
    history(db, feed, job(db, "daily", payload={"day": str(target)}))
    assert db.setting("analysis_dirty")
    daily(db, job(db, "analyze", payload={"day": str(target)}), settings)
    saved = db.rows("SELECT * FROM reports WHERE kind='screen'")[0]
    assert saved["data"]["status"] == "complete"
    assert saved["data"]["candidates"][0]["symbol"] == "SH600895"
    assert not db.rows("SELECT * FROM baskets"), "Screening cannot automatically change baskets"
    stock = db.rows("SELECT data FROM reports WHERE kind='stock'")[0]["data"]
    assert stock["data_versions"][-1][0] == db.setting("analysis_dirty")["daily"]
    assert stock["snapshot_hash"] == saved["data"]["snapshot_hash"]
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        path = f"/api/v1/candidates/{saved['id']}/approve"
        assert client.post(path, json={"name": "Rejected", "symbols": ["bad"]}).status_code == 422
        assert client.post(path, json={"name": "Rejected", "symbols": ["SZ000001"]}).status_code == 409
        response = client.post(path, json={"name": "Approved", "symbols": ["SH600895"]})
        assert response.status_code == 201, response.text
        assert response.json()["effective_day"] > str(target)
    assert len(db.rows("SELECT * FROM baskets")) == 1
    assert db.rows("SELECT * FROM audit WHERE action='candidate_approval'")


def test_analysis_retry_pins_all_inputs(db, settings, history_rows, monkeypatch):
    from quant_platform.jobs import analyze

    target, rows = seed_analysis(db, history_rows)
    with db.transaction() as conn:
        did = db.dataset(conn, "daily", str(target), [rows[-1]], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (target, did, jsonb(rows[-1]["data"])))
    monkeypatch.setattr(
        "quant_platform.storage.baskets.now",
        lambda: datetime.combine(target - timedelta(days=1), datetime.min.time(), CN),
    )
    with db.transaction() as conn:
        basket = save_basket(conn, BasketInput(name="Original", members={"SH600895": 1}))
    item = job(db, "analyze", payload={"day": str(target)})
    snapshot = analyze.analysis_snapshot(db, item, target, settings)
    db.defer(item, "interrupted", delay=0, transient=True)
    with db.transaction() as conn:
        conn.execute("UPDATE instruments SET name='ST changed',status='U'")
        conn.execute("UPDATE calendars SET is_open=false WHERE day=%s", (target,))
        conn.execute("UPDATE baskets SET archived=true WHERE id=%s", (basket["id"],))
        conn.execute("INSERT INTO rules(data) VALUES(%s)", (jsonb({"minimum_turnover": 999999999}),))
        corrected = {**rows[-1]["data"], "close": 100}
        revised = db.dataset(conn, "daily", str(target), [corrected], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (target, revised, jsonb(corrected)))
    retried = db.claim("test", "replacement")
    analyze.daily(db, retried, settings)
    stock = db.rows("SELECT data FROM reports WHERE kind='stock'")[0]["data"]
    assert stock["status"] == "complete" and stock["close"] == rows[-1]["data"]["close"]
    assert stock["dataset_watermark"] == snapshot["watermark"] < revised
    assert db.rows("SELECT data FROM reports WHERE kind='basket'")[0]["data"]["name"] == "Original"
    assert db.rows("SELECT data FROM reports WHERE kind='screen'")[0]["data"]["eligible"] == 1


@pytest.mark.parametrize(
    "samples,unhealthy,sample_environment,expected",
    [
        (240, 0, "production", "observed"),
        (235, 0, "production", "pending"),
        (240, 3, "production", "pending"),
        (240, 0, "test", "pending"),
        (240, 0, None, "pending"),
    ],
)
def test_qualification_requires_two_complete_production_sessions(
    db, settings, monkeypatch, samples, unhealthy, sample_environment, expected
):
    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=15, minute=5)
    monkeypatch.setattr("quant_platform.operations.now", lambda: at)
    settings.environment = "production"
    with db.transaction() as conn:
        for offset in (-1, 0):
            current = day + timedelta(days=offset)
            for i in range(samples):
                minute = datetime.combine(current, datetime.min.time(), CN).replace(hour=9, minute=30)
                minute += timedelta(minutes=i if i < 120 else i + 90)
                conn.execute(
                    "INSERT INTO qualification_samples VALUES(%s,%s,%s,%s)",
                    (minute, current, i >= unhealthy, jsonb({"environment": sample_environment})),
                )
    qualification(db, settings, job(db, "qualification", payload={"at": at.isoformat()}))
    assert db.setting("qualification")["status"] == expected


def test_evolving_legacy_import_preserves_identity_and_source(db, tmp_path):
    import json
    import sqlite3
    from quant_platform.legacy import migrate_legacy

    seed(db)
    initial = {"baskets": {"Watch": {"symbols": ["SH600895"]}}}
    with sqlite3.connect(tmp_path / "service.sqlite3") as source:
        source.execute("CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT)")
        source.execute("INSERT INTO monitor_state VALUES('settings',?)", (json.dumps({"data": initial}),))
    with sqlite3.connect(tmp_path / "alerts.sqlite3") as source:
        source.execute("CREATE TABLE events(id INTEGER PRIMARY KEY,mode TEXT,message TEXT)")
        source.execute("INSERT INTO events VALUES(1,'live','old'),(2,'demo','excluded')")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert migrate_legacy(db, tmp_path)["dry_run"]
    assert not db.rows("SELECT * FROM baskets")
    migrate_legacy(db, tmp_path, apply=True)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert migrate_legacy(db, tmp_path, apply=True)["already_imported"]
    basket = db.rows("SELECT * FROM baskets")[0]
    with sqlite3.connect(tmp_path / "alerts.sqlite3") as source:
        source.execute("INSERT INTO events VALUES(3,'live','new')")
    migrate_legacy(db, tmp_path, apply=True)
    assert db.rows("SELECT id,revision FROM baskets") == [{"id": basket["id"], "revision": 1}]
    assert len(db.rows("SELECT * FROM alerts")) == 2
    assert not db.rows("SELECT * FROM alert_state") and not db.rows("SELECT * FROM daily_bars")
    changed = {**initial, "alerts": {"stock_change": 0.05}}
    for revision, value in ((2, changed), (3, initial)):
        with sqlite3.connect(tmp_path / "service.sqlite3") as source:
            source.execute("UPDATE monitor_state SET value=?", (json.dumps({"data": value}),))
        migrate_legacy(db, tmp_path, apply=True)
        assert db.rows("SELECT id,revision FROM baskets") == [{"id": basket["id"], "revision": revision}]
    with db.transaction() as conn:
        conn.execute("UPDATE baskets SET paused=true,control_revision=1")
    with sqlite3.connect(tmp_path / "service.sqlite3") as source:
        source.execute("UPDATE monitor_state SET value=?", (json.dumps({"data": changed}),))
    with pytest.raises(Conflict, match="changed locally"):
        migrate_legacy(db, tmp_path, apply=True)
    assert db.rows("SELECT revision FROM baskets")[0]["revision"] == 3


def test_operational_metrics_cover_source_analysis_and_api_errors(db, settings, monkeypatch, tmp_path):
    day = seed(db)
    settings.artifact_root = tmp_path
    at = now()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO latest_quotes VALUES('SH600895',1,%s)",
            (jsonb({"source_time": (at - timedelta(seconds=30)).isoformat()}),),
        )
        db.set_setting(conn, "analysis_target", str(day))
        db.set_setting(conn, "last_analysis", {"as_of": str(day - timedelta(days=2)), "at": at.isoformat()})
    item = job(db, "daily")
    db.defer(item, "quota exhausted", transient=True)
    with TestClient(create_app(settings, db), raise_server_exceptions=False) as client:
        assert client.get("/api/v1/status").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        with monkeypatch.context() as patch:
            patch.setattr(db, "rows", Mock(side_effect=RuntimeError("private database failure")))
            response = client.get("/api/v1/instruments")
            assert response.status_code == 503 and "private" not in response.text
        assert client.get("/api/v1/quotes", params={"code": "bad"}).status_code == 422
        metrics = client.get("/api/v1/status").json()["metrics"]
    assert metrics["api"]["requests"] == 3
    assert metrics["api"]["client_errors"] == 2 and metrics["api"]["server_errors"] == 1
    assert metrics["api"]["mean_latency_seconds"] >= 0
    assert metrics["source_age_seconds"]["minimum"] >= 30
    assert metrics["analysis_session_lag"] == 2
    assert metrics["pending_quota_deferrals"] == 1
    assert metrics["artifact_disk"]["free_bytes"] > 0
