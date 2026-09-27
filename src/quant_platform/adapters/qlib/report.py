"""Isolated Qlib research report: IC, factor quantiles and a benchmark-relative book.

This module is the *only* place besides the runtime adapter that imports Qlib, and it always runs in
its own process (``python -m quant_platform.adapters.qlib.report`` with the job JSON on stdin). The
collector, the scheduler and the worker therefore never depend on the optional engine: a missing or
unusable install fails this one job and leaves collection plus the descriptive market report intact.

Everything is read through ``D.features`` against the immutable generation published by
:mod:`quant_platform.adapters.qlib.export`:

- factor: twenty-session momentum ``$close/Ref($close, 20) - 1``
- label: forward five-session return ``Ref($close, -5)/$close - 1``

Costs use the platform's standard simulated model (commission, stamp duty, slippage, lot) so the
reported book is stated on the same terms as every other simulated figure here. The result is written
to ``reports.kind='qlib'`` and mirrored as Markdown inside the generation directory. It is a
descriptive statistic, never a recommendation and never a target weight.
"""

import json
import re
import sys
from datetime import date

import numpy as np
import pandas as pd

from quant_platform.domain import digest, now, symbol
from quant_platform.domain.workflow import ENGINE_VERSION, PipelineBlocked
from quant_platform.storage import jsonb

BENCHMARK = "SH000300"
MOMENTUM_WINDOW = 20
LABEL_HORIZON = 5
REBALANCE_SESSIONS = 20
TOP_N = 30
GENERATION_PATTERN = r"generation-[a-f0-9]{32}"
# 沿用研究路径的成本常数（analysis/research.py），保证报告与原生研究记录可比。
LOT = 100
COMMISSION = 0.0003
STAMP = 0.0005
SLIPPAGE = 0.001
REPORT_VERSION = ENGINE_VERSION + ":qlib-report-1"
CLOSE_FIELD = "$close"
MOMENTUM_FIELD = "$close/Ref($close, 20) - 1"
FORWARD_FIELD = "Ref($close, -5)/$close - 1"


def generation_root(settings, name):
    if not re.fullmatch(GENERATION_PATTERN, str(name)):
        raise PipelineBlocked("Invalid Qlib generation name.")
    root = settings.artifact_root.absolute() / "qlib" / name
    if root.is_symlink() or not root.is_dir():
        raise PipelineBlocked("The Qlib generation is missing or unsafe.")
    if not (root / "manifest.json").is_file() or not (root / "calendars/day.txt").is_file():
        raise PipelineBlocked("The Qlib generation is incomplete.")
    return root


def load_engine():
    try:
        import qlib
        from qlib.data import D
    except ImportError:
        raise PipelineBlocked(
            "Qlib is unavailable in this runtime; install the optional 'qlib' extra to publish research reports."
        ) from None
    return qlib, D


_INITIALIZED = None


def initialize(root):
    """Initialise the process-wide Qlib runtime before any ``D.*`` accessor is used.

    ``D`` is a lazy proxy that raises ``AttributeError: Please run qlib.init() first`` for every
    accessor until ``qlib.init`` has run. Initialising inside ``panel`` was too late: ``build`` reads
    ``D.instruments``/``D.list_instruments`` first, so every report job failed with that error.
    """
    global _INITIALIZED
    qlib, D = load_engine()
    provider_uri = str(root)
    if _INITIALIZED != provider_uri:
        qlib.init(provider_uri=provider_uri, region="cn", kernels=1, expression_cache=None, dataset_cache=None)
        _INITIALIZED = provider_uri
    return D


def panel(root, instruments, start, end):
    """One cross-sectional panel of close, twenty-session momentum and forward five-session return."""
    D = initialize(root)
    frame = D.features(
        instruments,
        [CLOSE_FIELD, MOMENTUM_FIELD, FORWARD_FIELD],
        start_time=start,
        end_time=end,
        freq="day",
        disk_cache=0,
    )
    if frame is None or frame.shape[1] != 3:
        raise PipelineBlocked("Qlib returned an unexpected feature panel.")
    frame = frame.copy()
    frame.columns = ["close", "momentum", "forward"]
    frame.index = frame.index.set_names(["instrument", "datetime"])
    return frame


def sessions(D, start, end):
    stamps = D.calendar(start_time=start, end_time=end, freq="day")
    return [stamp.date() if hasattr(stamp, "date") else stamp for stamp in stamps]


def correlation(left, right):
    if len(left) < 3:
        return None
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if np.std(left) == 0 or np.std(right) == 0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    return value if np.isfinite(value) else None


def rank_correlation(left, right):
    return correlation(pd.Series(left).rank().to_numpy(), pd.Series(right).rank().to_numpy())


def quantile_means(frame):
    if frame.empty:
        return []
    buckets = pd.qcut(frame["momentum"], min(5, frame["momentum"].nunique()), labels=False, duplicates="drop")
    rows = []
    for bucket, group in frame.groupby(buckets):
        rows.append({"bucket": int(bucket), "count": int(len(group)), "mean_forward": float(group["forward"].mean())})
    return rows


def daily_ic(frame):
    values = []
    for _, group in frame.groupby(level="datetime"):
        if len(group) < 5:
            continue
        value = correlation(group["momentum"], group["forward"])
        if value is not None:
            values.append(value)
    return {
        "sessions": len(values),
        "mean": float(np.mean(values)) if values else None,
        "positive_ratio": float(np.mean([value > 0 for value in values])) if values else None,
    }


def benchmark_closes(db, start, end):
    rows = db.rows(
        "SELECT DISTINCT ON(b.day) b.day,(b.data->>'close')::float8 AS close FROM daily_bars b "
        "WHERE b.symbol=%s AND b.day BETWEEN %s AND %s ORDER BY b.day,b.dataset_id DESC",
        (BENCHMARK, start, end),
    )
    return {row["day"]: row["close"] for row in rows if row["close"] is not None}


def book(frame, days, benchmark):
    """Top-30 equal-weight book, rebalanced every twenty sessions, measured against the benchmark."""
    closes = frame["close"].unstack("instrument")
    momentum = frame["momentum"].unstack("instrument")
    closes.index = [stamp.date() if hasattr(stamp, "date") else stamp for stamp in closes.index]
    momentum.index = [stamp.date() if hasattr(stamp, "date") else stamp for stamp in momentum.index]
    periods, weights, turnover_total = [], {}, 0.0
    for index in range(0, len(days) - REBALANCE_SESSIONS, REBALANCE_SESSIONS):
        opened, closed = days[index], days[index + REBALANCE_SESSIONS]
        if opened not in momentum.index or opened not in closes.index or closed not in closes.index:
            continue
        ranked = momentum.loc[opened].dropna().sort_values(ascending=False)
        selected = [
            code for code in ranked.index if pd.notna(closes.at[closed, code]) and closes.at[opened, code]
        ][:TOP_N]
        if not selected:
            continue
        if opened not in benchmark or closed not in benchmark or not benchmark[opened]:
            raise PipelineBlocked("Benchmark history does not cover the reporting window.")
        target = {code: 1 / len(selected) for code in selected}
        turnover = 0.5 * sum(
            abs(target.get(code, 0.0) - weights.get(code, 0.0)) for code in set(target) | set(weights)
        )
        gross = float(np.mean([closes.at[closed, code] / closes.at[opened, code] - 1 for code in selected]))
        cost = turnover * (COMMISSION + STAMP + SLIPPAGE)
        turnover_total += turnover
        weights = target
        periods.append(
            {
                "day": str(opened),
                "next_day": str(closed),
                "holdings": len(selected),
                "gross_return": gross,
                "cost": cost,
                "net_return": gross - cost,
                "benchmark_return": benchmark[closed] / benchmark[opened] - 1,
                "turnover": turnover,
            }
        )
    if not periods:
        return {"status": "insufficient_history", "rebalances": 0, "periods": []}
    growth = float(np.prod([1 + row["net_return"] for row in periods]) - 1)
    benchmark_growth = float(np.prod([1 + row["benchmark_return"] for row in periods]) - 1)
    return {
        "status": "complete",
        "rebalances": len(periods),
        "cumulative_return": growth,
        "benchmark_cumulative_return": benchmark_growth,
        "relative_return": growth - benchmark_growth,
        "mean_turnover": turnover_total / len(periods),
        "periods": periods,
    }


def coverage_window(root):
    """The generation's real coverage window, taken from its instrument intervals.

    ``manifest["coverage"]`` maps every listed symbol to either its *last* covered session or the
    status string ``missing_adjustment_or_bars``. Deriving the window from it was wrong twice over:
    the window start became the latest end date (a one-session panel, so no factor, no label and no
    book), and a generation with no usable symbol at all would have raised on ``fromisoformat``.
    ``instruments/all.txt`` carries the authoritative ``code start end`` interval per instrument.
    """
    starts, ends = [], []
    for line in (root / "instruments" / "all.txt").read_text().splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise PipelineBlocked("The Qlib generation has an invalid instrument interval.")
        symbol(fields[0])
        starts.append(date.fromisoformat(fields[1]))
        ends.append(date.fromisoformat(fields[2]))
    if not starts:
        raise PipelineBlocked("The Qlib generation lists no instruments.")
    return min(starts), max(ends)


def build(db, settings, job):
    payload = job["payload"]
    name = payload["generation"]
    as_of = date.fromisoformat(payload["as_of"])
    root = generation_root(settings, name)
    D = initialize(root)
    instruments = D.list_instruments(D.instruments("all"), freq="day", as_list=True)
    if not instruments:
        raise PipelineBlocked("The Qlib generation lists no instruments.")
    names = {raw: symbol(raw) for raw in instruments}
    start, end = coverage_window(root)
    frame = panel(root, instruments, start, end)
    frame = frame.rename(index=names, level="instrument")
    days = sessions(D, start, end)
    benchmark = benchmark_closes(db, start, end)
    usable = frame[["momentum", "forward"]].dropna()
    report = {
        "report_version": REPORT_VERSION,
        "engine": ENGINE_VERSION,
        "generation": name,
        "dataset_watermark": payload.get("dataset_watermark"),
        "as_of": str(as_of),
        "window": {"start": str(start), "end": str(end)},
        "benchmark": BENCHMARK,
        "parameters": {
            "factor": MOMENTUM_FIELD,
            "label": FORWARD_FIELD,
            "momentum_window": MOMENTUM_WINDOW,
            "label_horizon": LABEL_HORIZON,
            "rebalance_sessions": REBALANCE_SESSIONS,
            "top_n": TOP_N,
        },
        "sessions": len(days),
        "instruments": len(instruments),
        "samples": int(len(usable)),
        "ic": correlation(usable["momentum"], usable["forward"]),
        "rank_ic": rank_correlation(usable["momentum"], usable["forward"]),
        "daily_ic": daily_ic(usable),
        "quantiles": quantile_means(usable),
        "book": book(frame, days, benchmark),
        "costs": {"commission": COMMISSION, "stamp_duty": STAMP, "slippage": SLIPPAGE, "lot": LOT},
        "label": "Descriptive Qlib statistics. Not a recommendation, not a target weight, no order.",
        "automatic_orders": False,
    }
    report["input_hash"] = digest(
        {
            "generation": name,
            "watermark": payload.get("dataset_watermark"),
            "parameters": report["parameters"],
            "report_version": REPORT_VERSION,
        }
    )
    return report


def percent(value):
    return "—" if value is None else f"{value * 100:.2f}%"


def decimal(value):
    return "—" if value is None else f"{value:.4f}"


def render_markdown(report):
    book = report["book"]
    lines = [
        f"# Qlib 研究报告 · {report['as_of']}",
        "",
        f"- 代次：`{report['generation']}`",
        f"- 窗口：{report['window']['start']} → {report['window']['end']}（{report['sessions']} 个交易日）",
        f"- 证券数：{report['instruments']}，有效样本：{report['samples']}",
        f"- 因子：`{report['parameters']['factor']}`；标签：`{report['parameters']['label']}`",
        f"- 基准：{report['benchmark']}",
        "",
        "## 预测能力",
        "",
        f"- IC：{decimal(report['ic'])}",
        f"- Rank IC：{decimal(report['rank_ic'])}",
        f"- 日均 IC：{decimal(report['daily_ic']['mean'])}（{report['daily_ic']['sessions']} 个交易日，"
        f"正比例 {percent(report['daily_ic']['positive_ratio'])}）",
        "",
        "## 五分位平均未来收益",
        "",
        "| 分位 | 样本数 | 平均未来 5 日收益 |",
        "| --- | --- | --- |",
    ]
    for row in report["quantiles"]:
        lines.append(f"| Q{row['bucket'] + 1} | {row['count']} | {percent(row['mean_forward'])} |")
    lines += ["", "## 相对基准组合", ""]
    if book["status"] == "complete":
        lines += [
            f"- 调仓次数：{book['rebalances']}（每 {REBALANCE_SESSIONS} 个交易日，前 {TOP_N} 只等权）",
            f"- 组合累计收益（扣费）：{percent(book['cumulative_return'])}",
            f"- 基准累计收益：{percent(book['benchmark_cumulative_return'])}",
            f"- 相对基准：{percent(book['relative_return'])}",
            f"- 平均单边换手：{percent(book['mean_turnover'])}",
            f"- 成本：佣金 {COMMISSION}，印花税 {STAMP}，滑点 {SLIPPAGE}，最小交易单位 {LOT} 股",
        ]
    else:
        lines.append("历史长度不足以完成一次完整调仓，组合收益未计算。")
    lines += [
        "",
        "> 本报告为描述性量化统计，不构成投资建议，也不是模型推荐或目标权重。",
        "",
    ]
    return "\n".join(lines)


def run(db, settings, job):
    report = build(db, settings, job)
    markdown = render_markdown(report)
    root = generation_root(settings, report["generation"])
    document = root / f"qlib-report-{report['as_of']}.md"
    document.write_text(markdown, encoding="utf-8")
    report = {**report, "markdown": markdown, "document": document.name, "reported_at": now().isoformat()}
    with db.publication(job) as conn:
        conn.execute(
            "INSERT INTO reports(kind,target,as_of,input_hash,data,engine) VALUES('qlib',%s,%s,%s,%s,%s) "
            "ON CONFLICT(kind,target,as_of,input_hash) DO UPDATE SET data=excluded.data,engine=excluded.engine",
            (BENCHMARK, date.fromisoformat(report["as_of"]), report["input_hash"], jsonb(report), REPORT_VERSION),
        )
        db.set_setting(conn, "qlib_report", {"as_of": report["as_of"], "generation": report["generation"]})
    return report


def main():
    job = json.load(sys.stdin)
    from quant_platform.config import Settings
    from quant_platform.storage import Database

    settings = Settings()
    db = Database(settings.dsn, role="quant_worker")
    try:
        run(db, settings, job)
    except PipelineBlocked as exc:
        print("QUAP_BLOCKED=" + str(exc))
        sys.exit(3)
    finally:
        db.close()


if __name__ == "__main__":
    main()
