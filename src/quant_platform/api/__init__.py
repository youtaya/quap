"""Authenticated API; browser clients never own market jobs."""

import hmac
import time
from collections import deque
from math import ceil
from threading import Lock
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import Field

from quant_platform.config import Settings
from quant_platform.domain import BasketInput, CN, ScreenRule, StrictModel, fresh, now, symbol
from quant_platform.storage import Database, jsonb
from quant_platform.storage.baskets import Conflict, save_basket


class Control(StrictModel):
    action: Literal["pause", "resume", "refresh", "analyze", "research", "doctor"]


class BasketControl(StrictModel):
    action: Literal["pause", "resume", "archive"]
    expected_revision: int = Field(ge=1)
    expected_control_revision: int = Field(0, ge=0)


class RuntimeOptions(StrictModel):
    quote_seconds: int = Field(ge=15, le=3600)
    expected_revision: int = Field(0, ge=0)


class RuleInput(StrictModel):
    rule: ScreenRule
    expected_revision: int = Field(ge=0)


class Approval(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    symbols: list[str] = Field(min_length=1, max_length=200)


def create_app(settings=None, database=None, read_database=None):
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.db = database or Database(settings.dsn, role="quant_worker")
        app.state.read_db = read_database or (
            database if database is not None else Database(settings.dsn, role="quant_read")
        )
        yield
        if database is None:
            app.state.db.close()
            if app.state.read_db is not app.state.db:
                app.state.read_db.close()

    app = FastAPI(title="Standalone Quant Platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    security = HTTPBearer(auto_error=False)
    timings, timing_lock = deque(maxlen=1000), Lock()

    @app.middleware("http")
    async def measure_request(request, call_next):
        started, code = time.monotonic(), 503
        try:
            response = await call_next(request)
            code = response.status_code
            return response
        finally:
            if request.url.path.startswith("/api/v1/"):
                finished = time.monotonic()
                with timing_lock:
                    timings.append((finished, finished - started, code))
                try:
                    from quant_platform.observations import append_jsonl

                    append_jsonl(
                        settings.observation_root,
                        "api",
                        {"path": request.url.path, "status": code, "at": now().isoformat()},
                    )
                except OSError:
                    pass

    def api_metrics():
        cutoff = time.monotonic() - 300
        with timing_lock:
            samples = [item for item in timings if item[0] >= cutoff]
        latencies = sorted(item[1] for item in samples)
        count = len(samples)
        return {
            "scope": "this API process; latest 1000 requests within 300 seconds; resets on restart",
            "requests": count,
            "client_errors": sum(400 <= code < 500 for _, _, code in samples),
            "server_errors": sum(code >= 500 for _, _, code in samples),
            "server_error_ratio": sum(code >= 500 for _, _, code in samples) / count if count else None,
            "mean_latency_seconds": sum(latencies) / count if count else None,
            "p95_latency_seconds": latencies[ceil(count * 0.95) - 1] if count else None,
        }

    def auth(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        token = settings.api_token.get_secret_value()
        if not token:
            raise HTTPException(503, "Operator authentication is not configured.")
        if not credentials or not hmac.compare_digest(credentials.credentials.encode(), token.encode()):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def db():
        return app.state.db

    def reads():
        return app.state.read_db

    @app.exception_handler(Conflict)
    async def conflict_handler(_, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def failure_handler(_, exc):
        return JSONResponse(status_code=503, content={"detail": "Backend unavailable; inspect operator diagnostics."})

    @app.get("/health/live")
    def live():
        return {"alive": True}

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])

    @router.get("/status")
    def status(store=Depends(reads)):
        from quant_platform.operations import status as get_status

        result = get_status(store, settings)
        result["metrics"]["api"] = api_metrics()
        result["db_roles"] = {
            "read": getattr(app.state.read_db, "role", None) or "shared",
            "write": getattr(app.state.db, "role", None) or "shared",
        }
        return result

    @router.get("/boards")
    def boards(store=Depends(reads)):
        from quant_platform.operations import board_status

        return board_status(store)

    @router.get("/settings")
    def runtime_options(store=Depends(reads)):
        rows = store.rows("SELECT value,revision FROM settings WHERE key='runtime_options'")
        return {
            "revision": rows[0]["revision"] if rows else 0,
            "quote_seconds": rows[0]["value"]["quote_seconds"] if rows else settings.quote_seconds,
            "provider": settings.provider,
            "history_sessions": settings.history_sessions,
            "realtime_rpm": settings.realtime_rpm,
            "ordinary_rpm": settings.ordinary_rpm,
            "daily_quota": settings.daily_quota,
            "qlib_enabled": settings.qlib_enabled,
        }

    @router.put("/settings")
    def change_options(value: RuntimeOptions, store=Depends(db)):
        with store.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(77610233)")
            prior = conn.execute("SELECT revision FROM settings WHERE key='runtime_options'").fetchone()
            if (prior["revision"] if prior else 0) != value.expected_revision:
                raise Conflict("Settings revision conflict.")
            store.set_setting(conn, "runtime_options", {"quote_seconds": value.quote_seconds})
            conn.execute(
                "INSERT INTO audit(action,target,data) VALUES('settings','runtime_options',%s)",
                (jsonb(value.model_dump()),),
            )
        return {"revision": value.expected_revision + 1}

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: int, store=Depends(db)):
        with store.transaction() as conn:
            row = conn.execute(
                "UPDATE jobs SET status='pending',failures=0,available_at=now(),error=NULL "
                "WHERE id=%s AND status='failed' RETURNING id",
                (job_id,),
            ).fetchone()
            if not row:
                raise Conflict("Only failed jobs can be retried; revalidate blocked capabilities with doctor.")
            conn.execute("INSERT INTO audit(action,target,data) VALUES('retry',%s,'{}')", (str(job_id),))
        return {"job_id": job_id}

    @router.get("/instruments")
    def instruments(search: str = Query("", max_length=120), store=Depends(reads)):
        return store.rows(
            "SELECT symbol,name,board,status,list_date FROM instruments "
            "WHERE strpos(lower(symbol || ' ' || name),lower(%s))>0 ORDER BY symbol LIMIT 500",
            (search,),
        )

    @router.get("/quotes")
    def quotes(code: str | None = None, store=Depends(reads)):
        if code:
            try:
                code = symbol(code)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        rows = store.rows(
            "SELECT * FROM latest_quotes WHERE (%s::text IS NULL OR symbol=%s) ORDER BY symbol LIMIT 20000",
            (code, code),
        )
        return [{**r, "fresh": fresh(r["data"].get("source_time"))} for r in rows]

    @router.get("/history/{code}")
    def history(code: str, as_of: date | None = None, limit: int = Query(500, ge=1, le=2000), store=Depends(reads)):
        try:
            return store.history(symbol(code), as_of or now().astimezone(CN).date(), limit)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/baskets")
    def baskets(store=Depends(reads)):
        return store.rows(
            "SELECT b.*,r.effective_day,r.data FROM baskets b JOIN basket_revisions r "
            "ON r.basket_id=b.id AND r.revision=b.revision ORDER BY b.created_at"
        )

    @router.post("/baskets", status_code=201)
    def create_basket(value: BasketInput, store=Depends(db)):
        with store.transaction() as conn:
            return save_basket(conn, value)

    @router.put("/baskets/{basket_id}")
    def edit_basket(basket_id: UUID, value: BasketInput, store=Depends(db)):
        with store.transaction() as conn:
            return save_basket(conn, value, basket_id)

    @router.get("/baskets/{basket_id}/revisions")
    def revisions(basket_id: UUID, store=Depends(reads)):
        return store.rows("SELECT * FROM basket_revisions WHERE basket_id=%s ORDER BY revision DESC", (basket_id,))

    @router.get("/baskets/{basket_id}/observations")
    def observations(basket_id: UUID, store=Depends(reads)):
        return store.rows(
            "SELECT * FROM basket_observations WHERE basket_id=%s ORDER BY minute DESC LIMIT 600", (basket_id,)
        )

    @router.post("/baskets/{basket_id}/control")
    def basket_control(basket_id: UUID, value: BasketControl, store=Depends(db)):
        with store.transaction() as conn:
            row = conn.execute("SELECT * FROM baskets WHERE id=%s FOR UPDATE", (basket_id,)).fetchone()
            if (
                not row
                or row["archived"]
                or row["revision"] != value.expected_revision
                or row["control_revision"] != value.expected_control_revision
            ):
                raise Conflict("Basket revision/control conflict or archived basket.")
            conn.execute(
                "UPDATE baskets SET paused=%s,archived=%s,control_revision=control_revision+1 WHERE id=%s",
                (value.action != "resume", value.action == "archive", basket_id),
            )
            conn.execute(
                "INSERT INTO audit(action,target,data) VALUES(%s,%s,%s)",
                ("basket_" + value.action, str(basket_id), jsonb(value.model_dump())),
            )
        return {"applied": True}

    @router.get("/rules")
    def rules(store=Depends(reads)):
        rows = store.rows("SELECT * FROM rules ORDER BY revision DESC LIMIT 1")
        return rows[0] if rows else {"revision": 0, "data": ScreenRule().model_dump()}

    @router.put("/rules")
    def change_rule(value: RuleInput, store=Depends(db)):
        with store.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(77610232)")
            previous = conn.execute("SELECT coalesce(max(revision),0) AS revision FROM rules").fetchone()
            if previous["revision"] != value.expected_revision:
                raise Conflict("Rule revision conflict.")
            row = conn.execute(
                "INSERT INTO rules(data) VALUES(%s) RETURNING revision", (jsonb(value.rule.model_dump()),)
            ).fetchone()
            conn.execute(
                "INSERT INTO audit(action,target,data) VALUES('rule','default',%s)", (jsonb(value.model_dump()),)
            )
            return row

    @router.get("/reports")
    def reports(
        kind: Literal["stock", "basket", "screen", "factor", "backtest"] = "basket",
        target: str | None = None,
        limit: int = Query(100, ge=1, le=500),
        store=Depends(reads),
    ):
        return store.rows(
            "SELECT * FROM reports WHERE kind=%s AND (%s::text IS NULL OR target=%s) "
            "ORDER BY as_of DESC,id DESC LIMIT %s",
            (kind, target, target, limit),
        )

    @router.post("/candidates/{report_id}/approve", status_code=201)
    def approve(report_id: int, value: Approval, store=Depends(db)):
        with store.transaction() as conn:
            row = conn.execute("SELECT data FROM reports WHERE id=%s AND kind='screen'", (report_id,)).fetchone()
            try:
                selected = [symbol(s) for s in value.symbols]
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            if (
                not row
                or not set(selected).issubset({r["symbol"] for r in row["data"]["candidates"]})
                or len(set(selected)) != len(selected)
            ):
                raise Conflict("Selection is not a unique subset of this report's candidates.")
            result = save_basket(conn, BasketInput(name=value.name, members={s: 1 for s in selected}))
            conn.execute(
                "INSERT INTO audit(action,target,data) VALUES('candidate_approval',%s,%s)",
                (str(report_id), jsonb(result)),
            )
            return result

    @router.get("/alerts")
    def alerts(store=Depends(reads)):
        return store.rows("SELECT * FROM alerts ORDER BY recorded_at DESC LIMIT 200")

    @router.get("/jobs")
    def jobs(store=Depends(reads)):
        return store.rows("SELECT * FROM jobs ORDER BY id DESC LIMIT 200")

    @router.get("/jobs/{job_id}")
    def job(job_id: int, store=Depends(reads)):
        rows = store.rows("SELECT * FROM jobs WHERE id=%s", (job_id,))
        if not rows:
            raise HTTPException(404, "Job not found.")
        return rows[0]

    @router.post("/control", status_code=202)
    def control(value: Control, store=Depends(db)):
        with store.transaction() as conn:
            if value.action in {"pause", "resume"}:
                store.set_setting(conn, "polling_paused", value.action == "pause")
                conn.execute("INSERT INTO audit(action,target,data) VALUES(%s,'polling','{}')", (value.action,))
                return {"applied": True}
            if value.action == "refresh":
                # Leave feed ownership, interval enforcement and quota accounting with the scheduler.
                job_id = store.enqueue(conn, "refresh", "history", f"manual-refresh:{uuid4()}", priority=210)
                return {"job_id": job_id}
            kind = {"doctor": "doctor", "research": "research"}.get(value.action, "analyze")
            target = store.setting("analysis_target")
            if kind in {"analyze", "research"} and not target:
                raise Conflict("No completed daily target is available.")
            queue = {"doctor": "history", "research": "research"}.get(kind, "analysis")
            job_id = store.enqueue(
                conn,
                kind,
                queue,
                f"manual:{uuid4()}",
                {"day": target} if target else {},
                200,
            )
            return {"job_id": job_id}

    app.include_router(router)
    return app
