"""Descriptive market-wide quant report from stored bars; statistics only, never recommendations or NAV."""

from datetime import date, datetime

import numpy as np
import pandas as pd

from quant_platform import __version__
from quant_platform.domain import digest
from quant_platform.storage import jsonb

ENGINE = "descriptive-v1"
REPORT_VERSION = __version__ + ":" + ENGINE
BENCHMARK = "SH000300"
KIND, TARGET = "market", "ALL"
DISCLAIMER = "本报告为描述性量化统计，不构成投资建议，也不是模型推荐或目标权重。"
DATA_NOTE = (
    "仅基于已入库的日线行情与复权因子计算的描述性统计；不含模型预测，不含推荐。"
    "数据缺失时显示为不可用，不做零值填充；复权价仅在同一证券内可比。"
)
COMPOSITE_LABEL = "Descriptive rank composite; not a recommendation or target weight"
FACTOR_NAMES = ("momentum_20", "momentum_60_ex_5", "volatility_20", "turnover_20", "reversal_5")
FACTOR_LABELS = {
    "momentum_20": "20日动量",
    "momentum_60_ex_5": "60日动量(剔除近5日)",
    "volatility_20": "20日年化波动率",
    "turnover_20": "20日平均成交额",
    "reversal_5": "5日反转",
}
BOARD_LABELS = {"Main Board": "沪深主板", "STAR Market": "科创板", "ChiNext": "创业板"}
FORWARD = 20
MIN_WINDOW_OBS = 15
MIN_CROSS_SECTION = 10
MIN_IC_SAMPLES = 3
LIST_LIMIT = 100

_SESSIONS_SQL = (
    "SELECT day FROM calendars WHERE exchange IN ('SSE','SZSE') AND is_open AND day<=%s "
    "GROUP BY day HAVING count(*)=2 ORDER BY day DESC LIMIT %s"
)
_LISTED_SQL = "status='L' AND (list_date IS NULL OR list_date<=%s) AND (delist_date IS NULL OR delist_date>%s)"


# ---------------------------------------------------------------------------
# Loading (one bounded query per table)
# ---------------------------------------------------------------------------


def latest_session(db, cutoff):
    rows = db.rows(_SESSIONS_SQL, (cutoff, 1))
    return rows[0]["day"] if rows else None


def load_inputs(db, as_of, lookback_sessions=260):
    """Collect flat row lists for `build`; every query is bounded by the lookback window."""
    days = sorted(row["day"] for row in db.rows(_SESSIONS_SQL, (as_of, lookback_sessions)))
    first = days[0] if days else as_of
    instruments = db.rows(
        f"SELECT symbol,name,board,list_date FROM instruments WHERE {_LISTED_SQL} ORDER BY symbol", (as_of, as_of)
    )
    bars = db.rows(
        "SELECT DISTINCT ON (symbol,day) symbol,day,dataset_id,data->>'open' AS open,data->>'high' AS high,"
        "data->>'low' AS low,data->>'close' AS close,data->>'pre_close' AS pre_close,data->>'volume' AS volume,"
        "data->>'turnover' AS turnover FROM daily_bars WHERE day>=%s AND day<=%s "
        f"AND symbol IN (SELECT symbol FROM instruments WHERE {_LISTED_SQL}) ORDER BY symbol,day,dataset_id DESC",
        (first, as_of, as_of, as_of),
    )
    factors = db.rows(
        "SELECT DISTINCT ON (symbol,day) symbol,day,dataset_id,factor FROM factors WHERE day>=%s AND day<=%s "
        f"AND symbol IN (SELECT symbol FROM instruments WHERE {_LISTED_SQL}) ORDER BY symbol,day,dataset_id DESC",
        (first, as_of, as_of, as_of),
    )
    benchmark = db.rows(
        "SELECT DISTINCT ON (day) day,dataset_id,data->>'close' AS close FROM daily_bars "
        "WHERE symbol=%s AND day>=%s AND day<=%s ORDER BY day,dataset_id DESC",
        (BENCHMARK, first, as_of),
    )
    constraints = db.rows(
        "SELECT DISTINCT ON (symbol) symbol,dataset_id,data->>'limit_up' AS limit_up,"
        "data->>'limit_down' AS limit_down,data->>'suspended' AS suspended FROM market_constraints "
        "WHERE day=%s ORDER BY symbol,dataset_id DESC",
        (days[-1] if days else as_of,),
    )
    return {
        "as_of": as_of,
        "days": days,
        "instruments": instruments,
        "bars": bars,
        "factors": factors,
        "benchmark": benchmark,
        "constraints": constraints,
    }


# ---------------------------------------------------------------------------
# Numeric helpers
# ---------------------------------------------------------------------------


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _truthy(value):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"true", "t", "1", "yes"}
    return bool(value)


def _latest_rows(frame, keys):
    """Keep the highest dataset_id per key; rows without dataset_id rank lowest."""
    if "dataset_id" not in frame.columns:
        frame = frame.assign(dataset_id=0)
    frame = frame.assign(dataset_id=pd.to_numeric(frame["dataset_id"], errors="coerce").fillna(0))
    return frame.sort_values("dataset_id", kind="mergesort").drop_duplicates(keys, keep="last")


def _corr(left, right):
    if len(left) < 3 or np.std(left) == 0 or np.std(right) == 0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    return value if np.isfinite(value) else None


def _rank_ic(factor, forward):
    pair = pd.concat([factor, forward], axis=1).dropna()
    if len(pair) < MIN_CROSS_SECTION:
        return None
    return _corr(pair.iloc[:, 0].rank().to_numpy(), pair.iloc[:, 1].rank().to_numpy())


def _pivot(rows, value, days, index="day", columns="symbol"):
    """Latest dataset wins per (symbol, day); missing cells stay NaN."""
    if not rows:
        return pd.DataFrame(index=pd.Index(days, name="day"))
    frame = pd.DataFrame(rows)
    frame[index] = frame[index].map(_day)
    frame = _latest_rows(frame, [columns, index])
    frame[value] = pd.to_numeric(frame[value], errors="coerce") if value in frame.columns else np.nan
    wide = frame.pivot(index=index, columns=columns, values=value)
    return wide.reindex(pd.Index(days, name="day")).astype(float)


def _ratio(frame, start, end):
    if start < 0 or end < 0 or end >= len(frame):
        return pd.Series(np.nan, index=frame.columns)
    result = frame.iloc[end] / frame.iloc[start] - 1
    return result.replace([np.inf, -np.inf], np.nan)


def _volatility(frame, end, window=20):
    if end - window < 0:
        return pd.Series(np.nan, index=frame.columns)
    slice_ = frame.iloc[end - window : end + 1]
    returns = slice_.pct_change(fill_method=None).iloc[1:].replace([np.inf, -np.inf], np.nan)
    std = returns.std(ddof=1) * np.sqrt(252)
    return std.where(returns.count() >= MIN_WINDOW_OBS)


def _window_mean(frame, end, window=20):
    if frame.empty or end - window + 1 < 0:
        return pd.Series(np.nan, index=frame.columns)
    slice_ = frame.iloc[end - window + 1 : end + 1]
    return slice_.mean().where(slice_.count() >= MIN_WINDOW_OBS)


def _snapshot(adjusted, turnover, t):
    out = pd.DataFrame(index=adjusted.columns)
    out["momentum_20"] = _ratio(adjusted, t - 20, t)
    out["momentum_60_ex_5"] = _ratio(adjusted, t - 60, t - 5)
    out["volatility_20"] = _volatility(adjusted, t)
    out["turnover_20"] = _window_mean(turnover.reindex(columns=adjusted.columns), t)
    out["reversal_5"] = _ratio(adjusted, t - 5, t)
    return out


def _mean(series):
    values = series.dropna()
    return float(values.mean()) if len(values) else None


def _median(series):
    values = series.dropna()
    return float(values.median()) if len(values) else None


def _count(mask):
    return int(mask.fillna(False).astype(bool).sum())


def _json_ready(value):
    if isinstance(value, dict):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return _number(value)
    return value


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _benchmark_section(rows, days):
    if not rows:
        return {"status": "unavailable", "symbol": BENCHMARK}
    frame = pd.DataFrame(rows)
    frame["symbol"] = BENCHMARK
    close = _pivot(frame.to_dict("records"), "close", days)
    if BENCHMARK not in close.columns or close[BENCHMARK].dropna().empty:
        return {"status": "unavailable", "symbol": BENCHMARK}
    series = close[BENCHMARK]
    last_index = len(days) - 1
    last = _number(series.iloc[last_index])
    if last is None:
        return {"status": "unavailable", "symbol": BENCHMARK, "reason": "No benchmark bar on the report session."}

    def back(n):
        if last_index - n < 0:
            return None
        prior = _number(series.iloc[last_index - n])
        return last / prior - 1 if prior else None

    returns = {f"return_{n}": back(n) for n in (1, 5, 20, 60, 120)}
    volatility = _number(_volatility(close, last_index).get(BENCHMARK))
    trailing = series.iloc[max(0, last_index - 249) : last_index + 1].dropna()
    drawdown = last / float(trailing.max()) - 1 if len(trailing) >= MIN_WINDOW_OBS else None

    def relative(window):
        window_values = series.iloc[max(0, last_index - window + 1) : last_index + 1]
        if len(window_values) < window or window_values.count() < window:
            return None
        return last / float(window_values.mean()) - 1

    return {
        "status": "available",
        "symbol": BENCHMARK,
        "close": last,
        **returns,
        "volatility_20": volatility,
        "drawdown_250": drawdown,
        "drawdown_sessions": int(len(trailing)),
        "close_vs_ma20": relative(20),
        "close_vs_ma60": relative(60),
        "sessions_available": int(series.count()),
    }


def _breadth_section(raw, adjusted, constraints, t):
    close_t = raw["close"].iloc[t]
    if t >= 1:
        previous = raw["pre_close"].iloc[t].where(raw["pre_close"].iloc[t].notna(), raw["close"].iloc[t - 1])
    else:
        previous = raw["pre_close"].iloc[t]
    change = (close_t - previous).where(close_t.notna() & previous.notna())
    adjusted_t = adjusted.iloc[t] if len(adjusted.columns) else pd.Series(dtype=float)

    def above(window):
        if t - window + 1 < 0 or adjusted.empty:
            return None
        slice_ = adjusted.iloc[t - window + 1 : t + 1]
        mean = slice_.mean().where(slice_.count() == window)
        valid = mean.notna() & adjusted_t.notna()
        return _count(adjusted_t[valid] > mean[valid]) / int(valid.sum()) if valid.sum() else None

    def extreme(kind):
        if t - 59 < 0 or adjusted.empty:
            return None
        slice_ = adjusted.iloc[t - 59 : t + 1]
        full = slice_.count() == 60
        bound = (slice_.max() if kind == "high" else slice_.min()).where(full)
        valid = bound.notna() & adjusted_t.notna()
        compare = adjusted_t[valid] >= bound[valid] if kind == "high" else adjusted_t[valid] <= bound[valid]
        return _count(compare)

    turnover_t = raw["turnover"].iloc[t][close_t.notna()] if "turnover" in raw else pd.Series(dtype=float)
    traded = int(close_t.notna().sum())
    turnover_missing_ratio = 1 - turnover_t.count() / traded if traded else None
    median_turnover = None
    if traded and turnover_missing_ratio is not None and turnover_missing_ratio <= 0.5:
        median_turnover = _median(turnover_t)
    section = {
        "session_symbols": traded,
        "advancers": _count(change > 0),
        "decliners": _count(change < 0),
        "unchanged": _count(change == 0),
        "change_unknown": int(close_t.notna().sum() - change.notna().sum()),
        "above_ma20_ratio": above(20),
        "above_ma60_ratio": above(60),
        "new_high_60": extreme("high"),
        "new_low_60": extreme("low"),
        "median_turnover": median_turnover,
        "turnover_missing_ratio": turnover_missing_ratio,
        "constraints_available": not constraints.empty,
        "limit_up": None,
        "limit_down": None,
        "suspended": None,
    }
    if not constraints.empty:
        joined = constraints.reindex(close_t.index)
        section["limit_up"] = _count(close_t >= joined["limit_up"])
        section["limit_down"] = _count(close_t <= joined["limit_down"])
        section["suspended"] = _count(joined["suspended"])
    return section


def _boards_section(instruments, raw, adjusted, t):
    boards = []
    for board, group in instruments.groupby("board", sort=True):
        symbols = [s for s in group["symbol"] if s in raw["close"].columns]
        adjusted_symbols = [s for s in symbols if s in adjusted.columns]
        sub = adjusted[adjusted_symbols] if adjusted_symbols else pd.DataFrame(index=adjusted.index)
        amplitude = (
            (raw["high"].iloc[t][symbols] / raw["low"].iloc[t][symbols] - 1) if symbols else pd.Series(dtype=float)
        )
        boards.append(
            {
                "board": board,
                "label": BOARD_LABELS.get(board, board),
                "count": int(len(group)),
                "with_bars": int(raw["close"].iloc[t][symbols].notna().sum()) if symbols else 0,
                "mean_return_1": _mean(_ratio(sub, t - 1, t)) if adjusted_symbols else None,
                "mean_return_5": _mean(_ratio(sub, t - 5, t)) if adjusted_symbols else None,
                "mean_return_20": _mean(_ratio(sub, t - 20, t)) if adjusted_symbols else None,
                "median_volatility_20": _median(_volatility(sub, t)) if adjusted_symbols else None,
                "median_amplitude": _median(amplitude.replace([np.inf, -np.inf], np.nan)),
            }
        )
    return boards


def _ranked(values, names, ascending, limit=10):
    ordered = values.dropna().sort_values(ascending=ascending, kind="mergesort")
    ordered = ordered.iloc[:limit]
    return [{"symbol": s, "name": names.get(s), "value": float(v)} for s, v in ordered.items()]


def _efficacy(adjusted, turnover, t):
    evaluation = [i for i in range(t - FORWARD, -1, -FORWARD)]
    evaluation.reverse()
    ics = {name: [] for name in FACTOR_NAMES}
    used = {name: [] for name in FACTOR_NAMES}
    for index in evaluation:
        snapshot = _snapshot(adjusted, turnover, index)
        forward = _ratio(adjusted, index, index + FORWARD)
        for name in FACTOR_NAMES:
            ic = _rank_ic(snapshot[name], forward)
            if ic is not None:
                ics[name].append(ic)
                used[name].append(index)
    return ics, used


def _factor_sections(adjusted, turnover, names, days, t):
    snapshot = _snapshot(adjusted, turnover, t)
    ics, used = _efficacy(adjusted, turnover, t)
    sections = {}
    for name in FACTOR_NAMES:
        values = snapshot[name]
        series = np.array(ics[name], dtype=float)
        evidence = {
            "samples": int(len(series)),
            "evaluation_days": [days[i] for i in used[name]],
            "forward_sessions": FORWARD,
            "ic_mean": None,
            "ic_std": None,
            "ic_ir": None,
            "ic_positive_ratio": None,
        }
        if len(series) >= MIN_IC_SAMPLES:
            std = float(np.std(series, ddof=1))
            evidence.update(
                status="evaluated",
                ic_mean=float(series.mean()),
                ic_std=std,
                ic_ir=float(series.mean() / std) if std > 0 else None,
                ic_positive_ratio=float((series > 0).mean()),
            )
        else:
            evidence["status"] = "insufficient_history"
        sections[name] = {
            "label": FACTOR_LABELS[name],
            "coverage": int(values.notna().sum()),
            "top": _ranked(values, names, ascending=False),
            "bottom": _ranked(values, names, ascending=True),
            "efficacy": evidence,
        }
    return snapshot, sections


def _composite_section(snapshot, instruments, raw_close_t):
    needed = snapshot[["momentum_20", "momentum_60_ex_5", "volatility_20"]].dropna()
    if needed.empty:
        return {"status": "insufficient_data", "label": COMPOSITE_LABEL, "count": 0, "top": [], "bottom": []}
    score = (
        0.5 * needed["momentum_20"].rank(pct=True)
        + 0.3 * needed["momentum_60_ex_5"].rank(pct=True)
        + 0.2 * (1 - needed["volatility_20"].rank(pct=True))
    )
    frame = needed.assign(score=score).join(instruments.set_index("symbol")[["name", "board"]], how="left")
    frame["close"] = raw_close_t.reindex(frame.index)
    frame = frame.reset_index().rename(columns={"index": "symbol"})
    frame = frame.sort_values(["score", "symbol"], ascending=[False, True], kind="mergesort")
    columns = ["symbol", "name", "board", "close", "score", "momentum_20", "momentum_60_ex_5", "volatility_20"]

    def rows(part):
        return [
            {key: (row[key] if key in ("symbol", "name", "board") else _number(row[key])) for key in columns}
            for _, row in part.iterrows()
        ]

    return {
        "status": "complete",
        "label": COMPOSITE_LABEL,
        "weights": {"momentum_20": 0.5, "momentum_60_ex_5": 0.3, "low_volatility_20": 0.2},
        "count": int(len(frame)),
        "top": rows(frame.head(20)),
        "bottom": rows(frame.tail(20).iloc[::-1]),
    }


def _risk_section(instruments, constraints, raw_close, days, names):
    flagged = instruments[instruments["name"].fillna("").str.contains("ST|退", regex=True)]
    warnings = [{"symbol": r.symbol, "name": r.name} for r in flagged.itertuples()]
    suspended = []
    if not constraints.empty:
        suspended = [{"symbol": s, "name": names.get(s)} for s in constraints.index[constraints["suspended"]]]
    young = None
    young_note = "至少需要 60 个交易日的窗口才能识别新上市证券。"
    if len(days) >= 60 and not raw_close.empty:
        counts = raw_close.count()
        young = [{"symbol": s, "name": names.get(s), "sessions": int(c)} for s, c in counts[counts < 60].items()]
        young_note = "窗口内不足 60 个交易日行情的证券。"
    return {
        "risk_warning_names": {"count": len(warnings), "items": warnings[:LIST_LIMIT]},
        "suspended": {
            "count": len(suspended) if not constraints.empty else None,
            "items": suspended[:LIST_LIMIT],
            "available": not constraints.empty,
        },
        "young_listings": {
            "count": len(young) if young is not None else None,
            "items": (young or [])[:LIST_LIMIT],
            "note": young_note,
        },
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(inputs):
    """Pure, deterministic report from flat row lists. All values are JSON-safe; missing data stays None."""
    as_of = _day(inputs["as_of"])
    days = sorted({_day(d) for d in inputs.get("days") or []})
    instruments = pd.DataFrame(inputs.get("instruments") or [], columns=["symbol", "name", "board", "list_date"])
    instruments = instruments.drop_duplicates("symbol").sort_values("symbol")
    names = dict(zip(instruments["symbol"], instruments["name"]))
    bars = inputs.get("bars") or []
    max_dataset = max(
        [
            int(r["dataset_id"])
            for key in ("bars", "factors", "benchmark", "constraints")
            for r in inputs.get(key) or []
            if r.get("dataset_id") is not None
        ]
        or [0]
    )
    meta = {
        "engine": ENGINE,
        "report_version": REPORT_VERSION,
        "as_of": as_of,
        "session": days[-1] if days else None,
        "window_start": days[0] if days else None,
        "sessions_available": len(days),
        "symbols_total": int(len(instruments)),
        "symbols_with_bars": 0,
        "coverage": None,
        "benchmark_available": False,
        "max_dataset_id": max_dataset,
        "data_note": DATA_NOTE,
        "disclaimer": DISCLAIMER,
    }
    input_hash = digest(
        {"as_of": as_of, "max_dataset_id": max_dataset, "symbols": int(len(instruments)), "sessions": len(days)}
    )
    if not days or not bars:
        return _json_ready(
            {
                "status": "insufficient_data",
                "reason": "No open sessions or daily bars stored for the requested window.",
                "meta": meta,
                "benchmark": (
                    _benchmark_section(inputs.get("benchmark") or [], days)
                    if days
                    else {"status": "unavailable", "symbol": BENCHMARK}
                ),
                "input_hash": input_hash,
            }
        )
    t = len(days) - 1
    raw = {field: _pivot(bars, field, days) for field in ("open", "high", "low", "close", "pre_close", "turnover")}
    factor = _pivot(inputs.get("factors") or [], "factor", days).reindex(columns=raw["close"].columns)
    anchor = factor.ffill().iloc[-1]
    adjusted = (raw["close"] * factor).div(anchor, axis=1)
    adjusted = adjusted.loc[:, adjusted.notna().any()].replace([np.inf, -np.inf], np.nan)
    constraints = pd.DataFrame(inputs.get("constraints") or [])
    if not constraints.empty:
        constraints = _latest_rows(constraints, ["symbol"]).set_index("symbol")
        for column in ("limit_up", "limit_down"):
            constraints[column] = pd.to_numeric(constraints.get(column), errors="coerce")
        suspended = (
            constraints["suspended"] if "suspended" in constraints.columns else pd.Series(index=constraints.index)
        )
        constraints["suspended"] = suspended.map(_truthy).astype(bool)
    close_t = raw["close"].iloc[t]
    meta["symbols_with_bars"] = int(close_t.notna().sum())
    meta["coverage"] = meta["symbols_with_bars"] / meta["symbols_total"] if meta["symbols_total"] else None
    meta["symbols_with_factors"] = int(len(adjusted.columns))
    benchmark = _benchmark_section(inputs.get("benchmark") or [], days)
    meta["benchmark_available"] = benchmark.get("status") == "available"
    snapshot, factors = _factor_sections(adjusted, raw["turnover"], names, days, t)
    report = {
        "status": "complete",
        "meta": meta,
        "benchmark": benchmark,
        "breadth": _breadth_section(raw, adjusted, constraints, t),
        "boards": _boards_section(instruments, raw, adjusted, t),
        "factors": factors,
        "composite": _composite_section(snapshot, instruments, close_t),
        "risk_flags": _risk_section(instruments, constraints, raw["close"], days, names),
        "input_hash": input_hash,
    }
    return _json_ready(report)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def store_report(conn, report):
    row = conn.execute(
        "INSERT INTO reports(kind,target,as_of,input_hash,data,engine) VALUES(%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT DO NOTHING RETURNING id",
        (KIND, TARGET, _day(report["meta"]["as_of"]), report["input_hash"], jsonb(report), ENGINE),
    ).fetchone()
    return row["id"] if row else None


def publish(db, settings, job):
    as_of = date.fromisoformat(job["payload"]["as_of"])
    report = build(load_inputs(db, as_of))
    with db.publication(job) as conn:
        store_report(conn, report)
    return report


def latest(db, as_of=None):
    rows = db.rows(
        "SELECT id,kind,target,as_of,input_hash,engine,created_at,data FROM reports "
        "WHERE kind=%s AND target=%s AND (%s::date IS NULL OR as_of=%s) ORDER BY as_of DESC,id DESC LIMIT 1",
        (KIND, TARGET, as_of, as_of),
    )
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _pct(value):
    return f"{value * 100:.2f}%" if _number(value) is not None else "—"


def _num(value, digits=4):
    return f"{value:.{digits}f}" if _number(value) is not None else "—"


def _int(value):
    return str(int(value)) if value is not None else "—"


def _amount(value):
    if _number(value) is None:
        return "—"
    return f"{value / 1e8:.2f} 亿元" if abs(value) >= 1e8 else f"{value / 1e4:.0f} 万元"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def render_markdown(report):
    meta = report["meta"]
    lines = [
        f"# 量化分析报告 · {meta.get('as_of')}",
        "",
        f"> {DISCLAIMER}",
        f"> {meta.get('data_note') or DATA_NOTE}",
        "",
        "## 概览",
        "",
        _table(
            ["项目", "数值"],
            [
                ["报告日期", meta.get("as_of") or "—"],
                ["最近交易日", meta.get("session") or "—"],
                ["窗口交易日数", _int(meta.get("sessions_available"))],
                ["在市证券数", _int(meta.get("symbols_total"))],
                ["当日有行情证券数", _int(meta.get("symbols_with_bars"))],
                ["行情覆盖率", _pct(meta.get("coverage"))],
                ["基准指数可用", "是" if meta.get("benchmark_available") else "否"],
                ["引擎", meta.get("engine") or ENGINE],
            ],
        ),
        "",
    ]
    if report.get("status") != "complete":
        lines += [f"**状态：{report.get('status')}** — {report.get('reason', '数据不足，无法生成完整报告。')}", ""]
        return "\n".join(lines)
    bench = report["benchmark"]
    lines += ["## 基准指数（沪深300，SH000300）", ""]
    if bench.get("status") == "available":
        lines += [
            _table(
                ["指标", "数值"],
                [
                    ["收盘", _num(bench.get("close"), 2)],
                    ["1日收益", _pct(bench.get("return_1"))],
                    ["5日收益", _pct(bench.get("return_5"))],
                    ["20日收益", _pct(bench.get("return_20"))],
                    ["60日收益", _pct(bench.get("return_60"))],
                    ["120日收益", _pct(bench.get("return_120"))],
                    ["20日年化波动率", _pct(bench.get("volatility_20"))],
                    [f"距近{bench.get('drawdown_sessions')}日高点回撤", _pct(bench.get("drawdown_250"))],
                    ["收盘相对MA20", _pct(bench.get("close_vs_ma20"))],
                    ["收盘相对MA60", _pct(bench.get("close_vs_ma60"))],
                ],
            ),
            "",
        ]
    else:
        lines += ["基准指数数据不可用。", ""]
    breadth = report["breadth"]
    lines += [
        "## 市场宽度",
        "",
        _table(
            ["指标", "数值"],
            [
                [
                    "上涨 / 下跌 / 平盘",
                    f"{_int(breadth['advancers'])} / {_int(breadth['decliners'])} / {_int(breadth['unchanged'])}",
                ],
                ["涨跌未知", _int(breadth.get("change_unknown"))],
                ["站上MA20比例", _pct(breadth.get("above_ma20_ratio"))],
                ["站上MA60比例", _pct(breadth.get("above_ma60_ratio"))],
                ["60日新高 / 新低", f"{_int(breadth.get('new_high_60'))} / {_int(breadth.get('new_low_60'))}"],
                ["成交额中位数", _amount(breadth.get("median_turnover"))],
                ["涨停 / 跌停", f"{_int(breadth.get('limit_up'))} / {_int(breadth.get('limit_down'))}"],
                ["停牌", _int(breadth.get("suspended"))],
                ["涨跌停数据来源", "market_constraints" if breadth.get("constraints_available") else "不可用"],
            ],
        ),
        "",
        "## 板块",
        "",
        _table(
            ["板块", "证券数", "有行情", "1日均收益", "5日均收益", "20日均收益", "20日波动率中位", "振幅中位"],
            [
                [
                    b["label"],
                    _int(b["count"]),
                    _int(b["with_bars"]),
                    _pct(b["mean_return_1"]),
                    _pct(b["mean_return_5"]),
                    _pct(b["mean_return_20"]),
                    _pct(b["median_volatility_20"]),
                    _pct(b["median_amplitude"]),
                ]
                for b in report["boards"]
            ],
        ),
        "",
        "## 因子横截面与历史有效性（Rank IC，前瞻20日）",
        "",
        _table(
            ["因子", "覆盖数", "IC均值", "IC标准差", "ICIR", "IC>0比例", "评估期数", "状态"],
            [
                [
                    f["label"],
                    _int(f["coverage"]),
                    _num(f["efficacy"]["ic_mean"]),
                    _num(f["efficacy"]["ic_std"]),
                    _num(f["efficacy"]["ic_ir"], 2),
                    _pct(f["efficacy"]["ic_positive_ratio"]),
                    _int(f["efficacy"]["samples"]),
                    f["efficacy"]["status"],
                ]
                for f in report["factors"].values()
            ],
        ),
        "",
    ]
    for name, section in report["factors"].items():
        if not section["top"]:
            lines += [f"### {section['label']}", "", "该因子在报告日无有效取值。", ""]
            continue
        lines += [
            f"### {section['label']}",
            "",
            _table(
                ["最高10", "名称", "取值", "最低10", "名称", "取值"],
                [
                    [
                        top["symbol"] if top else "",
                        (top or {}).get("name") or "",
                        _num(top["value"]) if top else "",
                        bottom["symbol"] if bottom else "",
                        (bottom or {}).get("name") or "",
                        _num(bottom["value"]) if bottom else "",
                    ]
                    for top, bottom in zip(
                        section["top"] + [None] * (10 - len(section["top"])),
                        section["bottom"] + [None] * (10 - len(section["bottom"])),
                    )
                ],
            ),
            "",
        ]
    composite = report["composite"]
    lines += ["## 描述性排名组合", "", f"_{composite['label']}_", ""]
    if composite.get("status") == "complete":
        for title, rows in (("前20", composite["top"]), ("后20", composite["bottom"])):
            lines += [
                f"### {title}",
                "",
                _table(
                    ["代码", "名称", "板块", "收盘", "综合分", "20日动量", "60日动量(剔5)", "20日波动率"],
                    [
                        [
                            r["symbol"],
                            r.get("name") or "",
                            BOARD_LABELS.get(r.get("board"), r.get("board") or ""),
                            _num(r["close"], 2),
                            _num(r["score"], 3),
                            _pct(r["momentum_20"]),
                            _pct(r["momentum_60_ex_5"]),
                            _pct(r["volatility_20"]),
                        ]
                        for r in rows
                    ],
                ),
                "",
            ]
    else:
        lines += ["有效数据不足，无法计算排名组合。", ""]
    risk = report["risk_flags"]
    lines += [
        "## 风险标识",
        "",
        f"- 风险警示名称（含 ST/退）：{_int(risk['risk_warning_names']['count'])}",
        f"- 停牌：{_int(risk['suspended']['count'])}"
        + ("" if risk["suspended"]["available"] else "（约束数据不可用）"),
        f"- 新上市/历史不足60日：{_int(risk['young_listings']['count'])}（{risk['young_listings']['note']}）",
        "",
        f"输入摘要：`{report['input_hash']}`",
        "",
        DISCLAIMER,
    ]
    return "\n".join(lines)
