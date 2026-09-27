"""The Qlib research report: descriptive statistics, isolation, and honest degradation.

Statistics are pure pandas and run everywhere. The subprocess contract is exercised with a fake
child, so a missing engine is a bounded failure and never touches the collection path. A real
``D.features`` read only runs in the separately installed optional environment
(``QUANT_TEST_QLIB=1``).
"""

import importlib.util
import os
import subprocess
import sys
from datetime import date, timedelta
from unittest.mock import Mock

import pandas as pd
import pytest

from quant_platform.adapters.qlib import report
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.jobs.worker import Worker

INSTRUMENTS = ["SH600000", "SH600001", "SH600002", "SH600003", "SH600004", "SH600005"]
SESSIONS = 41  # Two twenty-session rebalances plus the final close.


def panel(days):
    """A cross-sectional panel with momentum and a forward return proportional to it."""
    rows = []
    for index, code in enumerate(INSTRUMENTS):
        for offset, day in enumerate(days):
            momentum = (index - len(INSTRUMENTS) / 2) / 100 + offset * 1e-4
            rows.append(
                {
                    "instrument": code,
                    "datetime": pd.Timestamp(day),
                    "close": 10 + index + offset / 100,
                    "momentum": momentum,
                    "forward": momentum * 2,
                }
            )
    return pd.DataFrame(rows).set_index(["instrument", "datetime"])


@pytest.fixture
def days():
    start = date(2025, 1, 1)
    return [start + timedelta(days=i) for i in range(SESSIONS)]


def test_report_statistics_are_computed_from_a_panel(days):
    frame = panel(days)
    usable = frame[["momentum", "forward"]].dropna()

    # The factor is monotone in the label, so both correlations must be perfect and positive.
    assert report.correlation(usable["momentum"], usable["forward"]) == pytest.approx(1.0)
    assert report.rank_correlation(usable["momentum"], usable["forward"]) == pytest.approx(1.0)
    # A constant series has no correlation rather than a fabricated zero.
    assert report.correlation([1.0, 1.0, 1.0], [1.0, 2.0, 3.0]) is None

    quantiles = report.quantile_means(usable)
    assert [row["bucket"] for row in quantiles] == list(range(len(quantiles)))
    assert len(quantiles) >= 2
    assert [row["mean_forward"] for row in quantiles] == sorted(row["mean_forward"] for row in quantiles)
    assert sum(row["count"] for row in quantiles) == len(usable)

    daily = report.daily_ic(usable)
    assert daily["sessions"] == SESSIONS
    assert daily["mean"] == pytest.approx(1.0)
    assert daily["positive_ratio"] == pytest.approx(1.0)
    # Days with fewer than five names cannot support a cross-sectional correlation.
    assert report.daily_ic(usable.iloc[:4])["sessions"] == 0

    benchmark = {day: 100 + offset for offset, day in enumerate(days)}
    book = report.book(frame, days, benchmark)
    assert book["status"] == "complete"
    assert book["rebalances"] == 2
    assert all(period["holdings"] == len(INSTRUMENTS) for period in book["periods"])
    assert book["relative_return"] == pytest.approx(
        book["cumulative_return"] - book["benchmark_cumulative_return"]
    )
    # The first rebalance buys the book in from cash; the second holds the same names, so it is free.
    assert book["periods"][0]["turnover"] == pytest.approx(0.5)
    assert book["periods"][1]["turnover"] == pytest.approx(0.0)
    assert book["mean_turnover"] == pytest.approx(0.25)
    assert book["periods"][0]["net_return"] == pytest.approx(
        book["periods"][0]["gross_return"] - book["periods"][0]["cost"]
    )
    # A window too short for one rebalance is reported, not guessed.
    assert report.book(frame, days[:10], benchmark)["status"] == "insufficient_history"


def test_rendered_markdown_states_it_is_not_a_recommendation(days):
    frame = panel(days)
    usable = frame[["momentum", "forward"]].dropna()
    document = report.render_markdown(
        {
            "as_of": str(days[-1]),
            "generation": "generation-" + "a" * 32,
            "window": {"start": str(days[0]), "end": str(days[-1])},
            "sessions": len(days),
            "instruments": len(INSTRUMENTS),
            "samples": int(len(usable)),
            "parameters": {"factor": report.MOMENTUM_FIELD, "label": report.FORWARD_FIELD},
            "benchmark": report.BENCHMARK,
            "ic": report.correlation(usable["momentum"], usable["forward"]),
            "rank_ic": report.rank_correlation(usable["momentum"], usable["forward"]),
            "daily_ic": report.daily_ic(usable),
            "quantiles": report.quantile_means(usable),
            "book": report.book(frame, days, {day: 100 + i for i, day in enumerate(days)}),
        }
    )
    assert report.MOMENTUM_FIELD in document and report.FORWARD_FIELD in document
    assert report.BENCHMARK in document
    assert "不构成投资建议" in document


def test_report_job_runs_in_its_own_isolated_process(settings, monkeypatch):
    """The worker must shell out to the report module and hand it the job on stdin."""
    worker = Worker(Mock(), settings, "qlib-daily")
    captured, original = {}, subprocess.Popen

    def spawn(args, **kwargs):
        captured["args"] = args
        return original(
            [
                sys.executable,
                "-c",
                "import json,sys; job=json.load(sys.stdin); "
                "print('QUAP_BLOCKED=module=' + job['kind']); sys.exit(3)",
            ],
            **kwargs,
        )

    monkeypatch.setattr("quant_platform.jobs.worker.subprocess.Popen", spawn)
    job = {"id": 1, "kind": "qlib_report", "payload": {"generation": "generation-" + "a" * 32}}
    with pytest.raises(PipelineBlocked, match="module=qlib_report"):
        worker.execute(job)
    assert captured["args"][1:] == ["-m", "quant_platform.adapters.qlib.report"]


def test_report_reuses_the_data_deadline(settings):
    worker = Worker(Mock(), settings, "qlib-daily")
    assert worker.deadline({"kind": "qlib_report"}) == settings.data_deadline_seconds


def test_report_module_is_lazy_about_qlib():
    """Importing the report module must not drag the optional engine into the main process."""
    probe = "import sys; import quant_platform.adapters.qlib.report; print('qlib' in sys.modules)"
    output = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert output.returncode == 0, output.stderr
    assert output.stdout.strip() == "False"


@pytest.mark.skipif(
    importlib.util.find_spec("qlib") is not None,
    reason="This asserts the missing-engine path, which only exists without Qlib installed",
)
def test_missing_engine_is_a_safe_blocker():
    with pytest.raises(PipelineBlocked, match="optional 'qlib' extra"):
        report.load_engine()


def test_report_initialises_qlib_before_reading_the_data_proxy(tmp_path, settings, monkeypatch):
    """``D`` rejects every accessor until ``qlib.init`` runs, and ``build`` used to read it first.

    No test that runs without the optional engine could see this: the only test that touches a real
    ``D`` is skipped here. Faking the engine asserts the ordering directly instead.
    """
    import json as jsonlib

    generation = "generation-" + "a" * 32
    root = tmp_path / "qlib" / generation
    (root / "calendars").mkdir(parents=True)
    (root / "calendars" / "day.txt").write_text("2026-09-01\n")
    (root / "manifest.json").write_text(jsonlib.dumps({"coverage": {"SH600000": "2026-09-01"}}))
    settings.artifact_root = tmp_path
    initialised = []

    class Data:
        def __getattr__(self, name):
            assert initialised, f"D.{name} was read before qlib.init()"
            raise PipelineBlocked("reached the data proxy after init")

    class Engine:
        @staticmethod
        def init(**kwargs):
            initialised.append(kwargs)

    monkeypatch.setattr(report, "_INITIALIZED", None)
    monkeypatch.setattr(report, "load_engine", lambda: (Engine, Data()))
    job = {"id": 1, "kind": "qlib_report", "payload": {"generation": generation, "as_of": "2026-09-01"}}
    with pytest.raises(PipelineBlocked, match="after init"):
        report.build(Mock(), settings, job)
    assert initialised and initialised[0]["region"] == "cn"


def test_report_window_comes_from_the_generation_intervals(tmp_path):
    """`manifest["coverage"]` mixes status strings with last-covered dates, so it cannot bound a report.

    Deriving the window from it collapsed the panel to the single latest session, which left no
    twenty-session momentum, no forward label and no book.
    """
    root = tmp_path / "qlib" / ("generation-" + "a" * 32)
    (root / "instruments").mkdir(parents=True)
    intervals = root / "instruments" / "all.txt"
    intervals.write_text("SH600000\t2026-08-17\t2026-09-24\nSZ000001\t2026-08-03\t2026-09-24\n")
    assert report.coverage_window(root) == (date(2026, 8, 3), date(2026, 9, 24))
    intervals.write_text("")
    with pytest.raises(PipelineBlocked, match="lists no instruments"):
        report.coverage_window(root)
    intervals.write_text("SH600000\t2026-08-17\n")
    with pytest.raises(PipelineBlocked, match="invalid instrument interval"):
        report.coverage_window(root)


@pytest.mark.skipif(os.getenv("QUANT_TEST_QLIB") != "1", reason="Requires the isolated optional Qlib runtime")
def test_real_qlib_report_reads_a_generation(tmp_path, history_rows, settings):
    from quant_platform.adapters.qlib.export import GenerationStore

    store = GenerationStore(tmp_path)
    days = [row["day"] for row in history_rows]
    with store.lock:
        stage, name, _ = store.build({"SH600895": history_rows}, days)
        store.publish(stage, name)
    frame = report.panel(store.current(), ["SH600895"], days[0], days[-1])
    assert list(frame.columns) == ["close", "momentum", "forward"]
    assert frame["close"].notna().all()
    # Twenty prior sessions supply momentum; five future sessions supply the label.
    assert frame["momentum"].dropna().shape[0] >= len(days) - report.MOMENTUM_WINDOW - 1
    assert frame["forward"].dropna().shape[0] <= len(days) - report.LABEL_HORIZON
    # The whole report, not just the panel, must run against a published generation.
    settings.artifact_root = tmp_path
    stub = Mock()
    stub.rows.return_value = []
    built = report.build(
        stub, settings, {"id": 1, "kind": "qlib_report", "payload": {"generation": name, "as_of": str(days[-1])}}
    )
    assert built["generation"] == name and built["instruments"] == 1 and built["automatic_orders"] is False
