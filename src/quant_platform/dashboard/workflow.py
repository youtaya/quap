"""API-only Qlib workspaces: explicit intent, provenance, and human acceptance."""

import json
from datetime import datetime, timezone
from urllib.parse import quote, urlencode
from uuid import uuid4

import pandas as pd
import streamlit as st

from quant_platform.domain import CN, digest, symbol
from quant_platform.domain.workflow import PortfolioInput, ScanPolicy

MARKET_REPORT_DISCLAIMER = "本报告为描述性量化统计，不构成投资建议，也不是模型推荐或目标权重。"

WORKSPACES = {
    "Overview": "Are daily and five-minute recommendations ready?",
    "My Model Portfolio": "Review baseline weights and cash, then accept versioned Qlib proposals.",
    "Stock Research": "Inspect model views and prices without implying brokerage holdings.",
    "Low-Price Scan": "Rank low nominal-price stocks using Qlib predictions, not cheapness.",
    "Models & Validation": "Inspect evaluation and shadow evidence before a manual release decision.",
    "Data & Pipeline": "Trace prerequisites, immutable generations, dependencies, and scoped retries.",
}


def request_key(scope, body):
    identity = scope + ":" + digest(body)
    keys = st.session_state.setdefault("workflow_request_keys", {})
    if identity not in keys:
        keys[identity] = str(uuid4())
    return keys[identity]


def request_run(call, frequency, purpose="inference", portfolio_id=None, model_id=None):
    body = {"purpose": purpose, "frequency": frequency, "portfolio_id": portfolio_id, "model_id": model_id}
    body["request_key"] = str(uuid4())
    result = call("POST", "/model-training-runs" if purpose == "training" else "/pipeline-runs", body)
    if result is not None:
        st.success(f"Run {result['run_id']}: {result['state']}. Follow progress in Data & Pipeline.")


def latest(call, frequency, portfolio_id=None):
    params = {"frequency": frequency}
    if portfolio_id:
        params["portfolio_id"] = portfolio_id
    reports = call("GET", "/recommendations?" + urlencode(params))
    return reports[0] if reports else None


def readiness(call):
    data = call("GET", "/data-readiness")
    if not data:
        return
    for col, (frequency, label) in zip(st.columns(2), (("day", "Daily"), ("5min", "Five-minute"))):
        with col:
            state = data["frequencies"][frequency]
            st.metric(label, "Ready" if state["ready"] else "Blocked")
            for blocker in state["blockers"]:
                st.warning(blocker)
            model = state.get("model")
            if model:
                st.caption(f"Release {model['id']} · expires {model.get('expires_at')}")
    research = data.get("research_minutes")
    if research:
        with st.expander("Historical intraday training readiness"):
            st.json(research)
    st.caption("Qlib is required. Missing prerequisites never produce substitute recommendations.")


def show_report(call, table, local_time, report, *, accept_changes=False, portfolio=None):
    if report is None:
        st.info(
            "No valid Qlib proposal. Check Data & Pipeline for prerequisites; legacy reports are not recommendations."
        )
        return
    data = report["data"]
    st.caption(f"{report['frequency']} · model {report['model_id']} · report {report['id']}")
    st.caption(
        f"Feature cutoff: {local_time(report['as_of'])} · prediction available: {local_time(report['available_at'])}"
    )
    st.caption(
        f"Effective: {local_time(report['effective_from'])} · expires: {local_time(report['valid_until'])} (Beijing)"
    )
    st.metric("Proposed cash", f"{data['cash_weight']:.2%}")
    table(data.get("stocks"), columns=["symbol", "score", "baseline_weight", "target_weight", "action", "restrictions"])
    with st.expander("Provenance: source → dataset → model → prediction → evaluation → decision"):
        detail = call("GET", f"/recommendations/{report['id']}")
        if detail is not None:
            st.json(detail)
    if not accept_changes:
        return
    deadline = datetime.fromisoformat(report["effective_from"])
    if not report.get("valid", False) or datetime.now(timezone.utc) >= deadline:
        st.warning("Acceptance is closed. Request a fresh proposal with a future effective time.")
        return
    choices = [
        r["symbol"]
        for r in data.get("stocks", [])
        if r.get("target_weight") is not None and (r["target_weight"] > 0 or r.get("baseline_weight", 0) > 0)
    ]
    with st.form(f"accept_{report['id']}"):
        selected = st.multiselect("Changes to accept", choices, default=choices)
        name = st.text_input(
            "Portfolio name", value=portfolio["name"] if portfolio else "Reviewed Qlib targets", max_chars=120
        )
        st.caption(
            "Selected exact target weights are preserved; unselected baseline weights stay unchanged. Residual allocation stays cash."
        )
        confirmed = st.checkbox("I understand this creates a model-portfolio revision and sends no orders.")
        submitted = st.form_submit_button("Accept selected changes", type="primary")
    if submitted:
        if not confirmed or not selected:
            st.error("Select changes and confirm the model-portfolio-only action.")
            return
        body = {
            "portfolio_id": portfolio["id"] if portfolio else None,
            "name": name,
            "selected_symbols": sorted(selected),
            "expected_revision": portfolio["revision"] if portfolio else 0,
            "expected_policy_revision": report["policy_revision"],
        }
        body["request_key"] = request_key(f"accept:{report['id']}", body)
        result = call("POST", f"/recommendations/{report['id']}/accept", body)
        if result is not None:
            st.session_state.notice = f"Model-portfolio revision {result['revision']} accepted. No orders sent."
            st.rerun()


def portfolios(call, table, local_time):
    saved = call("GET", "/model-portfolios")
    if saved is None:
        return
    choices = {"new": None, **{p["id"]: p for p in saved}}
    selected_id = st.selectbox(
        "Model portfolio",
        list(choices),
        format_func=lambda key: "Create baseline" if key == "new" else choices[key]["name"],
    )
    selected = choices[selected_id]
    source = selected["data"] if selected else {"weights": {}, "cash_weight": 1.0}
    revision = selected["revision"] if selected else 0
    st.caption(
        "These are model target allocations, not brokerage positions. Fractions must sum to 1 including cash; no normalization."
    )
    with st.form(f"portfolio_{selected_id}_{revision}"):
        name = st.text_input("Portfolio name", selected["name"] if selected else "My model portfolio", max_chars=120)
        weights = st.data_editor(
            pd.DataFrame(
                [{"symbol": code, "weight": weight} for code, weight in source["weights"].items()],
                columns=["symbol", "weight"],
            ),
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                "symbol": st.column_config.TextColumn("Symbol", required=True),
                "weight": st.column_config.NumberColumn(
                    "Target fraction", min_value=0.000001, max_value=1.0, required=True, format="%.6f"
                ),
            },
        )
        cash = st.number_input(
            "Cash fraction", min_value=0.0, max_value=1.0, value=float(source["cash_weight"]), format="%.6f"
        )
        submitted = st.form_submit_button("Save next-session baseline", type="primary")
    if submitted:
        try:
            members = {}
            for row in weights.to_dict("records"):
                code = symbol(row["symbol"])
                if code in members:
                    raise ValueError("Duplicate symbol")
                members[code] = row["weight"]
            body = PortfolioInput(name=name, weights=members, cash_weight=cash, expected_revision=revision).model_dump()
        except (ValueError, TypeError):
            st.error("Use unique valid symbols, positive finite weights, and explicit cash totaling 1.")
        else:
            result = call(
                "PUT" if selected else "POST",
                f"/model-portfolios/{selected_id}" if selected else "/model-portfolios",
                body,
            )
            if result is not None:
                st.success(
                    f"Revision {result['revision']} saved; effective {local_time(result['effective_from'])} Beijing. No orders sent."
                )
    if selected:
        st.caption(f"Baseline revision {revision} · effective {local_time(selected['effective_from'])} Beijing")
        if st.button("Request daily diagnosis"):
            request_run(call, "day", portfolio_id=selected_id)
        for frequency, tab in zip(("day", "5min"), st.tabs(["Daily proposal", "Five-minute overlay"])):
            with tab:
                show_report(
                    call,
                    table,
                    local_time,
                    latest(call, frequency, selected_id),
                    accept_changes=True,
                    portfolio=selected,
                )
        with st.expander("Baseline revision history"):
            table(call("GET", f"/model-portfolios/{selected_id}/revisions"))


def market_report(call, table, local_time):
    st.warning(MARKET_REPORT_DISCLAIMER)
    as_of = st.date_input("报告交易日", value=datetime.now(timezone.utc).astimezone(CN).date())
    if st.button("生成/刷新报告"):
        result = call("POST", "/market-report", {"as_of": str(as_of)})
        if result is not None:
            st.success(f"已提交报告任务 #{result['job_id']}，由分析队列计算；完成后刷新本页查看。")
    current = call("GET", "/market-report")
    if not isinstance(current, dict) or not current.get("report"):
        st.info("尚无量化分析报告。点击“生成/刷新报告”提交任务，或在服务器上运行 `quant-platform report`。")
        return
    report = current["report"]
    meta = report.get("meta", {})
    st.caption(
        f"报告 #{current.get('report_id')} · 交易日 {meta.get('session') or meta.get('as_of')} · "
        f"生成于 {local_time(current.get('created_at'))} · 引擎 {current.get('engine') or meta.get('engine')}"
    )
    if current.get("markdown"):
        st.markdown(current["markdown"])
    if report.get("status") != "complete":
        st.info(f"报告状态：{report.get('status')}。{report.get('reason') or ''}")
        return
    st.subheader("因子 Rank IC 证据（前瞻20日，非重叠窗口）")
    table(
        [
            {
                "factor": section.get("label", name),
                "coverage": section.get("coverage"),
                **{key: section["efficacy"].get(key) for key in ("ic_mean", "ic_std", "ic_ir", "ic_positive_ratio")},
                "samples": section["efficacy"].get("samples"),
                "status": section["efficacy"].get("status"),
            }
            for name, section in report.get("factors", {}).items()
        ]
    )
    composite = report.get("composite", {})
    st.subheader("描述性排名组合")
    st.caption(composite.get("label", MARKET_REPORT_DISCLAIMER))
    columns = ["symbol", "name", "board", "close", "score", "momentum_20", "momentum_60_ex_5", "volatility_20"]
    for title, tab in zip(("top", "bottom"), st.tabs(["前20", "后20"])):
        with tab:
            table(composite.get(title), columns=columns)
    with st.expander("原始报告 JSON"):
        st.json(report)


def stocks(call, table, local_time):
    search = st.text_input("Search stocks", max_chars=120)
    table(call("GET", "/instruments?search=" + quote(search.strip(), safe="")))
    saved = call("GET", "/model-portfolios") or []
    contexts = {"standalone": None, **{p["id"]: p for p in saved}}
    context = st.selectbox(
        "Portfolio context",
        list(contexts),
        format_func=lambda key: "Standalone model view" if key == "standalone" else contexts[key]["name"],
    )
    with st.form("stock_research"):
        code = st.text_input("Stock code", key="stock_research_input")
        submitted = st.form_submit_button("View prices and model research")
    if submitted:
        try:
            st.session_state.stock_code = symbol(code)
        except ValueError:
            st.session_state.pop("stock_code", None)
            st.error("Enter a valid Shanghai/Shenzhen stock code.")
    if code := st.session_state.get("stock_code"):
        history = call("GET", f"/history/{quote(code)}?limit=500") or []
        st.caption("Unadjusted CNY prices are display data, not Qlib recommendations or expected returns.")
        if history:
            frame = (
                pd.DataFrame(
                    [
                        {
                            "day": r["day"],
                            **{field: r["data"].get(field) for field in ("open", "high", "low", "close", "volume")},
                        }
                        for r in history
                    ]
                )
                .sort_values("day")
                .set_index("day")
            )
            st.line_chart(frame[["open", "high", "low", "close"]])
            st.bar_chart(frame[["volume"]])
            st.caption("Physical volume in shares. Canonical public-market raw prices; not supplemental data.")
        for frequency, tab in zip(("day", "5min"), st.tabs(["Daily model view", "Five-minute model view"])):
            with tab:
                params = {"frequency": frequency}
                if context != "standalone":
                    params["portfolio_id"] = context
                result = call("GET", f"/recommendations/stock/{code}?" + urlencode(params))
                if result:
                    st.json(result)
                scores = call("GET", f"/research-stocks/{code}/scores?frequency={frequency}&limit=100")
                if isinstance(scores, dict) and scores.get("items"):
                    st.caption(
                        "Historical Qlib scores/ranks with original feature cutoff and observation time; not actionable proposals."
                    )
                    table(scores["items"])
                    st.line_chart(pd.DataFrame(scores["items"]).set_index("feature_cutoff")[["score"]])
                else:
                    st.info("Qlib score history unavailable for this stock and frequency.")
        with st.expander("Supplemental research data · separate from canonical prices"):
            from quant_platform.dashboard.research import snapshot_panel

            snapshot_panel(call, table, "stock:" + code, code=code)
        if st.button("Add to watchlist for next-session pool"):
            watch = call("GET", "/watchlist")
            if watch is not None:
                result = call(
                    "PUT",
                    "/watchlist",
                    {"symbols": sorted(set(watch["symbols"]) | {code}), "expected_revision": watch["revision"]},
                )
                if result is not None:
                    st.success("Watchlist updated. The stock remains warming_up until history is ready.")
    with st.expander("Watchlist"):
        watch = call("GET", "/watchlist")
        if watch is not None:
            text = st.text_area("Symbols separated by commas", value=", ".join(watch["symbols"]))
            if st.button("Save watchlist"):
                result = call(
                    "PUT",
                    "/watchlist",
                    {
                        "symbols": [s.strip() for s in text.split(",") if s.strip()],
                        "expected_revision": watch["revision"],
                    },
                )
                if result is not None:
                    st.success("Watchlist revision saved for the next-session pool.")


def scan(call, table, local_time):
    current = call("GET", "/scan-policy")
    if current is None:
        return
    policy = current["data"]
    st.warning(
        "Low nominal share price does not imply low risk or undervaluation. Scores are model ranks, not probabilities."
    )
    with st.form(f"scan_policy_{current['revision']}"):
        price = st.number_input("Maximum raw price (CNY)", min_value=0.01, value=float(policy["price_ceiling"]))
        turnover = st.number_input(
            "Minimum 20-session turnover (CNY)", min_value=0.0, value=float(policy["minimum_turnover"])
        )
        sessions = st.number_input(
            "Minimum listed sessions", min_value=60, max_value=2000, value=int(policy["minimum_sessions"])
        )
        st.caption(
            "Risk warnings are excluded. Policy changes invalidate prior reports and require a qualified matching release."
        )
        submitted = st.form_submit_button("Save policy revision")
    if submitted:
        try:
            value = ScanPolicy.model_validate(
                {**policy, "price_ceiling": price, "minimum_turnover": turnover, "minimum_sessions": sessions}
            )
        except ValueError as exc:
            st.error(str(exc))
        else:
            result = call(
                "PUT", "/scan-policy", {"policy": value.model_dump(), "expected_revision": current["revision"]}
            )
            if result is not None:
                st.session_state.notice = "Policy revision saved. Qualify a matching model before publication."
                st.rerun()
    if st.button("Run or reuse daily Qlib inference"):
        request_run(call, "day")
    report = latest(call, "day")
    if report:
        st.subheader("Qlib-ranked low-price candidates")
        table(report["data"].get("candidates"))
    show_report(call, table, local_time, report, accept_changes=True)


def models(call, table):
    from quant_platform.dashboard.research import factor_library, experiment_panel

    release, factors, experiments = st.tabs(
        ["Releases & diagnostics", "Factor library", "Experiments & configurations"]
    )
    with release:
        release_models(call, table)
    with factors:
        st.caption("Experimental definitions cannot be accepted as target weights.")
        factor_library(call, table)
    with experiments:
        experiment_panel(call, table)


def release_models(call, table):
    frequency = st.selectbox("Model frequency", ["day", "5min"])
    if st.button("Train challenger"):
        request_run(call, frequency, "training")
    versions = call("GET", "/models") or []
    table(versions, columns=["id", "frequency", "state", "passed", "created_at", "expires_at"])
    choices = [m for m in versions if m["frequency"] == frequency]
    if not choices:
        st.info("No models. Backfill and validate history before training; no sample release is provided.")
        return
    model = st.selectbox("Inspect model", choices, format_func=lambda m: f"{m['id']} · {m['state']}")
    st.json({"evaluation": model["evaluation"], "artifacts": model["metadata"]})
    st.caption(
        "Simulated evaluation is not brokerage P&L. Promotion requires 20 healthy production sessions in the same workflow lineage, this model's own evidence, and freshness."
    )
    table(call("GET", f"/models/{model['id']}/shadow-observations"))
    with st.expander("Realized quality, drift, and training importance"):
        from quant_platform.dashboard.research import diagnostics_panel

        diagnostics_panel(call, table, model["id"])
    if model["state"] == "shadow" and st.button("Observe challenger on latest live cutoff"):
        request_run(call, frequency, "shadow", model_id=model["id"])
    active = next((m for m in versions if m["frequency"] == frequency and m["state"] == "active"), None)
    confirmed = st.checkbox("I reviewed evaluation, lineage evidence, policy, and model age.")
    action = "rollback" if model["state"] == "retired" else "promote"
    if st.button(
        "Rollback approved Qlib release" if action == "rollback" else "Promote Qlib release",
        disabled=not confirmed or model["state"] not in {"shadow", "retired"},
    ):
        result = call(
            "POST", f"/models/{model['id']}/{action}", {"expected_active_id": active["id"] if active else None}
        )
        if result is not None:
            st.session_state.notice = "Qlib release activated. No native fallback is available."
            st.rerun()


def pipeline(call, table, operations):
    from quant_platform.dashboard.research import source_panels

    with st.expander("Research sources, collection, and quality center"):
        source_panels(call, table)
    readiness(call)
    runs = call("GET", "/pipeline-runs") or []
    table(runs, columns=["id", "frequency", "purpose", "as_of", "state", "error"])
    if runs:
        chosen = st.selectbox("Pipeline run", runs, format_func=lambda r: f"{r['id']} · {r['state']}")
        detail = call("GET", f"/pipeline-runs/{chosen['id']}")
        if detail:
            table(detail["steps"])
            with st.expander("Pinned input snapshot and step results"):
                st.json(detail)
        retryable = chosen["state"] in {"blocked", "failed"} and not (
            chosen["frequency"] == "5min" and chosen["purpose"] in {"inference", "shadow"}
        )
        if st.button("Retry same immutable inputs", disabled=not retryable):
            result = call("POST", f"/pipeline-runs/{chosen['id']}/retry")
            if result is not None:
                st.success("Retry requested. Changed inputs require a new run.")
    with st.expander("Immutable Qlib generations"):
        table(call("GET", "/qlib-generations"))
    with st.expander("Collection, permissions, jobs, and recovery"):
        operations()
    with st.expander("Legacy native reports · read-only archive"):
        st.warning("legacy_native: excluded from current recommendations and acceptance.")
        reports = call("GET", "/reports?limit=100") or []
        if reports:
            chosen = st.selectbox(
                "Legacy report", reports, format_func=lambda r: f"{r['as_of']} · {r['kind']} · {r['id']}"
            )
            st.json(chosen)
            st.download_button(
                "Export legacy report",
                json.dumps(chosen, ensure_ascii=False, indent=2),
                "legacy-report.json",
                "application/json",
            )


def render(page, call, table, local_time, operations):
    if page == "Overview":
        readiness(call)
        for frequency, tab in zip(("day", "5min"), st.tabs(["Daily", "Five-minute"])):
            with tab:
                show_report(call, table, local_time, latest(call, frequency))
        operations(compact=True)
    elif page == "My Model Portfolio":
        portfolios(call, table, local_time)
    elif page == "Stock Research":
        report_tab, model_tab = st.tabs(["量化分析报告", "Model views"])
        with report_tab:
            market_report(call, table, local_time)
        with model_tab:
            stocks(call, table, local_time)
    elif page == "Low-Price Scan":
        scan(call, table, local_time)
    elif page == "Models & Validation":
        models(call, table)
    else:
        pipeline(call, table, operations)
