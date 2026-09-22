from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from quant_platform.analysis import basket_summary, indicators, screen
from quant_platform.config import Settings
from quant_platform.domain import BasketInput, CN, board, fresh, session, source_time, symbol


@pytest.mark.parametrize(
    "code,expected",
    [
        ("600895.SH", "Main Board"),
        ("SZ300001", "ChiNext"),
        ("688001", "STAR Market"),
        ("SH689009", "STAR Market"),
        ("000001", "Main Board"),
        ("SZ301001", "ChiNext"),
    ],
)
def test_identity(code, expected):
    assert board(symbol(code)) == expected


@pytest.mark.parametrize("code", [1234, "600895.SZ", "SH000001", "BJ920001", "SH６００８９５", "SZ159001", "SH900001"])
def test_invalid_identity(code):
    with pytest.raises(ValueError):
        symbol(code)


@pytest.mark.parametrize("members", [{"600895.SH": 1, "SH600895": 2}, {"SH600895": -1}, {"SH600895": float("nan")}, {}])
def test_basket_validation(members):
    with pytest.raises(ValueError):
        BasketInput(name="basket", members=members)


def test_basket_normalizes_weights():
    item = BasketInput(name="Growth", members={"SH600895": 2, "SZ000001": 1})
    assert sum(item.members.values()) == pytest.approx(1)


@pytest.mark.parametrize("provider", ["demo", "tencent", "eastmoney"])
def test_no_public_production_provider(provider):
    with pytest.raises(ValueError):
        Settings(provider=provider)


def test_source_time_never_fabricates_date():
    assert source_time("10:30:00") is None
    assert source_time("2026-09-22 10:30:00").tzinfo == CN
    at = datetime(2026, 9, 22, 10, tzinfo=CN)
    assert fresh(at - timedelta(seconds=120), at)
    assert not fresh(at - timedelta(seconds=121), at)
    assert not fresh(at + timedelta(seconds=6), at)
    assert session(at, None) == ("unverified", False)
    assert session(at, False) == ("closed", False)
    assert session(at, True)[1]
    assert not session(at.replace(hour=12), True)[1]


def test_native_indicators(history_rows):
    result = indicators(history_rows)
    assert result["rsi14"] == 100
    assert result["atr14"] == pytest.approx(2)
    assert result["return20"] == pytest.approx(11.39 / 11.19 - 1)
    assert result["sma5"] == pytest.approx(11.37)
    assert result["volume_ratio20"] == 1
    assert result["drawdown60"] == 0
    assert result["average_turnover20"] == 30_000_000


def test_corporate_action_replacement(history_rows):
    original = indicators(history_rows)
    revised = deepcopy(history_rows)
    for row in revised[:70]:
        row["factor"] = 0.5
        row["factor_dataset_id"] += 10000
    result = indicators(revised)
    assert result["input_hash"] != original["input_hash"]
    assert result["average_volume20"] == original["average_volume20"]
    revised[-1]["factor"] = None
    assert indicators(revised)["status"] == "insufficient_data"
    assert indicators(history_rows[:5])["return20"] is None


def test_screen_deterministic_and_explicit(history_rows):
    metric = indicators(history_rows)
    day = history_rows[-1]["day"]
    stocks = {"SH600895": {"name": "Normal"}, "SZ000001": {"name": "ST risky"}}
    report = screen(stocks, {code: metric for code in stocks}, day)
    assert report["candidates"][0]["symbol"] == "SH600895"
    assert "name_based_risk_filter" in report["excluded"]["SZ000001"]
    assert not report["automatic_basket_changes"]


def test_stale_missing_references_do_not_rearm_and_poll_times_not_identity():
    at = datetime(2026, 9, 22, 10, tzinfo=CN)
    quote = {
        "close": 11,
        "pre_close": 10,
        "source_time": at.isoformat(),
        "received_at": at.isoformat(),
        "reference_verified": True,
    }
    quotes = {"SH600895": quote}
    result = basket_summary({"SH600895": 1, "SZ000001": 1}, quotes, at)
    assert result["coverage"] == 0.5
    assert result["daily_return"] == pytest.approx(0.1)
    quote["received_at"] = (at + timedelta(seconds=20)).isoformat()
    assert basket_summary({"SH600895": 1, "SZ000001": 1}, quotes, at)["observation"] == result["observation"]
    quote["reference_verified"] = False
    assert basket_summary({"SH600895": 1}, quotes, at)["daily_return"] is None
