"""Immutable Qlib datasets with explicit raw/adjusted units and provenance."""

import hashlib
import json
import math
import os
from bisect import bisect_left, bisect_right
from datetime import date, datetime, time
from pathlib import Path
from uuid import uuid4

import numpy as np

from quant_platform.domain import CN
from quant_platform.domain.labels import adjusted_return, label_window
from quant_platform.domain.workflow import DATA_CONTRACT, PipelineBlocked
from quant_platform.storage import jsonb

FIELDS = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "vwap",
    "amount",
    "factor",
    "raw_close",
    "raw_volume",
    "risk",
    "tradable",
    "limit_up",
    "limit_down",
    "listed_sessions",
    "daily_close",
    "slot",
    "label",
)


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_artifact(root, relative):
    base = Path(root).resolve()
    path = (base / relative).resolve(strict=True)
    if not path.is_relative_to(base) or path == base:
        raise PipelineBlocked("Artifact path is outside the owned root.")
    return path


def seal(folder, metadata):
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            files[str(path.relative_to(folder))] = checksum(path)
    manifest = {**metadata, "files": files}
    with (folder / "manifest.json").open("w") as stream:
        json.dump(manifest, stream, default=str, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest


def verify(folder, manifest):
    for name, expected in manifest["files"].items():
        path = safe_artifact(folder, name)
        if not path.is_file() or checksum(path) != expected:
            raise PipelineBlocked("Artifact checksum mismatch.")


def normalized(row, anchor):
    raw = row["data"]
    factor = float(row["factor"]) / anchor
    if not math.isfinite(factor) or factor <= 0:
        raise PipelineBlocked("Invalid Qlib adjustment multiplier.")
    volume = raw.get("volume")
    amount = raw.get("turnover", raw.get("amount"))
    out = {k: float(raw[k]) * factor for k in ("open", "high", "low", "close")}
    out.update(
        factor=factor,
        raw_close=float(raw["close"]),
        raw_volume=volume,
        volume=volume / factor if volume is not None else np.nan,
        amount=amount,
        vwap=amount / volume * factor if volume and amount is not None else np.nan,
    )
    return out


def write_bins(folder, histories, calendar, frequency, anchors, metadata):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "calendars").mkdir()
    (folder / "instruments").mkdir()
    stamps = [str(t) for t in calendar]
    (folder / "calendars" / f"{frequency}.txt").write_text("\n".join(stamps) + "\n")
    positions = {stamp: i for i, stamp in enumerate(stamps)}
    memberships, coverage = [], {}
    for code, rows in histories.items():
        if not rows:
            continue
        usable = [r for r in rows if str(r["time"]) in positions and r.get("factor")]
        if not usable:
            continue
        usable.sort(key=lambda r: str(r["time"]))
        first, last = positions[str(usable[0]["time"])], positions[str(usable[-1]["time"])]
        arrays = {field: np.full(last - first + 2, np.nan, dtype="<f4") for field in FIELDS}
        for array in arrays.values():
            array[0] = first
        for row in usable:
            values = normalized(row, anchors[code])
            values.update(row.get("features", {}))
            index = positions[str(row["time"])] - first + 1
            for field in FIELDS:
                value = values.get(field)
                arrays[field][index] = value if value is not None else np.nan
        target = folder / "features" / code.lower()
        target.mkdir(parents=True)
        for field, values in arrays.items():
            values.tofile(target / f"{field}.{frequency}.bin")
        memberships.append(f"{code}\t{usable[0]['time']}\t{usable[-1]['time']}")
        coverage[code] = str(usable[-1]["time"])
    if not memberships:
        raise PipelineBlocked("No valid Qlib rows; previous generations retained.")
    (folder / "instruments" / "all.txt").write_text("\n".join(memberships) + "\n")
    return seal(
        folder,
        {**metadata, "contract": DATA_CONTRACT, "frequency": frequency, "coverage": coverage, "anchors": anchors},
    )


def build_generation(db, settings, run, job):
    snapshot, cutoff, freq = run["snapshot"], run["as_of"], run["frequency"]
    if snapshot.get("research_generation_id"):
        generation = db.rows("SELECT * FROM qlib_generations WHERE id=%s", (snapshot["research_generation_id"],))[0]
        validate_path = safe_artifact(settings.artifact_root, generation["path"])
        verify(validate_path, generation["manifest"])
        if generation["frequency"] != freq or generation["manifest"]["policy"] != snapshot["policy"]:
            raise PipelineBlocked("Frozen research generation no longer matches the requested contract.")
        with db.publication(job) as conn:
            conn.execute(
                "UPDATE pipeline_steps SET result=%s WHERE job_id=%s",
                (jsonb({"generation_id": str(generation["id"])}), job["id"]),
            )
        return generation["id"]
    target, watermark = cutoff.astimezone(CN).date(), snapshot["watermark"]
    count = (
        snapshot["history_sessions"]
        if freq == "day"
        else snapshot["minute_history_sessions"] if run["purpose"] == "training" else 21
    )
    sse = {date.fromisoformat(r["day"]) for r in snapshot["calendars"] if r["exchange"] == "SSE" and r["is_open"]}
    szse = {date.fromisoformat(r["day"]) for r in snapshot["calendars"] if r["exchange"] == "SZSE" and r["is_open"]}
    all_days = sorted(sse & szse)
    days = all_days[-count:]
    if not days or days[-1] != target:
        raise PipelineBlocked("Input cutoff is not a verified completed trading session.")
    codes = list(snapshot["instruments"])
    dated_pools = {}
    if freq == "5min":
        dated_pools = {p["day"]: p["data"] for p in snapshot["pools"] if str(days[0]) <= p["day"] <= str(target)}
        if run["purpose"] == "training":
            if len(dated_pools) < count or any(
                not pool.get("out_of_sample")
                or not pool.get("ready")
                or "daily_weights" not in pool
                or set(pool["daily_weights"]) - set(pool["symbols"])
                for pool in dated_pools.values()
            ):
                raise PipelineBlocked(
                    "Dated out-of-sample intraday pools are incomplete; today's shortlist cannot backfill history."
                )
            codes = sorted({s for p in dated_pools.values() for s in p["symbols"]})
        else:
            pool = dated_pools.get(str(target))
            if not pool:
                raise PipelineBlocked("No frozen intraday pool.")
            codes = pool["symbols"]
            if not snapshot.get("daily_baseline"):
                raise PipelineBlocked("Intraday inference requires a published Qlib daily baseline.")
    histories, anchors = {}, {}
    calendar = (
        days
        if freq == "day"
        else [
            datetime.combine(day, time(h, m), CN).replace(tzinfo=None)
            for day in days
            for h, m in [(n // 60, n % 60) for n in list(range(575, 691, 5)) + list(range(785, 901, 5))]
            if datetime.combine(day, time(h, m), CN) <= cutoff
        ]
    )
    if freq == "5min" and run["purpose"] != "training":
        # Twenty rolling sessions include 960 finalized bars, even mid-session.
        calendar = calendar[-20 * 48 :]
    calendar_stamps = {str(stamp) for stamp in calendar}
    ready, omitted = [], {}
    training_ready = {str(day): set() for day in days[20:]} if freq == "5min" and run["purpose"] == "training" else {}
    positions = {str(stamp): index for index, stamp in enumerate(calendar)}
    for code in codes + (["SH000300"] if freq == "day" else []):
        daily = db.history(code, target, snapshot["history_sessions"], watermark)
        daily = [{**r, "factor": 1.0 if code == "SH000300" else r["factor"]} for r in daily]
        if not daily or not daily[0].get("factor"):
            omitted[code] = "missing_daily_history_or_adjustment"
            continue
        with db.transaction() as conn:
            db.fence(conn, job)
            conn.execute(
                "INSERT INTO normalization_anchors VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (code, float(daily[0]["data"]["close"]) * daily[0]["factor"], daily[0]["day"], daily[0]["dataset_id"]),
            )
            anchors[code] = conn.execute(
                "SELECT anchor FROM normalization_anchors WHERE symbol=%s", (code,)
            ).fetchone()["anchor"]
        states = db.rows(
            "SELECT * FROM security_states WHERE symbol=%s AND dataset_id<=%s ORDER BY effective_day,dataset_id",
            (code, watermark),
        )
        constraints = {
            r["day"]: r["data"]
            for r in db.rows(
                "SELECT DISTINCT ON(day) day,data FROM market_constraints WHERE symbol=%s AND day BETWEEN %s AND %s "
                "AND dataset_id<=%s ORDER BY day,dataset_id DESC",
                (code, days[0], target, watermark),
            )
        }
        daily_map = {r["day"]: r for r in daily}
        if freq == "day":
            rows = [{**r, "time": r["day"]} for r in daily if r["day"] >= days[0]]
        else:
            minutes = db.rows(
                "SELECT DISTINCT ON(bar_end) * FROM minute_bars WHERE symbol=%s AND bar_end>=%s AND bar_end<=%s "
                "AND dataset_id<=%s AND finalized ORDER BY bar_end,dataset_id DESC",
                (code, datetime.combine(days[0], time(0), CN), cutoff, watermark),
            )
            rows = [
                {
                    "day": r["bar_end"].astimezone(CN).date(),
                    "time": r["bar_end"].astimezone(CN).replace(tzinfo=None),
                    "data": {**r, "turnover": r["amount"]},
                    "factor": (daily_map.get(r["bar_end"].astimezone(CN).date()) or {}).get("factor"),
                    "available_at": r["available_at"],
                }
                for r in minutes
            ]
            if target not in daily_map:
                factors = db.rows(
                    "SELECT factor FROM factors WHERE symbol=%s AND day=%s AND dataset_id<=%s ORDER BY dataset_id DESC LIMIT 1",
                    (code, target, watermark),
                )
                for row in rows:
                    if row["day"] == target and factors:
                        row["factor"] = factors[0]["factor"]
        rows = [row for row in rows if str(row["time"]) in calendar_stamps]
        for row in rows:
            day = row["day"]
            candidates = [
                s
                for s in states
                if s["effective_day"] <= day
                and (not s["data"].get("end") or s["data"]["end"] >= str(day))
                and s["available_at"].astimezone(CN).date() <= day
            ]
            state = candidates[-1]["data"] if candidates else {}
            limit = constraints.get(day, {})
            previous = [d for d in daily_map if d < day]
            baseline = daily_map[max(previous)] if previous else None
            risk = float(state["risk_warning"]) if state.get("announcement_verified") else np.nan
            listing = snapshot["instruments"].get(code, {}).get("list_date")
            listed = (
                bisect_right(all_days, day) - bisect_left(all_days, date.fromisoformat(listing))
                if listing
                else len(all_days)
            )
            row["features"] = {
                "risk": 0 if code == "SH000300" else risk,
                "tradable": (
                    float(not limit.get("suspended") and (row["data"].get("volume") or 0) > 0) if limit else np.nan
                ),
                "limit_up": limit.get("limit_up"),
                "limit_down": limit.get("limit_down"),
                "listed_sessions": listed,
                "daily_close": (
                    baseline["data"]["close"] * baseline["factor"] / anchors[code]
                    if baseline and baseline.get("factor")
                    else np.nan
                ),
                "slot": (row["time"].hour * 60 + row["time"].minute) / 1440 if freq == "5min" else 0,
            }
        if freq == "5min" and run["purpose"] == "training":
            for index, row in enumerate(rows):
                window = label_window(days, row["time"], "5min")
                # One complete interval of processing lag precedes the reference entry.
                entry = rows[index + 2] if index + 2 < len(rows) else None
                if entry and (not window or entry["time"] != window[0].replace(tzinfo=None)):
                    entry = None
                exit_row = daily_map.get(window[1].date()) if window else None
                pool = dated_pools.get(str(row["day"]), {})
                if (
                    entry
                    and entry["day"] == row["day"]
                    and entry.get("factor")
                    and exit_row
                    and exit_row.get("factor")
                    and code in pool.get("symbols", [])
                ):
                    row["features"]["label"] = adjusted_return(
                        entry["data"]["open"], entry["factor"], exit_row["data"]["close"], exit_row["factor"]
                    )
        histories[code] = rows
        if training_ready:
            streak, previous_index = 0, -2
            valid_bars = {}
            for row in rows:
                index = positions[str(row["time"])]
                good = row.get("factor") and math.isfinite(row["factor"]) and row["factor"] > 0
                good = good and all(
                    row["data"].get(field) is not None and math.isfinite(row["data"][field])
                    for field in ("open", "high", "low", "close", "volume", "turnover")
                )
                streak = (streak + 1 if index == previous_index + 1 else 1) if good else 0
                previous_index = index
                day = str(row["day"])
                if (
                    day in training_ready
                    and streak >= 960
                    and all(
                        row["features"].get(field) is not None and math.isfinite(row["features"][field])
                        for field in ("risk", "tradable", "limit_up", "limit_down", "daily_close")
                    )
                ):
                    valid_bars[day] = valid_bars.get(day, 0) + 1
            for day, bars in valid_bars.items():
                if bars == 48:
                    training_ready[day].add(code)
            # Learning must not turn an incomplete feature window into an imputed sample.
            for row in rows:
                if code not in training_ready.get(str(row["day"]), set()):
                    row["features"].pop("label", None)
            continue
        expected = str(target) if freq == "day" else str(cutoff.astimezone(CN).replace(tzinfo=None))
        latest = rows[-1] if rows else None
        minimum_bars = 60 if freq == "day" else 20 * 48
        recent = rows[-minimum_bars:]
        warmed = len(recent) == minimum_bars and all(
            r.get("factor") and math.isfinite(r["factor"]) and r["factor"] > 0 for r in recent
        )
        if warmed:
            warmed = [str(r["time"]) for r in recent] == [str(t) for t in calendar[-minimum_bars:]]
        if (
            warmed
            and latest
            and str(latest["time"]) == expected
            and latest.get("factor")
            and math.isfinite(latest["features"]["risk"])
        ):
            ready.append(code)
        else:
            omitted[code] = (
                "incomplete_feature_window"
                if not warmed
                else "missing_current_bar_adjustment_or_point_in_time_security_state"
            )
    eligible = {
        code
        for code in codes
        if not snapshot["instruments"].get(code, {}).get("delist_date")
        or snapshot["instruments"][code]["delist_date"] >= str(target)
    }
    coverage = len(set(ready) & eligible) / len(eligible) if eligible else 0
    historical_coverage, historical_omissions = {}, {}
    if training_ready:
        for day, available in training_ready.items():
            members = set(dated_pools[day]["symbols"])
            historical_coverage[day] = len(members & available) / len(members) if members else 1
            historical_omissions[day] = sorted(members - available)
            if set(dated_pools[day]["daily_weights"]) - available:
                raise PipelineBlocked("Historical daily targets lack minute feature history at " + day)
        coverage = min(historical_coverage.values())
        ready = sorted(set().union(*training_ready.values()))
    if coverage < snapshot["policy"]["minimum_coverage"]:
        raise PipelineBlocked(
            f"Qlib input coverage {coverage:.1%} is below policy; inspect history and security-state coverage."
        )
    if run["purpose"] != "training":
        protected = {
            code
            for portfolio in snapshot.get("portfolios", [])
            for code, weight in portfolio["data"]["weights"].items()
            if weight > 0
        }
        baselines = list(snapshot.get("portfolio_daily_baselines", {}).values())
        if snapshot.get("daily_baseline"):
            baselines.append(snapshot["daily_baseline"])
        protected.update(
            code for baseline in baselines for code, weight in baseline["data"]["weights"].items() if weight > 0
        )
        if protected - set(ready):
            raise PipelineBlocked("Portfolio feature history unavailable: " + ", ".join(sorted(protected - set(ready))))
    generation_id = uuid4()
    root = settings.artifact_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    relative = f"qlib/generation-{generation_id.hex}"
    folder = root / relative
    folder.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "id": str(generation_id),
        "watermark": watermark,
        "as_of": str(cutoff),
        "source": "tushare",
        "ready": sorted(ready),
        "omitted": omitted,
        "market_coverage": coverage,
        "days": list(map(str, days)),
        "point_in_time_pools": dated_pools,
        "engine": snapshot["engine"],
        "policy": snapshot["policy"],
        "instruments": snapshot["instruments"],
        "historical_coverage": historical_coverage,
        "historical_omissions": historical_omissions,
    }
    if run["purpose"] != "training":
        histories = {code: rows for code, rows in histories.items() if code in ready}
    manifest = write_bins(folder, histories, calendar, freq, anchors, metadata)
    from quant_platform.adapters.qlib.runtime import validate_generation

    validate_generation(folder, manifest)
    with db.publication(job) as conn:
        conn.execute(
            "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (generation_id, freq, watermark, cutoff, DATA_CONTRACT, relative, jsonb(manifest)),
        )
        conn.execute(
            "UPDATE pipeline_steps SET result=%s WHERE job_id=%s",
            (jsonb({"generation_id": str(generation_id)}), job["id"]),
        )
    return generation_id
