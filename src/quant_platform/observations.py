"""Append-only operator logs outside the API process."""

import json
from pathlib import Path

from filelock import FileLock


def append_jsonl(root, name, payload):
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.jsonl"
    line = json.dumps(payload, default=str, allow_nan=False) + "\n"
    with FileLock(str(path) + ".lock"):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        if path.stat().st_size > 1_000_000:
            kept = path.read_text(encoding="utf-8").splitlines()[-200:]
            path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    return path


def count_lines(root, name):
    path = Path(root) / f"{name}.jsonl"
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())
