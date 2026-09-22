"""Idempotent scheduling and catch-up on verified exchange calendars."""

from datetime import datetime, timedelta, time

from quant_platform.domain import CN, digest, now, session
from quant_platform.storage import jsonb


def tick(db, settings, at=None):
    at = at or now()
    local = at.astimezone(CN)
    day = local.date()
    open_today = db.calendar_open(day)
    baskets = db.active_baskets(day)
    tracked = {code for basket in baskets if not basket["paused"] for code in basket["data"]["members"]}
    tracked.update(r["symbol"] for r in db.active_holdings())
    instruments = db.rows("SELECT symbol FROM instruments WHERE status='L' ORDER BY symbol")
    symbols = [r["symbol"] for r in instruments]
    reference = db.setting("directory", {})
    directory_fresh = bool(reference) and timedelta(0) <= at - datetime.fromisoformat(
        reference["fetched_at"]
    ) < timedelta(hours=48)
    with db.transaction() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(77610231) AS acquired").fetchone()["acquired"]:
            return
        for kind in ("calendar", "directory"):
            key = f"{kind}:{day}" if local.time() >= time(8, 30) else f"{kind}:{day - timedelta(days=1)}"
            db.enqueue(conn, kind, "history", key, priority=200)
        if open_today and local.time() >= time(9, 20):
            db.enqueue(conn, "factors", "history", f"morning-factors:{day}", {"day": str(day)}, 190)
        quote_blocked = conn.execute("SELECT 1 FROM capabilities WHERE endpoint='rt_k' AND status='blocked'").fetchone()
        if session(at, open_today)[1] and not db.setting("polling_paused", False) and not quote_blocked:
            busy = conn.execute(
                "SELECT 1 FROM jobs WHERE queue='quotes' AND status IN ('pending','running') LIMIT 1"
            ).fetchone()
            previous = conn.execute(
                "SELECT max(updated_at) AS at FROM jobs WHERE queue='quotes' AND status='complete'"
            ).fetchone()["at"]
            interval = db.setting("runtime_options", {}).get("quote_seconds", settings.quote_seconds)
            if not busy and (previous is None or (at - previous).total_seconds() >= interval):
                ordered = (
                    sorted(tracked.intersection(symbols)) + [s for s in symbols if s not in tracked]
                    if directory_fresh
                    else sorted(tracked.intersection(symbols))
                )
                cycle = int(at.timestamp())
                for i in range(0, len(ordered), 200):
                    db.enqueue(
                        conn,
                        "quotes",
                        "quotes",
                        f"quotes:{cycle}:{i}",
                        {"symbols": ordered[i : i + 200]},
                        100 if i == 0 else 50,
                    )
        cutoff = day if local.time() >= time(18, 30) else day - timedelta(days=1)
        days = conn.execute(
            "SELECT day FROM calendars WHERE exchange IN ('SSE','SZSE') AND is_open AND day<=%s "
            "GROUP BY day HAVING count(*)=2 ORDER BY day DESC LIMIT %s",
            (cutoff, settings.history_sessions),
        ).fetchall()
        if symbols and directory_fresh and days:
            target = days[0]["day"]
            db.set_setting(conn, "analysis_target", str(target))
            db.enqueue(conn, "benchmark", "history", f"benchmark:{target}:{day}", {"day": str(target)}, 150)
            for index, row in enumerate(days):
                date = row["day"]
                # Historical records are durable checkpoints. Recent dates revalidate daily; all dates weekly.
                cycle = str(day) if index < 5 else str(day - timedelta(days=day.weekday()))
                db.enqueue(conn, "daily", "history", f"daily:{date}:{cycle}", {"day": str(date)}, 100 - min(index, 99))
                db.enqueue(conn, "basics", "history", f"basics:{date}:{cycle}", {"day": str(date)}, 90 - min(index, 89))
            pending = conn.execute(
                "SELECT 1 FROM jobs WHERE kind IN ('daily','basics') AND status IN ('pending','running') LIMIT 1"
            ).fetchone()
            watermark = conn.execute(
                "SELECT coalesce(max(id),0) AS id FROM datasets WHERE endpoint IN ('daily','adj_factor','daily_basic')"
            ).fetchone()["id"]
            if watermark:
                # Debounce backfills: partial reports every 15 minutes, complete reports when collection drains.
                bucket = int(at.timestamp()) // 900
                signature = f"{target}:{watermark}" if not pending else f"partial:{target}:{bucket}"
                active = conn.execute(
                    "SELECT 1 FROM jobs WHERE kind='analyze' AND status IN ('pending','running') LIMIT 1"
                ).fetchone()
                if not active:
                    db.enqueue(conn, "analyze", "analysis", f"analyze:{signature}", {"day": str(target)}, 10)
        if open_today and (session(at, open_today)[1] or time(15) <= local.time() <= time(15, 5)):
            db.enqueue(
                conn,
                "qualification",
                "operations",
                f"qualification:{int(at.timestamp()) // 60}",
                {"at": at.isoformat()},
                100,
            )
        db.enqueue(conn, "maintenance", "operations", f"maintenance:{int(at.timestamp()) // 3600}", priority=10)
        db.enqueue(conn, "backup", "operations", f"backup:{day}")
        if local.time() >= time(9):
            db.enqueue(conn, "notify", "operations", f"notify:{day}", {"day": str(day)}, 80)
