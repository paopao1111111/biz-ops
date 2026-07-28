import os
import re
import threading
import time

import requests


_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|call|copy|execute)\b",
    re.IGNORECASE,
)
_ALLOWED_START = re.compile(r"^\s*(select|with|show|describe|desc)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\s+\d+\b", re.IGNORECASE)


class SupersetClient:
    def __init__(self):
        self._local = threading.local()

    @property
    def base_url(self):
        return os.getenv('SUPERSET_URL', 'http://galaxy.iweaver.ai').rstrip('/')

    @property
    def user(self):
        return os.getenv('SUPERSET_USER', 'admin')

    @property
    def password(self):
        return os.getenv('SUPERSET_PASS', '')

    @property
    def database_id(self):
        return int(os.getenv('SUPERSET_DB_ID', '1'))

    def ensure_configured(self):
        if not self.password:
            raise RuntimeError('SUPERSET_PASS is not configured')

    def _ensure_session(self):
        session = getattr(self._local, 'session', None)
        if session is not None:
            return
        self.ensure_configured()
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            f'{self.base_url}/api/v1/security/login',
            json={'username': self.user, 'password': self.password, 'provider': 'db'},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()['access_token']
        session.headers.update({'Authorization': f'Bearer {token}'})
        csrf_resp = session.get(f'{self.base_url}/api/v1/security/csrf_token/', timeout=10)
        csrf_resp.raise_for_status()
        csrf = csrf_resp.json().get('result', '')
        session.headers.update({'X-CSRFToken': csrf})
        self._local.session = session

    def _reset_session(self):
        self._local.session = None

    def execute_sql(self, sql, limit=1000, enforce_limit=False):
        safe_sql = validate_readonly_sql(sql, enforce_limit=enforce_limit)
        self._ensure_session()
        for attempt in range(2):
            resp = self._local.session.post(
                f'{self.base_url}/api/v1/sqllab/execute/',
                json={
                    'database_id': self.database_id,
                    'sql': safe_sql,
                    'runAsync': False,
                    'queryLimit': int(limit),
                },
                headers={'Referer': f'{self.base_url}/sqllab'},
                timeout=120,
            )
            if resp.status_code == 401 and attempt == 0:
                self._reset_session()
                self._ensure_session()
                continue
            resp.raise_for_status()
            result = resp.json()
            if result.get('errors'):
                message = result['errors'][0].get('message', '')
                raise RuntimeError(f'Superset SQL error: {message}')
            columns = [c.get('column_name') or c.get('name', '') for c in result.get('columns', [])]
            return result.get('data', []), columns
        raise RuntimeError('Superset SQL request failed after retry')


def validate_readonly_sql(sql, enforce_limit=False):
    text = str(sql or '').strip().rstrip(';')
    if not text:
        raise ValueError('sql is required')
    if ';' in text:
        raise ValueError('multiple SQL statements are not allowed')
    if not _ALLOWED_START.search(text):
        raise ValueError('only read-only SQL is allowed')
    if _FORBIDDEN_SQL.search(text):
        raise ValueError('SQL contains a forbidden keyword')
    if enforce_limit and not _LIMIT_RE.search(text) and not _looks_aggregate_only(text):
        text = f'{text}\nLIMIT 200'
    return text


def _looks_aggregate_only(sql):
    lowered = sql.lower()
    return 'count(' in lowered or 'min(' in lowered or 'max(' in lowered or 'sum(' in lowered


def run_query(sql, limit=1000, enforce_limit=False):
    client = SupersetClient()
    for attempt in range(3):
        try:
            return client.execute_sql(sql, limit=limit, enforce_limit=enforce_limit)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout):
            if attempt == 2:
                raise
            client._reset_session()
            time.sleep(2 * (attempt + 1))
