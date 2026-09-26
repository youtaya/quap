"""Durable per-attempt trial lineage and fenced publication; no model activation."""

import time
from uuid import uuid4

from quant_platform.adapters.qlib.data import safe_artifact, verify
from quant_platform.domain import digest
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.research.process import run_research
from quant_platform.research.factors import research_identity
from quant_platform.storage import jsonb
from quant_platform.storage.research import publish_artifact


def execute(db, settings, job, stop, runner=run_research):
    experiment = db.rows("SELECT * FROM research_experiments WHERE job_id=%s", (job["id"],))[0]
    generation = db.rows("SELECT * FROM qlib_generations WHERE id=%s", (experiment["generation_id"],))[0]
    spec = experiment["spec"]
    if spec.get("execution_identity") != research_identity():
        raise PipelineBlocked("Pinned research code/dependency identity changed.")
    if digest(generation["manifest"]) != spec["generation_manifest_hash"]:
        raise PipelineBlocked("Pinned research generation changed.")
    path = safe_artifact(settings.artifact_root, generation["path"])
    verify(path, generation["manifest"])
    started = time.monotonic()
    for config in (spec["baseline"], spec["candidate"]):
        for fold_number, fold in enumerate(spec["folds"]):
            prior = db.rows(
                "SELECT a.path,a.manifest FROM research_trials t JOIN research_trial_results r ON r.trial_id=t.id "
                "JOIN research_artifacts a ON a.id=r.artifact_id WHERE t.experiment_id=%s AND t.configuration_id=%s AND t.fold=%s AND r.state='succeeded' LIMIT 1",
                (experiment["id"], config["id"], fold_number),
            )
            if prior:
                verify(safe_artifact(settings.artifact_root, prior[0]["path"]), prior[0]["manifest"])
                continue
            trial_id, artifact_id = uuid4(), uuid4()
            relative = f"research/{artifact_id.hex}"
            with db.transaction() as conn:
                db.fence(conn, job)
                conn.execute(
                    "INSERT INTO research_trials(id,experiment_id,configuration_id,fold,fence,data) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        trial_id,
                        experiment["id"],
                        config["id"],
                        fold_number,
                        job["fence"],
                        jsonb(
                            {
                                "segments": fold,
                                "configuration": config,
                                "generation": str(generation["id"]),
                                "job_attempt": job["attempts"],
                                "execution_identity": spec["execution_identity"],
                            }
                        ),
                    ),
                )
            try:
                remaining = settings.training_deadline_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise PipelineBlocked("Research training deadline exceeded.")
                result = runner(
                    "quant_platform.research.experiments",
                    {
                        "action": "fold",
                        "path": str(path),
                        "manifest": generation["manifest"],
                        "execution_identity": spec["execution_identity"],
                        "configuration": config,
                        "fold": fold,
                        "reserved_test": spec["reserved_test"],
                        "output": str(settings.artifact_root.resolve() / relative),
                        "experiment_root": str(settings.artifact_root.resolve() / "experiments"),
                    },
                    remaining,
                    stop,
                )
                verify(safe_artifact(settings.artifact_root, relative), result["manifest"])
            except PipelineBlocked:
                with db.transaction() as conn:
                    db.fence(conn, job)
                    conn.execute(
                        "INSERT INTO research_trial_results(trial_id,state,data) VALUES(%s,'failed',%s)",
                        (trial_id, jsonb({"reason": "bounded_qlib_trial_failed"})),
                    )
                raise
            with db.transaction() as conn:
                db.fence(conn, job)
                publish_artifact(
                    conn, {"id": artifact_id, "kind": "experiment", "path": relative, "manifest": result["manifest"]}
                )
                conn.execute(
                    "INSERT INTO research_trial_results(trial_id,state,artifact_id,data) VALUES(%s,'succeeded',%s,%s)",
                    (trial_id, artifact_id, jsonb(result["summary"])),
                )
                conn.execute(
                    "UPDATE jobs SET progress=%s WHERE id=%s",
                    (
                        jsonb({"configuration_id": config["id"], "completed_fold": fold_number, "research_only": True}),
                        job["id"],
                    ),
                )
    with db.publication(job) as conn:
        conn.execute(
            "UPDATE jobs SET progress=%s WHERE id=%s",
            (
                jsonb(
                    {
                        "state": "comparison_complete",
                        "trials": 6,
                        "reserved_test_accessed": False,
                        "model_activated": False,
                    }
                ),
                job["id"],
            ),
        )
