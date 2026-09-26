"""Supplemental row validation and raw-price comparisons; no canonical writes."""

from datetime import datetime, time, timedelta
import math

from quant_platform.domain import CN, symbol

QUALITY_VERSION = "raw-cross-check-v1"
PRICE_FIELDS = ("open", "high", "low", "close")


def normalize(records, code, start, end, frequency, retrieved_at):
    """adata 2.9.5 already returns shares; never multiply volume a second time."""
    if not records:
        return [], {"status": "unavailable", "reason": "empty_response", "historical_availability": "unknown"}
    rows, problems, seen = [], [], set()
    cutoff = retrieved_at.astimezone(CN).replace(second=0, microsecond=0) - timedelta(minutes=1)
    excluded = 0
    for record in records:
        try:
            if symbol(record["stock_code"]) != code:
                raise ValueError("identity_mismatch")
            stamp = datetime.fromisoformat(record["trade_time"])
            stamp = stamp.replace(tzinfo=CN) if stamp.tzinfo is None else stamp.astimezone(CN)
            if frequency == "day":
                stamp = datetime.combine(stamp.date(), time(15), CN)
            elif (
                stamp.second
                or stamp.microsecond
                or stamp.time() < time(9, 30)
                or stamp.time() > time(15)
                or time(11, 30) < stamp.time() < time(13)
            ):
                raise ValueError("outside_session")
            if not start <= stamp.date() <= end:
                raise ValueError("outside_request_range")
            if frequency != "day" and stamp.date() != retrieved_at.astimezone(CN).date():
                raise ValueError("stale_intraday_session")
            if stamp > cutoff:
                excluded += 1
                continue
            if stamp in seen:
                raise ValueError("duplicate_timestamp")
            seen.add(stamp)
            values = {}
            for field in (*PRICE_FIELDS, "volume", "amount"):
                raw = record[field]
                if isinstance(raw, bool):
                    raise ValueError("invalid_numeric_value")
                value = float(raw)
                if not math.isfinite(value) or value < 0 or (field in PRICE_FIELDS and value == 0):
                    raise ValueError("invalid_numeric_value")
                values[field] = value
            if (
                not values["low"]
                <= min(values["open"], values["close"])
                <= max(values["open"], values["close"])
                <= values["high"]
            ):
                raise ValueError("inconsistent_ohlc")
            rows.append({"symbol": code, "time": stamp.isoformat(), **values})
        except (ValueError, TypeError, KeyError, OverflowError):
            problems.append("invalid_identity_timestamp_or_ohlcv")
    status = "quarantined" if problems else "available" if rows else "unavailable"
    return sorted(rows, key=lambda row: row["time"]), {
        "status": status,
        "invalid_rows": len(problems),
        "excluded_unfinalized_rows": excluded,
        "historical_availability": "unknown",
        "volume_conversion": "none; pinned adata already returns shares",
        "adjustment": "raw",
        "production_eligible": False,
    }


def compare_raw(rows, canonical, expected_times=None):
    """Both arguments must have identical raw-price units and finalized timestamps."""
    actual = {row["time"]: row for row in rows}
    benchmark = {row["time"]: row for row in canonical}
    differences = []
    for stamp in sorted(actual.keys() & benchmark.keys()):
        left, right = actual[stamp], benchmark[stamp]
        if left["symbol"] != right["symbol"]:
            raise ValueError("Cross-check identities differ.")
        for field in (*PRICE_FIELDS, "volume", "amount"):
            value, reference = left[field], right.get(field)
            if reference is None or not math.isfinite(float(reference)):
                differences.append({"time": stamp, "field": field, "state": "canonical_missing"})
                continue
            tolerance = max(0.01, abs(reference) * 0.001) if field in PRICE_FIELDS else abs(reference) * 0.01
            if abs(value - reference) > tolerance:
                differences.append({"time": stamp, "field": field, "supplemental": value, "canonical": reference})
    missing = sorted(set(expected_times if expected_times is not None else benchmark) - actual.keys())
    return {
        "metric_version": QUALITY_VERSION,
        "diagnostic_only": True,
        "tolerances": {"price_cny": 0.01, "price_relative": 0.001, "volume_amount_relative": 0.01},
        "matched_rows": len(actual.keys() & benchmark.keys()),
        "missing_rows": missing,
        "canonical_missing_rows": sorted(actual.keys() - benchmark.keys()),
        "deviations": differences[:1000],
        "deviation_count": len(differences),
        "completeness": "unknown" if expected_times is None else "incomplete" if missing else "complete",
        "status": "warning" if differences or missing else "available" if benchmark else "unavailable",
    }
