"""Competitor SEO research adapter for MCP orchestrator."""

import json
import os


def _configure_env(ctx):
    """Set AgentOS env vars from adapter config."""
    agentos_cfg = ctx.config.get("agentos", {})
    if agentos_cfg.get("base_url"):
        os.environ["AGENTOS_BASE_URL"] = str(agentos_cfg["base_url"])
    if agentos_cfg.get("token"):
        os.environ["AGENTOS_TOKEN"] = str(agentos_cfg["token"])
    if agentos_cfg.get("default_workflow_id"):
        os.environ["NEW_COZE_WORKFLOW_ID"] = str(agentos_cfg["default_workflow_id"])


def run_analyze(ctx, payload):
    """Run full competitor SEO analysis."""
    _configure_env(ctx)
    from .runtime.report_generator import analyze_competitor

    url = (payload.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "url is required"}

    max_pages = int(payload.get("max_pages") or ctx.config.get("max_pages") or 30)

    try:
        result = analyze_competitor(url, max_pages=max_pages)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # trim large data from response
    seo_summary = []
    for s in result.get("seo_data", []):
        seo_summary.append({
            "url": s["url"],
            "title": s.get("title", ""),
            "h1": s.get("h1", []),
            "meta_description": s.get("meta_description", ""),
            "url_type": s.get("url_type", ""),
            "llm_page_type": s.get("llm_page_type", ""),
            "status_code": s.get("status_code", 0),
            "internal_links": s.get("internal_links", 0),
            "external_links": s.get("external_links", 0),
        })

    return {
        "success": True,
        "url": result["url"],
        "domain": result["domain"],
        "report_filename": result["report_filename"],
        "report_path": result["report_path"],
        "report_text": result["report_text"],
        "crawl": result["crawl"],
        "seo_summary": seo_summary,
        "technical": result["technical"],
        "steps": result["steps"],
    }


def run_crawl_only(ctx, payload):
    """Crawl only, no LLM analysis."""
    _configure_env(ctx)
    from .runtime.crawl_fetcher import fetch_competitor
    from .runtime.seo_extractor import extract_all_pages

    url = (payload.get("url") or "").strip()
    if not url:
        return {"success": False, "error": "url is required"}

    max_pages = int(payload.get("max_pages") or 30)
    try:
        crawl_data = fetch_competitor(url, max_pages=max_pages)
        seo_data = extract_all_pages(crawl_data.get("pages", []))
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # strip raw HTML from response
    for page in crawl_data.get("pages", []):
        page.pop("html", None)

    return {
        "success": True,
        "url": url,
        "crawl": {
            "base_url": crawl_data["base_url"],
            "sitemap_url_count": crawl_data["sitemap_url_count"],
            "crawled_page_count": crawl_data["crawled_page_count"],
            "robots": crawl_data.get("robots", {}),
        },
        "seo_data": seo_data,
    }


def register(registry):
    """Register tools and workflows."""
    registry.tool("competitor_seo_analyze", run_analyze, "Run full competitor SEO analysis with LLM report")
    registry.tool("competitor_seo_crawl", run_crawl_only, "Crawl competitor site without LLM analysis")

    registry.workflow("competitor_seo_analyze", "competitor_seo_analyze", "Competitor SEO analysis", mode="async")
    registry.workflow("competitor_seo_crawl", "competitor_seo_crawl", "Competitor site crawl", mode="async")
