"""Deterministic, versioned native analytics; no Qlib imports or model claims."""

import re

import numpy as np
import pandas as pd

from quant_platform import __version__
from quant_platform.domain import ScreenRule, digest, fresh, number

ANALYSIS_VERSION = __version__ + ":native-2"


def wilder(series, periods=14):
    values = series.to_numpy(dtype=float)
    if len(values) < periods or not np.isfinite(values).all():
        return None
    result = float(values[:periods].mean())
    for value in values[periods:]:
        result = (result * (periods - 1) + value) / periods
    return result


def indicators(rows):
    empty = {"status": "insufficient_data", "bars": len(rows), "analysis_version": ANALYSIS_VERSION}
    if not rows or any(row.get("factor") is None for row in rows):
        return {**empty, "reason": "Missing bars or adjustment factors."}
    frame = pd.DataFrame([{**row["data"], "factor": row["factor"], "day": str(row["day"])} for row in rows])
    if frame.day.duplicated().any() or not frame.day.is_monotonic_increasing:
        raise ValueError("Analysis requires ordered unique trading dates.")
    scale = frame.factor / frame.factor.iloc[-1]
    for col in ("open", "close", "high", "low"):
        frame[col] = frame[col].astype(float) * scale
    close = frame.close
    returns = close.pct_change(fill_method=None).dropna()
    changes = close.diff().dropna()
    gain, loss = wilder(changes.clip(lower=0)), wilder((-changes).clip(lower=0))
    rsi = None if gain is None else 50.0 if gain == loss == 0 else 100.0 if loss == 0 else 100 - 100 / (1 + gain / loss)
    true_range = pd.concat(
        [frame.high - frame.low, (frame.high - close.shift()).abs(), (frame.low - close.shift()).abs()], axis=1
    ).max(axis=1)
    result = {
        "status": "complete",
        "bars": len(rows),
        "as_of": frame.day.iloc[-1],
        "analysis_version": ANALYSIS_VERSION,
        "adjustment_anchor": frame.day.iloc[-1],
        "rsi14": rsi,
        "atr14": wilder(true_range.iloc[1:]),
        "input_hash": digest([(r["dataset_id"], r.get("factor_dataset_id")) for r in rows]),
        "close": float(close.iloc[-1]),
        "price_unit": "as-of-adjusted CNY",
        "volume_unit": "shares",
    }
    for n in (5, 10, 20, 60):
        result[f"sma{n}"] = float(close.iloc[-n:].mean()) if len(close) >= n else None
    for n in (5, 20, 60):
        result[f"return{n}"] = float(close.iloc[-1] / close.iloc[-n - 1] - 1) if len(close) > n else None
    result["volatility20"] = float(returns.iloc[-20:].std(ddof=1) * np.sqrt(252)) if len(returns) >= 20 else None
    sample = close.iloc[-60:]
    result["drawdown60"] = float((sample / sample.cummax() - 1).min()) if len(sample) == 60 else None
    for col in ("volume", "turnover"):
        tail = pd.to_numeric(frame[col].iloc[-20:], errors="coerce")
        result[f"average_{col}20"] = float(tail.mean()) if len(tail) == 20 and tail.notna().all() else None
    avg = result["average_volume20"]
    volume = number(frame.volume.iloc[-1])
    result["volume_ratio20"] = volume / avg if volume is not None and avg else None
    result["risk_flags"] = [
        key
        for key, hit in {
            "below_sma60": result["sma60"] is not None and result["close"] < result["sma60"],
            "high_volatility": result["volatility20"] is not None and result["volatility20"] > 0.5,
        }.items()
        if hit
    ]
    return result


def basket_summary(members, quotes, at):
    denominator = sum(members.values())
    weights = {s: w / denominator for s, w in members.items()}
    eligible = {}
    for code, weight in weights.items():
        q = quotes.get(code, {})
        last, previous = number(q.get("close")), number(q.get("pre_close"))
        if (
            fresh(q.get("source_time"), at)
            and q.get("reference_verified")
            and last is not None
            and previous is not None
            and last > 0
            and previous > 0
        ):
            eligible[code] = (weight, last / previous - 1)
    coverage = sum(w for w, _ in eligible.values())
    contributions = {s: w / coverage * ret for s, (w, ret) in eligible.items()} if coverage else {}
    daily = sum(contributions.values()) if coverage else None
    return {
        "daily_return": daily,
        "reference_proxy": 1000 * (1 + daily) if daily is not None else None,
        "coverage": coverage,
        "total": len(weights),
        "eligible": len(eligible),
        "missing": sorted(set(weights) - set(eligible)),
        "contributions": contributions,
        "concentration": sum(w * w for w in weights.values()),
        "advancing": sum(ret > 0 for _, ret in eligible.values()),
        "declining": sum(ret < 0 for _, ret in eligible.values()),
        "observation": digest(
            {s: {k: v for k, v in quotes[s].items() if k != "received_at"} for s in sorted(eligible)}
        ),
        "label": "Daily reference proxy, not investable NAV",
        "analysis_version": ANALYSIS_VERSION,
    }


def screen(instruments, metrics, day, rule=None):
    rule = rule or ScreenRule()
    eligible, excluded = [], {}
    for code, stock in sorted(instruments.items()):
        item = metrics.get(code, {})
        reasons = []
        if stock.get("status", "L") != "L":
            reasons.append("not_currently_verified_listed")
        if rule.exclude_risk_names and re.search(r"ST|退", stock["name"].upper()):
            reasons.append("name_based_risk_filter")
        if item.get("bars", 0) < rule.minimum_bars:
            reasons.append("insufficient_history")
        if item.get("as_of") != str(day) or item.get("status") != "complete":
            reasons.append("missing_or_unadjusted_latest_history")
        if item.get("average_turnover20") is None or item["average_turnover20"] < rule.minimum_turnover:
            reasons.append("liquidity")
        if item.get("return20") is None or item.get("volatility20") is None:
            reasons.append("indicators_unavailable")
        if reasons:
            excluded[code] = reasons
        else:
            eligible.append({"symbol": code, **item})
    candidates = []
    if eligible:
        frame = pd.DataFrame(eligible)
        frame["score"] = rule.momentum_weight * frame.return20.rank(pct=True) + (1 - rule.momentum_weight) * (
            1 - frame.volatility20.rank(pct=True)
        )
        candidates = (
            frame.sort_values(["score", "symbol"], ascending=[False, True])
            .head(rule.top_n)[["symbol", "score", "return20", "volatility20", "average_turnover20"]]
            .to_dict("records")
        )
    return {
        "as_of": str(day),
        "candidates": candidates,
        "eligible": len(eligible),
        "excluded": excluded,
        "total": len(instruments),
        "rule": rule.model_dump(),
        "analysis_version": ANALYSIS_VERSION,
        "coverage": (
            sum(
                m.get("status") == "complete" and m.get("as_of") == str(day)
                for code, m in metrics.items()
                if code in instruments
            )
            / len(instruments)
            if instruments
            else 0
        ),
        "automatic_basket_changes": False,
    }
