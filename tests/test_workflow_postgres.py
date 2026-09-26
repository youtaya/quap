"""Workflow publication, dependency, approval, and API integration boundaries."""

from datetime import datetime, time, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import CN, now
from quant_platform.domain.workflow import Acceptance, PipelineBlocked, PortfolioInput, WatchlistInput
from quant_platform.pipeline import create_run, promote, validate_snapshot
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict
from quant_platform.storage.portfolios import accept, save_portfolio, save_watchlist

pytestmark = pytest.mark.postgres


def seed(db):
    day = now().astimezone(CN).date()
    with db.transaction() as conn:
        for i in range(-70, 5):
            for exchange in ("SSE", "SZSE"):
                conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, day + timedelta(days=i)))
        conn.execute(
            "INSERT INTO instruments VALUES('SH600000','Fixture','Main Board','SSE',%s,NULL,'L','{}',now())",
            (day - timedelta(days=500),),
        )
        db.set_setting(conn, "analysis_target", str(day - timedelta(days=1)))
    return day


def release(db, day, frequency="day", state="active", lineage="fixture-lineage"):
    generation_id, model_id = uuid4(), uuid4()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) VALUES(%s,%s,0,now(),'test',%s,'{}')",
            (generation_id, frequency, str(generation_id)),
        )
        conn.execute(
            "INSERT INTO model_versions(id,frequency,generation_id,state,contract,path,metadata,expires_at) "
            "VALUES(%s,%s,%s,%s,'test',%s,%s,now()+interval '1 day')",
            (
                model_id,
                frequency,
                generation_id,
                state,
                str(model_id),
                jsonb({"workflow_identity": lineage, "training_as_of": now().isoformat()}),
            ),
        )
        conn.execute("INSERT INTO model_evaluations(model_id,data,passed) VALUES(%s,'{}',true)", (model_id,))
    return generation_id, model_id


def proposal(db, settings):
    day = seed(db)
    generation_id, model_id = release(db, day)
    run = create_run(db, settings, {"request_key": "proposal-fixture"})
    prediction_id, report_id = uuid4(), uuid4()
    effective = datetime.combine(day + timedelta(days=1), time(9, 30), CN)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,'{}')",
            (prediction_id, run["id"], model_id, generation_id),
        )
        conn.execute(
            "INSERT INTO recommendations(id,prediction_id,portfolio_revision,policy_revision,frequency,as_of,available_at,effective_from,valid_until,data) "
            "VALUES(%s,%s,0,%s,'day',%s,now(),%s,%s,%s)",
            (
                report_id,
                prediction_id,
                run["snapshot"]["policy_revision"],
                run["as_of"],
                effective,
                effective + timedelta(hours=5),
                jsonb(
                    {
                        "weights": {"SH600000": 0.035},
                        "cash_weight": 0.965,
                        "stocks": [
                            {
                                "symbol": "SH600000",
                                "target_weight": 0.035,
                                "baseline_weight": 0,
                                "action": "add",
                                "score": 0.1,
                            }
                        ],
                    }
                ),
            ),
        )
    value = Acceptance(
        name="Accepted targets",
        expected_revision=0,
        expected_policy_revision=run["snapshot"]["policy_revision"],
        request_key="accept-fixture",
    )
    return report_id, value


def test_recommendation_notifications_dedupe_cooldown_and_ignore_scores(db, settings):
    from quant_platform.domain.workflow import ScanPolicy
    from quant_platform.jobs.notify import recommendation_change

    report_id, _ = proposal(db, settings)
    row = db.rows("SELECT * FROM recommendations WHERE id=%s", (report_id,))[0]
    at = now()
    output = row["data"]
    policy = ScanPolicy().model_dump()
    with db.transaction() as conn:
        assert recommendation_change(db, conn, report_id, "day", None, output, policy, at, row["valid_until"])
        assert not recommendation_change(db, conn, report_id, "day", None, output, policy, at, row["valid_until"])
        cosmetic = {**output, "weights": {"SH600000": 0.037}, "stocks": [{**output["stocks"][0], "score": 99}]}
        assert not recommendation_change(
            db, conn, uuid4(), "day", None, cosmetic, policy, at + timedelta(seconds=301), row["valid_until"]
        )
        changed = {**output, "weights": {"SH600000": 0.045}}
        assert not recommendation_change(
            db, conn, uuid4(), "day", None, changed, policy, at + timedelta(seconds=60), row["valid_until"]
        )
        assert recommendation_change(
            db, conn, uuid4(), "day", None, changed, policy, at + timedelta(seconds=301), row["valid_until"]
        )
    assert len(db.rows("SELECT * FROM alert_outbox")) == 2
    with pytest.raises(RuntimeError), db.transaction() as conn:
        recommendation_change(db, conn, uuid4(), "day", uuid4(), output, policy, at, row["valid_until"])
        raise RuntimeError("Publication rollback")
    assert len(db.rows("SELECT * FROM alert_outbox")) == 2


@pytest.mark.parametrize("mutation", [None, "expired", "superseded", "release", "policy"])
def test_notification_delivery_revalidates_report(db, settings, mutation):
    from quant_platform.domain.workflow import ScanPolicy
    from quant_platform.jobs.notify import deliver, recommendation_change

    report_id, _ = proposal(db, settings)
    row = db.rows("SELECT * FROM recommendations WHERE id=%s", (report_id,))[0]
    with db.transaction() as conn:
        recommendation_change(
            db, conn, report_id, "day", None, row["data"], ScanPolicy().model_dump(), now(), row["valid_until"]
        )
        if mutation == "expired":
            conn.execute(
                "UPDATE recommendations SET available_at=now()-interval '3 days',effective_from=now()-interval '2 days',valid_until=now()-interval '1 day'"
            )
        elif mutation == "superseded":
            conn.execute("UPDATE recommendations SET state='superseded'")
        elif mutation == "release":
            conn.execute("UPDATE model_versions SET state='retired'")
        elif mutation == "policy":
            conn.execute("INSERT INTO scan_policies(data) SELECT data FROM scan_policies LIMIT 1")
        db.enqueue(conn, "notify", "notify", "notify-fixture")
    sent = []
    item = db.claim("notify", "notifier")
    result = deliver(db, settings, item, send=sent.append)
    assert result == {"sent": 0 if mutation else 1, "skipped": 1 if mutation else 0, "undelivered": 0}
    assert db.rows("SELECT progress FROM jobs WHERE id=%s", (item["id"],))[0]["progress"] == {
        "channel": "webhook",
        **result,
    }
    with db.transaction() as conn:
        db.enqueue(conn, "notify", "notify", "notify-repeat")
    assert deliver(db, settings, db.claim("notify", "notifier"), send=sent.append) == {
        "sent": 0,
        "skipped": 0,
        "undelivered": 0,
    }
    event = db.rows("SELECT * FROM alert_outbox")[0]
    assert len(sent) == (0 if mutation else 1)
    assert event["delivered_at"] is not None
    assert event["last_error"] == ("skipped_stale_recommendation" if mutation else None)


def test_shadow_publication_rechecks_challenger_state(db, settings, monkeypatch):
    import sys
    from types import SimpleNamespace
    from quant_platform.adapters.qlib import runtime
    from quant_platform.domain.workflow import ScanPolicy

    day = seed(db)
    _, model_id = release(db, day, state="shadow")
    run = create_run(db, settings, {"request_key": "shadow-race", "purpose": "shadow", "model_id": model_id})
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete' WHERE kind='qlib_prepare'")
    job = db.claim("qlib-daily", "shadow-worker")
    monkeypatch.setattr(runtime, "generation_for", lambda *args: ({"id": uuid4(), "manifest": {}}, "/unused"))
    monkeypatch.setattr(runtime, "safe_artifact", lambda *args: "/unused")
    monkeypatch.setattr(runtime, "now", lambda: datetime.combine(day, time(8), CN))

    def prediction(*args):
        with db.transaction() as conn:
            conn.execute("UPDATE model_versions SET state='retired' WHERE id=%s", (model_id,))
        return {"SH600000": 1}, None, {"policy": ScanPolicy().model_dump(), "threshold": 0}

    monkeypatch.setattr(runtime, "predict_artifact", prediction)
    monkeypatch.setitem(
        sys.modules,
        "quant_platform.adapters.qlib.strategy",
        SimpleNamespace(market_features=lambda *args: {}, proposals=lambda *args, **kwargs: {"weights": {}}),
    )
    with pytest.raises(PipelineBlocked, match="release changed"):
        runtime.observe_shadow(db, settings, run, job)
    assert not db.rows("SELECT * FROM prediction_runs")
    assert not db.rows("SELECT * FROM model_shadow_samples")


def shadow_context(db, settings, monkeypatch, frequency):
    import sys
    from types import SimpleNamespace
    from quant_platform.adapters.qlib import runtime

    report_id, _ = proposal(db, settings)
    day = now().astimezone(CN).date() - timedelta(days=1)
    cutoff = datetime.combine(day, time(15) if frequency == "day" else time(14, 50), CN)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete'")
        conn.execute(
            "UPDATE recommendations SET available_at=%s,effective_from=%s,valid_until=now()+interval '1 day' WHERE id=%s",
            (datetime.combine(day, time(), CN), datetime.combine(day, time(9, 30), CN), report_id),
        )
    generation_id, model_id = release(db, day, frequency, "shadow")
    run = create_run(
        db,
        settings,
        {
            "request_key": "shadow-final",
            "purpose": "shadow",
            "frequency": frequency,
            "model_id": model_id,
            "as_of": cutoff,
        },
    )
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete' WHERE kind='qlib_prepare'")
    job = db.claim("qlib-daily" if frequency == "day" else "qlib-intraday", "shadow-worker")
    monkeypatch.setattr(runtime, "generation_for", lambda *args: ({"id": generation_id, "manifest": {}}, "/unused"))
    monkeypatch.setattr(runtime, "safe_artifact", lambda *args: "/unused")
    monkeypatch.setattr(runtime, "now", lambda: cutoff + timedelta(seconds=60))
    monkeypatch.setattr(
        runtime,
        "predict_artifact",
        lambda *args: (
            {"SH600000": 1},
            None,
            {"policy": run["snapshot"]["policy"], "threshold": 0},
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "quant_platform.adapters.qlib.strategy",
        SimpleNamespace(
            market_features=lambda *args: {},
            proposals=lambda *args, **kwargs: {"weights": {}},
        ),
    )
    return runtime, run, job, generation_id, model_id


@pytest.mark.parametrize("mutation", [None, "missing", "duplicate", "environment", "purpose", "off_grid", "other_day"])
def test_shadow_session_requires_every_expected_cutoff(db, settings, monkeypatch, mutation):
    from quant_platform.pipeline import shadow_cutoffs

    settings.environment = "production"
    runtime, run, job, generation_id, model_id = shadow_context(db, settings, monkeypatch, "5min")
    day = run["as_of"].astimezone(CN).date()
    cutoffs = sorted(shadow_cutoffs(day, "5min"))
    assert len(cutoffs) == 44
    with db.transaction() as conn:
        for index, cutoff in enumerate(cutoffs[:-1]):
            environment, purpose = "production", "shadow"
            if index == 0:
                if mutation == "missing":
                    continue
                if mutation == "duplicate":
                    cutoff = cutoffs[1]
                elif mutation == "environment":
                    environment = "test"
                elif mutation == "purpose":
                    purpose = "inference"
                elif mutation == "off_grid":
                    cutoff += timedelta(seconds=1)
                elif mutation == "other_day":
                    cutoff -= timedelta(days=1)
            prior_id = uuid4()
            conn.execute(
                "INSERT INTO pipeline_runs(id,request_key,input_hash,frequency,purpose,as_of,snapshot,state) "
                "VALUES(%s,%s,%s,'5min',%s,%s,'{}','succeeded')",
                (prior_id, str(prior_id), str(prior_id), purpose, cutoff),
            )
            conn.execute(
                "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,%s)",
                (uuid4(), prior_id, model_id, generation_id, jsonb({"shadow": True, "environment": environment})),
            )
    runtime.observe_shadow(db, settings, run, job)
    sample = db.rows("SELECT * FROM model_shadow_samples WHERE model_id=%s", (model_id,))[0]
    assert sample["healthy"] is (mutation is None)
    assert sample["data"]["expected_observations"] == 44
    assert sample["data"]["observations"] == (44 if mutation is None else 43)
    assert sample["data"]["missing_cutoffs"] == ([] if mutation is None else [cutoffs[0].isoformat()])
    assert len(db.rows("SELECT * FROM recommendations")) == 1
    assert not db.rows("SELECT * FROM alert_outbox")
    result = db.rows("SELECT result FROM pipeline_runs WHERE id=%s", (run["id"],))[0]["result"]
    assert result["published_recommendations"] is False
    assert result["healthy_session"] is (mutation is None)


@pytest.mark.parametrize(
    "frequency,mutation",
    [
        ("day", None),
        ("day", "late"),
        ("day", "off_grid"),
        ("day", "closed"),
        ("5min", "late"),
        ("5min", "off_grid"),
        ("5min", "closed"),
        ("5min", "missing_calendar"),
        ("5min", "policy"),
        ("5min", "lease"),
    ],
)
def test_shadow_failures_never_qualify_a_session(db, settings, monkeypatch, frequency, mutation):
    from quant_platform.storage import LostLease

    runtime, run, job, _, model_id = shadow_context(db, settings, monkeypatch, frequency)
    if mutation == "late":
        monkeypatch.setattr(
            runtime, "now", lambda: run["as_of"] + (timedelta(days=1) if frequency == "day" else timedelta(seconds=121))
        )
    elif mutation == "off_grid":
        run["as_of"] -= timedelta(seconds=1)
    with db.transaction() as conn:
        day = run["as_of"].astimezone(CN).date()
        if mutation == "closed":
            conn.execute("UPDATE calendars SET is_open=false WHERE day=%s", (day,))
        elif mutation == "missing_calendar":
            conn.execute("DELETE FROM calendars WHERE day=%s AND exchange='SZSE'", (day,))
        elif mutation == "policy":
            conn.execute("INSERT INTO scan_policies(data) SELECT data FROM scan_policies LIMIT 1")
        elif mutation == "lease":
            conn.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job["id"],))
    if mutation:
        with pytest.raises(LostLease if mutation == "lease" else PipelineBlocked):
            runtime.observe_shadow(db, settings, run, job)
        assert not db.rows("SELECT * FROM model_shadow_samples WHERE model_id=%s", (model_id,))
        assert not db.rows("SELECT * FROM prediction_runs WHERE model_id=%s", (model_id,))
    else:
        runtime.observe_shadow(db, settings, run, job)
        sample = db.rows("SELECT * FROM model_shadow_samples WHERE model_id=%s", (model_id,))[0]
        assert sample["healthy"] and sample["data"]["observations"] == 1
    assert len(db.rows("SELECT * FROM recommendations")) == 1
    assert not db.rows("SELECT * FROM alert_outbox")


def test_dependencies_and_failed_parent_block_child(db, settings):
    seed(db)
    run = create_run(db, settings, {"request_key": "dependency"})
    assert create_run(db, settings, {"request_key": "dependency"})["id"] == run["id"]
    assert db.claim("qlib-daily", "early") is None
    parent = db.claim("qlib-data", "data")
    db.defer(parent, "missing source", blocked=True)
    assert db.claim("qlib-daily", "still-early") is None
    assert db.rows("SELECT state FROM pipeline_runs WHERE id=%s", (run["id"],))[0]["state"] == "blocked"
    with pytest.raises(Conflict):
        create_run(db, settings, {"request_key": "dependency", "purpose": "training"})


def test_acceptance_preserves_cash_and_idempotency(db, settings):
    report_id, value = proposal(db, settings)
    result = accept(db, report_id, value)
    assert result["data"]["weights"] == {"SH600000": 0.035}
    assert result["data"]["cash_weight"] == pytest.approx(0.965)
    assert result["orders"] is False
    assert accept(db, report_id, value)["reused"] is True
    assert len(db.rows("SELECT * FROM model_portfolio_revisions")) == 1
    with pytest.raises(Conflict):
        accept(db, report_id, value.model_copy(update={"name": "Other"}))


@pytest.mark.parametrize("mutation", ["expired", "policy", "release", "superseded"])
def test_acceptance_rechecks_publication_prerequisites(db, settings, mutation):
    report_id, value = proposal(db, settings)
    with db.transaction() as conn:
        if mutation == "expired":
            conn.execute(
                "UPDATE recommendations SET available_at=now()-interval '3 days',effective_from=now()-interval '2 days',valid_until=now()-interval '1 day'"
            )
        elif mutation == "policy":
            conn.execute("INSERT INTO scan_policies(data) SELECT data FROM scan_policies LIMIT 1")
        elif mutation == "release":
            conn.execute("UPDATE model_versions SET state='retired'")
        else:
            conn.execute("UPDATE recommendations SET state='superseded'")
    with pytest.raises(Conflict):
        accept(db, report_id, value)
    assert not db.rows("SELECT * FROM model_portfolios")


def test_workflow_lineage_shadow_promotion(db):
    day = seed(db)
    _, candidate = release(db, day, "5min", "shadow")
    _, previous = release(db, day, "5min", "retired")
    with db.transaction() as conn:
        for i in range(1, 21):
            conn.execute(
                "INSERT INTO model_shadow_samples VALUES(%s,%s,%s,true)",
                (
                    candidate if i == 1 else previous,
                    day - timedelta(days=i),
                    jsonb(
                        {
                            "environment": "production",
                            "workflow_identity": "fixture-lineage",
                        }
                    ),
                ),
            )
    assert promote(db, candidate, None)["state"] == "active"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE model_versions SET created_at=now()-interval '15 days',state='retired' WHERE id=%s", (candidate,)
        )
    with pytest.raises(Conflict):
        promote(db, candidate, None)


@pytest.mark.parametrize(
    "mutation",
    [
        "nineteen_days",
        "duplicate_day",
        "unhealthy",
        "test_environment",
        "different_lineage",
        "missing_lineage",
        "failed_evaluation",
        "old_evidence",
        "future_evidence",
        "current_evidence",
        "closed",
        "missing_calendar",
        "own_old",
        "own_today",
        "own_missing",
        "shanghai_midnight",
    ],
)
def test_promotion_requires_recent_completed_lineage_and_own_sessions(db, monkeypatch, mutation):
    from quant_platform import pipeline

    day = seed(db)
    _, candidate = release(db, day, "5min", "shadow")
    _, previous = release(db, day, "5min", "retired")
    evidence = {"environment": "production", "workflow_identity": "fixture-lineage"}
    with db.transaction() as conn:
        for i in range(1, 21):
            conn.execute(
                "INSERT INTO model_shadow_samples VALUES(%s,%s,%s,true)",
                (
                    previous,
                    day - timedelta(days=i),
                    jsonb(evidence),
                ),
            )
        own_day = day - timedelta(days=1)
        if mutation == "own_old":
            own_day = day - timedelta(days=61)
        elif mutation in {"own_today", "shanghai_midnight"}:
            own_day = day
        if mutation != "own_missing":
            conn.execute(
                "INSERT INTO model_shadow_samples VALUES(%s,%s,%s,true)", (candidate, own_day, jsonb(evidence))
            )
        if mutation in {"nineteen_days", "duplicate_day", "shanghai_midnight"}:
            conn.execute(
                "DELETE FROM model_shadow_samples WHERE model_id=%s AND day=%s", (previous, day - timedelta(days=20))
            )
        if mutation == "duplicate_day":
            conn.execute(
                "INSERT INTO model_shadow_samples VALUES(%s,%s,%s,true)",
                (candidate, day - timedelta(days=2), jsonb(evidence)),
            )
        elif mutation == "unhealthy":
            conn.execute("UPDATE model_shadow_samples SET healthy=false WHERE day=%s", (day - timedelta(days=20),))
        elif mutation == "test_environment":
            conn.execute(
                'UPDATE model_shadow_samples SET data=data || \'{"environment":"test"}\'::jsonb WHERE model_id=%s',
                (previous,),
            )
        elif mutation == "different_lineage":
            conn.execute(
                'UPDATE model_versions SET metadata=metadata || \'{"workflow_identity":"other"}\'::jsonb WHERE id=%s',
                (previous,),
            )
        elif mutation == "missing_lineage":
            conn.execute("UPDATE model_shadow_samples SET data=data-'workflow_identity'")
        elif mutation == "failed_evaluation":
            conn.execute("UPDATE model_evaluations SET passed=false WHERE model_id=%s", (previous,))
        elif mutation in {"old_evidence", "future_evidence", "current_evidence"}:
            replacement = day + timedelta(
                days={"old_evidence": -61, "future_evidence": 1, "current_evidence": 0}[mutation]
            )
            conn.execute(
                "UPDATE model_shadow_samples SET day=%s WHERE model_id=%s AND day=%s",
                (replacement, previous, day - timedelta(days=20)),
            )
        elif mutation == "closed":
            conn.execute(
                "UPDATE calendars SET is_open=false WHERE day=%s AND exchange='SZSE'", (day - timedelta(days=20),)
            )
        elif mutation == "missing_calendar":
            conn.execute("DELETE FROM calendars WHERE day=%s AND exchange='SZSE'", (day - timedelta(days=20),))
    if mutation == "shanghai_midnight":
        monkeypatch.setattr(pipeline, "now", lambda: datetime.combine(day + timedelta(days=1), time(0, 30), CN))
        assert promote(db, candidate, None)["state"] == "active"
    else:
        with pytest.raises(Conflict):
            promote(db, candidate, None)
        assert db.rows("SELECT state FROM model_versions WHERE id=%s", (candidate,))[0]["state"] == "shadow"
        assert not db.rows("SELECT * FROM model_release_events")


def test_api_read_only_and_legacy_bypass_closed(db, settings):
    seed(db)
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/v1/recommendations").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        for path in (
            "recommendations",
            "data-readiness",
            "models",
            "pipeline-runs",
            "scan-policy",
            "watchlist",
            "model-portfolios",
        ):
            response = client.get("/api/v1/" + path)
            assert response.status_code == 200, response.text
        assert not db.rows("SELECT * FROM jobs")
        assert not db.rows("SELECT * FROM scan_policies")
        assert (
            client.post("/api/v1/candidates/1/approve", json={"name": "Old", "symbols": ["SH600000"]}).status_code
            == 410
        )
        response = client.post(
            "/api/v1/model-portfolios",
            json={"name": "Cash baseline", "weights": {"SH600000": 0.05}, "cash_weight": 0.95},
        )
        assert response.status_code == 201, response.text
        assert client.post("/api/v1/pipeline-runs", json={"request_key": "api-run"}).status_code == 202


def test_publication_detects_pending_portfolio_revision(db, settings):
    day = seed(db)
    with db.transaction() as conn:
        saved = save_portfolio(conn, PortfolioInput(name="Baseline"), effective=now() + timedelta(seconds=10))
        conn.execute(
            "UPDATE model_portfolio_revisions SET effective_from=%s",
            (datetime.combine(day - timedelta(days=3), time(9, 30), CN),),
        )
    run = create_run(db, settings, {"request_key": "pinned-baseline"})
    with db.transaction() as conn:
        save_portfolio(conn, PortfolioInput(name="Changed", expected_revision=1), UUID(saved["id"]))
    with db.transaction() as conn, pytest.raises(PipelineBlocked):
        validate_snapshot(conn, run["snapshot"])


def test_run_alias_idempotency_survives_input_changes(db, settings):
    seed(db)
    first = create_run(db, settings, {"request_key": "original"})
    assert create_run(db, settings, {"request_key": "alias"})["id"] == first["id"]
    with db.transaction() as conn:
        db.dataset(conn, "rt_min", "unrelated", [{"fixture": 1}], {})
    assert create_run(db, settings, {"request_key": "minute-change"})["id"] == first["id"]
    with db.transaction() as conn:
        conn.execute("INSERT INTO scan_policies(data) SELECT data FROM scan_policies LIMIT 1")
    assert create_run(db, settings, {"request_key": "alias"})["id"] == first["id"]
    assert create_run(db, settings, {"request_key": "new-inputs"})["id"] != first["id"]
    with pytest.raises(Conflict):
        create_run(db, settings, {"request_key": "alias", "purpose": "training"})


def test_snapshot_pins_calendar_and_pool_inputs(db, settings):
    day = seed(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO universe_snapshots(id,day,content_hash,data) VALUES(%s,%s,'pool-test',%s)",
            (uuid4(), day - timedelta(days=1), jsonb({"kind": "intraday", "symbols": ["SH600000"]})),
        )
    cutoff = datetime.combine(day - timedelta(days=1), time(10), CN)
    run = create_run(db, settings, {"request_key": "pinned", "frequency": "5min", "as_of": cutoff})
    with db.transaction() as conn:
        conn.execute("UPDATE calendars SET is_open=false")
        conn.execute("UPDATE universe_snapshots SET data='{}'")
    pinned = db.rows("SELECT snapshot FROM pipeline_runs WHERE id=%s", (run["id"],))[0]["snapshot"]
    assert all(row["is_open"] for row in pinned["calendars"])
    assert pinned["pools"][0]["data"]["symbols"] == ["SH600000"]


def test_portfolio_and_watchlist_share_capacity_limit(db):
    day = seed(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO instruments VALUES('SH600001','Fixture 2','Main Board','SSE',%s,NULL,'L','{}',now())", (day,)
        )
    save_watchlist(db, WatchlistInput(symbols=["SH600000"]), 1)
    with db.transaction() as conn, pytest.raises(Conflict, match="capacity"):
        save_portfolio(conn, PortfolioInput(name="Too many", weights={"SH600001": 0.05}, cash_weight=0.95), capacity=1)
    assert not db.rows("SELECT * FROM model_portfolios")


def test_intraday_acceptance_and_reads_recheck_daily_release(db, settings):
    daily_id, _ = proposal(db, settings)
    baseline = db.rows("SELECT * FROM recommendations WHERE id=%s", (daily_id,))[0]
    generation_id, minute_model = release(db, now().date(), "5min")
    cutoff = datetime.combine(now().astimezone(CN).date() - timedelta(days=1), time(10), CN)
    run = create_run(db, settings, {"request_key": "minute", "frequency": "5min", "as_of": cutoff})
    prediction_id, report_id = uuid4(), uuid4()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE pipeline_runs SET snapshot=%s WHERE id=%s",
            (jsonb({**run["snapshot"], "daily_baseline": baseline}), run["id"]),
        )
        conn.execute(
            "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,'{}')",
            (prediction_id, run["id"], minute_model, generation_id),
        )
        conn.execute(
            "INSERT INTO recommendations(id,prediction_id,portfolio_revision,policy_revision,frequency,as_of,available_at,effective_from,valid_until,data) "
            "VALUES(%s,%s,0,%s,'5min',now(),now(),now()+interval '5 minutes',now()+interval '7 minutes',%s)",
            (report_id, prediction_id, baseline["policy_revision"], jsonb(baseline["data"])),
        )
        conn.execute("UPDATE model_versions SET state='retired' WHERE frequency='day'")
    with pytest.raises(Conflict, match="baseline"):
        accept(
            db,
            report_id,
            Acceptance(
                name="Invalid overlay",
                expected_revision=0,
                expected_policy_revision=baseline["policy_revision"],
                request_key="invalid-overlay",
            ),
        )
    with TestClient(create_app(settings, db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        assert client.get("/api/v1/recommendations?frequency=5min").json() == []
        detail = client.get(f"/api/v1/recommendations/{report_id}").json()
        assert detail["valid"] is False and detail["acceptance_open"] is False


def scheduled_sources(db, settings):
    day = seed(db)
    settings.history_sessions = 120
    settings.minute_history_sessions = 20
    target = day - timedelta(days=1)
    with db.transaction() as conn:
        for index in range(120):
            source_day = target - timedelta(days=index)
            for exchange in ("SSE", "SZSE"):
                conn.execute(
                    "INSERT INTO calendars VALUES(%s,%s,true,now()) ON CONFLICT DO NOTHING", (exchange, source_day)
                )
            for endpoint in ("daily", "adj_factor", "constraints"):
                db.dataset(conn, endpoint, str(source_day), [{"day": str(source_day)}], {})
        did = db.dataset(conn, "index_daily", "SH000300", [{"day": str(target)}], {})
        conn.execute("INSERT INTO daily_bars VALUES('SH000300',%s,%s,'{}')", (target, did))
        did = db.dataset(conn, "namechange", "SH600000", [{"start": str(target)}], {})
        conn.execute("INSERT INTO security_states VALUES('SH600000',%s,%s,now(),'{}')", (target, did))
    return day


def test_scheduler_uses_scoped_sources_and_reacts_to_release_changes(db, settings):
    from quant_platform.jobs.scheduler import schedule_qlib

    day = scheduled_sources(db, settings)
    at = datetime.combine(day, time(8), CN)
    with db.transaction() as conn:
        jid = db.enqueue(conn, "daily", "history", "obsolete-failure", {"day": "2000-01-01"})
        conn.execute("UPDATE jobs SET status='failed' WHERE id=%s", (jid,))
    schedule_qlib(db, settings, at)
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='day'")) == 2
    with db.transaction() as conn:
        db.dataset(conn, "rt_min", "unrelated", [1], {})
    schedule_qlib(db, settings, at + timedelta(minutes=1))
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='day'")) == 2
    release(db, day)
    schedule_qlib(db, settings, at + timedelta(minutes=2))
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='day' AND purpose='inference'")) == 2
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='day' AND purpose='training'")) == 1


def test_scheduler_blocks_incomplete_sources_before_snapshotting(db, settings):
    from quant_platform.jobs.scheduler import schedule_qlib

    day = seed(db)
    schedule_qlib(db, settings, datetime.combine(day, time(8), CN))
    assert not db.rows("SELECT * FROM pipeline_runs")
    assert "incomplete" in db.setting("pipeline_blocker:day")["reason"]


def research_pools(db, settings, day):
    from quant_platform.domain import digest
    from quant_platform.pipeline import policy

    _, model_id = release(db, day, state="retired")
    days = [day - timedelta(days=index) for index in range(settings.minute_history_sessions, 0, -1)]
    with db.transaction() as conn:
        revision = policy(conn)["revision"]
        for index, pool_day in enumerate(days):
            code = "SH600000" if index < 2 else "SH600001"
            data = {
                "kind": "research_intraday",
                "out_of_sample": True,
                "ready": True,
                "symbols": [code],
                "daily_weights": {code: 0.035},
                "cash_weight": 0.965,
                "daily_model_id": str(model_id),
                "policy_revision": revision,
            }
            conn.execute(
                "INSERT INTO universe_snapshots(id,day,content_hash,data) VALUES(%s,%s,%s,%s)",
                (uuid4(), pool_day, digest({"day": pool_day, **data}), jsonb(data)),
            )
    return days, revision, model_id


def test_research_collection_tracks_dated_membership_and_exact_jobs(db, settings, monkeypatch):
    from unittest.mock import Mock
    from quant_platform.jobs.scheduler import schedule_research_minutes

    day = scheduled_sources(db, settings)
    days, revision, _ = research_pools(db, settings, day)
    at = datetime.combine(day, time(8), CN)
    state = schedule_research_minutes(db, settings, days[-1], revision, at)
    assert not state["ready"] and state["completed_jobs"] == 0
    jobs = db.rows("SELECT * FROM jobs WHERE kind='minute_history'")
    assert {j["payload"]["symbols"][0] for j in jobs} == {"SH600000", "SH600001"}
    early = [j for j in jobs if j["payload"]["symbols"] == ["SH600000"]]
    assert max(datetime.fromisoformat(j["payload"]["end"]).date() for j in early) < days[-1]
    later = [j for j in jobs if j["payload"]["symbols"] == ["SH600001"]]
    assert min(datetime.fromisoformat(j["payload"]["start"]).date() for j in later) <= days[0]
    enqueue = Mock(wraps=db.enqueue)
    monkeypatch.setattr(db, "enqueue", enqueue)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete' WHERE kind='minute_history'")
        unrelated = db.enqueue(conn, "minute_history", "minute-history", "unrelated-failure")
        conn.execute("UPDATE jobs SET status='failed' WHERE id=%s", (unrelated,))
    enqueue.reset_mock()
    assert schedule_research_minutes(db, settings, days[-1], revision, at)["ready"]
    enqueue.assert_not_called()
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='failed' WHERE id=%s", (jobs[0]["id"],))
    assert not schedule_research_minutes(db, settings, days[-1], revision, at)["ready"]


@pytest.mark.parametrize(
    "mutation", ["live", "in_sample", "unready", "policy", "evaluation", "minute_model", "missing_weights"]
)
def test_training_snapshot_excludes_unqualified_research_pools(db, settings, mutation):
    from quant_platform.pipeline import historical_pools

    day = scheduled_sources(db, settings)
    days, revision, model_id = research_pools(db, settings, day)
    with db.transaction() as conn:
        if mutation == "evaluation":
            conn.execute("UPDATE model_evaluations SET passed=false WHERE model_id=%s", (model_id,))
        elif mutation == "minute_model":
            conn.execute("UPDATE model_versions SET frequency='5min' WHERE id=%s", (model_id,))
        elif mutation == "missing_weights":
            conn.execute("UPDATE universe_snapshots SET data=data-'daily_weights'")
        else:
            change = {
                "live": {"kind": "intraday"},
                "in_sample": {"out_of_sample": False},
                "unready": {"ready": False},
                "policy": {"policy_revision": revision + 1},
            }[mutation]
            conn.execute("UPDATE universe_snapshots SET data=data || %s", (jsonb(change),))
        assert historical_pools(conn, days[-1], revision) == []
    run = create_run(
        db,
        settings,
        {
            "request_key": "training-pools",
            "purpose": "training",
            "frequency": "5min",
            "as_of": datetime.combine(days[-1], time(15), CN),
        },
    )
    assert run["snapshot"]["pools"] == []


def test_scheduler_periodic_training_reuses_requests_and_recovers_changed_inputs(db, settings):
    from quant_platform.jobs.scheduler import schedule_qlib

    day = scheduled_sources(db, settings)
    at = datetime.combine(day, time(8), CN)
    with db.transaction() as conn:
        db.dataset(conn, "stk_mins", "history", [1], {})
    schedule_qlib(db, settings, at)
    assert not db.rows("SELECT * FROM pipeline_runs WHERE frequency='5min'")
    assert not db.setting("research_minute_readiness")["ready"]
    research_pools(db, settings, day)
    schedule_qlib(db, settings, at + timedelta(minutes=1))
    assert not db.rows("SELECT * FROM pipeline_runs WHERE frequency='5min'")
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete' WHERE kind='minute_history'")
    schedule_qlib(db, settings, at + timedelta(minutes=2))
    runs = db.rows("SELECT * FROM pipeline_runs WHERE frequency='5min' AND purpose='training'")
    assert len(runs) == 1
    assert runs[0]["snapshot"]["pools"][0]["data"]["daily_weights"] == {"SH600000": 0.035}
    schedule_qlib(db, settings, at + timedelta(minutes=3))
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='5min'")) == 1
    with db.transaction() as conn:
        conn.execute("UPDATE pipeline_runs SET state='blocked' WHERE id=%s", (runs[0]["id"],))
        db.dataset(conn, "stk_mins", "history", [2], {})
    schedule_qlib(db, settings, at + timedelta(minutes=4))
    assert len(db.rows("SELECT * FROM pipeline_runs WHERE frequency='5min'")) == 2


def test_scheduler_does_not_repeat_bulk_enqueue_work(db, settings, monkeypatch):
    from unittest.mock import Mock
    from quant_platform.jobs.scheduler import tick

    day = seed(db)
    at = datetime.combine(day, time(8, 45), CN)
    with db.transaction() as conn:
        db.set_setting(conn, "directory", {"fetched_at": at.isoformat()})
        conn.execute(
            "INSERT INTO instruments VALUES('SH600001','Delisted','Main Board','SSE',%s,%s,'D','{}',now())",
            (day - timedelta(days=500), day - timedelta(days=10)),
        )
    enqueue = Mock(wraps=db.enqueue)
    monkeypatch.setattr(db, "enqueue", enqueue)
    tick(db, settings, at)
    assert len(db.rows("SELECT * FROM jobs WHERE kind='security_history'")) == 2
    enqueue.reset_mock()
    tick(db, settings, at + timedelta(seconds=5))
    assert not [call for call in enqueue.call_args_list if call.args[1] in {"daily", "security_history", "benchmark"}]


def test_frozen_pool_uses_only_revisions_known_at_session_open(db, settings):
    from quant_platform.jobs.market import frozen_pool

    day = seed(db)
    opened = datetime.combine(day, time(9, 30), CN)
    portfolio_id = uuid4()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO watchlist_revisions(symbols,created_at) VALUES(%s,%s),(%s,%s)",
            (jsonb(["SH600000"]), opened - timedelta(minutes=1), jsonb(["SH600001"]), opened + timedelta(minutes=1)),
        )
        conn.execute("INSERT INTO model_portfolios(id,name,revision) VALUES(%s,'Fixture',2)", (portfolio_id,))
        for revision, code, created in (
            (1, "SZ000001", opened - timedelta(days=1)),
            (2, "SZ000002", opened + timedelta(minutes=1)),
        ):
            conn.execute(
                "INSERT INTO model_portfolio_revisions VALUES(%s,%s,%s,%s,%s)",
                (portfolio_id, revision, opened, jsonb({"weights": {code: 0.05}, "cash_weight": 0.95}), created),
            )
    pool = frozen_pool(db, settings, day)
    assert pool["symbols"] == ["SH600000", "SZ000001"]
    assert pool["watchlist_revision"] == 1
    assert pool["portfolio_revisions"] == {str(portfolio_id): 1}
    with db.transaction() as conn:
        conn.execute("INSERT INTO watchlist_revisions(symbols) VALUES('[]')")
    assert frozen_pool(db, settings, day) == pool


def test_minute_history_chunks_have_stable_closed_month_boundaries():
    from datetime import date
    from quant_platform.jobs.scheduler import minute_ranges

    days = [date(2026, 8, 20), date(2026, 8, 21), date(2026, 9, 1), date(2026, 9, 2)]
    first = minute_ranges(days, date(2026, 9, 3))
    second = minute_ranges(days[1:] + [date(2026, 9, 3)], date(2026, 9, 4))
    assert first[0] == (date(2026, 8, 1), date(2026, 8, 31))
    assert set(first).issubset(second)


@pytest.fixture
def pg_tools(monkeypatch, postgres_url):
    import os
    import shutil
    import subprocess
    from pathlib import Path
    from psycopg.conninfo import conninfo_to_dict

    if shutil.which("pg_dump") and shutil.which("pg_restore"):
        return
    container = os.environ.get("QUANT_TEST_PG_CONTAINER")
    if not container:
        pytest.skip("PostgreSQL client binaries or QUANT_TEST_PG_CONTAINER are required")
    execute = subprocess.run
    source_name = conninfo_to_dict(postgres_url)["dbname"]

    def run(args, **kwargs):
        if args[0] == "pg_dump":
            path = Path(args[args.index("--file") + 1])
            forwarded = args[1 : args.index("--file")]
            result = execute(
                ["docker", "exec", container, "pg_dump", "-U", "postgres", "-d", source_name, *forwarded],
                capture_output=True,
                timeout=600,
            )
            if result.returncode == 0:
                path.write_bytes(result.stdout)
            return result
        if args[0] == "pg_restore":
            return execute(
                ["docker", "exec", "-i", container, "pg_restore", "-U", "postgres", *args[1:-1]],
                input=Path(args[-1]).read_bytes(),
                capture_output=True,
                timeout=600,
            )
        return execute(args, **kwargs)

    monkeypatch.setattr("quant_platform.storage.artifacts.subprocess.run", run)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_manifest",
        "duplicate",
        "traversal",
        "absolute",
        "symlink",
        "hardlink",
        "directory",
        "checksum",
        "invalid_json",
        "manifest_list",
        "files_list",
        "artifacts_list",
        "missing_dump",
        "unlisted",
        "unsupported",
        "corrupt_tar",
        "timestamp",
        "naive_timestamp",
        "digest_type",
        "artifact_path",
        "artifact_manifest",
    ],
)
def test_invalid_backup_never_restores_database(postgres_url, tmp_path, monkeypatch, mutation):
    import hashlib
    import io
    import json
    import tarfile
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from unittest.mock import Mock
    from quant_platform.storage import artifacts

    body = b"fixture-dump-not-executable"
    manifest = {
        "format": "quap-snapshot-v1",
        "snapshot_at": now().isoformat(),
        "artifacts": {},
        "files": {"database.dump": hashlib.sha256(body).hexdigest()},
    }
    entries = [("database.dump", body, tarfile.REGTYPE)]
    if mutation == "duplicate":
        entries.append(entries[0])
    elif mutation in {"traversal", "absolute", "unlisted", "unsupported"}:
        name = {
            "traversal": "../escape",
            "absolute": "/escape",
            "unlisted": "artifacts/extra",
            "unsupported": "unknown.bin",
        }[mutation]
        entries.append((name, body, tarfile.REGTYPE))
        if mutation != "unlisted":
            manifest["files"][name] = hashlib.sha256(body).hexdigest()
    elif mutation in {"symlink", "hardlink", "directory"}:
        entries.append(
            (
                "artifacts/unsafe",
                b"",
                {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "directory": tarfile.DIRTYPE}[mutation],
            )
        )
    elif mutation == "checksum":
        manifest["files"]["database.dump"] = "0" * 64
    elif mutation == "manifest_list":
        manifest = []
    elif mutation in {"files_list", "artifacts_list"}:
        manifest[mutation.split("_")[0]] = []
    elif mutation == "missing_dump":
        entries = []
        manifest["files"] = {}
    elif mutation in {"timestamp", "naive_timestamp"}:
        manifest["snapshot_at"] = "not-a-date" if mutation == "timestamp" else "2026-09-24T10:00:00"
    elif mutation == "digest_type":
        manifest["files"]["database.dump"] = ["0" * 64]
    elif mutation == "artifact_path":
        manifest["artifacts"] = {"../escape": {"files": {}}}
    elif mutation == "artifact_manifest":
        manifest["artifacts"] = {"models/fixture": []}
    archive = tmp_path / "corrupt.tar"
    with tarfile.open(archive, "w") as bundle:
        if mutation != "missing_manifest":
            entries.append(
                (
                    "manifest.json",
                    b"{broken" if mutation == "invalid_json" else json.dumps(manifest).encode(),
                    tarfile.REGTYPE,
                )
            )
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.type, info.size = kind, len(content)
            if kind in {tarfile.SYMTYPE, tarfile.LNKTYPE}:
                info.linkname = "../../escape"
            bundle.addfile(info, io.BytesIO(content))
    if mutation == "corrupt_tar":
        archive.write_bytes(b"not a tar archive")
    restore = Mock(side_effect=AssertionError("Malformed archives must not reach pg_restore"))
    monkeypatch.setattr(artifacts.subprocess, "run", restore)
    target_name = "quant_restore_" + uuid4().hex
    target_dsn = make_conninfo(postgres_url, dbname=target_name)
    destination = tmp_path / "restored"
    with psycopg.connect(postgres_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
        try:
            with pytest.raises(PipelineBlocked):
                artifacts.restore_check(archive, target_dsn, artifact_root=destination)
            restore.assert_not_called()
            assert not destination.exists() and not (tmp_path / "escape").exists()
            with psycopg.connect(target_dsn) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'"
                    ).fetchone()[0]
                    == 0
                )
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_name)))


def test_backup_restores_report_and_exact_artifacts_from_one_snapshot(
    db, settings, postgres_url, tmp_path, monkeypatch, pg_tools
):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    from quant_platform.adapters.qlib.data import seal
    from quant_platform.storage import artifacts
    from quant_platform.storage.research import store_artifact, publish_artifact

    from pydantic import SecretStr

    settings.database_url = SecretStr(postgres_url)
    settings.artifact_root = tmp_path / "source"
    settings.backup_root = tmp_path / "backups"
    report_id, _ = proposal(db, settings)
    recorder = settings.artifact_root / "experiments" / "123" / uuid4().hex
    recorder.mkdir(parents=True)
    (recorder / "evidence.json").write_text('{"fixture":true}')
    recorder_ref = {
        "path": recorder.relative_to(settings.artifact_root).as_posix(),
        "manifest": seal(recorder, {"fixture": True}),
    }
    research = [
        store_artifact(
            settings, kind, {"fixture_kind": kind}, {"recorder": recorder_ref} if kind == "experiment" else {}
        )
        for kind in ("snapshot", "experiment", "diagnostics")
    ]
    with db.transaction() as conn:
        for artifact in research:
            publish_artifact(conn, artifact)
        for table, column in (("qlib_generations", "manifest"), ("model_versions", "metadata")):
            row = conn.execute(f"SELECT * FROM {table}").fetchone()
            folder = settings.artifact_root / row["path"]
            folder.mkdir(parents=True)
            (folder / "fixture.bin").write_bytes(b"immutable-fixture")
            if table == "model_versions":
                (folder / "diagnostic_reference.json").write_text('{"training_only":true}')
            manifest = seal(folder, {"fixture": True})
            conn.execute(
                sql.SQL("UPDATE {} SET {}=%s WHERE id=%s").format(sql.Identifier(table), sql.Identifier(column)),
                (jsonb(manifest), row["id"]),
            )
        db.enqueue(conn, "backup", "backup-test", "consistent-backup")
    item = db.claim("backup-test", "backup-owner")
    execute = artifacts.subprocess.run

    def concurrent_publication(args, **kwargs):
        if args[0] == "pg_dump":
            with db.transaction() as conn:
                publish_artifact(conn, store_artifact(settings, "diagnostics", {"after_snapshot": True}, {}))
                conn.execute(
                    "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) "
                    "VALUES(%s,'day',0,now(),'test','published-after-snapshot','{}')",
                    (uuid4(),),
                )
        return execute(args, **kwargs)

    monkeypatch.setattr(artifacts.subprocess, "run", concurrent_publication)
    state = artifacts.backup(db, settings, item)
    assert state["artifact_count"] == 6 and not state["restore_verified"]
    assert len(db.rows("SELECT * FROM qlib_generations")) == 2
    target_name = "quant_restore_" + uuid4().hex
    target_dsn = make_conninfo(postgres_url, dbname=target_name)
    with psycopg.connect(postgres_url, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target_name)))
        try:
            destination = tmp_path / "restored"
            result = artifacts.restore_check(
                settings.backup_root / state["file"], target_dsn, db, artifact_root=destination
            )
            assert result["artifact_count"] == 6 and result["recommendations"] == 1
            assert db.setting("backup")["restore_verified"]
            with psycopg.connect(target_dsn) as restored:
                assert restored.execute("SELECT id FROM recommendations").fetchone()[0] == report_id
                assert restored.execute("SELECT count(*) FROM qlib_generations").fetchone()[0] == 1
                assert restored.execute("SELECT count(*) FROM research_artifacts").fetchone()[0] == 3
            for artifact in research:
                artifacts.verify(destination / artifact["path"], artifact["manifest"])
            artifacts.verify(destination / recorder_ref["path"], recorder_ref["manifest"])
            assert len(list(destination.rglob("diagnostic_reference.json"))) == 1
            assert len(list(destination.rglob("fixture.bin"))) == 2
            assert all(path.read_bytes() == b"immutable-fixture" for path in destination.rglob("fixture.bin"))
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_name)))


def test_retention_prunes_only_abandoned_owned_recorders(db, settings, tmp_path):
    import json
    import os
    from quant_platform.storage.artifacts import prune_orphans

    settings.artifact_root = tmp_path
    day = seed(db)
    _, model_id = release(db, day)
    kinds = ("referenced", "backup", "orphan", "fresh", "unowned", "malformed", "wrong_owner", "marker_link")
    folders = {kind: tmp_path / "experiments" / "123" / uuid4().hex for kind in kinds}
    ancient = (now() - timedelta(days=10)).timestamp()
    for kind, folder in folders.items():
        folder.mkdir(parents=True)
        (folder / "artifact.bin").write_bytes(b"fixture")
        marker = folder / "quap-owner.json"
        if kind == "malformed":
            marker.write_text("{")
        elif kind == "marker_link":
            marker.symlink_to(folders["referenced"] / "quap-owner.json")
        elif kind != "unowned":
            marker.write_text(
                json.dumps(
                    {
                        "format": "other-owner" if kind == "wrong_owner" else "quap-qlib-recorder-v1",
                        "recorder_id": folder.name,
                    }
                )
            )
        if kind != "fresh":
            os.utime(folder, (ancient, ancient))
    experiment = tmp_path / "experiments" / "123"
    (experiment / "meta.yaml").write_text("name: quap-day\n")
    linked = experiment / uuid4().hex
    linked.symlink_to(folders["unowned"], target_is_directory=True)
    (tmp_path / "experiments" / "456").symlink_to(experiment, target_is_directory=True)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE model_versions SET metadata=metadata || %s WHERE id=%s",
            (
                jsonb({"recorder": {"path": folders["referenced"].relative_to(tmp_path).as_posix(), "manifest": {}}}),
                model_id,
            ),
        )
        conn.execute(
            "INSERT INTO artifact_backups(id,data) VALUES(%s,%s)",
            (
                uuid4(),
                jsonb({"artifacts": [folders["backup"].relative_to(tmp_path).as_posix()]}),
            ),
        )
        running = db.enqueue(conn, "qlib_train", "training", "recorder-owner")
        conn.execute("UPDATE jobs SET status='running' WHERE id=%s", (running,))
        assert prune_orphans(conn, settings, now()) == 0
        conn.execute("UPDATE jobs SET status='failed' WHERE id=%s", (running,))
        assert prune_orphans(conn, settings, now()) == 1
        assert prune_orphans(conn, settings, now()) == 0
    assert not folders["orphan"].exists()
    assert all(folder.is_dir() for kind, folder in folders.items() if kind != "orphan")
    assert (experiment / "meta.yaml").is_file() and linked.is_symlink()


def test_maintenance_retains_reachable_artifacts_and_workflow_jobs(db, settings, tmp_path):
    import os
    from quant_platform.operations import maintenance
    from quant_platform.storage.artifacts import prune_orphans

    seed(db)
    run = create_run(db, settings, {"request_key": "retained-run"})
    settings.artifact_root = tmp_path
    kept, orphan, backup_only = ("qlib/generation-" + uuid4().hex for _ in range(3))
    ancient = (now() - timedelta(days=10)).timestamp()
    for name in (kept, orphan, backup_only):
        folder = tmp_path / name
        folder.mkdir(parents=True)
        (folder / "fixture").write_text("retained")
        os.utime(folder, (ancient, ancient))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) VALUES(%s,'day',0,now(),'test',%s,'{}')",
            (uuid4(), kept),
        )
        conn.execute(
            "INSERT INTO artifact_backups(id,data) VALUES(%s,%s)", (uuid4(), jsonb({"artifacts": [backup_only]}))
        )
        conn.execute("UPDATE jobs SET status='running' WHERE kind='qlib_prepare'")
        assert prune_orphans(conn, settings, now()) == 0
        conn.execute("UPDATE jobs SET status='complete',updated_at=now()-interval '40 days'")
        old = db.enqueue(conn, "quotes", "old", "old-unreferenced")
        conn.execute("UPDATE jobs SET status='complete',updated_at=now()-interval '40 days' WHERE id=%s", (old,))
        db.enqueue(conn, "maintenance", "maintenance-test", "retention")
    item = db.claim("maintenance-test", "maintainer")
    maintenance(db, settings, item)
    assert (tmp_path / kept).is_dir() and (tmp_path / backup_only).is_dir()
    assert not (tmp_path / orphan).exists()
    assert len(db.rows("SELECT * FROM pipeline_steps WHERE run_id=%s", (run["id"],))) == 2
    assert not db.rows("SELECT * FROM jobs WHERE id=%s", (old,))
    assert len(db.rows("SELECT tablename FROM pg_tables WHERE tablename ~ '^minute_bars_[0-9]{8}$'")) >= 3
