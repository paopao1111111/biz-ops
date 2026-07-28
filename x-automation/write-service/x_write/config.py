from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


_ALLOWED = {
    "database_path",
    "hmac_secret",
    "bind_host",
    "bind_port",
    "max_body_bytes",
    "max_media_upload_bytes",
    "media_storage_dir",
    "auth_max_skew_seconds",
    "nonce_ttl_seconds",
    "secrets_path",
    "x_api_base_url",
    "x_api_media_upload_url",
    "x_api_proxy_url",
    "x_api_timeout_seconds",
    "executor_enabled",
    "executor_tick_seconds",
    "operation_lease_seconds",
    "verify_ttl_seconds",
    "approval_ttl_seconds",
    "oauth_callback_url",
    "oauth_authorize_url",
    "oauth_token_url",
    "oauth_flow_ttl_seconds",
    "oauth_refresh_leeway_seconds",
}


@dataclass(frozen=True)
class Config:
    database_path: str
    hmac_secret: str
    bind_host: str = "127.0.0.1"
    bind_port: int = 8765
    max_body_bytes: int = 65536
    max_media_upload_bytes: int = 8 * 1024 * 1024
    media_storage_dir: str | None = None
    auth_max_skew_seconds: int = 300
    nonce_ttl_seconds: int = 600
    secrets_path: str | None = None
    x_api_base_url: str = "https://api.x.com"
    x_api_media_upload_url: str = "https://upload.x.com"
    x_api_proxy_url: str | None = None
    x_api_timeout_seconds: float = 20.0
    executor_enabled: bool = True
    executor_tick_seconds: float = 5.0
    operation_lease_seconds: int = 300
    verify_ttl_seconds: int = 3600
    approval_ttl_seconds: int = 3600
    oauth_callback_url: str | None = None
    oauth_authorize_url: str = "https://x.com/i/oauth2/authorize"
    oauth_token_url: str = "https://api.x.com/2/oauth2/token"
    oauth_flow_ttl_seconds: int = 600
    oauth_refresh_leeway_seconds: int = 300

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "Config":
        extra = set(raw) - _ALLOWED
        if extra:
            raise ConfigError(f"unknown config fields: {', '.join(sorted(extra))}")
        missing = {"database_path", "hmac_secret"} - set(raw)
        if missing:
            raise ConfigError(f"missing config fields: {', '.join(sorted(missing))}")
        values = dict(raw)
        for key in ("database_path", "hmac_secret", "bind_host", "x_api_base_url", "oauth_authorize_url", "oauth_token_url"):
            if key in values and (not isinstance(values[key], str) or not values[key]):
                raise ConfigError(f"{key} must be a non-empty string")
        for key in ("secrets_path", "x_api_proxy_url", "oauth_callback_url", "media_storage_dir"):
            if key in values and values[key] is not None and not isinstance(values[key], str):
                raise ConfigError(f"{key} must be a string or null")
        if values.get("bind_host", "127.0.0.1") not in ("127.0.0.1", "localhost", "::1"):
            raise ConfigError("bind_host must be loopback only; the write service must not listen externally")
        if len(values["hmac_secret"].encode("utf-8")) < 32:
            raise ConfigError("hmac_secret must be at least 32 bytes")
        for key, low, high in (
            ("bind_port", 0, 65535),
            ("max_body_bytes", 1, 10 * 1024 * 1024),
            ("max_media_upload_bytes", 1, 64 * 1024 * 1024),
            ("auth_max_skew_seconds", 1, 3600),
            ("nonce_ttl_seconds", 1, 86400),
            ("operation_lease_seconds", 30, 3600),
            ("verify_ttl_seconds", 60, 86400),
            ("approval_ttl_seconds", 60, 86400),
            ("oauth_flow_ttl_seconds", 60, 3600),
            ("oauth_refresh_leeway_seconds", 30, 3600),
        ):
            if key in values and (type(values[key]) is not int or not low <= values[key] <= high):
                raise ConfigError(f"{key} must be an integer between {low} and {high}")
        for key in ("x_api_timeout_seconds", "executor_tick_seconds"):
            if key in values and not (0.5 <= float(values[key]) <= 600):
                raise ConfigError(f"{key} must be between 0.5 and 600 seconds")
        if "executor_enabled" in values and type(values["executor_enabled"]) is not bool:
            raise ConfigError("executor_enabled must be a boolean")
        base = values.get("x_api_base_url", "https://api.x.com")
        if not str(base).startswith("https://"):
            raise ConfigError("x_api_base_url must use https://")
        media_upload = values.get("x_api_media_upload_url", "https://upload.x.com")
        if not str(media_upload).startswith("https://"):
            raise ConfigError("x_api_media_upload_url must use https://")
        for key, default in (
            ("oauth_authorize_url", "https://x.com/i/oauth2/authorize"),
            ("oauth_token_url", "https://api.x.com/2/oauth2/token"),
        ):
            if not str(values.get(key, default)).startswith("https://"):
                raise ConfigError(f"{key} must use https://")
        callback = values.get("oauth_callback_url")
        if callback is not None and not callback.startswith("https://"):
            raise ConfigError("oauth_callback_url must use https://")
        return cls(**values)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot load config: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config root must be an object")
        return cls.from_mapping(raw)

    def public_metadata(self) -> dict[str, Any]:
        return {
            "database_path": self.database_path,
            "bind_host": self.bind_host,
            "bind_port": self.bind_port,
            "max_body_bytes": self.max_body_bytes,
            "max_media_upload_bytes": self.max_media_upload_bytes,
            "auth_max_skew_seconds": self.auth_max_skew_seconds,
            "nonce_ttl_seconds": self.nonce_ttl_seconds,
            "executor_enabled": self.executor_enabled,
            "x_api_base_url": self.x_api_base_url,
            "x_api_proxy_configured": bool(self.x_api_proxy_url),
            "secrets_configured": bool(self.secrets_path),
            "oauth_callback_url": self.oauth_callback_url,
            "oauth2_callback_configured": bool(self.oauth_callback_url),
            "hmac_configured": True,
        }
