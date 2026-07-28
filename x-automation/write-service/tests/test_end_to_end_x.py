from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from x_write.credentials import CredentialStore
from x_write.db import Database
from x_write.executor import Executor
from x_write.repository import Repository
from x_write.xclient import XAPIClient


class FakeXHandler(BaseHTTPRequestHandler):
    calls: list = []
    failures: dict = {}

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        type(self).calls.append((self.command, self.path, dict(self.headers), body))
        key = (self.command, self.path)
        failure = type(self).failures.get(key)
        if failure:
            status, payload = failure
        else:
            routes = {
                ("GET", "/2/users/me"): (200, {"data": {"id": "1001", "username": "gray", "name": "G"}}),
                ("POST", "/2/users/1001/likes"): (200, {"data": {"liked": True}}),
                ("POST", "/2/tweets"): (200, {"data": {"id": "900", "text": "hello"}}),
                ("POST", "/2/articles/draft"): (200, {"data": {"article_id": "art-1"}}),
                ("POST", "/2/articles/art-1/publish"): (200, {"data": {"post_id": "post-1"}}),
            }
            status, payload = routes.get(key, (404, {"detail": "unknown"}))
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle

    def log_message(self, *args):
        pass


class EndToEndXTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.x_server = HTTPServer(("127.0.0.1", 0), FakeXHandler)
        cls.x_thread = threading.Thread(target=cls.x_server.serve_forever, daemon=True)
        cls.x_thread.start()
        cls.x_base = f"http://127.0.0.1:{cls.x_server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.x_server.shutdown()
        cls.x_server.server_close()

    def setUp(self):
        FakeXHandler.calls = []
        FakeXHandler.failures = {}
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tempdir.name, "write.db"))
        self.db.migrate()
        self.repo = Repository(self.db)
        secrets_path = os.path.join(self.tempdir.name, "secrets.json")
        with open(secrets_path, "w", encoding="utf-8") as fh:
            json.dump({
                "developer_apps": {"primary": {"consumer_key": "ck", "consumer_secret": "cs"}},
                "accounts": {"gray": {"developer_app": "primary", "access_token": "at", "access_token_secret": "ats"}},
            }, fh)
        os.chmod(secrets_path, 0o600)
        self.executor = Executor(
            self.repo, CredentialStore(secrets_path),
            client_factory=lambda creds: XAPIClient(creds, base_url=self.x_base, require_https=False),
            tick_seconds=1)
        account = self.repo.create_account("gray", "Gray", "tester", x_user_id="1001")
        self.account = self.repo.set_account_paused(
            self.repo.set_account_enabled(account["id"], True, "tester")["id"], False, "tester")

    def tearDown(self):
        self.tempdir.cleanup()

    def approved(self, request_type, payload):
        request = self.repo.create_request(self.account["id"], request_type, payload, "tester")
        self.repo.submit_request(request["id"], "tester")
        self.repo.approve_request(request["id"], "tester", {
            "content_hash": request["content_hash"], "request_version": request["version"]})
        return request["id"]

    def test_full_chain_like_sends_signed_request_once(self):
        request_id = self.approved("like", {"target": "https://x.com/a/status/55"})
        self.repo.set_global_pause(False, "tester")
        result = self.executor.tick()
        self.assertEqual("succeeded", result["status"])
        like_calls = [c for c in FakeXHandler.calls if c[1] == "/2/users/1001/likes"]
        self.assertEqual(1, len(like_calls))
        self.assertTrue(like_calls[0][2].get("Authorization", "").startswith("OAuth "))
        self.assertNotIn("ats", json.dumps(like_calls[0][2]))
        self.assertEqual("succeeded", self.repo.get_request(request_id)["status"])
        events = {row["event_type"] for row in self.repo.list_audit(100)}
        self.assertIn("operation.claim", events)
        self.assertIn("operation.step.succeeded", events)

    def test_server_error_marks_uncertain_without_replay(self):
        FakeXHandler.failures[("POST", "/2/tweets")] = (500, {"detail": "boom"})
        request_id = self.approved("post_create", {"text": "hello"})
        self.repo.set_global_pause(False, "tester")
        result = self.executor.tick()
        self.assertEqual("uncertain", result["status"])
        calls_before = [c for c in FakeXHandler.calls if c[1] == "/2/tweets"]
        self.assertEqual(1, len(calls_before))
        self.assertIsNone(self.executor.tick())
        self.assertEqual(1, len([c for c in FakeXHandler.calls if c[1] == "/2/tweets"]))
        reconciled = self.repo.reconcile_operation(result["id"], "succeeded", "confirmed manually", "tester")
        self.assertEqual("reconciled_succeeded", reconciled["status"])
        self.assertEqual("succeeded", self.repo.get_request(request_id)["status"])

    def test_article_two_approvals_two_posts(self):
        self.approved("article_draft_publish", {
            "article": {"schema_version": 1, "title": "T", "blocks": [{"type": "paragraph", "text": "body"}]},
        })
        self.repo.set_global_pause(False, "tester")
        first = self.executor.tick()
        self.assertEqual("awaiting_approval", first["status"])
        self.assertEqual("art-1", first["external_object_id"])
        self.assertEqual([], [c for c in FakeXHandler.calls if "publish" in c[1]])
        request = self.repo.get_request(first["request_id"])
        self.repo.approve_next_step(first["id"], "tester", {"content_hash": request["content_hash"]})
        final = self.executor.tick()
        self.assertEqual("succeeded", final["status"])
        paths = [c[1] for c in FakeXHandler.calls if "articles" in c[1]]
        self.assertEqual(["/2/articles/draft", "/2/articles/art-1/publish"], paths)

    def test_write_paused_sends_nothing(self):
        self.approved("like", {"target": "55"})
        self.assertIsNone(self.executor.tick())
        self.assertEqual([], FakeXHandler.calls)


if __name__ == "__main__":
    unittest.main()
