"""Feishu interactive-card sender used by unified monitoring."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import requests

from .config import get_feishu_config, load_env_files

logger = logging.getLogger(__name__)

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_TOKEN_CACHE = {"token": "", "expires_at": 0.0, "app_id": ""}


def get_tenant_token(app_id: str, app_secret: str) -> str:
    now = time.time()
    if (
        _TOKEN_CACHE.get("token")
        and _TOKEN_CACHE.get("app_id") == app_id
        and now < float(_TOKEN_CACHE.get("expires_at") or 0)
    ):
        return str(_TOKEN_CACHE["token"])

    session = requests.Session()
    session.trust_env = False
    resp = session.post(TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret}, timeout=(5, 15))
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Feishu auth failed: {data.get('msg') or data}")
    token = data["tenant_access_token"]
    expire = int(data.get("expire") or 7200)
    _TOKEN_CACHE.update({"token": token, "expires_at": now + expire - 300, "app_id": app_id})
    return token


class FeishuMonitoringSender:
    def __init__(self, config: dict[str, str] | None = None):
        load_env_files()
        self.config = config or get_feishu_config()

    def configured(self) -> bool:
        return bool(self.config.get("app_id") and self.config.get("app_secret") and self.config.get("chat_id"))

    def send_card(self, card: dict[str, Any]) -> dict[str, Any]:
        """Send a raw Feishu interactive card.

        Returns a small result dict and raises only for programming errors. Network/API
        failures are captured in the result so callers can keep business flows alive.
        """
        if not self.configured():
            return {"sent": False, "error": "Feishu config incomplete", "config": self.redacted_config()}
        try:
            token = get_tenant_token(self.config["app_id"], self.config["app_secret"])
            session = requests.Session()
            session.trust_env = False
            resp = session.post(
                MESSAGE_URL,
                params={"receive_id_type": "chat_id"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                json={
                    "receive_id": self.config["chat_id"],
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False),
                },
                timeout=(5, 15),
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return {"sent": False, "error": data.get("msg") or str(data), "result": data, "config": self.redacted_config()}
            return {
                "sent": True,
                "message_id": data.get("data", {}).get("message_id", ""),
                "config": self.redacted_config(),
            }
        except Exception as exc:
            logger.warning("Feishu monitoring card send failed: %s", exc, exc_info=True)
            return {"sent": False, "error": str(exc), "config": self.redacted_config()}

    def redacted_config(self) -> dict[str, bool]:
        return {
            "app_id_configured": bool(self.config.get("app_id")),
            "app_secret_configured": bool(self.config.get("app_secret")),
            "chat_id_configured": bool(self.config.get("chat_id")),
            "notify_user_id_configured": bool(self.config.get("notify_user_id")),
        }


def send_card(card: dict[str, Any]) -> dict[str, Any]:
    return FeishuMonitoringSender().send_card(card)
