# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""One-shot Qlib reader; process isolation prevents stale provider caches."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

from .history import QlibHistory
from .models import Basket


def read_qlib(path, action="calendar", **arguments):
    request = {"path": str(path), "action": action, **arguments}
    process = subprocess.run(
        [sys.executable, "-m", "legacy_monitor.reader"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    lines = [line for line in process.stdout.splitlines() if line.startswith("MONITOR_JSON=")]
    if process.returncode or not lines:
        raise RuntimeError(f"Isolated Qlib read failed: {process.stderr[-1500:]}")
    response = json.loads(lines[-1].split("=", 1)[1])
    if "error" in response:
        raise ValueError(response["error"])
    return response


def execute(request):
    history = QlibHistory(request["path"])
    response = {"days": sorted(day.isoformat() for day in history.calendar.days), "end_date": str(history.end_date)}
    action = request["action"]
    if action == "features":
        frame = history.features(request["symbol"], request.get("lookback", 1000)).reset_index()
        response["rows"] = json.loads(frame.to_json(orient="records", date_format="iso"))
    elif action == "resolve":
        baskets = tuple(
            Basket(item["name"], tuple(tuple(pair) for pair in item["members"]), item["universe"])
            for item in request["baskets"]
        )
        resolved = history.resolve(baskets, date.fromisoformat(request["day"]))
        response["baskets"] = [{"name": basket.name, "members": basket.members} for basket in resolved]
    elif action == "validate":
        symbols = history.api.list_instruments(history.api.instruments("all"), as_list=True)
        if not symbols:
            raise ValueError("Generated Qlib dataset has no instruments.")
        frame = history.api.features(symbols[:1], ["$close", "$volume"], freq="day", disk_cache=0)
        if frame.empty or frame["$close"].dropna().empty:
            raise ValueError("Generated Qlib features failed validation.")
        response["instruments"] = len(symbols)
    elif action != "calendar":
        raise ValueError("Unknown isolated-reader operation.")
    return response


if __name__ == "__main__":
    try:
        result = execute(json.load(sys.stdin))
    except Exception as error:
        result = {"error": str(error)}
    print("MONITOR_JSON=" + json.dumps(result, allow_nan=False))
