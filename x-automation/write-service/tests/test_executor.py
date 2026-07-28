from __future__ import annotations

import json
import os
import tempfile
import unittest

from x_write.credentials import CredentialStore
from x_write.db import Database
from x_write.executor import Executor
from x_write.repository import Repository, StateError
from x_write.xclient import XAPIError


class FakeClient:
    """Fake X API client recording calls; configure failures via .fail_with."""

    instances: list = []

    def __init__(self, credentials):
        self.calls: list = []
        self.fail_with: dict = {}
        self.verify_result = {"id": "1001", "username": "tester", "name": "T"}
        FakeClient.instances.append(self)

    def _maybe_fail(self, name):
        if name in self.fail_with:
            raise self.fail_with[name]

    def prepare_for_request(self):
        self.calls.append(("prepare_for_request",))
        self._maybe_fail("prepare_for_request")

    def verify_account(self):
        self.calls.append(("verify_account",))
        self._maybe_fail("verify_account")
        return self.verify_result

    def like_post(self, user_id, tweet_id):
        self.calls.append(("like", user_id, tweet_id))
        self._maybe_fail("like")
        return {"liked": True}

    def unlike_post(self, user_id, tweet_id):
        self.calls.append(("unlike", user_id, tweet_id))
        return {"liked": False}

    def repost_post(self, user_id, tweet_id):
        self.calls.append(("repost", user_id, tweet_id))
        return {"retweeted": True}

    def unrepost_post(self, user_id, tweet_id):
        self.calls.append(("unrepost", user_id, tweet_id))
        return {"retweeted": False}

    def create_post(self, text):
        self.calls.append(("create_post", text))
        self._maybe_fail("create_post")
        return {"id": "900", "text": text}

    def reply_post(self, text, tweet_id):
        self.calls.append(("reply", text, tweet_id))
        self._maybe_fail("reply")
        return {"id": "901", "text": text}

    def delete_post(self, tweet_id):
        self.calls.append(("delete_post", tweet_id))
        return {"deleted": True}

    def create_article_draft(self, article):
        self.calls.append(("article_draft", article["title"]))
        return {"article_id": "art-1"}

    def publish_article(self, article_id):
        self.calls.append(("article_publish", article_id))
        return {"article_id": article_id, "post_id": "post-1"}


class ExecutorTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tempdir.name, "service.sqlite3"))
        self.db.migrate()
        self.repo = Repository(self.db)
        secrets_path = os.path.join(self.tempdir.name, "secrets.json")
        with open(secrets_path, "w", encoding="utf-8") as fh:
            json.dump({
                "developer_apps": {"primary": {"consumer_key": "ck", "consumer_secret": "cs"}},
                "accounts": {"primary": {"developer_app": "primary", "access_token": "at", "access_token_secret": "ats"}},
            }, fh)
        os.chmod(secrets_path, 0o600)
        self.store = CredentialStore(secrets_path)
        FakeClient.instances = []
        self.executor = Executor(self.repo, self.store, client_factory=lambda creds: FakeClient(creds),
                                 verify_ttl_seconds=3600, lease_seconds=300, tick_seconds=1)
        account = self.repo.create_account("primary", "Primary", "tester", x_user_id="1001")
        self.account = self.repo.set_account_enabled(account["id"], True, "tester")
        self.account = self.repo.set_account_paused(account["id"], False, "tester")

    def tearDown(self):
        self.tempdir.cleanup()

    def fake(self) -> FakeClient:
        return FakeClient.instances[-1]

    def make_approved(self, request_type, payload):
        request = self.repo.create_request(self.account["id"], request_type, payload, "tester")
        self.repo.submit_request(request["id"], "tester")
        self.repo.approve_request(request["id"], "tester", {
            "content_hash": request["content_hash"], "request_version": request["version"],
        })
        return request["id"]

    def resume_global(self):
        self.repo.set_global_pause(False, "tester")

    def test_like_full_flow_success(self):
        request_id = self.make_approved("like", {"target": "https://x.com/someone/status/55"})
        self.resume_global()
        result = self.executor.tick()
        self.assertEqual("succeeded", result["status"])
        request = self.repo.get_request(request_id)
        self.assertEqual("succeeded", request["status"])
        self.assertEqual(("like", "1001", "55"), self.fake().calls[-1])
        audit = self.repo.list_audit(100)
        events = {row["event_type"] for row in audit}
        self.assertIn("operation.step.sending", events)
        self.assertIn("operation.step.succeeded", events)
        self.assertNotIn("ats", json.dumps(audit))

    def test_global_pause_blocks_claim(self):
        self.make_approved("like", {"target": "55"})
        self.assertIsNone(self.executor.tick())
        self.assertEqual([], FakeClient.instances)

    def test_account_pause_blocks_claim(self):
        self.repo.set_account_paused(self.account["id"], True, "tester")
        self.make_approved("like", {"target": "55"})
        self.resume_global()
        self.assertIsNone(self.executor.tick())
        self.assertEqual([], FakeClient.instances)

    def test_quota_defers_without_sending(self):
        self.repo.upsert_quota_policy(self.account["id"], "like", 3600, 0, "tester")
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()
        result = self.executor.tick()
        self.assertEqual("queued", result["status"])
        op = self.repo.get_request(request_id)["operations"][0]
        self.assertEqual("queued", op["status"])
        self.assertEqual([], FakeClient.instances)

    def test_approval_hash_mismatch_rejected(self):
        request = self.repo.create_request(self.account["id"], "like", {"target": "55"}, "tester")
        self.repo.submit_request(request["id"], "tester")
        with self.assertRaisesRegex(StateError, "hash"):
            self.repo.approve_request(request["id"], "tester", {
                "content_hash": "0" * 64, "request_version": 1})

    def test_timeout_after_send_marker_is_uncertain_and_never_retried(self):
        request_id = self.make_approved("post_create", {"text": "hello"})
        self.resume_global()
        executor = self.executor
        # Force the fake client to fail create_post with a transport timeout.
        original_factory = executor.client_factory

        class FailingClient(FakeClient):
            def create_post(self, text):
                self.calls.append(("create_post", text))
                raise XAPIError("timed out", code="timeout", outcome_uncertain=True)

        executor.client_factory = lambda creds: FailingClient(creds)
        result = executor.tick()
        self.assertEqual("uncertain", result["status"])
        request = self.repo.get_request(request_id)
        self.assertEqual("manual_reconciliation_required", request["status"])
        calls_before = len(FailingClient.instances[-1].calls)
        # A later tick must not resend: the operation is terminal-uncertain.
        self.assertIsNone(executor.tick())
        self.assertEqual(calls_before, len(FailingClient.instances[-1].calls))
        executor.client_factory = original_factory
        with self.assertRaisesRegex(StateError, "note"):
            self.repo.reconcile_operation(result["id"], "succeeded", "", "tester")
        reconciled = self.repo.reconcile_operation(result["id"], "succeeded", "confirmed on x.com", "tester")
        self.assertEqual("reconciled_succeeded", reconciled["status"])
        self.assertEqual("succeeded", self.repo.get_request(request_id)["status"])

    def test_known_4xx_is_failed_not_uncertain(self):
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()

        class ForbiddenClient(FakeClient):
            def like_post(self, user_id, tweet_id):
                raise XAPIError("forbidden", status_code=403, code="http_error")

        self.executor.client_factory = lambda creds: ForbiddenClient(creds)
        result = self.executor.tick()
        self.assertEqual("failed_known", result["status"])
        self.assertEqual("failed", self.repo.get_request(request_id)["status"])

    def test_reply_full_flow_success(self):
        request_id = self.make_approved("reply", {"target": "55", "text": "nice point"})
        self.resume_global()
        result = self.executor.tick()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual(("reply", "nice point", "55"), self.fake().calls[-1])
        request = self.repo.get_request(request_id)
        self.assertEqual("succeeded", request["status"])

    def test_reply_payload_validation(self):
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "reply", {"target": "55"}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "reply", {"text": "hi"}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "reply", {"target": "not-a-tweet", "text": "hi"}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "reply", {"target": "55", "text": "x" * 281}, "tester")

    def test_scheduled_operation_not_claimed_until_due(self):
        import time as _time
        future = int(_time.time()) + 3600
        request = self.repo.create_request(self.account["id"], "post_create",
                                            {"text": "scheduled", "scheduled_at": future}, "tester")
        self.repo.submit_request(request["id"], "tester")
        self.repo.approve_request(request["id"], "tester", {
            "content_hash": request["content_hash"], "request_version": request["version"],
        })
        self.resume_global()
        # Not due yet: nothing claimed, operation stays queued.
        self.assertIsNone(self.executor.tick())
        op = self.repo.get_request(request["id"])["operations"][0]
        self.assertEqual("queued", op["status"])
        self.assertEqual(future, op["scheduled_at"])
        # Backdate the schedule; the executor now claims and sends.
        with self.db.transaction() as conn:
            conn.execute("UPDATE write_operations SET scheduled_at=? WHERE id=?", (int(_time.time()) - 1, op["id"]))
        result = self.executor.tick()
        self.assertEqual("succeeded", result["status"])
        self.assertEqual("succeeded", self.repo.get_request(request["id"])["status"])

    def test_scheduled_at_validation_rejects_past_and_non_integer(self):
        import time as _time
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "post_create",
                                      {"text": "x", "scheduled_at": int(_time.time()) - 10}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "post_create",
                                      {"text": "x", "scheduled_at": "soon"}, "tester")

    def test_refresh_reauth_failure_pauses_before_send_marker(self):
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()

        class ReconnectClient(FakeClient):
            def prepare_for_request(self):
                self.calls.append(("prepare_for_request",))
                raise XAPIError(
                    "refresh token expired",
                    code="token_refresh_reauth_required",
                )

        self.executor.client_factory = lambda creds: ReconnectClient(creds)
        result = self.executor.tick()
        self.assertEqual("failed_known", result["status"])
        self.assertEqual("failed", self.repo.get_request(request_id)["status"])
        account = self.repo.get_account(self.account["id"])
        self.assertTrue(account["paused"])
        self.assertEqual("reconnect_required", account["authorization_status"])
        events = {row["event_type"] for row in self.repo.list_audit(100)}
        self.assertIn("account.authorization.invalidated", events)
        self.assertNotIn("operation.step.sending", events)
        self.assertEqual([("prepare_for_request",)], ReconnectClient.instances[-1].calls)

    def test_transient_refresh_failure_does_not_pause_or_mark_sending(self):
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()

        class TransientRefreshClient(FakeClient):
            def prepare_for_request(self):
                self.calls.append(("prepare_for_request",))
                raise XAPIError(
                    "token endpoint unavailable",
                    code="token_refresh_transient",
                    retry_after=2_000_000_000,
                )

        self.executor.client_factory = lambda creds: TransientRefreshClient(creds)
        result = self.executor.tick()
        self.assertEqual("failed_known", result["status"])
        self.assertEqual("failed", self.repo.get_request(request_id)["status"])
        account = self.repo.get_account(self.account["id"])
        self.assertFalse(account["paused"])
        self.assertNotEqual("reconnect_required", account["authorization_status"])
        events = {row["event_type"] for row in self.repo.list_audit(100)}
        self.assertNotIn("operation.step.sending", events)
        self.assertEqual([("prepare_for_request",)], TransientRefreshClient.instances[-1].calls)

    def test_identity_mismatch_pauses_account_and_fails(self):
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()

        class WrongAccount(FakeClient):
            def verify_account(self):
                return {"id": "2002", "username": "other", "name": "O"}

        self.executor.client_factory = lambda creds: WrongAccount(creds)
        result = self.executor.tick()
        self.assertEqual("failed_known", result["status"])
        account = self.repo.get_account(self.account["id"])
        self.assertTrue(account["paused"])
        self.assertEqual("failed", self.repo.get_request(request_id)["status"])

    def test_article_requires_second_human_approval(self):
        request_id = self.make_approved("article_draft_publish", {
            "article": {"schema_version": 1, "title": "Draft",
                        "blocks": [{"type": "paragraph", "text": "body"}]},
        })
        self.resume_global()
        result = self.executor.tick()
        self.assertEqual("awaiting_approval", result["status"])
        self.assertEqual("art-1", result["external_object_id"])
        self.assertEqual([], [c for c in self.fake().calls if c[0] == "article_publish"])
        request = self.repo.get_request(request_id)
        approved = self.repo.approve_next_step(result["id"], "tester", {"content_hash": request["content_hash"]})
        self.assertEqual("queued", approved["status"])
        final = self.executor.tick()
        self.assertEqual("succeeded", final["status"])
        publish_calls = [c for c in self.fake().calls if c[0] == "article_publish"]
        self.assertEqual([("article_publish", "art-1")], publish_calls)
        self.assertEqual("succeeded", self.repo.get_request(request_id)["status"])

    def test_recover_running_operations(self):
        request_id = self.make_approved("like", {"target": "55"})
        self.resume_global()
        op = self.repo.claim_next_operation(lease_seconds=-10)
        self.assertIsNotNone(op)
        # Lease expired with no send marker -> requeue.
        self.assertEqual(1, self.repo.recover_running_operations())
        op = self.repo.get_request(request_id)["operations"][0]
        self.assertEqual("queued", op["status"])
        # Mark sending, expire lease again -> uncertain.
        op = self.repo.claim_next_operation(lease_seconds=-10)
        self.repo.mark_step_sending(op["id"], 1)
        self.assertEqual(1, self.repo.recover_running_operations())
        op = self.repo.get_request(request_id)["operations"][0]
        self.assertEqual("uncertain", op["status"])
        self.assertEqual("manual_reconciliation_required", self.repo.get_request(request_id)["status"])

    def test_invalid_payload_rejected(self):
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "like", {"target": "not-a-tweet"}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "like", {"target": "55", "extra": 1}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "post_create", {"text": "x" * 281}, "tester")
        with self.assertRaises(StateError):
            self.repo.create_request(self.account["id"], "article_draft_publish", {
                "article": {"schema_version": 1, "title": "t",
                            "blocks": [{"type": "paragraph", "text": "b", "url": "javascript:alert(1)"}],
                            }}, "tester")

    def test_cancel_queued_request_cancels_operation(self):
        request_id = self.make_approved("like", {"target": "55"})
        cancelled = self.repo.cancel_request(request_id, "tester")
        self.assertEqual("cancelled", cancelled["status"])
        op = self.repo.get_request(request_id)["operations"][0]
        self.assertEqual("cancelled", op["status"])
        self.resume_global()
        self.assertIsNone(self.executor.tick())


if __name__ == "__main__":
    unittest.main()
