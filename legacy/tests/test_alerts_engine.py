# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import sqlite3
from datetime import timedelta

import pytest

from legacy_monitor import AlertSettings, AlertStore, Basket, DemoProvider, FeedError, MonitorEngine
from legacy_monitor.models import Snapshot


class Provider:
    mode = "live"
    source = "test"

    def __init__(self, clock, quotes):
        self.clock, self.quotes = clock, quotes
        self.failure = False
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.failure:
            raise FeedError("incomplete pagination")
        return Snapshot(tuple(self.quotes), self.source, self.mode, self.clock(), len(self.quotes))


def test_alert_entry_exit_cooldown_and_persistence(tmp_path, clock):
    path = tmp_path / "events.sqlite3"
    store = AlertStore(path, clock)

    def observe(value, identity, mode="live", scope="config"):
        return store.evaluate([("stock", "change", value, 0.03, identity)], "test", mode, scope, clock(), 300)

    assert len(observe(0.04, "a")) == 1
    assert observe(0.04, "a") == []
    clock.advance(600)
    assert observe(0.05, "b") == []
    assert observe(0, "c") == []
    assert len(observe(0.04, "d")) == 1
    assert observe(0, "e") == []
    clock.advance(1)
    assert observe(0.04, "f") == []
    clock.advance(600)
    assert observe(0.04, "g") == []
    observe(0, "h")
    assert len(observe(0.04, "i")) == 1
    store = AlertStore(path, clock)
    assert observe(0.04, "j") == []
    assert len(store.recent("live")) == 3
    assert len(observe(0.04, "j", mode="demo")) == 1
    assert len(observe(0.04, "j", scope="new-settings")) == 1
    assert len(store.recent("demo")) == 1
    assert len(store.recent("live", "STOCK")) == 4
    assert store.recent("live", "stock%") == []
    clock.advance(31 * 86400)
    assert store.recent("live") == []


def test_store_rollback(tmp_path, clock):
    store = AlertStore(tmp_path / "events.sqlite3", clock)
    # A malformed second observation rolls back the first event and its state.
    with pytest.raises(ValueError):
        store.evaluate([("x", "change", 1, 0.1, "a"), ("bad",)], "test", "live", "scope", clock(), 300)
    assert store.recent("live") == []
    assert len(store.evaluate([("x", "change", 1, 0.1, "a")], "test", "live", "scope", clock(), 300)) == 1


def test_engine_backoff_retains_snapshot_and_no_duplicate_alerts(tmp_path, clock, make_quote):
    feed = Provider(clock, [make_quote()])
    store = AlertStore(tmp_path / "events.sqlite3", clock)
    engine = MonitorEngine(feed, store, clock=clock, monotonic=clock.monotonic)
    result = engine.poll()
    first = result.snapshot
    assert len(result.events) == 3  # stock, combined main board, Shanghai main board
    assert len(engine.samples) == 1
    engine.poll(force=True)
    assert feed.calls == 1
    clock.advance(31)
    engine.poll()
    assert feed.calls == 2
    assert len(store.recent("live")) == 3
    assert len(engine.samples) == 1
    assert engine.result.summaries["Main Board"].volume == 100
    successful = result.snapshot
    feed.failure = True
    clock.advance(31)
    result = engine.poll()
    assert result.snapshot is successful
    assert result.error and not result.events
    assert engine.next_poll == clock.monotonic() + 60
    engine.poll(force=True)
    assert feed.calls == 3
    clock.advance(60)
    feed.failure = False
    feed.quotes = [make_quote(change=0)]
    assert engine.poll().error is None
    assert first is not engine.result.snapshot


@pytest.mark.parametrize("age", [121, -6, 86400, None])
def test_stale_and_missing_source_times_never_alert(tmp_path, clock, make_quote, age):
    stamp = clock() - timedelta(seconds=age) if age is not None else None
    feed = Provider(clock, [make_quote(timestamp=stamp)])
    engine = MonitorEngine(feed, AlertStore(tmp_path / "events.sqlite3", clock), clock=clock, monotonic=clock.monotonic)
    result = engine.poll()
    assert result.events == []
    assert result.summaries["Main Board"].coverage == 0
    assert not engine.samples


def test_missing_observation_does_not_rearm(tmp_path, clock, make_quote):
    feed = Provider(clock, [make_quote()])
    store = AlertStore(tmp_path / "events.sqlite3", clock)
    engine = MonitorEngine(feed, store, clock=clock, monotonic=clock.monotonic)
    engine.poll()
    clock.advance(400)
    feed.quotes = [make_quote(change=0, timestamp=None)]
    engine.poll()
    clock.advance(31)
    feed.quotes = [make_quote()]
    assert not engine.poll().events
    assert len(store.recent("live")) == 3


def test_missing_basket_coverage_suppresses_alerts(tmp_path, clock, make_quote):
    feed = Provider(clock, [make_quote()])
    basket = Basket("custom", (("SH600000", 1), ("SZ000001", 1)))
    engine = MonitorEngine(
        feed, AlertStore(tmp_path / "events.sqlite3", clock), (basket,), clock=clock, monotonic=clock.monotonic
    )
    result = engine.poll()
    assert result.summaries["custom"].coverage == 0.5
    assert all(event["target"] != "custom" for event in result.events)


def test_view_recalculates_staleness_without_alerts(tmp_path, clock, make_quote):
    engine = MonitorEngine(
        Provider(clock, [make_quote()]),
        AlertStore(tmp_path / "events.sqlite3", clock),
        clock=clock,
        monotonic=clock.monotonic,
    )
    engine.poll()
    clock.advance(121)
    assert engine.view().summaries["Main Board"].eligible == 0
    assert len(engine.store.recent("live")) == 3


def test_closed_market_and_latest_dated_snapshot(tmp_path, clock, make_quote):
    clock.now = clock.now.replace(hour=16)
    quotes = [
        make_quote(timestamp=clock().replace(hour=15)),
        make_quote("SZ000001", timestamp=clock() - timedelta(days=1)),
    ]
    engine = MonitorEngine(
        Provider(clock, quotes), AlertStore(tmp_path / "events.sqlite3", clock), clock=clock, monotonic=clock.monotonic
    )
    result = engine.poll()
    assert result.session == "Closed"
    assert not result.events
    assert result.summaries["Main Board"].eligible == 1
    assert not engine.samples


def test_date_and_membership_reset_chart(tmp_path, clock, make_quote):
    feed = Provider(clock, [make_quote()])
    engine = MonitorEngine(feed, AlertStore(tmp_path / "events.sqlite3", clock), clock=clock, monotonic=clock.monotonic)
    engine.poll()
    clock.advance(31)
    feed.quotes = [make_quote()]
    engine.poll()
    assert len(engine.samples) == 2
    clock.advance(86400)
    feed.quotes = [make_quote()]
    engine.poll()
    assert len(engine.samples) == 1
    clock.advance(31)
    feed.quotes.append(make_quote("SZ000001"))
    engine.poll()
    assert len(engine.samples) == 1


def test_storage_failure_is_visible_and_recoverable(tmp_path, clock, make_quote, monkeypatch):
    store = AlertStore(tmp_path / "events.sqlite3", clock)
    engine = MonitorEngine(Provider(clock, [make_quote()]), store, clock=clock, monotonic=clock.monotonic)
    evaluate = store.evaluate

    def fail(*args):
        raise sqlite3.OperationalError("read-only database")

    monkeypatch.setattr(store, "evaluate", fail)
    result = engine.poll()
    assert result.snapshot is not None
    assert result.storage_error and not result.events
    monkeypatch.setattr(store, "evaluate", evaluate)
    clock.advance(31)
    assert engine.poll().events


def test_demo_modes_and_day_rollover(tmp_path):
    store = AlertStore(tmp_path / "events.sqlite3")
    engine = MonitorEngine(DemoProvider(), store)
    for _ in range(181):
        engine.poll(force=True)
    assert len(engine.samples) == 1
    assert store.recent("demo")
    assert store.recent("live") == []


def test_chart_history_is_bounded(tmp_path, clock, make_quote):
    feed = Provider(clock, [make_quote()])
    engine = MonitorEngine(feed, AlertStore(tmp_path / "events.sqlite3", clock), clock=clock, monotonic=clock.monotonic)
    for _ in range(605):
        clock.tick += 31
        clock.now += timedelta(milliseconds=100)
        feed.quotes = [make_quote()]
        engine.poll()
    assert len(engine.samples) == 600


def test_turnover_alert_coverage(tmp_path, clock, make_quote):
    quotes = [make_quote(turnover=None), make_quote("SZ000001", turnover=5000)]
    engine = MonitorEngine(
        Provider(clock, quotes),
        AlertStore(tmp_path / "events.sqlite3", clock),
        settings=AlertSettings(turnover=1000),
        clock=clock,
        monotonic=clock.monotonic,
    )
    events = engine.poll().events
    turnover = [event["target"] for event in events if event["rule"] == "cumulative_turnover_cny"]
    assert "SZ000001" in turnover
    assert "Main Board" not in turnover
    assert "SH600000" not in turnover


def test_unavailable_qlib_basket_rejected():
    with pytest.raises(ValueError, match="Qlib"):
        MonitorEngine(DemoProvider(), None, (Basket("index", (), "csi300"),))


def test_engine_notifier_delivers_events_and_survives_failure(tmp_path, clock, make_quote):
    received = []

    engine = MonitorEngine(
        Provider(clock, [make_quote()]),
        AlertStore(tmp_path / "events.sqlite3", clock),
        clock=clock,
        monotonic=clock.monotonic,
        notifier=received.extend,
    )
    result = engine.poll()
    assert result.events
    assert received == result.events
    assert result.notify_error is None

    def fail(events):
        raise RuntimeError("smtp unavailable")

    engine = MonitorEngine(
        Provider(clock, [make_quote()]),
        AlertStore(tmp_path / "notify-fail.sqlite3", clock),
        clock=clock,
        monotonic=clock.monotonic,
        notifier=fail,
    )
    result = engine.poll()
    assert result.snapshot is not None
    assert result.events
    assert "smtp unavailable" in result.notify_error
