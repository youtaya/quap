"""Descriptive market report: pure builds from synthetic rows; no provider, Qlib, or database required."""

import json
from datetime import date, timedelta

import numpy as np
import pytest

from quant_platform.analysis import market_report
from quant_platform.analysis.market_report import COMPOSITE_LABEL, DISCLAIMER, build, render_markdown

SYMBOLS = 30
SESSIONS = 300


def sessions(count=SESSIONS, end=date(2026, 9, 25)):
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


def synthetic_inputs(symbols=SYMBOLS, count=SESSIONS, *, benchmark=True, turnover=True, seed=7, constraints=True):
    rng = np.random.default_rng(seed)
    days = sessions(count)
    boards = ["Main Board", "STAR Market", "ChiNext"]
    instruments, bars, factors = [], [], []
    for i in range(symbols):
        code = f"SH6{i:05d}" if i % 2 == 0 else f"SZ0{i:05d}"
        name = "ST退市股" if i == 0 else f"股票{i}"
        instruments.append({"symbol": code, "name": name, "board": boards[i % 3], "list_date": days[0]})
        prices = 10 * np.exp(np.cumsum(rng.normal(0.0002 * (i % 5), 0.02, count)))
        factor = 1.0
        for k, day in enumerate(days):
            if i == 1 and k < count - 30:
                continue  # young listing: only 30 sessions of history
            if k == count // 2:
                factor *= 1.1  # one adjustment event
            close = float(prices[k])
            bars.append(
                {
                    "symbol": code,
                    "day": day,
                    "dataset_id": 5,
                    "open": close * 0.99,
                    "high": close * 1.02,
                    "low": close * 0.98,
                    "close": close,
                    "pre_close": float(prices[k - 1]) if k else None,
                    "volume": 1_000_000,
                    "turnover": close * 1_000_000 if turnover else None,
                }
            )
            factors.append({"symbol": code, "day": day, "dataset_id": 6, "factor": factor})
    bench = []
    if benchmark:
        level = 4000 * np.exp(np.cumsum(rng.normal(0.0001, 0.01, count)))
        bench = [{"day": day, "dataset_id": 7, "close": float(level[k])} for k, day in enumerate(days)]
    cons = []
    if constraints:
        for row in instruments:
            last = next(b for b in reversed(bars) if b["symbol"] == row["symbol"])
            cons.append(
                {
                    "symbol": row["symbol"],
                    "dataset_id": 8,
                    "limit_up": last["close"] if row["symbol"].endswith("00002") else last["close"] * 1.1,
                    "limit_down": last["close"] * 0.9,
                    "suspended": row["symbol"].endswith("00004"),
                }
            )
    return {
        "as_of": days[-1],
        "days": days,
        "instruments": instruments,
        "bars": bars,
        "factors": factors,
        "benchmark": bench,
        "constraints": cons,
    }


def test_full_report_is_complete_deterministic_and_json_safe():
    inputs = synthetic_inputs()
    report = build(inputs)
    assert report["status"] == "complete"
    assert build(inputs) == report
    encoded = json.dumps(report, allow_nan=False)
    assert json.loads(encoded) == report
    meta = report["meta"]
    assert meta["as_of"] == "2026-09-25" and meta["session"] == "2026-09-25"
    assert meta["sessions_available"] == SESSIONS
    assert meta["symbols_total"] == SYMBOLS and meta["symbols_with_bars"] == SYMBOLS
    assert meta["coverage"] == 1.0 and meta["benchmark_available"] is True
    assert meta["engine"] == "descriptive-v1" and meta["max_dataset_id"] == 8
    assert "描述性" in meta["data_note"]
    assert len(report["input_hash"]) == 64


def test_benchmark_section_uses_raw_index_close():
    inputs = synthetic_inputs()
    bench = build(inputs)["benchmark"]
    closes = [row["close"] for row in inputs["benchmark"]]
    assert bench["status"] == "available"
    assert bench["close"] == pytest.approx(closes[-1])
    assert bench["return_1"] == pytest.approx(closes[-1] / closes[-2] - 1)
    assert bench["return_120"] == pytest.approx(closes[-1] / closes[-121] - 1)
    assert bench["drawdown_250"] == pytest.approx(closes[-1] / max(closes[-250:]) - 1)
    assert bench["drawdown_sessions"] == 250
    assert bench["close_vs_ma20"] == pytest.approx(closes[-1] / np.mean(closes[-20:]) - 1)
    assert bench["volatility_20"] > 0


def test_benchmark_absent_is_reported_unavailable_not_zero():
    report = build(synthetic_inputs(benchmark=False))
    assert report["status"] == "complete"
    assert report["benchmark"] == {"status": "unavailable", "symbol": "SH000300"}
    assert report["meta"]["benchmark_available"] is False


def test_breadth_counts_limits_and_suspensions_from_constraints():
    report = build(synthetic_inputs())
    breadth = report["breadth"]
    assert breadth["advancers"] + breadth["decliners"] + breadth["unchanged"] == SYMBOLS
    assert breadth["constraints_available"] is True
    assert breadth["limit_up"] == 1 and breadth["limit_down"] == 0 and breadth["suspended"] == 1
    assert 0 <= breadth["above_ma20_ratio"] <= 1 and 0 <= breadth["above_ma60_ratio"] <= 1
    assert breadth["median_turnover"] > 0
    assert report["risk_flags"]["suspended"]["items"] == [{"symbol": "SH600004", "name": "股票4"}]


def test_missing_turnover_is_none_and_constraints_absent_are_unavailable():
    report = build(synthetic_inputs(turnover=False, constraints=False))
    breadth = report["breadth"]
    assert breadth["median_turnover"] is None and breadth["turnover_missing_ratio"] == 1
    assert breadth["constraints_available"] is False
    assert breadth["limit_up"] is None and breadth["limit_down"] is None and breadth["suspended"] is None
    turnover = report["factors"]["turnover_20"]
    assert turnover["coverage"] == 0 and turnover["top"] == [] and turnover["bottom"] == []
    assert turnover["efficacy"]["status"] == "insufficient_history"
    assert report["risk_flags"]["suspended"]["count"] is None


def test_boards_cover_every_board_with_equal_weight_returns():
    report = build(synthetic_inputs())
    boards = {row["board"]: row for row in report["boards"]}
    assert set(boards) == {"Main Board", "STAR Market", "ChiNext"}
    assert sum(row["count"] for row in boards.values()) == SYMBOLS
    for row in boards.values():
        assert row["mean_return_1"] is not None and row["median_volatility_20"] > 0
        assert row["median_amplitude"] == pytest.approx(1.02 / 0.98 - 1)


def test_factor_snapshot_and_non_overlapping_ic_evaluation():
    inputs = synthetic_inputs()
    factors = build(inputs)["factors"]
    assert set(factors) == {"momentum_20", "momentum_60_ex_5", "volatility_20", "turnover_20", "reversal_5"}
    momentum = factors["momentum_20"]
    assert momentum["coverage"] == SYMBOLS
    assert len(momentum["top"]) == 10 and len(momentum["bottom"]) == 10
    assert momentum["top"][0]["value"] >= momentum["top"][-1]["value"] >= momentum["bottom"][-1]["value"]
    assert all(row["name"] for row in momentum["top"])
    evidence = momentum["efficacy"]
    assert evidence["status"] == "evaluated" and evidence["forward_sessions"] == 20
    evaluation = [date.fromisoformat(day) for day in evidence["evaluation_days"]]
    positions = [inputs["days"].index(day) for day in evaluation]
    assert all(b - a == 20 for a, b in zip(positions, positions[1:]))
    assert positions[-1] == SESSIONS - 1 - 20
    assert evidence["samples"] == len(evaluation) >= 3
    assert -1 <= evidence["ic_mean"] <= 1 and evidence["ic_std"] >= 0
    assert 0 <= evidence["ic_positive_ratio"] <= 1
    assert factors["momentum_60_ex_5"]["efficacy"]["samples"] < factors["reversal_5"]["efficacy"]["samples"]


def test_adjusted_momentum_uses_factor_ratio_within_symbol():
    inputs = synthetic_inputs()
    factors = build(inputs)["factors"]["momentum_20"]
    values = {row["symbol"]: row["value"] for row in factors["top"] + factors["bottom"]}
    symbol = next(iter(values))
    closes = [b["close"] for b in inputs["bars"] if b["symbol"] == symbol]
    assert values[symbol] == pytest.approx(closes[-1] / closes[-21] - 1)


def test_insufficient_history_for_ic_is_labelled():
    report = build(synthetic_inputs(count=50))
    assert report["status"] == "complete"
    for name, section in report["factors"].items():
        assert section["efficacy"]["status"] == "insufficient_history", name
        assert section["efficacy"]["ic_mean"] is None and section["efficacy"]["samples"] < 3
    assert report["factors"]["momentum_20"]["coverage"] == SYMBOLS
    assert report["factors"]["momentum_60_ex_5"]["coverage"] == 0
    assert report["composite"]["status"] == "insufficient_data" and report["composite"]["label"] == COMPOSITE_LABEL
    assert report["benchmark"]["return_120"] is None and report["benchmark"]["return_20"] is not None
    assert report["risk_flags"]["young_listings"]["count"] is None


def test_composite_is_labelled_descriptive_and_ranked():
    composite = build(synthetic_inputs())["composite"]
    assert composite["label"] == COMPOSITE_LABEL
    assert composite["status"] == "complete" and composite["count"] == SYMBOLS - 1  # young listing lacks 60d
    assert len(composite["top"]) == 20 and len(composite["bottom"]) == 20
    scores = [row["score"] for row in composite["top"]]
    assert scores == sorted(scores, reverse=True)
    assert composite["bottom"][0]["score"] <= composite["top"][-1]["score"]
    assert set(composite["top"][0]) == {
        "symbol",
        "name",
        "board",
        "close",
        "score",
        "momentum_20",
        "momentum_60_ex_5",
        "volatility_20",
    }
    assert "target_weight" not in json.dumps(composite)


def test_risk_flags_identify_names_suspensions_and_young_listings():
    risk = build(synthetic_inputs())["risk_flags"]
    assert risk["risk_warning_names"]["items"] == [{"symbol": "SH600000", "name": "ST退市股"}]
    assert risk["young_listings"]["count"] == 1
    assert risk["young_listings"]["items"][0] == {"symbol": "SZ000001", "name": "股票1", "sessions": 30}
    assert risk["suspended"]["available"] is True and risk["suspended"]["count"] == 1


def test_no_bars_degrades_to_insufficient_data_with_meta():
    inputs = synthetic_inputs(count=10)
    inputs["bars"] = []
    report = build(inputs)
    assert report["status"] == "insufficient_data"
    assert report["meta"]["symbols_total"] == SYMBOLS and report["meta"]["sessions_available"] == 10
    assert report["benchmark"]["status"] == "available"
    json.dumps(report, allow_nan=False)
    empty = build({"as_of": "2026-09-25", "days": [], "instruments": [], "bars": []})
    assert empty["status"] == "insufficient_data" and empty["benchmark"]["status"] == "unavailable"
    assert DISCLAIMER in render_markdown(empty)


def test_highest_dataset_id_wins_per_symbol_day():
    inputs = synthetic_inputs()
    last = inputs["days"][-1]
    stale = [dict(b, dataset_id=1, close=b["close"] * 3) for b in inputs["bars"] if b["day"] == last]
    inputs["bars"] = inputs["bars"] + stale
    assert build(inputs)["breadth"] == build(synthetic_inputs())["breadth"]


def test_render_markdown_contains_disclaimer_and_sections():
    report = build(synthetic_inputs())
    text = render_markdown(report)
    assert DISCLAIMER in text
    assert report["meta"]["data_note"] in text
    for heading in (
        "# 量化分析报告",
        "## 基准指数",
        "## 市场宽度",
        "## 板块",
        "## 因子横截面",
        "## 描述性排名组合",
        "## 风险标识",
    ):
        assert heading in text
    assert COMPOSITE_LABEL in text
    assert report["input_hash"] in text
    degraded = render_markdown(build(synthetic_inputs(benchmark=False, turnover=False, constraints=False)))
    assert "基准指数数据不可用" in degraded and "—" in degraded


def test_input_hash_tracks_dataset_and_universe_identity():
    base = synthetic_inputs()
    changed = synthetic_inputs()
    changed["bars"][0] = dict(changed["bars"][0], dataset_id=9)
    assert build(base)["input_hash"] != build(changed)["input_hash"]
    assert build(base)["input_hash"] == build(synthetic_inputs())["input_hash"]


class FakeStore:
    """Minimal in-memory stand-in: reads return stored rows; writes record enqueued jobs."""

    def __init__(self, report=None):
        self.report = report
        self.enqueued = []
        self.audit = []

    def rows(self, query, params=()):
        assert query.startswith("SELECT")
        if self.report is None or (params[2] is not None and str(params[2]) != self.report["meta"]["as_of"]):
            return []
        return [
            {
                "id": 11,
                "kind": "market",
                "target": "ALL",
                "as_of": date.fromisoformat(self.report["meta"]["as_of"]),
                "input_hash": self.report["input_hash"],
                "engine": "descriptive-v1",
                "created_at": "2026-09-25T08:00:00+00:00",
                "data": self.report,
            }
        ]

    def transaction(self):
        from contextlib import contextmanager

        store = self

        class Conn:
            def execute(self, query, params=()):
                assert query.startswith("INSERT INTO audit")
                store.audit.append(params)
                return self

        @contextmanager
        def scope():
            yield Conn()

        return scope()

    def enqueue(self, conn, kind, queue, key, payload=None, priority=0, available=None):
        self.enqueued.append({"kind": kind, "queue": queue, "key": key, "payload": payload, "priority": priority})
        return 42


def test_market_report_api_reads_and_enqueues_without_recommendation_language(settings):
    from fastapi.testclient import TestClient

    from quant_platform.api import create_app

    report = build(synthetic_inputs(count=70))
    store = FakeStore(report)
    with TestClient(create_app(settings, database=store)) as client:
        assert client.get("/api/v1/market-report").status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        body = client.get("/api/v1/market-report").json()
        assert body["report"] == report and body["report_id"] == 11
        assert DISCLAIMER in body["markdown"]
        assert client.get("/api/v1/market-report?as_of=2000-01-01").json() == {
            "report": None,
            "markdown": None,
            "reason": "No descriptive market report stored yet; request one with POST /market-report.",
        }
        assert client.post("/api/v1/market-report", json={"as_of": "not-a-date"}).status_code == 422
        assert client.post("/api/v1/market-report", json={"as_of": "2026-09-25", "extra": 1}).status_code == 422
        response = client.post("/api/v1/market-report", json={"as_of": "2026-09-25"})
        assert response.status_code == 202 and response.json() == {"job_id": 42}
    job = store.enqueued[0]
    assert job["kind"] == "market_report" and job["queue"] == "analysis" and job["priority"] == 50
    assert job["payload"] == {"as_of": "2026-09-25"} and job["key"].startswith("market-report:2026-09-25:")
    assert store.audit and store.audit[0][0] == "2026-09-25"


@pytest.mark.postgres
def test_store_and_latest_round_trip(db):
    report = build(synthetic_inputs(count=70))
    with db.transaction() as conn:
        first = market_report.store_report(conn, report)
        again = market_report.store_report(conn, report)
    assert first is not None and again is None
    row = market_report.latest(db)
    assert row["id"] == first and row["engine"] == "descriptive-v1" and row["data"] == report
    assert market_report.latest(db, date(2000, 1, 1)) is None
