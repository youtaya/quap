from datetime import datetime, timedelta

import pytest

from quant_platform.analysis import basket_summary
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


@pytest.mark.parametrize("provider", ["demo", "tushare", "adata", ""])
def test_only_the_tokenless_public_market_provider_is_accepted(provider):
    with pytest.raises(ValueError):
        Settings(provider=provider)


@pytest.mark.parametrize("scope", ["everything", "csi300", ""])
def test_history_scope_is_explicit(scope):
    with pytest.raises(ValueError):
        Settings(history_scope=scope)


def test_public_market_provider_is_the_default():
    settings = Settings()
    assert settings.provider == "market"
    assert settings.history_scope == "index"
    assert not hasattr(settings, "tushare_token")


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
