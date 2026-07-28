"""Feishu card templates for unified monitoring alerts and reports."""
from __future__ import annotations

from datetime import datetime
from typing import Any
import re

from .config import METRIC_LABELS, get_feishu_config

SEVERITY_COLOR = {
    "critical": "red",
    "high": "red",
    "warning": "orange",
    "info": "blue",
    "success": "green",
}

EDM_CATEGORY_LABELS = {
    "faq_question": "匹配成功",
    "refund_request": "退费",
    "cancel_subscription": "取消订阅",
    "billing_issue": "账单/退费问题",
    "delete_account": "注销账户",
    "bug_report": "问题反馈",
    "feature_request": "功能建议",
    "invoice_request": "发票问题",
    "service_error": "服务异常",
    "reply_failed": "流程异常",
    "parse_error": "流程异常",
    "error": "流程异常",
    "other": "匹配失败",
}

FEEDBACK_STATUS_LABELS = {
    "auto_replied": "已生成草稿/自动处理完成",
    "refund_pending": "已进入人工干预",
    "manual_required": "已进入人工干预",
    "no_email": "已进入人工干预（无邮箱）",
    "error": "自动处理失败",
    "positive_rating_only": "无需处理",
    "no_content": "无需处理",
}


def owner_mention() -> str:
    user_id = get_feishu_config().get("notify_user_id") or ""
    return f"<at id={user_id}></at>" if user_id else "@米萌"


def plain(value: Any, default: str = "-") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def truncate(value: Any, limit: int = 500) -> str:
    text = plain(value, "")
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text or "-"


def email_address_only(value: Any) -> str:
    """Return a plain email address so Feishu markdown does not swallow <email>."""
    text = plain(value, "")
    match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else (text or "-")


def card(title: str, markdown_blocks: list[str], color: str = "blue") -> dict[str, Any]:
    elements: list[dict[str, Any]] = []
    for idx, block in enumerate(markdown_blocks):
        if idx:
            elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": block})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": color},
        "elements": elements,
    }


def kv_lines(items: list[tuple[str, Any]], *, value_limit: int = 500) -> str:
    lines = []
    for label, value in items:
        lines.append(f"**{label}：** {truncate(value, value_limit)}")
    return "\n".join(lines)


def build_data_alert_card(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    severity = event.get("severity") or payload.get("severity") or "warning"
    metric_name = payload.get("metric_name") or event.get("object_id") or ""
    metric_label = payload.get("metric_label") or METRIC_LABELS.get(metric_name, metric_name)
    change_ratio = payload.get("change_ratio")
    threshold = payload.get("threshold")
    direction = payload.get("direction") or ""
    change_text = pct(change_ratio)
    if direction == "down" and change_text != "-":
        change_text = f"-{change_text}"
    elif direction == "up" and change_text != "-":
        change_text = f"+{change_text}"

    return card(
        "【数据异常预警】",
        [
            kv_lines([
                ("监测窗口", payload.get("window_label") or window_text(event)),
                ("异常指标", metric_label),
                ("当前值", payload.get("current_value")),
                ("对比值", payload.get("previous_value")),
                ("变化幅度", change_text),
                ("阈值", pct(threshold)),
            ]),
            kv_lines([
                ("异常原因判断", payload.get("reason") or "超过阈值，请结合投放、注册/支付链路和服务状态排查"),
                ("建议处理动作", payload.get("suggestion") or suggestion_for_metric(metric_name, direction)),
                ("负责人", owner_mention()),
            ]),
        ],
        SEVERITY_COLOR.get(severity, "orange"),
    )


def build_growth_metrics_alert_card(triggered_alerts: list[dict[str, Any]], all_alerts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build the original-style grouped growth metrics alert card."""
    triggered_alerts = triggered_alerts or []
    first = triggered_alerts[0] if triggered_alerts else {}
    window = f"{plain(first.get('window_start'))} ~ {plain(first.get('window_end'))}"
    label_map = {
        "registration_users": "注册用户数",
        "paid_users": "付费用户",
        "renewal_orders": "续费订单",
        "payment_amount": "付费金额",
    }

    blocks = [
        f"检测到 **{len(triggered_alerts)}** 个增长指标超过阈值。\n监控窗口：{window}"
    ]
    for alert in triggered_alerts:
        metric_name = str(alert.get("metric_name") or "")
        metric_label = label_map.get(metric_name) or alert.get("metric_label") or METRIC_LABELS.get(metric_name, metric_name)
        blocks.append(
            f"**{metric_label}**\n"
            f"当前：{fmt_number(alert.get('current_value'))}｜"
            f"上期：{fmt_number(alert.get('previous_value'))}｜"
            f"变化：{pct(alert.get('change_ratio'))}｜"
            f"阈值：{pct(alert.get('threshold'))}"
        )
    blocks.append(f"**负责人：** {owner_mention()} 请关注增长指标异常")
    return card("iWeaver 增长指标预警", blocks, "red")


def build_feedback_alert_card(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    source_label = payload.get("feedback_source") or payload.get("action_type") or payload.get("type") or "点赞/点踩"
    anomaly_type = payload.get("anomaly_type") or feedback_anomaly_type(payload)
    status = payload.get("process_status") or payload.get("status") or event.get("status") or "manual_required"
    return card(
        "【点踩点赞用户反馈异常预警】",
        [
            kv_lines([
                ("反馈来源", source_label),
                ("用户ID", payload.get("user_id") or event.get("user_id")),
                ("反馈时间", payload.get("feedback_time") or payload.get("created_at") or event.get("created_at")),
                ("反馈类型", anomaly_type),
                ("用户内容", payload.get("feedback_content") or payload.get("chat_content")),
                ("处理状态", FEEDBACK_STATUS_LABELS.get(str(status), status)),
                ("负责人", owner_mention()),
            ], value_limit=700),
        ],
        SEVERITY_COLOR.get(event.get("severity") or "warning", "orange"),
    )


def build_edm_alert_card(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    category = payload.get("category") or payload.get("anomaly_type") or event.get("status") or "other"
    raw_anomaly_type = payload.get("anomaly_type") or category
    anomaly_type = EDM_CATEGORY_LABELS.get(str(raw_anomaly_type), str(raw_anomaly_type))
    status = payload.get("process_status") or payload.get("action") or event.get("status") or "已进入人工干预"
    email_address = email_address_only(payload.get("email") or payload.get("from") or payload.get("from_email") or event.get("user_email"))
    title = "【EDM 自动回复异常预警】"
    return card(
        title,
        [
            kv_lines([
                ("邮件ID", email_address),
                ("邮件标题", payload.get("subject")),
                ("邮件时间", payload.get("email_time") or payload.get("date") or payload.get("received_at")),
                ("异常类型", anomaly_type),
                ("邮件内容", payload.get("content") or payload.get("body") or payload.get("snippet")),
                ("处理状态", status_label(status)),
                ("负责人", owner_mention()),
            ], value_limit=700),
        ],
        SEVERITY_COLOR.get(event.get("severity") or "warning", "orange"),
    )


def build_service_error_card(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    title = "【服务异常预警】"
    if event.get("source") == "edm":
        title = "【EDM 自动回复异常预警】"
    return card(
        title,
        [
            kv_lines([
                ("异常模块", payload.get("module") or event.get("source")),
                ("异常类型", payload.get("anomaly_type") or "服务异常"),
                ("错误信息", payload.get("error") or event.get("send_error")),
                ("连续失败次数", payload.get("fail_count")),
                ("处理状态", payload.get("process_status") or "自动重试中"),
                ("负责人", owner_mention()),
            ], value_limit=800),
        ],
        "red",
    )


def build_daily_report_card(report: dict[str, Any]) -> dict[str, Any]:
    report_date = report.get("report_date") or ""
    title = f"【{report_date} 自动化监控报告】"
    data = report.get("data") or {}
    feedback = report.get("feedback") or {}
    edm = report.get("edm") or {}
    system = report.get("system") or {}
    action_items = report.get("action_items") or []

    action_text = "\n".join(f"{idx + 1}. {item}" for idx, item in enumerate(action_items[:8])) or "暂无待处理事项"
    major_count = int(report.get("major_anomaly_count") or 0)
    color = "red" if major_count else ("orange" if int(report.get("warning_count") or 0) else "green")

    return card(
        title,
        [
            kv_lines([
                ("报告日期", report_date),
                ("整体结论", report.get("summary") or "无重大异常"),
            ]),
            "**一、核心数据监控**\n" + kv_lines([
                ("数据异常数", data.get("alert_count", 0)),
                ("严重异常数", data.get("critical_count", 0)),
                ("待关注指标", ", ".join(data.get("metrics", [])) or "无"),
            ]),
            "**二、点赞 / 点踩反馈**\n" + kv_lines([
                ("异常反馈数", feedback.get("alert_count", 0)),
                ("退费/取消订阅", feedback.get("refund_count", 0)),
                ("待人工处理", feedback.get("manual_count", 0)),
                ("自动处理失败", feedback.get("error_count", 0)),
            ]),
            "**三、EDM 自动回复**\n" + kv_lines([
                ("异常邮件数", edm.get("alert_count", 0)),
                ("高风险邮件", edm.get("high_risk_count", 0)),
                ("服务异常次数", edm.get("service_error_count", 0)),
                ("待人工处理", edm.get("manual_count", 0)),
            ]),
            "**四、系统状态**\n" + kv_lines([
                ("EDM 服务", system.get("edm_service", "未检查")),
                ("Feedback 服务", system.get("feedback_service", "未检查")),
                ("Dashboard 定时任务", system.get("dashboard_timers", "未检查")),
                ("xray 代理", system.get("xray", "未检查")),
                ("飞书推送", system.get("feishu", "正常")),
            ]),
            "**五、待处理事项**\n" + action_text,
        ],
        color,
    )


def build_card_for_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = event.get("event_type") or ""
    source = event.get("source") or ""
    if event_type == "data_anomaly" or source == "dashboard":
        return build_data_alert_card(event)
    if event_type == "feedback_alert" or source == "feedback":
        return build_feedback_alert_card(event)
    if event_type == "service_error":
        return build_service_error_card(event)
    if event_type == "edm_alert" or source == "edm":
        return build_edm_alert_card(event)
    return card(event.get("title") or "【自动化监控预警】", [kv_lines([("来源", source), ("类型", event_type), ("内容", event.get("payload"))])], "orange")


def feedback_anomaly_type(payload: dict[str, Any]) -> str:
    if payload.get("is_refund"):
        return "退费 / 取消订阅"
    status = str(payload.get("status") or payload.get("process_status") or "")
    if status == "error":
        return "流程异常"
    matched = payload.get("matched_faq")
    confidence = str(payload.get("match_confidence") or "")
    if not matched:
        return "匹配失败"
    if confidence in {"低", "无", "low", "none"}:
        return "内容模糊"
    return payload.get("reason_category") or "流程异常"


def status_label(status: Any) -> str:
    text = str(status or "").strip()
    mapping = {
        "notify_ops": "已进入人工干预",
        "notify_only": "已进入人工干预",
        "auto_reply": "已自动回复",
        "service_error": "自动重试中",
        "reply_failed": "自动处理失败",
        "error": "自动处理失败",
    }
    return mapping.get(text, text or "已进入人工干预")


def suggestion_for_metric(metric_name: str, direction: str = "") -> str:
    if metric_name == "registration_users":
        return "请检查官网访问、注册入口、登录服务、投放流量和埋点统计。"
    if metric_name in {"paid_users", "payment_amount", "paid_orders"}:
        return "请检查支付链路、价格页、订单回调、优惠活动和支付服务。"
    if metric_name == "renewal_orders":
        return "请检查订阅续费、支付回调、会员状态和历史订单同步。"
    return "请结合业务投放、产品链路、数据采集和服务日志排查。"


def window_text(event: dict[str, Any]) -> str:
    start = event.get("window_start") or ""
    end = event.get("window_end") or ""
    if start or end:
        return f"{start} ~ {end}"
    return "-"


def fmt_number(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except Exception:
        return str(value)
    if abs(number) >= 1000:
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    return f"{number:.4f}".rstrip("0").rstrip(".")


def pct(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value) * 100:.2f}%"
    except Exception:
        return str(value)


def parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None
