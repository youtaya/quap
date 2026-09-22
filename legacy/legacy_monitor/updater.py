# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Resumable daily windows and immutable, atomically published Qlib datasets."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from filelock import FileLock

from .feeds import TencentDaily
from .market import TradingCalendar, fingerprint
from .models import SHANGHAI, Basket, normalize_symbol, shanghai_now
from .reader import read_qlib
from .state import json_text


def completed_cutoff(now, update_at="18:30"):
    local = now.astimezone(SHANGHAI)
    at = datetime.strptime(update_at, "%H:%M").time()
    return local.date() if local.time() >= at else local.date() - timedelta(days=1)


def next_update(now, update_at="18:30"):
    local = now.astimezone(SHANGHAI)
    at = datetime.combine(local.date(), datetime.strptime(update_at, "%H:%M").time(), SHANGHAI)
    return at if at > local else at + timedelta(days=1)


class ManagedDataset:
    """Restrict writes/cleanup to an owned managed root; lock reads and publication."""

    marker = "qlib-market-monitor-v1"

    def __init__(self, runtime_root):
        parent = Path(runtime_root).resolve()
        self.root = parent / "qlib"
        if self.root.is_symlink():
            raise ValueError("Managed Qlib root must not be a symlink.")
        self.lock = FileLock(str(parent / "qlib-publication.lock"), timeout=120)

    def prepare(self):
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / ".monitor-owned"
        if marker.exists():
            if marker.read_text(encoding="utf-8") != self.marker:
                raise ValueError("Unrecognized managed-dataset ownership marker.")
        elif any(self.root.iterdir()):
            raise ValueError("Refusing to write to a nonempty, unmanaged Qlib directory.")
        else:
            marker.write_text(self.marker, encoding="utf-8")

    def current(self):
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        data = json.loads(pointer.read_text(encoding="utf-8"))
        name = data.get("generation", "")
        if not re.fullmatch(r"generation-[a-f0-9]{32}", name):
            raise ValueError("Invalid managed generation pointer.")
        path = self.root / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Managed generation is unavailable.")
        return path

    def manifest(self):
        with self.lock:
            path = self.current()
            return json.loads((path / "manifest.json").read_text(encoding="utf-8")) if path else None

    def features(self, symbol, lookback=1000):
        with self.lock:
            path = self.current()
            if path is None:
                raise ValueError("Initial Qlib history download has not been published yet.")
            response = read_qlib(path, "features", symbol=normalize_symbol(symbol), lookback=lookback)
        frame = pd.DataFrame(response["rows"])
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        return frame.set_index(["instrument", "datetime"])

    def publish(self, records, symbols, days, status, validator=read_qlib):
        with self.lock:
            self.prepare()
            old = self.current()
            for abandoned in self.root.iterdir():
                if re.fullmatch(r"staging-[a-f0-9]{32}", abandoned.name):
                    if abandoned.is_dir() and not abandoned.is_symlink():
                        shutil.rmtree(abandoned)
            generation = "generation-" + uuid.uuid4().hex
            stage = self.root / ("staging-" + uuid.uuid4().hex)
            stage.mkdir()
            (stage / "calendars").mkdir()
            (stage / "instruments").mkdir()
            calendars = sorted(days)
            if not calendars:
                raise ValueError("Cannot publish an empty trading calendar.")
            positions = {day: index for index, day in enumerate(calendars)}
            (stage / "calendars" / "day.txt").write_text("\n".join(calendars) + "\n", encoding="utf-8")
            instruments, coverage = [], {}
            for symbol in sorted(symbols):
                normalize_symbol(symbol)
                record = records.get(symbol, {})
                coverage[symbol] = {key: record.get(key) for key in ("end_date", "target", "fetched_at", "error")}
                raw = record.get("rows_json")
                coverage[symbol]["stored_bars"] = 0
                if not raw:
                    continue
                rows = [row for row in json.loads(raw) if row[0] in positions]
                if not rows:
                    continue
                coverage[symbol]["stored_bars"] = len(rows)
                first, last = positions[rows[0][0]], positions[rows[-1][0]]
                folder = stage / "features" / symbol.lower()
                folder.mkdir(parents=True)
                for field, column in (("open", 1), ("close", 2), ("high", 3), ("low", 4), ("volume", 5)):
                    values = np.full(last - first + 2, np.nan, dtype="<f4")
                    values[0] = first
                    for row in rows:
                        values[positions[row[0]] - first + 1] = np.nan if row[column] is None else row[column]
                    if field != "volume" and not np.isfinite(values[1:]).any():
                        raise ValueError(f"No valid {field} values for {symbol}.")
                    values.tofile(folder / f"{field}.day.bin")
                instruments.append(f"{symbol}\t{rows[0][0]}\t{rows[-1][0]}")
            if not instruments:
                raise ValueError("No usable symbol histories; retaining previous Qlib generation.")
            (stage / "instruments" / "all.txt").write_text("\n".join(instruments) + "\n", encoding="utf-8")
            manifest = {
                **status,
                "generation": generation,
                "published_at": shanghai_now().isoformat(),
                "source": "Tencent forward-adjusted daily",
                "price_unit": "adjusted CNY",
                "volume_unit": "shares",
                "calendar_end": calendars[-1],
                "coverage": coverage,
                "available": len(instruments),
            }
            (stage / "manifest.json").write_text(json_text(manifest), encoding="utf-8")
            validator(stage, "validate")
            # Make file contents durable before exposing the immutable generation.
            for file in stage.rglob("*"):
                if file.is_file():
                    with file.open("rb") as stream:
                        os.fsync(stream.fileno())
            destination = self.root / generation
            stage.rename(destination)
            temporary = self.root / "current.tmp"
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(json_text({"generation": generation}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / "current.json")
            self._cleanup({generation, old.name if old else ""})
            return manifest

    def _cleanup(self, keep):
        # Only monitor-generated, nonsymlink directories inside the ownership marker.
        for path in self.root.iterdir():
            if re.fullmatch(r"(?:generation|staging)-[a-f0-9]{32}", path.name) and path.name not in keep:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)


class ExternalHistory:
    """Read-only external datasets, with each Qlib registration isolated."""

    def __init__(self, path):
        self.path = str(Path(path).expanduser().resolve())
        info = read_qlib(self.path)
        self.calendar = TradingCalendar(date.fromisoformat(day) for day in info["days"])
        self.end_date = date.fromisoformat(info["end_date"])
        self._resolved = {}

    def resolve(self, baskets, day):
        if not any(basket.universe for basket in baskets):
            return baskets
        key = fingerprint((day, [asdict(basket) for basket in baskets]))
        if key not in self._resolved:
            result = read_qlib(self.path, "resolve", baskets=[asdict(basket) for basket in baskets], day=str(day))
            self._resolved = {
                key: tuple(
                    Basket(item["name"], tuple(tuple(pair) for pair in item["members"])) for item in result["baskets"]
                )
            }
        return self._resolved[key]

    def features(self, symbol, lookback=1000):
        result = read_qlib(self.path, "features", symbol=normalize_symbol(symbol), lookback=lookback)
        frame = pd.DataFrame(result["rows"])
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        return frame.set_index(["instrument", "datetime"])


def progress(records, symbols, target):
    result = {"total": len(symbols), "attempted": 0, "successful": 0, "failed": 0, "unavailable": 0, "lagging": 0}
    for symbol in symbols:
        row = records.get(symbol, {})
        attempted = row.get("target") == target
        result["attempted"] += attempted
        result["failed"] += bool(attempted and row.get("error"))
        result["unavailable"] += not bool(row.get("end_date"))
        result["lagging"] += bool(row.get("end_date") and row["end_date"] < target)
        result["successful"] += bool(attempted and not row.get("error") and row.get("end_date") == target)
    return result


class HistoryUpdater:
    """One resumable, throttled full-market update. The supervisor schedules runs."""

    def __init__(self, store, settings, feed=None, clock=shanghai_now, stopped=lambda: False):
        self.store, self.settings = store, settings
        self.feed, self.clock, self.stopped = feed or TencentDaily(), clock, stopped
        self.dataset = ManagedDataset(store.root)

    def run(self):
        with FileLock(str(self.store.root / "history-worker.lock"), timeout=0):
            try:
                return self._run()
            except Exception as exc:
                self.store.set(
                    "history",
                    {
                        **self.store.get("history", {}),
                        "status": "failed",
                        "error": str(exc),
                        "next_update": (self.clock() + timedelta(minutes=30)).isoformat(),
                    },
                )
                raise
            finally:
                self.feed.http.close()

    def _run(self):
        directory = self.store.get("directory", {})
        symbols = directory.get("symbols", {})
        if not symbols:
            raise ValueError("Historical update is waiting for a complete market directory.")
        age = self.clock() - datetime.fromisoformat(directory["fetched_at"])
        if not timedelta(0) <= age <= timedelta(hours=48):
            raise ValueError("Historical update requires a directory no older than 48 hours.")
        cutoff = completed_cutoff(self.clock(), self.settings["update_at"])
        bars = self.settings["bars"]
        self.store.set("history", {**self.store.get("history", {}), "status": "calendar", "error": None})
        calendars = [self.feed.fetch(code, cutoff, 630, benchmark=True) for code in ("SH000001", "SZ399001")]
        if not all(calendars):
            raise ValueError("Completed benchmark trading calendars are unavailable.")
        if calendars[0][-1][0] != calendars[1][-1][0]:
            raise ValueError("Benchmark calendar end dates disagree; retry after publication settles.")
        days = sorted(set(row[0] for rows in calendars for row in rows))
        target = days[-1]
        published_generation = (self.dataset.manifest() or {}).get("generation")
        records = self.store.windows()
        metadata = {
            symbol: {key: value for key, value in row.items() if key != "rows_json"} for symbol, row in records.items()
        }
        del records
        changed = False
        for index, symbol in enumerate(sorted(symbols)):
            if self.stopped():
                self.store.set(
                    "history",
                    {**self.store.get("history", {}), "status": "interrupted", "next_update": self.clock().isoformat()},
                )
                return None
            old = metadata.get(symbol, {})
            if (
                old.get("target") == target
                and old.get("end_date") == target
                and not old.get("error")
                and old.get("bars") == bars
            ):
                continue
            if old.get("target") == target and old.get("bars") == bars:
                if self.clock() - datetime.fromisoformat(old["fetched_at"]) < timedelta(minutes=30):
                    continue
            try:
                rows = self.feed.fetch(symbol, date.fromisoformat(target), bars)
                if not rows:
                    raise ValueError("No completed daily bars returned.")
                if any(row[0] >= days[0] and row[0] not in days for row in rows):
                    raise ValueError("Symbol bars disagree with the completed benchmark calendar.")
                rows = [row for row in rows if row[0] in days]
                if not rows:
                    raise ValueError("No stock history inside the available benchmark calendar window.")
                self.store.save_window(symbol, rows, target, bars, now=self.clock())
            except Exception as exc:
                self.store.save_window(symbol, None, target, bars, str(exc), self.clock())
            updated = self.store.window(symbol)
            metadata[symbol] = {key: value for key, value in updated.items() if key != "rows_json"}
            changed = True
            if index % 5 == 0 or index == len(symbols) - 1:
                self.store.set(
                    "history",
                    {
                        **progress(metadata, symbols, target),
                        "status": "downloading",
                        "target": target,
                        "current_symbol": symbol,
                        "generation": published_generation,
                        "updated_at": self.clock().isoformat(),
                        "error": None,
                    },
                )
        counts = progress(metadata, symbols, target)
        full = counts["successful"] == counts["total"]
        status = {
            **counts,
            "status": "complete" if full else "degraded",
            "target": target,
            "generation": published_generation,
            "updated_at": self.clock().isoformat(),
            "error": None,
        }
        status["next_update"] = (
            next_update(self.clock(), self.settings["update_at"]) if full else self.clock() + timedelta(minutes=30)
        ).isoformat()
        previous = self.dataset.manifest()
        if changed or previous is None or previous.get("total") != len(symbols) or previous.get("target") != target:
            self.store.set("history", {**status, "status": "publishing"})
            published = self.dataset.publish(self.store.windows(), symbols, days, status)
            status["generation"] = published["generation"]
        elif previous:
            status["generation"] = previous["generation"]
        self.store.set("history", status)
        self.store.set("calendar", {"days": days, "source": "Completed Tencent benchmark bars"})
        return status
