"""Refresh the operator candidate screen and holding note on the scheduler cadence."""

from quant_platform.operator_brief import run_brief
from quant_platform.storage import jsonb


def run(db, settings, job):
    profile = db.setting("operator_brief") or {}
    if profile.get("enabled") is False:
        with db.publication(job) as conn:
            conn.execute(
                "UPDATE jobs SET progress=%s WHERE id=%s",
                (jsonb({"skipped": True}), job["id"]),
            )
        return {"skipped": True}
    result = run_brief(
        settings,
        profile.get("email") or settings.notify_email,
        profile.get("symbol") or "SH600895",
        float(profile.get("cost") or 28),
        int(profile.get("top_n") or 3),
        when_changed=True,
    )
    progress = {
        "as_of": result["as_of"],
        "changed": result["changed"],
        "candidates": [item["symbol"] for item in result["candidates"]],
        "stance": result["holding"]["stance"],
        "delivery": result["messages"][0]["delivery"],
    }
    with db.publication(job) as conn:
        conn.execute("UPDATE jobs SET progress=%s WHERE id=%s", (jsonb(progress), job["id"]))
    return progress
