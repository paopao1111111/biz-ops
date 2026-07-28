import http.client
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory()
        base = Path(cls.tempdir.name)
        os.environ["METRICS_DB_PATH"] = str(base / "metrics.db")
        os.environ["AUTH_FILE"] = str(base / "auth.json")
        os.environ["SESSION_SECRET"] = "test-session-secret-which-is-longer-than-thirty-two-characters"
        os.environ["APP_TIMEZONE"] = "Asia/Shanghai"

        import auth
        auth.AUTH_FILE = Path(os.environ["AUTH_FILE"])
        auth.write_auth_config(auth.AUTH_FILE, "admin", "correct-password")

        import app
        cls.auth = auth
        cls.app = app
        app.DB_PATH = Path(os.environ["METRICS_DB_PATH"])
        app.initialize_database()
        connection = sqlite3.connect(app.DB_PATH)
        fields = (
            "week_start,week_end,window_start,window_end,window_kind,product,"
            "registration_exact,registration_attributed,activation_numerator,"
            "activation_denominator,user_turns,assistant_turns,active_users,topics,"
            "reports,data_complete,rule_version,collected_at,source_freshness"
        )
        rows = [
            ("2026-07-13", "2026-07-20", "2026-07-13 00:00:00", "2026-07-20 00:00:00", "full", "All", 2200, None, None, None, 100, 95, 80, 70, 0, 1, "2.0", "2026-07-20T10:00:00+08:00", "2026-07-20 09:59:00"),
            ("2026-07-13", "2026-07-20", "2026-07-13 00:00:00", "2026-07-20 00:00:00", "full", "iWeaver", 2198, None, 1646, 2198, 80, 75, 70, 60, 0, 1, "2.0", "2026-07-20T10:00:00+08:00", "2026-07-20 09:59:00"),
            ("2026-07-13", "2026-07-20", "2026-07-13 00:00:00", "2026-07-20 00:00:00", "full", "Palmly", None, 271, None, None, 15, 15, 13, 0, 15, 1, "2.0", "2026-07-20T10:00:00+08:00", "2026-07-20 09:59:00"),
            ("2026-07-13", "2026-07-20", "2026-07-13 00:00:00", "2026-07-20 00:00:00", "full", "LearningCoach", None, 1, 1, 1, 5, 5, 2, 2, 0, 1, "2.0", "2026-07-20T10:00:00+08:00", "2026-07-20 09:59:00"),
        ]
        connection.executemany(
            f"INSERT INTO weekly_product_metrics ({fields}) VALUES ({','.join('?' for _ in range(19))})",
            rows,
        )
        connection.execute(
            """INSERT INTO collector_runs
               (started_at,finished_at,status,weeks_requested,rows_written,source_freshness,rule_version,error_summary)
               VALUES ('2026-07-20T09:00:00+08:00','2026-07-20T10:00:00+08:00','success',12,48,'2026-07-20 09:59:00','2.0',NULL)"""
        )
        connection.commit()
        connection.close()

        cls.server = app.ThreadedHTTPServer(("127.0.0.1", 0), app.DashboardHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tempdir.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        connection.close()
        return result

    def test_auth_primitives(self):
        self.assertTrue(self.auth.authenticate("admin", "correct-password"))
        self.assertFalse(self.auth.authenticate("admin", "wrong-password"))
        token = self.auth.create_session_token("admin", now=1000)
        self.assertEqual(self.auth.validate_session_token(token, now=1001), "admin")
        self.assertIsNone(self.auth.validate_session_token(token + "x", now=1001))

    def test_static_route_and_redirect(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/login")
        status, _, payload = self.request("GET", "/static/app.css")
        self.assertEqual(status, 200)
        self.assertIn(b"--blue", payload)

    def test_login_cookie_and_authenticated_api(self):
        body = json.dumps({"username": "admin", "password": "correct-password"})
        status, headers, payload = self.request(
            "POST", "/login", body, {"Content-Type": "application/json"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload), {"ok": True})
        cookie = headers.get("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)

        status, _, payload = self.request(
            "GET", "/api/overview?week=2026-07-13", headers={"Cookie": cookie.split(";", 1)[0]}
        )
        self.assertEqual(status, 200)
        overview = json.loads(payload)
        self.assertTrue(overview["available"])
        self.assertEqual(overview["products"]["Palmly"]["registration_exact"], None)
        self.assertEqual(overview["products"]["iWeaver"]["activation_rate"], 74.9)

    def test_unauthenticated_api_is_denied(self):
        status, _, payload = self.request("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(payload)["error"], "unauthorized")


class MetricSQLTests(unittest.TestCase):
    def test_verified_join_and_product_scopes(self):
        from metrics import registration_sql, usage_sql

        usage = usage_sql("2026-05-01 00:00:00", "2026-07-20 00:00:00")
        registration = registration_sql(
            "2026-05-01 00:00:00",
            "2026-07-20 00:00:00",
            "2026-07-20 00:00:00",
        )
        self.assertIn("c.user_id = u.uuid", registration)
        self.assertNotIn("signin_openid", registration)
        self.assertIn("lunara_reports", usage)
        self.assertNotIn("lunara-palm", usage)
        self.assertIn("ILIKE '%learning-coach%'", usage)


if __name__ == "__main__":
    unittest.main()
