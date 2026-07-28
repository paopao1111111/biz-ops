"""LLM client - direct call to CLIProxy (same pattern as core/direct_llm.py)"""
import requests
import logging
from lib.config import Config

logger = logging.getLogger(__name__)

def call_llm(prompt: str, model: str = None, max_tokens: int = None, temperature: float = None) -> dict:
    """Call CLIProxy LLM endpoint. Returns {"success": bool, "output": str, "error": str}"""
    model = model or Config.LLM_MODEL
    max_tokens = max_tokens if max_tokens is not None else Config.LLM_MAX_TOKENS
    temperature = temperature if temperature is not None else Config.LLM_TEMPERATURE
    
    if not Config.LLM_API_KEY:
        return {"success": False, "error": "Missing LLM_API_KEY"}
    
    headers = {
        "Authorization": f"Bearer {Config.LLM_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    
    url = f"{Config.LLM_BASE_URL}/chat/completions"
    
    try:
        logger.info("Calling CLIProxy model=%s, prompt_length=%d", model, len(prompt))
        resp = requests.post(url, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        result = resp.json()
        
        choices = result.get("choices") or []
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, list):
                content = "".join(item.get("text", "") for item in content if isinstance(item, dict))
            if content.strip():
                return {"success": True, "output": content.strip()}
        
        return {"success": False, "error": f"No usable response: {str(result)[:300]}"}
    
    except requests.exceptions.Timeout:
        return {"success": False, "error": "LLM call timed out after 120s"}
    except requests.exceptions.HTTPError as e:
        detail = e.response.text[:300] if e.response is not None else str(e)
        return {"success": False, "error": f"HTTP {e.response.status_code}: {detail}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
