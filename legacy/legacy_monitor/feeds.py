# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Bounded public directory, quote, and daily-history transports."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime

import requests

from .models import SHANGHAI, Quote, Snapshot, classify_board, normalize_symbol, number, shanghai_now
from .providers import FeedError, QuoteProvider


class PublicHTTP:
    """Sequential bounded transport with injectable time and network dependencies."""

    def __init__(self, session=None, gap=0.2, monotonic=time.monotonic, sleep=time.sleep):
        self.session = session or requests.Session()
        self.gap, self.monotonic, self.sleep = gap, monotonic, sleep
        self.last_request = None

    def get(self, url, params, deadline, encoding="utf-8", parser=None):
        for attempt in range(2):
            if self.last_request is not None:
                delay = max(0, self.last_request + self.gap - self.monotonic())
                if self.monotonic() + delay >= deadline:
                    raise FeedError("Public-feed collection deadline exceeded.")
                self.sleep(delay)
            remaining = deadline - self.monotonic()
            if remaining <= 0.1:
                raise FeedError("Public-feed collection deadline exceeded.")
            self.last_request = self.monotonic()
            try:
                with self.session.get(
                    url,
                    params=params,
                    timeout=(min(3, remaining / 2), min(8, remaining / 2)),
                    headers={"User-Agent": "Qlib-Market-Monitor/1.0"},
                    stream=True,
                ) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_content(chunk_size=16384):
                        size += len(chunk)
                        if size > 2 * 1024 * 1024 or self.monotonic() >= deadline:
                            raise FeedError("Public response exceeds time or size limit.")
                        chunks.append(chunk)
                    text = b"".join(chunks).decode(encoding)
                    return parser(text) if parser else text
            except (requests.RequestException, ValueError) as exc:
                if attempt:
                    raise FeedError(f"Public request failed: {type(exc).__name__}") from exc
                self.sleep(1)
        raise FeedError("Public request failed.")

    def json(self, url, params, deadline, validator=None):
        def parse(text):
            value = json.loads(text)
            if validator is not None:
                validator(value)
            return value

        return self.get(url, params, deadline, parser=parse)

    def close(self):
        self.session.close()


class SinaDirectory:
    """Discover a complete supported directory; quote times here are not used."""

    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center."

    def __init__(self, http=None, clock=shanghai_now):
        self.http, self.clock = http or PublicHTTP(gap=2.0), clock
        self._pending = {}
        self._started = None

    def fetch(self):
        now = self.clock()
        if self._started is None or self._started.date() != now.date() or (now - self._started).total_seconds() > 7200:
            self._pending = {}
            self._started = now
        end = self.http.monotonic() + 180
        merged, totals = {}, {}
        for node in ("hs_a", "kcb"):
            total = self._count(node, end)
            checkpoint = self._pending.get(node)
            if checkpoint is None or checkpoint["total"] != total:
                checkpoint = {"total": total, "found": {}, "next_page": 1}
                self._pending[node] = checkpoint
            found = checkpoint["found"]
            for page in range(checkpoint["next_page"], 201):
                if len(found) == total:
                    break
                try:
                    rows = self.http.json(
                        self.url + "getHQNodeData",
                        {"node": node, "page": page, "num": 100, "sort": "symbol", "asc": 1},
                        end,
                        validator=self._nonempty_page,
                    )
                except FeedError as exc:
                    raise FeedError(f"Directory {node} page {page} failed at {len(found)}/{total}: {exc}") from exc
                if not isinstance(rows, list) or not rows:
                    raise FeedError(f"Directory {node} page {page} ended at {len(found)}/{total} records.")
                previous = len(found)
                candidate = dict(found)
                for row in rows:
                    if not isinstance(row, dict) or not re.fullmatch(
                        r"(?:sh|sz|bj)[0-9]{6}", str(row.get("symbol", ""))
                    ):
                        raise FeedError("Invalid directory security identity.")
                    if row.get("code") != row["symbol"][2:]:
                        raise FeedError("Inconsistent directory security code.")
                    candidate[row["symbol"]] = str(row.get("name") or row["symbol"])
                if len(candidate) > total or len(candidate) == previous:
                    self._pending.pop(node, None)
                    raise FeedError("Directory pagination duplicates or overruns the reported total.")
                found = candidate
                checkpoint.update(found=found, next_page=page + 1)
                if len(found) == total:
                    break
            else:
                raise FeedError("Directory page limit exceeded.")
            if self._count(node, end) != total:
                self._pending.pop(node, None)
                raise FeedError("Directory changed during pagination; retry later.")
            merged.update(found)
            totals[node] = total
        symbols = {}
        for code, name in merged.items():
            try:
                symbols[normalize_symbol(code)] = name
            except ValueError:
                continue
        if not symbols:
            raise FeedError("No supported Shanghai/Shenzhen securities discovered.")
        self._pending = {}
        started = self._started
        self._started = None
        return {
            "started_at": started.isoformat(),
            "symbols": dict(sorted(symbols.items())),
            "fetched_at": self.clock().isoformat(),
            "totals": totals,
            "excluded": len(merged) - len(symbols),
        }

    @staticmethod
    def _nonempty_page(value):
        if not isinstance(value, list) or not value:
            raise ValueError("Incomplete directory page.")

    def _count(self, node, end):
        value = self.http.json(self.url + "getHQNodeStockCount", {"node": node}, end)
        if isinstance(value, bool) or not re.fullmatch(r"[0-9]{1,5}", str(value)) or not 0 < int(value) <= 20000:
            raise FeedError("Invalid directory count.")
        return int(value)


def volume_shares(symbol, value):
    """Tencent STAR volumes are shares; other supported A-share volumes are lots."""
    value = number(value)
    if value is None or value < 0:
        return None
    return number(value * (1 if classify_board(symbol) == "STAR Market" else 100))


def parse_tencent(symbol, text, received_at, name=None):
    symbol = normalize_symbol(symbol)
    board = classify_board(symbol)
    if not text:
        return Quote(symbol, name or symbol, board, None, None, None, None, None, received_at)
    fields = text.split("~")
    if len(fields) < 38 or fields[2] != symbol[2:] or fields[0] != ("1" if symbol.startswith("SH") else "51"):
        raise FeedError(f"Malformed Tencent quote for {symbol}.")
    last, previous, percent = number(fields[3]), number(fields[4]), number(fields[32])
    if last is not None and previous is not None and previous > 0 and percent is not None:
        if abs((last / previous - 1) * 100 - percent) > 0.05:
            last = None
    stamp = None
    if re.fullmatch(r"[0-9]{14}", fields[30]):
        try:
            stamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=SHANGHAI)
        except ValueError:
            pass
    turnover = number(fields[37])
    turnover = number(turnover * 10000) if turnover is not None and turnover >= 0 else None
    return Quote(
        symbol,
        fields[1] or name or symbol,
        board,
        last,
        previous,
        volume_shares(symbol, fields[6]),
        turnover,
        stamp,
        received_at,
    )


class TencentProvider(QuoteProvider):
    """Fetch complete batches against an explicitly dated security directory."""

    source = "Tencent public HTTPS"
    url = "https://qt.gtimg.cn/q="
    pattern = re.compile(r'v_((?:sh|sz)[0-9]{6})="([^"\r\n]*)";')

    def __init__(self, symbols, http=None, clock=shanghai_now, deadline=90):
        self.symbols = dict(symbols)
        if not self.symbols or len(self.symbols) > 20000:
            raise ValueError("A nonempty supported directory is required.")
        for symbol in self.symbols:
            if normalize_symbol(symbol) != symbol:
                raise ValueError("Directory symbols must be normalized.")
        self.http, self.clock, self.deadline = http or PublicHTTP(), clock, deadline
        self.session = self.http.session
        self.directory_fresh = True

    def fetch(self):
        end = self.http.monotonic() + self.deadline
        codes, quotes = sorted(self.symbols), []
        for start in range(0, len(codes), 80):
            batch = codes[start : start + 80]
            body = self.http.get(self.url + ",".join(code.lower() for code in batch), {}, end, "gb18030")
            matches = self.pattern.findall(body)
            if self.pattern.sub("", body).strip():
                raise FeedError("Malformed Tencent batch response.")
            identities = [code.upper() for code, _ in matches]
            if len(identities) != len(set(identities)) or set(identities) != set(batch):
                raise FeedError("Incomplete, duplicated, or unexpected Tencent batch identities.")
            now = self.clock()
            quotes.extend(parse_tencent(code.upper(), text, now, self.symbols[code.upper()]) for code, text in matches)
        warnings = []
        if not self.directory_fresh:
            warnings.append("Directory is older than 48 hours; board/basket alerts are disabled.")
        missing = sum(q.daily_return is None or q.timestamp is None for q in quotes)
        if missing:
            warnings.append(f"{missing} securities have missing/invalid prices or source timestamps.")
        return Snapshot(
            tuple(sorted(quotes, key=lambda q: q.symbol)),
            self.source,
            self.mode,
            self.clock(),
            len(codes),
            warnings=tuple(warnings),
        )


class TencentDaily:
    """Download whole recent adjusted windows; never append across adjustment bases."""

    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def __init__(self, http=None):
        self.http = http or PublicHTTP(gap=1.0)

    def fetch(self, symbol, cutoff, bars=500, benchmark=False):
        code = symbol.lower()
        if benchmark:
            if code not in {"sh000001", "sz399001"}:
                raise ValueError("Unsupported calendar benchmark.")
        else:
            normalize_symbol(symbol)
        data = self.http.json(
            self.url, {"param": f"{code},day,,,{min(bars + 10, 640)},qfq"}, self.http.monotonic() + 30
        )
        if not isinstance(data, dict) or data.get("code") != 0:
            raise FeedError(f"Daily-history request rejected for {symbol}.")
        content = data.get("data")
        entry = content.get(code, {}) if isinstance(content, dict) else {}
        rows = entry.get("qfqday", entry.get("day")) if isinstance(entry, dict) else None
        if not isinstance(rows, list) or len(rows) > 1000:
            raise FeedError(f"Daily history unavailable for {symbol}.")
        result, seen = [], set()
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                raise FeedError(f"Invalid daily row for {symbol}.")
            try:
                day = datetime.strptime(row[0], "%Y-%m-%d").date()
            except (TypeError, ValueError) as exc:
                raise FeedError("Invalid daily date.") from exc
            if day in seen:
                raise FeedError("Duplicate daily date.")
            seen.add(day)
            if day > cutoff:
                continue
            opening, close, high, low = [number(value) for value in row[1:5]]
            volume = number(row[5]) if benchmark else volume_shares(symbol, row[5])
            if any(value is None or value <= 0 for value in (opening, close, high, low)):
                raise FeedError(f"Invalid daily price for {symbol}.")
            if low > min(opening, close) or high < max(opening, close) or high < low:
                raise FeedError(f"Inconsistent daily OHLC for {symbol}.")
            if volume is not None and volume < 0:
                volume = None
            result.append([day.isoformat(), opening, close, high, low, volume])
        return sorted(result)[-bars:]
