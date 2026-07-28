"""Configuration helpers for unified iWeaver monitoring."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

MONITORING_HOME = Path(os.getenv("IWEAVER_MONITORING_HOME", "/opt/iweaver-monitoring"))
DATA_DIR = Path(os.getenv("IWEAVER_MONITORING_DATA_DIR", str(MONITORING_HOME / "data")))
EVENT_DB_PATH = Path(os.getenv("IWEAVER_MONITORING_DB", str(DATA_DIR / "monitoring_events.db")))
LOG_DIR = Path(os.getenv("IWEAVER_MONITORING_LOG_DIR", str(MONITORING_HOME / "logs")))

DEFAULT_ENV_FILES = (
    MONITORING_HOME / ".env",
    Path("/opt/edm-system/.env"),
    Path("/srv/cloudcli-workspaces/default/agentos_mcp_orchestrator_transfer/.env"),
)

DEFAULT_NOTIFY_USER_ID = "ou_a88146ad16b8d6889a4f8557f74fc54e"

DASHBOARD_THRESHOLDS = {
    "registration_users": 0.10,
    "paid_users": 0.20,
    "renewal_orders": 0.20,
    "payment_amount": 0.20,
}

METRIC_LABELS = {
    "registration_users": "注册用户数",
    "registration_rate": "注册率",
    "first_day_activation_users": "首日激活用户数",
    "activation_rate": "激活率",
    "new_uv_activation_rate": "新UV激活率",
    "dau": "日活",
    "paid_users": "付费用户数",
    "paid_orders": "付费订单数",
    "renewal_orders": "续费订单数",
    "payment_amount": "付费金额",
    "gsc_impressions": "官网曝光量",
    "gsc_clicks": "官网点击量",
    "gsc_ctr": "官网点击率",
    "ga4_new_uv": "官网新UV",
}

HIGH_RISK_EDM_CATEGORIES = {
    "refund_request",
    "cancel_subscription",
    "billing_issue",
    "delete_account",
}


def load_env_files(paths: Iterable[Path] = DEFAULT_ENV_FILES, override: bool = False) -> None:
    """Load simple KEY=VALUE env files without requiring python-dotenv."""
    for path in paths:
        try:
            if not Path(path).exists():
                continue
            for raw in Path(path).read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and (override or key not in os.environ):
                    os.environ[key] = value
        except Exception:
            # Monitoring config loading must not break business flows.
            continue


def get_feishu_config() -> dict:
    load_env_files()
    return {
        "app_id": first_env("IWEAVER_MONITOR_FEISHU_APP_ID", "FEISHU_APP_ID", "FEISHU_BOT_APP_ID", "DASHBOARD_FEISHU_APP_ID"),
        "app_secret": first_env("IWEAVER_MONITOR_FEISHU_APP_SECRET", "FEISHU_APP_SECRET", "FEISHU_BOT_APP_SECRET", "DASHBOARD_FEISHU_APP_SECRET"),
        "chat_id": first_env("IWEAVER_MONITOR_FEISHU_CHAT_ID", "FEISHU_CHAT_ID", "FEISHU_ALERT_CHAT_ID", "DASHBOARD_FEISHU_CHAT_ID"),
        "notify_user_id": first_env("IWEAVER_MONITOR_NOTIFY_USER_ID", "FEISHU_NOTIFY_USER_ID") or DEFAULT_NOTIFY_USER_ID,
    }


def first_env(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
