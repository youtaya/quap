"""Public-market data boundary: tokenless HTTPS sources, explicit units, no synthetic fallback.

Capability identifiers (``stock_basic``, ``trade_cal``, ``daily`` ...) are stable internal names shared
with the ``capabilities``/``datasets`` tables. Rows returned by ``request`` follow one provider-neutral
contract regardless of the upstream site that served them:

- ``stock_basic``: code, name, exchange (SSE/SZSE), list_status, list_date (date or None), delist_date
- ``trade_cal``: exchange, day, is_open, inferred
- ``daily``/``index_daily``: code, day, open, high, low, close, pre_close, volume (shares), turnover (CNY or None)
- ``adj_factor``: code, day, factor, source
- ``suspend_d``: code, day, suspend_type ("S"), derived
- ``rt_k``: code, name, open, high, low, close, pre_close, volume (shares), turnover (CNY), trade_time
- ``stk_mins``/``rt_min``/``rt_min_daily``: code, time, open, high, low, close, volume (shares), turnover (CNY or None)
- ``namechange``: code, name, start_date, end_date, ann_date, change_reason
- ``stk_limit``: code, day, up_limit, down_limit, derived
"""

from datetime import date, datetime
from typing import Protocol

from quant_platform.domain import number

PROVIDER = "public-market"

CAPABILITIES = {
    "stock_basic",
    "trade_cal",
    "daily",
    "index_daily",
    "adj_factor",
    "suspend_d",
    "rt_k",
    "stk_mins",
    "rt_min",
    "rt_min_daily",
    "namechange",
    "stk_limit",
}
MINUTE_ENDPOINTS = {"stk_mins", "rt_min", "rt_min_daily"}


class ProviderError(RuntimeError):
    pass


class PermissionDenied(ProviderError):
    pass


class Deferred(ProviderError):
    def __init__(self, message, seconds=60):
        super().__init__(message)
        self.seconds = seconds


class MarketProvider(Protocol):
    def request(self, endpoint: str, params: dict, probe: bool = False) -> list[dict]: ...


def parse_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text[:8], fmt).date()
        except ValueError:
            pass
    raise ProviderError("Invalid provider trading date.")


def prices(row, strict=True):
    values = {key: number(row.get(key)) for key in ("open", "high", "low", "close", "pre_close")}
    valid = all(values[k] is not None and values[k] > 0 for k in ("open", "high", "low", "close"))
    valid = (
        valid
        and values["low"]
        <= min(values["open"], values["close"])
        <= max(values["open"], values["close"])
        <= values["high"]
    )
    if strict and not valid:
        raise ProviderError("Invalid historical OHLC.")
    if not valid:
        values = {key: None for key in values}
    return values


def positive(value, multiplier=1):
    value = number(value)
    return value * multiplier if value is not None and value >= 0 else None
