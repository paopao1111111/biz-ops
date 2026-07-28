"""Technical SEO checks."""

from urllib.parse import urlparse
import requests

USER_AGENT = "Mozilla/5.0 (compatible; SEOAnalyzer/1.0)"


def check_technical(base_url, robots_data=None, pages=None):
    """Run technical SEO checks."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    results = {
        "https": parsed.scheme == "https",
        "checks": [],
    }

    # check HTTPS redirect
    if parsed.scheme == "https":
        try:
            resp = requests.get(
                origin.replace("https://", "http://"),
                headers={"User-Agent": USER_AGENT},
                timeout=10,
                allow_redirects=False,
            )
            results["https_redirect"] = resp.status_code in (301, 302, 308)
        except Exception:
            results["https_redirect"] = None
    else:
        results["https_redirect"] = False
        results["checks"].append({"name": "HTTPS", "status": "fail", "detail": "Site not using HTTPS"})

    # robots.txt
    if robots_data:
        results["robots_exists"] = robots_data.get("robots_status") == 200
        results["sitemap_in_robots"] = bool(robots_data.get("robots_rules", {}).get("sitemaps"))
        if not results["robots_exists"]:
            results["checks"].append({"name": "robots.txt", "status": "fail", "detail": "robots.txt not found"})
    else:
        results["robots_exists"] = False
        results["sitemap_in_robots"] = False

    # viewport meta
    if pages:
        first_html = ""
        for p in pages:
            if p.get("url", "").rstrip("/") == origin or p.get("url", "").rstrip("/") == origin.rstrip("/"):
                first_html = p.get("html", "")
                break
        if not first_html and pages:
            first_html = pages[0].get("html", "")

        results["has_viewport"] = 'name="viewport"' in first_html.lower()
        if not results["has_viewport"]:
            results["checks"].append({"name": "Viewport", "status": "fail", "detail": "Missing viewport meta tag"})

        # response time
        times = [p.get("response_time_ms", 0) for p in pages if p.get("response_time_ms")]
        results["avg_response_time_ms"] = sum(times) // len(times) if times else 0
        results["max_response_time_ms"] = max(times) if times else 0
        slow = [p for p in pages if p.get("response_time_ms", 0) > 3000]
        if slow:
            results["checks"].append({
                "name": "Page Speed",
                "status": "warn",
                "detail": f"{len(slow)} pages have response time > 3s",
            })

        # check for duplicate titles
        titles = {}
        for p in pages:
            html = p.get("html", "")
            import re
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
            if m:
                t = m.group(1).strip()
                titles.setdefault(t, []).append(p.get("url", ""))
        dupes = {k: v for k, v in titles.items() if len(v) > 1}
        results["duplicate_titles"] = dupes
        if dupes:
            results["checks"].append({
                "name": "Duplicate Titles",
                "status": "warn",
                "detail": f"{len(dupes)} duplicate title(s) found",
            })

    # pass/fail summary
    results["score"] = 100
    for check in results["checks"]:
        if check["status"] == "fail":
            results["score"] -= 15
        elif check["status"] == "warn":
            results["score"] -= 5
    results["score"] = max(0, results["score"])

    return results
