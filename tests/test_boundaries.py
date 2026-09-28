"""Native dependency isolation and deployment packaging boundaries."""

import json
import logging
from pathlib import Path
import subprocess
import sys

import pytest

from quant_platform.cli import JsonFormatter


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
from quant_platform.analysis import basket_summary
from quant_platform.storage import Database
from quant_platform.config import Settings
assert create_app(Settings()).title == "Standalone Quant Platform"
assert basket_summary({}, {}, None)["coverage"] == 0
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
    prepare = load_local_preparation()
    first = prepare(root)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    assert set(first["created"]) == {"postgres_password", "database_url", "api_token"}
    assert set(before) == {"postgres_password", "database_url", "api_token"}
    assert not first["source_reachability_verified"]
    assert prepare(root)["created"] == []
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o444 for p in root.iterdir())
    assert len(before["api_token"].strip()) >= 32
    assert before["api_token"].decode().strip() not in json.dumps(first)
    assert before["postgres_password"].decode().strip() not in json.dumps(first)


def test_local_preparation_rejects_conflicting_database_credentials(tmp_path):
    import pytest

    root = tmp_path.resolve() / "secrets"
    root.mkdir()
    prepare = load_local_preparation()
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
    (root / "api_token").symlink_to(target)
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
        assert "QUANT_API_TOKEN_FILE" not in service["environment"]
        assert service["read_only"] and service["cpus"] <= 2 and service["pids_limit"] <= 256
        assert service["build"]["target"] == ("research-data" if role == "research-data" else "qlib")
    assert base["services"]["research-data"]["environment"]["QUANT_RESEARCH_DATA_ENABLED"].endswith(":-false}")


def test_every_enqueued_job_kind_has_a_handler():
    """调度器排队的种类，处理器必须认识。

    令牌时代的主表采集叫 `directory`，换源后改名 `securities`，但调度器与处理器的清单没有一起
    更新；升级过的部署因此留下永远无法执行的阻塞任务。
    """
    import ast

    from quant_platform.jobs.worker import JOB_KINDS

    enqueued = set()
    for path in (Path(__file__).parents[1] / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or len(node.args) < 2:
                continue
            callee = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            kind = node.args[1]
            if callee == "enqueue" and isinstance(kind, ast.Constant) and isinstance(kind.value, str):
                enqueued.add(kind.value)
    assert enqueued, "没有扫到任何 enqueue 调用，扫描逻辑本身失效了"
    assert enqueued <= JOB_KINDS, f"排队的种类没有处理器：{sorted(enqueued - JOB_KINDS)}"


def test_compose_wires_every_tunable_setting():
    """文档把采集窗口与限速列为可调参数；不传进容器，运维就只能重建镜像改默认值。"""
    import yaml

    from quant_platform.config import Settings

    base = yaml.safe_load((Path(__file__).parents[1] / "deploy" / "compose.yaml").read_text())
    wired = {
        name
        for service in base["services"].values()
        for name in (service.get("environment") or {})
    }
    # 不经环境变量是有意的：凭据走 secrets 挂载，容器内路径由 Dockerfile ENV 固定，
    # 部署形态由 Settings 校验锁定，接受运维覆盖就等于放弃这些不变量。
    deliberate = {
        "QUANT_DATABASE_URL",
        "QUANT_DATABASE_URL_FILE",
        "QUANT_API_TOKEN",
        "QUANT_API_TOKEN_FILE",
        "QUANT_ARTIFACT_ROOT",
        "QUANT_BACKUP_ROOT",
        "QUANT_PROVIDER",
        "QUANT_ENVIRONMENT",
        "QUANT_QLIB_ENABLED",
    }
    missing = {
        "QUANT_" + name.upper()
        for name in Settings.model_fields
        if "QUANT_" + name.upper() not in wired | deliberate
    }
    assert missing == set(), f"compose 没有把 {sorted(missing)} 传给容器"


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
