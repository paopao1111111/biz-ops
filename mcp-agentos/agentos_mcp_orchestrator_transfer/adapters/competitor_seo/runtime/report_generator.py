"""Orchestrate full competitor SEO analysis pipeline."""

import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .crawl_fetcher import fetch_competitor
from .seo_extractor import extract_all_pages
from .technical_checker import check_technical
from .llm_client import run_prompt

PROMPTS_PATH = Path(__file__).resolve().parent.parent / "assets" / "prompts.json"
REPORTS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "storage" / "reports"


def _load_prompts():
    return json.loads(PROMPTS_PATH.read_text(encoding="utf-8"))


def _render(template, **kwargs):
    text = template
    for key, val in kwargs.items():
        text = text.replace("{{" + key + "}}", json.dumps(val, ensure_ascii=False, indent=2) if not isinstance(val, str) else val)
    return text


def _compact_list(values, limit, item_len=160):
    return [str(v).strip()[:item_len] for v in values or [] if str(v).strip()][:limit]


def _compact_pages(seo_data):
    pages = []
    for s in seo_data:
        pages.append({
            "url": s.get("url", ""),
            "title": s.get("title", "")[:180],
            "meta_description": s.get("meta_description", "")[:240],
            "canonical": s.get("canonical", ""),
            "h1": _compact_list(s.get("h1"), 3),
            "h2": _compact_list(s.get("h2"), 8),
            "h3": _compact_list(s.get("h3"), 8),
            "internal_links": s.get("internal_links", 0),
            "external_links": s.get("external_links", 0),
            "image_count": s.get("image_count", 0),
            "images_without_alt": s.get("images_without_alt", 0),
            "faq_detected": s.get("faq_detected", False),
            "has_structured_data": s.get("has_structured_data", False),
            "structured_data_count": s.get("structured_data_count", 0),
            "cta_count": s.get("cta_count", 0),
            "url_type": s.get("url_type", ""),
            "llm_page_type": s.get("llm_page_type", ""),
            "status_code": s.get("status_code", 0),
            "response_time_ms": s.get("response_time_ms", 0),
        })
    return pages


def _llm_step(step_name, prompt_text):
    last_error = "unknown"
    for _ in range(2):
        result = run_prompt(prompt_text)
        if not result.get("success"):
            last_error = result.get("error", "unknown")
            continue
        output = result["output"]
        try:
            return json.loads(output)
        except (json.JSONDecodeError, TypeError):
            return {"raw_text": output}
    return {"error": last_error, "raw": ""}


def analyze_competitor(url, max_pages=30):
    """Full analysis pipeline for one competitor URL.

    Returns dict with all collected data and LLM analysis results.
    """
    steps = []

    # Step 1: Crawl
    steps.append({"step": "crawl", "status": "running"})
    crawl_data = fetch_competitor(url, max_pages=max_pages)
    steps[-1]["status"] = "done"
    steps[-1]["detail"] = f"Crawled {crawl_data['crawled_page_count']} pages, found {crawl_data['sitemap_url_count']} sitemap URLs"

    # Step 2: Extract SEO elements
    steps.append({"step": "extract", "status": "running"})
    seo_data = extract_all_pages(crawl_data.get("pages", []))
    steps[-1]["status"] = "done"

    # Step 3: Technical SEO
    steps.append({"step": "technical", "status": "running"})
    technical = check_technical(
        crawl_data["base_url"],
        robots_data=crawl_data.get("robots"),
        pages=crawl_data.get("pages", []),
    )
    steps[-1]["status"] = "done"

    # Step 4: LLM - Classify pages
    steps.append({"step": "classify_pages", "status": "running"})
    prompts = _load_prompts()
    classify_input = [{"url": s["url"], "title": s.get("title", ""), "h1": s.get("h1", []), "meta_description": s.get("meta_description", ""), "url_type": s.get("url_type", "")} for s in seo_data]
    classify_result = _llm_step("classify_pages", _render(prompts["classify_pages"], pages_json=classify_input))
    steps[-1]["status"] = "done"

    # merge classification back
    if isinstance(classify_result, dict) and "results" in classify_result:
        type_map = {r["url"]: r.get("page_type", "other") for r in classify_result["results"]}
        for s in seo_data:
            s["llm_page_type"] = type_map.get(s["url"], s.get("url_type", "other"))

    compact_pages = _compact_pages(seo_data)

    # Step 5: LLM - Content strategy
    steps.append({"step": "content_strategy", "status": "running"})
    content_result = _llm_step("content_strategy", _render(prompts["analyze_content_strategy"], pages_json=compact_pages))
    steps[-1]["status"] = "done"

    # Step 6: LLM - Conversion
    steps.append({"step": "conversion", "status": "running"})
    conversion_result = _llm_step("conversion", _render(prompts["analyze_conversion"], pages_json=compact_pages))
    steps[-1]["status"] = "done"

    # Step 7: LLM - GEO
    steps.append({"step": "geo", "status": "running"})
    geo_result = _llm_step("geo", _render(prompts["analyze_geo"], pages_json=compact_pages))
    steps[-1]["status"] = "done"

    # Step 8: LLM - Generate final report
    steps.append({"step": "report", "status": "running"})
    competitor_json = {
        "brand": urlparse(url).netloc,
        "base_url": crawl_data["base_url"],
        "sitemap_url_count": crawl_data["sitemap_url_count"],
        "crawled_page_count": crawl_data["crawled_page_count"],
    }
    seo_scores_json = {
        "technical_score": technical.get("score", 0),
        "page_count": len(seo_data),
        "avg_response_time_ms": technical.get("avg_response_time_ms", 0),
    }
    report_prompt = _render(
        prompts["report_prompt"],
        competitor_json=competitor_json,
        pages_json=compact_pages,
        seo_scores_json=seo_scores_json,
        technical_json=technical,
    )
    # prepend system prompt
    full_prompt = prompts["system_prompt"] + "\n\n" + report_prompt
    report_result = _llm_step("report", full_prompt)
    if isinstance(report_result, dict) and report_result.get("error"):
        steps[-1]["status"] = "failed"
        steps[-1]["error"] = report_result["error"]
        raise RuntimeError(f"AgentOS report step failed: {report_result['error']}")
    steps[-1]["status"] = "done"

    if isinstance(report_result, dict):
        report_text = report_result.get("raw_text", "") or report_result.get("raw", "")
    else:
        report_text = str(report_result)
    if not report_text.strip():
        raise RuntimeError("AgentOS report step returned empty report_text")

    # save report
    domain = urlparse(url).netloc.replace(".", "_")
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_filename = f"competitor_seo_{domain}_{date_str}.md"
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / report_filename
    report_path.write_text(report_text, encoding="utf-8")

    return {
        "success": True,
        "url": url,
        "domain": urlparse(url).netloc,
        "report_filename": report_filename,
        "report_path": str(report_path),
        "report_text": report_text,
        "crawl": {
            "base_url": crawl_data["base_url"],
            "sitemap_url_count": crawl_data["sitemap_url_count"],
            "crawled_page_count": crawl_data["crawled_page_count"],
        },
        "seo_data": seo_data,
        "technical": technical,
        "classify": classify_result,
        "content_strategy": content_result,
        "conversion": conversion_result,
        "geo": geo_result,
        "steps": steps,
    }
