"""Read-only Superset client pinned to DB2 for the weekly dashboard."""

import logging
import os
import re
import threading
import time

import requests

logger = logging.getLogger(__name__)

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|call|copy|execute)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with|show|describe|desc)\b", re.IGNORECASE)


class SupersetError(RuntimeError):
    """Raised when Superset authentication or query execution fails."""


class SupersetClient:
    def __init__(self):
        self._local = threading.local()

    @property
    def base_url(self):
        return os.getenv("SUPERSET_URL", "http://galaxy.iweaver.ai").rstrip("/")

    @property
    def user(self):
        return os.getenv("SUPERSET_USER", "admin")

    @property
    def password(self):
        return os.getenv("SUPERSET_PASS", "")

    @property
    def database_id(self):
        return int(os.getenv("SUPERSET_DB_ID", "2"))

    @property
    def expected_database_name(self):
        return os.getenv("SUPERSET_DB_NAME", "iweaver-hermes-ai")

    def ensure_configured(self):
        if not self.password:
            raise SupersetError("SUPERSET_PASS is not configured")
        if self.database_id != 2:
            raise SupersetError(f"Refusing to query database_id={self.database_id}; expected DB2")

    def _ensure_session(self):
        session = getattr(self._local, "session", None)
        if session is not None:
            return session

        self.ensure_configured()
        session = requests.Session()
        session.trust_env = False
        response = session.post(
            f"{self.base_url}/api/v1/security/login",
            json={"username": self.user, "password": self.password, "provider": "db"},
            timeout=20,
        )
        response.raise_for_status()
        token = response.json()["access_token"]
        session.headers.update({"Authorization": f"Bearer {token}"})

        csrf_response = session.get(
            f"{self.base_url}/api/v1/security/csrf_token/",
            timeout=15,
        )
        csrf_response.raise_for_status()
        csrf = csrf_response.json().get("result", "")
        session.headers.update({"X-CSRFToken": csrf})
        self._local.session = session
        return session

    def _reset_session(self):
        self._local.session = None

    def validate_database(self):
        session = self._ensure_session()
        response = session.get(
            f"{self.base_url}/api/v1/database/{self.database_id}",
            timeout=20,
        )
        response.raise_for_status()
        database_name = (response.json().get("result") or {}).get("database_name", "")
        if database_name != self.expected_database_name:
            raise SupersetError(
                f"DB{self.database_id} is {database_name!r}, expected {self.expected_database_name!r}"
            )
        return database_name

    def execute_sql(self, sql, limit=10000):
        safe_sql = validate_readonly_sql(sql)
        for attempt in range(3):
            session = self._ensure_session()
            try:
                response = session.post(
                    f"{self.base_url}/api/v1/sqllab/execute/",
                    json={
                        "database_id": self.database_id,
                        "sql": safe_sql,
                        "runAsync": False,
                        "queryLimit": int(limit),
                    },
                    headers={"Referer": f"{self.base_url}/sqllab"},
                    timeout=120,
                )
                if response.status_code == 401 and attempt < 2:
                    self._reset_session()
                    continue
                if response.status_code >= 400:
                    logger.error(
                        "Superset query failed with HTTP %s: %s",
                        response.status_code,
                        response.text[:500],
                    )
                response.raise_for_status()
                result = response.json()
                if result.get("errors"):
                    message = result["errors"][0].get("message", "unknown SQL error")
                    raise SupersetError(f"Superset SQL error: {message}")
                rows = result.get("data", [])
                if not isinstance(rows, list):
                    raise SupersetError("Superset returned a non-list data payload")
                return rows
            except (requests.ConnectionError, requests.ReadTimeout):
                if attempt == 2:
                    raise
                self._reset_session()
                time.sleep(2 * (attempt + 1))
        raise SupersetError("Superset SQL request failed after retries")


def validate_readonly_sql(sql):
    text = str(sql or "").strip().rstrip(";")
    if not text:
        raise ValueError("SQL is required")
    if ";" in text:
        raise ValueError("Multiple SQL statements are not allowed")
    if not _ALLOWED_START.search(text):
        raise ValueError("Only read-only SQL is allowed")
    if _FORBIDDEN_SQL.search(text):
        raise ValueError("SQL contains a forbidden keyword")
    return text
