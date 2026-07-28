"""Conservative filters for automated replies, bounces, and structural bulk mail."""
from typing import Mapping, Optional, Tuple

AUTO_SENDER_PATTERNS = ("mailer-daemon@", "postmaster@", "noreply@", "no-reply@", "donotreply@", "do-not-reply@")
SUBJECT_BLACKLIST_PATTERNS = (
    "out of office", "out of the office", "auto-reply", "automatic reply", "automatic response", "autoreply",
    "自动回复", "自动答复", "外出回复", "delivery status notification", "delivery failure", "mail delivery failed",
    "undeliverable", "returned mail", "failure notice", "message blocked",
)
BULK_BODY_PATTERNS = (
    "view this email in your browser", "manage your email preferences", "you are receiving this email because",
    "you're receiving this email because", "you received this email because", "unsubscribe from this",
    "unsubscribe here", "manage subscription", "list-unsubscribe", "powered by mailchimp",
)
BULK_HEADER_NAMES = {"list-unsubscribe", "list-unsubscribe-post", "list-id", "list-help", "list-post", "feedback-id", "x-campaign-id", "x-mailchimp-campaign-id", "x-campaign", "x-sg-eid", "x-mailgun-tag", "x-mailgun-variables", "x-beehiiv"}
HIGH_RISK_KEYWORDS = (
    "refund", "money back", "cancel subscription", "cancel my subscription", "cancel my account", "cancel my plan",
    "delete account", "charged twice", "billing issue", "payment dispute", "unauthorized charge", "failed payment",
    "退款", "退费", "取消订阅", "取消会员", "重复扣款", "删除账户", "注销账户", "reembolso", "devolución",
    "devolucion", "cancelar assinatura", "cancelar suscripción", "cancelar suscripcion", "excluir conta", "eliminar cuenta",
    "annuler mon abonnement", "remboursement", "rückerstattung", "구독 취소", "결제 취소", "환불", "계정 삭제",
)


def sender_blacklist_reason(sender_email):
    sender = (sender_email or "").lower()
    return next(("automated_sender:%s" % p for p in AUTO_SENDER_PATTERNS if p in sender), "")


def is_sender_blacklisted(sender_email): return bool(sender_blacklist_reason(sender_email))


def subject_blacklist_reason(subject):
    subject_lower = (subject or "").lower()
    return next(("automated_subject:%s" % p for p in SUBJECT_BLACKLIST_PATTERNS if p in subject_lower), "")


def is_subject_blacklisted(subject): return bool(subject_blacklist_reason(subject))


def bulk_reason(headers: Optional[Mapping[str, str]], body: str = "", latest_body: str = "") -> str:
    normalized = {str(k).lower(): str(v) for k, v in (headers or {}).items() if k is not None}
    structural = next((name for name in BULK_HEADER_NAMES if normalized.get(name, "").strip()), "")
    if structural: return "bulk_header:%s" % structural
    auto_submitted = normalized.get("auto-submitted", "").lower().strip()
    if auto_submitted and auto_submitted != "no": return "auto_submitted:%s" % auto_submitted
    precedence = normalized.get("precedence", "").lower().strip()
    if precedence in {"bulk", "list", "junk"}: return "precedence:%s" % precedence
    if normalized.get("x-auto-response-suppress", "").strip(): return "auto_response_suppress"
    # Body-only footer evidence is considered only in the latest extracted content and requires two signals.
    body_lower = (latest_body if latest_body is not None else body or "")[:10000].lower()
    hits = [pattern for pattern in BULK_BODY_PATTERNS if pattern in body_lower]
    if len(hits) >= 2: return "bulk_body:%s" % hits[0]
    return ""


def is_bulk_header(headers): return bool(bulk_reason(headers))


def classify_discard(sender_email: str, subject: str, body: str = "", headers: Optional[Mapping[str, str]] = None, latest_body: str = None) -> Tuple[bool, str]:
    reason = sender_blacklist_reason(sender_email) or subject_blacklist_reason(subject) or bulk_reason(headers, body, latest_body)
    return bool(reason), reason


def is_bulk_or_marketing_email(sender_email, subject, body="", headers=None):
    return classify_discard(sender_email, subject, body, headers)[0]


def contains_high_risk_keywords(text):
    text_lower = (text or "").lower()
    return any(keyword in text_lower for keyword in HIGH_RISK_KEYWORDS)
