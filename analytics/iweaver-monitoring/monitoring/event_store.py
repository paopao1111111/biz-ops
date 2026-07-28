"""SQLite event store for unified monitoring.

The store is intentionally small and dependency-free because it is imported from
long-running production services. Every operation opens and closes its own
connection so EDM, Feedback, dashboard jobs, and report jobs can write safely
from separate processes.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import EVENT_DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warning',
    title TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    user_id TEXT NOT NULL DEFAULT '',
    user_email TEXT NOT NULL DEFAULT '',
    window_start TEXT NOT NULL DEFAULT '',
    window_end TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    sent_to_feishu INTEGER NOT NULL DEFAULT 0,
    feishu_message_id TEXT NOT NULL DEFAULT '',
    send_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_monitor_events_created
ON monitor_events(created_at);

CREATE INDEX IF NOT EXISTS idx_monitor_events_source_created
ON monitor_events(source, created_at);

CREATE INDEX IF NOT EXISTS idx_monitor_events_type_created
ON monitor_events(event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_monitor_events_severity_created
ON monitor_events(severity, created_at);

CREATE TABLE IF NOT EXISTS monitor_report_runs (
    report_date TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'sent',
    feishu_message_id TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    send_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class MonitoringEventStore:
    def __init__(self, db_path: str | Path = EVENT_DB_PATH):
        ensure_dirs()
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def record_event(
        self,
        *,
        event_key: str,
        source: str,
        event_type: str,
        severity: str = "warning",
        title: str = "",
        object_id: str = "",
        user_id: str = "",
        user_email: str = "",
        window_start: str = "",
        window_end: str = "",
        status: str = "",
        payload: dict[str, Any] | None = None,
        sent_to_feishu: bool = False,
        feishu_message_id: str = "",
        send_error: str = "",
        created_at: str | None = None,
    ) -> dict[str, Any]:
        """Insert or update an event.

        Returns {'inserted': bool, 'event': dict}. Duplicate event_keys are
        updated with latest payload/status/send state but do not create another
        event row.
        """
        now = utc_now()
        created = created_at or now
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
        inserted = False
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO monitor_events (
                        event_key, source, event_type, severity, title, object_id,
                        user_id, user_email, window_start, window_end, status,
                        payload_json, sent_to_feishu, feishu_message_id, send_error,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_key,
                        source,
                        event_type,
                        severity,
                        title,
                        object_id,
                        user_id,
                        user_email,
                        window_start,
                        window_end,
                        status,
                        payload_json,
                        1 if sent_to_feishu else 0,
                        feishu_message_id or "",
                        send_error or "",
                        created,
                        now,
                    ),
                )
                inserted = True
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE monitor_events
                    SET severity=?, title=?, object_id=?, user_id=?, user_email=?,
                        window_start=?, window_end=?, status=?, payload_json=?,
                        sent_to_feishu=CASE WHEN ? THEN 1 ELSE sent_to_feishu END,
                        feishu_message_id=CASE WHEN ? != '' THEN ? ELSE feishu_message_id END,
                        send_error=CASE WHEN ? != '' THEN ? ELSE send_error END,
                        updated_at=?
                    WHERE event_key=?
                    """,
                    (
                        severity,
                        title,
                        object_id,
                        user_id,
                        user_email,
                        window_start,
                        window_end,
                        status,
                        payload_json,
                        1 if sent_to_feishu else 0,
                        feishu_message_id or "",
                        feishu_message_id or "",
                        send_error or "",
                        send_error or "",
                        now,
                        event_key,
                    ),
                )
            row = conn.execute("SELECT * FROM monitor_events WHERE event_key=?", (event_key,)).fetchone()
        return {"inserted": inserted, "event": row_to_dict(row) if row else {}}

    def mark_event_sent(self, event_key: str, message_id: str = "", error: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE monitor_events
                SET sent_to_feishu=?, feishu_message_id=?, send_error=?, updated_at=?
                WHERE event_key=?
                """,
                (0 if error else 1, message_id or "", error or "", utc_now(), event_key),
            )

    def list_events(
        self,
        *,
        start_at: str = "",
        end_at: str = "",
        source: str = "",
        event_type: str = "",
        severities: Iterable[str] | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if start_at:
            clauses.append("created_at >= ?")
            params.append(start_at)
        if end_at:
            clauses.append("created_at < ?")
            params.append(end_at)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if severities:
            sev_list = list(severities)
            if sev_list:
                clauses.append("severity IN ({})".format(",".join("?" for _ in sev_list)))
                params.extend(sev_list)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM monitor_events {where} ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_dict(row) for row in rows]

    def count_by(self, *, start_at: str, end_at: str, field: str) -> dict[str, int]:
        if field not in {"source", "event_type", "severity", "status"}:
            raise ValueError(f"unsupported count field: {field}")
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT {field} AS key, COUNT(*) AS count
                FROM monitor_events
                WHERE created_at >= ? AND created_at < ?
                GROUP BY {field}
                """,
                (start_at, end_at),
            ).fetchall()
        return {str(row["key"]): int(row["count"] or 0) for row in rows}

    def record_report_run(
        self,
        report_date: str,
        *,
        payload: dict[str, Any] | None = None,
        status: str = "sent",
        message_id: str = "",
        error: str = "",
    ) -> bool:
        """Record a daily report. Returns True when this is the first run."""
        now = utc_now()
        payload_json = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, default=str)
        with self._connect() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO monitor_report_runs (
                        report_date, status, feishu_message_id, payload_json,
                        send_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (report_date, status, message_id or "", payload_json, error or "", now, now),
                )
                return True
            except sqlite3.IntegrityError:
                conn.execute(
                    """
                    UPDATE monitor_report_runs
                    SET status=?, feishu_message_id=?, payload_json=?, send_error=?, updated_at=?
                    WHERE report_date=?
                    """,
                    (status, message_id or "", payload_json, error or "", now, report_date),
                )
                return False

    def get_report_run(self, report_date: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM monitor_report_runs WHERE report_date=?", (report_date,)).fetchone()
        return row_to_dict(row) if row else None


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    data = dict(row)
    if "payload_json" in data:
        try:
            data["payload"] = json.loads(data.get("payload_json") or "{}")
        except json.JSONDecodeError:
            data["payload"] = {}
    return data
