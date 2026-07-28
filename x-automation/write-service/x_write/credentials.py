from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, TypeVar, Union


class CredentialError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AccountCredentials:
    """OAuth 1.0a user-context credentials.

    The first four fields intentionally preserve the original constructor used by
    the existing client and tests.
    """

    consumer_key: str
    consumer_secret: str
    access_token: str
    access_token_secret: str
    credential_ref: str = ""
    developer_app: str = ""
    generation: int = 1
    x_user_id: str = ""
    username: str = ""

    @property
    def auth_type(self) -> str:
        return "oauth1"

    @property
    def complete(self) -> bool:
        return all((self.consumer_key, self.consumer_secret, self.access_token, self.access_token_secret))


@dataclass(frozen=True)
class OAuth2AppCredentials:
    name: str
    client_id: str
    client_secret: str
    redirect_uri: str
    generation: int = 1
    updated_at: int = 0

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.redirect_uri)


@dataclass(frozen=True)
class OAuth2Credentials:
    client_id: str
    client_secret: str
    access_token: str
    refresh_token: str
    expires_at: int
    scopes: tuple[str, ...]
    credential_ref: str = ""
    developer_app: str = "primary"
    generation: int = 1
    x_user_id: str = ""
    username: str = ""

    @property
    def auth_type(self) -> str:
        return "oauth2"

    @property
    def complete(self) -> bool:
        return bool(self.client_id and self.access_token and self.refresh_token and self.expires_at and self.scopes)

    def with_tokens(self, *, access_token: str, refresh_token: str, expires_at: int,
                    scopes: tuple[str, ...] | None = None) -> "OAuth2Credentials":
        return replace(
            self,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes if scopes is not None else self.scopes,
        )


ResolvedCredentials = Union[AccountCredentials, OAuth2Credentials]
T = TypeVar("T")
_REF_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_APP_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class CredentialStore:
    """Reads and atomically updates the service-user-owned credential store.

    Schema v2 separates developer applications from per-account authorization.
    Legacy ``developer_apps``/``accounts`` files remain readable and are
    normalized in memory; the first successful mutation writes schema v2.

    Secret values are never returned by list/summary methods. The store must be
    owned by the service user and mode 0600. Mutations use a process lock, an
    advisory file lock, same-directory temporary files, fsync, and os.replace.
    """

    SCHEMA_VERSION = 2
    _process_lock = threading.RLock()

    def __init__(self, path: str | None):
        self.path = path

    # ---------- public safe summaries ----------

    def list_refs(self) -> list[dict[str, Any]]:
        if not self.path:
            return []
        try:
            raw = self._load(require_exists=True)
        except CredentialError:
            return []
        refs: list[dict[str, Any]] = []
        for name, entry in sorted(raw["credentials"].items()):
            if not isinstance(entry, dict):
                continue
            auth_type = str(entry.get("auth_type") or "")
            app_name = str(entry.get("developer_app") or "")
            complete = False
            if auth_type == "oauth1":
                app = raw["oauth1_apps"].get(app_name)
                complete = bool(
                    isinstance(app, dict)
                    and app.get("consumer_key") and app.get("consumer_secret")
                    and entry.get("access_token") and entry.get("access_token_secret")
                )
            elif auth_type == "oauth2":
                app = raw["oauth2_apps"].get(app_name)
                complete = bool(
                    isinstance(app, dict) and app.get("client_id")
                    and entry.get("access_token") and entry.get("refresh_token")
                    and int(entry.get("expires_at") or 0) > 0
                )
            refs.append({
                "ref": name,
                "auth_type": auth_type,
                "developer_app": app_name,
                "complete": complete,
                "status": "ready" if complete else "incomplete",
                "scopes": self._safe_scopes(entry.get("scopes")),
                "expires_at": int(entry.get("expires_at") or 0) or None,
                "x_user_id": str(entry.get("x_user_id") or "") or None,
                "username": str(entry.get("username") or "") or None,
                "generation": max(1, int(entry.get("generation") or 1)),
                "updated_at": int(entry.get("updated_at") or 0) or None,
            })
        return refs

    def oauth2_app_summary(self, name: str = "primary") -> dict[str, Any]:
        self._validate_app_name(name)
        if not self.path:
            return self._empty_app_summary(name)
        try:
            raw = self._load(require_exists=True)
        except CredentialError:
            return self._empty_app_summary(name)
        app = raw["oauth2_apps"].get(name)
        if not isinstance(app, dict):
            return self._empty_app_summary(name)
        client_id = str(app.get("client_id") or "")
        return {
            "name": name,
            "configured": bool(client_id and app.get("redirect_uri")),
            "client_id_masked": self._mask(client_id),
            "client_secret_configured": bool(app.get("client_secret")),
            "redirect_uri": str(app.get("redirect_uri") or ""),
            "generation": max(1, int(app.get("generation") or 1)),
            "updated_at": int(app.get("updated_at") or 0) or None,
        }

    def migration_needed(self) -> bool:
        if not self.path:
            return False
        try:
            raw = self._read_json(require_exists=True)
        except CredentialError:
            return False
        return int(raw.get("schema_version") or 0) != self.SCHEMA_VERSION

    # ---------- internal resolution ----------

    def resolve(self, credential_ref: str) -> ResolvedCredentials:
        self._validate_ref(credential_ref)
        raw = self._load(require_exists=True)
        entry = raw["credentials"].get(credential_ref)
        if not isinstance(entry, dict):
            raise CredentialError("credential_ref_missing", "account credential reference not found")
        auth_type = str(entry.get("auth_type") or "")
        app_name = str(entry.get("developer_app") or "")
        generation = max(1, int(entry.get("generation") or 1))
        if auth_type == "oauth1":
            app = raw["oauth1_apps"].get(app_name)
            if not isinstance(app, dict):
                raise CredentialError("developer_app_missing", "developer app credentials not found")
            creds = AccountCredentials(
                consumer_key=str(app.get("consumer_key") or ""),
                consumer_secret=str(app.get("consumer_secret") or ""),
                access_token=str(entry.get("access_token") or ""),
                access_token_secret=str(entry.get("access_token_secret") or ""),
                credential_ref=credential_ref,
                developer_app=app_name,
                generation=generation,
                x_user_id=str(entry.get("x_user_id") or ""),
                username=str(entry.get("username") or ""),
            )
        elif auth_type == "oauth2":
            app = raw["oauth2_apps"].get(app_name)
            if not isinstance(app, dict):
                raise CredentialError("developer_app_missing", "developer app credentials not found")
            creds = OAuth2Credentials(
                client_id=str(app.get("client_id") or ""),
                client_secret=str(app.get("client_secret") or ""),
                access_token=str(entry.get("access_token") or ""),
                refresh_token=str(entry.get("refresh_token") or ""),
                expires_at=int(entry.get("expires_at") or 0),
                scopes=tuple(self._safe_scopes(entry.get("scopes"))),
                credential_ref=credential_ref,
                developer_app=app_name,
                generation=generation,
                x_user_id=str(entry.get("x_user_id") or ""),
                username=str(entry.get("username") or ""),
            )
        else:
            raise CredentialError("credentials_invalid", "credential authorization type is invalid")
        if not creds.complete:
            raise CredentialError("credentials_incomplete", "account credentials are incomplete")
        return creds

    def get_oauth2_app(self, name: str = "primary") -> OAuth2AppCredentials:
        self._validate_app_name(name)
        raw = self._load(require_exists=True)
        app = raw["oauth2_apps"].get(name)
        if not isinstance(app, dict):
            raise CredentialError("oauth2_app_missing", "OAuth 2.0 developer app is not configured")
        result = OAuth2AppCredentials(
            name=name,
            client_id=str(app.get("client_id") or ""),
            client_secret=str(app.get("client_secret") or ""),
            redirect_uri=str(app.get("redirect_uri") or ""),
            generation=max(1, int(app.get("generation") or 1)),
            updated_at=int(app.get("updated_at") or 0),
        )
        if not result.complete:
            raise CredentialError("oauth2_app_incomplete", "OAuth 2.0 developer app is incomplete")
        return result

    # ---------- safe mutations ----------

    def upsert_oauth2_app(self, name: str, client_id: str, client_secret: str | None,
                          redirect_uri: str) -> dict[str, Any]:
        self._validate_app_name(name)
        if not isinstance(client_id, str) or not client_id.strip():
            raise CredentialError("invalid_client_id", "client_id must be a non-empty string")
        if not isinstance(redirect_uri, str) or not redirect_uri.startswith("https://"):
            raise CredentialError("invalid_redirect_uri", "redirect_uri must use https://")
        if client_secret is not None and not isinstance(client_secret, str):
            raise CredentialError("invalid_client_secret", "client_secret must be a string")
        now = int(time.time())

        def mutate(raw: dict[str, Any]) -> None:
            old = raw["oauth2_apps"].get(name) or {}
            secret = str(client_secret) if client_secret is not None else str(old.get("client_secret") or "")
            raw["oauth2_apps"][name] = {
                "client_id": client_id.strip(),
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "generation": max(1, int(old.get("generation") or 0) + 1),
                "updated_at": now,
            }

        self._mutate(mutate)
        return self.oauth2_app_summary(name)

    def save_oauth1_credential(self, credential_ref: str, *, consumer_key: str,
                               consumer_secret: str, access_token: str,
                               access_token_secret: str, x_user_id: str,
                               username: str) -> dict[str, Any]:
        self._validate_ref(credential_ref)
        values = (consumer_key, consumer_secret, access_token, access_token_secret, x_user_id)
        if any(not isinstance(value, str) or not value for value in values):
            raise CredentialError("credentials_incomplete", "OAuth 1.0a credentials are incomplete")
        app_name = f"manual-{credential_ref}"
        self._validate_app_name(app_name)
        now = int(time.time())

        def mutate(raw: dict[str, Any]) -> None:
            old = raw["credentials"].get(credential_ref) or {}
            old_identity = str(old.get("x_user_id") or "")
            if old_identity and old_identity != str(x_user_id):
                raise CredentialError("identity_mismatch", "credential is already bound to another X user")
            generation = max(1, int(old.get("generation") or 0) + 1)
            raw["oauth1_apps"][app_name] = {
                "consumer_key": consumer_key,
                "consumer_secret": consumer_secret,
                "updated_at": now,
            }
            raw["credentials"][credential_ref] = {
                "auth_type": "oauth1",
                "developer_app": app_name,
                "access_token": access_token,
                "access_token_secret": access_token_secret,
                "x_user_id": str(x_user_id),
                "username": str(username or ""),
                "generation": generation,
                "updated_at": now,
            }

        self._mutate(mutate)
        return self._summary_for_ref(credential_ref)

    def save_oauth2_credential(self, credential_ref: str, *, developer_app: str,
                               access_token: str, refresh_token: str, expires_at: int,
                               scopes: list[str] | tuple[str, ...], x_user_id: str,
                               username: str) -> dict[str, Any]:
        self._validate_ref(credential_ref)
        self._validate_app_name(developer_app)
        if any(not isinstance(value, str) or not value for value in (access_token, refresh_token, x_user_id)):
            raise CredentialError("credentials_incomplete", "OAuth 2.0 credentials are incomplete")
        if type(expires_at) is not int or expires_at <= 0:
            raise CredentialError("credentials_invalid", "OAuth 2.0 expiry is invalid")
        clean_scopes = self._safe_scopes(scopes)
        if not clean_scopes:
            raise CredentialError("credentials_incomplete", "OAuth 2.0 scopes are missing")
        now = int(time.time())

        def mutate(raw: dict[str, Any]) -> None:
            if developer_app not in raw["oauth2_apps"]:
                raise CredentialError("oauth2_app_missing", "OAuth 2.0 developer app is not configured")
            old = raw["credentials"].get(credential_ref) or {}
            old_identity = str(old.get("x_user_id") or "")
            if old_identity and old_identity != str(x_user_id):
                raise CredentialError("identity_mismatch", "credential is already bound to another X user")
            generation = max(1, int(old.get("generation") or 0) + 1)
            raw["credentials"][credential_ref] = {
                "auth_type": "oauth2",
                "developer_app": developer_app,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
                "scopes": clean_scopes,
                "x_user_id": str(x_user_id),
                "username": str(username or ""),
                "generation": generation,
                "updated_at": now,
            }

        self._mutate(mutate)
        return self._summary_for_ref(credential_ref)

    def update_oauth2_tokens(self, credential_ref: str, *, expected_generation: int,
                             access_token: str, refresh_token: str, expires_at: int,
                             scopes: list[str] | tuple[str, ...]) -> OAuth2Credentials:
        self._validate_ref(credential_ref)
        clean_scopes = self._safe_scopes(scopes)
        if not access_token or not refresh_token or expires_at <= 0 or not clean_scopes:
            raise CredentialError("token_refresh_invalid", "refreshed OAuth 2.0 token response is incomplete")

        def mutate(raw: dict[str, Any]) -> None:
            entry = raw["credentials"].get(credential_ref)
            if not isinstance(entry, dict) or entry.get("auth_type") != "oauth2":
                raise CredentialError("credential_ref_missing", "OAuth 2.0 credential reference not found")
            if int(entry.get("generation") or 1) != int(expected_generation):
                raise CredentialError("credential_changed", "credential changed while token refresh was in progress")
            entry.update({
                "access_token": access_token,
                "refresh_token": refresh_token,
                "expires_at": int(expires_at),
                "scopes": clean_scopes,
                "updated_at": int(time.time()),
            })

        self._mutate(mutate)
        resolved = self.resolve(credential_ref)
        if not isinstance(resolved, OAuth2Credentials):
            raise CredentialError("credentials_invalid", "refreshed credential has the wrong authorization type")
        return resolved

    def delete_credential(self, credential_ref: str) -> dict[str, Any]:
        self._validate_ref(credential_ref)
        deleted: dict[str, Any] = {}

        def mutate(raw: dict[str, Any]) -> None:
            entry = raw["credentials"].get(credential_ref)
            if not isinstance(entry, dict):
                raise CredentialError("credential_ref_missing", "account credential reference not found")
            deleted.update({
                "ref": credential_ref,
                "auth_type": str(entry.get("auth_type") or ""),
                "developer_app": str(entry.get("developer_app") or ""),
            })
            del raw["credentials"][credential_ref]
            app_name = deleted["developer_app"]
            if deleted["auth_type"] == "oauth1" and not any(
                    item.get("developer_app") == app_name for item in raw["credentials"].values()
                    if isinstance(item, dict)):
                raw["oauth1_apps"].pop(app_name, None)

        self._mutate(mutate)
        return deleted

    # ---------- file handling ----------

    def _summary_for_ref(self, credential_ref: str) -> dict[str, Any]:
        for entry in self.list_refs():
            if entry["ref"] == credential_ref:
                return entry
        raise CredentialError("credential_ref_missing", "account credential reference not found")

    def _mutate(self, mutator: Callable[[dict[str, Any]], T]) -> T:
        if not self.path:
            raise CredentialError("credentials_not_configured", "no credential store is configured")
        path = Path(self.path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(path.name + ".lock")
        with self._process_lock:
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    raw = self._load(require_exists=False)
                    before = self._encode(raw)
                    working = copy.deepcopy(raw)
                    result = mutator(working)
                    self._validate_v2(working)
                    encoded = self._encode(working)
                    if path.exists():
                        self._atomic_write(path.with_name(path.name + ".bak"), before)
                    self._atomic_write(path, encoded)
                    # Re-read with all permission/schema checks before reporting success.
                    self._load(require_exists=True)
                    return result
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _load(self, *, require_exists: bool) -> dict[str, Any]:
        raw = self._read_json(require_exists=require_exists)
        if not raw:
            return self._empty_store()
        if int(raw.get("schema_version") or 0) == self.SCHEMA_VERSION:
            self._validate_v2(raw)
            return raw
        normalized = self._normalize_legacy(raw)
        self._validate_v2(normalized)
        return normalized

    def _read_json(self, *, require_exists: bool) -> dict[str, Any]:
        if not self.path:
            if require_exists:
                raise CredentialError("credentials_not_configured", "no credential store is configured")
            return {}
        path = Path(self.path)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            if require_exists:
                raise CredentialError("credentials_unavailable", "credential store is not readable")
            return {}
        except OSError as exc:
            raise CredentialError("credentials_unavailable", "credential store is not readable") from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise CredentialError("credentials_insecure_path", "credential store must be a regular file")
        if stat.S_IMODE(st.st_mode) & 0o077:
            raise CredentialError("credentials_insecure_permissions", "credential store must not be readable by group/others")
        if st.st_uid != os.geteuid():
            raise CredentialError("credentials_insecure_owner", "credential store must be owned by the service user")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            try:
                with os.fdopen(fd, "r", encoding="utf-8") as fh:
                    value = json.load(fh)
                fd = -1
            finally:
                if fd >= 0:
                    os.close(fd)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError("credentials_invalid", "credential store is not valid JSON") from exc
        if not isinstance(value, dict):
            raise CredentialError("credentials_invalid", "credential store root must be an object")
        return value

    @classmethod
    def _normalize_legacy(cls, raw: dict[str, Any]) -> dict[str, Any]:
        apps = raw.get("developer_apps") or {}
        accounts = raw.get("accounts") or {}
        if not isinstance(apps, dict) or not isinstance(accounts, dict):
            raise CredentialError("credentials_invalid", "legacy credential store shape is invalid")
        result = cls._empty_store()
        for name, app in apps.items():
            if isinstance(name, str) and isinstance(app, dict):
                result["oauth1_apps"][name] = {
                    "consumer_key": str(app.get("consumer_key") or ""),
                    "consumer_secret": str(app.get("consumer_secret") or ""),
                    "updated_at": 0,
                }
        for ref, entry in accounts.items():
            if not isinstance(ref, str) or not isinstance(entry, dict):
                continue
            result["credentials"][ref] = {
                "auth_type": "oauth1",
                "developer_app": str(entry.get("developer_app") or ""),
                "access_token": str(entry.get("access_token") or ""),
                "access_token_secret": str(entry.get("access_token_secret") or ""),
                "x_user_id": str(entry.get("x_user_id") or ""),
                "username": str(entry.get("username") or ""),
                "generation": max(1, int(entry.get("generation") or 1)),
                "updated_at": int(entry.get("updated_at") or 0),
            }
        return result

    @classmethod
    def _validate_v2(cls, raw: dict[str, Any]) -> None:
        if set(raw) != {"schema_version", "oauth1_apps", "oauth2_apps", "credentials"}:
            raise CredentialError("credentials_invalid", "credential store has unknown or missing top-level fields")
        if raw.get("schema_version") != cls.SCHEMA_VERSION:
            raise CredentialError("credentials_invalid", "credential store schema version is unsupported")
        for key in ("oauth1_apps", "oauth2_apps", "credentials"):
            if not isinstance(raw.get(key), dict):
                raise CredentialError("credentials_invalid", f"credential store {key} must be an object")
        for name in raw["oauth1_apps"]:
            cls._validate_app_name(name)
        for name in raw["oauth2_apps"]:
            cls._validate_app_name(name)
        for ref, entry in raw["credentials"].items():
            cls._validate_ref(ref)
            if not isinstance(entry, dict) or entry.get("auth_type") not in {"oauth1", "oauth2"}:
                raise CredentialError("credentials_invalid", "credential entry is invalid")
            cls._safe_scopes(entry.get("scopes"))

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        try:
            existing = os.lstat(path)
            if stat.S_ISLNK(existing.st_mode):
                raise CredentialError("credentials_insecure_path", "credential store target must not be a symlink")
        except FileNotFoundError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fd = -1
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    @classmethod
    def _empty_store(cls) -> dict[str, Any]:
        return {"schema_version": cls.SCHEMA_VERSION, "oauth1_apps": {}, "oauth2_apps": {}, "credentials": {}}

    @staticmethod
    def _encode(raw: dict[str, Any]) -> bytes:
        return (json.dumps(raw, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def _safe_scopes(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, (list, tuple)):
            raise CredentialError("credentials_invalid", "OAuth scopes must be a list")
        result: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 80 or any(ch.isspace() for ch in item):
                raise CredentialError("credentials_invalid", "OAuth scope value is invalid")
            if item not in result:
                result.append(item)
        return sorted(result)

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "•" * len(value)
        return f"{value[:4]}…{value[-4:]}"

    @staticmethod
    def _empty_app_summary(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "configured": False,
            "client_id_masked": "",
            "client_secret_configured": False,
            "redirect_uri": "",
            "generation": 0,
            "updated_at": None,
        }

    @staticmethod
    def _validate_ref(value: str) -> None:
        if not isinstance(value, str) or not _REF_RE.fullmatch(value):
            raise CredentialError("invalid_credential_ref", "credential reference is invalid")

    @staticmethod
    def _validate_app_name(value: str) -> None:
        if not isinstance(value, str) or not _APP_RE.fullmatch(value):
            raise CredentialError("invalid_developer_app", "developer app reference is invalid")
