"""
wp_tes LLM client - 优先直接调用 CLIProxy，失败时 fallback 到 AgentOS
"""
import os
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger('coze_llm')

# 导入统一的 LLM 客户端
from core.direct_llm import call_llm, DEFAULT_MODEL, DEFAULT_MAX_TOKENS, DEFAULT_TIMEOUT


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


WP_TES_LLM_TIMEOUT = int(os.getenv("WP_TES_LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
WP_TES_LLM_MAX_TOKENS = int(os.getenv("WP_TES_LLM_MAX_TOKENS", str(DEFAULT_MAX_TOKENS)))
WP_TES_ALLOW_AGENTOS_FALLBACK = _env_bool("WP_TES_ALLOW_AGENTOS_FALLBACK", True)


def call_coze_llm(
    workflow_id: str,
    parameters: Dict[str, Any],
    allow_agentos_fallback: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    调用 LLM（兼容旧接口）。

    Args:
        workflow_id: AgentOS workflow ID（仅用于 fallback）
        parameters: 参数字典，必须包含 "prompt" 键；不修改 prompt 内容
        allow_agentos_fallback: 是否允许 AgentOS 兜底，默认读取 WP_TES_ALLOW_AGENTOS_FALLBACK

    Returns:
        {"success": True, "output": "模型输出"} 或 {"success": False, "error": "错误信息"}
    """
    prompt = parameters.get("prompt", "")
    if not prompt:
        return {"success": False, "error": "Missing prompt in parameters"}

    fallback = WP_TES_ALLOW_AGENTOS_FALLBACK if allow_agentos_fallback is None else bool(allow_agentos_fallback)
    result = call_llm(
        prompt=prompt,
        model=DEFAULT_MODEL,
        temperature=0.7,
        max_tokens=WP_TES_LLM_MAX_TOKENS,
        timeout=WP_TES_LLM_TIMEOUT,
        fallback_to_agentos=fallback,
        workflow_id=workflow_id,
    )

    if not result.get("success"):
        logger.error("LLM call failed: %s", result.get("error"))
    return result
