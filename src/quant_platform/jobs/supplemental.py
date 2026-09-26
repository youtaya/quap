"""Fenced supplemental collection with durable quota reservations and outcomes."""

from datetime import date
from uuid import uuid4

from quant_platform.domain import CN, digest, now
from quant_platform.domain.research import SOURCE_CATALOG
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.providers import Deferred
from quant_platform.research.process import run_research
from quant_platform.research.supplemental import QUALITY_VERSION, normalize
from quant_platform.storage import LostLease, jsonb
from quant_platform.storage.research import canonical_comparison, enabled_source, publish_artifact, store_artifact


def reserve_attempt(db, settings, job, collection, code):
    with db.transaction() as conn:
        db.fence(conn, job)
        enabled_source(conn, collection["source_id"], collection["source_revision"])
        conn.execute("SELECT pg_advisory_xact_lock(77610261)")
        attempts = conn.execute(
            "SELECT a.*,o.data AS outcome FROM research_request_attempts a LEFT JOIN research_attempt_outcomes o ON o.attempt_id=a.id "
            "WHERE collection_id=%s AND symbol=%s ORDER BY attempt",
            (collection["id"], code),
        ).fetchall()
        if attempts and attempts[-1]["outcome"] and attempts[-1]["outcome"]["status"] == "available":
            return None, attempts[-1]["outcome"]
        if len(attempts) >= settings.research_attempts:
            return None, attempts[-1]["outcome"] or {
                "status": "unavailable",
                "records": [],
                "reason": "attempt_interrupted",
            }
        count = conn.execute(
            "SELECT count(*) AS n FROM research_request_attempts WHERE started_at>clock_timestamp()-interval '60 seconds'"
        ).fetchone()["n"]
        if count >= settings.research_rpm:
            raise Deferred("Supplemental collection quota exhausted.", seconds=60)
        attempt = conn.execute(
            "INSERT INTO research_request_attempts(collection_id,symbol,attempt) VALUES(%s,%s,%s) RETURNING id",
            (collection["id"], code, len(attempts) + 1),
        ).fetchone()["id"]
        return attempt, None


def collect(db, settings, job, stop, runner=run_research):
    if not settings.research_data_enabled:
        raise PipelineBlocked("Supplemental collection is disabled.")
    collection = db.rows("SELECT * FROM research_collections WHERE job_id=%s", (job["id"],))[0]
    scope = collection["scope"]
    source = SOURCE_CATALOG[collection["source_id"]]
    if source["frequency"] != "day" and scope["end"] != str(now().astimezone(CN).date()):
        raise PipelineBlocked("Same-day supplemental request expired; request a new current session explicitly.")
    for code in scope["symbols"]:
        if stop.is_set():
            raise LostLease("Supplemental collection cancelled.")
        if db.rows("SELECT 1 FROM research_snapshots WHERE collection_id=%s AND symbol=%s", (collection["id"], code)):
            continue
        while True:
            attempt, result = reserve_attempt(db, settings, job, collection, code)
            if attempt is None:
                break
            try:
                result = runner(
                    "quant_platform.research.adata_adapter",
                    {
                        "source_id": collection["source_id"],
                        "symbol": code,
                        "start": scope["start"],
                        "end": scope["end"],
                        "deadline": settings.research_request_deadline_seconds,
                        "response_cap": settings.research_response_bytes,
                    },
                    settings.research_request_deadline_seconds,
                    stop,
                )
            except PipelineBlocked:
                result = {"status": "unavailable", "records": [], "reason": "bounded_worker_failure"}
            result["retrieved_at"] = now().isoformat()
            with db.transaction() as conn:
                db.fence(conn, job)
                conn.execute(
                    "INSERT INTO research_attempt_outcomes(attempt_id,status,data) VALUES(%s,%s,%s)",
                    (attempt, result["status"], jsonb(result)),
                )
            if result["status"] == "available":
                break
        from datetime import datetime

        retrieved = datetime.fromisoformat(result.get("retrieved_at", now().isoformat()))
        rows, validation = normalize(
            result.get("records", []),
            code,
            date.fromisoformat(scope["start"]),
            date.fromisoformat(scope["end"]),
            source["frequency"],
            retrieved,
        )
        if result["status"] != "available":
            validation.update(status="unavailable", reason=result.get("reason", "source_unavailable"))
        watermark, comparison = canonical_comparison(db, scope, code, rows, retrieved)
        if validation["status"] == "available" and comparison["completeness"] != "complete":
            validation.update(status="quarantined", reason="incomplete_or_unknown_calendar_coverage")
        snapshot_id = uuid4()
        details = {**validation, "source": source, "request_scope": scope, "comparison": comparison}
        artifact = store_artifact(
            settings,
            "snapshot",
            {"rows": rows, "outcome": result, "quality": details},
            {
                "snapshot_id": str(snapshot_id),
                "source_revision": collection["source_revision"],
            },
        )
        with db.transaction() as conn:
            db.fence(conn, job)
            # Serialize against source disable/revision publication.
            conn.execute("SELECT pg_advisory_xact_lock(77610260)")
            enabled_source(conn, collection["source_id"], collection["source_revision"])
            publish_artifact(conn, artifact)
            conn.execute(
                "INSERT INTO research_snapshots(id,collection_id,source_id,source_revision,symbol,status,retrieved_at,event_start,event_end,payload_hash,row_count,artifact_id,data) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    snapshot_id,
                    collection["id"],
                    collection["source_id"],
                    collection["source_revision"],
                    code,
                    validation["status"],
                    retrieved,
                    rows[0]["time"] if rows else None,
                    rows[-1]["time"] if rows else None,
                    result.get("payload_hash", digest(result)),
                    len(rows),
                    artifact["id"],
                    jsonb(details),
                ),
            )
            conn.execute(
                "INSERT INTO research_quality_checks(id,snapshot_id,metric_version,canonical_watermark,data) VALUES(%s,%s,%s,%s,%s)",
                (uuid4(), snapshot_id, QUALITY_VERSION, watermark, jsonb(comparison)),
            )
            conn.execute(
                "UPDATE jobs SET progress=%s WHERE id=%s",
                (jsonb({"last_symbol": code, "status": validation["status"]}), job["id"]),
            )
    with db.publication(job) as conn:
        counts = conn.execute(
            "SELECT status,count(*) AS count FROM research_snapshots WHERE collection_id=%s GROUP BY status",
            (collection["id"],),
        ).fetchall()
        conn.execute(
            "UPDATE jobs SET progress=%s WHERE id=%s", (jsonb({"research_only": True, "outcomes": counts}), job["id"])
        )
