"""Persistent, fenced Qlib run graph and release/readiness control plane."""

from datetime import datetime, time, timedelta
from uuid import uuid4

from quant_platform.domain import CN, digest, now
from quant_platform.domain.workflow import DATA_CONTRACT, ENGINE_VERSION, PipelineBlocked, RunInput, ScanPolicy
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict

DAILY_CAPABILITIES = {
    "stock_basic",
    "trade_cal",
    "daily",
    "adj_factor",
    "index_daily",
    "namechange",
    "stk_limit",
    "suspend_d",
}
MINUTE_CAPABILITIES = {"stk_mins", "rt_min", "rt_min_daily"}
DAILY_DATASETS = {"stock_basic", "trade_cal", "daily", "adj_factor", "index_daily", "namechange", "constraints"}
VALID_REPORT = (
    "r.state='published' AND r.available_at<=now() AND r.valid_until>now() "
    "AND m.state='active' AND m.expires_at>now() AND e.passed "
    "AND r.policy_revision=(SELECT max(revision) FROM scan_policies) "
    "AND (r.portfolio_id IS NULL OR r.portfolio_revision=(SELECT revision FROM model_portfolios WHERE id=r.portfolio_id))"
)


def policy(conn):
    row = conn.execute("SELECT * FROM scan_policies ORDER BY revision DESC LIMIT 1").fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO scan_policies(data) VALUES(%s) RETURNING *", (jsonb(ScanPolicy().model_dump()),)
        ).fetchone()
    return row


def next_session(conn, after):
    rows = conn.execute(
        "SELECT day,bool_and(is_open) AS open,count(*) AS n FROM calendars "
        "WHERE exchange IN ('SSE','SZSE') AND day>%s GROUP BY day ORDER BY day LIMIT 30",
        (after,),
    ).fetchall()
    expected = after + timedelta(days=1)
    for row in rows:
        if row["day"] != expected or row["n"] != 2:
            raise PipelineBlocked("Next-session calendar coverage is incomplete.")
        if row["open"]:
            return row["day"]
        expected += timedelta(days=1)
    raise PipelineBlocked("Verified next trading session unavailable.")


def active_model(conn, frequency):
    return conn.execute(
        "SELECT m.*,e.passed,e.data AS evaluation FROM model_versions m JOIN model_evaluations e ON e.model_id=m.id "
        "WHERE frequency=%s AND state='active' AND expires_at>now() AND e.passed",
        (frequency,),
    ).fetchone()


def readiness(db, settings):
    capabilities = db.rows("SELECT * FROM capabilities")
    available = {r["endpoint"] for r in capabilities if r["status"] == "reachable" and r["data"].get("schema_verified")}
    roles = {
        r["role"]
        for r in db.rows(
            "SELECT role FROM heartbeats WHERE updated_at>now()-interval '20 seconds' "
            "AND data->>'status' IS DISTINCT FROM 'stopped'"
        )
    }
    with db.transaction() as conn:
        models = {f: active_model(conn, f) for f in ("day", "5min")}
    generations = db.rows("SELECT DISTINCT ON(frequency) * FROM qlib_generations ORDER BY frequency,created_at DESC")
    result = {}
    for freq, role in (("day", "qlib-daily"), ("5min", "qlib-intraday")):
        required = DAILY_CAPABILITIES | (MINUTE_CAPABILITIES if freq == "5min" else set())
        missing = sorted(required - available)
        blockers = ["permissions: " + ", ".join(missing)] if missing else []
        if role not in roles or "qlib-data" not in roles:
            blockers.append("required Qlib workers unavailable")
        if models[freq] is None:
            blockers.append("no approved, qualified, unexpired Qlib model")
        if not any(g["frequency"] == freq for g in generations):
            blockers.append("no validated Qlib generation")
        if freq == "5min" and models["day"] is None:
            blockers.append("daily model baseline unavailable")
        result[freq] = {"ready": not blockers, "blockers": blockers, "model": models[freq]}
    return {
        "engine": ENGINE_VERSION,
        "required": True,
        "native_fallback": False,
        "frequencies": result,
        "capabilities": capabilities,
        "generations": generations,
        "history_sessions": settings.history_sessions,
        "minute_history_sessions": settings.minute_history_sessions,
        "pool_capacity": settings.intraday_pool_capacity,
        "research_minutes": db.setting("research_minute_readiness", {"ready": False, "reason": "Not scheduled yet."}),
    }


def historical_pools(conn, cutoff, policy_revision):
    return conn.execute(
        "SELECT DISTINCT ON(u.day) u.id,u.day,u.data FROM universe_snapshots u "
        "JOIN model_versions m ON m.id::text=u.data->>'daily_model_id' "
        "JOIN model_evaluations e ON e.model_id=m.id WHERE u.day<=%s "
        "AND u.data->>'kind'='research_intraday' AND u.data->>'out_of_sample'='true' "
        "AND u.data->>'ready'='true' AND u.data->>'policy_revision'=%s AND e.passed AND m.frequency='day' "
        "AND jsonb_typeof(u.data->'daily_weights')='object' AND jsonb_typeof(u.data->'symbols')='array' "
        "ORDER BY u.day,u.created_at DESC,u.id",
        (cutoff, str(policy_revision)),
    ).fetchall()


def create_run(db, settings, request):
    request = RunInput.model_validate(request)
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        prior = conn.execute("SELECT * FROM pipeline_requests WHERE request_key=%s", (request.request_key,)).fetchone()
        request_data = request.model_dump(
            mode="json", exclude={"training_configuration_id"} if request.training_configuration_id is None else set()
        )
        if prior:
            if prior["request"] != request_data:
                raise Conflict("Idempotency key was already used for a different request.")
            return conn.execute("SELECT * FROM pipeline_runs WHERE id=%s", (prior["run_id"],)).fetchone()
        cutoff = request.as_of
        training_config, frozen = None, None
        if request.training_configuration_id:
            from quant_platform.storage.experiments import frozen_training

            training_config, frozen = frozen_training(conn, request.training_configuration_id)
            if training_config["frequency"] != request.frequency or (
                training_config["fixture_windows"] and settings.environment != "test"
            ):
                raise Conflict("Frozen training configuration frequency or environment mismatch.")
            if cutoff is not None and cutoff != frozen["as_of"]:
                raise Conflict("Frozen candidate training must use its pinned generation cutoff.")
            cutoff = frozen["as_of"]
        if cutoff is None:
            if request.frequency == "5min":
                from quant_platform.domain.workflow import five_minute_end

                cutoff = five_minute_end(now())
            else:
                target = conn.execute("SELECT value FROM settings WHERE key='analysis_target'").fetchone()
                if target:
                    cutoff = datetime.combine(datetime.fromisoformat(target["value"]).date(), time(15), CN)
        if cutoff is None or cutoff > now():
            raise PipelineBlocked("A completed, dated input cutoff is required.")
        if request.frequency == "5min":
            from quant_platform.domain.workflow import five_minute_end

            if five_minute_end(cutoff) != cutoff:
                raise Conflict("Intraday cutoff must be a completed five-minute session boundary.")
        selected_policy = policy(conn)
        conn.execute("SELECT pg_advisory_xact_lock(77610234)")
        endpoints = DAILY_DATASETS | (MINUTE_CAPABILITIES if request.frequency == "5min" else set())
        watermark = conn.execute(
            "SELECT coalesce(max(id),0) AS id FROM datasets WHERE endpoint=ANY(%s)", (sorted(endpoints),)
        ).fetchone()["id"]
        model = active_model(conn, request.frequency)
        if request.purpose == "shadow":
            model = conn.execute(
                "SELECT m.*,e.data AS evaluation FROM model_versions m JOIN model_evaluations e ON e.model_id=m.id "
                "WHERE m.id=%s AND m.frequency=%s AND m.state='shadow' AND e.passed",
                (request.model_id, request.frequency),
            ).fetchone()
            if model is None or now() >= model_deadline(model):
                raise PipelineBlocked("Shadow observation requires a qualified, fresh challenger.")
        portfolios = conn.execute(
            "SELECT p.id,p.revision AS head_revision,r.revision,r.data,r.effective_from FROM model_portfolios p "
            "JOIN LATERAL (SELECT * FROM model_portfolio_revisions WHERE portfolio_id=p.id AND effective_from<=%s "
            "ORDER BY effective_from DESC,revision DESC LIMIT 1) r ON true "
            "WHERE (%s::uuid IS NULL OR p.id=%s)",
            (cutoff, request.portfolio_id, request.portfolio_id),
        ).fetchall()
        if request.portfolio_id and not portfolios:
            raise Conflict("No effective model-portfolio baseline at this cutoff.")
        watchlist = conn.execute("SELECT * FROM watchlist_revisions ORDER BY revision DESC LIMIT 1").fetchone()
        instruments = conn.execute(
            "SELECT * FROM instruments WHERE list_date<=%s AND (%s OR delist_date IS NULL OR delist_date>=%s)",
            (cutoff.astimezone(CN).date(), request.purpose == "training", cutoff.astimezone(CN).date()),
        ).fetchall()
        daily = None
        portfolio_daily = {}
        if request.frequency == "5min":
            daily = conn.execute(
                "SELECT r.*,p.model_id,p.data AS prediction FROM recommendations r "
                "JOIN prediction_runs p ON p.id=r.prediction_id JOIN model_versions m ON m.id=p.model_id "
                "WHERE r.frequency='day' AND r.portfolio_id IS NULL AND r.state='published' "
                "AND r.available_at<=%s AND r.valid_until>%s AND m.state='active' AND m.expires_at>now() "
                "ORDER BY r.as_of DESC LIMIT 1",
                (cutoff, cutoff),
            ).fetchone()
            for portfolio in portfolios:
                row = conn.execute(
                    "SELECT r.* FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
                    "JOIN model_versions m ON m.id=p.model_id WHERE r.frequency='day' AND r.portfolio_id=%s "
                    "AND r.portfolio_revision=%s AND r.state='published' AND r.available_at<=%s "
                    "AND r.valid_until>%s AND m.state='active' AND m.expires_at>now() ORDER BY r.as_of DESC LIMIT 1",
                    (portfolio["id"], portfolio["revision"], cutoff, cutoff),
                ).fetchone()
                if row:
                    portfolio_daily[str(portfolio["id"])] = row
        snapshot = {
            "request": request_data,
            "engine": ENGINE_VERSION,
            "contract": DATA_CONTRACT,
            "watermark": watermark,
            "policy": selected_policy["data"],
            "policy_revision": selected_policy["revision"],
            "model_id": str(model["id"]) if model else None,
            "model": model,
            "portfolios": portfolios,
            "watchlist": watchlist,
            "instruments": {r["symbol"]: r for r in instruments},
            "daily_baseline": daily,
            "portfolio_daily_baselines": portfolio_daily,
            "history_sessions": settings.history_sessions,
            "minute_history_sessions": settings.minute_history_sessions,
            "calendars": conn.execute(
                "SELECT exchange,day,is_open FROM calendars WHERE day<=%s ORDER BY day,exchange",
                (cutoff.astimezone(CN).date(),),
            ).fetchall(),
            "pools": (
                (
                    historical_pools(conn, cutoff.astimezone(CN).date(), selected_policy["revision"])
                    if request.purpose == "training"
                    else conn.execute(
                        "SELECT day,data FROM universe_snapshots WHERE day<=%s AND data->>'kind'='intraday' ORDER BY day,created_at",
                        (cutoff.astimezone(CN).date(),),
                    ).fetchall()
                )
                if request.frequency == "5min"
                else []
            ),
        }
        if frozen:
            manifest = frozen["manifest"]
            if selected_policy["data"] != manifest["policy"]:
                raise PipelineBlocked("Frozen experiment policy differs from the current scan policy.")
            snapshot.update(
                training_configuration=training_config,
                research_freeze_id=str(frozen["id"]),
                research_generation_id=str(frozen["generation_id"]),
                reserved_test=frozen["data"]["reserved_test"],
                watermark=manifest["watermark"],
                instruments=manifest["instruments"],
            )
        snapshot = __import__("json").loads(__import__("json").dumps(snapshot, default=str))
        identity = {k: v for k, v in snapshot.items() if k != "request"}
        identity.update(frequency=request.frequency, purpose=request.purpose, as_of=cutoff.isoformat())
        key = digest(identity)
        existing = conn.execute("SELECT * FROM pipeline_runs WHERE input_hash=%s", (key,)).fetchone()
        if existing:
            conn.execute(
                "INSERT INTO pipeline_requests(request_key,run_id,request) VALUES(%s,%s,%s)",
                (request.request_key, existing["id"], jsonb(request_data)),
            )
            return existing
        run_id = uuid4()
        conn.execute(
            "INSERT INTO pipeline_runs(id,request_key,input_hash,frequency,purpose,as_of,snapshot) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (run_id, request.request_key, key, request.frequency, request.purpose, cutoff, jsonb(snapshot)),
        )
        conn.execute(
            "INSERT INTO pipeline_requests(request_key,run_id,request) VALUES(%s,%s,%s)",
            (request.request_key, run_id, jsonb(request_data)),
        )
        steps = [("prepare", "qlib-data")]
        if request.purpose == "training":
            steps += [("train", "qlib-train")]
        else:
            steps += [
                (
                    "shadow" if request.purpose == "shadow" else "infer",
                    "qlib-daily" if request.frequency == "day" else "qlib-intraday",
                )
            ]
        previous = None
        for name, queue in steps:
            job_id = db.enqueue(conn, "qlib_" + name, queue, f"pipeline:{run_id}:{name}", {"run_id": str(run_id)}, 100)
            conn.execute("INSERT INTO pipeline_steps(run_id,name,job_id) VALUES(%s,%s,%s)", (run_id, name, job_id))
            if previous:
                conn.execute("INSERT INTO job_dependencies VALUES(%s,%s)", (job_id, previous))
            previous = job_id
        return conn.execute("SELECT * FROM pipeline_runs WHERE id=%s", (run_id,)).fetchone()


def run_details(db, run_id):
    rows = db.rows("SELECT * FROM pipeline_runs WHERE id=%s", (run_id,))
    if not rows:
        raise Conflict("Pipeline run not found.")
    return {
        **rows[0],
        "steps": db.rows(
            "SELECT s.*,j.status,j.error,j.progress,j.available_at FROM pipeline_steps s JOIN jobs j ON j.id=s.job_id "
            "WHERE run_id=%s ORDER BY j.id",
            (run_id,),
        ),
    }


def change_policy(db, value):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        old = conn.execute("SELECT coalesce(max(revision),0) AS revision FROM scan_policies").fetchone()
        if old["revision"] != value.expected_revision:
            raise Conflict("Scan policy revision conflict.")
        row = conn.execute(
            "INSERT INTO scan_policies(data) VALUES(%s) RETURNING *", (jsonb(value.policy.model_dump()),)
        ).fetchone()
        conn.execute(
            "INSERT INTO audit(action,target,data) VALUES('scan_policy',%s,%s)",
            (str(row["revision"]), jsonb(row["data"])),
        )
        return row


def model_deadline(model):
    trained_as_of = datetime.fromisoformat(model["metadata"].get("training_as_of", str(model["created_at"])))
    return min(trained_as_of, model["created_at"]) + timedelta(days=35 if model["frequency"] == "day" else 14)


def validate_snapshot(conn, snapshot):
    current_policy = conn.execute("SELECT max(revision) AS revision FROM scan_policies").fetchone()
    if current_policy["revision"] != snapshot["policy_revision"]:
        raise PipelineBlocked("Policy changed during inference; request a fresh proposal.")
    for portfolio in snapshot["portfolios"]:
        head = conn.execute("SELECT revision FROM model_portfolios WHERE id=%s", (portfolio["id"],)).fetchone()
        if not head or head["revision"] != portfolio["head_revision"] or head["revision"] != portfolio["revision"]:
            raise PipelineBlocked("Model-portfolio baseline changed or has a pending revision.")
    baselines = list(snapshot.get("portfolio_daily_baselines", {}).values())
    if snapshot.get("daily_baseline"):
        baselines.append(snapshot["daily_baseline"])
    for baseline in baselines:
        valid = conn.execute(
            "SELECT 1 FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
            "JOIN model_versions m ON m.id=p.model_id JOIN model_evaluations e ON e.model_id=m.id "
            "WHERE r.id=%s AND r.state='published' AND r.policy_revision=%s AND e.passed "
            "AND r.valid_until>now() AND m.state='active' AND m.expires_at>now()",
            (baseline["id"], snapshot["policy_revision"]),
        ).fetchone()
        if not valid:
            raise PipelineBlocked("Pinned daily baseline expired or was superseded.")


def shadow_cutoffs(day, frequency):
    """Expected observation cutoffs, excluding bars without a future execution interval."""
    if frequency == "day":
        return {datetime.combine(day, time(15), CN)}
    return {
        datetime.combine(day, time(minute // 60, minute % 60), CN)
        for minute in (*range(575, 681, 5), *range(785, 891, 5))
    }


def promote(db, model_id, expected_active_id):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        model = conn.execute(
            "SELECT m.*,e.passed FROM model_versions m JOIN model_evaluations e ON e.model_id=m.id "
            "WHERE m.id=%s FOR UPDATE OF m",
            (model_id,),
        ).fetchone()
        if not model or not model["passed"] or model["state"] not in {"shadow", "retired"}:
            raise Conflict("Only qualified shadow/retired Qlib models can be promoted.")
        active = conn.execute(
            "SELECT id FROM model_versions WHERE frequency=%s AND state='active'", (model["frequency"],)
        ).fetchone()
        if (str(active["id"]) if active else None) != (str(expected_active_id) if expected_active_id else None):
            raise Conflict("Active release changed.")
        lineage = model["metadata"].get("workflow_identity")
        today = now().astimezone(CN).date()
        evidence = conn.execute(
            "SELECT count(DISTINCT s.day) AS days,bool_or(s.model_id=%s) AS own "
            "FROM model_shadow_samples s JOIN model_versions m ON m.id=s.model_id "
            "JOIN model_evaluations e ON e.model_id=m.id "
            "JOIN calendars a ON a.day=s.day AND a.exchange='SSE' AND a.is_open "
            "JOIN calendars b ON b.day=s.day AND b.exchange='SZSE' AND b.is_open "
            "WHERE s.healthy AND e.passed AND m.frequency=%s AND m.metadata->>'workflow_identity'=%s "
            "AND s.data->>'workflow_identity'=%s AND s.data->>'environment'='production' "
            "AND s.day>=%s AND s.day<%s",
            (model_id, model["frequency"], lineage, lineage, today - timedelta(days=60), today),
        ).fetchone()
        expires = model_deadline(model)
        if evidence["days"] < 20 or not evidence["own"] or now() >= expires:
            raise Conflict(
                "Twenty recent lineage shadow sessions, this model's own shadow evidence, and a fresh model are required."
            )
        conn.execute(
            "UPDATE model_versions SET state='retired' WHERE frequency=%s AND state='active'", (model["frequency"],)
        )
        conn.execute(
            "UPDATE model_versions SET state='active',approved_at=now(),expires_at=%s WHERE id=%s", (expires, model_id)
        )
        conn.execute(
            "INSERT INTO model_release_events(model_id,action,data) VALUES(%s,'promote',%s)",
            (model_id, jsonb({"previous": expected_active_id})),
        )
        return {"model_id": str(model_id), "state": "active"}


def retry_run(db, run_id):
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM pipeline_runs WHERE id=%s FOR UPDATE", (run_id,)).fetchone()
        if not row or row["state"] not in {"failed", "blocked"}:
            raise Conflict("Only blocked/failed runs can be retried.")
        if row["frequency"] == "5min" and row["purpose"] in {"inference", "shadow"}:
            raise Conflict("Expired intraday runs cannot be replayed as live recommendations.")
        conn.execute(
            "UPDATE jobs SET status='pending',failures=0,error=NULL,available_at=now() WHERE id IN "
            "(SELECT job_id FROM pipeline_steps WHERE run_id=%s) AND status IN ('blocked','failed')",
            (run_id,),
        )
        conn.execute("UPDATE pipeline_runs SET state='waiting',error=NULL,updated_at=now() WHERE id=%s", (run_id,))
        return {"run_id": str(run_id), "snapshot_reused": True}
