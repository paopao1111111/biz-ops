"""Shared SQLite connection and idempotent schema migration helpers."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent
DEFAULT_SCHEMA_PATH = BASE_DIR / "schema.sql"
CURRENT_SCHEMA_VERSION = 4


def connect_database(path, read_only=False, timeout=30):
    target = Path(path)
    if read_only:
        connection = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=timeout)
        connection.execute("PRAGMA query_only=ON")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(target, timeout=timeout)
    connection.row_factory = sqlite3.Row
    return connection


def migrate_connection(connection, schema_path=DEFAULT_SCHEMA_PATH):
    """Apply additive schema changes safely and record the migration version."""
    connection.executescript(Path(schema_path).read_text(encoding="utf-8"))
    connection.execute(
        """INSERT OR IGNORE INTO schema_migrations(version, applied_at, description)
           VALUES (?, ?, ?)""",
        (
            CURRENT_SCHEMA_VERSION,
            datetime.now(timezone.utc).isoformat(),
            "V4 pre-aggregated daily and monthly metric series",
        ),
    )
    connection.commit()


def migrate_database(path, schema_path=DEFAULT_SCHEMA_PATH):
    connection = connect_database(path, read_only=False)
    try:
        migrate_connection(connection, schema_path)
    finally:
        connection.close()


def table_columns(connection, table_name):
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")}
