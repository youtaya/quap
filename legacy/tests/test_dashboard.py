# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
from pathlib import Path

import pytest
import yaml

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from legacy_monitor.client import LiveClient
from legacy_monitor.models import shanghai_now
from legacy_monitor.state import RuntimeStore, load_config

APP = Path(__file__).resolve().parents[1] / "examples" / "market_monitor" / "app.py"


def widget(elements, label):
    return next(element for element in elements if element.label == label)


@pytest.fixture
def app(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"mode": "demo", "database": "events.sqlite3", "baskets": {}}), encoding="utf-8")
    monkeypatch.setenv("QLIB_MONITOR_CONFIG", str(config))
    return AppTest.from_file(str(APP), default_timeout=20).run()


def test_demo_dashboard_controls_and_persistence(app):
    assert not app.exception
    assert {metric.label for metric in app.metric} == {"科创板", "创业板", "主板"}
    captions = "\n".join(caption.value for caption in app.caption)
    assert "有效轮询间隔：暂无" in captions
    assert "暂无秒" not in captions
    assert app.session_state["engine"].provider.step == 1
    widget(app.button, "立即刷新").click().run()
    widget(app.button, "立即刷新").click().run()
    assert not app.exception
    engine = app.session_state["engine"]
    assert engine.store.recent("demo")
    assert len(engine.samples) == 3
    assert app.toast
    assert any("阈值" in toast.value for toast in app.toast)
    widget(app.multiselect, "显示板块").set_value(["STAR Market"]).run()
    assert {metric.label for metric in app.metric} == {"科创板"}
    widget(app.button, "开始").click().run()
    assert app.session_state["running"]
    widget(app.button, "暂停").click().run()
    assert not app.session_state["running"]
    assert not app.exception


def test_apply_custom_basket_and_reject_invalid_yaml(app):
    widget(app.text_area, "自定义篮子（YAML）").set_value(
        "My Basket:\n  symbols: [SH688001, SZ000001]\n  weights: [2, 1]"
    )
    widget(app.button, "应用设置").click().run()
    assert not app.exception
    assert "My Basket" in app.session_state["engine"].members
    widget(app.selectbox, "成分分组").set_value("My Basket").run()
    assert not app.exception
    widget(app.text_area, "自定义篮子（YAML）").set_value("bad: [")
    widget(app.button, "应用设置").click().run()
    assert not app.exception
    assert any("未能应用" in error.value for error in app.error)


def test_live_failure_never_falls_back_to_demo(app):
    widget(app.selectbox, "数据模式").set_value("live")
    widget(app.button, "应用设置").click().run()
    assert not app.exception
    assert isinstance(app.session_state["engine"], LiveClient)
    assert app.session_state["engine"].provider.mode == "live"
    assert app.session_state["engine"].result.snapshot is None
    assert widget(app.button, "立即刷新").disabled
    assert any("disconnected" in error.value for error in app.error)


def test_default_live_attachment_persists_acknowledged_settings(tmp_path, monkeypatch):
    path = tmp_path / "live.yaml"
    path.write_text(yaml.safe_dump({"database": "events.sqlite3", "history": {"enabled": False}}))
    monkeypatch.setenv("QLIB_MONITOR_CONFIG", str(path))
    config = load_config(path)
    store = RuntimeStore(config["database"])
    settings = {**config["settings"], "poll_seconds": 45}
    store.set("settings", {"revision": 2, "data": settings})
    store.set(
        "service", {"status": "running", "heartbeat": shanghai_now().isoformat(), "epoch": "testing", "polling": True}
    )
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert isinstance(app.session_state["engine"], LiveClient)
    assert app.session_state["running"]
    assert widget(app.number_input, "轮询间隔（秒）").value == 45
    widget(app.number_input, "轮询间隔（秒）").set_value(60)
    widget(app.button, "应用设置").click().run()
    assert not app.exception
    command = store.pending("testing")[0]
    assert command["action"] == "apply"
    assert command["payload"]["revision"] == 2
    assert store.get("settings")["data"]["poll_seconds"] == 45
    store.set("settings", {"revision": 3, "data": command["payload"]["settings"]})
    store.acknowledge(command["id"])
    reopened = AppTest.from_file(str(APP)).run()
    assert widget(reopened.number_input, "轮询间隔（秒）").value == 60
    widget(reopened.selectbox, "数据模式").set_value("demo")
    widget(reopened.button, "应用设置").click().run()
    assert not reopened.exception
    assert reopened.session_state["engine"].provider.mode == "demo"
    assert store.get("settings")["revision"] == 3
    assert store.health()["polling"]


def test_history_visible_without_successful_live_quotes(tmp_path, monkeypatch):
    import pandas as pd

    path = tmp_path / "live.yaml"
    path.write_text(yaml.safe_dump({"mode": "live", "database": "events.sqlite3"}))
    monkeypatch.setenv("QLIB_MONITOR_CONFIG", str(path))
    store = RuntimeStore(load_config(path)["database"])
    store.set("history", {"status": "downloading", "total": 100, "attempted": 20, "successful": 19, "failed": 1})
    data = pd.DataFrame(
        {"$close": [10], "$volume": [100]},
        index=pd.MultiIndex.from_tuples([("SH600895", pd.Timestamp("2026-09-21"))], names=["instrument", "datetime"]),
    )
    monkeypatch.setattr("legacy_monitor.updater.ManagedDataset.features", lambda *args: data)
    app = AppTest.from_file(str(APP)).run()
    assert any("downloading" in caption.value for caption in app.caption)
    widget(app.button, "加载历史数据").click().run()
    assert not app.exception
    assert app.session_state["history_frame"]["$close"].tolist() == [10]
    assert app.session_state["engine"].result.snapshot is None
