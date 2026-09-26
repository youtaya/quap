"""Trusted Qlib research execution; no database, provider credentials, or promotion."""

import json
from pathlib import Path
import sys
import time

import pandas as pd

from quant_platform.adapters.qlib.data import seal, verify
from quant_platform.adapters.qlib.runtime import initialize, load_bundle, make_handler, port_record, report_metrics
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.research.diagnostics import cross_section, distribution_drift, factor_diagnostics
from quant_platform.research.factors import research_identity


def evaluate_fold(payload):
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH
    from qlib.workflow import R
    from qlib.workflow.record_temp import SignalRecord
    from mlflow.tracking import MlflowClient

    started = time.monotonic()
    if payload.get("execution_identity") != research_identity():
        raise PipelineBlocked("Research parent and child execution identities differ.")
    manifest = payload["manifest"]
    verify(Path(payload["path"]), manifest)
    config = payload["configuration"]
    fold = payload["fold"]
    if fold["valid"][1] >= payload["reserved_test"][0]:
        raise PipelineBlocked("Development may not access the held-out test segment.")
    purge = 6 if manifest["frequency"] == "day" else 1
    last = manifest["days"].index(fold["valid"][1]) + purge
    if manifest["days"][last] >= payload["reserved_test"][0]:
        raise PipelineBlocked("Development label horizon overlaps the reserved test.")
    limited = {**manifest, "days": manifest["days"][: last + 1], "training_configuration": config}
    experiment_root = Path(payload["experiment_root"])
    initialize(payload["path"], experiment_root)
    segments = {**fold, "test": fold["valid"]}
    handler = make_handler(limited, segments)
    dataset = DatasetH(handler=handler, segments=segments)
    prepared = time.monotonic()
    model = LGBModel(
        loss="mse",
        learning_rate=config["learning_rate"],
        num_leaves=config["num_leaves"],
        seed=config["seed"],
        num_threads=1,
        early_stopping_rounds=50,
        num_boost_round=config["rounds"],
    )
    output = Path(payload["output"])
    output.mkdir(parents=True, exist_ok=False)
    with R.start(experiment_name="quap-development"):
        recorder = R.get_recorder()
        client = MlflowClient(tracking_uri=experiment_root.resolve().as_uri())
        recorder_folder = experiment_root.resolve() / client.get_run(recorder.id).info.experiment_id / recorder.id
        (recorder_folder / "quap-owner.json").write_text(
            json.dumps({"format": "quap-qlib-recorder-v1", "recorder_id": recorder.id})
        )
        R.log_params(
            stage="development_only",
            configuration_id=config["id"],
            generation=manifest["id"],
            segments=json.dumps(fold),
        )
        model.fit(dataset, verbose_eval=False)
        trained = time.monotonic()
        SignalRecord(model, dataset, recorder).generate()
        prediction = model.predict(dataset, segment="valid")
        labels = dataset.prepare("valid", col_set="label", data_key="raw")
        features = dataset.prepare("valid", col_set="feature", data_key="raw")
        qualities = []
        for stamp, scores in prediction.groupby(level="datetime"):
            qualities.append({"time": str(stamp), **cross_section(scores, labels.iloc[:, 0].reindex(scores.index))})
        targets = {day: pool["daily_weights"] for day, pool in manifest.get("point_in_time_pools", {}).items()}
        if manifest["frequency"] == "5min" and not targets:
            raise PipelineBlocked("Minute experiments require qualified dated daily targets.")
        report = port_record(recorder, prediction, segments, manifest, float(prediction.quantile(0.2)), targets or None)
        result = {
            "segments": fold,
            "selection_data": "validation_only",
            "reserved_test_accessed": False,
            "quality": qualities,
            "metrics": report_metrics(report),
            "factor_diagnostics": factor_diagnostics(features, labels),
            "latency": {
                "feature_preparation": prepared - started,
                "training": trained - prepared,
                "total": time.monotonic() - started,
            },
        }
        prediction.to_csv(output / "validation_predictions.csv")
        report.to_csv(output / "validation_backtest.csv")
        (output / "data.json").write_text(json.dumps(result, allow_nan=False))
    recorder_manifest = seal(recorder_folder, {"recorder_id": recorder.id, "research_only": True})
    artifact = seal(
        output,
        {
            "format": "quap-research-v1",
            "kind": "experiment",
            "research_only": True,
            "configuration_id": config["id"],
            "generation_id": manifest["id"],
            "execution_identity": payload["execution_identity"],
            "recorder": {
                "path": recorder_folder.relative_to(experiment_root.resolve().parent).as_posix(),
                "manifest": recorder_manifest,
            },
        },
    )
    return {
        "manifest": artifact,
        "summary": {key: value for key, value in result.items() if key != "factor_diagnostics"},
    }


def diagnose_features(payload):
    from qlib.data.dataset import DatasetH

    manifest = payload["manifest"]
    verify(Path(payload["path"]), manifest)
    initialize(payload["path"])
    model_path = Path(payload["model_path"])
    bundle = load_bundle(model_path, payload["model_manifest"])
    reference_path = model_path / "diagnostic_reference.json"
    if "diagnostic_reference.json" not in payload["model_manifest"].get("files", {}):
        return {"status": "unavailable", "reason": "model_has_no_training_baseline"}
    target = pd.Timestamp(manifest["as_of"]).tz_convert("Asia/Shanghai").tz_localize(None)
    target = target.normalize() if manifest["frequency"] == "day" else target
    segments = {"test": (str(target), str(target))}
    handler = make_handler(
        {**manifest, "training_configuration": bundle.get("training_configuration")},
        segments,
        bundle["processors"],
        inference=True,
    )
    features = DatasetH(handler=handler, segments=segments).prepare("test", col_set="feature", data_key="raw")
    features = features.loc[features.index.get_level_values("instrument").isin(payload["scores"])]
    reference = json.loads(reference_path.read_text())
    return distribution_drift(reference, features, payload["scores"], manifest["frequency"], target)


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read(32 * 1024 * 1024))
    action = payload.get("action", "fold")
    if action not in {"fold", "diagnostics"}:
        raise PipelineBlocked("Unknown research action.")
    result = evaluate_fold(payload) if action == "fold" else diagnose_features(payload)
    Path(sys.argv[1]).write_text(json.dumps(result, allow_nan=False))
