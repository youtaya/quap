# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Session rules, basket configuration, and daily-reference market proxies."""

from __future__ import annotations

import hashlib
import json
from datetime import time

from .models import BOARD_NAMES, SHANGHAI, Basket, Summary, number


def fingerprint(value) -> str:
    """Return a stable identity for a configuration or source observation."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def quote_observation(quote) -> str:
    return fingerprint((quote.timestamp, quote.last, quote.previous_close, quote.volume, quote.turnover))


def parse_baskets(config) -> tuple:
    """Parse named baskets with symbols/weights or a Qlib universe from safe YAML."""
    if not isinstance(config, dict):
        raise ValueError("篮子必须是名称到配置的映射。")
    baskets = []
    reserved = set(BOARD_NAMES) | {"Shanghai Main Board", "Shenzhen Main Board"}
    for name, spec in config.items():
        if not isinstance(name, str) or name in reserved or not isinstance(spec, dict):
            raise ValueError("篮子名称必须是非空字符串，且不能与内置板块重名。")
        if set(spec) - {"symbols", "weights", "universe"}:
            raise ValueError(f"{name} 含有未知篮子字段。")
        if "universe" in spec:
            if set(spec) != {"universe"}:
                raise ValueError("Qlib 宇宙不能同时指定代码或权重。")
            baskets.append(Basket(name, (), spec["universe"]))
            continue
        symbols = spec.get("symbols", [])
        if not isinstance(symbols, list):
            raise ValueError("篮子代码必须是带引号的 YAML 字符串列表。")
        weights = spec.get("weights", [1.0] * len(symbols))
        if not isinstance(weights, list) or len(weights) != len(symbols):
            raise ValueError("权重必须是与代码数量一致的列表。")
        baskets.append(Basket(name, tuple(zip(symbols, weights))))
    return tuple(baskets)


class TradingCalendar:
    """Only infer holidays inside the supplied calendar's date coverage."""

    def __init__(self, days=()):
        self.days = frozenset(days)
        self.first = min(self.days) if self.days else None
        self.last = max(self.days) if self.days else None

    def session(self, now):
        local = now.astimezone(SHANGHAI)
        day, at = local.date(), local.time()
        covered = self.first is not None and self.first <= day <= self.last
        status = "verified" if covered else "unverified"
        if day.weekday() >= 5 or (covered and day not in self.days):
            return "Closed", status, False
        if at < time(9, 30):
            return "Pre-open", status, False
        if at <= time(11, 30):
            return "Morning session", status, True
        if at < time(13):
            return "Lunch break", status, False
        if at <= time(15):
            return "Afternoon session", status, True
        return "Closed", status, False


def summarize(name, members, quotes, now, require_fresh=True) -> Summary:
    """Compute a daily-reference proxy, renormalizing only explicitly eligible weights.

    Closed-market display may use the dated final snapshot; alert evaluation always
    uses fresh quotes. Volume/turnover are snapshot totals, never sums across polls.
    """
    weights = dict(members)
    denominator = sum(weights.values())
    valid = [
        (q, weights[symbol])
        for symbol, q in quotes.items()
        if symbol in weights and q.daily_return is not None and (not require_fresh or q.fresh(now))
    ]
    eligible_weight = sum(weight for _, weight in valid)
    returns = [q.daily_return for q, _ in valid]
    volumes = [q.volume for q, _ in valid if number(q.volume) is not None and q.volume >= 0]
    turnovers = [q.turnover for q, _ in valid if number(q.turnover) is not None and q.turnover >= 0]
    turnover_weight = sum(w for q, w in valid if number(q.turnover) is not None and q.turnover >= 0)
    return Summary(
        name=name,
        total=len(weights),
        eligible=len(valid),
        coverage=eligible_weight / denominator if denominator else 0,
        daily_return=sum(q.daily_return * w / eligible_weight for q, w in valid) if eligible_weight else None,
        volume=sum(volumes) if volumes else None,
        turnover=sum(turnovers) if turnovers else None,
        advancing=sum(r > 1e-10 for r in returns),
        declining=sum(r < -1e-10 for r in returns),
        unchanged=sum(abs(r) <= 1e-10 for r in returns),
        observation=fingerprint(sorted((q.symbol, quote_observation(q), w) for q, w in valid)),
        turnover_coverage=turnover_weight / denominator if denominator else 0,
    )


def market_members(quotes, baskets):
    """Return complete discovered board memberships and validated custom baskets."""
    groups = {name: tuple((q.symbol, 1.0) for q in quotes.values() if q.board == name) for name in BOARD_NAMES}
    for exchange, name in (("SH", "Shanghai Main Board"), ("SZ", "Shenzhen Main Board")):
        groups[name] = tuple(
            (q.symbol, 1.0) for q in quotes.values() if q.board == "Main Board" and q.symbol.startswith(exchange)
        )
    for basket in baskets:
        if basket.universe:
            raise ValueError(f"计算指标前请先解析 {basket.name} 的 Qlib 宇宙。")
        if basket.name in groups:
            raise ValueError(f"篮子名称重复或与内置板块冲突：{basket.name}")
        groups[basket.name] = basket.members
    return groups
