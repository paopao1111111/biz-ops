#!/usr/bin/env python3
"""Check iWeaver core-data anomaly windows and send unified Feishu alerts."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

MONITORING_HOME = Path(__file__).resolve().parents[2]
if str(MONITORING_HOME) not in sys.path:
    sys.path.insert(0, str(MONITORING_HOME))

from monitoring.api import record_and_send_event
from monitoring.card_templates import build_growth_metrics_alert_card
from monitoring.config import DASHBOARD_THRESHOLDS, METRIC_LABELS, load_env_files
from monitoring.event_store import MonitoringEventStore
from monitoring.feishu_sender import FeishuMonitoringSender

PROJECT_DIR = Path("/srv/cloudcli-workspaces/default/agentos_mcp_orchestrator_transfer")
TZ = ZoneInfo("Asia/Shanghai")
WINDOWS = {
    "0-6": 6,
    "0-12": 12,
    "0-18": 18,
    "0-24": 24,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run unified dashboard window alert check")
    parser.add_argument("--date", default="", help="Local date YYYY-MM-DD. Defaults to today, or yesterday for 0-24 shortly after midnight.")
    parser.add_argument("--window", choices=sorted(WINDOWS), default="", help="Window label: 0-6, 0-12, 0-18, 0-24")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-send", action="store_true")
    args = parser.parse_args()

    load_env_files()
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))

    window_label = args.window or infer_window(datetime.now(TZ))
    target_date = parse_target_date(args.date, window_label)
    result = run_window_check(
        target_date=target_date,
        window_label=window_label,
        send=not args.no_send and not args.dry_run,
        record=not args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("success") else 2


def infer_window(now: datetime) -> str:
    hour = now.hour
    if hour < 6:
        return "0-24"
    if hour < 12:
        return "0-6"
    if hour < 18:
        return "0-12"
    return "0-18"


def parse_target_date(value: str, window_label: str):
    if value:
        return datetime.strptime(value, "%Y-%m-%d").date()
    now = datetime.now(TZ)
    if window_label == "0-24" and now.hour < 6:
        return (now - timedelta(days=1)).date()
    return now.date()


def run_window_check(target_date, window_label: str, send: bool = True, record: bool = True) -> dict:
    from adapters.dashboard_metrics.runtime.sql_metrics import fetch_window_metrics

    end_hour = WINDOWS[window_label]
    current_start_local = datetime.combine(target_date, datetime.min.time(), tzinfo=TZ)
    current_end_local = current_start_local + timedelta(hours=end_hour)
    previous_start_local = current_start_local - timedelta(days=1)
    previous_end_local = previous_start_local + timedelta(hours=end_hour)

    current = fetch_window_metrics(sql_time(current_start_local), sql_time(current_end_local))
    previous = fetch_window_metrics(sql_time(previous_start_local), sql_time(previous_end_local))

    alerts = []
    sent = []
    for metric_name, threshold in DASHBOARD_THRESHOLDS.items():
        cur = to_float(current.get(metric_name))
        prev = to_float(previous.get(metric_name))
        if cur is None or prev is None:
            continue
        ratio = abs(cur - prev) / max(abs(prev), 1.0)
        triggered = ratio > threshold
        direction = "up" if cur > prev else ("down" if cur < prev else "flat")
        item = {
            "metric_name": metric_name,
            "metric_label": METRIC_LABELS.get(metric_name, metric_name),
            "current_value": cur,
            "previous_value": prev,
            "change_ratio": ratio,
            "threshold": threshold,
            "triggered": triggered,
            "direction": direction,
            "window_label": window_label,
            "window_start": local_text(current_start_local),
            "window_end": local_text(current_end_local),
            "previous_window_start": local_text(previous_start_local),
            "previous_window_end": local_text(previous_end_local),
        }
        alerts.append(item)
        if not triggered:
            continue
        severity = "high" if ratio >= threshold * 2 else "warning"
        event_key = f"dashboard:{target_date.isoformat()}:{window_label}:{metric_name}"
        if record:
            event_result = record_and_send_event(
                send=False,
                event_key=event_key,
                source="dashboard",
                event_type="data_anomaly",
                severity=severity,
                title="数据异常预警",
                object_id=metric_name,
                window_start=local_text(current_start_local),
                window_end=local_text(current_end_local),
                status="triggered",
                payload=item,
            )
        else:
            event_result = {"dry_run": True, "event_key": event_key, "would_send": False, "payload": item}
        sent.append({"event_key": event_key, "result": event_result})

    triggered_alerts = [item for item in alerts if item.get("triggered")]
    summary_send = {"sent": False, "reason": "No triggered alerts"}
    if triggered_alerts:
        summary_severity = "high" if any(
            float(item.get("change_ratio") or 0) >= float(item.get("threshold") or 0) * 2
            for item in triggered_alerts
        ) else "warning"
        summary_event_key = f"dashboard_alert:{target_date.isoformat()}:{window_label}:growth_metrics"
        if record:
            store = MonitoringEventStore()
            summary_record = store.record_event(
                event_key=summary_event_key,
                source="dashboard_alert",
                event_type="growth_metrics_alert",
                severity=summary_severity,
                title="iWeaver 增长指标预警",
                object_id="growth_metrics",
                window_start=local_text(current_start_local),
                window_end=local_text(current_end_local),
                status="triggered",
                payload={
                    "window_label": window_label,
                    "window_start": local_text(current_start_local),
                    "window_end": local_text(current_end_local),
                    "triggered_count": len(triggered_alerts),
                    "triggered": triggered_alerts,
                },
            )
            event = summary_record.get("event") or {}
            if send:
                if not summary_record.get("inserted") and int(event.get("sent_to_feishu") or 0) == 1:
                    summary_send = {"sent": False, "skipped": True, "reason": "duplicate summary already sent", "event_key": summary_event_key}
                else:
                    card = build_growth_metrics_alert_card(triggered_alerts, alerts)
                    summary_send = FeishuMonitoringSender().send_card(card)
                    store.mark_event_sent(
                        summary_event_key,
                        message_id=str(summary_send.get("message_id") or ""),
                        error="" if summary_send.get("sent") else str(summary_send.get("error") or "send failed"),
                    )
            else:
                summary_send = {"sent": False, "reason": "send disabled", "event_key": summary_event_key}
        else:
            summary_send = {"dry_run": True, "event_key": summary_event_key, "would_send": bool(send)}

    return {
        "success": True,
        "target_date": target_date.isoformat(),
        "window_label": window_label,
        "window_start": local_text(current_start_local),
        "window_end": local_text(current_end_local),
        "previous_window_start": local_text(previous_start_local),
        "previous_window_end": local_text(previous_end_local),
        "alerts": alerts,
        "triggered": triggered_alerts,
        "sent": sent,
        "summary_send": summary_send,
    }


def sql_time(dt: datetime) -> str:
    return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def local_text(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def to_float(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
