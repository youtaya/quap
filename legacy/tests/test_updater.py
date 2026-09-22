# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import json
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest
from filelock import FileLock, Timeout

from legacy_monitor.models import SHANGHAI
from legacy_monitor.state import RuntimeStore
from legacy_monitor.updater import HistoryUpdater, ManagedDataset, completed_cutoff, next_update

SETTINGS = {"enabled": True, "bars": 500, "update_at": "18:30"}
DAYS = ["2026-09-10", "2026-09-11"]


def bars(days=DAYS, close=10):
    return [[day, close, close, close, close, 100] for day in days]


class DailyFeed:
    def __init__(self, stock_rows=None, calendar=None):
        self.http = Mock()
        self.rows = stock_rows if stock_rows is not None else {"SH600895": bars()}
        self.calendar = calendar or bars()
        self.calls = []

    def fetch(self, symbol, cutoff, count, benchmark=False):
        self.calls.append((symbol, cutoff, count, benchmark))
        result = self.calendar if benchmark else self.rows[symbol]
        if isinstance(result, Exception):
            raise result
        return [row for row in result if date.fromisoformat(row[0]) <= cutoff][-count:]


@pytest.fixture
def store(tmp_path, clock):
    state = RuntimeStore(tmp_path / "alerts.sqlite3")
    state.set("directory", {"symbols": {"SH600895": "Example"}, "fetched_at": clock().isoformat()})
    return state


@pytest.mark.parametrize(
    "stamp,expected",
    [
        ("2026-09-14T10:00:00", "2026-09-13"),
        ("2026-09-14T18:29:59", "2026-09-13"),
        ("2026-09-14T18:30:00", "2026-09-14"),
        ("2026-09-19T09:00:00", "2026-09-18"),
    ],
)
def test_completed_cutoff(stamp, expected):
    now = datetime.fromisoformat(stamp).replace(tzinfo=SHANGHAI)
    assert str(completed_cutoff(now)) == expected
    assert next_update(now) > now


def test_two_generations_read_through_real_qlib(store, clock):
    first = HistoryUpdater(store, SETTINGS, DailyFeed(), clock).run()
    assert first["status"] == "complete"
    assert first["successful"] == 1
    dataset = ManagedDataset(store.root)
    old = dataset.current()
    assert dataset.features("SH600895")["$close"].tolist() == [10, 10]
    clock.now = datetime(2026, 9, 14, 19, tzinfo=SHANGHAI)
    rows = bars([*DAYS, "2026-09-14"], close=20)
    second = HistoryUpdater(store, SETTINGS, DailyFeed({"SH600895": rows}, rows), clock).run()
    assert second["generation"] != first["generation"]
    assert old.exists()
    assert dataset.features("SH600895")["$close"].tolist() == [20, 20, 20]
    assert dataset.manifest()["volume_unit"] == "shares"
    assert dataset.manifest()["coverage"]["SH600895"]["stored_bars"] == 3
    # Revisions replace complete adjusted windows, rather than appending 20 to old 10s.
    assert json.loads(store.window("SH600895")["rows_json"])[0][2] == 20


def test_same_target_restart_skips_completed_symbols(store, clock):
    feed = DailyFeed()
    HistoryUpdater(store, SETTINGS, feed, clock).run()
    resumed = DailyFeed()
    result = HistoryUpdater(store, SETTINGS, resumed, clock).run()
    assert all(call[3] for call in resumed.calls)
    assert result["status"] == "complete"


def test_failed_new_symbol_is_absent_and_old_window_is_preserved(store, clock):
    HistoryUpdater(store, SETTINGS, DailyFeed(), clock).run()
    old_window = store.window("SH600895")["rows_json"]
    store.set("directory", {"symbols": {"SH600895": "old", "SZ000001": "new"}, "fetched_at": clock().isoformat()})
    clock.now = datetime(2026, 9, 14, 19, tzinfo=SHANGHAI)
    feed = DailyFeed({"SH600895": ValueError("offline"), "SZ000001": []}, bars([*DAYS, "2026-09-14"]))
    result = HistoryUpdater(store, SETTINGS, feed, clock).run()
    assert result["status"] == "degraded"
    assert result["failed"] == 2
    assert result["unavailable"] == result["lagging"] == 1
    assert store.window("SH600895")["rows_json"] == old_window
    assert store.window("SZ000001")["rows_json"] is None
    assert ManagedDataset(store.root).features("SH600895")["$close"].dropna().tolist() == [10, 10]
    assert datetime.fromisoformat(result["next_update"]) == clock() + timedelta(minutes=30)


def test_interruption_resumes_at_symbol_boundary(store, clock, monkeypatch):
    store.set("directory", {"symbols": {"SH600895": "first", "SZ000001": "second"}, "fetched_at": clock().isoformat()})
    feed = DailyFeed({"SH600895": bars(), "SZ000001": bars()})
    updater = HistoryUpdater(store, SETTINGS, feed, clock, stopped=lambda: store.window("SH600895") is not None)
    assert updater.run() is None
    assert store.get("history")["status"] == "interrupted"
    assert store.window("SZ000001") is None
    resumed = DailyFeed({"SZ000001": bars()})
    result = HistoryUpdater(store, SETTINGS, resumed, clock).run()
    assert result["successful"] == 2
    assert [call[0] for call in resumed.calls if not call[3]] == ["SZ000001"]


def test_crash_before_publication_preserves_current_and_external_files(store, clock, tmp_path):
    HistoryUpdater(store, SETTINGS, DailyFeed(), clock).run()
    dataset = ManagedDataset(store.root)
    old = dataset.current()
    external = tmp_path / "research"
    external.mkdir()
    protected = external / "keep.txt"
    protected.write_text("untouched")

    def fail(*args):
        raise ValueError("validation failed")

    with pytest.raises(ValueError, match="validation"):
        dataset.publish(store.windows(), {"SH600895": "Example"}, DAYS, {}, validator=fail)
    assert dataset.current() == old
    assert protected.read_text() == "untouched"
    assert dataset.features("SH600895")["$close"].tolist() == [10, 10]


def test_unowned_and_symlink_roots_are_rejected(tmp_path):
    root = tmp_path / "qlib"
    root.mkdir()
    (root / "research.txt").write_text("not monitor data")
    with pytest.raises(ValueError, match="unmanaged"):
        ManagedDataset(tmp_path).prepare()
    other = tmp_path / "other"
    other.mkdir()
    (other / "qlib").symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        ManagedDataset(other)


def test_stale_directory_no_fake_weekday_calendar_and_worker_lock(store, clock):
    clock.advance(49 * 3600)
    with pytest.raises(ValueError, match="48 hours"):
        HistoryUpdater(store, SETTINGS, DailyFeed(), clock).run()
    with FileLock(str(store.root / "history-worker.lock")):
        with pytest.raises(Timeout):
            HistoryUpdater(store, SETTINGS, DailyFeed(), clock).run()
    assert store.get("history")["status"] == "failed"


def test_retention_keeps_only_two_valid_generations(store, clock):
    dataset = ManagedDataset(store.root)
    store.save_window("SH600895", bars(), DAYS[-1], 500, now=clock())
    generations = []
    for _ in range(3):
        manifest = dataset.publish(store.windows(), {"SH600895": "Example"}, DAYS, {}, validator=lambda *args: None)
        generations.append(manifest["generation"])
    assert not (dataset.root / generations[0]).exists()
    assert all((dataset.root / generation).exists() for generation in generations[1:])
