"""FAQ matcher: keyword-based matching with localized templates."""
import json
import re
from pathlib import Path

FAQ_PATH = Path('/opt/edm-system/faq_knowledge.json')

class FAQMatcher:
    def __init__(self):
        self.faqs = self._load_faqs()
    
    def _load_faqs(self):
        if FAQ_PATH.exists():
            return json.loads(FAQ_PATH.read_text(encoding='utf-8'))
        return []
    
    def match(self, subject: str, body: str) -> dict:
        """Match latest email text to FAQ. Returns FAQ dict or None.

        Prefer the latest body over the subject. Subjects are often inherited from
        older threads or from our own localized replies, so subject-first matching
        can send a Chinese/English FAQ to a German user. Use the subject only when
        the latest body is empty/too short or when the body has no match.
        """
        body_text = (body or "").lower()
        subject_text = (subject or "").lower()
        body_match = self._match_text(body_text)
        if body_match:
            return body_match
        if not body_text.strip() or len(body_text.strip()) < 20:
            return self._match_text(subject_text)
        return None

    def _match_text(self, text: str) -> dict:
        for faq in self.faqs:
            if faq.get("manual_only"):
                continue
            keywords = faq.get("keywords", [])
            if any(self._contains_keyword(text, keyword) for keyword in keywords):
                return faq
        return None

    def render_template(self, faq: dict, language: str = "en") -> str:
        """Return the FAQ reply template in the detected sender language."""
        if not faq:
            return ""
        lang = (language or "en").split("-")[0].lower()
        templates = faq.get("templates") or {}
        return templates.get(lang) or templates.get("en") or faq.get("template", "")

    def _contains_keyword(self, text: str, keyword: str) -> bool:
        if not keyword:
            return False
        kw = keyword.lower().strip()
        # CJK keywords do not need word boundaries. Multi-word Latin phrases are
        # also safer as substring matches.
        if re.search(r"[一-鿿]", kw) or " " in kw:
            return kw in text
        return re.search(rf"(?<![a-z0-9_]){re.escape(kw)}(?![a-z0-9_])", text) is not None
    
    def get_all(self):
        """Return all FAQs"""
        return self.faqs
