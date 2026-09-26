"""Independent heartbeats, renewable leases and bounded worker lifecycles."""

import json
import logging
import os
import subprocess
import sys
import tempfile
import signal
import threading
import time
import uuid

from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.providers import Deferred, PermissionDenied, ProviderError
from quant_platform.providers.tushare import Tushare
from quant_platform.storage import LostLease
from . import analyze, collect
from .scheduler import tick

LOG = logging.getLogger(__name__)
MAX_QLIB_LOG_BYTES = 32 * 1024 * 1024


class Worker:
    def __init__(self, db, settings, role):
        self.db, self.settings, self.role = db, settings, role
        self.owner = f"{role}-{uuid.uuid4().hex}"
        self.stop = threading.Event()
        self.drained = threading.Event()
        self.state_lock = threading.Lock()
        self.job = None
        self.started = time.monotonic()
        self.feed = None

    def beat_once(self):
        with self.state_lock:
            job, started = self.job, self.started
        # Kill only this worker process; expired lease recovery is handled by PostgreSQL.
        if job and time.monotonic() - started > self.deadline(job) + 15:
            LOG.error("Worker deadline exceeded; exiting for supervised restart")
            os._exit(1)
        try:
            self.db.heartbeat(self.owner, self.role, {"job": job["id"] if job else None})
            if job and not self.db.renew(job) and not self.db.finished(job):
                self.stop.set()
        except Exception:
            LOG.error("Worker heartbeat unavailable")
            self.stop.set()

    def beat(self):
        # Continue renewing while an in-flight request drains after SIGTERM.
        while not self.drained.wait(5):
            self.beat_once()

    def run(self):
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: self.stop.set())
        self.db.heartbeat(self.owner, self.role, {"status": "starting"})
        thread = threading.Thread(target=self.beat, daemon=True)
        thread.start()
        try:
            while not self.stop.is_set():
                if self.role == "scheduler":
                    tick(self.db, self.settings)
                    self.stop.wait(5)
                    continue
                job = self.db.claim(self.role, self.owner)
                if job is None:
                    self.stop.wait(1)
                    continue
                with self.state_lock:
                    self.job, self.started = job, time.monotonic()
                try:
                    self.execute(job)
                except LostLease:
                    LOG.warning("Lease lost; discarded publication")
                except Exception as exc:
                    # Provider errors have sanitized messages. Never log unknown exception bodies/DSNs.
                    safe = (
                        str(exc) if isinstance(exc, (ProviderError, Deferred, PipelineBlocked)) else type(exc).__name__
                    )
                    try:
                        self.db.defer(
                            job,
                            safe,
                            getattr(exc, "seconds", 60 if job["kind"] == "quotes" else 1800),
                            blocked=isinstance(exc, (PermissionDenied, PipelineBlocked)),
                            transient=isinstance(exc, Deferred),
                        )
                    except LostLease:
                        pass
                    LOG.warning("Job %s failed: %s", job["id"], safe)
                finally:
                    with self.state_lock:
                        self.job = None
        finally:
            self.stop.set()
            self.drained.set()
            thread.join(timeout=6)
            if self.feed:
                self.feed.close()
            self.db.heartbeat(self.owner, self.role, {"status": "stopped"})

    def deadline(self, job):
        if job["kind"] in {"qlib_train", "qlib_experiment"}:
            return self.settings.training_deadline_seconds
        if job["kind"] in {"qlib_prepare", "backup", "qlib_diagnostics", "research_collect"}:
            return self.settings.data_deadline_seconds
        if job["kind"] in {"qlib_infer", "qlib_shadow"}:
            return self.settings.inference_deadline_seconds
        return 1800

    def qlib_step(self, job):
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "quant_platform.adapters.qlib.runtime"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=output,
                start_new_session=True,
            )
            try:
                process.stdin.write(json.dumps(job, default=str).encode())
                process.stdin.close()
                started = time.monotonic()
                while process.poll() is None:
                    if self.stop.wait(0.2):
                        raise LostLease("Worker stopped before Qlib publication.")
                    if time.monotonic() - started > self.deadline(job):
                        raise PipelineBlocked("Qlib task exceeded its configured deadline.")
                    if os.fstat(output.fileno()).st_size > MAX_QLIB_LOG_BYTES:
                        raise PipelineBlocked("Qlib task exceeded its diagnostic output limit.")
                if process.returncode:
                    output.seek(max(0, output.tell() - 8192))
                    tail = output.read().decode(errors="replace")
                    if process.returncode == 3:
                        reason = next(
                            (
                                line.split("QUAP_BLOCKED=", 1)[1]
                                for line in tail.splitlines()
                                if line.startswith("QUAP_BLOCKED=")
                            ),
                            "Qlib prerequisites are unavailable.",
                        )
                        raise PipelineBlocked(reason[:2000])
                    raise PipelineBlocked(
                        "Qlib engine step failed; inspect the generation and runtime qualification tests."
                    )
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()

    def execute(self, job):
        kind = job["kind"]
        if kind in {
            "directory",
            "calendar",
            "daily",
            "factors",
            "quotes",
            "doctor",
            "benchmark",
            "minute_history",
            "minute_live",
            "minute_repair",
            "security_history",
            "constraints",
        }:
            self.feed = self.feed or Tushare(self.settings, self.db)
        if kind in {"directory", "calendar"}:
            collect.reference(self.db, self.feed, job)
        elif kind in {"daily", "factors"}:
            collect.history(self.db, self.feed, job)
        elif kind == "quotes":
            collect.quotes(self.db, self.feed, job)
        elif kind == "benchmark":
            collect.benchmark(self.db, self.feed, job)
        elif kind == "intraday":
            analyze.intraday(self.db, job)
        elif kind in {"analyze", "research", "qlib_export"}:
            raise PipelineBlocked("Legacy native analysis/export is retired. Create an explicit Qlib pipeline run.")
        elif kind in {"qlib_prepare", "qlib_train", "qlib_infer", "qlib_shadow"}:
            self.qlib_step(job)
        elif kind == "research_collect":
            from .supplemental import collect as collect_supplemental

            collect_supplemental(self.db, self.settings, job, self.stop)
        elif kind in {"qlib_experiment", "qlib_diagnostics"}:
            from . import experiments, diagnostics

            handler = experiments.execute if kind == "qlib_experiment" else diagnostics.execute
            handler(self.db, self.settings, job, self.stop)
        elif kind in {"minute_history", "minute_live", "minute_repair", "security_history", "constraints"}:
            from . import market

            handler = market.minutes if kind.startswith("minute_") else getattr(market, kind)
            handler(self.db, self.feed, job)
        elif kind == "refresh":
            tick(self.db, self.settings)
            from quant_platform.storage import jsonb

            with self.db.publication(job) as conn:
                children = [
                    self.db.enqueue(conn, kind, "history", f"refresh:{job['id']}:{kind}", priority=200)
                    for kind in ("calendar", "directory")
                ]
                conn.execute(
                    "UPDATE jobs SET progress=%s WHERE id=%s",
                    (
                        jsonb(
                            {"children": children, "quotes": "subject to session, pause, interval and capability gates"}
                        ),
                        job["id"],
                    ),
                )
        elif kind == "doctor":
            from quant_platform.operations import doctor

            doctor(self.db, self.settings, self.feed)
            with self.db.publication(job):
                pass
        elif kind == "qualification":
            from quant_platform.operations import qualification

            qualification(self.db, self.settings, job)
        elif kind in {"maintenance", "backup"}:
            from quant_platform.operations import maintenance, backup

            (maintenance if kind == "maintenance" else backup)(self.db, self.settings, job)
        elif kind == "notify":
            from quant_platform.jobs.notify import deliver

            deliver(self.db, self.settings, job)
        else:
            raise ValueError("Unknown job kind.")
