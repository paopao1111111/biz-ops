"""
Competitor SEO LLM client - 优先直接调用 CLIProxy，失败时 fallback 到 AgentOS
"""
import os
from typing import Optional, Dict, Any

# 导入统一的 LLM 客户端
from core.direct_llm import call_llm, DEFAULT_MODEL, DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


COMPETITOR_SEO_LLM_TIMEOUT = int(os.getenv("COMPETITOR_SEO_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
COMPETITOR_SEO_LLM_MAX_TOKENS = int(os.getenv("COMPETITOR_SEO_LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
COMPETITOR_SEO_ALLOW_AGENTOS_FALLBACK = _env_bool("COMPETITOR_SEO_ALLOW_AGENTOS_FALLBACK", True)


def run_prompt(prompt_text: str, workflow_id: Optional[str] = None) -> Dict[str, Any]:
    """
    运行 LLM 提示词。

    Args:
        prompt_text: 提示词文本；不修改 prompt 内容
        workflow_id: AgentOS workflow ID（仅用于 fallback）

    Returns:
        {"success": True, "output": "模型输出"} 或 {"success": False, "error": "错误信息"}
    """
    return call_llm(
        prompt=prompt_text,
        model=DEFAULT_MODEL,
        temperature=0.7,
        max_tokens=COMPETITOR_SEO_LLM_MAX_TOKENS,
        timeout=COMPETITOR_SEO_LLM_TIMEOUT,
        fallback_to_agentos=COMPETITOR_SEO_ALLOW_AGENTOS_FALLBACK,
        workflow_id=workflow_id,
    )
