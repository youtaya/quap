"""Persistent, fenced Qlib run graph and release/readiness control plane."""

from datetime import datetime, time, timedelta
from uuid import uuid4

from quant_platform.domain import CN, digest, now
from quant_platform.domain.workflow import (
    CAPABILITY_LABELS,
    DATA_CONTRACT,
    ENGINE_VERSION,
    GATE_SPECS,
    GATE_STAGES,
    OUTPUT_GATE,
    PipelineBlocked,
    RunInput,
    ScanPolicy,
)
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict

DAILY_CAPABILITIES = {
    "securities",
    "calendar",
    "history",
    "factors",
    "benchmark",
    "security_history",
    "constraints",
}
MINUTE_CAPABILITIES = {"minutes"}
DAILY_DATASETS = {
    "securities",
    "calendar",
    "history",
    "factors",
    "benchmark",
    "security_history",
    "constraints",
}
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


def gate_specs(frequency):
    """门禁规格里属于该频率的那些，顺序即呈现顺序。"""
    return [spec for spec in GATE_SPECS if frequency in spec.get("frequencies", ("day", "5min"))]


def gate_chain(frequency, failures, evidence):
    """把门禁规格与本次判定结果拼成判决面消费的结构。

    `status` 三值，含义互不重叠：
      `passed`  自身条件成立；
      `blocked` 自身条件不成立，**且**上游都已通过 —— 这才是操作员此刻该处理的那一个；
      `waiting` 自身条件不成立，但上游也没过 —— 按附录 A.3 规则 2，下游**不得**显示自身失败状态。
                它的真实状态此刻无从判断，显示了就是在凭空造一个假的行动项。
    """
    resolved, chain = {}, []
    for spec in gate_specs(frequency):
        failed = spec["key"] in failures
        upstream = [resolved[key] for key in spec["depends_on"]]
        if not failed:
            status = "passed"
        else:
            status = "blocked" if all(item == "passed" for item in upstream) else "waiting"
        resolved[spec["key"]] = status
        chain.append(
            {
                "key": spec["key"],
                "stage": spec["stage"],
                "stage_label": GATE_STAGES[spec["stage"]],
                "action": spec["action"],
                "depends_on": list(spec["depends_on"]),
                "status": status,
                # 判决句由前端按 `key` 组稿，数字全部来自这里 —— 文案在展示层，事实在服务端，
                # 两边都不必去猜对方。
                "evidence": evidence.get(spec["key"], {}),
            }
        )
    return chain


def published_output(db, frequency):
    """⑥ 产出状态：今天是否已存在一份**有效**建议（`VALID_REPORT` 的全部条件都成立）。

    它不参与 `ready`：`ready` 问「能不能跑」，这里问「跑出来的东西在不在」。混为一谈会让
    「流水线可运行、但今天还没推理」看起来像故障。
    """
    rows = db.rows(
        "SELECT r.id,r.as_of,r.available_at,r.valid_until FROM recommendations r "
        "JOIN prediction_runs p ON p.id=r.prediction_id JOIN model_versions m ON m.id=p.model_id "
        "JOIN model_evaluations e ON e.model_id=m.id JOIN pipeline_runs a ON a.id=p.run_id "
        f"WHERE r.frequency=%s AND ({VALID_REPORT}) ORDER BY r.as_of DESC LIMIT 1",
        (frequency,),
    )
    row = rows[0] if rows else None
    return {
        "published": row is not None,
        "depends_on": list(OUTPUT_GATE["depends_on"]),
        "recommendation_id": str(row["id"]) if row else None,
        "as_of": row["as_of"].isoformat() if row else None,
        "valid_until": row["valid_until"].isoformat() if row else None,
    }


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
        # 「permissions」是旧令牌供应商的措辞；免令牌公开源没有权限可授，缺的是一次成功的真实
        # 采集（`Market._qualify` 据此置位 `schema_verified`）。
        #
        # 判定与文案在同一个 `if` 里成对写下：`failures` 是门禁链的输入，`evidence` 是判决句的
        # 数字来源，`blockers` 是旧接口沿用的字符串。三者出自同一处，所以不可能互相漂移。
        failures, evidence = {}, {}
        if missing:
            failures["capability"] = "数据源未验证：" + "、".join(
                CAPABILITY_LABELS.get(name, name) for name in missing
            )
            evidence["capability"] = {
                "missing": missing,
                "missing_labels": [CAPABILITY_LABELS.get(name, name) for name in missing],
                "required": sorted(required),
                "verified": sorted(required & available),
                "required_count": len(required),
                "missing_count": len(missing),
            }
        # 采集范围是**配置**，不是数据缺陷，所以它单列一条，而不是躲在覆盖门禁后面。生产验收要求
        # 全市场 ≥95% 覆盖；`index` 范围只采成分股与观察篮子，本就不满足这一条，必须说出来。
        if settings.history_scope != "all":
            failures["scope"] = (
                f"采集范围为「{settings.history_scope}」；生产验收要求 QUANT_HISTORY_SCOPE=all，"
                "且全市场日线覆盖率至少 95%"
            )
            evidence["scope"] = {"scope": settings.history_scope, "required_scope": "all", "minimum_coverage": 0.95}
        if role not in roles or "qlib-data" not in roles:
            failures["engine"] = "必需的 Qlib 服务未就绪"
            evidence["engine"] = {
                "required_roles": sorted({role, "qlib-data"}),
                "healthy_roles": sorted(roles),
                "healthy": False,
            }
        if not any(g["frequency"] == freq for g in generations):
            failures["generation"] = "没有已验证的 Qlib 数据代次"
            evidence["generation"] = {"frequency": freq, "generation_count": 0}
        if models[freq] is None:
            failures["model"] = "没有已发布、已合格且未过期的 Qlib 模型"
            evidence["model"] = {"frequency": freq}
        if freq == "5min" and models["day"] is None:
            failures["day_baseline"] = "缺少日线模型基线"
            evidence["day_baseline"] = {"missing_frequency": "day"}
        chain = gate_chain(freq, failures, evidence)
        # `blockers` 的顺序由 `GATE_SPECS` 决定，而不是 append 顺序。P0 时它是 append 出来的，
        # 于是「没有模型」排在「没有数据代次」前面——和附录 A.3 的依赖链正好反着读。
        blockers = [failures[spec["key"]] for spec in gate_specs(freq) if spec["key"] in failures]
        result[freq] = {
            "ready": not blockers,
            "blockers": blockers,
            "model": models[freq],
            "gates": chain,
            "output": published_output(db, freq),
        }
    return {
        "engine": ENGINE_VERSION,
        "required": True,
        "native_fallback": False,
        "frequencies": result,
        "capabilities": capabilities,
        "generations": generations,
        "history_sessions": settings.history_sessions,
        "factor_sessions": settings.factor_sessions,
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
        # 训练宇宙必须**等于平台声明的采集范围**。两者不一致时，`minimum_coverage`（下限 0.95）的
        # 分母是「全部上市证券」而分子只可能来自已采集的那一小撮：默认 `index` 范围下上限约 5%，
        # 无论数据多好都过不去。这和之前那个「能力门禁结构性不可满足」是同一类缺陷 —— 门禁测的不
        # 是数据，而是配置。`history_scope=all` 时范围就是全市场，语义不变。
        from quant_platform.jobs.collect import scoped_codes_on

        scoped = scoped_codes_on(conn, settings, cutoff.astimezone(CN).date())
        if not scoped:
            raise PipelineBlocked("No scoped securities; check index membership and the security master.")
        instruments = conn.execute(
            "SELECT * FROM instruments WHERE symbol=ANY(%s) AND (list_date IS NULL OR list_date<=%s) "
            "AND (%s OR delist_date IS NULL OR delist_date>=%s)",
            (
                sorted(scoped),
                cutoff.astimezone(CN).date(),
                request.purpose == "training",
                cutoff.astimezone(CN).date(),
            ),
        ).fetchall()
        daily = None
        portfolio_daily = {}
        if request.frequency == "5min":
            daily = conn.execute(                "SELECT r.*,p.model_id,p.data AS prediction FROM recommendations r "
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
            # 采集范围的**模式**要跟范围一起冻结：代次 manifest 里的 `coverage_detail.scope_mode` 只有
            # 拿到它才能说清「298 是这个模式的全部，不是全市场的一角」。范围本身已经由
            # `scoped_codes_on` 决定（见上），这里只是把它为什么是这些证券记下来。
            "scope_mode": settings.history_scope,
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
