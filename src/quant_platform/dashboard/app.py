"""Streamlit display/control client for the independent platform API."""

import os
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import streamlit as st

from quant_platform.dashboard.client import Client
from quant_platform.dashboard.workflow import WORKSPACES as DESCRIPTIONS, render

st.set_page_config(
    page_title="QUAP · 知衡量化",
    page_icon="📊",
    layout="wide",
    menu_items={"About": "QUAP · 知衡量化研究工作台｜授权行情 · 版本化组合 · 不执行交易"},
)

REQUIRED_ENDPOINTS = ("stock_basic", "trade_cal", "daily", "adj_factor", "rt_k")
WORKSPACES = {name: name for name in DESCRIPTIONS}
BOARDS = {"STAR Market": "科创板", "ChiNext": "创业板", "Main Board": "沪深主板"}
KINDS = {"basket": "组合分析", "stock": "个股分析", "screen": "选股结果", "factor": "因子诊断", "backtest": "研究路径"}
LABELS = {
    "symbol": "股票代码",
    "name": "名称",
    "board": "所属板块",
    "status": "状态",
    "list_date": "上市日期",
    "day": "交易日",
    "close": "收盘价（元）",
    "open": "开盘价（元）",
    "high": "最高价（元）",
    "low": "最低价（元）",
    "volume": "成交量（股）",
    "turnover": "成交额（元）",
    "score": "综合得分",
    "momentum": "动量",
    "volatility": "波动率",
    "coverage": "有效覆盖率",
    "daily_return": "当日涨跌幅",
    "role": "服务",
    "healthy": "心跳正常",
    "updated_at": "更新时间（北京）",
    "endpoint": "数据接口",
    "queue": "任务队列",
    "count": "数量",
    "id": "编号",
    "kind": "类型",
    "revision": "版本",
    "effective_day": "生效交易日",
    "created_at": "创建时间（北京）",
    "recorded_at": "记录时间（北京）",
    "error": "异常信息",
    "failures": "失败次数",
    "progress": "进度",
    "priority": "优先级",
    "available_at": "执行时间（北京）",
    "target": "研究对象",
    "as_of": "数据日期",
    "data": "详细数据",
    "owner": "实例",
    "source_time": "行情时间（北京）",
    "reference_proxy": "日内参考值",
    "missing_weight": "缺失权重",
    "eligible": "有效股票数",
}
VALUES = {
    **BOARDS,
    **KINDS,
    "L": "上市",
    "D": "退市",
    "P": "暂停上市",
    "pending": "等待执行",
    "running": "执行中",
    "completed": "已完成",
    "complete": "已完成",
    "done": "已完成",
    "failed": "执行失败",
    "blocked": "受限",
    "reachable": "可访问",
    "unavailable": "不可用",
    "unverified": "待验证",
    "circuit_open": "连接或字段异常",
    "denied": "权限不足",
    "quotes": "行情采集",
    "history": "历史采集",
    "analysis": "分析计算",
    "operations": "运维备份",
    "scheduler": "任务调度",
    "research": "研究计算",
    "notify": "消息通知",
    "qlib": "Qlib 适配器",
    "directory": "证券目录",
    "calendar": "交易日历",
    "daily": "日线采集",
    "factors": "复权因子",
    "analyze": "日终分析",
    "intraday": "盘中分析",
    "doctor": "接口诊断",
    "backup": "数据备份",
}

st.markdown(
    """
    <style>
      html, body, [data-testid="stApp"] {font-family: "PingFang SC", "Microsoft YaHei", sans-serif;}
      [data-testid="stAppViewContainer"] {background: #f5f7fb;}
      .block-container {padding-top: 2rem; padding-bottom: 2.5rem; max-width: 1520px;}
      #MainMenu, footer {visibility: hidden;}
      [data-testid="stHeader"] {background: transparent;}
      h1, h2, h3 {letter-spacing: -.025em; color: #152445;}
      h3 {font-size: 1.12rem !important;}
      [data-testid="stCaptionContainer"] {color: #69788f;}
      .qp-topline {display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap;
        gap:12px; color:#72809a; font-size:12px; padding-bottom:20px; border-bottom:1px solid #e3e8f1;}
      .qp-topline strong {color:#2c4269; letter-spacing:2px;}
      .qp-heading {padding:24px 0 20px;}
      .qp-heading h1 {font-size:28px; margin:0 0 8px; padding:0; font-weight:750;}
      .qp-heading p {color:#69788f; font-size:14px; margin:0;}
      .qp-ribbon {display:flex; flex-wrap:wrap; gap:8px; margin:0 0 14px;}
      .qp-pill {display:inline-flex; align-items:center; gap:6px; padding:5px 10px;
        border-radius:6px; font-size:12px; font-weight:550; white-space:nowrap;}
      .qp-ok {background:#e6f4ee; color:#127653;}
      .qp-warn {background:#fff3da; color:#945900;}
      .qp-bad {background:#fce9e9; color:#b53539;}
      .qp-info {background:#e9effc; color:#3159a6;}
      .qp-dot {height:5px; width:5px; border-radius:50%; background:currentColor;}
      [data-testid="stMetric"] {background:#fff; border:1px solid #e4e9f2; border-radius:12px;
        padding:18px 20px; min-height:116px; box-shadow:0 3px 14px #1c335104;}
      [data-testid="stMetricLabel"] {color:#6c7b91; font-size:13px;}
      [data-testid="stMetricValue"] {font-variant-numeric:tabular-nums; color:#1b2e50; font-size:30px;}
      [data-testid="stVerticalBlockBorderWrapper"] > div {border-color:#e2e8f2 !important; border-radius:12px;}
      [data-testid="stForm"] {background:#fff; border:1px solid #e3e9f2; border-radius:12px; padding:24px;}
      [data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button {border-radius:7px; min-height:40px;}
      [data-testid="stTabs"] [role="tablist"] {gap:28px; border-bottom:1px solid #e2e8f1; margin-bottom:16px;}
      [data-testid="stTabs"] [role="tab"] {padding-bottom:12px;}
      .qp-board {background:#fff; border:1px solid #e3e9f2; border-radius:12px; padding:20px; margin:0 0 8px;}
      .qp-board-head {display:flex; justify-content:space-between; align-items:center; color:#263e62; font-size:15px;}
      .qp-board-code {font-size:10px; letter-spacing:1px; color:#8491a6;}
      .qp-return {font-size:32px; font-weight:700; margin:12px 0 4px; font-variant-numeric:tabular-nums;}
      .qp-up {color:#d1434b;} .qp-down {color:#168361;} .qp-flat {color:#78869c;}
      .qp-board-meta {color:#7b899f; font-size:12px; margin-bottom:14px;}
      .qp-track {height:4px; background:#edf1f7; border-radius:3px; overflow:hidden; margin:10px 0;}
      .qp-track span {display:block; height:100%; background:#426dd2;}
      .qp-board-foot {display:flex; justify-content:space-between; color:#697a93; font-size:12px;}
      .qp-empty {padding:30px 20px; text-align:center; border:1px dashed #d5dfed; border-radius:12px;
        background:#fafcff; margin:8px 0 18px;}
      .qp-empty strong {display:block; color:#3b5175; font-size:15px; margin-bottom:8px;}
      .qp-empty p {font-size:13px; color:#7988a0; margin:0;}
      .qp-login {padding:46px 30px; border-radius:18px; color:#fff;
        background:linear-gradient(135deg,#142745 0%,#213e69 65%,#31578a 100%); min-height:370px;}
      .qp-login h1 {color:#fff; font-size:38px; line-height:1.5; margin:25px 0;}
      .qp-login p {color:#c3d1e9; font-size:14px; line-height:2;}
      .qp-login small {color:#91b4f6; letter-spacing:3px;}
      .qp-brand {display:flex; align-items:center; gap:12px; font-size:20px; font-weight:700; color:#f2f6ff;}
      .qp-logo {display:inline-grid; place-items:center; height:38px; width:38px; border-radius:10px;
        background:#4779ea; color:#fff; font-weight:800; font-size:23px;}
      .qp-brand small {display:block; font-size:10px; letter-spacing:2px; color:#859bbf; margin-top:3px;}
      .qp-side-note {color:#93a6c5; font-size:12px; line-height:2; padding:15px 0;}
      [data-testid="stSidebar"] {background:#13213b; min-width:230px; max-width:260px;}
      [data-testid="stSidebar"] [data-testid="stSidebarContent"] {background:#13213b;}
      [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {color:#b5c4df;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label {padding:11px 14px; border-radius:8px;
        margin:3px 0; width:100%; transition:background .15s;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) {background:#28446f;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label:hover {background:#203758;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label p {color:#c3cfe3; font-size:14px;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label:has(input:checked) p {color:#fff; font-weight:600;}
      [data-testid="stSidebar"] [data-testid="stRadio"] label > div:first-child {display:none;}
      [data-testid="stSidebar"] hr {border-color:#2b3a55;}
      [data-testid="stSidebar"] button {background:#203555; border-color:#354967; color:#dce6f7;}
      [data-testid="stSidebar"] [data-testid="stIconMaterial"] {color:#b5c4df;}
      @media(max-width:760px) {
        .block-container {padding:1.3rem 1rem 2rem;} .qp-heading h1 {font-size:24px;}
        .qp-login {min-height:0; padding:24px;} .qp-login h1 {font-size:28px;}
        .qp-topline {font-size:11px;} [data-testid="stMetric"] {min-height:100px; padding:14px;}
      }
    </style>
    """,
    unsafe_allow_html=True,
)


def pill(label, tone="info"):
    return f'<span class="qp-pill qp-{tone}"><span class="qp-dot"></span>{escape(str(label))}</span>'


def ribbon(pills):
    st.markdown('<div class="qp-ribbon">' + "".join(pills) + "</div>", unsafe_allow_html=True)


st.markdown(
    '<div class="qp-topline"><strong>QUAP / 知衡量化</strong>'
    "<span>沪深 A 股研究工作台　 /　 授权数据 · 独立研究 · 不执行交易</span></div>",
    unsafe_allow_html=True,
)

if "token" not in st.session_state:
    st.write("")
    left, right = st.columns([1.3, 1], gap="large")
    with left:
        st.markdown(
            '<div class="qp-login"><small>QUAP · RESEARCH WORKSPACE</small>'
            "<h1>让数据有据可循<br>让研究从容发生</h1>"
            "<p>连接沪深市场，构建观察组合。<br>从行情采集到因子诊断，在一个工作台完成研究闭环。</p>"
            "<p>01　真实数据　　02　版本追溯　　03　人工复核</p></div>",
            unsafe_allow_html=True,
        )
    with right:
        st.subheader("登录研究工作台")
        st.caption("仅限授权操作员访问。请使用本机 api_token，不是 Tushare 数据令牌。")
        with st.form("login"):
            token = st.text_input("访问令牌", type="password", placeholder="请输入操作员访问令牌")
            submitted = st.form_submit_button("进入工作台", type="primary", width="stretch")
        if submitted:
            probe = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), token)
            try:
                probe.request("GET", "/status")
                st.session_state.token = token
                st.rerun()
            except (ValueError, httpx.HTTPError):
                st.error("登录失败：请检查访问令牌，并确认后端服务已启动。")
        st.caption("安全提示：令牌仅用于当前会话。关闭页面不会中断后台采集。")
    st.stop()

client = Client(os.getenv("QUANT_API_URL", "http://127.0.0.1:8000"), st.session_state.token)

with st.sidebar:
    st.markdown(
        '<div class="qp-brand"><span class="qp-logo">Q</span><div>知衡量化<small>QUAP WORKSPACE</small></div></div>',
        unsafe_allow_html=True,
    )
    st.divider()
    st.caption("研究空间")
    page = st.radio("工作空间", list(WORKSPACES), format_func=lambda key: WORKSPACES[key], label_visibility="collapsed")
    st.divider()
    st.markdown(
        '<div class="qp-side-note">沪深 A 股 · 人工决策<br>总览与组合观察每 10 秒刷新<br>所有时间均为北京时间</div>',
        unsafe_allow_html=True,
    )
    if st.button("退出登录", width="stretch"):
        for key in list(st.session_state):
            del st.session_state[key]
        st.rerun()

st.markdown(
    f'<div class="qp-heading"><h1>{WORKSPACES[page]}</h1><p>{DESCRIPTIONS[page]}</p></div>',
    unsafe_allow_html=True,
)
if message := st.session_state.pop("notice", None):
    st.success(message)


def call(method, path, data=None):
    try:
        return client.request(method, path, data)
    except (httpx.HTTPError, ValueError) as exc:
        message = "后端连接已断开，请稍后重试。已保留的数据不代表实时行情。"
        if isinstance(exc, ValueError):
            code = str(exc).split(":", 1)[0]
            message = {
                "401": "访问令牌已失效，请退出后重新登录。",
                "409": "操作冲突或前置数据未就绪，请刷新页面并检查版本、交易日及任务状态。",
                "422": "输入不符合要求，请检查代码、权重及字段范围。",
                "404": "未找到对应记录，请刷新后重试。",
            }.get(code, "请求未完成，请检查服务状态或稍后重试。")
        st.error(message)
        return None


def local_time(iso_timestamp):
    if not iso_timestamp:
        return "—"
    try:
        moment = datetime.fromisoformat(str(iso_timestamp))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(ZoneInfo("Asia/Shanghai"))
    except (ValueError, TypeError):
        return str(iso_timestamp)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def empty(title, description):
    st.markdown(
        f'<div class="qp-empty"><strong>{escape(title)}</strong><p>{escape(description)}</p></div>',
        unsafe_allow_html=True,
    )


def table(rows, *, columns=None, message="暂无记录"):
    if not rows:
        empty(message, "数据就绪后将在此展示；不会使用演示数据填充。")
        return
    frame = pd.DataFrame(rows)
    if columns:
        frame = frame[[key for key in columns if key in frame.columns]]
    for key in frame.columns:
        if key.endswith("_at") or key == "source_time":
            frame[key] = frame[key].map(local_time)
        elif key in {"board", "status", "role", "queue", "kind"}:
            frame[key] = frame[key].map(lambda value: VALUES.get(value, value))
        elif key == "healthy":
            frame[key] = frame[key].map({True: "正常", False: "心跳过期"})
    st.dataframe(frame.rename(columns=LABELS), hide_index=True, width="stretch")


def feedback(result, text="操作已完成"):
    if result is not None:
        suffix = f"，任务编号 #{result['job_id']}，请在任务中心查看进度。" if result.get("job_id") else "。"
        st.success(text + suffix)


def status_ribbon(status):
    health = status["data_health"]
    health_tone = "ok" if health == "fresh" else "bad"
    ribbon(
        [
            pill("API 已连接", "ok"),
            pill(f"数据源 · {status['provider'].upper()}", "info" if status.get("configured") else "warn"),
            pill("行情覆盖完整" if health == "fresh" else "行情覆盖不足", health_tone),
            pill(
                "采集已暂停" if status.get("polling_paused") else "采集调度开启",
                "warn" if status.get("polling_paused") else "ok",
            ),
            pill(f"有效行情 {status['fresh_quote_count']:,} / {status['quote_count']:,}", "info"),
        ]
    )
    available = {
        c["endpoint"]
        for c in status.get("capabilities", [])
        if c.get("status") == "reachable" and (c.get("data") or {}).get("schema_verified")
    }
    missing = [endpoint for endpoint in REQUIRED_ENDPOINTS if endpoint not in available]
    capability_pill = pill("必需接口已验证", "ok") if not missing else pill("待验证接口：" + "、".join(missing), "warn")
    ribbon([capability_pill, pill(f"更新于 {local_time(status.get('timestamp'))} 北京时间", "info")])


@st.fragment(run_every=10)
def operations(compact=False):
    status = call("GET", "/status")
    if status is None:
        return
    status_ribbon(status)
    if any(item.get("status") == "blocked" for item in status.get("capabilities", [])):
        st.warning(
            "数据源授权待确认：请检查 Tushare 令牌和接口权限，再点击「接口诊断」。受限期间不展示模拟行情。", icon="⚠️"
        )
    elif status["data_health"] != "fresh":
        st.warning("行情数据覆盖不足或已过期。服务在线不代表行情实时有效；非交易时段也可能出现此提示。", icon="⚠️")
    metrics = status.get("metrics", {})
    a, b, c, d = st.columns(4)
    a.metric("已存行情 / 只", f"{status['quote_count']:,}")
    b.metric("有效行情 / 只", f"{status['fresh_quote_count']:,}", help="以源时间戳校验，120 秒内的当日行情。")
    c.metric("待执行任务", metrics.get("pending_jobs", "—"))
    d.metric("分析数据日期", status.get("analysis_target") or "尚未就绪")
    st.write("")
    st.subheader("市场板块概览")
    boards = {item["board"]: item for item in status.get("boards", [])}
    for col, (key, name) in zip(st.columns(3), BOARDS.items()):
        item = boards.get(key, {})
        change = item.get("daily_return")
        tone = "flat" if change is None or change == 0 else "up" if change > 0 else "down"
        value = "—" if change is None else f"{change:+.2%}"
        coverage = item.get("coverage")
        coverage_text = "待验证" if coverage is None else f"{coverage:.1%}"
        width = min(100, max(0, (coverage or 0) * 100))
        with col:
            st.markdown(
                f'<div class="qp-board"><div class="qp-board-head"><strong>{name}</strong>'
                f'<span class="qp-board-code">{escape(key.upper())}</span></div>'
                f'<div class="qp-return qp-{tone}">{value}</div><div class="qp-board-meta">等权样本当日涨跌幅</div>'
                f'<div class="qp-track"><span style="width:{width}%"></span></div>'
                f'<div class="qp-board-foot"><span>有效覆盖 {coverage_text}</span>'
                f'<span>上市 {item.get("listed", 0):,} 只</span></div></div>',
                unsafe_allow_html=True,
            )
    st.caption("红涨绿跌 · 以上为有效样本等权表现，非交易所指数；未覆盖权重不计入收益，覆盖不足时请谨慎解读。")
    if compact:
        return
    st.subheader("Collection controls")
    actions = {"refresh": "Update source data", "doctor": "Check provider capabilities"}
    actions["resume" if status.get("polling_paused") else "pause"] = (
        "恢复采集" if status.get("polling_paused") else "暂停采集"
    )
    for col, (action, label) in zip(st.columns(5), actions.items()):
        disabled = False
        if col.button(
            label,
            width="stretch",
            disabled=disabled,
            help="需先完成日线数据采集" if disabled else "后台执行，离开页面不影响任务",
        ):
            result = call("POST", "/control", {"action": action})
            feedback(result, "指令已受理")
            if result is not None and action in {"pause", "resume"}:
                st.rerun()
    st.write("")
    tasks, quality_tab, services, settings_tab = st.tabs(["任务中心", "数据质量与告警", "服务与接口", "运行设置"])
    with tasks:
        st.subheader("后台任务进度")
        table(status.get("jobs"), message="暂无后台任务")
        with st.expander("查看任务明细与失败重试"):
            jobs = call("GET", "/jobs") or []
            table(jobs, columns=["id", "kind", "queue", "status", "failures", "progress", "error", "available_at"])
            failed = [j["id"] for j in jobs if j["status"] == "failed"]
            if failed:
                job_id = st.selectbox("选择失败任务", failed)
                if st.button("重试任务"):
                    feedback(call("POST", f"/jobs/{job_id}/retry"), "重试已提交")
    with quality_tab:
        quality = status.get("data_quality") or {}
        q1, q2, q3 = st.columns(3)
        q1.metric("过期或缺失行情", quality.get("stale_or_missing_quote_count", "—"))
        q2.metric("缺失最新日线", quality.get("listed_missing_latest_completed_bar", "—"))
        q3.metric("复权因子修订", quality.get("factor_revision_keys", "—"))
        st.caption("数据质量与容器健康分别校验。缺失记录不会自动替换为模拟行情。")
        st.subheader("受限接口")
        table(quality.get("blocked_capabilities"), message="暂无受限接口记录")
        st.subheader("持久化告警")
        table(call("GET", "/alerts"), message="暂无告警记录")
    with services:
        left, right = st.columns(2)
        with left:
            st.subheader("服务心跳")
            table(status.get("services"), columns=["role", "healthy", "updated_at"])
        with right:
            st.subheader("数据源能力")
            table(status.get("capabilities"), columns=["endpoint", "status", "updated_at"])
        with st.expander("接口验证详情"):
            st.json(status.get("capabilities", []))
        st.caption("接口可访问不等于字段验证通过，请结合顶部必需接口状态判断。")
    with settings_tab:
        options = call("GET", "/settings")
        if options:
            with st.form("polling_settings"):
                seconds = st.number_input("行情轮询间隔（秒）", 15, 3600, options["quote_seconds"])
                if st.form_submit_button("保存运行设置", type="primary"):
                    result = call(
                        "PUT", "/settings", {"quote_seconds": seconds, "expected_revision": options["revision"]}
                    )
                    if result is not None:
                        st.session_state.notice = "运行设置已保存。"
                        st.rerun()
        with st.expander("运行指标 · 原始数据"):
            st.json(metrics)
        with st.expander("历史采集、备份与 Qlib 状态"):
            st.json(
                {
                    k: status.get(k)
                    for k in ("directory", "last_analysis", "backup", "qlib", "qualification", "incidents")
                }
            )


render(page, call, table, local_time, operations)
