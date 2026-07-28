import importlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from core.proxy_policy import patch_requests
from core.agentos_client import AgentOSClient
from core.config import load_config
from core.direct_llm import call_llm as run_llm_direct_first
from core.json_utils import dumps, parse_json_maybe
from core.prompt_registry import PromptRegistry
from core.task_runner import TaskRunner
from core.task_store import TaskStore
from core.tool_context import ToolContext
from core.workflow_registry import WorkflowRegistry


BASE_DIR = Path(__file__).resolve().parent
patch_requests()


def _resolve_path(path):
    raw = Path(str(path))
    return raw if raw.is_absolute() else BASE_DIR / raw


CONFIG = load_config(BASE_DIR / "config.yaml")

mcp = FastMCP(CONFIG.get("server", {}).get("name", "agentos-orchestrator"))

registry = WorkflowRegistry()
prompts = PromptRegistry()
adapter_configs = {}

agentos_cfg = CONFIG.get("agentos", {})
agentos = AgentOSClient(
    base_url=agentos_cfg.get("base_url", "https://agent.xiaoduoai.com"),
    token=agentos_cfg.get("token", ""),
    timeout=agentos_cfg.get("timeout", 300),
    poll_interval=agentos_cfg.get("poll_interval", 3),
)

store_path = _resolve_path(CONFIG.get("task_store", {}).get("path", "./storage/tasks"))
store = TaskStore(store_path)
runner = TaskRunner(store, max_workers=CONFIG.get("concurrency", {}).get("default_max_workers", 3))
ctx = ToolContext(
    config=CONFIG,
    agentos=agentos,
    prompts=prompts,
    registry=registry,
    store=store,
    runner=runner,
    adapter_configs=adapter_configs,
)


def _load_adapters():
    for item in CONFIG.get("adapters", []):
        if not item.get("enabled", True):
            continue
        name = item.get("name")
        adapter_configs[name] = item
        prompt_path = ((item.get("config") or {}).get("prompts_path") or "")
        if prompt_path:
            prompts.load_json(_resolve_path(prompt_path), namespace=name)
        module = importlib.import_module(item["module"])
        module.register(registry)


_load_adapters()


@mcp.tool()
def list_workflows() -> str:
    """List registered workflows and prompt names."""
    return dumps({
        "success": True,
        "workflows": registry.list_workflows(),
        "prompts": prompts.list(),
    })


@mcp.tool()
def start_workflow(workflow_name: str, payload: dict | str = "") -> str:
    """Start a registered workflow as a background task and return task_id."""
    data = _payload(payload)
    workflow = registry.get_workflow(workflow_name)
    if not workflow:
        return dumps({"success": False, "error": f"Unknown workflow: {workflow_name}"})

    duplicate = _find_recent_duplicate_task(workflow_name, data)
    if duplicate:
        return dumps({
            "success": True,
            "task_id": duplicate.get("task_id"),
            "workflow": workflow_name,
            "duplicate": True,
            "message": "A matching task is already running; returning the existing task_id instead of starting another one.",
            "progress": duplicate.get("progress", ""),
            "created_at": duplicate.get("created_at"),
            "updated_at": duplicate.get("updated_at"),
        })

    def run(task_id, task_payload):
        runtime_payload = dict(task_payload or {})
        runtime_payload["_task_id"] = task_id
        return registry.run_operation(ctx, workflow["operation"], runtime_payload)

    task_id = runner.submit(workflow_name, data, run)
    return dumps({"success": True, "task_id": task_id, "workflow": workflow_name})


@mcp.tool()
def get_task_status(task_id: str) -> str:
    """Get task status, progress, and child task summaries."""
    task = store.get_task(task_id)
    if not task:
        return dumps({"success": False, "error": "Task not found"})
    task = _maybe_mark_stale_task(task)
    children = []
    for child_id in task.get("children") or []:
        child = store.get_task(child_id)
        if child:
            child = _maybe_mark_stale_task(child)
            children.append({
                "task_id": child.get("task_id"),
                "type": child.get("type"),
                "status": child.get("status"),
                "progress": child.get("progress", ""),
                "error": child.get("error", ""),
                "heartbeat_at": child.get("heartbeat_at", ""),
                "runtime_seconds": child.get("runtime_seconds", 0),
            })
    return dumps({
        "success": True,
        "task_id": task_id,
        "type": task.get("type"),
        "status": task.get("status"),
        "progress": task.get("progress", ""),
        "error": task.get("error", ""),
        "heartbeat_at": task.get("heartbeat_at", ""),
        "runtime_seconds": task.get("runtime_seconds", 0),
        "children": children,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    })


@mcp.tool()
def get_task_result(task_id: str) -> str:
    """Get full task result."""
    task = store.get_task(task_id)
    if not task:
        return dumps({"success": False, "error": "Task not found"})
    return dumps({"success": True, "task": task})


@mcp.tool()
def list_tasks(limit: int = 50) -> str:
    """List recent tasks."""
    tasks = [_maybe_mark_stale_task(task) for task in store.list_tasks(limit=limit)]
    return dumps({"success": True, "tasks": tasks})


@mcp.tool()
def run_agentos_prompt(prompt_name: str, variables: dict | str = "", workflow_id: str = "", enforce_json: bool = True) -> str:
    """Render a registered prompt and run it through CLIProxy first; AgentOS is fallback only."""
    try:
        rendered = prompts.render(prompt_name, _payload(variables), enforce_json=enforce_json)
    except Exception as exc:
        return dumps({"success": False, "error": str(exc)})
    wid = workflow_id or agentos_cfg.get("default_workflow_id", "")
    result = run_llm_direct_first(rendered, workflow_id=wid, fallback_to_agentos=True)
    return dumps(result)


@mcp.tool()
def start_case_analysis_days(dates: list | str, max_concurrency: int = 3) -> str:
    """Analyze multiple dates in parallel through the wp_tes adapter."""
    date_list = _dates_payload(dates)
    if not date_list:
        return dumps({"success": False, "error": "dates is required"})

    def run(task_id, payload):
        store.update_task(task_id, progress=f"running {len(date_list)} day tasks")
        return _run_case_children(task_id, payload["dates"], payload.get("max_concurrency", max_concurrency))

    task_id = runner.submit("case_analysis_days", {"dates": date_list, "max_concurrency": max_concurrency}, run)
    return dumps({"success": True, "task_id": task_id, "dates": date_list})


@mcp.tool()
def start_case_analysis_range(start_date: str, end_date: str, max_concurrency: int = 3) -> str:
    """Analyze a date range in parallel through the wp_tes adapter."""
    try:
        dates = _build_dates(start_date, end_date)
    except Exception as exc:
        return dumps({"success": False, "error": str(exc)})
    return start_case_analysis_days(dates, max_concurrency=max_concurrency)


@mcp.tool()
def start_generate_blog(
    keyword: str,
    wp_status: str = "draft",
    wp_category_ids: str = "",
    wp_tag_ids: str = "",
    confirm_blog: bool = False,
) -> str:
    """Start wp_tes blog generation as a background workflow. Requires confirm_blog=true to prevent accidental post creation."""
    if not confirm_blog:
        return dumps({
            "success": False,
            "error": "Blog/article generation requires confirm_blog=true. If the user asked for a page, call start_generate_page instead.",
            "requires_confirmation": True,
            "workflow": "blog_generate",
        })
    payload = {
        "keyword": keyword,
        "wp_status": wp_status,
        "wp_category_ids": wp_category_ids,
        "wp_tag_ids": wp_tag_ids,
    }
    return start_workflow("blog_generate", payload)


@mcp.tool()
def start_generate_page(
    keyword: str,
    wp_title: str,
    wp_slug: str,
    layout: str = "",
    wp_status: str = "draft",
    page_type: str = "AI Summary",
) -> str:
    """Start wp_tes SEO page generation as a background workflow."""
    payload = {
        "keyword": keyword,
        "wp_title": wp_title,
        "wp_slug": wp_slug,
        "layout": layout,
        "wp_status": wp_status,
        "page_type": page_type,
    }
    return start_workflow("page_generate", payload)


@mcp.tool()
def start_rewrite_post(post_id: int, content_type: str = "post") -> str:
    """Start wp_tes audit-and-rewrite workflow as a background task."""
    return start_workflow("rewrite_post", {"post_id": post_id, "content_type": content_type})


@mcp.tool()
def dashboard_metrics_schema_probe(payload: dict | str = "") -> str:
    """Probe Superset schema for dashboard metrics."""
    return dumps(registry.run_operation(ctx, "dashboard_metrics_schema_probe", _payload(payload)))


@mcp.tool()
def dashboard_metrics_preview(payload: dict | str = "") -> str:
    """Fetch dashboard metrics without writing storage or Feishu."""
    return dumps(registry.run_operation(ctx, "dashboard_metrics_preview", _payload(payload)))


@mcp.tool()
def dashboard_metrics_daily_update(payload: dict | str = "") -> str:
    """Fetch, persist, and sync dashboard metrics."""
    return dumps(registry.run_operation(ctx, "dashboard_metrics_daily_update", _payload(payload)))


@mcp.tool()
def dashboard_metrics_alert_check(payload: dict | str = "") -> str:
    """Check 6-hour dashboard metric alerts."""
    return dumps(registry.run_operation(ctx, "dashboard_metrics_alert_check", _payload(payload)))


@mcp.tool()
def feishu_sheet_sync(payload: dict | str = "") -> str:
    """Sync dashboard metrics to the configured Feishu sheet."""
    return dumps(registry.run_operation(ctx, "feishu_sheet_sync", _payload(payload)))


@mcp.tool()
def competitor_seo_analyze(payload: dict | str = "") -> str:
    """Run full competitor SEO analysis and generate a markdown report."""
    return dumps(registry.run_operation(ctx, "competitor_seo_analyze", _payload(payload)))


@mcp.tool()
def competitor_seo_crawl(payload: dict | str = "") -> str:
    """Crawl a competitor site and extract SEO data without LLM analysis."""
    return dumps(registry.run_operation(ctx, "competitor_seo_crawl", _payload(payload)))


@mcp.tool()
def iweaver_admin_generate_agent_config(payload: dict | str = "") -> str:
    """Generate iWeaver Admin Agent config from agent_name/business_category without creating it."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_generate_agent_config", _payload(payload)))


@mcp.tool()
def iweaver_admin_create_agent(payload: dict | str = "") -> str:
    """Create an iWeaver Admin Agent draft. Does not publish preview/prod by default."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_create_agent", _payload(payload)))


@mcp.tool()
def iweaver_admin_list_agents(payload: dict | str = "") -> str:
    """List iWeaver Admin Agents."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_list_agents", _payload(payload)))


@mcp.tool()
def iweaver_admin_get_agent(agent_id: str) -> str:
    """Get one iWeaver Admin Agent by agent_id."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_get_agent", {"agent_id": agent_id}))


@mcp.tool()
def iweaver_admin_list_tools(payload: dict | str = "") -> str:
    """List iWeaver Admin Agent tools for toolIds mapping."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_list_tools", _payload(payload)))


@mcp.tool()
def iweaver_admin_list_models(payload: dict | str = "") -> str:
    """List iWeaver Admin Agent bestModel options exposed to the current admin account."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_list_models", _payload(payload)))


@mcp.tool()
def iweaver_admin_publish_preview(payload: dict | str = "") -> str:
    """Publish an iWeaver Admin Agent to preview/test. Requires explicit user approval."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_publish_preview", _payload(payload)))


@mcp.tool()
def iweaver_admin_publish_prod(payload: dict | str = "") -> str:
    """Push an iWeaver Admin Agent to production. Requires explicit user approval."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_publish_prod", _payload(payload)))


@mcp.tool()
def iweaver_admin_sync_rag(payload: dict | str = "") -> str:
    """Sync iWeaver Admin Agent RAG/knowledge metadata. Requires explicit user approval."""
    return dumps(registry.run_operation(ctx, "iweaver_admin_sync_rag", _payload(payload)))


@mcp.tool()
def page_stage_init(payload: dict | str = "") -> str:
    """Initialize staged SEO page generation and return state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_init", _payload(payload)))


@mcp.tool()
def page_stage_search(payload: dict | str = "") -> str:
    """SEO page stage: run SerpAPI search. Returns URLs and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_search", _payload(payload)))


@mcp.tool()
def page_stage_scrape(payload: dict | str = "") -> str:
    """SEO page stage: scrape URLs from state. Returns scrape summary and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_scrape", _payload(payload)))


@mcp.tool()
def page_stage_research(payload: dict | str = "") -> str:
    """SEO page stage: run LLM research from scraped pages. Returns research preview and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_research", _payload(payload)))


@mcp.tool()
def page_stage_generate_json(payload: dict | str = "") -> str:
    """SEO page stage: generate and parse page JSON. Returns SEO/use-case summary and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_generate_json", _payload(payload)))


@mcp.tool()
def page_stage_images(payload: dict | str = "") -> str:
    """SEO page stage: generate/upload use-case images. Returns image URLs and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_images", _payload(payload)))


@mcp.tool()
def page_stage_render(payload: dict | str = "") -> str:
    """SEO page stage: render HTML from generated page JSON. Returns html summary and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_render", _payload(payload)))


@mcp.tool()
def page_stage_publish(payload: dict | str = "") -> str:
    """SEO page stage: publish rendered HTML to WordPress. Returns WP result and state_id."""
    return dumps(registry.run_operation(ctx, "page_stage_publish", _payload(payload)))


@mcp.tool()
def blog_stage_init(payload: dict | str = "") -> str:
    """Initialize staged blog generation and return state_id."""
    return dumps(registry.run_operation(ctx, "blog_stage_init", _payload(payload)))


@mcp.tool()
def blog_stage_generate(payload: dict | str = "") -> str:
    """Blog stage: generate blog content. Returns title/slug summary and state_id."""
    return dumps(registry.run_operation(ctx, "blog_stage_generate", _payload(payload)))


@mcp.tool()
def blog_stage_publish(payload: dict | str = "") -> str:
    """Blog stage: publish generated blog content to WordPress. Returns WP result and state_id."""
    return dumps(registry.run_operation(ctx, "blog_stage_publish", _payload(payload)))


@mcp.tool()
def case_stage_plan(payload: dict | str = "") -> str:
    """Plan case-analysis dates for staged per-day execution."""
    return dumps(registry.run_operation(ctx, "case_stage_plan", _payload(payload)))


@mcp.tool()
def case_stage_day(payload: dict | str = "") -> str:
    """Run one case-analysis day synchronously. Report after each day."""
    return dumps(registry.run_operation(ctx, "case_stage_day", _payload(payload)))

def _build_dates(start_date, end_date):
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


def _run_case_children(parent_id, dates, max_concurrency):
    child_ids = {
        date: store.create_task("case_analysis_day", {"target_date": date}, parent_id=parent_id)
        for date in dates
    }
    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=max(1, int(max_concurrency or 3))) as executor:
        futures = {}
        for date, child_id in child_ids.items():
            store.update_task(child_id, status="running", progress=f"analyzing {date}")
            futures[executor.submit(registry.run_operation, ctx, "case_analysis_day", {"target_date": date})] = (date, child_id)
        for future in as_completed(futures):
            date, child_id = futures[future]
            try:
                result = future.result()
                ok = bool(result.get("success", True))
                status = "success" if ok else "fail"
                error = "" if ok else str(result.get("error", "failed"))
                store.update_task(child_id, status=status, progress="finished", result=result, error=error)
                results.append({"date": date, "task_id": child_id, "result": result})
                if not ok:
                    errors.append({"date": date, "task_id": child_id, "error": error})
            except Exception as exc:
                error = str(exc)
                store.update_task(child_id, status="fail", progress="failed", error=error)
                errors.append({"date": date, "task_id": child_id, "error": error})
                results.append({"date": date, "task_id": child_id, "result": {"success": False, "error": error}})
    results.sort(key=lambda item: item["date"])
    errors.sort(key=lambda item: item["date"])
    return {
        "success": not errors,
        "total": len(dates),
        "succeeded": len(dates) - len(errors),
        "failed": len(errors),
        "results": results,
        "errors": errors,
    }


def _canonical_payload(workflow_name, payload):
    payload = payload or {}
    if workflow_name == "page_generate":
        keys = ("keyword", "keyword1", "wp_title", "title", "wp_slug", "slug", "layout", "wp_status", "page_type")
    elif workflow_name == "blog_generate":
        keys = ("keyword", "wp_status", "wp_category_ids", "wp_tag_ids")
    else:
        return None
    canonical = {}
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            canonical[key] = str(value).strip()
    return canonical


def _parse_task_time(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _maybe_mark_stale_task(task, stale_after_seconds=1800):
    if not task or task.get("status") not in {"pending", "running"}:
        return task
    ref = _parse_task_time(task.get("heartbeat_at")) or _parse_task_time(task.get("updated_at")) or _parse_task_time(task.get("created_at"))
    if not ref:
        return task
    age = (datetime.now() - ref).total_seconds()
    if age <= stale_after_seconds:
        return task
    message = f"Task marked stale_timeout: no heartbeat/update for {int(age)}s. It is safe to submit again."
    updated = store.update_task(
        task.get("task_id"),
        status="fail",
        progress="stale_timeout",
        error=message,
        stale=True,
    )
    return updated or task


def _find_recent_duplicate_task(workflow_name, payload, max_age_seconds=1800):
    canonical = _canonical_payload(workflow_name, payload)
    if not canonical:
        return None
    now = datetime.now()
    for task in store.list_tasks(limit=300):
        if task.get("type") != workflow_name:
            continue
        if task.get("status") not in {"pending", "running"}:
            continue
        created_at = _parse_task_time(task.get("created_at"))
        if created_at and (now - created_at).total_seconds() > max_age_seconds:
            continue
        if _canonical_payload(workflow_name, task.get("payload") or {}) == canonical:
            return task
    return None


def _payload(value):
    if isinstance(value, dict):
        return value
    parsed = parse_json_maybe(value)
    if isinstance(parsed, dict):
        return parsed
    return {}


def _dates_payload(value):
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    parsed = parse_json_maybe(value)
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


if __name__ == "__main__":
    mcp.run(transport="stdio")
