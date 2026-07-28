"""Extract on-page SEO elements from HTML."""

import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse


class _HTMLExtractor(HTMLParser):
    """Simple HTML parser to extract SEO elements."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta = {}
        self.headings = {f"h{i}": [] for i in range(1, 7)}
        self.links = []
        self.images = []
        self.faqs = []
        self.ctas = []
        self.canonical = ""
        self.og = {}
        self.structured_data = []
        self._current_tag = None
        self._current_h = None
        self._in_title = False
        self._in_script_ld = False
        self._script_content = ""
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        self._depth += 1

        if tag == "title":
            self._in_title = True

        if tag == "meta":
            name = attrs_dict.get("name", "").lower()
            prop = attrs_dict.get("property", "").lower()
            content = attrs_dict.get("content", "")
            if name:
                self.meta[name] = content
            if prop:
                self.og[prop] = content
            if name == "description":
                self.meta["description"] = content
            if name == "keywords":
                self.meta["keywords"] = content

        if tag == "link":
            rel = attrs_dict.get("rel", "")
            href = attrs_dict.get("href", "")
            if "canonical" in rel:
                self.canonical = href

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._current_h = tag
            self._current_tag = ""

        if tag == "a":
            href = attrs_dict.get("href", "")
            self.links.append({"href": href, "text": ""})
            self._current_tag = "a"

        if tag == "img":
            self.images.append({
                "src": attrs_dict.get("src", ""),
                "alt": attrs_dict.get("alt", ""),
            })

        if tag == "script" and attrs_dict.get("type") == "application/ld+json":
            self._in_script_ld = True
            self._script_content = ""

        # detect CTA-like buttons
        if tag in ("button", "a"):
            cls = attrs_dict.get("class", "").lower()
            text = ""
            if any(k in cls for k in ["cta", "btn-primary", "signup", "start", "trial", "get-started"]):
                self.ctas.append({"tag": tag, "class": cls, "text": ""})

    def handle_endtag(self, tag):
        self._depth -= 1
        if tag == "title":
            self._in_title = False

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._current_h == tag:
            text = (self._current_tag or "").strip()
            if text:
                self.headings[tag].append(text)
            self._current_h = None
            self._current_tag = None

        if tag == "a":
            if self.links and self._current_tag == "a":
                if self._current_tag:
                    pass
            self._current_tag = None

        if tag == "script" and self._in_script_ld:
            self._in_script_ld = False
            if self._script_content.strip():
                self.structured_data.append(self._script_content.strip())

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._current_h and self._current_tag is not None:
            self._current_tag += data
        if self._in_script_ld:
            self._script_content += data
        if self._current_tag == "a" and self.links:
            self.links[-1]["text"] += data.strip()
        # update last CTA text
        if self.ctas and self._current_tag in ("button", "a"):
            self.ctas[-1]["text"] += data.strip()


def extract_seo(url, html):
    """Extract SEO elements from HTML string."""
    if not html or len(html) < 100:
        return {"url": url, "error": "empty or too short HTML"}

    parser = _HTMLExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass

    # detect FAQ patterns
    faqs = []
    lower_html = html.lower()
    # look for FAQ schema or common patterns
    for pattern in [r'itemtype="[^"]*FAQPage[^"]*"', r'class="[^"]*faq[^"]*"']:
        if re.search(pattern, lower_html):
            faqs.append(pattern)
            break

    # count internal/external links
    parsed = urlparse(url)
    internal = 0
    external = 0
    for link in parser.links:
        href = link.get("href", "")
        if not href or href.startswith("#") or href.startswith("javascript"):
            continue
        link_parsed = urlparse(href)
        if link_parsed.netloc == parsed.netloc or not link_parsed.netloc:
            internal += 1
        else:
            external += 1

    return {
        "url": url,
        "title": parser.title.strip(),
        "meta_description": parser.meta.get("description", ""),
        "meta_keywords": parser.meta.get("keywords", ""),
        "canonical": parser.canonical,
        "og_title": parser.og.get("og:title", ""),
        "og_description": parser.og.get("og:description", ""),
        "og_image": parser.og.get("og:image", ""),
        "h1": parser.headings.get("h1", []),
        "h2": parser.headings.get("h2", []),
        "h3": parser.headings.get("h3", []),
        "h4": parser.headings.get("h4", []),
        "h5": parser.headings.get("h5", []),
        "h6": parser.headings.get("h6", []),
        "internal_links": internal,
        "external_links": external,
        "image_count": len(parser.images),
        "images_without_alt": sum(1 for img in parser.images if not img.get("alt")),
        "faq_detected": bool(faqs),
        "has_structured_data": bool(parser.structured_data),
        "structured_data_count": len(parser.structured_data),
        "cta_count": len(parser.ctas),
    }


def extract_all_pages(pages):
    """Extract SEO elements from a list of crawled pages."""
    results = []
    for page in pages:
        html = page.get("html", "")
        url = page.get("url", "")
        seo = extract_seo(url, html)
        seo["url_type"] = page.get("url_type", "other")
        seo["status_code"] = page.get("status_code", 0)
        seo["response_time_ms"] = page.get("response_time_ms", 0)
        results.append(seo)
    return results
