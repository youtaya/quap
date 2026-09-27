"""Tokenless public quote sites used the way go-stock does: browser-like GETs, bounded, HTTPS-only.

Each client returns provider-neutral rows (see ``quant_platform.providers``). Volume is always
converted to shares and turnover to CNY; fields a site does not publish are ``None``, never zero.
"""

import json
import random
import re
import time
from datetime import datetime, timedelta

import httpx

from quant_platform.domain import CN, number, symbol
from . import Deferred, PermissionDenied, ProviderError, positive

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
BYTE_LIMIT = 8 * 1024 * 1024
REQUEST_DEADLINE = 60
LOT = 100
# Index identities the platform tracks: the two exchange composites and the CSI 300 benchmark.
INDEX_CODES = {"SH000001", "SZ399001", "SH000300"}
# Public 通达信 (TDX) quote servers. Only reachability is assumed; no vendor token exists.
TDX_HOSTS = (
    ("119.147.212.81", 7709),
    ("218.108.98.244", 7709),
    ("123.125.108.24", 7709),
)


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
    """Accept A-share identities and the three index identities the platform uses."""
    if isinstance(code, str) and code.upper() in INDEX_CODES:
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
    quote_url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    # 与 `list_url` 不同的主机：`push2` 被网络边缘拒绝时这台仍然应答，上市日期的兜底走这里。
    org_url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    referer = "https://quote.eastmoney.com/"
    A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
    CSI300_FILTER = "b:BK0500"
    # fqt: 0 不复权, 1 前复权 (qfq), 2 后复权 (hfq)
    RAW, QFQ, HFQ = 0, 1, 2

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

    def daily(self, code, start, end, fqt=RAW, limit=4000):
        code = symbol_or_index(code)
        rows = []
        for parts in self._kline(code, 101, fqt, start, end, limit):
            day = datetime.strptime(parts[0], "%Y-%m-%d").date()
            bar = _bar(code, day, parts[1], parts[2], parts[3], parts[4], positive(parts[5], LOT), positive(parts[6]))
            change = number(parts[9]) if len(parts) > 9 else None
            if bar["close"] is not None and change is not None:
                bar["pre_close"] = round(bar["close"] - change, 4)
            rows.append(bar)
        return fill_pre_close(rows)

    def daily_pair(self, code, start, end, limit=4000):
        """Raw bars plus 前复权 closes; the close ratio is this source's adjustment factor."""
        bars = self.daily(code, start, end, self.RAW, limit)
        qfq = {row["day"]: row["close"] for row in self.daily(code, start, end, self.QFQ, limit)}
        return bars, qfq

    def quotes(self, codes):
        """Realtime rows; ``f124`` is the source's own epoch stamp, never a locally inferred date."""
        codes = [symbol(code) for code in codes]
        params = {
            "fltt": "2",
            "invt": "2",
            "fields": "f12,f13,f14,f2,f15,f16,f17,f18,f5,f6,f124",
            "secids": ",".join(_secid(code) for code in codes),
        }
        payload = _json(self.transport.get(self.quote_url, params, self.referer))
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise ProviderError("Eastmoney quote envelope changed or request rejected.")
        items = data.get("diff")
        if isinstance(items, dict):
            items = list(items.values())
        rows = []
        for item in items or []:
            try:
                code = symbol(("SH" if item.get("f13") == 1 else "SZ") + str(item.get("f12")))
            except ValueError:
                continue
            stamp = number(item.get("f124"))
            moment = (
                datetime.fromtimestamp(stamp, tz=CN).strftime("%Y-%m-%d %H:%M:%S") if stamp and stamp > 0 else None
            )
            rows.append(
                {
                    "code": code,
                    "name": str(item.get("f14") or ""),
                    "open": number(item.get("f17")),
                    "high": number(item.get("f15")),
                    "low": number(item.get("f16")),
                    "close": number(item.get("f2")),
                    "pre_close": number(item.get("f18")),
                    "volume": positive(item.get("f5"), LOT),
                    "turnover": positive(item.get("f6")),
                    "trade_time": moment,
                }
            )
        return rows

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

    def listing_dates(self, page, size=5000):
        """Point-in-time listing dates, from a host that is not the one the security master uses.

        ``push2`` (``list_url``) is refused at the network edge on some networks while every other
        Eastmoney host keeps answering. The security master therefore publishes without a single
        listing date, and ``list_date <= day`` — the filter that decides whether a security was
        tradeable on a given session — silently degrades to "unknown means listed". That is a
        look-ahead bias in every backtest, so the dates are read from the data-centre host instead
        of being given up on. Returns bare 6-digit codes; the exchange prefix belongs to the domain
        layer, which is the only place that knows how to build a symbol.
        """
        params = {
            "reportName": "RPT_F10_BASIC_ORGINFO",
            "columns": "SECURITY_CODE,LISTING_DATE",
            "pageSize": str(size),
            "pageNumber": str(page),
            "sortColumns": "SECURITY_CODE",
            "sortTypes": "1",
        }
        payload = _json(self.transport.get(self.org_url, params, self.referer))
        result = payload.get("result") if isinstance(payload, dict) else None
        if not payload.get("success") or not isinstance(result, dict):
            raise ProviderError("Eastmoney listing-date envelope changed or request rejected.")
        rows = {}
        for item in result.get("data") or []:
            code, listed = str(item.get("SECURITY_CODE") or ""), item.get("LISTING_DATE")
            if not code or not listed:
                continue
            try:
                rows[code] = datetime.strptime(str(listed)[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
        return rows, int(result.get("count") or 0)

    def constituents(self, page, size=100, board=None):
        """Current CSI 300 membership. Public listings carry no dated constituent history."""
        params = {
            "pn": str(page),
            "pz": str(size),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f12",
            "fs": board or self.CSI300_FILTER,
            "fields": "f12,f13,f14",
        }
        payload = _json(self.transport.get(self.list_url, params, self.referer))
        data = payload.get("data") if isinstance(payload, dict) else None
        if payload.get("rc") != 0 or not isinstance(data, dict):
            raise ProviderError("Eastmoney constituent envelope changed or request rejected.")
        rows = []
        for item in data.get("diff") or []:
            try:
                rows.append(symbol(("SH" if item.get("f13") == 1 else "SZ") + str(item.get("f12"))))
            except ValueError:
                continue
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

    def daily(self, code, start, end, fq="", limit=2000):
        code = symbol_or_index(code)
        param = ",".join(
            [_lower(code), "day", start.isoformat() if start else "", end.isoformat() if end else "", str(min(limit, 2000)), fq]
        )
        payload = _json(self.transport.get(self.kline_url, {"param": param}, self.referer))
        data = (payload.get("data") or {}).get(_lower(code)) if isinstance(payload, dict) else None
        if payload.get("code") != 0 or not isinstance(data, dict):
            raise ProviderError("Tencent kline envelope changed or request rejected.")
        series = data.get(f"{fq}day" if fq else "day")
        if series is None:
            series = data.get("day")
        if not isinstance(series, list):
            raise ProviderError("Tencent kline series missing.")
        rows = []
        for item in series:
            if not isinstance(item, list) or len(item) < 6:
                raise ProviderError("Tencent kline row shape changed.")
            day = datetime.strptime(item[0], "%Y-%m-%d").date()
            rows.append(_bar(code, day, item[1], item[2], item[3], item[4], positive(item[5], LOT), None))
        return fill_pre_close(rows)

    def daily_pair(self, code, start, end, limit=2000):
        bars = self.daily(code, start, end, "", limit)
        # 前复权序列另有服务端上限：count 超过 800 时返回的行数反而更少（实测 2000 只回 640 行），
        # 所以复权一侧按 800 请求，才能拿到源实际能给出的最深一档。
        qfq = {row["day"]: row["close"] for row in self.daily(code, start, end, "qfq", min(limit, 800))}
        return bars, qfq

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
    count_url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
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

    def directory_page(self, page, size=100, node="sh_a"):
        """One page of an exchange-specific A-share list.

        ``node`` is deliberately ``sh_a``/``sz_a`` rather than ``hs_a``: the combined node also carries
        北交所 identities, and sorting it by ``symbol`` puts every ``bj`` row on page one, where the
        platform's ``symbol()`` filter drops them all and the page looks truncated.
        """
        params = {"page": str(page), "num": str(size), "sort": "symbol", "asc": "1", "node": node}
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

    def directory_count(self, node="sh_a"):
        """Exchange-specific listed count; ``None`` when the count endpoint is unavailable."""
        payload = _json(self.transport.get(self.count_url, {"node": node}, self.referer))
        return number(payload) if not isinstance(payload, (list, dict)) else None


class Tdx:
    """通达信公开行情服务器（pytdx）。连接或字段不可用即抛错，交给下一源。

    ``pytdx`` 是可选依赖：导入失败时本源直接不可用。日线 ``vol`` 按手（×100 股）、
    ``amount`` 按人民币换算，换算常数由夹具测试锁定。实时行情只在服务器给出完整
    日期时间时才被平台接受，否则按字段不合法降级到下一源。
    """

    name = "pytdx"
    LOT = 100
    PAGE = 1000

    def __init__(self, client=None, hosts=TDX_HOSTS):
        self._client = client
        self.hosts = tuple(hosts)
        self.api = None

    def _session(self):
        if self.api is not None:
            return self.api
        if self._client is not None:
            self.api = self._client
            return self.api
        try:
            from pytdx.hq import TdxHq_API
        except ImportError:
            raise ProviderError("通达信 client is unavailable.") from None
        try:
            api = TdxHq_API(heartbeat=False, auto_retry=False)
        except Exception as exc:  # noqa: BLE001 - a client that cannot be built is simply unavailable
            raise ProviderError("通达信 client could not be built (%s)." % type(exc).__name__) from exc
        for host, port in self.hosts:
            try:
                if api.connect(host, port, time_out=5):
                    self.api = api
                    return api
            except Exception:  # noqa: BLE001 - pytdx raises its own tree (ResponseHeaderRecvFails, ...)
                continue
        raise ProviderError("No 通达信 quote server is reachable.")

    @staticmethod
    def _guard(call, message):
        """Run one pytdx call, converting its private exception tree into a degradation signal.

        ``pytdx`` raises ``TdxException`` subclasses (``ResponseHeaderRecvFails`` and friends) that are
        unrelated to ``OSError``; if they escape they bypass ``Market._degrade`` and abort the whole
        capability instead of falling through to the next source.
        """
        try:
            return call()
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - any client failure must degrade, never escape
            raise ProviderError("%s (%s)." % (message, type(exc).__name__)) from exc

    def close(self):
        if self._client is None and self.api is not None:
            try:
                self.api.disconnect()
            except Exception:  # noqa: BLE001 - a dead socket must not mask the real error
                pass
        self.api = None

    @staticmethod
    def params():
        try:
            from pytdx.params import TDXParams
        except ImportError:
            # 通达信 是可选依赖：缺失时本源整体不可用，按降级顺序换下一源。
            raise ProviderError("通达信 client is unavailable.") from None

        return TDXParams

    def _market(self, code):
        return self.params().MARKET_SH if code.startswith("SH") else self.params().MARKET_SZ

    @staticmethod
    def _stamp(row):
        if row.get("datetime"):
            return str(row["datetime"])[:19]
        try:
            return f"{int(row['year']):04d}-{int(row['month']):02d}-{int(row['day']):02d}"
        except (KeyError, TypeError, ValueError):
            return None

    def _bars(self, code, category, count):
        code = symbol_or_index(code)
        api = self._session()
        fetch = api.get_index_bars if code in INDEX_CODES else api.get_security_bars
        rows = (
            self._guard(
                lambda: fetch(category, self._market(code), code[2:], 0, int(count)),
                "通达信 bar request failed",
            )
            or []
        )
        parsed = []
        for row in rows:
            stamp = self._stamp(row)
            if stamp is None:
                raise ProviderError("通达信 bar is missing a source timestamp.")
            parsed.append({**row, "code": code, "stamp": stamp})
        return parsed

    def daily(self, code, start=None, end=None, limit=800):
        rows = []
        for row in self._bars(code, self.params().KLINE_TYPE_RI_K, min(limit, 800)):
            try:
                day = datetime.strptime(row["stamp"][:10], "%Y-%m-%d").date()
            except ValueError:
                raise ProviderError("通达信 trading date is invalid.") from None
            if start and day < start or end and day > end:
                continue
            rows.append(
                _bar(
                    row["code"],
                    day,
                    row.get("open"),
                    row.get("close"),
                    row.get("high"),
                    row.get("low"),
                    positive(row.get("vol"), self.LOT),
                    positive(row.get("amount")),
                )
            )
        return fill_pre_close(rows)

    def minutes(self, code, limit=800):
        code = symbol_or_index(code)
        rows = []
        for row in self._bars(code, self.params().KLINE_TYPE_5MIN, min(limit, 800)):
            try:
                stamp = datetime.strptime(row["stamp"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ProviderError("通达信 minute timestamp is invalid.") from None
            rows.append(
                {
                    "code": code,
                    "time": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                    "open": number(row.get("open")),
                    "close": number(row.get("close")),
                    "high": number(row.get("high")),
                    "low": number(row.get("low")),
                    "volume": positive(row.get("vol"), self.LOT),
                    "turnover": positive(row.get("amount")),
                }
            )
        return rows

    def quotes(self, codes):
        codes = [symbol(code) for code in codes]
        api = self._session()
        requested = [(self._market(code), code[2:]) for code in codes]
        rows = (
            self._guard(lambda: api.get_security_quotes(requested), "通达信 quote request failed") or []
        )
        parsed = []
        for row in rows:
            code = symbol(("SH" if row.get("market") == self.params().MARKET_SH else "SZ") + str(row.get("code")))
            stamp = str(row.get("servertime") or "")
            parsed.append(
                {
                    "code": code,
                    "name": "",
                    "open": number(row.get("open")),
                    "high": number(row.get("high")),
                    "low": number(row.get("low")),
                    "close": number(row.get("price")),
                    "pre_close": number(row.get("last_close")),
                    "volume": positive(row.get("vol"), self.LOT),
                    "turnover": positive(row.get("amount")),
                    # The public server returns a bare clock time; the platform never infers a date.
                    "trade_time": stamp if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", stamp) else None,
                }
            )
        return parsed

    def directory(self, market):
        api = self._session()
        count = self._guard(lambda: api.get_security_count(market), "通达信 count request failed") or 0
        rows = []
        for start in range(0, int(count), self.PAGE):
            rows.extend(
                self._guard(
                    lambda start=start: api.get_security_list(market, start),
                    "通达信 list request failed",
                )
                or []
            )
        parsed = []
        for row in rows:
            try:
                code = symbol(("SH" if market == self.params().MARKET_SH else "SZ") + str(row.get("code")))
            except ValueError:
                continue
            parsed.append(
                {
                    "code": code,
                    "name": str(row.get("name") or "").strip(),
                    "exchange": "SSE" if code.startswith("SH") else "SZSE",
                    "list_status": "L",
                    "list_date": None,
                    "delist_date": None,
                }
            )
        return parsed


def factor_ratio(raw, qfq):
    """Cumulative adjustment factor ``qfq / raw`` per day; unusable pairs stay absent."""
    factors = {}
    for day, price in raw.items():
        adjusted = qfq.get(day)
        if not price or adjusted is None:
            continue
        factor = number(adjusted) / number(price) if number(price) else None
        if factor and factor > 0:
            factors[day] = factor
    return factors


def date_chunks(start, end, days=1400):
    """Split a date range so no single request needs more than ~950 sessions."""
    cursor = start
    while cursor <= end:
        stop = min(end, cursor + timedelta(days=days - 1))
        yield cursor, stop
        cursor = stop + timedelta(days=1)


__all__ = [
    "INDEX_CODES",
    "TDX_HOSTS",
    "EastMoney",
    "Sina",
    "Tdx",
    "Tencent",
    "Transport",
    "date_chunks",
    "factor_ratio",
    "fill_pre_close",
    "symbol_or_index",
]
