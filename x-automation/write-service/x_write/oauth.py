from __future__ import annotations

import base64
import hashlib
import re
import secrets
from typing import Any, Callable

from .config import Config
from .credentials import (
    AccountCredentials,
    CredentialError,
    CredentialStore,
    OAuth2Credentials,
    ResolvedCredentials,
)
from .repository import Repository, StateError
from .xclient import XAPIClient, XAPIError, build_oauth2_authorization_url, exchange_oauth2_code


DEFAULT_SCOPES = ("tweet.read", "tweet.write", "users.read", "like.write", "media.write", "offline.access")
_CODE_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,2048}$")
_STATE_RE = re.compile(r"^[A-Za-z0-9_-]{20,256}$")


class OAuthService:
    """Coordinates developer-app configuration and per-account authorization."""

    def __init__(self, config: Config, repository: Repository, credential_store: CredentialStore,
                 client_factory: Callable[[ResolvedCredentials], XAPIClient]):
        self.config = config
        self.repository = repository
        self.credential_store = credential_store
        self.client_factory = client_factory

    def status(self) -> dict[str, Any]:
        app = self.credential_store.oauth2_app_summary("primary")
        callback = self.config.oauth_callback_url or ""
        return {
            "oauth2_ready": bool(callback and app.get("configured") and app.get("redirect_uri") == callback),
            "callback_configured": bool(callback),
            "callback_url": callback,
            "authorize_url": self.config.oauth_authorize_url,
            "required_scopes": list(DEFAULT_SCOPES),
            "developer_app": app,
            "credential_refs": self.credential_store.list_refs(),
            "legacy_store_migration_needed": self.credential_store.migration_needed(),
        }

    def configure_app(self, *, client_id: str, client_secret: str | None,
                      actor: str) -> dict[str, Any]:
        callback = self.config.oauth_callback_url
        if not callback:
            raise StateError("HTTPS OAuth callback is not configured", "oauth_callback_unconfigured")
        try:
            summary = self.credential_store.upsert_oauth2_app(
                "primary", client_id, client_secret, callback,
            )
        except CredentialError as exc:
            raise StateError(str(exc), exc.code) from exc
        self.repository.audit_credential_event(
            "oauth.app.configure", actor, "oauth2-app:primary", {
                "developer_app": "primary",
                "generation": summary["generation"],
                "redirect_uri": callback,
                "client_secret_configured": summary["client_secret_configured"],
            },
        )
        return self.status()

    def start(self, *, actor: str, account_id: int | None = None,
              source_profile_id: str | None = None, source_label: str | None = None,
              display_name: str | None = None, expected_x_user_id: str | None = None) -> dict[str, Any]:
        callback = self.config.oauth_callback_url
        if not callback:
            raise StateError("HTTPS OAuth callback is not configured", "oauth_callback_unconfigured")
        try:
            app = self.credential_store.get_oauth2_app("primary")
        except CredentialError as exc:
            raise StateError(str(exc), exc.code) from exc
        if app.redirect_uri != callback:
            raise StateError("configured OAuth callback does not match the developer app", "oauth_callback_mismatch")
        source_profile_id = self._optional_text(source_profile_id, "source_profile_id", 120)
        source_label = self._optional_text(source_label, "source_label", 120)
        display_name = self._optional_text(display_name, "display_name", 120)
        expected_x_user_id = self._optional_numeric_id(expected_x_user_id)
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
        flow_key = secrets.token_hex(16)
        flow = self.repository.create_oauth_flow(
            flow_key=flow_key,
            state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            code_verifier=verifier,
            developer_app="primary",
            requested_scopes=list(DEFAULT_SCOPES),
            redirect_uri=callback,
            actor=actor,
            ttl_seconds=self.config.oauth_flow_ttl_seconds,
            account_id=account_id,
            source_profile_id=source_profile_id,
            source_label=source_label,
            display_name=display_name,
            expected_x_user_id=expected_x_user_id,
        )
        flow["authorization_url"] = build_oauth2_authorization_url(
            authorize_url=self.config.oauth_authorize_url,
            app=app,
            state=state,
            code_challenge=challenge,
            scopes=DEFAULT_SCOPES,
        )
        return flow

    def callback(self, *, state: str, code: str | None, error: str | None) -> dict[str, Any]:
        if not isinstance(state, str) or not _STATE_RE.fullmatch(state):
            raise StateError("OAuth state is invalid", "oauth_state_invalid")
        flow = self.repository.claim_oauth_flow(state)
        flow_key = flow["flow_key"]
        if error:
            safe_error = str(error)[:80]
            self.repository.fail_oauth_flow(flow_key, "oauth_denied")
            raise StateError(f"X authorization was not completed ({safe_error})", "oauth_denied")
        if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
            self.repository.fail_oauth_flow(flow_key, "oauth_code_invalid")
            raise StateError("OAuth authorization code is invalid", "oauth_code_invalid")
        try:
            app = self.credential_store.get_oauth2_app(flow["developer_app"])
            token = exchange_oauth2_code(
                app=app,
                code=code,
                code_verifier=flow["code_verifier"],
                token_url=self.config.oauth_token_url,
                proxy_url=self.config.x_api_proxy_url,
                timeout_seconds=self.config.x_api_timeout_seconds,
            )
            missing = sorted(set(flow["requested_scopes"]) - set(token.scopes))
            if missing:
                raise StateError("X authorization did not grant every required scope", "token_scope_insufficient")
            candidate = OAuth2Credentials(
                client_id=app.client_id,
                client_secret=app.client_secret,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=token.expires_at,
                scopes=token.scopes,
                developer_app=app.name,
            )
            verified = self.client_factory(candidate).verify_account()
            self._assert_target_identity(flow, verified)
            credential_ref = self._credential_ref(flow, verified)
            summary = self.credential_store.save_oauth2_credential(
                credential_ref,
                developer_app=app.name,
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                expires_at=token.expires_at,
                scopes=token.scopes,
                x_user_id=verified["id"],
                username=verified.get("username", ""),
            )
            account = self.repository.bind_authorized_account(
                credential_ref=credential_ref,
                auth_type="oauth2",
                generation=summary["generation"],
                verified=verified,
                scopes=list(token.scopes),
                actor=flow["actor"],
                account_id=flow["account_id"],
                display_name=flow["display_name"],
                source_profile_id=flow["source_profile_id"],
                source_label=flow["source_label"],
                expected_x_user_id=flow["expected_x_user_id"],
            )
            self.repository.audit_credential_event(
                "credential.oauth2.save", flow["actor"], credential_ref, {
                    "auth_type": "oauth2", "generation": summary["generation"],
                    "x_user_id": verified["id"], "username": verified.get("username", ""),
                    "scopes": list(token.scopes), "expires_at": token.expires_at,
                },
            )
            return self.repository.complete_oauth_flow(
                flow_key,
                credential_ref=credential_ref,
                account_id=account["id"],
                result={
                    "account_id": account["id"],
                    "x_user_id": verified["id"],
                    "username": verified.get("username", ""),
                    "auth_type": "oauth2",
                },
            )
        except (CredentialError, XAPIError, StateError) as exc:
            error_code = getattr(exc, "code", "oauth_callback_failed")
            self.repository.fail_oauth_flow(flow_key, error_code)
            if isinstance(exc, StateError):
                raise
            raise StateError(str(exc), error_code) from exc
        except Exception as exc:
            self.repository.fail_oauth_flow(flow_key, "oauth_callback_failed")
            raise StateError("OAuth callback could not be completed", "oauth_callback_failed") from exc

    def save_oauth1(self, *, actor: str, consumer_key: str, consumer_secret: str,
                    access_token: str, access_token_secret: str,
                    account_id: int | None = None, display_name: str | None = None,
                    source_profile_id: str | None = None, source_label: str | None = None,
                    expected_x_user_id: str | None = None) -> dict[str, Any]:
        values = (consumer_key, consumer_secret, access_token, access_token_secret)
        if any(not isinstance(value, str) or not value for value in values):
            raise StateError("OAuth 1.0a credentials are incomplete", "credentials_incomplete")
        candidate = AccountCredentials(consumer_key, consumer_secret, access_token, access_token_secret)
        try:
            verified = self.client_factory(candidate).verify_account()
            flow_like = {
                "account_id": account_id,
                "source_profile_id": self._optional_text(source_profile_id, "source_profile_id", 120),
                "source_label": self._optional_text(source_label, "source_label", 120),
                "display_name": self._optional_text(display_name, "display_name", 120),
                "expected_x_user_id": self._optional_numeric_id(expected_x_user_id),
            }
            self._assert_target_identity(flow_like, verified)
            credential_ref = self._credential_ref(flow_like, verified)
            summary = self.credential_store.save_oauth1_credential(
                credential_ref,
                consumer_key=consumer_key,
                consumer_secret=consumer_secret,
                access_token=access_token,
                access_token_secret=access_token_secret,
                x_user_id=verified["id"],
                username=verified.get("username", ""),
            )
            account = self.repository.bind_authorized_account(
                credential_ref=credential_ref,
                auth_type="oauth1",
                generation=summary["generation"],
                verified=verified,
                scopes=[],
                actor=actor,
                account_id=account_id,
                display_name=flow_like["display_name"],
                source_profile_id=flow_like["source_profile_id"],
                source_label=flow_like["source_label"],
                expected_x_user_id=flow_like["expected_x_user_id"],
            )
            self.repository.audit_credential_event(
                "credential.oauth1.save", actor, credential_ref, {
                    "auth_type": "oauth1", "generation": summary["generation"],
                    "x_user_id": verified["id"], "username": verified.get("username", ""),
                },
            )
            return {"account": account, "credential": summary}
        except (CredentialError, XAPIError, StateError) as exc:
            if isinstance(exc, StateError):
                raise
            raise StateError(str(exc), getattr(exc, "code", "oauth1_verification_failed")) from exc

    def delete_credential(self, credential_ref: str, actor: str) -> dict[str, Any]:
        usage = self.repository.credential_usage(credential_ref)
        if usage["accounts"] or usage["active_operations"]:
            raise StateError("credential is still bound to a write account", "credential_in_use")
        try:
            deleted = self.credential_store.delete_credential(credential_ref)
        except CredentialError as exc:
            raise StateError(str(exc), exc.code) from exc
        self.repository.audit_credential_event("credential.delete", actor, credential_ref, deleted)
        return deleted

    def _assert_target_identity(self, flow: dict[str, Any], verified: dict[str, Any]) -> None:
        x_user_id = str(verified.get("id") or "")
        expected = flow.get("expected_x_user_id")
        if expected and str(expected) != x_user_id:
            raise StateError("authorized X user does not match the expected identity", "identity_mismatch")
        account_id = flow.get("account_id")
        if account_id:
            account = self.repository.get_account(int(account_id))
            if account.get("x_user_id") and str(account["x_user_id"]) != x_user_id:
                raise StateError("authorized X user does not match the selected account", "identity_mismatch")
        source_profile_id = flow.get("source_profile_id")
        if source_profile_id:
            for account in self.repository.list_accounts():
                if account.get("source_profile_id") == source_profile_id and account.get("x_user_id") and str(account["x_user_id"]) != x_user_id:
                    raise StateError("Windows profile is already bound to another X identity", "identity_mismatch")

    def _credential_ref(self, flow: dict[str, Any], verified: dict[str, Any]) -> str:
        account_id = flow.get("account_id")
        if account_id:
            account = self.repository.get_account(int(account_id))
            if account.get("credential_ref"):
                return str(account["credential_ref"])
        x_user_id = str(verified["id"])
        for account in self.repository.list_accounts():
            if str(account.get("x_user_id") or "") == x_user_id and account.get("credential_ref"):
                return str(account["credential_ref"])
        return f"x-{x_user_id}"

    @staticmethod
    def _optional_text(value: Any, field: str, maximum: int) -> str | None:
        if value in (None, ""):
            return None
        if not isinstance(value, str) or len(value.strip()) > maximum:
            raise StateError(f"{field} is invalid", "validation_error")
        return value.strip()

    @staticmethod
    def _optional_numeric_id(value: Any) -> str | None:
        if value in (None, ""):
            return None
        text = str(value)
        if not text.isdigit() or len(text) > 20:
            raise StateError("expected_x_user_id is invalid", "validation_error")
        return text
