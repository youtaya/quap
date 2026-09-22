# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Run from legacy/: python -m streamlit run examples/market_monitor/app.py."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from legacy_monitor import (
    AlertSettings,
    AlertStore,
    DemoProvider,
    MonitorEngine,
    parse_baskets,
)
from legacy_monitor.labels import (
    BOARD_LABELS,
    CALENDAR_LABELS,
    EVENT_COLUMNS,
    MODE_LABELS,
    QUOTE_COLUMNS,
    SESSION_LABELS,
    SUMMARY_COLUMNS,
    display_name,
    rename_columns,
)
from legacy_monitor.models import BOARD_NAMES
from legacy_monitor.client import LiveClient
from legacy_monitor.state import RuntimeStore, load_config, validate_settings
from legacy_monitor.updater import ExternalHistory, ManagedDataset

st.set_page_config(page_title="A股行情监控", layout="wide")
st.title("A股行情监控")
st.caption("科创板 · 创业板 · 沪深主板 | 研究监控，不执行交易")

CONFIG_PATH = (
    Path(os.environ.get("QLIB_MONITOR_CONFIG", Path(__file__).with_name("config.yaml"))).expanduser().resolve()
)


@st.cache_data
def read_config(path):
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict):
        raise ValueError("配置必须是 YAML 映射。")
    if config.get("mode", "demo") not in {"demo", "live"}:
        raise ValueError("模式必须是 demo 或 live。")
    return config


try:
    defaults = read_config(str(CONFIG_PATH))
    service_config = load_config(CONFIG_PATH)
    runtime = RuntimeStore(service_config["database"])
    saved_settings = runtime.get("settings", {"revision": 0, "data": service_config["settings"]})
    defaults = {**defaults, **saved_settings["data"]}
    default_alerts = AlertSettings(**defaults.get("alerts", {}))
    parse_baskets(defaults.get("baskets", {}))
except (OSError, ValueError, TypeError, yaml.YAMLError) as error:
    st.error(f"无法加载配置：{error}")
    st.stop()

with st.sidebar:
    st.header("监控设置")
    with st.form("settings"):
        mode = st.selectbox(
            "数据模式",
            ["demo", "live"],
            index=0 if defaults.get("mode", "live") == "demo" else 1,
            format_func=lambda value: MODE_LABELS.get(value, value),
        )
        interval = st.number_input(
            "轮询间隔（秒）",
            min_value=15,
            max_value=3600,
            value=int(defaults.get("poll_seconds", 30)),
            step=15,
        )
        quote_source = st.selectbox(
            "Live quote provider",
            ["tencent", "eastmoney"],
            index=0 if defaults.get("quote_provider", "tencent") == "tencent" else 1,
        )
        provider_uri = st.text_input("可选 Qlib 数据集", value=defaults.get("provider_uri") or "")
        stock_threshold = st.number_input(
            "个股涨跌幅阈值（%）", min_value=0.01, value=default_alerts.stock_change * 100
        )
        basket_threshold = st.number_input(
            "板块/篮子阈值（%）", min_value=0.01, value=default_alerts.basket_change * 100
        )
        turnover_threshold = st.number_input(
            "成交额阈值（元；0 表示关闭）", min_value=0.0, value=float(default_alerts.turnover or 0)
        )
        cooldown = st.number_input("告警冷却（秒）", min_value=1, value=int(default_alerts.cooldown))
        basket_yaml = st.text_area(
            "自定义篮子（YAML）", height=220, value=yaml.safe_dump(defaults.get("baskets", {}), sort_keys=False)
        )
        apply = st.form_submit_button("应用设置")
    st.caption(
        "Live settings are saved by the background service without pausing it. Manual refresh respects feed throttling. Demo is isolated and never changes the live service."
    )

if apply or "engine" not in st.session_state:
    try:
        baskets = parse_baskets(yaml.safe_load(basket_yaml))
        settings = AlertSettings(
            stock_threshold / 100,
            basket_threshold / 100,
            turnover_threshold or None,
            cooldown,
            default_alerts.minimum_coverage,
        )
        warnings = []
        if mode == "live":
            engine = LiveClient(service_config)
            if apply:
                updated = validate_settings(
                    {
                        "poll_seconds": interval,
                        "quote_provider": quote_source,
                        "provider_uri": provider_uri.strip() or None,
                        "alerts": asdict(settings),
                        "baskets": yaml.safe_load(basket_yaml),
                    }
                )
                if runtime.health()["healthy"]:
                    engine.send("apply", {"revision": saved_settings["revision"], "settings": updated})
                else:
                    warnings.append("Live view selected. Start the background service before applying settings.")
            running = runtime.health().get("polling", True)
        else:
            history = ExternalHistory(provider_uri.strip()) if provider_uri.strip() else None
            store = None
            try:
                store = AlertStore(service_config["database"])
            except (OSError, sqlite3.Error) as error:
                warnings.append(f"无法打开告警库，已停止评估告警：{error}")
            engine = MonitorEngine(DemoProvider(), store, baskets, settings, history, interval)
            running = False
            engine.poll()
        st.session_state.engine = engine
        st.session_state.running = running
        st.session_state.setup_warnings = warnings
        st.session_state.history_frame = None
    except (ValueError, TypeError, RuntimeError, yaml.YAMLError) as error:
        st.error(f"设置未能应用：{error}")
        if "engine" not in st.session_state:
            st.stop()

for warning in st.session_state.get("setup_warnings", []):
    st.warning(warning)


def format_value(value, pattern=",.2f"):
    return "暂无" if value is None else format(value, pattern)


def display_pct(value):
    return None if value is None else round(value * 100, 4)


@st.fragment(run_every=5)
def dashboard():
    engine = st.session_state.engine
    live = isinstance(engine, LiveClient)
    healthy = True
    if live:
        health = runtime.health()
        healthy = health["healthy"]
        st.session_state.running = health.get("polling", True) and healthy
        st.caption(
            f"Background service: {health.get('status', 'not started')} | Worker: {health.get('worker', 'unavailable')} | Saved settings revision: {health.get('revision', 0)}"
        )
        if not healthy and st.button("Start background service"):
            from legacy_monitor.service import start_service

            try:
                start_service(CONFIG_PATH)
                st.rerun()
            except (RuntimeError, OSError) as error:
                st.error(str(error))
        directory = runtime.get("directory", {})
        st.caption(
            f"Directory: {len(directory.get('symbols', {}))} supported securities | Discovered: {directory.get('fetched_at', 'unavailable')} | Excluded: {directory.get('excluded', 0)}"
        )
        if runtime.get("directory_error"):
            st.warning(f"Directory refresh failed: {runtime.get('directory_error')}")
            st.caption(
                f"Automatic discovery retry: {runtime.get('directory_retry_at') or 'pending'}. Partial pages are not live market coverage."
            )
        if engine.command_id:
            acknowledgment = runtime.command_status(engine.command_id)
            if acknowledgment:
                st.caption(f"Command {acknowledgment['id']}: {acknowledgment['status']}")
                if acknowledgment["message"]:
                    st.error(acknowledgment["message"])
    controls = st.columns([1, 1, 1, 3])
    start = controls[0].button(
        "开始",
        disabled=st.session_state.running or not healthy,
        on_click=None if live else lambda: st.session_state.update(running=True),
    )
    pause = controls[1].button(
        "暂停",
        disabled=not st.session_state.running,
        on_click=None if live else lambda: st.session_state.update(running=False),
    )
    manual = controls[2].button("立即刷新", disabled=not healthy)
    new_events = []
    if live:
        try:
            if start or pause or manual:
                engine.send("resume" if start else "pause" if pause else "refresh")
        except RuntimeError as error:
            st.error(str(error))
        result = engine.view()
        last_id = st.session_state.get("live_last_event", max((event["id"] for event in result.events), default=0))
        new_events = [event for event in result.events if event["id"] > last_id]
        st.session_state.live_last_event = max([last_id] + [event["id"] for event in result.events])
    else:
        if start:
            st.session_state.running = True
        if pause:
            st.session_state.running = False
        if st.session_state.running or manual:
            new_events = engine.poll(force=manual).events
        result = engine.view()
    controls[3].caption("正在轮询" if st.session_state.running else "已暂停 — 仍可手动刷新")
    if engine.provider.mode == "demo":
        st.warning("演示模式 — 合成样本证券与虚拟交易时钟，不是真实板块指数。")
    else:
        st.info("公开行情 — 尽力而为的轮询，不是持牌实时交易所源。")
    if result.error:
        st.error(result.error)
    if result.storage_error:
        st.error(result.storage_error)
    if result.notify_error:
        st.error(result.notify_error)
    for event in new_events:
        st.toast(event["message"], icon="⚠️")
    snapshot = result.snapshot
    if snapshot is None:
        st.info("请选择开始或立即刷新以采集行情。当前还没有成功快照。")
        return
    now = engine.observation_time()
    fresh = sum(q.fresh(now) and q.daily_return is not None for q in snapshot.quotes)
    source_times = [q.timestamp for q in snapshot.quotes if q.timestamp is not None]
    quote_range = f"{min(source_times).isoformat()} — {max(source_times).isoformat()}" if source_times else "暂无"
    st.caption(
        f"来源：{snapshot.source} | 最近成功采集：{snapshot.fetched_at.isoformat()} | "
        f"时段：{SESSION_LABELS.get(result.session, result.session)} | "
        f"日历：{CALENDAR_LABELS.get(result.calendar_status, result.calendar_status)}"
    )
    st.caption(f"行情时间范围：{quote_range}")
    interval = "暂无" if engine.effective_interval is None else format(engine.effective_interval, ",.2f") + "秒"
    st.caption(
        f"有效新鲜报价：{fresh}/{len(snapshot.quotes)} | 申报记录：{snapshot.reported_total} | "
        f"已排除：{snapshot.excluded} | 采集耗时：{result.duration:.2f}秒 | "
        f"有效轮询间隔：{interval}"
    )
    if result.calendar_status == "unverified":
        st.warning("日历覆盖未校验。告警需要工作日交易时段，以及当日新鲜行情时间。")
    if fresh < len(snapshot.quotes):
        st.warning("部分报价过期、缺少时间戳或价格无效，不能触发告警。交易时段内它们不计入代理指数。")
    if result.session not in {"Morning session", "Afternoon session"}:
        st.info("非交易时段：告警已暂停。汇总显示最近一个有日期的快照，不是实时波动。")
    for warning in snapshot.warnings:
        st.caption(warning)
    selected = st.multiselect("显示板块", list(BOARD_NAMES), default=list(BOARD_NAMES), format_func=display_name)
    columns = st.columns(max(1, len(selected)))
    for column, name in zip(columns, selected):
        summary = result.summaries[name]
        column.metric(
            BOARD_LABELS.get(name, name),
            format_value(summary.proxy),
            None if summary.daily_return is None else f"{summary.daily_return:+.2%}",
            delta_color="inverse",
        )
        column.caption(f"日代理指数 · {summary.eligible}/{summary.total} 有效 · 权重覆盖 {summary.coverage:.1%}")
    st.caption(
        "日代理指数 = 1,000 × (1 + 加权日收益率)，不是官方指数。"
        "缺失成分会被剔除，剩余权重重新归一化。"
        f"板块/篮子告警要求有效权重覆盖率达到 {engine.settings.minimum_coverage:.0%}。"
        "A股配色：红涨绿跌。"
    )
    summaries = []
    for summary in result.summaries.values():
        row = asdict(summary)
        row.pop("observation")
        row["name"] = display_name(summary.name)
        row["daily_change_pct"] = display_pct(summary.daily_return)
        row.pop("daily_return")
        summaries.append(row)
    st.dataframe(rename_columns(pd.DataFrame(summaries), SUMMARY_COLUMNS), width="stretch", hide_index=True)
    if engine.samples:
        chart = pd.DataFrame(engine.samples).set_index("time")
        shown = [name for name in chart.columns if name in selected or name not in BOARD_NAMES]
        if shown:
            chart = chart[shown].dropna(axis=1, how="all").dropna(axis=0, how="all")
            if not chart.empty:
                st.line_chart(chart.rename(columns=display_name), width="stretch")
    st.caption("仅记录交易时段观察点；日期、成分或设置变化会重置。最多 600 个样本。")
    group = st.selectbox("成分分组", list(result.summaries), format_func=display_name)
    members = dict(engine.members[group])
    rows = []
    for q in snapshot.quotes:
        if q.symbol not in members:
            continue
        rows.append(
            {
                "symbol": q.symbol,
                "name": q.name,
                "last_cny": q.last,
                "previous_close_cny": q.previous_close,
                "daily_change_pct": display_pct(q.daily_return),
                "cumulative_volume_shares": q.volume,
                "cumulative_turnover_cny": q.turnover,
                "source_time": q.timestamp.isoformat() if q.timestamp else None,
                "fresh_valid": q.fresh(now) and q.daily_return is not None,
            }
        )
    missing = sorted(set(members) - {q.symbol for q in snapshot.quotes})
    if missing:
        st.warning("缺失成分：" + "，".join(missing[:30]) + ("…" if len(missing) > 30 else ""))
    frame = pd.DataFrame(rows)
    if not frame.empty:
        st.subheader("领涨领跌")
        eligible = frame[frame["fresh_valid"]].dropna(subset=["daily_change_pct"])
        st.dataframe(
            rename_columns(eligible.sort_values("daily_change_pct", key=abs, ascending=False).head(10), QUOTE_COLUMNS),
            width="stretch",
            hide_index=True,
        )
        st.subheader("成分股")
        st.dataframe(rename_columns(frame, QUOTE_COLUMNS), width="stretch", hide_index=True)
    st.subheader("本地告警事件")
    if result.events:
        st.warning(f"最近一轮已写入 {len(result.events)} 条告警。最新：{result.events[-1]['message']}")
    query = st.text_input("搜索告警")
    if engine.store:
        try:
            events = engine.store.recent(engine.provider.mode, query)
            shown_events = []
            for event in events:
                row = dict(event)
                row["target"] = display_name(row["target"])
                row["rule"] = display_name(row["rule"])
                row["mode"] = display_name(row["mode"])
                shown_events.append(row)
            st.dataframe(rename_columns(pd.DataFrame(shown_events), EVENT_COLUMNS), width="stretch", hide_index=True)
            st.caption(f"本地 SQLite：{engine.store.path} | 保留 30 天 | 收益率阈值使用小数，不是百分数。")
        except (sqlite3.Error, OSError) as error:
            st.error(f"无法读取已保存事件：{error}")


dashboard()


@st.fragment(run_every=5)
def history_panel():
    state = runtime.get("history", {})
    enabled = service_config["history"]["enabled"]
    waiting = "waiting for complete directory" if runtime.health()["healthy"] else "waiting for service"
    st.subheader("Automatic Qlib data updates")
    st.caption(
        f"Status: {state.get('status', waiting if enabled else 'disabled')} | Target: {state.get('target', 'unavailable')} | Next update: {state.get('next_update', 'startup')}"
    )
    if state.get("total"):
        st.progress(min(1.0, state.get("attempted", 0) / state["total"]))
        st.caption(
            " | ".join(
                f"{key}: {state.get(key, 0)}"
                for key in ("total", "attempted", "successful", "failed", "unavailable", "lagging")
            )
        )
    if state.get("error"):
        st.error(state["error"])
    st.caption(
        f"Published generation: {state.get('generation') or 'none yet'}. Managed history uses forward-adjusted CNY and volume in shares. Updates never modify your external research dataset."
    )
    with st.expander("Qlib 历史对照 — 与实时价格分开"):
        symbol = st.text_input("历史代码", value="SH600895")
        settings = runtime.get("settings", {"data": service_config["settings"]})["data"]
        external = settings.get("provider_uri")
        st.caption(
            f"Read-only external dataset: {external}" if external else f"Managed dataset: {runtime.root / 'qlib'}"
        )
        if st.button("加载历史数据"):
            try:
                history = ExternalHistory(external) if external else ManagedDataset(runtime.root)
                st.session_state.history_frame = history.features(symbol).reset_index().set_index("datetime")
                st.session_state.history_generation = state.get("generation")
            except Exception as error:
                st.session_state.history_frame = None
                st.error(f"历史数据不可用：{error}")
        historical = st.session_state.get("history_frame")
        if historical is not None:
            if not external and st.session_state.get("history_generation") != state.get("generation"):
                st.info("A newer history generation is available; reload the selected stock.")
            st.caption(f"最近有效收盘日期：{historical['$close'].last_valid_index()}")
            st.line_chart(historical[["$close"]], width="stretch")
            st.bar_chart(historical[["$volume"]], width="stretch")
            st.dataframe(historical, width="stretch")


history_panel()
st.caption(
    "Live polling and scheduled Qlib updates continue after this browser closes while the background service and computer remain running. Local research only; no trades are submitted."
)
