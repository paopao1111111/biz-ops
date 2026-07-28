#!/opt/edm-system/.venv/bin/python
"""EDM Auto Reply Service - Main entry point"""
import sys
import re
import time
import html
import logging
from datetime import datetime
from pathlib import Path

# Add lib to path
sys.path.insert(0, '/opt/edm-system')

from lib.config import Config
from lib.gmail_client import GmailClient
from lib.llm_client import call_llm
from lib.filter_rules import is_sender_blacklisted, is_subject_blacklisted, is_bulk_or_marketing_email, contains_high_risk_keywords
from lib.faq_matcher import FAQMatcher
from lib.email_utils import detect_language, extract_latest_reply
from lib.feishu_notifier import FeishuNotifier
from lib.queue import EmailQueue

# Setup logging
# Keep INFO logs in edm.log, and write only WARNING/ERROR to edm-error.log.
# Do not attach a stderr StreamHandler here: systemd redirects stderr to
# edm-error.log, so a normal StreamHandler would make INFO lines look like errors.
log_format = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
edm_log_handler = logging.FileHandler('/opt/edm-system/logs/edm.log')
edm_error_handler = logging.FileHandler('/opt/edm-system/logs/edm-error.log')
edm_error_handler.setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[edm_log_handler, edm_error_handler]
)
logger = logging.getLogger('edm_service')

class EDMService:
    REFUND_DRAFT_CATEGORIES = {'refund_request'}
    REFUND_DRAFT_KEYWORDS = [
        'refund', 'money back', 'refund request', 'chargeback',
        '退款', '退费', '申请退款', '退钱',
        'reembolso', 'devolución', 'devolucion',
    ]

    def __init__(self):
        self.gmail = GmailClient()
        self.faq_matcher = FAQMatcher()
        self.feishu = FeishuNotifier()
        self.queue = EmailQueue(Config.QUEUE_DB_PATH)
        self.poll_interval = Config.EDM_POLL_INTERVAL
        self.consecutive_fail_count = 0
        self.MAX_FAILS_BEFORE_ALERT = Config.EDM_SERVICE_ERROR_ALERT_THRESHOLD
    
    def run(self):
        """Main loop"""
        logger.info("EDM Service started, polling every %d seconds", self.poll_interval)
        
        while True:
            try:
                self.poll_and_process()
                if self.consecutive_fail_count > 0:
                    logger.info("Poll recovered after %d failures", self.consecutive_fail_count)
                    self.consecutive_fail_count = 0
            except Exception as e:
                self.consecutive_fail_count += 1
                logger.error("Poll failed (%d/%d): %s", self.consecutive_fail_count, self.MAX_FAILS_BEFORE_ALERT, e, exc_info=True)
                if self.consecutive_fail_count >= self.MAX_FAILS_BEFORE_ALERT:
                    self.feishu.notify_service_error(str(e), self.consecutive_fail_count)
                    self.consecutive_fail_count = 0
            
            time.sleep(self.poll_interval)
    
    def poll_and_process(self):
        """Poll Gmail and process new emails"""
        last_error = None
        for attempt in range(1, 4):
            try:
                emails = self.gmail.poll_messages(max_results=20)
                break
            except Exception as e:
                last_error = e
                if attempt >= 3:
                    raise
                logger.info("Gmail poll attempt %d/3 failed, rebuilding client and retrying: %s", attempt, e)
                try:
                    self.gmail = GmailClient()
                except Exception as rebuild_error:
                    logger.warning("Failed to rebuild Gmail client after poll error: %s", rebuild_error)
                time.sleep(5 * attempt)
        else:
            raise last_error
        
        logger.info("Polled %d messages", len(emails))
        
        for email in emails:
            try:
                self.process_email(email)
            except Exception as e:
                logger.error("Process email failed (%s): %s", email.get('message_id', ''), e, exc_info=True)
    
    def mark_read_safely(self, message_id: str, reason: str = ""):
        """Mark a Gmail message as read without failing the poll loop."""
        if not message_id:
            return
        try:
            self.gmail.mark_read(message_id)
            logger.debug("Marked read (%s): %s", reason, message_id)
        except Exception as e:
            logger.warning("Failed to mark read (%s): %s", message_id, e)

    def reply_to_address(self, sender: str) -> str:
        """Extract the real email address from a From header."""
        sender = sender or ""
        match = re.search(r"<([^>]+)>", sender)
        if match:
            return match.group(1).strip()
        match = re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sender)
        return match.group(0).strip() if match else sender.strip()

    def is_refund_related(self, body: str) -> bool:
        """Return True only when the user's latest email body asks for a refund."""
        text = (body or "").lower()
        return any(keyword.lower() in text for keyword in self.REFUND_DRAFT_KEYWORDS)

    def is_refund_faq(self, faq: dict | None) -> bool:
        """Identify refund FAQ entries without using FAQ metadata as user intent."""
        if not faq:
            return False
        faq_text = f"{faq.get('scenario', '')} {' '.join(faq.get('keywords') or [])}".lower()
        return any(keyword.lower() in faq_text for keyword in self.REFUND_DRAFT_KEYWORDS)

    def as_html(self, text: str) -> str:
        """Convert plain draft text to simple HTML while preserving existing HTML."""
        text = (text or "").strip()
        if not text:
            return ""
        if re.search(r"<\s*(p|br|div|html|body|ul|ol|li)\b", text, re.I):
            return text
        return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"

    def build_refund_draft_reply(self, email: dict, category: str, suggested_reply: str = "") -> str:
        """Build a fixed refund-processing draft for human review."""
        language = (email.get('language') or 'en').split('-')[0].lower()
        templates = {
            'zh': "您好，\n\n您的退款请求已收到。退款通常会在 7-15 个工作日内到账；在此期间，您的会员权益会保留到当前计费周期结束。\n\n如有其他问题，可以继续回复这封邮件。\n\n谢谢。",
            'es': "Hola,\n\nHemos recibido tu solicitud de reembolso. Los reembolsos normalmente llegan en un plazo de 7 a 15 días hábiles; mientras tanto, tu membresía seguirá activa hasta el final del período de facturación actual.\n\nSi tienes alguna otra pregunta, puedes responder a este correo.\n\nGracias.",
            'de': "Hallo,\n\nwir haben Ihre Rückerstattungsanfrage erhalten. Rückerstattungen werden in der Regel innerhalb von 7–15 Werktagen gutgeschrieben; Ihre Mitgliedschaft bleibt bis zum Ende des aktuellen Abrechnungszeitraums aktiv.\n\nWenn Sie weitere Fragen haben, antworten Sie bitte auf diese E-Mail.\n\nVielen Dank.",
            'en': "Hello,\n\nWe have received your refund request. Refunds are usually credited within 7-15 business days; your membership will remain active until the end of the current billing period.\n\nIf you have any other questions, please reply to this email.\n\nThank you.",
        }
        return self.as_html(templates.get(language) or templates['en'])

    def create_refund_reply_draft(self, email: dict, category: str, suggested_reply: str = "") -> dict:
        """Create a Gmail draft for refund-related messages; never sends the email."""
        to_addr = self.reply_to_address(email.get('from', ''))
        subject = email.get('subject', '') or ''
        reply_subject = subject if subject.lower().startswith('re:') else f"Re: {subject}"
        body_html = self.build_refund_draft_reply(email, category, suggested_reply)
        result = self.gmail.create_reply_draft(
            thread_id=email.get('thread_id', ''),
            to=to_addr,
            subject=reply_subject,
            body_html=body_html,
            in_reply_to=email.get('message_id', ''),
        )
        draft_id = result.get('id', '') if isinstance(result, dict) else ''
        logger.info("Created refund reply draft: message=%s draft=%s category=%s", email.get('message_id', ''), draft_id, category)
        return {'success': True, 'draft_id': draft_id, 'to': to_addr}

    def process_email(self, email):
        """Process single email"""
        message_id = email.get('message_id', '')
        sender = email.get('from', '')
        subject = email.get('subject', '')
        body = email.get('body', '')
        latest_body = extract_latest_reply(body)
        # Detect reply language from the customer's latest body first. Subjects can
        # be inherited from older threads or our own localized replies.
        language = detect_language(latest_body or subject)
        email_for_reply = dict(email)
        email_for_reply['body'] = latest_body
        email_for_reply['snippet'] = latest_body[:200] if latest_body else email.get('snippet', '')
        email_for_reply['language'] = language
        
        logger.info("Processing: %s from %s", subject[:50], sender)
        logger.info("Latest reply extracted: language=%s chars=%d", language, len(latest_body))
        
        # Layer 1: Sender blacklist
        if is_sender_blacklisted(sender):
            logger.info("Sender blacklisted: %s", sender)
            self.mark_read_safely(message_id, "blacklisted sender")
            return
        
        # Layer 1: Subject blacklist
        if is_subject_blacklisted(subject):
            logger.info("Subject blacklisted: %s", subject)
            self.mark_read_safely(message_id, "blacklisted subject")
            return
        
        # Layer 1: Bulk/newsletter/marketing emails should never be auto-replied.
        if is_bulk_or_marketing_email(sender, subject, body, email.get('headers', {})):
            logger.info("Bulk/marketing email discarded: %s from %s", subject[:80], sender)
            self.mark_read_safely(message_id, "bulk marketing")
            return
        
        # Check if already processed
        if self.queue.is_processed(message_id):
            logger.info("Already processed: %s", message_id)
            self.mark_read_safely(message_id, "already processed")
            return
        
        # Layer 2: FAQ keyword matching (fast, no LLM)
        faq = self.faq_matcher.match(subject, latest_body)
        if faq and self.is_refund_faq(faq) and not self.is_refund_related(latest_body):
            logger.info("Ignoring refund FAQ match because latest email body is not refund-related: #%d", faq.get("id", 0))
            faq = None

        if faq:
            category = "faq_question"
            logger.info("FAQ matched: #%d %s", faq["id"], faq["scenario"])

            # Refund-related FAQ replies must be drafted for manual review, never sent.
            if self.is_refund_related(latest_body):
                refund_category = "refund_request"
                try:
                    draft = self.create_refund_reply_draft(email_for_reply, refund_category)
                    draft_note = f"已创建 Gmail 回复草稿（draft_id: {draft.get('draft_id') or '-'}），未发送邮件。"
                    process_status = "已创建草稿，待人工确认发送"
                except Exception as e:
                    logger.error("Create refund draft failed: %s", e, exc_info=True)
                    draft_note = f"Gmail 草稿创建失败：{e}；未发送邮件，请人工处理。"
                    process_status = "草稿创建失败，已进入人工干预"
                self.queue.mark_processed(message_id, refund_category, faq_id=faq["id"])
                self.feishu.notify_high_risk(
                    email_for_reply,
                    refund_category,
                    llm_analysis=draft_note,
                    process_status=process_status,
                )
                self.mark_read_safely(message_id, "refund draft created")
                logger.info("Refund FAQ drafted and notified: #%d", faq["id"])
                return

            if not Config.EDM_AUTO_REPLY_ENABLED:
                self.queue.mark_processed(message_id, category, faq_id=faq["id"])
                self.feishu.notify_pending(
                    email_for_reply,
                    category,
                    f"FAQ #{faq['id']} matched ({faq['scenario']}), but external auto-reply is disabled"
                )
                self.mark_read_safely(message_id, "faq matched auto-reply disabled")
                logger.info("FAQ matched but external auto-reply disabled: #%d", faq["id"])
                return
            
            # Send external reply only when explicitly enabled.
            reply = self.faq_matcher.render_template(faq, language)
            try:
                self.gmail.send_reply(
                    thread_id=email.get('thread_id', ''),
                    to=(re.search(r"<([^>]+)>", sender).group(1) if "<" in sender else sender),
                    subject=f"Re: {subject}",
                    body_html=reply,
                    in_reply_to=message_id
                )
                self.queue.mark_processed(message_id, category, faq_id=faq["id"])
                self.feishu.notify_auto_reply(email_for_reply, category, faq["id"], reply_preview=reply)
                self.mark_read_safely(message_id, "auto replied")
                logger.info("Auto replied with FAQ #%d", faq["id"])
            except Exception as e:
                logger.error("Reply failed: %s", e)
                self.queue.mark_failed(message_id, str(e))
                # Mark as processed/read after notifying ops so transient Gmail errors do not
                # cause duplicate auto-replies on the next poll.
                self.queue.mark_processed(message_id, f"{category}_reply_failed", faq_id=faq["id"])
                self.feishu.notify_pending(email_for_reply, category, f"Reply failed: {e}")
                self.mark_read_safely(message_id, "reply failed")
            return
        
        # Layer 3: LLM classification (only if FAQ didn't match)
        classification = self.classify_with_llm(email_for_reply)
        category = classification.get("category", "other")
        refund_related = self.is_refund_related(latest_body)
        if refund_related and category != "refund_request":
            category = "refund_request"
        is_high_risk = classification.get("is_high_risk", False) or contains_high_risk_keywords(latest_body)
        action = classification.get("action", "notify_only")
        
        logger.info("LLM classified: %s (high_risk=%s, action=%s)", category, is_high_risk, action)
        
        # HARD RULE: High-risk categories must NOT auto-reply
        HIGH_RISK_CATEGORIES = ['cancel_subscription', 'refund_request', 'billing_issue', 'delete_account', 'spam']
        if category in HIGH_RISK_CATEGORIES:
            action = 'notify_ops'
            logger.info("Hard rule applied: %s -> notify_ops", category)
        
        # LLM classified emails: NEVER auto-reply
        # Only FAQ matches (Layer 2) should auto-reply
        self.queue.mark_processed(message_id, category)
        
        if category == 'spam':
            # Spam: silently discard, no notification
            logger.info("Spam discarded: %s", category)
        elif refund_related:
            try:
                draft = self.create_refund_reply_draft(email_for_reply, category)
                draft_note = f"已创建 Gmail 回复草稿（draft_id: {draft.get('draft_id') or '-'}），未发送邮件。"
                process_status = "已创建草稿，待人工确认发送"
            except Exception as e:
                logger.error("Create refund draft failed: %s", e, exc_info=True)
                draft_note = f"Gmail 草稿创建失败：{e}；未发送邮件，请人工处理。"
                process_status = "草稿创建失败，已进入人工干预"
            self.feishu.notify_high_risk(
                email_for_reply,
                category,
                llm_analysis=draft_note,
                process_status=process_status,
            )
            logger.info("Refund-related email drafted and notified: %s", category)
        elif is_high_risk or category in ['cancel_subscription', 'refund_request', 'billing_issue', 'delete_account']:
            self.feishu.notify_high_risk(email_for_reply, category)
            logger.info("Notified ops (high risk): %s", category)
        else:
            self.feishu.notify_pending(email_for_reply, category)
            logger.info("Pending manual handling: %s", category)
        
        self.mark_read_safely(message_id, category)
    
    def classify_with_llm(self, email: dict) -> dict:
        """Use LLM to classify email"""
        prompt = f"""Analyze this email and return JSON:

From: {email.get('from', '')}
Subject: {email.get('subject', '')}
Body: {email.get('body', '')[:2000]}

Return JSON with:
- category: one of [faq_question, refund_request, cancel_subscription, billing_issue, bug_report, feature_request, partnership, delete_account, invoice_request, spam, other]
- For refund_request, decide ONLY from the Body/latest user reply. Ignore the Subject, because it may be an old purchase/subscription thread title.
- is_high_risk: true if the Body/latest user reply contains refund/cancel/billing/delete-account intent
- action: one of [auto_reply, notify_ops, notify_only]
- reply: empty string; refund drafts are generated from a fixed approved template by the service

JSON:"""
        
        result = call_llm(prompt)
        
        if result.get("success"):
            try:
                import json
                # Extract JSON from response
                output = result["output"]
                if "```json" in output:
                    output = output.split("```json")[1].split("```")[0].strip()
                elif "```" in output:
                    output = output.split("```")[1].split("```")[0].strip()
                
                return json.loads(output)
            except Exception as e:
                logger.error("LLM response parse failed: %s", e)
        
        # Default fallback
        return {
            "category": "other",
            "is_high_risk": False,
            "action": "notify_only",
            "reply": ""
        }


if __name__ == "__main__":
    service = EDMService()
    service.run()
