import json
import os
from pathlib import Path
import re

from .paths import ASSETS_DIR

_PROMPTS_PATH = os.getenv("WP_TES_PROMPTS_PATH")
_PROMPTS_PATH = Path(_PROMPTS_PATH) if _PROMPTS_PATH else ASSETS_DIR / "prompts.json"

with _PROMPTS_PATH.open("r", encoding="utf-8") as _f:
    _PROMPTS = json.load(_f)

SEO_RESEARCH_PROMPT = _PROMPTS["SEO_RESEARCH_PROMPT"]
SEO_GENERATE_PROMPT = _PROMPTS["SEO_GENERATE_PROMPT"]
BLOG_GENERATE_PROMPT = _PROMPTS["BLOG_GENERATE_PROMPT"]
HOT_TOPIC_KEYWORD_PROMPT = _PROMPTS["HOT_TOPIC_KEYWORD_PROMPT"]
HOT_TOPIC_BLOG_PROMPT = _PROMPTS["HOT_TOPIC_BLOG_PROMPT"]
INSIGHT_ARTICLE_PROMPT = _PROMPTS["INSIGHT_ARTICLE_PROMPT"]
CONTENT_AUDIT_PROMPT = _PROMPTS["CONTENT_AUDIT_PROMPT"]
FEEDBACK_REPLY_PROMPT = _PROMPTS["FEEDBACK_REPLY_PROMPT"]
EDM_REPLY_PROMPT = _PROMPTS["EDM_REPLY_PROMPT"]


def format_prompt(prompt_text, **kwargs):
    """替换提示词中的 {var} 变量，只替换提供了值的变量，缺失的保持原样。"""
    def _replace(m):
        key = m.group(1)
        if key in kwargs:
            return str(kwargs[key])
        return m.group(0)
    return re.sub(r'\{(\w+)\}', _replace, prompt_text)
