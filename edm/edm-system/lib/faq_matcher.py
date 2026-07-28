"""Body-only FAQ matcher with explicit scoring and language metadata."""
import json
import re
from pathlib import Path

FAQ_PATH = Path(__file__).resolve().parent.parent / "faq_knowledge.json"


class FAQMatcher:
    def __init__(self, faq_path=None):
        self.faq_path = Path(faq_path) if faq_path else FAQ_PATH
        self.faqs = self._load_faqs()

    def _load_faqs(self):
        if self.faq_path.exists():
            return json.loads(self.faq_path.read_text(encoding="utf-8"))
        return []

    def match(self, subject: str, body: str) -> dict:
        """Match only the latest body; inherited purchase-thread subjects are metadata only."""
        text = (body or "").lower().strip()
        if not text:
            return None
        candidates = []
        for faq in self.faqs if isinstance(self.faqs, list) else []:
            if not isinstance(faq, dict):
                continue
            keywords = faq.get("keywords")
            if not isinstance(keywords, list):
                continue
            matched = [kw for kw in keywords if isinstance(kw, str) and self._contains_keyword(text, kw)]
            if not matched:
                continue
            phrase_hits = sum(1 for kw in matched if len(kw.strip()) >= 8 or " " in kw.strip())
            exact_hits = sum(1 for kw in matched if text == kw.lower().strip())
            score = sum(min(len(kw.strip()), 40) for kw in matched) + phrase_hits * 20 + exact_hits * 20
            candidate = dict(faq)
            candidate["match_metadata"] = {
                "source": "body", "matched_keywords": matched,
                "keyword_count": len(matched), "phrase_hits": phrase_hits, "exact_hits": exact_hits, "score": score,
            }
            candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["match_metadata"]["score"], item["match_metadata"]["keyword_count"]), reverse=True)
        best = candidates[0]
        meta = best["match_metadata"]
        meta["confident"] = meta["exact_hits"] >= 1 or meta["phrase_hits"] >= 1 or meta["keyword_count"] >= 2
        return best

    def delivery_policy(self, faq: dict) -> str:
        if not isinstance(faq, dict):
            return "manual"
        policy = faq.get("delivery_policy")
        return policy if policy in {"auto", "manual", "draft"} else "manual"

    def has_template(self, faq: dict, language: str) -> bool:
        lang = (language or "unknown").split("-")[0].lower()
        templates = faq.get("templates") if isinstance(faq, dict) else None
        return lang != "unknown" and isinstance(templates, dict) and bool(templates.get(lang))

    def render_template(self, faq: dict, language: str = "unknown") -> str:
        """Return only an exact same-language template; no English fallback."""
        if not isinstance(faq, dict):
            return ""
        lang = (language or "unknown").split("-")[0].lower()
        templates = faq.get("templates")
        if not isinstance(templates, dict):
            return ""
        template = templates.get(lang, "")
        return template if isinstance(template, str) else ""

    def _contains_keyword(self, text: str, keyword: str) -> bool:
        if not keyword:
            return False
        kw = keyword.lower().strip()
        if re.search(r"[一-鿿가-힣]", kw) or " " in kw:
            return kw in text
        return re.search(r"(?<![a-z0-9_])%s(?![a-z0-9_])" % re.escape(kw), text) is not None

    def get_all(self):
        return self.faqs
