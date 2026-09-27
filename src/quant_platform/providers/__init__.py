"""Public-market data boundary: tokenless HTTPS sources, explicit units, no synthetic fallback.

Capability identifiers are stable internal names shared with the ``capabilities``/``datasets`` tables.
They describe a *capability*, not a vendor endpoint; :mod:`quant_platform.providers.market` serves each
one from the first reachable source in the go-stock-style A-share degradation order
(通达信 → 东方财富 → 新浪 → 腾讯) and records the source that actually answered.

Provider-neutral contracts:

- ``securities``: symbol, name, exchange (SSE/SZSE), board, list_status, list_date, delist_date
- ``calendar``: exchange, day, is_open
- ``history``: symbol, day, open, high, low, close, pre_close, volume (shares), turnover (CNY or None)
- ``factors``: symbol, day, factor (qfq / raw), source
- ``benchmark``: symbol, day, open, high, low, close, pre_close, volume (shares), turnover (CNY or None)
- ``quotes``: symbol, name, open, high, low, close, pre_close, volume (shares), turnover (CNY), source_time
- ``minutes``: symbol, bar_end, bar_start, available_at, finalized, OHLC, volume (shares), amount (CNY)
- ``constraints``: symbol, day, limit_up, limit_down, suspended (derived from stored closes, never
  fetched). ``suspended`` is ``None`` — no tokenless source publishes suspensions, and reporting
  ``False`` would turn "unknown" into "confirmed tradeable".
- ``security_history``: symbol, name, start, end, risk_warning, observed (not announced)
"""

from quant_platform.domain import number

# go-stock A-share degradation order; each source is tried before the next.
SOURCES = ("pytdx", "eastmoney", "sina", "tencent")


class ProviderError(RuntimeError):
    pass


class PermissionDenied(ProviderError):
    pass


class Deferred(ProviderError):
    def __init__(self, message, seconds=60):
        super().__init__(message)
        self.seconds = seconds


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
