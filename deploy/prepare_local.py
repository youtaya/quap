"""Prepare private file secrets for the single-operator local Docker deployment.

Supply tushare_token yourself. Existing credentials are never replaced or printed.
The private directory is mode 0700; its mounted files are 0444 so container UID
10001 can read them without making the host directory traversable by other users.
Use a secret manager and explicit UID permissions for a multi-user Linux server.
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
from urllib.parse import quote, unquote, urlsplit

NAMES = ("postgres_password", "database_url", "api_token", "tushare_token")


def read_secret(path):
    if path.is_symlink():
        raise ValueError("Secret files must not be symbolic links.")
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 16384:
        raise ValueError("Secret file has an invalid type or size.")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("A supplied secret file is empty.")
    return value


def prepare(directory):
    directory = Path(directory).absolute()
    if os.name != "posix":
        raise ValueError("This helper requires POSIX file permissions.")
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("The secrets directory must not contain symbolic-link components.")
    if directory.exists() and (not directory.is_dir() or directory.stat().st_uid != os.getuid()):
        raise ValueError("The secrets directory must be owned by the invoking user.")
    supplied = {name: read_secret(directory / name) for name in NAMES}
    if not supplied["tushare_token"]:
        raise ValueError("Provision deploy/secrets/tushare_token privately before running this helper.")
    if supplied["database_url"] and not supplied["postgres_password"]:
        raise ValueError("Existing database_url requires its matching postgres_password file.")
    password = supplied["postgres_password"] or secrets.token_urlsafe(48)
    dsn = supplied["database_url"] or f"postgresql://quant:{quote(password, safe='')}@postgres:5432/quant"
    try:
        parsed = urlsplit(dsn)
        valid = (
            parsed.scheme == "postgresql"
            and parsed.username == "quant"
            and unquote(parsed.password or "") == password
            and parsed.hostname == "postgres"
            and parsed.port == 5432
            and parsed.path == "/quant"
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Existing database credentials do not match the local Compose database.")
    api_token = supplied["api_token"] or secrets.token_urlsafe(48)
    if len(api_token) < 32:
        raise ValueError("The existing operator token must contain at least 32 characters.")
    values = {**supplied, "postgres_password": password, "database_url": dsn, "api_token": api_token}
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    created = []
    for name in NAMES:
        path = directory / name
        if supplied[name] is None:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(values[name] + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            created.append(name)
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    return {
        "ready": True,
        "created": created,
        "secret_directory": str(directory),
        "credentials_disclosed": False,
        "provider_entitlement_verified": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--secrets-dir", type=Path, default=Path(__file__).with_name("secrets"))
    args = parser.parse_args()
    try:
        result = prepare(args.secrets_dir)
    except (OSError, ValueError):
        # Never render exception bodies: parsing or filesystem errors may contain private input.
        parser.exit(
            1,
            "Local secret preparation failed. Check private file ownership, contents, and matching database credentials.\n",
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
