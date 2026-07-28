"""Superset database client for iWeaver feedback system"""
import logging
import requests
from lib.config import Config

logger = logging.getLogger(__name__)


class SupersetClient:
    """Client for querying Superset database via API"""

    def __init__(self):
        self.base_url = Config.SUPERSET_URL
        self.username = Config.SUPERSET_USER
        self.password = Config.SUPERSET_PASS
        self.db_id = Config.SUPERSET_DB_ID
        self.session = requests.Session()
        self.session.trust_env = False
        self._login()

    def _login(self):
        """Login to Superset and get access token with CSRF token"""
        try:
            # Step 1: Login and get access token
            url = f"{self.base_url}/api/v1/security/login"
            data = {
                "username": self.username,
                "password": self.password,
                "provider": "db",
                "refresh": True,
            }
            resp = self.session.post(url, json=data, timeout=15)
            if resp.status_code >= 400:
                logger.error("Superset login failed: status=%s body=%s", resp.status_code, resp.text[:1000])
            resp.raise_for_status()
            token = resp.json().get("access_token")
            if token:
                self.session.headers["Authorization"] = f"Bearer {token}"
                logger.info("Superset login successful")

                # Step 2: Get CSRF token (same session, cookies auto-passed)
                csrf_url = f"{self.base_url}/api/v1/security/csrf_token/"
                resp = self.session.get(csrf_url, timeout=10)
                if resp.status_code >= 400:
                    logger.error("Superset CSRF failed: status=%s body=%s", resp.status_code, resp.text[:1000])
                if resp.status_code == 200:
                    csrf_token = resp.json().get("result", "")
                    if csrf_token:
                        self.session.headers["X-CSRFToken"] = csrf_token
                        logger.info("Superset CSRF token obtained")
            else:
                logger.error("Superset login failed: no token")
        except Exception as e:
            logger.error(f"Superset login error: {e}")

    def execute_sql(self, sql):
        """Execute SQL query and return results"""
        try:
            url = f"{self.base_url}/api/v1/sqllab/execute/"
            headers = {
                "Content-Type": "application/json",
                "Referer": f"{self.base_url}/sqllab"
            }
            data = {
                "database_id": self.db_id,
                "sql": sql,
                "schema": None,
                "tab": None,
                "tmp_table_name": None,
                "select_as_cta": False,
                "ctas_method": "TABLE",
                "queryLimit": 1000,
                "runAsync": False,
            }
            resp = self.session.post(url, headers=headers, json=data, timeout=30)
            if resp.status_code == 401:
                logger.debug("Superset token expired or unauthorized; refreshing login and retrying SQL")
                self._login()
                resp = self.session.post(url, headers=headers, json=data, timeout=30)
            if resp.status_code >= 400:
                logger.error("SQL execution failed: status=%s body=%s sql=%s", resp.status_code, resp.text[:2000], sql)
            resp.raise_for_status()
            result = resp.json()

            # Parse results
            if "results" in result:
                return result["results"]
            elif "data" in result:
                return result["data"]
            return []

        except Exception as e:
            logger.error(f"SQL execution error: {e}")
            return []

    def get_recent_feedback(self, minutes=10, limit=50):
        """Get recent feedback from attitude table
        attitude.user_id (varchar) → users.uuid (varchar)
        attitude.chat_logs_id (uuid) → chat_logs.id
        attitude.created_time (timestamp) — note: created_time not created_at
        chat_logs.message is jsonb; content may be absent for some rows
        """
        sql = f"""
            SELECT a.id, a.user_id, a.type, a.chat_logs_id, a.created_time,
                   COALESCE(
                       c.message ->> 'content',
                       c.message #>> '{{parts,0,text}}',
                       c.message #>> '{{data,0,data,result,output,output}}',
                       c.message #>> '{{data,result,output,output}}'
                   ) as feedback_content,
                   COALESCE(u_by_openid.email, u_by_email.email, u_by_uuid.email) as email,
                   COALESCE(u_by_openid.nickname, u_by_email.nickname, u_by_uuid.nickname, a.user_id) as user_name
            FROM attitude a
            LEFT JOIN chat_logs c ON a.chat_logs_id = c.id
            LEFT JOIN users u_by_openid ON a.user_id = u_by_openid.signin_openid
            LEFT JOIN users u_by_email ON a.user_id = u_by_email.email
            LEFT JOIN users u_by_uuid ON a.user_id = u_by_uuid.uuid
            WHERE a.created_time >= NOW() - ({int(minutes)} * INTERVAL '1 minute')
            ORDER BY a.created_time DESC
            LIMIT {int(limit)}
        """
        return self.execute_sql(sql)

    def get_recent_feedback_info(self, minutes=10, limit=50):
        """Get recent feedback from feedback_info table
        feedback_info.user_id (varchar) → users.uuid (varchar) or users.email
        feedback_info.message_id (varchar) → chat_logs.id (uuid, cast to text)
        feedback_info.type: thumbs_up / thumbs_down
        feedback_info.feedback_content: text (always NOT NULL)
        """
        sql = f"""
            SELECT f.id, f.user_id, f.type, f.feedback_content,
                   f.message_id, f.email, f.created_at,
                   COALESCE(u_by_openid.nickname, u_by_email.nickname, u_by_uuid.nickname, f.email, f.user_id) as user_name,
                   COALESCE(
                       c.message ->> 'content',
                       c.message #>> '{{parts,0,text}}',
                       c.message #>> '{{data,0,data,result,output,output}}',
                       c.message #>> '{{data,result,output,output}}'
                   ) as chat_content,
                   COALESCE(u_by_openid.email, u_by_email.email, u_by_uuid.email, f.email) as user_email
            FROM feedback_info f
            LEFT JOIN chat_logs c ON f.message_id = c.id::text
            LEFT JOIN users u_by_openid ON f.user_id = u_by_openid.signin_openid
            LEFT JOIN users u_by_email ON f.user_id = u_by_email.email
            LEFT JOIN users u_by_uuid ON f.user_id = u_by_uuid.uuid
            WHERE f.created_at >= NOW() - ({int(minutes)} * INTERVAL '1 minute')
            ORDER BY f.created_at DESC
            LIMIT {int(limit)}
        """
        return self.execute_sql(sql)
