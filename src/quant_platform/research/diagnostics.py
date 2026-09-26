"""Training-only reference distributions and informational realized quality metrics."""

import numpy as np
import pandas as pd

METRIC_VERSION = "qlib-diagnostics-v1"


def finite_array(values):
    return np.asarray(values, dtype=float)


def histogram_counts(values, edges):
    values = finite_array(values)
    edges = np.asarray(edges, dtype=float)
    count = max(1, len(edges) - 1)
    bins = np.full(len(values), count + 2, dtype=int)
    finite = np.isfinite(values)
    bins[finite] = np.searchsorted(edges[1:-1], values[finite], side="right") + 1
    bins[finite & (values < edges[0])] = 0
    bins[finite & (values > edges[-1])] = count + 1
    return np.bincount(bins, minlength=count + 3).tolist()


def reference_histogram(values):
    values = finite_array(values)
    finite = values[np.isfinite(values)]
    if not len(finite):
        return {"status": "unavailable", "reason": "no_finite_training_samples", "samples": len(values)}
    edges = np.unique(np.quantile(finite, np.linspace(0, 1, 11))).tolist()
    if len(edges) == 1:
        edges = edges * 2
    return {
        "status": "available",
        "edges": edges,
        "counts": histogram_counts(values, edges),
        "samples": len(values),
        "missing": int(len(values) - len(finite)),
        "constant": edges[0] == edges[-1],
    }


def psi(reference, values, epsilon=1e-6):
    if reference.get("status") != "available" or not reference.get("samples") or not len(values):
        return {"status": "unavailable", "reason": "missing_reference_or_observations"}
    counts = np.asarray(histogram_counts(values, reference["edges"]), dtype=float)
    base = np.asarray(reference["counts"], dtype=float)
    expected = (base + epsilon) / (base.sum() + epsilon * len(base))
    actual = (counts + epsilon) / (counts.sum() + epsilon * len(counts))
    value = float(np.sum((actual - expected) * np.log(actual / expected)))
    return {
        "status": "available",
        "psi": value,
        "samples": int(counts.sum()),
        "counts": counts.astype(int).tolist(),
        "warning": "high" if value >= 0.25 else "moderate" if value >= 0.1 else "low",
        "informational_only": True,
    }


def cross_section(scores, labels, minimum=30):
    score_series = pd.Series(scores, name="score", dtype=float)
    label_series = pd.Series(labels, name="label", dtype=float).reindex(score_series.index)
    frame = pd.concat([score_series, label_series], axis=1)
    frame = frame.replace([np.inf, -np.inf], np.nan)
    pairs = frame.dropna()
    result = {
        "status": "insufficient_data",
        "scored_count": len(scores),
        "valid_pairs": len(pairs),
        "excluded_count": len(frame) - len(pairs),
        "ic": None,
        "rank_ic": None,
    }
    if len(pairs) < minimum:
        result["reason"] = "fewer_than_required_pairs"
    elif pairs.score.nunique() <= 1 or pairs.label.nunique() <= 1:
        result["reason"] = "constant_scores_or_labels"
    else:
        result.update(
            status="available",
            ic=float(pairs.score.corr(pairs.label)),
            rank_ic=float(pairs.score.rank().corr(pairs.label.rank())),
        )
    return result


def training_reference(features, scores, frequency, importance):
    cohorts = {}
    if frequency == "day":
        groups = [("day", features)]
    else:
        groups = features.groupby(features.index.get_level_values("datetime").strftime("%H:%M"))
    for key, frame in groups:
        aligned = scores.reindex(frame.index)
        cohorts[str(key)] = {
            "features": {str(column): reference_histogram(frame[column].to_numpy()) for column in frame.columns},
            "score": reference_histogram(aligned.to_numpy()),
        }
    return {
        "metric_version": METRIC_VERSION,
        "scope": "training_only",
        "frequency": frequency,
        "cohorts": cohorts,
        "importance_gain": {str(column): float(value) for column, value in zip(features.columns, importance)},
    }


def distribution_drift(reference, features, scores, frequency, cutoff):
    if not reference or reference.get("scope") != "training_only":
        return {"status": "unavailable", "reason": "model_has_no_training_baseline"}
    if reference["frequency"] != frequency:
        raise ValueError("Diagnostic frequency mismatch.")
    key = "day" if frequency == "day" else pd.Timestamp(cutoff).strftime("%H:%M")
    cohort = reference["cohorts"].get(key)
    if not cohort:
        return {"status": "unavailable", "reason": "matching_time_of_day_cohort_unavailable"}
    if list(cohort["features"]) != list(map(str, features.columns)):
        raise ValueError("Diagnostic feature order mismatch.")
    return {
        "status": "available",
        "cohort": key,
        "features": {str(c): psi(cohort["features"][str(c)], features[c]) for c in features.columns},
        "score": psi(cohort["score"], list(scores.values())),
        "importance_gain": reference["importance_gain"],
    }


def factor_diagnostics(features, labels):
    bounded = features.replace([np.inf, -np.inf], np.nan)
    result = {
        "coverage": (1 - bounded.isna().mean()).to_dict(),
        "missingness": bounded.isna().mean().to_dict(),
        "distribution": bounded.describe(percentiles=[0.1, 0.5, 0.9])
        .replace([np.inf, -np.inf, np.nan], None)
        .to_dict(),
        "correlation": bounded.iloc[:, :64].corr().replace([np.inf, -np.inf, np.nan], None).to_dict(),
    }
    by_factor = {}
    label_series = labels.iloc[:, 0] if isinstance(labels, pd.DataFrame) else labels
    for column in bounded.columns:
        samples = []
        for stamp, frame in bounded[[column]].groupby(level="datetime"):
            quality = cross_section(frame[column], label_series.reindex(frame.index))
            samples.append({"time": str(stamp), **quality})
        by_factor[str(column)] = samples
    result["quality_by_time"] = by_factor
    return result


def rolling_quality(records):
    """One cross-sectional quality observation per session, including minute models."""
    grouped = {}
    for record in records:
        quality = record["data"].get("realized", {})
        grouped.setdefault(str(record["session"]), []).append(quality)
    sessions = []
    for day, qualities in sorted(grouped.items()):
        valid = [q for q in qualities if q.get("status") == "available"]
        sessions.append(
            {
                "session": day,
                "ic": float(np.mean([q["ic"] for q in valid])) if valid else None,
                "rank_ic": float(np.mean([q["rank_ic"] for q in valid])) if valid else None,
                "cutoffs": len(qualities),
                "valid_cutoffs": len(valid),
                "status": "available" if valid else "insufficient_data",
            }
        )
    windows = {}
    for n in (20, 60):
        sample = sessions[-n:]
        valid = [row for row in sample if row["status"] == "available"]
        complete = len(sample) == len(valid) == n
        windows[str(n)] = {
            "status": "available" if complete else "insufficient_data",
            "sessions": len(sample),
            "valid_sessions": len(valid),
            "ic": float(np.mean([r["ic"] for r in valid])) if complete else None,
            "rank_ic": float(np.mean([r["rank_ic"] for r in valid])) if complete else None,
        }
    return {"session_aggregation": True, "sessions": sessions, "rolling": windows}
