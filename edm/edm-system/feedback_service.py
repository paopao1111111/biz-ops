#!/usr/bin/env python3
"""
iWeaver 点踩点赞反馈自动回复系统

轮询 Superset 数据库获取用户反馈，使用 LLM 分析原因，
自动回复邮件并发送飞书通知。
"""
import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

# 添加 lib 到 path
sys.path.insert(0, '/opt/edm-system')

from lib.config import Config
from lib.superset_client import SupersetClient
from lib.feedback_analyzer import analyze_feedback
from lib.feishu_notifier import FeishuNotifier
from lib.gmail_client import GmailClient
import requests

MONITORING_PATH = '/opt/iweaver-monitoring'
if MONITORING_PATH not in sys.path:
    sys.path.insert(0, MONITORING_PATH)
try:
    from monitoring.api import safe_record_and_send_event
except Exception:  # monitoring must never break feedback service imports
    safe_record_and_send_event = None

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('/opt/edm-system/logs/feedback.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('feedback_service')

# 已处理 ID 记录文件
PROCESSED_IDS_FILE = Path('/opt/edm-system/data/processed_feedback_ids.json')


def load_processed_ids():
    """加载已处理的反馈 ID"""
    if PROCESSED_IDS_FILE.exists():
        try:
            data = json.loads(PROCESSED_IDS_FILE.read_text())
            return set(data.get('attitude_ids', [])), set(data.get('feedback_info_ids', []))
        except Exception as e:
            logger.error(f"Error loading processed IDs: {e}")
    return set(), set()


def save_processed_ids(attitude_ids, feedback_info_ids):
    """保存已处理的反馈 ID"""
    try:
        data = {
            'attitude_ids': list(attitude_ids)[-5000:],  # 保留最近 5000 条
            'feedback_info_ids': list(feedback_info_ids)[-5000:]
        }
        PROCESSED_IDS_FILE.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.error(f"Error saving processed IDs: {e}")


def send_monitoring_feedback_alert(feedback, analysis, status):
    """Best-effort unified monitoring alert for abnormal feedback records."""
    if safe_record_and_send_event is None:
        return {"send": {"sent": False, "error": "monitoring unavailable"}}
    try:
        analysis = analysis or {}
        feedback_id = str(feedback.get('id', ''))
        user_id = str(feedback.get('user_id', ''))
        fb_content = feedback.get('feedback_content') or feedback.get('chat_content') or ''
        is_refund = bool(analysis.get('is_refund'))
        severity = 'high' if status in ('refund_pending', 'error') or is_refund else 'warning'
        anomaly_type = '退费 / 取消订阅' if is_refund else ''
        payload = {
            'feedback_source': '点赞/点踩',
            'user_id': user_id,
            'feedback_id': feedback_id,
            'feedback_time': feedback.get('created_time') or feedback.get('created_at') or '',
            'type': feedback.get('type', ''),
            'feedback_content': fb_content,
            'status': status,
            'process_status': status,
            'anomaly_type': anomaly_type,
            'reason_category': analysis.get('reason_category', ''),
            'problem_summary': analysis.get('problem_summary', ''),
            'matched_faq': analysis.get('matched_faq'),
            'match_confidence': analysis.get('match_confidence', ''),
            'is_refund': is_refund,
            'email': feedback.get('user_email') or feedback.get('email') or '',
            'feishu_summary': analysis.get('feishu_summary', ''),
        }
        return safe_record_and_send_event(
            event_key=f"feedback:{feedback_id or user_id}:{status}",
            source='feedback',
            event_type='feedback_alert',
            severity=severity,
            title='点踩点赞用户反馈异常预警',
            object_id=feedback_id,
            user_id=user_id,
            user_email=feedback.get('user_email') or feedback.get('email') or '',
            status=status,
            payload=payload,
        )
    except Exception as e:
        logger.warning(f'Monitoring feedback alert failed: {e}', exc_info=True)
        return {"send": {"sent": False, "error": str(e)}}


def send_feishu_notification(notifier, feedback, analysis, status):
    """发送飞书通知卡片"""
    try:
        # 根据状态选择颜色
        color_map = {
            'auto_replied': 'green',
            'refund_pending': 'red',
            'manual_required': 'orange',
            'no_email': 'yellow',
            'error': 'red'
        }
        color = color_map.get(status, 'blue')

        # 构建卡片内容
        # 处理状态映射
        status_map = {
            'auto_replied': '✅ 已自动回复',
            'refund_pending': '🔴 退费待处理',
            'manual_required': '⏳ 需人工处理',
            'no_email': '📭 无邮箱无法回复',
            'error': '❌ 处理出错'
        }
        status_text = status_map.get(status, status)

        # 构建反馈内容（优先 feedback_content，回退 chat_content）
        fb_content = feedback.get('feedback_content') or feedback.get('chat_content') or 'N/A'
        if fb_content and len(fb_content) > 200:
            fb_content = fb_content[:200] + '...'

        elements = [
            {
                "tag": "markdown",
                "content": f"**用户**: {feedback.get('user_name', 'Unknown')}\n"
                          f"**邮箱**: {feedback.get('user_email') or feedback.get('email') or 'N/A'}\n"
                          f"**反馈类型**: {feedback.get('type', 'N/A')}\n"
                          f"**反馈ID**: {feedback.get('id', 'N/A')}\n"
                          f"**时间**: {feedback.get('created_time', feedback.get('created_at', 'N/A'))}"
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**用户反馈**:\n{fb_content}"
            },
            {"tag": "hr"},
        ]

        if analysis:
            elements.append({
                "tag": "markdown",
                "content": f"**问题分类**: {analysis.get('reason_category', 'N/A')}\n"
                          f"**问题总结**: {analysis.get('problem_summary', 'N/A')}\n"
                          f"**匹配FAQ**: {analysis.get('matched_faq') or '无'}\n"
                          f"**置信度**: {analysis.get('match_confidence', 'N/A')}\n"
                          f"**是否退费**: {'是' if analysis.get('is_refund') else '否'}\n"
                          f"**飞书摘要**: {analysis.get('feishu_summary', 'N/A')}"
            })
            elements.append({"tag": "hr"})

        elements.append({
            "tag": "markdown",
            "content": f"**处理状态**: {status_text}"
        })

        # 异常状态优先使用统一监控模板；失败时回退原卡片，避免影响业务流程。
        if status in {'refund_pending', 'manual_required', 'no_email', 'error'}:
            monitor_result = send_monitoring_feedback_alert(feedback, analysis, status)
            send = monitor_result.get('send') or {}
            if send.get('sent') or send.get('skipped'):
                logger.info(f"Unified monitoring feedback alert sent: {feedback.get('id')}")
                return

        # 发送原卡片作为非异常通知或统一模板失败后的兜底
        title = "iWeaver 用户反馈通知"
        notifier.send_card(title, elements, color=color)
        logger.info(f"Feishu notification sent: {feedback.get('id')}")

    except Exception as e:
        logger.error(f"Error sending feishu notification: {e}")


def append_to_feishu_sheet(row_data):
    """追加数据到飞书电子表格"""
    try:
        # 获取 tenant_access_token
        auth_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        auth_resp = requests.post(auth_url, json={
            "app_id": Config.FEISHU_SHEET_APP_ID,
            "app_secret": Config.FEISHU_SHEET_APP_SECRET
        }, timeout=10)
        auth_resp.raise_for_status()
        token = auth_resp.json().get("tenant_access_token")

        if not token:
            logger.error("Failed to get feishu sheet token")
            return False

        # 追加数据
        sheet_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{Config.FEISHU_FEEDBACK_SHEET_TOKEN}/values_append"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        data = {
            "valueRange": {
                "range": Config.FEISHU_FEEDBACK_SHEET_ID,
                "values": [row_data]
            }
        }

        resp = requests.post(sheet_url, headers=headers, json=data, timeout=10)
        resp.raise_for_status()
        logger.info(f"Recorded to sheet: {row_data[0]}")  # row_data[0] is timestamp
        return True

    except Exception as e:
        logger.error(f"Error appending to sheet: {e}")
        return False


def record_to_feishu_sheet(feedback, analysis, status):
    """记录到飞书电子表格"""
    try:
        fb_content = feedback.get('feedback_content') or feedback.get('chat_content') or ''
        row = [
            datetime.now().isoformat(),
            str(feedback.get('id', '')),
            str(feedback.get('user_id', '')),
            feedback.get('user_name', ''),
            (feedback.get('user_email') or feedback.get('email') or ''),
            feedback.get('type', ''),
            fb_content[:500] if fb_content else '',
            (analysis.get('reason_category', '') if analysis else ''),
            (analysis.get('problem_summary', '') if analysis else ''),
            (analysis.get('matched_faq', '') if analysis else ''),
            (analysis.get('match_confidence', '') if analysis else ''),
            '是' if (analysis and analysis.get('is_refund')) else '否',
            status,
            (analysis.get('feishu_summary', '') if analysis else '')
        ]

        # 写入飞书表格
        append_to_feishu_sheet(row)

    except Exception as e:
        logger.error(f"Error recording to sheet: {e}")


def is_positive_feedback(feedback):
    """Return True for simple thumbs-up/positive ratings that should not page Feishu."""
    value = str(feedback.get('type', '')).strip().lower()
    return value in {'1', 'true', 'thumbs_up', 'thumb_up', 'up', 'like', 'liked', 'positive'}


def has_text_feedback(feedback):
    """Return True when a record has user-written feedback text worth notifying about."""
    text = feedback.get('feedback_content') or feedback.get('chat_content') or ''
    return bool(str(text).strip())


def send_reply_email(gmail_client, feedback, analysis):
    """发送回复邮件"""
    try:
        email = feedback.get('user_email') or feedback.get('email')
        if not email:
            return False

        subject = analysis.get('email_subject', '')
        body = analysis.get('email_body', '')

        if not subject or not body:
            logger.warning(f"No email content generated for feedback {feedback.get('id')}")
            return False

        # 构建邮件 HTML
        body_html = f"<html><body><p>{body.replace(chr(10), '<br>')}</p></body></html>"

        # 发送回复（不使用 thread_id，因为是新邮件）
        gmail_client.send_reply(
            thread_id=None,
            to=email,
            subject=subject,
            body_html=body_html,
            in_reply_to=None
        )
        logger.info(f"Reply email sent to {email}: {feedback.get('id')}")
        return True

    except Exception as e:
        logger.error(f"Error sending reply email: {e}")
        return False


def process_feedback(client, notifier, gmail_client, processed_attitude_ids, processed_feedback_info_ids):
    """处理新的反馈"""
    processed_count = 0

    try:
        # 获取最近 10 分钟的反馈
        minutes = Config.FEEDBACK_TIME_WINDOW_MINUTES

        # 处理 feedback_info 表（优先）
        feedback_info_list = client.get_recent_feedback_info(minutes, limit=20)
        for feedback in feedback_info_list:
            fid = feedback.get('id')
            if fid in processed_feedback_info_ids:
                continue

            logger.info(f"Processing feedback_info {fid}")

            # 跳过无文字的反馈
            if not feedback.get('feedback_content'):
                logger.info(f"Skipping feedback_info {fid}: no content")
                processed_feedback_info_ids.add(fid)
                continue

            # 分析反馈
            analysis = analyze_feedback(feedback)

            # 确定处理状态
            status = 'error'
            if analysis:
                if analysis.get('is_refund'):
                    status = 'refund_pending'
                elif analysis.get('matched_faq') and analysis.get('match_confidence') in ['高', '中']:
                    # External replies are disabled by default; keep Feishu/sheet notifications only.
                    if Config.FEEDBACK_AUTO_REPLY_ENABLED and (feedback.get('user_email') or feedback.get('email')):
                        if send_reply_email(gmail_client, feedback, analysis):
                            status = 'auto_replied'
                        else:
                            status = 'manual_required'
                    elif feedback.get('user_email') or feedback.get('email'):
                        status = 'manual_required'
                    else:
                        status = 'no_email'
                else:
                    status = 'manual_required'

            # 发送飞书通知
            send_feishu_notification(notifier, feedback, analysis, status)

            # 记录到飞书表格
            record_to_feishu_sheet(feedback, analysis, status)

            processed_feedback_info_ids.add(fid)
            processed_count += 1

        # 处理 attitude 表
        attitude_list = client.get_recent_feedback(minutes, limit=20)
        for feedback in attitude_list:
            fid = feedback.get('id')
            if fid in processed_attitude_ids:
                continue

            # Plain thumbs-up ratings and empty ratings are useful for aggregate stats,
            # but they are too noisy for one-by-one Feishu notifications. Record them
            # to the sheet, mark processed, and do not call LLM/Feishu.
            if is_positive_feedback(feedback):
                logger.info(f"Skipping attitude {fid}: positive rating only")
                record_to_feishu_sheet(feedback, None, 'positive_rating_only')
                processed_attitude_ids.add(fid)
                processed_count += 1
                continue

            if not has_text_feedback(feedback):
                logger.info(f"Skipping attitude {fid}: no feedback content")
                record_to_feishu_sheet(feedback, None, 'no_content')
                processed_attitude_ids.add(fid)
                processed_count += 1
                continue

            logger.info(f"Processing attitude {fid}")

            # 分析反馈
            analysis = analyze_feedback(feedback)

            # 确定处理状态
            status = 'error'
            if analysis:
                if analysis.get('is_refund'):
                    status = 'refund_pending'
                elif analysis.get('matched_faq') and analysis.get('match_confidence') in ['高', '中']:
                    # External replies are disabled by default; keep Feishu/sheet notifications only.
                    if Config.FEEDBACK_AUTO_REPLY_ENABLED and (feedback.get('user_email') or feedback.get('email')):
                        if send_reply_email(gmail_client, feedback, analysis):
                            status = 'auto_replied'
                        else:
                            status = 'manual_required'
                    elif feedback.get('user_email') or feedback.get('email'):
                        status = 'manual_required'
                    else:
                        status = 'no_email'
                else:
                    status = 'manual_required'

            # 发送飞书通知
            send_feishu_notification(notifier, feedback, analysis, status)

            # 记录到飞书表格
            record_to_feishu_sheet(feedback, analysis, status)

            processed_attitude_ids.add(fid)
            processed_count += 1

        logger.info(f"Processed {processed_count} new feedbacks")

    except Exception as e:
        logger.error(f"Error in process_feedback: {e}")

    return processed_count


def main():
    """主循环"""
    logger.info("Feedback auto-reply service starting")

    # 初始化客户端
    try:
        client = SupersetClient()
        logger.info("Superset client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Superset client: {e}")
        sys.exit(1)

    try:
        notifier = FeishuNotifier()
        logger.info("Feishu notifier initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Feishu notifier: {e}")
        sys.exit(1)

    try:
        gmail_client = GmailClient()
        logger.info("Gmail client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Gmail client: {e}")
        sys.exit(1)

    # 加载已处理 ID
    processed_attitude_ids, processed_feedback_info_ids = load_processed_ids()
    logger.info(f"Loaded {len(processed_attitude_ids)} attitude IDs, {len(processed_feedback_info_ids)} feedback_info IDs")

    # 主循环
    interval = Config.FEEDBACK_POLL_INTERVAL
    logger.info(f"Polling interval: {interval} seconds")

    while True:
        try:
            count = process_feedback(client, notifier, gmail_client,
                                     processed_attitude_ids,
                                     processed_feedback_info_ids)

            # 保存已处理 ID
            if count > 0:
                save_processed_ids(processed_attitude_ids, processed_feedback_info_ids)

        except Exception as e:
            logger.error(f"Error in main loop: {e}")

        time.sleep(interval)


if __name__ == '__main__':
    main()
