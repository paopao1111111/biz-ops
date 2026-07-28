"""Safe high-level monitoring API for production services.

Callers can import this module from EDM/Feedback/Dashboard. All public helpers are
best-effort: they log and return a result dict, but never raise into the caller's
business flow.
"""
from __future__ import annotations

import logging
from typing import Any

from .card_templates import build_card_for_event
from .event_store import MonitoringEventStore
from .feishu_sender import FeishuMonitoringSender

logger = logging.getLogger(__name__)


def record_event(**kwargs: Any) -> dict[str, Any]:
    try:
        return MonitoringEventStore().record_event(**kwargs)
    except Exception as exc:
        logger.warning("monitoring record_event failed: %s", exc, exc_info=True)
        return {"inserted": False, "event": {}, "error": str(exc)}


def record_and_send_event(*, send: bool = True, force_send: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Record a monitoring event and optionally send its Feishu alert card.

    Duplicate event_keys are idempotent: if the event already has sent_to_feishu=1
    and force_send is False, the card is not sent again.
    """
    result = record_event(**kwargs)
    event = result.get("event") or {}
    if not send or not event:
        return result

    if not force_send and not result.get("inserted") and int(event.get("sent_to_feishu") or 0) == 1:
        result["send"] = {"sent": False, "skipped": True, "reason": "duplicate already sent"}
        return result

    try:
        card = build_card_for_event(event)
        send_result = FeishuMonitoringSender().send_card(card)
        result["send"] = send_result
        try:
            MonitoringEventStore().mark_event_sent(
                str(event.get("event_key") or kwargs.get("event_key") or ""),
                message_id=str(send_result.get("message_id") or ""),
                error="" if send_result.get("sent") else str(send_result.get("error") or "send failed"),
            )
        except Exception as mark_exc:
            logger.warning("monitoring mark_event_sent failed: %s", mark_exc, exc_info=True)
        return result
    except Exception as exc:
        logger.warning("monitoring send event failed: %s", exc, exc_info=True)
        result["send"] = {"sent": False, "error": str(exc)}
        return result


def safe_record_and_send_event(**kwargs: Any) -> dict[str, Any]:
    try:
        return record_and_send_event(**kwargs)
    except Exception as exc:
        logger.warning("safe_record_and_send_event swallowed error: %s", exc, exc_info=True)
        return {"inserted": False, "event": {}, "send": {"sent": False, "error": str(exc)}}
