"""Immutable research records and permanent request binding, separate from datasets."""

from datetime import datetime, time, timedelta
import json
from uuid import uuid4

from quant_platform.adapters.qlib.data import seal
from quant_platform.domain import CN, digest, now
from quant_platform.domain.research import CollectionInput, SOURCE_CATALOG, SourceChange, ResearchMutation
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict


class RequestReplay(Exception):
    def __init__(self, response):
        self.response = response


def prior_request(conn, operation, value):
    conn.execute("SELECT pg_advisory_xact_lock(77610260)")
    prior = conn.execute("SELECT * FROM research_requests WHERE request_key=%s", (value.request_key,)).fetchone()
    if prior:
        if prior["operation"] != operation or prior["request_hash"] != digest(value.model_dump(mode="json")):
            raise Conflict("Research request key is permanently bound to a different request.")
        raise RequestReplay(prior["response"])


def record_request(conn, operation, value, response):
    conn.execute(
        "INSERT INTO research_requests(request_key,operation,request_hash,response) VALUES(%s,%s,%s,%s)",
        (value.request_key, operation, digest(value.model_dump(mode="json")), jsonb(response)),
    )
    conn.execute(
        "INSERT INTO audit(action,target,data) VALUES(%s,%s,%s)",
        (operation, value.request_key, jsonb(response)),
    )
    return response


def control_job(db, job_id, value, action):
    value = ResearchMutation.model_validate(value)
    if action not in {"cancel", "retry"}:
        raise Conflict("Unknown research job action.")
    operation = f"research_job:{job_id}:{action}"
    try:
        with db.transaction() as conn:
            job = conn.execute("SELECT * FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
            prior_request(conn, operation, value)
            if not job or job["queue"] not in {"research-data", "qlib-research", "qlib-diagnostics"}:
                raise Conflict("Only isolated research jobs can use this control.")
            if job["fence"] != value.expected_revision:
                raise Conflict("Research job control revision changed; refresh before retrying.")
            allowed = {"pending", "running"} if action == "cancel" else {"blocked", "failed"}
            if job["status"] not in allowed:
                raise Conflict("Research job state does not permit this action.")
            status = "blocked" if action == "cancel" else "pending"
            error = "Cancelled by operator; published evidence remains immutable." if action == "cancel" else None
            conn.execute(
                "UPDATE jobs SET status=%s,error=%s,fence=fence+1,owner=NULL,lease_until=NULL,"
                "failures=0,available_at=now(),updated_at=now() WHERE id=%s",
                (status, error, job_id),
            )
            return record_request(
                conn,
                operation,
                value,
                {
                    "job_id": job_id,
                    "status": status,
                    "control_revision": job["fence"] + 1,
                    "cancelled": action == "cancel",
                },
            )
    except RequestReplay as replay:
        return replay.response


def sources(db):
    heads = {
        row["source_id"]: row
        for row in db.rows("SELECT DISTINCT ON(source_id) * FROM research_sources ORDER BY source_id,revision DESC")
    }
    result = []
    for key, catalog in SOURCE_CATALOG.items():
        head = heads.get(key, {"revision": 0, "enabled": False, "terms_acknowledged": False})
        latest = db.rows(
            "SELECT status,retrieved_at,data FROM research_snapshots WHERE source_id=%s ORDER BY retrieved_at DESC,id LIMIT 1",
            (key,),
        )
        result.append({**head, **catalog, "observed_health": latest[0] if latest else {"status": "unknown"}})
    return result


def change_source(db, source_id, value):
    value = SourceChange.model_validate(value)
    if source_id not in SOURCE_CATALOG:
        raise Conflict("Unknown research source.")
    operation = "research_source:" + source_id
    try:
        with db.transaction() as conn:
            prior_request(conn, operation, value)
            head = conn.execute(
                "SELECT * FROM research_sources WHERE source_id=%s ORDER BY revision DESC LIMIT 1", (source_id,)
            ).fetchone()
            revision = head["revision"] if head else 0
            if value.expected_revision != revision:
                raise Conflict("Research source revision changed.")
            conn.execute(
                "INSERT INTO research_sources(source_id,revision,enabled,terms_acknowledged,data) VALUES(%s,%s,%s,%s,%s)",
                (source_id, revision + 1, value.enabled, value.terms_acknowledged, jsonb(SOURCE_CATALOG[source_id])),
            )
            return record_request(
                conn, operation, value, {"source_id": source_id, "revision": revision + 1, "enabled": value.enabled}
            )
    except RequestReplay as replay:
        return replay.response


def enabled_source(conn, source_id, revision):
    source = conn.execute(
        "SELECT * FROM research_sources WHERE source_id=%s ORDER BY revision DESC LIMIT 1", (source_id,)
    ).fetchone()
    if not source or not source["enabled"] or not source["terms_acknowledged"] or source["revision"] != revision:
        raise PipelineBlocked("Research source is disabled or its acknowledged revision changed.")
    return source


def request_collection(db, settings, value):
    value = CollectionInput.model_validate(value)
    try:
        with db.transaction() as conn:
            prior_request(conn, "research_collection", value)
            if not settings.research_data_enabled:
                raise PipelineBlocked("Supplemental collection is disabled in this deployment.")
            enabled_source(conn, value.source_id, value.expected_revision)
            today = now().astimezone(CN).date()
            if value.end > today or (value.source_id.endswith("intraday") and value.end != today):
                raise Conflict("Research collection cannot request future data or historical intraday coverage.")
            identifier = uuid4()
            job_id = db.enqueue(
                conn,
                "research_collect",
                "research-data",
                "research:" + str(identifier),
                {"collection_id": str(identifier)},
            )
            conn.execute(
                "INSERT INTO research_collections(id,source_id,source_revision,job_id,scope) VALUES(%s,%s,%s,%s,%s)",
                (identifier, value.source_id, value.expected_revision, job_id, jsonb(value.model_dump(mode="json"))),
            )
            return record_request(
                conn, "research_collection", value, {"id": str(identifier), "job_id": job_id, "research_only": True}
            )
    except RequestReplay as replay:
        return replay.response


def store_artifact(settings, kind, payload, metadata):
    identifier = uuid4()
    relative = f"research/{identifier.hex}"
    folder = settings.artifact_root.resolve() / relative
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "data.json").write_text(json.dumps(payload, allow_nan=False, default=str))
    manifest = seal(folder, {"format": "quap-research-v1", "kind": kind, "research_only": True, **metadata})
    return {"id": identifier, "kind": kind, "path": relative, "manifest": manifest}


def publish_artifact(conn, artifact):
    conn.execute(
        "INSERT INTO research_artifacts(id,kind,path,manifest) VALUES(%s,%s,%s,%s)",
        (artifact["id"], artifact["kind"], artifact["path"], jsonb(artifact["manifest"])),
    )


def canonical_comparison(db, scope, code, rows, retrieved):
    from quant_platform.research.supplemental import compare_raw

    watermark = db.rows("SELECT coalesce(max(id),0) AS id FROM datasets")[0]["id"]
    start, end = scope["start"], scope["end"]
    calendars = db.rows(
        "SELECT day,bool_and(is_open) AS open,bool_and(is_open)=bool_or(is_open) AS agreed,count(*) AS n FROM calendars WHERE day BETWEEN %s AND %s "
        "AND exchange IN ('SSE','SZSE') GROUP BY day ORDER BY day",
        (start, end),
    )
    complete_calendar = len(calendars) == (
        datetime.fromisoformat(end) - datetime.fromisoformat(start)
    ).days + 1 and all(r["n"] == 2 and r["agreed"] for r in calendars)
    expected = None
    cutoff = retrieved.astimezone(CN).replace(second=0, microsecond=0) - timedelta(minutes=1)
    if scope["source_id"] == "adata-eastmoney-day":
        canonical = [
            {
                "symbol": code,
                "time": datetime.combine(r["day"], time(15), CN).isoformat(),
                **r["data"],
                "amount": r["data"].get("turnover", r["data"].get("amount")),
            }
            for r in db.history(code, end, 4000, watermark)
            if str(r["day"]) >= start and datetime.combine(r["day"], time(15), CN) <= cutoff
        ]
        if complete_calendar:
            expected = [
                datetime.combine(r["day"], time(15), CN).isoformat()
                for r in calendars
                if r["open"] and datetime.combine(r["day"], time(15), CN) <= cutoff
            ]
        return watermark, compare_raw(rows, canonical, expected)
    # Compare complete five-minute aggregates, never a one-minute row to a five-minute bar.
    if complete_calendar:
        expected = [
            datetime.combine(r["day"], time(n // 60, n % 60), CN).isoformat()
            for r in calendars
            if r["open"]
            for n in [*range(571, 691), *range(781, 901)]
            if datetime.combine(r["day"], time(n // 60, n % 60), CN) <= cutoff
        ]
    indexed = {row["time"]: row for row in rows}
    aggregates = []
    for row in rows:
        stamp = datetime.fromisoformat(row["time"])
        if stamp.minute % 5 or stamp.time() in {time(9, 30), time(13)}:
            continue
        group = [indexed.get((stamp - timedelta(minutes=n)).isoformat()) for n in range(4, 0, -1)] + [row]
        if all(group):
            aggregates.append(
                {
                    "symbol": code,
                    "time": stamp.isoformat(),
                    "open": group[0]["open"],
                    "close": row["close"],
                    "high": max(r["high"] for r in group),
                    "low": min(r["low"] for r in group),
                    "volume": sum(r["volume"] for r in group),
                    "amount": sum(r["amount"] for r in group),
                }
            )
    canonical = [
        {**r, "time": r["bar_end"].astimezone(CN).isoformat()}
        for r in db.rows(
            "SELECT DISTINCT ON(bar_end) * FROM minute_bars WHERE symbol=%s AND bar_end>=%s AND bar_end<=%s AND finalized AND dataset_id<=%s ORDER BY bar_end,dataset_id DESC",
            (code, datetime.fromisoformat(start).replace(tzinfo=CN), cutoff, watermark),
        )
    ]
    check = compare_raw(aggregates, canonical)
    missing = sorted(set(expected or []) - indexed.keys())
    check.update(
        comparison_frequency="5min",
        completeness="unknown" if expected is None else "incomplete" if missing else "complete",
        missing_rows=missing,
    )
    return watermark, check
