"""Streamlit display/control client for the independent platform API."""

import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx
import pandas as pd
import streamlit as st

from quant_platform.dashboard.client import Client

st.set_page_config(
    page_title="Quant Platform",
    page_icon="📈",
    layout="wide",
    menu_items={"About": "A-share Quant Platform · Entitled market data · Versioned baskets · No order execution"},
)

REQUIRED_ENDPOINTS = ("stock_basic", "trade_cal", "daily", "adj_factor", "rt_k")
WORKSPACES = {
    "Operations": "🛰️ Operations",
    "Baskets": "🧺 Baskets",
    "Stocks": "📈 Stocks",
    "Candidates": "🔎 Candidates",
    "Reports": "🗂️ Reports",
    "Research": "🧪 Research",
}

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.4rem; padding-bottom: 3rem; max-width: 1500px;}
      #MainMenu, footer {visibility: hidden;}
      .qp-header {
        display: flex; align-items: baseline; gap: .8rem; flex-wrap: wrap;
        padding: .2rem 0 .1rem 0; border-bottom: 1px solid rgba(128,128,128,.18); margin-bottom: 1rem;
      }
      .qp-title {font-size: 1.7rem; font-weight: 700; letter-spacing: -.01em;}
      .qp-tag {color: var(--text-color, #8a8f98); font-size: .9rem;}
      .qp-ribbon {display: flex; flex-wrap: wrap; gap: .45rem; margin: .1rem 0 1rem 0;}
      .qp-pill {
        display: inline-flex; align-items: center; gap: .35rem;
        padding: .22rem .6rem; border-radius: 999px; font-size: .8rem; font-weight: 600;
        border: 1px solid transparent; white-space: nowrap;
      }
      .qp-ok   {background: rgba(34,197,94,.14);  color: #15803d; border-color: rgba(34,197,94,.35);}
      .qp-warn {background: rgba(245,158,11,.15); color: #b45309; border-color: rgba(245,158,11,.38);}
      .qp-bad  {background: rgba(239,68,68,.14);  color: #b91c1c; border-color: rgba(239,68,68,.38);}
      .qp-info {background: rgba(59,130,246,.13); color: #1d4ed8; border-color: rgba(59,130,246,.35);}
      .qp-dot {height: .5rem; width: .5rem; border-radius: 50%; display: inline-block; background: currentColor;}
      div[data-testid="stMetric"] {
        background: rgba(128,128,128,.06); border: 1px solid rgba(128,128,128,.16);
        border-radius: 12px; padding: .7rem .9rem;
      }
      section[data-testid="stSidebar"] .qp-brand {font-weight: 700; font-size: 1.05rem; margin-bottom: .1rem;}
      .qp-muted {color: #8a8f98; font-size: .82rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


def pill(label, tone="info"):
    return f'<span class="qp-pill qp-{tone}"><span class="qp-dot"></span>{label}</span>'


def ribbon(pills):
    st.markdown('<div class="qp-ribbon">' + "".join(pills) + "</div>", unsafe_allow_html=True)


st.markdown(
    '<div class="qp-header"><span class="qp-title">A-share Quant Platform</span>'
    '<span class="qp-tag">Continuous research · Entitled market data · Versioned baskets · No order execution</span>'
    "</div>",
    unsafe_allow_html=True,
)

if "token" not in st.session_state:
    left, center, right = st.columns([1, 1.3, 1])
    with center:
        st.subheader("Operator sign-in")
        st.caption("Enter the operator token from your `api_token` secret. Do not enter the Tushare data token.")
        with st.form("login"):
            token = st.text_input("Operator token", type="password", placeholder="Operator token")
            submitted = st.form_submit_button("Sign in", use_container_width=True)
        if submitted:
            probe = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), token)
            try:
                probe.request("GET", "/status")
                st.session_state.token = token
                st.rerun()
            except (ValueError, httpx.HTTPError):
                st.error("Authentication failed or backend unavailable.")
        st.caption("Browser clients never own market jobs; collection runs in background workers.")
    st.stop()

client = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), st.session_state.token)

with st.sidebar:
    st.markdown('<div class="qp-brand">📈 Quant Platform</div>', unsafe_allow_html=True)
    st.markdown('<div class="qp-muted">Operator console</div>', unsafe_allow_html=True)
    st.divider()
    page = st.radio("Workspace", list(WORKSPACES), format_func=lambda key: WORKSPACES[key])
    st.divider()
    st.caption("Operations auto-refreshes every 10s.")
    if st.button("Sign out", use_container_width=True):
        del st.session_state.token
        st.rerun()


def call(method, path, data=None):
    try:
        return client.request(method, path, data)
    except (httpx.HTTPError, ValueError) as exc:
        st.error(str(exc) if isinstance(exc, ValueError) else "Backend disconnected. Retained data is not live.")
        return None


def local_time(iso_timestamp):
    if not iso_timestamp:
        return "—"
    try:
        moment = datetime.fromisoformat(iso_timestamp).astimezone(timezone.utc)
    except ValueError:
        return iso_timestamp
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def status_ribbon(status):
    health = status["data_health"]
    health_tone = "ok" if health == "fresh" else "bad"
    ribbon(
        [
            pill("Backend online", "ok"),
            pill(f"Provider: {status['provider']}", "info" if status.get("configured") else "warn"),
            pill(f"Data {health.replace('_', ' ')}", health_tone),
            pill(
                "Polling paused" if status.get("polling_paused") else "Polling active",
                "warn" if status.get("polling_paused") else "ok",
            ),
            pill(f"Fresh {status['fresh_quote_count']}/{status['quote_count']} quotes", "info"),
        ]
    )
    available = {
        c["endpoint"]
        for c in status.get("capabilities", [])
        if c.get("status") == "reachable" and (c.get("data") or {}).get("schema_verified")
    }
    missing = [endpoint for endpoint in REQUIRED_ENDPOINTS if endpoint not in available]
    capability_pill = (
        pill("Required capabilities verified", "ok") if not missing else pill("Missing: " + ", ".join(missing), "warn")
    )
    ribbon([capability_pill, pill(f"As of {local_time(status.get('timestamp'))}", "info")])


@st.fragment(run_every=10)
def operations():
    status = call("GET", "/status")
    if status is None:
        return
    status_ribbon(status)
    if status["data_health"] != "fresh":
        st.warning("Data is degraded or unavailable. Process health does not establish market-data freshness.")
    ages = status.get("metrics", {}).get("source_age_seconds", {}) or {}
    a, b, c, d = st.columns(4)
    a.metric("Stored quotes", status["quote_count"])
    b.metric("Fresh quotes", status["fresh_quote_count"], help="Quotes with a recent, trading-session source time.")
    c.metric("Provider", status["provider"], help="Only entitled Tushare data; no public/demo fallback.")
    newest = ages.get("minimum")
    d.metric(
        "Newest source age",
        f"{newest:.0f}s" if isinstance(newest, (int, float)) else "—",
        help="Seconds since the freshest stored quote's source time.",
    )
    st.caption(f"Analysis target: {status['analysis_target']} · Polling paused: {status['polling_paused']}")

    st.subheader("Market board coverage")
    st.dataframe(
        [
            {k: b.get(k) for k in ("board", "listed", "stored", "fresh", "coverage", "daily_return")}
            for b in status.get("boards", [])
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.caption("Equal-weight eligible stocks, not official exchange indexes. Missing weight is disclosed.")

    left, right = st.columns(2)
    with left:
        st.subheader("Service heartbeats")
        st.dataframe(status["services"], hide_index=True, use_container_width=True)
    with right:
        st.subheader("Provider capabilities")
        st.dataframe(status["capabilities"], hide_index=True, use_container_width=True)

    st.subheader("Operator controls")
    for col, action in zip(st.columns(6), ("pause", "resume", "refresh", "analyze", "research", "doctor")):
        if col.button(action.title(), use_container_width=True):
            result = call("POST", "/control", {"action": action})
            if result:
                st.info(result)

    st.subheader("Durable jobs")
    st.dataframe(status["jobs"], hide_index=True, use_container_width=True)
    with st.expander("Progress, failures and job IDs"):
        jobs = call("GET", "/jobs") or []
        st.dataframe(jobs, hide_index=True, use_container_width=True)
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

    st.subheader("Data quality")
    st.caption("Missing bars, stale quotes, factor revisions and blocked capabilities. Separate from container health.")
    quality = status.get("data_quality") or {}
    q1, q2, q3 = st.columns(3)
    q1.metric("Stale quotes", quality.get("stale_or_missing_quote_count", "—"))
    q2.metric("Missing latest bars", quality.get("listed_missing_latest_completed_bar", "—"))
    q3.metric("Factor revisions", quality.get("factor_revision_keys", "—"))
    st.dataframe(quality.get("blocked_capabilities") or [], hide_index=True, use_container_width=True)
    st.subheader("Persistent alerts")
    st.dataframe(call("GET", "/alerts") or [], hide_index=True, use_container_width=True)


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


def research_workspace():
    st.caption("Research path and factor diagnostics. research_value is not NAV. This screen does not submit orders.")
    factor = call("GET", "/reports?kind=factor&limit=1") or []
    backtest = call("GET", "/reports?kind=backtest&limit=1") or []
    if not factor and not backtest:
        st.info("No research reports yet. Collection and the research worker publish them after a pinned daily run.")
        return
    if backtest:
        data = backtest[0]["data"]
        st.metric("Research hash", str(data.get("research_hash", ""))[:12] or "—")
        st.write(data.get("label", "Research path value, not NAV"))
        st.dataframe(data.get("candidates") or [], hide_index=True, use_container_width=True)
        st.json(
            {
                "execution": data.get("execution"),
                "limit_check": data.get("limit_check"),
                "research_value": data.get("research_value"),
            }
        )
    if factor:
        st.subheader("Factor diagnostics")
        st.json(factor[0]["data"])


if page == "Operations":
    operations()
elif page == "Research":
    research_workspace()
elif page == "Baskets":
    baskets = call("GET", "/baskets") or []
    ribbon(
        [
            pill(f"{len(baskets)} baskets", "info"),
            (
                pill(f"{sum(1 for b in baskets if b.get('paused'))} paused", "warn")
                if any(b.get("paused") for b in baskets)
                else pill("None paused", "ok")
            ),
        ]
    )
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
            if col.button(action.title(), use_container_width=True):
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
        st.dataframe(
            call("GET", f"/baskets/{selected['id']}/revisions") or [], hide_index=True, use_container_width=True
        )
elif page == "Stocks":
    search = st.text_input("Search symbol or company")
    st.dataframe(call("GET", "/instruments?search=" + quote(search)) or [], hide_index=True, use_container_width=True)
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
        st.dataframe(report["data"]["candidates"], hide_index=True, use_container_width=True)
        codes = [c["symbol"] for c in report["data"]["candidates"]]
        selected = st.multiselect("Approve selected candidates", codes)
        name = st.text_input("Approved basket name", "Reviewed candidates")
        if st.button("Create next-session basket", disabled=not selected):
            st.info(call("POST", f"/candidates/{report['id']}/approve", {"name": name, "symbols": selected}))
        with st.expander("Exclusions and provenance"):
            st.json(report["data"])
else:
    kind = st.selectbox("Report type", ["basket", "stock", "screen", "factor", "backtest"])
    reports = call("GET", "/reports?kind=" + kind) or []
    if reports:
        chosen = st.selectbox("Report", reports, format_func=lambda r: f"{r['as_of']} · {r['target']} · #{r['id']}")
        st.json(chosen)
        st.download_button("Export report", json.dumps(chosen, indent=2), "report.json")
    else:
        st.info("No published reports yet. Collection and analysis run in background workers.")
