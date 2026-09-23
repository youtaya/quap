"""Personal research path: point-in-time filters, factor diagnostics and a non-NAV backtest."""

from datetime import date, timedelta

from quant_platform.analysis import screen
from quant_platform.analysis.research import evaluate
from quant_platform.domain import ScreenRule
from quant_platform.operations import copy_verified_replica, recovery_times


def _market():
    start = date(2026, 1, 5)
    days, cursor = [], start
    while len(days) < 50:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    codes = [f"SH60000{i}" for i in range(1, 7)]
    series, instruments = {}, {}
    for index, code in enumerate(codes):
        bars = {}
        for step, day in enumerate(days):
            close = 10 + index + step * (0.02 + index * 0.01)
            bars[day] = {
                "close": close,
                "factor": 1.0,
                "turnover": 30_000_000 + index,
                "volume": 1_000_000,
                "limit_up": close * 1.2,
                "limit_down": close * 0.8,
            }
        series[code] = bars
        instruments[code] = {"status": "L", "name": "Normal", "list_date": days[0]}
    instruments["SH600001"]["list_date"] = days[-1]
    return series, days, instruments


def test_research_hash_is_stable_and_withholds_unknown_limits():
    series, days, instruments = _market()
    rule = ScreenRule(top_n=2, minimum_bars=61)
    first = evaluate(series, days, instruments, set(), rule, limit_known=False)
    second = evaluate(series, days, instruments, set(), rule, limit_known=False)
    assert first["research_hash"] == second["research_hash"]
    assert first["execution"] == "withheld_limit_unknown"
    assert first["research_value"] is None
    assert "nav" not in first
    assert first["label"] == "Research path value, not NAV"
    assert first["automatic_orders"] is False
    assert "SH600001" not in {row["symbol"] for row in first["candidates"]}
    assert first["factors"]["return20"]["status"] == "complete"
    assert first["benchmark_status"] == "unavailable"


def test_known_limits_trade_in_lots_without_calling_the_result_nav():
    series, days, instruments = _market()
    signal_day = days[29]
    result = evaluate(series, days, instruments, {("SH600002", signal_day)}, ScreenRule(top_n=2), limit_known=True)
    assert result["execution"] == "simulated"
    assert result["trades"]
    assert all(trade["shares"] % 100 == 0 for trade in result["trades"])
    assert "SH600002" not in {row["symbol"] for row in result["candidates"]}
    assert result["research_value"] is not None
    assert "nav" not in result


def test_screen_uses_listing_and_suspension_flags():
    day = date(2026, 9, 22)
    metric = {
        "status": "complete",
        "as_of": str(day),
        "bars": 140,
        "return20": 0.1,
        "volatility20": 0.2,
        "average_turnover20": 30_000_000,
    }
    stocks = {
        "SH600895": {"name": "Normal", "status": "L", "list_date": day, "suspended": True},
        "SZ000001": {"name": "Gone", "status": "L", "list_date": day, "delist_date": day},
    }
    report = screen(stocks, {code: metric for code in stocks}, day)
    assert "suspended" in report["excluded"]["SH600895"]
    assert "delisted" in report["excluded"]["SZ000001"]


def test_replica_is_a_different_directory_and_recovery_times_are_differences(tmp_path):
    source = tmp_path / "primary" / "quant-20260101T000000.dump"
    source.parent.mkdir()
    source.write_bytes(b"dump-bytes")
    import hashlib

    checksum = hashlib.sha256(b"dump-bytes").hexdigest()
    copied = copy_verified_replica(source, tmp_path / "offsite", checksum)
    assert (tmp_path / "offsite" / source.name).read_bytes() == b"dump-bytes"
    assert copied["sha256"] == checksum
    try:
        copy_verified_replica(source, source.parent, checksum)
    except ValueError:
        pass
    else:
        raise AssertionError("replica directory must differ")
    backup_at = date(2026, 1, 1)
    from datetime import datetime, timezone

    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fault = start + timedelta(hours=2)
    finished = fault + timedelta(minutes=5)
    timing = recovery_times(start, fault, finished)
    assert timing["rpo_seconds"] == 7200
    assert timing["rto_seconds"] == 300
    assert backup_at
