# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Transactional local alert events and edge-triggered cooldown state."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from .labels import RULE_LABELS, display_name
from .market import fingerprint
from .models import number, shanghai_now


class AlertStore:
    """Persist events and rule state, partitioned by source, mode, and configuration.

    Parameters
    ----------
    path : str or Path
        Local SQLite file; no credentials or external notification service.
    clock : callable
        Wall clock used for retention independently of simulated quote time.
    """

    def __init__(self, path, clock=shanghai_now):
        self.path = str(Path(path).expanduser().resolve())
        self.clock = clock
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recorded_at REAL NOT NULL, observed_at TEXT NOT NULL,
                    source TEXT NOT NULL, mode TEXT NOT NULL, target TEXT NOT NULL,
                    rule TEXT NOT NULL, value REAL NOT NULL, threshold REAL NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rule_state (
                    key TEXT PRIMARY KEY, active INTEGER NOT NULL,
                    last_alert REAL, observation TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_mode_time ON events(mode, recorded_at);
            """)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=2)
        connection.row_factory = sqlite3.Row
        return connection

    def evaluate(self, observations, source, mode, scope, observed_at, cooldown):
        """Atomically process eligible observations and return only persisted events.

        Each observation contains target, rule, value, threshold, and identity.
        Ineligible/missing observations must be omitted, so they cannot rearm rules.
        An entry during cooldown is suppressed until another valid exit and entry.
        """
        recorded = self.clock().timestamp()
        now = observed_at.timestamp()
        events = []
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                for observation in observations:
                    target, rule, value, threshold, identity = observation
                    if number(value) is None or number(threshold) is None:
                        continue
                    key = fingerprint((source, mode, scope, target, rule, threshold))
                    state = connection.execute("SELECT * FROM rule_state WHERE key = ?", (key,)).fetchone()
                    if state is not None and state["observation"] == identity:
                        continue
                    active = value >= threshold
                    last_alert = state["last_alert"] if state else None
                    was_active = bool(state["active"]) if state else False
                    if active and not was_active and (last_alert is None or now - last_alert >= cooldown):
                        event = {
                            "recorded_at": recorded,
                            "observed_at": observed_at.isoformat(),
                            "source": source,
                            "mode": mode,
                            "target": target,
                            "rule": rule,
                            "value": value,
                            "threshold": threshold,
                            "message": (
                                f"{display_name(target)}：{RULE_LABELS.get(rule, rule)} "
                                f"{value:.6g} 达到阈值 {threshold:.6g}"
                            ),
                        }
                        connection.execute(
                            "INSERT INTO events (recorded_at, observed_at, source, mode, target, rule, value, threshold, message) "
                            "VALUES (:recorded_at, :observed_at, :source, :mode, :target, :rule, :value, :threshold, :message)",
                            event,
                        )
                        events.append(event)
                        last_alert = now
                    connection.execute(
                        "INSERT OR REPLACE INTO rule_state (key, active, last_alert, observation, updated_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (key, int(active), last_alert, identity, recorded),
                    )
                self._prune(connection, recorded)
        return events

    @staticmethod
    def _prune(connection, now):
        cutoff = now - 30 * 86400
        connection.execute("DELETE FROM events WHERE recorded_at < ?", (cutoff,))
        connection.execute("DELETE FROM rule_state WHERE updated_at < ?", (cutoff,))

    def recent(self, mode, search="", limit=200):
        """Return recent events for the selected mode; match search as literal text."""
        with closing(self._connect()) as connection:
            with connection:
                self._prune(connection, self.clock().timestamp())
                rows = connection.execute(
                    "SELECT * FROM events WHERE mode = ? AND instr(lower(message), lower(?)) > 0 "
                    "ORDER BY id DESC LIMIT ?",
                    (mode, search, min(max(int(limit), 1), 1000)),
                ).fetchall()
        return [dict(row) for row in rows]
