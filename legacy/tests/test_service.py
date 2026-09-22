# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
import threading
import time
from datetime import timedelta
from unittest.mock import Mock

import pytest
import yaml
from filelock import FileLock, Timeout

from legacy_monitor import AlertStore, MonitorEngine, Snapshot
from legacy_monitor.client import LiveClient
from legacy_monitor.models import shanghai_now
from legacy_monitor.providers import QuoteProvider
from legacy_monitor.service import MonitorService
from legacy_monitor.state import RuntimeStore, engine_packet, load_config, validate_settings


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"mode": "live", "database": "alerts.sqlite3", "history": {"enabled": False}}))
    return load_config(path)


class Provider(QuoteProvider):
    source = "Test live"

    def __init__(self, quote):
        self.quote = quote
        self.session = Mock()
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return Snapshot((self.quote,), self.source, self.mode, self.quote.received_at, 1)


def wait_for(check, timeout=8):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError("Timed out waiting for test service state")


def test_no_browser_service_polls_singleton_pause_settings_restart(config, make_quote):
    provider = Provider(make_quote())
    directory = Mock()
    directory.fetch.return_value = {"symbols": {"SH600000": "Example"}, "fetched_at": shanghai_now().isoformat()}
    service = MonitorService(config, lambda: directory, lambda _: provider)
    thread = threading.Thread(target=service.run)
    thread.start()
    try:
        wait_for(lambda: service.store.health()["healthy"] and provider.calls == 1)
        assert service.store.get("live")["snapshot"]["mode"] == "live"
        with pytest.raises(Timeout):
            MonitorService(config, lambda: directory, lambda _: provider).run()
        pause = service.store.command("pause")
        wait_for(lambda: service.store.command_status(pause)["status"] == "applied")
        assert service.store.get("polling") is False
        payload = {"revision": 0, "settings": {**config["settings"], "poll_seconds": 45}}
        update = service.store.command("apply", payload)
        wait_for(lambda: service.store.command_status(update)["status"] == "applied")
        assert service.store.get("settings")["revision"] == 1
        assert service.running is False
        conflict = service.store.command("apply", payload)
        wait_for(lambda: service.store.command_status(conflict)["status"] == "rejected")
        resume = service.store.command("resume")
        wait_for(lambda: service.store.command_status(resume)["status"] == "applied")
        # A reload/client attachment must not create an extra live poller.
        clients = [LiveClient(config), LiveClient(config)]
        for client in clients:
            client.view()
        assert provider.calls == 1
        service.store.command("stop")
        thread.join(timeout=8)
        assert not thread.is_alive()
        assert service.store.health()["status"] == "stopped"
        restarted = MonitorService(config, lambda: directory, lambda _: provider)
        assert restarted.settings["data"]["poll_seconds"] == 45
        assert restarted.running
    finally:
        service.stop_event.set()
        thread.join(timeout=8)


def test_client_recomputes_freshness_without_replaying_alerts(config, clock, make_quote):
    store = RuntimeStore(config["database"])
    engine = MonitorEngine(
        Provider(make_quote()), AlertStore(config["database"]), clock=clock, monotonic=clock.monotonic
    )
    engine.poll()
    events = engine.store.recent("live")
    assert events
    store.set("live", engine_packet(engine))
    store.set(
        "service", {"status": "running", "heartbeat": shanghai_now().isoformat(), "epoch": "test", "polling": True}
    )
    client = LiveClient(config)
    client.engine.clock = clock
    assert client.view().summaries["Main Board"].eligible == 1
    clock.advance(121)
    assert client.view().summaries["Main Board"].eligible == 0
    assert engine.store.recent("live") == events
    assert client.provider.source == "Background service"
    store.set("service", {"status": "running", "heartbeat": (shanghai_now() - timedelta(seconds=20)).isoformat()})
    assert "disconnected" in client.view().error
    assert client.result.snapshot is not None
    with pytest.raises(RuntimeError, match="disconnected"):
        client.send("refresh")


def test_stale_directory_disables_aggregate_but_not_stock_alerts(config, clock, make_quote):
    provider = Provider(make_quote())
    provider.directory_fresh = False
    engine = MonitorEngine(provider, AlertStore(config["database"]), clock=clock, monotonic=clock.monotonic)
    events = engine.poll().events
    assert events
    assert all(event["target"] == "SH600000" for event in events)


def test_history_scheduler_has_only_one_worker(config, monkeypatch):
    config["history"]["enabled"] = True
    service = MonitorService(config)
    service.store.set("directory", {"symbols": {"SH600895": "Example"}})
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    monkeypatch.setattr("legacy_monitor.service.subprocess.Popen", spawn)
    service.schedule_history()
    service.schedule_history()
    assert spawn.call_count == 1
    process.poll.return_value = 1
    service.schedule_history()
    assert service.store.get("history")["status"] == "failed"
    assert spawn.call_count == 1


@pytest.mark.parametrize(
    "settings",
    [
        {"poll_seconds": 0},
        {"poll_seconds": True},
        {"quote_provider": "demo"},
        {"baskets": {"index": {"universe": "csi300"}}},
        {"unknown": 1},
    ],
)
def test_invalid_live_settings_are_rejected(settings):
    with pytest.raises(ValueError):
        validate_settings(settings)


def test_legacy_start_requires_explicit_opt_in(monkeypatch):
    from legacy_monitor.service import require_legacy_opt_in, start_service

    monkeypatch.delenv("QLIB_MONITOR_LEGACY_ENABLE", raising=False)
    with pytest.raises(RuntimeError, match="Legacy public-feed startup is disabled"):
        start_service("unused-config.yaml")
    monkeypatch.setenv("QLIB_MONITOR_LEGACY_ENABLE", "1")
    require_legacy_opt_in()


def test_runtime_paths_and_default_live_mode(config):
    assert config["settings"]["quote_provider"] == "tencent"
    store = RuntimeStore(config["database"])
    assert store.root == __import__("pathlib").Path(config["path"]).parent
    assert not store.health()["healthy"]
    with FileLock(str(store.root / "service.lock")):
        with pytest.raises(Timeout):
            MonitorService(config).run()
