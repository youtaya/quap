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


def declared_scope(snapshot):
    """The securities this run intended to collect.

    Coverage has to be measured against the declared collection scope, not against the listed master.
    Under the default ``history_scope=index`` the platform only collects CSI 300 members plus the
    observation baskets, so dividing by every listed A-share reports a permanent ~5% and blocks
    training while the collection is in fact complete. Runs frozen before the scope was recorded fall
    back to the instruments they were frozen with, which is the same set for a full-market scope.
    """
    return {str(code) for code in (snapshot.get("scope") or snapshot["instruments"])}


def protected_codes(snapshot):
    """Securities a non-training run may not drop: frozen portfolios, watchlist and daily baselines.

    These sit outside the index scope by construction — an operator can hold anything — so they are
    added to the collection set rather than being silently absent from the generation.
    """
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
    return protected


def coverage_detail(snapshot, scope, eligible, ready, omitted, risk_uncovered, window_sessions, sessions):
    """Per-reason accounting of what entered the generation and what did not.

    A shortfall that is only reported as one percentage cannot be acted on, and a shortfall that is
    not reported at all is a silent sample cut. Every excluded security is named by reason here, the
    point-in-time risk state gap is stated as a limitation of the tokenless sources rather than as
    "no risk", and the depth of the adjusted window is reported per security because the front-adjusted
    series the reachable sources publish is shallower than the analysis window (see ``factor_sessions``).
    """
    reasons = {}
    for reason in omitted.values():
        reasons[reason] = reasons.get(reason, 0) + 1
    universe = len(snapshot["instruments"])
    covered = sorted(set(eligible) - set(risk_uncovered))
    return {
        "scope_mode": snapshot.get("scope_mode", "unknown"),
        "declared_scope": len(scope),
        "eligible": len(eligible),
        "ready": len(set(ready) & set(eligible)),
        "omitted_by_reason": dict(sorted(reasons.items())),
        "universe": universe,
        # 诊断用：整张上市主表里的占比。采集范围默认只是沪深 300，这个数天然很低，绝不能当门槛。
        "universe_coverage": len(set(ready)) / universe if universe else 0,
        "risk_state": {
            "dated": False,
            "source": "observed_security_name",
            "covered_securities": len(covered),
            "uncovered_securities": len(set(eligible) - set(covered)),
            "note": (
                "公开源不发布带日期的更名/风险公告，观测日之前的历史 K 线没有点位风险状态。这些"
                "样本的风险特征是 NaN（未知），不是「无风险」；训练接受未知，推断不接受。"
            ),
        },
        "adjusted_window": {
            "window_sessions": sessions,
            "securities": len(window_sessions),
            "minimum": min(window_sessions.values()) if window_sessions else 0,
            "median": sorted(window_sessions.values())[len(window_sessions) // 2] if window_sessions else 0,
            "maximum": max(window_sessions.values()) if window_sessions else 0,
            "shorter_than_window": sum(1 for value in window_sessions.values() if value < sessions),
            "note": (
                "腾讯前复权约 800 会话封顶，所以各股可用深度不同，深度与上市时间、流动性相关。"
                "按共同可用窗口取交集会砍掉大部分样本，因此这里只如实记录，不静默截齐。"
            ),
        },
    }


def checksum(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_artifact(root, relative):
    """Resolve an owned artifact, reporting a missing one as a blocked prerequisite.

    A run that pins a generation which is no longer on disk used to die with a bare
    ``FileNotFoundError`` from ``resolve(strict=True)`` — a crash, not a blocked prerequisite, so the
    operator got an engine traceback instead of the one fact that matters: the pinned input is gone
    and the run has to be re-created. Disk retention must not prune a pinned generation (see
    ``GenerationStore.publish``); this is the backstop for the ones that were pruned before that.
    """
    base = Path(root).resolve()
    try:
        path = (base / relative).resolve(strict=True)
    except FileNotFoundError:
        raise PipelineBlocked(
            f"The pinned generation artifact is missing from the owned root ({relative}); the run cannot "
            "be replayed and has to be re-created."
        ) from None
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
    scope = declared_scope(snapshot)
    protected = protected_codes(snapshot) if run["purpose"] != "training" else set()
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
    else:
        # 覆盖判定对「声明要采集的范围」做，不对整张上市主表做，见 ``declared_scope``。推断还要把被
        # 冻结的账簿与观察名单纳入，否则它们会因为不在指数范围里而缺席代次。
        codes = sorted(scope | protected)
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
    risk_uncovered, window_sessions = set(), {}
    training_ready = {str(day): set() for day in days[20:]} if freq == "5min" and run["purpose"] == "training" else {}
    positions = {str(stamp): index for index, stamp in enumerate(calendar)}
    for code in codes + (["SH000300"] if freq == "day" else []):
        daily = db.history(code, target, snapshot["history_sessions"], watermark)
        daily = [{**r, "factor": 1.0 if code == "SH000300" else r["factor"]} for r in daily]
        # 复权因子只覆盖源发布的最深窗口（腾讯前复权约 800 会话，见 ``factor_sessions``），窗口更早
        # 的那一段没有因子。要求窗口最老一行必须有因子会把「因子从某天起才可用」误判成「这只证券不
        # 可用」，把整批证券挡在代次之外；只要求存在已复权行，锚点钉在最老的那一行已复权行上，
        # 与 ``write_bins`` 取 ``usable[0]`` 的口径一致。未复权价绝不进代次。
        adjusted = [row for row in daily if row.get("factor")]
        if not daily or not adjusted:
            omitted[code] = "missing_daily_history_or_adjustment"
            continue
        with db.transaction() as conn:
            db.fence(conn, job)
            conn.execute(
                "INSERT INTO normalization_anchors VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (
                    code,
                    float(adjusted[0]["data"]["close"]) * adjusted[0]["factor"],
                    adjusted[0]["day"],
                    adjusted[0]["dataset_id"],
                ),
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
        window_sessions[code] = len(rows)
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
                # 没有点位状态覆盖这根 K 线时是 NaN（未知），不是 0（无风险）。``Market.security_history``
                # 只从观测当天起发布状态，所以观测日之前的每一根历史 K 线都是未知。
                "risk": 0 if code == "SH000300" else risk,
                # 停牌没有公开来源：``constraints`` 里的 ``suspended`` 是「没有任何已声明的停牌信息」，
                # 不是「已确认正常交易」。真正决定可交易性的是这根 K 线自己的成交量 —— 停牌日没有
                # K 线，因此也不会被算成可交易。
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
                        # ``risk`` 不在这里：观测日之前没有点位风险状态，历史池也一样。把它列进来会让
                        # 分钟训练永远凑不齐一个会话。未知的风险是 NaN，由模型自己处理，并记进
                        # ``coverage_detail.risk_state``。
                        for field in ("tradable", "limit_up", "limit_down", "daily_close")
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
        risk_known = latest is not None and math.isfinite(latest["features"]["risk"])
        # 训练接受未知的点位风险状态：公开源没有带日期的更名史，观测日之前的历史 K 线永远拿不到
        # 状态，要求它等于要求整段历史都不许训练。推断不接受 —— 决策日当天的状态是观测得到的，
        # 缺了就说明状态采集没跑到，那才是真的该挡住。缺口一律记进 ``coverage_detail.risk_state``。
        if (
            warmed
            and latest
            and str(latest["time"]) == expected
            and latest.get("factor")
            and (risk_known or run["purpose"] == "training")
        ):
            ready.append(code)
        else:
            omitted[code] = (
                "incomplete_feature_window"
                if not warmed
                else "missing_current_bar_adjustment_or_point_in_time_security_state"
            )
    for code, records in histories.items():
        # 观测日之前的历史 K 线没有点位风险状态。这是免令牌公开源的固有代价，不是「无风险」，
        # 也不能因此把整只证券排除出训练 —— 参考 QuantMind 对缺列的处置：显式告警，绝不静默跳过。
        if code != "SH000300" and records and not math.isfinite(records[-1]["features"].get("risk", np.nan)):
            risk_uncovered.add(code)
    eligible = {
        code
        for code in codes
        # 基准指数不是可交易证券，把它放进分母会虚高覆盖率，也不该出现在样本面里。
        if code != "SH000300"
        and (
            not snapshot["instruments"].get(code, {}).get("delist_date")
            or snapshot["instruments"][code]["delist_date"] >= str(target)
        )
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
        # 只说「覆盖率不足」会把操作者送去查采集，而真正的原因常常是某一条**特定的**排除理由占满了
        # 全部证券。把理由分布直接写进错误里，缺口才可诊断。
        reasons = {}
        for reason in omitted.values():
            reasons[reason] = reasons.get(reason, 0) + 1
        detail = ", ".join(f"{name}={count}" for name, count in sorted(reasons.items(), key=lambda i: -i[1])[:4])
        raise PipelineBlocked(
            f"Qlib input coverage {coverage:.1%} of {len(eligible)} scoped securities is below policy "
            f"{snapshot['policy']['minimum_coverage']:.0%}; omissions: {detail or 'none'}. "
            "`missing_current_bar_adjustment_or_point_in_time_security_state` means no recorded risk state "
            "reaches the decision date: public sources publish no dated rename history, so a state is only "
            "usable from the day it was observed. `missing_daily_history_or_adjustment` means the security "
            "has no adjusted bar in the window."
        )
    if run["purpose"] != "training":
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
        "source": "market",
        "ready": sorted(ready),
        "omitted": omitted,
        "market_coverage": coverage,
        # 覆盖率的分母、逐项排除理由、点位风险状态与复权窗口深度。参考 QuantMind 的缺列契约：样本面
        # 缩小时必须留下可读的告警，而不是让消费端从百分比里猜。
        "coverage_detail": coverage_detail(
            snapshot, scope, eligible, ready, omitted, risk_uncovered, window_sessions, len(days)
        ),
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
