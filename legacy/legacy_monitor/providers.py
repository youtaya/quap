# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Bounded public-feed polling and an explicitly simulated quote provider."""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta

import requests

from .models import Quote, SHANGHAI, Snapshot, classify_board, normalize_symbol, number, shanghai_now


class FeedError(RuntimeError):
    """An incomplete, unavailable, or invalid public-feed response."""


class QuoteProvider(ABC):
    mode = "live"
    source = "custom"

    @abstractmethod
    def fetch(self) -> Snapshot:
        """Return a complete snapshot or raise FeedError, never a silent partial result."""
        raise NotImplementedError


def security_identity(row) -> tuple:
    """Return a unique (exchange, code) key or raise FeedError for malformed rows."""
    code = row.get("f12") if isinstance(row, dict) else None
    market = row.get("f13") if isinstance(row, dict) else None
    if not isinstance(code, str) or len(code) != 6 or not code.isascii() or not code.isdigit():
        raise FeedError("证券标识不完整。")
    if not isinstance(market, int) or isinstance(market, bool):
        raise FeedError("证券标识不完整。")
    return str(market), code


def parse_quote(row: dict, received_at: datetime) -> Quote:
    """Parse Eastmoney fields with fltt=2: CNY prices, lots, CNY turnover, epoch time."""
    market = row.get("f13")
    if not isinstance(market, int) or isinstance(market, bool):
        raise ValueError("Invalid exchange identifier")
    exchange = {0: "SZ", 1: "SH"}.get(market)
    if exchange is None:
        raise ValueError("Unsupported exchange")
    symbol = normalize_symbol(exchange + str(row.get("f12", "")))
    last, previous = number(row.get("f2")), number(row.get("f18"))
    # Cross-check the percentage field to reject changed/mixed price scaling.
    change = number(row.get("f3"))
    if last is not None and previous is not None and previous > 0 and change is not None:
        if abs((last / previous - 1) * 100 - change) > 0.05:
            last = None
    volume, turnover = number(row.get("f5")), number(row.get("f6"))
    epoch = number(row.get("f124"))
    timestamp = None
    if epoch is not None and epoch > 0:
        try:
            timestamp = datetime.fromtimestamp(epoch, SHANGHAI)
        except (ValueError, OverflowError, OSError):
            pass
    return Quote(
        symbol=symbol,
        name=str(row.get("f14") or symbol),
        board=classify_board(symbol),
        last=last,
        previous_close=previous,
        volume=number(volume * 100) if volume is not None and volume >= 0 else None,
        turnover=turnover if turnover is not None and turnover >= 0 else None,
        timestamp=timestamp,
        received_at=received_at,
    )


class EastmoneyProvider(QuoteProvider):
    """Poll public A-share snapshots; no exchange-grade availability guarantee.

    Parameters
    ----------
    session : requests.Session, optional
        Injectable HTTP transport. Responses are bounded to 2 MiB per page.
    deadline : float
        Collection budget in seconds. A currently blocked socket read can exceed
        the budget by at most its configured read timeout (normally 8 seconds).
    """

    source = "东方财富公开 HTTPS"
    url = "https://82.push2.eastmoney.com/api/qt/clist/get"

    def __init__(self, session=None, deadline=90.0, clock=shanghai_now, monotonic=time.monotonic, sleep=time.sleep):
        if number(deadline) is None or deadline <= 0:
            raise ValueError("采集时限必须为正数。")
        self.session = session or requests.Session()
        self.deadline = deadline
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep

    def _request(self, params, end):
        for attempt in range(2):
            remaining = end - self.monotonic()
            if remaining <= 0.1:
                raise FeedError("快照采集超过时限。")
            try:
                with self.session.get(
                    self.url,
                    params=params,
                    timeout=(min(3, remaining / 2), min(8, remaining / 2)),
                    headers={"User-Agent": "Qlib-Market-Monitor/1.0", "Referer": "https://quote.eastmoney.com/"},
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=16384):
                        size += len(chunk)
                        if self.monotonic() >= end or size > 2 * 1024 * 1024:
                            raise FeedError("响应超过时限或体积上限。")
                        chunks.append(chunk)
                    return json.loads(b"".join(chunks))
            except (requests.RequestException, ValueError) as exc:
                if attempt or self.monotonic() + 1 >= end:
                    raise FeedError(f"公开行情请求失败：{type(exc).__name__}") from exc
                self.sleep(1)
        raise FeedError("公开行情请求失败。")

    def fetch(self) -> Snapshot:
        end = self.monotonic() + self.deadline
        params = {
            "pn": 1,
            "pz": 100,
            "po": 0,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f12",
            "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
            "fields": "f2,f3,f5,f6,f12,f13,f14,f18,f124",
        }
        raw, total = {}, None
        for page in range(1, 201):
            params["pn"] = page
            payload = self._request(params, end)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("diff"), (list, dict)):
                raise FeedError(f"第 {page} 页快照无效或不完整。")
            page_total = data.get("total")
            if not isinstance(page_total, int) or isinstance(page_total, bool) or not 0 < page_total <= 20000:
                raise FeedError("市场成分总数无效。")
            if total is not None and total != page_total:
                raise FeedError("分页过程中成分总数发生变化，请下一轮重试。")
            total = page_total
            rows = list(data["diff"].values()) if isinstance(data["diff"], dict) else data["diff"]
            if not rows:
                raise FeedError("分页在达到申报总数前结束。")
            previous_count = len(raw)
            for row in rows:
                raw[security_identity(row)] = (row, self.clock())
            if len(raw) == previous_count:
                raise FeedError("检测到重复分页。")
            if len(raw) > total:
                raise FeedError("快照数量超过申报的成分总数。")
            if len(raw) == total:
                break
            if self.monotonic() + 0.2 >= end:
                raise FeedError("快照采集超过时限。")
            self.sleep(0.2)
        else:
            raise FeedError("达到分页上限仍未完成快照。")
        if self.monotonic() >= end:
            raise FeedError("快照采集超过时限。")
        quotes, excluded = [], 0
        for row, received in raw.values():
            try:
                quotes.append(parse_quote(row, received))
            except ValueError:
                excluded += 1
        if not quotes:
            raise FeedError("未返回受支持的沪深 A 股证券。")
        invalid = sum(q.daily_return is None for q in quotes)
        missing_time = sum(q.timestamp is None for q in quotes)
        warnings = []
        if excluded:
            warnings.append(f"已排除 {excluded} 条不受支持的交易所/代码前缀记录。")
        if invalid:
            warnings.append(f"{invalid} 只证券价格字段无效、缺失或涨跌幅口径不一致。")
        if missing_time:
            warnings.append(f"{missing_time} 只证券缺少有效行情时间，无法触发告警。")
        return Snapshot(
            tuple(sorted(quotes, key=lambda q: q.symbol)),
            self.source,
            self.mode,
            self.clock(),
            total,
            excluded,
            tuple(warnings),
        )


class DemoProvider(QuoteProvider):
    """Small deterministic sample, not whole-board coverage or real market prices."""

    mode = "demo"
    source = "演示 — 合成样本证券"
    symbols = ("SH688001", "SH689009", "SZ300001", "SZ301001", "SH600000", "SH605001", "SZ000001", "SZ002001")

    def __init__(self):
        self.step = 0

    def fetch(self) -> Snapshot:
        # A virtual weekday session makes the demo useful on holidays and weekends.
        trading_day, tick = divmod(self.step, 180)
        calendar_days = trading_day + 2 * (trading_day // 5)
        now = datetime(2026, 9, 14, 10, tzinfo=SHANGHAI) + timedelta(days=calendar_days, seconds=30 * tick)
        move = (0, 0.015, 0.04, 0.04, 0, -0.04)[self.step % 6]
        quotes = []
        for i, symbol in enumerate(self.symbols):
            previous = 10.0 + i
            change = move * (1 if i % 3 else -1)
            volume = 100000.0 + (self.step % 180) * 5000 + i * 100
            quotes.append(
                Quote(
                    symbol,
                    f"演示 {symbol}",
                    classify_board(symbol),
                    previous * (1 + change),
                    previous,
                    volume,
                    volume * previous,
                    now,
                    now,
                )
            )
        self.step += 1
        return Snapshot(
            tuple(quotes),
            self.source,
            self.mode,
            now,
            len(quotes),
            warnings=("演示：合成样本篮子，不是真实全市场覆盖。",),
        )
