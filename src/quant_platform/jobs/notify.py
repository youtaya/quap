"""Deliver persisted alerts to the operator. Failures do not delete or rearm alerts."""

from datetime import datetime

import httpx

from quant_platform.domain import digest, now
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.storage import jsonb


def recommendation_change(db, conn, report_id, frequency, portfolio_id, output, policy, available, expires):
    """Persist meaningful Qlib proposal changes in the publication transaction."""
    key = f"recommendation-notify:{frequency}:{portfolio_id or 'global'}"
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
    prior = conn.execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
    prior = prior["value"] if prior else {}
    weights = output["weights"]
    actions = {
        row["symbol"]: row["action"]
        for row in output["stocks"]
        if row.get("baseline_weight", 0) > 0 or row.get("target_weight") is None or row.get("target_weight", 0) > 0
    }
    previous_weights, previous_actions = prior.get("weights", {}), prior.get("actions", {})
    changes = [
        code
        for code in sorted(set(weights) | set(previous_weights) | set(actions) | set(previous_actions))
        if (
            abs(weights.get(code, 0) - previous_weights.get(code, 0)) > 1e-10
            and abs(weights.get(code, 0) - previous_weights.get(code, 0)) + 1e-10 >= policy["change_band"]
        )
        or actions.get(code, "watch") != previous_actions.get(code, "watch")
    ]
    if not changes or (prior and (available - datetime.fromisoformat(prior["at"])).total_seconds() < 300):
        return False
    event_key = digest([key, str(report_id)])
    inserted = conn.execute(
        "INSERT INTO alert_keys(event_key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_key", (event_key,)
    ).fetchone()
    if not inserted:
        return False
    payload = {
        "kind": "qlib_recommendation_change",
        "recommendation_id": str(report_id),
        "frequency": frequency,
        "portfolio_id": str(portfolio_id) if portfolio_id else None,
        "changed_symbols": changes,
        "available_at": available.isoformat(),
        "valid_until": expires.isoformat(),
        "message": "Review the current Qlib proposal and its validity in the dashboard. No orders sent.",
        "orders": False,
    }
    conn.execute("INSERT INTO alerts(event_key,data) VALUES(%s,%s)", (event_key, jsonb(payload)))
    conn.execute("INSERT INTO alert_outbox(event_key,data) VALUES(%s,%s)", (event_key, jsonb(payload)))
    db.set_setting(
        conn, key, {"weights": weights, "actions": actions, "at": available.isoformat(), "report_id": str(report_id)}
    )
    return True


def current_recommendation(conn, report_id):
    from quant_platform.pipeline import VALID_REPORT, validate_snapshot

    row = conn.execute(
        "SELECT r.*,a.snapshot FROM recommendations r JOIN prediction_runs p ON p.id=r.prediction_id "
        "JOIN pipeline_runs a ON a.id=p.run_id JOIN model_versions m ON m.id=p.model_id "
        "JOIN model_evaluations e ON e.model_id=m.id WHERE r.id=%s AND " + VALID_REPORT,
        (report_id,),
    ).fetchone()
    if not row:
        return False
    if row["frequency"] == "5min":
        snapshot = row["snapshot"]
        pinned = snapshot.get("portfolio_daily_baselines", {}).get(str(row["portfolio_id"]))
        if not snapshot.get("daily_baseline") or (row["portfolio_id"] and not pinned):
            return False
        try:
            validate_snapshot(
                conn,
                {
                    **snapshot,
                    "portfolios": [],
                    "portfolio_daily_baselines": {str(row["portfolio_id"]): pinned} if pinned else {},
                },
            )
        except PipelineBlocked:
            return False
    return True


def webhook_sender(url):
    def send(payload):
        with httpx.Client(timeout=10.0, follow_redirects=False, trust_env=False) as client:
            response = client.post(url, json={"event_key": payload["event_key"], "data": payload["data"]})
            response.raise_for_status()

    return send


def deliver(db, settings, job, send=None):
    if send is None and not settings.notify_webhook:
        with db.publication(job) as conn:
            conn.execute(
                "UPDATE jobs SET progress=%s WHERE id=%s",
                (jsonb({"channel": "unconfigured", "sent": 0}), job["id"]),
            )
        return {"channel": "unconfigured", "sent": 0}
    send = send or webhook_sender(settings.notify_webhook)
    pending = db.rows("SELECT * FROM alert_outbox WHERE delivered_at IS NULL ORDER BY event_key")
    sent = skipped = 0
    for row in pending:
        with db.transaction() as conn:
            db.fence(conn, job)
            if row["data"].get("kind") == "qlib_recommendation_change" and not current_recommendation(
                conn, row["data"]["recommendation_id"]
            ):
                conn.execute(
                    "UPDATE alert_outbox SET delivered_at=%s,last_error='skipped_stale_recommendation' WHERE event_key=%s",
                    (now(), row["event_key"]),
                )
                skipped += 1
                continue
        try:
            send({"event_key": row["event_key"], "data": row["data"]})
        except Exception as exc:
            with db.transaction() as conn:
                db.fence(conn, job)
                conn.execute(
                    "UPDATE alert_outbox SET attempts=attempts+1, last_error=%s WHERE event_key=%s",
                    (type(exc).__name__, row["event_key"]),
                )
            continue
        with db.transaction() as conn:
            db.fence(conn, job)
            conn.execute(
                "UPDATE alert_outbox SET delivered_at=now(), last_error=NULL WHERE event_key=%s AND delivered_at IS NULL",
                (row["event_key"],),
            )
        sent += 1
    with db.publication(job) as conn:
        remaining = conn.execute("SELECT count(*) AS n FROM alert_outbox WHERE delivered_at IS NULL").fetchone()["n"]
        result = {"sent": sent, "skipped": skipped, "undelivered": remaining}
        conn.execute(
            "UPDATE jobs SET progress=%s WHERE id=%s",
            (jsonb({"channel": "webhook", **result}), job["id"]),
        )
    return result
