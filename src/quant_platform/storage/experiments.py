"""Research configuration revisions, development admission, and explicit candidate freeze."""

from uuid import uuid4

from quant_platform.domain import digest
from quant_platform.domain.research import (
    FactorInput,
    FactorSetInput,
    TrainingConfigurationInput,
    ExperimentInput,
    FreezeInput,
)
from quant_platform.domain.workflow import DATA_CONTRACT, PipelineBlocked
from quant_platform.research.factors import compile_factor, development_folds, research_identity
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict
from quant_platform.storage.research import RequestReplay, prior_request, record_request

LABEL_CONTRACTS = {"day": "Ref($open,-6)/Ref($open,-1)-1", "5min": "lagged-entry-open-to-next-session-close-v1"}


def inventory():
    return [
        {
            "id": "alpha158-v1",
            "frequency": "day",
            "engine": "qlib.contrib.data.handler.Alpha158",
            "feature_count": 158,
            "warmup": 60,
            "label_contract": LABEL_CONTRACTS["day"],
            "qlib": "0.9.7",
            "immutable": True,
        },
        {
            "id": "minute-v1",
            "frequency": "5min",
            "feature_count": 15,
            "windows": [1, 3, 6, 12],
            "features": ["return", "range", "volume", "vwap_deviation", "daily_context", "time_of_day"],
            "warmup": 960,
            "label_contract": LABEL_CONTRACTS["5min"],
            "qlib": "0.9.7",
            "immutable": True,
        },
    ]


def save_revision(db, settings, resource, value):
    contracts = {
        "factors": (FactorInput, "factor_definitions"),
        "factor-sets": (FactorSetInput, "factor_sets"),
        "training-configurations": (TrainingConfigurationInput, "training_configurations"),
    }
    contract, table = contracts[resource]
    value = contract.model_validate(value)
    try:
        with db.transaction() as conn:
            prior_request(conn, resource, value)
            head = conn.execute(
                f"SELECT id,revision FROM {table} WHERE name=%s ORDER BY revision DESC LIMIT 1", (value.name,)
            ).fetchone()
            revision = head["revision"] if head else 0
            if revision != value.expected_revision:
                raise Conflict("Research definition revision changed.")
            identifier = uuid4()
            data = value.model_dump(mode="json", exclude={"request_key", "expected_revision"})
            if resource == "factors":
                data.update(compile_factor(value.expression))
            elif resource == "factor-sets":
                members = [
                    conn.execute("SELECT * FROM factor_definitions WHERE id=%s", (key,)).fetchone()
                    for key in value.factors
                ]
                if any(m is None for m in members) or len({m["name"] for m in members}) != len(members):
                    raise Conflict("Factor set requires known, uniquely named factor revisions.")
                data.update(
                    features=[
                        {"id": str(m["id"]), "revision": m["revision"], "name": m["name"], **m["data"]} for m in members
                    ]
                )
                data["warmup"] = max(m["data"]["warmup"] for m in members)
                data["feature_hash"] = digest(data["features"])
            else:
                if value.fixture_windows and settings.environment != "test":
                    raise Conflict("Fixture windows are forbidden outside the test environment.")
                factor_set = (
                    conn.execute("SELECT * FROM factor_sets WHERE id=%s", (value.factor_set_id,)).fetchone()
                    if value.factor_set_id
                    else None
                )
                if value.factor_set_id and not factor_set:
                    raise Conflict("Factor-set revision not found.")
                data.update(
                    factor_set_revision=factor_set["revision"] if factor_set else None,
                    features=factor_set["data"]["features"] if factor_set else None,
                    warmup=max(
                        60 if value.frequency == "day" else 20, factor_set["data"]["warmup"] if factor_set else 0
                    ),
                    label_contract=LABEL_CONTRACTS[value.frequency],
                    backend="lightgbm",
                    qlib="0.9.7",
                    lightgbm="4.7.0",
                )
                data["configuration_hash"] = digest(data)
            parent = head["id"] if head else None
            if resource == "training-configurations":
                conn.execute(
                    "INSERT INTO training_configurations(id,name,revision,parent_id,factor_set_id,frequency,data) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (identifier, value.name, revision + 1, parent, value.factor_set_id, value.frequency, jsonb(data)),
                )
            else:
                conn.execute(
                    f"INSERT INTO {table}(id,name,revision,parent_id,data) VALUES(%s,%s,%s,%s,%s)",
                    (identifier, value.name, revision + 1, parent, jsonb(data)),
                )
            if resource == "factor-sets":
                for index, key in enumerate(value.factors):
                    conn.execute("INSERT INTO factor_set_members VALUES(%s,%s,%s)", (identifier, index, key))
            return record_request(
                conn, resource, value, {"id": str(identifier), "revision": revision + 1, "research_only": True}
            )
    except RequestReplay as replay:
        return replay.response


def configuration(conn, identifier):
    row = conn.execute("SELECT * FROM training_configurations WHERE id=%s", (identifier,)).fetchone()
    if not row:
        raise Conflict("Training configuration not found.")
    return {**row["data"], "id": str(row["id"]), "revision": row["revision"]}


def create_experiment(db, settings, value):
    value = ExperimentInput.model_validate(value)
    try:
        with db.transaction() as conn:
            prior_request(conn, "research_experiment", value)
            if value.expected_revision != 0 or value.baseline_configuration_id == value.candidate_configuration_id:
                raise Conflict(
                    "A new experiment requires revision zero and distinct baseline/candidate configurations."
                )
            generation = conn.execute("SELECT * FROM qlib_generations WHERE id=%s", (value.generation_id,)).fetchone()
            if (
                not generation
                or generation["contract"] != DATA_CONTRACT
                or generation["manifest"].get("source") != "tushare"
            ):
                raise PipelineBlocked(
                    "Experiments require a qualified canonical Qlib generation, never supplemental snapshots."
                )
            manifest = generation["manifest"]
            if manifest.get("market_coverage", 0) < manifest.get("policy", {}).get("minimum_coverage", 1):
                raise PipelineBlocked("Canonical generation coverage is insufficient.")
            baseline = configuration(conn, value.baseline_configuration_id)
            candidate = configuration(conn, value.candidate_configuration_id)
            common = ("frequency", "train_sessions", "validation_sessions", "test_sessions", "label_contract", "seed")
            if any(baseline[k] != candidate[k] for k in common) or candidate["frequency"] != generation["frequency"]:
                raise Conflict("Comparison must share frequency, windows, label contract, and seed.")
            if settings.environment != "test" and (baseline["fixture_windows"] or candidate["fixture_windows"]):
                raise Conflict("Fixture configurations cannot run in production.")
            folds, holdout = development_folds(
                manifest["days"], candidate, max(baseline["warmup"], candidate["warmup"])
            )
            spec = {
                "baseline": baseline,
                "candidate": candidate,
                "folds": folds,
                "reserved_test": holdout,
                "generation_manifest_hash": digest(manifest),
                "label_contract": candidate["label_contract"],
                "source_watermark": generation["watermark"],
                "selection_data": "validation_only",
                "research_only": True,
                "execution_identity": research_identity(),
            }
            identifier = uuid4()
            job_id = db.enqueue(
                conn,
                "qlib_experiment",
                "qlib-research",
                "experiment:" + str(identifier),
                {"experiment_id": str(identifier)},
            )
            conn.execute(
                "INSERT INTO research_experiments(id,generation_id,baseline_id,candidate_id,job_id,spec) VALUES(%s,%s,%s,%s,%s,%s)",
                (
                    identifier,
                    generation["id"],
                    value.baseline_configuration_id,
                    value.candidate_configuration_id,
                    job_id,
                    jsonb(spec),
                ),
            )
            return record_request(
                conn, "research_experiment", value, {"id": str(identifier), "job_id": job_id, "revision": 1}
            )
    except RequestReplay as replay:
        return replay.response


def freeze_candidate(db, experiment_id, value):
    value = FreezeInput.model_validate(value)
    operation = "freeze:" + str(experiment_id)
    try:
        with db.transaction() as conn:
            prior_request(conn, operation, value)
            experiment = conn.execute(
                "SELECT e.*,j.status FROM research_experiments e JOIN jobs j ON j.id=e.job_id WHERE e.id=%s",
                (experiment_id,),
            ).fetchone()
            if not experiment or experiment["status"] != "complete" or value.expected_revision != 1:
                raise Conflict("Freeze requires a completed experiment at revision one.")
            completed = conn.execute(
                "SELECT DISTINCT t.configuration_id,t.fold FROM research_trials t JOIN research_trial_results r "
                "ON r.trial_id=t.id WHERE t.experiment_id=%s AND r.state='succeeded' AND r.artifact_id IS NOT NULL",
                (experiment_id,),
            ).fetchall()
            expected = {
                (identifier, fold)
                for identifier in (experiment["baseline_id"], experiment["candidate_id"])
                for fold in range(3)
            }
            if {(row["configuration_id"], row["fold"]) for row in completed} != expected:
                raise Conflict("Freeze requires all six successful development trials.")
            if experiment["spec"].get("execution_identity") != research_identity():
                raise Conflict("Research execution identity changed; compare again with the current implementation.")
            if value.configuration_id not in {experiment["baseline_id"], experiment["candidate_id"]}:
                raise Conflict("Selected configuration was not evaluated by this experiment.")
            if conn.execute(
                "SELECT 1 FROM research_freezes WHERE experiment_id=%s OR configuration_id=%s",
                (experiment_id, value.configuration_id),
            ).fetchone():
                raise Conflict("Candidate is already frozen; reuse its immutable training configuration.")
            identifier = uuid4()
            conn.execute(
                "INSERT INTO research_freezes(id,experiment_id,configuration_id,generation_id,data) VALUES(%s,%s,%s,%s,%s)",
                (
                    identifier,
                    experiment_id,
                    value.configuration_id,
                    experiment["generation_id"],
                    jsonb(
                        {
                            "reserved_test": experiment["spec"]["reserved_test"],
                            "selection": "manual_validation_evidence",
                            "execution_identity": experiment["spec"]["execution_identity"],
                        }
                    ),
                ),
            )
            return record_request(
                conn,
                operation,
                value,
                {
                    "id": str(identifier),
                    "revision": 2,
                    "training_configuration_id": str(value.configuration_id),
                    "model_activated": False,
                },
            )
    except RequestReplay as replay:
        return replay.response


def frozen_training(conn, identifier):
    config = configuration(conn, identifier)
    frozen = conn.execute(
        "SELECT f.*,g.as_of,g.path,g.manifest FROM research_freezes f JOIN qlib_generations g ON g.id=f.generation_id WHERE configuration_id=%s",
        (identifier,),
    ).fetchone()
    if not frozen:
        raise PipelineBlocked("Training configuration must be explicitly frozen after development comparison.")
    if frozen["data"].get("execution_identity") != research_identity():
        raise PipelineBlocked("Frozen research code identity changed; a new comparison and freeze are required.")
    return config, frozen


def test_exposure(conn, run, generation_id):
    """Bind holdout disclosure once per fenced training run, including retries."""
    conn.execute("SELECT pg_advisory_xact_lock(77610260)")
    start, end = run["snapshot"]["reserved_test"]
    freeze_id = run["snapshot"]["research_freeze_id"]
    prior = conn.execute("SELECT * FROM research_test_exposures WHERE run_id=%s", (run["id"],)).fetchone()
    if prior:
        if (
            str(prior["freeze_id"]) != str(freeze_id)
            or str(prior["generation_id"]) != str(generation_id)
            or prior["data"]["test_start"] != start
            or prior["data"]["test_end"] != end
        ):
            raise PipelineBlocked("Pinned research test exposure changed.")
        return prior["data"]
    reused = conn.execute(
        "SELECT 1 FROM research_test_exposures x JOIN pipeline_runs p ON p.id=x.run_id WHERE p.frequency=%s "
        "AND x.data->>'test_start'<=%s AND x.data->>'test_end'>=%s UNION ALL "
        "SELECT 1 FROM model_evaluations e JOIN model_versions m ON m.id=e.model_id WHERE m.frequency=%s "
        "AND e.data->'segments'->'test'->>0<=%s AND e.data->'segments'->'test'->>1>=%s LIMIT 1",
        (run["frequency"], end, start, run["frequency"], end, start),
    ).fetchone()
    exposure = {
        "test_start": start,
        "test_end": end,
        "previously_exposed": bool(reused),
        "unbiased_holdout": not bool(reused),
    }
    conn.execute(
        "INSERT INTO research_test_exposures(run_id,freeze_id,generation_id,data) VALUES(%s,%s,%s,%s)",
        (run["id"], freeze_id, generation_id, jsonb(exposure)),
    )
    return exposure
