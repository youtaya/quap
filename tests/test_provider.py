"""Fixture-locked parsing, units, degradation order and partial responses for the market provider.

Every source is exercised through ``httpx.MockTransport`` or an injected fake 通达信 client, so the
suite never touches the network. Each source's own unit conversion (手→股, 亿元→元) is pinned here,
together with the go-stock degradation order and the "a missing value is not a zero" contract.
"""

from datetime import date, datetime, time, timedelta
import sys
import types

import httpx
import pytest

from quant_platform.config import Settings
from quant_platform.domain import CN, now
from quant_platform.providers import Deferred, PermissionDenied, ProviderError
from quant_platform.providers.market import CAPABILITY_SOURCES, Market, paged
from quant_platform.providers.sources import Tdx, Transport, factor_ratio

DAY = date(2026, 9, 21)
NEXT = date(2026, 9, 22)
HOSTS = {
    "eastmoney": "https://push2his.eastmoney.com",
    "eastmoney-quote": "https://push2.eastmoney.com",
    "eastmoney-datacenter": "https://datacenter-web.eastmoney.com",
    "sina-quote": "https://hq.sinajs.cn",
    "sina-kline": "https://quotes.sina.cn",
    "sina-list": "https://vip.stock.finance.sina.com.cn",
    "tencent-quote": "https://qt.gtimg.cn",
    "tencent-kline": "https://web.ifzq.gtimg.cn",
    "tencent-minute": "https://ifzq.gtimg.cn",
}


class Params:
    MARKET_SH = 1
    MARKET_SZ = 0
    KLINE_TYPE_RI_K = 9
    KLINE_TYPE_5MIN = 0


class ResponseHeaderRecvFails(Exception):
    """Stand-in for pytdx's own exception tree, which is deliberately not an ``OSError``."""


# The autouse fixture below replaces ``Tdx._session``; keep the real one for the handshake test.
REAL_SESSION = Tdx._session


@pytest.fixture(autouse=True)
def no_real_tdx(monkeypatch):
    """An injected fake client works; a real 通达信 socket is always a test bug."""
    original = REAL_SESSION

    def guarded(self):
        if self._client is None:
            raise ProviderError("通达信 is unavailable in tests; inject a fake client.")
        return original(self)

    monkeypatch.setattr(Tdx, "_session", guarded)
    monkeypatch.setattr(Tdx, "params", staticmethod(lambda: Params))


class FakePytdx:
    """Minimal raw pytdx-shaped client, injected into the :class:`Tdx` wrapper."""

    def __init__(self, bars=None, index_bars=None, quotes=None, directory=None, error=None):
        self.bars = bars if bars is not None else []
        self.index_bars = index_bars if index_bars is not None else self.bars
        self.quote_rows = quotes if quotes is not None else []
        self.listings = directory if directory is not None else {}
        self.error = error

    def _guard(self):
        if self.error:
            raise self.error

    def get_security_bars(self, category, market, code, start, count):
        self._guard()
        return self.bars

    def get_index_bars(self, category, market, code, start, count):
        self._guard()
        return self.index_bars

    def get_security_quotes(self, requested):
        self._guard()
        return self.quote_rows

    def get_security_count(self, market):
        self._guard()
        return len(self.listings.get(market, []))

    def get_security_list(self, market, start):
        self._guard()
        return self.listings.get(market, [])


def tdx_bar(day, close=10.5):
    return {
        "datetime": f"{day} 15:00:00",
        "open": 10.0,
        "high": 10.8,
        "low": 9.9,
        "close": close,
        "vol": 12345,
        "amount": 13000000,
    }


def eastmoney_kline(day, close, change=0.5, code="600895"):
    return {
        "rc": 0,
        "data": {"code": code, "klines": [f"{day},10.00,{close:.2f},10.80,9.90,12345,13000000,0,0,{change:.2f},0"]},
    }


def sina_kline(day, close="10.50", volume="12345", amount="13000000"):
    return [
        {"day": str(day), "open": "10.00", "high": "10.80", "low": "9.90", "close": close, "volume": volume, "amount": amount}
    ]


def build(settings, routes=None, tdx=None, down=()):
    """Route by URL prefix. Anything neither routed nor explicitly down fails loudly."""
    routes = routes or {}
    blocked = {HOSTS[name] for name in down}

    def respond(request):
        url = str(request.url)
        for prefix, value in routes.items():
            if url.startswith(prefix):
                if callable(value):
                    return value(request)
                if isinstance(value, httpx.Response):
                    return value
                return httpx.Response(200, json=value)
        for prefix in blocked:
            if url.startswith(prefix):
                return httpx.Response(500)
        raise AssertionError(f"Unexpected provider request: {url}")

    market = Market(
        settings,
        client=httpx.Client(transport=httpx.MockTransport(respond)),
        sleep=lambda _: None,
        tdx=tdx if tdx is not None else Tdx(),
    )
    return market


# ------------------------------------------------------------------ units and parsing


def test_eastmoney_daily_units_and_factor_ratio(settings):
    def route(request):
        close = 10.5 if request.url.params["fqt"] == "0" else 8.4  # 前复权 ÷ 不复权 = 0.8
        return httpx.Response(200, json=eastmoney_kline(DAY, close))

    market = build(settings, {HOSTS["eastmoney"]: route})
    result = market.history("SH600895", DAY, DAY)
    assert result["source"] == "eastmoney"
    bar = result["bars"][0]
    assert bar["volume"] == 12345 * 100  # 手 → 股
    assert bar["turnover"] == 13000000  # 东方财富的成交额已经是人民币元
    assert bar["pre_close"] == pytest.approx(10.0)
    assert result["factors"][DAY] == pytest.approx(0.8)
    assert result["factor_source"] == "eastmoney"
    # 因子走的是 ``_factors`` 的成对请求，不经过 ``_degrade("factors")``；这次上报就是它的契约证据。
    assert market.last_sources["factors"] == "eastmoney"


def test_eastmoney_quotes_use_the_source_epoch_stamp(settings):
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "600895",
                    "f13": 1,
                    "f14": "张江高科",
                    "f2": 10.5,
                    "f15": 10.8,
                    "f16": 9.9,
                    "f17": 10.0,
                    "f18": 10.2,
                    "f5": 12345,
                    "f6": 13000000,
                    "f124": 1789000000,
                }
            ]
        },
    }
    market = build(settings, {HOSTS["eastmoney-quote"]: payload})
    row = market.quotes(["SH600895"])[0]
    assert row["symbol"] == "SH600895"
    assert row["volume"] == 12345 * 100
    assert row["turnover"] == 13000000
    assert row["source_time"] == datetime.fromtimestamp(1789000000, tz=CN).strftime("%Y-%m-%d %H:%M:%S")
    assert market.last_sources["quotes"] == "eastmoney"


def test_tencent_quote_units_and_split_turnover(settings):
    fields = ["0"] * 49
    fields[0], fields[1], fields[2] = "1", "张江高科", "600895"
    fields[3], fields[4], fields[5] = "10.50", "10.20", "10.00"
    fields[6] = "12345"
    fields[30] = "20260922145959"
    fields[33], fields[34] = "10.80", "9.90"
    fields[35] = "12345/13000000/13000000"
    fields[47], fields[48] = "11.22", "9.18"
    body = f'v_sh600895="{"~".join(fields)}";'

    market = build(
        settings,
        {HOSTS["tencent-quote"]: httpx.Response(200, content=body.encode("gbk"))},
        down=("eastmoney-quote", "sina-quote"),
    )
    row = market.quotes(["SH600895"])[0]
    assert row["volume"] == 12345 * 100
    assert row["turnover"] == 13000000
    assert row["source_time"] == "20260922145959"


def test_tencent_daily_publishes_no_turnover_and_a_qfq_pair(settings):
    def route(request):
        close = "10.50" if not request.url.params["param"].rsplit(",", 1)[-1] else "8.40"
        return httpx.Response(
            200,
            json={"code": 0, "data": {"sh600895": {"day": [[str(DAY), "10.00", close, "10.80", "9.90", "12345"]]}}},
        )

    market = build(settings, {HOSTS["tencent-kline"]: route}, down=("eastmoney", "sina-kline"))
    result = market.history("SH600895", DAY, DAY)
    assert result["source"] == "tencent"
    assert result["bars"][0]["volume"] == 12345 * 100
    assert result["bars"][0]["turnover"] is None  # 该源不发布成交额：缺失保持缺失
    assert result["factors"][DAY] == pytest.approx(0.8)


def test_tencent_front_adjusted_pair_asks_for_the_depth_the_source_honours(settings):
    """腾讯的前复权序列在 count 超过 800 时被服务端截得更短（实测 2000 只回 640 行）。

    复权因子窗口是准入判定的一项硬要求，所以复权一侧必须按源实际兑现的最深一档请求。
    """
    requested = []

    def route(request):
        requested.append(request.url.params["param"].split(","))
        return httpx.Response(
            200,
            json={"code": 0, "data": {"sh600895": {"day": [[str(DAY), "10.00", "10.50", "10.80", "9.90", "12345"]]}}},
        )

    market = build(settings, {HOSTS["tencent-kline"]: route}, down=("eastmoney", "sina-kline"))
    result = market.history("SH600895", DAY, DAY)
    assert result["source"] == "tencent"
    assert [(row[-2], row[-1]) for row in requested] == [("2000", ""), ("800", "qfq")]


def test_sina_daily_volume_is_already_shares(settings):
    market = build(settings, {HOSTS["sina-kline"]: sina_kline(DAY)}, down=("eastmoney", "tencent-kline"))
    result = market.history("SH600895", DAY, DAY)
    assert result["source"] == "sina"
    assert result["bars"][0]["volume"] == 12345
    assert result["bars"][0]["turnover"] == 13000000
    assert result["factors"] == {}  # 新浪日线不提供成对复权价


def test_pytdx_bars_are_converted_from_lots(settings):
    market = build(settings, {}, tdx=Tdx(client=FakePytdx(bars=[tdx_bar(DAY)])), down=("eastmoney", "tencent-kline"))
    result = market.history("SH600895", DAY, DAY)
    assert result["source"] == "pytdx"
    assert result["bars"][0]["volume"] == 12345 * 100
    assert result["bars"][0]["turnover"] == 13000000
    assert result["factors"] == {} and result["factor_source"] is None


def test_missing_values_stay_none_and_never_zero(settings):
    market = build(
        settings,
        {HOSTS["sina-kline"]: sina_kline(DAY, volume="-", amount=None)},
        down=("eastmoney", "tencent-kline"),
    )
    bar = market.history("SH600895", DAY, DAY)["bars"][0]
    assert bar["volume"] is None and bar["turnover"] is None


def test_factor_ratio_skips_unusable_pairs():
    assert factor_ratio({DAY: 10.0, NEXT: 0.0}, {DAY: 8.0, NEXT: 5.0}) == {DAY: pytest.approx(0.8)}
    assert factor_ratio({DAY: 10.0}, {}) == {}


# ------------------------------------------------------------------ degradation order


def test_pytdx_failure_degrades_to_eastmoney(settings):
    tdx = Tdx(client=FakePytdx(error=ProviderError("no reachable server")))
    market = build(settings, {HOSTS["eastmoney"]: eastmoney_kline(DAY, 10.5)}, tdx=tdx)
    assert market.history("SH600895", DAY, DAY)["source"] == "eastmoney"
    assert market.last_sources["history"] == "eastmoney"


def test_pytdx_private_exception_degrades_instead_of_escaping(settings):
    """pytdx 抛的是自己的异常树（非 OSError）；它必须按降级顺序换下一源，而不是中断整个能力。"""
    tdx = Tdx(client=FakePytdx(error=ResponseHeaderRecvFails("bad response header")))
    market = build(settings, {HOSTS["eastmoney"]: eastmoney_kline(DAY, 10.5)}, tdx=tdx)
    assert market.history("SH600895", DAY, DAY)["source"] == "eastmoney"


def test_every_pytdx_surface_converts_its_own_exception():
    tdx = Tdx(client=FakePytdx(error=ResponseHeaderRecvFails("bad response header")))
    for call in (
        lambda: tdx.daily("SH600895", DAY, DAY),
        lambda: tdx.minutes("SH600895"),
        lambda: tdx.quotes(["SH600895"]),
        lambda: tdx.directory(Params.MARKET_SH),
    ):
        with pytest.raises(ProviderError):
            call()


def test_unreachable_tdx_handshake_is_a_provider_error(monkeypatch):
    """握手失败（TCP 通、协议不通）同样只能降级，不能把 pytdx 异常抛给调用方。"""
    attempted = []

    class FakeApi:
        def __init__(self, **kwargs):
            pass

        def connect(self, host, port, time_out=None):
            attempted.append(host)
            raise ResponseHeaderRecvFails("bad response header")

    package = types.ModuleType("pytdx")
    package.__path__ = []
    module = types.ModuleType("pytdx.hq")
    module.TdxHq_API = FakeApi
    package.hq = module
    monkeypatch.setitem(sys.modules, "pytdx", package)
    monkeypatch.setitem(sys.modules, "pytdx.hq", module)

    tdx = Tdx(hosts=(("127.0.0.1", 7709), ("127.0.0.2", 7709)))
    with pytest.raises(ProviderError) as error:
        REAL_SESSION(tdx)
    assert attempted == ["127.0.0.1", "127.0.0.2"]  # 每个候选服务器都试过
    assert "reachable" in str(error.value)


def test_empty_response_degrades_to_the_next_source(settings):
    routes = {
        HOSTS["eastmoney"]: {"rc": 0, "data": {"code": "600895", "klines": []}},
        HOSTS["sina-kline"]: sina_kline(DAY),
    }
    market = build(settings, routes, down=("tencent-kline",))
    assert market.history("SH600895", DAY, DAY)["source"] == "sina"


def test_all_sources_failing_reports_the_attempted_order(settings):
    market = build(settings, {}, down=("eastmoney", "sina-kline", "tencent-kline"))
    with pytest.raises(ProviderError) as error:
        market.history("SH600895", DAY, DAY)
    message = str(error.value)
    assert message.startswith("No public source served history")
    assert "pytdx" in message and "tencent" in message


def test_quotes_without_a_source_timestamp_degrade(settings):
    tdx = Tdx(
        client=FakePytdx(
            quotes=[
                {
                    "market": 1,
                    "code": "600895",
                    "servertime": "14:59:59",  # 只有时钟时间；平台不推断日期
                    "open": 10.0,
                    "high": 10.8,
                    "low": 9.9,
                    "price": 10.5,
                    "last_close": 10.2,
                    "vol": 12345,
                    "amount": 13000000,
                }
            ]
        )
    )
    payload = {
        "rc": 0,
        "data": {
            "diff": [
                {
                    "f12": "600895",
                    "f13": 1,
                    "f14": "张江高科",
                    "f2": 10.5,
                    "f15": 10.8,
                    "f16": 9.9,
                    "f17": 10.0,
                    "f18": 10.2,
                    "f5": 12345,
                    "f6": 13000000,
                    "f124": 1789000000,
                }
            ]
        },
    }
    market = build(settings, {HOSTS["eastmoney-quote"]: payload}, tdx=tdx)
    assert market.quotes(["SH600895"])[0]["source_time"]
    assert market.last_sources["quotes"] == "eastmoney"


def test_incomplete_quote_batch_is_rejected(settings):
    market = build(
        settings,
        {HOSTS["eastmoney-quote"]: {"rc": 0, "data": {"diff": []}}},
        down=("sina-quote", "tencent-quote"),
    )
    with pytest.raises(ProviderError):
        market.quotes(["SH600895"])


def test_sina_quote_rows_without_a_timestamp_never_serve(settings):
    fields = ["0"] * 32
    fields[0], fields[1], fields[2], fields[3] = "张江高科", "10.00", "10.20", "10.50"
    fields[4], fields[5], fields[8], fields[9] = "10.80", "9.90", "12345", "13000000"
    body = f'var hq_str_sh600895="{",".join(fields)}";'
    market = build(
        settings,
        {HOSTS["sina-quote"]: httpx.Response(200, content=body.encode("gbk"))},
        down=("eastmoney-quote", "tencent-quote"),
    )
    with pytest.raises(ProviderError) as error:
        market.quotes(["SH600895"])
    assert "tencent" in str(error.value)


def test_capability_source_order_is_declared():
    assert CAPABILITY_SOURCES["history"] == ("pytdx", "eastmoney", "sina", "tencent")
    # 主表保留新浪兜底：东方财富与通达信都不可达时主表仍须可用。
    assert CAPABILITY_SOURCES["securities"] == ("eastmoney", "pytdx", "sina")
    # 两项都是纯推导，没有源响应可以校验；写成源能力会让 readiness 门禁永远不成立。
    assert CAPABILITY_SOURCES["constraints"] == ("derived",)
    assert CAPABILITY_SOURCES["security_history"] == ("derived",)


# ------------------------------------------------------------------ security master and calendar


def test_security_master_keeps_only_supported_identities(settings):
    payload = {
        "rc": 0,
        "data": {
            "total": 2,
            "diff": [
                {"f12": "600895", "f13": 1, "f14": "张江高科", "f26": "19960422"},
                {"f12": "300750", "f13": 0, "f14": "宁德时代", "f26": "20180611"},
            ],
        },
    }
    market = build(settings, {HOSTS["eastmoney-quote"]: payload})
    rows = market.securities()
    assert set(rows) == {"SH600895", "SZ300750"}
    assert rows["SH600895"]["board"] == "Main Board"
    assert rows["SH600895"]["list_date"] == date(1996, 4, 22)
    assert rows["SZ300750"]["exchange"] == "SZSE"
    assert market.last_sources["securities"] == "eastmoney"
    dropped = {}
    Market._add_security(dropped, {"code": "SH830799", "name": "北交所样本"})
    Market._add_security(dropped, {"code": "SH510300", "name": "沪深300ETF"})
    assert dropped == {}


def test_security_master_falls_back_to_the_tdx_directory(settings):
    listings = {
        Params.MARKET_SH: [{"code": "600895", "name": "张江高科"}],
        Params.MARKET_SZ: [{"code": "300750", "name": "宁德时代"}],
    }
    tdx = Tdx(client=FakePytdx(directory=listings))
    # 数据中心主机一并标为不可达：这两个用例测的是主表降级，日期补充有自己的用例。
    market = build(settings, {}, tdx=tdx, down=("eastmoney-quote", "eastmoney-datacenter"))
    assert set(market.securities()) == {"SH600895", "SZ300750"}
    assert market.last_sources["securities"] == "pytdx"


def test_security_master_falls_back_to_sina_exchange_nodes(settings):
    """东方财富与通达信都不可达时，主表仍须可用；新浪按交易所节点分页，避免北交所挤满首页。"""
    pages = {
        "sh_a": [
            {"code": "600895", "symbol": "sh600895", "name": "张江高科"},
            {"code": "688981", "symbol": "sh688981", "name": "中芯国际"},
        ],
        "sz_a": [{"code": "300750", "symbol": "sz300750", "name": "宁德时代"}],
    }
    counts = {"sh_a": 2, "sz_a": 1}
    seen_nodes = []

    def route(request):
        node = request.url.params["node"]
        seen_nodes.append(node)
        if "getHQNodeStockCount" in str(request.url):
            return httpx.Response(200, json=str(counts[node]))
        return httpx.Response(200, json=pages[node])

    market = build(
        settings,
        {HOSTS["sina-list"]: route},
        down=("eastmoney", "eastmoney-quote", "eastmoney-datacenter"),
    )
    rows = market.securities()
    assert set(rows) == {"SH600895", "SH688981", "SZ300750"}
    assert rows["SH688981"]["board"] == "STAR Market"
    assert set(seen_nodes) == {"sh_a", "sz_a"}  # 从不使用会被北交所污染的 hs_a 合并节点
    assert market.last_sources["securities"] == "sina"


def test_listing_dates_are_filled_from_the_data_centre_host_when_push2_is_refused(settings):
    """`push2` 被网络边缘拒绝时上市日期仍要补齐，否则 `list_date <= day` 退化成「未知即已上市」。

    两台主机是两个独立的可达性问题：主表来自新浪，日期来自东方财富的数据中心接口。少了这一步，
    回测会把上市前的区间当成可交易，而 `list_dates_verified` 会一直是 false。
    """
    pages = {"sh_a": [{"code": "600895", "symbol": "sh600895", "name": "张江高科"}], "sz_a": []}
    counts = {"sh_a": 1, "sz_a": 0}
    asked = []

    def sina(request):
        node = request.url.params["node"]
        if "getHQNodeStockCount" in str(request.url):
            return httpx.Response(200, json=str(counts[node]))
        return httpx.Response(200, json=pages[node])

    def datacenter(request):
        asked.append(dict(request.url.params))
        return httpx.Response(
            200,
            json={
                "success": True,
                "result": {
                    "count": 2,
                    "pages": 1,
                    "data": [
                        {"SECURITY_CODE": "600895", "LISTING_DATE": "1996-04-22 00:00:00"},
                        {"SECURITY_CODE": "300750", "LISTING_DATE": "2018-06-11 00:00:00"},
                    ],
                },
            },
        )

    market = build(
        settings,
        {HOSTS["sina-list"]: sina, HOSTS["eastmoney-datacenter"]: datacenter},
        down=("eastmoney", "eastmoney-quote"),
    )
    rows = market.securities()
    assert rows["SH600895"]["list_date"] == date(1996, 4, 22)
    assert market.listing_dates_filled == 1  # 只补主表里缺的那些
    assert market.last_sources["securities"] == "sina"  # 身份来源不变，日期是补充
    assert asked[0]["reportName"] == "RPT_F10_BASIC_ORGINFO"


def test_listing_date_enrichment_never_fails_an_already_published_master(settings):
    """补充是尽力而为：日期拿不到时主表照常发布，缺口继续由 `list_dates_verified=false` 暴露。"""
    pages = {"sh_a": [{"code": "600895", "symbol": "sh600895", "name": "张江高科"}], "sz_a": []}
    counts = {"sh_a": 1, "sz_a": 0}

    def sina(request):
        node = request.url.params["node"]
        if "getHQNodeStockCount" in str(request.url):
            return httpx.Response(200, json=str(counts[node]))
        return httpx.Response(200, json=pages[node])

    market = build(
        settings,
        {HOSTS["sina-list"]: sina},
        down=("eastmoney", "eastmoney-quote", "eastmoney-datacenter"),
    )
    rows = market.securities()
    assert set(rows) == {"SH600895"}
    assert rows["SH600895"]["list_date"] is None
    assert market.listing_dates_filled == 0


def test_constituents_prefer_eastmoney(settings):
    payload = {
        "rc": 0,
        "data": {"total": 2, "diff": [{"f12": "600519", "f13": 1}, {"f12": "000001", "f13": 0}]},
    }
    market = build(settings, {HOSTS["eastmoney-quote"]: payload})
    assert market.constituents() == {"SH600519", "SZ000001"}
    assert market.last_sources["constituents"] == "eastmoney"


def test_constituents_degrade_to_the_sina_hs300_node(settings):
    """默认 history_scope=index：成分股取不到就等于不采集，因此必须有第二条路径。"""
    members = [
        {"code": "600036", "symbol": "sh600036", "name": "招商银行"},
        {"code": "000858", "symbol": "sz000858", "name": "五粮液"},
        {"code": "300750", "symbol": "sz300750", "name": "宁德时代"},
    ]
    seen_nodes = []

    def route(request):
        node = request.url.params["node"]
        seen_nodes.append(node)
        if "getHQNodeStockCount" in str(request.url):
            return httpx.Response(200, json=str(len(members)))
        return httpx.Response(200, json=members)

    market = build(settings, {HOSTS["sina-list"]: route}, down=("eastmoney-quote",))
    assert market.constituents() == {"SH600036", "SZ000858", "SZ300750"}
    assert market.last_sources["constituents"] == "sina"
    assert set(seen_nodes) == {"hs300"}


def test_constituents_fail_loudly_when_no_source_answers(settings):
    market = build(settings, {}, down=("eastmoney-quote", "sina-list"))
    with pytest.raises(ProviderError) as error:
        market.constituents()
    assert "constituents" in str(error.value)


def test_paged_stops_on_the_announced_total_without_over_fetching():
    """四个分页列表共用同一个停止条件；翻多一页就是白花一次配额。"""
    requested, seen = [], set()

    def fetch(page):
        requested.append(page)
        return [{"code": f"SH60000{page}"}], 2

    def add(rows):
        seen.update(row["code"] for row in rows)
        return len(seen)

    covered, total = paged(fetch, add, 100)
    assert (covered, total) == (2, 2)
    assert requested == [1, 2]  # 达到公布总数即停，不请求第 3 页


def test_paged_stops_on_an_empty_page_when_the_total_is_unknown():
    requested = []

    def fetch(page):
        requested.append(page)
        return ([{"code": "SH600895"}], None) if page == 1 else ([], None)

    covered, total = paged(fetch, lambda rows: 1 if rows else 0, 100)
    assert total is None
    assert requested == [1, 2]


def test_paged_reads_the_announced_total_from_the_first_page_only():
    """总数只在首页解析：每页都补一次计数请求会把配额翻倍。"""
    requested, seen = [], set()

    def fetch(page):
        requested.append(page)
        # 第 2 页起谎报一个更大的总数：真实实现只认首页那一次。
        return [{"code": f"SH60000{page}"}], (3 if page == 1 else 99)

    def add(rows):
        seen.update(row["code"] for row in rows)
        return len(seen)

    covered, total = paged(fetch, add, 100)
    assert total == 3
    assert requested == [1, 2, 3]
    assert covered == 3


def test_paged_never_exceeds_its_page_limit():
    requested = []

    def fetch(page):
        requested.append(page)
        return [{"code": f"SH60000{page}"}], None

    covered, total = paged(fetch, lambda rows: 1, 4)
    assert requested == [1, 2, 3, 4]
    assert covered == 1 and total is None


def test_calendar_is_derived_per_exchange_from_index_bars(settings):
    def route(request):
        secid = request.url.params["secid"]
        if secid == "1.000001":
            return httpx.Response(200, json=eastmoney_kline(DAY, 1.0, change=0.0, code="000001"))
        return httpx.Response(
            200,
            json={
                "rc": 0,
                "data": {
                    "code": "399001",
                    "klines": [
                        f"{DAY},1,1,1,1,0,0,0,0,0,0",
                        f"{NEXT},1,1,1,1,0,0,0,0,0,0",
                    ],
                },
            },
        )

    market = build(settings, {HOSTS["eastmoney"]: route})
    calendars = market.calendar(DAY, NEXT)
    assert calendars["SSE"] == {DAY: True, NEXT: False}
    assert calendars["SZSE"] == {DAY: True, NEXT: True}
    # 日历由两根指数 K 线推导，走的是 ``history`` 的请求，所以必须自己记一次契约验证。
    assert market.last_sources["calendar"] == "eastmoney"


def test_benchmark_records_its_own_capability_qualification(settings):
    """`SH000300` 不是上市证券，不在 `scoped_codes` 里，所以它只能靠自己那次请求留下证据。

    不记的话 `readiness` 永远把 `benchmark` 列进「未验证能力」，而门禁要求
    `DAILY_CAPABILITIES` 的每一项都带 `schema_verified`，判定就永远不成立。
    """

    def route(request):
        close = 10.5 if request.url.params["fqt"] == "0" else 8.4
        return httpx.Response(200, json=eastmoney_kline(DAY, close, code="000300"))

    market = build(settings, {HOSTS["eastmoney"]: route})
    result = market.benchmark(DAY, DAY)
    assert result["symbol"] == "SH000300"
    assert market.last_sources["benchmark"] == "eastmoney"


# ------------------------------------------------------------------ derived capabilities


def test_constraints_are_derived_from_stored_closes_without_any_provider_call(settings):
    """`CAPABILITY_SOURCES` declares constraints as `derived`; the rule must not fetch history.

    One history request per security per session is `sessions x securities` calls (1500 x 298 in a
    default deployment), which no tokenless quota can serve, so the bootstrap never converged.
    """
    market = build(settings, {})  # any provider request would raise AssertionError
    rows = market.constraints(NEXT, {"SH688001": 10.0}, {"SH688001": "科创样本"})
    assert rows[0]["limit_up"] == pytest.approx(12.0)  # 科创板 ±20%
    assert rows[0]["limit_down"] == pytest.approx(8.0)
    assert rows[0]["derived"] is True
    # 停牌没有免令牌来源：``False`` 会把「不知道」写成「已确认可交易」，所以报未知并标出来源。
    assert rows[0]["suspended"] is None and rows[0]["suspended_source"] == "unavailable"
    assert market.last_sources == {}


def test_risk_warning_names_use_the_narrower_band(settings):
    """The 5% band is a leading marker, not any name that happens to contain the letters `ST`."""
    feed = build(settings, {})
    main = feed.constraints(NEXT, {"SH600895": 10.0}, {"SH600895": "ST张江"})[0]
    assert main["limit_up"] == pytest.approx(10.5)  # 主板风险警示 ±5%
    assert main["limit_down"] == pytest.approx(9.5)

    starred = feed.constraints(NEXT, {"SH600895": 10.0}, {"SH600895": "*ST张江"})[0]
    assert starred["limit_up"] == pytest.approx(10.5)
    assert starred["limit_up"] == main["limit_up"]

    # 名称里恰好含 `ST` 的普通股票不能被当成风险警示股（`TEST`、`STONE`）。
    plain = feed.constraints(NEXT, {"SH600895": 10.0}, {"SH600895": "TEST"})[0]
    assert plain["limit_up"] == pytest.approx(11.0)

    # 科创板/创业板的风险警示股仍保持 ±20%，收窄只作用于主板。
    growth = feed.constraints(NEXT, {"SZ300750": 10.0}, {"SZ300750": "ST宁德"})[0]
    assert growth["limit_up"] == pytest.approx(12.0)

    # 退市整理期名称按「退」识别。
    delisting = feed.constraints(NEXT, {"SH600895": 10.0}, {"SH600895": "张江退"})[0]
    assert delisting["limit_up"] == pytest.approx(10.5)


def test_risk_warning_is_read_from_the_name_prefix_in_both_consumers(settings):
    """涨跌停档位与 `security_history` 的风险标记必须共用同一套判定，不能各写一遍。"""
    feed = build(settings, {})
    for name, expected in (
        ("ST张江", True),
        ("*ST张江", True),
        ("SST张江", True),
        ("S*ST张江", True),
        ("张江退", True),
        ("TEST", False),
        ("张江高科", False),
    ):
        assert feed.security_history("SH600895", name)[0]["risk_warning"] is expected, name


def test_constraints_skip_a_missing_or_unusable_close(settings):
    rows = build(settings, {}).constraints(NEXT, {"SH600895": None, "SZ000001": 0.0, "SH600000": 20.0}, {})
    assert [row["symbol"] for row in rows] == ["SH600000"]
    assert rows[0]["limit_up"] == pytest.approx(22.0)  # 主板 ±10%


def test_security_history_is_observed_not_announced(settings):
    row = build(settings, {}).security_history("SH600895", "ST张江")[0]
    assert row["risk_warning"] is True
    assert row["observed"] is True and row["announcement_verified"] is True
    assert row["end"] is None


def test_security_history_declares_how_far_back_the_observation_reaches(settings):
    """观测日是契约的一部分：状态只能从它自己那天起算，所以回填必须自报滞后。

    `build_generation` 只接受 `effective_day` 覆盖决策日的状态。月度全表刷新会把最新状态钉在当月
    1 日，而 1 日 18:30 之前的决策日还是上月最后一个交易日 —— 那个窗口里没有任何状态覆盖它，覆盖率
    会**每月归零一次**。修法是让调度器按决策日观测；这条用例锁定「回填必须自报滞后」这个前提，
    免得日后有人把回填当成当日观测。
    """
    feed = build(settings, {})
    today = now().astimezone(CN).date()
    same_day = feed.security_history("SH600895", "张江高科", datetime.combine(today, time(15), CN))[0]
    assert same_day["start"] == str(today)
    assert same_day["observed_lag_days"] == 0 and same_day["backfilled"] is False

    earlier = today - timedelta(days=3)
    backfilled = feed.security_history("SH600895", "张江高科", datetime.combine(earlier, time(15), CN))[0]
    assert backfilled["start"] == str(earlier)
    assert backfilled["observed_lag_days"] == 3 and backfilled["backfilled"] is True
    assert backfilled["available_at"].date() == earlier


def test_minute_bars_reject_off_grid_and_auction_rows():
    bar = {"time": "2026-09-21 10:00:00", "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0, "volume": 1, "turnover": 1}
    assert Market._minute_bar("SH600895", {**bar, "time": "2026-09-21 09:30:00"}, now()) is None  # 集合竞价不合成
    with pytest.raises(ProviderError):
        Market._minute_bar("SH600895", {**bar, "time": "2026-09-21 10:01:00"}, now())
    with pytest.raises(ProviderError):
        Market._minute_bar("SH600895", {**bar, "volume": None}, now())


# ------------------------------------------------------------------ transport hardening


@pytest.mark.parametrize("status,error", [(403, PermissionDenied), (429, Deferred), (302, ProviderError)])
def test_transport_status_handling(status, error):
    def route(request):
        return httpx.Response(status, headers={"Location": "http://untrusted.test"} if status == 302 else {})

    transport = Transport(
        client=httpx.Client(transport=httpx.MockTransport(route)),
        sleep=lambda _: None,
    )
    with pytest.raises(error):
        transport.get("https://push2his.eastmoney.com/api")


def test_transport_refuses_plain_http():
    transport = Transport(client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200))))
    with pytest.raises(ProviderError, match="HTTPS"):
        transport.get("http://push2his.eastmoney.com/api")


def test_transport_retries_then_reports_a_bounded_failure():
    attempts = []
    transport = Transport(
        client=httpx.Client(transport=httpx.MockTransport(lambda request: attempts.append(request) or httpx.Response(500))),
        sleep=lambda _: None,
    )
    with pytest.raises(ProviderError):
        transport.get("https://push2his.eastmoney.com/api")
    assert len(attempts) == 3


def test_probe_reports_every_source_without_raising(settings):
    def route(request):
        if "push2his" in str(request.url):
            return httpx.Response(200, json=eastmoney_kline(DAY, 10.5))
        return httpx.Response(500)

    market = build(settings, {HOSTS["eastmoney"]: route})
    result = market.probe()
    assert set(result) == {"pytdx", "eastmoney", "sina", "tencent"}
    assert result["eastmoney"]["reachable"] is True
    assert result["pytdx"]["reachable"] is False and result["sina"]["reachable"] is False


# ------------------------------------------------------------------ configuration contract


def test_no_vendor_token_is_configured_anywhere(settings):
    assert settings.provider == "market"
    assert settings.history_scope == "index"
    assert not any("tushare" in name.lower() for name in Settings.model_fields)
    with pytest.raises(ValueError):
        Settings(api_token="a" * 32, environment="test", provider="tushare")
    with pytest.raises(ValueError):
        Settings(api_token="a" * 32, environment="test", history_scope="everything")


def test_market_without_a_database_has_no_quota_accounting(settings):
    assert build(settings, {}).budget is None
