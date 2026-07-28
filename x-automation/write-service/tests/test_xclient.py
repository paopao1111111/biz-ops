from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from x_write.credentials import AccountCredentials, OAuth2Credentials
from x_write.xclient import XAPIClient, XAPIError


CREDS = AccountCredentials("ck", "cs", "at", "ats")


class FakeXHandler(BaseHTTPRequestHandler):
    responses: dict = {}
    calls: list = []

    def _handle(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        type(self).calls.append((self.command, self.path, dict(self.headers), body))
        key = (self.command, self.path)
        status, payload = type(self).responses.get(key, (404, {"detail": "not found"}))
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if status == 429:
            self.send_header("x-rate-limit-reset", "2000000000")
        self.end_headers()
        self.wfile.write(raw)

    do_GET = _handle
    do_POST = _handle
    do_DELETE = _handle

    def log_message(self, *args):
        pass


class XClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeXHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeXHandler.calls = []
        FakeXHandler.responses = {
            ("GET", "/2/users/me"): (200, {"data": {"id": "1001", "username": "tester", "name": "T"}}),
            ("POST", "/2/users/1001/likes"): (200, {"data": {"liked": True}}),
            ("DELETE", "/2/users/1001/likes/55"): (200, {"data": {"liked": False}}),
            ("POST", "/2/users/1001/retweets"): (200, {"data": {"retweeted": True}}),
            ("DELETE", "/2/users/1001/retweets/55"): (200, {"data": {"retweeted": False}}),
            ("POST", "/2/tweets"): (200, {"data": {"id": "900", "text": "hello"}}),
            ("DELETE", "/2/tweets/900"): (200, {"data": {"deleted": True}}),
            ("POST", "/2/articles/draft"): (200, {"data": {"article_id": "a1"}}),
            ("POST", "/2/articles/a1/publish"): (200, {"data": {"post_id": "p9"}}),
        }

    def client(self):
        return XAPIClient(CREDS, base_url=self.base, timeout_seconds=5, require_https=False)

    def oauth2_credentials(self, *, expires_at=None, access_token="old-access", refresh_token="old-refresh"):
        return OAuth2Credentials(
            client_id="client-id",
            client_secret="client-secret",
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at if expires_at is not None else int(time.time()) - 1,
            scopes=("like.write", "offline.access", "tweet.read", "tweet.write", "users.read"),
            credential_ref="x-1001",
            developer_app="primary",
            generation=3,
            x_user_id="1001",
            username="tester",
        )

    @staticmethod
    def refreshed_credentials(old, *, access_token, refresh_token, expires_at, scopes):
        return OAuth2Credentials(
            client_id=old.client_id,
            client_secret=old.client_secret,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=tuple(scopes),
            credential_ref=old.credential_ref,
            developer_app=old.developer_app,
            generation=old.generation,
            x_user_id=old.x_user_id,
            username=old.username,
        )

    def test_base_url_must_be_https(self):
        with self.assertRaisesRegex(XAPIError, "HTTPS"):
            XAPIClient(CREDS, base_url="http://api.example.com")

    def test_endpoints_and_oauth_header(self):
        client = self.client()
        self.assertEqual("1001", client.verify_account()["id"])
        self.assertTrue(client.like_post("1001", "55")["liked"])
        self.assertFalse(client.unlike_post("1001", "55")["liked"])
        self.assertTrue(client.repost_post("1001", "55")["retweeted"])
        self.assertFalse(client.unrepost_post("1001", "55")["retweeted"])
        self.assertEqual("900", client.create_post("hello")["id"])
        self.assertTrue(client.delete_post("900")["deleted"])
        self.assertEqual("a1", client.create_article_draft({"title": "t", "content_state": {}})["article_id"])
        self.assertEqual("p9", client.publish_article("a1")["post_id"])
        methods = {(c[0], c[1]) for c in FakeXHandler.calls}
        self.assertIn(("POST", "/2/users/1001/likes"), methods)
        like_call = [c for c in FakeXHandler.calls if c[1] == "/2/users/1001/likes"][0]
        self.assertTrue(like_call[2].get("Authorization", "").startswith("OAuth "))
        self.assertEqual(b'{"tweet_id": "55"}', like_call[3])
        self.assertNotIn("ats", json.dumps(like_call[2]))

    def test_rate_limit_known_failure_with_retry_after(self):
        FakeXHandler.responses[("POST", "/2/users/1001/likes")] = (429, {"detail": "too many"})
        with self.assertRaises(XAPIError) as ctx:
            self.client().like_post("1001", "55")
        self.assertEqual("rate_limited", ctx.exception.code)
        self.assertEqual(2000000000, ctx.exception.retry_after)
        self.assertFalse(ctx.exception.outcome_uncertain)

    def test_server_error_on_write_is_uncertain(self):
        FakeXHandler.responses[("POST", "/2/tweets")] = (500, {"detail": "boom"})
        with self.assertRaises(XAPIError) as ctx:
            self.client().create_post("hello")
        self.assertTrue(ctx.exception.outcome_uncertain)

    def test_invalid_json_on_write_is_uncertain(self):
        FakeXHandler.responses[("POST", "/2/tweets")] = (200, b"not-json")
        with self.assertRaises(XAPIError) as ctx:
            self.client().create_post("hello")
        self.assertEqual("invalid_response", ctx.exception.code)
        self.assertTrue(ctx.exception.outcome_uncertain)

    def test_client_error_is_known_failure(self):
        FakeXHandler.responses[("POST", "/2/users/1001/likes")] = (403, {"detail": "forbidden"})
        with self.assertRaises(XAPIError) as ctx:
            self.client().like_post("1001", "55")
        self.assertEqual("http_error", ctx.exception.code)
        self.assertFalse(ctx.exception.outcome_uncertain)

    def test_missing_post_id_is_uncertain(self):
        FakeXHandler.responses[("POST", "/2/tweets")] = (200, {"data": {}})
        with self.assertRaises(XAPIError) as ctx:
            self.client().create_post("hello")
        self.assertTrue(ctx.exception.outcome_uncertain)

    def test_oauth2_refresh_rotates_tokens_before_api_request(self):
        old = self.oauth2_credentials()
        latest = {"value": old}
        persisted = []
        FakeXHandler.responses[("POST", "/2/oauth2/token")] = (200, {
            "token_type": "bearer",
            "expires_in": 7200,
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "scope": "tweet.read tweet.write users.read like.write offline.access",
        })

        def persist(ref, **values):
            self.assertEqual("x-1001", ref)
            self.assertEqual(3, values["expected_generation"])
            latest["value"] = self.refreshed_credentials(old, **{
                key: values[key] for key in ("access_token", "refresh_token", "expires_at", "scopes")
            })
            persisted.append(dict(values))
            return latest["value"]

        client = XAPIClient(
            old,
            base_url=self.base,
            token_url=self.base + "/2/oauth2/token",
            timeout_seconds=5,
            require_https=False,
            credential_reloader=lambda ref: latest["value"],
            token_persister=persist,
        )
        client.prepare_for_request()
        self.assertEqual("new-access", client.credentials.access_token)
        self.assertEqual("new-refresh", client.credentials.refresh_token)
        self.assertEqual(1, len(persisted))
        client.verify_account()

        token_call = [call for call in FakeXHandler.calls if call[1] == "/2/oauth2/token"][0]
        form = urllib.parse.parse_qs(token_call[3].decode("utf-8"))
        self.assertEqual(["refresh_token"], form["grant_type"])
        self.assertEqual(["old-refresh"], form["refresh_token"])
        self.assertTrue(token_call[2].get("Authorization", "").startswith("Basic "))
        verify_call = [call for call in FakeXHandler.calls if call[1] == "/2/users/me"][-1]
        self.assertEqual("Bearer new-access", verify_call[2].get("Authorization"))
        self.assertNotIn("new-refresh", json.dumps(verify_call[2]))

    def test_concurrent_oauth2_clients_refresh_same_ref_once(self):
        old = self.oauth2_credentials()
        latest = {"value": old}
        state_lock = threading.Lock()
        errors = []
        FakeXHandler.responses[("POST", "/2/oauth2/token")] = (200, {
            "token_type": "bearer",
            "expires_in": 7200,
            "access_token": "shared-access",
            "refresh_token": "shared-refresh",
            "scope": "tweet.read tweet.write users.read like.write offline.access",
        })

        def reload(_ref):
            with state_lock:
                return latest["value"]

        def persist(_ref, **values):
            with state_lock:
                current = latest["value"]
                if values["expected_generation"] != current.generation:
                    raise RuntimeError("generation changed")
                latest["value"] = self.refreshed_credentials(current, **{
                    key: values[key] for key in ("access_token", "refresh_token", "expires_at", "scopes")
                })
                return latest["value"]

        clients = [XAPIClient(
            old,
            base_url=self.base,
            token_url=self.base + "/2/oauth2/token",
            timeout_seconds=5,
            require_https=False,
            credential_reloader=reload,
            token_persister=persist,
        ) for _ in range(2)]
        barrier = threading.Barrier(3)

        def refresh(client):
            try:
                barrier.wait()
                client.prepare_for_request()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=refresh, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual([], errors)
        self.assertEqual(1, len([call for call in FakeXHandler.calls if call[1] == "/2/oauth2/token"]))
        self.assertTrue(all(client.credentials.access_token == "shared-access" for client in clients))

    def test_oauth2_refresh_rejects_scope_downgrade(self):
        old = self.oauth2_credentials()
        FakeXHandler.responses[("POST", "/2/oauth2/token")] = (200, {
            "token_type": "bearer",
            "expires_in": 7200,
            "access_token": "limited-access",
            "refresh_token": "limited-refresh",
            "scope": "tweet.read tweet.write users.read offline.access",
        })
        client = XAPIClient(
            old,
            base_url=self.base,
            token_url=self.base + "/2/oauth2/token",
            timeout_seconds=5,
            require_https=False,
            credential_reloader=lambda ref: old,
            token_persister=lambda *args, **kwargs: self.fail("scope downgrade must not persist"),
        )
        with self.assertRaises(XAPIError) as ctx:
            client.prepare_for_request()
        self.assertEqual("token_scope_insufficient", ctx.exception.code)
        self.assertEqual("old-access", client.credentials.access_token)

    def test_oauth2_invalid_grant_requires_reconnection(self):
        old = self.oauth2_credentials()
        FakeXHandler.responses[("POST", "/2/oauth2/token")] = (400, {
            "error": "invalid_grant",
            "error_description": "refresh token expired",
        })
        client = XAPIClient(
            old,
            base_url=self.base,
            token_url=self.base + "/2/oauth2/token",
            timeout_seconds=5,
            require_https=False,
            credential_reloader=lambda ref: old,
            token_persister=lambda *args, **kwargs: self.fail("invalid grant must not persist"),
        )
        with self.assertRaises(XAPIError) as ctx:
            client.prepare_for_request()
        self.assertEqual("token_refresh_reauth_required", ctx.exception.code)
        self.assertFalse(ctx.exception.outcome_uncertain)


if __name__ == "__main__":
    unittest.main()
