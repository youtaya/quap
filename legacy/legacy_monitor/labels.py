# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Chinese display labels for monitor output. Internal keys stay English."""

from __future__ import annotations

BOARD_LABELS = {
    "STAR Market": "科创板",
    "ChiNext": "创业板",
    "Main Board": "主板",
    "Shanghai Main Board": "上证主板",
    "Shenzhen Main Board": "深证主板",
}

SESSION_LABELS = {
    "Not started": "未开始",
    "Pre-open": "开盘前",
    "Morning session": "上午交易",
    "Lunch break": "午间休市",
    "Afternoon session": "下午交易",
    "Closed": "已收盘",
}

CALENDAR_LABELS = {
    "simulated": "演示日历",
    "verified": "已校验",
    "unverified": "未校验",
}

MODE_LABELS = {"demo": "演示", "live": "实时"}

RULE_LABELS = {
    "absolute_daily_return": "绝对日涨跌幅",
    "cumulative_turnover_cny": "累计成交额（元）",
}

SUMMARY_COLUMNS = {
    "name": "名称",
    "total": "成分数",
    "eligible": "有效数",
    "coverage": "权重覆盖率",
    "volume": "成交量（股）",
    "turnover": "成交额（元）",
    "advancing": "上涨",
    "declining": "下跌",
    "unchanged": "平盘",
    "turnover_coverage": "成交额覆盖率",
    "daily_change_pct": "日涨跌幅（%）",
}

QUOTE_COLUMNS = {
    "symbol": "代码",
    "name": "名称",
    "last_cny": "最新价（元）",
    "previous_close_cny": "昨收（元）",
    "daily_change_pct": "日涨跌幅（%）",
    "cumulative_volume_shares": "累计成交量（股）",
    "cumulative_turnover_cny": "累计成交额（元）",
    "source_time": "行情时间",
    "fresh_valid": "有效新鲜",
}

EVENT_COLUMNS = {
    "id": "编号",
    "recorded_at": "写入时间",
    "observed_at": "观察时间",
    "source": "来源",
    "mode": "模式",
    "target": "对象",
    "rule": "规则",
    "value": "观测值",
    "threshold": "阈值",
    "message": "说明",
}


def display_name(name):
    """Return the Chinese label for a board/session/mode/rule, else the original name."""
    return BOARD_LABELS.get(name) or SESSION_LABELS.get(name) or MODE_LABELS.get(name) or RULE_LABELS.get(name) or name


def rename_columns(frame, mapping):
    return frame.rename(columns={key: label for key, label in mapping.items() if key in frame.columns})
