"""Server-side LLM helpers for the content workflow (comment analysis + drafting).

Calls the operator's New API relay (OpenAI-compatible) only. Never touches the
browse worker or X credentials. All prompts enforce the operator's content rules:
relevant, no boilerplate, no fabrication, no attacks, no links unless allowed.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE_URL = os.getenv("WORKFLOW_LLM_BASE_URL", "http://127.0.0.1:8318/v1")
DEFAULT_MODEL = os.getenv("WORKFLOW_LLM_MODEL", "glm-5.1")
DEFAULT_TIMEOUT = 60.0


class LLMError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _chat(messages: list[dict[str, str]], *, model: str, base_url: str, api_key: str,
          timeout: float, max_tokens: int = 900) -> str:
    if not api_key:
        raise LLMError("llm_unconfigured", "WORKFLOW_LLM_KEY is not set")
    payload = json.dumps({"model": model, "messages": messages, "temperature": 0.6,
                          "max_tokens": max_tokens}).encode("utf-8")
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions",
                                     data=payload, method="POST")
    request.add_header("Authorization", "Bearer " + api_key)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=max(5.0, float(timeout))) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", "replace") if exc.fp else ""
        raise LLMError("llm_http_error", f"LLM returned HTTP {exc.code}: {body[:300]}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMError("llm_unreachable", f"LLM endpoint unavailable: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise LLMError("llm_invalid_response", "LLM returned invalid JSON") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        raise LLMError("llm_invalid_response", "LLM response had no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LLMError("llm_invalid_response", "LLM choice was not an object")
    content = first.get("message", {}).get("content") if isinstance(first.get("message"), dict) else None
    if not isinstance(content, str) or not content.strip():
        raise LLMError("llm_invalid_response", "LLM response had no content")
    return content.strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise LLMError("llm_invalid_response", "LLM did not return a JSON object")
    try:
        value = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError("llm_invalid_response", f"LLM JSON was invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMError("llm_invalid_response", "LLM JSON root was not an object")
    return value


def analyze_post(*, post_text: str, author_handle: str, keyword: str,
                 base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                 api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Returns {summary, intent, pain_points, angle, risk, recommend}."""
    api_key = api_key or os.getenv("WORKFLOW_LLM_KEY", "")
    prompt = (
        "你是社媒运营助手。分析下面这条 X 帖子，输出 JSON。字段：summary(内容摘要，<=120字)、"
        "intent(发帖人意图或问题)、pain_points(痛点，可空)、angle(可回复的切入点)、"
        "risk(内容风险提示，如敏感/争议/信息不足；无风险写'无')、recommend(布尔，是否建议回复)。\n"
        "规则：若帖子过短、无法理解、与关键词无实际关系或风险高，recommend 设为 false。\n"
        f"关键词：{keyword}\n作者：@{author_handle}\n帖子正文：\n{post_text[:4000]}"
    )
    text = _chat([{"role": "user", "content": prompt}], model=model, base_url=base_url,
                 api_key=api_key, timeout=timeout)
    result = _parse_json_object(text)
    for key in ("summary", "intent", "angle", "risk"):
        if not isinstance(result.get(key), str):
            raise LLMError("llm_invalid_response", f"LLM analysis missing field {key}")
    result["recommend"] = bool(result.get("recommend", True))
    return result


def draft_comment(*, post_text: str, author_handle: str, account_persona: str,
                  comment_style: str, recent_comments: list[str],
                  base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                  api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                  extra_instruction: str = "") -> dict[str, str]:
    """Returns {comment, rationale}."""
    api_key = api_key or os.getenv("WORKFLOW_LLM_KEY", "")
    recent = "\n".join(f"- {c}" for c in recent_comments[-8:]) or "（暂无）"
    prompt = (
        "你是社媒运营助手。为下面这条 X 帖子写一条评论草稿，输出 JSON：{comment, rationale}。\n"
        "规则：\n"
        "1. 与原帖具体内容相关，不用通用套话。\n"
        "2. 不复述整篇帖子，提供观点/补充信息/提问或自然互动。\n"
        "3. 不虚构事实，不承诺无法确认的结果，不攻击贬低，不引导争议。\n"
        "4. 不重复近期已发布的评论。默认不带链接。\n"
        "5. 满足 X 字数限制(<=280字)。\n"
        f"回复账号人设：{account_persona or '专业、友善的运营人员'}\n"
        f"评论风格：{comment_style or '自然、有见地、简短'}\n"
        f"近期已发评论（避免重复）：\n{recent}\n"
        f"补充要求：{extra_instruction or '无'}\n"
        f"原帖作者：@{author_handle}\n原帖正文：\n{post_text[:4000]}"
    )
    text = _chat([{"role": "user", "content": prompt}], model=model, base_url=base_url,
                 api_key=api_key, timeout=timeout)
    result = _parse_json_object(text)
    if not isinstance(result.get("comment"), str) or not result["comment"].strip():
        raise LLMError("llm_invalid_response", "LLM draft had no comment")
    if len(result["comment"]) > 280:
        result["comment"] = result["comment"][:280]
    if not isinstance(result.get("rationale"), str):
        result["rationale"] = ""
    return {"comment": result["comment"].strip(), "rationale": result["rationale"].strip()}


def _parse_json_array(text: str) -> list[Any]:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        # tolerate {"topics": [...]} shape
        obj = _parse_json_object(text)
        if isinstance(obj.get("topics"), list):
            return obj["topics"]
        raise LLMError("llm_invalid_response", "LLM did not return a JSON array")
    raw = text[start:end + 1]
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        # Tolerant recovery: models sometimes emit trailing commas, unescaped
        # quotes/newlines copied from source posts, or get truncated at the
        # token limit. Strip trailing commas, then brace-scan top-level objects
        # over the FULL tail (the [..] slice above may itself be truncated) and
        # keep the ones that parse individually.
        import re as _re
        cleaned = _re.sub(r",(\s*[}\]])", r"\1", raw)
        try:
            value = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            value = _recover_array_objects(text[start:])
            if not value:
                raise LLMError("llm_invalid_response", f"LLM JSON array invalid: {exc}") from exc
    if not isinstance(value, list):
        raise LLMError("llm_invalid_response", "LLM JSON was not an array")
    return value


def _recover_array_objects(raw: str) -> list[Any]:
    """Brace-scan a (possibly broken) JSON array body and parse each top-level
    object individually, skipping fragments that don't parse. Rescues valid
    topics when one object is malformed or the tail is truncated."""
    items: list[Any] = []
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(raw):
        if escape:
            escape = False
            continue
        if in_string:
            if ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = raw[start:i + 1]
                try:
                    obj = json.loads(fragment)
                    if isinstance(obj, dict):
                        items.append(obj)
                except json.JSONDecodeError:
                    pass
                start = -1
    return items


def summarize_topics(*, candidate_posts: list[dict[str, Any]], keyword: str,
                     account_persona: str = "",
                     base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                     api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                     extra_instruction: str = "") -> list[dict[str, Any]]:
    """Cluster candidate posts into <=8 post topics. Each topic:
    {topic_id, theme, key_points[], extension_angles[], suggested_links[], risk, recommend}."""
    api_key = api_key or os.getenv("WORKFLOW_LLM_KEY", "")
    if not candidate_posts:
        return []
    # Keep the prompt bounded: top 20 posts, truncated bodies.
    digest_lines: list[str] = []
    for idx, post in enumerate(candidate_posts[:20], start=1):
        body = str(post.get("text") or "")[:500]
        digest_lines.append(f"{idx}. @{post.get('author_handle','')} : {body}")
    digest = "\n".join(digest_lines)
    prompt = (
        "你是社媒运营选题助手。下面是按关键词搜到的多条 X 帖子。把它们归纳成不超过 8 个可发帖的选题，"
        "输出 JSON 数组，每个元素：{topic_id(形如 t1), theme(选题主题<=40字), key_points(主要观点/共同结论数组), "
        "extension_angles(可延展的发帖角度数组), suggested_links(建议引用来源链接数组，可为空), "
        "risk(事实/版权/舆情风险，无则写'无'), recommend(布尔，是否建议生成帖子)}。\n"
        "规则：\n"
        "1. 同一事件合并为一个选题，不重复。\n"
        "2. 主题基于已给出帖子，不得虚构数据或事件。\n"
        "3. 信息不足、风险高、广告或低质量内容 recommend 设为 false。\n"
        "4. suggested_links 只能来自帖子中出现的链接或为空，不得编造。\n"
        "5. 只输出 JSON 数组本身，不要 markdown 代码块；字符串内不要出现未转义的双引号和换行，"
        "每条文字不超过 40 字，不要大段复制原文。\n"
        f"关键词：{keyword}\n回复账号人设：{account_persona or '专业、友善的运营人员'}\n"
        f"补充要求：{extra_instruction or '无'}\n候选帖子：\n{digest}"
    )
    text = _chat([{"role": "user", "content": prompt}], model=model, base_url=base_url,
                 api_key=api_key, timeout=timeout, max_tokens=2000)
    try:
        raw = _parse_json_array(text)
    except LLMError:
        # One strict retry: feed the model's broken output back and demand valid JSON.
        retry_messages = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": text[:1500]},
            {"role": "user", "content": "上一条输出不是合法 JSON（可能被截断或含未转义字符）。请重新输出，只输出一个合法的 JSON 数组，不要任何解释和代码块标记。"},
        ]
        text = _chat(retry_messages, model=model, base_url=base_url,
                     api_key=api_key, timeout=timeout, max_tokens=2000)
        raw = _parse_json_array(text)
    topics: list[dict[str, Any]] = []
    for index, item in enumerate(raw[:8], start=1):
        if not isinstance(item, dict):
            continue
        topic_id = item.get("topic_id") or f"t{index}"
        theme = item.get("theme")
        if not isinstance(theme, str) or not theme.strip():
            continue
        def _as_str_list(value: Any) -> list[str]:
            if isinstance(value, list):
                return [str(v) for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
            return []
        topics.append({
            "topic_id": str(topic_id),
            "theme": theme.strip()[:80],
            "key_points": _as_str_list(item.get("key_points")),
            "extension_angles": _as_str_list(item.get("extension_angles")),
            "suggested_links": _as_str_list(item.get("suggested_links")),
            "risk": str(item.get("risk") or "无")[:300],
            "recommend": bool(item.get("recommend", True)),
        })
    return topics


def generate_post_text(*, topic: dict[str, Any], account_persona: str = "",
                       post_style: str = "", recent_posts: list[str] | None = None,
                       suggested_link: str | None = None, language: str = "en",
                       base_url: str = DEFAULT_BASE_URL, model: str = DEFAULT_MODEL,
                       api_key: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                       extra_instruction: str = "") -> dict[str, str]:
    """Generate an X post body from a topic. Returns {text, rationale, link}.
    language: "en" (default, native English X tone) or "zh"."""
    api_key = api_key or os.getenv("WORKFLOW_LLM_KEY", "")
    recent = recent_posts or []
    recent_block = "\n".join(f"- {p}" for p in recent[-8:]) or "(none)"
    link_note = (f"Assigned link (may be appended at the end): {suggested_link}\n"
                 if suggested_link else "No link by default; only include the assigned link if the task allows.\n")
    angles = topic.get("extension_angles") or []
    angles_block = "\n".join(f"- {a}" for a in angles[:6]) or "(none)"
    lang_name = "English" if language != "zh" else "中文"
    prompt = (
        f"You are a social media ghostwriter. Write ONE X (Twitter) post in {lang_name} "
        "based on the topic below. Output JSON: {text, rationale, link}.\n"
        "Tone rules (IMPORTANT):\n"
        "1. Sound like a real person tweeting, not a press release: first person, casual, "
        "specific, conversational. It should read like something the account owner actually wrote.\n"
        "2. No corporate or bookish tone. Never use phrases like 'in today's fast-paced world', "
        "'game-changer', 'delve into', '近日', '本文', '显著', '赋能'.\n"
        "3. Lead with the core point in the first line. Hard limit 280 characters.\n"
        "4. Base everything on the topic's key points; do not invent data, events, or quotes.\n"
        "5. Do not copy source sentences verbatim. 1-2 hashtags max, none is fine.\n"
        "6. Must not be highly similar to the recent posts listed below.\n"
        "7. " + link_note +
        "8. The link field holds the link actually used (empty string if none). "
        "rationale may be written in Chinese for the operator.\n"
        f"Topic: {topic.get('theme','')}\n"
        f"Key points: {', '.join(topic.get('key_points') or []) or 'none'}\n"
        f"Angles:\n{angles_block}\n"
        f"Account persona: {account_persona or 'a knowledgeable, friendly operator'}\n"
        f"Style: {post_style or 'casual, informative, natural'}\n"
        f"Recent posts (avoid similarity):\n{recent_block}\n"
        f"Extra instruction: {extra_instruction or 'none'}"
    )
    text = _chat([{"role": "user", "content": prompt}], model=model, base_url=base_url,
                 api_key=api_key, timeout=timeout)
    result = _parse_json_object(text)
    if not isinstance(result.get("text"), str) or not result["text"].strip():
        raise LLMError("llm_invalid_response", "LLM post draft had no text")
    if len(result["text"]) > 280:
        result["text"] = result["text"][:280]
    if not isinstance(result.get("rationale"), str):
        result["rationale"] = ""
    link = result.get("link")
    if not isinstance(link, str):
        link = ""
    return {"text": result["text"].strip(), "rationale": result["rationale"].strip(),
            "link": link.strip()}
