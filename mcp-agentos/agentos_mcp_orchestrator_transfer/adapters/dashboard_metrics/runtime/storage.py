import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_date TEXT NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL,
    source TEXT NOT NULL,
    frequency TEXT NOT NULL,
    window_start TEXT,
    window_end TEXT,
    dimensions_json TEXT NOT NULL DEFAULT '{}',
    dimensions_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    reason TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL,
    UNIQUE(metric_date, metric_name, source, frequency, dimensions_hash)
);

CREATE INDEX IF NOT EXISTS idx_metric_points_name_date
ON metric_points(metric_name, metric_date);

CREATE INDEX IF NOT EXISTS idx_metric_points_source_date
ON metric_points(source, metric_date);

CREATE TABLE IF NOT EXISTS alert_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_name TEXT NOT NULL,
    current_value REAL,
    previous_value REAL,
    change_ratio REAL,
    threshold REAL NOT NULL,
    triggered INTEGER NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    previous_window_start TEXT NOT NULL,
    previous_window_end TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_runs_created
ON alert_runs(created_at);
"""


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def dimensions_key(dimensions):
    normalized = json.dumps(dimensions or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return normalized, hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:24]


class MetricStore:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_metrics(self, metrics):
        collected_at = utc_now()
        written = 0
        with self._connect() as conn:
            for item in metrics:
                dimensions_json, dim_hash = dimensions_key(item.get('dimensions'))
                conn.execute(
                    """
                    INSERT INTO metric_points (
                        metric_date, metric_name, metric_value, source, frequency,
                        window_start, window_end, dimensions_json, dimensions_hash,
                        status, reason, collected_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(metric_date, metric_name, source, frequency, dimensions_hash)
                    DO UPDATE SET
                        metric_value=excluded.metric_value,
                        window_start=excluded.window_start,
                        window_end=excluded.window_end,
                        status=excluded.status,
                        reason=excluded.reason,
                        collected_at=excluded.collected_at
                    """,
                    (
                        item['metric_date'],
                        item['metric_name'],
                        item.get('metric_value'),
                        item['source'],
                        item.get('frequency', 'daily'),
                        item.get('window_start'),
                        item.get('window_end'),
                        dimensions_json,
                        dim_hash,
                        item.get('status', 'ok'),
                        item.get('reason', ''),
                        item.get('collected_at') or collected_at,
                    ),
                )
                written += 1
        return written

    def record_alerts(self, alerts):
        created_at = utc_now()
        with self._connect() as conn:
            for item in alerts:
                conn.execute(
                    """
                    INSERT INTO alert_runs (
                        metric_name, current_value, previous_value, change_ratio, threshold,
                        triggered, window_start, window_end, previous_window_start,
                        previous_window_end, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item['metric_name'],
                        item.get('current_value'),
                        item.get('previous_value'),
                        item.get('change_ratio'),
                        item['threshold'],
                        1 if item.get('triggered') else 0,
                        item['window_start'],
                        item['window_end'],
                        item['previous_window_start'],
                        item['previous_window_end'],
                        json.dumps(item.get('details') or {}, ensure_ascii=False, sort_keys=True),
                        created_at,
                    ),
                )
        return len(alerts)

    def latest_metrics(self, limit=200):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM metric_points
                ORDER BY metric_date DESC, collected_at DESC, metric_name ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def metric_series(self, metric_name='', start_date='', end_date='', source=''):
        filters = []
        params = []
        if metric_name:
            filters.append('metric_name = ?')
            params.append(metric_name)
        if start_date:
            filters.append('metric_date >= ?')
            params.append(start_date)
        if end_date:
            filters.append('metric_date <= ?')
            params.append(end_date)
        if source:
            filters.append('source = ?')
            params.append(source)
        where = f"WHERE {' AND '.join(filters)}" if filters else ''
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM metric_points
                {where}
                ORDER BY metric_name ASC, metric_date ASC, source ASC
                LIMIT 5000
                """,
                params,
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def recent_alerts(self, limit=100):
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert_runs
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [row_to_dict(row) for row in rows]

    def metric_names(self):
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT metric_name FROM metric_points ORDER BY metric_name ASC"
            ).fetchall()
        return [row['metric_name'] for row in rows]


def row_to_dict(row):
    data = dict(row)
    if 'dimensions_json' in data:
        try:
            data['dimensions'] = json.loads(data.pop('dimensions_json') or '{}')
        except json.JSONDecodeError:
            data['dimensions'] = {}
    if 'details_json' in data:
        try:
            data['details'] = json.loads(data.pop('details_json') or '{}')
        except json.JSONDecodeError:
            data['details'] = {}
    return data
