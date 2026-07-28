import json
import re
import os
import time
import logging
import requests
import urllib.parse

logger = logging.getLogger('blog_generat')

from . import html_generat as html_runtime
from .html_generat import (
    _run_coze_workflow,
    _parse_json_string_if_possible,
    clean_output_content,
    wp_request,
    upload_media_to_wp,
    convert_image_source_to_webp_bytes,
    slugify_english,
    WP_API_BASE_URL,
    _local_search_serpapi,
    _local_scrape_all,
    _local_merge_pages,
    WORKFLOW_PROVIDER,
    sanitize_iweaver_urls,
)
from .coze_llm import call_coze_llm
from .prompts import BLOG_GENERATE_PROMPT, HOT_TOPIC_KEYWORD_PROMPT, HOT_TOPIC_BLOG_PROMPT, format_prompt

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




BLOG_WORKFLOW_ID = "7630731281847812096"
HOT_TOPIC_WORKFLOW_ID = "7631095025312464896"

# --- Local provider config ---
NEW_COZE_WORKFLOW_ID = os.getenv("NEW_COZE_WORKFLOW_ID", "").strip()
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "").strip()
COVER_IMAGE_BASE_URL = os.getenv("COVER_IMAGE_BASE_URL", "").strip()
COVER_IMAGE_API_KEY = os.getenv("COVER_IMAGE_API_KEY", "").strip()
COVER_IMAGE_URL = os.getenv(
    "COVER_IMAGE_URL",
    f"{COVER_IMAGE_BASE_URL.rstrip('/')}/v1/responses" if COVER_IMAGE_BASE_URL else html_runtime.USE_CASE_IMAGE_URL,
).strip()
COVER_IMAGE_RESPONSES_MODEL = os.getenv(
    "COVER_IMAGE_RESPONSES_MODEL",
    html_runtime.USE_CASE_IMAGE_RESPONSES_MODEL,
).strip()
COVER_IMAGE_MODEL = os.getenv("COVER_IMAGE_MODEL", html_runtime.USE_CASE_IMAGE_MODEL).strip()
COVER_IMAGE_SIZE = os.getenv("COVER_IMAGE_SIZE", "1024x1024").strip()
COVER_IMAGE_QUALITY = os.getenv("COVER_IMAGE_QUALITY", "auto").strip()
COVER_IMAGE_OUTPUT_FORMAT = os.getenv("COVER_IMAGE_OUTPUT_FORMAT", "png").strip()
COVER_IMAGE_TIMEOUT = int(os.getenv("COVER_IMAGE_TIMEOUT", "300"))

# --- Article index for internal linking ---
from .paths import ASSETS_DIR, STORAGE_DIR

_ARTICLE_INDEX_PATH = ASSETS_DIR / "article_index.json"
_ARTICLE_INDEX = []

def _load_article_index():
    global _ARTICLE_INDEX
    if _ARTICLE_INDEX:
        return
    try:
        with open(_ARTICLE_INDEX_PATH, "r", encoding="utf-8") as f:
            _ARTICLE_INDEX = json.load(f)
    except Exception:
        _ARTICLE_INDEX = []


def _find_relevant_articles(keyword, limit=10):
    """Find articles relevant to the keyword for internal linking."""
    _load_article_index()
    if not _ARTICLE_INDEX:
        return ""

    kw = keyword.lower()
    words = set(re.findall(r'[a-z]+', kw))

    scored = []
    for art in _ARTICLE_INDEX:
        title_lower = art["title"].lower()
        score = 0
        for w in words:
            if len(w) >= 3 and w in title_lower:
                score += 1
        if score > 0:
            scored.append((score, art))

    scored.sort(key=lambda x: -x[0])
    lines = []
    for _, art in scored[:limit]:
        lines.append(f'- {art["url"]} — {art["title"]}')
    return "\n".join(lines)


def _local_fetch_reddit_hot(niche, limit=10):
    """Fetch hot Reddit posts for a niche."""
    import urllib.request
    url = f"https://www.reddit.com/search.json?q={urllib.parse.quote(niche)}&sort=hot&limit=25&t=week"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; iweaver-bot/1.0)"}
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        logger.error(f"Reddit fetch failed: {e}")
        return ""
    posts = []
    for child in data.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("score", 0) < 5:
            continue
        posts.append(f"- {d.get('title', '')} (score:{d.get('score', 0)}, comments:{d.get('num_comments', 0)})")
        if len(posts) >= limit:
            break
    return "\n".join(posts)


def _local_fetch_x_trends(niche):
    """Fetch X/Twitter trends via SerpAPI."""
    if not SERPAPI_KEY:
        return ""
    query = urllib.parse.urlencode({
        "engine": "google",
        "q": f"site:x.com OR site:twitter.com {niche} 2026",
        "num": 15, "tbs": "qdr:w", "api_key": SERPAPI_KEY,
    })
    try:
        resp = requests.get(f"https://serpapi.com/search.json?{query}", timeout=30)
        data = resp.json()
    except Exception as e:
        logger.error(f"X trends fetch failed: {e}")
        return ""
    results = []
    for item in data.get("organic_results", []):
        title = item.get("title", "")
        snippet = item.get("snippet", "")
        link = item.get("link", "")
        results.append(f"{title}\n  {snippet}\n  {link}")
    return "\n\n".join(results[:15])


def _local_extract_paa(keyword):
    """Extract People Also Ask via SerpAPI."""
    if not SERPAPI_KEY:
        return []
    query = urllib.parse.urlencode({
        "engine": "google", "q": keyword, "num": 10, "api_key": SERPAPI_KEY,
    })
    try:
        resp = requests.get(f"https://serpapi.com/search.json?{query}", timeout=30)
        data = resp.json()
    except Exception:
        return []
    return [q.get("question", "") for q in data.get("related_questions", []) if q.get("question")]


def _local_extract_tool_images(blog_json_str):
    """Search for tool logos via SerpAPI google_images."""
    if not SERPAPI_KEY:
        return {}
    tools = []
    tm = re.search(r'"tools_mentioned"\s*:\s*\[([^\]]*)\]', blog_json_str, re.DOTALL)
    if tm:
        try:
            tools = json.loads('[' + tm.group(1) + ']')
        except Exception:
            pass
    if not tools:
        return {}
    result = {}
    for tool_name in tools[:5]:
        if not isinstance(tool_name, str) or not tool_name.strip():
            continue
        tool_name = tool_name.strip()
        try:
            query = urllib.parse.urlencode({
                "engine": "google_images",
                "q": f"{tool_name} software logo official",
                "num": 5, "api_key": SERPAPI_KEY,
            })
            resp = requests.get(f"https://serpapi.com/search.json?{query}", timeout=20)
            data = resp.json()
            for img in data.get("images_results", []):
                img_url = img.get("original", "") or img.get("thumbnail", "")
                if not img_url or any(ext in img_url.lower() for ext in [".svg", ".ico"]):
                    continue
                result[tool_name] = {"image": img_url, "site": img.get("link", "") or ""}
                break
        except Exception:
            continue
    return result


def _local_generate_cover_image(title):
    """Generate cover image via CLIProxy Responses image tool."""
    api_key = COVER_IMAGE_API_KEY or html_runtime.USE_CASE_IMAGE_API_KEY
    if not COVER_IMAGE_URL or not api_key:
        return ""
    prompt = (
        f"Generate an image: A professional, modern blog cover image for an article titled '{title}'. "
        f"Clean, tech-oriented, visually appealing with a blue/purple gradient background. "
        f"Abstract geometric shapes or icons related to the topic. No text in the image. "
        f"Flat design, minimalist, suitable for a tech blog header. 16:9 aspect ratio."
    )
    payload = {
        "model": COVER_IMAGE_RESPONSES_MODEL,
        "input": prompt,
        "tools": [
            {
                "type": "image_generation",
                "model": COVER_IMAGE_MODEL,
                "size": COVER_IMAGE_SIZE,
                "quality": COVER_IMAGE_QUALITY,
                "output_format": COVER_IMAGE_OUTPUT_FORMAT,
            }
        ],
        "stream": False,
    }
    try:
        resp = requests.post(
            COVER_IMAGE_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=COVER_IMAGE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        image_source = html_runtime.extract_image_source_from_cliproxy_response(data)
        if image_source:
            return image_source
        logger.error(f"Cover image generation returned no image data: {str(data)[:500]}")
    except Exception as e:
        logger.error(f"Cover image generation failed: {e}")
    return ""


def _local_merge_with_paa(keyword, pages, paa_list):
    """Merge keyword + page texts + PAA into context string."""
    parts = [f"Keyword: {keyword}\n"]
    for i, page in enumerate(pages):
        if page.strip():
            parts.append(f"--- Page {i+1} ---\n{page.strip()[:6000]}\n")
    if paa_list:
        parts.append("People Also Ask:\n")
        for q in paa_list:
            parts.append(f"- {q}")
    return "\n".join(parts)


def _local_blog_pipeline(keyword, generate_cover=True):
    """Full local blog pipeline: search → scrape → merge+PAA → LLM → tool images → cover → assemble."""
    if not NEW_COZE_WORKFLOW_ID:
        return {"success": False, "error": "NEW_COZE_WORKFLOW_ID not configured"}

    urls = _local_search_serpapi(keyword, num=5)
    if not urls:
        error = f"SerpAPI returned no results for: {keyword}"
        if html_runtime.LAST_SERPAPI_ERROR:
            error = f"{error}. Diagnostic: {html_runtime.LAST_SERPAPI_ERROR}"
        return {"success": False, "error": error}
    pages = _local_scrape_all(urls[:3])
    paa = _local_extract_paa(keyword)
    context = _local_merge_with_paa(keyword, pages, paa)

    prompt = format_prompt(BLOG_GENERATE_PROMPT, keyword=keyword, context=context, paa=json.dumps(paa))

    # Add relevant internal links
    relevant = _find_relevant_articles(keyword)
    if relevant:
        prompt += (
            "\n\n## Available Internal Links (use 2-3 most relevant ones in the article)\n"
            "When mentioning iWeaver or linking to related content, use ONLY these real URLs:\n"
            f"- https://www.iweaver.ai/ — iWeaver homepage\n"
            f"{relevant}\n"
            "Do NOT use any other iweaver.ai URLs."
        )

    prompt = _enforce_json_output(prompt)
    r = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": prompt})
    if not r.get("success"):
        return {"success": False, "error": f"Blog LLM failed: {r.get('error')}"}

    blog_raw = clean_output_content(r["output"])
    logger.info(f'[BLOG] LLM raw output (first 500): {blog_raw[:500]}')
    parsed = _try_parse_blog_json(blog_raw)
    if not isinstance(parsed, dict):
        logger.error(f'[BLOG] Failed to parse JSON. Raw output: {blog_raw[:1000]}')
        return {"success": False, "error": f"Failed to parse blog JSON: {blog_raw[:500]}"}

    # Sanitize fabricated iWeaver URLs
    parsed["content"] = sanitize_iweaver_urls(parsed.get("content", ""))

    # Log TDK fields
    logger.info(f'[BLOG] Parsed OK. title={parsed.get("title","")[:60]}')
    logger.info(f'[BLOG] TDK: meta_title={parsed.get("meta_title","")}')
    logger.info(f'[BLOG] TDK: meta_description={parsed.get("meta_description","")}')
    logger.info(f'[BLOG] TDK: focus_keywords={parsed.get("focus_keywords","")}')
    logger.info(f'[BLOG] TDK: slug={parsed.get("slug","")}')
    logger.info(f'[BLOG] TDK: featured_snippet={parsed.get("featured_snippet","")[:80]}')

    tool_images = _local_extract_tool_images(blog_raw)
    if tool_images:
        parsed["tool_images"] = tool_images

    if generate_cover:
        cover_b64 = _local_generate_cover_image(parsed.get("title", "") or keyword)
        if cover_b64:
            parsed["cover_image_base64"] = cover_b64

    return {"success": True, "blog": parsed}


def _local_hot_topic_pipeline(niche):
    """Full local hot topic pipeline: reddit + x → LLM keyword → search → scrape → LLM blog → assemble."""
    if not NEW_COZE_WORKFLOW_ID:
        return {"success": False, "error": "NEW_COZE_WORKFLOW_ID not configured"}

    # Step 1: Fetch trends
    reddit_trends = _local_fetch_reddit_hot(niche)
    x_trends = _local_fetch_x_trends(niche)

    # Step 2: LLM pick keyword
    keyword_prompt = format_prompt(HOT_TOPIC_KEYWORD_PROMPT, niche=niche, reddit_trends=reddit_trends, x_trends=x_trends)
    r1 = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": keyword_prompt})
    if not r1.get("success"):
        return {"success": False, "error": f"Hot topic keyword LLM failed: {r1.get('error')}"}

    # Parse keyword from LLM output
    keyword_output = r1["output"]
    keyword = ""
    keyword_reason = ""
    try:
        kw_parsed = _try_parse_blog_json(keyword_output)
        if isinstance(kw_parsed, dict):
            keyword = kw_parsed.get("keyword", "")
            keyword_reason = kw_parsed.get("reason", "")
    except Exception:
        pass
    if not keyword:
        km = re.search(r'"keyword"\s*:\s*"((?:[^"\\]|\\.)*)"', keyword_output)
        if km:
            keyword = km.group(1)
    if not keyword:
        keyword = niche

    # Step 3: Search + scrape for the keyword
    urls = _local_search_serpapi(keyword, num=5)
    pages = _local_scrape_all(urls) if urls else []
    paa = _local_extract_paa(keyword)
    context = _local_merge_with_paa(keyword, pages, paa)

    # Step 4: LLM generate blog
    blog_prompt = format_prompt(HOT_TOPIC_BLOG_PROMPT, keyword=keyword, context=context, paa=json.dumps(paa))

    # Add relevant internal links
    relevant = _find_relevant_articles(keyword)
    if relevant:
        blog_prompt += (
            "\n\n## Available Internal Links (use 2-3 most relevant ones in the article)\n"
            "When mentioning iWeaver or linking to related content, use ONLY these real URLs:\n"
            f"- https://www.iweaver.ai/ — iWeaver homepage\n"
            f"{relevant}\n"
            "Do NOT use any other iweaver.ai URLs."
        )

    blog_prompt = _enforce_json_output(blog_prompt)
    r2 = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": blog_prompt})
    if not r2.get("success"):
        return {"success": False, "error": f"Hot topic blog LLM failed: {r2.get('error')}"}

    blog_raw = clean_output_content(r2["output"])
    logger.info(f'[HOT_TOPIC] LLM raw output (first 500): {blog_raw[:500]}')
    parsed = _try_parse_blog_json(blog_raw)
    if not isinstance(parsed, dict):
        logger.error(f'[HOT_TOPIC] Failed to parse JSON. Raw output: {blog_raw[:1000]}')
        return {"success": False, "error": f"Failed to parse blog JSON: {blog_raw[:500]}"}

    # Sanitize fabricated iWeaver URLs
    parsed["content"] = sanitize_iweaver_urls(parsed.get("content", ""))

    logger.info(f'[HOT_TOPIC] Parsed OK. title={parsed.get("title","")[:60]}')
    logger.info(f'[HOT_TOPIC] TDK: meta_title={parsed.get("meta_title","")}')
    logger.info(f'[HOT_TOPIC] TDK: meta_description={parsed.get("meta_description","")}')
    logger.info(f'[HOT_TOPIC] TDK: focus_keywords={parsed.get("focus_keywords","")}')
    logger.info(f'[HOT_TOPIC] TDK: slug={parsed.get("slug","")}')

    tool_images = _local_extract_tool_images(blog_raw)
    if tool_images:
        parsed["tool_images"] = tool_images

    cover_b64 = _local_generate_cover_image(parsed.get("title", ""))
    if cover_b64:
        parsed["cover_image_base64"] = cover_b64

    return {"success": True, "blog": parsed, "keyword": keyword, "keyword_reason": keyword_reason}
WP_POSTS_URL = f"{WP_API_BASE_URL}/posts"


def _strip_markdown_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r'^```[a-zA-Z]*\n?', '', s)
        s = re.sub(r'\n?```$', '', s)
        s = s.strip()
    return s


def _find_matching_bracket(raw, start):
    depth = 0
    in_str = False
    i = start
    while i < len(raw):
        c = raw[i]
        if c == '\\' and in_str and i + 1 < len(raw):
            i += 2
            continue
        if c == '"':
            in_str = not in_str
        elif not in_str:
            if c in '[{':
                depth += 1
            elif c in ']}':
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _extract_blog_fields_regex(raw):
    result = {}
    for field in ["title", "slug", "meta_title", "meta_description", "focus_keywords", "featured_snippet"]:
        m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            val = m.group(1)
            val = val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
            result[field] = val

    cm = re.search(r'"content"\s*:\s*"', raw)
    if cm:
        start = cm.end()
        rest = raw[start:]
        end_m = re.search(r'",\s*\n\s*"(?:faq|tools_mentioned|video_embeds|lsi_keywords|tool_images)"', rest)
        if end_m:
            content_raw = rest[:end_m.start()]
        else:
            end_m2 = re.search(r'"\s*\n\s*\}', rest)
            content_raw = rest[:end_m2.start()] if end_m2 else rest
        content_raw = content_raw.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
        result["content"] = content_raw

    for arr_field in ["faq", "tools_mentioned", "video_embeds", "lsi_keywords"]:
        am = re.search(rf'"{arr_field}"\s*:\s*[\[{{]', raw)
        if not am:
            continue
        bracket_pos = am.end() - 1
        end_pos = _find_matching_bracket(raw, bracket_pos)
        if end_pos > 0:
            arr_str = raw[bracket_pos:end_pos + 1]
            try:
                result[arr_field] = json.loads(arr_str)
            except Exception:
                pass

    if result.get("title") and result.get("content"):
        return result
    return None


def _try_parse_blog_json(raw):
    if not raw or not isinstance(raw, str):
        return None
    s = _strip_markdown_fences(raw)
    try:
        return json.loads(s)
    except Exception:
        pass
    m = re.search(r'\{[\s\S]*\}', s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return _extract_blog_fields_regex(s)


def generate_blog(keyword, generate_cover=True):
    if WORKFLOW_PROVIDER == "local":
        return _local_blog_pipeline(keyword, generate_cover=generate_cover)
    result = _run_coze_workflow(keyword, workflow_id=BLOG_WORKFLOW_ID, timeout_seconds=600)

    if not result.get("success"):
        return result

    raw_content = result["content"]
    raw_content = clean_output_content(raw_content)

    wrapper = _parse_json_string_if_possible(raw_content)
    if isinstance(wrapper, str):
        wrapper = _parse_json_string_if_possible(wrapper)

    blog_raw = None
    tool_images_raw = None

    if isinstance(wrapper, dict) and "blog_raw" in wrapper:
        blog_raw = wrapper.get("blog_raw", "")
        tool_images_raw = wrapper.get("tool_images_raw", "{}")
    else:
        blog_raw = raw_content

    parsed = _try_parse_blog_json(blog_raw)
    if not isinstance(parsed, dict):
        debug_path = STORAGE_DIR / f'debug_hot_{time.strftime("%H%M%S")}.txt'
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, 'w', encoding='utf-8') as f:
            f.write(f"=== raw_content ===\n{raw_content}\n\n=== blog_raw ===\n{str(blog_raw)}\n")
        return {"success": False, "error": f"Failed to parse blog JSON (debug: {debug_path}): {str(blog_raw)[:500]}"}

    if tool_images_raw:
        ti = _try_parse_blog_json(tool_images_raw)
        if isinstance(ti, dict):
            parsed["tool_images"] = ti

    cover_b64 = ""
    try:
        raw_data = result.get("raw_data") or ""
        if isinstance(raw_data, str):
            raw_data = _parse_json_string_if_possible(raw_data)
        if isinstance(raw_data, dict):
            cover_b64 = raw_data.get("cover_image_base64", "")
    except Exception:
        pass
    if cover_b64:
        parsed["cover_image_base64"] = cover_b64

    return {"success": True, "blog": parsed}


def generate_hot_topic_blog(niche):
    if WORKFLOW_PROVIDER == "local":
        return _local_hot_topic_pipeline(niche)
    result = _run_coze_workflow(niche, workflow_id=HOT_TOPIC_WORKFLOW_ID, timeout_seconds=900)

    if not result.get("success"):
        return result

    keyword = ""
    keyword_reason = ""
    try:
        raw_data = result.get("raw_data") or ""
        if isinstance(raw_data, str):
            raw_data = _parse_json_string_if_possible(raw_data)
        if isinstance(raw_data, dict):
            keyword = raw_data.get("keyword", "")
            keyword_reason = raw_data.get("keyword_reason", "")
    except Exception:
        pass

    raw_content = result["content"]
    raw_content = clean_output_content(raw_content)

    wrapper = _parse_json_string_if_possible(raw_content)
    if isinstance(wrapper, str):
        wrapper = _parse_json_string_if_possible(wrapper)

    blog_raw = None
    tool_images_raw = None

    if isinstance(wrapper, dict) and "blog_raw" in wrapper:
        blog_raw = wrapper.get("blog_raw", "")
        tool_images_raw = wrapper.get("tool_images_raw", "{}")
    else:
        blog_raw = raw_content

    parsed = _try_parse_blog_json(blog_raw)
    if not isinstance(parsed, dict):
        debug_path = STORAGE_DIR / f'debug_hot_{time.strftime("%H%M%S")}.txt'
        os.makedirs(os.path.dirname(debug_path), exist_ok=True)
        with open(debug_path, 'w', encoding='utf-8') as f:
            f.write(f"=== raw_content ===\n{raw_content}\n\n=== blog_raw ===\n{str(blog_raw)}\n")
        return {"success": False, "error": f"Failed to parse blog JSON (debug: {debug_path}): {str(blog_raw)[:500]}"}

    if tool_images_raw:
        ti = _try_parse_blog_json(tool_images_raw)
        if isinstance(ti, dict):
            parsed["tool_images"] = ti

    cover_b64 = ""
    try:
        if isinstance(raw_data, dict):
            cover_b64 = raw_data.get("cover_image_base64", "")
    except Exception:
        pass
    if cover_b64:
        parsed["cover_image_base64"] = cover_b64

    return {"success": True, "blog": parsed, "keyword": keyword, "keyword_reason": keyword_reason}


def html_to_gutenberg(html_content):
    if not html_content or not html_content.strip():
        return ""

    blocks = []
    remaining = html_content.strip()

    heading_pattern = re.compile(r'<(h[2-6])([^>]*)>(.*?)</\1>', re.DOTALL)
    p_pattern = re.compile(r'<p([^>]*)>(.*?)</p>', re.DOTALL)
    ul_pattern = re.compile(r'<ul([^>]*)>(.*?)</ul>', re.DOTALL)
    ol_pattern = re.compile(r'<ol([^>]*)>(.*?)</ol>', re.DOTALL)
    blockquote_pattern = re.compile(r'<blockquote([^>]*)>(.*?)</blockquote>', re.DOTALL)
    table_pattern = re.compile(r'<table([^>]*)>(.*?)</table>', re.DOTALL)
    hr_pattern = re.compile(r'<hr\s*/?>')
    img_pattern = re.compile(r'<img([^>]+?)/?>', re.DOTALL)
    pre_pattern = re.compile(r'<pre([^>]*)>(.*?)</pre>', re.DOTALL)

    block_pattern = re.compile(
        r'(<(?:h[2-6]|p|ul|ol|blockquote|table|pre)[\s>].*?</(?:h[2-6]|p|ul|ol|blockquote|table|pre)>|<hr\s*/?>|<img[^>]+?/?>)',
        re.DOTALL
    )

    for match in block_pattern.finditer(remaining):
        tag_html = match.group(0)

        h = heading_pattern.match(tag_html)
        if h:
            tag, attrs, inner = h.group(1), h.group(2), h.group(3)
            level = int(tag[1])
            blocks.append(
                f'<!-- wp:heading {{"level":{level}}} -->\n'
                f'<{tag} class="wp-block-heading">{inner}</{tag}>\n'
                f'<!-- /wp:heading -->'
            )
            continue

        p = p_pattern.match(tag_html)
        if p:
            inner = p.group(2)
            blocks.append(
                f'<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->'
            )
            continue

        ul = ul_pattern.match(tag_html)
        if ul:
            inner = ul.group(2)
            blocks.append(
                f'<!-- wp:list -->\n<ul class="wp-block-list">{inner}</ul>\n<!-- /wp:list -->'
            )
            continue

        ol = ol_pattern.match(tag_html)
        if ol:
            inner = ol.group(2)
            blocks.append(
                f'<!-- wp:list {{"ordered":true}} -->\n<ol class="wp-block-list">{inner}</ol>\n<!-- /wp:list -->'
            )
            continue

        bq = blockquote_pattern.match(tag_html)
        if bq:
            inner = bq.group(2)
            blocks.append(
                f'<!-- wp:quote -->\n<blockquote class="wp-block-quote">{inner}</blockquote>\n<!-- /wp:quote -->'
            )
            continue

        tbl = table_pattern.match(tag_html)
        if tbl:
            inner = tbl.group(2)
            blocks.append(
                f'<!-- wp:table -->\n<figure class="wp-block-table"><table>{inner}</table></figure>\n<!-- /wp:table -->'
            )
            continue

        pre_m = pre_pattern.match(tag_html)
        if pre_m:
            inner = pre_m.group(2)
            blocks.append(
                f'<!-- wp:code -->\n<pre class="wp-block-code"><code>{inner}</code></pre>\n<!-- /wp:code -->'
            )
            continue

        if hr_pattern.match(tag_html):
            blocks.append(
                '<!-- wp:separator -->\n<hr class="wp-block-separator has-alpha-channel-opacity"/>\n<!-- /wp:separator -->'
            )
            continue

        if img_pattern.match(tag_html):
            src_match = re.search(r'src="([^"]+)"', tag_html)
            alt_match = re.search(r'alt="([^"]*)"', tag_html)
            if src_match:
                src = src_match.group(1)
                alt = alt_match.group(1) if alt_match else ""
                blocks.append(
                    f'<!-- wp:image -->\n'
                    f'<figure class="wp-block-image"><img src="{src}" alt="{alt}"/></figure>\n'
                    f'<!-- /wp:image -->'
                )
            continue

    return "\n\n".join(blocks)


def build_faq_gutenberg(faq_list):
    if not faq_list:
        return ""

    blocks = [
        '<!-- wp:heading {"level":2} -->\n'
        '<h2 class="wp-block-heading">Frequently Asked Questions</h2>\n'
        '<!-- /wp:heading -->'
    ]

    for item in faq_list:
        q = item.get("question", "").strip()
        a = item.get("answer", "").strip()
        if not q:
            continue
        blocks.append(
            f'<!-- wp:heading {{"level":3}} -->\n'
            f'<h3 class="wp-block-heading">{q}</h3>\n'
            f'<!-- /wp:heading -->'
        )
        if a:
            blocks.append(
                f'<!-- wp:paragraph -->\n<p>{a}</p>\n<!-- /wp:paragraph -->'
            )

    return "\n\n".join(blocks)


def build_video_embeds(video_urls):
    if not video_urls:
        return ""

    blocks = []
    for url in video_urls:
        url = url.strip()
        if not url:
            continue
        blocks.append(
            f'<!-- wp:embed {{"url":"{url}","type":"video","providerNameSlug":"youtube","responsive":true}} -->\n'
            f'<figure class="wp-block-embed is-type-video is-provider-youtube wp-block-embed-youtube">'
            f'<div class="wp-block-embed__wrapper">\n{url}\n</div></figure>\n'
            f'<!-- /wp:embed -->'
        )

    return "\n\n".join(blocks)


def process_cover_image(base64_data, slug):
    if not base64_data:
        return None

    try:
        webp_bytes = convert_image_source_to_webp_bytes(base64_data)
        filename = slugify_english(slug, "cover", fallback="blog-cover", max_words=7, strip_digits=True)
        result = upload_media_to_wp(webp_bytes, filename, alt_text=f"{slug} cover image")
        return result.get("id")
    except Exception as exc:
        logger.warning(f"Cover image upload failed: {exc}")
        return None


def process_cover_image_file(file_path, slug):
    if not file_path:
        return None

    try:
        with open(file_path, "rb") as f:
            webp_bytes = f.read()
        filename = slugify_english(slug, "cover", fallback="blog-cover", max_words=7, strip_digits=True)
        result = upload_media_to_wp(webp_bytes, filename, alt_text=f"{slug} cover image")
        return result.get("id")
    except Exception as exc:
        logger.warning(f"Cover image file upload failed: {exc}")
        return None


def process_tool_images(tool_images_dict, slug):
    if not tool_images_dict or not isinstance(tool_images_dict, dict):
        return {}

    wp_tool_images = {}
    for tool_name, info in tool_images_dict.items():
        if not isinstance(info, dict):
            continue
        image_url = info.get("image", "")
        if not image_url:
            continue
        try:
            webp_bytes = convert_image_source_to_webp_bytes(image_url)
            filename = slugify_english(slug, tool_name, fallback="tool-image", max_words=7, strip_digits=True)
            result = upload_media_to_wp(webp_bytes, filename, alt_text=f"{tool_name} logo")
            wp_tool_images[tool_name] = result.get("source_url", "")
        except Exception as exc:
            logger.warning(f"Tool image upload failed for {tool_name}: {exc}")

    return wp_tool_images


def inject_tool_images(gutenberg_content, tool_image_map):
    if not tool_image_map:
        return gutenberg_content

    inserted = set()
    lines = gutenberg_content.split("\n")
    result_lines = []

    for line in lines:
        result_lines.append(line)
        if "<!-- /wp:paragraph -->" in line or "<!-- /wp:heading -->" in line:
            for tool_name, wp_url in tool_image_map.items():
                if tool_name in inserted or not wp_url:
                    continue
                if tool_name.lower() in line.lower():
                    image_block = (
                        f'\n<!-- wp:image -->\n'
                        f'<figure class="wp-block-image"><img src="{wp_url}" alt="{tool_name}"/>'
                        f'<figcaption class="wp-element-caption">{tool_name}</figcaption></figure>\n'
                        f'<!-- /wp:image -->'
                    )
                    result_lines.append(image_block)
                    inserted.add(tool_name)

    remaining = [name for name, url in tool_image_map.items() if name not in inserted and url]
    if remaining:
        for tool_name in remaining:
            wp_url = tool_image_map[tool_name]
            image_block = (
                f'\n<!-- wp:image -->\n'
                f'<figure class="wp-block-image"><img src="{wp_url}" alt="{tool_name}"/>'
                f'<figcaption class="wp-element-caption">{tool_name}</figcaption></figure>\n'
                f'<!-- /wp:image -->'
            )
            result_lines.append(image_block)

    return "\n".join(result_lines)


def build_featured_snippet_block(snippet):
    if not snippet or not snippet.strip():
        return ""
    return (
        '<!-- wp:paragraph {"className":"featured-snippet"} -->\n'
        f'<p class="featured-snippet"><strong>{snippet.strip()}</strong></p>\n'
        '<!-- /wp:paragraph -->'
    )


def post_blog_to_wp(title, slug, content, status, seo_data, featured_media_id=None, tag_ids=None, category_ids=None):
    data = {
        "title": title,
        "slug": slug,
        "status": status or "draft",
        "content": content,
    }

    if featured_media_id:
        data["featured_media"] = featured_media_id

    if tag_ids:
        data["tags"] = tag_ids

    if category_ids:
        data["categories"] = category_ids

    meta = {}
    if seo_data:
        if seo_data.get("meta_title"):
            meta["rank_math_title"] = seo_data["meta_title"][:60]
        if seo_data.get("meta_description"):
            meta["rank_math_description"] = seo_data["meta_description"][:155]
        if seo_data.get("focus_keywords"):
            meta["rank_math_focus_keyword"] = seo_data["focus_keywords"]
    if meta:
        data["meta"] = meta

    existing_id = None
    try:
        lookup = wp_request("GET", WP_POSTS_URL, params={"slug": slug, "status": "any", "per_page": 1})
        if lookup.status_code < 400:
            existing = lookup.json()
            if isinstance(existing, list) and existing:
                existing_id = existing[0].get("id")
    except Exception as exc:
        logger.warning(f"Blog slug lookup failed for {slug}: {exc}")

    endpoint = f"{WP_POSTS_URL}/{existing_id}" if existing_id else WP_POSTS_URL
    response = wp_request("POST", endpoint, json=data)
    response.raise_for_status()

    result = response.json()
    return {
        "id": result.get("id"),
        "link": result.get("link"),
        "slug": result.get("slug"),
        "status": result.get("status"),
        "status_code": response.status_code,
        "updated_existing": bool(existing_id),
    }


def assemble_and_publish(blog_data, wp_status="draft", wp_tag_ids=None, wp_category_ids=None):
    title = blog_data.get("title", "Untitled Blog Post")
    slug = blog_data.get("slug", slugify_english(title, fallback="blog-post"))
    content_html = blog_data.get("content", "")
    faq_list = blog_data.get("faq", [])
    video_urls = blog_data.get("video_embeds", [])
    cover_file = blog_data.get("cover_image_file", "")
    cover_b64 = blog_data.get("cover_image_base64", "")
    tool_images = blog_data.get("tool_images", {})
    snippet = blog_data.get("featured_snippet", "")

    seo_data = {
        "meta_title": blog_data.get("meta_title", ""),
        "meta_description": blog_data.get("meta_description", ""),
        "focus_keywords": blog_data.get("focus_keywords", ""),
    }

    gutenberg = ""

    snippet_block = build_featured_snippet_block(snippet)
    if snippet_block:
        gutenberg += snippet_block + "\n\n"

    main_blocks = html_to_gutenberg(content_html)
    if main_blocks:
        gutenberg += main_blocks

    video_blocks = build_video_embeds(video_urls)
    if video_blocks:
        gutenberg += "\n\n" + video_blocks

    faq_blocks = build_faq_gutenberg(faq_list)
    if faq_blocks:
        gutenberg += "\n\n" + faq_blocks

    featured_media_id = process_cover_image_file(cover_file, slug) if cover_file else process_cover_image(cover_b64, slug)

    wp_tool_images = process_tool_images(tool_images, slug)
    if wp_tool_images:
        gutenberg = inject_tool_images(gutenberg, wp_tool_images)

    wp_result = post_blog_to_wp(
        title=title,
        slug=slug,
        content=gutenberg,
        status=wp_status,
        seo_data=seo_data,
        featured_media_id=featured_media_id,
        tag_ids=wp_tag_ids,
        category_ids=wp_category_ids,
    )

    return {
        "slug": slug,
        "id": wp_result.get("id"),
        "link": wp_result.get("link"),
        "status": wp_result.get("status"),
        "seo": seo_data,
        "has_cover": featured_media_id is not None,
        "tool_images_count": len(wp_tool_images),
        "updated_existing": bool(wp_result.get("updated_existing")),
    }
