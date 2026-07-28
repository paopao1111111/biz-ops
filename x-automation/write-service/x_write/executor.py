from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from .credentials import CredentialError, CredentialStore
from .repository import Repository, StateError
from .xclient import XAPIClient, XAPIError


class Executor:
    """Conservative single-operation executor.

    Rules that must always hold:
    - never send while the global write pause is set
    - never send for a disabled or paused account
    - write the durable send marker before every mutating API call
    - never automatically retry after the send marker exists
    - uncertain outcomes require human reconciliation
    """

    def __init__(self, repo: Repository, credential_store: CredentialStore,
                 *, client_factory: Callable[..., XAPIClient] = XAPIClient,
                 verify_ttl_seconds: int = 3600, lease_seconds: int = 300,
                 tick_seconds: float = 5.0, executor_enabled: bool = True):
        self.repo = repo
        self.credential_store = credential_store
        self.client_factory = client_factory
        self.verify_ttl_seconds = verify_ttl_seconds
        self.lease_seconds = lease_seconds
        self.tick_seconds = max(0.5, tick_seconds)
        self.executor_enabled = executor_enabled
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    def start(self) -> None:
        if not self.executor_enabled:
            return
        recovered = self.repo.recover_running_operations()
        if recovered:
            logging.info("[x-write executor] recovered %d stale operations", recovered)
        self._thread = threading.Thread(target=self._loop, name="x-write-executor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.last_error = "internal_error"
                logging.exception("[x-write executor] tick failed")
            self._stop.wait(self.tick_seconds)

    # ---------- client plumbing ----------

    @staticmethod
    def _credential_ref(account: dict[str, Any]) -> str:
        return str(account.get("credential_ref") or account.get("metadata", {}).get("credential_ref") or account["account_key"])

    def _client_context(self, account: dict[str, Any]) -> tuple[XAPIClient, str, int]:
        ref = self._credential_ref(account)
        creds = self.credential_store.resolve(ref)
        return self.client_factory(creds), ref, max(1, int(getattr(creds, "generation", 1) or 1))

    def _client_for(self, account: dict[str, Any]) -> XAPIClient:
        return self._client_context(account)[0]

    def _verify_identity(self, account: dict[str, Any], client: XAPIClient) -> None:
        metadata = account.get("metadata", {})
        verified_at = int(metadata.get("verified_at") or 0)
        if verified_at and time.time() - verified_at < self.verify_ttl_seconds:
            return
        verified = client.verify_account()
        self.repo.record_verification(account["id"], "executor", verified)

    # ---------- step dispatch ----------

    def _run_step(self, client: XAPIClient, account: dict[str, Any], operation: dict[str, Any],
                  step_name: str, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        user_id = account.get("x_user_id") or account.get("metadata", {}).get("verified_x_user_id")
        if not user_id:
            raise StateError("account has no verified X user id", "identity_unverified")
        if step_name == "like":
            return None, client.like_post(user_id, payload["target"])
        if step_name == "unlike":
            return None, client.unlike_post(user_id, payload["target"])
        if step_name == "repost":
            return None, client.repost_post(user_id, payload["target"])
        if step_name == "unrepost":
            return None, client.unrepost_post(user_id, payload["target"])
        if step_name == "post_create":
            asset_ids = payload.get("media_asset_ids") or []
            if asset_ids:
                rows = self.repo.media_assets_for_ids([int(i) for i in asset_ids])
                resolved: list[str] = []
                for row in rows:
                    if not row.get("x_media_id"):
                        raise StateError("media asset has not been uploaded to X yet", "media_not_ready")
                    resolved.append(row["x_media_id"])
                created = client.create_post(payload["text"], media_ids=resolved)
            else:
                created = client.create_post(payload["text"])
            return created["id"], created
        if step_name == "media_upload":
            return self._run_media_upload_step(client, payload)
        if step_name == "reply":
            created = client.reply_post(payload["text"], payload["target"])
            return created["id"], created
        if step_name == "post_delete":
            return payload["target"], client.delete_post(payload["target"])
        if step_name == "article_create_draft":
            created = client.create_article_draft(payload["article"])
            return created["article_id"], created
        if step_name == "article_publish":
            article_id = operation.get("external_object_id")
            if not article_id:
                raise StateError("article publish requires a created draft id", "missing_article_id")
            published = client.publish_article(article_id)
            return published.get("post_id") or article_id, published
        raise StateError(f"unsupported step {step_name}", "unsupported_step")

    def _run_media_upload_step(self, client: XAPIClient, payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
        """Upload all media assets for a post_create operation to X at send
        time, persisting each media_id onto its media_assets row. The subsequent
        post_create step re-reads the media_ids from the rows — no cross-step
        in-memory state. Upload happens here (not at console image-upload time)
        so the ~24h media_id expiry never bites scheduled posts."""
        asset_ids = payload.get("media_asset_ids") or []
        if not asset_ids:
            return None, {"media_ids": []}
        uploaded: list[str] = []
        for asset_id in asset_ids:
            asset = self.repo.get_media_asset(int(asset_id))
            if asset["status"] == "ready" and asset.get("x_media_id"):
                uploaded.append(asset["x_media_id"])
                continue
            if not asset.get("local_path"):
                raise StateError("media asset has no local bytes to upload", "media_not_ready")
            try:
                with open(asset["local_path"], "rb") as handle:
                    media_bytes = handle.read()
            except OSError as exc:
                raise StateError(f"media asset file could not be read: {exc}", "media_not_ready") from exc
            result = client.upload_media(media_bytes, mime_type=asset["mime_type"])
            self.repo.mark_media_ready(int(asset_id), x_media_id=result["media_id"])
            uploaded.append(result["media_id"])
        return uploaded[0] if uploaded else None, {"media_ids": uploaded}

    # ---------- main tick ----------

    def tick(self) -> dict[str, Any] | None:
        operation = self.repo.claim_next_operation(self.lease_seconds)
        if not operation:
            return None
        operation_id = operation["id"]
        try:
            return self._execute(operation)
        except StateError as exc:
            self.repo.complete_step_failure(operation_id, operation["current_step"], exc.code, str(exc), None, uncertain=False)
            self.last_error = exc.code
            return self.repo.get_operation(operation_id)
        except Exception:
            self.last_error = "internal_error"
            logging.exception("[x-write executor] unexpected failure operation=%s", operation_id)
            self.repo.complete_step_failure(operation_id, operation["current_step"], "unexpected_error",
                                            "unexpected executor failure; reconcile manually", None, uncertain=True)
            return self.repo.get_operation(operation_id)

    def _execute(self, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = operation["id"]
        account = self.repo.get_account(operation["account_id"])
        request = self.repo.get_request(operation["request_id"])
        payload = request["payload"]
        detail = self.repo.get_operation(operation_id)
        steps = detail["steps"]

        if self.repo.approval_expired_for_operation(operation_id):
            self.repo.cancel_operation_for_expired_approval(operation_id)
            return self.repo.get_operation(operation_id)

        quota_conn = self.repo.db.connect()
        try:
            block = self.repo.quota_block_reason(quota_conn, account["id"], operation["operation_type"])
        finally:
            quota_conn.close()
        if block:
            self.repo.release_operation(operation_id, operation["lease_token"], block)
            return self.repo.get_operation(operation_id)

        try:
            client, credential_ref, credential_generation = self._client_context(account)
            prepare = getattr(client, "prepare_for_request", None)
            if callable(prepare):
                prepare()
            credential_generation = max(
                credential_generation,
                int(getattr(client, "credential_generation", credential_generation) or credential_generation),
            )
        except CredentialError as exc:
            self.repo.complete_step_failure(operation_id, operation["current_step"], exc.code, str(exc), None, uncertain=False)
            self.last_error = exc.code
            return self.repo.get_operation(operation_id)
        except XAPIError as exc:
            if exc.code in {
                "token_refresh_reauth_required", "token_refresh_invalid",
                "token_scope_insufficient", "credential_changed",
            }:
                self.repo.invalidate_authorization(account["id"], "executor", exc.code)
            self.repo.complete_step_failure(operation_id, operation["current_step"], exc.code,
                                            str(exc), exc.retry_after, uncertain=False)
            self.last_error = exc.code
            return self.repo.get_operation(operation_id)

        try:
            self._verify_identity(account, client)
        except XAPIError as exc:
            self.repo.complete_step_failure(operation_id, operation["current_step"], exc.code,
                                            str(exc), exc.retry_after, uncertain=bool(exc.outcome_uncertain))
            self.last_error = exc.code
            return self.repo.get_operation(operation_id)
        account = self.repo.get_account(account["id"])

        for step in steps:
            if step["status"] in ("succeeded", "skipped"):
                continue
            if step["status"] in ("failed", "uncertain"):
                break
            current = self.repo.get_operation(operation_id)
            if current["status"] == "awaiting_approval":
                return current
            block = self.repo.pre_send_block_reason(
                account["id"], credential_ref, credential_generation, operation["operation_type"],
            )
            if block in {"global_write_paused", "account_disabled", "account_paused"}:
                self.repo.release_operation(operation_id, operation["lease_token"], block)
                return self.repo.get_operation(operation_id)
            if block:
                self.repo.complete_step_failure(
                    operation_id, step["step_order"], block,
                    "credential or authorization changed before send", None, uncertain=False,
                )
                self.last_error = block
                return self.repo.get_operation(operation_id)
            if step["step_name"] == "media_upload" and account.get("auth_type") == "oauth2":
                capability = (account.get("capabilities") or {}).get("media_upload") or {}
                if not capability.get("allowed"):
                    reason = capability.get("reason") or "media_upload_not_allowed"
                    self.repo.invalidate_authorization(account["id"], "executor", "token_scope_insufficient")
                    self.repo.complete_step_failure(
                        operation_id, step["step_order"], "token_scope_insufficient",
                        f"account lacks media.write scope: {reason}", None, uncertain=False,
                    )
                    self.last_error = "token_scope_insufficient"
                    return self.repo.get_operation(operation_id)
            self.repo.mark_step_sending(operation_id, step["step_order"])
            try:
                external_id, response = self._run_step(client, account, current, step["step_name"], payload)
            except XAPIError as exc:
                uncertain = bool(exc.outcome_uncertain)
                self.repo.complete_step_failure(operation_id, step["step_order"], exc.code,
                                                str(exc), exc.retry_after, uncertain=uncertain)
                self.last_error = exc.code
                return self.repo.get_operation(operation_id)
            except StateError as exc:
                self.repo.complete_step_failure(operation_id, step["step_order"], exc.code, str(exc), None, uncertain=False)
                self.last_error = exc.code
                return self.repo.get_operation(operation_id)
            await_next = step["step_name"] == "article_create_draft"
            self.repo.complete_step_success(operation_id, step["step_order"], external_id,
                                            response, await_next_approval=await_next)
            self.last_error = None
        return self.repo.get_operation(operation_id)
