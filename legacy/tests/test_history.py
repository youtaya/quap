# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from legacy_monitor import Basket, QlibHistory


def test_dated_qlib_api_calls():
    api = Mock()
    api.calendar.return_value = pd.to_datetime(["2026-09-11", "2026-09-14"])
    api.instruments.return_value = {"market": "csi300"}
    api.list_instruments.return_value = ["SH600000", "SZ000001"]
    history = QlibHistory(data_api=api)
    baskets = history.resolve((Basket("index", (), "csi300"),), date(2026, 9, 14))
    assert baskets[0].members == (("SH600000", 1), ("SZ000001", 1))
    api.list_instruments.assert_called_once_with(
        {"market": "csi300"}, start_time="2026-09-14", end_time="2026-09-14", freq="day", as_list=True
    )
    assert history.end_date == date(2026, 9, 14)
    with pytest.raises(ValueError, match="不覆盖"):
        history.resolve((Basket("index", (), "csi300"),), date(2026, 9, 15))
    api.features.return_value = pd.DataFrame({"$close": [10.0], "$volume": [100.0]})
    history.features("600000")
    assert api.features.call_args.args == (["SH600000"], ["$close", "$volume"])
    assert api.features.call_args.kwargs["disk_cache"] == 0
    api.features.return_value = pd.DataFrame({"$close": [float("nan")], "$volume": [100]})
    with pytest.raises(ValueError, match="没有"):
        history.features("SH600000")


def test_universe_membership_cache_keeps_multiple_markets():
    api = Mock()
    api.calendar.return_value = pd.to_datetime(["2026-09-14"])

    def list_instruments(instruments, **kwargs):
        market = instruments["market"]
        return ["SH600000"] if market == "csi300" else ["SZ000001"]

    api.instruments.side_effect = lambda market: {"market": market}
    api.list_instruments.side_effect = list_instruments
    history = QlibHistory(data_api=api)
    baskets = (Basket("csi", (), "csi300"), Basket("all", (), "all"))
    first = history.resolve(baskets, date(2026, 9, 14))
    second = history.resolve(baskets, date(2026, 9, 14))
    assert first[0].members == (("SH600000", 1.0),)
    assert first[1].members == (("SZ000001", 1.0),)
    assert second[0].members == first[0].members
    assert second[1].members == first[1].members
    assert api.list_instruments.call_count == 2


def test_missing_qlib_data(tmp_path):
    with pytest.raises(ValueError, match="calendars/day.txt"):
        QlibHistory(tmp_path)


def test_tiny_qlib_dataset(tmp_path):
    root = tmp_path / "dataset"
    (root / "calendars").mkdir(parents=True)
    (root / "instruments").mkdir()
    (root / "features" / "sh600000").mkdir(parents=True)
    (root / "calendars" / "day.txt").write_text("2026-09-11\n2026-09-14\n2026-09-15\n", encoding="utf-8")
    (root / "instruments" / "all.txt").write_text("SH600000\t2026-09-11\t2026-09-15\n", encoding="utf-8")
    for field, values in (("close", [10, 11, 12]), ("volume", [100, 200, 300])):
        np.array([0, *values], dtype="<f4").tofile(root / "features" / "sh600000" / f"{field}.day.bin")
    # Isolate Qlib's process-global registration from the rest of the test suite.
    program = """
import sys
from datetime import date
from legacy_monitor import Basket, QlibHistory
h = QlibHistory(sys.argv[1])
assert h.end_date == date(2026, 9, 15)
assert QlibHistory(sys.argv[1]).end_date == h.end_date
members = h.resolve((Basket('all members', (), 'all'),), date(2026, 9, 14))
assert members[0].members == (('SH600000', 1.0),)
frame = h.features('SH600000')
assert frame['$close'].tolist() == [10, 11, 12]
assert frame['$volume'].tolist() == [100, 200, 300]
try:
    h.features('SZ000001')
except ValueError:
    pass
else:
    raise AssertionError('Missing history must not masquerade as zero prices')
print('tiny Qlib dataset passed')
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", program, str(root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tiny Qlib dataset passed" in result.stdout
