"""Research-only panels inside the existing API-only workspaces."""

from datetime import datetime
from urllib.parse import urlencode

import pandas as pd
import streamlit as st

from quant_platform.domain import CN
from quant_platform.dashboard.workflow import request_key


def paged(call, path, key, size=25):
    index = int(st.number_input("Results page", min_value=1, max_value=4000, value=1, key="page:" + key))
    separator = "&" if "?" in path else "?"
    response = call("GET", f"{path}{separator}limit={size}&offset={(index - 1) * size}")
    if not isinstance(response, dict):
        return []
    if response.get("next_offset") is not None:
        st.caption("More results are available on the next page.")
    return response.get("items", [])


def mutate(call, method, path, body):
    body = {**body, "request_key": request_key(path, body)}
    result = call(method, path, body)
    if result is not None:
        st.session_state.notice = (
            f"Research request recorded: {result}. No model release or recommendation acceptance occurred."
        )
        st.rerun()
    return result


def job_controls(call, item, key):
    state = item.get("status")
    if state not in {"pending", "running", "blocked", "failed"}:
        return
    action = "cancel" if state in {"pending", "running"} else "retry"
    if st.button(f"{action.title()} research job", key="control:" + key):
        mutate(
            call, "POST", f"/research-jobs/{item['job_id']}/{action}", {"expected_revision": item["control_revision"]}
        )


def source_panels(call, table):
    st.subheader("Supplemental research sources")
    st.warning(
        "adata is display/cross-check data only. It cannot qualify production data, pools, models, or proposals."
    )
    response = call("GET", "/research-sources")
    if not isinstance(response, dict):
        st.info("Research source catalog unavailable.")
        return
    sources = response.get("items", [])
    if not sources:
        return
    selected_id = st.selectbox("Research source", [row["source_id"] for row in sources])
    selected = next(row for row in sources if row["source_id"] == selected_id)
    st.json(selected)
    with st.form("source:" + selected["source_id"]):
        enabled = st.checkbox("Enable this research source", value=selected.get("enabled", False))
        terms = st.checkbox("I have reviewed and acknowledge this upstream's data terms.")
        submitted = st.form_submit_button("Save source revision")
    if submitted:
        mutate(
            call,
            "PUT",
            "/research-sources/" + selected["source_id"],
            {"expected_revision": selected["revision"], "enabled": enabled, "terms_acknowledged": terms},
        )
    if not response.get("collection_enabled"):
        st.info("Collection is off in this deployment. Source acknowledgment alone does not enable network access.")
    with st.form("research_collection"):
        symbols = st.text_area("Explicit stock codes (comma-separated, at most 50)")
        today = datetime.now(CN).date()
        start = st.date_input("Research start date", value=today, max_value=today)
        end = st.date_input("Research end date", value=today, max_value=today)
        confirmed = st.checkbox("Request this bounded collection; no historical availability is implied.")
        submitted = st.form_submit_button(
            "Request research collection",
            disabled=not response.get("collection_enabled") or not selected.get("enabled"),
        )
    if submitted:
        if not confirmed:
            st.error("Confirm the explicit collection request.")
        else:
            mutate(
                call,
                "POST",
                "/research-collections",
                {
                    "source_id": selected["source_id"],
                    "expected_revision": selected["revision"],
                    "symbols": [value.strip() for value in symbols.split(",") if value.strip()],
                    "start": str(start),
                    "end": str(end),
                },
            )
    collections = paged(call, "/research-collections", "collections")
    table(collections)
    if collections:
        by_id = {row["id"]: row for row in collections}
        item_id = st.selectbox(
            "Inspect collection job", list(by_id), format_func=lambda key: f"{key} · {by_id[key]['status']}"
        )
        job_controls(call, by_id[item_id], "collection")
    snapshot_panel(call, table, "source=" + selected["source_id"], source_id=selected["source_id"])


def snapshot_panel(call, table, key, **filters):
    st.caption(
        "Snapshot retrieval time is not historical availability. Quarantined/failed results remain visible and never replace successful evidence."
    )
    rows = paged(call, "/research-snapshots?" + urlencode(filters), "snapshots:" + key)
    table(rows, columns=["id", "source_id", "symbol", "status", "retrieved_at", "row_count"])
    if not rows:
        st.info("No supplemental snapshot is available. Coverage and historical availability are unknown.")
        return
    chosen = st.selectbox(
        "Inspect supplemental snapshot",
        rows,
        key="snapshot:" + key,
        format_func=lambda row: f"{row['symbol']} · {row['status']} · {row['retrieved_at']}",
    )
    detail = call("GET", f"/research-snapshots/{chosen['id']}?limit=100")
    if detail:
        st.json(detail["snapshot"])
        table(detail["rows"]["items"])
        if detail["rows"].get("next_offset") is not None:
            st.caption("Preview limited to 100 rows; the authenticated API supports further pages.")
    checks = call("GET", "/research-quality?" + urlencode({"snapshot_id": chosen["id"]}))
    if checks:
        st.subheader("Diagnostic differences and coverage")
        st.json(checks)


def factor_library(call, table):
    inventory = call("GET", "/factors/inventory")
    if inventory:
        with st.expander("Immutable Alpha158 and five-minute inventory"):
            st.json(inventory)
    factors = paged(call, "/factors", "factors")
    table(factors)
    with st.form("new_factor"):
        name = st.text_input("Factor name", max_chars=64)
        expression = st.text_area("Restricted daily factor expression", value="$close/Ref($close,5)-1", max_chars=2048)
        author = st.text_input("Factor author", max_chars=120)
        revision = st.number_input("Expected factor revision (0 for new)", min_value=0, value=0)
        st.caption(
            "Market fields, arithmetic, Abs/Log, past Ref/Delta, Mean/Std/Sum/Min/Max/Corr only; windows 1–252. No Python or labels."
        )
        submitted = st.form_submit_button("Save experimental factor")
    if submitted:
        mutate(
            call,
            "POST",
            "/factors",
            {"name": name, "expression": expression, "author": author, "expected_revision": int(revision)},
        )
    sets = paged(call, "/factor-sets", "factor-sets")
    table(sets)
    with st.form("new_factor_set"):
        name = st.text_input("Factor-set name", max_chars=120)
        members = st.multiselect(
            "Ordered factor revisions", factors, format_func=lambda row: f"{row['name']} r{row['revision']}"
        )
        revision = st.number_input("Expected factor-set revision (0 for new)", min_value=0, value=0)
        submitted = st.form_submit_button("Save experimental factor set")
    if submitted:
        mutate(
            call,
            "POST",
            "/factor-sets",
            {"name": name, "factors": [row["id"] for row in members], "expected_revision": int(revision)},
        )


def configurations(call, table):
    rows = paged(call, "/training-configurations", "configurations")
    table(rows)
    sets = paged(call, "/factor-sets", "configuration-sets")
    with st.form("new_training_configuration"):
        name = st.text_input("Training-configuration name", max_chars=120)
        frequency = st.selectbox("Configuration frequency", ["day", "5min"])
        factor_set = st.selectbox(
            "Feature contract",
            [None, *sets],
            format_func=lambda row: "Built-in features" if row is None else f"{row['name']} r{row['revision']}",
        )
        rate = st.number_input("Learning rate", min_value=0.001, max_value=0.3, value=0.05, format="%.3f")
        leaves = st.number_input("LightGBM leaves", min_value=2, max_value=127, value=31)
        rounds = st.number_input("Maximum boosting rounds", min_value=10, max_value=2000, value=1000)
        seed = st.number_input("Random seed", min_value=0, max_value=2147483647, value=42)
        revision = st.number_input("Expected configuration revision (0 for new)", min_value=0, value=0)
        st.caption(
            "Three fixed chronological folds. Daily windows: 756/252/252 sessions; five-minute: 120/40/60. The final test is reserved until freeze."
        )
        submitted = st.form_submit_button("Save immutable training configuration")
    if submitted:
        train, valid, test = (756, 252, 252) if frequency == "day" else (120, 40, 60)
        mutate(
            call,
            "POST",
            "/training-configurations",
            {
                "name": name,
                "frequency": frequency,
                "factor_set_id": factor_set["id"] if factor_set else None,
                "learning_rate": rate,
                "num_leaves": int(leaves),
                "rounds": int(rounds),
                "seed": int(seed),
                "expected_revision": int(revision),
                "train_sessions": train,
                "validation_sessions": valid,
                "test_sessions": test,
            },
        )
    return rows


def experiment_panel(call, table):
    configs = configurations(call, table)
    generations = call("GET", "/qlib-generations") or []
    if len(configs) >= 2 and generations:
        with st.form("compare_research"):
            generation = st.selectbox(
                "Canonical prepared generation",
                generations,
                format_func=lambda row: f"{row['id']} · {row['frequency']}",
            )
            baseline = st.selectbox(
                "Baseline configuration", configs, format_func=lambda row: f"{row['name']} r{row['revision']}"
            )
            candidate = st.selectbox(
                "Candidate configuration", configs, index=1, format_func=lambda row: f"{row['name']} r{row['revision']}"
            )
            submitted = st.form_submit_button("Compare development folds")
        if submitted:
            mutate(
                call,
                "POST",
                "/research-experiments",
                {
                    "expected_revision": 0,
                    "generation_id": generation["id"],
                    "baseline_configuration_id": baseline["id"],
                    "candidate_configuration_id": candidate["id"],
                },
            )
    else:
        st.info("Comparison requires two immutable configurations and a qualified canonical generation.")
    experiments = paged(call, "/research-experiments", "experiments")
    table(experiments)
    if not experiments:
        return
    by_id = {row["id"]: row for row in experiments}
    selected_id = st.selectbox(
        "Inspect development experiment", list(by_id), format_func=lambda key: f"{key} · {by_id[key]['status']}"
    )
    selected = by_id[selected_id]
    detail = call("GET", f"/research-experiments/{selected['id']}?limit=100")
    if not detail:
        return
    trials = detail["trials"]["items"]
    table(trials)
    curves = []
    for trial in trials:
        for point in (trial.get("result") or {}).get("quality", []):
            curves.append(
                {
                    "time": point["time"],
                    "configuration": str(trial["configuration_id"]),
                    "fold": trial["fold"],
                    "rank_ic": point.get("rank_ic"),
                }
            )
    if curves:
        frame = pd.DataFrame(curves)
        st.line_chart(frame, x="time", y="rank_ic", color="configuration")
    with st.expander("Pinned folds, holdout, attempts, and latency"):
        st.json(detail)
    if detail["trials"].get("next_offset") is not None:
        st.caption("Showing the first 100 attempts; additional attempts remain available through the paginated API.")
    with st.expander("Factor coverage, distribution, correlation, and stability"):
        if st.checkbox("Load immutable validation factor evidence", key="factor-evidence:" + selected_id):
            trial_evidence(call, table, selected_id, trials)
    job_controls(call, detail["experiment"], "experiment")
    frozen = detail.get("freeze")
    if selected["status"] == "complete" and not frozen:
        with st.form("freeze_candidate"):
            chosen = st.selectbox("Configuration to freeze", [selected["baseline_id"], selected["candidate_id"]])
            confirmed = st.checkbox("I select using validation evidence only; final test exposure will be recorded.")
            submitted = st.form_submit_button("Freeze candidate specification")
        if submitted and confirmed:
            mutate(
                call,
                "POST",
                f"/research-experiments/{selected['id']}/freeze",
                {"configuration_id": chosen, "expected_revision": 1},
            )
    if frozen:
        st.caption(
            "Frozen specification only. Training still uses existing evaluation, shadow, and manual release gates."
        )
        if st.button("Create challenger from frozen candidate"):
            config = (
                selected["spec"]["baseline"]
                if str(selected["baseline_id"]) == str(frozen["configuration_id"])
                else selected["spec"]["candidate"]
            )
            body = {
                "purpose": "training",
                "frequency": config["frequency"],
                "training_configuration_id": frozen["configuration_id"],
            }
            body["request_key"] = request_key("frozen-training:" + str(frozen["id"]), body)
            result = call("POST", "/model-training-runs", body)
            if result is not None:
                st.success(f"Challenger run {result['run_id']} requested; no active release change.")


def trial_evidence(call, table, experiment_id, trials):
    available = {row["id"]: row for row in trials if row.get("state") == "succeeded" and row.get("artifact_id")}
    if not available:
        st.info("Successful factor evidence is unavailable.")
        return
    trial_id = st.selectbox(
        "Validation trial",
        list(available),
        format_func=lambda key: f"{available[key]['configuration_id']} · fold {available[key]['fold']} · {key}",
        key="trial-evidence:" + experiment_id,
    )
    path = f"/research-experiments/{experiment_id}/trials/{trial_id}/diagnostics"
    catalog = call("GET", path + "?limit=1")
    if not catalog or catalog.get("status") != "available":
        st.info("Factor diagnostics unavailable; no substitute evidence is generated.")
        return
    feature = st.selectbox("Validation feature", catalog["features"], key="feature-evidence:" + trial_id)
    index = int(
        st.number_input("Factor stability page", min_value=1, max_value=1667, value=1, key="stability:" + trial_id)
    )
    evidence = call("GET", path + "?" + urlencode({"feature": feature, "limit": 60, "offset": (index - 1) * 60}))
    if not evidence or evidence.get("status") != "available":
        st.info("Selected factor evidence unavailable.")
        return
    st.caption(
        "Validation-only diagnostics, not final-test evidence or production qualification. Correlations cover the first 64 features."
    )
    st.json({key: evidence.get(key) for key in ("coverage", "missingness", "distribution", "correlation")})
    quality = evidence["quality"]
    table(quality["items"])
    if quality["items"]:
        st.line_chart(pd.DataFrame(quality["items"]).set_index("time")[["ic", "rank_ic"]])
    if quality.get("next_offset") is not None:
        st.caption("More stability observations are available on the next page.")


def diagnostics_panel(call, table, model_id):
    response = call("GET", f"/models/{model_id}/diagnostics?limit=100")
    if not response or response.get("status") == "unavailable":
        st.info(
            "Diagnostics unavailable. Old models need new training for a training-only drift baseline; labels must mature before quality is reported."
        )
        return
    st.caption(
        "Informational only: PSI levels 0.1/0.25 do not trigger trading, retraining, or promotion. Intraday quality is aggregated by session."
    )
    st.json(response["summary"]["rolling"])
    sessions = response["summary"].get("sessions", [])
    if sessions:
        st.line_chart(pd.DataFrame(sessions).set_index("session")[["ic", "rank_ic"]])
    items = response.get("items", [])
    table(items)
    if items:
        latest = items[0]["data"]
        drift = latest.get("drift", {})
        st.json(
            {"drift": drift, "realized": latest.get("realized"), "stage_latency_seconds": latest.get("latency_seconds")}
        )
        importance = drift.get("importance_gain", {})
        if importance:
            st.bar_chart(pd.DataFrame({"gain": importance}).sort_values("gain", ascending=False).head(30))
