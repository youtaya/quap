# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""UI-independent contracts for public A-share monitoring."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
BOARD_NAMES = ("STAR Market", "ChiNext", "Main Board")


def shanghai_now() -> datetime:
    """Return an aware China Standard Time timestamp."""
    return datetime.now(SHANGHAI)


def number(value) -> Optional[float]:
    """Convert a finite numeric field without replacing missing values with zero."""
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def classify_board(symbol: str) -> str:
    """Classify supported exchange-qualified A-share/STAR CDR symbols."""
    exchange, prefix = symbol[:2], symbol[2:5]
    if exchange == "SH" and prefix in {"688", "689"}:
        return "STAR Market"
    if exchange == "SZ" and prefix in {"300", "301"}:
        return "ChiNext"
    if (exchange == "SH" and prefix in {"600", "601", "603", "605"}) or (
        exchange == "SZ" and prefix in {"000", "001", "002", "003"}
    ):
        return "Main Board"
    raise ValueError(f"Unsupported A-share symbol: {symbol}")


def normalize_symbol(value: str) -> str:
    """Normalize SH600000, 600000.SH, and quoted six-digit stock codes."""
    if not isinstance(value, str):
        raise ValueError("Symbols must be strings; quote leading-zero codes in YAML.")
    symbol = value.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ)", symbol):
        symbol = symbol[-2:] + symbol[:6]
    elif re.fullmatch(r"\d{6}", symbol):
        symbol = ("SH" if symbol.startswith("6") else "SZ") + symbol
    if not re.fullmatch(r"(SH|SZ)\d{6}", symbol):
        raise ValueError(f"Invalid symbol: {value}")
    classify_board(symbol)
    return symbol


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    board: str
    last: Optional[float]
    previous_close: Optional[float]
    volume: Optional[float]
    turnover: Optional[float]
    timestamp: Optional[datetime]
    received_at: datetime

    @property
    def daily_return(self) -> Optional[float]:
        last, previous = number(self.last), number(self.previous_close)
        if last is None or previous is None or last <= 0 or previous <= 0:
            return None
        return number(last / previous - 1)

    def fresh(self, now: datetime, max_age: float = 120) -> bool:
        if self.timestamp is None or self.timestamp.tzinfo is None:
            return False
        stamp = self.timestamp.astimezone(SHANGHAI)
        local = now.astimezone(SHANGHAI)
        return stamp.date() == local.date() and -5 <= (local - stamp).total_seconds() <= max_age


@dataclass(frozen=True)
class Snapshot:
    quotes: Tuple[Quote, ...]
    source: str
    mode: str
    fetched_at: datetime
    reported_total: int
    excluded: int = 0
    warnings: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Basket:
    name: str
    members: Tuple[Tuple[str, float], ...]
    universe: Optional[str] = None

    def __post_init__(self):
        if not self.name or not self.name.strip():
            raise ValueError("Basket name must not be empty.")
        if bool(self.members) == bool(self.universe):
            raise ValueError("Specify either nonempty members or a Qlib universe, not both.")
        normalized = tuple((normalize_symbol(symbol), number(weight)) for symbol, weight in self.members)
        if len({symbol for symbol, _ in normalized}) != len(normalized):
            raise ValueError(f"Duplicate symbols in basket {self.name}.")
        if any(weight is None or weight <= 0 for _, weight in normalized):
            raise ValueError("Basket weights must be positive and finite.")
        if normalized and number(sum(weight for _, weight in normalized)) is None:
            raise ValueError("Basket weight sum must be finite.")
        if self.universe is not None and (not isinstance(self.universe, str) or not self.universe.strip()):
            raise ValueError("Qlib universe must be a nonempty string.")
        object.__setattr__(self, "members", normalized)


@dataclass(frozen=True)
class AlertSettings:
    stock_change: float = 0.03
    basket_change: float = 0.01
    turnover: Optional[float] = None
    cooldown: float = 300
    minimum_coverage: float = 0.95

    def __post_init__(self):
        values = [self.stock_change, self.basket_change, self.cooldown]
        if self.turnover is not None:
            values.append(self.turnover)
        if any(number(value) is None or value <= 0 for value in values):
            raise ValueError("Alert thresholds and cooldown must be positive finite numbers.")
        if number(self.minimum_coverage) is None or not 0 < self.minimum_coverage <= 1:
            raise ValueError("Minimum coverage must be in (0, 1].")


@dataclass
class Summary:
    name: str
    total: int
    eligible: int
    coverage: float
    daily_return: Optional[float]
    volume: Optional[float]
    turnover: Optional[float]
    advancing: int = 0
    declining: int = 0
    unchanged: int = 0
    observation: str = ""
    turnover_coverage: float = 0.0

    @property
    def proxy(self) -> Optional[float]:
        return None if self.daily_return is None else 1000 * (1 + self.daily_return)


@dataclass
class PollResult:
    snapshot: Optional[Snapshot] = None
    summaries: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    error: Optional[str] = None
    storage_error: Optional[str] = None
    notify_error: Optional[str] = None
    duration: float = 0.0
    session: str = "Not started"
    calendar_status: str = "unverified"
