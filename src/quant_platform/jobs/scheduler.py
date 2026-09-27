"""Idempotent scheduling and catch-up on verified exchange calendars."""

from datetime import date, datetime, timedelta, time
from calendar import monthrange

from quant_platform.domain import CN, digest, now, session
from quant_platform.domain.workflow import PipelineBlocked, five_minute_end, intraday_window

# 分析窗口之外还要多采集一个会话。窗口内最早一天的价格限制由「它前一天的收盘」推导，而那一天落在
# 分析窗口之外：少了这个 lead-in，最早一天的 `constraints` 永远推不出来，准入判定也就永远不成立，
# 平台永远不会产出代次。lead-in 只提供一根收盘价，不进代次窗口。
LEAD_IN_SESSIONS = 1


def checkpoint(db, conn, key, identity):
    """Commit scheduling intent with its jobs, once per input revision."""
    value = digest(identity)
    prior = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    if prior and prior["value"] == value:
        return False
    db.set_setting(conn, key, value)
    return True


def source_days(conn, cutoff, count):
    return [
        row["day"]
        for row in conn.execute(
            "SELECT day FROM calendars WHERE exchange IN ('SSE','SZSE') AND is_open AND day<=%s "
            "GROUP BY day HAVING count(*)=2 ORDER BY day DESC LIMIT %s",
            (cutoff, count),
        ).fetchall()
    ]


def source_gaps(conn, days, count, factor_sessions):
    """Why the dated daily source window is not yet admissible; empty when it is.

    Raw bars and derived limits must cover the whole window, because the platform contract is
    unadjusted prices plus limits derived from the previous close. The limits for the window's oldest
    session come from the close of the session before it, which the collection window carries as a
    lead-in (see ``LEAD_IN_SESSIONS``); requiring a limit per session is therefore satisfiable.

    Adjustment factors are not the same window: they are bounded by the deepest front-adjusted series
    the reachable sources publish (腾讯 stops at 800 sessions and 东方财富 may be unreachable), so only
    a contiguous suffix of ``factor_sessions`` sessions is required. Demanding a factor for every
    session would make the admission check unsatisfiable, which stalls the platform instead of
    reporting a real gap.
    """
    if len(days) < count:
        return {"sessions": [len(days), count]}
    scopes = [str(day) for day in days]
    present = {
        row["endpoint"]: row["n"]
        for row in conn.execute(
            "SELECT endpoint,count(DISTINCT scope) AS n FROM datasets "
            "WHERE endpoint IN ('history','constraints') AND scope=ANY(%s) GROUP BY endpoint",
            (scopes,),
        ).fetchall()
    }
    adjusted = {
        row["scope"]
        for row in conn.execute(
            "SELECT DISTINCT scope FROM datasets WHERE endpoint='factors' AND scope=ANY(%s)", (scopes,)
        ).fetchall()
    }
    # ``days`` is most-recent-first, so the walk stops at the first unadjusted session.
    suffix = 0
    for day in days:
        if str(day) not in adjusted:
            break
        suffix += 1
    benchmark = conn.execute(
        "SELECT 1 FROM daily_bars WHERE symbol='SH000300' AND day=%s LIMIT 1", (days[0],)
    ).fetchone()
    missing = conn.execute(
        "SELECT 1 FROM instruments i WHERE (i.list_date IS NULL OR i.list_date<=%s) "
        "AND (i.delist_date IS NULL OR i.delist_date>=%s) "
        "AND NOT EXISTS(SELECT 1 FROM security_states s WHERE s.symbol=i.symbol) LIMIT 1",
        (days[0], days[-1]),
    ).fetchone()
    gaps = {}
    for endpoint in ("history", "constraints"):
        covered = present.get(endpoint, 0)
        if covered < count:
            gaps[endpoint] = [covered, count]
    if suffix < factor_sessions:
        gaps["factors"] = [suffix, factor_sessions]
    if not benchmark:
        gaps["benchmark"] = str(days[0])
    if missing:
        gaps["security_states"] = "a listed security has no recorded state"
    return gaps


def minute_ranges(days, today):
    """Closed calendar months have stable boundaries; the current month uses days."""
    ranges = set()
    for day in days:
        if (day.year, day.month) == (today.year, today.month):
            ranges.add((day, day))
        else:
            ranges.add((day.replace(day=1), day.replace(day=monthrange(day.year, day.month)[1])))
    return sorted(ranges)


def enqueue_minute_history(db, conn, code, days, today):
    jobs = []
    for first, last in minute_ranges(days, today):
        start = datetime.combine(first, time(9, 30), CN)
        end = datetime.combine(last, time(15), CN)
        jobs.append(
            db.enqueue(
                conn,
                "minute_history",
                "minute-history",
                f"minute-history:{code}:{start}:{end}",
                {"symbols": [code], "start": start.isoformat(), "end": end.isoformat()},
                10,
            )
        )
    return jobs


def schedule_research_minutes(db, settings, target, policy_revision, at):
    """Collect dated research membership and admit only its completed dependencies."""
    from quant_platform.pipeline import historical_pools

    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610242)")
        days = sorted(source_days(conn, target, settings.minute_history_sessions))
        pools = {row["day"]: row for row in historical_pools(conn, target, policy_revision) if row["day"] in days}
        missing = [str(day) for day in days if day not in pools]
        state = {
            "ready": False,
            "target": str(target),
            "policy_revision": policy_revision,
            "required_sessions": settings.minute_history_sessions,
            "pool_sessions": len(pools),
            "missing_pool_days": missing,
            "at": at.isoformat(),
        }
        if len(days) < settings.minute_history_sessions or missing:
            state["reason"] = "Qualified dated out-of-sample daily pools are incomplete."
        else:
            identity = digest(
                {
                    "days": days,
                    "pools": [str(pools[day]["id"]) for day in days],
                    "policy": policy_revision,
                    "range_month": str(at.date().replace(day=1)),
                }
            )
            previous = conn.execute("SELECT value FROM settings WHERE key='schedule:research-minute'").fetchone()
            checkpoint_data = previous["value"] if previous else {}
            if checkpoint_data.get("identity") != identity:
                required = {}
                for index, day in enumerate(days):
                    pool = pools[day]["data"]
                    for code in set(pool["symbols"]) | set(pool["daily_weights"]):
                        # Twenty prior sessions supply features; the following session supplies evaluation context.
                        required.setdefault(code, set()).update(days[max(0, index - 20) : index + 2])
                jobs = [
                    job
                    for code, history in sorted(required.items())
                    for job in enqueue_minute_history(db, conn, code, history, at.date())
                ]
                checkpoint_data = {"identity": identity, "jobs": jobs}
                db.set_setting(conn, "schedule:research-minute", checkpoint_data)
            jobs = checkpoint_data["jobs"]
            complete = conn.execute(
                "SELECT count(*) AS n FROM jobs WHERE id=ANY(%s) AND status='complete'", (jobs,)
            ).fetchone()["n"]
            state.update(
                identity=identity, required_jobs=len(jobs), completed_jobs=complete, ready=complete == len(jobs)
            )
            state["reason"] = None if state["ready"] else "Historical pool minute collection is incomplete."
        db.set_setting(conn, "research_minute_readiness", state)
        return state


def schedule_training(db, requests, frequency, cutoff, period, identity):
    prefix = f"challenger:{frequency}:{period}:"
    prior = db.rows(
        "SELECT 1 FROM pipeline_requests q JOIN pipeline_runs r ON r.id=q.run_id "
        "WHERE starts_with(q.request_key,%s) AND r.state NOT IN ('blocked','failed','superseded') LIMIT 1",
        (prefix,),
    )
    if not prior:
        requests.append(
            {"purpose": "training", "frequency": frequency, "as_of": cutoff, "request_key": prefix + identity}
        )


def tick(db, settings, at=None):
    # 采集范围的定义只有一份，就在 ``collect`` 里；调度器、采集与流水线必须用同一个集合，
    # 否则平台会训练在一个它并不采集的宇宙上。
    from quant_platform.jobs.collect import scoped_codes_on

    at = at or now()
    local = at.astimezone(CN)
    day = local.date()
    open_today = db.calendar_open(day)
    baskets = db.active_baskets(day)
    tracked = {code for basket in baskets if not basket["paused"] for code in basket["data"]["members"]}
    instruments = db.rows("SELECT symbol FROM instruments WHERE status='L' ORDER BY symbol")
    symbols = [r["symbol"] for r in instruments]
    reference = db.setting("directory", {})
    directory_fresh = bool(reference) and timedelta(0) <= at - datetime.fromisoformat(
        reference["fetched_at"]
    ) < timedelta(hours=48)
    with db.transaction() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(77610231) AS acquired").fetchone()["acquired"]:
            return
        for kind in ("calendar", "securities"):
            key = f"{kind}:{day}" if local.time() >= time(8, 30) else f"{kind}:{day - timedelta(days=1)}"
            db.enqueue(conn, kind, "history", key, priority=200)
        if open_today and local.time() >= time(9, 20):
            # 只刷新当日复权因子：当日 K 线未收盘，不能当作日线发布。
            db.enqueue(conn, "factors", "history", f"morning-factors:{day}", {"end": str(day)}, 190)
            db.enqueue(conn, "constraints", "history", f"morning-constraints:{day}", {"day": str(day)}, 190)
        if directory_fresh:
            cycle = str(day.replace(day=1))
            historical_symbols = [
                r["symbol"]
                for r in conn.execute(
                    "SELECT symbol FROM instruments WHERE status IN ('L','D','P') ORDER BY symbol"
                ).fetchall()
            ]
            if checkpoint(db, conn, "schedule:security", [cycle, historical_symbols]):
                for code in historical_symbols:
                    db.enqueue(
                        conn, "security_history", "history", f"security-history:{code}:{cycle}", {"symbol": code}, 20
                    )
        quote_blocked = conn.execute("SELECT 1 FROM capabilities WHERE endpoint='quotes' AND status='blocked'").fetchone()
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
        # 采集窗口比分析窗口多一个会话：`constraints` 在窗口内最早一天要用前一天的收盘，见
        # ``LEAD_IN_SESSIONS``。分析窗口本身仍是 ``history_sessions`` 个会话。
        window = source_days(conn, cutoff, settings.history_sessions + LEAD_IN_SESSIONS)
        days = window[: settings.history_sessions]
        if directory_fresh and days:
            # 点位风险状态只能从观测当天起算（公开源不发布带日期的更名史），而 `build_generation`
            # 要求状态的 `effective_day` 覆盖决策日。上面那条月度全表扫描把最新状态钉在**当月 1 日**：
            # 1 日 18:30 之前的 `analysis_target` 还是上月最后一个交易日，那个窗口里没有任何状态能
            # 覆盖决策日，覆盖率会再次归零。这是**每个月都会复发一次**的回归，不是首轮一次性的缺口。
            #
            # 修法：对采集范围按**决策日**观测一次。`security_history` 是纯本地推导（读主表名称），
            # 不消耗任何源配额，所以每天一次是免费的；月度全表扫描保留，用来覆盖范围之外的历史证券。
            scoped = sorted(scoped_codes_on(conn, settings, days[0]))
            if scoped and checkpoint(db, conn, "schedule:security-scoped", [str(days[0]), scoped]):
                for code in scoped:
                    db.enqueue(
                        conn,
                        "security_history",
                        "history",
                        f"security-scoped:{code}:{days[0]}",
                        {"symbol": code, "day": str(days[0])},
                        30,
                    )
        if symbols and directory_fresh and days and checkpoint(db, conn, "schedule:history", [day, window, symbols]):
            target = days[0]
            db.set_setting(conn, "analysis_target", str(target))
            db.enqueue(conn, "benchmark", "history", f"benchmark:{target}:{day}", {"day": str(target)}, 150)
            # One request per security covers the whole window; the job fans the window out per session.
            db.enqueue(
                conn,
                "history",
                "history",
                f"history:{window[-1]}:{target}:{day}",
                {"start": str(window[-1]), "end": str(target)},
                100,
            )
            for index, source_day in enumerate(days):
                # Historical records are durable checkpoints. Recent dates revalidate daily; all dates weekly.
                cycle = str(day) if index < 5 else str(day - timedelta(days=day.weekday()))
                db.enqueue(
                    conn,
                    "constraints",
                    "history",
                    f"constraints:{source_day}:{cycle}",
                    {"day": str(source_day)},
                    90 - min(index, 89),
                )
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
        db.enqueue(conn, "notify", "notify", f"notify:{int(at.timestamp()) // 60}")
        if local.time() >= time(15, 30):
            from .diagnostics import schedule_diagnostics

            schedule_diagnostics(db, conn)
    schedule_qlib(db, settings, at)


def schedule_qlib(db, settings, at):
    from quant_platform.pipeline import DAILY_DATASETS, MINUTE_CAPABILITIES, create_run, model_deadline
    from .market import frozen_pool

    local = at.astimezone(CN)
    target = db.setting("analysis_target")
    requests, cutoff = [], None
    with db.transaction() as conn:
        if not conn.execute("SELECT pg_try_advisory_xact_lock(77610242) AS acquired").fetchone()["acquired"]:
            return
        inspect_daily = checkpoint(db, conn, "schedule:qlib-check", int(at.timestamp()) // 60)
    if target and inspect_daily:
        with db.transaction() as conn:
            days = source_days(conn, date.fromisoformat(target), settings.history_sessions)
            gaps = source_gaps(conn, days, settings.history_sessions, settings.factor_sessions)
            ready = not gaps
            watermark = conn.execute(
                "SELECT coalesce(max(id),0) AS id FROM datasets WHERE endpoint=ANY(%s)", (sorted(DAILY_DATASETS),)
            ).fetchone()["id"]
            controls = {
                "policy": conn.execute("SELECT max(revision) AS revision FROM scan_policies").fetchone(),
                "models": conn.execute(
                    "SELECT id FROM model_versions WHERE state='active' ORDER BY frequency"
                ).fetchall(),
                "portfolios": conn.execute("SELECT id,revision FROM model_portfolios ORDER BY id").fetchall(),
                "watchlist": conn.execute("SELECT max(revision) AS revision FROM watchlist_revisions").fetchone(),
            }
        if ready:
            cutoff = datetime.combine(date.fromisoformat(target), time(15), CN)
            identity = digest({"watermark": watermark, "controls": controls})
            requests.append(
                {
                    "purpose": "inference",
                    "frequency": "day",
                    "as_of": cutoff,
                    "request_key": f"daily:{target}:{identity}",
                }
            )
            # A published immutable generation feeds one isolated Qlib research report. Queueing it
            # here keeps reporting off the collection path: a missing Qlib install fails only that job.
            with db.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(77610242)")
                db.enqueue(
                    conn,
                    "qlib_export",
                    "qlib-data",
                    f"qlib-export:{target}:{watermark}",
                    {"day": str(target), "watermark": watermark},
                    80,
                )
            schedule_training(db, requests, "day", cutoff, str(local.date().replace(day=1)), identity)
            minute_watermark = db.rows(
                "SELECT coalesce(max(id),0) AS id FROM datasets WHERE endpoint=ANY(%s)", (sorted(MINUTE_CAPABILITIES),)
            )[0]["id"]
            research = schedule_research_minutes(
                db, settings, date.fromisoformat(target), controls["policy"]["revision"], local
            )
            if research["ready"] and minute_watermark:
                week = str(local.date() - timedelta(days=local.weekday()))
                schedule_training(
                    db,
                    requests,
                    "5min",
                    cutoff,
                    week,
                    digest({"daily": identity, "minute": minute_watermark, "pools": research["identity"]}),
                )
        else:
            with db.transaction() as conn:
                db.set_setting(
                    conn,
                    "pipeline_blocker:day",
                    {
                        "reason": "Required dated daily source partitions are incomplete.",
                        # 逐项列出缺什么、缺多少。只说「不完整」会让运维在 1500 个会话里自己找。
                        "gaps": gaps,
                        "at": at.isoformat(),
                    },
                )
    minute_cutoff = five_minute_end(at)
    if db.calendar_open(local.date()) and local.time() >= time(9, 30):
        pool = frozen_pool(db, settings, local.date())
        if pool["symbols"]:
            with db.transaction() as conn:
                conn.execute("SELECT pg_advisory_xact_lock(77610242)")
                days = source_days(conn, local.date() - timedelta(days=1), 20)
                if checkpoint(db, conn, "schedule:minute-history", [local.date(), pool["symbols"], days]):
                    for code in pool["symbols"]:
                        enqueue_minute_history(db, conn, code, days, local.date())
                if minute_cutoff and 2 <= (at - minute_cutoff).total_seconds() < 120:
                    for i in range(0, len(pool["symbols"]), 100):
                        db.enqueue(
                            conn,
                            "minute_live",
                            "minute-live",
                            f"minute-live:{minute_cutoff}:{i}",
                            {"symbols": pool["symbols"][i : i + 100], "cutoff": minute_cutoff.isoformat()},
                            200,
                        )
                    if (at - minute_cutoff).total_seconds() >= 30:
                        for code in pool["symbols"]:
                            present = conn.execute(
                                "SELECT 1 FROM minute_bars WHERE symbol=%s AND bar_end=%s AND finalized LIMIT 1",
                                (code, minute_cutoff),
                            ).fetchone()
                            if not present:
                                db.enqueue(
                                    conn,
                                    "minute_repair",
                                    "minute-live",
                                    f"minute-repair:{minute_cutoff}:{code}",
                                    {"symbols": [code], "cutoff": minute_cutoff.isoformat()},
                                    150,
                                )
            if minute_cutoff and 2 <= (at - minute_cutoff).total_seconds() < 120:
                available = db.rows(
                    "SELECT count(DISTINCT symbol) AS n FROM minute_bars WHERE bar_end=%s AND finalized "
                    "AND symbol=ANY(%s)",
                    (minute_cutoff, pool["symbols"]),
                )[0]["n"]
                if available == len(pool["symbols"]):
                    try:
                        intraday_window(minute_cutoff, at)
                        requests.append(
                            {
                                "purpose": "inference",
                                "frequency": "5min",
                                "as_of": minute_cutoff,
                                "request_key": f"intraday:{minute_cutoff}",
                            }
                        )
                    except PipelineBlocked:
                        pass
    for frequency in ("day", "5min"):
        observed_cutoff = (
            cutoff
            if frequency == "day"
            else next((r["as_of"] for r in requests if r["frequency"] == "5min" and r["purpose"] == "inference"), None)
        )
        if observed_cutoff:
            challengers = db.rows(
                "SELECT * FROM model_versions WHERE frequency=%s AND state='shadow' ORDER BY created_at DESC LIMIT 1",
                (frequency,),
            )
            for model in challengers:
                if at < model_deadline(model):
                    requests.append(
                        {
                            "purpose": "shadow",
                            "frequency": frequency,
                            "model_id": model["id"],
                            "as_of": observed_cutoff,
                            "request_key": f"shadow:{model['id']}:{observed_cutoff}",
                        }
                    )
    for request in requests:
        if db.rows("SELECT 1 FROM pipeline_requests WHERE request_key=%s", (request["request_key"],)):
            continue
        try:
            create_run(db, settings, request)
        except PipelineBlocked as exc:
            with db.transaction() as conn:
                db.set_setting(
                    conn, "pipeline_blocker:" + request["frequency"], {"reason": str(exc), "at": at.isoformat()}
                )
