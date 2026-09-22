"""Readiness, capability qualification, bounded retention and recoverable backups."""

import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from psycopg import pq, sql
from psycopg.conninfo import conninfo_to_dict

from quant_platform.domain import CN, now, fresh, session
from quant_platform.analysis import basket_summary
from quant_platform.providers import ProviderError, PermissionDenied
from quant_platform.providers.tushare import Tushare
from quant_platform.storage import jsonb


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
    required = {"stock_basic", "trade_cal", "daily", "adj_factor", "rt_k"}
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
    metrics["quota_usage"] = db.rows(
        "SELECT endpoint,window_start,sum(used) AS requests FROM quota "
        "WHERE window_start>now()-interval '1 day' GROUP BY endpoint,window_start ORDER BY window_start DESC LIMIT 100"
    )
    return {
        "provider": "tushare",
        "configured": bool(settings.tushare_token.get_secret_value()),
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
        "qlib": db.setting("qlib", {"enabled": settings.qlib_enabled, "status": "not_published"}),
        "incidents": db.rows("SELECT * FROM incidents ORDER BY updated_at DESC"),
        "qualification": db.setting("qualification", {"status": "pending", "required_trading_sessions": 2}),
        "data_health": "fresh" if total and verified == total and directory_fresh else "degraded_or_unavailable",
        "timestamp": at.isoformat(),
    }


def doctor(db, settings, feed=None):
    owned = feed is None
    feed = feed or Tushare(settings, db)
    today = now().astimezone(CN).date()
    completed = db.rows("SELECT max(day) AS day FROM calendars WHERE exchange='SSE' AND is_open AND day<%s", (today,))[
        0
    ]["day"]
    completed = completed or today - timedelta(days=1)
    requests = {
        "stock_basic": {"ts_code": "600895.SH"},
        "trade_cal": {"exchange": "SSE", "start_date": today.strftime("%Y%m%d"), "end_date": today.strftime("%Y%m%d")},
        "daily": {"ts_code": "600895.SH", "trade_date": completed.strftime("%Y%m%d")},
        "adj_factor": {"ts_code": "600895.SH", "trade_date": completed.strftime("%Y%m%d")},
        "rt_k": {"ts_code": "600895.SH"},
        "index_daily": {"ts_code": "000300.SH", "trade_date": completed.strftime("%Y%m%d")},
        "suspend_d": {"trade_date": completed.strftime("%Y%m%d")},
        "daily_basic": {"ts_code": "600895.SH", "trade_date": completed.strftime("%Y%m%d")},
    }
    successful = set()
    try:
        for endpoint, params in requests.items():
            try:
                rows = feed.request(endpoint, params, probe=True)
                if not rows and endpoint != "suspend_d":
                    db.capability(endpoint, "unverified", {"entitled": True, "rows": 0, "schema_verified": True})
                    continue
                successful.add(endpoint)
            except ProviderError:
                pass
        required = {"stock_basic", "trade_cal", "daily", "adj_factor", "rt_k"}
        with db.transaction() as conn:
            dependencies = {
                "directory": {"stock_basic"},
                "calendar": {"trade_cal"},
                "daily": {"daily", "adj_factor"},
                "factors": {"adj_factor"},
                "quotes": {"rt_k"},
                "benchmark": {"index_daily"},
                "basics": {"daily_basic"},
            }
            kinds = [kind for kind, endpoints in dependencies.items() if endpoints <= successful]
            conn.execute(
                "UPDATE jobs SET status='pending',available_at=now(),failures=0 "
                "WHERE status='blocked' AND kind=ANY(%s)",
                (kinds,),
            )
            db.set_setting(
                conn,
                "doctor",
                {
                    "checked_at": now().isoformat(),
                    "successful": sorted(successful),
                    "required_available": required <= successful,
                    "screen_available": "daily_basic" in successful,
                },
            )
        return {
            "successful": sorted(successful),
            "missing": sorted(required - successful),
            "missing_for_screen": [] if "daily_basic" in successful else ["daily_basic"],
            "live_soak": "pending",
        }
    finally:
        if owned:
            feed.close()


def maintenance(db, settings, job):
    at = now()
    with db.publication(job) as conn:
        # Create future partitions only; default partitions safely hold initial-day data.
        for offset in (1, 2, 3):
            start = (at + timedelta(days=offset)).date()
            end = start + timedelta(days=1)
            for table, column in (("quotes", "collected_at"), ("alerts", "recorded_at")):
                name = f"{table}_{start:%Y%m%d}"
                conn.execute(
                    sql.SQL("CREATE TABLE IF NOT EXISTS {} PARTITION OF {} FOR VALUES FROM ({}) TO ({})").format(
                        sql.Identifier(name), sql.Identifier(table), sql.Literal(str(start)), sql.Literal(str(end))
                    )
                )
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
            "AND p.relname IN ('quotes','alerts') ORDER BY c.relname"
        ).fetchall()
        retired = 0
        for partition in partitions:
            name, parent = partition["relname"], partition["parent"]
            if not re.fullmatch(parent + r"_\d{8}", name):
                continue
            day = datetime.strptime(name[-8:], "%Y%m%d").date()
            if day >= (at - timedelta(days=8 if parent == "quotes" else 366)).date():
                continue
            if not conn.execute(sql.SQL("SELECT 1 FROM {} LIMIT 1").format(sql.Identifier(name))).fetchone():
                conn.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(name)))
                retired += 1
                if retired >= 10:
                    break
        conn.execute(
            "DELETE FROM jobs WHERE id IN (SELECT id FROM jobs WHERE status='complete' "
            "AND updated_at<now()-interval '30 days' ORDER BY id LIMIT 10000)"
        )
        conn.execute(
            "DELETE FROM datasets d WHERE id IN (SELECT id FROM datasets WHERE endpoint='rt_k' "
            "AND fetched_at<now()-interval '365 days' ORDER BY id LIMIT 10000) "
            "AND NOT EXISTS(SELECT 1 FROM quotes q WHERE q.id=d.id) "
            "AND NOT EXISTS(SELECT 1 FROM latest_quotes q WHERE q.dataset_id=d.id)"
        )
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
    root = settings.backup_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    name = "quant-" + now().strftime("%Y%m%dT%H%M%S") + ".dump"
    temporary, target = root / (name + ".pending"), root / name
    result = subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(temporary)],
        env=pg_environment(settings.dsn),
        capture_output=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError("Backup failed; verify PostgreSQL client and backup volume.")
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    with target.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    with db.publication(job) as conn:
        db.set_setting(
            conn,
            "backup",
            {
                "at": now().isoformat(),
                "file": name,
                "bytes": target.stat().st_size,
                "sha256": checksum,
                "restore_verified": False,
            },
        )
    for old in sorted(root.glob("quant-????????T??????.dump"))[:-7]:
        if not old.is_symlink() and old.parent == root:
            old.unlink()


def restore_check(archive, target_dsn, source_db=None):
    """Only restore into an explicitly named empty verification database."""
    import psycopg

    env = pg_environment(target_dsn)
    if not env["PGDATABASE"].startswith("quant_restore_"):
        raise ValueError("Restore verification requires a separate quant_restore_* database.")
    with psycopg.connect(target_dsn) as conn:
        if conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]:
            raise ValueError("Verification database must be empty.")
    result = subprocess.run(
        ["pg_restore", "--exit-on-error", "--no-owner", "--dbname", env["PGDATABASE"], str(Path(archive).resolve())],
        env=env,
        capture_output=True,
        timeout=600,
    )
    if result.returncode:
        raise RuntimeError("Restore validation failed.")
    with psycopg.connect(target_dsn) as conn:
        result = {
            "schema": conn.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "reports": conn.execute("SELECT count(*) FROM reports").fetchone()[0],
            "restored": True,
        }
    with Path(archive).open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if source_db:
        with source_db.transaction() as conn:
            saved = conn.execute("SELECT value FROM settings WHERE key='backup' FOR UPDATE").fetchone()
            if saved and saved["value"].get("sha256") == checksum:
                source_db.set_setting(
                    conn,
                    "backup",
                    {
                        **saved["value"],
                        "restore_verified": True,
                        "verified_at": now().isoformat(),
                        "verification": result,
                    },
                )
    return result
