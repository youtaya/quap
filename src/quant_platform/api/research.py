"""Authenticated, bounded research resources; GET never schedules work."""

import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from quant_platform.adapters.qlib.data import safe_artifact, verify
from quant_platform.domain import symbol
from quant_platform.domain.research import (
    CollectionInput,
    ExperimentInput,
    FactorInput,
    FactorSetInput,
    FreezeInput,
    ResearchMutation,
    SourceChange,
    TrainingConfigurationInput,
)
from quant_platform.research.diagnostics import METRIC_VERSION, rolling_quality
from quant_platform.storage import experiments, research


def page(rows, limit, offset):
    return {
        "items": rows[:limit],
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if len(rows) > limit else None,
    }


def create_research_router(writes, reads, settings):
    router = APIRouter()

    @router.get("/research-sources")
    def sources(store=Depends(reads)):
        return {
            "items": research.sources(store),
            "collection_enabled": settings.research_data_enabled,
            "production_source": "tushare",
            "production_eligible": False,
        }

    @router.put("/research-sources/{source_id}")
    def source_change(source_id: str, value: SourceChange, store=Depends(writes)):
        return research.change_source(store, source_id, value)

    @router.post("/research-collections", status_code=202)
    def collect(value: CollectionInput, store=Depends(writes)):
        return research.request_collection(store, settings, value)

    @router.get("/research-collections")
    def collections(
        limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), store=Depends(reads)
    ):
        rows = store.rows(
            "SELECT c.*,j.status,j.error,j.progress,j.fence AS control_revision FROM research_collections c "
            "JOIN jobs j ON j.id=c.job_id ORDER BY c.created_at DESC,c.id LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        return page(rows, limit, offset)

    @router.get("/research-snapshots")
    def snapshots(
        code: str | None = None,
        source_id: str | None = Query(None, max_length=80),
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        if code:
            try:
                code = symbol(code)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        rows = store.rows(
            "SELECT * FROM research_snapshots WHERE (%s::text IS NULL OR symbol=%s) "
            "AND (%s::text IS NULL OR source_id=%s) ORDER BY retrieved_at DESC,id LIMIT %s OFFSET %s",
            (code, code, source_id, source_id, limit + 1, offset),
        )
        return page(rows, limit, offset)

    @router.get("/research-snapshots/{snapshot_id}")
    def snapshot(
        snapshot_id: UUID,
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=5000),
        store=Depends(reads),
    ):
        rows = store.rows(
            "SELECT s.*,a.path,a.manifest FROM research_snapshots s "
            "JOIN research_artifacts a ON a.id=s.artifact_id WHERE s.id=%s",
            (snapshot_id,),
        )
        if not rows:
            raise HTTPException(404, "Snapshot not found.")
        result = rows[0]
        path = safe_artifact(settings.artifact_root, result["path"])
        verify(path, result["manifest"])
        data_file = path / "data.json"
        if data_file.stat().st_size > 32 * 1024 * 1024:
            raise HTTPException(409, "Research artifact exceeds the display bound.")
        payload = json.loads(data_file.read_text())
        values = payload.get("rows", [])
        return {
            "snapshot": result,
            "rows": page(values[offset : offset + limit + 1], limit, offset),
            "research_only": True,
            "historical_availability": "unknown",
        }

    @router.get("/research-quality")
    def quality(
        snapshot_id: UUID | None = None,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        rows = store.rows(
            "SELECT q.*,s.symbol,s.source_id,s.status FROM research_quality_checks q "
            "JOIN research_snapshots s ON s.id=q.snapshot_id WHERE (%s::uuid IS NULL OR q.snapshot_id=%s) "
            "ORDER BY q.created_at DESC,q.id LIMIT %s OFFSET %s",
            (snapshot_id, snapshot_id, limit + 1, offset),
        )
        return page(rows, limit, offset)

    @router.get("/factors/inventory")
    def inventory():
        return {"items": experiments.inventory(), "research_only": True}

    def definitions(store, resource, limit, offset):
        tables = {
            "factors": "factor_definitions",
            "factor-sets": "factor_sets",
            "training-configurations": "training_configurations",
        }
        table = tables[resource]
        extra = ",f.id AS freeze_id" if resource == "training-configurations" else ""
        join = " LEFT JOIN research_freezes f ON f.configuration_id=d.id" if extra else ""
        rows = store.rows(
            f"SELECT d.*{extra} FROM {table} d{join} ORDER BY d.created_at DESC,d.id LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        return page(rows, limit, offset)

    @router.get("/factors")
    def factors(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), store=Depends(reads)):
        return definitions(store, "factors", limit, offset)

    @router.post("/factors", status_code=201)
    def factor(value: FactorInput, store=Depends(writes)):
        return experiments.save_revision(store, settings, "factors", value)

    @router.get("/factor-sets")
    def factor_sets(
        limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), store=Depends(reads)
    ):
        return definitions(store, "factor-sets", limit, offset)

    @router.post("/factor-sets", status_code=201)
    def factor_set(value: FactorSetInput, store=Depends(writes)):
        return experiments.save_revision(store, settings, "factor-sets", value)

    @router.get("/training-configurations")
    def configurations(
        limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), store=Depends(reads)
    ):
        return definitions(store, "training-configurations", limit, offset)

    @router.post("/training-configurations", status_code=201)
    def configuration(value: TrainingConfigurationInput, store=Depends(writes)):
        return experiments.save_revision(store, settings, "training-configurations", value)

    @router.post("/research-experiments", status_code=202)
    def experiment(value: ExperimentInput, store=Depends(writes)):
        return experiments.create_experiment(store, settings, value)

    @router.get("/research-experiments")
    def experiment_list(
        limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=100000), store=Depends(reads)
    ):
        rows = store.rows(
            "SELECT e.*,j.status,j.error,j.progress,j.fence AS control_revision,f.configuration_id AS frozen_configuration_id "
            "FROM research_experiments e JOIN jobs j ON j.id=e.job_id LEFT JOIN research_freezes f ON f.experiment_id=e.id "
            "ORDER BY e.created_at DESC,e.id LIMIT %s OFFSET %s",
            (limit + 1, offset),
        )
        return page(rows, limit, offset)

    @router.get("/research-experiments/{experiment_id}")
    def experiment_detail(
        experiment_id: UUID,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        rows = store.rows(
            "SELECT e.*,j.status,j.error,j.progress,j.fence AS control_revision "
            "FROM research_experiments e JOIN jobs j ON j.id=e.job_id WHERE e.id=%s",
            (experiment_id,),
        )
        if not rows:
            raise HTTPException(404, "Experiment not found.")
        trials = store.rows(
            "SELECT t.*,r.state,r.data AS result,r.artifact_id FROM research_trials t "
            "LEFT JOIN research_trial_results r ON r.trial_id=t.id WHERE t.experiment_id=%s "
            "ORDER BY t.created_at,t.id LIMIT %s OFFSET %s",
            (experiment_id, limit + 1, offset),
        )
        freezes = store.rows("SELECT * FROM research_freezes WHERE experiment_id=%s", (experiment_id,))
        return {
            "experiment": rows[0],
            "trials": page(trials, limit, offset),
            "freeze": freezes[0] if freezes else None,
            "research_only": True,
            "model_activated": False,
        }

    @router.get("/research-experiments/{experiment_id}/trials/{trial_id}/diagnostics")
    def trial_diagnostics(
        experiment_id: UUID,
        trial_id: UUID,
        feature: str | None = Query(None, max_length=128),
        limit: int = Query(60, ge=1, le=252),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        rows = store.rows(
            "SELECT t.configuration_id,t.fold,a.path,a.manifest FROM research_trials t "
            "JOIN research_trial_results r ON r.trial_id=t.id "
            "JOIN research_artifacts a ON a.id=r.artifact_id "
            "WHERE t.experiment_id=%s AND t.id=%s AND r.state='succeeded'",
            (experiment_id, trial_id),
        )
        if not rows:
            raise HTTPException(404, "Successful trial evidence not found for this experiment.")
        trial = rows[0]
        path = safe_artifact(settings.artifact_root, trial["path"])
        data_file = path / "data.json"
        verify(path, trial["manifest"])
        if data_file.stat().st_size > 32 * 1024 * 1024:
            raise HTTPException(409, "Research artifact exceeds the display bound.")
        evidence = json.loads(data_file.read_text()).get("factor_diagnostics")
        if not evidence or not evidence.get("coverage"):
            return {"status": "unavailable", "research_only": True, "reason": "factor_evidence_unavailable"}
        features = list(evidence["coverage"])
        chosen = feature if feature is not None else features[0]
        if chosen not in features:
            raise HTTPException(422, "Feature is not part of this trial's immutable evidence.")
        quality = evidence.get("quality_by_time", {}).get(chosen, [])
        return {
            "status": "available",
            "research_only": True,
            "selection_data": "validation_only",
            "experiment_id": experiment_id,
            "trial_id": trial_id,
            "configuration_id": trial["configuration_id"],
            "fold": trial["fold"],
            "features": features,
            "feature": chosen,
            "coverage": evidence["coverage"][chosen],
            "missingness": evidence.get("missingness", {}).get(chosen),
            "distribution": evidence.get("distribution", {}).get(chosen, {}),
            "correlation": evidence.get("correlation", {}).get(chosen),
            "correlation_scope": "first_64_features",
            "quality": page(quality[offset : offset + limit + 1], limit, offset),
        }

    @router.post("/research-experiments/{experiment_id}/freeze")
    def freeze(experiment_id: UUID, value: FreezeInput, store=Depends(writes)):
        return experiments.freeze_candidate(store, experiment_id, value)

    @router.post("/research-jobs/{job_id}/cancel")
    def cancel(job_id: int, value: ResearchMutation, store=Depends(writes)):
        return research.control_job(store, job_id, value, "cancel")

    @router.post("/research-jobs/{job_id}/retry", status_code=202)
    def retry(job_id: int, value: ResearchMutation, store=Depends(writes)):
        return research.control_job(store, job_id, value, "retry")

    @router.get("/models/{model_id}/diagnostics")
    def diagnostics(
        model_id: UUID,
        limit: int = Query(25, ge=1, le=100),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        if not store.rows("SELECT id FROM model_versions WHERE id=%s", (model_id,)):
            raise HTTPException(404, "Model not found.")
        latest = "SELECT DISTINCT ON(prediction_id) * FROM model_diagnostics WHERE model_id=%s AND metric_version=%s ORDER BY prediction_id,observed_at DESC,id"
        rows = store.rows(
            f"SELECT * FROM ({latest}) d ORDER BY session DESC,observed_at DESC,id LIMIT %s OFFSET %s",
            (model_id, METRIC_VERSION, limit + 1, offset),
        )
        summary_rows = store.rows(
            f"WITH latest AS ({latest}), sessions AS (SELECT DISTINCT session FROM latest ORDER BY session DESC LIMIT 60) "
            "SELECT * FROM latest WHERE session IN (SELECT session FROM sessions) ORDER BY session,observed_at LIMIT 10000",
            (model_id, METRIC_VERSION),
        )
        return {
            **page(rows, limit, offset),
            "status": "available" if rows else "unavailable",
            "summary": rolling_quality(summary_rows),
            "diagnostic_only": True,
            "metric_version": METRIC_VERSION,
        }

    @router.get("/research-stocks/{code}/scores")
    def score_history(
        code: str,
        frequency: str = Query("day", pattern="^(day|5min)$"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0, le=100000),
        store=Depends(reads),
    ):
        try:
            code = symbol(code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        rows = store.rows(
            "SELECT p.id,p.model_id,p.generation_id,r.as_of AS feature_cutoff,p.created_at AS observation_time, "
            "(p.data->'scores'->>%s)::double precision AS score,"
            "(SELECT count(*)+1 FROM jsonb_each_text(p.data->'scores') v WHERE v.value::double precision > (p.data->'scores'->>%s)::double precision) AS rank "
            "FROM prediction_runs p JOIN pipeline_runs r ON r.id=p.run_id WHERE r.frequency=%s AND r.purpose='inference' "
            "AND p.data->'scores' ? %s ORDER BY r.as_of DESC,p.id LIMIT %s OFFSET %s",
            (code, code, frequency, code, limit + 1, offset),
        )
        return {
            **page(rows, limit, offset),
            "symbol": code,
            "frequency": frequency,
            "ranking": "qlib_only",
            "historical_not_actionable": True,
        }

    return router
