"""Minute and point-in-time reference collection for Qlib datasets."""

from datetime import date, datetime, time

from quant_platform.domain import CN, digest, now, number, provider_code, symbol
from quant_platform.providers import Deferred, ProviderError
from quant_platform.providers.tushare import parse_date
from quant_platform.storage import jsonb


def minutes(db, feed, job):
    payload = job["payload"]
    endpoint = {"minute_history": "stk_mins", "minute_live": "rt_min", "minute_repair": "rt_min_daily"}[job["kind"]]
    start = datetime.fromisoformat(payload["start"]) if payload.get("start") else None
    end = datetime.fromisoformat(payload["end"]) if payload.get("end") else None
    rows = feed.minutes(endpoint, payload["symbols"], start, end)
    rows = [r for r in rows if r["finalized"]]
    if not rows:
        raise Deferred("Waiting for finalized authorized minute bars.", 30)
    for row in rows:
        if db.calendar_open(row["bar_end"].astimezone(CN).date()) is not True:
            raise Deferred("Minute calendar not verified.", 60)
    with db.publication(job) as conn:
        identity = [{k: v for k, v in r.items() if k != "available_at"} for r in rows]
        did = db.dataset(conn, endpoint, digest(payload), identity, {"frequency": "5min", "units": "CNY/shares"})
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
    code = symbol(job["payload"]["symbol"])
    rows = feed.request("namechange", {"ts_code": provider_code(code)})
    if not rows or len(rows) >= 6000:
        raise ProviderError("Historical security names are empty or potentially truncated.")
    parsed = []
    for r in rows:
        if symbol(r["ts_code"]) != code:
            raise ProviderError("Historical security identity mismatch.")
        start = parse_date(r["start_date"])
        end = parse_date(r["end_date"]) if r.get("end_date") else None
        announced = parse_date(r["ann_date"]) if r.get("ann_date") else None
        parsed.append(
            {
                "symbol": code,
                "name": r["name"],
                "start": str(start),
                "end": str(end) if end else None,
                "risk_warning": "ST" in r["name"].upper() or "退" in r["name"],
                "announcement_verified": announced is not None,
                "available_at": datetime.combine(announced or start, time(0), CN),
            }
        )
    with db.publication(job) as conn:
        did = db.dataset(conn, "namechange", code, parsed, {"point_in_time": True})
        for r in parsed:
            conn.execute(
                "INSERT INTO security_states VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (code, r["start"], did, r["available_at"], jsonb(r)),
            )


def constraints(db, feed, job):
    day = date.fromisoformat(job["payload"]["day"])
    params = {"trade_date": day.strftime("%Y%m%d")}
    limits = feed.request("stk_limit", params)
    events = feed.request("suspend_d", params)
    if not limits or len(limits) >= 6000 or len(events) >= 6000:
        raise ProviderError("Trading constraints are empty or potentially truncated.")
    suspended = set()
    for event in events:
        if parse_date(event["trade_date"]) != day:
            raise ProviderError("Misdated suspension event.")
        if event["suspend_type"] == "S":
            suspended.add(symbol(event["ts_code"]))
    parsed, seen = [], set()
    for row in limits:
        try:
            code = symbol(row["ts_code"])
        except ValueError:
            continue
        upper, lower = number(row["up_limit"]), number(row["down_limit"])
        if (
            code in seen
            or parse_date(row["trade_date"]) != day
            or upper is None
            or lower is None
            or not 0 < lower < upper
        ):
            raise ProviderError("Invalid dated price limits.")
        seen.add(code)
        parsed.append(
            {"symbol": code, "day": day, "limit_up": upper, "limit_down": lower, "suspended": code in suspended}
        )
    with db.publication(job) as conn:
        did = db.dataset(conn, "constraints", str(day), parsed, {"sources": ["stk_limit", "suspend_d"]})
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
