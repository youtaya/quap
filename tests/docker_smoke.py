"""Opt-in container smoke test. Uses only disposable databases and no external feed."""

import importlib.metadata
import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import psycopg
from psycopg import sql

from quant_platform.cli import migrate
from quant_platform.config import Settings
from quant_platform.operations import backup, restore_check
from quant_platform.storage import Database, LostLease, jsonb
from quant_platform.domain import CN, BasketInput, now


def fixture_worker(role):
    """Test-only injection stays in the mounted tests, never in the production package."""
    from quant_platform.jobs import analyze, collect, scheduler
    from quant_platform.jobs.worker import Worker
    from quant_platform.providers import PermissionDenied

    settings = Settings()
    if os.getenv("QUANT_DOCKER_SMOKE") != "1" or settings.environment != "test":
        raise RuntimeError("Fixture workers require explicit test configuration")
    db = Database(settings.dsn)
    assert db.rows("SELECT current_database() AS name")[0]["name"].startswith("quant_test_smoke_")
    at = datetime.fromisoformat(os.environ["QUANT_SMOKE_AT"])
    for module in (analyze, collect, scheduler):
        module.now = lambda: at

    class Feed:
        def securities(self):
            return {
                "SH600895": {
                    "name": "Fixture",
                    "board": "Main Board",
                    "exchange": "SSE",
                    "list_date": at.date() - timedelta(days=1000),
                    "delist_date": None,
                    "list_status": "L",
                }
            }

        def request(self, endpoint, params, **kwargs):
            if endpoint != "trade_cal":
                raise PermissionDenied("Optional fixture capability is not enabled")
            start, end = (datetime.strptime(params[key], "%Y%m%d").date() for key in ("start_date", "end_date"))
            return [
                {
                    "exchange": params["exchange"],
                    "cal_date": (start + timedelta(days=i)).strftime("%Y%m%d"),
                    "is_open": 1,
                }
                for i in range((end - start).days + 1)
            ]

        def daily_partition(self, endpoint, day, codes, job=None):
            return [
                {
                    "symbol": code,
                    "day": day,
                    **(
                        {"factor": 1}
                        if endpoint == "adj_factor"
                        else {"open": 10, "close": 10, "high": 11, "low": 9, "volume": 1000000, "turnover": 30000000}
                    ),
                }
                for code in sorted(codes)
            ]

        def quotes(self, codes):
            return [
                {
                    "symbol": code,
                    "close": 10.5,
                    "pre_close": 10,
                    "source_time": at.isoformat(),
                    "received_at": at.isoformat(),
                    "reference_verified": False,
                }
                for code in codes
            ]

        def close(self):
            pass

    worker = Worker(db, settings, role)
    worker.feed = Feed()
    try:
        worker.run()
    finally:
        db.close()


def wait_for(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise AssertionError("Container smoke condition timed out")


def main():
    if os.getenv("QUANT_DOCKER_SMOKE") != "1":
        raise RuntimeError("Explicit test-only opt-in is required")
    admin_dsn = os.environ["QUANT_SMOKE_ADMIN_DSN"]
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        if not admin.info.dbname.startswith("quant_test"):
            raise RuntimeError("Refusing non-test database server configuration")
        suffix = uuid4().hex[:12]
        source_name, restore_name = "quant_test_smoke_" + suffix, "quant_restore_smoke_" + suffix
        for name in (source_name, restore_name):
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    source_dsn = admin_dsn.rsplit("/", 1)[0] + "/" + source_name
    restore_dsn = admin_dsn.rsplit("/", 1)[0] + "/" + restore_name
    root = Path(tempfile.mkdtemp(prefix="quant-smoke-"))
    settings = Settings(
        database_url=source_dsn,
        api_token="smoke-fixture-" * 4,
        environment="test",
        artifact_root=root / "artifacts",
        backup_root=root / "backups",
    )
    migrate(settings)
    migrate(settings)
    db = Database(source_dsn)
    fixtures = os.getenv("QUANT_SMOKE_FIXTURES") == "1"
    fixture_at = now().astimezone(CN).replace(hour=10, minute=0, second=0, microsecond=0)
    env = {
        **os.environ,
        "QUANT_DATABASE_URL": source_dsn,
        "QUANT_API_TOKEN": settings.api_token.get_secret_value(),
        "QUANT_TUSHARE_TOKEN": "",
        "QUANT_ENVIRONMENT": "test",
        "QUANT_HISTORY_SESSIONS": "120",
        "QUANT_SMOKE_AT": fixture_at.isoformat(),
        "QUANT_ARTIFACT_ROOT": str(settings.artifact_root),
        "QUANT_BACKUP_ROOT": str(settings.backup_root),
    }
    processes = []

    def launch(*args):
        command = [sys.executable, "-m", "quant_platform.cli", *args]
        if fixtures and args[0] == "worker":
            command = [sys.executable, str(Path(__file__).resolve()), "--fixture-worker", args[-1]]
        process = subprocess.Popen(
            command,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        processes.append(process)
        return process

    try:
        for name in ("pyqlib", "mlflow", "lightgbm"):
            try:
                importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                continue
            raise AssertionError("Optional package installed in native image: " + name)
        for role in ("scheduler", "quotes", "history", "analysis"):
            launch("worker", "--role", role)
        launch("api", "--host", "127.0.0.1", "--port", "18081")
        wait_for(lambda: len(db.rows("SELECT DISTINCT role FROM heartbeats")) == 4)
        with httpx.Client(base_url="http://127.0.0.1:18081", trust_env=False) as client:

            def live():
                try:
                    return client.get("/health/live").status_code == 200
                except httpx.HTTPError:
                    return False

            wait_for(live)
            assert client.get("/api/v1/status").status_code == 401
            client.headers["Authorization"] = "Bearer " + settings.api_token.get_secret_value()
            assert client.get("/api/v1/status").status_code == 200
            if not fixtures:
                assert client.get("/api/v1/history/SH600895").json() == []
        if fixtures:
            wait_for(lambda: db.rows("SELECT 1 FROM reports WHERE kind='screen' AND data->>'status'='complete'"), 120)
            assert len(db.history("SH600895", fixture_at.date())) == 120
            with db.transaction() as conn:
                basket_id = uuid4()
                value = BasketInput(name="Fixture Basket", members={"SH600895": 1})
                conn.execute("INSERT INTO baskets(id,name,revision) VALUES(%s,%s,1)", (basket_id, value.name))
                conn.execute(
                    "INSERT INTO basket_revisions(basket_id,revision,effective_day,data) VALUES(%s,1,%s,%s)",
                    (basket_id, fixture_at.date(), jsonb(value.model_dump(exclude={"expected_revision"}))),
                )
                db.enqueue(conn, "quotes", "quotes", "fixture-tracked-quotes", {"symbols": ["SH600895"]}, 1000)
            wait_for(lambda: len(db.rows("SELECT * FROM alerts")) == 2)
            assert len(db.rows("SELECT * FROM basket_observations")) == 1
        old = processes[1]
        old.terminate()
        old.wait(timeout=15)
        launch("worker", "--role", "quotes")
        wait_for(lambda: db.rows("SELECT count(*) AS n FROM heartbeats WHERE role='quotes'")[0]["n"] == 2)
        with db.transaction() as conn:
            db.set_setting(conn, "smoke-sentinel", {"persisted": True})
        restart = os.getenv("QUANT_SMOKE_EXPECT_DB_RESTART") == "1"
        if restart:
            with db.transaction() as conn:
                db.enqueue(conn, "test", "smoke-recovery", "restart-checkpoint")
            abandoned = db.claim("smoke-recovery", "abandoned-owner")
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE jobs SET progress=%s,lease_until=now()-interval '1 second' WHERE id=%s",
                    (jsonb({"checkpoint": "persisted"}), abandoned["id"]),
                )
            started = db.rows("SELECT pg_postmaster_start_time() AS at")[0]["at"]
            print(json.dumps({"stage": "database_restart_requested", "test_database": source_name}), flush=True)

            def restarted():
                try:
                    with psycopg.connect(source_dsn, connect_timeout=2) as conn:
                        return conn.execute("SELECT pg_postmaster_start_time()").fetchone()[0] != started
                except psycopg.OperationalError:
                    return False

            wait_for(restarted, 180)
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                process.wait(timeout=20)
            db.close()
            db = Database(source_dsn)
            assert db.setting("smoke-sentinel") == {"persisted": True}
            recovered = db.claim("smoke-recovery", "replacement-owner")
            assert recovered["fence"] > abandoned["fence"] and recovered["progress"] == {"checkpoint": "persisted"}
            try:
                with db.publication(abandoned):
                    raise AssertionError("Abandoned owner must be fenced out after database restart")
            except LostLease:
                pass
            with db.publication(recovered) as conn:
                db.enqueue(conn, "test", "smoke-child", "restart-child")
            with db.transaction() as conn:
                db.enqueue(conn, "test", "smoke-child", "restart-child")
            assert len(db.rows("SELECT * FROM jobs WHERE dedupe='restart-child'")) == 1
            for role in ("scheduler", "quotes", "history", "analysis"):
                launch("worker", "--role", role)
            launch("api", "--host", "127.0.0.1", "--port", "18081")
            if fixtures:
                with db.transaction() as conn:
                    jid = db.enqueue(conn, "quotes", "quotes", "post-restart-quotes", {"symbols": ["SH600895"]}, 1000)
                wait_for(lambda: db.rows("SELECT status FROM jobs WHERE id=%s", (jid,))[0]["status"] == "complete")
                wait_for(
                    lambda: db.rows("SELECT count(*) AS n FROM jobs WHERE kind='intraday' AND status='complete'")[0][
                        "n"
                    ]
                    >= 3
                )
                assert len(db.rows("SELECT * FROM alerts")) == 2
                assert len(db.rows("SELECT * FROM basket_observations")) == 1
        with db.transaction() as conn:
            db.enqueue(conn, "backup", "smoke-backup", "smoke-backup")
        item = db.claim("smoke-backup", "smoke-owner")
        backup(db, settings, item)
        state = db.setting("backup")
        restored = restore_check(settings.backup_root / state["file"], restore_dsn, db)
        assert restored["schema"] == "0003" and db.setting("backup")["restore_verified"]
        with psycopg.connect(restore_dsn) as restored_db:
            assert restored_db.execute("SELECT value FROM settings WHERE key='smoke-sentinel'").fetchone()[0] == {
                "persisted": True
            }
        print(
            json.dumps(
                {
                    "migrations": "passed",
                    "native_boundary": "passed",
                    "authenticated_api": "passed",
                    "browser_independent_workers": "passed",
                    "collector_restart": "passed",
                    "backup_restore": "passed",
                    "fixture_collection_analysis": "passed" if fixtures else "not_requested",
                    "persistent_database_restart": "passed" if restart else "not_requested",
                }
            )
        )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            process.wait(timeout=20)
        db.close()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--fixture-worker":
        fixture_worker(sys.argv[2])
    else:
        main()
