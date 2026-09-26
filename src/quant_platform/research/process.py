"""Bounded trusted-code research subprocesses. This is not a generated-code sandbox."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.storage import LostLease

ALLOWED_MODULES = frozenset({"quant_platform.research.adata_adapter", "quant_platform.research.experiments"})


def run_research(module, payload, deadline, stop):
    if module not in ALLOWED_MODULES:
        raise PipelineBlocked("Research subprocess module is not allowlisted.")
    with tempfile.TemporaryDirectory(prefix="quap-research-") as temporary:
        output_path = Path(temporary) / "result.json"
        env = {
            "PATH": str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin",
            "HOME": temporary,
            "TMPDIR": temporary,
            "XDG_CACHE_HOME": temporary,
            "PYTHONUNBUFFERED": "1",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
        }
        body = json.dumps(payload, allow_nan=False, default=str).encode()
        if len(body) > 32 * 1024 * 1024:
            raise PipelineBlocked("Research input exceeds the bounded contract.")
        with tempfile.TemporaryFile() as logs, tempfile.TemporaryFile() as inputs:
            inputs.write(body)
            inputs.seek(0)
            process = subprocess.Popen(
                [sys.executable, "-m", module, str(output_path)],
                stdin=inputs,
                stdout=logs,
                stderr=logs,
                env=env,
                cwd=temporary,
                start_new_session=True,
                close_fds=True,
            )
            try:
                started = time.monotonic()
                while process.poll() is None:
                    if stop.wait(0.1):
                        raise LostLease("Research cancelled before publication.")
                    if time.monotonic() - started >= deadline:
                        raise PipelineBlocked("Research subprocess deadline exceeded.")
                    if os.fstat(logs.fileno()).st_size > 32 * 1024 * 1024:
                        raise PipelineBlocked("Research diagnostic output limit exceeded.")
                if process.returncode or not output_path.is_file() or output_path.stat().st_size > 32 * 1024 * 1024:
                    raise PipelineBlocked("Research subprocess did not return a bounded valid result.")
                try:
                    result = json.loads(output_path.read_text())
                except (OSError, ValueError):
                    raise PipelineBlocked("Research subprocess returned unreadable or malformed JSON.") from None
                if not isinstance(result, dict):
                    raise PipelineBlocked("Research subprocess result must be a JSON object.")
                return result
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                process.wait()
