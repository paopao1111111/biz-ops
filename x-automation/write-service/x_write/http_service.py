from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .auth import AuthHeaders, AuthenticationError, HMACAuthenticator
from .config import Config
from .credentials import CredentialError, CredentialStore
from .executor import Executor
from .oauth import OAuthService
from .repository import NotFoundError, Repository, StateError
from .xclient import XAPIClient, XAPIError


class ValidationError(ValueError):
    pass


def validate_object(value: Any, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError("JSON body must be an object")
    missing = required - set(value)
    extra = set(value) - required - optional
    if missing:
        raise ValidationError(f"missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise ValidationError(f"unknown fields: {', '.join(sorted(extra))}")
    return value


def require_string(value: Any, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ValidationError(f"{field} must be a non-empty string")
    return value


class ServiceHandler(BaseHTTPRequestHandler):
    server_version = "x-write"
    sys_version = ""

    @property
    def app(self) -> "XWriteHTTPServer":
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        self._dispatch()

    def do_POST(self) -> None:
        self._dispatch()

    def do_PATCH(self) -> None:
        self._dispatch()

    # ---------- plumbing ----------

    def _dispatch(self) -> None:
        split = urlsplit(self.path)
        path = split.path
        query = parse_qs(split.query)
        try:
            # Media upload has its own larger cap; HMAC is computed over the
            # full body either way, so read with the right limit up front.
            parts = path.strip("/").split("/")
            is_media_upload = (self.command == "POST" and len(parts) == 4
                               and parts[0] == "api" and parts[1] == "media-assets"
                               and parts[3] == "upload")
            body = self._read_body(
                self.app.config.max_media_upload_bytes if is_media_upload
                else self.app.config.max_body_bytes)
            if path != "/health":
                self._authenticate(path, body)
            self._route(path, query, body)
        except AuthenticationError as exc:
            self._json(401, {"error": "unauthorized", "message": str(exc)})
        except ValidationError as exc:
            self._json(400, {"error": "validation_error", "message": str(exc)})
        except NotFoundError as exc:
            self._json(404, {"error": "not_found", "message": str(exc)})
        except StateError as exc:
            self._json(409, {"error": getattr(exc, "code", "state_error"), "message": str(exc)})
        except Exception:
            import logging
            logging.exception("[x-write] unhandled request error")
            self._json(500, {"error": "internal_error"})

    def _route(self, path: str, query: dict[str, list[str]], body: bytes) -> None:
        repo = self.app.repository
        if self.command == "GET" and path == "/health":
            self._json(200, {"ok": True, "service": "x_write", "version": _version()})
        elif self.command == "GET" and path == "/api/status":
            self._json(200, repo.status() | {
                "executor_last_error": self.app.executor.last_error if self.app.executor else None,
            })
        elif self.command == "GET" and path == "/api/config":
            self._json(200, self.app.config.public_metadata())
        elif self.command == "GET" and path == "/api/credential-refs":
            self._json(200, {
                "credential_refs": self.app.credential_store.list_refs(),
                "secrets_configured": bool(self.app.config.secrets_path),
            })
        elif self.command == "GET" and path == "/api/oauth/status":
            self._json(200, self.app.oauth_service.status())
        elif self.command == "POST" and path == "/api/oauth/app":
            data = self._json_body(body, {"client_id", "actor"}, {"client_secret"})
            secret = data.get("client_secret")
            if secret is not None and not isinstance(secret, str):
                raise ValidationError("client_secret must be a string")
            self._json(200, self.app.oauth_service.configure_app(
                client_id=require_string(data["client_id"], "client_id"),
                client_secret=secret,
                actor=require_string(data["actor"], "actor"),
            ))
        elif self.command == "POST" and path == "/api/oauth/start":
            data = self._json_body(body, {"actor"}, {
                "account_id", "source_profile_id", "source_label", "display_name", "expected_x_user_id",
            })
            account_id = data.get("account_id")
            if account_id is not None and type(account_id) is not int:
                raise ValidationError("account_id must be an integer")
            self._json(201, self.app.oauth_service.start(
                actor=require_string(data["actor"], "actor"),
                account_id=account_id,
                source_profile_id=self._optional_string(data.get("source_profile_id"), "source_profile_id"),
                source_label=self._optional_string(data.get("source_label"), "source_label"),
                display_name=self._optional_string(data.get("display_name"), "display_name"),
                expected_x_user_id=self._optional_string(data.get("expected_x_user_id"), "expected_x_user_id"),
            ))
        elif self.command == "POST" and path == "/api/oauth/callback":
            data = self._json_body(body, {"state", "actor"}, {"code", "error"})
            self._json(200, self.app.oauth_service.callback(
                state=require_string(data["state"], "state"),
                code=self._optional_string(data.get("code"), "code"),
                error=self._optional_string(data.get("error"), "error"),
            ))
        elif self.command == "POST" and path == "/api/credentials/oauth1":
            data = self._json_body(body, {
                "consumer_key", "consumer_secret", "access_token", "access_token_secret", "actor",
            }, {"account_id", "display_name", "source_profile_id", "source_label", "expected_x_user_id"})
            account_id = data.get("account_id")
            if account_id is not None and type(account_id) is not int:
                raise ValidationError("account_id must be an integer")
            self._json(201, self.app.oauth_service.save_oauth1(
                actor=require_string(data["actor"], "actor"),
                consumer_key=require_string(data["consumer_key"], "consumer_key"),
                consumer_secret=require_string(data["consumer_secret"], "consumer_secret"),
                access_token=require_string(data["access_token"], "access_token"),
                access_token_secret=require_string(data["access_token_secret"], "access_token_secret"),
                account_id=account_id,
                display_name=self._optional_string(data.get("display_name"), "display_name"),
                source_profile_id=self._optional_string(data.get("source_profile_id"), "source_profile_id"),
                source_label=self._optional_string(data.get("source_label"), "source_label"),
                expected_x_user_id=self._optional_string(data.get("expected_x_user_id"), "expected_x_user_id"),
            ))
        elif self.command == "POST" and path == "/api/global/pause":
            data = self._json_body(body, {"actor"})
            repo.set_global_pause(True, require_string(data["actor"], "actor"))
            self._json(200, repo.status())
        elif self.command == "POST" and path == "/api/global/resume":
            data = self._json_body(body, {"actor"})
            repo.set_global_pause(False, require_string(data["actor"], "actor"))
            self._json(200, repo.status())
        elif self.command == "GET" and path == "/api/accounts":
            self._json(200, {"accounts": repo.list_accounts()})
        elif self.command == "POST" and path == "/api/accounts":
            data = self._json_body(body, {"account_key", "display_name", "actor"}, {"x_user_id", "metadata"})
            metadata = data.get("metadata", {})
            if not isinstance(metadata, dict):
                raise ValidationError("metadata must be an object")
            account = repo.create_account(
                require_string(data["account_key"], "account_key"),
                require_string(data["display_name"], "display_name"),
                require_string(data["actor"], "actor"),
                self._optional_string(data.get("x_user_id"), "x_user_id"),
                metadata,
            )
            self._json(201, account)
        elif self.command == "GET" and path == "/api/requests":
            self._json(200, {"requests": repo.list_requests(self._int_query(query, "limit", 100),
                                                            self._str_query(query, "status"))})
        elif self.command == "GET" and path == "/api/media-assets":
            account_id = self._int_query_or_none(query, "account_id")
            status = self._str_query(query, "status")
            self._json(200, {"media_assets": repo.list_media_assets(
                account_id=account_id, status=status)})
        elif self.command == "POST" and path == "/api/media-assets":
            self._register_media_asset(body)
        elif self.command == "POST" and path == "/api/requests":
            data = self._json_body(body, {"account_id", "request_type", "payload", "actor"})
            if type(data["account_id"]) is not int:
                raise ValidationError("account_id must be an integer")
            request = repo.create_request(
                data["account_id"], require_string(data["request_type"], "request_type"),
                data["payload"], require_string(data["actor"], "actor"))
            self._json(201, request)
        elif self.command == "GET" and path == "/api/operations":
            self._json(200, {"operations": repo.list_operations(self._int_query(query, "limit", 100),
                                                                self._str_query(query, "status"))})
        elif self.command == "GET" and path == "/api/audit":
            self._json(200, {"audit": repo.list_audit(self._int_query(query, "limit", 100),
                                                      self._str_query(query, "target_type"),
                                                      self._str_query(query, "target_id"))})
        else:
            routed = self._route_subresource(path, body)
            if not routed:
                self._json(404, {"error": "not_found"})

    def _route_subresource(self, path: str, body: bytes) -> bool:
        repo = self.app.repository
        parts = path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] == "api":
            resource, rest = parts[1], parts[2:]
        else:
            return False

        if resource == "oauth" and len(rest) >= 2 and rest[0] == "flows":
            flow_key = rest[1]
            if len(rest) == 2 and self.command == "GET":
                self._json(200, self.app.repository.get_oauth_flow(flow_key))
                return True
            if len(rest) == 3 and rest[2] == "cancel" and self.command == "POST":
                data = self._json_body(body, {"actor"})
                self._json(200, self.app.repository.cancel_oauth_flow(
                    flow_key, require_string(data["actor"], "actor")))
                return True
            return False

        if resource == "credentials" and len(rest) == 2 and rest[1] == "delete" and self.command == "POST":
            data = self._json_body(body, {"actor"})
            self._json(200, self.app.oauth_service.delete_credential(
                rest[0], require_string(data["actor"], "actor")))
            return True

        if resource == "accounts" and len(rest) == 2 and rest[0].isdigit():
            account_id, action = int(rest[0]), rest[1]
            if action == "verify" and self.command == "POST":
                data = self._json_body(body, {"actor"})
                self._verify_account(account_id, require_string(data["actor"], "actor"))
                return True
            if action == "quota" and self.command == "GET":
                self._json(200, {"policies": repo.list_quota_policies(account_id)})
                return True
            if action == "quota" and self.command == "POST":
                data = self._json_body(body, {"operation_type", "window_seconds", "max_operations", "actor"})
                for key in ("window_seconds", "max_operations"):
                    if type(data[key]) is not int:
                        raise ValidationError(f"{key} must be an integer")
                policy = repo.upsert_quota_policy(
                    account_id, require_string(data["operation_type"], "operation_type"),
                    data["window_seconds"], data["max_operations"], require_string(data["actor"], "actor"))
                self._json(200, policy)
                return True
            if action == "metadata" and self.command == "PATCH":
                data = self._json_body(body, {"actor"}, {"display_name", "x_user_id", "metadata"})
                metadata = data.get("metadata")
                if metadata is not None and not isinstance(metadata, dict):
                    raise ValidationError("metadata must be an object")
                account = repo.update_account_metadata(
                    account_id, require_string(data["actor"], "actor"),
                    require_string(data["display_name"], "display_name") if "display_name" in data else None,
                    self._optional_string(data.get("x_user_id"), "x_user_id") if "x_user_id" in data else None,
                    metadata)
                self._json(200, account)
                return True
            if action in {"enable", "disable", "pause", "resume"} and self.command == "POST":
                data = self._json_body(body, {"actor"})
                actor = require_string(data["actor"], "actor")
                if action in {"enable", "resume"}:
                    current = repo.get_account(account_id)
                    if current.get("authorization_status") != "verified" or not current.get("x_user_id"):
                        raise StateError("account must complete OAuth identity verification first", "identity_unverified")
                if action in {"enable", "disable"}:
                    account = repo.set_account_enabled(account_id, action == "enable", actor)
                else:
                    account = repo.set_account_paused(account_id, action == "pause", actor)
                self._json(200, account)
                return True
            return False

        if resource == "requests" and rest and rest[0].isdigit():
            request_id = int(rest[0])
            if len(rest) == 1 and self.command == "GET":
                self._json(200, repo.get_request(request_id) | {"approvals": repo.list_approvals(request_id)})
                return True
            if len(rest) == 2 and self.command == "POST":
                action = rest[1]
                if action == "submit":
                    data = self._json_body(body, {"actor"})
                    self._json(200, repo.submit_request(request_id, require_string(data["actor"], "actor")))
                    return True
                if action == "approve":
                    data = self._json_body(body, {"actor", "content_hash", "request_version"}, {"reason"})
                    if type(data["request_version"]) is not int:
                        raise ValidationError("request_version must be an integer")
                    result = repo.approve_request(request_id, require_string(data["actor"], "actor"), {
                        "content_hash": require_string(data["content_hash"], "content_hash"),
                        "request_version": data["request_version"],
                        "reason": str(data.get("reason") or ""),
                    }, approval_ttl_seconds=self.app.config.approval_ttl_seconds)
                    self._json(200, result)
                    return True
                if action == "cancel":
                    data = self._json_body(body, {"actor"})
                    self._json(200, repo.cancel_request(request_id, require_string(data["actor"], "actor")))
                    return True
            return False

        if resource == "operations" and rest and rest[0].isdigit():
            operation_id = int(rest[0])
            if len(rest) == 1 and self.command == "GET":
                self._json(200, repo.get_operation(operation_id))
                return True
            if len(rest) == 2 and self.command == "POST":
                action = rest[1]
                if action == "approve-next-step":
                    data = self._json_body(body, {"actor", "content_hash"}, {"reason"})
                    self._json(200, repo.approve_next_step(operation_id, require_string(data["actor"], "actor"), {
                        "content_hash": require_string(data["content_hash"], "content_hash"),
                        "reason": str(data.get("reason") or ""),
                    }))
                    return True
                if action == "reconcile":
                    data = self._json_body(body, {"actor", "outcome", "note"})
                    outcome = require_string(data["outcome"], "outcome")
                    if outcome not in {"succeeded", "failed"}:
                        raise ValidationError("outcome must be 'succeeded' or 'failed'")
                    self._json(200, repo.reconcile_operation(
                        operation_id, outcome, require_string(data["note"], "note"),
                        require_string(data["actor"], "actor")))
                    return True
            return False

        if resource == "media-assets" and rest and rest[0] != "upload":
            asset_key = rest[0]
            if len(rest) == 2 and rest[1] == "upload" and self.command == "POST":
                self._upload_media_bytes(asset_key, body)
                return True
            return False
        return False

    def _verify_account(self, account_id: int, actor: str) -> None:
        repo = self.app.repository
        executor = self.app.executor
        if executor is None:
            raise StateError("executor is disabled; account verification is unavailable", "executor_disabled")
        account = repo.get_account(account_id)
        try:
            client = executor._client_for(account)
            prepare = getattr(client, "prepare_for_request", None)
            if callable(prepare):
                prepare()
            verified = client.verify_account()
        except CredentialError as exc:
            repo.record_verification_failure(account_id, actor, exc.code)
            raise StateError(str(exc), exc.code) from exc
        except XAPIError as exc:
            repo.record_verification_failure(account_id, actor, exc.code)
            raise StateError(str(exc), exc.code) from exc
        self._json(200, repo.record_verification(account_id, actor, verified))

    # ---------- helpers ----------

    def _read_body(self, max_bytes: int | None = None) -> bytes:
        cap = max_bytes if max_bytes is not None else self.app.config.max_body_bytes
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValidationError("invalid Content-Length") from exc
        if length < 0 or length > cap:
            raise ValidationError("request body too large")
        return self.rfile.read(length) if length else b""

    def _raw_body(self, max_bytes: int) -> bytes:
        """Read a raw (non-JSON) body up to ``max_bytes``. Kept for explicit
        callers; the dispatch path now reads the upload body up front."""
        return self._read_body(max_bytes)

    def _media_storage_dir(self) -> Path:
        import os
        base = self.app.config.media_storage_dir or os.path.join(
            os.path.dirname(os.path.abspath(self.app.config.database_path)), "media_assets")
        path = Path(base)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _register_media_asset(self, body: bytes) -> None:
        data = self._json_body(body, {"sha256", "mime_type", "byte_size", "actor"},
                               {"account_id", "asset_key", "bytes_base64", "metadata"})
        sha256 = require_string(data["sha256"], "sha256")
        mime_type = require_string(data["mime_type"], "mime_type")
        if type(data["byte_size"]) is not int or data["byte_size"] < 0:
            raise ValidationError("byte_size must be a non-negative integer")
        account_id = data.get("account_id")
        if account_id is not None and type(account_id) is not int:
            raise ValidationError("account_id must be an integer")
        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise ValidationError("metadata must be an object")
        local_path: str | None = None
        bytes_b64 = data.get("bytes_base64")
        if bytes_b64 is not None:
            if not isinstance(bytes_b64, str):
                raise ValidationError("bytes_base64 must be a string")
            import base64
            try:
                raw = base64.b64decode(bytes_b64, validate=True)
            except Exception as exc:
                raise ValidationError("bytes_base64 is not valid base64") from exc
            asset_key = data.get("asset_key") or __import__("secrets").token_hex(16)
            local_path = str(self._media_storage_dir() / f"{asset_key}.bin")
            with open(local_path, "wb") as handle:
                handle.write(raw)
        asset = self.app.repository.register_media_asset(
            asset_key=data.get("asset_key"),
            account_id=account_id,
            sha256=sha256,
            mime_type=mime_type,
            byte_size=data["byte_size"],
            local_path=local_path,
            metadata=metadata,
            actor=require_string(data["actor"], "actor"),
        )
        # Never echo the file bytes back; only the asset row.
        self._json(201, {"media_asset": asset})

    def _upload_media_bytes(self, asset_key: str, raw: bytes) -> None:
        import hashlib
        repo = self.app.repository
        conn = repo.db.connect()
        try:
            row = conn.execute(
                "SELECT id FROM media_assets WHERE asset_key=?", (asset_key,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise NotFoundError("media asset not found")
        asset_id = int(row["id"])
        local_path = str(self._media_storage_dir() / f"{asset_key}.bin")
        with open(local_path, "wb") as handle:
            handle.write(raw)
        asset = repo.set_media_local_bytes(
            asset_id, sha256=hashlib.sha256(raw).hexdigest(),
            byte_size=len(raw), local_path=local_path,
        )
        self._json(200, {"media_asset": asset})

    def _authenticate(self, path: str, body: bytes) -> None:
        values = AuthHeaders(
            self.headers.get("X-Internal-Timestamp", ""),
            self.headers.get("X-Internal-Nonce", ""),
            self.headers.get("X-Internal-Signature", ""),
        )
        self.app.authenticator.verify(values, self.command, path, body)

    @staticmethod
    def _json_body(body: bytes, required: set[str], optional: set[str] = set()) -> dict[str, Any]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("body must be valid JSON") from exc
        return validate_object(value, required, optional)

    @staticmethod
    def _optional_string(value: Any, field: str) -> str | None:
        if value is None:
            return None
        return require_string(value, field)

    @staticmethod
    def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
        raw = query.get(name, [None])[0]
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ValidationError(f"{name} must be an integer") from exc

    @staticmethod
    def _str_query(query: dict[str, list[str]], name: str) -> str | None:
        return query.get(name, [None])[0]

    @staticmethod
    def _int_query_or_none(query: dict[str, list[str]], name: str) -> int | None:
        raw = query.get(name, [None])[0]
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError as exc:
            raise ValidationError(f"{name} must be an integer") from exc

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def _version() -> str:
    from . import __version__
    return __version__


class XWriteHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, config: Config, repository: Repository, authenticator: HMACAuthenticator,
                 executor: Executor | None = None, credential_store: CredentialStore | None = None,
                 oauth_service: OAuthService | None = None):
        self.config = config
        self.repository = repository
        self.authenticator = authenticator
        self.executor = executor
        self.credential_store = credential_store or (
            executor.credential_store if executor is not None else CredentialStore(config.secrets_path)
        )

        def client_factory(credentials):
            if executor is not None:
                return executor.client_factory(credentials)
            return XAPIClient(
                credentials,
                base_url=config.x_api_base_url,
                proxy_url=config.x_api_proxy_url,
                timeout_seconds=config.x_api_timeout_seconds,
                token_url=config.oauth_token_url,
                refresh_leeway_seconds=config.oauth_refresh_leeway_seconds,
                credential_reloader=self.credential_store.resolve,
                token_persister=self.credential_store.update_oauth2_tokens,
            )

        self.oauth_service = oauth_service or OAuthService(
            config, repository, self.credential_store, client_factory,
        )
        super().__init__((config.bind_host, config.bind_port), ServiceHandler)


def build_server(config: Config, repository: Repository, authenticator: HMACAuthenticator,
                 executor: Executor | None = None, credential_store: CredentialStore | None = None,
                 oauth_service: OAuthService | None = None) -> XWriteHTTPServer:
    return XWriteHTTPServer(
        config, repository, authenticator, executor, credential_store, oauth_service,
    )
