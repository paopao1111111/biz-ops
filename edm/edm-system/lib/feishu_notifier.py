"""Feishu notifier with rich interactive cards"""
import json
import requests
import logging
import time
import sys
from datetime import datetime
from lib.config import Config

MONITORING_PATH = '/opt/iweaver-monitoring'
if MONITORING_PATH not in sys.path:
    sys.path.insert(0, MONITORING_PATH)
try:
    from monitoring.api import safe_record_and_send_event
except Exception:  # monitoring must never break EDM imports
    safe_record_and_send_event = None

logger = logging.getLogger(__name__)

class FeishuNotifier:
    def __init__(self):
        self.app_id = Config.FEISHU_APP_ID
        self.app_secret = Config.FEISHU_APP_SECRET
        self.chat_id = Config.FEISHU_CHAT_ID
        self.notify_user_id = Config.FEISHU_NOTIFY_USER_ID
        self._token = None
        self._token_expires_at = 0
    
    def _get_token(self):
        if self._token and time.time() < self._token_expires_at:
            return self._token
        
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(url, json={
            "app_id": self.app_id,
            "app_secret": self.app_secret
        })
        data = resp.json()
        if data.get("code") == 0:
            self._token = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            self._token_expires_at = time.time() + expire - 300
            logger.info("Feishu token refreshed, expires in %ds", expire)
            return self._token
        raise Exception(f"Failed to get token: {data}")
    
    def send_card(self, title: str, elements: list, color: str = "green"):
        """
        Send interactive card to Feishu
        elements: list of element dicts (markdown, divider, note, etc.)
        color: green/red/orange
        """
        if not self.chat_id:
            logger.warning("FEISHU_CHAT_ID not configured")
            return False
        
        token = self._get_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color
            },
            "elements": elements
        }
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "receive_id": self.chat_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False)
        }
        
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        
        if data.get("code") == 0:
            logger.info("Feishu card sent: %s", title)
            return True
        else:
            logger.error("Feishu card failed: %s", data)
            return False
    
    def _send_monitoring_event(self, **kwargs):
        if safe_record_and_send_event is None:
            return {"send": {"sent": False, "error": "monitoring unavailable"}}
        try:
            return safe_record_and_send_event(**kwargs)
        except Exception as e:
            logger.warning("Monitoring event send failed: %s", e, exc_info=True)
            return {"send": {"sent": False, "error": str(e)}}

    def _build_email_info(self, email):
        """Build email info markdown block"""
        lines = []
        if email.get('from'):
            lines.append(f"**发件人：** {email['from']}")
        if email.get('subject'):
            lines.append(f"**主题：** {email['subject']}")
        if email.get('date'):
            lines.append(f"**时间：** {email['date']}")
        if email.get('snippet'):
            # Truncate snippet to 200 chars
            snippet = email['snippet'][:200] + "..." if len(email['snippet']) > 200 else email['snippet']
            lines.append(f"**摘要：** {snippet}")
        return "\n".join(lines)
    
    def notify_auto_reply(self, email, category, faq_id=None, reply_preview=""):
        """Green card: auto reply success"""
        elements = [
            {
                "tag": "markdown",
                "content": self._build_email_info(email)
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**分类：** {category}\n**匹配 FAQ：** #{faq_id}\n**处理方式：** ✅ 自动回复"
            }
        ]
        if reply_preview:
            elements.append({
                "tag": "markdown",
                "content": f"**回复预览：**\n{reply_preview[:300]}"
            })
        
        return self.send_card("[EDM] 已自动回复", elements, "green")
    
    def notify_high_risk(self, email, category, error="", llm_analysis="", process_status="已进入人工干预"):
        """Red card: high risk / needs manual handling"""
        elements = [
            {
                "tag": "markdown",
                "content": self._build_email_info(email)
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**分类：** {category}\n**风险等级：** 🔴 高风险\n**处理方式：** {process_status}"
            }
        ]
        
        if llm_analysis:
            elements.append({
                "tag": "markdown",
                "content": f"**AI 分析：**\n{llm_analysis[:500]}"
            })
        
        if error:
            elements.append({
                "tag": "markdown",
                "content": f"**错误信息：** {error}"
            })
        
        # Add @mention
        if self.notify_user_id:
            elements.append({
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"<at id={self.notify_user_id}></at> 请及时处理退费/取消请求"
                    }
                ]
            })
        
        payload = {
            "email": email.get('from', ''),
            "from": email.get('from', ''),
            "subject": email.get('subject', ''),
            "email_time": email.get('date', ''),
            "category": category,
            "anomaly_type": category,
            "snippet": email.get('snippet', ''),
            "content": email.get('body', '')[:1000] if email.get('body') else email.get('snippet', ''),
            "action": "notify_ops",
            "process_status": process_status,
            "llm_analysis": llm_analysis or "",
            "error": error or "",
        }
        event_key = f"edm:{email.get('message_id') or email.get('thread_id') or email.get('from', '')}:{category}:high_risk"
        result = self._send_monitoring_event(
            event_key=event_key,
            source="edm",
            event_type="edm_alert",
            severity="high",
            title="EDM 自动回复异常预警",
            object_id=email.get('message_id', ''),
            user_email=email.get('from', ''),
            status=category,
            payload=payload,
        )
        send = result.get("send") or {}
        if send.get("sent") or send.get("skipped"):
            return True
        return self.send_card("[EDM] 高风险 - 需人工处理", elements, "red")

    def notify_service_error(self, error="", fail_count=0):
        """Red card: EDM service/network error, not a customer high-risk request."""
        elements = [
            {
                "tag": "markdown",
                "content": (
                    "**分类：** service_error\\n"
                    "**风险等级：** 🔴 服务异常\\n"
                    "**处理方式：** 自动重试中，需检查 Gmail/代理连接\\n"
                    f"**连续失败次数：** {fail_count}"
                )
            }
        ]

        if error:
            elements.append({
                "tag": "markdown",
                "content": f"**错误信息：** {error}"
            })

        if self.notify_user_id:
            elements.append({
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": f"<at id={self.notify_user_id}></at> 请检查 EDM 服务的 Gmail/代理连接；这不是退费/取消请求"
                    }
                ]
            })

        event_key = f"edm:service_error:{datetime.utcnow().strftime('%Y%m%d%H')}:{str(error)[:120]}"
        result = self._send_monitoring_event(
            event_key=event_key,
            source="edm",
            event_type="service_error",
            severity="critical",
            title="EDM 服务异常",
            object_id="edm-auto-reply",
            status="service_error",
            payload={
                "module": "EDM 自动回复",
                "anomaly_type": "服务异常",
                "error": error or "",
                "fail_count": fail_count,
                "process_status": "自动重试中",
            },
        )
        send = result.get("send") or {}
        if send.get("sent") or send.get("skipped"):
            return True
        return self.send_card("[EDM] 服务异常 - 自动重试中", elements, "red")

    
    def notify_pending(self, email, category, reason="", llm_analysis=""):
        """Orange card: pending manual handling"""
        elements = [
            {
                "tag": "markdown",
                "content": self._build_email_info(email)
            },
            {"tag": "hr"},
            {
                "tag": "markdown",
                "content": f"**分类：** {category}\n**处理方式：** ⏳ 待人工处理"
            }
        ]
        
        if llm_analysis:
            elements.append({
                "tag": "markdown",
                "content": f"**AI 分析：**\n{llm_analysis[:500]}"
            })
        
        if reason:
            elements.append({
                "tag": "markdown",
                "content": f"**原因：** {reason}"
            })
        
        payload = {
            "email": email.get('from', ''),
            "from": email.get('from', ''),
            "subject": email.get('subject', ''),
            "email_time": email.get('date', ''),
            "category": category,
            "anomaly_type": category,
            "snippet": email.get('snippet', ''),
            "content": email.get('body', '')[:1000] if email.get('body') else email.get('snippet', ''),
            "action": "notify_only",
            "process_status": "已进入人工干预",
            "reason": reason or "",
            "llm_analysis": llm_analysis or "",
        }
        event_key = f"edm:{email.get('message_id') or email.get('thread_id') or email.get('from', '')}:{category}:pending"
        result = self._send_monitoring_event(
            event_key=event_key,
            source="edm",
            event_type="edm_alert",
            severity="warning",
            title="EDM 自动回复异常预警",
            object_id=email.get('message_id', ''),
            user_email=email.get('from', ''),
            status=category,
            payload=payload,
        )
        send = result.get("send") or {}
        if send.get("sent") or send.get("skipped"):
            return True
        return self.send_card("[EDM] 待人工处理", elements, "orange")


    def append_to_sheet(self, sheet_token, sheet_id, data_rows):
        """Append data rows to Feishu sheet"""
        try:
            # Use sheet-specific credentials
            url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
            resp = requests.post(url, json={
                "app_id": Config.FEISHU_SHEET_APP_ID,
                "app_secret": Config.FEISHU_SHEET_APP_SECRET
            }, timeout=10)
            auth_data = resp.json()
            if auth_data.get("code") != 0:
                logger.error("Failed to get sheet token: %s", auth_data)
                return False

            token = auth_data["tenant_access_token"]

            # Append data
            sheet_url = f"https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{sheet_token}/values_append"
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            payload = {
                "valueRange": {
                    "range": sheet_id,
                    "values": data_rows
                }
            }

            resp = requests.post(sheet_url, headers=headers, json=payload, timeout=10)
            result = resp.json()

            if result.get("code") == 0:
                logger.info("Appended %d rows to sheet", len(data_rows))
                return True
            else:
                logger.error("Failed to append to sheet: %s", result)
                return False

        except Exception as e:
            logger.error("Error appending to sheet: %s", e)
            return False
