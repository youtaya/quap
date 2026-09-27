"""Transactional, edge-triggered intraday observation alerts.

The native recommendation and screening jobs that used to live here are retired: ``Worker.execute``
rejects their job kinds and the rules API answers 410, so a tombstone function with no caller was
only a second place to keep that decision in step.
"""

from quant_platform.analysis import basket_summary
from quant_platform.domain import CN, digest, fresh, now, session
from quant_platform.storage import jsonb


def alert(conn, key, observation, value, threshold, cooldown, data, at):
    if value is None:
        return
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
    previous = conn.execute("SELECT * FROM alert_state WHERE key=%s FOR UPDATE", (key,)).fetchone()
    if previous and previous["observation"] == observation:
        return
    active = abs(value) >= threshold
    last = previous["last_alert"] if previous else None
    if active and not (previous and previous["active"]) and (last is None or (at - last).total_seconds() >= cooldown):
        event_key = digest((key, observation))
        inserted = conn.execute(
            "INSERT INTO alert_keys(event_key) VALUES(%s) ON CONFLICT DO NOTHING RETURNING event_key", (event_key,)
        ).fetchone()
        if inserted:
            payload = {**data, "value": value, "threshold": threshold, "source": "market", "observed_at": at}
            conn.execute(
                "INSERT INTO alerts(event_key,data) VALUES(%s,%s)",
                (event_key, jsonb(payload)),
            )
            conn.execute(
                "INSERT INTO alert_outbox(event_key,data) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (event_key, jsonb(payload)),
            )
        last = at
    conn.execute(
        "INSERT INTO alert_state VALUES(%s,%s,%s,%s,now()) ON CONFLICT(key) DO UPDATE "
        "SET active=excluded.active,observation=excluded.observation,last_alert=excluded.last_alert,updated_at=now()",
        (key, active, observation, last),
    )


def intraday(db, job):
    at = now()
    day = at.astimezone(CN).date()
    quotes = {r["symbol"]: r["data"] for r in db.rows("SELECT * FROM latest_quotes")}
    baskets = db.active_baskets(day)
    active = session(at, db.calendar_open(day))[1]
    with db.publication(job) as conn:
        for basket in baskets:
            if basket["paused"]:
                continue
            data = basket_summary(basket["data"]["members"], quotes, at)
            data.update({"source": "market", "as_of": at.isoformat(), "basket_revision": basket["revision"]})
            conn.execute(
                "INSERT INTO basket_observations VALUES(%s,%s,%s,%s) ON CONFLICT(basket_id,revision,minute) "
                "DO UPDATE SET data=excluded.data",
                (basket["id"], basket["revision"], at.replace(second=0, microsecond=0), jsonb(data)),
            )
            policy = basket["data"]["alerts"]
            scope = digest((str(basket["id"]), basket["revision"], str(day), policy))
            if active and data["coverage"] >= policy["minimum_coverage"]:
                alert(
                    conn,
                    scope,
                    data["observation"],
                    data["daily_return"],
                    policy["basket_change"],
                    policy["cooldown"],
                    {"target": basket["name"], "rule": "basket_daily_return"},
                    at,
                )
            if active:
                for code in basket["data"]["members"]:
                    q = quotes.get(code, {})
                    if (
                        fresh(q.get("source_time"), at)
                        and q.get("reference_verified")
                        and q.get("pre_close")
                        and q.get("close")
                    ):
                        identity = digest({k: v for k, v in q.items() if k != "received_at"})
                        alert(
                            conn,
                            scope + code,
                            identity,
                            q["close"] / q["pre_close"] - 1,
                            policy["stock_change"],
                            policy["cooldown"],
                            {"target": code, "rule": "stock_daily_return"},
                            at,
                        )
