# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Dashboard facade: reads persisted live data and never polls or evaluates alerts."""

import sqlite3
from datetime import date

from .alerts import AlertStore
from .engine import MonitorEngine
from .market import TradingCalendar
from .models import AlertSettings
from .providers import QuoteProvider
from .state import RuntimeStore, restore_engine


class StoredProvider(QuoteProvider):
    source = "Background service"

    def fetch(self):
        raise RuntimeError("Dashboard clients must not fetch live market data.")


class LiveClient:
    """Expose the engine's display interface without giving the UI poll ownership."""

    def __init__(self, config):
        self.runtime = RuntimeStore(config["database"])
        self.config = config
        try:
            store = AlertStore(config["database"])
        except (OSError, sqlite3.Error):
            store = None
        self.engine = MonitorEngine(StoredProvider(), store)
        self.last_packet = None
        self.command_id = None
        self.view()

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def send(self, action, payload=None):
        self.command_id = self.runtime.command(action, payload)
        return self.command_id

    def poll(self, force=False):
        if force:
            self.send("refresh")
        return self.view()

    def view(self):
        packet = self.runtime.get("live", {})
        if packet.get("saved_at") != self.last_packet:
            restore_engine(self.engine, packet)
            self.last_packet = packet.get("saved_at")
        saved = self.runtime.get("settings", {"data": self.config["settings"]})
        self.engine.settings = AlertSettings(**saved["data"]["alerts"])
        self.engine.interval = saved["data"]["poll_seconds"]
        days = packet.get("calendar") or self.runtime.get("calendar", {}).get("days", [])
        self.engine.calendar = TradingCalendar(date.fromisoformat(day) for day in days)
        result = self.engine.view()
        health = self.runtime.health()
        failure = None
        if not health["healthy"]:
            failure = "Background service is stopped or disconnected. Retained data is not live."
        elif health.get("worker_unresponsive"):
            failure = "Live worker is unresponsive. Retained data is not live; inspect service logs."
        elif health.get("worker_error"):
            failure = health["worker_error"]
        result.error = " | ".join(str(item) for item in (packet.get("error"), failure) if item) or None
        return result
