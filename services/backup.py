"""Consistent daily database backups with bounded retention."""
from __future__ import annotations

from datetime import datetime, timedelta
import gzip
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from sqlalchemy.engine import make_url

from config import settings
from database import repo

log = logging.getLogger(__name__)
PREFIX = "immoalert-"
MARKER = "last_backup_at"
MIN_INTERVAL = timedelta(hours=23)


def _destination():
    directory = Path(settings.backup_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    return directory


def _write_sqlite(path, target):
    """The online backup API stays consistent while the bot keeps writing."""
    raw = target.with_suffix(".tmp")
    source = sqlite3.connect(path)
    copy = sqlite3.connect(raw)
    try:
        source.backup(copy)
    finally:
        copy.close()
        source.close()
    with open(raw, "rb") as plain, gzip.open(target, "wb") as packed:
        shutil.copyfileobj(plain, packed)
    raw.unlink()


def _write_postgres(url, target):
    if not shutil.which("pg_dump"):
        raise RuntimeError("pg_dump est introuvable : installez le client PostgreSQL pour sauvegarder.")
    environment = {**os.environ, "PGPASSWORD": url.password or ""}
    command = ["pg_dump", "--no-owner", "-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username or "postgres", url.database]
    with gzip.open(target, "wb") as packed:
        process = subprocess.run(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if process.returncode:
            raise RuntimeError("pg_dump a échoué.")
        packed.write(process.stdout)


def prune(directory, keep):
    files = sorted((p for p in directory.glob(PREFIX + "*.gz") if p.is_file()), key=lambda p: p.name, reverse=True)
    for old in files[keep:]:
        old.unlink(missing_ok=True)
    return max(0, len(files) - keep)


def run_backup(*, force=False, now=None):
    """Back up at most once a day unless forced. Returns the file path or None."""
    now = now or datetime.utcnow()
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database in (None, "", ":memory:"):
        return None
    last = repo.get_runtime_setting(MARKER)
    if not force and last:
        try:
            if now - datetime.fromisoformat(last) < MIN_INTERVAL:
                return None
        except ValueError:
            pass
    directory = _destination()
    target = directory / f"{PREFIX}{now:%Y%m%d-%H%M%S}.sql.gz"
    try:
        if url.get_backend_name() == "sqlite":
            _write_sqlite(url.database, target)
        else:
            _write_postgres(url, target)
        os.chmod(target, 0o600)
    except Exception as exc:
        target.unlink(missing_ok=True)
        target.with_suffix(".tmp").unlink(missing_ok=True)
        log.error("Database backup failed (%s)", type(exc).__name__)
        return None
    repo.set_runtime_setting(MARKER, now.isoformat())
    prune(directory, settings.backup_retention)
    log.info("Database backup written: %s", target.name)
    return target
