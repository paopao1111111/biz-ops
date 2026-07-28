# seo_generator.py
import os
import logging
import requests
import base64
import re
import json
import time
import unicodedata
from io import BytesIO
from urllib.parse import urlparse
from PIL import Image

logger = logging.getLogger('html_generat')
# from flask import jsonify

# 配置大模型API
AGENTOS_TOKEN = os.getenv("AGENTOS_TOKEN", "").strip()
AGENTOS_RUN_URL = os.getenv(
    "AGENTOS_RUN_URL",
    "https://agent.xiaoduoai.com/api/public/workflow_api/test_run",
)
AGENTOS_PROCESS_URL = os.getenv(
    "AGENTOS_PROCESS_URL",
    AGENTOS_RUN_URL.replace("test_run", "get_process"),
)
AGENTOS_COMMIT_ID = os.getenv("AGENTOS_COMMIT_ID", "")
AGENTOS_START_TIMEOUT = int(os.getenv("AGENTOS_START_TIMEOUT", "60"))
AGENTOS_POLL_TIMEOUT = int(os.getenv("AGENTOS_POLL_TIMEOUT", "300"))
AGENTOS_POLL_INTERVAL = float(os.getenv("AGENTOS_POLL_INTERVAL", "2"))
WORKFLOW_PROVIDER = os.getenv("WORKFLOW_PROVIDER", "coze").strip().lower()
COZE_BASE_URL = os.getenv("COZE_BASE_URL", "http://localhost:8888").rstrip("/")
COZE_WORKFLOW_ID = os.getenv("COZE_WORKFLOW_ID", "7628898395792343040").strip()
COZE_PROJECT_ID = os.getenv("COZE_PROJECT_ID", "").strip()
COZE_WORKFLOW_TOKEN = os.getenv("COZE_WORKFLOW_TOKEN", "").strip()
COZE_WORKFLOW_TIMEOUT = int(os.getenv("COZE_WORKFLOW_TIMEOUT", "300"))
COZE_RUN_URL = os.getenv("COZE_RUN_URL", f"{COZE_BASE_URL}/v1/workflow/run")
COZE_RUN_HISTORY_URL = os.getenv("COZE_RUN_HISTORY_URL", f"{COZE_BASE_URL}/v1/workflow/get_run_history")
COZE_IMAGE_WORKFLOW_ID = os.getenv("COZE_IMAGE_WORKFLOW_ID", "7629246632613117952").strip()
COZE_IMAGE_PROJECT_ID = os.getenv("COZE_IMAGE_PROJECT_ID", "7628909862734266368").strip()
COZE_IMAGE_WORKFLOW_TIMEOUT = int(os.getenv("COZE_IMAGE_WORKFLOW_TIMEOUT", str(COZE_WORKFLOW_TIMEOUT)))

# --- Local provider config (new Coze minimal workflows) ---
from .coze_llm import call_coze_llm
from .prompts import SEO_RESEARCH_PROMPT, SEO_GENERATE_PROMPT, format_prompt
from core.direct_llm import CLIPROXY_URL, CLIPROXY_API_KEY

NEW_COZE_WORKFLOW_ID = os.getenv("NEW_COZE_WORKFLOW_ID", "").strip()
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
LAST_SERPAPI_ERROR = ""

# Use-case image generation via the CLIProxy image model.
# The page LLM prompt is unchanged; these prompts are built from the four generated use cases.
USE_CASE_IMAGE_RESPONSES_MODEL = os.getenv("USE_CASE_IMAGE_RESPONSES_MODEL", "gpt-5.5").strip()
USE_CASE_IMAGE_MODEL = os.getenv("USE_CASE_IMAGE_MODEL", "gpt-image-2-codex").strip()
USE_CASE_IMAGE_URL = os.getenv(
    "USE_CASE_IMAGE_URL",
    CLIPROXY_URL.replace("/chat/completions", "/responses"),
).strip()
USE_CASE_IMAGE_API_KEY = os.getenv("USE_CASE_IMAGE_API_KEY", CLIPROXY_API_KEY).strip()
USE_CASE_IMAGE_TIMEOUT = int(os.getenv("USE_CASE_IMAGE_TIMEOUT", "300"))
USE_CASE_IMAGE_MAX_WORKERS = int(os.getenv("USE_CASE_IMAGE_MAX_WORKERS", "4"))
USE_CASE_IMAGE_SIZE = os.getenv("USE_CASE_IMAGE_SIZE", "1024x1024").strip()
USE_CASE_IMAGE_QUALITY = os.getenv("USE_CASE_IMAGE_QUALITY", "auto").strip()
USE_CASE_IMAGE_OUTPUT_FORMAT = os.getenv("USE_CASE_IMAGE_OUTPUT_FORMAT", "png").strip()


def _emit_progress(progress_cb, message):
    if progress_cb:
        try:
            progress_cb(message)
        except Exception:
            pass


def _enforce_json_output(prompt):
    """在提示词末尾加强 JSON 输出约束"""
    return prompt + (
        "\n\n--- CRITICAL OUTPUT INSTRUCTION ---\n"
        "You MUST respond with ONLY a valid JSON object. Nothing else.\n"
        "Do NOT include any text before or after the JSON.\n"
        "Do NOT use markdown code fences (```json or ```).\n"
        "Do NOT include explanations, comments, or commentary.\n"
        "Your ENTIRE response must be a single valid JSON object starting with { and ending with }.\n"
        "If you include anything other than JSON, the system will fail.\n"
        "--- END INSTRUCTION ---"
    )
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_BLOCKED_DOMAINS = [
    "tiktok.com", "youtube.com", "reddit.com", "instagram.com",
    "facebook.com", "twitter.com", "x.com", "pinterest.com",
    "quora.com", "linkedin.com", "amazon.com", "ebay.com",
]
DEFAULT_RESEARCH_URL_LIMIT = int(os.getenv("SEO_RESEARCH_URL_LIMIT", "3"))
MIN_RESEARCH_PAGES = int(os.getenv("SEO_MIN_RESEARCH_PAGES", "2"))
URL_RELEVANCE_PENALTY_TERMS = [
    "how-to-tell", "detect", "detector", "detection", "checker", "plagiarism",
    "review", "reviews", "alternatives", "pricing", "blog", "library",
    "article", "news", "guide", "tutorial", "what-is",
]
URL_RELEVANCE_BOOST_TERMS = [
    "tool", "tools", "writer", "writing", "generator", "agent", "app",
    "software", "product", "solution", "platform", "summarizer", "summarize",
]
URL_RELEVANCE_PRODUCT_DOMAINS = [
    "grammarly.com", "quillbot.com", "type.ai", "jasper.ai", "copy.ai",
    "writesonic.com", "wordtune.com", "notion.so", "canva.com",
]


def _local_search_serpapi(keyword, num=5):
    """SerpAPI Google search, returns list of URLs."""
    global LAST_SERPAPI_ERROR
    import urllib.parse
    LAST_SERPAPI_ERROR = ""
    serpapi_key = os.getenv("SERPAPI_KEY", "").strip() or SERPAPI_KEY
    if not serpapi_key:
        LAST_SERPAPI_ERROR = "SERPAPI_KEY is not configured in the MCP runtime environment"
        return []
    query = urllib.parse.urlencode({
        "engine": "google", "q": keyword, "num": num * 2, "api_key": serpapi_key,
    })
    try:
        resp = requests.get(f"https://serpapi.com/search.json?{query}", timeout=30)
        data = resp.json()
    except Exception as e:
        LAST_SERPAPI_ERROR = f"{type(e).__name__}: {e}"
        logger.error(f"SerpAPI search failed: {e}")
        return []
    if data.get("error"):
        LAST_SERPAPI_ERROR = str(data.get("error"))
        logger.error(f"SerpAPI search returned error: {LAST_SERPAPI_ERROR}")
        return []
    urls = []
    for item in data.get("organic_results", []):
        link = item.get("link", "")
        if not link:
            continue
        domain = urlparse(link).netloc.lower()
        if any(b in domain for b in FIRECRAWL_BLOCKED_DOMAINS):
            continue
        urls.append(link)
        if len(urls) >= num:
            break
    return urls


def _score_research_url(keyword, url):
    """Score URL-level relevance before expensive scrape/research stages."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    haystack = f"{host} {path.replace('-', ' ').replace('_', ' ')}"
    tokens = [t for t in re.findall(r"[a-z0-9]+", str(keyword).lower()) if len(t) > 1]
    score = 0
    for token in tokens:
        if token in haystack:
            score += 3
        if token in host:
            score += 1
    if any(term in path or term in host for term in URL_RELEVANCE_BOOST_TERMS):
        score += 4
    if any(domain in host for domain in URL_RELEVANCE_PRODUCT_DOMAINS):
        score += 3
    if any(term in path or term in host for term in URL_RELEVANCE_PENALTY_TERMS):
        score -= 5
    if path in ("", "/"):
        score -= 1
    if len(path.strip("/").split("/")) <= 2:
        score += 1
    return score


def _select_research_urls(keyword, urls, limit=None):
    """Keep the best URL candidates for LLM research while preserving fallback order."""
    limit = int(limit or DEFAULT_RESEARCH_URL_LIMIT or 3)
    if limit <= 0 or len(urls) <= limit:
        return list(urls)
    scored = []
    for index, url in enumerate(urls):
        scored.append((_score_research_url(keyword, url), -index, url))
    scored.sort(reverse=True)
    return [url for _, _, url in scored[:limit]]


def _local_scrape_firecrawl(url):
    """Firecrawl v2 scrape, returns page text."""
    if not FIRECRAWL_API_KEY:
        return ""
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v2/scrape",
            headers={
                "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
                "Content-Type": "application/json",
            },
            json={"url": url, "formats": ["markdown"]},
            timeout=60,
        )
        data = resp.json()
        return (data.get("data", {}).get("markdown", "") or "")[:8000]
    except Exception as e:
        logger.error(f"Firecrawl scrape failed for {url}: {e}")
        return ""


def _local_scrape_all(urls, max_workers=5, progress_cb=None):
    """Scrape multiple URLs in parallel."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results = [""] * len(urls)
    done = 0
    total = len(urls)
    _emit_progress(progress_cb, f"firecrawl_scraping 0/{total}")
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {pool.submit(_local_scrape_firecrawl, u): i for i, u in enumerate(urls)}
        for fut in as_completed(future_map):
            idx = future_map[fut]
            try:
                results[idx] = fut.result()
            except Exception:
                results[idx] = ""
            done += 1
            _emit_progress(progress_cb, f"firecrawl_scraping {done}/{total}")
    return results


def _local_merge_pages(keyword, pages):
    """Merge keyword + page texts into context string."""
    parts = [f"Keyword: {keyword}\n"]
    for i, page in enumerate(pages):
        if page.strip():
            parts.append(f"--- Page {i+1} ---\n{page.strip()[:6000]}\n")
    return "\n".join(parts)


def _local_seo_pipeline(keyword, progress_cb=None):
    """Full local SEO pipeline: search → scrape → merge → LLM research → LLM generate."""
    if not NEW_COZE_WORKFLOW_ID:
        return {"success": False, "error": "NEW_COZE_WORKFLOW_ID not configured"}

    # Step 1: Search + scrape
    _emit_progress(progress_cb, "serpapi_searching")
    urls = _local_search_serpapi(keyword, num=5)
    if not urls:
        error = f"SerpAPI returned no results for: {keyword}"
        if LAST_SERPAPI_ERROR:
            error = f"{error}. Diagnostic: {LAST_SERPAPI_ERROR}"
        return {"success": False, "error": error}
    _emit_progress(progress_cb, f"serpapi_found {len(urls)}")
    research_urls = _select_research_urls(keyword, urls)
    _emit_progress(progress_cb, f"research_urls_selected {len(research_urls)}/{len(urls)}")
    pages = _local_scrape_all(research_urls, progress_cb=progress_cb)
    scraped_count = sum(1 for page in pages if str(page).strip())
    min_pages = min(MIN_RESEARCH_PAGES, len(urls))
    if scraped_count < min_pages and len(research_urls) < len(urls):
        fallback_urls = [url for url in urls if url not in set(research_urls)]
        _emit_progress(progress_cb, f"research_urls_backfill {scraped_count}/{min_pages}")
        fallback_pages = _local_scrape_all(fallback_urls, max_workers=2, progress_cb=progress_cb)
        for url, page in zip(fallback_urls, fallback_pages):
            if scraped_count >= min_pages:
                break
            research_urls.append(url)
            pages.append(page)
            if str(page).strip():
                scraped_count += 1
    _emit_progress(progress_cb, "merging_research_context")
    context = _local_merge_pages(keyword, pages)

    # Step 2: LLM research
    _emit_progress(progress_cb, "llm_research")
    research_prompt = format_prompt(SEO_RESEARCH_PROMPT, keyword=keyword, context=context)
    research_prompt = _enforce_json_output(research_prompt)
    r1 = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": research_prompt})
    if not r1.get("success"):
        return {"success": False, "error": f"SEO research LLM failed: {r1.get('error')}"}
    research_report = r1["output"]

    # Step 3: LLM generate
    _emit_progress(progress_cb, "llm_generate_page_json")
    generate_prompt = format_prompt(SEO_GENERATE_PROMPT, keyword=keyword, context=research_report)
    generate_prompt = _enforce_json_output(generate_prompt)
    r2 = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": generate_prompt})
    if not r2.get("success"):
        return {"success": False, "error": f"SEO generate LLM failed: {r2.get('error')}"}

    # Sanitize any fabricated iWeaver URLs in the output
    _emit_progress(progress_cb, "sanitizing_generated_json")
    content = sanitize_iweaver_urls(r2["output"])
    return {"success": True, "content": content, "raw_data": ""}


# 配置请求URL
url = os.getenv("WP_PAGES_URL", "https://www.iweaver.ai/wp-json/wp/v2/pages")
WP_AUTH_HEADER = os.getenv("WP_AUTH_HEADER", "").strip()

# 设置请求头
headers = {
    "sec-ch-ua-platform": "macOS",
    "Referer": "https://admin.iweaver.ai/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "Content-Type": "application/json",
    "sec-ch-ua-mobile": "?0"
}
if WP_AUTH_HEADER:
    headers["Authorization"] = WP_AUTH_HEADER

WP_CONNECT_TIMEOUT = 10
WP_READ_TIMEOUT = 150
WP_MAX_RETRIES = 3
WP_RETRY_BACKOFF_SECONDS = (2, 5, 10)
from .paths import STORAGE_DIR

MEDIA_ID_CACHE_PATH = os.getenv("MEDIA_ID_CACHE_PATH", str(STORAGE_DIR / "media_id_cache.json"))
WP_API_BASE_URL = url.rsplit("/", 1)[0]
WP_MEDIA_URL = f"{WP_API_BASE_URL}/media"

def _legacy_generate_seo_content_dify(keyword):
    try:
        headers = {
            'Authorization': f'Bearer {API_KEY}',
            'Content-Type': 'application/json'
        }
        
        data = {
            "inputs": {"input": keyword},
            "response_mode": "blocking",
            "user": "abc-123"
        }
        
        max_retries = 3
        timeout = 300
        
        for attempt in range(max_retries):
            try:
                logger.info(f"开始第{attempt+1}次尝试...")
                response = requests.post(API_URL, headers=headers, json=data, timeout=timeout)
                break
            except requests.exceptions.Timeout:
                if attempt == max_retries - 1:
                    raise
                import time
                time.sleep(5)
        
        if response.status_code == 200:
            result = response.json()
            
            # 提取output内容
            if 'data' in result and 'outputs' in result['data'] and 'output' in result['data']['outputs']:
                content = result['data']['outputs']['output'].strip()
                return {'success': True, 'content': content}
            elif 'output' in result:
                content = result['output'].strip()
                return {'success': True, 'content': content}
            else:
                return {'success': False, 'error': f"无法提取output内容: {result}"}
        else:
            return {'success': False, 'error': f"API调用失败: {response.status_code}"}
            
    except requests.exceptions.Timeout:
        return {'success': False, 'error': "请求超时"}
    except Exception as e:
        return {'success': False, 'error': f"出错: {str(e)}"}

def _iter_text_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_text_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_text_values(item)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped


def _looks_like_final_output(text):
    if not text:
        return False
    text = text.strip()
    if text.startswith("```"):
        text = clean_output_content(text)
    if not text.startswith("{"):
        return False

    parsed = _parse_json_string_if_possible(text)
    if isinstance(parsed, dict):
        final_keys = {"main", "seo", "faq", "why_choose", "use_cases"}
        return bool(final_keys.intersection(parsed.keys()))

    wrapper_keys = ('"output"', '"result"', '"text"', '"content"', '"message"', '"answer"')
    if any(key in text for key in wrapper_keys):
        return False

    markers = ('"main"', '"seo"', '"faq"', '"why_choose"', '"use_cases"')
    return any(marker in text for marker in markers)


def _parse_json_string_if_possible(text):
    if not isinstance(text, str):
        return None

    candidate = text.strip()
    if not candidate:
        return None

    if candidate.startswith("```"):
        candidate = clean_output_content(candidate)

    if not candidate or candidate[0] not in "[{":
        return None

    try:
        return json.loads(candidate)
    except Exception:
        return None


def _extract_final_output_from_text(text):
    if not isinstance(text, str):
        return None

    candidate = text.strip()
    if not candidate:
        return None

    if candidate.startswith("```"):
        candidate = clean_output_content(candidate)

    parsed = _parse_json_string_if_possible(candidate)
    if parsed is not None:
        nested = _extract_agentos_output(parsed)
        if nested:
            return nested

    if _looks_like_final_output(candidate):
        return candidate

    return None


def _extract_agentos_output(payload):
    if isinstance(payload, dict):
        preferred_keys = (
            "output",
            "raw_output",
            "text",
            "result",
            "content",
            "answer",
            "message",
            "data",
        )
        for key in preferred_keys:
            value = payload.get(key)
            if isinstance(value, str):
                extracted = _extract_final_output_from_text(value)
                if extracted:
                    return extracted
            elif isinstance(value, (dict, list)):
                nested = _extract_agentos_output(value)
                if nested:
                    return nested

    for text in _iter_text_values(payload):
        extracted = _extract_final_output_from_text(text)
        if extracted:
            return extracted
    return None


def _is_agentos_finished(process_payload):
    data = process_payload.get("data") if isinstance(process_payload, dict) else None
    if not isinstance(data, dict):
        return False

    execute_status = data.get("executeStatus")
    if execute_status in (2, 3, 4, 5):
        return True

    node_results = data.get("nodeResults")
    if isinstance(node_results, list) and node_results:
        unfinished_statuses = {0, 1, 2}
        return all(
            not isinstance(node, dict) or node.get("nodeStatus") not in unfinished_statuses
            for node in node_results
        )

    return False


def _summarize_agentos_process_issue(process_payload):
    data = process_payload.get("data") if isinstance(process_payload, dict) else None
    if not isinstance(data, dict):
        return "AgentOS finished but no final JSON content was found."

    execute_status = data.get("executeStatus")
    node_results = data.get("nodeResults") or []
    node_errors = []
    for node in node_results:
        if not isinstance(node, dict):
            continue
        error_info = (node.get("errorInfo") or "").strip()
        if error_info:
            node_errors.append(f'{node.get("NodeName") or node.get("nodeId")}: {error_info}')

    if node_errors:
        return (
            f"AgentOS finished with executeStatus={execute_status}, but no final JSON content "
            f"was found. Node errors: {' | '.join(node_errors[:3])}"
        )

    return (
        f"AgentOS finished with executeStatus={execute_status}, but the backend could not parse "
        f"the final JSON content from the workflow payload."
    )


def _request_agentos_process(headers, workflow_id, execute_id):
    attempts = (
        ("get", {"workflow_id": workflow_id, "execute_id": execute_id}),
        ("post", {"workflow_id": workflow_id, "execute_id": execute_id}),
        (
            "post",
            {
                "workflow_id": workflow_id,
                "execute_id": execute_id,
                "commit_id": AGENTOS_COMMIT_ID,
            },
        ),
        ("get", {"execute_id": execute_id}),
        ("post", {"execute_id": execute_id}),
    )

    last_error = None
    for method, payload in attempts:
        try:
            if method == "get":
                response = requests.get(
                    AGENTOS_PROCESS_URL,
                    headers=headers,
                    params=payload,
                    timeout=AGENTOS_START_TIMEOUT,
                )
            else:
                response = requests.post(
                    AGENTOS_PROCESS_URL,
                    headers=headers,
                    json=payload,
                    timeout=AGENTOS_START_TIMEOUT,
                )
            if response.status_code == 200:
                return response.json()
            last_error = f"{method.upper()} {response.status_code}: {response.text[:300]}"
        except Exception as exc:
            last_error = str(exc)
    raise RuntimeError(last_error or "Unable to fetch AgentOS workflow process.")


def _run_agentos_workflow(keyword):
    if not AGENTOS_TOKEN:
        return {"success": False, "error": "Missing AGENTOS_TOKEN environment variable."}

    request_headers = {
        "Authorization": f"Bearer {AGENTOS_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    payload = {
        "input": {"input": keyword},
        "commit_id": AGENTOS_COMMIT_ID,
    }

    try:
        response = requests.post(
            AGENTOS_RUN_URL,
            headers=request_headers,
            json=payload,
            timeout=AGENTOS_START_TIMEOUT,
        )
        response.raise_for_status()
        run_result = response.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "AgentOS start request timed out."}
    except Exception as exc:
        return {"success": False, "error": f"AgentOS start request failed: {exc}"}

    data = run_result.get("data") or {}
    workflow_id = data.get("workflow_id")
    execute_id = data.get("execute_id")

    if not workflow_id or not execute_id:
        return {
            "success": False,
            "error": f"AgentOS start response missing workflow_id/execute_id: {run_result}",
        }

    started_at = time.time()
    last_process_payload = None
    while time.time() - started_at < AGENTOS_POLL_TIMEOUT:
        try:
            process_payload = _request_agentos_process(request_headers, workflow_id, execute_id)
            last_process_payload = process_payload
            content = _extract_agentos_output(process_payload)
            if content:
                return {"success": True, "content": content}
            if _is_agentos_finished(process_payload):
                return {
                    "success": False,
                    "error": _summarize_agentos_process_issue(process_payload),
                }
        except Exception as exc:
            last_process_payload = {"error": str(exc)}

        time.sleep(AGENTOS_POLL_INTERVAL)

    return {
        "success": False,
        "error": f"AgentOS workflow polling timed out. Last payload: {last_process_payload}",
    }


def _extract_coze_content(run_result):
    payload_data = run_result.get("data")
    extracted = _extract_agentos_output(payload_data)
    if extracted:
        return extracted

    parsed_payload = _parse_json_string_if_possible(payload_data)
    if isinstance(parsed_payload, dict):
        for key in ("output", "result", "text", "content", "answer", "message"):
            value = parsed_payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return json.dumps(parsed_payload, ensure_ascii=False)

    if isinstance(payload_data, str) and payload_data.strip():
        return payload_data.strip()

    if isinstance(payload_data, (dict, list)):
        return json.dumps(payload_data, ensure_ascii=False)

    return ""


def _request_coze_run_history(request_headers, workflow_id, execute_id, timeout_seconds):
    response = requests.get(
        COZE_RUN_HISTORY_URL,
        headers=request_headers,
        params={
            "workflow_id": workflow_id,
            "execute_id": execute_id,
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in (0, "0", None):
        raise ValueError(f"Coze run history returned an error: {payload}")
    return payload


def _run_coze_workflow(input_value, workflow_id=None, project_id=None, timeout_seconds=None, is_async=False):
    workflow_id = (workflow_id or COZE_WORKFLOW_ID or "").strip()
    project_id = (project_id or COZE_PROJECT_ID or "").strip()
    timeout_seconds = timeout_seconds or COZE_WORKFLOW_TIMEOUT

    if not COZE_WORKFLOW_TOKEN:
        return {"success": False, "error": "Missing COZE_WORKFLOW_TOKEN environment variable."}
    if not workflow_id:
        return {"success": False, "error": "Missing Coze workflow id."}

    request_headers = {
        "Authorization": f"Bearer {COZE_WORKFLOW_TOKEN}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
    }
    payload = {
        "workflow_id": workflow_id,
        "parameters": {"input": input_value},
        "is_async": bool(is_async),
    }
    if project_id:
        payload["project_id"] = project_id

    try:
        response = requests.post(
            COZE_RUN_URL,
            headers=request_headers,
            json=payload,
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        run_result = response.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Coze workflow request timed out."}
    except Exception as exc:
        return {"success": False, "error": f"Coze workflow request failed: {exc}"}

    if run_result.get("code") not in (0, "0", None):
        return {"success": False, "error": f"Coze workflow returned an error: {run_result}"}

    if is_async:
        execute_id = run_result.get("execute_id")
        if not execute_id:
            return {"success": False, "error": f"Coze async workflow returned no execute_id: {run_result}"}

        started_at = time.time()
        last_payload = None
        while time.time() - started_at < timeout_seconds:
            try:
                history_payload = _request_coze_run_history(
                    request_headers,
                    workflow_id,
                    execute_id,
                    min(timeout_seconds, WP_READ_TIMEOUT),
                )
                last_payload = history_payload
                history_items = history_payload.get("data") or []
                if history_items:
                    latest = history_items[0]
                    execute_status = str(latest.get("execute_status", "")).strip().lower()
                    if execute_status == "success":
                        output_value = latest.get("output")
                        if isinstance(output_value, str) and output_value.strip():
                            return {"success": True, "content": output_value.strip(), "execute_id": execute_id}
                        if isinstance(output_value, (dict, list)):
                            return {
                                "success": True,
                                "content": json.dumps(output_value, ensure_ascii=False),
                                "execute_id": execute_id,
                            }
                        return {"success": False, "error": f"Coze async workflow finished without output: {latest}"}
                    if execute_status in {"fail", "failed", "cancel", "canceled"}:
                        return {"success": False, "error": f"Coze async workflow failed: {latest}"}
            except Exception as exc:
                last_payload = {"error": str(exc)}

            time.sleep(2)

        return {
            "success": False,
            "error": f"Coze async workflow polling timed out. Last payload: {last_payload}",
        }

    content = _extract_coze_content(run_result)
    if content:
        return {"success": True, "content": content, "raw_data": run_result.get("data", "")}

    return {
        "success": False,
        "error": f"Coze workflow returned no usable content: {run_result}",
    }


def generate_seo_content(keyword, progress_cb=None):
    if WORKFLOW_PROVIDER == "coze":
        _emit_progress(progress_cb, "coze_workflow_running")
        return _run_coze_workflow(keyword)
    if WORKFLOW_PROVIDER == "agentos":
        _emit_progress(progress_cb, "agentos_workflow_running")
        return _run_agentos_workflow(keyword)
    if WORKFLOW_PROVIDER == "local":
        return _local_seo_pipeline(keyword, progress_cb=progress_cb)
    return {
        "success": False,
        "error": f"Unsupported WORKFLOW_PROVIDER: {WORKFLOW_PROVIDER}",
    }


def clean_output_content(content):
    """
    清理输出内容，去除可能的markdown代码块标记
    """
    content = content.strip()
    
    # 匹配以 ```json 或 ``` 开头和结尾的代码块
    # 模式1: ```json ... ```
    # 模式2: ``` ... ```
    pattern = r'^```(?:json)?\s*\n?(.*?)\n?```$'
    match = re.search(pattern, content, re.DOTALL)
    
    if match:
        # 提取代码块内的内容
        return match.group(1).strip()
    
    # 如果没有代码块标记，直接返回原内容
    return content


IWEAVER_HOMEPAGE = "https://www.iweaver.ai/"


def sanitize_iweaver_urls(html):
    """Replace fabricated iWeaver URLs with the real homepage."""
    if not html:
        return html
    def _replace(m):
        url = m.group(1)
        if url.rstrip('/') in ('https://www.iweaver.ai', 'http://www.iweaver.ai'):
            return m.group(0)
        return f'href="{IWEAVER_HOMEPAGE}"'
    return re.sub(r'href=["\']([^"\']*iweaver\.ai[^"\']*)["\']', _replace, html, flags=re.IGNORECASE)


FORBIDDEN_COPY_REPLACEMENTS = (
    (re.compile(r"\bNo\s+Sign[\s-]*up\s+(?:Needed|Required)\b", re.IGNORECASE), "Signup required"),
    (re.compile(r"\bNo\s+Login\s+Required\b", re.IGNORECASE), "Login required"),
    (re.compile(r"\bNo\s+Credit\s+Card\s+Required\b", re.IGNORECASE), "Pricing details available after signup"),
)


def sanitize_copy_text(value):
    if not isinstance(value, str):
        return value

    cleaned = value.strip()
    for pattern, replacement in FORBIDDEN_COPY_REPLACEMENTS:
        cleaned = pattern.sub(replacement, cleaned)

    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:!?]){2,}", r"\1", cleaned)
    return cleaned.strip(" ,")


def sanitize_generated_content(value):
    if isinstance(value, dict):
        return {key: sanitize_generated_content(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_generated_content(item) for item in value]
    if isinstance(value, str):
        return sanitize_copy_text(value)
    return value


def parse_keyword_candidates(raw_keywords):
    if isinstance(raw_keywords, str):
        candidates = re.split(r"[,|\n]+", raw_keywords)
    elif isinstance(raw_keywords, list):
        candidates = raw_keywords
    else:
        candidates = []

    normalized = []
    seen = set()
    for item in candidates:
        keyword = sanitize_copy_text(str(item or ""))
        if not keyword:
            continue
        dedupe_key = keyword.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(keyword)
    return normalized


def trim_to_char_limit(value, max_length):
    text = sanitize_copy_text(value)
    if len(text) <= max_length:
        return text

    truncated = text[: max_length + 1]
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0]
    truncated = truncated[:max_length].rstrip(" ,.;:-|")
    return truncated


def trim_description_to_complete_sentence(value, max_length=155):
    text = sanitize_copy_text(value)
    if len(text) <= max_length:
        return text

    truncated = trim_to_char_limit(text, max_length)
    sentence_endings = [".", "!", "?"]

    sentence_candidates = re.findall(r"[^.!?]+[.!?]", text)
    complete_sentences = []
    for sentence in sentence_candidates:
        sentence = sanitize_copy_text(sentence)
        if sentence and len(sentence) <= max_length:
            complete_sentences.append(sentence)

    if complete_sentences:
        combined = ""
        for sentence in complete_sentences:
            candidate = f"{combined} {sentence}".strip()
            if len(candidate) > max_length:
                break
            combined = candidate
        if combined:
            return combined

    last_period = max(truncated.rfind(mark) for mark in sentence_endings)
    if last_period >= 0:
        cleaned = truncated[: last_period + 1].strip()
        if cleaned:
            return cleaned

    truncated = truncated.rstrip(" ,.;:-|")
    if truncated and truncated[-1] not in sentence_endings:
        truncated = f"{truncated}."
    return truncated


def normalize_seo_title(raw_title, primary_keyword):
    keyword = sanitize_copy_text(primary_keyword)
    title = sanitize_copy_text(raw_title)
    suffix = "| iWeaver"

    if not title:
        title = keyword

    if title.endswith("|iWeaver"):
        title = title[:-9].rstrip()
    elif title.endswith("| iWeaver"):
        title = title[:-10].rstrip()

    if keyword and keyword.casefold() not in title.casefold():
        title = keyword

    available_length = 60 - len(suffix) - 1
    title = trim_to_char_limit(title, max(available_length, 1))
    final_title = f"{title} {suffix}".strip()
    return trim_to_char_limit(final_title, 60)


def normalize_seo_description(raw_description, fallback_description):
    description = sanitize_copy_text(raw_description) or sanitize_copy_text(fallback_description)
    return trim_description_to_complete_sentence(description, 155)


def build_seo_payload(data, keyword_input):
    seo_data = data.get("seo") if isinstance(data.get("seo"), dict) else {}
    keyword_candidates = parse_keyword_candidates(keyword_input)
    primary_keyword = keyword_candidates[0] if keyword_candidates else sanitize_copy_text(data.get("main", {}).get("title_H1", ""))

    seo_title = normalize_seo_title(seo_data.get("title", ""), primary_keyword)
    seo_description = normalize_seo_description(
        seo_data.get("description", ""),
        data.get("main", {}).get("description", ""),
    )

    provided_keywords = parse_keyword_candidates(seo_data.get("keywords", []))
    merged_keywords = []
    seen = set()
    for keyword in provided_keywords + keyword_candidates:
        dedupe_key = keyword.casefold()
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        merged_keywords.append(keyword)
        if len(merged_keywords) == 5:
            break

    if primary_keyword and primary_keyword.casefold() not in seen:
        merged_keywords.insert(0, primary_keyword)
        merged_keywords = merged_keywords[:5]

    return {
        "title": seo_title,
        "description": seo_description,
        "keywords": merged_keywords,
        "focus_keyword": ", ".join(merged_keywords),
    }


def slugify_english(*parts, fallback="generated-asset", max_words=0, strip_digits=False):
    raw_text = "-".join(str(part).strip() for part in parts if str(part or "").strip())
    if not raw_text:
        raw_text = fallback

    normalized = unicodedata.normalize("NFKD", raw_text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    if strip_digits:
        ascii_text = re.sub(r"[0-9]", "", ascii_text)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if max_words > 0:
        slug = "-".join(slug.split("-")[:max_words])
    return slug or fallback


def normalize_use_case_image_map(image_map):
    normalized = {}
    image_map = image_map if isinstance(image_map, dict) else {}
    for index in range(1, 5):
        key = f"image_url_{index}"
        normalized[key] = str(image_map.get(key, "") or "").strip()
    return normalized


def use_case_placeholder_image_url(index=1):
    colors = [
        ("#EAF1FF", "#D8E5FF", "#155DFC"),
        ("#EEF8FF", "#DDF3FF", "#0EA5E9"),
        ("#F4F0FF", "#E9DFFF", "#6841EA"),
        ("#EEFDF7", "#DDF8EA", "#10B981"),
    ]
    bg, panel, accent = colors[(int(index or 1) - 1) % len(colors)]
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="768" viewBox="0 0 1024 768">
<rect width="1024" height="768" fill="{bg}"/>
<rect x="132" y="132" width="760" height="504" rx="44" fill="{panel}"/>
<rect x="208" y="220" width="320" height="36" rx="18" fill="{accent}" opacity=".88"/>
<rect x="208" y="296" width="608" height="28" rx="14" fill="{accent}" opacity=".28"/>
<rect x="208" y="350" width="520" height="28" rx="14" fill="{accent}" opacity=".20"/>
<circle cx="742" cy="506" r="88" fill="{accent}" opacity=".18"/>
<path d="M662 530c56-80 100-80 160 0" fill="none" stroke="{accent}" stroke-width="28" stroke-linecap="round" opacity=".42"/>
</svg>"""
    encoded = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"


def complete_use_case_image_map(image_map):
    normalized = normalize_use_case_image_map(image_map)
    for index in range(1, 5):
        key = f"image_url_{index}"
        if not normalized.get(key):
            normalized[key] = use_case_placeholder_image_url(index)
    return normalized


def wp_request(method, endpoint, **kwargs):
    request_headers = dict(headers)
    extra_headers = kwargs.pop("headers", None)
    if extra_headers:
        request_headers.update(extra_headers)

    timeout = kwargs.pop("timeout", (WP_CONNECT_TIMEOUT, WP_READ_TIMEOUT))
    retryable_statuses = {429, 500, 502, 503, 504}
    last_error = None
    last_response = None

    for attempt in range(WP_MAX_RETRIES):
        try:
            response = requests.request(
                method,
                endpoint,
                headers=request_headers,
                timeout=timeout,
                **kwargs,
            )
            last_response = response
            if response.status_code not in retryable_statuses:
                return response
        except requests.exceptions.RequestException as exc:
            last_error = exc

        if attempt < WP_MAX_RETRIES - 1:
            time.sleep(WP_RETRY_BACKOFF_SECONDS[min(attempt, len(WP_RETRY_BACKOFF_SECONDS) - 1)])

    if last_response is not None:
        return last_response
    raise last_error


def load_media_id_cache():
    if not os.path.exists(MEDIA_ID_CACHE_PATH):
        return {}
    try:
        with open(MEDIA_ID_CACHE_PATH, "r", encoding="utf-8") as cache_file:
            cache = json.load(cache_file)
        if isinstance(cache, dict):
            return cache
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_media_id_cache(cache):
    try:
        with open(MEDIA_ID_CACHE_PATH, "w", encoding="utf-8") as cache_file:
            json.dump(cache, cache_file, ensure_ascii=False, indent=2)
    except OSError:
        pass


def resolve_media_id(media_value):
    if isinstance(media_value, str):
        media_value = media_value.strip()
    if not media_value:
        return ""
    if str(media_value).isdigit():
        return int(media_value)
    if not isinstance(media_value, str) or not media_value.lower().startswith(("http://", "https://")):
        return media_value

    normalized_url = media_value.split("?", 1)[0]
    media_cache = load_media_id_cache()
    cached_item = media_cache.get(normalized_url)
    if isinstance(cached_item, dict) and cached_item.get("id"):
        return cached_item["id"]

    file_name = urlparse(normalized_url).path.rsplit("/", 1)[-1]
    response = wp_request(
        "GET",
        WP_MEDIA_URL,
        params={
            "search": file_name,
            "media_type": "image",
            "per_page": 20,
            "_fields": "id,source_url",
        },
    )
    response.raise_for_status()

    payload = response.json()
    if isinstance(payload, list):
        media_items = payload
    elif isinstance(payload, dict):
        if payload.get("id") and payload.get("source_url"):
            media_items = [payload]
        elif payload.get("code") or payload.get("message"):
            raise ValueError(f"Media lookup failed: {payload}")
        else:
            raise ValueError(f"Unexpected media lookup response: {payload}")
    else:
        raise ValueError(f"Unexpected media lookup payload type: {type(payload).__name__}")

    for item in media_items:
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url", "")).split("?", 1)[0]
        if source_url == normalized_url:
            media_id = item.get("id")
            media_cache[normalized_url] = {
                "id": media_id,
                "source_url": source_url,
                "updated_at": int(time.time()),
            }
            save_media_id_cache(media_cache)
            return media_id

    raise ValueError(f"Unable to resolve media ID from URL: {media_value}")


def get_registered_acf_fields(endpoint):
    try:
        response = wp_request("OPTIONS", endpoint)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        return None

    payload = response.json()
    acf_schema = payload.get("schema", {}).get("properties", {}).get("acf", {})
    properties = acf_schema.get("properties", {})
    if isinstance(properties, dict):
        return properties
    return {}


def normalize_acf_response(acf_value):
    if isinstance(acf_value, dict):
        return acf_value
    return {}


def field_has_expected_value(field_name, expected_value, saved_acf):
    if field_name not in saved_acf:
        return False

    actual_value = saved_acf.get(field_name)
    if field_name == "agent_page_desc":
        return isinstance(actual_value, str) and actual_value.strip() == str(expected_value).strip()

    if field_name == "agent_page_img":
        if actual_value in ("", None, False, [], {}):
            return False
        if isinstance(actual_value, int):
            return actual_value == expected_value
        if isinstance(actual_value, str) and str(expected_value).isdigit():
            return actual_value.strip() == str(expected_value)
        if isinstance(actual_value, dict):
            actual_id = actual_value.get("id") or actual_value.get("ID")
            if actual_id:
                return int(actual_id) == int(expected_value)
            return bool(actual_value)
        return bool(actual_value)

    return saved_acf.get(field_name) == expected_value


def decode_image_source(image_source):
    if not isinstance(image_source, str):
        raise ValueError("Image source must be a string.")

    image_source = image_source.strip()
    if not image_source:
        raise ValueError("Image source is empty.")

    if image_source.startswith("data:"):
        _, encoded = image_source.split(",", 1)
        return base64.b64decode(encoded)

    if image_source.lower().startswith(("http://", "https://")):
        response = requests.get(image_source, timeout=(WP_CONNECT_TIMEOUT, WP_READ_TIMEOUT))
        response.raise_for_status()
        return response.content

    try:
        return base64.b64decode(image_source)
    except Exception as exc:
        raise ValueError("Unsupported image source format.") from exc


def convert_image_source_to_webp_bytes(image_source):
    raw_bytes = decode_image_source(image_source)
    source_buffer = BytesIO(raw_bytes)
    output_buffer = BytesIO()

    with Image.open(source_buffer) as image:
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            converted = image.convert("RGBA")
        else:
            converted = image.convert("RGB")
        converted.save(output_buffer, format="WEBP", quality=90, method=6)

    return output_buffer.getvalue()


def upload_media_to_wp(image_bytes, filename, alt_text=""):
    safe_filename = slugify_english(filename.rsplit(".", 1)[0], fallback="use-case-image") + ".webp"
    upload_response = wp_request(
        "POST",
        WP_MEDIA_URL,
        data=image_bytes,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_filename}"',
            "Content-Type": "image/webp",
        },
    )
    upload_response.raise_for_status()

    if not upload_response.headers.get("content-type", "").startswith("application/json"):
        raise ValueError(f"Unexpected WordPress media upload response: {upload_response.text[:300]}")

    payload = upload_response.json()
    media_id = payload.get("id")
    source_url = payload.get("source_url")
    if not media_id or not source_url:
        raise ValueError(f"WordPress media upload missing id/source_url: {payload}")

    normalized_source_url = str(source_url).split("?", 1)[0]
    media_cache = load_media_id_cache()
    media_cache[normalized_source_url] = {
        "id": media_id,
        "source_url": normalized_source_url,
        "updated_at": int(time.time()),
    }
    save_media_id_cache(media_cache)

    if alt_text:
        metadata_response = wp_request(
            "POST",
            f"{WP_MEDIA_URL}/{media_id}",
            json={
                "alt_text": alt_text.strip(),
                "title": alt_text.strip(),
            },
        )
        if metadata_response.status_code not in (200, 201):
            raise ValueError(f"WordPress media metadata update failed: {metadata_response.text[:300]}")
        if metadata_response.headers.get("content-type", "").startswith("application/json"):
            payload = metadata_response.json()

    return {
        "id": media_id,
        "source_url": payload.get("source_url", source_url),
        "filename": safe_filename,
    }


def update_wp_page_content(page_id, complete_html):
    if not page_id:
        return {"success": False, "error": "page_id is required"}
    endpoint = f"{url}/{page_id}"
    response = wp_request(
        "POST",
        endpoint,
        json={"content": complete_html},
    )
    if response.headers.get("content-type", "").startswith("application/json"):
        payload = response.json()
    else:
        payload = response.text
    if response.status_code not in (200, 201):
        return {
            "success": False,
            "status_code": response.status_code,
            "response": payload,
            "error": payload,
        }
    return {
        "success": True,
        "status_code": response.status_code,
        "page_id": page_id,
        "response": payload,
    }


def parse_use_case_image_payload(content):
    parsed = _parse_json_string_if_possible(content)
    if not isinstance(parsed, dict):
        raise ValueError("Image workflow did not return a valid JSON object.")
    return parsed


def build_use_case_image_prompt(keyword, case, index):
    """Build a deterministic image prompt from one generated use case."""
    title = sanitize_copy_text(case.get("title", "")) if isinstance(case, dict) else ""
    description = sanitize_copy_text(case.get("description", "")) if isinstance(case, dict) else ""
    keyword = sanitize_copy_text(keyword)

    return (
        "Generate one image for an iWeaver SaaS landing page use-case section.\n\n"
        f"Primary keyword: {keyword}\n"
        f"Use case {index} title: {title}\n"
        f"Use case {index} description: {description}\n\n"
        "Visual direction:\n"
        "- Modern SaaS website illustration for this exact use case.\n"
        "- Show realistic people, documents, screens, knowledge workflows, or productivity objects that match the use case.\n"
        "- Clean bright workspace, professional technology product feel.\n"
        "- Blue and purple iWeaver-style gradient accents, soft lighting, polished composition.\n"
        "- 4:3 aspect ratio, suitable for a website use-case card image.\n"
        "- No readable text, no letters, no logos, no watermark, no UI copy.\n"
        "- Do not include the iWeaver logo.\n"
        "Return only the image data or image URL."
    )


def _iter_response_strings(value):
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            yield stripped
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_response_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_response_strings(item)


def _looks_like_base64_image(text):
    if not isinstance(text, str):
        return False
    compact = re.sub(r"\s+", "", text.strip())
    if len(compact) < 200:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        return False
    return compact.startswith(("iVBOR", "/9j/", "UklGR", "R0lGOD"))


def extract_image_source_from_cliproxy_response(payload):
    """Extract a data:image URL, HTTP image URL, or raw base64 image from common image responses."""
    if isinstance(payload, dict):
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "image_generation_call" and item.get("result"):
                    return re.sub(r"\s+", "", str(item["result"]))

        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                value = item.get("url") or item.get("image_url")
                if isinstance(value, dict):
                    value = value.get("url")
                if isinstance(value, str) and value.strip():
                    return value.strip()
                b64_value = item.get("b64_json") or item.get("base64") or item.get("image_base64")
                if isinstance(b64_value, str) and b64_value.strip():
                    return re.sub(r"\s+", "", b64_value)

    for text in _iter_response_strings(payload):
        data_match = re.search(r"data:image/[^;]+;base64,[A-Za-z0-9+/=\r\n]+", text)
        if data_match:
            return re.sub(r"\s+", "", data_match.group(0))

    for text in _iter_response_strings(payload):
        url_match = re.search(r"https?://[^\s\"'<>]+", text)
        if url_match:
            candidate = url_match.group(0).rstrip(".,);]")
            if re.search(r"\.(?:png|jpe?g|webp|gif)(?:\?|$)", candidate, re.IGNORECASE):
                return candidate

    for text in _iter_response_strings(payload):
        if _looks_like_base64_image(text):
            return re.sub(r"\s+", "", text.strip())
    return ""


def _image_headers():
    return {
        "Authorization": f"Bearer {USE_CASE_IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }


def _call_use_case_image_responses(prompt):
    payload = {
        "model": USE_CASE_IMAGE_RESPONSES_MODEL,
        "input": prompt,
        "tools": [
            {
                "type": "image_generation",
                "model": USE_CASE_IMAGE_MODEL,
                "size": USE_CASE_IMAGE_SIZE,
                "quality": USE_CASE_IMAGE_QUALITY,
                "output_format": USE_CASE_IMAGE_OUTPUT_FORMAT,
            }
        ],
        "stream": False,
    }
    try:
        logger.info(
            "Calling CLIProxy responses image tool top_model=%s, image_model=%s, size=%s, prompt_length=%s",
            USE_CASE_IMAGE_RESPONSES_MODEL,
            USE_CASE_IMAGE_MODEL,
            USE_CASE_IMAGE_SIZE,
            len(prompt),
        )
        response = requests.post(USE_CASE_IMAGE_URL, headers=_image_headers(), json=payload, timeout=USE_CASE_IMAGE_TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": f"CLIProxy responses image tool timed out after {USE_CASE_IMAGE_TIMEOUT}s"}
    except requests.exceptions.HTTPError as exc:
        detail = exc.response.text[:500] if exc.response is not None else str(exc)
        return {"success": False, "error": f"CLIProxy responses image tool HTTP error: {detail}"}
    except Exception as exc:
        return {"success": False, "error": f"CLIProxy responses image tool failed: {exc}"}

    image_source = extract_image_source_from_cliproxy_response(result)
    if not image_source:
        return {"success": False, "error": f"CLIProxy responses image tool returned no image data: {str(result)[:500]}"}
    return {"success": True, "image_source": image_source, "provider": "cliproxy_responses_image_generation"}


def call_use_case_image_model(prompt):
    if not USE_CASE_IMAGE_URL or not USE_CASE_IMAGE_API_KEY or not USE_CASE_IMAGE_MODEL:
        return {"success": False, "error": "Missing CLIProxy image model configuration"}

    return _call_use_case_image_responses(prompt)

def generate_one_use_case_image(keyword, page_slug, index, case, progress_cb=None, target_filename=""):
    _emit_progress(progress_cb, f"use_case_image_generating {index}/4")
    prompt = build_use_case_image_prompt(keyword, case, index)
    result = call_use_case_image_model(prompt)
    if not result.get("success"):
        raise ValueError(result.get("error", "Failed to generate use case image."))

    _emit_progress(progress_cb, f"use_case_image_generated {index}/4")
    _emit_progress(progress_cb, f"use_case_image_uploading {index}/4")
    base_slug = slugify_english(page_slug or keyword, fallback="use-case-image")
    case_title = case.get("title", "") if isinstance(case, dict) else ""
    file_stem = slugify_english(base_slug, case_title, fallback=base_slug, max_words=7, strip_digits=True)
    webp_bytes = convert_image_source_to_webp_bytes(result["image_source"])
    upload_filename = target_filename or f"{file_stem}.webp"
    uploaded = upload_media_to_wp(webp_bytes, upload_filename, alt_text=case_title)
    _emit_progress(progress_cb, f"use_case_image_uploaded {index}/4")
    return index, uploaded


def generate_use_case_images(content_data, keyword, page_slug="", progress_cb=None):
    use_cases = ((content_data or {}).get("use_cases") or {}).get("cases") or []
    if len(use_cases) < 4:
        raise ValueError("Use case image generation requires 4 use cases from the content workflow output.")

    from concurrent.futures import ThreadPoolExecutor, as_completed

    uploaded_images = {}
    max_workers = max(1, min(USE_CASE_IMAGE_MAX_WORKERS, len(use_cases[:4])))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(generate_one_use_case_image, keyword, page_slug, index, case, progress_cb): index
            for index, case in enumerate(use_cases[:4], start=1)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                image_index, uploaded = future.result()
                uploaded_images[f"image_url_{image_index}"] = uploaded["source_url"]
            except Exception as exc:
                logger.warning("Use case image %s generation failed: %s", index, exc)

    missing = [f"image_url_{index}" for index in range(1, 5) if not uploaded_images.get(f"image_url_{index}")]
    if missing:
        raise ValueError(f"Use case image generation failed for: {', '.join(missing)}")
    _emit_progress(progress_cb, "use_case_images_finished")
    return uploaded_images


def ensure_use_case_image_urls(content_data, keyword, page_slug="", provided_images=None, progress_cb=None):
    normalized_images = normalize_use_case_image_map(provided_images)
    if all(normalized_images.values()):
        return normalized_images
    try:
        return generate_use_case_images(content_data, keyword, page_slug=page_slug, progress_cb=progress_cb)
    except Exception as e:
        logger.warning(f"Image generation failed, continuing without images: {e}")
        return normalized_images


def split_highlighted_brand(title, brand="iWeaver"):
    text = (title or "").strip()
    if not text:
        return "", "", ""

    match = re.search(re.escape(brand), text, re.IGNORECASE)
    if not match:
        return text, "", ""

    prefix = text[:match.start()].strip()
    highlight = text[match.start():match.end()]
    suffix = text[match.end():].strip()
    return prefix, highlight, suffix


def render_hero_section(main_data):
    title_prefix, title_highlight, title_suffix = split_highlighted_brand(main_data.get("title_H1", ""))
    h1_parts = []
    if title_prefix:
        h1_parts.append(f'<span data-wp-key="hero.titlePrefix" id="iw-editable-1">{title_prefix}</span>')
    if title_highlight:
        h1_parts.append(
            '<span class="bg-gradient-to-r from-[#155DFC] to-[#00D3F2] '
            'bg-clip-text text-transparent" data-wp-key="hero.titleHighlight" '
            f'id="iw-editable-2">{title_highlight}</span>'
        )
    if title_suffix:
        h1_parts.append(f'<span data-wp-key="hero.titleSuffix" id="iw-editable-3">{title_suffix}</span>')
    if not h1_parts:
        h1_parts.append('<span data-wp-key="hero.titlePrefix" id="iw-editable-1"></span>')
    h1_html = ' '.join(h1_parts)

    return f"""
    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="hero">
    <section class="md:mt-[130px] mb-[42px]" data-xd-module="hero"><div class="absolute top-0 left-0 w-full h-full pointer-events-none"><div class="absolute top-0 left-0 w-full h-[440px] md:opacity-60 bg-[#eff6ff]/50 blur-[64px]"></div></div><div class="iw-container mx-auto max-w-[1200px] pt-[56px] md:pt-0 md:px-4 text-center z-10 relative"><div><h1 class="text-4xl md:text-[72px] font-bold text-[#101828] tracking-tight mb-6" id="iw-editable-0">{h1_html}</h1><p class="max-w-[665px] mx-auto text-[20px] text-[#4A5565] leading-[32.5px] tracking-[-0.45px]" data-wp-key="hero.description" id="iw-editable-5">{main_data["description"]}</p></div></div></section>
    </div>
    <!-- /wp:html -->

    """


def render_faq_section(faq_data):
    faq_items = faq_data.get("items", [])
    question_base_id = 47
    items_html = []

    for index, item in enumerate(faq_items):
        expanded = index == len(faq_items) - 1
        expanded_attr = "true" if expanded else "false"
        plus_opacity = "0" if expanded else "100"
        minus_opacity = "100" if expanded else "0"
        max_height = "80px !important;" if expanded else "0px !important;"
        content_classes = "faq-content max-h-0 overflow-hidden transition-all duration-300 ease-in-out"
        if not expanded:
            content_classes += " opacity-0"

        items_html.append(
            f'<div class="faq-item border border-[#E5E7EB] rounded-[14px] overflow-hidden bg-white transition-all duration-300" data-index="{index}">'
            f'<h3 class="m-0">'
            f'<button class="faq-button flex items-center justify-between w-full p-[24px] text-left group cursor-pointer" aria-expanded="{expanded_attr}">'
            f'<span class="text-[20px] font-medium text-[#222427] group-hover:text-[#3252F3] transition-colors pr-8 leading-[28px]" data-wp-key="faq.items[{index}].question" id="iw-editable-{question_base_id + index}">{item["question"]}</span>'
            f'<div class="faq-icon flex-shrink-0 text-[#3252F3] relative w-5 h-5"><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="plus-icon absolute inset-0 transition-opacity duration-300 opacity-{plus_opacity}"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg><svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="minus-icon absolute inset-0 transition-opacity duration-300 opacity-{minus_opacity}"><line x1="5" y1="12" x2="19" y2="12"></line></svg></div>'
            f'</button>'
            f'</h3>'
            f'<div class="{content_classes}" style="max-height: {max_height}"><div class="px-[24px] pb-[24px] text-[#222427] opacity-80 leading-[28px] text-[18px]" data-wp-key="faq.items[{index}].answer">{item["answer"]}</div></div>'
            f'</div>'
        )

    return f"""

    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="faq">
    <section class="mb-10 lg:mb-[160px] bg-white" id="faq-section" data-xd-module="faq"><div class="iw-container mx-auto px-4 max-w-[800px]"><div class="text-center md:mb-[80px] mb-[60px]"><h2 class="text-[32px] md:text-[50px] font-bold text-[#222427] mb-6 leading-[50px] md:leading-[70px]" data-wp-key="faq.title" id="iw-editable-46">{faq_data["title"]}</h2></div><div class="space-y-[24px]">{''.join(items_html)}</div></div></section>
    </div>
    <!-- /wp:html -->

    """


def build_html_from_data(data, keyword, input_2_html="", use_case_image=None, progress_cb=None):
    use_case_image = complete_use_case_image_map(use_case_image)
    _emit_progress(progress_cb, "rendering_page_html")
    main_1 = render_hero_section(data["main"])

    if input_2_html:
        input_2 = input_2_html
    else:
        input_2 = """

          <div class="iw-root" data-iw-root="true">
            <div class="iw-container mx-auto max-w-[1200px] mb-10 lg:mb-[160px]">
              <div class="max-w-[768px] mx-auto flex flex-col items-center">
                
            <div data-agent-root="true" data-layout="upload-input" class="flex w-full flex-col items-center">
              <div class="flex w-fit items-center bg-[#F2F6FF] rounded-[32px] p-[4px] mb-[32px] gap-[4px] ">
                
          <button type="button" data-tab="file" class="flex items-center gap-[6px] rounded-[28px] px-[20px] py-[10px] text-[14px] font-medium text-[#030A1A] transition-all bg-white shadow-[0_1px_3px_rgba(0,0,0,0.1)]">
            
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path fill-rule="evenodd" clip-rule="evenodd" d="M10.0091 1.33838H2.67578V13.3384C2.67578 14.0748 3.27273 14.6717 4.00911 14.6717H12.0091C12.7455 14.6717 13.3424 14.0748 13.3424 13.3384V4.67171L10.0091 1.33838Z" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
            <path d="M9.33984 1.33838V4.00505C9.33984 4.74143 9.9368 5.33838 10.6732 5.33838H13.3398" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
          </svg>
        
            File
          </button>
        
                
          <button type="button" data-tab="input" class="flex items-center gap-[6px] rounded-[28px] px-[20px] py-[10px] text-[14px] font-medium text-[#030A1A] transition-all text-[#6b7280]">
            
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M8.66797 14H14.0013" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
            <path d="M14.1147 4.54126C14.4671 4.18888 14.6652 3.71091 14.6652 3.2125C14.6653 2.71409 14.4674 2.23607 14.115 1.8836C13.7626 1.53112 13.2846 1.33307 12.7862 1.33301C12.2878 1.33295 11.8098 1.53088 11.4573 1.88326L2.55999 10.7826C2.4052 10.9369 2.29073 11.127 2.22665 11.3359L1.34599 14.2373C1.32876 14.2949 1.32746 14.3562 1.34222 14.4145C1.35699 14.4728 1.38727 14.5261 1.42985 14.5686C1.47244 14.6111 1.52573 14.6413 1.58409 14.656C1.64245 14.6707 1.70369 14.6693 1.76132 14.6519L4.66332 13.7719C4.8721 13.7084 5.0621 13.5947 5.21665 13.4406L14.1147 4.54126Z" stroke="black" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"></path>
          </svg>
        
            Input
          </button>
        
              </div>
              <div class="w-full relative">
                <div data-panel="file" class="block w-full">
          <div class="w-full">
            <div class="w-full rounded-[16px] border border-[#F3F4F6] bg-white p-[30px] shadow-[0_8px_20px_-6px_rgba(0,0,0,0.05)]">
              <div class="relative flex flex-col items-center justify-center py-[40px] md:py-[70px] rounded-[14px] border-2 border-solid border-[#E4E7EC] bg-[rgba(249,250,251,0.5997)]">
                <div data-role="agent-upload-count" class="absolute right-[16px] top-[16px] rounded-[6px] border border-[#E4E7EC] bg-white px-[12px] py-[4px] text-[13px] font-medium text-[#344054]">
                  0 / 20
                </div>
                <label data-role="agent-upload-hint" class="flex cursor-pointer flex-col items-center group">
                  <div class="relative mb-[20px]">
          <svg xmlns="http://www.w3.org/2000/svg" class="w-[28px] h-[37px] md:w-[54px] md:h-[72px]" viewBox="24 16 54 72" fill="none">
            <g filter="url(#filter0_dd_3393_3265)">
              <path fill-rule="evenodd" clip-rule="evenodd" d="M24 19C24 17.3431 25.3431 16 27 16H53.6587C54.5122 16 55.3253 16.3635 55.8944 16.9996L71.2357 34.1458C71.7279 34.6959 72 35.4081 72 36.1462V81C72 82.6569 70.6569 84 69 84H27C25.3431 84 24 82.6569 24 81V19Z" fill="white"></path>
            </g>
            <g filter="url(#filter1_d_3393_3265)">
              <path fill-rule="evenodd" clip-rule="evenodd" d="M53.6587 16C54.5122 16 55.3253 16.3635 55.8944 16.9996L71.2357 34.1458C71.7279 34.6959 72 35.4081 72 36.1462V37C72 35.8954 71.1046 35 70 35H57C55.8954 35 55 34.1046 55 33V18C55 16.8954 54.1046 16 53 16H53.6587Z" fill="white"></path>
            </g>
            <circle cx="60" cy="70" r="18" fill="#222427"></circle>
            <rect x="54" y="62" width="12" height="16" fill="#D8D8D8" fill-opacity="0.01"></rect>
            <rect x="59" y="63" width="2" height="14" rx="1" fill="white"></rect>
            <path fill-rule="evenodd" clip-rule="evenodd" d="M59.2929 63.0502C59.6834 62.6597 60.3166 62.6597 60.7071 63.0502C61.0976 63.4408 61.0976 64.0739 60.7071 64.4645L56.4645 68.7071C56.0739 69.0976 55.4408 69.0976 55.0503 68.7071C54.6597 68.3166 54.6597 67.6834 55.0503 67.2929L59.2929 63.0502Z" fill="white"></path>
            <path fill-rule="evenodd" clip-rule="evenodd" d="M64.9491 67.2929C65.3397 67.6834 65.3397 68.3166 64.9491 68.7071C64.5586 69.0976 63.9255 69.0976 63.5349 68.7071L59.2923 64.4644C58.9018 64.0739 58.9018 63.4407 59.2923 63.0502C59.6828 62.6597 60.316 62.6597 60.7065 63.0502L64.9491 67.2929Z" fill="white"></path>
            <defs>
              <filter id="filter0_dd_3393_3265" x="0" y="0" width="88" height="108" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
                <feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood>
                <feColorMatrix in="SourceAlpha" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0" result="hardAlpha"></feColorMatrix>
                <feOffset dx="-4" dy="4"></feOffset>
                <feGaussianBlur stdDeviation="10"></feGaussianBlur>
                <feColorMatrix type="matrix" values="0 0 0 0 0.06 0 0 0 0 0.14 0 0 0 0 0.3 0 0 0 0.101598 0"></feColorMatrix>
                <feBlend mode="normal" in2="BackgroundImageFix" result="effect1_dropShadow_3393_3265"></feBlend>
                <feColorMatrix in="SourceAlpha" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0" result="hardAlpha"></feColorMatrix>
                <feOffset></feOffset>
                <feGaussianBlur stdDeviation="0.5"></feGaussianBlur>
                <feColorMatrix type="matrix" values="0 0 0 0 0.0871066 0 0 0 0 0.255162 0 0 0 0 0.507246 0 0 0 0.234125 0"></feColorMatrix>
                <feBlend mode="normal" in2="effect1_dropShadow_3393_3265" result="effect2_dropShadow_3393_3265"></feBlend>
                <feBlend mode="normal" in="SourceGraphic" in2="effect2_dropShadow_3393_3265" result="shape"></feBlend>
              </filter>
              <filter id="filter1_d_3393_3265" x="46" y="12" width="31" height="33" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
                <feFlood flood-opacity="0" result="BackgroundImageFix"></feFlood>
                <feColorMatrix in="SourceAlpha" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0" result="hardAlpha"></feColorMatrix>
                <feOffset dx="-1" dy="2"></feOffset>
                <feGaussianBlur stdDeviation="3"></feGaussianBlur>
                <feColorMatrix type="matrix" values="0 0 0 0 0.06 0 0 0 0 0.14 0 0 0 0 0.3 0 0 0 0.106647 0"></feColorMatrix>
                <feBlend mode="normal" in2="BackgroundImageFix" result="effect1_dropShadow_3393_3265"></feBlend>
                <feBlend mode="normal" in="SourceGraphic" in2="effect1_dropShadow_3393_3265" result="shape"></feBlend>
              </filter>
            </defs>
          </svg>
        </div>
                  <h3 class="md:text-[20px] text-[14px] font-bold text-[#22272b] mb-[4px]">Upload Files</h3>
                  <input data-role="agent-upload-input" class="hidden" type="file" multiple accept=".doc,.docx,.pdf,.ppt,.pptx,.txt,.md,.csv,.xls,.xlsx,.png,.jpg,.jpeg,.webp,.gif,.mp3,.wav,.m4a,.mp4,.mov" />
                </label>
                <div data-role="agent-uploading-hint" class="hidden flex-col items-center justify-center w-full z-20 shrink-0">
                  <div class="mb-[24px] h-[72px] w-[72px] animate-spin rounded-full border-[6px] border-[#dbe6ff] border-t-[#0055FF]"></div>
                  <h3 class="mb-[8px] text-[20px] font-bold text-[#22272b]">Uploading...</h3>
                  <p class="text-[14px] text-[#9ca3af]">Please wait while we process your files</p>
                </div>
              </div>
            </div>
          </div>
        </div>
                <div data-panel="input" class="hidden w-full">
          <div class="w-full">
            <div class="relative w-full bg-white rounded-[16px] shadow-[0_8px_20px_-6px_rgba(0,0,0,0.05)] border border-[#F3F4F6] pt-4 pb-11 px-5 md:h-[334px] h-[206px] md:mb-[32px] mb-[40px]">
              <textarea data-role="agent-textarea" class="w-full h-full resize-none outline-none border-none text-[16px] text-[#374151] placeholder:text-[#9CA3AF] bg-transparent" placeholder="Write the text you want to get high-quality paragraphs." maxlength="5000"></textarea>
              <div class="absolute bottom-3 right-5 text-[14px] text-[#9CA3AF]"><span data-role="agent-textarea-counter">0</span>/5000</div>
            </div>
            <div class="flex justify-center">
              
          <button data-role="agent-textarea-submit" class="flex h-[56px] min-w-[240px] cursor-pointer items-center justify-center gap-[8px] rounded-[14px] bg-[#0055FF] px-8 py-4 text-[16px] font-medium text-white shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1)] transition-colors hover:bg-blue-700 " type="button">
            <span class="flex items-center gap-[8px]">
              
          <svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M11.017 2.814a1 1 0 0 1 1.966 0l1.051 5.558a2 2 0 0 0 1.594 1.594l5.558 1.051a1 1 0 0 1 0 1.966l-5.558 1.051a2 2 0 0 0-1.594 1.594l-1.051 5.558a1 1 0 0 1-1.966 0l-1.051-5.558a2 2 0 0 0-1.594-1.594l-5.558-1.051a1 1 0 0 1 0-1.966l5.558-1.051a2 2 0 0 0 1.594-1.594z"></path>
            <path d="M20 2v4"></path>
            <path d="M22 4h-4"></path>
            <circle cx="4" cy="20" r="2"></circle>
          </svg>
        
              Summarize
            </span>
          </button>
        
            </div>
          </div>
        </div>
              </div>
            </div>
          
              </div>
            </div>
          </div>
        
    """

    how_to_3 = f"""

    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="howto">
    <section class="mb-10 lg:mb-[160px]" data-xd-module="howto"><div class="iw-container mx-auto px-4 max-w-[1200px]"><div class="text-center md:mb-[70px] mb-[60px]"><h2 class="text-[32px] md:text-[50px] font-bold text-[#222427] mb-4 leading-[50px] md:leading-[70px]" data-wp-key="howTo.title" id="iw-editable-15">{data["how_to"]["title"]}</h2></div><div class="grid grid-cols-1 md:grid-cols-3 gap-[24px]"><div class="h-full bg-white rounded-[24px] p-0 overflow-hidden shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)] md:shadow-[0_1px_2px_rgba(0,0,0,0.1),0_1px_3px_rgba(0,0,0,0.1)] md:border md:border-[#F3F4F6] flex flex-col items-center pt-[48px] px-8 pb-[48px]"><div class="text-[56px] font-bold text-[#05f] leading-[66px] mb-[16px]" data-wp-key="howTo.steps[0].number">01</div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="howTo.steps[0].title" id="iw-editable-16">{data["how_to"]["steps"][0]["title"]}</h3><p class="w-full text-center text-[18px] text-[#222427] opacity-80 leading-[30px]" data-wp-key="howTo.steps[0].description" id="iw-editable-17">{data["how_to"]["steps"][0]["description"]}</p></div><div class="h-full bg-white rounded-[24px] p-0 overflow-hidden shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)] md:shadow-[0_1px_2px_rgba(0,0,0,0.1),0_1px_3px_rgba(0,0,0,0.1)] md:border md:border-[#F3F4F6] flex flex-col items-center pt-[48px] px-8 pb-[48px]"><div class="text-[56px] font-bold text-[#05f] leading-[66px] mb-[16px]" data-wp-key="howTo.steps[1].number">02</div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="howTo.steps[1].title" id="iw-editable-18">{data["how_to"]["steps"][1]["title"]}</h3><p class="w-full text-center text-[18px] text-[#222427] opacity-80 leading-[30px]" data-wp-key="howTo.steps[1].description" id="iw-editable-19">{data["how_to"]["steps"][1]["description"]}</p></div><div class="h-full bg-white rounded-[24px] p-0 overflow-hidden shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)] md:shadow-[0_1px_2px_rgba(0,0,0,0.1),0_1px_3px_rgba(0,0,0,0.1)] md:border md:border-[#F3F4F6] flex flex-col items-center pt-[48px] px-8 pb-[48px]"><div class="text-[56px] font-bold text-[#05f] leading-[66px] mb-[16px]" data-wp-key="howTo.steps[2].number">03</div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="howTo.steps[2].title" id="iw-editable-20">{data["how_to"]["steps"][2]["title"]}</h3><p class="w-full text-center text-[18px] text-[#222427] opacity-80 leading-[30px]" data-wp-key="howTo.steps[2].description" id="iw-editable-21">{data["how_to"]["steps"][2]["description"]}</p></div></div></div></section>
    </div>
    <!-- /wp:html -->

    """

    why_choose_4 = f"""

    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="features">
    <section class="mb-10 lg:mb-[160px] bg-white relative" data-xd-module="features"><div class="iw-container mx-auto max-w-[1200px] px-4"><div class="text-center md:mb-[70px] mb-[60px] max-w-[1200px] mx-auto"><h2 class="text-[32px] md:text-[50px] font-bold text-[#222427] mb-6 leading-[50px] md:leading-[70px]" data-wp-key="features.title" id="iw-editable-22">{data["why_choose"]["title"]}</h2></div><div class="max-w-[1200px] mx-auto flex flex-col gap-[30px]"><div class="flex flex-col md:flex-row gap-[30px] items-stretch"><div class="md:flex-1 bg-[#F9FAFB] rounded-[24px] overflow-hidden transition-all duration-300 flex flex-col items-center pt-[40px] pb-[40px]"><div class="w-[72px] h-[72px] bg-white rounded-[18px] shadow-[0px_1.125px_3.375px_0px_rgba(0,0,0,0.1),0px_1.125px_2.25px_0px_rgba(0,0,0,0.1)] flex items-center justify-center text-[#155DFC] mb-[24px]"><svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-smartphone"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"></rect><path d="M12 18h.01"></path></svg></div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="features.items[0].title" id="iw-editable-23">{data["why_choose"]["factors"][0]["title"]}</h3><p class="w-full px-6 text-center text-[#222427] text-[18px] leading-[30px]" data-wp-key="features.items[0].description" id="iw-editable-24">{data["why_choose"]["factors"][0]["description"]}</p></div><div class="md:flex-1 bg-[#F9FAFB] rounded-[24px] overflow-hidden transition-all duration-300 flex flex-col items-center pt-[40px] pb-[40px]"><div class="w-[72px] h-[72px] bg-white rounded-[18px] shadow-[0px_1.125px_3.375px_0px_rgba(0,0,0,0.1),0px_1.125px_2.25px_0px_rgba(0,0,0,0.1)] flex items-center justify-center text-[#155DFC] mb-[24px]"><svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-monitor"><rect width="20" height="14" x="2" y="3" rx="2"></rect><line x1="8" x2="16" y1="21" y2="21"></line><line x1="12" x2="12" y1="17" y2="21"></line></svg></div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="features.items[1].title" id="iw-editable-25">{data["why_choose"]["factors"][1]["title"]}</h3><p class="w-full px-6 text-center text-[#222427] text-[18px] leading-[30px]" data-wp-key="features.items[1].description" id="iw-editable-26">{data["why_choose"]["factors"][1]["description"]}</p></div><div class="md:flex-1 bg-[#F9FAFB] rounded-[24px] overflow-hidden transition-all duration-300 flex flex-col items-center pt-[40px] pb-[40px]"><div class="w-[72px] h-[72px] bg-white rounded-[18px] shadow-[0px_1.125px_3.375px_0px_rgba(0,0,0,0.1),0px_1.125px_2.25px_0px_rgba(0,0,0,0.1)] flex items-center justify-center text-[#155DFC] mb-[24px]"><svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-users"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"></path><circle cx="9" cy="7" r="4"></circle><path d="M22 21v-2a4 4 0 0 0-3-3.87"></path><path d="M16 3.13a4 4 0 0 1 0 7.75"></path></svg></div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="features.items[2].title" id="iw-editable-27">{data["why_choose"]["factors"][2]["title"]}</h3><p class="w-full px-6 text-center text-[#222427] text-[18px] leading-[30px]" data-wp-key="features.items[2].description" id="iw-editable-28">{data["why_choose"]["factors"][2]["description"]}</p></div></div><div class="flex flex-col md:flex-row gap-[30px] md:max-w-[790px] md:mx-auto w-full items-stretch"><div class="md:flex-1 bg-[#F9FAFB] rounded-[24px] overflow-hidden transition-all duration-300 flex flex-col items-center pt-[40px] pb-[40px]"><div class="w-[72px] h-[72px] bg-white rounded-[18px] shadow-[0px_1.125px_3.375px_0px_rgba(0,0,0,0.1),0px_1.125px_2.25px_0px_rgba(0,0,0,0.1)] flex items-center justify-center text-[#155DFC] mb-[24px]"><svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-puzzle-icon lucide-puzzle"><path d="M15.39 4.39a1 1 0 0 0 1.68-.474 2.5 2.5 0 1 1 3.014 3.015 1 1 0 0 0-.474 1.68l1.683 1.682a2.414 2.414 0 0 1 0 3.414L19.61 15.39a1 1 0 0 1-1.68-.474 2.5 2.5 0 1 0-3.014 3.015 1 1 0 0 1 .474 1.68l-1.683 1.682a2.414 2.414 0 0 1-3.414 0L8.61 19.61a1 1 0 0 0-1.68.474 2.5 2.5 0 1 1-3.014-3.015 1 1 0 0 0 .474-1.68l-1.683-1.682a2.414 2.414 0 0 1 0-3.414L4.39 8.61a1 1 0 0 1 1.68.474 2.5 2.5 0 1 0 3.014-3.015 1 1 0 0 1-.474-1.68l1.683-1.682a2.414 2.414 0 0 1 3.414 0z"></path></svg></div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="features.items[3].title" id="iw-editable-29">{data["why_choose"]["factors"][3]["title"]}</h3><p class="w-full px-6 text-center text-[#222427] text-[18px] leading-[30px]" data-wp-key="features.items[3].description" id="iw-editable-30">{data["why_choose"]["factors"][3]["description"]}</p></div><div class="md:flex-1 bg-[#F9FAFB] rounded-[24px] overflow-hidden transition-all duration-300 flex flex-col items-center pt-[40px] pb-[40px]"><div class="w-[72px] h-[72px] bg-white rounded-[18px] shadow-[0px_1.125px_3.375px_0px_rgba(0,0,0,0.1),0px_1.125px_2.25px_0px_rgba(0,0,0,0.1)] flex items-center justify-center text-[#155DFC] mb-[24px]"><svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="lucide lucide-file-text"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"></path><path d="M14 2v4a2 2 0 0 0 2 2h4"></path><path d="M10 9H8"></path><path d="M16 13H8"></path><path d="M16 17H8"></path></svg></div><h3 class="w-full text-center text-[28px] font-bold text-[#222427] leading-[38px] mb-[8px]" data-wp-key="features.items[4].title" id="iw-editable-31">{data["why_choose"]["factors"][4]["title"]}</h3><p class="w-full px-6 text-center text-[#222427] text-[18px] leading-[30px]" data-wp-key="features.items[4].description" id="iw-editable-32">{data["why_choose"]["factors"][4]["description"]}</p></div></div></div></div></section>
    </div>
    <!-- /wp:html -->

    """

    Key_Features_4_5 = f"""


"""

    use_cases_5 = f"""

    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="use-cases">
    <section class="mb-10 lg:mb-[160px] bg-white" data-xd-module="use-cases"><div class="iw-container mx-auto max-w-[1200px] px-4"><div class="text-center md:mb-[80px] mb-[60px]"><h2 class="text-[32px] md:text-[50px] font-bold text-[#101828] mb-6 leading-[50px]
    md:leading-[70px]" data-wp-key="useCases.title" id="iw-editable-33">{data["use_cases"]["title"]}</h2></div><div class="flex flex-col md:gap-[96px] gap-[80px] max-w-[1200px] mx-auto"><div class="flex flex-col items-center gap-[40px]
    lg:flex-row"><div class="flex-1 flex flex-col gap-[24px] order-2 lg:order-none"><h3 class="text-[30px] font-bold text-[#101828]
    tracking-[0.3955px] leading-[36px]" data-wp-key="useCases.items[0].title" id="iw-editable-34">{data["use_cases"]["cases"][0]["title"]}</h3><p class="text-[18px] text-[#4a5565] leading-[29.25px] tracking-[-0.4395px]" data-wp-key="useCases.items[0].description" id="iw-editable-35">{data["use_cases"]["cases"][0]["description"]}</p><a href="https://www.iweaver.ai/app/chat/0" class="rounded-full px-[32px] w-fit h-[44px]
    min-w-[163px] bg-[#155DFC] hover:bg-blue-700 shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1),0px_1px_2px_0px_rgba(0,0,0,0.1)] text-[14px]
    font-medium tracking-[-0.1504px] flex items-center justify-center gap-2 text-white cursor-pointer transition-colors" id="iw-editable-36">{data["use_cases"]["cases"][0]["button_text"]}<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4"><path d="M5 12h14"></path><path d="m12 5 7 7-7 7"></path></svg></a></div><div class="flex-1 w-full order-1 lg:order-none"><div class="w-full max-w-[700px] aspect-[4/3]
    lg:aspect-auto lg:h-[528px] rounded-[24px] overflow-hidden shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)]
    bg-gray-100 relative mx-auto"><img src="{use_case_image["image_url_1"]}" alt="Students and researchers" class="absolute inset-0 w-full h-full object-cover" loading="lazy" data-wp-key="useCases.items[0].image" id="iw-editable-37"></div></div></div><div class="flex flex-col items-center gap-[40px] lg:flex-row-reverse"><div class="flex-1 flex
    flex-col gap-[24px] order-2 lg:order-none"><h3 class="text-[30px] font-bold text-[#101828] tracking-[0.3955px] leading-[36px]" data-wp-key="useCases.items[1].title" id="iw-editable-38">{data["use_cases"]["cases"][1]["title"]}</h3><p class="text-[18px] text-[#4a5565] leading-[29.25px]
    tracking-[-0.4395px]" data-wp-key="useCases.items[1].description" id="iw-editable-39">{data["use_cases"]["cases"][1]["description"]}</p><a href="https://www.iweaver.ai/app/chat/0" class="rounded-full px-[32px] w-fit h-[44px] min-w-[163px] bg-[#155DFC] hover:bg-blue-700
    shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1),0px_1px_2px_0px_rgba(0,0,0,0.1)] text-[14px] font-medium tracking-[-0.1504px] flex items-center
    justify-center gap-2 text-white cursor-pointer transition-colors" id="iw-editable-40">{data["use_cases"]["cases"][1]["button_text"]}<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4"><path d="M5 12h14"></path><path d="m12 5 7 7-7 7"></path></svg></a></div><div class="flex-1
    w-full order-1 lg:order-none"><div class="w-full max-w-[700px] aspect-[4/3] lg:aspect-auto lg:h-[528px] rounded-[24px] overflow-hidden
    shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)] bg-gray-100 relative mx-auto"><img src="{use_case_image["image_url_2"]}" alt="Content Creators" class="absolute inset-0
    w-full h-full object-cover" loading="lazy" data-wp-key="useCases.items[1].image" id="iw-editable-41"></div></div></div><div class="flex
    flex-col items-center gap-[40px] lg:flex-row"><div class="flex-1 flex flex-col gap-[24px] order-2 lg:order-none"><h3 class="text-[30px]
    font-bold text-[#101828] tracking-[0.3955px] leading-[36px]" data-wp-key="useCases.items[2].title" id="iw-editable-42">{data["use_cases"]["cases"][2]["title"]}</h3><p class="text-[18px] text-[#4a5565] leading-[29.25px] tracking-[-0.4395px]" data-wp-key="useCases.items[2].description" id="iw-editable-43">{data["use_cases"]["cases"][2]["description"]}</p><a href="https://www.iweaver.ai/app/chat/0" class="rounded-full px-[32px] w-fit
    h-[44px] min-w-[163px] bg-[#155DFC] hover:bg-blue-700 shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1),0px_1px_2px_0px_rgba(0,0,0,0.1)]
    text-[14px] font-medium tracking-[-0.1504px] flex items-center justify-center gap-2 text-white cursor-pointer transition-colors" id="iw-editable-44">{data["use_cases"]["cases"][2]["button_text"]}<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4"><path d="M5 12h14"></path><path d="m12 5 7 7-7 7"></path></svg></a></div><div class="flex-1 w-full order-1 lg:order-none"><div class="w-full max-w-[700px] aspect-[4/3]
    lg:aspect-auto lg:h-[528px] rounded-[24px] overflow-hidden shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)]
    bg-gray-100 relative mx-auto"><img src="{use_case_image["image_url_3"]}" alt="Professionals" class="absolute inset-0 w-full h-full object-cover" loading="lazy" data-wp-key="useCases.items[2].image" id="iw-editable-45"></div></div></div><div class="flex flex-col items-center gap-[40px] lg:flex-row-reverse"><div class="flex-1 flex
    flex-col gap-[24px] order-2 lg:order-none"><h3 class="text-[30px] font-bold text-[#101828] tracking-[0.3955px] leading-[36px]" data-wp-key="useCases.items[3].title" id="iw-editable-46">{data["use_cases"]["cases"][3]["title"]}</h3><p class="text-[18px] text-[#4a5565]
    leading-[29.25px] tracking-[-0.4395px]" data-wp-key="useCases.items[3].description" id="iw-editable-47">{data["use_cases"]["cases"][3]["description"]}</p><a href="https://www.iweaver.ai/app/chat/0" class="rounded-full px-[32px] w-fit h-[44px] min-w-[163px] bg-[#155DFC] hover:bg-blue-700
    shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1),0px_1px_2px_0px_rgba(0,0,0,0.1)] text-[14px] font-medium tracking-[-0.1504px] flex items-center
    justify-center gap-2 text-white cursor-pointer transition-colors" id="iw-editable-48">{data["use_cases"]["cases"][3]["button_text"]}<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="w-4 h-4"><path d="M5 12h14"></path><path d="m12 5 7 7-7 7"></path></svg></a></div><div class="flex-1
    w-full order-1 lg:order-none"><div class="w-full max-w-[700px] aspect-[4/3] lg:aspect-auto lg:h-[528px] rounded-[24px] overflow-hidden
    shadow-[0px_20px_25px_-5px_rgba(0,0,0,0.1),0px_8px_10px_-6px_rgba(0,0,0,0.1)] bg-gray-100 relative mx-auto"><img src="{use_case_image["image_url_4"]}" alt="Journalists and Media Workers" class="absolute inset-0 w-full h-full object-cover" loading="lazy" data-wp-key="useCases.items[3].image" id="iw-editable-49"></div></div></div></div></div></section>
    </div>
    
    <!-- /wp:html -->

    """

    faq_6 = render_faq_section(data["faq"])

    cta_7 = f"""

    <!-- wp:html -->
    <div class="iw-root" data-iw-root="true" data-block-key="cta">
    <section class="mb-10 lg:mb-[160px] px-4" data-xd-module="cta"><div class="iw-container mx-auto max-w-[1152px]"><div class="relative px-[24px] rounded-[48px] overflow-hidden bg-gradient-to-b from-[#dfe9fc] via-[#f1f5fe] to-[#dfe9fc] h-[488px] flex flex-col items-center justify-center text-center shadow-[0px_1px_3px_0px_rgba(0,0,0,0.1),0px_1px_2px_-1px_rgba(0,0,0,0.1)]"><div class="absolute top-[61px] left-[144px] w-[864px] h-[366px] bg-[rgba(194,122,255,0.1)] blur-[120px] rounded-[33554400px] pointer-events-none"></div><div class="relative z-10 w-full mx-auto flex flex-col items-center"><h2 class="text-[30px] md:text-[48px] font-bold text-[#1a1a1a] mb-[24px] leading-[38px] md:leading-[56px]" id="iw-editable-53"><span data-wp-key="cta.titleLine1" id="iw-editable-54" class="">{data["cta"]["title_H1"]}</span><br><span data-wp-key="cta.titleLine2" id="iw-editable-55" class="">{data["cta"]["title_H2"]}</span></h2><p class="md:text-[18px] text-[16px] text-[#4a5565] mb-[40px] max-w-[592px] mx-auto font-medium leading-[28px]" data-wp-key="cta.description" id="iw-editable-56">{data["cta"]["description"]}</p><a href="https://www.iweaver.ai/app/chat/0" class="inline-flex items-center justify-center rounded-full px-[40px] min-w-[246px] w-fit h-[56px] text-[18px] font-medium bg-[#6841ea] hover:bg-[#5b36d0] text-white shadow-[0px_10px_15px_0px_rgba(0,0,0,0.1),0px_4px_6px_0px_rgba(0,0,0,0.1)] transition-all border-none cursor-pointer no-underline" data-wp-key="cta.buttonText" id="iw-editable-57">{data["cta"]["button_text"]}</a></div></div></div></section>
    </div>
    <!-- /wp:html -->

    """

    complete_html = main_1 + input_2 + how_to_3 + why_choose_4 + use_cases_5 + faq_6 + cta_7
    seo_payload = build_seo_payload(data, keyword)

    return {
        "html": complete_html,
        "seo": seo_payload,
        "content": data,
        "image_urls": use_case_image,
    }


def html_text(keyword, input_2_html, use_case_image_list, page_slug="", progress_cb=None, generate_images=True):

    result = generate_seo_content(keyword, progress_cb=progress_cb)
    if not result.get("success"):
        raise ValueError(result.get("error", "Failed to generate SEO content"))

    _emit_progress(progress_cb, "parsing_page_json")
    content = result["content"]
    data_str = clean_output_content(content)
    data = sanitize_generated_content(json.loads(data_str))

    use_case_image = normalize_use_case_image_map(use_case_image_list)
    if generate_images:
        _emit_progress(progress_cb, "use_case_images")
        use_case_image = ensure_use_case_image_urls(
            data,
            keyword,
            page_slug=page_slug,
            provided_images=use_case_image_list,
            progress_cb=progress_cb,
        )
    else:
        _emit_progress(progress_cb, "use_case_images_queued_async")

    return build_html_from_data(
        data,
        keyword=keyword,
        input_2_html=input_2_html,
        use_case_image=use_case_image,
        progress_cb=progress_cb,
    )


def post_to_wp(
    complete_html,
    title="WP HTML 多 Block 测试6",
    slug="wp-html-test-6",
    status="draft",
    wp_tag_ids=None,
    agent_page_img="",
    agent_page_desc="",
    seo_data=None,
):
    def wp_request(method, endpoint, **kwargs):
        request_headers = dict(headers)
        extra_headers = kwargs.pop("headers", None)
        if extra_headers:
            request_headers.update(extra_headers)

        timeout = kwargs.pop("timeout", (WP_CONNECT_TIMEOUT, WP_READ_TIMEOUT))
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error = None
        last_response = None

        for attempt in range(WP_MAX_RETRIES):
            try:
                response = requests.request(
                    method,
                    endpoint,
                    headers=request_headers,
                    timeout=timeout,
                    **kwargs,
                )
                last_response = response
                if response.status_code not in retryable_statuses:
                    return response
            except requests.exceptions.RequestException as exc:
                last_error = exc

            if attempt < WP_MAX_RETRIES - 1:
                time.sleep(WP_RETRY_BACKOFF_SECONDS[min(attempt, len(WP_RETRY_BACKOFF_SECONDS) - 1)])

        if last_response is not None:
            return last_response
        raise last_error

    def load_media_id_cache():
        if not os.path.exists(MEDIA_ID_CACHE_PATH):
            return {}
        try:
            with open(MEDIA_ID_CACHE_PATH, "r", encoding="utf-8") as cache_file:
                cache = json.load(cache_file)
            if isinstance(cache, dict):
                return cache
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_media_id_cache(cache):
        try:
            with open(MEDIA_ID_CACHE_PATH, "w", encoding="utf-8") as cache_file:
                json.dump(cache, cache_file, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def resolve_media_id(media_value):
        if isinstance(media_value, str):
            media_value = media_value.strip()
        if not media_value:
            return ""
        if str(media_value).isdigit():
            return int(media_value)
        if not isinstance(media_value, str) or not media_value.lower().startswith(("http://", "https://")):
            return media_value

        normalized_url = media_value.split("?", 1)[0]
        media_cache = load_media_id_cache()
        cached_item = media_cache.get(normalized_url)
        if isinstance(cached_item, dict) and cached_item.get("id"):
            return cached_item["id"]

        media_endpoint = f"{url.rsplit('/', 1)[0]}/media"
        file_name = urlparse(normalized_url).path.rsplit("/", 1)[-1]
        response = wp_request(
            "GET",
            media_endpoint,
            params={
                "search": file_name,
                "media_type": "image",
                "per_page": 20,
                "_fields": "id,source_url",
            },
        )
        response.raise_for_status()

        payload = response.json()
        if isinstance(payload, list):
            media_items = payload
        elif isinstance(payload, dict):
            if payload.get("id") and payload.get("source_url"):
                media_items = [payload]
            elif payload.get("code") or payload.get("message"):
                raise ValueError(f"Media lookup failed: {payload}")
            else:
                raise ValueError(f"Unexpected media lookup response: {payload}")
        else:
            raise ValueError(f"Unexpected media lookup payload type: {type(payload).__name__}")

        for item in media_items:
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("source_url", "")).split("?", 1)[0]
            if source_url == normalized_url:
                media_id = item.get("id")
                media_cache[normalized_url] = {
                    "id": media_id,
                    "source_url": source_url,
                    "updated_at": int(time.time()),
                }
                save_media_id_cache(media_cache)
                return media_id

        raise ValueError(f"Unable to resolve media ID from URL: {media_value}")

    def get_registered_acf_fields(endpoint):
        try:
            response = wp_request("OPTIONS", endpoint)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            return None

        payload = response.json()
        acf_schema = payload.get("schema", {}).get("properties", {}).get("acf", {})
        properties = acf_schema.get("properties", {})
        if isinstance(properties, dict):
            return properties
        return {}

    def normalize_acf_response(acf_value):
        if isinstance(acf_value, dict):
            return acf_value
        return {}

    def field_has_expected_value(field_name, expected_value, saved_acf):
        if field_name not in saved_acf:
            return False

        actual_value = saved_acf.get(field_name)
        if field_name == "agent_page_desc":
            return isinstance(actual_value, str) and actual_value.strip() == str(expected_value).strip()

        if field_name == "agent_page_img":
            if actual_value in ("", None, False, [], {}):
                return False
            if isinstance(actual_value, int):
                return actual_value == expected_value
            if isinstance(actual_value, str) and str(expected_value).isdigit():
                return actual_value.strip() == str(expected_value)
            if isinstance(actual_value, dict):
                actual_id = actual_value.get("id") or actual_value.get("ID")
                if actual_id:
                    return int(actual_id) == int(expected_value)
                return bool(actual_value)
            return bool(actual_value)

        return saved_acf.get(field_name) == expected_value

    acf_data = {}
    resolved_agent_page_img = resolve_media_id(agent_page_img)
    if resolved_agent_page_img:
        acf_data["agent_page_img"] = resolved_agent_page_img
    if isinstance(agent_page_desc, str) and agent_page_desc.strip():
        acf_data["agent_page_desc"] = agent_page_desc.strip()
    if not isinstance(wp_tag_ids, list) or not wp_tag_ids:
        wp_tag_ids = [139]
    else:
        wp_tag_ids = [int(wp_tag_ids[0])]

    data = {
        "title": title,
        "slug": slug,
        "template": "elementor_header_footer",
        "parent": 5256,
        "status": status,
        "categories": [3, 8],
        "tags": wp_tag_ids,
        "content": complete_html,
        "meta": {
            "project_source": "wordpress-website-astro"
        }
    }

    if isinstance(seo_data, dict):
        seo_title = sanitize_copy_text(seo_data.get("title", ""))
        seo_description = sanitize_copy_text(seo_data.get("description", ""))
        seo_keywords = seo_data.get("focus_keyword", "")
        seo_keywords = ", ".join(parse_keyword_candidates(seo_keywords))

        if seo_title:
            data["meta"]["rank_math_title"] = seo_title
        if seo_description:
            data["meta"]["rank_math_description"] = seo_description
        if seo_keywords:
            data["meta"]["rank_math_focus_keyword"] = seo_keywords

    if acf_data:
        data["acf"] = acf_data

    try:
        create_payload = dict(data)
        create_payload.pop("acf", None)
        response = wp_request("POST", url, json=create_payload)

        logger.info(f"状态码: {response.status_code}")

        if response.headers.get('content-type', '').startswith('application/json'):
            json_response = response.json()

            if response.status_code not in (200, 201):
                return {
                    'success': False,
                    'status_code': response.status_code,
                    'response': json_response,
                    'error': json_response,
                }

            if acf_data:
                page_id = json_response.get("id")
                if not page_id:
                    return {
                        'success': False,
                        'status_code': response.status_code,
                        'response': json_response,
                        'error': 'WordPress page created but response did not include page id for ACF update.',
                    }

                page_endpoint = f"{url}/{page_id}"
                acf_response = wp_request(
                    "POST",
                    f"{page_endpoint}?context=edit&_fields=id,slug,acf",
                    json={"acf": acf_data},
                )

                if acf_response.headers.get('content-type', '').startswith('application/json'):
                    acf_json = acf_response.json()
                else:
                    acf_json = acf_response.text

                if acf_response.status_code not in (200, 201):
                    return {
                        'success': False,
                        'status_code': acf_response.status_code,
                        'response': {
                            'page_create': json_response,
                            'acf_update': acf_json,
                        },
                        'error': 'Page created but ACF update failed.',
                    }

                update_acf = normalize_acf_response(acf_json.get("acf") if isinstance(acf_json, dict) else None)
                invalid_saved_fields = [
                    field_name for field_name, expected_value in acf_data.items()
                    if not field_has_expected_value(field_name, expected_value, update_acf)
                ]

                verify_json = None
                if invalid_saved_fields:
                    verify_response = wp_request(
                        "GET",
                        f"{page_endpoint}?context=edit&_fields=id,slug,acf",
                    )
                    verify_response.raise_for_status()
                    verify_json = verify_response.json()
                    saved_acf = normalize_acf_response(verify_json.get("acf"))
                    invalid_saved_fields = [
                        field_name for field_name, expected_value in acf_data.items()
                        if not field_has_expected_value(field_name, expected_value, saved_acf)
                    ]

                if invalid_saved_fields:
                    registered_acf_fields = get_registered_acf_fields(page_endpoint)
                    persistence_error_detail = (
                        "The page REST schema is currently missing the expected ACF field definitions."
                        if registered_acf_fields is not None
                        else "WordPress kept returning incomplete ACF data, so please verify the field group settings."
                    )
                    return {
                        'success': False,
                        'status_code': acf_response.status_code,
                        'response': {
                            'page_create': json_response,
                            'acf_update': acf_json,
                            'acf_verify': verify_json,
                            'acf_schema': list(registered_acf_fields.keys()) if registered_acf_fields is not None else None,
                        },
                        'error': (
                            'Page created, but WordPress did not persist these ACF fields with real values: '
                            f'{", ".join(invalid_saved_fields)}. '
                            f'{persistence_error_detail}'
                        ),
                    }

                return {
                    'success': True,
                    'status_code': response.status_code,
                    'response': {
                        'page_create': json_response,
                        'acf_update': acf_json,
                        'acf_verify': verify_json,
                    },
                    'seo': data["meta"],
                }

            return {
                'success': True,
                'status_code': response.status_code,
                'response': json_response,
                'seo': data["meta"],
            }

        return {
            'success': response.status_code in (200, 201),
            'status_code': response.status_code,
            'response': response.text,
            'seo': data["meta"],
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"请求发生错误: {e}")
        return {'success': False, 'error': str(e)}

if __name__ == '__main__':
    keyword = "book summarise,document ,AI Book Summarizer,AI Reading, multiple files"
    input_html = ""
    image_url_list = ""

    generated = html_text(keyword,input_html,image_url_list)
    post_to_wp(generated["html"], seo_data=generated.get("seo"))
