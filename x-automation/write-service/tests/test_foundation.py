from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import uuid

from x_write.auth import AuthHeaders, AuthenticationError, HMACAuthenticator, sign
from x_write.config import Config
from x_write.db import Database
from x_write.http_service import build_server
from x_write.repository import Repository


SECRET = "s" * 40


class FoundationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "service.sqlite3")
        self.db = Database(self.db_path)
        self.db.migrate()
        self.repo = Repository(self.db)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_migrations_schema_wal_and_default_pause(self) -> None:
        expected = {
            "write_accounts", "write_requests", "write_approvals", "write_operations",
            "write_operation_steps", "media_assets", "quota_policies", "quota_usage",
            "write_audit_log", "write_settings", "used_nonces",
        }
        with self.db.connect() as conn:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue(expected <= tables)
            self.assertEqual("wal", conn.execute("PRAGMA journal_mode").fetchone()[0].lower())
            self.assertEqual(1, conn.execute("PRAGMA foreign_keys").fetchone()[0])
        self.assertTrue(self.repo.status()["global_write_paused"])

    def test_hmac_timestamp_replay_and_body_binding(self) -> None:
        auth = HMACAuthenticator(self.db, SECRET, max_skew_seconds=30, nonce_ttl_seconds=60)
        now = int(time.time())
        body = b'{"actor":"test"}'
        timestamp = str(now)
        signature = sign(SECRET, timestamp, "nonce-1", "POST", "/api/global/pause", body)
        headers = AuthHeaders(timestamp, "nonce-1", signature)
        auth.verify(headers, "POST", "/api/global/pause", body, now)
        with self.assertRaisesRegex(AuthenticationError, "replayed"):
            auth.verify(headers, "POST", "/api/global/pause", body, now)
        old = str(now - 31)
        stale = AuthHeaders(old, "nonce-2", sign(SECRET, old, "nonce-2", "GET", "/api/status", b""))
        with self.assertRaisesRegex(AuthenticationError, "timestamp"):
            auth.verify(stale, "GET", "/api/status", b"", now)
        altered = AuthHeaders(timestamp, "nonce-3", sign(SECRET, timestamp, "nonce-3", "POST", "/x", b"a"))
        with self.assertRaisesRegex(AuthenticationError, "signature"):
            auth.verify(altered, "POST", "/x", b"b", now)

    def test_transactional_audit_rolls_back_mutation_if_audit_fails(self) -> None:
        original = self.repo._audit

        def fail(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        self.repo._audit = fail
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            self.repo.set_global_pause(False, "tester")
        self.assertTrue(self.repo.status()["global_write_paused"])
        self.repo._audit = original
        with self.db.connect() as conn:
            self.assertEqual(0, conn.execute("SELECT COUNT(*) FROM write_audit_log").fetchone()[0])

    def test_account_state_defaults_and_audited_transitions(self) -> None:
        account = self.repo.create_account("primary", "Primary", "tester", metadata={"region": "us"})
        self.assertFalse(account["enabled"])
        self.assertTrue(account["paused"])
        account = self.repo.set_account_enabled(account["id"], True, "tester")
        account = self.repo.set_account_paused(account["id"], False, "tester")
        account = self.repo.update_account_metadata(account["id"], "tester", display_name="Updated")
        self.assertTrue(account["enabled"])
        self.assertFalse(account["paused"])
        self.assertEqual("Updated", account["display_name"])
        with self.db.connect() as conn:
            self.assertEqual(4, conn.execute("SELECT COUNT(*) FROM write_audit_log").fetchone()[0])


class HTTPTestCase(FoundationTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config = Config.from_mapping({
            "database_path": self.db_path,
            "hmac_secret": SECRET,
            "bind_host": "127.0.0.1",
            "bind_port": 0,
            "max_body_bytes": 256,
        })
        auth = HMACAuthenticator(self.db, SECRET)
        self.server = build_server(self.config, self.repo, auth)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        super().tearDown()

    def request(self, method: str, path: str, payload=None, signed=True, extra_headers=None):
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        headers = {} if extra_headers is None else dict(extra_headers)
        if signed:
            timestamp = str(int(time.time()))
            nonce = uuid.uuid4().hex
            headers.update({
                "X-Internal-Timestamp": timestamp,
                "X-Internal-Nonce": nonce,
                "X-Internal-Signature": sign(SECRET, timestamp, nonce, method, path, body),
            })
        if body:
            headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read()
        result = response.status, dict(response.getheaders()), json.loads(raw)
        conn.close()
        return result

    def test_http_auth_validation_headers_and_no_secret_exposure(self) -> None:
        status, headers, health = self.request("GET", "/health", signed=False)
        self.assertEqual(200, status)
        self.assertEqual("x_write", health["service"])
        self.assertIn("version", health)
        self.assertEqual("no-store", headers["Cache-Control"])
        self.assertEqual("nosniff", headers["X-Content-Type-Options"])

        status, _, payload = self.request("GET", "/api/status", signed=False)
        self.assertEqual(401, status)
        self.assertEqual("unauthorized", payload["error"])

        status, _, payload = self.request("POST", "/api/global/resume", {"actor": "test", "extra": True})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])
        self.assertTrue(self.repo.status()["global_write_paused"])

        status, _, metadata = self.request("GET", "/api/config")
        self.assertEqual(200, status)
        self.assertTrue(metadata["hmac_configured"])
        self.assertNotIn("hmac_secret", metadata)
        self.assertNotIn(SECRET, json.dumps(metadata))

    def test_http_account_create_is_disabled_and_paused(self) -> None:
        status, _, account = self.request("POST", "/api/accounts", {
            "account_key": "web", "display_name": "Web", "actor": "test", "metadata": {"team": "ops"}
        })
        self.assertEqual(201, status)
        self.assertFalse(account["enabled"])
        self.assertTrue(account["paused"])

    def test_http_replay_is_rejected(self) -> None:
        path = "/api/status"
        timestamp = str(int(time.time()))
        nonce = uuid.uuid4().hex
        headers = {
            "X-Internal-Timestamp": timestamp,
            "X-Internal-Nonce": nonce,
            "X-Internal-Signature": sign(SECRET, timestamp, nonce, "GET", path, b""),
        }
        first = self.request("GET", path, signed=False, extra_headers=headers)
        second = self.request("GET", path, signed=False, extra_headers=headers)
        self.assertEqual(200, first[0])
        self.assertEqual(401, second[0])


class CredentialRefsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_secrets(self, mode=0o600):
        path = os.path.join(self.tempdir.name, "secrets.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({
                "developer_apps": {"primary": {"consumer_key": "ck", "consumer_secret": "cs"}},
                "accounts": {
                    "x10": {"developer_app": "primary", "access_token": "at", "access_token_secret": "ats"},
                    "x11": {"developer_app": "missing", "access_token": "at", "access_token_secret": "ats"},
                },
            }, fh)
        os.chmod(path, mode)
        return path

    def test_list_refs_never_exposes_secret_values(self):
        from x_write.credentials import CredentialStore
        store = CredentialStore(self.write_secrets())
        refs = store.list_refs()
        self.assertEqual(2, len(refs))
        self.assertEqual({
            "ref", "auth_type", "developer_app", "complete", "status", "scopes",
            "expires_at", "x_user_id", "username", "generation", "updated_at",
        }, set(refs[0]))
        self.assertTrue([r for r in refs if r["ref"] == "x10"][0]["complete"])
        self.assertFalse([r for r in refs if r["ref"] == "x11"][0]["complete"])
        self.assertNotIn("ats", json.dumps(refs))
        self.assertNotIn("cs", json.dumps(refs))

    def test_list_refs_missing_or_insecure_or_invalid(self):
        from x_write.credentials import CredentialStore
        self.assertEqual([], CredentialStore(None).list_refs())
        self.assertEqual([], CredentialStore(os.path.join(self.tempdir.name, "absent.json")).list_refs())
        self.assertEqual([], CredentialStore(self.write_secrets(0o644)).list_refs())
        bad = os.path.join(self.tempdir.name, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{not json")
        os.chmod(bad, 0o600)
        self.assertEqual([], CredentialStore(bad).list_refs())


if __name__ == "__main__":
    unittest.main()
