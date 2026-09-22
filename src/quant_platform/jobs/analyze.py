"""Native reports and transactional, edge-triggered research alerts."""

from datetime import date, datetime, timedelta

from quant_platform.analysis import ANALYSIS_VERSION, basket_summary, holding_advice, indicators, screen
from quant_platform.domain import CN, HoldingPolicy, ScreenRule, digest, fresh, now, number, session
from quant_platform.storage import jsonb


def report(conn, kind, target, day, data):
    key = digest(data)
    conn.execute(
        "INSERT INTO reports(kind,target,as_of,input_hash,data) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (kind, str(target), day, key, jsonb(data)),
    )


def alert(conn, key, observation, value, threshold, cooldown, data, at):
    if value is None:
        return
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
    previous = conn.execute("SELECT * FROM alert_state WHERE key=%s FOR UPDATE", (key,)).fetchone()
    if previous and previous["observation"] == observation:
        return
    active = abs(value) >= threshold
    last = previous["last_alert"] if previous else None
    if active and not (previous and previous["active"]) and (last is None or (at - last).total_seconds() >= cooldown):
        event_key = digest((key, observation))
        inserted = conn.execute(
            "INSERT INTO alert_keys(event_key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_key", (event_key,)
        ).fetchone()
        if inserted:
            conn.execute(
                "INSERT INTO alerts(event_key,data) VALUES(%s,%s)",
                (
                    event_key,
                    jsonb({**data, "value": value, "threshold": threshold, "source": "tushare", "observed_at": at}),
                ),
            )
        last = at
    conn.execute(
        "INSERT INTO alert_state VALUES(%s,%s,%s,%s,now()) ON CONFLICT(key) DO UPDATE "
        "SET active=excluded.active,observation=excluded.observation,last_alert=excluded.last_alert,updated_at=now()",
        (key, active, observation, last),
    )


def _latest_screen_candidates(db):
    rows = db.rows("SELECT data FROM reports WHERE kind='screen' ORDER BY as_of DESC,id DESC LIMIT 1")
    return (rows[0]["data"].get("candidates") or []) if rows else []


def _stock_report_map(db, symbols):
    if not symbols:
        return {}
    rows = db.rows(
        "SELECT DISTINCT ON (target) target,data FROM reports WHERE kind='stock' AND target=ANY(%s) "
        "ORDER BY target,as_of DESC,id DESC",
        (symbols,),
    )
    return {row["target"]: row["data"] for row in rows}


def _instrument_names(db, symbols):
    if not symbols:
        return {}
    return {
        row["symbol"]: row["name"]
        for row in db.rows("SELECT symbol,name FROM instruments WHERE symbol=ANY(%s)", (symbols,))
    }


def intraday(db, job):
    at = now()
    day = at.astimezone(CN).date()
    quotes = {r["symbol"]: r["data"] for r in db.rows("SELECT * FROM latest_quotes")}
    baskets = db.active_baskets(day)
    holdings = db.active_holdings()
    holding_symbols = [row["symbol"] for row in holdings]
    names = _instrument_names(db, holding_symbols)
    stock_reports = _stock_report_map(db, holding_symbols)
    candidates = _latest_screen_candidates(db)
    policy = HoldingPolicy()
    active = session(at, db.calendar_open(day))[1]
    with db.publication(job) as conn:
        for basket in baskets:
            if basket["paused"]:
                continue
            data = basket_summary(basket["data"]["members"], quotes, at)
            data.update({"source": "tushare", "as_of": at.isoformat(), "basket_revision": basket["revision"]})
            conn.execute(
                "INSERT INTO basket_observations VALUES(%s,%s,%s,%s) ON CONFLICT(basket_id,revision,minute) "
                "DO UPDATE SET data=excluded.data",
                (basket["id"], basket["revision"], at.replace(second=0, microsecond=0), jsonb(data)),
            )
            policy_basket = basket["data"]["alerts"]
            scope = digest((str(basket["id"]), basket["revision"], str(day), policy_basket))
            if active and data["coverage"] >= policy_basket["minimum_coverage"]:
                alert(
                    conn,
                    scope,
                    data["observation"],
                    data["daily_return"],
                    policy_basket["basket_change"],
                    policy_basket["cooldown"],
                    {"target": basket["name"], "rule": "basket_daily_return"},
                    at,
                )
            if active:
                for code in basket["data"]["members"]:
                    q = quotes.get(code, {})
                    if (
                        fresh(q.get("source_time"), at)
                        and q.get("reference_verified")
                        and q.get("pre_close")
                        and q.get("close")
                    ):
                        identity = digest({k: v for k, v in q.items() if k != "received_at"})
                        alert(
                            conn,
                            scope + code,
                            identity,
                            q["close"] / q["pre_close"] - 1,
                            policy_basket["stock_change"],
                            policy_basket["cooldown"],
                            {"target": code, "rule": "stock_daily_return"},
                            at,
                        )
        for holding in holdings:
            quote = quotes.get(holding["symbol"], {})
            data = holding_advice(
                holding,
                quote,
                at,
                stock_report=stock_reports.get(holding["symbol"]),
                screen_candidates=candidates,
                policy=policy,
                name=names.get(holding["symbol"], ""),
            )
            data.update({"source": "tushare", "as_of": at.isoformat(), "holding_revision": holding["revision"]})
            conn.execute(
                "INSERT INTO holding_observations VALUES(%s,%s,%s,%s) ON CONFLICT(holding_id,revision,minute) "
                "DO UPDATE SET data=excluded.data",
                (holding["id"], holding["revision"], at.replace(second=0, microsecond=0), jsonb(data)),
            )
            if active and data.get("pnl_pct") is not None:
                identity = digest({"last": data["last"], "cost_price": data["cost_price"], "pnl_pct": data["pnl_pct"]})
                alert(
                    conn,
                    digest((str(holding["id"]), holding["revision"], str(day), policy.model_dump())),
                    identity,
                    data["pnl_pct"],
                    policy.alert_change,
                    policy.cooldown,
                    {"target": holding["symbol"], "rule": "holding_cost_pnl"},
                    at,
                )


def analysis_snapshot(db, job, target, settings):
    with db.transaction() as conn:
        db.fence(conn, job)
        snapshot = (
            conn.execute("SELECT progress FROM jobs WHERE id=%s", (job["id"],)).fetchone()["progress"].get("snapshot")
        )
        if snapshot is None:
            conn.execute("SELECT pg_advisory_xact_lock(77610234)")
            # One MVCC statement captures mutable reference/control tables with the committed data watermark.
            snapshot = conn.execute(
                "SELECT jsonb_build_object("
                "'watermark',(SELECT coalesce(max(id),0) FROM datasets),"
                "'instruments',coalesce((SELECT jsonb_object_agg(symbol,to_jsonb(i)) FROM instruments i "
                "WHERE list_date<=%(day)s AND (delist_date IS NULL OR delist_date>%(day)s)),'{}'),"
                "'calendars',coalesce((SELECT jsonb_agg(to_jsonb(c)) FROM calendars c WHERE day<=%(day)s),'[]'),"
                "'baskets',coalesce((SELECT jsonb_agg(to_jsonb(a)) FROM ("
                "SELECT b.id,b.paused,r.revision,r.data FROM baskets b JOIN LATERAL "
                "(SELECT * FROM basket_revisions WHERE basket_id=b.id AND effective_day<=%(day)s "
                "ORDER BY effective_day DESC,revision DESC LIMIT 1) r ON true WHERE NOT b.archived) a),'[]'),"
                "'holdings',coalesce((SELECT jsonb_agg(to_jsonb(h)) FROM holdings h WHERE NOT h.archived),'[]'),"
                "'rule',(SELECT data FROM rules ORDER BY revision DESC LIMIT 1),"
                "'rule_revision',(SELECT coalesce(max(revision),0) FROM rules)) AS snapshot",
                {"day": target},
            ).fetchone()["snapshot"]
            snapshot.update(
                rule=snapshot["rule"] or ScreenRule().model_dump(),
                history_sessions=settings.history_sessions,
                analysis_version=ANALYSIS_VERSION,
            )
            conn.execute(
                "UPDATE jobs SET progress=progress || %s WHERE id=%s", (jsonb({"snapshot": snapshot}), job["id"])
            )
        if snapshot.get("analysis_version") != ANALYSIS_VERSION:
            raise RuntimeError("Analysis code changed; request a new run instead of mixing checkpoint versions.")
        return snapshot


def calendar_indicators(rows, target, calendar):
    """Missing bars and unknown calendar days cannot become synthetic consecutive sessions."""
    start = rows[0]["day"] if rows else target
    dates = [start + timedelta(days=i) for i in range((target - start).days + 1)]
    unknown = [str(day) for day in dates if str(day) not in calendar]
    observed = {str(row["day"]) for row in rows}
    missing = [str(day) for day in dates if calendar.get(str(day)) is True and str(day) not in observed]
    unexpected = sorted(day for day in observed if calendar.get(day) is False)
    invalid = bool(unknown or missing or unexpected or not calendar.get(str(target)))
    result = indicators([] if invalid else rows)
    if invalid:
        result.update(
            status="calendar_gaps",
            bars=len(rows),
            as_of=str(rows[-1]["day"]) if rows else None,
            reason="Session metrics unavailable until calendar and bar coverage is verified.",
            missing_sessions=missing,
            unknown_calendar_days=unknown,
            non_session_bars=unexpected,
        )
    elif result.get("as_of") != str(target):
        result["status"] = "stale_or_missing"
    result.update(
        requested_as_of=str(target),
        source="tushare",
        data_versions=[(r["dataset_id"], r.get("factor_dataset_id")) for r in rows],
        input_hash=digest([(r["dataset_id"], r.get("factor_dataset_id")) for r in rows]),
    )
    return result


def daily(db, job, settings):
    target = date.fromisoformat(job["payload"]["day"])
    snapshot = analysis_snapshot(db, job, target, settings)
    instruments = snapshot["instruments"]
    rule = ScreenRule(**snapshot["rule"])
    metrics = {}
    calendars = {
        exchange: {row["day"]: row["is_open"] for row in snapshot["calendars"] if row["exchange"] == exchange}
        for exchange in ("SSE", "SZSE")
    }
    snapshot_hash = digest(snapshot)
    raw_closes = {}
    benchmark_rows = db.history("SH000300", target, snapshot["history_sessions"], snapshot["watermark"])
    benchmark = calendar_indicators([{**r, "factor": 1.0} for r in benchmark_rows], target, calendars["SSE"])
    for index, (code, instrument) in enumerate(instruments.items()):
        rows = db.history(code, target, snapshot["history_sessions"], snapshot["watermark"])
        result = calendar_indicators(rows, target, calendars[instrument["exchange"]])
        if instrument["status"] == "U":
            result["status"] = "unverified_identity"
        result["snapshot_hash"] = snapshot_hash
        result["dataset_watermark"] = snapshot["watermark"]
        result["benchmark_status"] = "available" if benchmark.get("status") == "complete" else "unavailable"
        result["benchmark_data_versions"] = benchmark["data_versions"]
        for n in (5, 20, 60):
            stock_return, index_return = result.get(f"return{n}"), benchmark.get(f"return{n}")
            result[f"excess_return{n}"] = (
                stock_return - index_return
                if (
                    stock_return is not None
                    and index_return is not None
                    and result["benchmark_status"] == "available"
                    and result["status"] == "complete"
                )
                else None
            )
        metrics[code] = result
        raw_closes[code] = number(rows[-1]["data"].get("close")) if rows else None
        # Symbol-level report checkpoints survive an interrupted large analysis run.
        with db.transaction() as conn:
            db.fence(conn, job)
            report(conn, "stock", code, target, result)
            if index % 50 == 0:
                conn.execute(
                    "UPDATE jobs SET progress=progress || %s WHERE id=%s",
                    (jsonb({"done": index + 1, "total": len(instruments)}), job["id"]),
                )
    fundamentals = db.basics_window(target, rule.history_sessions, snapshot["watermark"])
    output = screen(instruments, metrics, target, rule, fundamentals)
    output["rule_revision"] = snapshot["rule_revision"]
    output["dataset_watermark"] = snapshot["watermark"]
    output["snapshot_hash"] = snapshot_hash
    output["snapshot"] = snapshot
    output["inputs"] = {code: item.get("input_hash") for code, item in metrics.items()}
    output["status"] = "complete" if output["coverage"] == 1 else "partial"
    baskets = snapshot["baskets"] or []
    holdings = snapshot.get("holdings") or []
    with db.publication(job) as conn:
        report(conn, "screen", "default", target, output)
        for basket in baskets:
            if basket["paused"]:
                continue
            members = basket["data"]["members"]
            available = {
                code: metrics[code] for code in members if code in metrics and metrics[code].get("status") == "complete"
            }
            coverage = sum(weight for code, weight in members.items() if code in available)
            report(
                conn,
                "basket",
                basket["id"],
                target,
                {
                    "name": basket["data"]["name"],
                    "basket_revision": basket["revision"],
                    "snapshot_hash": snapshot_hash,
                    "coverage": coverage,
                    "members": available,
                    "missing": sorted(set(members) - set(available)),
                    "weights": members,
                    "analysis_version": ANALYSIS_VERSION,
                    "as_of": str(target),
                    "status": "complete" if coverage >= 1 - 1e-10 else "partial",
                    "interpretation": "Research indicators and risk flags; no trade execution or return guarantee.",
                },
            )
        for holding in holdings:
            advice = holding_advice(
                holding,
                last_close=raw_closes.get(holding["symbol"]),
                stock_report=metrics.get(holding["symbol"]),
                screen_candidates=output["candidates"],
                name=(instruments.get(holding["symbol"]) or {}).get("name", ""),
            )
            report(
                conn,
                "holding",
                holding["id"],
                target,
                {
                    **advice,
                    "holding_id": holding["id"],
                    "holding_revision": holding["revision"],
                    "snapshot_hash": snapshot_hash,
                    "as_of": str(target),
                    "quantity": holding.get("quantity"),
                    "note": holding.get("note") or "",
                    "status": "complete" if advice["action"] != "insufficient_data" else "partial",
                    "interpretation": advice["disclaimer"],
                },
            )
        db.set_setting(
            conn, "last_analysis", {"as_of": str(target), "at": now().isoformat(), "coverage": output["coverage"]}
        )
        if settings.qlib_enabled:
            db.enqueue(
                conn,
                "qlib_export",
                "qlib",
                f"qlib:{target}:{digest(output['inputs'])}",
                {"day": str(target), "watermark": snapshot["watermark"]},
            )
