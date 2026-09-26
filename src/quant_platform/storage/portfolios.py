"""Versioned model targets and transactional acceptance; never brokerage orders."""

from datetime import datetime, time
from uuid import uuid4

from quant_platform.domain import CN, digest, now
from quant_platform.domain.workflow import PipelineBlocked, PortfolioInput, ScanPolicy
from quant_platform.pipeline import active_model, next_session, validate_snapshot
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict


def check_symbols(conn, codes):
    known = {
        r["symbol"]
        for r in conn.execute("SELECT symbol FROM instruments WHERE symbol=ANY(%s)", (list(codes),)).fetchall()
    }
    if set(codes) - known:
        raise Conflict("Unknown security identities: " + ", ".join(sorted(set(codes) - known)))


def save_portfolio(conn, value, portfolio_id=None, effective=None, origin="manual", capacity=300):
    conn.execute("SELECT pg_advisory_xact_lock(77610240)")
    value = PortfolioInput.model_validate(value)
    check_symbols(conn, value.weights)
    others = conn.execute(
        "SELECT r.data FROM model_portfolios p JOIN model_portfolio_revisions r "
        "ON r.portfolio_id=p.id AND r.revision=p.revision WHERE p.id IS DISTINCT FROM %s::uuid",
        (portfolio_id,),
    ).fetchall()
    watch = conn.execute("SELECT symbols FROM watchlist_revisions ORDER BY revision DESC LIMIT 1").fetchone()
    selected = (
        set(value.weights) | {s for p in others for s in p["data"]["weights"]} | set(watch["symbols"] if watch else [])
    )
    if len(selected) > capacity:
        raise Conflict("Explicit portfolio/watchlist selections exceed the intraday capacity.")
    if portfolio_id:
        head = conn.execute("SELECT * FROM model_portfolios WHERE id=%s FOR UPDATE", (portfolio_id,)).fetchone()
        if not head or head["revision"] != value.expected_revision:
            raise Conflict("Model-portfolio revision conflict.")
        revision = head["revision"] + 1
        conn.execute(
            "UPDATE model_portfolios SET name=%s,revision=%s WHERE id=%s", (value.name, revision, portfolio_id)
        )
    else:
        if value.expected_revision:
            raise Conflict("New model portfolios require revision zero.")
        portfolio_id, revision = uuid4(), 1
        conn.execute(
            "INSERT INTO model_portfolios(id,name,revision) VALUES(%s,%s,%s)", (portfolio_id, value.name, revision)
        )
    current = now()
    effective = effective or datetime.combine(next_session(conn, current.astimezone(CN).date()), time(9, 30), CN)
    if effective <= current:
        raise Conflict("A revision must take effect in the future; request a fresh proposal.")
    data = {
        "name": value.name,
        "weights": value.weights,
        "cash_weight": value.cash_weight,
        "origin": origin,
        "baseline_type": "model_portfolio",
        "orders": False,
    }
    conn.execute(
        "INSERT INTO model_portfolio_revisions(portfolio_id,revision,effective_from,data) VALUES(%s,%s,%s,%s)",
        (portfolio_id, revision, effective, jsonb(data)),
    )
    conn.execute(
        "INSERT INTO audit(action,target,data) VALUES('model_portfolio_revision',%s,%s)",
        (str(portfolio_id), jsonb({"revision": revision, "effective_from": effective, **data})),
    )
    return {"id": str(portfolio_id), "revision": revision, "effective_from": effective, "data": data, "orders": False}


def save_watchlist(db, value, capacity):
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        head = conn.execute("SELECT coalesce(max(revision),0) AS revision FROM watchlist_revisions").fetchone()
        if head["revision"] != value.expected_revision:
            raise Conflict("Watchlist revision conflict.")
        check_symbols(conn, value.symbols)
        portfolios = conn.execute(
            "SELECT r.data FROM model_portfolios p JOIN model_portfolio_revisions r "
            "ON r.portfolio_id=p.id AND r.revision=p.revision"
        ).fetchall()
        selected = set(value.symbols) | {s for p in portfolios for s in p["data"]["weights"]}
        if len(selected) > capacity:
            raise Conflict("Explicit portfolio/watchlist selections exceed the intraday capacity.")
        row = conn.execute(
            "INSERT INTO watchlist_revisions(symbols) VALUES(%s) RETURNING *", (jsonb(value.symbols),)
        ).fetchone()
        conn.execute(
            "INSERT INTO audit(action,target,data) VALUES('watchlist_revision',%s,%s)",
            (str(row["revision"]), jsonb(value.model_dump())),
        )
        return {**row, "intraday_state": "warming_up", "pool_effective": "next session"}


def accept(db, recommendation_id, value, capacity=300):
    request_hash = digest({"recommendation_id": str(recommendation_id), **value.model_dump(mode="json")})
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(77610240)")
        prior = conn.execute(
            "SELECT * FROM recommendation_acceptances WHERE request_key=%s", (value.request_key,)
        ).fetchone()
        if prior:
            if prior["request_hash"] != request_hash:
                raise Conflict("Acceptance idempotency key was used for different inputs.")
            return {"id": str(prior["portfolio_id"]), "revision": prior["revision"], "reused": True, "orders": False}
        row = conn.execute(
            "SELECT r.*,p.model_id,a.snapshot FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
            "JOIN pipeline_runs a ON a.id=p.run_id WHERE r.id=%s FOR UPDATE OF r",
            (recommendation_id,),
        ).fetchone()
        if not row or row["state"] != "published" or not row["available_at"] <= now() < row["valid_until"]:
            raise Conflict("Recommendation is absent, superseded, or expired.")
        if now() >= row["effective_from"]:
            raise Conflict("Recommendation effective time has passed; request a fresh proposal.")
        release = active_model(conn, row["frequency"])
        if not release or release["id"] != row["model_id"]:
            raise Conflict("Recommendation no longer references the approved active release.")
        policy = conn.execute("SELECT * FROM scan_policies ORDER BY revision DESC LIMIT 1").fetchone()
        if (
            not policy
            or row["policy_revision"] != policy["revision"]
            or value.expected_policy_revision != policy["revision"]
        ):
            raise Conflict("Scan policy changed; request a fresh proposal.")
        if row["portfolio_id"] != value.portfolio_id or row["portfolio_revision"] != value.expected_revision:
            raise Conflict("Recommendation portfolio context/revision does not match acceptance.")
        if row["frequency"] == "5min":
            snapshot = row["snapshot"]
            pinned = snapshot.get("portfolio_daily_baselines", {}).get(str(value.portfolio_id))
            if not snapshot.get("daily_baseline") or (value.portfolio_id and not pinned):
                raise Conflict("Pinned daily model baseline unavailable.")
            try:
                validate_snapshot(
                    conn,
                    {
                        **snapshot,
                        "portfolios": [],
                        "portfolio_daily_baselines": {str(value.portfolio_id): pinned} if pinned else {},
                    },
                )
            except PipelineBlocked as exc:
                raise Conflict(str(exc)) from None
        baseline = {}
        if value.portfolio_id:
            current = conn.execute(
                "SELECT p.revision,r.data FROM model_portfolios p JOIN model_portfolio_revisions r "
                "ON r.portfolio_id=p.id AND r.revision=p.revision WHERE p.id=%s FOR UPDATE OF p",
                (value.portfolio_id,),
            ).fetchone()
            if not current or current["revision"] != value.expected_revision:
                raise Conflict("Model-portfolio revision changed.")
            baseline = current["data"]["weights"]
        weights = row["data"]["weights"]
        if value.selected_symbols is not None:
            offered = {s["symbol"] for s in row["data"]["stocks"] if s.get("target_weight") is not None}
            if not set(value.selected_symbols).issubset(offered):
                raise Conflict("Selected changes are not available in this proposal.")
            weights = {**baseline}
            for code in value.selected_symbols:
                weights.pop(code, None)
                if row["data"]["weights"].get(code, 0) > 0:
                    weights[code] = row["data"]["weights"][code]
        cash = 1 - sum(weights.values())
        limits = ScanPolicy.model_validate(policy["data"])
        if (
            cash < limits.minimum_cash - 1e-8
            or any(w > limits.max_weight + 1e-8 for w in weights.values())
            or len(weights) > limits.top_n
        ):
            raise Conflict("Selected changes violate the approved portfolio risk limits.")
        targets = PortfolioInput(
            name=value.name, weights=weights, cash_weight=cash, expected_revision=value.expected_revision
        )
        result = save_portfolio(
            conn, targets, value.portfolio_id, row["effective_from"], "accepted_qlib_proposal", capacity
        )
        conn.execute(
            "INSERT INTO recommendation_acceptances(request_key,recommendation_id,portfolio_id,revision,request_hash) "
            "VALUES(%s,%s,%s,%s,%s)",
            (value.request_key, recommendation_id, result["id"], result["revision"], request_hash),
        )
        return result
