"""Daily 09:00 Asia/Shanghai research notices; SMTP send is not an order."""

import logging
import smtplib
from email.message import EmailMessage

from quant_platform.analysis import DISCLAIMER, holding_advice
from quant_platform.domain import CN, HoldingInput, HoldingPolicy, now, number
from quant_platform.providers import Deferred
from quant_platform.storage import jsonb
from quant_platform.storage.baskets import Conflict
from quant_platform.storage.holdings import save_holding

LOG = logging.getLogger(__name__)

WATCH_SYMBOL = "SH600895"
WATCH_NAME = "张江高科"
WATCH_COST = 28.0
CANDIDATE_LIMIT = 3
DISCLAIMER_ZH = "以上为研究建议，不是订单、不是交易推荐，也不承诺收益；审批由人工完成。"

ACTION_ZH = {
    "hold": "继续持有",
    "review_add": "考虑补仓（仅研究复核，不下单）",
    "review_reduce": "考虑减仓/部分出货（仅研究复核，不下单）",
    "review_exit": "考虑出货（仅研究复核，不下单）",
    "insufficient_data": "数据不足，暂不给出操作建议",
    "review_entry": "复核是否纳入观察（仅研究，不自动改篮子）",
}

REASON_ZH = {
    "loss_exceeds_exit_threshold": "相对成本亏损已达到或超过退出阈值",
    "name_based_risk_filter": "名称含 ST 或退市风险提示",
    "below_sma60": "价格低于 60 日均线",
    "high_volatility": "20 日波动率偏高",
    "gain_exceeds_reduce_threshold": "相对成本盈利已达到或超过减仓阈值",
    "profitable_and_no_longer_undervalued": "已盈利且不再出现在低估候选中",
    "still_undervalued": "仍出现在相对估值候选中",
    "pnl_within_add_band": "相对成本涨幅仍落在补仓观察带内",
    "no_review_threshold_hit": "未触及补仓、减仓或出货阈值",
    "missing_or_stale_price": "缺少有效最新价或行情未核验",
    "low_pe_ttm_vs_peers": "PE_TTM 低于同业分位",
    "low_pb_vs_peers": "PB 低于同业分位",
    "low_pe_ttm_vs_own_history": "PE_TTM 低于自身历史分位",
    "low_pb_vs_own_history": "PB 低于自身历史分位",
    "relative_valuation_rank": "相对估值综合排名靠前",
}

SOURCE_ZH = {
    "fresh_quote": "当日已核验实时行情",
    "daily_close": "上一完整交易日收盘价",
}


def smtp_ready(settings):
    return bool(settings.smtp_host and settings.notify_email and settings.smtp_password.get_secret_value())


def watch_levels(cost=WATCH_COST, policy=None):
    policy = policy or HoldingPolicy()
    return {
        "add_ceiling": cost * (1 + policy.add_max_pnl),
        "reduce_price": cost * (1 + policy.reduce_gain),
        "exit_price": cost * (1 - policy.exit_loss),
        "policy": policy.model_dump(),
    }


def _pct(value):
    return "未知" if number(value) is None else f"{float(value) * 100:.0f}%"


def _num(value, digits=2):
    return "未知" if number(value) is None else f"{float(value):.{digits}f}"


def _reason(key):
    return REASON_ZH.get(key, key)


def screen_notice(candidates, names, as_of, sent_day, limit=CANDIDATE_LIMIT):
    ranked = list(candidates or [])[:limit]
    lines = [
        f"发送日（上海）：{sent_day}",
        f"筛选 as-of：{as_of or '暂无已完成报告'}",
        "规则：行业内与自身历史的 PE_TTM / PB 相对分位；亏损或缺失估值不会被当成 0。",
        f"本次列出不超过 {limit} 只候选，供人工复核，不会自动改篮子或下单。",
        "",
    ]
    if not ranked:
        lines.append("今日没有可用的相对估值候选。若日频分析尚未完成，请等待 history/analysis 工人补齐后再看下一封。")
    else:
        if len(ranked) < limit:
            lines.append(f"合格候选不足 {limit} 只，以下为全部入围结果。")
            lines.append("")
        for index, row in enumerate(ranked, 1):
            code = row.get("symbol")
            name = names.get(code) or code
            turnover = number(row.get("average_turnover20"))
            turnover_text = "未知" if turnover is None else f"{turnover / 10_000:.0f} 万元"
            reasons = row.get("reasons") or ["relative_valuation_rank"]
            action_key = row.get("action") or "review_entry"
            lines.extend(
                [
                    f"第{index}名  {code}  {name}",
                    f"  行业：{row.get('industry') or '未知'}（分位口径：{row.get('peer_scope') or '未知'}）",
                    f"  综合分：{_num(row.get('score'), 4)}",
                    f"  PE_TTM：{_num(row.get('pe_ttm'))}（同业分位 {_pct(row.get('pe_industry_pct'))}，自身历史 {_pct(row.get('pe_history_pct'))}）",
                    f"  PB：{_num(row.get('pb'))}（同业分位 {_pct(row.get('pb_industry_pct'))}，自身历史 {_pct(row.get('pb_history_pct'))}）",
                    f"  20日均成交额：{turnover_text}",
                    f"  建议动作：{ACTION_ZH.get(action_key, action_key)}",
                    "  推荐理由：",
                ]
            )
            lines.extend(f"    - {_reason(item)}" for item in reasons)
            lines.append("")
    lines.extend(["免责声明：", DISCLAIMER_ZH, DISCLAIMER])
    count = len(ranked)
    return {
        "subject": f"【QUAP 研究】{sent_day} 相对估值候选（{count}只）",
        "body": "\n".join(lines).strip() + "\n",
    }


def holding_notice(advice, name, sent_day, levels=None):
    levels = levels or watch_levels(number(advice.get("cost_price")) or WATCH_COST)
    action = advice.get("action") or "insufficient_data"
    pnl = number(advice.get("pnl_pct"))
    lines = [
        f"发送日（上海）：{sent_day}",
        f"标的：{name or WATCH_NAME}  {advice.get('symbol') or WATCH_SYMBOL}",
        f"持仓成本：{_num(advice.get('cost_price'))} 元",
        f"最新价格：{_num(advice.get('last'))} 元（来源：{SOURCE_ZH.get(advice.get('price_source'), '无有效价格')}）",
        f"浮动盈亏：{'未知' if pnl is None else f'{pnl * 100:+.2f}%'}",
        f"是否仍在低估候选：{'是' if advice.get('still_undervalued') else '否'}",
        "",
        f"当前建议：{ACTION_ZH.get(action, action)}",
        "建议理由：",
    ]
    lines.extend(f"  - {_reason(item)}" for item in (advice.get("reasons") or ["missing_or_stale_price"]))
    flags = advice.get("risk_flags") or []
    if flags:
        lines.append("风险标记：" + "、".join(_reason(item) for item in flags))
    lines.extend(
        [
            "",
            f"价位参考（按成本 {_num(advice.get('cost_price') or WATCH_COST)} 元与持仓策略）：",
            f"  - 补仓观察带：现价不高于 {_num(levels['add_ceiling'])} 元（相对成本涨幅 ≤ {levels['policy']['add_max_pnl'] * 100:.0f}%），"
            "且仍出现在低估候选中、未同时亮高波动旗。",
            f"  - 减仓观察位：现价达到或超过 {_num(levels['reduce_price'])} 元（相对成本涨幅 ≥ {levels['policy']['reduce_gain'] * 100:.0f}%），"
            "或已盈利但不再低估。",
            f"  - 出货观察位：现价跌至或低于 {_num(levels['exit_price'])} 元（相对成本亏损 ≥ {levels['policy']['exit_loss'] * 100:.0f}%），"
            "或名称风险 / 跌破年线且高波动同时出现。",
            "",
            "免责声明：",
            DISCLAIMER_ZH,
            DISCLAIMER,
        ]
    )
    return {
        "subject": f"【QUAP 研究】{sent_day} {name or WATCH_NAME}（{advice.get('symbol') or WATCH_SYMBOL}）持仓建议",
        "body": "\n".join(lines).strip() + "\n",
    }


def ensure_watch_holding(conn):
    row = conn.execute("SELECT * FROM holdings WHERE symbol=%s AND NOT archived FOR UPDATE", (WATCH_SYMBOL,)).fetchone()
    if row and abs(float(row["cost_price"]) - WATCH_COST) < 1e-6:
        return row
    value = HoldingInput(
        symbol=WATCH_SYMBOL,
        cost_price=WATCH_COST,
        quantity=row["quantity"] if row else None,
        note=(row["note"] if row and row["note"] else "张江高科 daily research watch"),
        expected_revision=row["revision"] if row else 0,
    )
    try:
        return save_holding(conn, value, row["id"] if row else None)
    except Conflict:
        return row


def compose_daily_notices(db, sent_day):
    screen_rows = db.rows("SELECT as_of,data FROM reports WHERE kind='screen' ORDER BY as_of DESC,id DESC LIMIT 1")
    screen = screen_rows[0] if screen_rows else None
    candidates = (screen["data"].get("candidates") or []) if screen else []
    holdings = [row for row in db.active_holdings() if row["symbol"] == WATCH_SYMBOL]
    holding = holdings[0] if holdings else {"symbol": WATCH_SYMBOL, "cost_price": WATCH_COST}
    codes = [WATCH_SYMBOL, *[row["symbol"] for row in candidates[:CANDIDATE_LIMIT]]]
    names = {
        row["symbol"]: row["name"]
        for row in db.rows("SELECT symbol,name FROM instruments WHERE symbol=ANY(%s)", (codes,))
    }
    names.setdefault(WATCH_SYMBOL, WATCH_NAME)
    quotes = db.rows("SELECT data FROM latest_quotes WHERE symbol=%s", (WATCH_SYMBOL,))
    bars = db.rows(
        "SELECT data FROM daily_bars WHERE symbol=%s ORDER BY day DESC,dataset_id DESC LIMIT 1", (WATCH_SYMBOL,)
    )
    stocks = db.rows(
        "SELECT data FROM reports WHERE kind='stock' AND target=%s ORDER BY as_of DESC,id DESC LIMIT 1",
        (WATCH_SYMBOL,),
    )
    advice = holding_advice(
        holding,
        quote=(quotes[0]["data"] if quotes else {}),
        at=now(),
        last_close=number((bars[0]["data"] or {}).get("close")) if bars else None,
        stock_report=(stocks[0]["data"] if stocks else {}),
        screen_candidates=candidates,
        name=names.get(WATCH_SYMBOL, WATCH_NAME),
    )
    as_of = str(screen["as_of"]) if screen else None
    cost = number(holding["cost_price"]) or WATCH_COST
    return [
        screen_notice(candidates, names, as_of, sent_day),
        holding_notice(advice, names.get(WATCH_SYMBOL, WATCH_NAME), sent_day, watch_levels(cost)),
    ]


def deliver(settings, message):
    sender = settings.smtp_from or settings.smtp_user or settings.notify_email
    email = EmailMessage()
    email["Subject"] = message["subject"]
    email["From"] = sender
    email["To"] = settings.notify_email
    email.set_content(message["body"])
    timeout = 30
    try:
        client = (
            smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=timeout)
            if settings.smtp_port == 465
            else smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
        )
        with client:
            if settings.smtp_port != 465 and settings.smtp_starttls:
                client.starttls()
            password = settings.smtp_password.get_secret_value()
            if password:
                client.login(settings.smtp_user or settings.notify_email, password)
            client.send_message(email)
    except (smtplib.SMTPException, OSError):
        raise Deferred("smtp unavailable", seconds=1800) from None


def run(db, settings, job):
    sent_day = str((job.get("payload") or {}).get("day") or now().astimezone(CN).date())
    with db.transaction() as conn:
        db.fence(conn, job)
        ensure_watch_holding(conn)
    if not smtp_ready(settings):
        raise Deferred("smtp unconfigured", seconds=1800)
    messages = compose_daily_notices(db, sent_day)
    sent = list((job.get("progress") or {}).get("sent") or [])
    for index, message in enumerate(messages):
        if index in sent:
            continue
        deliver(settings, message)
        sent.append(index)
        with db.transaction() as conn:
            db.fence(conn, job)
            conn.execute("UPDATE jobs SET progress=progress || %s WHERE id=%s", (jsonb({"sent": sent}), job["id"]))
    with db.publication(job) as conn:
        db.set_setting(
            conn,
            "last_notify",
            {
                "day": sent_day,
                "at": now().isoformat(),
                "to": settings.notify_email,
                "subjects": [item["subject"] for item in messages],
            },
        )
    LOG.info("Daily research notices sent")
