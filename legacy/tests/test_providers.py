# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import json

import pytest
import requests

from legacy_monitor.providers import DemoProvider, EastmoneyProvider, FeedError, parse_quote


def row(code="600000", **kwargs):
    result = {
        "f12": code,
        "f13": 1,
        "f14": "Test",
        "f2": 10.5,
        "f18": 10,
        "f3": 5,
        "f5": 12,
        "f6": 12000,
        "f124": 1789351200,
    }
    result.update(kwargs)
    return result


def page(rows, total):
    return {"data": {"total": total, "diff": rows}}


class Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        yield json.dumps(self.payload).encode()


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({**kwargs, "params": dict(kwargs["params"])})
        payload = next(self.responses)
        if isinstance(payload, Exception):
            raise payload
        return Response(payload)


def provider(clock, responses, **kwargs):
    return EastmoneyProvider(
        session=Session(responses), clock=clock, monotonic=clock.monotonic, sleep=clock.advance, **kwargs
    )


def test_field_units_and_timestamp(clock):
    quote = parse_quote(row(f124=clock().timestamp()), clock())
    assert quote.last == 10.5
    assert quote.daily_return == pytest.approx(0.05)
    assert quote.volume == 1200
    assert quote.turnover == 12000
    assert quote.timestamp == clock()
    assert quote.received_at == clock()
    assert parse_quote(row(code="000001", f13=0), clock()).symbol == "SZ000001"


@pytest.mark.parametrize("bad", [None, "-", "NaN", "inf"])
def test_missing_numeric_values(clock, bad):
    quote = parse_quote(row(f2=bad, f5=bad, f6=bad, f124=bad), clock())
    assert quote.last is None
    assert quote.volume is None
    assert quote.turnover is None
    assert quote.timestamp is None
    assert quote.daily_return is None


def test_scaling_and_invalid_volume(clock):
    quote = parse_quote(row(f2=1050, f5=-1, f6=-1), clock())
    assert quote.daily_return is None
    assert quote.volume is None
    assert quote.turnover is None
    assert parse_quote(row(f124=1e100), clock()).timestamp is None


def test_paginated_server_caps_and_overlap(clock):
    feed = provider(clock, [page([row()], 3), page([row(), row("600001")], 3), page([row("600002")], 3)])
    snapshot = feed.fetch()
    assert len(snapshot.quotes) == snapshot.reported_total == 3
    assert [call["params"]["pn"] for call in feed.session.calls] == [1, 2, 3]
    assert feed.session.calls[0]["params"]["fltt"] == 2
    assert all(max(call["timeout"]) <= 8 for call in feed.session.calls)


def test_unsupported_records_are_counted(clock):
    snapshot = provider(clock, [page({"0": row(), "1": row("510300"), "2": row("830001", f13=2)}, 3)]).fetch()
    assert snapshot.excluded == 2
    assert len(snapshot.quotes) == 1
    assert snapshot.warnings


@pytest.mark.parametrize(
    "responses",
    [
        [page([row()], 2), page([row()], 2)],
        [page([row()], 2), page([], 2)],
        [page([row()], 2), {"data": None}],
        [page([row()], 2), page([row("600001")], 3)],
        [page([row(), row("600001")], 1)],
        [page([{"f12": "600000"}], 1)],
        [page([row(f13={})], 1)],
        [page([row(f13=True)], 1)],
        [page([row(code="60000")], 1)],
        [page([row()], 0)],
        [{"data": []}],
        [None],
    ],
)
def test_incomplete_snapshots_fail(clock, responses):
    with pytest.raises(FeedError):
        provider(clock, responses).fetch()


def test_retry_is_bounded(clock):
    feed = provider(clock, [requests.Timeout(), page([row()], 1)])
    assert len(feed.fetch().quotes) == 1
    assert len(feed.session.calls) == 2
    failure = provider(clock, [requests.Timeout(), requests.Timeout()])
    with pytest.raises(FeedError, match="Timeout"):
        failure.fetch()
    assert len(failure.session.calls) == 2


def test_deadline(clock):
    feed = provider(clock, [page([row()], 2)], deadline=0.15)
    with pytest.raises(FeedError, match="时限"):
        feed.fetch()
    assert len(feed.session.calls) == 1


def test_response_size_limit(clock, monkeypatch):
    monkeypatch.setattr(Response, "iter_content", lambda self, chunk_size: iter([b"x" * (2 * 1024 * 1024 + 1)]))
    with pytest.raises(FeedError, match="体积"):
        provider(clock, [page([row()], 1)]).fetch()


def test_response_deadline_while_reading(clock, monkeypatch):
    def chunks(self, chunk_size):
        clock.advance(91)
        yield b"{}"

    monkeypatch.setattr(Response, "iter_content", chunks)
    with pytest.raises(FeedError, match="时限或体积"):
        provider(clock, [page([row()], 1)]).fetch()


def test_demo_clock_never_moves_backward():
    demo = DemoProvider()
    stamps = [demo.fetch().fetched_at for _ in range(901)]
    assert all(left < right for left, right in zip(stamps, stamps[1:]))
    assert all(stamp.weekday() < 5 for stamp in stamps)


def test_maximum_pages(clock):
    feed = provider(clock, [page([row(str(600000 + i))], 201) for i in range(200)])
    with pytest.raises(FeedError, match="分页上限"):
        feed.fetch()
    assert len(feed.session.calls) == 200


def test_demo_deterministic_and_labeled():
    first, second = DemoProvider(), DemoProvider()
    for _ in range(6):
        a, b = first.fetch(), second.fetch()
        assert a == b
        assert a.mode == "demo"
        assert len({quote.board for quote in a.quotes}) == 3
        assert "合成" in a.warnings[0]
