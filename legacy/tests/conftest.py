# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from datetime import datetime, timedelta

import pytest

from legacy_monitor.models import SHANGHAI, Quote, classify_board


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 14, 10, tzinfo=SHANGHAI)
        self.tick = 100.0

    def __call__(self):
        return self.now

    def monotonic(self):
        return self.tick

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("Monitor tests must not access the network.")

    monkeypatch.setattr("requests.sessions.Session.request", fail)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_quote(clock):
    def create(symbol="SH600000", change=0.04, **kwargs):
        values = dict(
            symbol=symbol,
            name=symbol,
            board=classify_board(symbol),
            last=10 * (1 + change),
            previous_close=10.0,
            volume=100.0,
            turnover=1000.0,
            timestamp=clock(),
            received_at=clock(),
        )
        values.update(kwargs)
        return Quote(**values)

    return create
