# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Validated configuration and transactional, browser-independent local state."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import yaml

from .market import TradingCalendar, parse_baskets
from .models import AlertSettings, Quote, Snapshot, number, shanghai_now

EDITABLE = {"poll_seconds", "quote_provider", "provider_uri", "alerts", "baskets"}


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - EDITABLE:
        raise ValueError("Unknown live-service settings.")
    result = {
        "poll_seconds": 30,
        "quote_provider": "tencent",
        "provider_uri": None,
        "alerts": {},
        "baskets": {},
        **value,
    }
    interval = number(result["poll_seconds"])
    if isinstance(result["poll_seconds"], bool) or interval is None or not 15 <= interval <= 3600:
        raise ValueError("Poll interval must be between 15 and 3600 seconds.")
    if result["quote_provider"] not in {"tencent", "eastmoney"}:
        raise ValueError("Select tencent or eastmoney explicitly.")
    if result["provider_uri"] is not None and not isinstance(result["provider_uri"], str):
        raise ValueError("External Qlib dataset must be a local path.")
    try:
        AlertSettings(**result["alerts"])
        baskets = parse_baskets(result["baskets"])
    except TypeError as exc:
        raise ValueError("Invalid alert or basket settings.") from exc
    if any(basket.universe for basket in baskets) and not result["provider_uri"]:
        raise ValueError("Named universes require an external Qlib dataset with dated membership.")
    return result


def load_config(path):
    path = Path(path).expanduser().resolve()
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict) or config.get("mode", "live") not in {"live", "demo"}:
        raise ValueError("Configuration must be a mapping with mode live or demo.")
    settings = validate_settings({key: config[key] for key in EDITABLE if key in config})
    if settings["provider_uri"]:
        external = Path(settings["provider_uri"]).expanduser()
        settings["provider_uri"] = str((path.parent / external).resolve())
    database = Path(config.get("database", ".runtime/alerts.sqlite3")).expanduser()
    database = (path.parent / database).resolve()
    historical = {"enabled": True, "bars": 500, "update_at": "18:30", **config.get("history", {})}
    if not isinstance(historical["enabled"], bool):
        raise ValueError("history.enabled must be a boolean.")
    bars = historical["bars"]
    if isinstance(bars, bool) or not isinstance(bars, int) or not 5 <= bars <= 630:
        raise ValueError("history.bars must be an integer between 5 and 630.")
    try:
        cutoff = datetime.strptime(historical["update_at"], "%H:%M").time()
        if cutoff.hour < 18:
            raise ValueError("Daily-history cutoff must be at or after 18:00 China time.")
    except (TypeError, ValueError) as exc:
        raise ValueError("history.update_at must be HH:MM, at or after 18:00 China time.") from exc
    return {**config, "path": str(path), "database": str(database), "settings": settings, "history": historical}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=lambda item: item.isoformat())


class RuntimeStore:
    """Short SQLite transactions; each operation owns its connection."""

    def __init__(self, database):
        self.path = Path(database).expanduser().resolve().with_name("service.sqlite3")
        self.root = self.path.parent
        self.root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS monitor_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, epoch TEXT NOT NULL,
                    action TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history_windows (
                    symbol TEXT PRIMARY KEY, rows_json TEXT, target TEXT NOT NULL,
                    fetched_at TEXT NOT NULL, end_date TEXT, error TEXT, bars INTEGER NOT NULL
                );
                """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.Connection(self.path, timeout=10)) as db:
            db.row_factory = sqlite3.Row
            with db:
                yield db

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM monitor_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES (?, ?)", (key, json_text(value)))

    def health(self, now=None):
        state = self.get("service", {})
        try:
            age = ((now or shanghai_now()) - datetime.fromisoformat(state["heartbeat"])).total_seconds()
            healthy = state.get("status") == "running" and 0 <= age <= 15
        except (KeyError, TypeError, ValueError):
            healthy = False
        return {**state, "healthy": healthy}

    def command(self, action, payload=None):
        health = self.health()
        if not health["healthy"]:
            raise RuntimeError("Service is stopped or disconnected; start it before sending commands.")
        if action not in {"apply", "pause", "resume", "refresh", "stop"}:
            raise ValueError("Unknown service command.")
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO commands(epoch, action, payload, created_at) VALUES (?, ?, ?, ?)",
                (health["epoch"], action, json_text(payload or {}), shanghai_now().isoformat()),
            )
            command_id = cursor.lastrowid
            db.execute("DELETE FROM commands WHERE id < ? AND status != 'pending'", (command_id - 1000,))
        return command_id

    def pending(self, epoch):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM commands WHERE epoch=? AND status='pending' ORDER BY id", (epoch,)
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def acknowledge(self, command_id, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE commands SET status=?, message=? WHERE id=?",
                ("rejected" if error else "applied", str(error or ""), command_id),
            )

    def command_status(self, command_id):
        with self.connect() as db:
            row = db.execute("SELECT id, status, message FROM commands WHERE id=?", (command_id,)).fetchone()
        return dict(row) if row else None

    def window(self, symbol):
        with self.connect() as db:
            row = db.execute("SELECT * FROM history_windows WHERE symbol=?", (symbol,)).fetchone()
        return dict(row) if row else None

    def save_window(self, symbol, rows, target, bars, error=None, now=None):
        old = self.window(symbol)
        if rows:
            content, end = json_text(rows), rows[-1][0]
        else:
            content, end = (old["rows_json"], old["end_date"]) if old else (None, None)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO history_windows VALUES (?, ?, ?, ?, ?, ?, ?)",
                (symbol, content, target, (now or shanghai_now()).isoformat(), end, error, bars),
            )

    def windows(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM history_windows ORDER BY symbol").fetchall()
        return {row["symbol"]: dict(row) for row in rows}


def engine_packet(engine):
    """One atomic publication includes quotes, chart scope, and poll failure state."""
    return {
        "snapshot": asdict(engine.result.snapshot) if engine.result.snapshot else None,
        "members": engine.members,
        "samples": list(engine.samples),
        "scope": engine._scope,
        "observation": engine._last_observation,
        "effective_interval": engine.effective_interval,
        "error": engine.result.error,
        "storage_error": engine.result.storage_error,
        "duration": engine.result.duration,
        "events": engine.result.events,
        "calendar": sorted(str(day) for day in engine.calendar.days),
        "saved_at": shanghai_now().isoformat(),
    }


def restore_engine(engine, packet):
    """Restore display state only; never replay alert evaluation from persisted data."""
    snapshot = packet.get("snapshot")
    engine.result.snapshot = None
    if snapshot:
        quotes = []
        for raw in snapshot["quotes"]:
            row = dict(raw)
            for key in ("timestamp", "received_at"):
                if isinstance(row.get(key), str):
                    row[key] = datetime.fromisoformat(row[key])
            quotes.append(Quote(**row))
        engine.result.snapshot = Snapshot(
            tuple(quotes),
            snapshot["source"],
            snapshot["mode"],
            datetime.fromisoformat(snapshot["fetched_at"]),
            snapshot["reported_total"],
            snapshot.get("excluded", 0),
            tuple(snapshot.get("warnings", [])),
        )
    engine.members = {
        name: tuple(tuple(pair) for pair in members) for name, members in packet.get("members", {}).items()
    }
    engine.samples.clear()
    for row in packet.get("samples", [])[-600:]:
        engine.samples.append({**row, "time": datetime.fromisoformat(row["time"])})
    engine._scope, engine._last_observation = packet.get("scope"), packet.get("observation")
    engine.calendar = TradingCalendar(datetime.fromisoformat(day).date() for day in packet.get("calendar", []))
    engine.effective_interval = packet.get("effective_interval")
    engine.result.error = packet.get("error")
    engine.result.storage_error = packet.get("storage_error")
    engine.result.duration = packet.get("duration", 0)
    engine.result.events = packet.get("events", [])
