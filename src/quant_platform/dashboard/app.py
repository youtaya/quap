"""Streamlit display/control client for the independent platform API."""

import json
import os
from urllib.parse import quote

import httpx
import pandas as pd
import streamlit as st

from quant_platform.dashboard.client import Client

st.set_page_config(page_title="Quant Platform", layout="wide")
st.title("A-share Quant Platform")
st.caption("Continuous research · Entitled market data · Versioned baskets · No order execution")

if "token" not in st.session_state:
    with st.form("login"):
        token = st.text_input("Operator token", type="password")
        if st.form_submit_button("Sign in"):
            client = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), token)
            try:
                client.request("GET", "/status")
                st.session_state.token = token
                st.rerun()
            except (ValueError, httpx.HTTPError):
                st.error("Authentication failed or backend unavailable.")
    st.stop()

client = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), st.session_state.token)
if st.sidebar.button("Sign out"):
    del st.session_state.token
    st.rerun()
page = st.sidebar.radio("Workspace", ["Operations", "Baskets", "Stocks", "Candidates", "Reports"])


def call(method, path, data=None):
    try:
        return client.request(method, path, data)
    except (httpx.HTTPError, ValueError) as exc:
        st.error(str(exc) if isinstance(exc, ValueError) else "Backend disconnected. Retained data is not live.")
        return None


@st.fragment(run_every=10)
def operations():
    status = call("GET", "/status")
    if status is None:
        return
    a, b, c = st.columns(3)
    a.metric("Stored quotes", status["quote_count"])
    b.metric("Fresh quotes", status["fresh_quote_count"])
    c.metric("Provider", status["provider"])
    if status["data_health"] != "fresh":
        st.warning("Data is degraded or unavailable. Process health does not establish market-data freshness.")
    st.caption(f"Analysis target: {status['analysis_target']} | Polling paused: {status['polling_paused']}")
    st.dataframe(status["services"], hide_index=True)
    st.subheader("Market board coverage")
    st.dataframe(
        [
            {k: b.get(k) for k in ("board", "listed", "stored", "fresh", "coverage", "daily_return")}
            for b in status.get("boards", [])
        ],
        hide_index=True,
    )
    st.caption("Equal-weight eligible stocks, not official exchange indexes. Missing weight is disclosed.")
    st.subheader("Provider capabilities")
    st.dataframe(status["capabilities"], hide_index=True)
    for col, action in zip(st.columns(5), ("pause", "resume", "refresh", "analyze", "doctor")):
        if col.button(action.title()):
            result = call("POST", "/control", {"action": action})
            if result:
                st.info(result)
    st.subheader("Durable jobs")
    st.dataframe(status["jobs"], hide_index=True)
    with st.expander("Progress, failures and job IDs"):
        jobs = call("GET", "/jobs") or []
        st.dataframe(jobs, hide_index=True)
        failed = [j["id"] for j in jobs if j["status"] == "failed"]
        if failed:
            job_id = st.selectbox("Failed job", failed)
            if st.button("Retry failed job"):
                st.info(call("POST", f"/jobs/{job_id}/retry"))
    with st.expander("Persistent polling settings and metrics"):
        options = call("GET", "/settings")
        if options:
            seconds = st.number_input("Polling interval (seconds)", 15, 3600, options["quote_seconds"])
            if st.button("Save polling interval"):
                st.info(call("PUT", "/settings", {"quote_seconds": seconds, "expected_revision": options["revision"]}))
        st.json(status.get("metrics", {}))
    with st.expander("History, backup and optional Qlib status"):
        st.json({k: status[k] for k in ("directory", "last_analysis", "backup", "qlib", "qualification", "incidents")})
    st.subheader("Persistent alerts")
    st.dataframe(call("GET", "/alerts") or [], hide_index=True)


@st.fragment(run_every=10)
def basket_history(basket_id):
    observations = call("GET", f"/baskets/{basket_id}/observations") or []
    if observations:
        records = [
            {
                "time": o["minute"],
                "proxy": o["data"].get("reference_proxy"),
                "series": f"revision {o['revision']} · {pd.Timestamp(o['minute']).tz_convert('Asia/Shanghai').date()}",
            }
            for o in observations
        ]
        frame = pd.DataFrame(records).pivot(index="time", columns="series", values="proxy").sort_index()
        st.line_chart(frame)
        st.caption("Daily reference proxy, not NAV. Trading dates and basket revisions are separate series.")
        st.json(observations[0]["data"])
    else:
        st.info("No observations for this basket yet; pending revisions activate next trading session.")


if page == "Operations":
    operations()
elif page == "Baskets":
    baskets = call("GET", "/baskets") or []
    choices = {"New basket": None, **{f"{b['name']} · {b['id']}": b for b in baskets}}
    selected = choices[st.selectbox("Basket", list(choices))]
    source = selected["data"] if selected else {}
    if not selected:
        imported = st.file_uploader("Import basket JSON", type=["json"])
        if imported:
            try:
                source = json.loads(imported.getvalue())
                if not isinstance(source, dict) or not isinstance(source.get("members"), dict):
                    raise ValueError("Invalid basket document")
            except (ValueError, TypeError):
                st.error("Import requires an exported basket object with members and positive weights.")
                source = {}
    with st.form("basket_editor"):
        name = st.text_input("Name", value=source.get("name", "My basket"))
        members = st.text_area(
            "Members and positive weights (JSON)", value=json.dumps(source.get("members", {"SH600895": 1}), indent=2)
        )
        alerts = st.text_area(
            "Alert policy (JSON)",
            value=json.dumps(
                source.get(
                    "alerts", {"stock_change": 0.03, "basket_change": 0.01, "minimum_coverage": 0.95, "cooldown": 300}
                ),
                indent=2,
            ),
        )
        if st.form_submit_button("Save next-session revision"):
            try:
                value = {
                    "name": name,
                    "members": json.loads(members),
                    "expected_revision": selected["revision"] if selected else 0,
                }
                value["alerts"] = json.loads(alerts)
                result = call(
                    "PUT" if selected else "POST", f"/baskets/{selected['id']}" if selected else "/baskets", value
                )
                if result:
                    st.success(result)
            except (ValueError, TypeError):
                st.error("Enter a valid symbol-to-weight JSON object.")
    if selected:
        st.caption(f"Latest revision {selected['revision']} becomes effective {selected['effective_day']}.")
        st.download_button("Export basket", json.dumps(selected["data"], indent=2), "basket.json")
        for col, action in zip(st.columns(3), ("pause", "resume", "archive")):
            if col.button(action.title()):
                result = call(
                    "POST",
                    f"/baskets/{selected['id']}/control",
                    {
                        "action": action,
                        "expected_revision": selected["revision"],
                        "expected_control_revision": selected["control_revision"],
                    },
                )
                if result:
                    st.rerun()
        basket_history(selected["id"])
        st.dataframe(call("GET", f"/baskets/{selected['id']}/revisions") or [], hide_index=True)
elif page == "Stocks":
    search = st.text_input("Search symbol or company")
    st.dataframe(call("GET", "/instruments?search=" + quote(search)) or [], hide_index=True)
    code = st.text_input("History symbol", "SH600895")
    if st.button("Load stock history and reports"):
        history = call("GET", "/history/" + quote(code)) or []
        if history:
            frame = pd.DataFrame([{"day": r["day"], **r["data"]} for r in history])
            st.line_chart(frame.set_index("day")[["close"]])
            st.caption("Unadjusted CNY; current charts are independent of the optional Qlib adapter.")
        st.json(call("GET", "/reports?kind=stock&target=" + quote(code) + "&limit=1") or [])
elif page == "Candidates":
    current = call("GET", "/rules")
    if current:
        with st.form("screen_rule"):
            rule = st.text_area("Versioned screen rule (JSON)", json.dumps(current["data"], indent=2))
            if st.form_submit_button("Save rule"):
                try:
                    st.info(call("PUT", "/rules", {"rule": json.loads(rule), "expected_revision": current["revision"]}))
                except ValueError:
                    st.error("Invalid JSON rule.")
    reports = call("GET", "/reports?kind=screen&limit=1") or []
    if reports:
        report = reports[0]
        st.caption(f"As of {report['as_of']} | Coverage {report['data']['coverage']:.1%}")
        st.dataframe(report["data"]["candidates"], hide_index=True)
        codes = [c["symbol"] for c in report["data"]["candidates"]]
        selected = st.multiselect("Approve selected candidates", codes)
        name = st.text_input("Approved basket name", "Reviewed candidates")
        if st.button("Create next-session basket", disabled=not selected):
            st.info(call("POST", f"/candidates/{report['id']}/approve", {"name": name, "symbols": selected}))
        with st.expander("Exclusions and provenance"):
            st.json(report["data"])
else:
    kind = st.selectbox("Report type", ["basket", "stock", "screen"])
    reports = call("GET", "/reports?kind=" + kind) or []
    if reports:
        chosen = st.selectbox("Report", reports, format_func=lambda r: f"{r['as_of']} · {r['target']} · #{r['id']}")
        st.json(chosen)
        st.download_button("Export report", json.dumps(chosen, indent=2), "report.json")
    else:
        st.info("No published reports yet. Collection and analysis run in background workers.")
