"""Candidate and holding letters, without production market writes."""

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from quant_platform.analysis import indicators
from quant_platform.api import create_app
from quant_platform.config import Settings
from quant_platform.operator_brief import advise_holding, run_brief


def rising(days=130, start=20.0, step=0.05):
    rows = []
    for offset in range(days):
        close = start + step * offset
        rows.append(
            {
                "day": date(2025, 1, 1) + timedelta(days=offset),
                "factor": 1.0,
                "dataset_id": "fixture",
                "data": {
                    "open": close,
                    "high": close + 0.2,
                    "low": close - 0.2,
                    "close": close,
                    "volume": 1_000_000,
                    "turnover": 50_000_000,
                },
            }
        )
    return rows


def market(code, universe):
    names = {"SH600895": "张江高科", "SH600000": "甲", "SZ000001": "乙", "SH601398": "丙", "SZ000002": "丁"}
    series = {
        "SH600895": rising(start=30),
        "SH600000": rising(start=10, step=0.2),
        "SZ000001": rising(start=10, step=0.05),
        "SH601398": rising(start=10, step=0.01),
        "SZ000002": rising(start=40, step=-0.05),
    }
    metrics = {key: indicators(rows) for key, rows in series.items()}
    day = date.fromisoformat(metrics[code]["as_of"])
    return {
        "names": names,
        "metrics": metrics,
        "instruments": {
            key: {"name": names[key], "status": "L", "list_date": None, "delist_date": None, "suspended": False}
            for key in names
        },
        "day": day,
    }


def test_holding_advice_trails_profit_and_adds_only_on_the_average():
    metrics = indicators(rising(start=20, step=0.1))
    advice = advise_holding(metrics, 28)
    assert advice["stance"] == "继续持有"
    assert advice["sell"] >= 28
    assert advice["sell"] < metrics["close"]
    assert advice["add"] == round(metrics["sma20"], 2)
    assert advice["add"] < metrics["close"]


def test_holding_advice_does_not_add_below_the_long_average():
    metrics = indicators(rising(start=40, step=-0.08))
    advice = advise_holding(metrics, 28)
    assert metrics["close"] < metrics["sma60"]
    assert advice["stance"] == "不建议继续持有"
    assert advice["add"] is None
    assert advice["sell"] == round(metrics["close"], 2)


def test_brief_sends_three_candidates_and_records_smtp(tmp_path, monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            assert host == "smtp.test"
            assert port == 587

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return None

        def has_extn(self, name):
            return False

        def send_message(self, message):
            sent.append((message["To"], message["Subject"], message.get_content()))

    monkeypatch.setattr("quant_platform.operator_brief.smtplib.SMTP", FakeSMTP)
    settings = Settings(
        database_url="postgresql://quant@localhost/quant",
        api_token="x" * 32,
        environment="test",
        observation_root=tmp_path,
        smtp_host="smtp.test",
        smtp_from="desk@example.com",
    )
    result = run_brief(settings, "jxiaoping@gmail.com", "600895", 28, market=market)
    assert [item["delivered"] for item in result["messages"]] == [True, True]
    assert [item[0] for item in sent] == ["jxiaoping@gmail.com", "jxiaoping@gmail.com"]
    candidates, holding = (item[2] for item in sent)
    assert sum(line.startswith(("1. ", "2. ", "3. ")) for line in candidates.splitlines()) == 3
    assert "张江高科" in holding
    assert "继续持有" in holding or "不建议继续持有" in holding
    assert "出货参考价" in holding
    assert "补仓参考价" in holding
    assert "不是收益承诺" in holding
    saved = json.loads((tmp_path / "operator_brief.json").read_text())
    assert saved["email"] == "jxiaoping@gmail.com"
    assert "daily_bars" not in Path(run_brief.__code__.co_filename).read_text()


def test_unconfigured_smtp_keeps_the_letter(tmp_path):
    settings = Settings(
        database_url="postgresql://quant@localhost/quant",
        environment="test",
        observation_root=tmp_path,
    )
    result = run_brief(settings, "jxiaoping@gmail.com", "SH600895", 28, market=market)
    assert result["messages"][0]["delivery"] == "smtp_unconfigured"
    assert "得分" in result["messages"][0]["body"]
    assert result["messages"][1]["body"].count("元") >= 2


def test_brief_endpoint_rejects_a_bad_address(tmp_path):
    settings = Settings(
        database_url="postgresql://quant@localhost/quant",
        api_token="y" * 32,
        environment="test",
        observation_root=tmp_path,
    )

    class Store:
        def close(self):
            return None

    with TestClient(create_app(settings, Store())) as client:
        denied = client.post("/api/v1/briefs", json={"email": "jxiaoping@gmail.com", "cost": 28})
        assert denied.status_code == 401
        accepted = client.post(
            "/api/v1/briefs",
            headers={"Authorization": "Bearer " + "y" * 32},
            json={"email": "not-an-email", "symbol": "SH600895", "cost": 28},
        )
        assert accepted.status_code == 422


def test_dashboard_notify_posts_the_holding_cost(monkeypatch):
    from streamlit.testing.v1 import AppTest

    from quant_platform.dashboard import client
    from test_boundaries_dashboard import fake_request

    APP = Path(client.__file__).with_name("app.py")

    calls = []

    def request(self, method, path, data=None, timeout=20):
        calls.append((method, path, data, timeout))
        if path == "/briefs":
            return {
                "messages": [
                    {
                        "subject": "QUAP 候选筛选",
                        "body": "1. 甲得分 0.9\n2. 乙得分 0.8\n3. 丙得分 0.7\n",
                        "delivered": True,
                        "delivery": "sent",
                    },
                    {
                        "subject": "QUAP 张江高科持仓建议（成本 28.00 元）",
                        "body": "建议：继续持有\n出货参考价：30.00 元\n补仓参考价：27.50 元\n",
                        "delivered": True,
                        "delivery": "sent",
                    },
                ]
            }
        return fake_request(self, method, path, data)

    monkeypatch.setattr("quant_platform.dashboard.client.Client.request", request)
    app = AppTest.from_file(str(APP), default_timeout=15)
    app.session_state["token"] = "test-token"
    app.run()
    app.sidebar.radio[0].set_value("Notify").run()
    app.text_input[0].set_value("jxiaoping@gmail.com").run()
    next(button for button in app.button if button.label == "Send candidate and holding mail now").click().run()
    assert not app.exception
    payload = next(data for method, path, data, timeout in calls if path == "/briefs")
    assert payload["email"] == "jxiaoping@gmail.com"
    assert payload["symbol"] == "SH600895"
    assert payload["cost"] == 28.0
    assert payload["top_n"] == 3
    assert any("继续持有" in text.value for text in app.text)
    assert any(timeout == 90 for method, path, data, timeout in calls if path == "/briefs")
