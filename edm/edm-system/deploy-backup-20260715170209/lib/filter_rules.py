"""Filter rules: sender, subject, header, and bulk-email blacklists."""
import re
from typing import Mapping, Optional

# Exact substrings or domain suffixes. Domain suffixes may be written as
# "@example.com" or "example.com" and match subdomains too.
SENDER_BLACKLIST_PATTERNS = [
    # Automated / transactional senders
    "noreply@", "no-reply@", "mailer-daemon@", "postmaster@",
    "notifications@", "alert@", "notification@", "donotreply@", "do-not-reply@",
    "failed-payments+", "invoice+statements@", "receipts@",

    # Marketing / newsletter role accounts
    "marketing@", "promo@", "newsletter@", "news@", "ads@", "offer@", "offers@",
    "deals@", "campaign@", "bulk@", "mass@", "digest@", "weekly@", "daily@",
    "update@", "updates@",
    "info@", "hello@", "team@", "community@", "support@",

    # Email service providers / social / content platforms
    "mailchimp.com", "sendgrid.net", "hubspot.com", "constantcontact.com", "sendinblue.com",
    "mailgun.org", "mail.beehiiv.com", "beehiiv.com", "substack.com", "ghost.io",
    "dev.to", "medium.com", "quora.com", "linkedin.com", "facebookmail.com",
    "twitter.com", "x.com", "reddit.com", "pinterest.com", "producthunt.com",
    "instagram.com", "mail.instagram.com", "facebook.com",

    # Known recurring noisy vendors/newsletters in this mailbox
    "anthropic.com", "openai.com", "nerdwallet.com", "moz.com", "ahrefs.com",
    "linermail.com", "elementor.com", "g2.com", "crowdin.com", "otterly.ai",
    "adsy.com", "brand24.com", "vimeo.com", "hotjar.com", "supabase.com",
    "algoreducation.com", "mail-app.algoreducation.com", "tiktok.com", "shop.tiktok.com",
    "stripe.com", "awin.com", "mail.awin.com",

    # One-off noisy domains/senders seen in production
    "nonex.co.th", "apropos.domains", "makeinvideo.live", "ac-guyane.fr",
    "pmrelocations.com", "adly.news", "dothastudio.com", "avantel.ru",
    "marketplacegeek.com", "bizhelpcentral.com", "email.seaarea.com", "amazoncloud.cn",
    "about.me", "jitter.video", "ucloud-global.com", "iweaver@iweaver.ai",
    "whiteangle.seo@gmail.com", "catcherofdreams96@gmail.com",
    "richardharry.digital@gmail.com", "swiftjournals@gmail.com",
]

SUBJECT_BLACKLIST_PATTERNS = [
    # Auto replies / bounces
    "out of office", "out of the office", "auto-reply", "automatic reply", "automatic response",
    "autoreply", "自动回复", "自动答复", "外出回复", "delivery status", "undeliverable",

    # Marketing / newsletter / product updates
    "newsletter", "promotion", "promotional", "limited time", "special offer", "exclusive deal",
    "act now", "free trial", "discount", "coupon", "% off", "gift card", "amazon gift card",
    "black friday", "cyber monday", "digest", "weekly roundup", "daily digest", "your weekly",
    "your daily", "what's new", "what’s new", "supa update", "starter", "free credits",
    "recommended for you", "suggested spaces", "trending on", "top stories", "new from",
    "webinar", "you might like", "picks for you", "curated for you",

    # Monitoring/audit noise
    "site audit", "crawl failed", "crawl error", "4xx page", "5xx page",
]

BULK_BODY_PATTERNS = [
    "view this email in your browser",
    "manage your email preferences",
    "email preferences",
    "you are receiving this email because",
    "you're receiving this email because",
    "you received this email because",
    "unsubscribe from this",
    "unsubscribe here",
    "unsubscribe instantly",
    "manage subscription",
    "manage your subscription",
    "list-unsubscribe",
    "powered by mailchimp",
    "campaign monitor",
    "beehiiv",
]

# Only customer-risk phrases. Do NOT include generic "unsubscribe": every newsletter
# has an unsubscribe footer and that caused noisy high-risk false positives.
HIGH_RISK_KEYWORDS = [
    "refund", "money back", "cancel subscription", "cancel my subscription",
    "cancel my account", "cancel my plan", "delete account", "charged twice",
    "billing issue", "payment dispute", "unauthorized charge", "failed payment",
    "退款", "取消订阅", "取消会员", "重复扣款", "退费", "删除账户", "注销账户",
    "reembolso", "cancelar suscripción", "cancelar suscripcion", "eliminar cuenta",
]

BULK_HEADER_NAMES = {
    "list-unsubscribe", "list-unsubscribe-post", "list-id", "list-help", "list-post",
    "feedback-id", "x-campaign-id", "x-mailchimp-campaign-id", "x-campaign",
    "x-sg-eid", "x-mailgun-tag", "x-mailgun-variables", "x-beehiiv", "x-hubspot-correlation-id",
}


def _extract_domain(sender_email: str) -> str:
    sender_lower = (sender_email or "").lower()
    match = re.search(r"<[^<>@\s]+@([^<>\s]+)>", sender_lower)
    if not match:
        match = re.search(r"[^<>\s@]+@([^<>\s]+)", sender_lower)
    if not match:
        return ""
    return match.group(1).strip().strip(">,;.")


def _domain_matches(domain: str, pattern: str) -> bool:
    pattern = pattern.lower().strip()
    if not domain:
        return False
    if pattern.startswith("@"):
        suffix = pattern[1:]
    elif "@" not in pattern and "." in pattern:
        suffix = pattern
    else:
        return False
    return domain == suffix or domain.endswith("." + suffix)


def is_sender_blacklisted(sender_email: str) -> bool:
    sender_lower = (sender_email or "").lower()
    domain = _extract_domain(sender_lower)
    for pattern in SENDER_BLACKLIST_PATTERNS:
        pattern_lower = pattern.lower()
        if pattern_lower in sender_lower:
            return True
        if _domain_matches(domain, pattern_lower):
            return True
    return False


def is_subject_blacklisted(subject: str) -> bool:
    subject_lower = (subject or "").lower()
    return any(pattern in subject_lower for pattern in SUBJECT_BLACKLIST_PATTERNS)


def is_bulk_header(headers: Optional[Mapping[str, str]]) -> bool:
    """Detect bulk/auto-response mail from Gmail headers."""
    if not headers:
        return False
    normalized = {str(k).lower(): str(v) for k, v in headers.items() if k is not None}

    if any(name in normalized and normalized[name].strip() for name in BULK_HEADER_NAMES):
        return True

    auto_submitted = normalized.get("auto-submitted", "").lower().strip()
    if auto_submitted and auto_submitted != "no":
        return True

    precedence = normalized.get("precedence", "").lower().strip()
    if precedence in {"bulk", "list", "junk"}:
        return True

    auto_suppress = normalized.get("x-auto-response-suppress", "").lower().strip()
    if auto_suppress:
        return True

    return False


def is_bulk_or_marketing_email(sender_email: str, subject: str, body: str = "", headers: Optional[Mapping[str, str]] = None) -> bool:
    if is_sender_blacklisted(sender_email) or is_subject_blacklisted(subject):
        return True
    if is_bulk_header(headers):
        return True
    combined = f"{sender_email or ''}\n{subject or ''}\n{(body or '')[:5000]}".lower()
    return any(pattern in combined for pattern in BULK_BODY_PATTERNS)


def contains_high_risk_keywords(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(keyword in text_lower for keyword in HIGH_RISK_KEYWORDS)
