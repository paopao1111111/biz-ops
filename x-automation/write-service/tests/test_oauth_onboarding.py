from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import unittest

import x_write.oauth as oauth_module
from x_write.config import Config
from x_write.credentials import AccountCredentials, CredentialStore, OAuth2Credentials
from x_write.db import Database
from x_write.oauth import DEFAULT_SCOPES, OAuthService
from x_write.repository import Repository, StateError
from x_write.xclient import OAuth2TokenResponse


class IdentityClient:
    def __init__(self, credentials, identity=None):
        self.credentials = credentials
        self.identity = identity or {"id": "1001", "username": "gray", "name": "Gray"}

    def verify_account(self):
        return dict(self.identity)


class OAuthOnboardingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_path = os.path.join(self.tmp.name, "credentials.json")
        self.store = CredentialStore(self.store_path)
        self.db = Database(os.path.join(self.tmp.name, "write.db"))
        self.db.migrate()
        self.repo = Repository(self.db)
        self.config = Config.from_mapping({
            "database_path": self.db.path,
            "hmac_secret": "h" * 40,
            "secrets_path": self.store_path,
            "oauth_callback_url": "https://console.example.test/oauth/x/callback",
        })
        self.identities = []

        def factory(credentials):
            identity = self.identities.pop(0) if self.identities else None
            return IdentityClient(credentials, identity)

        self.service = OAuthService(self.config, self.repo, self.store, factory)
        self.service.configure_app(client_id="client-public", client_secret="client-secret", actor="tester")
        self.original_exchange = oauth_module.exchange_oauth2_code

    def tearDown(self):
        oauth_module.exchange_oauth2_code = self.original_exchange
        self.tmp.cleanup()

    def token(self):
        return OAuth2TokenResponse(
            access_token="oauth2-access-value",
            refresh_token="oauth2-refresh-value",
            expires_at=2_000_000_000,
            scopes=tuple(DEFAULT_SCOPES),
        )

    def test_windows_profile_link_callback_creates_paused_account_without_secret_exposure(self):
        flow = self.service.start(
            actor="tester", source_profile_id="k1euo8fi", source_label="Profile 10 · Pixel Mara",
            display_name="Pixel Mara",
        )
        parsed = urllib.parse.urlsplit(flow["authorization_url"])
        query = urllib.parse.parse_qs(parsed.query)
        state = query["state"][0]
        self.assertEqual("S256", query["code_challenge_method"][0])
        self.assertEqual(list(DEFAULT_SCOPES), query["scope"][0].split())
        self.assertNotIn("client-secret", flow["authorization_url"])

        oauth_module.exchange_oauth2_code = lambda **kwargs: self.token()
        completed = self.service.callback(state=state, code="authorization-code", error=None)
        self.assertEqual("succeeded", completed["status"])
        self.assertEqual("1001", completed["result"]["x_user_id"])

        accounts = self.repo.list_accounts()
        self.assertEqual(1, len(accounts))
        account = accounts[0]
        self.assertFalse(account["enabled"])
        self.assertTrue(account["paused"])
        self.assertEqual("verified", account["authorization_status"])
        self.assertEqual("oauth2", account["auth_type"])
        self.assertEqual("k1euo8fi", account["source_profile_id"])
        self.assertEqual("1001", account["x_user_id"])
        self.assertTrue(account["capabilities"]["like"]["allowed"])
        self.assertFalse(account["capabilities"]["article_publish"]["allowed"])

        refs = self.store.list_refs()
        self.assertEqual("x-1001", refs[0]["ref"])
        self.assertEqual("oauth2", refs[0]["auth_type"])
        serialized = json.dumps({"flow": completed, "accounts": accounts, "refs": refs,
                                 "audit": self.repo.list_audit(100)})
        for secret in ("client-secret", "oauth2-access-value", "oauth2-refresh-value", "authorization-code"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(0o600, os.stat(self.store_path).st_mode & 0o777)

        with self.assertRaisesRegex(StateError, "already"):
            self.service.callback(state=state, code="authorization-code", error=None)

    def test_oauth1_form_verifies_then_saves_and_rotation_rejects_other_identity(self):
        result = self.service.save_oauth1(
            actor="tester", consumer_key="consumer-key", consumer_secret="consumer-secret",
            access_token="account-token", access_token_secret="account-token-secret",
            source_profile_id="profile-1", display_name="Gray",
        )
        self.assertEqual("1001", result["account"]["x_user_id"])
        self.assertEqual("oauth1", result["credential"]["auth_type"])
        resolved = self.store.resolve("x-1001")
        self.assertIsInstance(resolved, AccountCredentials)
        self.assertEqual("account-token", resolved.access_token)

        self.identities.append({"id": "2002", "username": "other", "name": "Other"})
        with self.assertRaisesRegex(StateError, "does not match"):
            self.service.save_oauth1(
                actor="tester", consumer_key="new-key", consumer_secret="new-secret",
                access_token="new-token", access_token_secret="new-token-secret",
                account_id=result["account"]["id"],
            )
        still = self.store.resolve("x-1001")
        self.assertEqual("account-token", still.access_token)

    def test_delete_is_blocked_while_account_references_credential(self):
        result = self.service.save_oauth1(
            actor="tester", consumer_key="ck", consumer_secret="cs",
            access_token="at", access_token_secret="ats",
        )
        with self.assertRaisesRegex(StateError, "still bound") as ctx:
            self.service.delete_credential(result["credential"]["ref"], "tester")
        self.assertEqual("credential_in_use", ctx.exception.code)

    def test_public_summaries_do_not_return_secret_field_names_or_values(self):
        status = self.service.status()
        raw = json.dumps(status)
        self.assertNotIn("client-secret", raw)
        self.assertNotIn("access_token", raw)
        self.assertNotIn("refresh_token", raw)
        self.assertTrue(status["developer_app"]["client_secret_configured"])

    def test_expired_and_denied_flows_are_consumed_without_token_exchange(self):
        expired = self.service.start(actor="tester", source_profile_id="profile-expired")
        expired_state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(expired["authorization_url"]).query
        )["state"][0]
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE oauth_authorization_flows SET expires_at=1 WHERE flow_key=?",
                (expired["flow_key"],),
            )
        with self.assertRaises(StateError) as ctx:
            self.service.callback(state=expired_state, code="authorization-code", error=None)
        self.assertEqual("oauth_state_expired", ctx.exception.code)
        expired_status = self.repo.get_oauth_flow(expired["flow_key"])
        self.assertEqual("expired", expired_status["status"])
        with self.db.connect() as conn:
            verifier = conn.execute(
                "SELECT code_verifier FROM oauth_authorization_flows WHERE flow_key=?",
                (expired["flow_key"],),
            ).fetchone()[0]
        self.assertEqual("", verifier)

        denied = self.service.start(actor="tester", source_profile_id="profile-denied")
        denied_state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(denied["authorization_url"]).query
        )["state"][0]
        with self.assertRaises(StateError) as ctx:
            self.service.callback(state=denied_state, code=None, error="access_denied")
        self.assertEqual("oauth_denied", ctx.exception.code)
        self.assertEqual("failed", self.repo.get_oauth_flow(denied["flow_key"])["status"])
        with self.assertRaises(StateError) as replay:
            self.service.callback(state=denied_state, code="authorization-code", error=None)
        self.assertEqual("oauth_state_replayed", replay.exception.code)
        self.assertEqual([], self.store.list_refs())

    def test_scope_loss_fails_flow_without_persisting_credentials(self):
        flow = self.service.start(actor="tester", source_profile_id="profile-scopes")
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(flow["authorization_url"]).query
        )["state"][0]
        oauth_module.exchange_oauth2_code = lambda **kwargs: OAuth2TokenResponse(
            access_token="limited-access",
            refresh_token="limited-refresh",
            expires_at=2_000_000_000,
            scopes=("tweet.read", "tweet.write", "users.read", "offline.access"),
        )
        with self.assertRaises(StateError) as ctx:
            self.service.callback(state=state, code="authorization-code", error=None)
        self.assertEqual("token_scope_insufficient", ctx.exception.code)
        self.assertEqual([], self.store.list_refs())
        self.assertEqual("failed", self.repo.get_oauth_flow(flow["flow_key"])["status"])

    def test_identity_mismatch_does_not_replace_existing_credential(self):
        existing = self.service.save_oauth1(
            actor="tester", consumer_key="old-key", consumer_secret="old-secret",
            access_token="old-token", access_token_secret="old-token-secret",
            source_profile_id="profile-fixed",
        )
        flow = self.service.start(
            actor="tester",
            account_id=existing["account"]["id"],
            source_profile_id="profile-fixed",
        )
        state = urllib.parse.parse_qs(
            urllib.parse.urlsplit(flow["authorization_url"]).query
        )["state"][0]
        self.identities.append({"id": "2002", "username": "other", "name": "Other"})
        oauth_module.exchange_oauth2_code = lambda **kwargs: self.token()
        with self.assertRaises(StateError) as ctx:
            self.service.callback(state=state, code="authorization-code", error=None)
        self.assertEqual("identity_mismatch", ctx.exception.code)
        resolved = self.store.resolve(existing["credential"]["ref"])
        self.assertIsInstance(resolved, AccountCredentials)
        self.assertEqual("old-token", resolved.access_token)
        self.assertEqual("oauth1", self.repo.get_account(existing["account"]["id"])["auth_type"])
        self.assertEqual("failed", self.repo.get_oauth_flow(flow["flow_key"])["status"])

    def test_app_update_preserves_omitted_secret_and_never_returns_it(self):
        updated = self.service.configure_app(
            client_id="rotated-client-id",
            client_secret=None,
            actor="tester",
        )
        app = self.store.get_oauth2_app("primary")
        self.assertEqual("rotated-client-id", app.client_id)
        self.assertEqual("client-secret", app.client_secret)
        self.assertEqual(2, app.generation)
        serialized = json.dumps(updated)
        self.assertNotIn("client-secret", serialized)
        self.assertNotIn("rotated-client-id", serialized)
        self.assertTrue(updated["developer_app"]["client_secret_configured"])


if __name__ == "__main__":
    unittest.main()
