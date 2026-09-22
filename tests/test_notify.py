from pydantic import SecretStr
import pytest

from quant_platform.analysis import holding_advice
from quant_platform.config import Settings
from quant_platform.jobs.notify import (
    WATCH_COST,
    deliver,
    holding_notice,
    screen_notice,
    smtp_ready,
    watch_levels,
)
from quant_platform.providers import Deferred


def test_notify_defaults_to_operator_gmail():
    settings = Settings(api_token="a" * 32, tushare_token="test-only-fixture", environment="test")
    assert settings.notify_email == "jxiaoping@gmail.com"
    assert settings.smtp_host == "smtp.gmail.com"
    assert not smtp_ready(settings)
    settings = settings.model_copy(update={"smtp_password": SecretStr("app-password")})
    assert smtp_ready(settings)


def test_screen_notice_lists_top_three_reasons():
    candidates = [
        {
            "symbol": "SH600895",
            "score": 0.81,
            "pe_ttm": 12.3,
            "pb": 1.2,
            "industry": "园区开发",
            "peer_scope": "industry",
            "pe_industry_pct": 0.22,
            "pb_industry_pct": 0.30,
            "pe_history_pct": 0.18,
            "pb_history_pct": 0.25,
            "average_turnover20": 80_000_000,
            "reasons": ["low_pe_ttm_vs_peers", "low_pb_vs_own_history"],
            "action": "review_entry",
        },
        {"symbol": "SZ000001", "score": 0.7, "pe_ttm": 8, "pb": 0.9, "reasons": ["relative_valuation_rank"]},
        {"symbol": "SZ000002", "score": 0.6, "pe_ttm": 9, "pb": 1.1, "reasons": ["low_pb_vs_peers"]},
        {"symbol": "SH600000", "score": 0.5, "pe_ttm": 10, "pb": 1.3, "reasons": ["low_pe_ttm_vs_own_history"]},
    ]
    names = {"SH600895": "张江高科", "SZ000001": "平安银行", "SZ000002": "万科A"}
    message = screen_notice(candidates, names, "2026-09-21", "2026-09-22")
    assert message["subject"] == "【QUAP 研究】2026-09-22 相对估值候选（3只）"
    assert "SH600000" not in message["body"]
    assert "张江高科" in message["body"] and "PE_TTM 低于同业分位" in message["body"]
    assert "不是订单" in message["body"]
    assert "复核是否纳入观察" in message["body"]
    assert "建议动作：review_entry" not in message["body"]
    empty = screen_notice([], {}, None, "2026-09-22")
    assert "没有可用的相对估值候选" in empty["body"]


def test_holding_notice_levels_and_actions():
    levels = watch_levels(WATCH_COST)
    assert levels["exit_price"] == 23.8
    assert levels["reduce_price"] == 33.6
    assert levels["add_ceiling"] == pytest.approx(28.56)
    holding = {"symbol": "SH600895", "cost_price": WATCH_COST}
    held = holding_advice(holding, last_close=29.5, screen_candidates=[{"symbol": "SH600895"}])
    message = holding_notice(held, "张江高科", "2026-09-22", levels)
    assert "继续持有" in message["body"]
    assert "23.80" in message["body"] and "33.60" in message["body"] and "28.56" in message["body"]
    assert (
        "考虑出货" in holding_notice(holding_advice(holding, last_close=23.8), "张江高科", "2026-09-22", levels)["body"]
    )
    assert (
        "考虑补仓"
        in holding_notice(
            holding_advice(holding, last_close=28.2, screen_candidates=[{"symbol": "SH600895"}]),
            "张江高科",
            "2026-09-22",
            levels,
        )["body"]
    )
    assert (
        "考虑减仓" in holding_notice(holding_advice(holding, last_close=33.6), "张江高科", "2026-09-22", levels)["body"]
    )
    assert message["subject"].endswith("张江高科（SH600895）持仓建议")


class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.actions = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self):
        self.actions.append("starttls")

    def login(self, user, password):
        self.actions.append(("login", user, password))

    def send_message(self, message):
        self.actions.append(("send", str(message["To"]), str(message["Subject"]), message.get_content()))


def test_deliver_uses_starttls_and_login(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr("quant_platform.jobs.notify.smtplib.SMTP", FakeSMTP)
    settings = Settings(
        api_token="a" * 32,
        tushare_token="test-only-fixture",
        environment="test",
        smtp_password=SecretStr("app-password"),
    )
    deliver(settings, {"subject": "主题", "body": "正文\n"})
    client = FakeSMTP.instances[0]
    assert client.host == "smtp.gmail.com" and client.port == 587
    assert client.actions[0] == "starttls"
    assert client.actions[1] == ("login", "jxiaoping@gmail.com", "app-password")
    assert client.actions[2][1:] == ("jxiaoping@gmail.com", "主题", "正文\n")


def test_deliver_connection_failure_is_deferred(monkeypatch):
    def boom(*args, **kwargs):
        raise ConnectionRefusedError("offline")

    monkeypatch.setattr("quant_platform.jobs.notify.smtplib.SMTP", boom)
    settings = Settings(
        api_token="a" * 32,
        tushare_token="test-only-fixture",
        environment="test",
        smtp_password=SecretStr("app-password"),
    )
    try:
        deliver(settings, {"subject": "主题", "body": "正文"})
    except Deferred as exc:
        assert exc.seconds == 1800
        assert "smtp" in str(exc)
    else:
        raise AssertionError("SMTP failures must defer")
