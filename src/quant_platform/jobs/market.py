"""Minute and point-in-time reference collection for Qlib datasets."""

from datetime import date, datetime, time

from quant_platform.domain import CN, digest, now, symbol
from quant_platform.providers import Deferred, ProviderError
from quant_platform.storage import jsonb

MINUTE_ENDPOINTS = {"minute_history": "stk_mins", "minute_live": "rt_min", "minute_repair": "rt_min_daily"}


def minutes(db, feed, job):
    payload = job["payload"]
    endpoint = MINUTE_ENDPOINTS[job["kind"]]
    start = datetime.fromisoformat(payload["start"]) if payload.get("start") else None
    end = datetime.fromisoformat(payload["end"]) if payload.get("end") else None
    rows = feed.minutes(endpoint, payload["symbols"], start, end)
    rows = [r for r in rows if r["finalized"]]
    if not rows:
        raise Deferred("Waiting for finalized five-minute bars.", 30)
    for row in rows:
        if db.calendar_open(row["bar_end"].astimezone(CN).date()) is not True:
            raise Deferred("Minute calendar not verified.", 60)
    with db.publication(job) as conn:
        identity = [{k: v for k, v in r.items() if k != "available_at"} for r in rows]
        did = db.dataset(
            conn,
            "minutes",
            digest(payload),
            identity,
            {
                "frequency": "5min",
                "units": "CNY/shares",
                "origin": job["kind"],
                "source": feed.last_sources.get("minutes"),
            },
        )
        for r in rows:
            conn.execute(
                "INSERT INTO minute_bars(symbol,bar_end,dataset_id,bar_start,available_at,open,high,low,close,volume,amount,finalized) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true) ON CONFLICT DO NOTHING",
                (
                    r["symbol"],
                    r["bar_end"],
                    did,
                    r["bar_start"],
                    r["available_at"],
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    r["volume"],
                    r["amount"],
                ),
            )
        db.set_setting(conn, "minute_dirty", {"watermark": did, "as_of": str(max(r["bar_end"] for r in rows))})


def security_history(db, feed, job):
    """Point-in-time risk state observed from the source-published security name.

    Public sources publish no dated name-change history, so the state starts the day it was observed.
    That makes the observation date part of the contract: ``build_generation`` only accepts a state
    whose ``effective_day`` reaches the decision date. The scheduler therefore passes the analysis
    target explicitly instead of letting ``now()`` decide — otherwise a monthly refresh stamped on
    the 1st would leave the previous month's final session with no covering state at all, and the
    input coverage would collapse to zero once a month. ``observed_lag_days`` records how far back
    the stamp reaches, so a backfilled state can never pass for a contemporaneous observation.
    """
    code = symbol(job["payload"]["symbol"])
    rows = db.rows("SELECT name,list_date FROM instruments WHERE symbol=%s", (code,))
    if not rows:
        raise Deferred("Waiting for the security master before recording risk state.", 1800)
    requested = job["payload"].get("day")
    at = datetime.combine(date.fromisoformat(requested), time(15), CN) if requested else None
    parsed = feed.security_history(code, rows[0]["name"], at)
    with db.publication(job) as conn:
        did = db.dataset(
            conn,
            "security_history",
            code,
            parsed,
            {
                "point_in_time": False,
                "observed": True,
                "source": "securities",
                "observed_day": parsed[0]["start"],
                "observed_lag_days": parsed[0]["observed_lag_days"],
            },
        )
        for r in parsed:
            conn.execute(
                "INSERT INTO security_states VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (code, r["start"], did, r["available_at"], jsonb(r)),
            )


def constraints(db, feed, job):
    """Derived price limits for the tracked universe, from stored closes only.

    Scoped exactly like ``history``: deriving limits for a security the platform never collects bars
    for would need one source request per listed A-share per session and could never converge.
    ``CAPABILITY_SOURCES`` declares ``constraints`` as ``derived``, so the previous close comes from
    ``daily_bars`` rather than from one history request per security per session.
    """
    from quant_platform.jobs.collect import scoped_codes

    day = date.fromisoformat(job["payload"]["day"])
    codes = sorted(scoped_codes(db, feed.settings, day))
    if not codes:
        raise Deferred("Waiting for the scoped security master and index membership.", 1800)
    names = {row["symbol"]: row["name"] for row in db.rows("SELECT symbol,name FROM instruments")}
    closes = {
        row["symbol"]: row["data"].get("close")
        for row in db.rows(
            "WITH b AS (SELECT DISTINCT ON(symbol,day) symbol,day,data FROM daily_bars "
            "WHERE symbol=ANY(%s) AND day<%s ORDER BY symbol,day DESC,dataset_id DESC) "
            "SELECT DISTINCT ON(symbol) symbol,data FROM b ORDER BY symbol,day DESC",
            (codes, day),
        )
    }
    parsed = feed.constraints(day, closes, names)
    if not parsed:
        raise Deferred("Waiting for previous-session closes before deriving limits.", 1800)
    with db.publication(job) as conn:
        did = db.dataset(
            conn,
            "constraints",
            str(day),
            parsed,
            {"derived": True, "limit_source": "previous_close_rule", "source": "daily_bars"},
        )
        for row in parsed:
            conn.execute(
                "INSERT INTO market_constraints VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (row["symbol"], day, did, now(), jsonb(row)),
            )


def frozen_pool(db, settings, day):
    existing = db.rows(
        "SELECT * FROM universe_snapshots WHERE day=%s AND data->>'kind'='intraday' ORDER BY created_at LIMIT 1", (day,)
    )
    if existing:
        return existing[0]["data"]
    from uuid import uuid4

    opened = datetime.combine(day, time(9, 30), CN)
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610241)")
        existing = conn.execute(
            "SELECT data FROM universe_snapshots WHERE day=%s AND data->>'kind'='intraday' LIMIT 1", (day,)
        ).fetchone()
        if existing:
            return existing["data"]
        portfolios = conn.execute(
            "SELECT p.id,r.revision,r.data FROM model_portfolios p JOIN LATERAL (SELECT revision,data FROM model_portfolio_revisions "
            "WHERE portfolio_id=p.id AND effective_from<=%s AND created_at<=%s "
            "ORDER BY effective_from DESC,revision DESC LIMIT 1) r ON true",
            (opened, opened),
        ).fetchall()
        watch = conn.execute(
            "SELECT revision,symbols FROM watchlist_revisions WHERE created_at<=%s " "ORDER BY revision DESC LIMIT 1",
            (opened,),
        ).fetchone()
        selected = {s for p in portfolios for s in p["data"]["weights"]} | set(watch["symbols"] if watch else [])
        if len(selected) > settings.intraday_pool_capacity:
            raise ProviderError("Explicit portfolio/watchlist selections exceed intraday pool capacity.")
        baseline = conn.execute(
            "SELECT r.id,r.data FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
            "JOIN model_versions m ON m.id=p.model_id JOIN model_evaluations e ON e.model_id=m.id "
            "WHERE r.frequency='day' AND r.portfolio_id IS NULL AND e.passed "
            "AND r.state='published' AND r.available_at<=%s AND r.effective_from<=%s "
            "AND r.valid_until>%s AND m.state='active' AND m.expires_at>%s "
            "AND r.policy_revision=(SELECT max(revision) FROM scan_policies WHERE created_at<=%s) "
            "ORDER BY r.as_of DESC LIMIT 1",
            (opened, opened, opened, opened, opened),
        ).fetchone()
        candidates = [r["symbol"] for r in baseline["data"].get("shortlist", [])][:200] if baseline else []
        room = settings.intraday_pool_capacity - len(selected)
        additions = [s for s in candidates if s not in selected]
        data = {
            "kind": "intraday",
            "symbols": sorted(selected | set(additions[:room])),
            "omitted": additions[room:],
            "daily_recommendation_id": str(baseline["id"]) if baseline else None,
            "frozen_at": opened.isoformat(),
            "watchlist_revision": watch["revision"] if watch else None,
            "portfolio_revisions": {str(p["id"]): p["revision"] for p in portfolios},
        }
        # Do not freeze an empty pool before initial data/model setup completes.
        if not data["symbols"]:
            return data
        conn.execute(
            "INSERT INTO universe_snapshots(id,day,content_hash,data) VALUES(%s,%s,%s,%s)",
            (uuid4(), day, digest({"day": day, **data}), jsonb(data)),
        )
        return data
