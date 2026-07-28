#!/usr/bin/env python3
"""Send the unified daily Feishu monitoring report."""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MONITORING_HOME = Path(__file__).resolve().parents[2]
if str(MONITORING_HOME) not in sys.path:
    sys.path.insert(0, str(MONITORING_HOME))

from monitoring.card_templates import EDM_CATEGORY_LABELS, METRIC_LABELS, build_daily_report_card
from monitoring.config import load_env_files
from monitoring.event_store import MonitoringEventStore
from monitoring.feishu_sender import FeishuMonitoringSender

TZ = ZoneInfo("Asia/Shanghai")
EDM_SYSTEM_DIR = Path("/opt/edm-system")
EDM_QUEUE_DB = EDM_SYSTEM_DIR / "data" / "queue.db"


def main() -> int:
    parser = argparse.ArgumentParser(description="Send unified daily monitoring report")
    parser.add_argument("--date", default="", help="Report date YYYY-MM-DD; default yesterday in Asia/Shanghai")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Send even if this date was already sent")
    args = parser.parse_args()

    load_env_files()
    report_date = args.date or (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    report = build_report(report_date)

    store = MonitoringEventStore()
    existing = store.get_report_run(report_date)
    if existing and not args.force and not args.dry_run and existing.get("status") == "sent" and existing.get("feishu_message_id"):
        result = {"success": True, "skipped": True, "reason": "report already sent", "report_date": report_date, "existing": existing}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    card = build_daily_report_card(report)
    if args.dry_run:
        result = {"success": True, "dry_run": True, "report": report, "card": card}
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    send_result = FeishuMonitoringSender().send_card(card)
    status = "sent" if send_result.get("sent") else "failed"
    first = store.record_report_run(
        report_date,
        payload=report,
        status=status,
        message_id=str(send_result.get("message_id") or ""),
        error="" if send_result.get("sent") else str(send_result.get("error") or "send failed"),
    )
    result = {"success": bool(send_result.get("sent")), "first_run": first, "report": report, "send": send_result}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result["success"] else 2


def build_report(report_date: str) -> dict:
    start_utc, end_utc = local_day_utc_range(report_date)
    # The 0-24 dashboard window is intentionally checked at 00:05 the next
    # local day. Query a small grace period after day-end, then filter dashboard
    # events by their payload/window date so the full-day anomaly is included in
    # the correct T-1 report without pulling next-day EDM/feedback events.
    grace_end_utc = add_minutes(end_utc, 15)
    store = MonitoringEventStore()
    raw_events = store.list_events(start_at=start_utc, end_at=grace_end_utc, limit=5000)
    events = filter_events_for_report(raw_events, report_date, start_utc, end_utc)

    data_events = [e for e in events if e.get("source") == "dashboard" or e.get("event_type") == "data_anomaly"]
    feedback_events = [e for e in events if e.get("source") == "feedback" or e.get("event_type") == "feedback_alert"]
    edm_events = [e for e in events if e.get("source") == "edm" or e.get("event_type") in {"edm_alert", "service_error"}]

    feedback_totals = collect_feedback_totals(report_date)
    edm_totals = collect_edm_totals(report_date)
    system_status = collect_system_status()

    major_events = [e for e in events if str(e.get("severity")) in {"critical", "high"}]
    warning_events = [e for e in events if str(e.get("severity")) == "warning"]

    action_items = build_action_items(data_events, feedback_events, edm_events, system_status)
    summary = "无重大异常"
    if major_events:
        summary = f"发现 {len(major_events)} 条高风险/严重异常，请优先处理。"
    elif warning_events:
        summary = f"发现 {len(warning_events)} 条一般预警，建议关注。"

    return {
        "report_date": report_date,
        "period": {"start_utc": start_utc, "end_utc": end_utc, "timezone": "Asia/Shanghai"},
        "summary": summary,
        "major_anomaly_count": len(major_events),
        "warning_count": len(warning_events),
        "data": summarize_data(data_events),
        "feedback": summarize_feedback(feedback_events, feedback_totals),
        "edm": summarize_edm(edm_events, edm_totals),
        "system": system_status,
        "action_items": action_items,
        "event_count": len(events),
    }


def local_day_utc_range(report_date: str) -> tuple[str, str]:
    day = datetime.strptime(report_date, "%Y-%m-%d").date()
    start_local = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    end_local = start_local + timedelta(days=1)
    return iso_utc(start_local), iso_utc(end_local)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def add_minutes(iso_value: str, minutes: int) -> str:
    dt = datetime.fromisoformat(iso_value.replace("Z", "+00:00"))
    return iso_utc(dt + timedelta(minutes=minutes))


def filter_events_for_report(events: list[dict], report_date: str, start_utc: str, end_utc: str) -> list[dict]:
    filtered = []
    start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    for event in events:
        source = event.get("source") or ""
        event_type = event.get("event_type") or ""
        if source == "dashboard" or event_type == "data_anomaly":
            payload = event.get("payload") or {}
            window_start = str(payload.get("window_start") or event.get("window_start") or "")
            if window_start.startswith(report_date):
                filtered.append(event)
            continue
        created = datetime.fromisoformat(str(event.get("created_at")).replace("Z", "+00:00"))
        if start_dt <= created < end_dt:
            filtered.append(event)
    return filtered


def summarize_data(events: list[dict]) -> dict:
    metrics = []
    critical = 0
    for event in events:
        payload = event.get("payload") or {}
        metric = payload.get("metric_label") or METRIC_LABELS.get(payload.get("metric_name"), payload.get("metric_name"))
        if metric:
            metrics.append(str(metric))
        if event.get("severity") in {"critical", "high"}:
            critical += 1
    return {"alert_count": len(events), "critical_count": critical, "metrics": sorted(set(metrics))}


def summarize_feedback(events: list[dict], totals: dict) -> dict:
    manual = 0
    refund = 0
    error = 0
    for event in events:
        payload = event.get("payload") or {}
        if payload.get("is_refund") or "退费" in str(payload.get("anomaly_type") or ""):
            refund += 1
        status = str(payload.get("status") or event.get("status") or "")
        if status in {"manual_required", "refund_pending", "no_email"}:
            manual += 1
        if status == "error" or event.get("severity") in {"critical", "high"} and event.get("status") == "error":
            error += 1
    result = dict(totals or {})
    result.update({"alert_count": len(events), "refund_count": refund, "manual_count": manual, "error_count": error})
    return result


def summarize_edm(events: list[dict], totals: dict) -> dict:
    high_risk = 0
    service_error = 0
    manual = 0
    for event in events:
        payload = event.get("payload") or {}
        category = str(payload.get("category") or event.get("status") or "")
        if event.get("event_type") == "service_error" or category == "service_error":
            service_error += 1
        if event.get("severity") in {"critical", "high"} or category in {"refund_request", "cancel_subscription", "billing_issue", "delete_account"}:
            high_risk += 1
        if str(payload.get("action") or event.get("status") or "") in {"notify_ops", "notify_only", "pending", "manual"}:
            manual += 1
    result = dict(totals or {})
    result.update({"alert_count": len(events), "high_risk_count": high_risk, "service_error_count": service_error, "manual_count": manual})
    return result


def collect_feedback_totals(report_date: str) -> dict:
    try:
        if str(EDM_SYSTEM_DIR) not in sys.path:
            sys.path.insert(0, str(EDM_SYSTEM_DIR))
        from lib.superset_client import SupersetClient

        client = SupersetClient()
        start = report_date
        end = (datetime.strptime(report_date, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
        sql = f"""
        WITH feedback_rows AS (
          SELECT type::text AS type, 'feedback_info' AS source FROM feedback_info
          WHERE created_at >= '{start}' AND created_at < '{end}'
          UNION ALL
          SELECT type::text AS type, 'attitude' AS source FROM attitude
          WHERE created_time >= '{start}' AND created_time < '{end}'
        )
        SELECT COUNT(*) AS total,
               COUNT(CASE WHEN lower(type) IN ('1','true','thumbs_up','thumb_up','up','like','liked','positive') THEN 1 END) AS thumbs_up,
               COUNT(CASE WHEN lower(type) IN ('0','false','-1','thumbs_down','thumb_down','down','dislike','negative') THEN 1 END) AS thumbs_down
        FROM feedback_rows
        """
        rows = client.execute_sql(sql)
        row = rows[0] if rows else {}
        return {
            "total_feedback": int(float(row.get("total") or 0)),
            "thumbs_up": int(float(row.get("thumbs_up") or 0)),
            "thumbs_down": int(float(row.get("thumbs_down") or 0)),
        }
    except Exception as exc:
        return {"total_feedback": "未统计", "thumbs_up": "未统计", "thumbs_down": "未统计", "collect_error": str(exc)[:200]}


def collect_edm_totals(report_date: str) -> dict:
    try:
        if not EDM_QUEUE_DB.exists():
            return {"processed_email_count": 0, "categories": {}}
        conn = sqlite3.connect(str(EDM_QUEUE_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT category, COUNT(*) AS count
            FROM processed
            WHERE processed_at >= ? AND processed_at < date(?, '+1 day')
            GROUP BY category
            """,
            (report_date, report_date),
        ).fetchall()
        categories = {str(r["category"]): int(r["count"] or 0) for r in rows}
        return {
            "processed_email_count": sum(categories.values()),
            "categories": categories,
            "category_labels": {EDM_CATEGORY_LABELS.get(k, k): v for k, v in categories.items()},
        }
    except Exception as exc:
        return {"processed_email_count": "未统计", "collect_error": str(exc)[:200]}


def collect_system_status() -> dict:
    return {
        "edm_service": service_status("edm-auto-reply.service"),
        "feedback_service": service_status("feedback-autoreply.service"),
        "dashboard_timers": dashboard_timer_status(),
        "xray": service_status("xray.service"),
        "feishu": "正常",
    }


def service_status(unit: str) -> str:
    try:
        proc = subprocess.run(["systemctl", "is-active", unit], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=5)
        text = (proc.stdout or proc.stderr or "").strip()
        return "正常" if text == "active" else f"异常({text or proc.returncode})"
    except Exception as exc:
        return f"未检查({str(exc)[:50]})"


def dashboard_timer_status() -> str:
    units = ["iweaver-monitor-window-alert.timer", "iweaver-monitor-daily-report.timer", "dashboard-metrics-daily.timer"]
    statuses = [f"{u}:{service_status(u)}" for u in units]
    return "；".join(statuses)


def build_action_items(data_events: list[dict], feedback_events: list[dict], edm_events: list[dict], system_status: dict) -> list[str]:
    items = []
    for event in sorted(data_events, key=lambda e: e.get("severity") in {"critical", "high"}, reverse=True)[:3]:
        payload = event.get("payload") or {}
        metric = payload.get("metric_label") or payload.get("metric_name") or event.get("object_id")
        items.append(f"数据指标 {metric} 在 {payload.get('window_label') or ''} 窗口异常，请检查业务链路。")
    for event in feedback_events[:3]:
        payload = event.get("payload") or {}
        user = payload.get("user_id") or event.get("user_id") or "未知用户"
        items.append(f"反馈异常需处理：用户 {user}，类型 {payload.get('anomaly_type') or payload.get('reason_category') or event.get('status')}。")
    for event in edm_events[:3]:
        payload = event.get("payload") or {}
        email = payload.get("email") or payload.get("from") or event.get("user_email") or "未知邮箱"
        items.append(f"EDM 异常需处理：{email}，类型 {payload.get('anomaly_type') or payload.get('category') or event.get('status')}。")
    for name, value in system_status.items():
        if isinstance(value, str) and value.startswith("异常"):
            items.append(f"系统服务异常：{name} = {value}。")
    return items[:8]


if __name__ == "__main__":
    raise SystemExit(main())
