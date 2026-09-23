"""Pinned personal research run. It does not change baskets or submit orders."""

from datetime import date

from quant_platform.analysis.research import evaluate
from quant_platform.domain import ScreenRule, digest, symbol
from quant_platform.jobs.analyze import analysis_snapshot, report
from quant_platform.storage import jsonb


def _suspensions(db, watermark):
    rows = db.rows(
        "SELECT DISTINCT ON (scope) scope, metadata FROM datasets WHERE endpoint='daily' AND id<=%s "
        "ORDER BY scope, id DESC",
        (watermark,),
    )
    found = set()
    for row in rows:
        try:
            day = date.fromisoformat(row["scope"])
        except ValueError:
            continue
        for event in (row["metadata"] or {}).get("suspension_events") or []:
            if event.get("suspend_type") != "S":
                continue
            try:
                found.add((symbol(event.get("ts_code", "")), day))
            except ValueError:
                continue
    return found


def run(db, job, settings):
    target = date.fromisoformat(job["payload"]["day"])
    snapshot = analysis_snapshot(db, job, target, settings)
    instruments = snapshot["instruments"]
    series = {}
    for code in instruments:
        rows = db.history(code, target, snapshot["history_sessions"], snapshot["watermark"])
        series[code] = {
            row["day"]: {
                "close": row["data"].get("close"),
                "factor": row["factor"],
                "turnover": row["data"].get("turnover"),
                "volume": row["data"].get("volume"),
            }
            for row in rows
            if row.get("factor") is not None
        }
    calendars = {}
    for row in snapshot["calendars"]:
        calendars.setdefault(row["exchange"], {})[row["day"]] = row["is_open"]
    sse, szse = calendars.get("SSE", {}), calendars.get("SZSE", {})
    days = sorted(
        date.fromisoformat(str(day))
        for day, open_ in sse.items()
        if open_ and szse.get(day) and date.fromisoformat(str(day)) <= target
    )
    benchmark_rows = db.history("SH000300", target, snapshot["history_sessions"], snapshot["watermark"])
    benchmark = {
        row["day"]: {"close": row["data"].get("close"), "factor": row["factor"] or 1.0} for row in benchmark_rows
    }
    outcome = evaluate(
        series,
        days,
        instruments,
        _suspensions(db, snapshot["watermark"]),
        ScreenRule(**snapshot["rule"]),
        limit_known=False,
        benchmark=benchmark or None,
    )
    outcome["snapshot_hash"] = digest(snapshot)
    outcome["dataset_watermark"] = snapshot["watermark"]
    with db.publication(job) as conn:
        report(conn, "factor", "default", target, outcome["factors"])
        report(conn, "backtest", "default", target, outcome)
        conn.execute(
            "UPDATE jobs SET progress=progress || %s WHERE id=%s",
            (jsonb({"research_hash": outcome["research_hash"], "execution": outcome["execution"]}), job["id"]),
        )
