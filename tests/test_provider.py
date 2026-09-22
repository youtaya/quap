import json
from datetime import date

import httpx
import pytest

from quant_platform.providers import Deferred, PermissionDenied, ProviderError
from quant_platform.providers.tushare import FIELDS, Tushare, parse_bar, parse_basic, parse_quote


def row(code="600895.SH"):
    return {
        "ts_code": code,
        "trade_date": "20260921",
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10,
        "pre_close": 10,
        "vol": 123,
        "amount": 456,
        "trade_time": "2026-09-22 10:00:00",
    }


@pytest.mark.parametrize("code", ["600895.SH", "688001.SH", "689009.SH", "300001.SZ", "000001.SZ"])
def test_endpoint_specific_units(code):
    daily, quote = parse_bar(row(code)), parse_quote(row(code))
    assert daily["volume"] == 12300
    assert daily["turnover"] == 456000
    assert quote["volume"] == 123
    assert quote["turnover"] == 456
    assert not quote["reference_verified"]


def test_missing_values_not_zeros():
    raw = row()
    raw.update(vol="-", amount=None, trade_time="10:00:00", close=0)
    quote = parse_quote(raw)
    assert all(quote[key] is None for key in ("volume", "turnover", "source_time", "close"))
    with pytest.raises(ProviderError):
        parse_bar(raw)
    basic = parse_basic({"ts_code": "600895.SH", "trade_date": "20260921", "pe_ttm": None, "pb": "-", "pe": ""})
    assert basic["pe_ttm"] is None and basic["pb"] is None and basic["pe"] is None
    assert "industry" in FIELDS["stock_basic"]
    assert parse_basic({**basic, "ts_code": "600895.SH", "trade_date": "20260921", "pe_ttm": 12.3, "pb": 1.2})[
        "pe_ttm"
    ] == pytest.approx(12.3)


def transport(settings, handler):
    return Tushare(settings, client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda _: None)


def test_https_no_redirect_or_credential_leak(settings):
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://api.tushare.pro"
        return httpx.Response(302, headers={"Location": "http://untrusted.test"})

    feed = transport(settings, handler)
    with pytest.raises(ProviderError) as error:
        feed.request("daily", {})
    assert len(requests) == 3
    assert "test-only-fixture" not in str(error.value)


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"code": 2002, "msg": "permission"}, PermissionDenied),
        ({"code": 1, "msg": "每分钟"}, Deferred),
        ({"code": 0, "data": {}}, ProviderError),
    ],
)
def test_permission_rate_schema_failures(settings, payload, error):
    feed = transport(settings, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(error):
        feed.request("daily", {})


def test_explicit_timestamp_field_and_complete_identity(settings):
    def handler(request):
        body = json.loads(request.content)
        assert "trade_time" in body["fields"]
        fields = FIELDS["rt_k"].split(",")
        return httpx.Response(
            200, json={"code": 0, "data": {"fields": fields, "items": [[row().get(f) for f in fields]]}}
        )

    feed = transport(settings, handler)
    assert len(feed.quotes(["SH600895"])) == 1
    with pytest.raises(ProviderError):
        feed.quotes(["SH600895", "SZ000001"])


def test_request_cap_partitions_using_documented_single_symbol(settings, monkeypatch):
    feed = transport(settings, lambda _: httpx.Response(500))
    calls = []

    def request(endpoint, params):
        calls.append(params)
        return [row()] * 6000 if "ts_code" not in params else [row(params["ts_code"])]

    monkeypatch.setattr(feed, "request", request)
    data = feed.daily_partition("daily", date(2026, 9, 21), {"SH600895", "SZ000001"})
    assert len(calls) == 3 and len(data) == 2
    calls.clear()

    def basics(endpoint, params):
        calls.append((endpoint, params.get("ts_code")))
        item = {**row(params.get("ts_code", "600895.SH")), "pe_ttm": 10, "pb": 1}
        return [item] * 6000 if "ts_code" not in params else [item]

    monkeypatch.setattr(feed, "request", basics)
    valued = feed.daily_partition("daily_basic", date(2026, 9, 21), {"SH600895", "SZ000001"})
    assert len(valued) == 2 and all(item["pe_ttm"] == 10 for item in valued)


def test_credential_absence_blocks_without_network(settings):
    settings.tushare_token = __import__("pydantic").SecretStr("")
    feed = transport(settings, lambda _: pytest.fail("Must not call provider without credentials"))
    with pytest.raises(PermissionDenied):
        feed.request("daily", {})
