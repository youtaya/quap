"""Optional immutable Qlib export; native data never depends on this artifact."""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import numpy as np
from filelock import FileLock

from quant_platform.domain import now, symbol


def isolated_read(path, code=None):
    result = subprocess.run(
        [sys.executable, "-I", "-m", "quant_platform.adapters.qlib.reader"],
        input=json.dumps({"path": str(path), "symbol": code}),
        text=True,
        capture_output=True,
        timeout=120,
        cwd=path,
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("QUANT_RESULT=")]
    if result.returncode or not lines:
        raise RuntimeError("Optional Qlib reader failed; native research remains available.")
    return json.loads(lines[-1].split("=", 1)[1])


def external_features(path, code):
    """Read an operator-mounted read-only external dataset without publishing to it."""
    root = Path(path).expanduser().resolve(strict=True)
    if not (root / "calendars/day.txt").is_file() or not (root / "instruments/all.txt").is_file():
        raise ValueError("An explicit Qlib daily dataset is required.")
    return isolated_read(root, symbol(code))


def external_universe(path, name, day):
    """Require explicit dated membership; never infer constituents from a current directory."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ValueError("Invalid universe name.")
    root = Path(path).expanduser().resolve(strict=True)
    membership = root / "instruments" / f"{name}.txt"
    if not membership.is_file() or not membership.resolve().is_relative_to(root):
        raise ValueError("Verified dated universe membership is unavailable.")
    result = set()
    for line in membership.read_text().splitlines():
        fields = line.split()
        if len(fields) != 3:
            raise ValueError("Universe membership must provide symbol, start date and end date.")
        code, start, end = symbol(fields[0]), date.fromisoformat(fields[1]), date.fromisoformat(fields[2])
        if start > end:
            raise ValueError("Invalid membership interval.")
        if start <= day <= end:
            result.add(code)
    return sorted(result)


class GenerationStore:
    def __init__(self, root):
        self.root = Path(root).absolute() / "qlib"
        if self.root.is_symlink():
            raise ValueError("Qlib artifact root cannot be a symlink.")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.root.parent / "qlib.lock"), timeout=120)

    def prepare(self):
        marker = self.root / ".owned"
        if marker.exists():
            if marker.read_text() != "quant-platform-v1":
                raise ValueError("Unrecognized artifact owner.")
        elif any(self.root.iterdir()):
            raise ValueError("Refusing nonempty unowned artifact directory.")
        else:
            marker.write_text("quant-platform-v1")

    def current(self):
        pointer = self.root / "current.json"
        if not pointer.exists():
            return None
        name = json.loads(pointer.read_text())["generation"]
        if not re.fullmatch(r"generation-[a-f0-9]{32}", name):
            raise ValueError("Invalid generation pointer.")
        path = self.root / name
        if path.is_symlink() or not path.is_dir():
            raise ValueError("Generation missing or unsafe.")
        return path

    def features(self, code):
        with self.lock:
            path = self.current()
            if path is None:
                raise ValueError("No Qlib generation published.")
            return isolated_read(path, symbol(code))

    def build(self, histories, days, metadata=None):
        self.prepare()
        stage = self.root / ("staging-" + uuid.uuid4().hex)
        stage.mkdir()
        try:
            return self._build(histories, days, stage, metadata or {})
        except BaseException:
            shutil.rmtree(stage)
            raise

    def _build(self, histories, days, stage, metadata):
        name = "generation-" + uuid.uuid4().hex
        (stage / "calendars").mkdir()
        (stage / "instruments").mkdir()
        (stage / "calendars/day.txt").write_text("\n".join(map(str, days)) + "\n")
        positions = {str(day): i for i, day in enumerate(days)}
        ranges, coverage = [], {}
        for code, rows in histories.items() if hasattr(histories, "items") else histories:
            symbol(code)
            rows = [row for row in rows if str(row["day"]) in positions]
            if not rows or any(r.get("factor") is None for r in rows):
                coverage[code] = "missing_adjustment_or_bars"
                continue
            first, last = positions[str(rows[0]["day"])], positions[str(rows[-1]["day"])]
            folder = stage / "features" / code.lower()
            folder.mkdir(parents=True)
            for field in ("open", "high", "low", "close", "volume"):
                values = np.full(last - first + 2, np.nan, dtype="<f4")
                values[0] = first
                for row in rows:
                    value = row["data"].get(field)
                    if value is not None:
                        factor = 1 if field == "volume" else row["factor"] / rows[-1]["factor"]
                        values[positions[str(row["day"])] - first + 1] = value * factor
                values.tofile(folder / f"{field}.day.bin")
            ranges.append(f"{code}\t{rows[0]['day']}\t{rows[-1]['day']}")
            coverage[code] = str(rows[-1]["day"])
        if not ranges:
            raise ValueError("No exportable history; previous generation retained.")
        (stage / "instruments/all.txt").write_text("\n".join(ranges) + "\n")
        manifest = {
            **metadata,
            "generation": name,
            "coverage": coverage,
            "source": "tushare",
            "price_unit": "adjusted CNY",
            "volume_unit": "shares",
            "not_benchmark_normalized": True,
            "published_at": now().isoformat(),
        }
        (stage / "manifest.json").write_text(json.dumps(manifest))
        isolated_read(stage)
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        return stage, name, manifest

    def publish(self, stage, name):
        old = self.current()
        stage.rename(self.root / name)
        temporary = self.root / "current.pending"
        with temporary.open("w") as stream:
            json.dump({"generation": name}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.root / "current.json")
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        keep = {name, old.name if old else ""}
        for path in self.root.iterdir():
            if re.fullmatch(r"(?:generation|staging)-[a-f0-9]{32}", path.name) and path.name not in keep:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)


def export(db, settings, job):
    if not settings.qlib_enabled:
        raise ValueError("Optional Qlib component is disabled.")
    day = date.fromisoformat(job["payload"]["day"])
    with db.transaction() as conn:
        db.fence(conn, job)
        conn.execute("SELECT pg_advisory_xact_lock(77610234)")
        watermark = job["payload"].get(
            "watermark", conn.execute("SELECT coalesce(max(id),0) AS id FROM datasets").fetchone()["id"]
        )
    histories = (
        (row["symbol"], db.history(row["symbol"], day, settings.history_sessions, watermark))
        for row in db.rows("SELECT symbol FROM instruments WHERE status='L' ORDER BY symbol")
    )
    days = [
        r["day"]
        for r in db.rows(
            "SELECT day FROM calendars WHERE exchange='SSE' AND is_open AND day<=%s " "ORDER BY day DESC LIMIT %s",
            (day, settings.history_sessions),
        )
    ][::-1]
    store = GenerationStore(settings.artifact_root)
    with store.lock:
        current = store.current()
        if current:
            manifest = json.loads((current / "manifest.json").read_text())
            if manifest.get("job_id") == job["id"]:
                with db.publication(job) as conn:
                    db.set_setting(conn, "qlib", {"enabled": True, "status": "published", **manifest})
                return
        stage, name, manifest = store.build(histories, days, {"job_id": job["id"], "dataset_watermark": watermark})
        with db.publication(job) as conn:
            store.publish(stage, name)
            db.set_setting(conn, "qlib", {"enabled": True, "status": "published", **manifest})
