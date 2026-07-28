import json
import re
import os
import base64
import ssl
import time
import logging
import requests
import urllib.request
from io import BytesIO
from PIL import Image

logger = logging.getLogger('insight_article')

from .html_generat import (
    _run_coze_workflow,
    _parse_json_string_if_possible,
    clean_output_content,
    _extract_coze_content,
    COZE_BASE_URL,
    COZE_WORKFLOW_TOKEN,
    COZE_IMAGE_WORKFLOW_ID,
    COZE_IMAGE_PROJECT_ID,
    COZE_IMAGE_WORKFLOW_TIMEOUT,
    slugify_english,
    WORKFLOW_PROVIDER,
)
from .coze_llm import call_coze_llm
from .paths import STORAGE_DIR
from .prompts import INSIGHT_ARTICLE_PROMPT, format_prompt

INSIGHT_ARTICLE_WORKFLOW_ID = "7631900477424140288"
NEW_COZE_WORKFLOW_ID = os.getenv("NEW_COZE_WORKFLOW_ID", "").strip()


def _try_fix_json(text):
    text = text.strip()
    if not text.startswith("{"):
        idx = text.find("{")
        if idx >= 0:
            text = text[idx:]
    if not text.endswith("}"):
        idx = text.rfind("}")
        if idx >= 0:
            text = text[:idx + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r'(?<=: ")(.*?)(?="(?:,|\s*\n|\s*}))', lambda m: m.group(1).replace('"', '\\"'), text)
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    try:
        import ast
        return ast.literal_eval(text)
    except Exception:
        pass
    return text


def _run_insight_workflow(params, timeout_seconds=300):
    if WORKFLOW_PROVIDER == "local":
        if not NEW_COZE_WORKFLOW_ID:
            return {"success": False, "error": "NEW_COZE_WORKFLOW_ID not configured"}
        prompt = format_prompt(INSIGHT_ARTICLE_PROMPT, **params)
        prompt += (
            "\n\n--- CRITICAL OUTPUT INSTRUCTION ---\n"
            "You MUST respond with ONLY a valid JSON object. Nothing else.\n"
            "Do NOT include any text before or after the JSON.\n"
            "Do NOT use markdown code fences.\n"
            "Your ENTIRE response must be a single valid JSON object starting with { and ending with }.\n"
            "--- END INSTRUCTION ---"
        )
        r = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": prompt})
        if not r.get("success"):
            return {"success": False, "error": r.get("error")}
        return {"success": True, "content": r["output"]}
    if not COZE_WORKFLOW_TOKEN:
        return {"success": False, "error": "Missing COZE_WORKFLOW_TOKEN"}
    headers = {
        "Authorization": f"Bearer {COZE_WORKFLOW_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "workflow_id": INSIGHT_ARTICLE_WORKFLOW_ID,
        "parameters": params,
        "is_async": False,
    }
    try:
        resp = requests.post(
            f"{COZE_BASE_URL}/v1/workflow/run",
            headers=headers, json=payload, timeout=timeout_seconds,
        )
        resp.raise_for_status()
        run_result = resp.json()
    except requests.exceptions.Timeout:
        return {"success": False, "error": "Workflow request timed out"}
    except Exception as e:
        return {"success": False, "error": f"Workflow request failed: {e}"}

    if run_result.get("code") not in (0, "0", None):
        return {"success": False, "error": f"Workflow error: {run_result}"}

    content = _extract_coze_content(run_result)
    if content:
        return {"success": True, "content": content}
    return {"success": False, "error": f"No usable content: {run_result}"}

INSIGHT_WP_URL = os.getenv("INSIGHT_WP_URL", "https://insight.xiaoduoai.com/wp-json/wp/v2")
INSIGHT_WP_USER = os.getenv("INSIGHT_WP_USER", "")
INSIGHT_WP_APP_PASSWORD = os.getenv("INSIGHT_WP_APP_PASSWORD", "")
INSIGHT_WP_AUTH = base64.b64encode(
    f"{INSIGHT_WP_USER}:{INSIGHT_WP_APP_PASSWORD}".encode()
).decode() if INSIGHT_WP_USER and INSIGHT_WP_APP_PASSWORD else ""
INSIGHT_WP_HEADERS = {
    "Content-Type": "application/json",
}
if INSIGHT_WP_AUTH:
    INSIGHT_WP_HEADERS["Authorization"] = f"Basic {INSIGHT_WP_AUTH}"
INSIGHT_SSL_CTX = ssl._create_unverified_context()

CATEGORY_MAP = {
    "服务式营销": 4, "淘宝资讯": 26, "客服管理": 1, "京东资讯": 25,
    "抖音资讯": 24, "AI提效": 27, "智能客服机器人": 105, "拼多多资讯": 114,
    "电商知识": 23, "跨境电商资讯": 175, "智能客服系统": 157, "智能化前沿": 122,
    "技术洞察": 21, "大模型": 20, "快手资讯": 206, "智能工具": 121,
    "微信资讯": 57, "小红书": 280, "多策": 282, "服务接待": 2,
    "质检": 104, "聚合接待": 186, "工单系统": 149,
}
CATEGORY_ID_TO_NAME = {v: k for k, v in CATEGORY_MAP.items()}


def _insight_wp_request(method, path, **kwargs):
    url = f"{INSIGHT_WP_URL}/{path}" if not path.startswith("http") else path
    headers = dict(INSIGHT_WP_HEADERS)
    extra = kwargs.pop("headers", None)
    if extra:
        headers.update(extra)
    for attempt in range(3):
        try:
            resp = requests.request(
                method, url, headers=headers,
                timeout=kwargs.pop("timeout", (10, 60)),
                verify=False, **kwargs,
            )
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
        except requests.RequestException:
            pass
        time.sleep(2 * (attempt + 1))
    return resp


def generate_topics(category_name):
    result = _run_insight_workflow(
        {"mode": "topics", "keyword": "", "category": category_name},
        timeout_seconds=120,
    )
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Workflow failed")}

    raw = clean_output_content(result["content"])
    parsed = _parse_json_string_if_possible(raw)
    if isinstance(parsed, str):
        parsed = _parse_json_string_if_possible(parsed)
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(raw)
        except Exception:
            return {"success": False, "error": f"Failed to parse topics JSON: {raw[:500]}"}

    return {"success": True, "topics": parsed.get("topics", [])}


def generate_article(keyword, category_name):
    result = _run_insight_workflow(
        {"mode": "article", "keyword": keyword, "category": category_name},
        timeout_seconds=300,
    )
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Workflow failed")}

    raw_content = result["content"]
    raw = clean_output_content(raw_content)
    parsed = _parse_json_string_if_possible(raw)
    if isinstance(parsed, str):
        parsed = _parse_json_string_if_possible(parsed)
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = _try_fix_json(raw)
            if not isinstance(parsed, dict):
                debug_path = STORAGE_DIR / "debug_article_raw.txt"
                with debug_path.open("w", encoding="utf-8") as f:
                    f.write(raw)
                return {"success": False, "error": f"Failed to parse article JSON (saved to {debug_path}): {raw[:300]}"}

    return {"success": True, "article": parsed}


GEMINI_IMAGE_API_URL = os.getenv("GEMINI_IMAGE_API_URL", "")
GEMINI_IMAGE_API_KEY = os.getenv("GEMINI_IMAGE_API_KEY", "")
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-3-pro-image-preview")


def _generate_image(description):
    if not GEMINI_IMAGE_API_URL or not GEMINI_IMAGE_API_KEY:
        return None
    headers = {
        "Authorization": f"Bearer {GEMINI_IMAGE_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GEMINI_IMAGE_MODEL,
        "messages": [
            {
                "role": "user",
                "content": f"Generate an image: {description}. Only output the image, no text explanation.",
            }
        ],
        "max_tokens": 4096,
    }
    try:
        resp = requests.post(GEMINI_IMAGE_API_URL, headers=headers, json=payload, timeout=120, verify=False)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"[IMAGE] API request failed: {e}")
        return None

    choices = data.get("choices", [])
    if not choices:
        logger.warning(f"[IMAGE] No choices in response: {data}")
        return None

    message = choices[0].get("message", {})
    content = message.get("content", "")

    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url:
                        return url
                elif part.get("type") == "image":
                    b64 = part.get("data") or part.get("image", "")
                    if b64:
                        return f"data:image/png;base64,{b64}"
        logger.warning(f"[IMAGE] No image found in content parts: {[p.get('type') for p in content if isinstance(p, dict)]}")
        return None

    if isinstance(content, str):
        if content.startswith("http") or content.startswith("data:"):
            return content.strip()
        import re
        url_match = re.search(r'https?://\S+\.(png|jpg|jpeg|webp|gif)', content)
        if url_match:
            return url_match.group(0)
        b64_match = re.search(r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+', content)
        if b64_match:
            return b64_match.group(0)

    logger.warning(f"[IMAGE] Could not extract image from response, content type: {type(content).__name__}, snippet: {str(content)[:200]}")
    return None


def _decode_image_source(source):
    if source.startswith("data:"):
        _, encoded = source.split(",", 1)
        return base64.b64decode(encoded)
    if source.startswith("http"):
        resp = requests.get(source, timeout=(10, 60), verify=False)
        resp.raise_for_status()
        return resp.content
    return base64.b64decode(source)


def _to_webp(raw_bytes):
    buf_in = BytesIO(raw_bytes)
    buf_out = BytesIO()
    with Image.open(buf_in) as img:
        if img.mode in ("RGBA", "LA") or "transparency" in img.info:
            img = img.convert("RGBA")
        else:
            img = img.convert("RGB")
        img.save(buf_out, format="WEBP", quality=85, method=4)
    return buf_out.getvalue()


def _upload_image_to_insight(image_source, filename, alt_text=""):
    raw_bytes = _decode_image_source(image_source)
    webp_bytes = _to_webp(raw_bytes)
    safe_name = slugify_english(filename, fallback="article-image", max_words=7) + ".webp"

    resp = _insight_wp_request(
        "POST", "media",
        data=webp_bytes,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "Content-Type": "image/webp",
        },
        timeout=(10, 120),
    )
    resp.raise_for_status()
    payload = resp.json()
    media_id = payload.get("id")
    source_url = payload.get("source_url", "")

    if alt_text and media_id:
        _insight_wp_request("POST", f"media/{media_id}", json={
            "alt_text": alt_text, "title": alt_text,
        })

    return {"id": media_id, "source_url": source_url}


def generate_article_images(image_prompts, slug=""):
    uploaded = []
    for i, prompt in enumerate(image_prompts or []):
        desc = prompt.get("description", "")
        alt = prompt.get("alt", desc)
        if not desc:
            continue
        try:
            image_source = _generate_image(desc)
            if not image_source:
                uploaded.append({"position": prompt.get("position", ""), "source_url": "", "error": "Image generation returned empty"})
                continue
            result = _upload_image_to_insight(
                image_source,
                f"{slug or 'article'}-img-{i+1}",
                alt_text=alt,
            )
            uploaded.append({
                "position": prompt.get("position", ""),
                "source_url": result["source_url"],
                "media_id": result["id"],
                "alt": alt,
            })
        except Exception as e:
            uploaded.append({"position": prompt.get("position", ""), "source_url": "", "error": str(e)})
    return uploaded


def article_to_gutenberg(article_data, uploaded_images=None):
    blocks = []
    image_map = {}
    for img in (uploaded_images or []):
        pos = img.get("position", "")
        if pos and img.get("source_url"):
            image_map[pos] = img

    summary = article_data.get("summary", "")
    if summary:
        blocks.append(
            '<!-- wp:quote -->\n'
            f'<blockquote class="wp-block-quote"><p>{summary}</p></blockquote>\n'
            '<!-- /wp:quote -->'
        )

    for si, section in enumerate(article_data.get("sections", [])):
        level = section.get("level", 2)
        heading = section.get("heading", "")
        content = section.get("content", "")

        if heading:
            blocks.append(
                f'<!-- wp:heading {{"level":{level}}} -->\n'
                f'<h{level} class="wp-block-heading">{heading}</h{level}>\n'
                f'<!-- /wp:heading -->'
            )

        if content:
            blocks.extend(_html_content_to_blocks(content))

        for sub in section.get("subsections", []):
            sub_level = sub.get("level", 3)
            sub_heading = sub.get("heading", "")
            sub_content = sub.get("content", "")
            if sub_heading:
                blocks.append(
                    f'<!-- wp:heading {{"level":{sub_level}}} -->\n'
                    f'<h{sub_level} class="wp-block-heading">{sub_heading}</h{sub_level}>\n'
                    f'<!-- /wp:heading -->'
                )
            if sub_content:
                blocks.extend(_html_content_to_blocks(sub_content))

        img_key = f"after_section_{si+1}"
        if img_key in image_map:
            img = image_map[img_key]
            blocks.append(
                f'<!-- wp:image -->\n'
                f'<figure class="wp-block-image size-full">'
                f'<img src="{img["source_url"]}" alt="{img.get("alt", "")}" '
                f'style="aspect-ratio:16/9;object-fit:cover"/>'
                f'</figure>\n'
                f'<!-- /wp:image -->'
            )

    return "\n\n".join(blocks)


def _html_content_to_blocks(html):
    blocks = []
    block_re = re.compile(
        r'(<(?:p|ul|ol|table|blockquote|h[2-6])[\s>].*?</(?:p|ul|ol|table|blockquote|h[2-6])>|<hr\s*/?>)',
        re.DOTALL,
    )
    for m in block_re.finditer(html):
        tag_html = m.group(0)

        if re.match(r'<p[\s>]', tag_html):
            inner = re.sub(r'^<p[^>]*>(.*)</p>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(f'<!-- wp:paragraph -->\n<p>{inner}</p>\n<!-- /wp:paragraph -->')

        elif re.match(r'<ul[\s>]', tag_html):
            inner = re.sub(r'^<ul[^>]*>(.*)</ul>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(f'<!-- wp:list -->\n<ul class="wp-block-list">{inner}</ul>\n<!-- /wp:list -->')

        elif re.match(r'<ol[\s>]', tag_html):
            inner = re.sub(r'^<ol[^>]*>(.*)</ol>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(f'<!-- wp:list {{"ordered":true}} -->\n<ol class="wp-block-list">{inner}</ol>\n<!-- /wp:list -->')

        elif re.match(r'<table[\s>]', tag_html):
            inner = re.sub(r'^<table[^>]*>(.*)</table>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(
                f'<!-- wp:table -->\n'
                f'<figure class="wp-block-table"><table class="has-fixed-layout">{inner}</table></figure>\n'
                f'<!-- /wp:table -->'
            )

        elif re.match(r'<blockquote[\s>]', tag_html):
            inner = re.sub(r'^<blockquote[^>]*>(.*)</blockquote>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(f'<!-- wp:quote -->\n<blockquote class="wp-block-quote">{inner}</blockquote>\n<!-- /wp:quote -->')

        elif re.match(r'<h([2-6])[\s>]', tag_html):
            lm = re.match(r'<h([2-6])', tag_html)
            lvl = int(lm.group(1))
            inner = re.sub(rf'^<h{lvl}[^>]*>(.*)</h{lvl}>$', r'\1', tag_html, flags=re.DOTALL)
            blocks.append(
                f'<!-- wp:heading {{"level":{lvl}}} -->\n'
                f'<h{lvl} class="wp-block-heading">{inner}</h{lvl}>\n'
                f'<!-- /wp:heading -->'
            )

        elif re.match(r'<hr', tag_html):
            blocks.append(
                '<!-- wp:separator -->\n'
                '<hr class="wp-block-separator has-alpha-channel-opacity"/>\n'
                '<!-- /wp:separator -->'
            )

    if not blocks and html.strip():
        blocks.append(f'<!-- wp:paragraph -->\n<p>{html.strip()}</p>\n<!-- /wp:paragraph -->')

    return blocks


def post_article_to_insight(title, slug, gutenberg_content, category_ids=None,
                            featured_media_id=None, seo_data=None, status="draft"):
    data = {
        "title": title,
        "slug": slug,
        "content": gutenberg_content,
        "status": status,
    }
    if category_ids:
        data["categories"] = category_ids
    if featured_media_id:
        data["featured_media"] = featured_media_id

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

    resp = _insight_wp_request("POST", "posts", json=data)
    resp.raise_for_status()
    result = resp.json()
    return {
        "id": result.get("id"),
        "link": result.get("link"),
        "slug": result.get("slug"),
        "status": result.get("status"),
    }


def run_full_pipeline(keyword, category_name, generate_images=True):
    art_result = generate_article(keyword, category_name)
    if not art_result.get("success"):
        return art_result

    article = art_result["article"]
    slug = article.get("slug", slugify_english(keyword, fallback="insight-article"))

    uploaded_images = []
    featured_media_id = None
    if generate_images and article.get("image_prompts"):
        uploaded_images = generate_article_images(article["image_prompts"], slug=slug)
        for img in uploaded_images:
            if img.get("media_id"):
                featured_media_id = img["media_id"]
                break

    gutenberg = article_to_gutenberg(article, uploaded_images)

    category_ids = article.get("suggested_categories", [])
    cat_id = CATEGORY_MAP.get(category_name)
    if cat_id and cat_id not in category_ids:
        category_ids.insert(0, cat_id)

    wp_result = post_article_to_insight(
        title=article.get("title", keyword),
        slug=slug,
        gutenberg_content=gutenberg,
        category_ids=category_ids,
        featured_media_id=featured_media_id,
        seo_data=article.get("seo"),
        status="draft",
    )

    return {
        "success": True,
        "article_title": article.get("title", ""),
        "slug": slug,
        "wp": wp_result,
        "images_generated": len([i for i in uploaded_images if i.get("source_url")]),
        "images_failed": len([i for i in uploaded_images if i.get("error")]),
        "category_ids": category_ids,
    }
