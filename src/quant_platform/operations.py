"""Readiness, capability qualification, bounded retention and recoverable backups."""

import hashlib
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from psycopg import pq, sql
from psycopg.conninfo import conninfo_to_dict

from quant_platform.domain import CN, now, fresh, session
from quant_platform.analysis import basket_summary
from quant_platform.observations import count_lines
from quant_platform.providers.market import CAPABILITY_SOURCES, Market
from quant_platform.storage import jsonb

# Rolling range partitions maintained by `maintenance`, mapped to the age at which an empty
# partition is reclaimed. Insertion order is the creation order. Migration 0010 repeats this
# allow-list inside its SECURITY DEFINER helpers; the two must stay in step.
PARTITION_RETIRE_DAYS = {"quotes": 8, "alerts": 366, "minute_bars": 8}


def board_status(db, at=None):
    at = at or now()
    rows = db.rows(
        "SELECT i.symbol,i.board,q.data FROM instruments i LEFT JOIN latest_quotes q USING(symbol) "
        "WHERE i.status='L' ORDER BY i.symbol"
    )
    output = []
    for name in ("STAR Market", "ChiNext", "Main Board"):
        selected = [r for r in rows if r["board"] == name]
        quotes = {r["symbol"]: r["data"] or {} for r in selected}
        summary = basket_summary({r["symbol"]: 1 for r in selected}, quotes, at) if selected else {}
        output.append(
            {
                "board": name,
                "listed": len(selected),
                "stored": sum(bool(r["data"]) for r in selected),
                "fresh": sum(fresh(q.get("source_time"), at) for q in quotes.values()),
                **summary,
                "weighting": "equal-weight eligible stocks; not an exchange index",
            }
        )
    return output


def qualification(db, settings, job):
    at = now()
    local = at.astimezone(CN)
    active = session(at, db.calendar_open(local.date()))[1]
    requested = datetime.fromisoformat(job["payload"]["at"])
    observe = active and 0 <= (at - requested).total_seconds() <= 90
    boards = board_status(db, at)
    total = sum(b["listed"] for b in boards)
    coverage = sum(b.get("eligible", 0) for b in boards) / total if total else 0
    required = {"securities", "calendar", "history", "factors", "quotes"}
    capabilities = db.rows("SELECT * FROM capabilities")
    available = {c["endpoint"] for c in capabilities if c["status"] == "reachable" and c["data"].get("schema_verified")}
    healthy_roles = {
        r["role"]
        for r in db.rows(
            "SELECT role FROM heartbeats WHERE updated_at>now()-interval '20 seconds' "
            "AND data->>'status' IS DISTINCT FROM 'stopped'"
        )
    }
    directory = db.setting("directory", {})
    reference_fresh = bool(directory) and timedelta(0) <= at - datetime.fromisoformat(
        directory["fetched_at"]
    ) < timedelta(hours=48)
    healthy = (
        coverage >= 0.95
        and required <= available
        and reference_fresh
        and {"scheduler", "quotes", "history", "analysis", "operations"} <= healthy_roles
    )
    with db.publication(job) as conn:
        if observe:
            conn.execute(
                "INSERT INTO qualification_samples VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    at.replace(second=0, microsecond=0),
                    local.date(),
                    healthy,
                    jsonb(
                        {
                            "eligible_coverage": coverage,
                            "available": sorted(available),
                            "roles": sorted(healthy_roles),
                            "environment": settings.environment,
                        }
                    ),
                ),
            )
        completed = local.date() if local.hour >= 15 else local.date() - timedelta(days=1)
        days = conn.execute(
            "WITH days AS (SELECT day FROM calendars WHERE exchange IN ('SSE','SZSE') AND is_open "
            "AND day<=%s GROUP BY day HAVING count(*)=2 ORDER BY day DESC LIMIT 2) "
            "SELECT d.day,count(s.minute) AS samples,count(s.minute) FILTER(WHERE s.healthy) AS healthy "
            "FROM days d LEFT JOIN qualification_samples s ON s.day=d.day "
            "AND s.data->>'environment'=%s GROUP BY d.day ORDER BY d.day",
            (completed, settings.environment),
        ).fetchall()
        passed = len(days) == 2 and all(d["samples"] >= 236 and d["healthy"] / d["samples"] >= 0.99 for d in days)
        db.set_setting(
            conn,
            "qualification",
            {
                "status": "observed" if passed and settings.environment == "production" else "pending",
                "environment": settings.environment,
                "required_trading_sessions": 2,
                "days": days,
                "current_coverage": coverage,
                "at": at.isoformat(),
                "scope": "Data observation only; backup, bootstrap and deployment acceptance remain separate.",
            },
        )


def data_quality(db, at=None):
    """Quote, bar and factor gaps. This is not container health."""
    at = at or now()
    day = at.astimezone(CN).date()
    blocked = db.rows(
        "SELECT endpoint,status FROM capabilities WHERE status IN ('blocked','unverified','circuit_open') "
        "ORDER BY endpoint"
    )
    stale = sum(not fresh(row["data"].get("source_time"), at) for row in db.rows("SELECT data FROM latest_quotes"))
    missing = db.rows(
        "WITH last_open AS (SELECT max(day) AS day FROM calendars WHERE exchange='SSE' AND is_open AND day<%s) "
        "SELECT count(*) AS n FROM instruments i CROSS JOIN last_open d "
        "WHERE d.day IS NOT NULL AND i.status='L' AND (i.list_date IS NULL OR i.list_date<=d.day) "
        "AND (i.delist_date IS NULL OR i.delist_date>d.day) "
        "AND NOT EXISTS (SELECT 1 FROM daily_bars b WHERE b.symbol=i.symbol AND b.day=d.day)",
        (day,),
    )[0]["n"]
    revisions = db.rows(
        "SELECT count(*) AS n FROM (SELECT symbol,day FROM factors GROUP BY symbol,day HAVING count(*)>1) revisions"
    )[0]["n"]
    # 有 K 线、但一根复权因子都没有的证券：它们进不了代次（未复权价绝不入样本），也不会出现在任何
    # 门禁的报错里 —— 样本面就这么无声地缩小了。参考 QuantMind 对缺列的处置：缺口必须显式报出来，
    # 而不是让消费端从百分比里猜。这里给数量与前若干只名单，运维据此判断是采集没跑完还是源不支持。
    unadjusted = [
        row["symbol"]
        for row in db.rows(
            "SELECT b.symbol FROM (SELECT DISTINCT symbol FROM daily_bars) b "
            "WHERE NOT EXISTS(SELECT 1 FROM factors f WHERE f.symbol=b.symbol) ORDER BY b.symbol"
        )
    ]
    undated = db.rows("SELECT count(*) AS n FROM instruments WHERE list_date IS NULL")[0]["n"]
    states = db.rows(
        "SELECT count(*) AS n, min(effective_day)::text AS first_day, max(effective_day)::text AS last_day, "
        "count(*) FILTER(WHERE coalesce((data->>'backfilled')::boolean,false)) AS backfilled "
        "FROM security_states"
    )[0]
    return {
        "blocked_capabilities": blocked,
        "stale_or_missing_quote_count": stale,
        "listed_missing_latest_completed_bar": missing,
        "factor_revision_keys": revisions,
        "bars_without_any_adjustment": {"count": len(unadjusted), "symbols": unadjusted[:20]},
        "instruments_without_list_date": undated,
        "point_in_time_risk_state": {
            "rows": states["n"],
            "first_day": states["first_day"],
            "last_day": states["last_day"],
            "backfilled": states["backfilled"],
            "dated": False,
            "note": (
                "公开源不发布带日期的更名/风险公告，状态只能从观测当天起算。观测日之前的历史 K 线"
                "没有点位风险状态，训练里是 NaN（未知）而不是「无风险」；跨越观测日的回测结论必须"
                "声明这一点。"
            ),
        },
        "note": "Data quality is independent of container health.",
    }


def market_health(at, total, verified, directory_fresh, open_today):
    """Whether market data is fresh, merely quiet, or actually broken.

    "Nothing is verified" is not one state. Outside a trading session the sources publish nothing
    new, so ``verified != total`` is expected — yet the overview reported ``degraded_or_unavailable``
    every weekend and every holiday, which is exactly what destroys an alarm's credibility: a real
    collection failure looks identical to a Sunday. A closed session with a fresh directory is
    reported as quiet; only a session that should have produced data and did not is degraded.
    """
    if total and verified == total and directory_fresh:
        return "fresh"
    state, active = session(at, open_today)
    if directory_fresh and not active and state in {"closed", "pre-open", "lunch"}:
        return "quiet_outside_session"
    return "degraded_or_unavailable"


def recovery_times(backup_at, fault_at, finished_at):
    return {
        "fault_at": fault_at.isoformat(),
        "restore_finished_at": finished_at.isoformat(),
        "rpo_seconds": (fault_at - backup_at).total_seconds(),
        "rto_seconds": (finished_at - fault_at).total_seconds(),
    }


def copy_verified_replica(source, replica_root, checksum):
    replica_root = Path(replica_root).resolve()
    source = Path(source).resolve()
    if replica_root == source.parent:
        raise ValueError("Backup replica must use a different directory than the primary dump.")
    replica_root.mkdir(parents=True, exist_ok=True)
    temporary = replica_root / (source.name + ".pending")
    target = replica_root / source.name
    shutil.copyfile(source, temporary)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
        replica_checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if replica_checksum != checksum:
        temporary.unlink(missing_ok=True)
        raise RuntimeError("Backup replica checksum does not match the primary dump.")
    os.replace(temporary, target)
    return {
        "file": target.name,
        "sha256": replica_checksum,
        "bytes": target.stat().st_size,
        "at": now().isoformat(),
        "directory": str(replica_root),
    }


def status(db, settings):
    at = now()
    heartbeats = db.rows(
        "SELECT *,updated_at>now()-interval '20 seconds' AND data->>'status' IS DISTINCT FROM 'stopped' "
        "AS healthy FROM heartbeats ORDER BY updated_at DESC LIMIT 50"
    )
    latest = db.rows("SELECT data FROM latest_quotes")
    fresh_count = sum(fresh(row["data"].get("source_time"), at) for row in latest)
    directory = db.setting("directory", {})
    boards = board_status(db, at)
    total = sum(b["listed"] for b in boards)
    verified = sum(b.get("eligible", 0) for b in boards)
    directory_fresh = bool(directory) and timedelta(0) <= at - datetime.fromisoformat(
        directory["fetched_at"]
    ) < timedelta(hours=48)
    metrics = db.rows(
        "SELECT count(*) FILTER(WHERE status='pending') AS pending_jobs, "
        "count(*) FILTER(WHERE status='running' AND lease_until<now()) AS expired_leases, "
        "count(*) FILTER(WHERE status='pending' AND strpos(lower(error),'quota')>0) AS pending_quota_deferrals, "
        "min(available_at) FILTER(WHERE status='pending') AS next_due FROM jobs"
    )[0]
    metrics["eligible_market_coverage"] = verified / total if total else 0
    ages = sorted(
        (at - datetime.fromisoformat(row["data"]["source_time"])).total_seconds()
        for row in latest
        if row["data"].get("source_time")
    )
    metrics["source_age_seconds"] = {
        "stored_quotes": len(latest),
        "dated_quotes": len(ages),
        "minimum": ages[0] if ages else None,
        "maximum": ages[-1] if ages else None,
        "mean": sum(ages) / len(ages) if ages else None,
    }
    analysis = db.setting("last_analysis")
    target = db.setting("analysis_target")
    metrics["analysis_session_lag"] = (
        db.rows(
            "SELECT count(*) AS n FROM calendars WHERE exchange='SSE' AND is_open AND day>%s AND day<=%s",
            (analysis["as_of"], target),
        )[0]["n"]
        if analysis and target
        else None
    )
    metrics["analysis_age_seconds"] = (
        (at - datetime.fromisoformat(analysis["at"])).total_seconds() if analysis else None
    )
    try:
        disk = shutil.disk_usage(settings.artifact_root)
        metrics["artifact_disk"] = {
            "total_bytes": disk.total,
            "free_bytes": disk.free,
            "used_ratio": disk.used / disk.total,
        }
    except OSError:
        metrics["artifact_disk"] = None
    metrics["persisted_api_events"] = count_lines(settings.observation_root, "api")
    metrics["quota_usage"] = db.rows(
        "SELECT endpoint,window_start,sum(used) AS requests FROM quota "
        "WHERE window_start>now()-interval '1 day' GROUP BY endpoint,window_start ORDER BY window_start DESC LIMIT 100"
    )
    return {
        "provider": "market",
        "configured": True,
        "sources": db.setting("doctor", {}).get("sources", {}),
        "boards": boards,
        "metrics": metrics,
        "services": heartbeats,
        "capabilities": db.rows("SELECT * FROM capabilities ORDER BY endpoint"),
        "jobs": db.rows("SELECT queue,status,count(*) FROM jobs GROUP BY queue,status ORDER BY queue,status"),
        "directory": directory,
        "quote_count": len(latest),
        "fresh_quote_count": fresh_count,
        "last_analysis": db.setting("last_analysis"),
        "analysis_target": db.setting("analysis_target"),
        "polling_paused": db.setting("polling_paused", False),
        "backup": db.setting("backup"),
        "qlib": __import__("quant_platform.pipeline", fromlist=["readiness"]).readiness(db, settings),
        "incidents": db.rows("SELECT * FROM incidents ORDER BY updated_at DESC"),
        "qualification": db.setting("qualification", {"status": "pending", "required_trading_sessions": 2}),
        "data_quality": data_quality(db, at),
        # 非交易时段没有新行情是**正常**的，不该和真实采集故障共用一个字段：周末长期告警会让
        # 这个字段失去可信度。见 ``market_health``。
        "data_health": market_health(
            at, total, verified, directory_fresh, db.calendar_open(at.astimezone(CN).date())
        ),
        "timestamp": at.isoformat(),
    }


def doctor(db, settings, feed=None):
    """Probe the four public sources; there is no vendor token or entitlement to check."""
    owned = feed is None
    feed = feed or Market(settings, db)
    try:
        probes = feed.probe()
        reachable = {name for name, item in probes.items() if item.get("reachable")}
        successful = set()
        for capability, sources in CAPABILITY_SOURCES.items():
            usable = [source for source in sources if source == "derived" or source in reachable]
            previous = (db.rows("SELECT data FROM capabilities WHERE endpoint=%s", (capability,)) or [{}])[0]
            earned = previous.get("data") or {}
            # 派生能力没有可探测的源，「契约已验证」就是推导代码本身；其余能力由真实采集在
            # Market._qualify 里置位。探测只能证明源此刻可达，证明不了响应满足契约，所以这里只
            # 沿用真实采集留下的证据，绝不因为探测成功就把它写成已验证。
            derived = sources == ("derived",)
            verified = derived or (bool(earned.get("schema_verified")) and bool(usable))
            evidence = {
                "sources": list(sources),
                "reachable_sources": usable,
                "schema_verified": verified,
                "probe": {source: probes.get(source, {}) for source in sources if source in probes},
            }
            if derived:
                evidence.update(source="derived", verified_at=now().isoformat())
            elif verified:
                # 真实采集留下的来源、行数与时间必须保留：探测没有资格改写它们。
                evidence.update(
                    source=earned.get("source"),
                    rows=earned.get("rows"),
                    verified_at=earned.get("verified_at"),
                )
            db.capability(capability, "reachable" if usable else "blocked", evidence)
            if usable:
                successful.add(capability)
        from quant_platform.pipeline import DAILY_CAPABILITIES, MINUTE_CAPABILITIES

        required = DAILY_CAPABILITIES | {"quotes"}
        with db.transaction() as conn:
            # 旧供应商的端点行必须删掉：`data_quality` 把它们读成永久阻塞项，运维面板会一直显示
            # 十几个平台已经没有的能力，真正的缺口被埋在里面。
            conn.execute(
                "DELETE FROM capabilities WHERE NOT(endpoint=ANY(%s))", (list(CAPABILITY_SOURCES),)
            )
            dependencies = {
                "securities": {"securities"},
                "calendar": {"calendar"},
                "history": {"history", "factors"},
                "benchmark": {"benchmark"},
                "quotes": {"quotes"},
                "security_history": {"security_history"},
                "constraints": {"constraints"},
                "minute_history": {"minutes", "calendar"},
                "minute_live": {"minutes", "calendar"},
                "minute_repair": {"minutes", "calendar"},
            }
            kinds = [kind for kind, endpoints in dependencies.items() if endpoints <= successful]
            conn.execute(
                "UPDATE jobs SET status='pending',available_at=now(),failures=0 "
                "WHERE status='blocked' AND kind=ANY(%s)",
                (kinds,),
            )
            # 升级过的部署会留下已退役种类的任务（令牌时代把主表采集叫 `directory`）。它们既没有
            # 处理器，也不会被上面的解除阻塞逻辑碰到，只会作为「阻塞」一直挂在运维面板上，把真正的
            # 缺口埋掉。显式判失败，让「这个种类已经不存在」可见。
            from quant_platform.jobs.worker import JOB_KINDS

            conn.execute(
                "UPDATE jobs SET status='failed',error='Job kind is retired: no handler exists in this "
                "release. Re-enqueue the equivalent current kind.' "
                "WHERE status IN ('pending','blocked') AND NOT(kind=ANY(%s))",
                (sorted(JOB_KINDS),),
            )
            db.set_setting(
                conn,
                "doctor",
                {
                    "checked_at": now().isoformat(),
                    "sources": probes,
                    "reachable": sorted(reachable),
                    "successful": sorted(successful),
                    "required_available": required <= successful,
                    "intraday_available": (DAILY_CAPABILITIES | MINUTE_CAPABILITIES) <= successful,
                },
            )
        return {
            "sources": probes,
            "successful": sorted(successful),
            "missing": sorted(required - successful),
            "intraday_missing": sorted((DAILY_CAPABILITIES | MINUTE_CAPABILITIES) - successful),
            "live_soak": "pending",
        }
    finally:
        if owned:
            feed.close()


def maintenance(db, settings, job):
    at = now()
    with db.publication(job) as conn:
        # Create future partitions only; the default partition safely holds initial-day data.
        # Neither operation can run as plain DDL here: the worker connects as `quant_worker` and the
        # partitioned parents belong to the migration user, so the ownership checks that PostgreSQL
        # applies to `CREATE TABLE ... PARTITION OF` and `DROP TABLE` would reject every statement.
        # Migration 0010 exposes them as SECURITY DEFINER helpers keyed by (table, day); the table
        # allow-list in those helpers is the same one enumerated by PARTITION_RETIRE_DAYS.
        for offset in (1, 2, 3):
            day = (at + timedelta(days=offset)).date()
            for table in PARTITION_RETIRE_DAYS:
                if table == "minute_bars" and conn.execute(
                    "SELECT 1 FROM minute_bars_default WHERE bar_end>=%s AND bar_end<%s LIMIT 1",
                    (day, day + timedelta(days=1)),
                ).fetchone():
                    continue
                conn.execute("SELECT ensure_range_partition(%s,%s)", (table, day))
        for table, column, days in (
            ("quotes", "collected_at", 7),
            ("alerts", "recorded_at", 365),
            ("reports", "created_at", 365),
            ("basket_observations", "minute", 365),
            ("quota", "window_start", 2),
            ("alert_keys", "recorded_at", 365),
            ("alert_state", "updated_at", 365),
            ("heartbeats", "updated_at", 2),
            ("audit", "created_at", 365),
            ("qualification_samples", "minute", 365),
        ):
            # Bound deletion by tableoid+ctid; ctid alone is not unique across partitions.
            conn.execute(
                sql.SQL(
                    "DELETE FROM {} t USING (SELECT tableoid,ctid FROM {} WHERE {}<%s LIMIT 10000) old "
                    "WHERE t.tableoid=old.tableoid AND t.ctid=old.ctid"
                ).format(sql.Identifier(table), sql.Identifier(table), sql.Identifier(column)),
                (at - timedelta(days=days),),
            )
        partitions = conn.execute(
            "SELECT c.relname,p.relname AS parent FROM pg_inherits i "
            "JOIN pg_class c ON c.oid=i.inhrelid JOIN pg_class p ON p.oid=i.inhparent "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
            "AND p.relname = ANY(%s) ORDER BY c.relname",
            (list(PARTITION_RETIRE_DAYS),),
        ).fetchall()
        retired = 0
        for partition in partitions:
            name, parent = partition["relname"], partition["parent"]
            if not re.fullmatch(parent + r"_\d{8}", name):
                continue
            day = datetime.strptime(name[-8:], "%Y%m%d").date()
            if day >= (at - timedelta(days=PARTITION_RETIRE_DAYS[parent])).date():
                continue
            # The helper re-checks the parent/child link and only drops a partition that holds no
            # rows, so a day that was collected can never be reclaimed here.
            if conn.execute("SELECT retire_empty_partition(%s,%s) AS retired", (parent, day)).fetchone()["retired"]:
                retired += 1
                if retired >= 10:
                    break
        conn.execute(
            "DELETE FROM jobs WHERE id IN (SELECT j.id FROM jobs j WHERE j.status='complete' "
            "AND j.updated_at<now()-interval '30 days' AND j.kind!='minute_history' "
            "AND NOT EXISTS(SELECT 1 FROM pipeline_steps s WHERE s.job_id=j.id) "
            "AND NOT EXISTS(SELECT 1 FROM research_collections c WHERE c.job_id=j.id) "
            "AND NOT EXISTS(SELECT 1 FROM research_experiments e WHERE e.job_id=j.id) "
            "AND NOT EXISTS(SELECT 1 FROM diagnostic_inputs i WHERE i.job_id=j.id) "
            "AND NOT EXISTS(SELECT 1 FROM job_dependencies d WHERE d.job_id=j.id OR d.parent_id=j.id) "
            "ORDER BY j.id LIMIT 10000)"
        )
        conn.execute(
            "DELETE FROM datasets d WHERE id IN (SELECT id FROM datasets WHERE endpoint='quotes' "
            "AND fetched_at<now()-interval '365 days' ORDER BY id LIMIT 10000) "
            "AND NOT EXISTS(SELECT 1 FROM quotes q WHERE q.id=d.id) "
            "AND NOT EXISTS(SELECT 1 FROM latest_quotes q WHERE q.dataset_id=d.id)"
        )
        from quant_platform.storage.artifacts import prune_orphans

        prune_orphans(conn, settings, at)
        backup_state = db.setting("backup", {})
        old = not backup_state or at - datetime.fromisoformat(backup_state["at"]) > timedelta(hours=30)
        settings.artifact_root.mkdir(parents=True, exist_ok=True)
        low = shutil.disk_usage(settings.artifact_root).free < 2 * 1024**3
        for key, active in (("backup_overdue", old), ("low_disk", low)):
            conn.execute(
                "INSERT INTO incidents VALUES(%s,%s,now()) ON CONFLICT(key) DO UPDATE "
                "SET data=excluded.data,updated_at=now()",
                (key, jsonb({"active": active})),
            )


def pg_environment(dsn):
    """Preserve explicit libpq options without putting credentials in process arguments."""
    parameters = conninfo_to_dict(dsn)
    variables = {
        option.keyword.decode(): option.envvar.decode() for option in pq.Conninfo.get_defaults() if option.envvar
    }
    if not parameters.get("dbname") or any(key not in variables for key in parameters):
        raise ValueError("Backup connections require an explicit database and environment-compatible libpq options.")
    # Ambient service/SSL settings must not redirect or weaken the configured connection.
    env = {key: value for key, value in os.environ.items() if not key.startswith("PG")}
    env["PGCONNECT_TIMEOUT"] = "5"
    env.update({variables[key]: value for key, value in parameters.items()})
    return env


def backup(db, settings, job):
    from quant_platform.storage.artifacts import backup as snapshot_backup

    return snapshot_backup(db, settings, job)


def restore_check(archive, target_dsn, source_db=None, fault_at=None, artifact_root=None):
    """Verify a consistent database/artifact bundle in an isolated destination."""
    from quant_platform.storage.artifacts import restore_check as restore_bundle

    return restore_bundle(archive, target_dsn, source_db, fault_at, artifact_root)
