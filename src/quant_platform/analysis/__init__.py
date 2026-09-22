"""Deterministic, versioned native analytics; no Qlib imports or model claims."""

import re

import numpy as np
import pandas as pd

from quant_platform import __version__
from quant_platform.domain import HoldingPolicy, ScreenRule, digest, fresh, number

ANALYSIS_VERSION = __version__ + ":native-3"
DISCLAIMER = "Research suggestion; not an order, recommendation to trade, or return guarantee."


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


def _industry(stock):
    payload = stock.get("data") if isinstance(stock.get("data"), dict) else {}
    return str(payload.get("industry") or stock.get("industry") or "").strip()


def _current_basic(rows, day):
    if not rows:
        return None
    last = rows[-1]
    last_day = str(last.get("day") or (last.get("data") or {}).get("day") or "")
    if last_day != str(day):
        return None
    data = last.get("data") if isinstance(last.get("data"), dict) and "pe_ttm" in last.get("data", {}) else last
    return data


def _own_percentile(rows, field, current, limit):
    values = []
    for row in rows:
        data = row.get("data") if isinstance(row.get("data"), dict) and field in row.get("data", {}) else row
        value = number(data.get(field))
        if value is not None and value > 0:
            values.append(value)
    values = values[-limit:]
    if current is None or current <= 0 or not values:
        return None
    if values[-1] != current:
        values.append(current)
    return float(pd.Series(values).rank(pct=True, method="average").iloc[-1])


def screen(instruments, metrics, day, rule=None, fundamentals=None):
    rule = rule or ScreenRule()
    fundamentals = fundamentals or {}
    eligible, excluded = [], {}
    for code, stock in sorted(instruments.items()):
        item = metrics.get(code, {})
        basic = _current_basic(fundamentals.get(code) or [], day)
        pe_ttm = number((basic or {}).get("pe_ttm"))
        pb = number((basic or {}).get("pb"))
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
        if basic is None:
            reasons.append("missing_fundamentals")
        else:
            if pe_ttm is None or pe_ttm <= 0:
                reasons.append("non_positive_pe_ttm")
            elif pe_ttm > rule.max_pe_ttm:
                reasons.append("pe_ttm_above_cap")
            if pb is None or pb <= 0:
                reasons.append("non_positive_pb")
            elif pb > rule.max_pb:
                reasons.append("pb_above_cap")
        if reasons:
            excluded[code] = reasons
        else:
            eligible.append(
                {
                    "symbol": code,
                    "industry": _industry(stock),
                    "pe_ttm": pe_ttm,
                    "pb": pb,
                    "pe_history_pct": _own_percentile(
                        fundamentals.get(code) or [], "pe_ttm", pe_ttm, rule.history_sessions
                    ),
                    "pb_history_pct": _own_percentile(fundamentals.get(code) or [], "pb", pb, rule.history_sessions),
                    "average_turnover20": item["average_turnover20"],
                }
            )
    candidates = []
    if eligible:
        frame = pd.DataFrame(eligible)
        industry_counts = frame.groupby("industry")["symbol"].transform("count")
        frame["peer_scope"] = np.where(
            (frame["industry"] != "") & (industry_counts >= rule.min_industry_peers), "industry", "universe"
        )
        frame["pe_industry_pct"] = np.nan
        frame["pb_industry_pct"] = np.nan
        industry_mask = frame["peer_scope"] == "industry"
        if industry_mask.any():
            grouped = frame.loc[industry_mask]
            frame.loc[industry_mask, "pe_industry_pct"] = grouped.groupby("industry")["pe_ttm"].rank(pct=True)
            frame.loc[industry_mask, "pb_industry_pct"] = grouped.groupby("industry")["pb"].rank(pct=True)
        universe_mask = ~industry_mask
        if universe_mask.any():
            frame.loc[universe_mask, "pe_industry_pct"] = frame.loc[universe_mask, "pe_ttm"].rank(pct=True)
            frame.loc[universe_mask, "pb_industry_pct"] = frame.loc[universe_mask, "pb"].rank(pct=True)
        frame["pe_history_pct"] = frame["pe_history_pct"].fillna(frame["pe_industry_pct"])
        frame["pb_history_pct"] = frame["pb_history_pct"].fillna(frame["pb_industry_pct"])
        frame["score"] = (
            rule.pe_weight * (1 - frame.pe_industry_pct)
            + rule.pb_weight * (1 - frame.pb_industry_pct)
            + rule.pe_history_weight * (1 - frame.pe_history_pct)
            + rule.pb_history_weight * (1 - frame.pb_history_pct)
        )
        ranked = frame.sort_values(["score", "symbol"], ascending=[False, True]).head(rule.top_n)
        for row in ranked.to_dict("records"):
            hints = []
            if row["pe_industry_pct"] <= 0.4:
                hints.append("low_pe_ttm_vs_peers")
            if row["pb_industry_pct"] <= 0.4:
                hints.append("low_pb_vs_peers")
            if row["pe_history_pct"] <= 0.4:
                hints.append("low_pe_ttm_vs_own_history")
            if row["pb_history_pct"] <= 0.4:
                hints.append("low_pb_vs_own_history")
            candidates.append(
                {
                    "symbol": row["symbol"],
                    "score": float(row["score"]),
                    "pe_ttm": row["pe_ttm"],
                    "pb": row["pb"],
                    "industry": row["industry"],
                    "peer_scope": row["peer_scope"],
                    "pe_industry_pct": float(row["pe_industry_pct"]),
                    "pb_industry_pct": float(row["pb_industry_pct"]),
                    "pe_history_pct": float(row["pe_history_pct"]),
                    "pb_history_pct": float(row["pb_history_pct"]),
                    "average_turnover20": row["average_turnover20"],
                    "reasons": hints or ["relative_valuation_rank"],
                    "action": "review_entry",
                }
            )
    fundamentals_present = sum(1 for code in instruments if _current_basic(fundamentals.get(code) or [], day))
    return {
        "as_of": str(day),
        "candidates": candidates,
        "eligible": len(eligible),
        "excluded": excluded,
        "total": len(instruments),
        "rule": rule.model_dump(),
        "analysis_version": ANALYSIS_VERSION,
        "disclaimer": DISCLAIMER,
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
        "fundamentals_coverage": fundamentals_present / len(instruments) if instruments else 0,
        "automatic_basket_changes": False,
    }


def holding_advice(
    holding, quote=None, at=None, last_close=None, stock_report=None, screen_candidates=None, policy=None, name=""
):
    policy = policy or HoldingPolicy()
    screen_candidates = screen_candidates or []
    quote = quote or {}
    stock_report = stock_report or {}
    last = None
    source = None
    quote_close = number(quote.get("close"))
    if (
        fresh(quote.get("source_time"), at)
        and quote.get("reference_verified")
        and quote_close is not None
        and quote_close > 0
    ):
        last, source = quote_close, "fresh_quote"
    else:
        close = number(last_close)
        if close is not None and close > 0:
            last, source = close, "daily_close"
    cost = number(holding.get("cost_price"))
    if last is None or cost is None or cost <= 0:
        return {
            "symbol": holding.get("symbol"),
            "action": "insufficient_data",
            "reasons": ["missing_or_stale_price"],
            "last": last,
            "cost_price": cost,
            "pnl_pct": None,
            "pnl_amount": None,
            "price_source": source,
            "still_undervalued": False,
            "risk_flags": stock_report.get("risk_flags") or [],
            "disclaimer": DISCLAIMER,
            "analysis_version": ANALYSIS_VERSION,
            "policy": policy.model_dump(),
        }
    pnl_pct = last / cost - 1
    quantity = number(holding.get("quantity"))
    pnl_amount = (last - cost) * quantity if quantity is not None else None
    still = holding.get("symbol") in {row["symbol"] for row in screen_candidates}
    flags = stock_report.get("risk_flags") or []
    risky_name = bool(re.search(r"ST|退", (name or holding.get("name") or "").upper()))
    if pnl_pct <= -policy.exit_loss:
        action, reasons = "review_exit", ["loss_exceeds_exit_threshold"]
    elif risky_name:
        action, reasons = "review_exit", ["name_based_risk_filter"]
    elif "below_sma60" in flags and "high_volatility" in flags:
        action, reasons = "review_exit", ["below_sma60", "high_volatility"]
    elif pnl_pct >= policy.reduce_gain:
        action, reasons = "review_reduce", ["gain_exceeds_reduce_threshold"]
    elif policy.reduce_if_not_undervalued and pnl_pct > 0 and not still:
        action, reasons = "review_reduce", ["profitable_and_no_longer_undervalued"]
    elif still and pnl_pct <= policy.add_max_pnl and "high_volatility" not in flags:
        action, reasons = "review_add", ["still_undervalued", "pnl_within_add_band"]
    else:
        action, reasons = "hold", ["no_review_threshold_hit"]
    return {
        "symbol": holding.get("symbol"),
        "action": action,
        "reasons": reasons,
        "last": last,
        "cost_price": cost,
        "pnl_pct": pnl_pct,
        "pnl_amount": pnl_amount,
        "price_source": source,
        "still_undervalued": still,
        "risk_flags": flags,
        "disclaimer": DISCLAIMER,
        "analysis_version": ANALYSIS_VERSION,
        "policy": policy.model_dump(),
    }
