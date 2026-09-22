# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import json
from datetime import date
from unittest.mock import Mock

import pytest
import requests

from legacy_monitor.feeds import PublicHTTP, SinaDirectory, TencentDaily, TencentProvider, parse_tencent
from legacy_monitor.providers import FeedError


class FakeHTTP:
    def __init__(self, values):
        self.values = iter(values)
        self.calls = []
        self.session = Mock()

    def monotonic(self):
        return 0

    def get(self, url, params, deadline, encoding="utf-8"):
        self.calls.append((url, dict(params), deadline, encoding))
        value = next(self.values)
        if isinstance(value, Exception):
            raise value
        return value

    def json(self, url, params, deadline, validator=None):
        return self.get(url, params, deadline)

    def close(self):
        pass


def security(code):
    return {"symbol": code, "code": code[2:], "name": code}


def quote_text(code="sh600895", timestamp="20260914100000", **overrides):
    fields = [""] * 60
    values = {
        0: "1" if code.startswith("sh") else "51",
        1: "Example",
        2: code[2:],
        3: "10.4",
        4: "10",
        6: "12",
        30: timestamp,
        32: "4",
        37: "2.5",
    }
    values.update({int(key): value for key, value in overrides.items()})
    for index, value in values.items():
        fields[index] = value
    return "~".join(fields)


def quote_line(code="sh600895", **kwargs):
    return f'v_{code}="{quote_text(code, **kwargs)}";\n'


def test_complete_directory_merges_star_overlap_and_excludes_other_boards(clock):
    http = FakeHTTP(
        [
            "3",
            [security("sh600895"), security("bj920001")],
            [security("sh688001")],
            "3",
            "1",
            [security("sh688001")],
            "1",
        ]
    )
    result = SinaDirectory(http, clock).fetch()
    assert set(result["symbols"]) == {"SH600895", "SH688001"}
    assert result["fetched_at"] == clock().isoformat()
    assert result["totals"] == {"hs_a": 3, "kcb": 1}


@pytest.mark.parametrize(
    "values",
    [
        ["2", [security("sh600895")], []],
        ["2", [security("sh600895")], [security("sh600895")]],
        ["1", [security("sh600895")], "2"],
        ["1", [{"symbol": "sh600895", "code": "000001"}]],
        [True],
        ["0"],
        ["20001"],
    ],
)
def test_bad_directory_is_never_published(values):
    with pytest.raises(FeedError):
        SinaDirectory(FakeHTTP(values)).fetch()


@pytest.mark.parametrize(
    "symbol,expected", [("SH600895", 1200), ("SZ000001", 1200), ("SZ300001", 1200), ("SH688001", 12), ("SH689009", 12)]
)
def test_tencent_board_specific_units(symbol, expected, clock):
    quote = parse_tencent(symbol, quote_text(symbol.lower()), clock())
    assert quote.volume == expected
    assert quote.turnover == 25000
    assert quote.timestamp == clock()
    assert quote.daily_return == pytest.approx(0.04)


def test_missing_and_bad_tencent_fields(clock):
    empty = parse_tencent("SH600895", "", clock())
    assert empty.last is empty.timestamp is empty.volume is None
    quote = parse_tencent("SH600895", quote_text(**{"3": "1040", "6": "-1", "30": "wrong", "37": "NaN"}), clock())
    assert quote.last is quote.timestamp is quote.volume is quote.turnover is None
    with pytest.raises(FeedError):
        parse_tencent("SH600895", quote_text("sz600895"), clock())


@pytest.mark.parametrize("body", [quote_line("sz000001"), quote_line() + quote_line(), "<html>error</html>", ""])
def test_incomplete_or_malformed_quote_batches_fail(body):
    with pytest.raises(FeedError):
        TencentProvider({"SH600895": "Example"}, FakeHTTP([body])).fetch()


def test_quote_batches_have_maximum_80_symbols():
    codes = [f"SH{600000 + index}" for index in range(81)]
    http = FakeHTTP(["".join(quote_line(code.lower()) for code in codes[:80]), quote_line(codes[-1].lower())])
    provider = TencentProvider(dict.fromkeys(codes, "Example"), http)
    provider.directory_fresh = False
    snapshot = provider.fetch()
    assert len(snapshot.quotes) == snapshot.reported_total == 81
    assert len(http.calls[0][0].split("=")[1].split(",")) == 80
    assert "48 hours" in snapshot.warnings[0]


def test_history_units_cutoff_and_corporate_action_annotation():
    rows = [["2026-09-11", "10", "11", "12", "9", "3", {"dividend": 1}], ["2026-09-14", "11", "12", "13", "10", "5"]]
    for code, expected in (("SH688001", 3), ("SH600895", 300)):
        feed = TencentDaily(FakeHTTP([{"code": 0, "data": {code.lower(): {"qfqday": rows}}}]))
        actual = feed.fetch(code, date(2026, 9, 11), 500)
        assert actual == [["2026-09-11", 10, 11, 12, 9, expected]]


@pytest.mark.parametrize(
    "rows",
    [
        [["2026-09-11", "10", "11", "9", "9", "3"]],
        [["bad", "10", "11", "12", "9", "3"]],
        [["2026-09-11", "0", "11", "12", "9", "3"]],
        [["2026-09-11", "10", "11", "12", "9", "3"]] * 2,
    ],
)
def test_history_validation(rows):
    feed = TencentDaily(FakeHTTP([{"code": 0, "data": {"sh600895": {"qfqday": rows}}}]))
    with pytest.raises(FeedError):
        feed.fetch("SH600895", date(2026, 9, 14))


def test_transport_retry_gap_and_bounds(clock):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.return_value = [b"{}"]
    session = Mock()
    session.get.side_effect = [requests.Timeout(), response, response]
    http = PublicHTTP(session, gap=1, monotonic=clock.monotonic, sleep=clock.advance)
    assert http.json("https://example.test", {}, clock.tick + 30) == {}
    assert http.json("https://example.test", {}, clock.tick + 30) == {}
    assert clock.tick >= 102
    assert session.get.call_count == 3
    assert session.get.call_args.kwargs["stream"]
    assert max(session.get.call_args.kwargs["timeout"]) <= 8
    response.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
    session.get.side_effect = None
    session.get.return_value = response
    with pytest.raises(FeedError, match="size"):
        http.get("https://example.test", {}, clock.tick + 30)
    with pytest.raises(FeedError, match="deadline"):
        http.get("https://example.test", {}, clock.tick)


def test_empty_directory_page_retries_once_within_total_attempt_budget(clock):
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.iter_content.side_effect = [[b"[]"], [json.dumps([security("sh600895")]).encode()]]
    session = Mock()
    session.get.return_value = response
    http = PublicHTTP(session, gap=1, monotonic=clock.monotonic, sleep=clock.advance)
    rows = http.json("https://example.test", {}, clock.tick + 30, validator=SinaDirectory._nonempty_page)
    assert rows[0]["symbol"] == "sh600895"
    assert session.get.call_count == 2
    response.iter_content.side_effect = [[b"[]"], [b"[]"]]
    with pytest.raises(FeedError):
        http.json("https://example.test", {}, clock.tick + 30, validator=SinaDirectory._nonempty_page)
    assert session.get.call_count == 4


def test_directory_resumes_without_publishing_partial_data(clock):
    http = FakeHTTP(
        [
            "2",
            [security("sh600895")],
            FeedError("temporary empty response"),
            "2",
            [security("sz000001")],
            "2",
            "1",
            [security("sh688001")],
            "1",
        ]
    )
    directory = SinaDirectory(http, clock)
    with pytest.raises(FeedError):
        directory.fetch()
    clock.advance(1800)
    complete = directory.fetch()
    assert set(complete["symbols"]) == {"SH600895", "SZ000001", "SH688001"}
    pages = [params["page"] for _, params, _, _ in http.calls if params.get("node") == "hs_a" and "page" in params]
    assert pages == [1, 2, 2]
    assert complete["started_at"] != complete["fetched_at"]
    assert not directory._pending


def test_directory_checkpoints_expire_and_changed_totals_restart(clock):
    for changed_total, elapsed in ((3, 1800), (2, 7201)):
        http = FakeHTTP(["2", [security("sh600895")], FeedError("offline")])
        directory = SinaDirectory(http, clock)
        with pytest.raises(FeedError):
            directory.fetch()
        clock.advance(elapsed)
        http.values = iter([str(changed_total), FeedError("check starting page")])
        with pytest.raises(FeedError):
            directory.fetch()
        assert http.calls[-1][1]["page"] == 1
