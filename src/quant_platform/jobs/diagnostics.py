"""Asynchronous diagnostics; publication only enqueues intent on the live path."""

from datetime import date, datetime, timedelta
import time
from uuid import uuid4

from quant_platform.adapters.qlib.data import safe_artifact, verify
from quant_platform.domain import CN, digest, now
from quant_platform.domain.labels import adjusted_return, label_window
from quant_platform.research.diagnostics import METRIC_VERSION, cross_section
from quant_platform.research.process import run_research
from quant_platform.research.factors import research_identity
from quant_platform.storage import jsonb
from quant_platform.storage.research import publish_artifact, store_artifact


def enqueue_diagnostics(db, conn, prediction_id, revision="initial", observation=None):
    payload = {"prediction_id": str(prediction_id)}
    if observation:
        payload["observation"] = observation
    return db.enqueue(
        conn, "qlib_diagnostics", "qlib-diagnostics", f"diagnostics:{prediction_id}:{revision}", payload, priority=-10
    )


def calendar_days(calendar, cutoff):
    expected, days = cutoff.astimezone(CN).date(), []
    for row in calendar:
        if row["n"] != 2 or not row.get("agreed", True) or date.fromisoformat(row["day"]) != expected:
            break
        if row["open"]:
            days.append(row["day"])
        expected += timedelta(days=1)
    return days


def build_observation(conn, prediction, observed):
    watermark = conn.execute("SELECT coalesce(max(id),0) AS id FROM datasets").fetchone()["id"]
    cutoff = prediction["as_of"]
    calendar = conn.execute(
        "SELECT day,bool_and(is_open) AS open,bool_and(is_open)=bool_or(is_open) AS agreed,count(*) AS n "
        "FROM calendars WHERE exchange IN ('SSE','SZSE') AND day BETWEEN %s AND %s GROUP BY day ORDER BY day",
        (cutoff.astimezone(CN).date(), cutoff.astimezone(CN).date() + timedelta(days=30)),
    ).fetchall()
    calendar = [{**row, "day": str(row["day"])} for row in calendar]
    window = label_window(calendar_days(calendar, cutoff), cutoff, prediction["frequency"])
    if window:
        calendar = [row for row in calendar if row["day"] <= str(window[1].date())]
    mature = bool(window and observed >= window[1])
    revisions = {}
    if mature:
        codes = sorted(prediction["data"]["scores"])
        dates = sorted({window[0].date(), window[1].date()})
        for table in ("daily_bars", "factors", "market_constraints"):
            revisions[table] = conn.execute(
                f"SELECT DISTINCT ON(symbol,day) symbol,day,dataset_id FROM {table} "
                "WHERE symbol=ANY(%s) AND day=ANY(%s) AND dataset_id<=%s ORDER BY symbol,day,dataset_id DESC",
                (codes, dates, watermark),
            ).fetchall()
        if prediction["frequency"] == "5min":
            revisions["minute_bars"] = conn.execute(
                "SELECT DISTINCT ON(symbol) symbol,dataset_id FROM minute_bars WHERE symbol=ANY(%s) "
                "AND bar_end=%s AND finalized AND dataset_id<=%s ORDER BY symbol,dataset_id DESC",
                (codes, window[0], watermark),
            ).fetchall()
    identity = {
        "calendar": calendar,
        "mature": mature,
        "revisions": revisions,
        "execution_identity": digest(research_identity()),
        "metric_version": METRIC_VERSION,
    }
    return {
        "source_revision": digest(identity),
        "observed_at": observed.isoformat(),
        "data": {"watermark": watermark, **identity},
    }


def schedule_diagnostics(db, conn):
    checkpoint = conn.execute("SELECT value FROM settings WHERE key='schedule:diagnostics'").fetchone()
    state = checkpoint["value"] if checkpoint else {}
    observed = now()
    if state.get("at") and observed - datetime.fromisoformat(state["at"]) < timedelta(minutes=5):
        return
    pending = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE queue='qlib-diagnostics' AND status IN ('pending','running')"
    ).fetchone()["n"]
    if pending >= 1000:
        return
    rows = conn.execute(
        "SELECT p.*,r.as_of,r.frequency FROM prediction_runs p JOIN pipeline_runs r ON r.id=p.run_id "
        "WHERE p.created_at>=now()-interval '180 days' AND (%s::uuid IS NULL OR p.id>%s::uuid) "
        "AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.queue='qlib-diagnostics' AND j.status IN ('pending','running') "
        "AND j.payload->>'prediction_id'=p.id::text) ORDER BY p.id LIMIT %s",
        (state.get("cursor"), state.get("cursor"), min(25, 1000 - pending)),
    ).fetchall()
    for row in rows:
        observation = build_observation(conn, row, observed)
        revision = observation["source_revision"]
        if not conn.execute(
            "SELECT 1 FROM model_diagnostics WHERE prediction_id=%s AND metric_version=%s AND source_revision=%s",
            (row["id"], METRIC_VERSION, revision),
        ).fetchone():
            enqueue_diagnostics(db, conn, row["id"], revision, observation)
    db.set_setting(
        conn, "schedule:diagnostics", {"cursor": str(rows[-1]["id"]) if rows else None, "at": observed.isoformat()}
    )


def observation_input(db, job, prediction):
    with db.transaction() as conn:
        db.fence(conn, job)
        existing = conn.execute("SELECT * FROM diagnostic_inputs WHERE job_id=%s", (job["id"],)).fetchone()
        if existing:
            return existing
        observation = job["payload"].get("observation") or build_observation(conn, prediction, now())
        conn.execute(
            "INSERT INTO diagnostic_inputs(job_id,prediction_id,source_revision,observed_at,data) VALUES(%s,%s,%s,%s,%s)",
            (
                job["id"],
                prediction["id"],
                observation["source_revision"],
                observation["observed_at"],
                jsonb(observation["data"]),
            ),
        )
        return {**observation, "observed_at": datetime.fromisoformat(observation["observed_at"])}


def realized_quality(db, prediction, observation):
    scores = prediction["data"]["scores"]
    calendar = observation["data"]["calendar"]
    window = label_window(calendar_days(calendar, prediction["as_of"]), prediction["as_of"], prediction["frequency"])
    empty = cross_section(scores, {})
    if not window:
        return {**empty, "reason": "calendar_or_label_window_unavailable"}
    entry, exit_at = window
    if observation["observed_at"] < exit_at:
        return {**empty, "reason": "label_not_mature", "matures_at": exit_at.isoformat()}
    watermark = observation["data"]["watermark"]
    codes = sorted(scores)
    daily = db.rows(
        "SELECT b.symbol,b.day,b.data,f.factor FROM (SELECT DISTINCT ON(symbol,day) symbol,day,data FROM daily_bars "
        "WHERE symbol=ANY(%s) AND day=ANY(%s) AND dataset_id<=%s ORDER BY symbol,day,dataset_id DESC) b "
        "LEFT JOIN LATERAL (SELECT factor FROM factors WHERE symbol=b.symbol AND day=b.day AND dataset_id<=%s ORDER BY dataset_id DESC LIMIT 1) f ON true",
        (codes, sorted({entry.date(), exit_at.date()}), watermark, watermark),
    )
    daily = {(row["symbol"], row["day"]): row for row in daily}
    constraints = db.rows(
        "SELECT DISTINCT ON(symbol,day) symbol,day,data FROM market_constraints WHERE symbol=ANY(%s) AND day=ANY(%s) AND dataset_id<=%s ORDER BY symbol,day,dataset_id DESC",
        (codes, sorted({entry.date(), exit_at.date()}), watermark),
    )
    constraints = {(row["symbol"], row["day"]): row["data"] for row in constraints}
    minutes = {}
    if prediction["frequency"] == "5min":
        minutes = {
            r["symbol"]: r
            for r in db.rows(
                "SELECT DISTINCT ON(symbol) symbol,open,volume FROM minute_bars WHERE symbol=ANY(%s) AND bar_end=%s AND finalized AND dataset_id<=%s ORDER BY symbol,dataset_id DESC",
                (codes, entry, watermark),
            )
        }
    labels, exclusions = {}, {}
    for code in codes:
        start, end = daily.get((code, entry.date())), daily.get((code, exit_at.date()))
        states = [constraints.get((code, day)) for day in {entry.date(), exit_at.date()}]
        if any(not state or state.get("suspended", True) for state in states):
            exclusions[code] = "suspended_or_constraints_unavailable"
            continue
        if not start or not end:
            exclusions[code] = "missing_future_prices_or_adjustments"
            continue
        opening = start["data"] if prediction["frequency"] == "day" else minutes.get(code, {})
        if not opening.get("volume") or not end["data"].get("volume"):
            exclusions[code] = "missing_or_zero_volume"
            continue
        value = adjusted_return(
            opening.get("open"),
            start.get("factor"),
            end["data"].get("open" if prediction["frequency"] == "day" else "close"),
            end.get("factor"),
        )
        if value is None:
            exclusions[code] = "invalid_price_or_adjustment"
        else:
            labels[code] = value
    return {
        **cross_section(scores, labels),
        "entry": entry.isoformat(),
        "exit": exit_at.isoformat(),
        "exclusions": exclusions,
    }


def execute(db, settings, job, stop, runner=run_research):
    started = time.monotonic()
    prediction = db.rows(
        "SELECT p.*,r.as_of,r.frequency FROM prediction_runs p JOIN pipeline_runs r ON r.id=p.run_id WHERE p.id=%s",
        (job["payload"]["prediction_id"],),
    )[0]
    observation = observation_input(db, job, prediction)
    existing = db.rows(
        "SELECT 1 FROM model_diagnostics WHERE model_id=%s AND prediction_id=%s AND metric_version=%s AND source_revision=%s",
        (prediction["model_id"], prediction["id"], METRIC_VERSION, observation["source_revision"]),
    )
    if existing:
        with db.publication(job):
            return
    generation = db.rows("SELECT * FROM qlib_generations WHERE id=%s", (prediction["generation_id"],))[0]
    model = db.rows("SELECT * FROM model_versions WHERE id=%s", (prediction["model_id"],))[0]
    data_path = safe_artifact(settings.artifact_root, generation["path"])
    model_path = safe_artifact(settings.artifact_root, model["path"])
    verify(data_path, generation["manifest"])
    verify(model_path, model["metadata"])
    execution_identity = digest(research_identity())
    previous = db.rows(
        "SELECT d.data,a.path,a.manifest FROM model_diagnostics d JOIN research_artifacts a ON a.id=d.artifact_id "
        "WHERE d.prediction_id=%s AND d.metric_version=%s AND d.data->>'execution_identity'=%s ORDER BY d.observed_at DESC LIMIT 1",
        (prediction["id"], METRIC_VERSION, execution_identity),
    )
    if previous:
        verify(safe_artifact(settings.artifact_root, previous[0]["path"]), previous[0]["manifest"])
        drift = previous[0]["data"]["drift"]
    elif "diagnostic_reference.json" not in model["metadata"].get("files", {}):
        drift = {"status": "unavailable", "reason": "model_has_no_training_baseline"}
    else:
        drift = runner(
            "quant_platform.research.experiments",
            {
                "action": "diagnostics",
                "path": str(data_path),
                "manifest": generation["manifest"],
                "model_path": str(model_path),
                "model_manifest": model["metadata"],
                "scores": prediction["data"]["scores"],
            },
            settings.data_deadline_seconds,
            stop,
        )
    quality = realized_quality(db, prediction, observation)
    data = {
        "drift": drift,
        "realized": quality,
        "source_watermark": observation["data"]["watermark"],
        "execution_identity": execution_identity,
        "drift_reused": bool(previous),
        "diagnostic_only": True,
        "frequency": prediction["frequency"],
        "cutoff": str(prediction["as_of"]),
        "latency_seconds": time.monotonic() - started,
    }
    artifact = store_artifact(
        settings,
        "diagnostics",
        data,
        {"model_id": str(model["id"]), "prediction_id": str(prediction["id"]), "metric_version": METRIC_VERSION},
    )
    with db.publication(job) as conn:
        publish_artifact(conn, artifact)
        conn.execute(
            "INSERT INTO model_diagnostics(id,model_id,prediction_id,metric_version,source_revision,observed_at,session,artifact_id,data) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                uuid4(),
                model["id"],
                prediction["id"],
                METRIC_VERSION,
                observation["source_revision"],
                observation["observed_at"],
                prediction["as_of"].astimezone(CN).date(),
                artifact["id"],
                jsonb(data),
            ),
        )
