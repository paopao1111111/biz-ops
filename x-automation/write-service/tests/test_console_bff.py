from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONTROLLER_APP = Path("/private/tmp/x-browse-v2-staging/controller/app.py")
STAGING_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STAGING_ROOT))

from x_write.auth import HMACAuthenticator  # noqa: E402
from x_write.config import Config  # noqa: E402
from x_write.credentials import CredentialStore  # noqa: E402
from x_write.db import Database  # noqa: E402
from x_write.executor import Executor  # noqa: E402
from x_write.http_service import build_server  # noqa: E402
from x_write.repository import Repository  # noqa: E402

XWRITE_SECRET = "internal-secret-" + "x" * 32


class VerifyClient:
    def __init__(self, credentials):
        self.credentials = credentials

    def verify_account(self):
        return {"id": "1001", "username": "gray", "name": "Gray"}


class ConsoleBFFTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="x-bff-", dir="/private/tmp")

        # Real x-write service (executor disabled: nothing can be sent).
        cls.write_db = Database(os.path.join(cls.tmp, "write.db"))
        cls.write_db.migrate()
        write_repo = Repository(cls.write_db)
        secrets_path = os.path.join(cls.tmp, "credentials.json")
        with open(secrets_path, "w", encoding="utf-8") as fh:
            json.dump({
                "developer_apps": {"primary": {"consumer_key": "ck", "consumer_secret": "cs"}},
                "accounts": {
                    "gray": {
                        "developer_app": "primary",
                        "access_token": "at",
                        "access_token_secret": "ats",
                    },
                },
            }, fh)
        os.chmod(secrets_path, 0o600)
        store = CredentialStore(secrets_path)
        write_config = Config.from_mapping({
            "database_path": cls.write_db.path,
            "hmac_secret": XWRITE_SECRET,
            "bind_port": 0,
            "executor_enabled": False,
            "secrets_path": secrets_path,
        })
        executor = Executor(
            write_repo, store, client_factory=lambda credentials: VerifyClient(credentials),
            executor_enabled=False,
        )
        cls.write_server = build_server(
            write_config, write_repo, HMACAuthenticator(cls.write_db, XWRITE_SECRET),
            executor=executor, credential_store=store,
        )
        cls.write_thread = threading.Thread(target=cls.write_server.serve_forever, daemon=True)
        cls.write_thread.start()
        write_url = f"http://127.0.0.1:{cls.write_server.server_address[1]}"

        # Real console controller with BFF enabled.
        os.environ.update(
            X_CONSOLE_DB=os.path.join(cls.tmp, "console.db"),
            X_CONSOLE_HOST="127.0.0.1",
            X_CONSOLE_PORT="0",
            X_CONSOLE_ADMIN_PASSWORD="fixture-admin-only",
            X_CONSOLE_SESSION_SECRET="fixture-session-only",
            X_CONSOLE_WORKER_SECRET="fixture-worker-only",
            X_CONSOLE_XWRITE_URL=write_url,
            X_CONSOLE_XWRITE_SECRET=XWRITE_SECRET,
        )
        spec = importlib.util.spec_from_file_location("controller_bff_verification", CONTROLLER_APP)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.app.init()
        cls.server = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.H)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        jar = http.cookiejar.CookieJar()
        cls.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        body = urllib.parse.urlencode({"password": "fixture-admin-only"}).encode()
        req = urllib.request.Request(cls.base + "/login", data=body,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        cls.opener.open(req).read()
        index = cls.opener.open(cls.base + "/").read().decode()
        cls.csrf = index.split('<meta name="csrf-token" content="', 1)[1].split('"', 1)[0]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.write_server.shutdown()
        cls.write_server.server_close()
        cls.write_thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        for key in ("X_CONSOLE_XWRITE_URL", "X_CONSOLE_XWRITE_SECRET"):
            os.environ.pop(key, None)

    def admin(self, method, path, data=None, csrf=True):
        body = None if method == "GET" else json.dumps(data or {}, separators=(",", ":")).encode()
        headers = {"Accept": "application/json"}
        if method != "GET":
            headers["Content-Type"] = "application/json"
            if csrf:
                headers["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req) as response:
                return response.getcode(), json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_status_and_account_flow_through_console(self):
        status, payload = self.admin("GET", "/api/x-write/status")
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["data"]["global_write_paused"])
        self.assertFalse(payload["data"]["write_execution_available"])

        status, payload = self.admin("POST", "/api/x-write/accounts", {
            "account_key": "gray", "display_name": "Gray", "x_user_id": "1001"})
        self.assertEqual(200, status)
        account = payload["data"]
        self.assertFalse(account["enabled"])
        self.assertTrue(account["paused"])
        self.assertEqual("1001", account["x_user_id"])
        self.assertNotIn("access_token", json.dumps(payload))

        account_id = account["id"]
        status, payload = self.admin("POST", f"/api/x-write/accounts/{account_id}/verify")
        self.assertEqual(200, status)
        self.assertEqual("verified", payload["data"]["authorization_status"])
        status, payload = self.admin("POST", f"/api/x-write/accounts/{account_id}/enable")
        self.assertEqual(200, status)
        status, payload = self.admin("POST", f"/api/x-write/accounts/{account_id}/resume")
        self.assertEqual(200, status)

        status, payload = self.admin("POST", "/api/x-write/requests", {
            "account_id": account_id, "request_type": "like",
            "payload": {"target": "https://x.com/someone/status/55"}})
        self.assertEqual(200, status)
        request = payload["data"]
        self.assertEqual("draft", request["status"])
        self.assertEqual("55", request["payload"]["target"])

        status, payload = self.admin("POST", f"/api/x-write/requests/{request['id']}/submit")
        self.assertEqual(200, status)
        self.assertEqual("pending_approval", payload["data"]["status"])

        status, payload = self.admin("POST", f"/api/x-write/requests/{request['id']}/approve", {
            "content_hash": "0" * 64, "request_version": 1})
        self.assertEqual(409, status)
        self.assertEqual("approval_hash_mismatch", payload["error"]["code"])

        status, payload = self.admin("POST", f"/api/x-write/requests/{request['id']}/approve", {
            "content_hash": request["content_hash"], "request_version": request["version"]})
        self.assertEqual(200, status)
        self.assertEqual("queued", payload["data"]["status"])
        self.assertEqual(1, len(payload["data"]["operations"]))
        operation = payload["data"]["operations"][0]
        self.assertEqual("queued", operation["status"])

        status, payload = self.admin("GET", f"/api/x-write/operations/{operation['id']}")
        self.assertEqual(200, status)
        self.assertEqual("like", payload["data"]["operation_type"])

        status, payload = self.admin("GET", "/api/x-write/audit?limit=50")
        self.assertEqual(200, status)
        events = {row["event_type"] for row in payload["data"]["audit"]}
        self.assertIn("request.approve", events)
        self.assertTrue(all(row["actor"] for row in payload["data"]["audit"]))

        # Nothing may execute while the global write pause is set.
        self.assertEqual("queued", operation["status"])

    def test_console_auth_and_csrf_enforced(self):
        req = urllib.request.Request(self.base + "/api/x-write/status", headers={"Accept": "application/json"})
        plain = urllib.request.build_opener()
        try:
            plain.open(req)
            self.fail("expected 401 without session")
        except urllib.error.HTTPError as exc:
            self.assertEqual(401, exc.code)
        status, payload = self.admin("POST", "/api/x-write/global/resume", {}, csrf=False)
        self.assertEqual(403, status)

    def test_write_service_stays_paused_and_browse_unaffected(self):
        status, payload = self.admin("GET", "/api/x/overview")
        self.assertEqual(200, status)
        status, payload = self.admin("GET", "/api/x-write/status")
        self.assertTrue(payload["data"]["global_write_paused"])


if __name__ == "__main__":
    unittest.main()
