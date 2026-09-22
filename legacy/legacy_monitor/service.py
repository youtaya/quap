# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Browser-independent monitoring service. Run ``python -m ...service --help``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from filelock import FileLock, Timeout

from .alerts import AlertStore
from .engine import MonitorEngine
from .feeds import SinaDirectory, TencentProvider
from .market import TradingCalendar, parse_baskets
from .models import AlertSettings, shanghai_now
from .providers import EastmoneyProvider, FeedError
from .state import RuntimeStore, engine_packet, load_config, restore_engine, validate_settings
from .updater import ExternalHistory, HistoryUpdater, next_update

LOGGER = logging.getLogger(__name__)
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "examples" / "market_monitor" / "config.yaml"


class MonitorService:
    """Single writer of live observations; the supervisor never blocks on quotes."""

    def __init__(self, config, directory_factory=SinaDirectory, provider_factory=None):
        self.config = config
        self.store = RuntimeStore(config["database"])
        self.epoch = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.directory_factory, self.provider_factory = directory_factory, provider_factory
        self.history_process = None
        self.worker = None
        self.engine = None
        self.settings = self.store.get("settings", {"revision": 0, "data": config["settings"]})
        self.settings["data"] = validate_settings(self.settings["data"])
        self.running = self.store.get("polling", True)
        self.worker_status = "starting"
        self.worker_error = None
        self.worker_tick = time.monotonic()

    def heartbeat(self, status="running"):
        self.store.set(
            "service",
            {
                "status": status,
                "heartbeat": shanghai_now().isoformat(),
                "epoch": self.epoch,
                "pid": os.getpid(),
                "polling": self.running,
                "revision": self.settings["revision"],
                "worker": self.worker_status,
                "worker_error": self.worker_error,
                "worker_unresponsive": time.monotonic() - self.worker_tick > 210,
            },
        )

    def run(self):
        with FileLock(str(self.store.root / "service.lock"), timeout=0):
            self.store.set("settings", self.settings)
            self.heartbeat()
            self.worker = threading.Thread(target=self.live_loop, name="market-quotes", daemon=False)
            self.worker.start()
            try:
                while not self.stop_event.wait(1):
                    self.process_controls()
                    self.schedule_history()
                    if not self.stop_event.is_set() and not self.worker.is_alive():
                        self.worker_error = "Live worker exited unexpectedly. Restart the service."
                        raise RuntimeError(self.worker_error)
                    self.heartbeat()
            finally:
                self.stop_event.set()
                self.heartbeat("stopping")
                # HTTP operations and Qlib reads have explicit upper bounds.
                self.worker.join()
                if self.history_process is not None:
                    try:
                        self.history_process.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        # This is our own child, not an arbitrary PID from a stale file.
                        self.history_process.terminate()
                        self.history_process.wait(timeout=30)
                self.heartbeat("stopped")

    def process_controls(self):
        for command in self.store.pending(self.epoch):
            action = command["action"]
            if action == "stop":
                self.stop_event.set()
            elif action in {"pause", "resume"}:
                self.running = action == "resume"
                self.store.set("polling", self.running)
            else:
                continue
            self.store.acknowledge(command["id"])

    def build_engine(self, settings, directory):
        baskets = parse_baskets(settings["baskets"])
        history = None
        if settings["provider_uri"]:
            history = ExternalHistory(settings["provider_uri"])
            history.resolve(baskets, shanghai_now().date())
        if self.provider_factory:
            provider = self.provider_factory(directory)
        elif settings["quote_provider"] == "eastmoney":
            provider = EastmoneyProvider()
        else:
            provider = TencentProvider(directory["symbols"])
        try:
            store = AlertStore(self.config["database"])
        except (OSError, sqlite3.Error):
            LOGGER.exception("Alert database unavailable")
            store = None
        engine = MonitorEngine(
            provider, store, baskets, AlertSettings(**settings["alerts"]), history, settings["poll_seconds"]
        )
        return engine

    def live_commands(self, directory):
        manual = False
        for command in self.store.pending(self.epoch):
            if command["action"] not in {"apply", "refresh"}:
                continue
            try:
                if command["action"] == "apply":
                    payload = command["payload"]
                    if payload.get("revision") != self.settings["revision"]:
                        raise ValueError("Settings changed in another session; reload before applying.")
                    settings = validate_settings(payload["settings"])
                    if settings["provider_uri"]:
                        external = Path(settings["provider_uri"]).expanduser()
                        settings["provider_uri"] = str((Path(self.config["path"]).parent / external).resolve())
                    replacement = self.build_engine(settings, directory)
                    old = self.engine
                    if old is not None:
                        # Configuration changes must not bypass quote-feed throttling.
                        replacement.next_poll = old.next_poll
                        replacement.last_attempt = old.last_attempt
                        replacement.failures = old.failures
                    self.settings = {"revision": self.settings["revision"] + 1, "data": settings}
                    self.store.set("settings", self.settings)
                    self.engine = replacement
                    self.store.set("live", engine_packet(replacement))
                    if old is not None:
                        old.provider.session.close()
                else:
                    manual = True
                self.store.acknowledge(command["id"])
            except Exception as exc:
                self.store.acknowledge(command["id"], str(exc))
        return manual

    def live_loop(self):
        directory_feed = self.directory_factory()
        retry_directory = shanghai_now()
        directory = self.store.get("directory", {})
        try:
            while not self.stop_event.is_set():
                self.worker_tick = time.monotonic()
                try:
                    now = shanghai_now()
                    if now >= retry_directory:
                        self.worker_status = "discovering"
                        try:
                            directory = directory_feed.fetch()
                            self.store.set("directory", directory)
                            self.store.set("directory_error", None)
                            self.store.set("directory_retry_at", None)
                            retry_directory = next_update(now)
                        except Exception as exc:
                            self.store.set("directory_error", str(exc))
                            retry_directory = shanghai_now() + timedelta(minutes=30)
                            self.store.set("directory_retry_at", retry_directory.isoformat())
                    if not directory.get("symbols"):
                        raise FeedError("Waiting for a complete Shanghai/Shenzhen security directory.")
                    if self.engine is None:
                        self.engine = self.build_engine(self.settings["data"], directory)
                        restore_engine(self.engine, self.store.get("live", {}))
                    manual = self.live_commands(directory)
                    engine = self.engine
                    if isinstance(engine.provider, TencentProvider):
                        engine.provider.symbols = dict(directory["symbols"])
                    age = shanghai_now() - datetime.fromisoformat(directory["fetched_at"])
                    engine.provider.directory_fresh = timedelta(0) <= age <= timedelta(hours=48)
                    if engine.history is None:
                        calendar = self.store.get("calendar", {})
                        engine.calendar = TradingCalendar(date.fromisoformat(day) for day in calendar.get("days", []))
                    self.worker_status = "polling" if self.running or manual else "paused"
                    self.worker_tick = time.monotonic()
                    previous = engine.last_attempt
                    if self.running or manual:
                        engine.poll(force=manual)
                    if previous != engine.last_attempt:
                        self.store.set("live", engine_packet(engine))
                    self.worker_error = None
                except Exception as exc:
                    self.worker_error = str(exc)
                    self.worker_status = "failed"
                    LOGGER.exception("Live worker iteration failed")
                    self.stop_event.wait(15)
                self.worker_tick = time.monotonic()
                self.stop_event.wait(0.5)
        finally:
            directory_feed.http.close()
            if self.engine is not None:
                self.engine.provider.session.close()

    def schedule_history(self):
        if not self.config["history"]["enabled"] or self.stop_event.is_set():
            return
        if self.history_process is not None:
            code = self.history_process.poll()
            if code is None:
                return
            self.history_process = None
            if code:
                state = self.store.get("history", {})
                self.store.set(
                    "history",
                    {
                        **state,
                        "status": "failed",
                        "error": state.get("error") or f"History worker exited ({code})",
                        "next_update": (shanghai_now() + timedelta(minutes=30)).isoformat(),
                    },
                )
        if not self.store.get("directory", {}).get("symbols"):
            return
        state = self.store.get("history", {})
        due = state.get("next_update")
        if state.get("status") in {"downloading", "publishing", "calendar", "interrupted"}:
            due = None
        if due and shanghai_now() < datetime.fromisoformat(due):
            return
        self.history_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "legacy_monitor.service",
                "update",
                "--config",
                self.config["path"],
                "--epoch",
                self.epoch,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def require_legacy_opt_in():
    if os.environ.get("QLIB_MONITOR_LEGACY_ENABLE") != "1":
        raise RuntimeError(
            "Legacy public-feed startup is disabled. Use the quap quant-platform service for production. "
            "For an intentional experimental/rollback run, stop the replacement collectors first "
            "and set QLIB_MONITOR_LEGACY_ENABLE=1. Legacy status/stop remain available."
        )


def start_service(config_path):
    require_legacy_opt_in()
    config = load_config(config_path)
    store = RuntimeStore(config["database"])
    with FileLock(str(store.root / "start.lock"), timeout=15):
        if store.health()["healthy"]:
            return store.health()
        # Check the OS lock, not the stored PID. A busy unhealthy service must not be duplicated.
        try:
            with FileLock(str(store.root / "service.lock"), timeout=0):
                pass
        except Timeout as exc:
            raise RuntimeError("Service lock is held but heartbeat is stale; inspect local logs.") from exc
        log = store.root / "launch.log"
        if log.exists() and log.stat().st_size > 1024 * 1024:
            os.replace(log, store.root / "launch.previous.log")
        with log.open("ab") as stream:
            process = subprocess.Popen(
                [sys.executable, "-m", "legacy_monitor.service", "run", "--config", config["path"]],
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=stream,
                start_new_session=True,
            )
        for _ in range(100):
            if store.health()["healthy"]:
                return store.health()
            if process.poll() is not None:
                raise RuntimeError(f"Service exited during startup; inspect {log}.")
            time.sleep(0.1)
        raise RuntimeError(f"Service startup not confirmed; inspect {log} before retrying.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "run", "status", "stop", "update"))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--epoch", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.action in {"start", "run"}:
        try:
            require_legacy_opt_in()
        except RuntimeError as exc:
            parser.exit(1, str(exc) + "\n")
    config = load_config(args.config)
    store = RuntimeStore(config["database"])
    if args.action == "start":
        print(json.dumps(start_service(args.config), indent=2))
    elif args.action == "status":
        print(
            json.dumps(
                {
                    "service": store.health(),
                    "history": store.get("history", {}),
                    "directory_error": store.get("directory_error"),
                },
                indent=2,
            )
        )
    elif args.action == "stop":
        print("Stop requested:", store.command("stop"))
    elif args.action == "update":
        if not args.epoch:
            parser.error("The update worker is started by the running service.")

        def stopped():
            health = store.health()
            return not health["healthy"] or health.get("epoch") != args.epoch

        if not stopped():
            HistoryUpdater(store, config["history"], stopped=stopped).run()
    else:
        handler = RotatingFileHandler(
            store.root / "service.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
        service = MonitorService(config)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: service.stop_event.set())
        try:
            service.run()
        except Timeout:
            parser.exit(1, "Another monitor service already owns this runtime directory.\n")


if __name__ == "__main__":
    main()
