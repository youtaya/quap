"""Pinned personal research run. It does not change baskets or submit orders."""

from datetime import date

from quant_platform.domain import symbol


def _suspensions(db, watermark):
    rows = db.rows(
        "SELECT DISTINCT ON (scope) scope, metadata FROM datasets WHERE endpoint='daily' AND id<=%s "
        "ORDER BY scope, id DESC",
        (watermark,),
    )
    found = set()
    for row in rows:
        try:
            day = date.fromisoformat(row["scope"])
        except ValueError:
            continue
        for event in (row["metadata"] or {}).get("suspension_events") or []:
            if event.get("suspend_type") != "S":
                continue
            try:
                found.add((symbol(event.get("ts_code", "")), day))
            except ValueError:
                continue
    return found


def run(db, job, settings):
    from quant_platform.domain.workflow import PipelineBlocked

    raise PipelineBlocked("Native evaluation is retired. Create an explicit Qlib model-training run.")
