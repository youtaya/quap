"""Deterministic, versioned native analytics; no Qlib imports or model claims.

Only the live intraday observation summary remains here. The ``native-2`` technical indicators and
the native screening rule were superseded by the versioned Qlib scan policy: ``Worker.execute``
rejects the legacy ``analyze``/``research`` job kinds, the rules API answers 410, and the report's
own history marks that architecture superseded, so keeping the unreachable implementations would only
leave a second place to keep the retirement decision in step.
"""

from quant_platform import __version__
from quant_platform.domain import digest, fresh, number

ANALYSIS_VERSION = __version__ + ":native-2"


def basket_summary(members, quotes, at):
    denominator = sum(members.values())
    weights = {s: w / denominator for s, w in members.items()}
    eligible = {}
    for code, weight in weights.items():
        q = quotes.get(code, {})
        last, previous = number(q.get("close")), number(q.get("pre_close"))
        if (
            fresh(q.get("source_time"), at)
            and q.get("reference_verified")
            and last is not None
            and previous is not None
            and last > 0
            and previous > 0
        ):
            eligible[code] = (weight, last / previous - 1)
    coverage = sum(w for w, _ in eligible.values())
    contributions = {s: w / coverage * ret for s, (w, ret) in eligible.items()} if coverage else {}
    daily = sum(contributions.values()) if coverage else None
    return {
        "daily_return": daily,
        "reference_proxy": 1000 * (1 + daily) if daily is not None else None,
        "coverage": coverage,
        "total": len(weights),
        "eligible": len(eligible),
        "missing": sorted(set(weights) - set(eligible)),
        "contributions": contributions,
        "concentration": sum(w * w for w in weights.values()),
        "advancing": sum(ret > 0 for _, ret in eligible.values()),
        "declining": sum(ret < 0 for _, ret in eligible.values()),
        "observation": digest(
            {s: {k: v for k, v in quotes[s].items() if k != "received_at"} for s in sorted(eligible)}
        ),
        "label": "Daily reference proxy, not investable NAV",
        "analysis_version": ANALYSIS_VERSION,
    }
