"""Native dependency isolation and API-only dashboard behavior."""

import json
import logging
from pathlib import Path
import subprocess
import sys

import httpx
import pytest
from streamlit.testing.v1 import AppTest

from quant_platform.cli import JsonFormatter
from quant_platform.dashboard import client

APP = Path(client.__file__).with_name("app.py")


def test_native_imports_with_qlib_explicitly_forbidden(tmp_path):
    program = """
import sys, importlib.abc, importlib.metadata
class NoQlib(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"qlib", "adata"}:
            raise AssertionError("Optional engine/provider imported by native application")
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
for name in ("pyqlib", "mlflow", "lightgbm", "adata"):
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
            "timestamp": "2026-09-24T01:35:00+00:00",
            "boards": [
                {"board": "STAR Market", "listed": 20, "coverage": 0.5, "daily_return": 0.025},
                {"board": "ChiNext", "listed": 10, "coverage": 1.0, "daily_return": -0.01},
                {"board": "Main Board", "listed": 30, "coverage": 0, "daily_return": None},
            ],
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
    if path == "/scan-policy":
        from quant_platform.domain.workflow import ScanPolicy

        return {"revision": 2, "data": ScanPolicy().model_dump()}
    if path == "/watchlist":
        return {"revision": 0, "symbols": []}
    if path == "/data-readiness":
        return {
            "frequencies": {
                frequency: {"ready": False, "blockers": ["No approved Qlib model"], "model": None}
                for frequency in ("day", "5min")
            }
        }
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
    assert app.text_input[0].label == "访问令牌"
    assert not app.sidebar.radio


def test_dashboard_degraded_status_and_history_without_quotes(monkeypatch):
    app = signed_app(monkeypatch)
    assert not app.exception
    assert any("覆盖不足" in warning.value for warning in app.warning)
    app.sidebar.radio[0].set_value("Models & Validation").run()
    assert any("No models" in item.value for item in app.info)
    app.sidebar.radio[0].set_value("Stock Research").run()
    next(item for item in app.text_input if item.label == "Stock code").set_value("600895")
    next(b for b in app.button if b.label == "View prices and model research").click().run()
    assert not app.exception
    assert any("Unadjusted CNY" in text.value for text in app.caption)


def test_dashboard_disconnection_is_visible(monkeypatch):
    app = signed_app(monkeypatch)

    def disconnected(*args, **kwargs):
        raise httpx.ConnectError("test offline")

    monkeypatch.setattr(client.Client, "request", disconnected)
    app.run()
    assert not app.exception
    assert any("连接已断开" in error.value for error in app.error)


def test_dashboard_portfolio_editor_sends_explicit_cash_and_revision(monkeypatch):
    calls = []

    def request(self, method, path, data=None):
        calls.append((method, path, data))
        if method == "POST" and path == "/model-portfolios":
            return {"revision": 1, "effective_from": "2026-09-25T09:30:00+08:00"}
        return fake_request(self, method, path, data)

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.sidebar.radio[0].set_value("My Model Portfolio").run()
    next(b for b in app.button if b.label == "Save next-session baseline").click().run()
    assert not app.exception
    payload = next(data for method, path, data in calls if method == "POST")
    assert payload["expected_revision"] == 0
    assert payload["cash_weight"] == 1
    assert payload["weights"] == {}


@pytest.mark.parametrize(
    "page",
    ["Overview", "Stock Research", "My Model Portfolio", "Low-Price Scan", "Models & Validation", "Data & Pipeline"],
)
def test_dashboard_workspaces_render_without_data(monkeypatch, page):
    app = signed_app(monkeypatch)
    app.sidebar.radio[0].set_value(page).run()
    assert not app.exception


def test_dashboard_beijing_time_market_colors_and_missing_values(monkeypatch):
    app = signed_app(monkeypatch)
    html = "\n".join(item.value for item in app.markdown)
    assert "2026-09-24 09:35:00" in html
    assert 'qp-up">+2.50%' in html
    assert 'qp-down">-1.00%' in html
    assert 'qp-flat">—' in html
    assert "有效覆盖 50.0%" in html
    assert "科创板" in html and "沪深主板" in html
    assert any("No approved Qlib model" in item.value for item in app.warning)
    assert not any(b.label == "运行分析" for b in app.button)


def test_dashboard_escapes_provider_text(monkeypatch):
    def request(self, method, path, data=None):
        result = fake_request(self, method, path, data)
        if path == "/status":
            result["provider"] = '<img src=x onerror="alert(1)">'
        return result

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.run()
    html = "\n".join(item.value for item in app.markdown)
    assert "&lt;IMG" in html
    assert "<IMG" not in html
    assert not app.exception


def test_dashboard_scan_policy_uses_yuan_with_revision(monkeypatch):
    calls = []

    def request(self, method, path, data=None):
        calls.append((method, path, data))
        if method == "PUT":
            return {"revision": 3}
        return fake_request(self, method, path, data)

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.sidebar.radio[0].set_value("Low-Price Scan").run()
    next(n for n in app.number_input if n.label == "Minimum 20-session turnover (CNY)").set_value(35_000_000.0)
    next(n for n in app.number_input if n.label == "Maximum raw price (CNY)").set_value(8.0)
    next(b for b in app.button if b.label == "Save policy revision").click().run()
    assert not app.exception
    value = next(data for method, path, data in calls if method == "PUT")
    assert value["expected_revision"] == 2
    assert value["policy"]["minimum_turnover"] == 35_000_000
    assert value["policy"]["price_ceiling"] == 8
    assert "momentum_weight" not in value["policy"]
    assert app.success


def test_dashboard_stock_code_normalization_and_invalid_input(monkeypatch):
    calls = []

    def request(self, method, path, data=None):
        calls.append(path)
        return fake_request(self, method, path, data)

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.sidebar.radio[0].set_value("Stock Research").run()
    field = next(item for item in app.text_input if item.label == "Stock code")
    field.set_value("600895.sh")
    next(b for b in app.button if b.label == "View prices and model research").click().run()
    assert "/history/SH600895?limit=500" in calls
    assert not app.exception
    calls.clear()
    next(item for item in app.text_input if item.label == "Stock code").set_value("invalid")
    next(b for b in app.button if b.label == "View prices and model research").click().run()
    assert app.error and not app.exception
    assert not any(path.startswith("/history/") for path in calls)


def test_dashboard_legacy_reports_are_read_only(monkeypatch):
    calls = []

    def request(self, method, path, data=None):
        calls.append((method, path))
        if path == "/reports?limit=100":
            return [{"id": 1, "kind": "screen", "as_of": "2026-09-23", "engine": "legacy_native", "data": {}}]
        return fake_request(self, method, path, data)

    app = signed_app(monkeypatch)
    monkeypatch.setattr(client.Client, "request", request)
    app.sidebar.radio[0].set_value("Data & Pipeline").run()
    assert not app.exception
    assert any("legacy_native" in item.value for item in app.warning)
    assert all(method == "GET" for method, path in calls)
    assert not any("approve" in path for method, path in calls)


def test_dashboard_login_and_logout_clear_private_state(monkeypatch):
    monkeypatch.setattr(client.Client, "request", fake_request)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.text_input[0].set_value("test-token")
    next(b for b in app.button if b.label == "进入工作台").click().run()
    assert app.sidebar.radio and not app.exception
    app.session_state["stock_code"] = "SH600895"
    next(b for b in app.button if b.label == "退出登录").click().run()
    assert not app.sidebar.radio and not app.exception
    assert "token" not in app.session_state
    assert "stock_code" not in app.session_state


@pytest.mark.postgres
def test_dashboard_mutations_through_real_api_and_database(monkeypatch, db, settings, tmp_path):
    from fastapi.testclient import TestClient
    from quant_platform.api import create_app
    from quant_platform.storage import jsonb
    from test_workflow_postgres import proposal

    report_id, _ = proposal(db, settings)
    settings.artifact_root = settings.observation_root = tmp_path
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO reports(kind,target,as_of,input_hash,data) VALUES('screen','default',current_date,'ui-fixture',%s)",
            (jsonb({"coverage": 1, "candidates": [{"symbol": "SH600000", "score": 0.8}]}),),
        )
    with TestClient(create_app(settings, db)) as api:

        def request(self, method, path, data=None):
            response = api.request(
                method, "/api/v1" + path, json=data, headers={"Authorization": "Bearer " + self.token}
            )
            if response.status_code >= 300:
                raise ValueError(f"{response.status_code}: {response.json().get('detail')}")
            return response.json()

        monkeypatch.setattr(client.Client, "request", request)
        app = AppTest.from_file(str(APP), default_timeout=20)
        app.session_state["token"] = settings.api_token.get_secret_value()
        app.run()
        assert not app.exception and not app.error
        app.sidebar.radio[0].set_value("My Model Portfolio").run()
        next(n for n in app.text_input if n.label == "Portfolio name").set_value("UI baseline")
        next(b for b in app.button if b.label == "Save next-session baseline").click().run()
        assert not app.exception and not app.error
        saved = db.rows("SELECT * FROM model_portfolios")[0]
        assert saved["name"] == "UI baseline" and saved["revision"] == 1
        app.run()
        app.selectbox[0].set_value(str(saved["id"])).run()
        next(n for n in app.text_input if n.label == "Portfolio name").set_value("Revised baseline")
        next(b for b in app.button if b.label == "Save next-session baseline").click().run()
        assert not app.exception and not app.error
        assert db.rows("SELECT revision FROM model_portfolios")[0]["revision"] == 2
        app.sidebar.radio[0].set_value("Low-Price Scan").run()
        next(b for b in app.button if b.label == "Accept selected changes").click().run()
        assert app.error and not db.rows("SELECT * FROM recommendation_acceptances")
        next(c for c in app.checkbox if "sends no orders" in c.label).check().run()
        next(b for b in app.button if b.label == "Accept selected changes").click().run()
        assert not app.exception and not app.error
        accepted = db.rows("SELECT * FROM recommendation_acceptances WHERE recommendation_id=%s", (report_id,))[0]
        weights = db.rows(
            "SELECT data FROM model_portfolio_revisions WHERE portfolio_id=%s", (accepted["portfolio_id"],)
        )[0]["data"]
        assert weights["weights"] == {"SH600000": 0.035}
        assert weights["cash_weight"] == pytest.approx(0.965)
        next(b for b in app.button if b.label == "Save policy revision").click().run()
        assert len(db.rows("SELECT * FROM scan_policies")) == 2
        app.sidebar.radio[0].set_value("Data & Pipeline").run()
        assert app.json and not app.exception
        next(n for n in app.number_input if n.label == "行情轮询间隔（秒）").set_value(45)
        next(b for b in app.button if b.label == "保存运行设置").click().run()
        assert db.setting("runtime_options")["quote_seconds"] == 45
        app.sidebar.radio[0].set_value("Low-Price Scan").run()
        next(b for b in app.button if b.label == "Run or reuse daily Qlib inference").click().run()
        assert all(row["kind"].startswith("qlib_") for row in db.rows("SELECT kind FROM jobs"))
        assert not db.rows("SELECT * FROM baskets")
        assert not app.exception and not app.error


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
    for role in ("qlib-data", "qlib-train", "qlib-daily", "qlib-intraday"):
        service = base["services"][role]
        assert not service.get("profiles")
        assert service["command"] == ["worker", "--role", role]
        assert service["platform"] == "linux/amd64"
    assert "research" not in base["services"] and "qlib" not in base["services"]
    for role in ("research-data", "qlib-research", "qlib-diagnostics"):
        service = base["services"][role]
        assert service["profiles"] == ["research"]
        assert service["command"] == ["worker", "--role", role]
        assert service["secrets"] == ["database_url"]
        assert not {"QUANT_API_TOKEN_FILE", "QUANT_TUSHARE_TOKEN_FILE"} & service["environment"].keys()
        assert service["read_only"] and service["cpus"] <= 2 and service["pids_limit"] <= 256
        assert service["build"]["target"] == ("research-data" if role == "research-data" else "qlib")
    assert base["services"]["research-data"]["environment"]["QUANT_RESEARCH_DATA_ENABLED"].endswith(":-false}")


def test_dockerignore_excludes_secrets_and_runtime():
    text = (Path(__file__).parents[1] / ".dockerignore").read_text()
    for pattern in (".git", ".venv", ".runtime", "deploy/secrets", "legacy", "tests"):
        assert pattern in text.splitlines()


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
