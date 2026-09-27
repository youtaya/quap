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
QLIB_REPORT_DISCLAIMER = "Qlib 研究报告为描述性统计，不构成投资建议，也不是目标权重；平台不执行交易。"

# 工作空间的**键**是路由标识符：侧边栏单选把它交给 ``render``，``render`` 再拿它做 ``page ==``
# 比较。所以键保持英文不动，``WORKSPACE_LABELS`` 是显示名，``WORKSPACES`` 是标题下的一句话说明。
WORKSPACES = {
    "Overview": "今天能不能出建议，以及为什么。",
    "My Model Portfolio": "查看基线权重与现金，采纳版本化的 Qlib 建议。",
    "Stock Research": "查看行情与模型视角，不代表券商持仓。",
    "Qlib Report": "因子 IC、前瞻收益分位与相对基准的等权组合。",
    "Low-Price Scan": "用 Qlib 预测给低价股排序，而不是按便宜程度。",
    "Models & Validation": "在人工发布前查看评估与影子证据。",
    "Data & Pipeline": "追溯前置条件、不可变代次、依赖关系与定向重试。",
}

WORKSPACE_LABELS = {
    "Overview": "今日",
    "My Model Portfolio": "我的组合",
    "Stock Research": "个股研究",
    "Qlib Report": "研究报告",
    "Low-Price Scan": "选股发现",
    "Models & Validation": "模型与验证",
    "Data & Pipeline": "运行记录与配置",
}

# 频率与状态的取值来自 API，出现在表头和行内文案里；集中在这里，供仪表盘与表格共用。
FREQUENCIES = {"day": "日线", "5min": "五分钟"}
STATES = {
    "pending": "等待执行",
    "running": "执行中",
    "completed": "已完成",
    "complete": "已完成",
    "done": "已完成",
    "failed": "执行失败",
    "blocked": "受阻",
    "shadow": "影子观察",
    "active": "已发布",
    "retired": "已退役",
    "succeeded": "已成功",
    "rejected": "未通过",
    "quarantined": "已隔离",
    "available": "可用",
    "unavailable": "不可用",
    "ready": "已就绪",
    "frozen": "已冻结",
    "cancelled": "已取消",
}


def _decimal(value, digits=4):
    return "—" if value is None else f"{value:.{digits}f}"


def _percent(value):
    return "—" if value is None else f"{value * 100:.2f}%"


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
        st.success(f"任务 {result['run_id']} 已提交。进度见「运行记录与配置」。")


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
    for col, (frequency, label) in zip(st.columns(2), (("day", "日线"), ("5min", "五分钟"))):
        with col:
            state = data["frequencies"][frequency]
            st.metric(label, "已就绪" if state["ready"] else "受阻")
            for blocker in state["blockers"]:
                st.warning(blocker)
            model = state.get("model")
            if model:
                st.caption(f"发布版本 {model['id']} · 过期时间 {model.get('expires_at')}")
    research = data.get("research_minutes")
    if research:
        with st.expander("历史盘中训练就绪情况"):
            st.json(research)
    st.caption("本平台依赖 Qlib；缺少前置条件时不会生成替代建议。")


def show_report(call, table, local_time, report, *, accept_changes=False, portfolio=None):
    if report is None:
        st.info("暂无有效的 Qlib 建议。请到「运行记录与配置」查看前置条件；历史报告不是建议。")
        return
    data = report["data"]
    frequency = FREQUENCIES.get(report["frequency"], report["frequency"])
    st.caption(f"{frequency} · 模型 {report['model_id']} · 报告 {report['id']}")
    st.caption(f"特征截点：{local_time(report['as_of'])} · 预测可用时间：{local_time(report['available_at'])}")
    st.caption(
        f"生效时间：{local_time(report['effective_from'])} · 失效时间：{local_time(report['valid_until'])}（北京时间）"
    )
    st.metric("建议现金比例", f"{data['cash_weight']:.2%}")
    table(data.get("stocks"), columns=["symbol", "score", "baseline_weight", "target_weight", "action", "restrictions"])
    with st.expander("溯源链路：数据源 → 数据集 → 模型 → 预测 → 评估 → 决策"):
        detail = call("GET", f"/recommendations/{report['id']}")
        if detail is not None:
            st.json(detail)
    if not accept_changes:
        return
    deadline = datetime.fromisoformat(report["effective_from"])
    if not report.get("valid", False) or datetime.now(timezone.utc) >= deadline:
        st.warning("采纳窗口已关闭。请重新生成一个生效时间在未来的建议。")
        return
    choices = [
        r["symbol"]
        for r in data.get("stocks", [])
        if r.get("target_weight") is not None and (r["target_weight"] > 0 or r.get("baseline_weight", 0) > 0)
    ]
    with st.form(f"accept_{report['id']}"):
        selected = st.multiselect("本次采纳的调整", choices, default=choices)
        name = st.text_input(
            "组合名称", value=portfolio["name"] if portfolio else "已复核的 Qlib 目标", max_chars=120
        )
        st.caption("所选标的按精确目标权重写入；未选中的标的保持基线权重不变。剩余部分留作现金。")
        confirmed = st.checkbox("我了解：这只会生成一个模型组合版本，不会发送任何委托。")
        submitted = st.form_submit_button("采纳所选调整", type="primary")
    if submitted:
        if not confirmed or not selected:
            st.error("请选择要采纳的调整，并确认该操作仅作用于模型组合。")
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
            st.session_state.notice = f"已采纳模型组合版本 {result['revision']}，未发送任何委托。"
            st.rerun()


def portfolios(call, table, local_time):
    saved = call("GET", "/model-portfolios")
    if saved is None:
        return
    choices = {"new": None, **{p["id"]: p for p in saved}}
    selected_id = st.selectbox(
        "模型组合",
        list(choices),
        format_func=lambda key: "新建基线" if key == "new" else choices[key]["name"],
    )
    selected = choices[selected_id]
    source = selected["data"] if selected else {"weights": {}, "cash_weight": 1.0}
    revision = selected["revision"] if selected else 0
    st.caption("这些是模型目标配置，不是券商持仓。各比例含现金在内必须合计为 1，平台不做归一化。")
    with st.form(f"portfolio_{selected_id}_{revision}"):
        name = st.text_input("组合名称", selected["name"] if selected else "我的模型组合", max_chars=120)
        weights = st.data_editor(
            pd.DataFrame(
                [{"symbol": code, "weight": weight} for code, weight in source["weights"].items()],
                columns=["symbol", "weight"],
            ),
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                "symbol": st.column_config.TextColumn("股票代码", required=True),
                "weight": st.column_config.NumberColumn(
                    "目标权重", min_value=0.000001, max_value=1.0, required=True, format="%.6f"
                ),
            },
        )
        cash = st.number_input(
            "现金比例", min_value=0.0, max_value=1.0, value=float(source["cash_weight"]), format="%.6f"
        )
        submitted = st.form_submit_button("保存为下一交易日基线", type="primary")
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
            st.error("请使用唯一且有效的股票代码、正的有限权重，并显式填写合计为 1 的现金比例。")
        else:
            result = call(
                "PUT" if selected else "POST",
                f"/model-portfolios/{selected_id}" if selected else "/model-portfolios",
                body,
            )
            if result is not None:
                st.success(
                    f"版本 {result['revision']} 已保存，自 {local_time(result['effective_from'])}（北京时间）生效。"
                    "未发送任何委托。"
                )
    if selected:
        st.caption(f"基线版本 {revision} · 自 {local_time(selected['effective_from'])}（北京时间）生效")
        if st.button("请求日线诊断"):
            request_run(call, "day", portfolio_id=selected_id)
        for frequency, tab in zip(("day", "5min"), st.tabs(["日线建议", "五分钟叠加"])):
            with tab:
                show_report(
                    call,
                    table,
                    local_time,
                    latest(call, frequency, selected_id),
                    accept_changes=True,
                    portfolio=selected,
                )
        with st.expander("基线版本历史"):
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
        st.info("尚无量化分析报告。点击「生成/刷新报告」提交任务，或在服务器上运行 `quant-platform report`。")
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
        st.info(f"报告状态：{STATES.get(report.get('status'), report.get('status'))}。{report.get('reason') or ''}")
        return
    st.subheader("因子 Rank IC 证据（前瞻 20 日，非重叠窗口）")
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
    for title, tab in zip(("top", "bottom"), st.tabs(["前 20", "后 20"])):
        with tab:
            table(composite.get(title), columns=columns)
    with st.expander("原始报告 JSON"):
        st.json(report)


def qlib_report(call, table, local_time):
    """Rendered Qlib research evidence: IC, quantiles and the benchmark-relative book."""
    st.warning(QLIB_REPORT_DISCLAIMER)
    current = call("GET", "/qlib-report")
    if not isinstance(current, dict) or not current.get("report"):
        st.info("尚无 Qlib 研究报告。日报数据就绪并完成一次不可变代次导出后，系统会自动排队报告任务。")
        return
    summary = current.get("summary") or {}
    book = summary.get("book") or {}
    st.caption(
        f"报告 #{current.get('report_id')} · 数据日期 {current.get('as_of')} · 基准 {current.get('target')} · "
        f"引擎 {current.get('engine')}"
    )
    daily = summary.get("daily_ic") or {}
    columns = st.columns(5)
    columns[0].metric("IC", _decimal(summary.get("ic")))
    columns[1].metric("Rank IC", _decimal(summary.get("rank_ic")))
    columns[2].metric("日均 IC", _decimal(daily.get("mean")))
    columns[3].metric("相对基准收益", _percent(book.get("relative_return")))
    columns[4].metric("有效样本", f"{summary.get('samples') or 0:,}")

    st.subheader("五分位平均未来 5 日收益")
    quantiles = summary.get("quantiles") or []
    if quantiles:
        st.bar_chart(
            pd.DataFrame(
                [{"分位": f"Q{row['bucket'] + 1}", "平均未来收益": row["mean_forward"]} for row in quantiles]
            ).set_index("分位")
        )
        table(
            [{"bucket": f"Q{row['bucket'] + 1}", **row} for row in quantiles],
            columns=["bucket", "count", "mean_forward"],
        )
    else:
        st.info("样本不足，无法分层。")

    st.subheader("相对基准的等权组合")
    if book.get("status") == "complete":
        st.caption(
            f"每 20 个交易日再平衡，取动量前 30 只等权；已扣除佣金、印花税与滑点。调仓 {book['rebalances']} 次，"
            f"平均单边换手 {_percent(book.get('mean_turnover'))}。"
        )
        comparison = pd.DataFrame(
            {
                "累计收益": {
                    "组合（扣费）": book.get("cumulative_return"),
                    f"基准 {current.get('target')}": book.get("benchmark_cumulative_return"),
                }
            }
        )
        st.bar_chart(comparison)
        rows = current["report"].get("book", {}).get("periods", [])
        # The book reuses `turnover` for the one-sided portfolio turnover ratio, while the field
        # means CNY traded value everywhere else, so it is relabelled here instead of in LABELS.
        table(
            rows,
            columns=["day", "next_day", "holdings", "net_return", "benchmark_return", "turnover"],
            labels={"turnover": "单边换手"},
        )
    else:
        st.info("历史长度不足以完成一次完整调仓，组合收益未计算。")
    if current.get("markdown"):
        with st.expander("Markdown 报告"):
            st.markdown(current["markdown"])
    with st.expander("原始报告 JSON"):
        st.json(current["report"])


def stocks(call, table, local_time):
    search = st.text_input("搜索股票", max_chars=120)
    table(call("GET", "/instruments?search=" + quote(search.strip(), safe="")))
    saved = call("GET", "/model-portfolios") or []
    contexts = {"standalone": None, **{p["id"]: p for p in saved}}
    context = st.selectbox(
        "组合上下文",
        list(contexts),
        format_func=lambda key: "独立模型视角" if key == "standalone" else contexts[key]["name"],
    )
    with st.form("stock_research"):
        code = st.text_input("股票代码", key="stock_research_input")
        submitted = st.form_submit_button("查看行情与模型研究")
    if submitted:
        try:
            st.session_state.stock_code = symbol(code)
        except ValueError:
            st.session_state.pop("stock_code", None)
            st.error("请输入有效的沪市/深市股票代码。")
    if code := st.session_state.get("stock_code"):
        history = call("GET", f"/history/{quote(code)}?limit=500") or []
        st.caption("未复权的元价格属于展示数据，不是 Qlib 建议或预期收益。")
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
            st.caption("成交量为股数。这是公开市场的原始价格，不是补充数据源。")
        for frequency, tab in zip(("day", "5min"), st.tabs(["日线模型视角", "五分钟模型视角"])):
            with tab:
                params = {"frequency": frequency}
                if context != "standalone":
                    params["portfolio_id"] = context
                result = call("GET", f"/recommendations/stock/{code}?" + urlencode(params))
                if result:
                    st.json(result)
                scores = call("GET", f"/research-stocks/{code}/scores?frequency={frequency}&limit=100")
                if isinstance(scores, dict) and scores.get("items"):
                    st.caption("历史 Qlib 得分与排名，附原始特征截点与观测时间；不是可执行建议。")
                    table(scores["items"])
                    st.line_chart(pd.DataFrame(scores["items"]).set_index("feature_cutoff")[["score"]])
                else:
                    st.info("该股票在该频率下暂无 Qlib 得分历史。")
        with st.expander("补充研究数据 · 与规范价格相互独立"):
            from quant_platform.dashboard.research import snapshot_panel

            snapshot_panel(call, table, "stock:" + code, code=code)
        if st.button("加入观察池（供下一交易日使用）"):
            watch = call("GET", "/watchlist")
            if watch is not None:
                result = call(
                    "PUT",
                    "/watchlist",
                    {"symbols": sorted(set(watch["symbols"]) | {code}), "expected_revision": watch["revision"]},
                )
                if result is not None:
                    st.success("观察池已更新。历史数据就绪前，该股票保持预热状态。")
    with st.expander("观察池"):
        watch = call("GET", "/watchlist")
        if watch is not None:
            text = st.text_area("股票代码（逗号分隔）", value=", ".join(watch["symbols"]))
            if st.button("保存观察池"):
                result = call(
                    "PUT",
                    "/watchlist",
                    {
                        "symbols": [s.strip() for s in text.split(",") if s.strip()],
                        "expected_revision": watch["revision"],
                    },
                )
                if result is not None:
                    st.success("观察池版本已保存，供下一交易日使用。")


def scan(call, table, local_time):
    current = call("GET", "/scan-policy")
    if current is None:
        return
    policy = current["data"]
    st.warning("名义股价低不代表风险低或估值低。得分是模型排名，不是概率。")
    with st.form(f"scan_policy_{current['revision']}"):
        price = st.number_input("最高原始股价（元）", min_value=0.01, value=float(policy["price_ceiling"]))
        turnover = st.number_input(
            "20 日最小成交额（元）", min_value=0.0, value=float(policy["minimum_turnover"])
        )
        sessions = st.number_input(
            "最小上市交易日数", min_value=60, max_value=2000, value=int(policy["minimum_sessions"])
        )
        st.caption("风险警示股票已被排除。策略变更会使既有报告失效，并要求匹配一个已合格的发布版本。")
        submitted = st.form_submit_button("保存策略版本")
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
                st.session_state.notice = "策略版本已保存。发布前请先合格化一个匹配的模型。"
                st.rerun()
    if st.button("运行或复用当日 Qlib 推理"):
        request_run(call, "day")
    report = latest(call, "day")
    if report:
        st.subheader("Qlib 排序的低价候选")
        table(report["data"].get("candidates"))
    show_report(call, table, local_time, report, accept_changes=True)


def models(call, table):
    from quant_platform.dashboard.research import factor_library, experiment_panel

    release, factors, experiments = st.tabs(["发布与诊断", "因子库", "实验与配置"])
    with release:
        release_models(call, table)
    with factors:
        st.caption("实验性定义不能被采纳为目标权重。")
        factor_library(call, table)
    with experiments:
        experiment_panel(call, table)


def release_models(call, table):
    frequency = st.selectbox(
        "模型频率", ["day", "5min"], format_func=lambda value: FREQUENCIES.get(value, value)
    )
    if st.button("训练候选模型"):
        request_run(call, frequency, "training")
    versions = call("GET", "/models") or []
    table(versions, columns=["id", "frequency", "state", "passed", "created_at", "expires_at"])
    choices = [m for m in versions if m["frequency"] == frequency]
    if not choices:
        st.info("暂无模型。训练前请先补齐并验证历史数据；平台不提供示例发布版本。")
        return
    model = st.selectbox(
        "查看模型详情", choices, format_func=lambda m: f"{m['id']} · {STATES.get(m['state'], m['state'])}"
    )
    st.json({"evaluation": model["evaluation"], "artifacts": model["metadata"]})
    st.caption(
        "模拟评估不等于券商盈亏。晋级需要在同一工作流谱系中积累 20 个健康的生产交易日、该模型自身的证据，"
        "以及时效性。"
    )
    table(call("GET", f"/models/{model['id']}/shadow-observations"))
    with st.expander("实现质量、漂移与训练重要性"):
        from quant_platform.dashboard.research import diagnostics_panel

        diagnostics_panel(call, table, model["id"])
    if model["state"] == "shadow" and st.button("在最新实盘截点上观察候选模型"):
        request_run(call, frequency, "shadow", model_id=model["id"])
    active = next((m for m in versions if m["frequency"] == frequency and m["state"] == "active"), None)
    confirmed = st.checkbox("我已复核评估、谱系证据、策略与模型时效。")
    action = "rollback" if model["state"] == "retired" else "promote"
    if st.button(
        "回滚已发布的 Qlib 版本" if action == "rollback" else "发布 Qlib 版本",
        disabled=not confirmed or model["state"] not in {"shadow", "retired"},
    ):
        result = call(
            "POST", f"/models/{model['id']}/{action}", {"expected_active_id": active["id"] if active else None}
        )
        if result is not None:
            st.session_state.notice = "Qlib 发布版本已激活。平台没有原生替代方案。"
            st.rerun()


def pipeline(call, table, operations):
    from quant_platform.dashboard.research import source_panels

    with st.expander("研究数据源、采集与质量中心"):
        source_panels(call, table)
    readiness(call)
    runs = call("GET", "/pipeline-runs") or []
    table(runs, columns=["id", "frequency", "purpose", "as_of", "state", "error"])
    if runs:
        chosen = st.selectbox(
            "流水线运行", runs, format_func=lambda r: f"{r['id']} · {STATES.get(r['state'], r['state'])}"
        )
        detail = call("GET", f"/pipeline-runs/{chosen['id']}")
        if detail:
            table(detail["steps"])
            with st.expander("已固定的输入快照与步骤结果"):
                st.json(detail)
        retryable = chosen["state"] in {"blocked", "failed"} and not (
            chosen["frequency"] == "5min" and chosen["purpose"] in {"inference", "shadow"}
        )
        if st.button("用相同输入重试", disabled=not retryable):
            result = call("POST", f"/pipeline-runs/{chosen['id']}/retry")
            if result is not None:
                st.success("已请求重试。输入发生变化时请新建一次运行。")
    with st.expander("不可变 Qlib 数据代次"):
        table(
            call("GET", "/qlib-generations"),
            columns=["id", "frequency", "origin", "watermark", "as_of", "contract", "path", "created_at"],
        )
    with st.expander("采集、权限、任务与恢复"):
        operations()
    with st.expander("历史原生报告 · 只读归档"):
        st.warning("legacy_native：不纳入当前建议与采纳流程。")
        reports = call("GET", "/reports?limit=100") or []
        if reports:
            chosen = st.selectbox(
                "历史报告", reports, format_func=lambda r: f"{r['as_of']} · {r['kind']} · {r['id']}"
            )
            st.json(chosen)
            st.download_button(
                "导出历史报告",
                json.dumps(chosen, ensure_ascii=False, indent=2),
                "legacy-report.json",
                "application/json",
            )


def render(page, call, table, local_time, operations):
    if page == "Overview":
        readiness(call)
        for frequency, tab in zip(("day", "5min"), st.tabs(["日线", "五分钟"])):
            with tab:
                show_report(call, table, local_time, latest(call, frequency))
        operations(compact=True)
    elif page == "My Model Portfolio":
        portfolios(call, table, local_time)
    elif page == "Stock Research":
        report_tab, model_tab = st.tabs(["量化分析报告", "模型视角"])
        with report_tab:
            market_report(call, table, local_time)
        with model_tab:
            stocks(call, table, local_time)
    elif page == "Qlib Report":
        qlib_report(call, table, local_time)
    elif page == "Low-Price Scan":
        scan(call, table, local_time)
    elif page == "Models & Validation":
        models(call, table)
    else:
        pipeline(call, table, operations)
