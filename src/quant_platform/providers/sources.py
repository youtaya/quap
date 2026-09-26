"""Tokenless public quote sites used the way go-stock does: browser-like GETs, bounded, HTTPS-only.

Each client returns provider-neutral rows (see ``quant_platform.providers``). Volume is always
converted to shares and turnover to CNY; fields a site does not publish are ``None``, never zero.
"""

import json
import random
import re
import time
from datetime import date, datetime, timedelta

import httpx

from quant_platform.domain import number, symbol
from . import Deferred, PermissionDenied, ProviderError, positive

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BYTE_LIMIT = 8 * 1024 * 1024
REQUEST_DEADLINE = 60
LOT = 100


class Transport:
    """One retrying GET helper shared by every source; redirects and proxies are rejected."""

    def __init__(self, client=None, sleep=time.sleep):
        self.client = client or httpx.Client(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False, verify=True
        )
        self.sleep = sleep

    def close(self):
        self.client.close()

    def get(self, url, params=None, referer=None):
        if not url.startswith("https://"):
            raise ProviderError("Only HTTPS sources are permitted.")
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        if referer:
            headers["Referer"] = referer
        started = time.monotonic()
        for attempt in range(3):
            try:
                with self.client.stream("GET", url, params=params, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        raise ProviderError("Source redirects are prohibited.")
                    if response.status_code in {401, 403}:
                        raise PermissionDenied("Source rejected the request (blocked or forbidden).")
                    if response.status_code == 429:
                        raise Deferred("Source rate limit; retry later.", 300)
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > BYTE_LIMIT or time.monotonic() - started > REQUEST_DEADLINE:
                            raise ProviderError("Source response exceeded the byte/time budget.")
                    return bytes(content)
            except (Deferred, PermissionDenied):
                raise
            except (httpx.HTTPError, ProviderError) as exc:
                if attempt == 2 or isinstance(exc, ProviderError) and "prohibited" in str(exc):
                    raise ProviderError(f"Source transport failed: {type(exc).__name__}") from None
                self.sleep(2**attempt + random.random())
        raise ProviderError("Unreachable request state.")


def _json(content):
    try:
        payload = json.loads(content)
    except ValueError:
        raise ProviderError("Source returned malformed JSON.") from None
    return payload


def _secid(code):
    code = symbol_or_index(code)
    return ("1." if code.startswith("SH") else "0.") + code[2:]


def symbol_or_index(code):
    """Accept A-share identities and the two index identities the platform uses."""
    if isinstance(code, str) and code.upper() in {"SH000001", "SH000300"}:
        return code.upper()
    return symbol(code)


def _lower(code):
    code = symbol_or_index(code)
    return code[:2].lower() + code[2:]


def _bar(code, day, open_, close, high, low, volume, turnover):
    return {
        "code": code,
        "day": day,
        "open": number(open_),
        "high": number(high),
        "low": number(low),
        "close": number(close),
        "pre_close": None,
        "volume": volume,
        "turnover": turnover,
    }


def fill_pre_close(rows):
    """Derive pre_close from the previous bar; the first bar keeps whatever the site supplied."""
    rows.sort(key=lambda row: row["day"])
    previous = None
    for row in rows:
        if previous is not None and row.get("pre_close") is None:
            row["pre_close"] = previous
        previous = row["close"]
    return rows


class EastMoney:
    name = "eastmoney"
    kline_url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    list_url = "https://push2.eastmoney.com/api/qt/clist/get"
    referer = "https://quote.eastmoney.com/"
    A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

    def __init__(self, transport):
        self.transport = transport

    def _kline(self, code, klt, fqt, beg, end, limit):
        params = {
            "secid": _secid(code),
            "klt": str(klt),
            "fqt": str(fqt),
            "beg": beg.strftime("%Y%m%d") if beg else "0",
            "end": end.strftime("%Y%m%d") if end else "20500101",
            "lmt": str(limit),
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        }
        payload = _json(self.transport.get(self.kline_url, params, self.referer))
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("rc") != 0 or not isinstance(data, dict) or not isinstance(data.get("klines"), list):
            raise ProviderError("Eastmoney kline envelope changed or request rejected.")
        if str(data.get("code")) != symbol_or_index(code)[2:]:
            raise ProviderError("Eastmoney kline identity mismatch.")
        rows = []
        for line in data["klines"]:
            parts = str(line).split(",")
            if len(parts) < 7:
                raise ProviderError("Eastmoney kline row shape changed.")
            rows.append(parts)
        return rows

    def daily(self, code, start, end, adjusted=False, limit=4000):
        code = symbol_or_index(code)
        rows = []
        for parts in self._kline(code, 101, 2 if adjusted else 0, start, end, limit):
            day = datetime.strptime(parts[0], "%Y-%m-%d").date()
            bar = _bar(code, day, parts[1], parts[2], parts[3], parts[4], positive(parts[5], LOT), positive(parts[6]))
            change = number(parts[9]) if len(parts) > 9 else None
            if bar["close"] is not None and change is not None:
                bar["pre_close"] = round(bar["close"] - change, 4)
            rows.append(bar)
        return fill_pre_close(rows)

    def minutes(self, code, start, end, limit=4000):
        code = symbol(code)
        rows = []
        for parts in self._kline(code, 5, 0, start, end, limit):
            rows.append(
                {
                    "code": code,
                    "time": parts[0] + ":00",
                    "open": number(parts[1]),
                    "close": number(parts[2]),
                    "high": number(parts[3]),
                    "low": number(parts[4]),
                    "volume": positive(parts[5], LOT),
                    "turnover": positive(parts[6]),
                }
            )
        return rows

    def directory_page(self, page, size=100):
        params = {
            "pn": str(page),
            "pz": str(size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": self.A_SHARE_FILTER,
            "fields": "f12,f13,f14,f26",
        }
        payload = _json(self.transport.get(self.list_url, params, self.referer))
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise ProviderError("Eastmoney list envelope changed or request rejected.")
        rows = []
        for item in data.get("diff") or []:
            try:
                code = symbol(("SH" if item.get("f13") == 1 else "SZ") + str(item.get("f12")))
            except ValueError:
                continue
            listed = item.get("f26")
            try:
                list_date = datetime.strptime(str(listed), "%Y%m%d").date() if listed not in (None, "-", "") else None
            except ValueError:
                list_date = None
            rows.append(
                {
                    "code": code,
                    "name": str(item.get("f14") or ""),
                    "exchange": "SSE" if code.startswith("SH") else "SZSE",
                    "list_status": "L",
                    "list_date": list_date,
                    "delist_date": None,
                }
            )
        return rows, int(data.get("total") or 0)


class Tencent:
    name = "tencent"
    quote_url = "https://qt.gtimg.cn/q="
    kline_url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    minute_url = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"
    referer = "https://gu.qq.com/"

    def __init__(self, transport):
        self.transport = transport

    def quotes(self, codes):
        codes = [symbol(code) for code in codes]
        content = self.transport.get(self.quote_url + ",".join(_lower(code) for code in codes), None, self.referer)
        text = content.decode("gbk", errors="replace")
        rows = []
        for match in re.finditer(r'v_(s[hz]\d{6})="([^"]*)"', text):
            fields = match.group(2).split("~")
            if len(fields) < 49:
                raise ProviderError("Tencent quote row shape changed.")
            code = symbol(match.group(1)[2:] + "." + match.group(1)[:2].upper())
            traded = fields[35].split("/")
            rows.append(
                {
                    "code": code,
                    "name": fields[1],
                    "close": number(fields[3]),
                    "pre_close": number(fields[4]),
                    "open": number(fields[5]),
                    "high": number(fields[33]),
                    "low": number(fields[34]),
                    "volume": positive(fields[6], LOT),
                    "turnover": positive(traded[2]) if len(traded) == 3 else None,
                    "trade_time": fields[30] if re.fullmatch(r"\d{14}", fields[30]) else None,
                    "limit_up": number(fields[47]),
                    "limit_down": number(fields[48]),
                }
            )
        return rows

    def daily(self, code, start, end, adjusted=False, limit=2000):
        code = symbol_or_index(code)
        kind = "hfq" if adjusted else ""
        param = ",".join(
            [_lower(code), "day", start.isoformat() if start else "", end.isoformat() if end else "", str(min(limit, 2000)), kind]
        )
        payload = _json(self.transport.get(self.kline_url, {"param": param}, self.referer))
        data = (payload.get("data") or {}).get(_lower(code)) if isinstance(payload, dict) else None
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ProviderError("Tencent kline envelope changed or request rejected.")
        series = data.get("hfqday" if adjusted else "day")
        if series is None:
            series = data.get("qfqday") if not adjusted else None
        if not isinstance(series, list):
            raise ProviderError("Tencent kline series missing.")
        rows = []
        for item in series:
            if not isinstance(item, list) or len(item) < 6:
                raise ProviderError("Tencent kline row shape changed.")
            day = datetime.strptime(item[0], "%Y-%m-%d").date()
            rows.append(_bar(code, day, item[1], item[2], item[3], item[4], positive(item[5], LOT), None))
        return fill_pre_close(rows)

    def minutes(self, code, limit=320):
        code = symbol(code)
        payload = _json(
            self.transport.get(self.minute_url, {"param": f"{_lower(code)},m5,,{min(limit, 320)}"}, self.referer)
        )
        data = (payload.get("data") or {}).get(_lower(code)) if isinstance(payload, dict) else None
        if payload.get("code") != 0 or not isinstance(data, dict) or not isinstance(data.get("m5"), list):
            raise ProviderError("Tencent minute envelope changed or request rejected.")
        rows = []
        for item in data["m5"]:
            if not isinstance(item, list) or len(item) < 6 or not re.fullmatch(r"\d{12}", str(item[0])):
                raise ProviderError("Tencent minute row shape changed.")
            stamp = datetime.strptime(item[0], "%Y%m%d%H%M")
            rows.append(
                {
                    "code": code,
                    "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": number(item[1]),
                    "close": number(item[2]),
                    "high": number(item[3]),
                    "low": number(item[4]),
                    "volume": positive(item[5], LOT),
                    "turnover": None,
                }
            )
        return rows


class Sina:
    name = "sina"
    quote_url = "https://hq.sinajs.cn/list="
    kline_url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"
    list_url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
    factor_url = "https://finance.sina.com.cn/realstock/company/{code}/hfq.js"
    referer = "https://finance.sina.com.cn/"

    def __init__(self, transport):
        self.transport = transport

    def quotes(self, codes):
        codes = [symbol(code) for code in codes]
        content = self.transport.get(self.quote_url + ",".join(_lower(code) for code in codes), None, self.referer)
        text = content.decode("gbk", errors="replace")
        rows = []
        for match in re.finditer(r'hq_str_(s[hz]\d{6})="([^"]*)"', text):
            fields = match.group(2).split(",")
            if len(fields) < 32:
                continue  # Unknown or delisted identities come back empty.
            code = symbol(match.group(1)[2:] + "." + match.group(1)[:2].upper())
            stamp = None
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields[30]) and re.fullmatch(r"\d{2}:\d{2}:\d{2}", fields[31]):
                stamp = fields[30] + " " + fields[31]
            rows.append(
                {
                    "code": code,
                    "name": fields[0],
                    "open": number(fields[1]),
                    "pre_close": number(fields[2]),
                    "close": number(fields[3]),
                    "high": number(fields[4]),
                    "low": number(fields[5]),
                    "volume": positive(fields[8]),
                    "turnover": positive(fields[9]),
                    "trade_time": stamp,
                }
            )
        return rows

    def _kline(self, code, scale, length):
        params = {"symbol": _lower(code), "scale": str(scale), "ma": "no", "datalen": str(min(length, 1970))}
        payload = _json(self.transport.get(self.kline_url, params, self.referer))
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise ProviderError("Sina kline envelope changed.")
        return payload

    def daily(self, code, start, end, limit=1970):
        code = symbol_or_index(code)
        rows = []
        for item in self._kline(code, 240, limit):
            day = datetime.strptime(str(item.get("day"))[:10], "%Y-%m-%d").date()
            if start and day < start or end and day > end:
                continue
            rows.append(
                _bar(
                    code,
                    day,
                    item.get("open"),
                    item.get("close"),
                    item.get("high"),
                    item.get("low"),
                    positive(item.get("volume")),
                    positive(item.get("amount")),
                )
            )
        return fill_pre_close(rows)

    def minutes(self, code, limit=1970):
        code = symbol(code)
        rows = []
        for item in self._kline(code, 5, limit):
            rows.append(
                {
                    "code": code,
                    "time": str(item.get("day")),
                    "open": number(item.get("open")),
                    "close": number(item.get("close")),
                    "high": number(item.get("high")),
                    "low": number(item.get("low")),
                    "volume": positive(item.get("volume")),
                    "turnover": positive(item.get("amount")),
                }
            )
        return rows

    def factors(self, code):
        """Sparse cumulative adjustment factors keyed by ex-date; 1.0 applies before the first event."""
        code = symbol(code)
        text = self.transport.get(self.factor_url.format(code=_lower(code)), None, self.referer).decode(
            "utf-8", errors="replace"
        )
        marker = text.find("{")
        if marker < 0:
            raise ProviderError("Sina factor script changed.")
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[marker:])
        except ValueError:
            raise ProviderError("Sina factor script is malformed.") from None
        events = []
        for item in payload.get("data") or []:
            factor = number(item.get("f"))
            if factor is None or factor <= 0:
                raise ProviderError("Sina factor value invalid.")
            events.append((datetime.strptime(item["d"], "%Y-%m-%d").date(), factor))
        events.sort()
        return events

    def directory_page(self, page, size=100):
        params = {"page": str(page), "num": str(size), "sort": "symbol", "asc": "1", "node": "hs_a"}
        payload = _json(self.transport.get(self.list_url, params, self.referer))
        if payload is None:
            return [], None
        if not isinstance(payload, list):
            raise ProviderError("Sina list envelope changed.")
        rows = []
        for item in payload:
            try:
                code = symbol(str(item.get("code")) + "." + str(item.get("symbol"))[:2].upper())
            except ValueError:
                continue
            rows.append(
                {
                    "code": code,
                    "name": str(item.get("name") or ""),
                    "exchange": "SSE" if code.startswith("SH") else "SZSE",
                    "list_status": "L",
                    "list_date": None,
                    "delist_date": None,
                }
            )
        return rows, None


def factor_on(events, day):
    """Cumulative factor effective on ``day`` given sorted (ex_date, factor) events."""
    current = 1.0
    for event_day, factor in events:
        if event_day <= day:
            current = factor
        else:
            break
    return current


def date_chunks(start, end, days=1400):
    """Split a date range so no single request needs more than ~950 sessions."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


__all__ = [
    "EastMoney",
    "Tencent",
    "Sina",
    "Transport",
    "date_chunks",
    "factor_on",
    "fill_pre_close",
    "symbol_or_index",
]
