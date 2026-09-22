"""Native dependency isolation and API-only dashboard behavior."""

import json
import logging
from pathlib import Path
import subprocess
import sys

import httpx
from streamlit.testing.v1 import AppTest

from quant_platform.cli import JsonFormatter
from quant_platform.dashboard import client

APP = Path(client.__file__).with_name("app.py")


def test_native_imports_with_qlib_explicitly_forbidden(tmp_path):
    program = """
import sys, importlib.abc, importlib.metadata
class NoQlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "qlib" or fullname.startswith("qlib."):
            raise AssertionError("Qlib imported by native application")
sys.meta_path.insert(0, NoQlib())
from quant_platform.api import create_app
from quant_platform.jobs.worker import Worker
from quant_platform.jobs.collect import history, quotes
from quant_platform.dashboard.client import Client
from quant_platform.analysis import indicators
from quant_platform.storage import Database
from quant_platform.config import Settings
assert create_app(Settings()).title == "Standalone Quant Platform"
assert indicators([])["status"] == "insufficient_data"
assert not any(m == "qlib" or m.startswith("qlib.") for m in sys.modules)
for name in ("pyqlib", "mlflow", "lightgbm"):
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        continue
    raise AssertionError(name + " installed in native environment")
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", program], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_log_messages_are_encoded_as_valid_json():
    record = logging.LogRecord("test", logging.WARNING, "", 0, 'line "one"\nline two', (), None)
    assert json.loads(JsonFormatter().format(record))["message"] == record.getMessage()


def fake_request(self, method, path, data=None):
    if path == "/status":
        return {
            "provider": "tushare",
            "quote_count": 0,
            "fresh_quote_count": 0,
            "data_health": "degraded_or_unavailable",
            "analysis_target": None,
            "polling_paused": False,
            "services": [],
            "capabilities": [],
            "jobs": [],
            "directory": {},
            "last_analysis": None,
            "backup": None,
            "qlib": {},
            "qualification": {},
            "incidents": [],
        }
    if path == "/settings":
        return {"quote_seconds": 30, "revision": 0}
    if path.startswith("/history/"):
        return [{"day": "2026-09-21", "data": {"close": 10}}]
    return []


def signed_app(monkeypatch):
    monkeypatch.setattr(client.Client, "request", fake_request)
    app = AppTest.from_file(str(APP), default_timeout=15)
    app.session_state["token"] = "test-token"
    return app.run()


def test_dashboard_requires_login_without_spawning_workers():
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    assert app.text_input[0].label == "Operator token"
    assert not app.sidebar.radio


def test_dashboard_degraded_status_and_history_without_quotes(monkeypatch):
    app = signed_app(monkeypatch)
    assert not app.exception
    assert any("degraded" in warning.value for warning in app.warning)
    app.sidebar.radio[0].set_value("Stocks").run()
    next(b for b in app.button if b.label == "Load stock history and reports").click().run()
    assert not app.exception
    assert any("Unadjusted CNY" in text.value for text in app.caption)


def test_dashboard_disconnection_is_visible(monkeypatch):
    app = signed_app(monkeypatch)

    def disconnected(*args, **kwargs):
        raise httpx.ConnectError("test offline")

    monkeypatch.setattr(client.Client, "request", disconnected)
    app.run()
    assert not app.exception
    assert any("disconnected" in error.value for error in app.error)


def test_dashboard_basket_editor_sends_policy_and_revision(monkeypatch):
    calls = []

    def request(self, method, path, data=None):
        calls.append((method, path, data))
        if method == "POST" and path == "/baskets":
            return {"revision": 1, "effective_day": "2026-09-23"}
        return fake_request(self, method, path, data)

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.sidebar.radio[0].set_value("Baskets").run()
    next(b for b in app.button if b.label == "Save next-session revision").click().run()
    assert not app.exception
    payload = next(data for method, path, data in calls if method == "POST")
    assert payload["expected_revision"] == 0
    assert payload["alerts"]["minimum_coverage"] == 0.95
    assert payload["members"] == {"SH600895": 1}


def test_package_build_excludes_runtime_and_secrets():
    import tomllib

    config = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    build = config["tool"]["hatch"]["build"]
    assert {".venv", ".runtime", "deploy/secrets"} <= set(build["exclude"])
    assert "deploy/secrets" not in build["targets"]["sdist"]["include"]


def load_local_preparation():
    import runpy

    return runpy.run_path(str(Path(__file__).parents[1] / "deploy/prepare_local.py"))["prepare"]


def test_local_preparation_preserves_credentials_and_private_directory(tmp_path):
    import stat

    root = tmp_path.resolve() / "secrets"
    root.mkdir()
    (root / "tushare_token").write_text("test-fixture-token")
    prepare = load_local_preparation()
    first = prepare(root)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert set(first["created"]) == {"postgres_password", "database_url", "api_token"}
    assert not first["provider_entitlement_verified"]
    assert prepare(root)["created"] == []
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in root.iterdir())
    assert len(before["api_token"].strip()) >= 32
    assert "test-fixture-token" not in json.dumps(first)


def test_local_preparation_rejects_missing_token_and_conflicting_database(tmp_path):
    import pytest

    root = tmp_path.resolve() / "secrets"
    root.mkdir()
    prepare = load_local_preparation()
    with pytest.raises(ValueError, match="Provision"):
        prepare(root)
    assert not list(root.iterdir())
    (root / "tushare_token").write_text("test-fixture-token")
    (root / "postgres_password").write_text("test-password")
    (root / "database_url").write_text("postgresql://quant:wrong@postgres:5432/quant")
    with pytest.raises(ValueError, match="do not match"):
        prepare(root)
    assert not (root / "api_token").exists()


def test_local_preparation_rejects_symlink_secret(tmp_path):
    import pytest

    root = tmp_path.resolve() / "secrets"
    root.mkdir()
    target = tmp_path / "outside"
    target.write_text("test-fixture-token")
    (root / "tushare_token").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        load_local_preparation()(root)
    assert target.read_text() == "test-fixture-token"


def test_local_compose_uses_persistent_backup_volume():
    import yaml

    root = Path(__file__).parents[1] / "deploy"
    local = yaml.safe_load((root / "compose.local.yaml").read_text())
    assert "local-backups" in local["volumes"]
    base = yaml.safe_load((root / "compose.yaml").read_text())
    assert base["services"]["api"]["ports"] == ["127.0.0.1:8000:8000"]
    assert base["services"]["dashboard"]["ports"] == ["127.0.0.1:8501:8501"]
    assert not base["services"]["postgres"].get("ports")


def test_built_distribution_contents():
    import os
    import tarfile
    import zipfile
    import pytest

    if os.getenv("QUANT_TEST_ARTIFACTS") != "1":
        pytest.skip("Build distribution archives first and set QUANT_TEST_ARTIFACTS=1")
    root = Path(__file__).parents[1] / "dist"
    with tarfile.open(root / "standalone_quant_platform-0.1.0.tar.gz") as archive:
        members = archive.getmembers()
        assert all(not item.issym() and not item.islnk() for item in members)
        names = [item.name for item in members]
        assert not any(
            part in name.split("/") for name in names for part in (".venv", ".runtime", "secrets", "__pycache__")
        )
        assert sum(item.size for item in members) < 5_000_000
    with zipfile.ZipFile(root / "standalone_quant_platform-0.1.0-py3-none-any.whl") as archive:
        names = archive.namelist()
        assert "quant_platform/storage/schema.sql" in names
        assert "quant_platform/storage/migrations/versions/0002_recovery.py" in names
        assert not any(name.startswith(("tests/", "qlib/")) or "__pycache__" in name for name in names)
        metadata = archive.read("standalone_quant_platform-0.1.0.dist-info/METADATA").decode()
        for line in metadata.splitlines():
            if line.startswith("Requires-Dist:") and any(name in line for name in ("pyqlib", "mlflow")):
                assert "extra == 'qlib'" in line or 'extra == "qlib"' in line
