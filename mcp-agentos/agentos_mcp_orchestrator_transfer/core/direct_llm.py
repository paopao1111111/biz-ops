"""
Direct LLM client - 优先直接调用 CLIProxy，失败时 fallback 到 AgentOS
"""
import os
import logging
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# CLIProxy 配置
CLIPROXY_URL = os.getenv("CLIPROXY_URL", "http://127.0.0.1:8317/v1/chat/completions")
CLIPROXY_API_KEY = os.getenv("CLIPROXY_API_KEY", 'iw-k2ECp85Ti5xGgqSWO')
DEFAULT_MODEL = os.getenv("DEFAULT_LLM_MODEL", "qwen3.7-plus")
DEFAULT_TEMPERATURE = float(os.getenv("DIRECT_LLM_TEMPERATURE", "0.7"))
DEFAULT_MAX_TOKENS = int(os.getenv("DIRECT_LLM_MAX_TOKENS", "8192"))
DEFAULT_TIMEOUT = int(os.getenv("DIRECT_LLM_TIMEOUT", "300"))

# AgentOS 配置（备选）
AGENTOS_BASE_URL = os.getenv("AGENTOS_BASE_URL", "https://agent.xiaoduoai.com").rstrip("/")
AGENTOS_TOKEN = os.getenv("AGENTOS_TOKEN", "").strip()
AGENTOS_TIMEOUT = int(os.getenv("AGENTOS_TIMEOUT", "300"))
AGENTOS_POLL_INTERVAL = float(os.getenv("AGENTOS_POLL_INTERVAL", "3"))
AGENTOS_WORKFLOW_ID = os.getenv("NEW_COZE_WORKFLOW_ID", "")


def call_direct_llm(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
) -> Dict[str, Any]:
    """
    直接调用 CLIProxy 模型。
    注意：这里不改 prompt，只负责把调用发到 OpenAI-compatible 中转站。
    """
    if not CLIPROXY_API_KEY:
        return {"success": False, "error": "Missing CLIPROXY_API_KEY", "provider": "cliproxy"}

    headers = {
        "Authorization": f"Bearer {CLIPROXY_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    try:
        logger.info(
            "Calling CLIProxy with model=%s, prompt_length=%s, max_tokens=%s, timeout=%s",
            model,
            len(prompt or ""),
            max_tokens,
            timeout,
        )
        resp = requests.post(CLIPROXY_URL, headers=headers, json=payload, timeout=timeout)
        resp.raise_for_status()
        result = resp.json()

        choices = result.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = message.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in content
                )
            if isinstance(content, str) and content.strip():
                logger.info("CLIProxy call succeeded, output_length=%s", len(content))
                return {"success": True, "output": content, "provider": "cliproxy"}

        error_msg = f"No usable choices in CLIProxy response: {str(result)[:500]}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "cliproxy"}

    except requests.exceptions.Timeout:
        error_msg = f"CLIProxy direct LLM call timed out after {timeout}s"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "cliproxy"}
    except requests.exceptions.HTTPError as e:
        if e.response is not None:
            response_text = e.response.text[:500]
            status = e.response.status_code
        else:
            status = "unknown"
            response_text = str(e)
        error_msg = f"CLIProxy HTTP error: {status} - {response_text}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "cliproxy"}
    except requests.exceptions.RequestException as e:
        error_msg = f"CLIProxy direct LLM call failed: {e}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "cliproxy"}
    except Exception as e:
        error_msg = f"Unexpected CLIProxy direct LLM error: {e}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "cliproxy"}


def _extract_agentos_output(payload: Any) -> Optional[str]:
    """Best-effort extraction for AgentOS fallback payloads."""
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    if isinstance(payload, dict):
        for key in ("output", "raw_output", "text", "result", "content", "answer", "message"):
            value = payload.get(key)
            extracted = _extract_agentos_output(value)
            if extracted:
                return extracted
        data = payload.get("data")
        extracted = _extract_agentos_output(data)
        if extracted:
            return extracted
        node_results = payload.get("nodeResults")
        extracted = _extract_agentos_output(node_results)
        if extracted:
            return extracted
    if isinstance(payload, list):
        for item in payload:
            extracted = _extract_agentos_output(item)
            if extracted:
                return extracted
    return None


def call_agentos_llm(
    prompt: str,
    workflow_id: Optional[str] = None,
    timeout: int = AGENTOS_TIMEOUT,
) -> Dict[str, Any]:
    """
    调用 AgentOS（备选方案）。只有 direct 调用失败且调用方允许 fallback 时才会走到这里。
    """
    if not AGENTOS_TOKEN:
        return {"success": False, "error": "Missing AGENTOS_TOKEN", "provider": "agentos"}

    wid = workflow_id or AGENTOS_WORKFLOW_ID
    if not wid:
        return {"success": False, "error": "Missing workflow_id", "provider": "agentos"}

    import time

    headers = {
        "Authorization": f"Bearer {AGENTOS_TOKEN}",
        "Content-Type": "application/json",
    }

    start_url = f"{AGENTOS_BASE_URL}/api/public/workflow_api/test_run"
    payload = {
        "workflow_id": wid,
        "input": {"prompt": prompt},
    }

    try:
        logger.info("Calling AgentOS fallback with workflow_id=%s", wid)
        resp = requests.post(start_url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except requests.exceptions.HTTPError as e:
        detail = e.response.text[:500] if e.response is not None else str(e)
        error_msg = f"AgentOS start failed: {e}; response={detail}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "agentos"}
    except Exception as e:
        error_msg = f"AgentOS start failed: {e}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "agentos"}

    if result.get("code") not in (0, "0", None):
        error_msg = f"AgentOS start error: {result}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "agentos"}

    data = result.get("data") or {}
    execute_id = data.get("execute_id")
    if not execute_id:
        error_msg = f"AgentOS missing execute_id: {result}"
        logger.error(error_msg)
        return {"success": False, "error": error_msg, "provider": "agentos"}

    poll_url = f"{AGENTOS_BASE_URL}/api/public/workflow_api/get_process"
    deadline = time.time() + timeout
    last_payload = None

    while time.time() < deadline:
        time.sleep(AGENTOS_POLL_INTERVAL)
        try:
            proc_resp = requests.get(
                poll_url,
                headers=headers,
                params={"workflow_id": wid, "execute_id": execute_id},
                timeout=15,
            )
            proc_resp.raise_for_status()
            proc = proc_resp.json()
            last_payload = proc
        except Exception as exc:
            last_payload = {"error": str(exc)}
            continue

        data = proc.get("data") or {}
        status = data.get("executeStatus")
        output = _extract_agentos_output(data)
        if output:
            logger.info("AgentOS fallback call succeeded, output_length=%s", len(output))
            return {"success": True, "output": output, "provider": "agentos", "execute_id": execute_id}

        if status in (2, "2"):
            error_msg = f"AgentOS finished but no output found: {str(proc)[:500]}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "provider": "agentos"}

        if status in (3, 4, "3", "4"):
            error_msg = f"AgentOS workflow failed: {str(proc)[:500]}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "provider": "agentos"}

    error_msg = f"AgentOS polling timed out. Last payload: {str(last_payload)[:500]}"
    logger.error(error_msg)
    return {"success": False, "error": error_msg, "provider": "agentos"}


def call_llm(
    prompt: str,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    timeout: int = DEFAULT_TIMEOUT,
    fallback_to_agentos: bool = True,
    workflow_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    统一 LLM 调用接口：CLIProxy 优先，AgentOS 只作为 fallback。
    如果 fallback 也失败，错误会同时保留 direct_error 和 fallback_error，避免 AgentOS 401 覆盖真实直连错误。
    """
    direct_result = call_direct_llm(
        prompt=prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )

    if direct_result.get("success"):
        return direct_result

    direct_error = direct_result.get("error") or "Unknown CLIProxy direct LLM error"

    if not fallback_to_agentos:
        return {
            "success": False,
            "error": f"CLIProxy direct LLM failed; AgentOS fallback disabled: {direct_error}",
            "direct_error": direct_error,
            "provider": "cliproxy",
        }

    logger.warning("CLIProxy direct LLM failed first: %s; falling back to AgentOS", direct_error)
    fallback_result = call_agentos_llm(
        prompt=prompt,
        workflow_id=workflow_id,
        timeout=max(timeout, AGENTOS_TIMEOUT),
    )

    if fallback_result.get("success"):
        fallback_result["direct_error"] = direct_error
        return fallback_result

    fallback_error = fallback_result.get("error") or "Unknown AgentOS fallback error"
    combined_error = (
        f"CLIProxy direct LLM failed first: {direct_error}; "
        f"AgentOS fallback then failed: {fallback_error}"
    )
    logger.error(combined_error)
    return {
        "success": False,
        "error": combined_error,
        "direct_error": direct_error,
        "fallback_error": fallback_error,
        "provider": "cliproxy_then_agentos",
    }


# 便捷函数
def run_prompt(prompt_text: str, workflow_id: Optional[str] = None) -> Dict[str, Any]:
    """
    兼容旧接口的便捷函数。
    """
    return call_llm(prompt=prompt_text, workflow_id=workflow_id, fallback_to_agentos=True)
