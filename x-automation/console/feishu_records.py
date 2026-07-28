"""Feishu spreadsheet recorder for the content workflow.

Appends one row per sent comment / published post into a Feishu 电子表格
(sheets v2 values_append — same API family as the case-analysis daily job).
Uses the operator's Feishu app credentials (FEISHU_APP_ID / FEISHU_APP_SECRET).

Configuration via env:
  FEISHU_WORKFLOW_SHEET_TOKEN  - spreadsheet token (the /sheets/ URL token)
  FEISHU_WORKFLOW_REPLY_SHEET  - worksheet id for comment records
  FEISHU_WORKFLOW_POST_SHEET   - worksheet id for post records
If any are unset, recording is skipped (no error raised to the caller).

Token handling: thread-safe cached tenant token, refreshed before expiry.
Never logs secret values.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any

_BASE = "https://open.feishu.cn/open-apis"
_token_cache: dict[str, dict[str, Any]] = {}
_token_lock = threading.Lock()


class FeishuRecordError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _config() -> dict[str, str | None]:
    return {
        "app_id": os.getenv("FEISHU_APP_ID") or os.getenv("FEISHU_WORKFLOW_APP_ID"),
        "app_secret": os.getenv("FEISHU_APP_SECRET") or os.getenv("FEISHU_WORKFLOW_APP_SECRET"),
        "sheet_token": os.getenv("FEISHU_WORKFLOW_SHEET_TOKEN"),
        "reply_sheet": os.getenv("FEISHU_WORKFLOW_REPLY_SHEET"),
        "post_sheet": os.getenv("FEISHU_WORKFLOW_POST_SHEET"),
    }


def _configured(cfg: dict[str, str | None]) -> bool:
    return bool(cfg["app_id"] and cfg["app_secret"] and cfg["sheet_token"]
                and (cfg["reply_sheet"] or cfg["post_sheet"]))


def _tenant_token(app_id: str, app_secret: str) -> str:
    cache_key = app_id
    with _token_lock:
        cached = _token_cache.get(cache_key)
        if cached and cached["expires_at"] - 60 > time.time():
            return cached["token"]
    payload = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(_BASE + "/auth/v3/tenant_access_token/internal",
                                     data=payload, method="POST")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", "replace") if exc.fp else ""
        raise FeishuRecordError("feishu_auth_failed", f"tenant token HTTP {exc.code}: {body[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeishuRecordError("feishu_unreachable", str(exc)) from exc
    if data.get("code") != 0:
        raise FeishuRecordError("feishu_auth_failed",
                                str(data.get("msg") or data.get("code"))[:200])
    token = data.get("tenant_access_token")
    if not token:
        raise FeishuRecordError("feishu_auth_failed", "no tenant_access_token in response")
    expire = data.get("expire") or 7200
    with _token_lock:
        _token_cache[cache_key] = {"token": token, "expires_at": time.time() + int(expire)}
    return token


def _append_rows(sheet_token: str, sheet_id: str, token: str,
                 rows: list[list[Any]]) -> dict[str, Any]:
    ncols = max(len(r) for r in rows)
    end_col = chr(ord("A") + ncols - 1) if ncols <= 26 else "Z"
    rng = f"{sheet_id}!A1:{end_col}{len(rows)}"
    payload = json.dumps({"valueRange": {"range": rng, "values": rows}},
                         ensure_ascii=False).encode("utf-8")
    url = f"{_BASE}/sheets/v2/spreadsheets/{sheet_token}/values_append?insertDataOption=INSERT_ROWS"
    request = urllib.request.Request(url, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read(1 << 20))
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", "replace") if exc.fp else ""
        raise FeishuRecordError("feishu_write_failed", f"values_append HTTP {exc.code}: {body[:200]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeishuRecordError("feishu_unreachable", str(exc)) from exc
    if data.get("code") != 0:
        raise FeishuRecordError("feishu_write_failed", str(data.get("msg") or data.get("code"))[:200])
    return data.get("data") or {}


def _fmt_time(value):
    """Sheets rows are for operators: render epoch values as readable local time."""
    s = str(value or "").strip()
    if s.isdigit() and len(s) >= 9:
        try:
            import time as _t
            return _t.strftime("%Y-%m-%d %H:%M", _t.localtime(int(s)))
        except (ValueError, OverflowError):
            return s
    return s


def record_reply(*, account_name: str, post_url: str, post_time: str,
                 summary: str, angle: str, comment: str, sent_at: str,
                 config: dict[str, str] | None = None) -> bool:
    """Append one comment-record row. Returns False if not configured (skip)."""
    cfg = config or _config()
    sheet_id = cfg.get("reply_sheet")
    if not _configured(cfg) or not sheet_id:
        return False
    row = [account_name, post_url, _fmt_time(post_time), summary, angle, comment, _fmt_time(sent_at)]
    token = _tenant_token(cfg["app_id"], cfg["app_secret"])
    _append_rows(cfg["sheet_token"], sheet_id, token, [row])
    return True


def record_post(*, account_name: str, body_text: str, image_url: str,
                published_at: str, post_url: str,
                config: dict[str, str] | None = None) -> bool:
    """Append one post-record row. Returns False if not configured (skip)."""
    cfg = config or _config()
    sheet_id = cfg.get("post_sheet")
    if not _configured(cfg) or not sheet_id:
        return False
    row = [account_name, body_text, image_url, _fmt_time(published_at), post_url]
    token = _tenant_token(cfg["app_id"], cfg["app_secret"])
    _append_rows(cfg["sheet_token"], sheet_id, token, [row])
    return True
