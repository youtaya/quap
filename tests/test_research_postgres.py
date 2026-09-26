"""Isolated PostgreSQL research durability, role, and canonical isolation checks."""

from datetime import datetime, timedelta
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import CN
from quant_platform.adapters.qlib.data import seal
from quant_platform.jobs.supplemental import collect, reserve_attempt
from quant_platform.providers import Deferred
from quant_platform.storage import LostLease, jsonb
from quant_platform.storage.baskets import Conflict
from quant_platform.storage.experiments import save_revision, create_experiment, freeze_candidate
from quant_platform.storage.research import change_source, request_collection, control_job
from quant_platform.domain.workflow import DATA_CONTRACT, PipelineBlocked

pytestmark = pytest.mark.postgres


def enable(db, settings, monkeypatch, tmp_path):
    at = datetime(2026, 1, 5, 9, 36, tzinfo=CN)
    monkeypatch.setattr("quant_platform.storage.research.now", lambda: at)
    monkeypatch.setattr("quant_platform.jobs.supplemental.now", lambda: at)
    settings = settings.model_copy(update={"research_data_enabled": True, "artifact_root": tmp_path})
    source = "adata-eastmoney-intraday"
    changed = change_source(
        db, source, {"request_key": "enable", "expected_revision": 0, "enabled": True, "terms_acknowledged": True}
    )
    assert changed["revision"] == 1
    with db.transaction() as conn:
        for exchange in ("SSE", "SZSE"):
            conn.execute("INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, at.date()))
    value = {
        "request_key": "collection",
        "expected_revision": 1,
        "source_id": source,
        "symbols": ["SH600000"],
        "start": str(at.date()),
        "end": str(at.date()),
    }
    return settings, value


def production_counts(db):
    tables = (
        "datasets",
        "daily_bars",
        "minute_bars",
        "factors",
        "security_states",
        "qlib_generations",
        "model_versions",
        "recommendations",
    )
    return {name: db.rows(f"SELECT count(*) AS n FROM {name}")[0]["n"] for name in tables}


def test_research_source_collection_replay_and_production_isolation(db, settings, monkeypatch, tmp_path):
    settings, value = enable(db, settings, monkeypatch, tmp_path)
    before = production_counts(db)
    first = request_collection(db, settings, value)
    assert request_collection(db, settings, value) == first
    with pytest.raises(Conflict):
        request_collection(db, settings, {**value, "symbols": ["SZ000001"]})
    job = db.claim("research-data", "fixture")
    assert db.claim("research-data", "other") is None
    calls = []

    def runner(module, payload, deadline, stop):
        calls.append(payload)
        assert "database" not in str(payload).lower() and "token" not in str(payload).lower()
        return {
            "status": "available",
            "records": [
                {
                    "stock_code": "600000",
                    "trade_time": f"2026-01-05 09:{minute}:00",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10,
                    "volume": 100,
                    "amount": 1000,
                }
                for minute in range(31, 36)
            ],
        }

    collect(db, settings, job, Event(), runner)
    snapshot = db.rows("SELECT * FROM research_snapshots")[0]
    assert snapshot["status"] == "available" and snapshot["row_count"] == 5
    assert snapshot["historical_available_at"] is None
    assert len(calls) == 1 and production_counts(db) == before
    assert db.rows("SELECT status FROM jobs WHERE id=%s", (job["id"],))[0]["status"] == "complete"
    with TestClient(create_app(settings, database=db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        response = client.get(f"/api/v1/research-snapshots/{snapshot['id']}?limit=2")
        assert response.status_code == 200, response.text
        assert len(response.json()["rows"]["items"]) == 2
        assert response.json()["rows"]["next_offset"] == 2
        assert client.get("/api/v1/research-quality").status_code == 200


def test_source_disable_between_collection_and_publication_fails_closed(db, settings, monkeypatch, tmp_path):
    settings, value = enable(db, settings, monkeypatch, tmp_path)
    request_collection(db, settings, value)
    job = db.claim("research-data", "fixture")

    def runner(*args):
        change_source(db, value["source_id"], {"request_key": "disable", "expected_revision": 1, "enabled": False})
        return {"status": "available", "records": []}

    with pytest.raises(PipelineBlocked, match="disabled"):
        collect(db, settings, job, Event(), runner)
    assert not db.rows("SELECT * FROM research_snapshots")
    assert not db.rows("SELECT * FROM research_artifacts")


def test_research_quota_attempt_recovery_and_fenced_cancel(db, settings, monkeypatch, tmp_path):
    settings, value = enable(db, settings, monkeypatch, tmp_path)
    result = request_collection(db, settings, value)
    collection = db.rows("SELECT * FROM research_collections")[0]
    job = db.claim("research-data", "fixture")
    settings = settings.model_copy(update={"research_rpm": 1})
    attempt, cached = reserve_attempt(db, settings, job, collection, "SH600000")
    assert attempt and cached is None
    with pytest.raises(Deferred):
        reserve_attempt(db, settings, job, collection, "SH600000")
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO research_attempt_outcomes VALUES(%s,'available',%s,now())",
            (attempt, jsonb({"status": "available", "records": []})),
        )
    assert reserve_attempt(db, settings, job, collection, "SH600000")[0] is None
    body = {"request_key": "cancel", "expected_revision": job["fence"]}
    cancelled = control_job(db, result["job_id"], body, "cancel")
    assert control_job(db, result["job_id"], body, "cancel") == cancelled
    with pytest.raises(LostLease), db.publication(job):
        pass
    with pytest.raises(Conflict):
        control_job(db, result["job_id"], {"request_key": "stale", "expected_revision": job["fence"]}, "retry")
    control_job(
        db, result["job_id"], {"request_key": "retry", "expected_revision": cancelled["control_revision"]}, "retry"
    )
    assert db.claim("research-data", "replacement")["fence"] > job["fence"]


def test_research_definitions_revisions_and_supplemental_generation_rejected(db, settings):
    value = {
        "name": "Momentum",
        "author": "fixture",
        "expression": "$close/Ref($close,5)-1",
        "expected_revision": 0,
        "request_key": "factor",
    }
    factor = save_revision(db, settings, "factors", value)
    assert save_revision(db, settings, "factors", value) == factor
    with pytest.raises(Conflict):
        save_revision(db, settings, "factors", {**value, "request_key": "stale"})
    members = save_revision(
        db,
        settings,
        "factor-sets",
        {"name": "Daily", "factors": [factor["id"]], "expected_revision": 0, "request_key": "set"},
    )
    config = {
        "name": "Base",
        "frequency": "day",
        "factor_set_id": members["id"],
        "expected_revision": 0,
        "request_key": "base",
    }
    baseline = save_revision(db, settings, "training-configurations", config)
    candidate = save_revision(
        db,
        settings,
        "training-configurations",
        {**config, "name": "Candidate", "request_key": "candidate", "rounds": 20},
    )
    with pytest.raises(PipelineBlocked, match="canonical"):
        create_experiment(
            db,
            settings,
            {
                "generation_id": str(uuid4()),
                "baseline_configuration_id": baseline["id"],
                "candidate_configuration_id": candidate["id"],
                "expected_revision": 0,
                "request_key": "invalid-source",
            },
        )
    assert not db.rows("SELECT * FROM model_versions")


def test_research_api_new_lists_and_role_boundaries(db, settings):
    with TestClient(create_app(settings, database=db)) as client:
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        for path in (
            "research-collections",
            "research-snapshots",
            "research-quality",
            "factors",
            "factor-sets",
            "training-configurations",
            "research-experiments",
        ):
            response = client.get("/api/v1/" + path)
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []
    from psycopg.errors import InsufficientPrivilege

    roles = db.rows("SELECT rolname FROM pg_roles WHERE rolname IN ('quant_read','quant_worker')")
    if len(roles) != 2:
        pytest.skip("Both database roles are required to verify research permissions.")
    for role in roles:
        for table in (
            "research_requests",
            "research_sources",
            "research_artifacts",
            "research_collections",
            "research_snapshots",
            "research_quality_checks",
            "research_request_attempts",
            "research_attempt_outcomes",
            "factor_definitions",
            "factor_sets",
            "factor_set_members",
            "training_configurations",
            "research_experiments",
            "research_trials",
            "research_trial_results",
            "research_freezes",
            "research_test_exposures",
            "diagnostic_inputs",
            "model_diagnostics",
        ):
            permissions = db.rows(
                "SELECT has_table_privilege(%s,%s,'SELECT') AS read,has_table_privilege(%s,%s,'UPDATE') AS update,"
                "has_table_privilege(%s,%s,'INSERT') AS insert,has_table_privilege(%s,%s,'DELETE') AS delete,"
                "has_table_privilege(%s,%s,'TRUNCATE') AS truncate",
                (role["rolname"], table) * 5,
            )[0]
            assert permissions["read"] and not any(permissions[key] for key in ("update", "delete", "truncate"))
            assert permissions["insert"] == (role["rolname"] == "quant_worker")
            with pytest.raises(InsufficientPrivilege), db.transaction() as conn:
                conn.execute("SET LOCAL ROLE " + role["rolname"])
                conn.execute(f"DELETE FROM {table} WHERE false")


def comparison_fixture(db, settings, tmp_path):
    """Synthetic sealed input for worker durability only; the runner is injected."""
    settings = settings.model_copy(update={"artifact_root": tmp_path})
    generation_id = uuid4()
    folder = tmp_path / "generations" / generation_id.hex
    folder.mkdir(parents=True)
    (folder / "fixture.bin").write_bytes(b"durability-fixture-not-qlib-data")
    days = [(datetime(2024, 1, 1) + timedelta(days=i)).date().isoformat() for i in range(180)]
    manifest = seal(folder, {"source": "tushare", "days": days, "market_coverage": 1, "fixture": True})
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) "
            "VALUES(%s,'day',0,now(),%s,%s,%s)",
            (generation_id, DATA_CONTRACT, str(folder.relative_to(tmp_path)), jsonb(manifest)),
        )
    configs = []
    for name in ("Baseline", "Candidate"):
        configs.append(
            save_revision(
                db,
                settings,
                "training-configurations",
                {
                    "name": name,
                    "expected_revision": 0,
                    "request_key": name,
                    "train_sessions": 20,
                    "validation_sessions": 10,
                    "test_sessions": 10,
                    "fixture_windows": True,
                    "rounds": 10,
                },
            )
        )
    value = {
        "request_key": "comparison",
        "expected_revision": 0,
        "generation_id": str(generation_id),
        "baseline_configuration_id": configs[0]["id"],
        "candidate_configuration_id": configs[1]["id"],
    }
    experiment = create_experiment(db, settings, value)
    assert create_experiment(db, settings, value) == experiment
    return settings, experiment, configs[1]["id"]


def fold_result(module, payload, deadline, stop):
    assert module == "quant_platform.research.experiments" and deadline > 0
    assert payload["fold"]["valid"][1] < payload["reserved_test"][0]
    assert "test" not in payload["fold"]
    folder = Path(payload["output"])
    folder.mkdir(parents=True)
    summary = {"research_only": True, "selection_data": "validation_only"}
    (folder / "data.json").write_text(json.dumps(summary))
    return {"summary": summary, "manifest": seal(folder, {"fixture": True})}


def retry_research(db, job, key):
    cancelled = control_job(db, job["id"], {"request_key": key, "expected_revision": job["fence"]}, "cancel")
    value = {"request_key": key + "-retry", "expected_revision": cancelled["control_revision"]}
    retried = control_job(db, job["id"], value, "retry")
    assert control_job(db, job["id"], value, "retry") == retried
    return db.claim(job["queue"], "replacement")


def test_experiment_retry_reuses_folds_and_freezes_only_complete_evidence(db, settings, tmp_path):
    from quant_platform.jobs.experiments import execute
    from quant_platform.storage.experiments import test_exposure as disclose_test

    settings, experiment, candidate = comparison_fixture(db, settings, tmp_path)
    before = production_counts(db)
    freeze = {"request_key": "freeze", "expected_revision": 1, "configuration_id": candidate}
    job = db.claim("qlib-research", "fixture")
    calls = []

    def interrupted(module, payload, deadline, stop):
        calls.append(payload["fold"])
        if len(calls) == 2:
            raise PipelineBlocked("Fixture interrupted trial")
        return fold_result(module, payload, deadline, stop)

    with pytest.raises(PipelineBlocked, match="interrupted"):
        execute(db, settings, job, Event(), interrupted)
    assert len(db.rows("SELECT * FROM research_trials")) == 2
    with pytest.raises(Conflict, match="completed"):
        freeze_candidate(db, experiment["id"], freeze)
    # A terminal job alone is insufficient: all six distinct successful folds are required.
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='complete' WHERE id=%s", (job["id"],))
    with pytest.raises(Conflict, match="six"):
        freeze_candidate(db, experiment["id"], freeze)
    with db.transaction() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE id=%s", (job["id"],))
    replacement = retry_research(db, job, "restart-comparison")
    assert replacement["fence"] > job["fence"]
    with pytest.raises(LostLease), db.publication(job):
        pass
    execute(db, settings, replacement, Event(), fold_result)
    trials = db.rows("SELECT t.*,r.state FROM research_trials t JOIN research_trial_results r ON r.trial_id=t.id")
    assert len(trials) == 7 and sum(t["state"] == "succeeded" for t in trials) == 6
    assert sum(t["fence"] == job["fence"] for t in trials) == 2
    frozen = freeze_candidate(db, experiment["id"], freeze)
    assert freeze_candidate(db, experiment["id"], freeze) == frozen
    assert not frozen["model_activated"] and production_counts(db) == before
    assert not db.rows("SELECT * FROM model_release_events")
    spec = db.rows("SELECT spec,generation_id FROM research_experiments WHERE id=%s", (experiment["id"],))[0]
    snapshot = {"research_freeze_id": frozen["id"], "reserved_test": spec["spec"]["reserved_test"]}
    with db.transaction() as conn:
        training_id = db.enqueue(conn, "qlib_train", "qlib-cpu", "holdout-retry")
    training_job = db.claim("qlib-cpu", "trainer")
    assert training_job["id"] == training_id
    for index in range(2):
        run = {"id": uuid4(), "frequency": "day", "snapshot": snapshot}
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO pipeline_runs(id,request_key,input_hash,frequency,purpose,as_of,snapshot) "
                "VALUES(%s,%s,%s,'day','training',now(),%s)",
                (run["id"], str(run["id"]), str(run["id"]), jsonb(snapshot)),
            )
            db.fence(conn, training_job)
            disclosure = disclose_test(conn, run, spec["generation_id"])
        assert disclosure["previously_exposed"] == bool(index)
        # The same run retains its original disclosure after a new worker fence.
        db.defer(training_job, "Fixture retry", delay=0, transient=True)
        training_job = db.claim("qlib-cpu", "new-trainer")
        with db.transaction() as conn:
            db.fence(conn, training_job)
            assert disclose_test(conn, run, spec["generation_id"]) == disclosure
        with pytest.raises(PipelineBlocked, match="changed"), db.transaction() as conn:
            disclose_test(conn, run, uuid4())
    assert len(db.rows("SELECT * FROM research_test_exposures")) == 2


def diagnostic_fixture(db, settings, tmp_path):
    from quant_platform.jobs.diagnostics import enqueue_diagnostics

    settings = settings.model_copy(update={"artifact_root": tmp_path})
    generation_id, model_id, run_id, prediction_id = (uuid4() for _ in range(4))
    manifests = []
    for identifier, filename in ((generation_id, "fixture.bin"), (model_id, "diagnostic_reference.json")):
        folder = tmp_path / identifier.hex
        folder.mkdir()
        (folder / filename).write_text("{}")
        manifests.append(seal(folder, {"fixture": True}))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) "
            "VALUES(%s,'day',0,now(),'fixture',%s,%s)",
            (generation_id, generation_id.hex, jsonb(manifests[0])),
        )
        conn.execute(
            "INSERT INTO model_versions(id,frequency,generation_id,state,contract,path,metadata) "
            "VALUES(%s,'day',%s,'challenger','fixture',%s,%s)",
            (model_id, generation_id, model_id.hex, jsonb(manifests[1])),
        )
        conn.execute(
            "INSERT INTO pipeline_runs(id,request_key,input_hash,frequency,purpose,as_of,snapshot) "
            "VALUES(%s,%s,%s,'day','prediction',%s,'{}')",
            (run_id, str(run_id), str(run_id), datetime(2026, 1, 5, 15, tzinfo=CN)),
        )
        conn.execute(
            "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,%s)",
            (prediction_id, run_id, model_id, generation_id, jsonb({"scores": {"SH600000": 0.1}})),
        )
        enqueue_diagnostics(db, conn, prediction_id)
    return settings, prediction_id


def test_diagnostics_replay_reuses_verified_drift_and_rejects_corruption(db, settings, tmp_path):
    from quant_platform.jobs.diagnostics import execute, enqueue_diagnostics

    settings, prediction_id = diagnostic_fixture(db, settings, tmp_path)
    before = production_counts(db)
    calls = []

    def runner(*args):
        calls.append(args)
        return {"status": "available", "features": {}, "fixture": True}

    execute(db, settings, db.claim("qlib-diagnostics", "first"), Event(), runner)
    first = db.rows("SELECT * FROM model_diagnostics")[0]
    assert not first["data"]["drift_reused"]
    # A different job carrying the same observation cannot append duplicate evidence.
    with db.transaction() as conn:
        enqueue_diagnostics(db, conn, prediction_id, "duplicate-job")
    execute(db, settings, db.claim("qlib-diagnostics", "duplicate"), Event(), runner)
    assert len(db.rows("SELECT * FROM model_diagnostics")) == 1
    for revision in (0, 1):
        with db.transaction() as conn:
            for exchange in ("SSE", "SZSE"):
                conn.execute(
                    "INSERT INTO calendars VALUES(%s,%s,true,now())", (exchange, datetime(2026, 1, 5 + revision).date())
                )
            enqueue_diagnostics(db, conn, prediction_id, "calendar-" + str(revision))
        job = db.claim("qlib-diagnostics", "revision")
        if revision == 0:
            execute(db, settings, job, Event(), runner)
            latest = db.rows("SELECT * FROM model_diagnostics WHERE id!=%s", (first["id"],))[0]
            assert latest["data"]["drift_reused"] and latest["data"]["drift"] == first["data"]["drift"]
            artifact = db.rows("SELECT * FROM research_artifacts WHERE id=%s", (latest["artifact_id"],))[0]
            (tmp_path / artifact["path"] / "data.json").write_text("corrupt")
        else:
            with pytest.raises(PipelineBlocked, match="checksum"):
                execute(db, settings, job, Event(), runner)
    assert len(calls) == 1 and len(db.rows("SELECT * FROM model_diagnostics")) == 2
    assert production_counts(db) == before


@pytest.mark.parametrize("failure", ["cancel", "publication"])
def test_diagnostic_failure_keeps_observation_and_rejects_partial_publication(
    db, settings, tmp_path, monkeypatch, failure
):
    from quant_platform.jobs import diagnostics

    settings, prediction_id = diagnostic_fixture(db, settings, tmp_path)
    job = db.claim("qlib-diagnostics", "fixture")
    original_publish = diagnostics.publish_artifact
    cancelled = None

    def runner(*args):
        nonlocal cancelled
        if failure == "cancel":
            cancelled = control_job(
                db, job["id"], {"request_key": "cancel-drift", "expected_revision": job["fence"]}, "cancel"
            )
        return {"status": "available", "fixture": True}

    def failed_publication(conn, artifact):
        original_publish(conn, artifact)
        raise RuntimeError("Fixture publication rollback")

    if failure == "publication":
        monkeypatch.setattr(diagnostics, "publish_artifact", failed_publication)
    with pytest.raises(LostLease if failure == "cancel" else RuntimeError):
        diagnostics.execute(db, settings, job, Event(), runner)
    pinned = db.rows("SELECT * FROM diagnostic_inputs WHERE job_id=%s", (job["id"],))[0]
    assert not db.rows("SELECT * FROM model_diagnostics") and not db.rows("SELECT * FROM research_artifacts")
    if cancelled:
        control_job(
            db, job["id"], {"request_key": "retry-drift", "expected_revision": cancelled["control_revision"]}, "retry"
        )
        replacement = db.claim("qlib-diagnostics", "replacement")
    else:
        replacement = retry_research(db, job, "restart-drift")
    monkeypatch.setattr(diagnostics, "publish_artifact", original_publish)
    diagnostics.execute(db, settings, replacement, Event(), lambda *args: {"status": "available", "fixture": True})
    assert db.rows("SELECT * FROM diagnostic_inputs WHERE job_id=%s", (job["id"],))[0] == pinned
    assert len(db.rows("SELECT * FROM model_diagnostics WHERE prediction_id=%s", (prediction_id,))) == 1
    assert len(db.rows("SELECT * FROM research_artifacts")) == 1
    assert not db.rows("SELECT * FROM recommendations")
