"""In-memory UI fixture only; no database, provider, or Qlib runtime calls."""

from urllib.parse import urlsplit
from uuid import UUID

import streamlit as st

from quant_platform.domain.research import SOURCE_CATALOG
from quant_platform.dashboard.workflow import WORKSPACES, render


def identifier(number):
    return str(UUID(int=number))


def fixture_call(method, path, body=None):
    route = urlsplit(path).path
    state = st.session_state.setdefault(
        "fixture_data",
        {
            "source_revision": 0,
            "enabled": False,
            "factors": [],
            "factor-sets": [],
            "training-configurations": [],
            "research-experiments": [],
            "research-collections": [],
            "mutations": [],
        },
    )
    if method != "GET":
        state["mutations"].append({"method": method, "path": route, "body": body})
        if route.startswith("/research-sources/"):
            state.update(source_revision=state["source_revision"] + 1, enabled=body["enabled"])
            return {"revision": state["source_revision"], "enabled": body["enabled"]}
        if route == "/research-collections":
            row = {"id": identifier(10), "job_id": 10, "status": "complete", "control_revision": 1, "scope": body}
            state["research-collections"].append(row)
            return row
        resource = route.lstrip("/")
        if resource in {"factors", "factor-sets", "training-configurations"}:
            row = {
                "id": identifier(100 + len(state["mutations"])),
                "name": body["name"],
                "revision": body["expected_revision"] + 1,
                "frequency": body.get("frequency", "day"),
                "data": body,
            }
            state[resource].append(row)
            return row
        if route == "/research-experiments":
            row = {
                "id": identifier(20),
                "job_id": 20,
                "status": "complete",
                "control_revision": 1,
                "baseline_id": body["baseline_configuration_id"],
                "candidate_id": body["candidate_configuration_id"],
                "spec": {"baseline": {"frequency": "day"}, "candidate": {"frequency": "day"}},
                "fixture_trials_only": True,
            }
            state["research-experiments"].append(row)
            return row
        if route.endswith("/freeze"):
            state["freeze"] = {"id": identifier(30), "configuration_id": body["configuration_id"]}
            return {**state["freeze"], "model_activated": False}
        if route == "/model-training-runs":
            return {"run_id": identifier(40), "state": "pending"}
        raise AssertionError("Unsupported fixture mutation: " + route)
    if route == "/research-sources":
        return {
            "items": [
                {
                    **SOURCE_CATALOG["adata-eastmoney-intraday"],
                    "revision": state["source_revision"],
                    "enabled": state["enabled"],
                    "terms_acknowledged": state["enabled"],
                    "observed_health": {"status": "unknown"},
                }
            ],
            "collection_enabled": True,
            "production_source": "tushare",
        }
    if route == "/factors/inventory":
        from quant_platform.storage.experiments import inventory

        return {"items": inventory()}
    if route.lstrip("/") in state and isinstance(state[route.lstrip("/")], list):
        return {"items": state[route.lstrip("/")], "next_offset": None}
    if route.startswith("/research-experiments/") and route.endswith("/diagnostics"):
        return {
            "status": "available",
            "features": ["R_Momentum"],
            "coverage": 0.95,
            "missingness": 0.05,
            "distribution": {"mean": 0.1},
            "correlation": {"R_Momentum": 1.0},
            "quality": {
                "items": [{"time": "2025-01-01", "ic": 0.1, "rank_ic": 0.2, "valid_pairs": 35}],
                "next_offset": None,
            },
        }
    if route.startswith("/research-experiments/"):
        experiment = state["research-experiments"][0]
        return {
            "experiment": experiment,
            "freeze": state.get("freeze"),
            "trials": {
                "items": [
                    {
                        "id": identifier(200 + side * 3 + fold),
                        "artifact_id": identifier(300 + side * 3 + fold),
                        "configuration_id": config,
                        "fold": fold,
                        "state": "succeeded",
                        "result": {"quality": [{"time": f"2025-0{fold + 1}-01", "rank_ic": 0.1 + fold / 10}]},
                    }
                    for side, config in enumerate((experiment["baseline_id"], experiment["candidate_id"]))
                    for fold in range(3)
                ]
            },
        }
    snapshot = {
        "id": identifier(11),
        "symbol": "SH600000",
        "source_id": "adata-eastmoney-intraday",
        "status": "quarantined",
        "retrieved_at": "2026-01-05T09:36:00+08:00",
        "row_count": 1,
        "historical_available_at": None,
    }
    if route == "/research-snapshots":
        return {"items": [snapshot] if state["research-collections"] else [], "next_offset": None}
    if route.startswith("/research-snapshots/"):
        return {
            "snapshot": snapshot,
            "rows": {
                "items": [{"symbol": "SH600000", "time": "2026-01-05T09:31:00+08:00", "close": 10}],
                "next_offset": None,
            },
        }
    if route == "/research-quality":
        return {
            "items": [
                {"diagnostic_only": True, "completeness": "incomplete", "missing_rows": ["2026-01-05T09:32:00+08:00"]}
            ]
        }
    if route == "/qlib-generations":
        return [{"id": identifier(1), "frequency": "day", "fixture_only": True}]
    if route == "/data-readiness":
        return {
            "frequencies": {
                frequency: {"ready": False, "blockers": ["Fixture only; no production release"], "model": None}
                for frequency in ("day", "5min")
            }
        }
    if route == "/watchlist":
        return {"symbols": [], "revision": 0}
    if route == "/scan-policy":
        from quant_platform.domain.workflow import ScanPolicy

        return {"revision": 0, "data": ScanPolicy().model_dump()}
    return []


def fixture_app():
    st.set_page_config(page_title="QUAP Research UI Fixture", layout="wide")
    st.warning("FIXTURE ONLY — in-memory UI demonstration. No external data, database, Qlib training, or orders.")
    if notice := st.session_state.pop("notice", None):
        st.success(notice)
    page = st.sidebar.radio("Workspace", list(WORKSPACES), index=5)

    def table(rows, columns=None):
        if rows:
            st.dataframe(rows, width="stretch")

    render(page, fixture_call, table, str, lambda **kwargs: None)


if __name__ == "__main__":
    fixture_app()
