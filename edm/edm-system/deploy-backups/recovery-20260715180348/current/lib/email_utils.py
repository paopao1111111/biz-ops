"""Email body normalization, bounded quoted recovery, and language utilities."""
import html
import re

_REPLY_MARKERS = (" wrote:", " escribió:", " a écrit", " a ecrit", " schrieb", " escreveu:", " 작성:", " 写道", "发件人:", "发件人：", "-----original message-----")
LANGUAGE_MARKERS = {
    "es": (" reembolso", " suscripción", " cancelar", " cuenta", " gracias", " por favor", " solicitud"),
    "de": (" rückerstattung", " kündigen", " rechnung", " vielen dank", " bitte", " ich möchte", " konto"),
    "fr": (" remboursement", " annuler", " abonnement", " compte", " merci", " s'il vous plaît", " je souhaite"),
    "pt": (" reembolso", " cancelar", " assinatura", " excluir conta", " obrigado", " por favor", " gostaria"),
}


def normalize_email_body(body: str) -> str:
    """Convert raw HTML to bounded plain text at the service boundary."""
    text = body or ""
    if re.search(r"<\s*(html|body|div|p|br|blockquote|table|span)\b", text, re.I):
        text = re.sub(r"<\s*(script|style)\b[^>]*>.*?<\s*/\s*\1\s*>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
        text = re.sub(r"<\s*/\s*(p|div|li|tr|blockquote)\s*>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return re.sub(r"[ \t]+", " ", text)[:50000]


def extract_latest_reply(body: str) -> str:
    if not body:
        return ""
    text = normalize_email_body(body)
    latest = []
    for line in text.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()
        if _is_reply_marker(lower) or re.match(r"^-{2,}\s*(forwarded|original)\s+message\s*-{2,}$", lower):
            break
        if stripped.startswith(">"):
            continue
        latest.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(latest).strip())


def recover_quoted_context(body: str, max_chars: int = 1200) -> str:
    """Recover a small quoted block for human context only; callers must never auto-send from it."""
    text = normalize_email_body(body)
    recovered = []
    in_old = False
    for line in text.split("\n"):
        stripped = line.strip()
        lower = stripped.lower()
        if _is_reply_marker(lower) or re.match(r"^-{2,}\s*(forwarded|original)\s+message\s*-{2,}$", lower):
            in_old = True
            continue
        if stripped.startswith(">"):
            in_old = True
            stripped = stripped.lstrip("> ")
        if in_old and stripped:
            recovered.append(stripped)
            if len("\n".join(recovered)) >= max_chars:
                break
    return "\n".join(recovered)[:max_chars]


def _is_reply_marker(line: str) -> bool:
    if any(marker in line for marker in _REPLY_MARKERS):
        return True
    return ((line.startswith("on ") and line.endswith("wrote:"))
            or (line.startswith("el ") and ("escribió:" in line or "escribio:" in line))
            or (" schrieb am " in line) or (line.startswith("am ") and " schrieb" in line)
            or (line.startswith("em ") and " escreveu:" in line))


def detect_language(text: str) -> str:
    """Return a supported language code; ambiguous short text remains unknown."""
    if not (text or "").strip():
        return "unknown"
    compact = " %s " % text.strip().lower()
    hangul = sum("가" <= ch <= "힣" for ch in compact)
    cjk = sum("一" <= ch <= "鿿" for ch in compact)
    alpha = sum(ch.isalpha() for ch in compact)
    if hangul >= 2:
        return "ko"
    if cjk >= 2 and cjk >= max(2, alpha * 0.15):
        return "zh"
    scores = {lang: sum(marker in compact for marker in markers) for lang, markers in LANGUAGE_MARKERS.items()}
    if re.search(r"[¿¡ñ]", compact): scores["es"] += 2
    if re.search(r"[äöüß]", compact): scores["de"] += 2
    if re.search(r"[àâçèêëîïôùûüÿœ]", compact): scores["fr"] += 2
    if re.search(r"[ãõ]", compact): scores["pt"] += 2
    best = max(scores, key=scores.get)
    if scores[best] > 0 and list(scores.values()).count(scores[best]) == 1:
        return best
    words = re.findall(r"[a-z]+(?:'[a-z]+)?", compact)
    english_markers = {"the", "please", "thank", "account", "upload", "document", "cannot", "can't", "how", "what", "why", "where", "when", "help", "need", "want", "code", "file", "email", "login", "subscription", "payment", "issue", "error", "work", "working", "available", "supported"}
    if len(words) >= 4 and sum(word in english_markers for word in words) >= 1 and all(ord(ch) < 128 for ch in compact):
        return "en"
    return "unknown"
