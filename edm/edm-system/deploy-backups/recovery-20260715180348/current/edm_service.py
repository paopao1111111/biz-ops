#!/opt/edm-system/.venv/bin/python
"""EDM service with centralized, durable delivery decisions."""
import html
import json
import logging
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from lib.config import Config
from lib.email_utils import detect_language, extract_latest_reply, normalize_email_body, recover_quoted_context
from lib.faq_matcher import FAQMatcher
from lib.filter_rules import classify_discard, contains_high_risk_keywords

logger = logging.getLogger("edm_service")
AUTO_SEND_FAQ_IDS = {10, 20, 21, 22, 23}
HIGH_RISK_CATEGORIES = {"cancel_subscription", "refund_request", "billing_issue", "delete_account", "bug_report", "partnership", "commercial", "spam"}

REQUEST_PATTERNS = (
    r"\b(?:please\s+)?refund(?:\s+me|\s+my|\s+this)?\b", r"\bi (?:want|need|request|would like) (?:a )?refund\b", r"\bmoney back\b",
    r"申请退款", r"请退款", r"我要退款", r"给我退款", r"(?:quiero|solicito|quisiera|necesito|pido|por favor) (?:un |el )?(?:reembolso|devolución|devolucion)",
    r"(?:pueden|podrían|podrian|quiero que me|necesito que me) (?:hacer|dar|tramitar|procesar|devuelvan) (?:un |el |mi )?(?:reembolso|dinero)",
    r"(?:quero|solicito|gostaria de|por favor) (?:um )?reembolso", r"(?:je souhaite|je demande|merci de|je voudrais|j'aimerais) (?:un |le )?remboursement",
    r"(?:pouvez-vous|pourriez-vous|veuillez) (?:me )?(?:rembourser|faire|effectuer|traiter) (?:un |le |mon )?remboursement?", r"(?:remboursez|rembourser)[- ]moi",
    r"(?:ich möchte|ich beantrage|bitte) (?:eine )?(?:rückerstattung|rueckerstattung)", r"환불(?:을)? (?:원합니다|요청합니다|해주세요|받고 싶습니다)",
)
CONTEXT_PATTERNS = (
    r"refund (?:status|policy|eligibility|process|meaning)", r"where is my refund", r"not (?:asking|requesting) (?:for )?a refund", r"no refund",
    r"退款(?:状态|政策|规则|是什么意思|这个词)", r"(?:没有|没|不是).{0,8}(?:申请|要求)?退款", r"politique de remboursement", r"signifie.{0,20}remboursement",
    r"je ne demande pas.*remboursement", r"(?:análisis|analisis|significa|qué significa|que significa).{0,25}(?:reembolso|devolución|devolucion|cancelar suscripción)",
    r"(?:no|nunca) (?:quiero|solicito|pido|estoy (?:pidiendo|solicitando)) (?:un )?(?:reembolso|devolución|devolucion)",
    r"no (?:es|se trata de) (?:una )?(?:solicitud|petición|peticion) de (?:reembolso|devolución|devolucion)", r"no solicito reembolso", r"no pido reembolso",
    r"(?:estado|estatus|política|politica|proceso|plazo|elegibilidad|condiciones) (?:de|del|para el) (?:reembolso|devolución|devolucion)",
    r"(?:dónde|donde) (?:está|esta) mi (?:reembolso|devolución)", r"(?:pregunta|consulta|información|informacion).{0,20}(?:reembolso|devolución|devolucion)",
    r"(?:não|nao) (?:quero|solicito) reembolso", r"환불.{0,20}(?:뜻|번역|질문)", r"환불(?:을)? (?:신청하지|요청하지) 않았",
    r"(?:rückerstattungsrichtlinie|rueckerstattungsrichtlinie)", r"keine (?:rückerstattung|rueckerstattung)",
)


def classify_refund_intent(body):
    """Clause-aware intent: the latest explicit request overrides earlier discussion or negation."""
    text = (body or "").lower()
    clauses = [c.strip() for c in re.split(r"[.!?。！？;；]+|\b(?:but|however|pero|mais|aber)\b|(?:但是|不过|하지만)", text) if c.strip()]
    result = "none"
    for clause in clauses or [text]:
        contextual = any(re.search(p, clause) for p in CONTEXT_PATTERNS)
        if contextual:
            result = "not_request"
        elif any(re.search(p, clause) for p in REQUEST_PATTERNS):
            result = "request"
    return result


def decide_delivery(email, source, category, faq=None, classification=None, queue=None):
    body = email.get("body", "") or ""
    language = email.get("language", "unknown") or "unknown"
    refund_intent = classify_refund_intent(body)
    if refund_intent == "request": return {"action": "draft", "reason": "refund_request"}
    if contains_high_risk_keywords(body) or category in HIGH_RISK_CATEGORIES:
        return {"action": "manual", "reason": "high_risk_context" if refund_intent == "not_request" else "high_risk"}
    if source != "faq": return {"action": "manual", "reason": "llm_never_sends"}
    faq_data = faq if isinstance(faq, dict) else {}
    faq_id = faq_data.get("id")
    if faq_id not in AUTO_SEND_FAQ_IDS or faq_data.get("delivery_policy") != "auto": return {"action": "manual", "reason": "faq_not_allowlisted"}
    if not email.get("thread_id"): return {"action": "manual", "reason": "missing_thread_id"}
    if email.get("quoted_recovered"): return {"action": "manual", "reason": "quoted_context_manual_only"}
    if len(body.strip()) < Config.EDM_AUTO_REPLY_MIN_BODY_CHARS: return {"action": "manual", "reason": "body_too_short"}
    match_metadata = faq_data.get("match_metadata")
    if not isinstance(match_metadata, dict) or match_metadata.get("confident") is not True: return {"action": "manual", "reason": "faq_match_not_confident"}
    templates = faq_data.get("templates")
    if language == "unknown" or not isinstance(templates, dict) or not templates.get(language): return {"action": "manual", "reason": "same_language_template_missing"}
    if queue and queue.thread_auto_sent_recently(email.get("thread_id"), Config.EDM_THREAD_COOLDOWN_SECONDS): return {"action": "manual", "reason": "thread_cooldown"}
    if not Config.EDM_AUTO_REPLY_ENABLED: return {"action": "manual", "reason": "auto_reply_disabled"}
    return {"action": "auto_send", "reason": "allowlisted_faq"}


def execute_delivery(service, email, decision, category, faq=None, reply=""):
    """Sole Gmail send/draft executor with send and bookkeeping failures separated."""
    action, message_id, thread_id = decision["action"], email.get("message_id", ""), email.get("thread_id", "")
    faq_id = faq.get("id") if isinstance(faq, dict) else None
    if action == "auto_send":
        if not thread_id: return {"action": "manual", "reason": "missing_thread_id"}
        if not service.queue.claim_send(message_id, category, faq_id=faq_id, thread_id=thread_id):
            return {"action": "manual", "reason": "send_claim_blocked"}
        try:
            result = service.gmail.send_reply(thread_id=thread_id, to=service.reply_to_address(email.get("from", "")), subject=service.reply_subject(email.get("subject", "")), body_html=reply, in_reply_to=message_id)
        except Exception as exc:
            service.queue.release_send_claim(message_id, category, "gmail_send_failed", faq_id=faq_id, thread_id=thread_id, error=str(exc))
            raise
        try:
            if not service.queue.confirm_sent(message_id, category, decision["reason"], faq_id=faq_id, thread_id=thread_id):
                raise RuntimeError("auto_sent transition rejected")
        except Exception as exc:
            try: service.queue.preserve_sent_unconfirmed(message_id, category, "post_send_bookkeeping_failed", faq_id=faq_id, thread_id=thread_id, error=str(exc))
            except Exception: logger.critical("SEND SUCCEEDED but sent_unconfirmed persistence also failed: %s", message_id, exc_info=True)
            return {"action": "sent_unconfirmed", "result": result, "error": str(exc)}
        return {"action": action, "result": result}
    if action == "draft":
        try:
            result = service.gmail.create_reply_draft(thread_id=thread_id, to=service.reply_to_address(email.get("from", "")), subject=service.reply_subject(email.get("subject", "")), body_html=service.build_refund_draft_reply(email), in_reply_to=message_id)
        except Exception as exc:
            service.queue.record_outcome(message_id, category, "retryable_error", "gmail_draft_failed", faq_id=faq_id, thread_id=thread_id, error=str(exc))
            raise
        draft_id = result.get("id", "") if isinstance(result, dict) else ""
        service.queue.record_outcome(message_id, category, "draft_created", decision["reason"], faq_id=faq_id, thread_id=thread_id, draft_id=draft_id)
        return {"action": action, "draft_id": draft_id}
    service.queue.record_outcome(message_id, category, "manual", decision.get("reason", "manual"), faq_id=faq_id, thread_id=thread_id)
    return {"action": "manual"}


class EDMService:
    def __init__(self, gmail=None, faq_matcher=None, feishu=None, queue=None, llm_caller=None):
        if gmail is None:
            from lib.gmail_client import GmailClient
            gmail = GmailClient()
        if feishu is None:
            from lib.feishu_notifier import FeishuNotifier
            feishu = FeishuNotifier()
        if queue is None:
            from lib.queue import EmailQueue
            queue = EmailQueue(Config.QUEUE_DB_PATH)
        if llm_caller is None:
            from lib.llm_client import call_llm
            llm_caller = call_llm
        self.gmail, self.faq_matcher, self.feishu, self.queue, self.llm_caller = gmail, faq_matcher or FAQMatcher(), feishu, queue, llm_caller
        self.poll_interval, self.consecutive_fail_count = Config.EDM_POLL_INTERVAL, 0
        self.MAX_FAILS_BEFORE_ALERT = getattr(Config, "EDM_SERVICE_ERROR_ALERT_THRESHOLD", 3)

    def run(self):
        logger.info("EDM Service started, polling every %d seconds", self.poll_interval)
        while True:
            try:
                self.poll_and_process()
                if self.consecutive_fail_count: logger.info("Poll recovered after %d failures", self.consecutive_fail_count); self.consecutive_fail_count = 0
            except Exception as exc:
                self.consecutive_fail_count += 1
                logger.error("Poll failed (%d/%d): %s", self.consecutive_fail_count, self.MAX_FAILS_BEFORE_ALERT, exc, exc_info=True)
                if self.consecutive_fail_count >= self.MAX_FAILS_BEFORE_ALERT:
                    try: self.feishu.notify_service_error(str(exc), self.consecutive_fail_count)
                    except Exception: logger.exception("Service-error alert failed")
                    self.consecutive_fail_count = 0
            time.sleep(self.poll_interval)

    def poll_and_process(self):
        for attempt in range(1, 4):
            try: emails = self.gmail.poll_messages(max_results=20); break
            except Exception:
                if attempt >= 3: raise
                logger.info("Gmail poll attempt %d/3 failed, rebuilding client", attempt, exc_info=True)
                try:
                    from lib.gmail_client import GmailClient
                    self.gmail = GmailClient()
                except Exception: logger.warning("Failed to rebuild Gmail client", exc_info=True)
                time.sleep(5 * attempt)
        for email in emails:
            try: self.process_email(email)
            except Exception: logger.exception("Process email failed: %s", email.get("message_id", ""))

    def mark_read_safely(self, message_id, reason=""):
        if message_id:
            try: self.gmail.mark_read(message_id)
            except Exception: logger.warning("Failed to mark read (%s): %s", reason, message_id, exc_info=True)

    @staticmethod
    def reply_to_address(sender):
        match = re.search(r"<([^>]+)>", sender or "") or re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", sender or "")
        return match.group(1 if "<" in match.group(0) else 0).strip() if match else (sender or "").strip()

    @staticmethod
    def reply_subject(subject): return subject if (subject or "").lower().startswith("re:") else "Re: %s" % (subject or "")

    @staticmethod
    def as_html(text):
        text = (text or "").strip()
        if not text: return ""
        if re.search(r"<\s*(p|br|div|html|body|ul|ol|li)\b", text, re.I): return text
        return "<p>%s</p>" % html.escape(text).replace("\n", "<br>")

    def build_refund_draft_reply(self, email):
        templates = {
            "zh": "您好，\n\n我们已收到您的退款请求，并会交由客服团队人工核实。此草稿不代表退款已获批准或承诺具体到账时间。\n\n如需补充订单或付款信息，请直接回复此邮件。\n\n谢谢。",
            "en": "Hello,\n\nWe received your refund request and will have our support team review it manually. This draft does not confirm approval or promise a processing time.\n\nPlease reply with any relevant order or payment details.\n\nThank you.",
            "es": "Hola,\n\nHemos recibido tu solicitud de reembolso y nuestro equipo de soporte la revisará manualmente. Este borrador no confirma la aprobación ni promete un plazo de tramitación.\n\nResponde con los datos relevantes del pedido o pago.\n\nGracias.",
            "de": "Hallo,\n\nwir haben Ihre Rückerstattungsanfrage erhalten. Unser Support-Team wird sie manuell prüfen. Dieser Entwurf bestätigt keine Genehmigung und verspricht keine Bearbeitungsfrist.\n\nBitte antworten Sie mit relevanten Bestell- oder Zahlungsdaten.\n\nVielen Dank.",
            "fr": "Bonjour,\n\nNous avons reçu votre demande de remboursement. Notre équipe d’assistance va l’examiner manuellement. Ce brouillon ne confirme pas son approbation et ne promet aucun délai.\n\nVous pouvez répondre avec les informations de commande ou de paiement utiles.\n\nMerci.",
            "pt": "Olá,\n\nRecebemos sua solicitação de reembolso. Nossa equipe de suporte fará uma análise manual. Este rascunho não confirma aprovação nem promete prazo de processamento.\n\nResponda com os dados relevantes do pedido ou pagamento.\n\nObrigado.",
            "ko": "안녕하세요.\n\n환불 요청을 접수했으며 지원팀에서 수동으로 검토할 예정입니다. 이 초안은 환불 승인이나 처리 기간을 보장하지 않습니다.\n\n관련 주문 또는 결제 정보를 회신해 주세요.\n\n감사합니다.",
        }
        return self.as_html(templates.get(email.get("language", "unknown"), templates["en"]))

    def classify_with_llm(self, email):
        result = self.llm_caller("Classify for manual support triage only; never authorize sending. Return JSON category/is_high_risk/action. Body: %s" % email.get("body", "")[:2000])
        if result.get("success"):
            try: return json.loads(result.get("output", "").replace("```json", "").replace("```", "").strip())
            except Exception: logger.exception("LLM response parse failed")
        return {"category": "other", "is_high_risk": False, "action": "notify_only"}

    def notify_safely(self, method, *args, **kwargs):
        try: getattr(self.feishu, method)(*args, **kwargs)
        except Exception: logger.exception("Feishu %s failed", method)

    def process_email(self, email):
        message_id = email.get("message_id", "")
        if self.queue.is_processed(message_id): self.mark_read_safely(message_id, "already processed"); return
        raw = normalize_email_body(email.get("body", ""))
        latest = extract_latest_reply(raw)
        quoted = recover_quoted_context(raw)
        discard, reason = classify_discard(email.get("from", ""), email.get("subject", ""), raw, email.get("headers", {}), latest_body=latest)
        if discard:
            self.queue.record_outcome(message_id, "filtered", "discarded", reason, thread_id=email.get("thread_id", "")); self.mark_read_safely(message_id, "discarded"); return
        current = dict(email, body=latest, language=detect_language(latest), snippet=latest[:200], quoted_context=quoted, quoted_recovered=bool(not latest and quoted))
        faq = self.faq_matcher.match("", latest)
        if faq: category, source, classification = "faq_question", "faq", None
        else: classification = self.classify_with_llm(current); category, source = classification.get("category", "other"), "llm"
        decision = decide_delivery(current, source, category, faq=faq, classification=classification, queue=self.queue)
        reply = self.faq_matcher.render_template(faq, current["language"]) if faq else ""
        try: outcome = execute_delivery(self, current, decision, category, faq=faq, reply=reply)
        except Exception as exc:
            logger.error("Delivery failed and remains unread for retry: %s", exc, exc_info=True)
            self.notify_safely("notify_pending", current, category, "Retryable delivery failure: %s" % exc)
            return
        if outcome["action"] == "auto_send": self.notify_safely("notify_auto_reply", current, category, faq["id"], reply_preview=reply)
        elif outcome["action"] == "sent_unconfirmed": self.notify_safely("notify_pending", current, category, "CRITICAL: Gmail send succeeded; bookkeeping unconfirmed; automatic retry blocked")
        elif outcome["action"] == "draft": self.notify_safely("notify_high_risk", current, category, llm_analysis="Gmail draft created; not sent", process_status="Draft pending manual review")
        elif category in HIGH_RISK_CATEGORIES or contains_high_risk_keywords(latest): self.notify_safely("notify_high_risk", current, category)
        else: self.notify_safely("notify_pending", current, category, decision["reason"])
        self.mark_read_safely(message_id, outcome["action"])


if __name__ == "__main__": EDMService().run()
