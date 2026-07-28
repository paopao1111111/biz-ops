from __future__ import annotations

import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CONTROLLER_APP = Path("/private/tmp/x-browse-v2-staging/controller/app.py")


class FakeWriteHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    response_status = 200
    response_payload: dict = {"status": "succeeded"}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).calls.append({
            "method": self.command,
            "path": self.path,
            "headers": dict(self.headers),
            "body": body,
        })
        raw = json.dumps(type(self).response_payload, separators=(",", ":")).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        return


class CaptureLog:
    def __init__(self):
        self.entries: list[str] = []

    def info(self, message, *args):
        self.entries.append(message % args if args else str(message))

    def exception(self, message, *args):
        self.entries.append(message % args if args else str(message))


class OAuthBFFTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="x-oauth-bff-", dir="/private/tmp")
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeWriteHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}"

        cls.env_keys = {
            "X_CONSOLE_DB": os.path.join(cls.tmp, "console.db"),
            "X_CONSOLE_HOST": "127.0.0.1",
            "X_CONSOLE_PORT": "0",
            "X_CONSOLE_ADMIN_PASSWORD": "fixture-admin-only",
            "X_CONSOLE_SESSION_SECRET": "fixture-session-only",
            "X_CONSOLE_WORKER_SECRET": "fixture-worker-only",
            "X_CONSOLE_XWRITE_URL": upstream_url,
            "X_CONSOLE_XWRITE_SECRET": "internal-secret-" + "x" * 32,
            "X_CONSOLE_SECURE_COOKIE": "1",
            "X_CONSOLE_LOGIN_RATE_LIMIT": "10",
            "X_CONSOLE_OAUTH_RATE_LIMIT": "60",
        }
        cls.old_env = {key: os.environ.get(key) for key in cls.env_keys}
        os.environ.update(cls.env_keys)
        spec = importlib.util.spec_from_file_location("controller_oauth_bff_verification", CONTROLLER_APP)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.app.init()
        cls.server = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.H)
        cls.base_port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.original_log = cls.app.LOG

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join(timeout=5)
        cls.app.LOG = cls.original_log
        for key, value in cls.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        FakeWriteHandler.calls = []
        FakeWriteHandler.response_status = 200
        FakeWriteHandler.response_payload = {"status": "succeeded"}
        self.app.RATE_BUCKETS.clear()
        self.app.LOGIN_RATE_LIMIT = 10
        self.app.OAUTH_RATE_LIMIT = 60
        self.app.LOG = self.original_log

    def request(self, method: str, path: str, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.base_port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        payload = response.read()
        result = response.status, dict(response.getheaders()), payload
        conn.close()
        return result

    @staticmethod
    def callback_path(state: str, code: str = "authorization-code") -> str:
        return "/oauth/x/callback?" + urllib.parse.urlencode({"state": state, "code": code})

    def test_public_callback_forwards_code_once_without_echo_or_cache(self):
        state = "s" * 32
        code = "authorization-code-sensitive"
        status, headers, body = self.request("GET", self.callback_path(state, code))
        text = body.decode("utf-8")
        self.assertEqual(200, status)
        self.assertIn("授权已接收", text)
        self.assertNotIn(state, text)
        self.assertNotIn(code, text)
        self.assertEqual("no-store", headers.get("Cache-Control"))
        self.assertEqual("no-referrer", headers.get("Referrer-Policy"))
        self.assertEqual(1, len(FakeWriteHandler.calls))
        call = FakeWriteHandler.calls[0]
        self.assertEqual("/api/oauth/callback", call["path"])
        forwarded = json.loads(call["body"])
        self.assertEqual(state, forwarded["state"])
        self.assertEqual(code, forwarded["code"])
        self.assertEqual("oauth-callback", forwarded["actor"])
        self.assertIn("X-Internal-Signature", call["headers"])
        self.assertNotIn(code, call["path"])

    def test_callback_rejects_duplicate_unknown_and_ambiguous_parameters(self):
        state = "s" * 32
        cases = (
            f"/oauth/x/callback?state={state}&state={state}&code=one",
            f"/oauth/x/callback?state={state}&code=one&unexpected=1",
            f"/oauth/x/callback?state={state}&code=one&error=access_denied",
            f"/oauth/x/callback?state={state}",
        )
        for path in cases:
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(400, status)
                self.assertIn("授权未完成", body.decode("utf-8"))
        self.assertEqual([], FakeWriteHandler.calls)

    def test_callback_maps_upstream_replay_to_generic_failure(self):
        state = "r" * 32
        code = "one-time-code-sensitive"
        FakeWriteHandler.response_status = 409
        FakeWriteHandler.response_payload = {
            "error": "oauth_state_replayed",
            "message": "state already consumed",
        }
        status, _, body = self.request("GET", self.callback_path(state, code))
        text = body.decode("utf-8")
        self.assertEqual(400, status)
        self.assertIn("oauth_state_replayed", text)
        self.assertNotIn(state, text)
        self.assertNotIn(code, text)
        self.assertNotIn("state already consumed", text)
        self.assertEqual(1, len(FakeWriteHandler.calls))

    def test_callback_request_log_redacts_query_values(self):
        capture = CaptureLog()
        self.app.LOG = capture
        state = "l" * 32
        code = "log-secret-code"
        try:
            status, _, _ = self.request("GET", self.callback_path(state, code))
        finally:
            self.app.LOG = self.original_log
        self.assertEqual(200, status)
        joined = "\n".join(capture.entries)
        self.assertIn("OAuth callback request received", joined)
        self.assertNotIn(state, joined)
        self.assertNotIn(code, joined)

    def test_callback_rate_limit_blocks_upstream_amplification(self):
        self.app.OAUTH_RATE_LIMIT = 1
        first = self.request("GET", self.callback_path("a" * 32, "first-code"))
        second = self.request("GET", self.callback_path("b" * 32, "second-code"))
        self.assertEqual(200, first[0])
        self.assertEqual(429, second[0])
        self.assertEqual(1, len(FakeWriteHandler.calls))
        self.assertNotIn("second-code", second[2].decode("utf-8"))

    def test_secure_session_cookie_and_encoded_path_rejection(self):
        form = urllib.parse.urlencode({"password": "fixture-admin-only"}).encode("utf-8")
        status, headers, _ = self.request("POST", "/login", form, {
            "Content-Type": "application/x-www-form-urlencoded",
        })
        self.assertEqual(303, status)
        cookie = headers["Set-Cookie"]
        self.assertIn("Secure", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertEqual("max-age=31536000", headers.get("Strict-Transport-Security"))
        session_cookie = cookie.split(";", 1)[0]
        status, _, body = self.request("GET", "/api/x-write/%2e%2e/status", headers={
            "Cookie": session_cookie,
        })
        self.assertEqual(404, status)
        self.assertEqual("not_found", json.loads(body)["error"]["code"])
        self.assertEqual([], FakeWriteHandler.calls)

    def test_login_failures_are_rate_limited(self):
        self.app.LOGIN_RATE_LIMIT = 1
        form = urllib.parse.urlencode({"password": "wrong"}).encode("utf-8")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        first = self.request("POST", "/login", form, headers)
        second = self.request("POST", "/login", form, headers)
        self.assertEqual(200, first[0])
        self.assertEqual(429, second[0])
        self.assertIn("登录尝试过多", second[2].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
