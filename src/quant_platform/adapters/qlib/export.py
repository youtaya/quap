"""Optional immutable Qlib export; native data never depends on this artifact."""

import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import date, datetime, time
from pathlib import Path

import numpy as np
from filelock import FileLock

from quant_platform.domain import CN, now, symbol
from quant_platform.domain.workflow import DATA_CONTRACT
from quant_platform.storage import jsonb


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
        # The native research path is retired, so a failed isolated read is a hard failure of this
        # job. Saying a native fallback "remains available" would send an operator looking for one.
        raise RuntimeError("Isolated Qlib reader failed; the request is blocked, not degraded.")
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
        ranges, coverage, trimmed = [], {}, {}
        for code, rows in histories.items() if hasattr(histories, "items") else histories:
            symbol(code)
            rows = [row for row in rows if str(row["day"]) in positions]
            # 复权因子只覆盖源发布的最深窗口，更早的那一段没有因子。整体丢弃一只证券会把「因子
            # 从某天起才可用」误判成「这只证券不可用」，所以按从新到旧的最长连续已复权区间截取。
            # 截掉的会话数写进 manifest，未复权价绝不混进训练集。
            adjusted = len(rows)
            while adjusted and rows[adjusted - 1].get("factor") is not None:
                adjusted -= 1
            if adjusted == len(rows):
                coverage[code] = "missing_adjustment_or_bars"
                continue
            if adjusted:
                trimmed[code] = adjusted
            rows = rows[adjusted:]
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
        from quant_platform.adapters.qlib.data import seal

        manifest = seal(
            stage,
            {
                **metadata,
                "generation": name,
                "contract": DATA_CONTRACT,
                "frequency": "day",
                # 调度器发布的日频数据集不是 pipeline 预备代次：实验比较需要冻结池与策略快照，
                # 因此用 origin 把两类代次区分开，读端据此过滤。
                "origin": "scheduled",
                "days": [str(day) for day in days],
                "coverage": coverage,
                # 因缺少复权因子而被截掉的更早会话数。为 0 的证券不出现在这里。
                "trimmed_unadjusted": trimmed,
                "source": "market",
                "price_unit": "adjusted CNY",
                "volume_unit": "shares",
                "not_benchmark_normalized": True,
                "published_at": now().isoformat(),
            },
        )
        isolated_read(stage)
        for path in stage.rglob("*"):
            if path.is_file():
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        return stage, name, manifest

    def publish(self, stage, name, retain=()):
        """Publish ``name`` and prune, but never prune a generation a live run still pins.

        Retention is bounded on purpose — the current generation and the previous one — but the
        pipeline pins a generation by id in ``pipeline_runs.snapshot`` and replays it later. Pruning a
        pinned generation leaves a run that can never run again: its retry fails on a directory the
        platform itself deleted. The caller passes the set of still-referenced generations and those
        are exempt.
        """
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
        keep = {name, old.name if old else "", *retain}
        for path in self.root.iterdir():
            if re.fullmatch(r"(?:generation|staging)-[a-f0-9]{32}", path.name) and path.name not in keep:
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)


def _summary(manifest):
    """Operator-facing status: the provenance record without its per-file checksum index."""
    return {key: value for key, value in manifest.items() if key != "files"}


def register_generation(conn, root, folder, manifest, watermark, as_of):
    """Record a published generation in the provenance registry exactly once.

    ``/qlib-generations`` and ``data-readiness`` read ``qlib_generations``, so a generation that is
    published to disk but never registered looks like a platform holding no data at all.
    """
    relative = folder.resolve().relative_to(Path(root).resolve()).as_posix()
    conn.execute(
        "INSERT INTO qlib_generations(id,frequency,watermark,as_of,contract,path,manifest) "
        "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(path) DO NOTHING",
        (uuid.uuid4(), "day", watermark, as_of, DATA_CONTRACT, relative, jsonb(manifest)),
    )


def export(db, settings, job):
    if not settings.qlib_enabled:
        raise ValueError("Optional Qlib component is disabled.")
    day = date.fromisoformat(job["payload"]["day"])
    as_of = datetime.combine(day, time(15), CN)
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
    # 磁盘上只留两代是刻意的，但流水线会**按 id 钉住**一代并在之后用它：`qlib_prepare` 把 generation
    # 写进 `pipeline_steps.result`，`qlib_train` / `qlib_infer` 随后按这个 id 取回同一代；冻结实验还会
    # 把它钉进 `pipeline_runs.snapshot.research_generation_id`。两条路径的产物都落在同一个 `qlib/`
    # 目录下，所以导出端的保留策略**会删掉流水线自己的输入**。把被钉住的那一代剪掉，就等于亲手造出
    # 一个永远跑不起来的运行：重试会在平台自己删掉的目录上报 `FileNotFoundError`。未结束的运行所引用
    # 的代次必须豁免。
    pinned = {
        row["path"].rsplit("/", 1)[-1]
        for row in db.rows(
            "SELECT g.path FROM pipeline_steps s "
            "JOIN pipeline_runs r ON r.id=s.run_id "
            "JOIN qlib_generations g ON g.id::text=s.result->>'generation_id' "
            "WHERE s.name='prepare' AND r.state IN ('waiting','running','blocked') "
            "UNION "
            "SELECT g.path FROM pipeline_runs r "
            "JOIN qlib_generations g ON g.id::text=r.snapshot->>'research_generation_id' "
            "WHERE r.state IN ('waiting','running','blocked')"
        )
    }
    with store.lock:
        current = store.current()
        if current:
            manifest = json.loads((current / "manifest.json").read_text())
            if manifest.get("job_id") == job["id"]:
                with db.publication(job) as conn:
                    register_generation(conn, settings.artifact_root, current, manifest, watermark, as_of)
                    db.set_setting(conn, "qlib", {"enabled": True, "status": "published", **_summary(manifest)})
                return
        stage, name, manifest = store.build(histories, days, {"job_id": job["id"], "dataset_watermark": watermark})
        with db.publication(job) as conn:
            store.publish(stage, name, pinned)
            register_generation(conn, settings.artifact_root, store.root / name, manifest, watermark, as_of)
            db.set_setting(conn, "qlib", {"enabled": True, "status": "published", **_summary(manifest)})
            # A published generation is the input for one Qlib research report. Reporting runs in its
            # own isolated process, so a missing/unusable Qlib install fails that job alone.
            db.enqueue(
                conn,
                "qlib_report",
                "qlib-data",
                f"qlib-report:{name}",
                {"generation": name, "day": str(day), "dataset_watermark": watermark, "as_of": str(day)},
                60,
            )
