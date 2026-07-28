from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

from .db import REQUEST_TYPES

TWEET_ID_RE = re.compile(r"^[0-9]{1,19}$")
STATUS_URL_RE = re.compile(r"^https://(?:www\.)?(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status(?:es)?/([0-9]{1,19})(?:[/?#].*)?$")
ARTICLE_BLOCK_TYPES = {"paragraph", "heading", "list", "quote", "code", "image", "divider"}


class PayloadError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_tweet_id(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadError("invalid_target", "target must be a tweet id or x.com status URL")
    candidate = value.strip()
    match = STATUS_URL_RE.match(candidate)
    if match:
        candidate = match.group(1)
    if not TWEET_ID_RE.match(candidate):
        raise PayloadError("invalid_target", "target must be a numeric tweet id or x.com status URL")
    return candidate.lstrip("0") or "0"


def validate_post_text(value: Any) -> str:
    if not isinstance(value, str):
        raise PayloadError("invalid_text", "text must be a string")
    if value != value.strip() or not value:
        raise PayloadError("invalid_text", "text must not be empty or have leading/trailing whitespace")
    if len(value) > 280:
        raise PayloadError("invalid_text", "text is limited to 280 characters in phase 1")
    if any(unicodedata.category(char) in {"Cc", "Cf"} for char in value):
        raise PayloadError("invalid_text", "text must not contain control characters")
    return value


def _validate_link(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise PayloadError("invalid_link", f"{field} links must use https://")
    return value


def validate_scheduled_at(value: Any) -> int:
    import time as _time
    if type(value) is not int:
        raise PayloadError("invalid_scheduled_at", "scheduled_at must be a unix epoch integer")
    if value <= int(_time.time()):
        raise PayloadError("invalid_scheduled_at", "scheduled_at must be in the future")
    if value > int(_time.time()) + 86400 * 30:
        raise PayloadError("invalid_scheduled_at", "scheduled_at must be within 30 days")
    return value


def validate_article(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PayloadError("invalid_article", "article must be an object")
    extra = set(value) - {"schema_version", "title", "blocks"}
    if extra:
        raise PayloadError("invalid_article", f"article has unsupported fields: {', '.join(sorted(extra))}")
    if value.get("schema_version") != 1:
        raise PayloadError("invalid_article", "article schema_version must be 1")
    title = value.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 250:
        raise PayloadError("invalid_article", "article title is required and limited to 250 characters")
    blocks = value.get("blocks")
    if not isinstance(blocks, list) or not blocks or len(blocks) > 200:
        raise PayloadError("invalid_article", "article must contain 1-200 blocks")
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise PayloadError("invalid_article", f"block {index} must be an object")
        extra = set(block) - {"type", "text", "items", "url", "media_asset_id", "language"}
        if extra:
            raise PayloadError("invalid_article", f"block {index} has unsupported fields: {', '.join(sorted(extra))}")
        block_type = block.get("type")
        if block_type not in ARTICLE_BLOCK_TYPES:
            raise PayloadError("invalid_article", f"block {index} has unsupported type {block_type!r}")
        if block_type == "divider":
            continue
        if block_type == "list":
            items = block.get("items")
            if not isinstance(items, list) or not items or len(items) > 100:
                raise PayloadError("invalid_article", f"list block {index} needs 1-100 items")
            for item in items:
                if not isinstance(item, str) or len(item) > 2000:
                    raise PayloadError("invalid_article", f"list block {index} items must be strings up to 2000 chars")
            continue
        if block_type == "image":
            if not isinstance(block.get("media_asset_id"), int):
                raise PayloadError("invalid_article", f"image block {index} needs an integer media_asset_id")
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000:
            raise PayloadError("invalid_article", f"block {index} text must be 1-10000 characters")
        if "url" in block:
            _validate_link(block["url"], f"block {index}")
    return {"schema_version": 1, "title": title, "blocks": blocks}


def validate_payload(request_type: str, payload: Any) -> dict:
    if request_type not in REQUEST_TYPES:
        raise PayloadError("invalid_request_type", f"request_type must be one of {', '.join(REQUEST_TYPES)}")
    if not isinstance(payload, dict):
        raise PayloadError("invalid_payload", "payload must be an object")
    if request_type in {"like", "unlike", "repost", "unrepost", "post_delete"}:
        extra = set(payload) - {"target"}
        if extra:
            raise PayloadError("invalid_payload", f"unsupported fields: {', '.join(sorted(extra))}")
        return {"target": normalize_tweet_id(payload.get("target"))}
    if request_type == "reply":
        extra = set(payload) - {"text", "target", "scheduled_at"}
        if extra:
            raise PayloadError("invalid_payload", f"unsupported fields: {', '.join(sorted(extra))}")
        if "text" not in payload or "target" not in payload:
            raise PayloadError("invalid_payload", "reply requires both text and target")
        result = {"text": validate_post_text(payload.get("text")), "target": normalize_tweet_id(payload.get("target"))}
        scheduled = payload.get("scheduled_at")
        if scheduled is not None:
            result["scheduled_at"] = validate_scheduled_at(scheduled)
        return result
    if request_type == "post_create":
        extra = set(payload) - {"text", "scheduled_at", "media_asset_ids"}
        if extra:
            raise PayloadError("invalid_payload", f"unsupported fields: {', '.join(sorted(extra))}")
        result = {"text": validate_post_text(payload.get("text"))}
        if "media_asset_ids" in payload and payload["media_asset_ids"] is not None:
            media_ids = payload["media_asset_ids"]
            if not isinstance(media_ids, list) or not media_ids or len(media_ids) > 4:
                raise PayloadError("invalid_media", "media_asset_ids must be a list of 1-4 integers")
            if not all(isinstance(x, int) and not isinstance(x, bool) for x in media_ids):
                raise PayloadError("invalid_media", "media_asset_ids must be integers")
            result["media_asset_ids"] = media_ids
        scheduled = payload.get("scheduled_at")
        if scheduled is not None:
            result["scheduled_at"] = validate_scheduled_at(scheduled)
        return result
    extra = set(payload) - {"article"}
    if extra:
        raise PayloadError("invalid_payload", f"unsupported fields: {', '.join(sorted(extra))}")
    return {"article": validate_article(payload.get("article"))}
