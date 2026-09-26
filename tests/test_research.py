"""Research safety contracts; fixtures never access an external provider."""

from datetime import date, datetime, timedelta
import importlib.util
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import httpx
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient

from quant_platform.api import create_app
from quant_platform.domain import CN, digest
from quant_platform.domain.labels import adjusted_return, label_window
from quant_platform.domain.research import FactorInput, TrainingConfigurationInput
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.jobs.diagnostics import build_observation, calendar_days, realized_quality
from quant_platform.research.adata_adapter import ControlledTransport, TransportUnavailable, collect_one
from quant_platform.research.diagnostics import (
    cross_section,
    distribution_drift,
    psi,
    reference_histogram,
    rolling_quality,
    training_reference,
)
from quant_platform.research.factors import compile_factor, development_folds, feature_spec
from quant_platform.research.process import run_research
from quant_platform.research.supplemental import normalize, compare_raw


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os')",
        "$label",
        "Ref($close,-1)",
        "Ref($close,0)",
        "Mean($close,253)",
        "$close.__class__",
        "open('/tmp/x')",
        "Mean($close,1.5)",
        "1",
        "1e999*$close",
        "Ref($open,1-2)",
        "Abs(" * 18 + "$close" + ")" * 18,
        "$close" * 400,
        "Corr($close,$label,10)",
        "$close[0]",
    ],
)
def test_factor_language_rejects_untrusted_or_future_input(expression):
    with pytest.raises(ValueError):
        compile_factor(expression)
    with pytest.raises(ValueError):
        FactorInput(name="Unsafe", expression=expression, author="fixture", expected_revision=0, request_key="invalid")


def test_factor_canonical_hash_warmup_and_feature_order():
    first = compile_factor(" Mean(Ref($close, 2), 5) / $open ")
    assert first == compile_factor(first["expression"])
    assert first["warmup"] == 6 and first["required_fields"] == ["close", "open"]
    features = [{"name": "A", **first}, {"name": "B", **compile_factor("Corr($volume,$close,20)")}]
    config = {"frequency": "day", "features": features}
    config["configuration_hash"] = digest(config)
    assert feature_spec(config)[1] == ["R_A", "R_B"]
    config["features"].reverse()
    with pytest.raises(PipelineBlocked, match="hash"):
        feature_spec(config)


@pytest.mark.parametrize("frequency,purge", [("day", 6), ("5min", 1)])
def test_three_folds_fixed_windows_and_holdout(frequency, purge):
    days = pd.bdate_range("2024-01-01", periods=180).strftime("%Y-%m-%d").tolist()
    config = {"frequency": frequency, "train_sessions": 20, "validation_sessions": 10, "test_sessions": 10}
    folds, heldout = development_folds(days, config)
    assert len(folds) == 3
    for fold in folds:
        assert days.index(fold["train"][1]) - days.index(fold["train"][0]) + 1 == 20
        assert days.index(fold["valid"][0]) - days.index(fold["train"][1]) == purge + 1
        assert days.index(fold["valid"][1]) + purge < days.index(heldout[0])
    assert folds[0]["valid"][1] < folds[1]["valid"][0] < folds[2]["valid"][0]
    assert heldout == (days[-purge - 10], days[-purge - 1])
    with pytest.raises(PipelineBlocked):
        development_folds(days[:50], config)


def test_minute_features_unchanged_and_fixture_windows_explicit():
    with pytest.raises(ValueError):
        TrainingConfigurationInput(
            name="minute", frequency="5min", factor_set_id=uuid4(), request_key="x", expected_revision=0
        )
    with pytest.raises(ValueError):
        TrainingConfigurationInput(name="short", train_sessions=10, request_key="x", expected_revision=0)


URL = "https://push2.eastmoney.com/api/qt/stock/trends2/get"
PARAMS = {"secid": "1.600000", "fqt": 0}


@pytest.mark.parametrize(
    "url,params,kwargs",
    [
        (URL.replace("https", "http"), PARAMS, {}),
        ("https://evil.test/api/qt/stock/trends2/get", PARAMS, {}),
        (URL + "?foo=bar", PARAMS, {}),
        (URL, {"secid": "1.600001"}, {}),
        (URL, {**PARAMS, "fqt": 1}, {}),
        (URL, PARAMS, {"verify": False}),
        (URL.replace("push2.", "user@push2."), PARAMS, {}),
    ],
)
def test_transport_rejects_before_network(url, params, kwargs):
    def forbidden(request):
        pytest.fail("Rejected request reached network transport")

    transport = ControlledTransport("SH600000", transport=httpx.MockTransport(forbidden))
    with pytest.raises(TransportUnavailable):
        transport.request(url=url, params=params, **kwargs)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": URL}),
        httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=b""),
        httpx.Response(200, json={"data": {"code": "600001"}}),
        httpx.Response(200, content=b"x" * 1000),
    ],
)
def test_transport_response_boundaries(response):
    transport = ControlledTransport(
        "SH600000", response_cap=100, transport=httpx.MockTransport(lambda request: response)
    )
    with pytest.raises(TransportUnavailable):
        transport.request(url=URL, params=PARAMS)


def test_transport_single_request_and_no_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://forbidden.test:9999")
    observed = []

    def response(request):
        observed.append(request)
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(200, json={"data": {"code": "600000", "trends": []}})

    transport = ControlledTransport("SH600000", transport=httpx.MockTransport(response))
    assert transport.request(url=URL, params=PARAMS).json()["data"]["code"] == "600000"
    with pytest.raises(TransportUnavailable):
        transport.request(url=URL, params=PARAMS)
    assert len(observed) == 1


def raw_row(**overrides):
    return {
        "stock_code": "600000",
        "trade_time": "2026-01-05 09:31:00",
        "open": 10,
        "high": 11,
        "low": 9,
        "close": 10.5,
        "volume": 1200,
        "amount": 12000,
        **overrides,
    }


def test_supplemental_units_quarantine_cutoff_and_crosscheck():
    retrieved = datetime(2026, 1, 5, 9, 33, tzinfo=CN)
    rows, quality = normalize(
        [raw_row(), raw_row(trade_time="2026-01-05 09:33:00")],
        "SH600000",
        retrieved.date(),
        retrieved.date(),
        "1min",
        retrieved,
    )
    assert quality["status"] == "available" and quality["excluded_unfinalized_rows"] == 1
    assert rows[0]["volume"] == 1200 and not quality["production_eligible"]
    for bad in (
        raw_row(stock_code="600001"),
        raw_row(high=8),
        raw_row(volume=-1),
        raw_row(amount=float("nan")),
        raw_row(trade_time="2026-01-04 09:31:00"),
    ):
        _, state = normalize([raw_row(), bad], "SH600000", retrieved.date(), retrieved.date(), "1min", retrieved)
        assert state["status"] == "quarantined"
    _, duplicate = normalize([raw_row(), raw_row()], "SH600000", retrieved.date(), retrieved.date(), "1min", retrieved)
    assert duplicate["invalid_rows"] == 1
    canonical = [{**rows[0], "close": 10.55, "volume": 1300}]
    diff = compare_raw(rows, canonical, [rows[0]["time"]])
    assert diff["deviation_count"] == 2 and diff["diagnostic_only"]
    assert compare_raw([], canonical)["completeness"] == "unknown"
    with pytest.raises(ValueError):
        compare_raw(rows, [{**canonical[0], "symbol": "SZ000001"}])


def test_histograms_constant_missing_overflow_and_cohorts():
    assert reference_histogram([np.nan, np.inf])["status"] == "unavailable"
    ref = reference_histogram([2, 2, 2, np.nan])
    assert ref["constant"] and sum(ref["counts"]) == 4
    assert psi(ref, [2, 2, 2, np.nan])["psi"] == pytest.approx(0)
    assert psi(ref, [1, 3, np.nan])["counts"] == [1, 0, 1, 1]
    index = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-01-05 09:35", "2026-01-05 09:40"]), ["SH600000", "SZ000001"]],
        names=["datetime", "instrument"],
    )
    features = pd.DataFrame({"A": [1, 2, 100, 200]}, index=index)
    reference = training_reference(features, features.A, "5min", [3])
    result = distribution_drift(
        reference, features.iloc[:2], {"SH600000": 1, "SZ000001": 2}, "5min", "2026-01-06 09:35"
    )
    assert result["features"]["A"]["psi"] == pytest.approx(0)
    assert distribution_drift(reference, features.iloc[:2], {}, "5min", "2026-01-06 09:45")["status"] == "unavailable"


def test_cross_section_samples_constants_alignment_and_rollup():
    scores = {str(i): i for i in range(30)}
    labels = {str(i): i / 100 for i in reversed(range(30))}
    assert cross_section(scores, labels)["rank_ic"] == pytest.approx(1)
    assert cross_section(scores, {**labels, "unscored": 100})["valid_pairs"] == 30
    assert cross_section(scores, {k: 0 for k in scores})["reason"] == "constant_scores_or_labels"
    assert cross_section(scores, dict(list(labels.items())[:29]))["ic"] is None
    rows = [
        {"session": str(date(2026, 1, 1) + timedelta(days=i)), "data": {"realized": cross_section(scores, labels)}}
        for i in range(20)
    ]
    assert rolling_quality(rows)["rolling"]["20"]["ic"] == pytest.approx(1)
    rows.append(rows[-1])
    assert len(rolling_quality(rows)["sessions"]) == 20
    rows[-1] = {"session": "2026-01-21", "data": {"realized": {"status": "insufficient_data"}}}
    assert rolling_quality(rows)["rolling"]["20"]["ic"] is None


def test_exact_calendar_label_windows_and_corporate_action():
    days = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12", "2026-01-13"]
    cutoff = datetime(2026, 1, 5, 15, tzinfo=CN)
    entry, exit_at = label_window(days, cutoff, "day")
    assert entry.isoformat() == "2026-01-06T09:30:00+08:00"
    assert exit_at.isoformat() == "2026-01-13T09:30:00+08:00"
    assert label_window(days, cutoff.replace(hour=11, minute=25), "5min") is None
    minute_entry, minute_exit = label_window(days, cutoff.replace(hour=10), "5min")
    assert minute_entry.hour == 10 and minute_entry.minute == 10 and minute_exit.day == 6
    assert adjusted_return(10, 1, 5, 2) == 0
    assert adjusted_return(10, 0, 5, 2) is None


class ObservationConnection:
    def __init__(self):
        self.watermark, self.revision = 50, 1
        self.calendar = [
            {"day": date(2026, 1, 5) + timedelta(days=i), "open": True, "n": 2, "agreed": True} for i in range(31)
        ]

    def execute(self, query, params=()):
        if "max(id)" in query:
            return SimpleNamespace(fetchone=lambda: {"id": self.watermark})
        if "FROM calendars" in query:
            return SimpleNamespace(fetchall=lambda: self.calendar)
        assert "dataset_id<=%s" in query
        return SimpleNamespace(
            fetchall=lambda: [{"symbol": "SH600000", "day": date(2026, 1, 6), "dataset_id": self.revision}]
        )


def test_diagnostic_identity_ignores_unrelated_watermarks_and_elapsed_days():
    conn = ObservationConnection()
    prediction = {"as_of": datetime(2026, 1, 5, 15, tzinfo=CN), "frequency": "day", "data": {"scores": {"SH600000": 1}}}
    at = datetime(2026, 1, 20, 15, tzinfo=CN)
    first = build_observation(conn, prediction, at)
    conn.watermark = 100
    second = build_observation(conn, prediction, at + timedelta(days=1))
    assert first["source_revision"] == second["source_revision"]
    conn.revision = 2
    assert build_observation(conn, prediction, at)["source_revision"] != first["source_revision"]
    early = build_observation(conn, prediction, prediction["as_of"])
    assert not early["data"]["mature"]
    early["observed_at"] = prediction["as_of"]

    class NoReads:
        def rows(self, *args):
            pytest.fail("Premature labels read future prices")

    assert realized_quality(NoReads(), prediction, early)["reason"] == "label_not_mature"
    calendar = [{**row, "day": str(row["day"])} for row in conn.calendar]
    calendar[1]["agreed"] = False
    assert len(calendar_days(calendar, prediction["as_of"])) == 1


@pytest.mark.parametrize(
    "migration", ["0006_research_sources.py", "0007_research_experiments.py", "0008_model_diagnostics.py"]
)
def test_research_migrations_revoke_inherited_mutation_grants(monkeypatch, migration):
    import re
    import runpy
    from alembic import op
    from quant_platform import storage

    statements = []
    monkeypatch.setattr(op, "execute", statements.append)
    path = Path(storage.__file__).parent / "migrations" / "versions" / migration
    runpy.run_path(str(path))["upgrade"]()
    sql = " ".join(statements)
    tables = set(re.findall(r"CREATE TABLE (\w+)", sql))
    revoked = re.search(r"REVOKE UPDATE,DELETE,TRUNCATE ON (.*?) FROM quant_worker", sql, re.DOTALL)
    granted = re.search(r"GRANT SELECT,INSERT ON (.*?) TO quant_worker", sql, re.DOTALL)
    assert tables and revoked and granted
    assert tables == {name.strip() for name in revoked[1].split(",")}
    assert tables == {name.strip() for name in granted[1].split(",")}


def test_research_child_rejects_arbitrary_modules():
    with pytest.raises(PipelineBlocked, match="allowlisted"):
        run_research("os", {}, 1, Event())


@pytest.mark.parametrize("outcome", ["success", "deadline", "cancel", "exit", "invalid", "nonobject", "logs", "result"])
def test_research_child_secret_isolation_bounds_and_cleanup(monkeypatch, outcome):
    from quant_platform.research import process
    from quant_platform.storage import LostLease

    for key in ("QUANT_DATABASE_URL", "QUANT_API_TOKEN", "QUANT_TUSHARE_TOKEN", "HTTPS_PROXY", "PYTHONPATH"):
        monkeypatch.setenv(key, "fixture-secret-must-not-leak")
    killed, waited = [], []
    running = outcome in {"deadline", "cancel", "logs"}
    child = SimpleNamespace(
        pid=987654,
        returncode=1 if outcome == "exit" else 0,
        poll=lambda: None if running else 0,
        wait=lambda: waited.append(True),
    )

    def spawn(args, **kwargs):
        assert kwargs["start_new_session"] and kwargs["close_fds"]
        assert args[1:3] == ["-m", "quant_platform.research.experiments"]
        assert set(kwargs["env"]) == {
            "PATH",
            "HOME",
            "TMPDIR",
            "XDG_CACHE_HOME",
            "PYTHONUNBUFFERED",
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
        }
        assert "fixture-secret" not in str(kwargs["env"])
        assert kwargs["env"]["HOME"] == kwargs["cwd"]
        assert json.load(kwargs["stdin"]) == {"fixture": True}
        if outcome == "logs":
            kwargs["stdout"].truncate(32 * 1024 * 1024 + 1)
        result = Path(args[-1])
        result.write_text("{" if outcome == "invalid" else "[]" if outcome == "nonobject" else '{"ok":true}')
        if outcome == "result":
            with result.open("r+b") as stream:
                stream.truncate(32 * 1024 * 1024 + 1)
        return child

    monkeypatch.setattr(process.subprocess, "Popen", spawn)
    monkeypatch.setattr(process.os, "killpg", lambda pid, sig: killed.append(pid))
    stop = Event()
    if outcome == "cancel":
        stop.set()
    if outcome == "success":
        assert run_research("quant_platform.research.experiments", {"fixture": True}, 60, stop) == {"ok": True}
    else:
        with pytest.raises(LostLease if outcome == "cancel" else PipelineBlocked):
            run_research(
                "quant_platform.research.experiments", {"fixture": True}, 0 if outcome == "deadline" else 60, stop
            )
    assert waited == [True]
    assert killed == ([child.pid] if running else [])


def test_research_artifacts_are_referenced_and_checksum_verified(settings, tmp_path):
    from quant_platform.storage.research import store_artifact
    from quant_platform.storage.artifacts import references, artifact_files

    settings = settings.model_copy(update={"artifact_root": tmp_path})
    artifact = store_artifact(settings, "snapshot", {"rows": []}, {"source_revision": 1})

    class Connection:
        def execute(self, query):
            if "to_regclass" in query:
                return SimpleNamespace(fetchone=lambda: {"name": "research_artifacts"})
            return SimpleNamespace(fetchall=lambda: [artifact] if "FROM research_artifacts" in query else [])

    refs = references(Connection())
    assert artifact["path"] in refs
    files = artifact_files(tmp_path, refs)
    assert len(files) == 2
    payload = Path(tmp_path / artifact["path"] / "data.json")
    payload.write_text(json.dumps({"rows": ["corrupt"]}))
    with pytest.raises(PipelineBlocked):
        artifact_files(tmp_path, refs)


def test_research_archive_roundtrip_and_retention_protects_references(settings, tmp_path):
    import io
    import os
    import tarfile
    from quant_platform.adapters.qlib.data import seal, checksum
    from quant_platform.storage import artifacts
    from quant_platform.storage.research import store_artifact

    settings = settings.model_copy(update={"artifact_root": tmp_path / "source"})
    root = settings.artifact_root
    recorder = root / "experiments" / "123" / uuid4().hex
    recorder.mkdir(parents=True)
    (recorder / "quap-owner.json").write_text(
        json.dumps({"format": "quap-qlib-recorder-v1", "recorder_id": recorder.name})
    )
    recorder_ref = {"path": recorder.relative_to(root).as_posix(), "manifest": seal(recorder, {"fixture": True})}
    rows = [
        store_artifact(settings, kind, {"fixture": kind}, {"recorder": recorder_ref} if kind == "experiment" else {})
        for kind in ("snapshot", "experiment", "diagnostics")
    ]
    backup_only = store_artifact(settings, "snapshot", {}, {})
    orphan = store_artifact(settings, "diagnostics", {}, {})

    class Connection:
        running = True

        def execute(self, query, params=()):
            if "to_regclass" in query:
                return SimpleNamespace(fetchone=lambda: {"name": "research_artifacts"})
            if "FROM jobs" in query:
                return SimpleNamespace(fetchone=lambda: {"id": 1} if self.running else None)
            data = (
                rows
                if "FROM research_artifacts" in query
                else [{"data": {"artifacts": [backup_only["path"]]}}] if "FROM artifact_backups" in query else []
            )
            return SimpleNamespace(fetchall=lambda: data)

    conn = Connection()
    references = artifacts.references(conn)
    assert len(references) == 4 and recorder_ref["path"] in references
    files = artifacts.artifact_files(root, references)
    dump = tmp_path / "fixture.dump"
    dump.write_bytes(b"not-a-real-database-dump")
    files["database.dump"] = (dump, checksum(dump))
    manifest = {
        "format": "quap-snapshot-v1",
        "snapshot_at": datetime.now(CN).isoformat(),
        "artifacts": references,
        "files": {name: value[1] for name, value in files.items()},
    }
    bundle = tmp_path / "fixture.tar"
    with tarfile.open(bundle, "w") as archive:
        for name, (path, _) in files.items():
            archive.add(path, arcname=name, recursive=False)
        body = json.dumps(manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(body)
        archive.addfile(info, io.BytesIO(body))
    destination = tmp_path / "unpacked"
    destination.mkdir()
    assert artifacts.unpack_verified(bundle, destination) == manifest
    for relative, expected in references.items():
        artifacts.verify(destination / "artifacts" / relative, expected)
    at = datetime.now(CN)
    ancient = (at - timedelta(days=10)).timestamp()
    for folder in artifacts.owned_directories(root):
        os.utime(folder, (ancient, ancient))
    assert artifacts.prune_orphans(conn, settings, at) == 0
    conn.running = False
    assert artifacts.prune_orphans(conn, settings, at) == 1
    assert not (root / orphan["path"]).exists()
    assert (root / backup_only["path"]).is_dir()
    assert all((root / relative).is_dir() for relative in references)
    (root / rows[0]["path"] / "data.json").write_text("corrupt")
    with pytest.raises(PipelineBlocked, match="checksum"):
        artifacts.artifact_files(root, references)


@pytest.mark.skipif(importlib.util.find_spec("qlib") is None, reason="Requires isolated pinned Qlib runtime")
def test_real_qlib_custom_factors_fold_and_safe_model_bundle(tmp_path, monkeypatch):
    from test_qlib_workflow import fixture_generation
    from quant_platform.adapters.qlib.runtime import initialize, train_artifact, load_bundle
    from quant_platform.research.factors import research_identity
    from quant_platform.research import experiments
    from quant_platform.research.experiments import evaluate_fold, diagnose_features
    from qlib.data import D

    path, manifest = fixture_generation(tmp_path / "generation", "day")
    expressions = [compile_factor("$close/Ref($close,5)-1"), compile_factor("Mean($volume,5)")]
    config = {
        "id": str(uuid4()),
        "frequency": "day",
        "features": [{"name": f"Factor{i}", **value} for i, value in enumerate(expressions)],
        "learning_rate": 0.05,
        "num_leaves": 5,
        "rounds": 10,
        "seed": 42,
        "train_sessions": 20,
        "validation_sessions": 10,
        "test_sessions": 10,
    }
    config["configuration_hash"] = digest({k: v for k, v in config.items() if k != "id"})
    initialize(path)
    frame = D.features(["SH600000"], [value["expression"] for value in expressions])
    assert not frame.dropna().empty
    folds, holdout = development_folds(manifest["days"], config)
    handlers = []
    make_handler = experiments.make_handler

    def observed_handler(limited, segments):
        handler = make_handler(limited, segments)
        norm = handler.infer_processors[1]
        assert pd.Timestamp(norm.fit_start_time) == pd.Timestamp(segments["train"][0])
        assert pd.Timestamp(norm.fit_end_time) == pd.Timestamp(segments["train"][1])
        assert limited["days"][-1] < holdout[0]
        handlers.append(handler)
        return handler

    monkeypatch.setattr(experiments, "make_handler", observed_handler)
    payload = {
        "path": str(path),
        "manifest": manifest,
        "configuration": config,
        "reserved_test": holdout,
        "experiment_root": str(tmp_path / "experiments"),
        "execution_identity": research_identity(),
    }
    for index, fold in enumerate(folds):
        result = evaluate_fold({**payload, "fold": fold, "output": str(tmp_path / f"trial-{index}")})
        assert not result["summary"]["reserved_test_accessed"]
        assert result["summary"]["segments"] == fold
    assert len({id(handler.infer_processors[1]) for handler in handlers}) == 3
    with pytest.raises(PipelineBlocked, match="held-out"):
        evaluate_fold({**payload, "fold": {**folds[-1], "valid": holdout}})
    monkeypatch.setattr(experiments, "make_handler", make_handler)
    child_result = run_research(
        "quant_platform.research.experiments",
        {**payload, "action": "fold", "fold": folds[0], "output": str(tmp_path / "child-trial")},
        120,
        Event(),
    )
    assert child_result["summary"]["selection_data"] == "validation_only"
    assert not child_result["summary"]["reserved_test_accessed"]
    output = tmp_path / "model"
    model_manifest = train_artifact(path, manifest, output, tmp_path / "experiments", training_configuration=config)
    bundle = load_bundle(output, model_manifest)
    assert bundle["training_configuration"] == config
    assert (output / "diagnostic_reference.json").is_file()
    drift = diagnose_features(
        {
            "path": str(path),
            "manifest": manifest,
            "model_path": str(output),
            "model_manifest": model_manifest,
            "scores": {"SH600000": 0.1},
        }
    )
    assert drift["status"] == "available"
    assert set(drift["features"]) == {"R_Factor0", "R_Factor1"}
    with pytest.raises(PipelineBlocked):
        load_bundle(output, {**model_manifest, "training_configuration": None})


class ReadOnlyStore:
    def __init__(self):
        self.queries = []

    def rows(self, query, params=()):
        assert query.startswith(("SELECT", "WITH"))
        self.queries.append((query, params))
        return []

    def transaction(self):
        pytest.fail("A research GET attempted a write transaction")


def test_authenticated_research_reads_validation_and_pagination(settings):
    store = ReadOnlyStore()
    with TestClient(create_app(settings, database=store)) as client:
        paths = [
            "/research-sources",
            "/research-collections",
            "/research-snapshots",
            "/research-quality",
            "/factors",
            "/factor-sets",
            "/training-configurations",
            "/research-experiments",
        ]
        for path in paths:
            assert client.get("/api/v1" + path).status_code == 401
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        for path in paths:
            response = client.get("/api/v1" + path)
            assert response.status_code == 200, response.text
        for path in paths[1:]:
            assert client.get("/api/v1" + path + "?limit=101").status_code == 422
        assert client.get("/api/v1/research-snapshots?code=invalid").status_code == 422
        response = client.post(
            "/api/v1/factors",
            json={
                "name": "Bad",
                "author": "fixture",
                "expression": "Ref($close,-1)",
                "request_key": "x",
                "expected_revision": 0,
            },
        )
        assert response.status_code == 422
        assert (
            client.put(
                "/api/v1/research-sources/adata-eastmoney-day",
                json={"enabled": True, "request_key": "x", "expected_revision": 0},
            ).status_code
            == 422
        )
        assert client.get(f"/api/v1/models/{uuid4()}/diagnostics").status_code == 404


def test_trial_factor_evidence_is_scoped_bounded_read_only_and_verified(settings, tmp_path):
    from quant_platform.storage.research import store_artifact
    from quant_platform.research.diagnostics import factor_diagnostics

    settings = settings.model_copy(update={"artifact_root": tmp_path})
    index = pd.MultiIndex.from_product(
        [pd.date_range("2026-01-01", periods=3), range(35)], names=["datetime", "instrument"]
    )
    features = pd.DataFrame({"A": np.arange(105), "B": np.arange(105)[::-1]}, index=index)
    evidence = factor_diagnostics(features, features.A)
    artifact = store_artifact(settings, "experiment", {"factor_diagnostics": evidence}, {})
    experiment_id, trial_id, config_id = uuid4(), uuid4(), uuid4()

    class TrialStore(ReadOnlyStore):
        def rows(self, query, params=()):
            super().rows(query, params)
            assert "t.experiment_id=%s AND t.id=%s AND r.state='succeeded'" in query
            return (
                [{**artifact, "configuration_id": config_id, "fold": 1}] if params == (experiment_id, trial_id) else []
            )

    store = TrialStore()
    path = f"/api/v1/research-experiments/{experiment_id}/trials/{trial_id}/diagnostics"
    with TestClient(create_app(settings, database=store)) as client:
        assert client.get(path).status_code == 401
        assert store.queries == []
        client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
        result = client.get(path + "?feature=B&limit=1&offset=1")
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["feature"] == "B" and body["research_only"]
        assert body["selection_data"] == "validation_only"
        assert body["quality"]["next_offset"] == 2
        assert len(body["quality"]["items"]) == 1
        assert body["quality"]["items"][0]["rank_ic"] == pytest.approx(-1)
        assert client.get(path + "?feature=unknown").status_code == 422
        assert client.get(path + "?limit=253").status_code == 422
        assert client.get(path.replace(str(experiment_id), str(uuid4()))).status_code == 404
        (tmp_path / artifact["path"] / "data.json").write_text("{}")
        assert client.get(path).status_code == 409


@pytest.mark.skipif(importlib.util.find_spec("adata") is None, reason="Requires isolated research-data extra")
def test_real_pinned_adata_adapter_with_fixture_transport():
    def response(request):
        return httpx.Response(
            200,
            json={"data": {"code": "600000", "preClose": 10, "trends": ["2026-01-05 09:31,10,10.5,11,9,12,12000,10"]}},
        )

    payload = {
        "source_id": "adata-eastmoney-intraday",
        "symbol": "SH600000",
        "start": "2026-01-05",
        "end": "2026-01-05",
    }
    result = collect_one(payload, httpx.MockTransport(response))
    assert result["status"] == "available", result
    assert result["records"][0]["volume"] == 1200
    assert result["records"][0]["open"] == 10
    for malformed in ({"code": "600000", "trends": []}, None, {"code": "600000", "preClose": 10, "trends": ["bad"]}):
        unavailable = collect_one(
            payload, httpx.MockTransport(lambda request: httpx.Response(200, json={"data": malformed}))
        )
        assert unavailable["status"] == "unavailable" and not unavailable["records"]
    daily = collect_one(
        {**payload, "source_id": "adata-eastmoney-day"},
        httpx.MockTransport(lambda request: pytest.fail("HTTP daily request escaped policy")),
    )
    assert daily["status"] == "unavailable" and daily["reason"] == "transport_policy_rejected"


@pytest.mark.parametrize("already_exposed", [False, True])
def test_holdout_disclosure_is_immutable_across_retry(already_exposed):
    from quant_platform.storage.experiments import test_exposure

    class Connection:
        def __init__(self):
            self.record = None
            self.checks = 0

        def execute(self, query, params=()):
            result = None
            if query.startswith("SELECT *"):
                result = self.record
            elif "UNION ALL" in query:
                self.checks += 1
                result = {"exists": True} if already_exposed else None
            elif query.startswith("INSERT"):
                assert self.record is None
                self.record = {
                    "run_id": params[0],
                    "freeze_id": params[1],
                    "generation_id": params[2],
                    "data": params[3].obj,
                }
            else:
                assert "pg_advisory_xact_lock" in query
            return SimpleNamespace(fetchone=lambda: result)

    conn = Connection()
    generation_id = uuid4()
    run = {
        "id": uuid4(),
        "frequency": "day",
        "snapshot": {"reserved_test": ["2026-01-01", "2026-06-01"], "research_freeze_id": str(uuid4())},
    }
    first = test_exposure(conn, run, generation_id)
    assert first["previously_exposed"] == already_exposed
    assert first["unbiased_holdout"] != already_exposed
    assert test_exposure(conn, run, generation_id) == first
    assert conn.checks == 1
    with pytest.raises(PipelineBlocked, match="exposure changed"):
        test_exposure(conn, run, uuid4())
    changed = {**run, "snapshot": {**run["snapshot"], "reserved_test": ["2026-02-01", "2026-06-01"]}}
    with pytest.raises(PipelineBlocked, match="exposure changed"):
        test_exposure(conn, changed, generation_id)


@pytest.mark.parametrize("pending", [0, 999, 1000])
def test_diagnostic_scheduler_bounds_queue_pages_and_cooldown(monkeypatch, pending):
    from quant_platform.jobs import diagnostics

    at = datetime(2026, 1, 20, tzinfo=CN)
    monkeypatch.setattr(diagnostics, "now", lambda: at)
    monkeypatch.setattr(
        diagnostics,
        "build_observation",
        lambda conn, row, observed: {"source_revision": "fixture", "observed_at": observed.isoformat(), "data": {}},
    )

    class Connection:
        def __init__(self):
            self.state = None
            self.queries = []

        def execute(self, query, params=()):
            self.queries.append(query)
            result, rows = None, []
            if "FROM settings" in query:
                result = {"value": self.state} if self.state else None
            elif "count(*)" in query:
                result = {"n": pending}
            elif "FROM prediction_runs" in query:
                assert params[-1] == min(25, 1000 - pending)
                rows = [{"id": uuid4()} for _ in range(params[-1])]
            else:
                assert "FROM model_diagnostics" in query
            return SimpleNamespace(fetchone=lambda: result, fetchall=lambda: rows)

    class Store:
        def __init__(self):
            self.jobs = []

        def enqueue(self, conn, kind, queue, dedupe, payload, priority):
            assert (kind, queue, priority) == ("qlib_diagnostics", "qlib-diagnostics", -10)
            self.jobs.append((dedupe, payload))

        def set_setting(self, conn, key, state):
            assert key == "schedule:diagnostics"
            conn.state = state

    conn, store = Connection(), Store()
    diagnostics.schedule_diagnostics(store, conn)
    assert len(store.jobs) == min(25, 1000 - pending)
    assert all(payload["observation"]["source_revision"] == "fixture" for _, payload in store.jobs)
    if pending < 1000:
        queries = len(conn.queries)
        diagnostics.schedule_diagnostics(store, conn)
        assert len(conn.queries) == queries + 1
        assert len(store.jobs) == min(25, 1000 - pending)


@pytest.mark.parametrize("frequency", ["day", "5min"])
def test_realized_labels_use_exact_prices_adjustments_and_original_universe(frequency):
    cutoff = datetime(2026, 1, 5, 15 if frequency == "day" else 10, tzinfo=CN)
    calendar = [
        {"day": str(stamp.date()), "n": 2, "agreed": True, "open": stamp.weekday() < 5}
        for stamp in pd.date_range("2026-01-05", "2026-01-13")
    ]
    entry, exit_at = label_window(calendar_days(calendar, cutoff), cutoff, frequency)
    codes = [f"SH{600000 + i}" for i in range(35)]
    prediction = {
        "frequency": frequency,
        "as_of": cutoff,
        "data": {"scores": {code: i for i, code in enumerate(codes)}},
    }
    daily, constraints, minutes = [], [], []
    for i, code in enumerate(codes):
        daily.extend(
            [
                {
                    "symbol": code,
                    "day": entry.date(),
                    "factor": 1,
                    "data": {"open": 10 if frequency == "day" else 999, "volume": 100},
                },
                {
                    "symbol": code,
                    "day": exit_at.date(),
                    "factor": 2,
                    "data": {
                        "open": 5 * (1 + i / 100) if frequency == "day" else 999,
                        "close": 5 * (1 + i / 100),
                        "volume": 100,
                    },
                },
            ]
        )
        constraints.extend(
            {"symbol": code, "day": day, "data": {"suspended": False}} for day in (entry.date(), exit_at.date())
        )
        minutes.append({"symbol": code, "open": 10, "volume": 100})
    daily[1]["factor"] = None
    daily[3]["data"]["volume"] = 0
    constraints[4]["data"]["suspended"] = True
    daily = [row for row in daily if not (row["symbol"] == codes[3] and row["day"] == exit_at.date())]
    constraints = [row for row in constraints if row["symbol"] != codes[4]]

    class Store:
        def rows(self, query, params):
            assert params[0] == codes and params[-1] == 77
            if "FROM daily_bars" in query:
                return list(reversed(daily))
            if "FROM market_constraints" in query:
                return list(reversed(constraints))
            assert frequency == "5min" and "FROM minute_bars" in query and params[1] == entry
            return list(reversed(minutes))

    observation = {"observed_at": exit_at, "data": {"calendar": calendar, "watermark": 77}}
    result = realized_quality(Store(), prediction, observation)
    assert result["scored_count"] == 35 and result["valid_pairs"] == 30
    assert result["ic"] == pytest.approx(1) and result["rank_ic"] == pytest.approx(1)
    assert len(result["exclusions"]) == 5
    assert result["entry"] == entry.isoformat() and result["exit"] == exit_at.isoformat()
