# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Optional, read-only historical context through Qlib's public data API."""

from __future__ import annotations

from pathlib import Path
from threading import RLock

import pandas as pd

from .market import TradingCalendar
from .models import Basket, normalize_symbol


class QlibHistory:
    """Initialize one local Qlib dataset and expose dated history and universes.

    Qlib's providers are process-global. Switching datasets requires restarting
    the dashboard; this avoids silently changing another session's provider.
    """

    _lock = RLock()
    _uri = None

    def __init__(self, provider_uri=None, data_api=None):
        self.api = data_api
        if data_api is None:
            path = Path(provider_uri or "").expanduser().resolve()
            if not provider_uri or not (path / "calendars" / "day.txt").is_file():
                raise ValueError("请选择包含 calendars/day.txt 的本地 Qlib 数据集。")
            import qlib  # pylint: disable=C0415
            from qlib.config import C  # pylint: disable=C0415
            from qlib.constant import REG_CN  # pylint: disable=C0415
            from qlib.data import D  # pylint: disable=C0415

            with self._lock:
                if self._uri is not None and self._uri != str(path):
                    raise ValueError("切换进程级 Qlib 数据集需要重启看板。")
                if self._uri is None:
                    if C.registered:
                        raise ValueError("Qlib 已在外部初始化，请在独立进程中运行本看板。")
                    qlib.init(
                        provider_uri=str(path),
                        region=REG_CN,
                        kernels=1,
                        expression_cache=None,
                        dataset_cache=None,
                        default_disk_cache=0,
                    )
                    type(self)._uri = str(path)
                self.api = D
        days = pd.DatetimeIndex(self.api.calendar(freq="day"))
        if days.empty:
            raise ValueError("该 Qlib 数据集没有日频交易日历。")
        self.calendar = TradingCalendar(timestamp.date() for timestamp in days)
        self.end_date = days.max().date()
        self._members = {}

    def resolve(self, baskets, day):
        """Resolve universe membership as of day without silently using a stale date."""
        result = []
        for basket in baskets:
            if not basket.universe:
                result.append(basket)
                continue
            if not self.calendar.first <= day <= self.calendar.last:
                raise ValueError(f"Qlib 日历不覆盖 {day}，无法解析 {basket.name} 的成分。")
            key = (basket.universe, day)
            if key not in self._members:
                instruments = self.api.instruments(market=basket.universe)
                symbols = self.api.list_instruments(
                    instruments, start_time=str(day), end_time=str(day), freq="day", as_list=True
                )
                members = tuple((normalize_symbol(symbol), 1.0) for symbol in symbols)
                if not members:
                    raise ValueError(f"Qlib 宇宙 {basket.universe} 在 {day} 没有成分。")
                self._members[key] = members
            result.append(Basket(basket.name, self._members[key]))
        return tuple(result)

    def features(self, symbol, lookback=90):
        """Return historical close/volume without mixing them with unadjusted live quotes."""
        symbol = normalize_symbol(symbol)
        end = pd.Timestamp(self.end_date)
        data = self.api.features(
            [symbol],
            ["$close", "$volume"],
            start_time=end - pd.Timedelta(days=lookback),
            end_time=end,
            freq="day",
            disk_cache=0,
        )
        if data.empty or data["$close"].dropna().empty:
            raise ValueError(f"该 Qlib 数据集没有 {symbol} 的历史收盘价。")
        return data
