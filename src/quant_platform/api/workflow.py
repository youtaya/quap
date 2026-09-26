"""Authenticated Qlib workflow resources; reads never trigger background work."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from quant_platform import pipeline
from quant_platform.domain import now, symbol
from quant_platform.domain.workflow import (
    Acceptance,
    PipelineBlocked,
    PolicyChange,
    PortfolioInput,
    ReleaseAction,
    RunInput,
    ScanPolicy,
    WatchlistInput,
)
from quant_platform.storage.portfolios import accept, save_portfolio, save_watchlist

VALID_REPORT = pipeline.VALID_REPORT


def attach_validity(store, rows, drop_snapshot=False):
    with store.transaction() as conn:
        for row in rows:
            if row["valid"] and row["frequency"] == "5min":
                snapshot = row["snapshot"]
                pinned = snapshot.get("portfolio_daily_baselines", {}).get(str(row["portfolio_id"]))
                if not snapshot.get("daily_baseline") or (row["portfolio_id"] and not pinned):
                    row["valid"] = False
                else:
                    try:
                        pipeline.validate_snapshot(
                            conn,
                            {
                                **snapshot,
                                "portfolios": [],
                                "portfolio_daily_baselines": {str(row["portfolio_id"]): pinned} if pinned else {},
                            },
                        )
                    except PipelineBlocked:
                        row["valid"] = False
            row["acceptance_open"] = bool(row["valid"] and now() < row["effective_from"])
            if drop_snapshot:
                row.pop("snapshot", None)
    return rows


def create_workflow_router(writes, reads, settings):
    router = APIRouter()

    @router.get("/data-readiness")
    def readiness(store=Depends(reads)):
        return pipeline.readiness(store, settings)

    @router.get("/qlib-generations")
    def generations(store=Depends(reads)):
        return store.rows("SELECT * FROM qlib_generations ORDER BY created_at DESC LIMIT 100")

    @router.get("/pipeline-runs")
    def runs(store=Depends(reads)):
        return store.rows(
            "SELECT id,frequency,purpose,as_of,state,error,result,created_at FROM pipeline_runs ORDER BY created_at DESC LIMIT 100"
        )

    @router.post("/pipeline-runs", status_code=202)
    def create_run(value: RunInput, store=Depends(writes)):
        run = pipeline.create_run(store, settings, value)
        return {"run_id": str(run["id"]), "state": run["state"], "result": run["result"]}

    @router.get("/pipeline-runs/{run_id}")
    def run_detail(run_id: UUID, store=Depends(reads)):
        return pipeline.run_details(store, run_id)

    @router.post("/pipeline-runs/{run_id}/retry", status_code=202)
    def retry(run_id: UUID, store=Depends(writes)):
        return pipeline.retry_run(store, run_id)

    @router.post("/model-training-runs", status_code=202)
    def train(value: RunInput, store=Depends(writes)):
        if value.purpose != "training":
            raise HTTPException(422, "Training requests must explicitly set purpose=training.")
        return create_run(value, store)

    @router.get("/models")
    def models(store=Depends(reads)):
        return store.rows(
            "SELECT m.*,e.data AS evaluation,e.passed FROM model_versions m JOIN model_evaluations e "
            "ON e.model_id=m.id ORDER BY m.created_at DESC LIMIT 100"
        )

    @router.post("/models/{model_id}/promote")
    @router.post("/models/{model_id}/rollback")
    def promote(model_id: UUID, value: ReleaseAction, store=Depends(writes)):
        return pipeline.promote(store, model_id, value.expected_active_id)

    @router.get("/models/{model_id}/shadow-observations")
    def shadow(model_id: UUID, store=Depends(reads)):
        return store.rows("SELECT * FROM model_shadow_samples WHERE model_id=%s ORDER BY day DESC", (model_id,))

    @router.get("/scan-policy")
    def policy(store=Depends(reads)):
        rows = store.rows("SELECT * FROM scan_policies ORDER BY revision DESC LIMIT 1")
        return rows[0] if rows else {"revision": 0, "data": ScanPolicy().model_dump()}

    @router.put("/scan-policy")
    def edit_policy(value: PolicyChange, store=Depends(writes)):
        return pipeline.change_policy(store, value)

    @router.get("/model-portfolios")
    def portfolios(store=Depends(reads)):
        return store.rows(
            "SELECT p.*,r.effective_from,r.data FROM model_portfolios p JOIN model_portfolio_revisions r "
            "ON r.portfolio_id=p.id AND r.revision=p.revision ORDER BY p.created_at"
        )

    @router.post("/model-portfolios", status_code=201)
    def create_portfolio(value: PortfolioInput, store=Depends(writes)):
        with store.transaction() as conn:
            return save_portfolio(conn, value, capacity=settings.intraday_pool_capacity)

    @router.put("/model-portfolios/{portfolio_id}")
    def edit_portfolio(portfolio_id: UUID, value: PortfolioInput, store=Depends(writes)):
        with store.transaction() as conn:
            return save_portfolio(conn, value, portfolio_id, capacity=settings.intraday_pool_capacity)

    @router.get("/model-portfolios/{portfolio_id}/revisions")
    def revisions(portfolio_id: UUID, store=Depends(reads)):
        return store.rows(
            "SELECT * FROM model_portfolio_revisions WHERE portfolio_id=%s ORDER BY revision DESC", (portfolio_id,)
        )

    @router.get("/watchlist")
    def watchlist(store=Depends(reads)):
        rows = store.rows("SELECT * FROM watchlist_revisions ORDER BY revision DESC LIMIT 1")
        return rows[0] if rows else {"revision": 0, "symbols": []}

    @router.put("/watchlist")
    def edit_watchlist(value: WatchlistInput, store=Depends(writes)):
        return save_watchlist(store, value, settings.intraday_pool_capacity)

    @router.get("/recommendations")
    def recommendations(
        frequency: str = Query("day", pattern="^(day|5min)$"),
        portfolio_id: UUID | None = None,
        valid_only: bool = True,
        store=Depends(reads),
    ):
        rows = store.rows(
            f"SELECT r.*,p.model_id,p.generation_id,a.snapshot,({VALID_REPORT}) AS valid "
            "FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id JOIN model_versions m ON m.id=p.model_id "
            "JOIN model_evaluations e ON e.model_id=m.id JOIN pipeline_runs a ON a.id=p.run_id "
            "WHERE r.frequency=%s AND r.portfolio_id IS NOT DISTINCT FROM %s::uuid "
            f"AND (NOT %s OR ({VALID_REPORT})) ORDER BY r.as_of DESC LIMIT 100",
            (frequency, portfolio_id, valid_only),
        )
        return [row for row in attach_validity(store, rows, drop_snapshot=True) if not valid_only or row["valid"]]

    @router.get("/recommendations/stock/{code}")
    def stock(
        code: str,
        frequency: str = Query("day", pattern="^(day|5min)$"),
        portfolio_id: UUID | None = None,
        store=Depends(reads),
    ):
        try:
            code = symbol(code)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        reports = recommendations(frequency, portfolio_id, True, store)
        if not reports:
            return {"symbol": code, "state": "unavailable", "reason": "No valid Qlib report", "orders": False}
        report = reports[0]
        view = next((r for r in report["data"]["stocks"] if r["symbol"] == code), None)
        if view is None:
            return {
                "symbol": code,
                "state": "unavailable",
                "reason": "Feature/prediction coverage unavailable",
                "orders": False,
            }
        view = dict(view)
        if portfolio_id is None:
            for key in ("baseline_weight", "target_weight", "action"):
                view.pop(key, None)
        return {
            "symbol": code,
            "state": "available",
            "model_view": view,
            "portfolio_id": portfolio_id,
            "report_id": report["id"],
            "as_of": report["as_of"],
            "available_at": report["available_at"],
            "valid_until": report["valid_until"],
            "orders": False,
        }

    @router.get("/recommendations/{recommendation_id}")
    def detail(recommendation_id: UUID, store=Depends(reads)):
        rows = store.rows(
            f"SELECT r.*,p.model_id,p.generation_id,p.run_id,g.manifest,m.metadata,e.data AS evaluation,({VALID_REPORT}) AS valid,"
            "a.snapshot FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
            "JOIN qlib_generations g ON g.id=p.generation_id JOIN model_versions m ON m.id=p.model_id "
            "JOIN model_evaluations e ON e.model_id=m.id JOIN pipeline_runs a ON a.id=p.run_id WHERE r.id=%s",
            (recommendation_id,),
        )
        if not rows:
            raise HTTPException(404, "Recommendation not found.")
        return {
            **attach_validity(store, rows)[0],
            "human_decisions": store.rows(
                "SELECT * FROM recommendation_acceptances WHERE recommendation_id=%s", (recommendation_id,)
            ),
        }

    @router.post("/recommendations/{recommendation_id}/accept", status_code=201)
    def accept_recommendation(recommendation_id: UUID, value: Acceptance, store=Depends(writes)):
        return accept(store, recommendation_id, value, settings.intraday_pool_capacity)

    return router
