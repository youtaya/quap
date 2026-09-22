"""Validate outside transactions; publish data and downstream jobs atomically."""

from datetime import date, timedelta

from quant_platform.domain import CN, digest, now, number
from quant_platform.providers import Deferred, ProviderError
from quant_platform.providers.tushare import parse_date
from quant_platform.storage import jsonb


def reference(db, feed, job):
    kind = job["kind"]
    if kind == "directory":
        rows = feed.securities()
        with db.publication(job) as conn:
            for code, row in rows.items():
                conn.execute(
                    "INSERT INTO instruments VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now()) ON CONFLICT(symbol) "
                    "DO UPDATE SET name=excluded.name,board=excluded.board,exchange=excluded.exchange,"
                    "list_date=excluded.list_date,delist_date=excluded.delist_date,status=excluded.status,"
                    "data=excluded.data,fetched_at=now()",
                    (
                        code,
                        row["name"],
                        row["board"],
                        row["exchange"],
                        row["list_date"],
                        row["delist_date"],
                        row["list_status"],
                        jsonb(row),
                    ),
                )
            conn.execute("UPDATE instruments SET status='U',fetched_at=now() WHERE NOT(symbol=ANY(%s))", (list(rows),))
            db.set_setting(
                conn,
                "directory",
                {"fetched_at": now().isoformat(), "count": len(rows), "symbols": sorted(rows), "hash": digest(rows)},
            )
    else:
        today = now().astimezone(CN).date()
        records = []
        for exchange in ("SSE", "SZSE"):
            rows = feed.request(
                "trade_cal",
                {
                    "exchange": exchange,
                    "start_date": (today - timedelta(days=1200)).strftime("%Y%m%d"),
                    "end_date": (today + timedelta(days=90)).strftime("%Y%m%d"),
                },
            )
            seen = set()
            for row in rows:
                day = parse_date(row["cal_date"])
                if day in seen or row["exchange"] != exchange or row["is_open"] not in (0, 1, "0", "1"):
                    raise ProviderError("Conflicting exchange calendar.")
                seen.add(day)
                records.append((exchange, day, bool(int(row["is_open"]))))
            if today not in seen:
                raise ProviderError("Calendar does not cover today.")
        with db.publication(job) as conn:
            for exchange, day, opened in records:
                conn.execute(
                    "INSERT INTO calendars VALUES(%s,%s,%s,now()) ON CONFLICT(exchange,day) "
                    "DO UPDATE SET is_open=excluded.is_open,fetched_at=now()",
                    (exchange, day, opened),
                )


def benchmark(db, feed, job):
    from quant_platform.providers.tushare import prices, positive

    target = date.fromisoformat(job["payload"]["day"])
    rows = feed.request(
        "index_daily",
        {
            "ts_code": "000300.SH",
            "start_date": (target - timedelta(days=1200)).strftime("%Y%m%d"),
            "end_date": target.strftime("%Y%m%d"),
        },
    )
    parsed, seen = [], set()
    for row in rows:
        day = parse_date(row["trade_date"])
        if row["ts_code"] != "000300.SH" or day > target or day in seen:
            raise ProviderError("Invalid benchmark history.")
        seen.add(day)
        parsed.append(
            {
                "symbol": "SH000300",
                "day": day,
                **prices(row),
                "volume": positive(row.get("vol"), 100),
                "turnover": positive(row.get("amount"), 1000),
            }
        )
    if not parsed or len(parsed) >= 6000:
        raise ProviderError("Benchmark history is empty or truncated.")
    parsed.sort(key=lambda r: r["day"])
    with db.publication(job) as conn:
        did = db.dataset(conn, "index_daily", "SH000300", parsed, {"kind": "benchmark", "as_of": str(target)})
        for row in parsed:
            conn.execute(
                "INSERT INTO daily_bars VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                ("SH000300", row["day"], did, jsonb(row)),
            )


def history(db, feed, job):
    day = date.fromisoformat(job["payload"]["day"])
    codes = {
        r["symbol"]
        for r in db.rows(
            "SELECT symbol FROM instruments WHERE list_date<=%s " "AND (delist_date IS NULL OR delist_date>=%s)",
            (day, day),
        )
    }
    if not codes:
        raise Deferred("Waiting for verified security master.", 300)
    factors = feed.daily_partition("adj_factor", day, codes, job=job)
    rows = feed.daily_partition("daily", day, codes, job=job) if job["kind"] == "daily" else []
    if not factors or (job["kind"] == "daily" and not rows):
        raise Deferred("Daily data not published; retaining previous revision.", 1800)
    factor_codes = {r["symbol"] for r in factors}
    suspension_events = []
    capability = db.rows("SELECT status FROM capabilities WHERE endpoint='suspend_d'")
    if rows and capability and capability[0]["status"] == "reachable":
        try:
            events = feed.request("suspend_d", {"trade_date": day.strftime("%Y%m%d")})
            if len(events) >= 6000 or any(
                parse_date(e["trade_date"]) != day or e["suspend_type"] not in {"S", "R"} for e in events
            ):
                raise ProviderError("Suspension events are truncated or invalid.")
            suspension_events = events
        except ProviderError:
            pass
    metadata = {
        "day": str(day),
        "total": len(codes),
        "factor_count": len(factors),
        "bar_count": len(rows),
        "missing_bars": sorted(codes - {r["symbol"] for r in rows}) if rows else [],
        "missing_reason": "unknown; no inferred suspension",
        "price_unit": "unadjusted CNY",
        "volume_unit": "shares",
        "turnover_unit": "CNY",
        "suspension_events": suspension_events,
        "suspension_note": "Reported S/R events are not proof that a missing bar is a full-day suspension.",
    }
    with db.publication(job) as conn:
        fid = db.dataset(
            conn, "adj_factor", str(day), factors, metadata, "complete" if factor_codes == codes else "partial"
        )
        for row in factors:
            conn.execute(
                "INSERT INTO factors VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (row["symbol"], day, fid, row["factor"]),
            )
        if rows:
            did = db.dataset(
                conn, "daily", str(day), rows, metadata, "complete" if len(rows) == len(codes) else "partial"
            )
            for row in rows:
                conn.execute(
                    "INSERT INTO daily_bars VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (row["symbol"], day, did, jsonb(row)),
                )
            # The scheduler coalesces this durable dirty marker into bounded analysis runs.
            db.set_setting(conn, "analysis_dirty", {"daily": did, "factors": fid, "at": now().isoformat()})
            repair = job["payload"].get("repair", 0)
            if (len(rows) != len(codes) or factor_codes != codes) and repair < 3:
                retry = now() + timedelta(minutes=30)
                db.enqueue(
                    conn,
                    "daily",
                    "history",
                    f"repair:{day}:{int(retry.timestamp()) // 1800}",
                    {"day": str(day), "repair": repair + 1},
                    -10,
                    retry,
                )


def quotes(db, feed, job):
    day = now().astimezone(CN).date()
    from quant_platform.domain import session

    if not session(now(), db.calendar_open(day))[1] or db.setting("polling_paused", False):
        with db.publication(job):
            return
    codes = job["payload"]["symbols"]
    rows = feed.quotes(codes)
    prior = db.rows("SELECT max(day) AS day FROM calendars WHERE exchange='SSE' AND is_open AND day<%s", (day,))[0][
        "day"
    ]
    baseline = {}
    if prior:
        baseline = {
            r["symbol"]: r
            for r in db.rows(
                "SELECT DISTINCT ON(b.symbol) b.symbol,b.data,f.factor AS old_factor,n.factor AS new_factor "
                "FROM daily_bars b LEFT JOIN LATERAL (SELECT factor FROM factors WHERE symbol=b.symbol AND day=b.day "
                "ORDER BY dataset_id DESC LIMIT 1) f ON true LEFT JOIN LATERAL "
                "(SELECT factor FROM factors WHERE symbol=b.symbol AND day=%s ORDER BY dataset_id DESC LIMIT 1) n ON true "
                "WHERE b.day=%s AND b.symbol=ANY(%s) ORDER BY b.symbol,b.dataset_id DESC",
                (day, prior, codes),
            )
        }
    for row in rows:
        old = baseline.get(row["symbol"], {})
        if old.get("old_factor") and old.get("new_factor") and row["pre_close"]:
            expected = old["data"]["close"] * old["old_factor"] / old["new_factor"]
            row["reference_verified"] = abs(expected - row["pre_close"]) <= max(0.011, expected * 0.0005)
    with db.publication(job) as conn:
        identity = [{k: v for k, v in row.items() if k != "received_at"} for row in rows]
        did = db.dataset(
            conn,
            "rt_k",
            digest(codes),
            identity,
            {"requested": len(codes), "received": len(rows), "volume_unit": "shares", "turnover_unit": "CNY"},
        )
        conn.execute("INSERT INTO quotes VALUES(now(),%s,%s)", (did, jsonb(rows)))
        for row in rows:
            conn.execute(
                "INSERT INTO latest_quotes VALUES(%s,%s,%s) ON CONFLICT(symbol) DO UPDATE "
                "SET dataset_id=excluded.dataset_id,data=excluded.data",
                (row["symbol"], did, jsonb(row)),
            )
        db.enqueue(conn, "intraday", "analysis", f"intraday:{did}:{job['id']}", {"dataset_id": did}, 100)
