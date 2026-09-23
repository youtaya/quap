"""Personal research path. Output is not NAV and does not submit orders."""

from datetime import date, datetime

import numpy as np
import pandas as pd

from quant_platform import __version__
from quant_platform.domain import ScreenRule, digest

RESEARCH_VERSION = __version__ + ":research-1"
LOT = 100
COMMISSION = 0.0003
STAMP = 0.0005
SLIPPAGE = 0.001


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _corr(left, right):
    if len(left) < 3 or np.std(left) == 0 or np.std(right) == 0:
        return None
    value = float(np.corrcoef(left, right)[0, 1])
    return value if np.isfinite(value) else None


def _adjusted(bars, days):
    anchor = None
    for day in reversed(days):
        factor = _number((bars.get(day) or {}).get("factor"))
        if factor:
            anchor = factor
            break
    if not anchor:
        return {}
    adjusted = {}
    for day, bar in bars.items():
        close, factor = _number(bar.get("close")), _number(bar.get("factor"))
        if close is not None and factor:
            adjusted[day] = close * factor / anchor
    return adjusted


def _window_values(bars, adjusted, days, index):
    needed = days[index - 20 : index + 1]
    if len(needed) < 21 or any(day not in adjusted for day in needed):
        return None
    closes = [adjusted[day] for day in needed]
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]
    turnover = [_number((bars.get(day) or {}).get("turnover")) for day in needed[-20:]]
    draw_days = days[max(0, index - 59) : index + 1]
    draw = None
    if len(draw_days) == 60 and all(day in adjusted for day in draw_days):
        peak = adjusted[draw_days[0]]
        worst = 0.0
        for day in draw_days:
            peak = max(peak, adjusted[day])
            worst = min(worst, adjusted[day] / peak - 1)
        draw = worst
    return {
        "return20": closes[-1] / closes[0] - 1,
        "volatility20": float(np.std(returns, ddof=1) * np.sqrt(252)),
        "average_turnover20": float(np.mean(turnover)) if all(item is not None for item in turnover) else None,
        "drawdown60": draw,
    }


def _listed(instrument, day):
    if instrument.get("status", "L") != "L":
        return False
    if instrument.get("list_date") is not None and _day(instrument["list_date"]) > day:
        return False
    if instrument.get("delist_date") is not None and _day(instrument["delist_date"]) <= day:
        return False
    return True


def _weights(scores, rule):
    ordered = sorted(scores, key=lambda item: (-item["score"], item["symbol"]))[: rule.top_n]
    weight = 1 / len(ordered)
    return {item["symbol"]: weight for item in ordered}, ordered


def _turnover(previous, current):
    codes = set(previous) | set(current)
    return 0.5 * sum(abs(current.get(code, 0) - previous.get(code, 0)) for code in codes)


def _quantiles(values):
    frame = pd.DataFrame(values)
    if frame.empty:
        return []
    frame["bucket"] = pd.qcut(frame["factor"], min(5, len(frame)), labels=False, duplicates="drop")
    rows = []
    for bucket, group in frame.groupby("bucket"):
        rows.append({"bucket": int(bucket), "count": int(len(group)), "mean_forward": float(group.forward.mean())})
    return rows


def _factor_report(samples, books):
    if len(samples) < 3:
        return {"status": "insufficient_cross_section", "count": len(samples)}
    factor = np.array([row["factor"] for row in samples], dtype=float)
    forward = np.array([row["forward"] for row in samples], dtype=float)
    turns = [_turnover(a, b) for a, b in zip(books, books[1:])] if len(books) > 1 else []
    return {
        "status": "complete",
        "count": len(samples),
        "ic": _corr(factor, forward),
        "rank_ic": _corr(pd.Series(factor).rank().to_numpy(), pd.Series(forward).rank().to_numpy()),
        "quantiles": _quantiles(samples),
        "signal_turnover": float(np.mean(turns)) if turns else None,
    }


def _simulate(signals, adjusted, days, limit_known):
    if not limit_known:
        return {"execution": "withheld_limit_unknown", "trades": [], "research_value": None}
    cash, shares = 1_000_000.0, {}
    trades = []
    for signal in signals:
        index = days.index(signal["day"])
        if index + 1 >= len(days):
            continue
        execution = days[index + 1]
        targets = signal["weights"]
        prices = {}
        for symbol in set(shares) | set(targets):
            bar = signal["execution_bars"].get(symbol) or {}
            close = adjusted.get(symbol, {}).get(execution)
            limit_up, limit_down = _number(bar.get("limit_up")), _number(bar.get("limit_down"))
            if close is None or limit_up is None or limit_down is None:
                continue
            prices[symbol] = (close, limit_up, limit_down)
        for symbol in list(shares):
            if symbol not in prices:
                continue
            close, _, limit_down = prices[symbol]
            if close <= limit_down:
                continue
            fill = close * (1 - SLIPPAGE)
            held = shares.pop(symbol)
            notional = held * fill
            cash += notional - notional * (COMMISSION + STAMP)
            trades.append({"day": str(execution), "symbol": symbol, "side": "sell", "shares": held})
        equity = cash + sum(amount * prices[symbol][0] for symbol, amount in shares.items() if symbol in prices)
        for symbol, weight in targets.items():
            if symbol not in prices:
                continue
            close, limit_up, _ = prices[symbol]
            if close >= limit_up:
                continue
            fill = close * (1 + SLIPPAGE)
            lot_cost = fill * LOT * (1 + COMMISSION)
            affordable = int((equity * weight) // lot_cost) * LOT
            if affordable < LOT or cash < affordable * fill * (1 + COMMISSION):
                continue
            cost = affordable * fill
            cash -= cost + cost * COMMISSION
            shares[symbol] = shares.get(symbol, 0) + affordable
            trades.append({"day": str(execution), "symbol": symbol, "side": "buy", "shares": affordable})
    last = days[-1]
    marked = cash
    for symbol, amount in shares.items():
        price = adjusted.get(symbol, {}).get(last)
        if price is None:
            return {"execution": "simulated", "trades": trades, "research_value": None}
        marked += amount * price
    return {"execution": "simulated", "trades": trades, "research_value": marked}


def evaluate(series, days, instruments, suspended, rule=None, limit_known=False, benchmark=None):
    """Deterministic research record. `research_value` is not a net asset value."""
    rule = rule or ScreenRule()
    days = [_day(day) for day in days]
    suspended = {(_symbol, _day(day)) for _symbol, day in suspended}
    adjusted = {symbol: _adjusted(bars, days) for symbol, bars in series.items()}
    benchmark_adjusted = _adjusted(benchmark, days) if benchmark else {}
    benchmark_ready = bool(benchmark) and all(day in benchmark_adjusted for day in days)
    signals = []
    books = {name: [] for name in ("return20", "volatility20", "average_turnover20", "drawdown60", "excess_return20")}
    samples = {name: [] for name in books}
    for index, day in enumerate(days):
        if index < 20 or index + 20 >= len(days):
            continue
        forward_day = days[index + 20]
        scored = []
        for symbol, instrument in instruments.items():
            if not _listed(instrument, day) or (symbol, day) in suspended:
                continue
            values = _window_values(series.get(symbol, {}), adjusted.get(symbol, {}), days, index)
            if values is None or values["return20"] is None or values["volatility20"] is None:
                continue
            if benchmark_ready and days[index - 20] in benchmark_adjusted and day in benchmark_adjusted:
                values["excess_return20"] = values["return20"] - (
                    benchmark_adjusted[day] / benchmark_adjusted[days[index - 20]] - 1
                )
            if forward_day not in adjusted.get(symbol, {}):
                continue
            scored.append({"symbol": symbol, **values})
            forward = adjusted[symbol][forward_day] / adjusted[symbol][day] - 1
            for name in ("return20", "volatility20", "average_turnover20", "drawdown60"):
                if values.get(name) is not None:
                    samples[name].append({"symbol": symbol, "factor": values[name], "forward": forward})
            if values.get("excess_return20") is not None:
                samples["excess_return20"].append(
                    {"symbol": symbol, "factor": values["excess_return20"], "forward": forward}
                )
        if not scored:
            continue
        frame = pd.DataFrame(scored)
        frame["score"] = rule.momentum_weight * frame.return20.rank(pct=True) + (1 - rule.momentum_weight) * (
            1 - frame.volatility20.rank(pct=True)
        )
        weights, ordered = _weights(frame.to_dict("records"), rule)
        for name, column, reverse in (
            ("return20", "return20", True),
            ("volatility20", "volatility20", False),
            ("average_turnover20", "average_turnover20", True),
            ("drawdown60", "drawdown60", True),
            ("excess_return20", "excess_return20", True),
        ):
            usable = [row for row in scored if row.get(column) is not None]
            if not usable:
                continue
            usable.sort(key=lambda row: row[column], reverse=reverse)
            count = max(1, len(usable) // 5)
            books[name].append({row["symbol"]: 1 / count for row in usable[:count]})
        signals.append(
            {
                "day": day,
                "weights": weights,
                "ordered": ordered,
                "execution_bars": {symbol: series.get(symbol, {}).get(days[index + 1], {}) for symbol in weights},
            }
        )
    last = signals[-1]["ordered"] if signals else []
    candidates = [
        {
            "symbol": row["symbol"],
            "score": row["score"],
            "return20": row["return20"],
            "volatility20": row["volatility20"],
        }
        for row in last
    ]
    path = _simulate(signals, adjusted, days, limit_known)
    payload = {
        "research_version": RESEARCH_VERSION,
        "status": "complete" if signals else "insufficient_history",
        "limit_check": "applied" if limit_known else "unknown",
        "execution": path["execution"],
        "candidates": candidates,
        "factors": {name: _factor_report(samples[name], books[name]) for name in samples},
        "benchmark_status": "available" if benchmark_ready else "unavailable",
        "trades": path["trades"],
        "research_value": path["research_value"],
        "label": "Research path value, not NAV",
        "costs": {"commission": COMMISSION, "stamp_duty": STAMP, "slippage": SLIPPAGE, "lot": LOT},
        "automatic_orders": False,
    }
    payload["research_hash"] = digest({key: value for key, value in payload.items() if key != "research_hash"})
    return payload
