"""Isolated real-Qlib training, evaluation, and inference; no native fallback."""

import json
import math
from datetime import datetime, time
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from quant_platform.domain import CN, digest, now
from quant_platform.domain.labels import DAILY_LABEL
from quant_platform.research.factors import feature_spec
from quant_platform.domain.workflow import DATA_CONTRACT, ENGINE_VERSION, PipelineBlocked, intraday_window
from quant_platform.storage import jsonb
from .data import safe_artifact, seal, verify


def initialize(path, experiment_root=None):
    import qlib
    from importlib.metadata import version

    if version("pyqlib") != "0.9.7" or version("lightgbm") != "4.7.0":
        raise PipelineBlocked("The pinned Qlib/LightGBM runtime is required.")
    kwargs = {}
    if experiment_root:
        experiment_root = Path(experiment_root).resolve()
        experiment_root.mkdir(parents=True, exist_ok=True)
        kwargs["exp_manager"] = {
            "class": "MLflowExpManager",
            "module_path": "qlib.workflow.expm",
            "kwargs": {"uri": experiment_root.as_uri(), "default_exp_name": "quap"},
        }
    qlib.init(provider_uri=str(path), region="cn", kernels=1, expression_cache=None, dataset_cache=None, **kwargs)


def validate_generation(path, manifest):
    from qlib.data import D

    verify(path, manifest)
    initialize(path)
    calendar = D.calendar(freq=manifest["frequency"])
    if not len(calendar):
        raise PipelineBlocked("Qlib calendar is empty.")
    code = next(iter(manifest["coverage"]))
    frame = D.features(
        [code], ["$close", "$factor", "$raw_close", "$volume", "$raw_volume", "$vwap"], freq=manifest["frequency"]
    )
    valid = frame.dropna(subset=["$close", "$factor", "$raw_close"])
    if valid.empty or not np.allclose(valid["$close"] / valid["$factor"], valid["$raw_close"], rtol=1e-5):
        raise PipelineBlocked("Real Qlib price restoration failed.")
    volumes = frame.dropna(subset=["$volume", "$factor", "$raw_volume"])
    if not np.allclose(volumes["$volume"] * volumes["$factor"], volumes["$raw_volume"], rtol=1e-5):
        raise PipelineBlocked("Real Qlib physical-volume restoration failed.")


def segments_for(days, frequency, sizes=None):
    train, valid, test = sizes or ((756, 252, 252) if frequency == "day" else (120, 40, 60))
    purge = 6 if frequency == "day" else 1
    needed = train + valid + test + 2 * purge
    if len(days) < needed + (60 if frequency == "day" else 2):
        raise PipelineBlocked(f"Training needs at least {needed} usable sessions plus feature warm-up.")
    if len(days) < needed + purge + (60 if frequency == "day" else 2):
        raise PipelineBlocked("Held-out labels require completed future reference sessions.")
    window = days[-needed - purge : -purge]
    return {
        "train": (window[0], window[train - 1]),
        "valid": (window[train + purge], window[train + purge + valid - 1]),
        "test": (window[train + valid + 2 * purge], window[-1]),
    }


def make_handler(manifest, segments, processors=None, inference=False):
    from qlib.contrib.data.handler import Alpha158
    from qlib.data.dataset.handler import DataHandlerLP

    fit_start, fit_end = segments.get("train", segments["test"])
    if processors is None:
        processors = [
            {"class": "ProcessInf"},
            {
                "class": "RobustZScoreNorm",
                "kwargs": {
                    "fields_group": "feature",
                    "fit_start_time": fit_start,
                    "fit_end_time": fit_end,
                    "clip_outlier": True,
                },
            },
            {"class": "Fillna", "kwargs": {"fields_group": "feature", "fill_value": 0}},
        ]
    codes = sorted(set(manifest["coverage"]) - {"SH000300"})
    common = {
        "instruments": codes,
        "start_time": manifest["days"][0],
        "end_time": str(pd.Timestamp(manifest["days"][-1]) + pd.Timedelta(hours=23, minutes=59)),
        "infer_processors": processors,
        "learn_processors": [] if inference else [{"class": "DropnaLabel"}],
        "init_data": False,
    }
    custom = feature_spec(manifest.get("training_configuration"))
    if manifest["frequency"] == "day":
        label = (["$label" if inference else DAILY_LABEL], ["LABEL0"])
        if custom:
            handler = DataHandlerLP(
                **common,
                data_loader={
                    "class": "QlibDataLoader",
                    "kwargs": {
                        "freq": "day",
                        "config": {"feature": custom, "label": label},
                    },
                },
            )
        else:
            handler = Alpha158(**common, fit_start_time=fit_start, fit_end_time=fit_end, label=label)
    else:
        expressions, names = [], []
        for n in (1, 3, 6, 12):
            expressions += [
                f"$close/Ref($close,{n})-1",
                f"Mean($high-$low,{n})/$close",
                f"$volume/(Mean($volume,{n})+1e-12)",
            ]
            names += [f"return_{n}", f"range_{n}", f"volume_{n}"]
        expressions += ["$close/$vwap-1", "$close/$daily_close-1", "$slot"]
        names += ["vwap_deviation", "daily_context", "time_of_day"]
        handler = DataHandlerLP(
            **common,
            data_loader={
                "class": "QlibDataLoader",
                "kwargs": {
                    "freq": "5min",
                    "config": {"feature": (expressions, names), "label": (["$label"], ["LABEL0"])},
                },
            },
        )
    handler.setup_data(init_type=DataHandlerLP.IT_LS if inference else DataHandlerLP.IT_FIT_SEQ)
    return handler


def port_record(recorder, prediction, segments, manifest, threshold=None, daily_targets=None, artifact_path=None):
    from qlib.workflow.record_temp import PortAnaRecord
    from qlib.data import D

    frequency = manifest["frequency"]
    benchmark = "SH000300" if frequency == "day" else pd.Series(0.0, index=D.calendar(freq="5min"))
    start, end = segments["test"]
    end = str(pd.Timestamp(end) + pd.Timedelta(hours=15)) if frequency == "5min" else end
    exchange = {
        "class": "ChinaExchange",
        "module_path": "quant_platform.adapters.qlib.strategy",
        "kwargs": {
            "freq": frequency,
            "start_time": start,
            "end_time": end,
            "codes": sorted(set(manifest["coverage"]) - {"SH000300"}),
        },
    }
    strategy = {
        "class": "TargetWeightStrategy",
        "module_path": "quant_platform.adapters.qlib.strategy",
        "kwargs": {
            "signal": "<PRED>",
            "policy": manifest["policy"],
            "frequency": frequency,
            "daily_targets": daily_targets,
            "threshold": threshold,
        },
    }
    record_type = (
        type("ScopedPortAnaRecord", (PortAnaRecord,), {"artifact_path": artifact_path})
        if artifact_path
        else PortAnaRecord
    )
    record = record_type(
        recorder,
        config={
            "strategy": strategy,
            "executor": {
                "class": "SimulatorExecutor",
                "module_path": "qlib.backtest.executor",
                "kwargs": {"time_per_step": frequency, "generate_portfolio_metrics": True},
            },
            "backtest": {
                "start_time": start,
                "end_time": end,
                "account": 1_000_000,
                "benchmark": benchmark,
                "exchange_kwargs": {"exchange": exchange},
            },
        },
    )
    record.generate()
    suffix = "1day" if frequency == "day" else "5min"
    return recorder.load_object(f"{record.artifact_path}/report_normal_{suffix}.pkl")


def report_metrics(report):
    net = (report["return"] - report["cost"]).fillna(0)
    curve = (1 + net).cumprod()
    benchmark = (1 + report["bench"].fillna(0)).cumprod()
    return {
        "net_return": float(curve.iloc[-1] - 1),
        "excess_return": float(curve.iloc[-1] - benchmark.iloc[-1]),
        "max_drawdown": float((1 - curve / curve.cummax()).max()),
        "observations": len(report),
    }


def save_bundle(output, model, processors, metadata):
    from qlib.data.dataset.processor import ProcessInf, RobustZScoreNorm, Fillna

    if [type(p) for p in processors] != [ProcessInf, RobustZScoreNorm, Fillna]:
        raise PipelineBlocked("Unsupported processor chain; qualify a new artifact contract.")
    norm = processors[1]
    model.model.save_model(str(output / "model.txt"))
    np.savez(output / "processors.npz", mean=norm.mean_train, std=norm.std_train)
    state = {
        **metadata,
        "format": "lightgbm-text-qlib-processors-v1",
        "normalizer": {
            "columns": norm.cols.tolist(),
            "fit_start": str(norm.fit_start_time),
            "fit_end": str(norm.fit_end_time),
            "clip_outlier": norm.clip_outlier,
        },
    }
    (output / "bundle.json").write_text(json.dumps(state, allow_nan=False))


def load_bundle(model_path, model_manifest):
    import lightgbm as lgb
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset.processor import ProcessInf, RobustZScoreNorm, Fillna

    required = {"model.txt", "processors.npz", "bundle.json"}
    if not required.issubset(model_manifest.get("files", {})):
        raise PipelineBlocked("Model manifest is incomplete.")
    verify(model_path, model_manifest)
    bundle = json.loads((model_path / "bundle.json").read_text())
    if bundle.get("format") != "lightgbm-text-qlib-processors-v1":
        raise PipelineBlocked("Unsupported model artifact format.")
    if bundle.get("training_configuration") != model_manifest.get("training_configuration"):
        raise PipelineBlocked("Model training configuration metadata mismatch.")
    feature_spec(bundle.get("training_configuration"))
    state = bundle["normalizer"]
    norm = RobustZScoreNorm(state["fit_start"], state["fit_end"], "feature", state["clip_outlier"])
    norm.cols = pd.MultiIndex.from_tuples([tuple(c) for c in state["columns"]])
    with np.load(model_path / "processors.npz", allow_pickle=False) as arrays:
        norm.mean_train, norm.std_train = arrays["mean"].copy(), arrays["std"].copy()
    if norm.mean_train.shape != (len(norm.cols),) or norm.std_train.shape != norm.mean_train.shape:
        raise PipelineBlocked("Fitted processor dimensions do not match feature columns.")
    model = LGBModel()
    model.model = lgb.Booster(model_file=str(model_path / "model.txt"))
    bundle.update(model=model, processors=[ProcessInf(), norm, Fillna("feature", 0)])
    return bundle


def workflow_identity(manifest, sizes, rounds):
    from quant_platform.research.factors import research_identity

    sources = research_identity()
    return digest(
        {
            "contract": DATA_CONTRACT,
            "frequency": manifest["frequency"],
            "policy": manifest["policy"],
            "qlib": "0.9.7",
            "sources": sources,
            "segment_sizes": sizes,
            "rounds": rounds,
            "training_configuration": manifest.get("training_configuration"),
        }
    )


def reconstruct_pools(prediction, manifest, segments):
    """Use only daily held-out predictions; never today's shortlist or fitted samples."""
    from .strategy import finite, market_features, proposals

    if manifest["frequency"] != "day":
        raise PipelineBlocked("Research pools require a daily Qlib model.")
    days = manifest["days"]
    fitted_through = max(segments["train"][1], segments["valid"][1])
    first_test = segments["test"][0]
    if days.index(first_test) - days.index(fitted_through) <= 6:
        raise PipelineBlocked("Daily pool predictions overlap fitted or validation label intervals.")
    pools = {}
    for stamp, values in prediction.groupby(level="datetime"):
        signal_day = str(pd.Timestamp(stamp).date())
        if signal_day < first_test or signal_day not in days or signal_day == days[-1]:
            continue
        scores = {str(code): float(score) for code, score in values.droplevel("datetime").items() if finite(score)}
        features = market_features(list(scores), pd.Timestamp(signal_day))
        instruments = manifest.get("instruments", {})
        eligible = (
            {
                code
                for code, item in instruments.items()
                if item["list_date"] <= signal_day
                and (not item.get("delist_date") or item["delist_date"] >= signal_day)
            }
            if instruments
            else set(scores)
        )
        ready = {
            code
            for code in eligible
            if code in scores
            and all(
                finite(features.get(code, {}).get(field))
                for field in (
                    "price",
                    "risk",
                    "tradable",
                    "limit_up",
                    "limit_down",
                    "listed_sessions",
                    "average_turnover20",
                )
            )
        }
        coverage = len(ready) / len(eligible) if eligible else 0
        result = proposals(
            {code: value for code, value in scores.items() if code in eligible}, features, manifest["policy"]
        )
        next_day = days[days.index(signal_day) + 1]
        pools[next_day] = {
            "kind": "research_intraday",
            "out_of_sample": True,
            "research_only": True,
            "signal_day": signal_day,
            "fitted_through": fitted_through,
            "test_start": first_test,
            "symbols": [row["symbol"] for row in result["shortlist"]],
            "daily_weights": result["weights"],
            "cash_weight": result["cash_weight"],
            "daily_generation_id": manifest["id"],
            "source_watermark": manifest.get("watermark"),
            "policy": manifest["policy"],
            "coverage": coverage,
            "ready": coverage >= manifest["policy"]["minimum_coverage"],
            "omitted": sorted(eligible - ready),
        }
    if not pools:
        raise PipelineBlocked("No out-of-sample daily predictions are available for historical pools.")
    return pools


def train_artifact(
    path,
    manifest,
    output,
    experiment_root,
    sizes=None,
    rounds=1000,
    daily_targets=None,
    training_configuration=None,
    test_exposure=None,
):
    from qlib.contrib.model.gbdt import LGBModel
    from qlib.data.dataset import DatasetH
    from qlib.workflow import R
    from qlib.workflow.record_temp import SignalRecord, SigAnaRecord
    from .strategy import STRATEGY_VERSION

    if training_configuration:
        feature_spec(training_configuration)
        manifest = {**manifest, "training_configuration": training_configuration}
        sizes = tuple(training_configuration[k] for k in ("train_sessions", "validation_sessions", "test_sessions"))
        rounds = training_configuration["rounds"]
    initialize(path, experiment_root)
    segments = segments_for(manifest["days"], manifest["frequency"], sizes)
    handler = make_handler(manifest, segments)
    dataset = DatasetH(handler=handler, segments=segments)
    model = LGBModel(
        loss="mse",
        learning_rate=(training_configuration or {}).get("learning_rate", 0.05),
        num_leaves=(training_configuration or {}).get("num_leaves", 31),
        seed=(training_configuration or {}).get("seed", 42),
        num_threads=4,
        early_stopping_rounds=50,
        num_boost_round=rounds,
    )
    output.mkdir(parents=True, exist_ok=False)
    from mlflow.tracking import MlflowClient
    from mlflow.exceptions import MlflowException

    client = MlflowClient(tracking_uri=Path(experiment_root).resolve().as_uri())
    experiment_name = f"quap-{manifest['frequency']}"
    if client.get_experiment_by_name(experiment_name) is None:
        try:
            client.create_experiment(experiment_name)
        except MlflowException:
            if client.get_experiment_by_name(experiment_name) is None:
                raise
    with R.start(experiment_name=experiment_name):
        recorder = R.get_recorder()
        recorder_folder = Path(experiment_root).resolve() / client.get_run(recorder.id).info.experiment_id / recorder.id
        (recorder_folder / "quap-owner.json").write_text(
            json.dumps(
                {
                    "format": "quap-qlib-recorder-v1",
                    "recorder_id": recorder.id,
                }
            )
        )
        R.log_params(
            engine=ENGINE_VERSION,
            contract=DATA_CONTRACT,
            strategy=STRATEGY_VERSION,
            generation=manifest["id"],
            segments=json.dumps(segments),
        )
        model.fit(dataset, verbose_eval=False)
        SignalRecord(model, dataset, recorder).generate()
        SigAnaRecord(recorder).generate()
        prediction = recorder.load_object("pred.pkl")
        valid_prediction = model.predict(dataset, segment="valid")
        threshold = float(valid_prediction.quantile(0.2))
        if not math.isfinite(threshold):
            raise PipelineBlocked("Validation calibration is not finite.")
        if manifest["frequency"] == "5min" and not daily_targets:
            raise PipelineBlocked("Intraday evaluation requires dated out-of-sample daily target weights.")
        report = port_record(recorder, prediction, segments, manifest, threshold, daily_targets)
        metrics = report_metrics(report)
        ric = recorder.load_object("sig_analysis/ric.pkl")
        rank_ic = float(ric.mean())
        metrics["rank_ic"] = rank_ic if math.isfinite(rank_ic) else None
        passed = (
            metrics["rank_ic"] is not None
            and rank_ic > 0
            and metrics["excess_return"] > 0
            and metrics["max_drawdown"] <= 0.2
        )
        if manifest["frequency"] == "5min":
            baseline_report = port_record(
                recorder, prediction, segments, manifest, -1e30, daily_targets, "daily_baseline"
            )
            baseline_metrics = report_metrics(baseline_report)
            metrics["daily_baseline"] = baseline_metrics
            passed = (
                metrics["rank_ic"] is not None
                and rank_ic > 0
                and metrics["net_return"] >= baseline_metrics["net_return"]
                and metrics["max_drawdown"] <= baseline_metrics["max_drawdown"]
            )
        pools = {}
        if manifest["frequency"] == "day":
            research = DatasetH(handler=handler, segments={"research": (segments["test"][0], manifest["days"][-1])})
            research_prediction = model.predict(research, segment="research")
            pools = reconstruct_pools(research_prediction, manifest, segments)
            (output / "research_pools.json").write_text(json.dumps(pools, allow_nan=False))
            research_prediction.to_csv(output / "research_predictions.csv")
            metrics["historical_pool_coverage_passed"] = all(pool["ready"] for pool in pools.values())
            passed = passed and metrics["historical_pool_coverage_passed"]
        evaluation = {
            **metrics,
            "passed": passed,
            "recorder_id": recorder.id,
            "segments": segments,
            "strategy": STRATEGY_VERSION,
            "simulated_account": True,
            "shadow_sessions_required": 20,
            "test_exposure": test_exposure,
        }
        bundle = {
            "columns": list(dataset.prepare("test", col_set="feature").columns),
            "contract": DATA_CONTRACT,
            "frequency": manifest["frequency"],
            "threshold": threshold,
            "anchors": manifest["anchors"],
            "policy": manifest["policy"],
            "training_configuration": training_configuration,
        }
        from quant_platform.research.diagnostics import training_reference

        training_features = dataset.prepare("train", col_set="feature", data_key="raw")
        reference = training_reference(
            training_features,
            model.predict(dataset, segment="train"),
            manifest["frequency"],
            model.model.feature_importance(importance_type="gain"),
        )
        (output / "diagnostic_reference.json").write_text(json.dumps(reference, allow_nan=False))
        save_bundle(output, model, handler.infer_processors, bundle)
        prediction.to_csv(output / "predictions.csv")
        report.to_csv(output / "backtest.csv")
        (output / "evaluation.json").write_text(json.dumps(evaluation, default=str, allow_nan=False))
    recorder_manifest = seal(recorder_folder, {"recorder_id": recorder.id})
    artifact_manifest = seal(
        output,
        {
            "contract": DATA_CONTRACT,
            "frequency": manifest["frequency"],
            "evaluation": evaluation,
            "training_as_of": manifest["as_of"],
            "research_pool_count": len(pools),
            "training_configuration": training_configuration,
            "diagnostic_baseline": "diagnostic_reference.json",
            "recorder": {
                "path": recorder_folder.relative_to(Path(experiment_root).resolve().parent).as_posix(),
                "manifest": recorder_manifest,
            },
            "workflow_identity": workflow_identity(manifest, sizes, rounds),
        },
    )
    return artifact_manifest


def generation_for(db, settings, run):
    rows = db.rows(
        "SELECT g.* FROM qlib_generations g JOIN pipeline_steps s ON g.id=(s.result->>'generation_id')::uuid "
        "WHERE s.run_id=%s AND s.name='prepare'",
        (run["id"],),
    )
    if not rows:
        raise PipelineBlocked("Prepared generation dependency is absent.")
    generation = rows[0]
    path = safe_artifact(settings.artifact_root, generation["path"])
    verify(path, generation["manifest"])
    return generation, path


def train_model(db, settings, run, job):
    generation, path = generation_for(db, settings, run)
    model_id = uuid4()
    relative = f"models/{model_id.hex}"
    output = settings.artifact_root.resolve() / relative
    daily_targets = None
    if run["frequency"] == "5min":
        pools = generation["manifest"]["point_in_time_pools"]
        if any(
            not pool.get("out_of_sample") or not pool.get("ready") or "daily_weights" not in pool
            for pool in pools.values()
        ):
            raise PipelineBlocked("Intraday training requires qualified reconstructed daily pools and target weights.")
        daily_targets = {day: pool["daily_weights"] for day, pool in pools.items()}
    training_config = run["snapshot"].get("training_configuration")
    exposure = None
    if training_config:
        from quant_platform.storage.experiments import test_exposure

        with db.transaction() as conn:
            db.fence(conn, job)
            exposure = test_exposure(conn, run, generation["id"])
    artifact = train_artifact(
        path,
        generation["manifest"],
        output,
        settings.artifact_root / "experiments",
        daily_targets=daily_targets,
        training_configuration=training_config,
        test_exposure=exposure,
    )
    evaluation = artifact["evaluation"]
    with db.publication(job) as conn:
        conn.execute(
            "INSERT INTO model_versions(id,frequency,generation_id,state,contract,path,metadata) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (
                model_id,
                run["frequency"],
                generation["id"],
                "shadow" if evaluation["passed"] else "rejected",
                DATA_CONTRACT,
                relative,
                jsonb(artifact),
            ),
        )
        conn.execute(
            "INSERT INTO model_evaluations(model_id,data,passed) VALUES(%s,%s,%s)",
            (model_id, jsonb(evaluation), evaluation["passed"]),
        )
        if run["frequency"] == "day":
            for day, pool in json.loads((output / "research_pools.json").read_text()).items():
                data = {**pool, "daily_model_id": str(model_id), "policy_revision": run["snapshot"]["policy_revision"]}
                conn.execute(
                    "INSERT INTO universe_snapshots(id,day,content_hash,data) VALUES(%s,%s,%s,%s)",
                    (uuid4(), day, digest({"day": day, **data}), jsonb(data)),
                )
        conn.execute(
            "UPDATE pipeline_steps SET result=%s WHERE job_id=%s", (jsonb({"model_id": str(model_id)}), job["id"])
        )
        conn.execute(
            "UPDATE pipeline_runs SET state='succeeded',result=%s,updated_at=now() WHERE id=%s",
            (
                jsonb({"model_id": str(model_id), "evaluation": evaluation, "published_recommendations": False}),
                run["id"],
            ),
        )


def predict_artifact(data_path, manifest, model_path, model_manifest):
    from qlib.data.dataset import DatasetH

    verify(data_path, manifest)
    initialize(data_path)
    bundle = load_bundle(model_path, model_manifest)
    if bundle["contract"] != manifest["contract"] or bundle["frequency"] != manifest["frequency"]:
        raise PipelineBlocked("Model and dataset contracts do not match.")
    if any(
        code in bundle["anchors"] and not math.isclose(anchor, bundle["anchors"][code])
        for code, anchor in manifest["anchors"].items()
    ):
        raise PipelineBlocked("Model normalization anchors changed.")
    target = pd.Timestamp(manifest["as_of"]).tz_convert("Asia/Shanghai").tz_localize(None)
    target = target.normalize() if manifest["frequency"] == "day" else target
    segments = {"test": (str(target), str(target))}
    handler = make_handler(
        {**manifest, "training_configuration": bundle.get("training_configuration")},
        segments,
        bundle["processors"],
        inference=True,
    )
    dataset = DatasetH(handler=handler, segments=segments)
    features = dataset.prepare("test", col_set="feature")
    if list(features.columns) != bundle["columns"]:
        raise PipelineBlocked("Feature order does not match the fitted model.")
    prediction = bundle["model"].predict(dataset)
    scores = (
        {str(code): float(value) for (_, code), value in prediction.items() if math.isfinite(value)}
        if prediction.index.names == ["datetime", "instrument"]
        else {
            str(index[prediction.index.names.index("instrument")]): float(value)
            for index, value in prediction.items()
            if math.isfinite(value)
        }
    )
    return scores, target, bundle


def infer(db, settings, run, job):
    from .strategy import market_features, proposals
    from quant_platform.pipeline import active_model, next_session

    generation, data_path = generation_for(db, settings, run)
    snapshot = run["snapshot"]
    if not snapshot.get("model_id"):
        raise PipelineBlocked("No approved model was pinned; create a new run after model approval.")
    with db.transaction() as conn:
        model = active_model(conn, run["frequency"])
    if not model or str(model["id"]) != snapshot["model_id"]:
        raise PipelineBlocked("Pinned model is no longer an approved active release.")
    model_path = safe_artifact(settings.artifact_root, model["path"])
    scores, target, bundle = predict_artifact(data_path, generation["manifest"], model_path, model["metadata"])
    if bundle["policy"] != snapshot["policy"]:
        raise PipelineBlocked("Scan policy changed; qualify a model/strategy release for this policy.")
    features = market_features(list(scores), target, run["frequency"])
    groups = [(None, 0, {})] + [(p["id"], p["revision"], p["data"]["weights"]) for p in snapshot["portfolios"]]
    outputs = []
    for portfolio_id, revision, baseline in groups:
        daily = None
        if run["frequency"] == "5min":
            if portfolio_id:
                pinned = snapshot.get("portfolio_daily_baselines", {}).get(str(portfolio_id))
                if not pinned or pinned["portfolio_revision"] != revision:
                    raise PipelineBlocked("Model portfolio has no matching pinned Qlib daily baseline.")
                daily = pinned["data"]["weights"]
            else:
                daily = snapshot["daily_baseline"]["data"]["weights"]
        output = proposals(scores, features, snapshot["policy"], baseline, daily, bundle["threshold"])
        outputs.append((portfolio_id, revision, output))
    available = now()
    with db.publication(job) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        from quant_platform.pipeline import validate_snapshot

        validate_snapshot(conn, snapshot)
        current = active_model(conn, run["frequency"])
        if current is None or current["id"] != model["id"]:
            raise PipelineBlocked("Model release changed during inference.")
        if run["frequency"] == "5min":
            effective, expires = intraday_window(run["as_of"], available)
        else:
            day = next_session(conn, run["as_of"].astimezone(CN).date())
            effective, expires = datetime.combine(day, time(9, 30), CN), datetime.combine(day, time(15), CN)
            if effective <= available:
                raise PipelineBlocked("Daily next-session publication deadline missed.")
        prediction_id = uuid4()
        conn.execute(
            "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,%s)",
            (
                prediction_id,
                run["id"],
                model["id"],
                generation["id"],
                jsonb({"scores": scores, "as_of": str(run["as_of"])}),
            ),
        )
        from quant_platform.jobs.diagnostics import enqueue_diagnostics

        enqueue_diagnostics(db, conn, prediction_id)
        report_ids = []
        for portfolio_id, revision, output in outputs:
            report_id = uuid4()
            output.update(
                engine=ENGINE_VERSION,
                model_id=str(model["id"]),
                generation_id=str(generation["id"]),
                evaluation=model["evaluation"],
                coverage=generation["manifest"]["market_coverage"],
                omitted=generation["manifest"]["omitted"],
            )
            conn.execute(
                "UPDATE recommendations SET state='superseded' WHERE frequency=%s AND portfolio_id IS NOT DISTINCT FROM %s::uuid AND state='published'",
                (run["frequency"], portfolio_id),
            )
            conn.execute(
                "INSERT INTO recommendations(id,prediction_id,portfolio_id,portfolio_revision,policy_revision,frequency,as_of,available_at,effective_from,valid_until,data) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    report_id,
                    prediction_id,
                    portfolio_id,
                    revision,
                    snapshot["policy_revision"],
                    run["frequency"],
                    run["as_of"],
                    available,
                    effective,
                    expires,
                    jsonb(output),
                ),
            )
            from quant_platform.jobs.notify import recommendation_change

            recommendation_change(
                db, conn, report_id, run["frequency"], portfolio_id, output, snapshot["policy"], available, expires
            )
            report_ids.append(str(report_id))
            if run["frequency"] == "day" and portfolio_id is None:
                pool = {
                    "kind": "research_intraday",
                    "out_of_sample": True,
                    "research_only": True,
                    "ready": True,
                    "signal_day": str(run["as_of"].astimezone(CN).date()),
                    "fitted_through": model["evaluation"]["segments"]["valid"][1],
                    "test_start": model["evaluation"]["segments"]["test"][0],
                    "symbols": [row["symbol"] for row in output["shortlist"]],
                    "daily_weights": output["weights"],
                    "cash_weight": output["cash_weight"],
                    "daily_model_id": str(model["id"]),
                    "daily_generation_id": str(generation["id"]),
                    "daily_recommendation_id": str(report_id),
                    "policy_revision": snapshot["policy_revision"],
                    "policy": snapshot["policy"],
                    "source_watermark": snapshot["watermark"],
                    "coverage": generation["manifest"]["market_coverage"],
                }
                conn.execute(
                    "INSERT INTO universe_snapshots(id,day,content_hash,data) VALUES(%s,%s,%s,%s)",
                    (uuid4(), day, digest({"day": day, **pool}), jsonb(pool)),
                )
        conn.execute(
            "UPDATE pipeline_runs SET state='succeeded',result=%s,error=NULL,updated_at=now() WHERE id=%s",
            (jsonb({"recommendation_ids": report_ids, "prediction_id": str(prediction_id)}), run["id"]),
        )
        conn.execute(
            "UPDATE pipeline_steps SET result=%s WHERE job_id=%s",
            (jsonb({"prediction_id": str(prediction_id)}), job["id"]),
        )


def observe_shadow(db, settings, run, job):
    from .strategy import market_features, proposals
    from quant_platform.pipeline import model_deadline, next_session, shadow_cutoffs, validate_snapshot

    generation, data_path = generation_for(db, settings, run)
    snapshot = run["snapshot"]
    rows = db.rows(
        "SELECT m.* FROM model_versions m JOIN model_evaluations e ON e.model_id=m.id "
        "WHERE m.id=%s AND m.state='shadow' AND e.passed",
        (snapshot["model_id"],),
    )
    if not rows or now() >= model_deadline(rows[0]):
        raise PipelineBlocked("Shadow challenger is no longer qualified or fresh.")
    model = rows[0]
    scores, target, bundle = predict_artifact(
        data_path, generation["manifest"], safe_artifact(settings.artifact_root, model["path"]), model["metadata"]
    )
    if bundle["policy"] != snapshot["policy"]:
        raise PipelineBlocked("Shadow policy differs from the evaluated policy.")
    daily = snapshot["daily_baseline"]["data"]["weights"] if run["frequency"] == "5min" else None
    output = proposals(
        scores,
        market_features(list(scores), target, run["frequency"]),
        snapshot["policy"],
        daily_weights=daily,
        threshold=bundle["threshold"],
    )
    observed = now()
    day = run["as_of"].astimezone(CN).date()
    with db.publication(job) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        validate_snapshot(conn, snapshot)
        fresh = conn.execute(
            "SELECT m.* FROM model_versions m JOIN model_evaluations e ON e.model_id=m.id "
            "WHERE m.id=%s AND m.state='shadow' AND e.passed",
            (model["id"],),
        ).fetchone()
        if not fresh or observed >= model_deadline(fresh):
            raise PipelineBlocked("Shadow release changed during prediction.")
        expected = shadow_cutoffs(day, run["frequency"])
        opened = conn.execute(
            "SELECT count(*) AS n FROM calendars WHERE day=%s AND exchange IN ('SSE','SZSE') AND is_open",
            (day,),
        ).fetchone()["n"]
        if opened != 2 or run["as_of"] not in expected or observed < run["as_of"]:
            raise PipelineBlocked("Shadow observation requires a completed trading-session cutoff.")
        if run["frequency"] == "5min":
            intraday_window(run["as_of"], observed)
        elif observed >= datetime.combine(next_session(conn, day), time(9, 30), CN):
            raise PipelineBlocked("Backfilled shadow runs are not production observations.")
        prediction_id = uuid4()
        conn.execute(
            "INSERT INTO prediction_runs(id,run_id,model_id,generation_id,data) VALUES(%s,%s,%s,%s,%s)",
            (
                prediction_id,
                run["id"],
                model["id"],
                generation["id"],
                jsonb(
                    {
                        "shadow": True,
                        "scores": scores,
                        "as_of": str(run["as_of"]),
                        "observed_at": str(observed),
                        "weights": output["weights"],
                        "environment": settings.environment,
                    }
                ),
            ),
        )
        from quant_platform.jobs.diagnostics import enqueue_diagnostics

        enqueue_diagnostics(db, conn, prediction_id)
        observed_cutoffs = {
            row["as_of"]
            for row in conn.execute(
                "SELECT DISTINCT r.as_of FROM prediction_runs p JOIN pipeline_runs r ON r.id=p.run_id "
                "WHERE p.model_id=%s AND p.data->>'shadow'='true' AND p.data->>'environment'=%s "
                "AND r.purpose='shadow' AND r.frequency=%s AND (r.as_of AT TIME ZONE 'Asia/Shanghai')::date=%s",
                (model["id"], settings.environment, run["frequency"], day),
            ).fetchall()
        }
        missing = expected - observed_cutoffs
        observations = len(expected & observed_cutoffs)
        healthy = not missing
        evidence = {
            "environment": settings.environment,
            "observations": observations,
            "expected_observations": len(expected),
            "missing_cutoffs": [value.isoformat() for value in sorted(missing)],
            "last_prediction_id": str(prediction_id),
            "workflow_identity": model["metadata"]["workflow_identity"],
        }
        conn.execute(
            "INSERT INTO model_shadow_samples(model_id,day,data,healthy) VALUES(%s,%s,%s,%s) "
            "ON CONFLICT(model_id,day) DO UPDATE SET data=excluded.data,healthy=excluded.healthy",
            (model["id"], day, jsonb(evidence), healthy),
        )
        result = {
            "shadow": True,
            "prediction_id": str(prediction_id),
            "published_recommendations": False,
            "healthy_session": healthy,
            "observations": observations,
        }
        conn.execute("UPDATE pipeline_steps SET result=%s WHERE job_id=%s", (jsonb(result), job["id"]))
        conn.execute(
            "UPDATE pipeline_runs SET state='succeeded',result=%s,error=NULL,updated_at=now() WHERE id=%s",
            (jsonb(result), run["id"]),
        )


def execute_job(db, settings, job):
    rows = db.rows("SELECT * FROM pipeline_runs WHERE id=%s", (job["payload"]["run_id"],))
    if not rows:
        raise PipelineBlocked("Pipeline run not found.")
    run = rows[0]
    with db.transaction() as conn:
        db.fence(conn, job)
        conn.execute("UPDATE pipeline_runs SET state='running',updated_at=now() WHERE id=%s", (run["id"],))
    if job["kind"] == "qlib_prepare":
        from .data import build_generation

        build_generation(db, settings, run, job)
    elif job["kind"] == "qlib_train":
        train_model(db, settings, run, job)
    elif job["kind"] == "qlib_infer":
        infer(db, settings, run, job)
    elif job["kind"] == "qlib_shadow":
        observe_shadow(db, settings, run, job)
    else:
        raise PipelineBlocked("Unknown Qlib step.")


if __name__ == "__main__":
    import sys
    from quant_platform.config import Settings
    from quant_platform.storage import Database

    settings = Settings()
    db = Database(settings.dsn, role="quant_worker")
    try:
        execute_job(db, settings, json.load(sys.stdin))
    except PipelineBlocked as exc:
        print("QUAP_BLOCKED=" + str(exc))
        sys.exit(3)
    finally:
        db.close()
