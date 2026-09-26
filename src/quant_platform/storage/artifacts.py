"""Snapshot-consistent backups and conservative retention of immutable artifacts."""

import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row

from quant_platform.adapters.qlib.data import checksum, safe_artifact, verify
from quant_platform.domain import now
from quant_platform.domain.workflow import PipelineBlocked
from quant_platform.storage import jsonb

LIFECYCLE_LOCK = 77610243


def references(conn):
    artifacts = {}
    for row in conn.execute(
        "SELECT path,manifest FROM qlib_generations UNION ALL SELECT path,metadata FROM model_versions"
    ).fetchall():
        artifacts[row["path"]] = row["manifest"]
        recorder = row["manifest"].get("recorder")
        if recorder:
            artifacts[recorder["path"]] = recorder["manifest"]
    # Older backup schemas remain restorable before the additive research migration.
    if conn.execute("SELECT to_regclass('public.research_artifacts') AS name").fetchone()["name"]:
        for row in conn.execute("SELECT path,manifest FROM research_artifacts").fetchall():
            artifacts[row["path"]] = row["manifest"]
            recorder = row["manifest"].get("recorder")
            if recorder:
                artifacts[recorder["path"]] = recorder["manifest"]
    return artifacts


def artifact_files(root, artifacts):
    files = {}
    for relative, manifest in artifacts.items():
        folder = safe_artifact(root, relative)
        if "files" not in manifest:
            raise PipelineBlocked("Referenced artifact has no checksum manifest.")
        verify(folder, manifest)
        stored = safe_artifact(folder, "manifest.json")
        if json.loads(stored.read_text()) != manifest:
            raise PipelineBlocked("Published and stored artifact manifests differ.")
        for name in [*manifest["files"], "manifest.json"]:
            path = safe_artifact(folder, name)
            archive_name = "artifacts/" + path.relative_to(Path(root).resolve()).as_posix()
            files[archive_name] = (path, checksum(path))
    return files


def backup(db, settings, job):
    from quant_platform.operations import copy_verified_replica, pg_environment

    root = settings.backup_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    backup_id = uuid4()
    name = "quant-" + now().strftime("%Y%m%dT%H%M%S") + "-" + backup_id.hex + ".tar"
    target = root / name
    with tempfile.TemporaryDirectory(prefix=".backup-", dir=root) as temporary:
        temporary = Path(temporary)
        dump = temporary / "database.dump"
        staged = temporary / name
        with db.transaction() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_LOCK,))
            snapshot = conn.execute("SELECT pg_export_snapshot() AS id,now() AS at").fetchone()
            artifacts = references(conn)
            files = artifact_files(settings.artifact_root, artifacts)
            result = subprocess.run(
                ["pg_dump", "--format=custom", "--no-owner", "--snapshot", snapshot["id"], "--file", str(dump)],
                env=pg_environment(settings.dsn),
                capture_output=True,
                timeout=600,
            )
            if result.returncode:
                raise RuntimeError("Backup failed; verify PostgreSQL client and backup volume.")
            files["database.dump"] = (dump, checksum(dump))
            manifest = {
                "format": "quap-snapshot-v1",
                "snapshot_at": snapshot["at"].isoformat(),
                "artifacts": artifacts,
                "files": {name: value[1] for name, value in files.items()},
            }
            with tarfile.open(staged, "w") as archive:
                for archive_name, (path, expected) in files.items():
                    archive.add(path, arcname=archive_name, recursive=False)
                    if checksum(path) != expected:
                        raise PipelineBlocked("Artifact changed while creating the backup.")
                body = json.dumps(manifest, allow_nan=False).encode()
                info = tarfile.TarInfo("manifest.json")
                info.size, info.mode = len(body), 0o600
                archive.addfile(info, io.BytesIO(body))
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staged, target)
    archive_hash = checksum(target)
    replica = (
        copy_verified_replica(target, settings.backup_replica_root, archive_hash)
        if settings.backup_replica_root
        else None
    )
    state = {
        "id": str(backup_id),
        "at": manifest["snapshot_at"],
        "file": name,
        "format": manifest["format"],
        "bytes": target.stat().st_size,
        "sha256": archive_hash,
        "artifact_count": len(artifacts),
        "restore_verified": False,
        "replica": replica,
    }
    with db.publication(job) as conn:
        conn.execute(
            "INSERT INTO artifact_backups(id,data) VALUES(%s,%s)",
            (backup_id, jsonb({**state, "artifacts": sorted(artifacts)})),
        )
        db.set_setting(conn, "backup", state)
    # Legacy database-only dumps remain untouched; only owned, committed bundles are retired.
    with db.transaction() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_LOCK,))
        for old in conn.execute("SELECT id,data FROM artifact_backups ORDER BY created_at DESC OFFSET 7").fetchall():
            old_name = old["data"]["file"]
            if not re.fullmatch(r"quant-\d{8}T\d{6}-[0-9a-f]{32}\.tar", old_name):
                continue
            path = root / old_name
            if path.is_file() and not path.is_symlink():
                path.unlink()
                conn.execute("DELETE FROM artifact_backups WHERE id=%s", (old["id"],))
    return state


def unpack_verified(archive, destination):
    """Reject malformed bundles before any database restore is attempted."""
    try:
        return _unpack_verified(archive, destination)
    except (tarfile.TarError, KeyError, ValueError, TypeError, UnicodeError) as exc:
        raise PipelineBlocked("Backup archive or manifest is malformed.") from exc


def _unpack_verified(archive, destination):
    """Never extract links, duplicate names, traversal paths, or unlisted content."""
    with tarfile.open(archive, "r:") as bundle:
        members = bundle.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names) or any(not member.isfile() for member in members):
            raise PipelineBlocked("Backup contains duplicate or nonregular entries.")
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or str(path) != name:
                raise PipelineBlocked("Backup contains an unsafe path.")
        info = bundle.getmember("manifest.json")
        if info.size > 64 * 1024 * 1024:
            raise PipelineBlocked("Backup manifest exceeds the supported size.")
        manifest = json.load(bundle.extractfile(info))
        if not isinstance(manifest, dict) or manifest.get("format") != "quap-snapshot-v1":
            raise PipelineBlocked("Backup format is unsupported.")
        if not isinstance(manifest.get("files"), dict) or not isinstance(manifest.get("artifacts"), dict):
            raise PipelineBlocked("Backup requires file and artifact manifests.")
        snapshot_at = datetime.fromisoformat(manifest["snapshot_at"])
        if snapshot_at.tzinfo is None:
            raise PipelineBlocked("Backup snapshot timestamp must include a timezone.")
        if any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in manifest["files"].values()
        ):
            raise PipelineBlocked("Backup contains invalid checksums.")
        if set(names) != set(manifest["files"]) | {"manifest.json"}:
            raise PipelineBlocked("Backup content does not match its manifest.")
        for relative, metadata in manifest["artifacts"].items():
            path = PurePosixPath(relative)
            if path.is_absolute() or ".." in path.parts or str(path) != relative or relative == ".":
                raise PipelineBlocked("Backup contains an unsafe artifact reference.")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("files"), dict):
                raise PipelineBlocked("Backup contains an invalid artifact manifest.")
        if "database.dump" not in manifest["files"] or any(
            name != "database.dump" and not name.startswith("artifacts/") for name in manifest["files"]
        ):
            raise PipelineBlocked("Backup contains unsupported content.")
        for name, expected in manifest["files"].items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(name) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if checksum(target) != expected:
                raise PipelineBlocked("Backup checksum mismatch.")
    return manifest


def restore_check(archive, target_dsn, source_db=None, fault_at=None, artifact_root=None):
    from quant_platform.operations import pg_environment, recovery_times

    started = now()
    archive = Path(archive).resolve(strict=True)
    env = pg_environment(target_dsn)
    if not env["PGDATABASE"].startswith("quant_restore_"):
        raise ValueError("Restore verification requires a separate quant_restore_* database.")
    with psycopg.connect(target_dsn) as conn:
        if conn.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]:
            raise ValueError("Verification database must be empty.")
    destination = Path(artifact_root or archive.parent / (archive.stem + "-restored-artifacts")).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("Artifact verification requires a new, isolated destination.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=destination.parent) as temporary:
        temporary = Path(temporary)
        manifest = unpack_verified(archive, temporary)
        result = subprocess.run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--dbname",
                env["PGDATABASE"],
                str(temporary / "database.dump"),
            ],
            env=env,
            capture_output=True,
            timeout=600,
        )
        if result.returncode:
            raise RuntimeError("Restore validation failed.")
        with psycopg.connect(target_dsn, row_factory=dict_row) as conn:
            restored = references(conn)
            if restored != manifest["artifacts"]:
                raise PipelineBlocked("Restored database and artifact references differ.")
            artifacts = temporary / "artifacts"
            artifacts.mkdir(exist_ok=True)
            artifact_files(artifacts, restored)
            result = {
                "schema": conn.execute("SELECT version_num FROM alembic_version").fetchone()["version_num"],
                "reports": conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"],
                "recommendations": conn.execute("SELECT count(*) AS n FROM recommendations").fetchone()["n"],
                "artifact_count": len(restored),
                "artifact_root": str(destination),
                "restored": True,
            }
        os.replace(artifacts, destination)
    finished = now()
    result.update(recovery_times(datetime.fromisoformat(manifest["snapshot_at"]), fault_at or started, finished))
    if source_db:
        archive_hash = checksum(archive)
        with source_db.transaction() as conn:
            saved = conn.execute("SELECT value FROM settings WHERE key='backup' FOR UPDATE").fetchone()
            if saved and saved["value"].get("sha256") == archive_hash:
                source_db.set_setting(
                    conn,
                    "backup",
                    {
                        **saved["value"],
                        "restore_verified": True,
                        "verified_at": finished.isoformat(),
                        "verification": result,
                    },
                )
    return result


def owned_directories(root):
    for parent, pattern in (
        ("qlib", r"generation-[0-9a-f]{32}"),
        ("models", r"[0-9a-f]{32}"),
        ("research", r"[0-9a-f]{32}"),
    ):
        directory = root / parent
        if directory.is_dir() and not directory.is_symlink():
            for folder in directory.iterdir():
                if re.fullmatch(pattern, folder.name) and folder.is_dir() and not folder.is_symlink():
                    yield folder
    experiments = root / "experiments"
    if not experiments.is_dir() or experiments.is_symlink():
        return
    for experiment in experiments.iterdir():
        if not re.fullmatch(r"[0-9]+", experiment.name) or not experiment.is_dir() or experiment.is_symlink():
            continue
        for folder in experiment.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            marker = folder / "quap-owner.json"
            if not marker.is_file() or marker.is_symlink() or marker.stat().st_size > 1024:
                continue
            try:
                owner = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            if owner == {"format": "quap-qlib-recorder-v1", "recorder_id": folder.name}:
                yield folder


def prune_orphans(conn, settings, at):
    """Published references are retained; only abandoned owned directories expire."""
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LIFECYCLE_LOCK,))
    if conn.execute(
        "SELECT 1 FROM jobs WHERE (kind LIKE 'qlib_%%' OR kind='research_collect') AND status='running' LIMIT 1"
    ).fetchone():
        return 0
    protected = set(references(conn))
    for backup_row in conn.execute("SELECT data FROM artifact_backups").fetchall():
        protected.update(backup_row["data"].get("artifacts", []))
    root = settings.artifact_root.resolve()
    cutoff = at - timedelta(seconds=max(settings.training_deadline_seconds, settings.data_deadline_seconds) + 3600)
    removed = 0
    for folder in owned_directories(root):
        relative = folder.relative_to(root).as_posix()
        if relative in protected or any(path.startswith(relative + "/") for path in protected):
            continue
        if datetime.fromtimestamp(folder.stat().st_mtime, cutoff.tzinfo) >= cutoff:
            continue
        shutil.rmtree(folder)
        removed += 1
        if removed >= 100:
            return removed
    return removed
