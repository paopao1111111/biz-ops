import os
import json
import uuid
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path


ADAPTER_DIR = Path(__file__).resolve().parent
ASSETS_DIR = ADAPTER_DIR / "assets"
LAYOUTS_DIR = ASSETS_DIR / "page_layouts"
PROJECT_DIR = ADAPTER_DIR.parents[1]
STAGE_RUNS_DIR = PROJECT_DIR / "storage" / "stage_runs"
STAGE_ASSETS_DIR = PROJECT_DIR / "storage" / "stage_assets"
WP_UPLOADS_BASE_URL = "https://www.iweaver.ai/wp-content/uploads"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stage_path(state_id):
    safe = str(state_id or "").strip()
    if not safe or "/" in safe or ".." in safe:
        raise ValueError("valid state_id is required")
    return STAGE_RUNS_DIR / f"{safe}.json"


def _stage_asset_path(state_id, filename):
    safe_state = str(state_id or "").strip()
    safe_name = str(filename or "").strip()
    if not safe_state or "/" in safe_state or ".." in safe_state:
        raise ValueError("valid state_id is required")
    if not safe_name or "/" in safe_name or ".." in safe_name:
        raise ValueError("valid filename is required")
    return STAGE_ASSETS_DIR / safe_state / safe_name


def _new_state_id(prefix):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"


def _save_state(state):
    STAGE_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    path = _stage_path(state["state_id"])
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return state


def _load_state(state_id):
    path = _stage_path(state_id)
    if not path.is_file():
        raise ValueError(f"state not found: {state_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _public_state(state, extra=None):
    result = {
        "success": True,
        "state_id": state.get("state_id"),
        "workflow": state.get("workflow"),
        "stage": state.get("stage"),
        "next_stage": state.get("next_stage", ""),
        "created_at": state.get("created_at"),
        "updated_at": state.get("updated_at"),
        "summary": state.get("summary", {}),
    }
    if extra:
        result.update(extra)
    return result


def _get_state_id(payload):
    state_id = str((payload or {}).get("state_id") or "").strip()
    if not state_id:
        raise ValueError("state_id is required")
    return state_id


def _extract_wp_page_id(wp_result):
    if not isinstance(wp_result, dict):
        return None
    response = wp_result.get("response")
    if isinstance(response, dict):
        if response.get("id"):
            return response.get("id")
        page_create = response.get("page_create")
        if isinstance(page_create, dict) and page_create.get("id"):
            return page_create.get("id")
    return wp_result.get("id")


def _slugify_words(*parts, fallback="use-case-image", max_words=7):
    raw_text = " ".join(str(part).strip() for part in parts if str(part or "").strip())
    normalized = unicodedata.normalize("NFKD", raw_text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[a-zA-Z0-9]+", ascii_text.lower())
    words = words[: int(max_words or 7)]
    return "-".join(words) or fallback


def _content_month_parts(state):
    created_at = str(state.get("created_at") or _now())
    try:
        dt = datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        dt = datetime.now()
    return dt.strftime("%Y"), dt.strftime("%m")


def _page_input_html(payload):
    input_html = str((payload or {}).get("input_html") or "")
    layout = str((payload or {}).get("layout") or "")
    if layout and not input_html and layout != "none":
        tpl_path = LAYOUTS_DIR / f"{layout}.html"
        if tpl_path.is_file():
            input_html = tpl_path.read_text(encoding="utf-8")
    return input_html


def _progress(ctx, payload, message):
    task_id = (payload or {}).get("_task_id")
    if task_id and getattr(ctx, "store", None):
        ctx.store.update_task(task_id, progress=message)


def register(registry):
    registry.tool("case_analysis_day", run_case_analysis_day, "Run wp_tes case analysis for one date")
    registry.tool("blog_generate", run_blog_generate, "Generate and publish a wp_tes blog draft")
    registry.tool("page_generate", run_page_generate, "Generate and publish a wp_tes page draft")
    registry.tool("audit_post", run_audit_post, "Audit a WordPress post/page")
    registry.tool("rewrite_post", run_rewrite_post, "Audit then rewrite a WordPress post/page")
    registry.tool("apply_rewrite_post", run_apply_rewrite_post, "Apply a rewrite result to a WordPress post/page")

    registry.tool("page_stage_init", run_page_stage_init, "Initialize staged SEO page generation state")
    registry.tool("page_stage_search", run_page_stage_search, "SEO page stage: SerpAPI search")
    registry.tool("page_stage_scrape", run_page_stage_scrape, "SEO page stage: Firecrawl scrape")
    registry.tool("page_stage_research", run_page_stage_research, "SEO page stage: LLM research")
    registry.tool("page_stage_generate_json", run_page_stage_generate_json, "SEO page stage: generate page JSON")
    registry.tool("page_stage_images", run_page_stage_images, "SEO page stage: enqueue async use-case image generation")
    registry.tool("page_stage_render", run_page_stage_render, "SEO page stage: render HTML")
    registry.tool("page_stage_publish", run_page_stage_publish, "SEO page stage: publish WordPress page")

    registry.tool("blog_stage_init", run_blog_stage_init, "Initialize staged blog generation state")
    registry.tool("blog_stage_generate", run_blog_stage_generate, "Blog stage: generate blog JSON/content")
    registry.tool("blog_stage_publish", run_blog_stage_publish, "Blog stage: assemble and publish WordPress post")

    registry.tool("case_stage_plan", run_case_stage_plan, "Plan case analysis date list")
    registry.tool("case_stage_day", run_case_stage_day, "Run one case analysis day synchronously")

    registry.workflow("case_analysis_day", "case_analysis_day", "Analyze one day of user cases", mode="async")
    registry.workflow("blog_generate", "blog_generate", "Generate one blog post", mode="async")
    registry.workflow("page_generate", "page_generate", "Generate one SEO page", mode="async")
    registry.workflow("audit_post", "audit_post", "Audit one WordPress post/page", mode="async")
    registry.workflow("rewrite_post", "rewrite_post", "Rewrite one audited WordPress post/page", mode="async")
    registry.workflow("apply_rewrite_post", "apply_rewrite_post", "Apply one rewrite result to WordPress", mode="async")


def run_case_analysis_day(ctx, payload):
    target_date = str(payload.get("target_date") or payload.get("date") or "").strip()
    if not target_date:
        return {"success": False, "error": "target_date is required"}
    _configure_runtime_env(ctx)
    from .runtime.case_analysis import run_case_analysis
    result = run_case_analysis(target_date=target_date)
    return {"success": bool(result.get("success", True)), "date": target_date, "result": result}


def run_blog_generate(ctx, payload):
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return {"success": False, "error": "keyword is required"}
    _configure_runtime_env(ctx)
    from .runtime.blog_generat import generate_blog, assemble_and_publish

    _progress(ctx, payload, "blog_generating_content")
    blog_result = generate_blog(keyword)
    if not blog_result.get("success"):
        return blog_result
    blog_data = blog_result.get("blog") or {}
    category_ids = _parse_int_list(payload.get("wp_category_ids"))
    tag_ids = _parse_int_list(payload.get("wp_tag_ids"))
    _progress(ctx, payload, "blog_publishing_wordpress")
    publish_result = assemble_and_publish(
        blog_data,
        wp_status=str(payload.get("wp_status") or "draft"),
        wp_tag_ids=tag_ids,
        wp_category_ids=category_ids,
    )
    return {
        "success": True,
        "keyword": keyword,
        "title": publish_result.get("title") or blog_data.get("title", ""),
        "slug": publish_result.get("slug") or blog_data.get("slug", ""),
        "wp": publish_result.get("wp") or publish_result,
        "seo": publish_result.get("seo", {}),
        "raw": publish_result,
    }


def run_page_generate(ctx, payload):
    keyword = str(payload.get("keyword") or payload.get("keyword1") or "").strip()
    title = str(payload.get("wp_title") or payload.get("title") or keyword).strip()
    slug = str(payload.get("wp_slug") or payload.get("slug") or "").strip()
    if not keyword:
        return {"success": False, "error": "keyword is required"}
    if not slug:
        return {"success": False, "error": "wp_slug is required"}

    _configure_runtime_env(ctx)
    from .runtime import html_generat as h

    input_html = str(payload.get("input_html") or "")
    layout = str(payload.get("layout") or "")
    if layout and not input_html and layout != "none":
        _progress(ctx, payload, "loading_page_layout")
        tpl_path = LAYOUTS_DIR / f"{layout}.html"
        if tpl_path.is_file():
            input_html = tpl_path.read_text(encoding="utf-8")

    _progress(ctx, payload, "page_generating_content")
    generated = h.html_text(
        keyword=keyword,
        input_2_html=input_html,
        use_case_image_list={},
        page_slug=slug,
        progress_cb=lambda message: _progress(ctx, payload, message),
        generate_images=False,
    )
    state = {
        "state_id": _new_state_id("page"),
        "workflow": "page_generate",
        "stage": "render",
        "next_stage": "publish",
        "created_at": _now(),
        "payload": {
            "keyword": keyword,
            "title": title,
            "slug": slug,
            "layout": layout,
            "wp_status": str(payload.get("wp_status") or "draft"),
            "page_type": str(payload.get("page_type") or "AI Summary"),
            "agent_page_img": str(payload.get("agent_page_img") or ""),
            "agent_page_desc": str(payload.get("agent_page_desc") or ""),
            "input_html": input_html,
        },
        "content_data": generated.get("content", {}),
        "seo": generated.get("seo", {}),
        "image_prompts": _build_use_case_image_jobs(h, generated.get("content", {}), keyword),
        "summary": {"keyword": keyword, "title": title, "slug": slug},
    }
    state["image_urls"], state["image_files"] = _build_deterministic_use_case_image_urls(state)
    rendered = h.build_html_from_data(
        state["content_data"],
        keyword=keyword,
        input_2_html=input_html,
        use_case_image=state["image_urls"],
    )
    state["html"] = rendered["html"]
    _progress(ctx, payload, "page_publishing_wordpress")
    wp_result = h.post_to_wp(
        complete_html=state["html"],
        title=title,
        slug=slug,
        status=str(payload.get("wp_status") or "draft"),
        wp_tag_ids=_page_tag(payload.get("page_type")),
        agent_page_img=str(payload.get("agent_page_img") or ""),
        agent_page_desc=str(payload.get("agent_page_desc") or ""),
        seo_data=generated.get("seo", {}),
    )
    state["wp_result"] = wp_result
    state["wp_id"] = _extract_wp_page_id(wp_result)
    state["stage"] = "publish"
    state["next_stage"] = ""
    state["image_task_id"] = ""
    state["image_task_status"] = "deterministic_url"
    state["summary"] = {
        **state.get("summary", {}),
        "wp_success": bool(wp_result.get("success", True)),
        "wp_id": state["wp_id"],
        "image_count": sum(1 for value in state["image_urls"].values() if value),
        "image_urls": state["image_urls"],
        "image_files": state["image_files"],
        "image_task_status": "deterministic_url",
        "image_prompt_count": len(state["image_prompts"]),
    }
    _save_state(state)
    _progress(ctx, payload, "page_publish_finished" if wp_result.get("success", True) else "page_publish_failed")
    return {
        "success": bool(wp_result.get("success", True)),
        "title": title,
        "slug": slug,
        "wp": wp_result,
        "seo": generated.get("seo", {}),
        "state_id": state["state_id"],
        "image_urls": state["image_urls"],
        "image_files": state["image_files"],
        "image_task_status": "deterministic_url",
    }


def run_audit_post(ctx, payload):
    post_id = payload.get("post_id")
    if not post_id:
        return {"success": False, "error": "post_id is required"}
    _configure_runtime_env(ctx)
    from .runtime.content_audit import audit_post
    result = audit_post(int(post_id), content_type=str(payload.get("content_type") or "post"))
    return {"success": True, "result": result}


def run_rewrite_post(ctx, payload):
    post_id = payload.get("post_id")
    if not post_id:
        return {"success": False, "error": "post_id is required"}
    _configure_runtime_env(ctx)
    from .runtime.content_audit import audit_post, rewrite_post
    audit = audit_post(int(post_id), content_type=str(payload.get("content_type") or "post"))
    failed = audit.get("failed_indicators") or []
    if not failed:
        return {"success": True, "message": "All indicators passed, no rewrite needed", "audit": audit}
    rewritten = rewrite_post(
        original_html=audit.get("content_html", ""),
        failed_indicators=failed,
        focus_keywords=audit.get("focus_keywords", ""),
        article_title=audit.get("title", ""),
    )
    return {"success": bool(rewritten.get("success")), "audit": audit, "rewrite": rewritten}


def _rewrite_from_task(ctx, task_id):
    if not getattr(ctx, "store", None):
        return None, {"success": False, "error": "task store is not available"}
    task = ctx.store.get_task(str(task_id))
    if not task:
        return None, {"success": False, "error": f"task not found: {task_id}"}
    if task.get("status") not in ("success", "completed"):
        return None, {
            "success": False,
            "error": f"task is not complete: {task.get('status')}",
            "progress": task.get("progress", ""),
        }
    result = task.get("result") or {}
    if not isinstance(result, dict) or not result.get("success"):
        return None, {"success": False, "error": "task result is not successful", "task": task}
    return result, None


def run_apply_rewrite_post(ctx, payload):
    """Apply generated rewrite content and SEO metadata to WordPress.

    Accepts either:
    - task_id from rewrite_post, or
    - post_id + content + optional SEO fields.
    """
    _configure_runtime_env(ctx)
    from .runtime.content_audit import publish_rewrite

    payload = payload or {}
    task_result = None
    task_id = str(payload.get("task_id") or "").strip()
    if task_id:
        task_result, error = _rewrite_from_task(ctx, task_id)
        if error:
            return error

    audit = (task_result or {}).get("audit") or {}
    rewrite_result = ((task_result or {}).get("rewrite") or {}).get("rewrite") or {}

    post_id = payload.get("post_id") or audit.get("post_id")
    if not post_id:
        return {"success": False, "error": "post_id is required, or provide a rewrite task_id"}

    content_type = str(payload.get("content_type") or audit.get("content_type") or "post")
    mode = str(payload.get("mode") or "update")
    if mode not in ("update", "draft"):
        return {"success": False, "error": "mode must be 'update' or 'draft'"}

    new_content = payload.get("content") or rewrite_result.get("content")
    if not new_content:
        return {"success": False, "error": "rewrite content is required"}

    new_seo = payload.get("seo")
    if not isinstance(new_seo, dict):
        new_seo = {
            "meta_title": payload.get("meta_title") or rewrite_result.get("meta_title"),
            "meta_description": payload.get("meta_description") or rewrite_result.get("meta_description"),
            "focus_keywords": payload.get("focus_keywords") or rewrite_result.get("focus_keywords"),
        }

    new_title = str(payload.get("new_title") or payload.get("title") or "").strip() or None

    try:
        publish = publish_rewrite(
            int(post_id),
            new_content,
            new_seo,
            mode=mode,
            content_type=content_type,
            new_title=new_title,
        )
    except Exception as exc:
        return {"success": False, "error": f"failed to apply rewrite: {exc}"}

    return {
        "success": True,
        "post_id": int(post_id),
        "content_type": content_type,
        "mode": mode,
        "title_updated": bool(new_title),
        "seo_updated": bool(new_seo),
        "publish": publish,
        "message": "Rewrite applied to WordPress.",
    }


def build_dates(start_date, end_date):
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        start, end = end, start
    dates = []
    current = start
    while current <= end:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def run_case_analysis_days(ctx, dates, max_concurrency=3):
    def worker(date):
        return run_case_analysis_day(ctx, {"target_date": date})
    return ctx.runner.run_parallel(dates, worker, max_concurrency=max_concurrency)




def run_page_stage_init(ctx, payload):
    keyword = str(payload.get("keyword") or payload.get("keyword1") or "").strip()
    title = str(payload.get("wp_title") or payload.get("title") or keyword).strip()
    slug = str(payload.get("wp_slug") or payload.get("slug") or "").strip()
    if not keyword:
        return {"success": False, "error": "keyword is required"}
    if not slug:
        return {"success": False, "error": "wp_slug/slug is required"}
    state = {
        "state_id": _new_state_id("page"),
        "workflow": "page_generate_staged",
        "stage": "init",
        "next_stage": "page_stage_search",
        "created_at": _now(),
        "payload": {
            "keyword": keyword,
            "title": title,
            "slug": slug,
            "layout": str(payload.get("layout") or ""),
            "wp_status": str(payload.get("wp_status") or "draft"),
            "page_type": str(payload.get("page_type") or "AI Summary"),
            "agent_page_img": str(payload.get("agent_page_img") or ""),
            "agent_page_desc": str(payload.get("agent_page_desc") or ""),
            "input_html": _page_input_html(payload),
        },
        "summary": {"keyword": keyword, "title": title, "slug": slug},
    }
    _save_state(state)
    return _public_state(state, {"message": "Initialized page stage state. Run page_stage_search next."})


def run_page_stage_search(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    keyword = state["payload"]["keyword"]
    urls = h._local_search_serpapi(keyword, num=int(payload.get("num") or 5))
    if not urls:
        detail = getattr(h, "LAST_SERPAPI_ERROR", "")
        error = f"SerpAPI returned no results for: {keyword}"
        if detail:
            error = f"{error}. Diagnostic: {detail}"
        return {"success": False, "state_id": state["state_id"], "stage": "search", "error": error}
    state["urls"] = urls
    state["stage"] = "search"
    state["next_stage"] = "page_stage_scrape"
    state["summary"] = {**state.get("summary", {}), "urls_count": len(urls), "urls": urls[:5]}
    _save_state(state)
    return _public_state(state, {"urls": urls, "message": f"Search completed: {len(urls)} URLs."})


def run_page_stage_scrape(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    urls = state.get("urls") or []
    if not urls:
        return {"success": False, "state_id": state["state_id"], "stage": "scrape", "error": "No URLs in state. Run page_stage_search first."}
    keyword = state["payload"]["keyword"]
    max_research_urls = int(payload.get("max_research_urls") or getattr(h, "DEFAULT_RESEARCH_URL_LIMIT", 3) or 3)
    min_research_pages = min(int(payload.get("min_research_pages") or getattr(h, "MIN_RESEARCH_PAGES", 2) or 2), len(urls))
    research_urls = h._select_research_urls(keyword, urls, limit=max_research_urls)
    pages = h._local_scrape_all(research_urls, max_workers=int(payload.get("max_workers") or 5))
    scraped_count = sum(1 for page in pages if str(page).strip())
    if scraped_count < min_research_pages and len(research_urls) < len(urls):
        selected = set(research_urls)
        fallback_urls = [url for url in urls if url not in selected]
        fallback_pages = h._local_scrape_all(fallback_urls, max_workers=min(2, int(payload.get("max_workers") or 5)))
        for url, page in zip(fallback_urls, fallback_pages):
            if scraped_count >= min_research_pages:
                break
            research_urls.append(url)
            pages.append(page)
            if str(page).strip():
                scraped_count += 1
    state["pages"] = pages
    state["candidate_urls"] = urls
    state["research_urls"] = research_urls
    state["stage"] = "scrape"
    state["next_stage"] = "page_stage_research"
    state["summary"] = {
        **state.get("summary", {}),
        "scraped_count": scraped_count,
        "total_urls": len(research_urls),
        "candidate_urls_count": len(urls),
        "research_urls_count": len(research_urls),
        "research_urls": research_urls,
    }
    _save_state(state)
    return _public_state(state, {"message": f"Scrape completed: {scraped_count}/{len(research_urls)} selected pages scraped from {len(urls)} candidates."})


def run_page_stage_research(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    keyword = state["payload"]["keyword"]
    pages = state.get("pages") or []
    if not pages:
        return {"success": False, "state_id": state["state_id"], "stage": "research", "error": "No scraped pages in state. Run page_stage_scrape first."}
    context = h._local_merge_pages(keyword, pages)
    prompt = h.format_prompt(h.SEO_RESEARCH_PROMPT, keyword=keyword, context=context)
    prompt = h._enforce_json_output(prompt)
    r = h.call_coze_llm(h.NEW_COZE_WORKFLOW_ID, {"prompt": prompt})
    if not r.get("success"):
        return {"success": False, "state_id": state["state_id"], "stage": "research", "error": f"SEO research LLM failed: {r.get('error')}"}
    research_report = r.get("output", "")
    state["research_report"] = research_report
    state["stage"] = "research"
    state["next_stage"] = "page_stage_generate_json"
    state["summary"] = {**state.get("summary", {}), "research_chars": len(research_report)}
    _save_state(state)
    return _public_state(state, {"research_preview": research_report[:1000], "message": "LLM research completed."})


def run_page_stage_generate_json(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    keyword = state["payload"]["keyword"]
    research_report = state.get("research_report") or ""
    if not research_report:
        return {"success": False, "state_id": state["state_id"], "stage": "generate_json", "error": "No research_report in state. Run page_stage_research first."}
    prompt = h.format_prompt(h.SEO_GENERATE_PROMPT, keyword=keyword, context=research_report)
    prompt = h._enforce_json_output(prompt)
    r = h.call_coze_llm(h.NEW_COZE_WORKFLOW_ID, {"prompt": prompt})
    if not r.get("success"):
        return {"success": False, "state_id": state["state_id"], "stage": "generate_json", "error": f"SEO generate LLM failed: {r.get('error')}"}
    raw = h.sanitize_iweaver_urls(r.get("output", ""))
    data = h.sanitize_generated_content(json.loads(h.clean_output_content(raw)))
    seo_payload = h.build_seo_payload(data, keyword)
    state["content_data"] = data
    state["seo"] = seo_payload
    state["stage"] = "generate_json"
    state["next_stage"] = "page_stage_images"
    use_cases = ((data.get("use_cases") or {}).get("cases") or []) if isinstance(data, dict) else []
    state["summary"] = {**state.get("summary", {}), "h1": (data.get("main") or {}).get("title_H1", ""), "use_cases_count": len(use_cases), "seo": seo_payload}
    _save_state(state)
    return _public_state(state, {"seo": seo_payload, "use_cases_count": len(use_cases), "message": "Page JSON generated and parsed."})


def _build_use_case_image_jobs(h, content_data, keyword):
    cases = (((content_data or {}).get("use_cases") or {}).get("cases") or [])[:4]
    jobs = []
    for index, case in enumerate(cases, start=1):
        jobs.append({
            "image_key": f"image_url_{index}",
            "index": index,
            "title": case.get("title", "") if isinstance(case, dict) else "",
            "prompt": h.build_use_case_image_prompt(keyword, case, index),
        })
    return jobs


def _build_deterministic_use_case_image_urls(state):
    data = state.get("content_data") or {}
    cases = (((data or {}).get("use_cases") or {}).get("cases") or [])[:4]
    page_title = ((state.get("payload") or {}).get("title") or (state.get("payload") or {}).get("keyword") or "").strip()
    year, month = _content_month_parts(state)
    image_urls = {}
    image_files = []
    for index, case in enumerate(cases, start=1):
        use_case_title = case.get("title", "") if isinstance(case, dict) else ""
        slug = _slugify_words(page_title, use_case_title, fallback=f"use-case-{index}", max_words=7)
        filename = f"{slug}.webp"
        image_key = f"image_url_{index}"
        image_urls[image_key] = f"{WP_UPLOADS_BASE_URL}/{year}/{month}/{filename}"
        image_files.append({
            "image_key": image_key,
            "index": index,
            "filename": filename,
            "url": image_urls[image_key],
            "page_title": page_title,
            "use_case_title": use_case_title,
            "year": year,
            "month": month,
        })
    for index in range(len(cases) + 1, 5):
        image_urls[f"image_url_{index}"] = ""
    return image_urls, image_files


def _run_page_use_case_image_task(ctx, task_id, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h

    state = _load_state(_get_state_id(payload))
    data = state.get("content_data") or {}
    cases = (((data or {}).get("use_cases") or {}).get("cases") or [])[:4]
    image_files = state.get("image_files") or []
    keyword = state["payload"]["keyword"]
    page_slug = state["payload"].get("slug", "")

    if not cases:
        return {"success": False, "state_id": state["state_id"], "error": "No use cases found in stage state."}

    file_by_index = {
        int(item.get("index")): item
        for item in image_files
        if isinstance(item, dict) and str(item.get("index") or "").isdigit()
    }
    results = {}

    state["image_task_status"] = "running"
    state["image_task_id"] = task_id
    state["image_upload_results"] = {}
    state["summary"] = {
        **state.get("summary", {}),
        "image_task_status": "running",
        "image_task_id": task_id,
    }
    _save_state(state)

    def worker(item):
        index, case = item
        target = file_by_index.get(index, {})
        target_filename = target.get("filename", "")
        target_url = target.get("url", "")
        image_index, uploaded = h.generate_one_use_case_image(
            keyword,
            page_slug,
            index,
            case,
            target_filename=target_filename,
        )
        source_url = str(uploaded.get("source_url") or "")
        return image_index, {
            "success": True,
            "image_key": f"image_url_{image_index}",
            "index": image_index,
            "filename": uploaded.get("filename") or target_filename,
            "target_url": target_url,
            "source_url": source_url,
            "media_id": uploaded.get("id"),
            "url_matches_target": bool(target_url and source_url.split("?", 1)[0] == target_url),
        }

    max_workers = max(1, min(int(getattr(h, "USE_CASE_IMAGE_MAX_WORKERS", 4) or 4), len(cases)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        future_map = {
            pool.submit(worker, (index, case)): index
            for index, case in enumerate(cases, start=1)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                image_index, result = future.result()
            except Exception as exc:
                image_index = index
                result = {
                    "success": False,
                    "image_key": f"image_url_{index}",
                    "index": index,
                    "error": str(exc),
                    "target_url": (file_by_index.get(index) or {}).get("url", ""),
                }
            results[f"image_url_{image_index}"] = result
            state = _load_state(_get_state_id(payload))
            state["image_upload_results"] = {**(state.get("image_upload_results") or {}), **results}
            completed = len(state["image_upload_results"])
            state["summary"] = {
                **state.get("summary", {}),
                "image_upload_completed": completed,
                "image_upload_total": len(cases),
            }
            _save_state(state)
            if getattr(ctx, "store", None):
                ctx.store.update_task(task_id, progress=f"use_case_images {completed}/{len(cases)}")

    failed = [item for item in results.values() if not item.get("success")]
    final_status = "completed" if not failed else ("partial_failed" if len(failed) < len(results) else "failed")
    state = _load_state(_get_state_id(payload))
    state["image_task_status"] = final_status
    state["image_upload_results"] = results
    state["summary"] = {
        **state.get("summary", {}),
        "image_task_status": final_status,
        "image_task_id": task_id,
        "image_upload_completed": len(results) - len(failed),
        "image_upload_failed": len(failed),
        "image_upload_total": len(cases),
    }
    _save_state(state)
    return {
        "success": not failed,
        "state_id": state["state_id"],
        "image_task_status": final_status,
        "completed": len(results) - len(failed),
        "failed": len(failed),
        "results": results,
    }


def _run_blog_cover_image_task(ctx, task_id, payload):
    _configure_runtime_env(ctx)
    from .runtime import blog_generat as b

    state = _load_state(_get_state_id(payload))
    title_or_topic = str(payload.get("title") or state.get("payload", {}).get("keyword") or "").strip()
    if not title_or_topic:
        return {"success": False, "state_id": state["state_id"], "error": "No title or keyword available for cover image."}

    state["cover_image_task_status"] = "running"
    state["cover_image_task_id"] = task_id
    state["summary"] = {
        **state.get("summary", {}),
        "cover_image_task_status": "running",
        "cover_image_task_id": task_id,
    }
    _save_state(state)
    if getattr(ctx, "store", None):
        ctx.store.update_task(task_id, progress="cover_image_generating")

    cover_b64 = b._local_generate_cover_image(title_or_topic)
    state = _load_state(_get_state_id(payload))
    if not cover_b64:
        state["cover_image_task_status"] = "failed"
        state["summary"] = {
            **state.get("summary", {}),
            "cover_image_task_status": "failed",
        }
        _save_state(state)
        return {"success": False, "state_id": state["state_id"], "error": "Cover image generation returned empty."}

    cover_path = _stage_asset_path(state["state_id"], "blog-cover.webp")
    cover_path.parent.mkdir(parents=True, exist_ok=True)
    cover_path.write_bytes(b.convert_image_source_to_webp_bytes(cover_b64))
    state["cover_image_file"] = str(cover_path)
    state.pop("cover_image_base64", None)
    blog_data = state.get("blog_data")
    if isinstance(blog_data, dict):
        blog_data["cover_image_file"] = str(cover_path)
        blog_data.pop("cover_image_base64", None)
        state["blog_data"] = blog_data
    state["cover_image_task_status"] = "completed"
    state["summary"] = {
        **state.get("summary", {}),
        "cover_image_task_status": "completed",
        "cover_image_file": str(cover_path),
    }
    _save_state(state)
    if getattr(ctx, "store", None):
        ctx.store.update_task(task_id, progress="cover_image_generated")
    return {"success": True, "state_id": state["state_id"], "cover_image_task_status": "completed"}


def run_page_stage_images(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    data = state.get("content_data")
    if not isinstance(data, dict):
        return {"success": False, "state_id": state["state_id"], "stage": "images", "error": "No content_data in state. Run page_stage_generate_json first."}
    keyword = state["payload"]["keyword"]
    provided_images = h.normalize_use_case_image_map(payload.get("provided_images") or {})
    image_prompts = _build_use_case_image_jobs(h, data, keyword)

    if all(provided_images.values()):
        image_status = "provided"
        state["image_urls"] = provided_images
        image_files = []
    else:
        image_status = "queued"
        state["image_urls"], image_files = _build_deterministic_use_case_image_urls(state)

    state["image_prompts"] = image_prompts
    state["image_files"] = image_files
    state["image_task_id"] = ""
    state["image_task_status"] = image_status
    state["image_upload_results"] = {}
    state["stage"] = "images"
    state["next_stage"] = "page_stage_render"
    state["summary"] = {
        **state.get("summary", {}),
        "image_count": sum(1 for value in state["image_urls"].values() if value),
        "image_urls": state["image_urls"],
        "image_task_status": image_status,
        "image_prompt_count": len(image_prompts),
        "image_files": image_files,
    }
    _save_state(state)

    image_task_id = ""
    if image_status == "queued" and getattr(ctx, "runner", None):
        image_task_id = ctx.runner.submit(
            "page_use_case_images",
            {"state_id": state["state_id"]},
            lambda task_id, task_payload: _run_page_use_case_image_task(ctx, task_id, task_payload),
        )
        state["image_task_id"] = image_task_id
        state["summary"] = {
            **state.get("summary", {}),
            "image_task_id": image_task_id,
            "image_task_status": "queued",
        }
        _save_state(state)

    return _public_state(state, {
        "image_task_status": image_status,
        "image_task_id": image_task_id,
        "image_prompts": image_prompts,
        "image_files": image_files,
        "image_urls": state["image_urls"],
        "message": "Use-case image URLs were generated deterministically; image generation/upload is running asynchronously in parallel.",
    })


def run_page_stage_render(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    data = state.get("content_data")
    if not isinstance(data, dict):
        return {"success": False, "state_id": state["state_id"], "stage": "render", "error": "No content_data in state. Run page_stage_generate_json first."}
    rendered = h.build_html_from_data(
        data,
        keyword=state["payload"]["keyword"],
        input_2_html=state["payload"].get("input_html", ""),
        use_case_image=state.get("image_urls") or {},
    ) if hasattr(h, "build_html_from_data") else None
    if rendered is None:
        # Fallback: use the existing html_text only if caller explicitly accepts rerun; this avoids silently rerunning search/LLM.
        return {"success": False, "state_id": state["state_id"], "stage": "render", "error": "html_generat.build_html_from_data is not available yet; render stage needs extraction from html_text."}
    state["html"] = rendered["html"] if isinstance(rendered, dict) else rendered
    state["seo"] = (rendered.get("seo") if isinstance(rendered, dict) else state.get("seo")) or state.get("seo", {})
    state["stage"] = "render"
    state["next_stage"] = "page_stage_publish"
    state["summary"] = {**state.get("summary", {}), "html_chars": len(state["html"])}
    _save_state(state)
    return _public_state(state, {"html_chars": len(state["html"]), "message": "HTML rendered."})


def run_page_stage_publish(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime import html_generat as h
    state = _load_state(_get_state_id(payload))
    html = state.get("html") or ""
    if not html:
        return {"success": False, "state_id": state["state_id"], "stage": "publish", "error": "No rendered html in state. Run page_stage_render first."}
    pp = state["payload"]
    wp_result = h.post_to_wp(
        complete_html=html,
        title=pp.get("title") or pp.get("keyword"),
        slug=pp.get("slug"),
        status=str(payload.get("wp_status") or pp.get("wp_status") or "draft"),
        wp_tag_ids=_page_tag(pp.get("page_type")),
        agent_page_img=pp.get("agent_page_img", ""),
        agent_page_desc=pp.get("agent_page_desc", ""),
        seo_data=state.get("seo", {}),
    )
    wp_id = _extract_wp_page_id(wp_result)
    state["wp_result"] = wp_result
    if wp_id:
        state["wp_id"] = wp_id

    state["stage"] = "publish"
    state["next_stage"] = ""
    state["summary"] = {
        **state.get("summary", {}),
        "wp_success": bool(wp_result.get("success", True)),
        "wp_id": wp_id,
        "image_task_status": state.get("image_task_status", ""),
    }
    _save_state(state)
    return _public_state(state, {
        "wp": wp_result,
        "image_urls": state.get("image_urls", {}),
        "image_files": state.get("image_files", []),
        "image_task_status": state.get("image_task_status", ""),
        "message": "WordPress page publish completed with deterministic use-case image URLs.",
    })


def run_blog_stage_init(ctx, payload):
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return {"success": False, "error": "keyword is required"}
    state = {
        "state_id": _new_state_id("blog"),
        "workflow": "blog_generate_staged",
        "stage": "init",
        "next_stage": "blog_stage_generate",
        "created_at": _now(),
        "payload": {
            "keyword": keyword,
            "wp_status": str(payload.get("wp_status") or "draft"),
            "wp_category_ids": payload.get("wp_category_ids") or "",
            "wp_tag_ids": payload.get("wp_tag_ids") or "",
        },
        "summary": {"keyword": keyword},
    }
    _save_state(state)
    return _public_state(state, {"message": "Initialized blog stage state. Run blog_stage_generate next."})


def run_blog_stage_generate(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime.blog_generat import generate_blog
    state = _load_state(_get_state_id(payload))
    keyword = state["payload"]["keyword"]

    cover_task_id = state.get("cover_image_task_id") or ""
    if not cover_task_id and getattr(ctx, "runner", None):
        cover_task_id = ctx.runner.submit(
            "blog_cover_image",
            {"state_id": state["state_id"], "title": keyword},
            lambda task_id, task_payload: _run_blog_cover_image_task(ctx, task_id, task_payload),
        )
        state = _load_state(state["state_id"])
        state["cover_image_task_id"] = cover_task_id
        if not state.get("cover_image_task_status"):
            state["cover_image_task_status"] = "queued"
        state["summary"] = {
            **state.get("summary", {}),
            "cover_image_task_id": cover_task_id,
            "cover_image_task_status": state.get("cover_image_task_status", "queued"),
        }
        _save_state(state)

    result = generate_blog(keyword, generate_cover=False)
    if not result.get("success"):
        return {"success": False, "state_id": state["state_id"], "stage": "generate", "error": result.get("error", "blog generation failed"), "raw": result}
    blog_data = result.get("blog") or {}
    state = _load_state(state["state_id"])
    if state.get("cover_image_file"):
        blog_data["cover_image_file"] = state["cover_image_file"]
    elif state.get("cover_image_base64"):
        blog_data["cover_image_base64"] = state["cover_image_base64"]
    state["blog_data"] = blog_data
    state["stage"] = "generate"
    state["next_stage"] = "blog_stage_publish"
    state["summary"] = {**state.get("summary", {}), "title": blog_data.get("title", ""), "slug": blog_data.get("slug", ""), "content_chars": len(blog_data.get("content", "")), "faq_count": len(blog_data.get("faq", []) or []), "cover_image_task_id": cover_task_id, "cover_image_task_status": state.get("cover_image_task_status", "")}
    _save_state(state)
    return _public_state(state, {"title": blog_data.get("title", ""), "slug": blog_data.get("slug", ""), "cover_image_task_id": cover_task_id, "cover_image_task_status": state.get("cover_image_task_status", ""), "message": "Blog content generated. Cover image generation is running in parallel."})


def run_blog_stage_publish(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime.blog_generat import assemble_and_publish
    state = _load_state(_get_state_id(payload))
    blog_data = state.get("blog_data") or {}
    if not blog_data:
        return {"success": False, "state_id": state["state_id"], "stage": "publish", "error": "No blog_data in state. Run blog_stage_generate first."}
    if not blog_data.get("cover_image_file") and blog_data.get("cover_image_base64"):
        from .runtime import blog_generat as b
        cover_path = _stage_asset_path(state["state_id"], "blog-cover.webp")
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(b.convert_image_source_to_webp_bytes(blog_data["cover_image_base64"]))
        blog_data["cover_image_file"] = str(cover_path)
        blog_data.pop("cover_image_base64", None)
        state["cover_image_file"] = str(cover_path)
        state.pop("cover_image_base64", None)
        state["blog_data"] = blog_data
        _save_state(state)
    if not blog_data.get("cover_image_file") and not blog_data.get("cover_image_base64"):
        state = _load_state(state["state_id"])
        cover_file = state.get("cover_image_file", "")
        cover_b64 = state.get("cover_image_base64", "")
        if cover_file:
            blog_data["cover_image_file"] = cover_file
            state["blog_data"] = blog_data
            _save_state(state)
        elif cover_b64:
            from .runtime import blog_generat as b
            cover_path = _stage_asset_path(state["state_id"], "blog-cover.webp")
            cover_path.parent.mkdir(parents=True, exist_ok=True)
            cover_path.write_bytes(b.convert_image_source_to_webp_bytes(cover_b64))
            state["cover_image_file"] = str(cover_path)
            state.pop("cover_image_base64", None)
            blog_data["cover_image_file"] = str(cover_path)
            blog_data.pop("cover_image_base64", None)
            state["blog_data"] = blog_data
            _save_state(state)
        else:
            return {
                "success": False,
                "state_id": state["state_id"],
                "stage": "publish",
                "error": "Cover image is not ready yet; publish was not started. Retry blog_stage_publish after the cover task completes.",
                "cover_image_task_id": state.get("cover_image_task_id", ""),
                "cover_image_task_status": state.get("cover_image_task_status", "unknown"),
            }
    category_ids = _parse_int_list(payload.get("wp_category_ids") or state["payload"].get("wp_category_ids"))
    tag_ids = _parse_int_list(payload.get("wp_tag_ids") or state["payload"].get("wp_tag_ids"))
    try:
        publish_result = assemble_and_publish(
            blog_data,
            wp_status=str(payload.get("wp_status") or state["payload"].get("wp_status") or "draft"),
            wp_tag_ids=tag_ids,
            wp_category_ids=category_ids,
        )
    except Exception as exc:
        state["publish_error"] = str(exc)
        state["summary"] = {
            **state.get("summary", {}),
            "publish_error": str(exc),
        }
        _save_state(state)
        return {
            "success": False,
            "state_id": state["state_id"],
            "stage": "publish",
            "error": str(exc),
            "message": "WordPress blog publish failed before completion; retry is safe because publish is slug-idempotent.",
        }
    state["publish_result"] = publish_result
    state["stage"] = "publish"
    state["next_stage"] = ""
    state["summary"] = {**state.get("summary", {}), "published_slug": publish_result.get("slug"), "seo": publish_result.get("seo", {})}
    _save_state(state)
    return _public_state(state, {"publish": publish_result, "message": "WordPress blog publish completed."})


def run_case_stage_plan(ctx, payload):
    start_date = str(payload.get("start_date") or "").strip()
    end_date = str(payload.get("end_date") or "").strip()
    dates = payload.get("dates")
    if dates:
        if isinstance(dates, str):
            dates = [item.strip() for item in dates.split(",") if item.strip()]
        else:
            dates = [str(item).strip() for item in dates if str(item).strip()]
    elif start_date and end_date:
        dates = build_dates(start_date, end_date)
    else:
        return {"success": False, "error": "dates or start_date/end_date is required"}
    return {"success": True, "dates": dates, "count": len(dates), "next_stage": "case_stage_day", "message": "Run case_stage_day once per date and report after each day."}


def run_case_stage_day(ctx, payload):
    return run_case_analysis_day(ctx, payload)


def _configure_runtime_env(ctx):
    agentos_cfg = ctx.config.get("agentos", {})
    if agentos_cfg.get("base_url"):
        os.environ["AGENTOS_BASE_URL"] = str(agentos_cfg["base_url"])
    if agentos_cfg.get("token"):
        os.environ["AGENTOS_TOKEN"] = str(agentos_cfg["token"])
    if agentos_cfg.get("default_workflow_id"):
        os.environ["NEW_COZE_WORKFLOW_ID"] = str(agentos_cfg["default_workflow_id"])
    os.environ.setdefault("WP_TES_ASSETS_DIR", str(ASSETS_DIR))


def _parse_int_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return [int(item) for item in value if str(item).isdigit()]
    return [int(item.strip()) for item in str(value).split(",") if item.strip().isdigit()]


def _page_tag(page_type):
    tag = {
        "AI Summary": 139,
        "AI Writing": 140,
        "AI Analysis": 141,
        "AI Mind Map": 142,
        "AI Converter": 145,
    }.get(str(page_type or "AI Summary"), 139)
    return [tag]
