"""Deliver persisted alerts to the operator. Failures do not delete or rearm alerts."""

import httpx

from quant_platform.storage import jsonb


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
    sent = 0
    for row in pending:
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
        conn.execute(
            "UPDATE jobs SET progress=%s WHERE id=%s",
            (jsonb({"channel": "webhook", "sent": sent, "undelivered": len(pending) - sent}), job["id"]),
        )
    return {"sent": sent, "undelivered": len(pending) - sent}
