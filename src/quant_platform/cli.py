"""Independent lifecycle commands; no Qlib import at application startup."""

import argparse
import json
import logging
import os
from pathlib import Path

from quant_platform.config import Settings
from quant_platform.storage import Database


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": self.formatTime(record),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


def migrate(settings):
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "storage/migrations"))
    config.attributes["url"] = settings.dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    command.upgrade(config, "head")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "migrate",
            "api",
            "dashboard",
            "worker",
            "status",
            "doctor",
            "health",
            "import-legacy",
            "restore-check",
            "brief",
        ],
    )
    parser.add_argument(
        "--role", choices=["scheduler", "quotes", "history", "analysis", "operations", "research", "notify", "qlib"]
    )
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--apply", action="store_true", help="Apply legacy import; otherwise dry-run")
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--target-dsn-file", type=Path)
    parser.add_argument("--fault-at", help="ISO timestamp used to record restore RPO/RTO")
    parser.add_argument("--email", help="Operator address for an immediate research brief")
    parser.add_argument("--symbol", default="SH600895")
    parser.add_argument("--cost", type=float, default=28)
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings()
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    if args.action == "brief":
        if not args.email:
            parser.error("brief requires --email")
        from quant_platform.operator_brief import run_brief

        print(json.dumps(run_brief(settings, args.email, args.symbol, args.cost, args.top), ensure_ascii=False, indent=2))
        return
    if args.action == "migrate":
        migrate(settings)
        return
    if args.action == "api":
        import uvicorn
        from quant_platform.api import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
        return
    if args.action == "dashboard":
        import subprocess
        import sys

        app = Path(__file__).parent / "dashboard/app.py"
        raise SystemExit(
            subprocess.call(
                [
                    sys.executable,
                    "-m",
                    "streamlit",
                    "run",
                    str(app),
                    "--server.address",
                    args.host,
                    "--server.port",
                    str(args.port),
                    "--server.headless",
                    "true",
                    "--browser.gatherUsageStats",
                    "false",
                ]
            )
        )
    if args.action == "restore-check":
        if not args.archive or not args.target_dsn_file:
            parser.error("restore-check requires --archive and --target-dsn-file")
        from quant_platform.operations import restore_check

        from datetime import datetime

        source_db = Database(settings.dsn)
        fault = datetime.fromisoformat(args.fault_at) if args.fault_at else None
        try:
            print(
                json.dumps(
                    restore_check(args.archive, args.target_dsn_file.read_text().strip(), source_db, fault),
                    default=str,
                )
            )
        finally:
            source_db.close()
        return
    db = Database(settings.dsn, role="quant_worker" if args.action == "worker" else None)
    try:
        if args.action == "worker":
            if not args.role:
                parser.error("worker requires --role")
            from quant_platform.jobs.worker import Worker

            Worker(db, settings, args.role).run()
        elif args.action == "health":
            rows = db.rows(
                "SELECT 1 FROM heartbeats WHERE role=%s AND updated_at>now()-interval '20 seconds' "
                "AND data->>'status' IS DISTINCT FROM 'stopped' LIMIT 1",
                (args.role,),
            )
            raise SystemExit(0 if rows else 1)
        elif args.action == "import-legacy":
            if not args.runtime:
                parser.error("import-legacy requires --runtime")
            from quant_platform.legacy import migrate_legacy

            print(json.dumps(migrate_legacy(db, args.runtime, args.apply), default=str, indent=2))
        else:
            from quant_platform.operations import doctor, status

            result = doctor(db, settings) if args.action == "doctor" else status(db, settings)
            print(json.dumps(result, default=str, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
