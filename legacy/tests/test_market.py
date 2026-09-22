# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from datetime import date, timedelta

import pytest

from legacy_monitor.market import TradingCalendar, market_members, parse_baskets, summarize
from legacy_monitor.models import AlertSettings, Basket, classify_board, normalize_symbol


@pytest.mark.parametrize(
    "symbol, expected, board",
    [
        ("688001.sh", "SH688001", "STAR Market"),
        ("SH689009", "SH689009", "STAR Market"),
        ("sz300001", "SZ300001", "ChiNext"),
        ("301001", "SZ301001", "ChiNext"),
        ("600000", "SH600000", "Main Board"),
        ("601001", "SH601001", "Main Board"),
        ("603001", "SH603001", "Main Board"),
        ("605001", "SH605001", "Main Board"),
        ("000001", "SZ000001", "Main Board"),
        ("001001", "SZ001001", "Main Board"),
        ("002001", "SZ002001", "Main Board"),
        ("003001", "SZ003001", "Main Board"),
    ],
)
def test_board_classification(symbol, expected, board):
    assert normalize_symbol(symbol) == expected
    assert classify_board(expected) == board


@pytest.mark.parametrize("symbol", [1, "1", "SH000001", "SZ688001", "BJ830001", "SH900001", "SZ200001", "SH510300"])
def test_unsupported_symbols(symbol):
    with pytest.raises(ValueError):
        normalize_symbol(symbol)


@pytest.mark.parametrize(
    "config",
    [
        [],
        {"x": {"symbols": []}},
        {"x": {"symbols": ["000001", "SZ000001"]}},
        {"x": {"symbols": ["000001"], "weights": [0]}},
        {"x": {"symbols": ["000001"], "weights": [float("nan")]}},
        {"x": {"symbols": ["000001"], "weights": [float("inf")]}},
        {"x": {"symbols": ["000001"], "weights": []}},
        {"x": {"symbols": ["000001"], "universe": "all"}},
        {"x": {"universe": ""}},
        {"x": {"symbols": [1]}},
        {"Main Board": {"symbols": ["000001"]}},
        {"x": {"symbol": "000001"}},
    ],
)
def test_invalid_baskets(config):
    with pytest.raises(ValueError):
        parse_baskets(config)


def test_basket_defaults():
    baskets = parse_baskets({"test": {"symbols": ["000001", "688001"]}})
    assert baskets[0].members == (("SZ000001", 1.0), ("SH688001", 1.0))
    assert parse_baskets({"index": {"universe": "csi300"}})[0].universe == "csi300"


@pytest.mark.parametrize(
    "settings",
    [
        {"stock_change": 0},
        {"cooldown": -1},
        {"minimum_coverage": 1.1},
        {"turnover": float("inf")},
        {"basket_change": float("nan")},
    ],
)
def test_invalid_alert_settings(settings):
    with pytest.raises(ValueError):
        AlertSettings(**settings)


def test_weighted_summary_and_missing_data(clock, make_quote):
    first = make_quote(change=0.1)
    second = make_quote("SZ000001", change=-0.1)
    members = ((first.symbol, 3.0), (second.symbol, 1.0))
    quotes = {q.symbol: q for q in (first, second)}
    summary = summarize("test", members, quotes, clock())
    assert summary.daily_return == pytest.approx(0.05)
    assert summary.proxy == pytest.approx(1050)
    assert (summary.advancing, summary.declining, summary.unchanged) == (1, 1, 0)
    assert summary.volume == 200
    assert summary.turnover == 2000
    assert summary.coverage == 1
    partial = summarize("test", members, {first.symbol: first}, clock())
    assert partial.coverage == 0.75
    assert partial.daily_return == pytest.approx(0.1)
    assert partial.total == 2
    stale = summarize("test", members, quotes, clock() + timedelta(seconds=121))
    assert stale.proxy is None
    assert stale.volume is None
    assert stale.coverage == 0
    assert summarize("empty", (), {}, clock()).proxy is None


def test_missing_turnover_coverage(clock, make_quote):
    quote = make_quote(turnover=None)
    summary = summarize("x", ((quote.symbol, 1.0),), {quote.symbol: quote}, clock())
    assert summary.coverage == 1
    assert summary.turnover_coverage == 0
    assert summary.turnover is None


@pytest.mark.parametrize("age, valid", [(0, True), (120, True), (121, False), (-5, True), (-6, False), (86400, False)])
def test_freshness(clock, make_quote, age, valid):
    assert make_quote(timestamp=clock() - timedelta(seconds=age)).fresh(clock()) is valid


def test_missing_naive_time_and_invalid_prices(clock, make_quote):
    assert not make_quote(timestamp=None).fresh(clock())
    assert not make_quote(timestamp=clock().replace(tzinfo=None)).fresh(clock())
    for values in ({"last": 0}, {"last": float("inf")}, {"previous_close": 0}, {"previous_close": None}):
        assert make_quote(**values).daily_return is None


@pytest.mark.parametrize(
    "hour,minute,second,session,active",
    [
        (9, 29, 59, "Pre-open", False),
        (9, 30, 0, "Morning session", True),
        (11, 30, 0, "Morning session", True),
        (11, 30, 1, "Lunch break", False),
        (12, 59, 59, "Lunch break", False),
        (13, 0, 0, "Afternoon session", True),
        (15, 0, 0, "Afternoon session", True),
        (15, 0, 1, "Closed", False),
    ],
)
def test_session_boundaries(clock, hour, minute, second, session, active):
    now = clock().replace(hour=hour, minute=minute, second=second)
    assert TradingCalendar().session(now) == (session, "unverified", active)


def test_holidays_weekends_and_calendar_coverage(clock):
    calendar = TradingCalendar([date(2026, 9, 11), date(2026, 9, 15)])
    assert calendar.session(clock()) == ("Closed", "verified", False)
    assert calendar.session(clock() + timedelta(days=1)) == ("Morning session", "verified", True)
    assert calendar.session(clock() + timedelta(days=2)) == ("Morning session", "unverified", True)
    assert TradingCalendar().session(clock() + timedelta(days=5)) == ("Closed", "unverified", False)


def test_full_board_membership(make_quote):
    quotes = {
        q.symbol: q
        for q in [make_quote("SH688001"), make_quote("SZ300001"), make_quote("SH600000"), make_quote("SZ000001")]
    }
    groups = market_members(quotes, (Basket("mine", (("SZ000001", 2),)),))
    assert len(groups["Main Board"]) == 2
    assert len(groups["Shanghai Main Board"]) == 1
    assert len(groups["Shenzhen Main Board"]) == 1
    assert groups["mine"] == (("SZ000001", 2),)
