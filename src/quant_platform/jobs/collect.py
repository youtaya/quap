"""Validate outside transactions; publish data and downstream jobs atomically.

Every capability is served by :class:`quant_platform.providers.market.Market`, which degrades
通达信 → 东方财富 → 新浪 → 腾讯 and records the source that actually answered. Collection needs no
vendor token, and the dataset metadata names the source that served each capability.
"""

from collections import defaultdict
from datetime import date, time, timedelta

from quant_platform.domain import CN, digest, now
from quant_platform.providers import Deferred, ProviderError
from quant_platform.providers.market import Market
from quant_platform.storage import jsonb

BAR_FIELDS = ("open", "high", "low", "close", "pre_close", "volume", "turnover")
REPAIR_LIMIT = 3
# 源配额按分钟窗口补充，所以被配额截断的一轮只需等一个窗口，不需要等半小时。等待时间比窗口略长，
# 让 `quota` 里那个分钟桶确定已经翻页。
QUOTA_RETRY_SECONDS = 70
# 收盘时刻：当日 15:00（Asia/Shanghai）之后，当日 K 线才是最终日线。
CLOSE = time(15, 0)


def closed_session(day, at):
    """Whether ``day``'s daily bar is final at ``at``; an open session's bar is still forming."""
    local = at.astimezone(CN)
    return day < local.date() or (day == local.date() and local.time() >= CLOSE)


def collected_through(conn, endpoint, start):
    """Whether collection for ``endpoint`` has already reached back to ``start``.

    One request per security covers the whole window, so a security holding the newest session looks
    complete even when the request that produced it never reached the window's first session. Widening
    the window — a deeper ``history_sessions``, or the lead-in session the scheduler adds so the
    oldest session's price limits are derivable — must therefore re-fetch, or the extra sessions are
    never collected and the admission check stays unsatisfied forever. The earliest dated session ever
    published for the endpoint is the durable record of how far back collection has gone.
    """
    row = conn.execute(
        "SELECT min(scope) AS earliest FROM datasets WHERE endpoint=%s AND scope ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'",
        (endpoint,),
    ).fetchone()
    return bool(row["earliest"]) and date.fromisoformat(row["earliest"]) <= start


def completed_codes(conn, kind, end, codes):
    """Securities that already hold everything ``kind`` requires for the window ending at ``end``.

    A ``factors`` job only needs the factor: the morning refresh runs while today's bar is still
    forming, so also demanding a bar would make that job retry forever. A ``history`` job needs both,
    because a bar without an adjustment factor cannot enter a generation.

    This answers "is the newest session already in hand", which is only conclusive once the caller has
    confirmed the window itself was already collected: see ``collected_through``.
    """
    if kind == "factors":
        sql = "SELECT DISTINCT symbol FROM factors WHERE day=%s AND symbol=ANY(%s)"
    else:
        sql = (
            "SELECT b.symbol FROM daily_bars b JOIN LATERAL "
            "(SELECT 1 FROM factors WHERE symbol=b.symbol AND day=b.day ORDER BY dataset_id DESC LIMIT 1) f "
            "ON true WHERE b.day=%s AND b.symbol=ANY(%s)"
        )
    return {row["symbol"] for row in conn.execute(sql, (end, sorted(codes))).fetchall()}


def window_start(db, settings, day):
    """First session of the history window: the verified calendar when it reaches back far enough."""
    rows = db.rows(
        "SELECT day FROM calendars WHERE exchange='SSE' AND is_open AND day<=%s ORDER BY day DESC LIMIT %s",
        (day, settings.history_sessions),
    )
    if len(rows) == settings.history_sessions:
        return rows[-1]["day"]
    # Calendar coverage is incomplete; widen the request window so the first bootstrap still works.
    return day - timedelta(days=int(settings.history_sessions * 1.6) + 60)


_LISTED_SQL = (
    "SELECT symbol FROM instruments WHERE status='L' AND (list_date IS NULL OR list_date<=%s) "
    "AND (delist_date IS NULL OR delist_date>=%s)"
)
_TRACKED_SQL = (
    "SELECT r.data FROM baskets b JOIN LATERAL (SELECT * FROM basket_revisions WHERE basket_id=b.id "
    "AND effective_day<=%s ORDER BY effective_day DESC,revision DESC LIMIT 1) r ON true "
    "WHERE NOT b.archived AND NOT b.paused"
)


def scoped_codes_on(conn, settings, day):
    """``scoped_codes`` read through a connection the caller already holds.

    The pipeline needs the same set as collection, and it holds a transaction of its own, so the
    definition lives here once and both callers share it. Two copies would drift, and a drift here
    means the platform trains on a different universe than the one it collects.
    """
    listed = {row["symbol"] for row in conn.execute(_LISTED_SQL, (day, day)).fetchall()}
    if settings.history_scope == "all":
        return listed
    tracked = {
        code for row in conn.execute(_TRACKED_SQL, (day,)).fetchall() for code in (row["data"].get("members") or [])
    }
    row = conn.execute("SELECT value FROM settings WHERE key='index_members'").fetchone()
    membership = row["value"] if row else {}
    members = set((membership or {}).get("symbols") or [])
    return listed & (members | tracked)


def scoped_codes(db, settings, day):
    """Default history scope: listed CSI 300 members and unpaused observation baskets.

    ``SH000300`` is not part of this set; the benchmark is a separate ``benchmark`` job. Under the
    default ``index`` scope an unresolved membership yields an empty set, and the caller defers.
    """
    with db.transaction() as conn:
        return scoped_codes_on(conn, settings, day)


def reference(db, feed, job):
    if job["kind"] == "securities":
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
            # 成分股取不到时绝不能让已写好的主表回滚：默认 `history_scope=index` 下范围会因此为空，
            # 但 `all` 范围不受影响。缺口显式记进 directory，由操作者用 doctor 复核。
            try:
                members = feed.constituents()
                membership = {
                    "index": "SH000300",
                    "symbols": sorted(members & set(rows)),
                    "fetched_at": now().isoformat(),
                    "source": feed.last_sources.get("constituents"),
                    "dated": False,
                    "error": None,
                }
            except ProviderError as exc:
                membership = {
                    "index": "SH000300",
                    "symbols": [],
                    "fetched_at": now().isoformat(),
                    "source": None,
                    "dated": False,
                    "error": str(exc),
                }
            # 上市日期由东方财富单独补齐（见 ``Market._attach_listing_dates``）。主表来源与日期来源
            # 是两个不同的问题：通达信/新浪做主表时日期仍可能补上，所以这里分开记录，缺口才看得见。
            dated = sum(1 for row in rows.values() if row["list_date"])
            db.set_setting(
                conn,
                "directory",
                {
                    "fetched_at": now().isoformat(),
                    "count": len(rows),
                    "symbols": sorted(rows),
                    "hash": digest(rows),
                    "source": feed.last_sources.get("securities"),
                    "list_dates_verified": dated == len(rows),
                    "list_dates_count": dated,
                    "list_dates_filled": getattr(feed, "listing_dates_filled", 0),
                    "index_members": membership,
                },
            )
            if membership["symbols"]:
                db.set_setting(
                    conn,
                    "index_members",
                    {
                        "index": membership["index"],
                        "symbols": membership["symbols"],
                        "fetched_at": membership["fetched_at"],
                        "dated": False,
                    },
                )
        return
    settings = feed.settings
    today = now().astimezone(CN).date()
    start = today - timedelta(days=settings.history_sessions * 2 + 366)
    calendars = feed.calendar(start, today + timedelta(days=90))
    records = []
    for exchange in ("SSE", "SZSE"):
        days = calendars.get(exchange) or {}
        if today not in days:
            raise ProviderError("Calendar does not cover today.")
        for day, opened in sorted(days.items()):
            records.append((exchange, day, bool(opened)))
    with db.publication(job) as conn:
        for exchange, day, opened in records:
            conn.execute(
                "INSERT INTO calendars VALUES(%s,%s,%s,now()) ON CONFLICT(exchange,day) "
                "DO UPDATE SET is_open=excluded.is_open,fetched_at=now()",
                (exchange, day, opened),
            )


def benchmark(db, feed, job):
    target = date.fromisoformat(job["payload"]["day"])
    start = window_start(db, feed.settings, target)
    result = feed.benchmark(start, target)
    parsed, seen = [], set()
    for row in result["bars"]:
        if row["day"] > target or row["day"] in seen or row["close"] is None:
            raise ProviderError("Invalid benchmark history.")
        seen.add(row["day"])
        parsed.append({"symbol": "SH000300", "day": row["day"], **{key: row[key] for key in BAR_FIELDS}})
    if not parsed:
        raise ProviderError("Benchmark history is empty.")
    parsed.sort(key=lambda row: row["day"])
    metadata = {"kind": "benchmark", "as_of": str(target), "source": result["source"]}
    with db.publication(job) as conn:
        did = db.dataset(conn, "benchmark", "SH000300", parsed, metadata)
        for row in parsed:
            conn.execute(
                "INSERT INTO daily_bars VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                ("SH000300", row["day"], did, jsonb(row)),
            )


def history(db, feed, job):
    """One request per security covering the whole window, then day-scoped publications.

    A single ``history`` job fetches ``[start, end]`` once per security and writes one ``history`` and
    one ``factors`` dataset per session that the window covers. That keeps the admission check (one
    partition per session per endpoint) intact without one vendor call per security per day. A
    security whose source failed is left out of the completed set so a bounded repair job can retry
    it; a missing bar is never reinterpreted as a suspension. ``start`` is the scheduler's collection
    window, which carries one lead-in session older than the analysis window so the oldest analysis
    session's price limits have a previous close to be derived from.

    The per-source budget is a per-minute window, so one round can only reach as many securities as
    the window allows. When it is exhausted the round stops and hands the remainder to a successor
    one window later, and only a round that made no progress at all consumes retry budget. Draining
    the whole security list first would instead record one pointless deferral per security and then
    spend the three-round budget on a backfill that needs dozens of rounds to finish.

    A ``factors`` job is the same request with bars suppressed: the morning refresh needs today's
    adjustment factor, but today's bar is still forming. A daily bar is published only for a session
    that has closed, so an intraday snapshot can never occupy ``daily_bars`` for good.
    """
    settings = feed.settings
    payload = job["payload"]
    factors_only = job["kind"] == "factors"
    at = now()
    end = date.fromisoformat(payload["end"])
    start = date.fromisoformat(payload["start"]) if payload.get("start") else window_start(db, settings, end)
    codes = scoped_codes(db, settings, end)
    if not codes:
        raise Deferred("Waiting for verified security master and index membership.", 300)
    with db.transaction() as conn:
        # 窗口比已采集到的范围更宽时，所有证券都要重取：只看最新会话会把「请求从未触及新的第一天」
        # 误判成「这只证券已经完成」。
        saved = completed_codes(conn, job["kind"], end, codes) if collected_through(conn, job["kind"], start) else set()
    bars_by_day, factors_by_day = defaultdict(list), defaultdict(list)
    failures, sources, factor_sources = {}, {}, {}
    open_sessions = set()
    quota_exhausted = False
    for code in sorted(codes):
        if code in saved:
            continue
        try:
            result = feed.history(code, start, end)
        except Deferred:
            # 配额耗尽会平等地影响后面每一只证券；继续遍历只会给剩下的几百只各记一条 Deferred。
            quota_exhausted = True
            break
        except ProviderError as exc:
            failures[code] = type(exc).__name__
            continue
        sources[code] = result["source"]
        factor_sources[code] = result["factor_source"]
        for row in result["bars"]:
            if row["day"] > end or row["close"] is None:
                continue
            if factors_only or not closed_session(row["day"], at):
                # 盘中未收盘的那根 K 线是半成品；缺失日线不等于停牌，也绝不写入。
                open_sessions.add(row["day"])
            else:
                bars_by_day[row["day"]].append({"symbol": code, "day": row["day"], **{k: row[k] for k in BAR_FIELDS}})
            factor = result["factors"].get(row["day"])
            if factor is not None:
                factors_by_day[row["day"]].append({"symbol": code, "day": row["day"], "factor": factor})
        if feed.db and job:
            feed.db.checkpoint(job, "history", code, {"end": str(end), "source": result["source"]})
    window = sorted(set(bars_by_day) | set(factors_by_day))
    if not window:
        if not quota_exhausted and len(saved) == len(codes):
            # 窗口内每只证券都已就绪：没有可发布的内容，但任务确实完成了。
            with db.publication(job):
                pass
            return
        if quota_exhausted:
            raise Deferred("Source quota exhausted before any security was fetched.", QUOTA_RETRY_SECONDS)
        raise ProviderError("No public source served the requested history window.")
    covered = {row["symbol"] for rows in factors_by_day.values() for row in rows}
    quality = "complete" if covered == codes and not failures else "partial"
    with db.publication(job) as conn:
        last_bar, last_factor = None, None
        for day in window:
            bars, factors = bars_by_day.get(day, []), factors_by_day.get(day, [])
            metadata = {
                "day": str(day),
                "window_start": str(start),
                "window_end": str(end),
                "total": len(codes),
                "bar_count": len(bars),
                "factor_count": len(factors),
                "missing_bars": sorted(codes - {row["symbol"] for row in bars}),
                "missing_factors": sorted(codes - {row["symbol"] for row in factors}),
                "missing_reason": "source did not serve the security; a missing bar is not a suspension",
                "failures": failures,
                "factors_only": factors_only,
                "open_sessions": sorted(str(day) for day in open_sessions),
                "sources": {code: sources[code] for code in sorted(sources)},
                "factor_sources": {code: factor_sources[code] for code in sorted(factor_sources)},
                "price_unit": "unadjusted CNY",
                "volume_unit": "shares",
                "turnover_unit": "CNY",
            }
            if bars:
                did = db.dataset(conn, "history", str(day), bars, metadata, quality)
                for row in bars:
                    conn.execute(
                        "INSERT INTO daily_bars VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (row["symbol"], row["day"], did, jsonb(row)),
                    )
                last_bar = did
            if factors:
                fid = db.dataset(conn, "factors", str(day), factors, metadata, quality)
                for row in factors:
                    conn.execute(
                        "INSERT INTO factors VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                        (row["symbol"], row["day"], fid, row["factor"]),
                    )
                last_factor = fid
        # The scheduler coalesces this durable dirty marker into bounded analysis runs.
        db.set_setting(
            conn,
            "analysis_dirty",
            {"history": last_bar, "factors": last_factor, "as_of": str(end), "at": now().isoformat()},
        )
        complete = completed_codes(conn, job["kind"], end, codes)
        if len(complete) < len(codes):
            # 有进展的一轮只是「还没走完」，不是失败；只有一无所获的一轮才消耗重试预算。
            budget = payload.get("repair", 0) + (0 if len(complete) > len(saved) else 1)
            if budget < REPAIR_LIMIT:
                delay = timedelta(seconds=QUOTA_RETRY_SECONDS) if quota_exhausted else timedelta(minutes=30)
                retry = now() + delay
                # 桶宽与等待时间一致：更细的键不会让下一轮撞上本轮尚未领取的续跑任务。
                bucket = int(retry.timestamp()) // (QUOTA_RETRY_SECONDS if quota_exhausted else 1800)
                db.enqueue(
                    conn,
                    job["kind"],
                    "history",
                    f"repair:{job['kind']}:{end}:{bucket}",
                    {"start": str(start), "end": str(end), "repair": budget},
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
            "quotes",
            digest(codes),
            identity,
            {
                "requested": len(codes),
                "received": len(rows),
                "source": feed.last_sources.get("quotes"),
                "volume_unit": "shares",
                "turnover_unit": "CNY",
            },
        )
        conn.execute("INSERT INTO quotes VALUES(now(),%s,%s)", (did, jsonb(rows)))
        for row in rows:
            conn.execute(
                "INSERT INTO latest_quotes VALUES(%s,%s,%s) ON CONFLICT(symbol) DO UPDATE "
                "SET dataset_id=excluded.dataset_id,data=excluded.data",
                (row["symbol"], did, jsonb(row)),
            )
        db.enqueue(conn, "intraday", "analysis", f"intraday:{did}:{job['id']}", {"dataset_id": did}, 100)


__all__ = ["Market", "benchmark", "history", "quotes", "reference", "scoped_codes", "window_start"]
