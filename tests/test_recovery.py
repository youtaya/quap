"""Real PostgreSQL regressions for recovery, durable controls and data gates."""

from datetime import datetime, time, timedelta
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import CN, BasketInput, now
from quant_platform.jobs.scheduler import tick
from quant_platform.jobs.worker import Worker
from quant_platform.operations import board_status, doctor, maintenance, qualification
from quant_platform.providers import Deferred, ProviderError
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict, save_basket
from test_postgres import seed

pytestmark = pytest.mark.postgres


def job(db, kind, queue="test", payload=None):
    with db.transaction() as conn:
        db.enqueue(conn, kind, queue, f"{kind}:{now().isoformat()}", payload)
    return db.claim(queue, "test-owner")


def test_quota_deferrals_do_not_exhaust_failure_budget(db):
    item = job(db, "history")
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
    item = {"id": 1, "kind": "quotes"}
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
    daily = db.rows("SELECT payload FROM jobs WHERE kind='history'")
    assert all(r["payload"]["end"] < str(day) for r in daily), "No unfinished current-day history"


def test_scheduler_collects_a_lead_in_session_so_limits_are_derivable(db, settings):
    """窗口内最早一天的价格限制要用它前一天的收盘，而那一天落在分析窗口之外。

    采集窗口因此必须比分析窗口多一个会话；少了它，最早一天的 `constraints` 永远推不出来，准入
    判定永远不成立。lead-in 只提供一根收盘价，不进分析窗口，也不要求它自己的限制。
    """
    day = seed(db)
    settings.history_sessions = 30
    sessions = [day - timedelta(days=index) for index in range(31)]
    at = datetime.combine(day, time(19), CN)
    with db.transaction() as conn:
        conn.execute("DELETE FROM calendars")
        for session in sessions:
            for exchange in ("SSE", "SZSE"):
                conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, session))
        db.set_setting(conn, "directory", {"fetched_at": at.isoformat()})
    tick(db, settings, at)
    payload = db.rows("SELECT payload FROM jobs WHERE kind='history'")[0]["payload"]
    assert payload == {"start": str(sessions[-1]), "end": str(sessions[0])}
    scopes = {row["payload"]["day"] for row in db.rows("SELECT payload FROM jobs WHERE kind='constraints'")}
    assert scopes == {str(session) for session in sessions[:30]}, "lead-in 不进分析窗口"


def test_blocked_capability_prevents_new_quote_work(db, settings):
    day = seed(db)
    at = datetime.combine(day, datetime.min.time(), CN).replace(hour=10)
    with db.transaction() as conn:
        db.set_setting(conn, "directory", {"fetched_at": at.isoformat()})
    db.capability("quotes", "blocked", {})
    tick(db, settings, at)
    assert not db.rows("SELECT * FROM jobs WHERE kind='quotes'")


def provider_bars(rows):
    """A provider bar always carries a previous close; the shared fixture stores only session values."""
    bars, previous = [], None
    for row in rows:
        bars.append({"day": row["day"], "pre_close": previous or row["data"]["open"], **row["data"]})
        previous = row["data"]["close"]
    return bars


def test_partial_history_is_recorded_and_repair_is_bounded(db, settings, history_rows):
    """One source failing for one security is a recorded gap, never a fabricated suspension."""
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO instruments VALUES('SZ000001','Fixture 2','Main Board','SZSE',%s,NULL,'L','{}',now())",
            (target - timedelta(days=500),),
        )
    feed = Mock()
    feed.settings, feed.db = settings, None
    attempted = []

    def fetch(code, start, end):
        attempted.append(code)
        if code == "SZ000001":
            raise ProviderError("no source served this security")
        return {
            "symbol": code,
            "source": "eastmoney",
            "factor_source": "eastmoney",
            "bars": provider_bars(rows),
            "factors": {r["day"]: 1 for r in rows},
        }

    feed.history.side_effect = fetch
    history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert attempted == ["SH600895", "SZ000001"]
    dataset = db.rows("SELECT quality,metadata FROM datasets WHERE endpoint='history' ORDER BY id DESC LIMIT 1")[0]
    assert dataset["quality"] == "partial"
    assert dataset["metadata"]["missing_bars"] == ["SZ000001"]
    assert dataset["metadata"]["failures"] == {"SZ000001": "ProviderError"}
    assert dataset["metadata"]["factors_only"] is False
    repair = db.rows("SELECT payload,available_at FROM jobs WHERE strpos(dedupe,'repair:')=1")
    assert len(repair) == 1 and repair[0]["payload"]["repair"] == 0
    assert repair[0]["available_at"] > now(), "A round with progress still queues the remainder"


def test_history_repair_budget_is_spent_only_by_a_round_without_progress(db, settings, history_rows):
    """A long backfill is a continuation, not a failure; a round that completes nothing is the failure."""
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    with db.transaction() as conn:
        # 该会话没有任何因子，所以这一轮即使拿到了 K 线也不会让任何证券变「完成」。
        conn.execute("DELETE FROM factors WHERE day=%s", (target,))
    feed = Mock()
    feed.settings, feed.db = settings, None
    feed.history.side_effect = lambda code, start, end: {
        "symbol": code,
        "source": "sina",
        "factor_source": None,
        "bars": provider_bars(rows),
        "factors": {},
    }
    history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert db.rows("SELECT 1 FROM datasets WHERE endpoint='history' AND scope=%s", (str(target),))
    repair = db.rows("SELECT payload FROM jobs WHERE strpos(dedupe,'repair:')=1")
    assert [r["payload"]["repair"] for r in repair] == [1], "Bars without a factor complete nothing"


def test_history_stops_where_the_source_budget_defers_the_rest(db, settings, history_rows):
    """The per-source budget is a minute window; draining the list first wastes the whole round."""
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    with db.transaction() as conn:
        for index in range(3):
            conn.execute(
                "INSERT INTO instruments VALUES(%s,'Fixture %s','Main Board','SZSE',%s,NULL,'L','{}',now())",
                (f"SZ00000{index + 2}", index, target - timedelta(days=500)),
            )
    feed = Mock()
    feed.settings, feed.db = settings, None
    attempted = []

    def fetch(code, start, end):
        attempted.append(code)
        if len(attempted) > 1:
            raise Deferred("Source quota exhausted; request deferred.")
        return {
            "symbol": code,
            "source": "sina",
            "factor_source": "sina",
            "bars": provider_bars(rows),
            "factors": {r["day"]: 1 for r in rows},
        }

    feed.history.side_effect = fetch
    history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert attempted == ["SH600895", "SZ000002"], "The round must stop at the first exhausted request"
    repair = db.rows("SELECT payload,available_at,dedupe FROM jobs WHERE strpos(dedupe,'repair:')=1")
    assert len(repair) == 1
    assert repair[0]["payload"]["repair"] == 0, "Progress was made; the retry budget is untouched"
    assert repair[0]["available_at"] - now() < timedelta(minutes=5), "Wait one quota window, not half an hour"
    assert repair[0]["dedupe"].rsplit(":", 1)[-1].isdigit()


def test_history_defers_the_job_when_the_budget_is_already_exhausted(db, settings, history_rows):
    """Nothing fetched and no successor is right: the worker defers the same job with the same delay."""
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    feed = Mock()
    feed.settings, feed.db = settings, None
    feed.history.side_effect = Deferred("Source quota exhausted; request deferred.")
    with pytest.raises(Deferred):
        history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert not db.rows("SELECT 1 FROM datasets WHERE endpoint='history' AND scope=%s", (str(target),))
    assert not db.rows("SELECT 1 FROM jobs WHERE strpos(dedupe,'repair:')=1")


def test_history_completes_without_publishing_when_every_security_is_ready(db, settings, history_rows):
    """A repeat of a finished window has nothing to write; it must still release the lease."""
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    feed = Mock()
    feed.settings, feed.db = settings, None
    feed.history.side_effect = lambda code, start, end: {
        "symbol": code,
        "source": "sina",
        "factor_source": "sina",
        "bars": provider_bars(rows),
        "factors": {r["day"]: 1 for r in rows},
    }
    payload = {"start": str(rows[0]["day"]), "end": str(target)}
    history(db, feed, job(db, "history", payload=payload))
    feed.history.reset_mock()
    repeat = job(db, "history", payload=payload)
    history(db, feed, repeat)
    feed.history.assert_not_called()
    assert db.rows("SELECT status FROM jobs WHERE id=%s", (repeat["id"],))[0]["status"] == "complete"


def test_widening_the_window_refetches_securities_that_already_hold_the_newest_session(db, settings, history_rows):
    """窗口变宽（`history_sessions` 调大，或调度器补的 lead-in 会话）必须重取。

    一次请求覆盖整个窗口，所以只看最新会话会把「请求从未触及新的第一天」误判成「这只证券已经
    完成」。那样多出来的会话永远补不上，准入判定也就永远不成立。
    """
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    lead_in = rows[0]["day"] - timedelta(days=1)
    with db.transaction() as conn:
        for exchange in ("SSE", "SZSE"):
            conn.execute(
                "INSERT INTO calendars VALUES(%s,%s,true,now()) ON CONFLICT DO NOTHING", (exchange, lead_in)
            )
    window_rows = [{**rows[0], "day": lead_in}] + rows
    feed = Mock()
    feed.settings, feed.db = settings, None
    attempted = []

    def fetch(code, start, end):
        attempted.append((code, start))
        served = [row for row in window_rows if start <= row["day"] <= end]
        return {
            "symbol": code,
            "source": "tencent",
            "factor_source": "tencent",
            "bars": provider_bars(served),
            "factors": {row["day"]: 1 for row in served},
        }

    feed.history.side_effect = fetch
    history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert attempted == [("SH600895", rows[0]["day"])]
    assert not db.rows("SELECT 1 FROM datasets WHERE endpoint='history' AND scope=%s", (str(lead_in),))

    attempted.clear()
    wider = {"start": str(lead_in), "end": str(target)}
    history(db, feed, job(db, "history", payload=wider))
    assert attempted == [("SH600895", lead_in)], "窗口更宽时必须重取已持有最新会话的证券"
    assert db.rows("SELECT 1 FROM datasets WHERE endpoint='history' AND scope=%s", (str(lead_in),))

    attempted.clear()
    history(db, feed, job(db, "history", payload=wider))
    assert attempted == [], "窗口没变时不得重复抓取"


def test_factors_only_refresh_never_publishes_an_open_session_bar(db, settings, history_rows, monkeypatch):
    """The morning refresh writes today's factor but must not freeze a half-formed daily bar."""
    from quant_platform.jobs import collect
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    today = now().astimezone(CN).date()
    intraday = datetime.combine(today, time(10, 0), CN)
    monkeypatch.setattr(collect, "now", lambda: intraday)
    feed = Mock()
    feed.settings, feed.db = settings, None
    feed.history.side_effect = lambda code, start, end: {
        "symbol": code,
        "source": "sina",
        "factor_source": "sina",
        "bars": [{**provider_bars(rows)[-1], "day": today}],
        "factors": {today: 0.8},
    }
    before = db.rows("SELECT count(*) AS n FROM daily_bars WHERE day=%s", (today,))[0]["n"]
    history(db, feed, job(db, "factors", payload={"start": str(today), "end": str(today)}))
    assert db.rows("SELECT count(*) AS n FROM daily_bars WHERE day=%s", (today,))[0]["n"] == before
    assert db.rows("SELECT factor FROM factors WHERE symbol='SH600895' AND day=%s", (today,))[0]["factor"] == 0.8
    dataset = db.rows("SELECT metadata FROM datasets WHERE endpoint='factors' ORDER BY id DESC LIMIT 1")[0]
    assert dataset["metadata"]["factors_only"] is True
    assert dataset["metadata"]["open_sessions"] == [str(today)]
    assert not db.rows("SELECT 1 FROM datasets WHERE endpoint='history' AND scope=%s", (str(today),))


def seed_daily_window(db, days, *, adjusted, benchmark, states=True):
    """Publish one history/constraints partition per session, plus factors for the adjusted suffix."""
    with db.transaction() as conn:
        for day in days:
            for exchange in ("SSE", "SZSE"):
                conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, day))
            conn.execute(
                "INSERT INTO instruments VALUES('SH600895','Fixture','Main Board','SSE',%s,NULL,'L','{}',now()) "
                "ON CONFLICT(symbol) DO NOTHING",
                (days[-1],),
            )
            db.dataset(conn, "history", str(day), [{"symbol": "SH600895"}], {})
            db.dataset(conn, "constraints", str(day), [{"symbol": "SH600895"}], {})
            if day in adjusted:
                db.dataset(conn, "factors", str(day), [{"symbol": "SH600895"}], {})
        if benchmark:
            did = db.dataset(conn, "benchmark", "SH000300", [{"symbol": "SH000300"}], {})
            conn.execute("INSERT INTO daily_bars VALUES('SH000300',%s,%s,'{}')", (days[0], did))
        if states:
            did = db.dataset(conn, "security_history", "SH600895", [{"symbol": "SH600895"}], {})
            conn.execute("INSERT INTO security_states VALUES('SH600895',%s,%s,now(),'{}')", (days[-1], did))


def test_source_gaps_names_every_short_endpoint_with_its_counts(db):
    from quant_platform.jobs.scheduler import source_gaps

    days = [now().astimezone(CN).date() - timedelta(days=index) for index in range(4)]
    seed_daily_window(db, days, adjusted={days[0], days[1]}, benchmark=False)
    with db.transaction() as conn:
        gaps = source_gaps(conn, days, 4, 3)
    assert gaps == {"factors": [2, 3], "benchmark": str(days[0])}
    with db.transaction() as conn:
        assert source_gaps(conn, days, 5, 3) == {"sessions": [4, 5]}


def test_source_gaps_accepts_an_adjusted_suffix_shorter_than_the_raw_window(db):
    """Public front-adjusted series are shallower than the raw bars; that is not a blocker."""
    from quant_platform.jobs.scheduler import source_gaps

    days = [now().astimezone(CN).date() - timedelta(days=index) for index in range(4)]
    seed_daily_window(db, days, adjusted={days[0], days[1]}, benchmark=True)
    with db.transaction() as conn:
        assert source_gaps(conn, days, 4, 2) == {}
        # A hole in the middle of the suffix is a real gap: the walk stops at the newest missing day.
        assert source_gaps(conn, days, 4, 3) == {"factors": [2, 3]}


def test_source_gaps_reports_a_listed_security_without_a_recorded_state(db):
    from quant_platform.jobs.scheduler import source_gaps

    days = [now().astimezone(CN).date() - timedelta(days=index) for index in range(3)]
    seed_daily_window(db, days, adjusted=set(days), benchmark=True, states=False)
    with db.transaction() as conn:
        assert source_gaps(conn, days, 3, 3) == {"security_states": "a listed security has no recorded state"}


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
        assert client.post("/api/v1/baskets", json={"name": "one", "members": {"SH600895": 1}}).status_code == 410
        with db.transaction() as conn:
            basket = save_basket(conn, BasketInput(name="Legacy monitoring", members={"SH600895": 1}))
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
        for kind in ("securities", "history", "quotes", "qlib_export"):
            jid = db.enqueue(conn, kind, "test", kind)
            conn.execute("UPDATE jobs SET status='blocked' WHERE id=%s", (jid,))
    feed = Mock()
    # 只有东方财富与通达信可达：依赖新浪/腾讯的能力必须继续阻塞。
    feed.probe.return_value = {
        "pytdx": {"reachable": True, "rows": 5},
        "eastmoney": {"reachable": True, "rows": 5},
        "sina": {"reachable": False, "error_type": "ProviderError"},
        "tencent": {"reachable": False, "error_type": "ProviderError"},
    }
    doctor(db, settings, feed)
    states = {r["kind"]: r["status"] for r in db.rows("SELECT kind,status FROM jobs")}
    assert states == {"securities": "pending", "history": "pending", "quotes": "pending", "qlib_export": "blocked"}
    assert db.setting("doctor")["reachable"] == ["eastmoney", "pytdx"]


def test_doctor_blocks_everything_when_no_source_answers(db, settings):
    seed(db)
    with db.transaction() as conn:
        jid = db.enqueue(conn, "history", "test", "blocked-history")
        conn.execute("UPDATE jobs SET status='blocked' WHERE id=%s", (jid,))
    feed = Mock()
    feed.probe.return_value = {name: {"reachable": False, "error_type": "ProviderError"} for name in
                               ("pytdx", "eastmoney", "sina", "tencent")}
    result = doctor(db, settings, feed)
    assert "history" in result["missing"]
    assert db.rows("SELECT status FROM jobs WHERE kind='history'")[0]["status"] == "blocked"


def test_doctor_fails_out_jobs_of_a_retired_kind(db, settings):
    """令牌时代的主表任务叫 `directory`，原生分析叫 `analyze`/`research`。

    升级后这些种类既没有处理器，也永远不会被解除阻塞，只会一直挂在运维面板上把真正的缺口埋掉。
    """
    from quant_platform.jobs.worker import JOB_KINDS

    with db.transaction() as conn:
        jobs = [
            db.enqueue(conn, kind, "history", f"retired-{kind}")
            for kind in ("directory", "analyze", "research")
        ]
        conn.execute("UPDATE jobs SET status='blocked' WHERE id=ANY(%s)", (jobs,))
    feed = Mock()
    feed.probe.return_value = {"sina": {"reachable": True, "rows": 5}}
    doctor(db, settings, feed)

    for job_id in jobs:
        row = db.rows("SELECT * FROM jobs WHERE id=%s", (job_id,))[0]
        assert row["status"] == "failed" and "retired" in row["error"]
    assert not {"directory", "analyze", "research"} & JOB_KINDS and "securities" in JOB_KINDS


def test_worker_refuses_a_kind_outside_the_declared_vocabulary(db, settings):
    from quant_platform.jobs.worker import JOB_KINDS, Worker

    worker = Worker(db, settings, "history")
    with pytest.raises(ValueError, match="Unknown job kind"):
        worker.execute({"kind": "directory", "id": 1, "payload": {}})
    assert "directory" not in JOB_KINDS


def test_market_report_job_publishes_a_report(db, settings):
    """`POST /market-report` 与工作台的「生成/刷新报告」都排这个任务。

    分派链漏掉它时，两条路径都只会拿到一个以 `Unknown job kind.` 失败的任务 —— 接口存在、
    工作台页面存在、`market_report.publish` 也写好了，但没有任何地方调用它。
    """
    from quant_platform.analysis import market_report
    from quant_platform.jobs.worker import Worker

    day = seed(db)
    with db.transaction() as conn:
        db.enqueue(conn, "market_report", "analysis", "report-dispatch", {"as_of": str(day)})
    Worker(db, settings, "analysis").execute(db.claim("analysis", "tester"))

    rows = db.rows("SELECT * FROM reports WHERE kind=%s AND target=%s", (market_report.KIND, market_report.TARGET))
    assert len(rows) == 1
    assert rows[0]["as_of"] == day and rows[0]["engine"] == market_report.ENGINE


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


def seed_analysis(db, history_rows):
    """Publish a collected window the way ``collect.history`` does: one dated partition per session.

    Dated scopes are what ``collected_through`` reads back to decide whether the requested window was
    already collected, so a fixture that wants a repeat run to skip fetching must look like real
    collection rather than one undated blob. The newest session is deliberately left uncollected.
    """
    today = seed(db)
    with db.transaction() as conn:
        conn.execute("UPDATE instruments SET name='Fixture'")
        # `create_run` 的训练宇宙等于采集范围（默认 `index` = 成分股 ∪ 观察篮子）。这个夹具会建流水线
        # 运行，所以必须声明成分，否则范围为空、连运行都建不出来。
        db.set_setting(conn, "index_members", {"index": "SH000300", "symbols": ["SH600895"], "dated": False})
    target = today - timedelta(days=1)
    rows = [{**r, "day": target - timedelta(days=len(history_rows) - 1 - i)} for i, r in enumerate(history_rows)]
    with db.transaction() as conn:
        for row in rows:
            for exchange in ("SSE", "SZSE"):
                conn.execute(
                    "INSERT INTO calendars VALUES(%s,%s,true,now()) ON CONFLICT DO NOTHING", (exchange, row["day"])
                )
            fid = db.dataset(conn, "factors", str(row["day"]), [row], {})
            conn.execute("INSERT INTO factors VALUES('SH600895',%s,%s,1)", (row["day"], fid))
        for row in rows[:-1]:
            did = db.dataset(conn, "history", str(row["day"]), [row], {})
            conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (row["day"], did, jsonb(row["data"])))
    return target, rows


def test_collection_never_publishes_native_recommendations(db, settings, history_rows):
    from quant_platform.jobs.collect import history

    target, rows = seed_analysis(db, history_rows)
    settings.history_scope = "all"
    feed = Mock()
    feed.settings, feed.db = settings, None
    feed.history.side_effect = lambda code, start, end: {
        "symbol": code,
        "source": "eastmoney",
        "factor_source": "eastmoney",
        "bars": provider_bars(rows),
        "factors": {r["day"]: 1 for r in rows},
    }
    history(db, feed, job(db, "history", payload={"start": str(rows[0]["day"]), "end": str(target)}))
    assert db.setting("analysis_dirty")
    assert db.history("SH600895", target)[-1]["data"]["close"] == rows[-1]["data"]["close"]
    assert not db.rows("SELECT * FROM reports")
    assert not db.rows("SELECT * FROM recommendations")
    assert not db.rows("SELECT * FROM model_portfolios")
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        assert (
            client.post("/api/v1/candidates/1/approve", json={"name": "Rejected", "symbols": ["SH600895"]}).status_code
            == 410
        )


def test_pipeline_retry_pins_all_inputs(db, settings, history_rows):
    from quant_platform.pipeline import create_run, retry_run

    target, rows = seed_analysis(db, history_rows)
    with db.transaction() as conn:
        did = db.dataset(conn, "history", str(target), [rows[-1]], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (target, did, jsonb(rows[-1]["data"])))
        db.set_setting(conn, "analysis_target", str(target))
    run = create_run(db, settings, {"request_key": "retry-pinned"})
    item = db.claim("qlib-data", "first")
    db.defer(item, "interrupted", blocked=True)
    with db.transaction() as conn:
        conn.execute("UPDATE instruments SET name='ST changed',status='U'")
        conn.execute("UPDATE calendars SET is_open=false WHERE day=%s", (target,))
        conn.execute("INSERT INTO scan_policies(data) SELECT data FROM scan_policies LIMIT 1")
        corrected = {**rows[-1]["data"], "close": 100}
        revised = db.dataset(conn, "history", str(target), [corrected], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH600895',%s,%s,%s)", (target, revised, jsonb(corrected)))
    assert retry_run(db, run["id"])["snapshot_reused"]
    retried = db.claim("qlib-data", "replacement")
    assert retried["id"] == item["id"] and retried["fence"] > item["fence"]
    snapshot = db.rows("SELECT snapshot FROM pipeline_runs WHERE id=%s", (run["id"],))[0]["snapshot"]
    assert snapshot == run["snapshot"]
    assert snapshot["watermark"] < revised
    assert (
        db.history("SH600895", target, watermark=snapshot["watermark"])[-1]["data"]["close"]
        == rows[-1]["data"]["close"]
    )
    assert not db.rows("SELECT * FROM recommendations")


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
    item = job(db, "history")
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


@pytest.mark.parametrize("failure", ["deadline", "stop", "output"])
def test_qlib_worker_terminates_its_subprocess(settings, monkeypatch, failure):
    import subprocess
    import sys
    from quant_platform.domain.workflow import PipelineBlocked
    from quant_platform.storage import LostLease

    worker = Worker(Mock(), settings, "qlib-daily")
    processes = []
    original = subprocess.Popen

    def spawn(args, **kwargs):
        process = original(
            [sys.executable, "-c", "import sys,time; sys.stdin.read(); print('x'*2048,flush=True); time.sleep(30)"],
            **kwargs,
        )
        processes.append(process)
        return process

    monkeypatch.setattr("quant_platform.jobs.worker.subprocess.Popen", spawn)
    if failure == "deadline":
        monkeypatch.setattr(worker, "deadline", lambda job: 0)
    elif failure == "stop":
        worker.stop.set()
    else:
        monkeypatch.setattr("quant_platform.jobs.worker.MAX_QLIB_LOG_BYTES", 1024)
    with pytest.raises(LostLease if failure == "stop" else PipelineBlocked):
        worker.qlib_step({"id": 1, "kind": "qlib_infer"})
    assert processes[0].poll() is not None


def test_qlib_worker_preserves_safe_blocker_and_hides_unknown_output(settings, monkeypatch):
    import subprocess
    import sys
    from quant_platform.domain.workflow import PipelineBlocked

    original = subprocess.Popen
    worker = Worker(Mock(), settings, "qlib-daily")
    for exit_code, output, expected in (
        (3, "QUAP_BLOCKED=Missing finalized bars.", "Missing finalized bars"),
        (1, "private connection details", "Qlib engine step failed"),
    ):

        def spawn(args, **kwargs):
            return original(
                [sys.executable, "-c", f"import sys; sys.stdin.read(); print({output!r}); sys.exit({exit_code})"],
                **kwargs,
            )

        monkeypatch.setattr("quant_platform.jobs.worker.subprocess.Popen", spawn)
        with pytest.raises(PipelineBlocked, match=expected) as error:
            worker.qlib_step({"id": 1, "kind": "qlib_infer"})
        assert "private" not in str(error.value)
