"""Immediate operator mail for a candidate screen and one holding.

This path is not the production collector. It does not write market tables and
does not submit orders. When Tushare is not configured, the letters use a
public daily window and say so.
"""

import json
import re
import smtplib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import httpx

from quant_platform.analysis import indicators, screen
from quant_platform.domain import ScreenRule, now, symbol

SOURCE_NOTE = (
    "本次即时简报使用腾讯前复权日K和新浪财经成交额排名。"
    "本机未配置 Tushare 令牌，行情没有写入生产库，上市与停牌状态未做供应商核验。"
    "样本是成交额靠前的沪深 A 股，不是全市场。"
    "这是研究输出，不是收益承诺，系统不提交订单。"
)
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def advise_holding(metrics, cost):
    close = float(metrics["close"])
    sma20, sma60, atr = metrics.get("sma20"), metrics.get("sma60"), metrics.get("atr14")
    volatility = metrics.get("volatility20")
    flags = set(metrics.get("risk_flags") or [])
    pnl = close / cost - 1
    reasons = [
        f"持仓成本 {cost:.2f} 元，最近价 {close:.2f} 元，浮动盈亏 {pnl:.2%}。",
        f"20 日均线 {sma20:.2f}，60 日均线 {sma60:.2f}，ATR14 {atr:.2f}，"
        f"20 日年化波动率 {volatility:.2%}，20 日收益 {metrics['return20']:.2%}。",
    ]
    below60 = "below_sma60" in flags
    high_vol = "high_volatility" in flags
    if below60:
        stance = "不建议继续持有"
        reasons.append(f"最近价低于 60 日均线 {sma60:.2f}，中期趋势已破。")
    elif high_vol:
        stance = "继续持有，不要补仓"
        reasons.append("20 日年化波动率超过 50%，仓位可以留着，但不再加仓。")
    else:
        stance = "继续持有"
        reasons.append("最近价仍在 60 日均线上方，且波动率没有超过 50%。")
    if below60:
        sell = round(close, 2)
        reasons.append(f"出货参考价 {sell:.2f} 元：跌破 60 日均线后按现价附近减仓，不等待回到成本。")
    elif pnl >= 0:
        sell = round(max(cost, close - 2 * atr), 2)
        reasons.append(
            f"出货参考价 {sell:.2f} 元：现价减去 2 倍 ATR，且不低于成本 {cost:.2f} 元。跌破该价再出货。"
        )
    else:
        sell = round(cost, 2)
        reasons.append(f"出货参考价 {sell:.2f} 元：仍在 60 日均线上方，回到成本附近再处理亏损仓。")
    if below60 or high_vol or close <= sma20:
        add = None
        if below60 or high_vol:
            reasons.append("补仓：当前不给补仓价。")
        else:
            reasons.append(f"补仓：最近价已经低于 20 日均线 {sma20:.2f}，先不补，等重新站上再评估。")
    else:
        add = round(float(sma20), 2)
        reasons.append(f"补仓参考价 {add:.2f} 元，只考虑回踩 20 日均线，不在现价追高。")
    return {"stance": stance, "sell": sell, "add": add, "pnl": pnl, "reasons": reasons}


def candidate_reason(row, name):
    return (
        f"{name}（{row['symbol']}）在本次样本中的得分 {row['score']:.3f}。"
        f"近 20 日收益 {row['return20']:.2%}，20 日年化波动率 {row['volatility20']:.2%}，"
        f"近 20 日平均成交额 {row['average_turnover20']:.0f} 元。"
        "得分是收益分位乘以 0.6，加上低波动分位乘以 0.4；"
        "已要求至少 120 根日K、20 日平均成交额不低于 2000 万元，并排除名称中的 ST 和退。"
    )


def candidate_letter(day, screened, names):
    rows = screened["candidates"][:3]
    lines = [
        "QUAP 候选股票筛选",
        f"截至 {day}",
        SOURCE_NOTE,
        f"规则覆盖样本 {screened['total']} 只，通过过滤 {screened['eligible']} 只，下面取得分最高的 {len(rows)} 只。",
        "",
    ]
    if len(rows) < 3:
        lines.append("通过过滤的股票少于 3 只，这里只列出实际入选的股票，不补空位。")
        lines.append("")
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {candidate_reason(row, names.get(row['symbol'], row['symbol']))}")
        lines.append("")
    return {"subject": f"QUAP 候选筛选 {day}：{len(rows)} 只", "body": "\n".join(lines).strip() + "\n"}


def holding_letter(code, name, cost, metrics):
    advice = advise_holding(metrics, cost)
    add = "不补仓" if advice["add"] is None else f"{advice['add']:.2f} 元"
    lines = [
        f"QUAP 持仓建议：{name}（{code}）",
        f"截至 {metrics['as_of']}",
        SOURCE_NOTE,
        "",
        f"建议：{advice['stance']}",
        f"出货参考价：{advice['sell']:.2f} 元",
        f"补仓参考价：{add}",
        "",
        "理由：",
        *[f"- {reason}" for reason in advice["reasons"]],
    ]
    return {
        "subject": f"QUAP {name}持仓建议（成本 {cost:.2f} 元）",
        "body": "\n".join(lines).strip() + "\n",
        "advice": advice,
    }


def _rows(raw):
    parsed = {}
    for item in raw:
        day = date.fromisoformat(str(item[0]))
        close = float(item[2])
        shares = float(item[5]) * 100
        parsed[day] = {
            "day": day,
            "factor": 1.0,
            "dataset_id": "operator-public-qfq",
            "factor_dataset_id": "operator-public-qfq",
            "data": {
                "open": float(item[1]),
                "close": close,
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": shares,
                "turnover": shares * close,
            },
        }
    return [parsed[day] for day in sorted(parsed)]


def _provider_symbol(code):
    return ("sh" if code.startswith("SH") else "sz") + code[2:]


def _load_market(code, universe):
    headers = {"User-Agent": "quant-platform-operator-brief", "Referer": "https://quote.eastmoney.com/"}
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, headers=headers) as client:
        rank = client.get(
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData",
            params={"page": 1, "num": max(universe * 2, 40), "sort": "amount", "asc": 0, "node": "hs_a"},
            headers={"Referer": "https://vip.stock.finance.sina.com.cn/"},
        )
        rank.raise_for_status()
        names = {}
        for item in rank.json():
            try:
                names[symbol(str(item["code"]))] = str(item["name"])
            except (TypeError, ValueError, KeyError):
                continue
            if len(names) >= universe:
                break
        if code not in names:
            quote = client.get(f"https://qt.gtimg.cn/q={_provider_symbol(code)}")
            quote.raise_for_status()
            names[code] = quote.content.decode("gbk").split("~")[1]
    wanted = list(dict.fromkeys([*names, code]))

    def bars(one):
        try:
            with httpx.Client(timeout=20, follow_redirects=False, trust_env=False, headers=headers) as client:
                response = client.get(
                    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                    params={"param": f"{_provider_symbol(one)},day,,,320,qfq"},
                )
                response.raise_for_status()
                node = response.json()["data"][_provider_symbol(one)]
            return one, _rows(node.get("qfqday") or node.get("day") or [])
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            return one, []

    metrics = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for one, rows in pool.map(bars, wanted):
            item = indicators(rows) if len(rows) >= 120 else {"status": "insufficient_data", "bars": len(rows)}
            if item.get("status") == "complete":
                metrics[one] = item
    if code not in metrics:
        raise ValueError(f"No complete daily window for {code}.")
    day = max(date.fromisoformat(item["as_of"]) for item in metrics.values())
    instruments = {
        one: {"name": names.get(one, one), "status": "L", "list_date": None, "delist_date": None, "suspended": False}
        for one in metrics
    }
    return {"names": names, "metrics": metrics, "instruments": instruments, "day": day}


def send_email(settings, to, subject, body):
    host = settings.smtp_host.strip()
    if not host:
        raise ValueError("SMTP is not configured.")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from or settings.smtp_user or "quant-platform@localhost"
    message["To"] = to
    message.set_content(body, charset="utf-8")
    port = settings.smtp_port
    smtp = smtplib.SMTP_SSL(host, port, timeout=20) if port == 465 else smtplib.SMTP(host, port, timeout=20)
    with smtp:
        smtp.ehlo()
        if port not in {25, 465} and smtp.has_extn("starttls"):
            smtp.starttls()
            smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(message)


def _price_bucket(value):
    if value is None:
        return None
    return int(round(float(value) * 2))


def decision_fingerprint(candidates, holding):
    return {
        "candidates": [item["symbol"] for item in candidates],
        "symbol": holding["symbol"],
        "stance": holding["stance"],
        "sell": _price_bucket(holding["sell"]),
        "add": _price_bucket(holding["add"]),
    }


def run_brief(settings, email, code, cost, top_n=3, market=None, when_changed=False):
    if email and not EMAIL_PATTERN.fullmatch(email):
        raise ValueError("Invalid notification email.")
    if not email and not when_changed:
        raise ValueError("Invalid notification email.")
    code = symbol(code)
    if cost <= 0:
        raise ValueError("Holding cost must be positive.")
    loaded = (market or _load_market)(code, max(top_n * 12, 36))
    screened = screen(
        loaded["instruments"],
        loaded["metrics"],
        loaded["day"],
        ScreenRule(top_n=top_n, minimum_bars=120),
    )
    names = loaded["names"]
    candidates = [
        {
            "symbol": row["symbol"],
            "name": names.get(row["symbol"], row["symbol"]),
            "score": round(float(row["score"]), 3),
        }
        for row in screened["candidates"][:top_n]
    ]
    held = holding_letter(code, names.get(code, code), cost, loaded["metrics"][code])
    holding = {
        "symbol": code,
        "name": names.get(code, code),
        "stance": held["advice"]["stance"],
        "sell": held["advice"]["sell"],
        "add": held["advice"]["add"],
        "close": round(float(loaded["metrics"][code]["close"]), 2),
    }
    messages = [candidate_letter(loaded["day"], screened, names), {"subject": held["subject"], "body": held["body"]}]
    fingerprint = decision_fingerprint(candidates, holding)
    previous = latest_brief(settings).get("fingerprint")
    changed = fingerprint != previous
    send = bool(email) and (changed or not when_changed)
    delivered = []
    for item in messages:
        record = {"subject": item["subject"], "body": item["body"], "delivered": False, "delivery": "unchanged"}
        if not email:
            record["delivery"] = "no_recipient"
        elif not send:
            record["delivery"] = "unchanged"
        elif not settings.smtp_host.strip():
            record["delivery"] = "smtp_unconfigured"
        else:
            try:
                send_email(settings, email, item["subject"], item["body"])
            except Exception as exc:
                record["delivery"] = type(exc).__name__
            else:
                record["delivered"] = True
                record["delivery"] = "sent"
        delivered.append(record)
    result = {
        "email": email,
        "as_of": str(loaded["day"]),
        "source_note": SOURCE_NOTE,
        "candidates": candidates,
        "holding": holding,
        "fingerprint": fingerprint,
        "changed": changed,
        "updated_at": now().isoformat(),
        "messages": delivered,
    }
    path = Path(settings.observation_root) / "operator_brief.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def latest_brief(settings):
    path = Path(settings.observation_root) / "operator_brief.json"
    if not path.exists():
        return {"messages": []}
    return json.loads(path.read_text(encoding="utf-8"))
