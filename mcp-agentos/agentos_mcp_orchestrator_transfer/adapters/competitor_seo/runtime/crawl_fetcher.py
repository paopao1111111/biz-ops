"""Crawl competitor website: robots.txt, sitemap, core pages."""

import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

DEFAULT_TIMEOUT = 30
MAX_PAGES = 30
USER_AGENT = "Mozilla/5.0 (compatible; SEOAnalyzer/1.0)"

HEADERS = {"User-Agent": USER_AGENT}


def fetch_url(url, timeout=DEFAULT_TIMEOUT):
    """Fetch a URL and return (status_code, text, headers, elapsed_ms)."""
    start = time.time()
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        elapsed = int((time.time() - start) * 1000)
        return resp.status_code, resp.text, dict(resp.headers), elapsed
    except requests.Timeout:
        return 0, "", {}, 0
    except Exception as exc:
        return 0, str(exc), {}, 0


def fetch_robots_and_sitemap(base_url):
    """Fetch robots.txt and parse sitemap URLs."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = f"{origin}/robots.txt"

    robots_status, robots_text, _, _ = fetch_url(robots_url)

    sitemap_urls = []
    robots_rules = {"allow": [], "disallow": [], "sitemaps": []}

    if robots_status == 200 and robots_text:
        for line in robots_text.splitlines():
            line = line.strip()
            lower = line.lower()
            if lower.startswith("sitemap:"):
                sm = line.split(":", 1)[1].strip()
                sitemap_urls.append(sm)
                robots_rules["sitemaps"].append(sm)
            elif lower.startswith("allow:"):
                robots_rules["allow"].append(line.split(":", 1)[1].strip())
            elif lower.startswith("disallow:"):
                robots_rules["disallow"].append(line.split(":", 1)[1].strip())

    if not sitemap_urls:
        for path in ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml"]:
            sm_url = origin + path
            status, text, _, _ = fetch_url(sm_url)
            if status == 200 and ("<urlset" in text or "<sitemapindex" in text):
                sitemap_urls.append(sm_url)
                break

    return {
        "robots_url": robots_url,
        "robots_status": robots_status,
        "robots_text": robots_text[:2000],
        "robots_rules": robots_rules,
        "sitemap_urls": sitemap_urls,
    }


def parse_sitemap(sitemap_url, depth=0):
    """Parse sitemap XML and return list of page URLs. Handles sitemap index."""
    if depth > 3:
        return []
    status, text, _, _ = fetch_url(sitemap_url)
    if status != 200 or not text:
        return []

    urls = []
    try:
        root = ET.fromstring(text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        # sitemap index
        for sitemap in root.findall(".//sm:sitemap/sm:loc", ns):
            loc = sitemap.text.strip() if sitemap.text else ""
            if loc:
                urls.extend(parse_sitemap(loc, depth + 1))

        # url entries
        for url_elem in root.findall(".//sm:url/sm:loc", ns):
            loc = url_elem.text.strip() if url_elem.text else ""
            if loc:
                urls.append(loc)
    except ET.ParseError:
        pass

    return urls


def classify_url(url):
    """Quick heuristic URL classification."""
    path = urlparse(url).path.lower()
    if path in ("/", ""):
        return "homepage"
    if any(k in path for k in ["/blog", "/article", "/post", "/news"]):
        return "blog"
    if any(k in path for k in ["/pricing", "/price", "/plan"]):
        return "pricing"
    if any(k in path for k in ["/tool", "/app", "/feature", "/product"]):
        return "tool"
    if any(k in path for k in ["/about", "/team", "/company"]):
        return "about"
    if any(k in path for k in ["/doc", "/help", "/support", "/guide", "/faq"]):
        return "docs"
    if any(k in path for k in ["/login", "/signup", "/register", "/try", "/start"]):
        return "landing"
    return "other"


def select_core_pages(all_urls, base_url, max_pages=MAX_PAGES):
    """Select diverse core pages from sitemap URLs."""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # filter to same domain
    same_domain = [u for u in all_urls if urlparse(u).netloc == parsed.netloc]

    # group by type
    by_type = {}
    for url in same_domain:
        t = classify_url(url)
        by_type.setdefault(t, []).append(url)

    selected = []
    # ensure homepage
    homepage = origin + "/"
    if homepage not in selected:
        selected.append(homepage)

    # pick from each type
    priority = ["tool", "pricing", "blog", "about", "docs", "landing", "other"]
    for t in priority:
        for url in by_type.get(t, []):
            if url not in selected and len(selected) < max_pages:
                selected.append(url)

    return selected[:max_pages]


def crawl_pages(urls):
    """Crawl list of URLs and return page data."""
    pages = []
    for url in urls:
        status, html, headers, elapsed = fetch_url(url)
        pages.append({
            "url": url,
            "status_code": status,
            "html": html,
            "headers": headers,
            "response_time_ms": elapsed,
            "content_type": headers.get("Content-Type", ""),
            "url_type": classify_url(url),
        })
    return pages


def fetch_competitor(url, max_pages=MAX_PAGES):
    """Full crawl pipeline for one competitor."""
    parsed = urlparse(url)
    if not parsed.scheme:
        url = "https://" + url
        parsed = urlparse(url)

    origin = f"{parsed.scheme}://{parsed.netloc}"

    # Step 1: robots + sitemap
    robots_data = fetch_robots_and_sitemap(origin)

    # Step 2: parse sitemaps
    all_sitemap_urls = []
    for sm_url in robots_data["sitemap_urls"]:
        all_sitemap_urls.extend(parse_sitemap(sm_url))

    # Step 3: select core pages
    if all_sitemap_urls:
        core_urls = select_core_pages(all_sitemap_urls, origin, max_pages)
    else:
        core_urls = [origin + "/"]

    # Step 4: crawl pages
    pages = crawl_pages(core_urls)

    return {
        "base_url": origin,
        "robots": robots_data,
        "sitemap_url_count": len(all_sitemap_urls),
        "crawled_page_count": len(pages),
        "pages": pages,
    }
