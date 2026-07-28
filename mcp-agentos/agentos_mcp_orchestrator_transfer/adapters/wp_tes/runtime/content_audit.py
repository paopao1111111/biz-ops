import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from urllib.parse import urlparse

logger = logging.getLogger('content_audit')

from .html_generat import (
    sanitize_iweaver_urls,

    wp_request,
    WP_API_BASE_URL,
    _parse_json_string_if_possible,
    clean_output_content,
    COZE_BASE_URL,
    COZE_WORKFLOW_TOKEN,
    WORKFLOW_PROVIDER,
)
from .coze_llm import call_coze_llm
from .prompts import CONTENT_AUDIT_PROMPT, format_prompt

WP_POSTS_URL = f"{WP_API_BASE_URL}/posts"
WP_PAGES_URL = f"{WP_API_BASE_URL}/pages"

CONTENT_AUDIT_WORKFLOW_ID = os.getenv('CONTENT_AUDIT_WORKFLOW_ID', '7632259009373798400')
NEW_COZE_WORKFLOW_ID = os.getenv('NEW_COZE_WORKFLOW_ID', '').strip()

GSC_TOKEN_JSON = os.getenv('GSC_TOKEN_JSON', '')
GSC_SITE_URL = os.getenv('GSC_SITE_URL', 'sc-domain:iweaver.ai')
GSC_PROXY_HOST = os.getenv('GSC_PROXY_HOST', '127.0.0.1')
GSC_PROXY_PORT = int(os.getenv('GSC_PROXY_PORT', '10808'))
GA4_PROPERTY_ID = os.getenv('GA4_PROPERTY_ID', '435515520')

FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_AUDIT_SHEET_TOKEN = os.getenv('FEISHU_AUDIT_SHEET_TOKEN', '')

IWEAVER_DOMAIN = 'www.iweaver.ai'

INDICATOR_FRAMEWORK = {
    'ctr': {
        'label': 'CTR',
        'dimension': 'gsc',
        'threshold': 3.0,
        'unit': '%',
        'diagnosis': 'Title不吸引 / SERP弱',
        'action': '重写Title（关键词+结果+场景）',
    },
    'top10_keywords': {
        'label': 'Top10关键词数',
        'dimension': 'gsc',
        'threshold': 30,
        'unit': '',
        'diagnosis': '关键词覆盖不足',
        'action': '扩展长尾词 + H2优化',
    },
    'impression_growth': {
        'label': 'Impression增长',
        'dimension': 'gsc',
        'threshold': 10.0,
        'unit': '%',
        'diagnosis': '内容未覆盖搜索意图',
        'action': '增加问题型关键词 + 优化FAQ',
    },
    'word_count': {
        'label': '字数',
        'dimension': 'content',
        'threshold': 800,
        'unit': 'words',
        'diagnosis': '内容浅 / 不完整',
        'action': '扩展到1000–1500单词',
    },
    'faq_count': {
        'label': 'FAQ数量',
        'dimension': 'content',
        'threshold': 5,
        'unit': '',
        'diagnosis': '未覆盖用户问题',
        'action': '补FAQ（5–10个）',
    },
    'use_case_count': {
        'label': 'Use Case',
        'dimension': 'content',
        'threshold': 3,
        'unit': '',
        'diagnosis': '场景不足',
        'action': '补3–5个真实场景',
    },
    'info_density': {
        'label': '信息密度',
        'dimension': 'content',
        'threshold': 3.0,
        'unit': '/100w',
        'diagnosis': '内容"空话多"',
        'action': '增加数据/步骤/对比',
    },
    'semantic_coverage': {
        'label': '语义覆盖',
        'dimension': 'content',
        'threshold': 80.0,
        'unit': '%',
        'diagnosis': 'SEO关键词不完整',
        'action': '增加NLP相关词',
    },
    'avg_session_duration': {
        'label': '停留时间',
        'dimension': 'ga',
        'threshold': 60,
        'unit': 's',
        'diagnosis': '开头无hook',
        'action': '重写Hero（结果导向）',
    },
    'scroll_depth': {
        'label': 'Scroll深度',
        'dimension': 'ga',
        'threshold': 40.0,
        'unit': '%',
        'diagnosis': '结构不引导',
        'action': '优化结构（Problem→Use case→CTA）',
    },
    'registration_rate': {
        'label': '注册率',
        'dimension': 'ga',
        'threshold': 30.0,
        'unit': '%',
        'diagnosis': '无互动点',
        'action': '增加demo / input box',
    },
    'internal_links': {
        'label': '内链数量',
        'dimension': 'links',
        'threshold': 3,
        'unit': '',
        'diagnosis': '无hub结构',
        'action': '增加3+内链',
    },
}

DIMENSION_LABELS = {
    'gsc': 'Google Search Console',
    'content': '内容质量',
    'ga': 'Google Analytics',
    'links': '内链结构',
}


# ============================================================
# WP Post Fetching
# ============================================================

def _wp_base_url(content_type='post'):
    return WP_PAGES_URL if content_type == 'page' else WP_POSTS_URL


def fetch_published_posts(page=1, per_page=20, search='', content_type='post'):
    params = {
        'status': 'publish',
        'per_page': per_page,
        'page': page,
        'orderby': 'date',
        'order': 'desc',
        '_fields': 'id,title,slug,link,date,excerpt',
    }
    if search:
        params['search'] = search

    resp = wp_request('GET', _wp_base_url(content_type), params=params)
    resp.raise_for_status()

    posts = resp.json()
    total = int(resp.headers.get('X-WP-Total', 0))
    total_pages = int(resp.headers.get('X-WP-TotalPages', 0))

    result_posts = []
    for p in posts:
        result_posts.append({
            'id': p['id'],
            'title': p.get('title', {}).get('rendered', ''),
            'slug': p.get('slug', ''),
            'link': p.get('link', ''),
            'date': p.get('date', ''),
            'excerpt': p.get('excerpt', {}).get('rendered', '')[:100],
        })

    return {'posts': result_posts, 'total': total, 'total_pages': total_pages}


def fetch_post_detail(post_id, content_type='post'):
    url = f"{_wp_base_url(content_type)}/{post_id}"
    resp = wp_request('GET', url, params={
        '_fields': 'id,title,slug,link,content,meta,date,modified'
    })
    resp.raise_for_status()
    return resp.json()


# ============================================================
# Content Analysis Functions
# ============================================================

def strip_html_tags(html):
    if not html:
        return ''
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'&[a-zA-Z]+;', ' ', text)
    text = re.sub(r'&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def count_words(html):
    text = strip_html_tags(html)
    words = text.split()
    return len(words)


def extract_faq_count(html):
    if not html:
        return 0

    count = 0

    ld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
    for ld in ld_matches:
        try:
            data = json.loads(ld)
            if isinstance(data, dict) and data.get('@type') == 'FAQPage':
                entities = data.get('mainEntity', [])
                if isinstance(entities, list):
                    count = max(count, len(entities))
        except (json.JSONDecodeError, ValueError):
            pass

    if count > 0:
        return count

    faq_section = re.search(
        r'<h2[^>]*>.*?(?:FAQ|Frequently\s+Asked\s+Questions).*?</h2>(.*?)(?=<h2|$)',
        html, re.DOTALL | re.IGNORECASE
    )
    if faq_section:
        h3s = re.findall(r'<h3[^>]*>', faq_section.group(1))
        if h3s:
            return len(h3s)

    question_headings = re.findall(r'<h[23][^>]*>[^<]*\?[^<]*</h[23]>', html, re.IGNORECASE)
    return len(question_headings)


def extract_use_case_count(html):
    if not html:
        return 0

    pattern = r'<h[23][^>]*>[^<]*(?:use\s*case|scenario|example|how\s+to\s+use|application|real[- ]world)[^<]*</h[23]>'
    matches = re.findall(pattern, html, re.IGNORECASE)
    return len(matches)


def calculate_info_density(html):
    text = strip_html_tags(html)
    words = text.split()
    word_count = len(words)
    if word_count == 0:
        return 0.0

    numbers = re.findall(r'\b\d+[\d,.]*%?\b', text)
    steps = re.findall(r'(?i)\b(?:step\s+\d|first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\b', text)
    comparisons = re.findall(
        r'(?i)\b(?:more\s+than|less\s+than|compared\s+to|vs\.?|versus|better|worse|faster|slower|'
        r'increase|decrease|higher|lower|reduce|improve|boost|double|triple)\b',
        text
    )

    total_points = len(numbers) + len(steps) + len(comparisons)
    return round((total_points / word_count) * 100, 1)


def assess_semantic_coverage(html, focus_keywords):
    if not focus_keywords:
        return 100.0

    text = strip_html_tags(html).lower()

    keywords = [k.strip().lower() for k in re.split(r'[,;|]', focus_keywords) if k.strip()]
    if not keywords:
        return 100.0

    expanded = set()
    for kw in keywords:
        expanded.add(kw)
        parts = kw.split()
        for p in parts:
            if len(p) > 3:
                expanded.add(p)
        if kw.endswith('s'):
            expanded.add(kw[:-1])
        elif kw.endswith('ing'):
            expanded.add(kw[:-3])
            expanded.add(kw[:-3] + 'e')
        else:
            expanded.add(kw + 's')

    found = sum(1 for term in expanded if term in text)
    coverage = (found / len(expanded)) * 100 if expanded else 100.0
    return round(min(coverage, 100.0), 1)


def count_internal_links(html, domain=IWEAVER_DOMAIN):
    if not html:
        return 0

    links = re.findall(r'<a\s[^>]*href="([^"]*)"[^>]*>', html, re.IGNORECASE)
    count = 0
    for href in links:
        if domain in href and not href.startswith('#'):
            parsed = urlparse(href)
            if parsed.path and parsed.path != '/':
                count += 1
    return count


# ============================================================
# GSC Real API Functions
# ============================================================

_gsc_service = None

def _gsc_configured():
    return bool(GSC_TOKEN_JSON and os.path.exists(GSC_TOKEN_JSON))


def _get_gsc_service():
    global _gsc_service
    if _gsc_service:
        return _gsc_service
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2, socks

        with open(GSC_TOKEN_JSON) as f:
            token_data = json.load(f)

        creds = Credentials(
            token=token_data['token'],
            refresh_token=token_data['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=token_data['client_id'],
            client_secret=token_data['client_secret'],
            scopes=['https://www.googleapis.com/auth/webmasters.readonly'],
        )

        proxy_info = httplib2.ProxyInfo(
            proxy_type=socks.PROXY_TYPE_SOCKS5,
            proxy_host=GSC_PROXY_HOST,
            proxy_port=GSC_PROXY_PORT,
        )
        http = httplib2.Http(proxy_info=proxy_info)
        authed_http = AuthorizedHttp(creds, http=http)
        _gsc_service = build('searchconsole', 'v1', http=authed_http)
        return _gsc_service
    except Exception as e:
        logger.warning(f"[GSC] Failed to init service: {e}")
        return None


def _gsc_query(post_url, dimensions=None, row_limit=1000):
    service = _get_gsc_service()
    if not service:
        return None

    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')

    body = {
        'startDate': start_date,
        'endDate': end_date,
        'dimensionFilterGroups': [{
            'filters': [{
                'dimension': 'page',
                'operator': 'equals',
                'expression': post_url,
            }]
        }],
        'rowLimit': row_limit,
    }
    if dimensions:
        body['dimensions'] = dimensions

    try:
        resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        return resp
    except Exception as e:
        logger.warning(f"[GSC] Query failed: {e}")
        return None


def fetch_gsc_ctr(post_url):
    if not _gsc_configured():
        return {'value': 0.0, 'mock': True}

    resp = _gsc_query(post_url, dimensions=['page'])
    if not resp or not resp.get('rows'):
        return {'value': 0.0, 'mock': False}

    ctr = resp['rows'][0].get('ctr', 0.0)
    return {'value': round(ctr * 100, 2), 'mock': False}


def fetch_gsc_top10_keywords(post_url):
    if not _gsc_configured():
        return {'value': 0, 'mock': True}

    resp = _gsc_query(post_url, dimensions=['query'], row_limit=1000)
    if not resp or not resp.get('rows'):
        return {'value': 0, 'mock': False}

    top10_count = sum(1 for r in resp['rows'] if r.get('position', 999) <= 10)
    return {'value': top10_count, 'mock': False}


def fetch_gsc_impression_growth(post_url):
    if not _gsc_configured():
        return {'value': 0.0, 'mock': True}

    service = _get_gsc_service()
    if not service:
        return {'value': 0.0, 'mock': True}

    now = datetime.now()
    this_month_end = now.strftime('%Y-%m-%d')
    this_month_start = (now - timedelta(days=30)).strftime('%Y-%m-%d')
    last_month_end = (now - timedelta(days=31)).strftime('%Y-%m-%d')
    last_month_start = (now - timedelta(days=61)).strftime('%Y-%m-%d')

    page_filter = {
        'dimensionFilterGroups': [{
            'filters': [{'dimension': 'page', 'operator': 'equals', 'expression': post_url}]
        }],
        'dimensions': ['page'],
        'rowLimit': 1,
    }

    try:
        r1 = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body={
            **page_filter, 'startDate': this_month_start, 'endDate': this_month_end
        }).execute()
        r2 = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body={
            **page_filter, 'startDate': last_month_start, 'endDate': last_month_end
        }).execute()

        this_imp = r1['rows'][0]['impressions'] if r1.get('rows') else 0
        last_imp = r2['rows'][0]['impressions'] if r2.get('rows') else 0

        if last_imp == 0:
            growth = 100.0 if this_imp > 0 else 0.0
        else:
            growth = ((this_imp - last_imp) / last_imp) * 100

        return {'value': round(growth, 1), 'mock': False}
    except Exception as e:
        logger.warning(f"[GSC] Impression growth failed: {e}")
        return {'value': 0.0, 'mock': False}


# ============================================================
# GA4 Real API Functions
# ============================================================

_ga4_service = None

def _ga4_configured():
    return bool(GA4_PROPERTY_ID and GSC_TOKEN_JSON and os.path.exists(GSC_TOKEN_JSON))


def _get_ga4_service():
    global _ga4_service
    if _ga4_service:
        return _ga4_service
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from google_auth_httplib2 import AuthorizedHttp
        import httplib2, socks

        with open(GSC_TOKEN_JSON) as f:
            token_data = json.load(f)

        creds = Credentials(
            token=token_data['token'],
            refresh_token=token_data['refresh_token'],
            token_uri='https://oauth2.googleapis.com/token',
            client_id=token_data['client_id'],
            client_secret=token_data['client_secret'],
            scopes=token_data.get('scopes', []),
        )

        proxy_info = httplib2.ProxyInfo(
            proxy_type=socks.PROXY_TYPE_SOCKS5,
            proxy_host=GSC_PROXY_HOST,
            proxy_port=GSC_PROXY_PORT,
        )
        http = httplib2.Http(proxy_info=proxy_info)
        authed_http = AuthorizedHttp(creds, http=http)
        _ga4_service = build('analyticsdata', 'v1beta', http=authed_http)
        return _ga4_service
    except Exception as e:
        logger.warning(f"[GA4] Failed to init service: {e}")
        return None


def _ga4_query_page(post_path, metrics):
    service = _get_ga4_service()
    if not service:
        return None
    try:
        response = service.properties().runReport(
            property=f'properties/{GA4_PROPERTY_ID}',
            body={
                'dateRanges': [{'startDate': '30daysAgo', 'endDate': 'today'}],
                'dimensions': [{'name': 'pagePath'}],
                'metrics': [{'name': m} for m in metrics],
                'dimensionFilter': {
                    'filter': {
                        'fieldName': 'pagePath',
                        'stringFilter': {'matchType': 'EXACT', 'value': post_path},
                    }
                },
                'limit': 1,
            }
        ).execute()
        return response
    except Exception as e:
        logger.warning(f"[GA4] Query failed for {post_path}: {e}")
        return None


def fetch_ga4_session_duration(post_path):
    if not _ga4_configured():
        return {'value': 0.0, 'mock': True}

    resp = _ga4_query_page(post_path, ['averageSessionDuration'])
    if not resp or not resp.get('rows'):
        return {'value': 0.0, 'mock': False}

    val = float(resp['rows'][0]['metricValues'][0]['value'])
    return {'value': round(val, 1), 'mock': False}


def fetch_ga4_scroll_depth(post_path):
    if not _ga4_configured():
        return {'value': 0.0, 'mock': True}

    resp = _ga4_query_page(post_path, ['scrolledUsers', 'totalUsers'])
    if not resp or not resp.get('rows'):
        return {'value': 0.0, 'mock': False}

    scrolled = float(resp['rows'][0]['metricValues'][0]['value'])
    total = float(resp['rows'][0]['metricValues'][1]['value'])
    pct = (scrolled / total * 100) if total > 0 else 0.0
    return {'value': round(pct, 1), 'mock': False}


def fetch_ga4_registration_rate(post_path):
    if not _ga4_configured():
        return {'value': 0.0, 'mock': True}

    resp = _ga4_query_page(post_path, ['conversions', 'totalUsers'])
    if not resp or not resp.get('rows'):
        return {'value': 0.0, 'mock': False}

    conversions = float(resp['rows'][0]['metricValues'][0]['value'])
    total = float(resp['rows'][0]['metricValues'][1]['value'])
    pct = (conversions / total * 100) if total > 0 else 0.0
    return {'value': round(pct, 1), 'mock': False}


# ============================================================
# Audit Single Post
# ============================================================

def audit_post(post_id, content_type='post'):
    post = fetch_post_detail(post_id, content_type=content_type)
    content_html = post.get('content', {}).get('rendered', '')
    title = post.get('title', {}).get('rendered', '')
    link = post.get('link', '')
    slug = post.get('slug', '')
    meta = post.get('meta', {})
    focus_keywords = ''
    if isinstance(meta, dict):
        focus_keywords = meta.get('rank_math_focus_keyword', '') or ''
    post_path = urlparse(link).path if link else ''

    indicators = {}

    indicators['ctr'] = fetch_gsc_ctr(link)
    indicators['top10_keywords'] = fetch_gsc_top10_keywords(link)
    indicators['impression_growth'] = fetch_gsc_impression_growth(link)

    wc = count_words(content_html)
    indicators['word_count'] = {'value': wc, 'mock': False}

    fc = extract_faq_count(content_html)
    indicators['faq_count'] = {'value': fc, 'mock': False}

    uc = extract_use_case_count(content_html)
    indicators['use_case_count'] = {'value': uc, 'mock': False}

    density = calculate_info_density(content_html)
    indicators['info_density'] = {'value': density, 'mock': False}

    coverage = assess_semantic_coverage(content_html, focus_keywords)
    indicators['semantic_coverage'] = {'value': coverage, 'mock': False}

    indicators['avg_session_duration'] = fetch_ga4_session_duration(post_path)
    indicators['scroll_depth'] = fetch_ga4_scroll_depth(post_path)
    indicators['registration_rate'] = fetch_ga4_registration_rate(post_path)

    il = count_internal_links(content_html)
    indicators['internal_links'] = {'value': il, 'mock': False}

    results = {}
    passed_count = 0
    failed_list = []

    for key, framework in INDICATOR_FRAMEWORK.items():
        ind = indicators[key]
        value = ind['value']
        threshold = framework['threshold']
        is_passed = value >= threshold
        if is_passed:
            passed_count += 1

        entry = {
            'key': key,
            'label': framework['label'],
            'dimension': framework['dimension'],
            'value': value,
            'threshold': threshold,
            'unit': framework['unit'],
            'passed': is_passed,
            'mock': ind.get('mock', False),
        }
        if not is_passed:
            entry['diagnosis'] = framework['diagnosis']
            entry['action'] = framework['action']
            failed_list.append(entry)

        results[key] = entry

    dimensions = {}
    for dim_key, dim_label in DIMENSION_LABELS.items():
        dim_indicators = [v for v in results.values() if v['dimension'] == dim_key]
        dim_passed = sum(1 for i in dim_indicators if i['passed'])
        dim_mock = any(i['mock'] for i in dim_indicators)
        dimensions[dim_key] = {
            'label': dim_label,
            'mock': dim_mock,
            'passed': dim_passed,
            'total': len(dim_indicators),
            'indicators': dim_indicators,
        }

    return {
        'post_id': post_id,
        'content_type': content_type,
        'title': title,
        'slug': slug,
        'link': link,
        'focus_keywords': focus_keywords,
        'score': passed_count,
        'total': len(INDICATOR_FRAMEWORK),
        'dimensions': dimensions,
        'failed_indicators': failed_list,
        'content_html': content_html,
    }


# ============================================================
# Rewrite via Coze Workflow
# ============================================================

def _strip_markdown_fences(s):
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r'^```[a-zA-Z]*\n?', '', s)
        s = re.sub(r'\n?```$', '', s)
        s = s.strip()
    return s


def _try_parse_rewrite_json(raw):
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

    result = {}
    for field in ["meta_title", "meta_description", "focus_keywords", "changes_summary"]:
        fm = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if fm:
            val = fm.group(1)
            val = val.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
            result[field] = val

    cm = re.search(r'"content"\s*:\s*"', raw)
    if cm:
        start = cm.end()
        rest = raw[start:]
        end_m = re.search(r'",\s*\n\s*"(?:meta_title|meta_description|focus_keywords|changes_summary)"', rest)
        if end_m:
            content_raw = rest[:end_m.start()]
        else:
            end_m2 = re.search(r'"\s*\n?\s*\}', rest)
            content_raw = rest[:end_m2.start()] if end_m2 else rest
        content_raw = content_raw.replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')
        result["content"] = content_raw

    if result.get("content"):
        return result
    return None


def rewrite_post(original_html, failed_indicators, focus_keywords, article_title):
    if not CONTENT_AUDIT_WORKFLOW_ID:
        return {'success': False, 'error': 'CONTENT_AUDIT_WORKFLOW_ID not configured'}

    failed_text = ""
    for ind in failed_indicators:
        failed_text += (
            f"- {ind['label']}: current={ind['value']}{ind.get('unit','')}, "
            f"threshold={ind['threshold']}{ind.get('unit','')}, "
            f"diagnosis={ind.get('diagnosis','')}, "
            f"action={ind.get('action','')}\n"
        )

    input_params = {
        "original_content": original_html,
        "failed_indicators": failed_text,
        "focus_keywords": focus_keywords or "",
        "article_title": article_title or "",
    }

    # --- Local provider: use new Coze minimal workflow ---
    if WORKFLOW_PROVIDER == "local":
        if not NEW_COZE_WORKFLOW_ID:
            return {'success': False, 'error': 'NEW_COZE_WORKFLOW_ID not configured'}
        prompt = format_prompt(CONTENT_AUDIT_PROMPT, **input_params)
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
            return {'success': False, 'error': r.get('error')}
        raw_data = r["output"]
        logger.info(f'[LOCAL] Content audit LLM output (first 500): {str(raw_data)[:500]}')
    else:
        headers = {
            "Authorization": f"Bearer {COZE_WORKFLOW_TOKEN}",
            "Content-Type": "application/json",
        }
        payload = {
            "workflow_id": CONTENT_AUDIT_WORKFLOW_ID,
            "parameters": input_params,
            "is_async": False,
        }

        try:
            run_url = f"{COZE_BASE_URL}/v1/workflow/run"
            resp = requests.post(run_url, headers=headers, json=payload, timeout=600)
            resp.raise_for_status()
            run_result = resp.json()
        except requests.exceptions.Timeout:
            return {'success': False, 'error': 'Coze workflow request timed out.'}
        except Exception as exc:
            return {'success': False, 'error': f'Coze workflow request failed: {exc}'}

        if run_result.get('code') not in (0, '0', None):
            return {'success': False, 'error': f'Coze workflow returned an error: {run_result}'}

        raw_data = run_result.get('data', '')

    # data may be a JSON string like '{"output": "```json\n{...}```"}'
    if isinstance(raw_data, str):
        try:
            outer = json.loads(raw_data)
            if isinstance(outer, dict) and 'output' in outer:
                raw_data = outer['output']
        except (json.JSONDecodeError, ValueError):
            pass

    raw = clean_output_content(raw_data) if isinstance(raw_data, str) else str(raw_data)
    raw = _strip_markdown_fences(raw)

    parsed = _parse_json_string_if_possible(raw)
    if isinstance(parsed, str):
        parsed = _parse_json_string_if_possible(parsed)

    if isinstance(parsed, dict) and parsed.get('content'):
        parsed['content'] = sanitize_iweaver_urls(parsed['content'])
        return {'success': True, 'rewrite': parsed}

    parsed = _try_parse_rewrite_json(raw)
    if isinstance(parsed, dict) and parsed.get('content'):
        parsed['content'] = sanitize_iweaver_urls(parsed['content'])
        return {'success': True, 'rewrite': parsed}

    return {'success': False, 'error': f'Failed to parse rewrite result: {str(raw)[:500]}'}


# ============================================================
# Publish Rewrite to WP
# ============================================================

def publish_rewrite(post_id, new_content, new_seo, mode='draft', content_type='post', new_title=None):
    base_url = _wp_base_url(content_type)
    if mode == 'update':
        data = {'content': new_content}
        if new_title:
            data['title'] = new_title
        if new_seo:
            meta = {}
            if new_seo.get('meta_title'):
                meta['rank_math_title'] = new_seo['meta_title'][:60]
            if new_seo.get('meta_description'):
                meta['rank_math_description'] = new_seo['meta_description'][:155]
            if new_seo.get('focus_keywords'):
                meta['rank_math_focus_keyword'] = new_seo['focus_keywords']
            if meta:
                data['meta'] = meta

        url = f"{base_url}/{post_id}"
        resp = wp_request('POST', url, json=data)
        resp.raise_for_status()
        result = resp.json()
        return {
            'mode': 'update',
            'id': result.get('id'),
            'link': result.get('link'),
            'slug': result.get('slug'),
        }

    else:
        original = fetch_post_detail(post_id, content_type=content_type)
        original_title = original.get('title', {}).get('rendered', 'Untitled')

        data = {
            'title': new_title or f"[AUDIT REWRITE] {original_title}",
            'slug': f"{original.get('slug', 'post')}-audit-rewrite",
            'status': 'draft',
            'content': new_content,
        }

        if new_seo:
            meta = {}
            if new_seo.get('meta_title'):
                meta['rank_math_title'] = new_seo['meta_title'][:60]
            if new_seo.get('meta_description'):
                meta['rank_math_description'] = new_seo['meta_description'][:155]
            if new_seo.get('focus_keywords'):
                meta['rank_math_focus_keyword'] = new_seo['focus_keywords']
            if meta:
                data['meta'] = meta

        resp = wp_request('POST', base_url, json=data)
        resp.raise_for_status()
        result = resp.json()
        return {
            'mode': 'draft',
            'id': result.get('id'),
            'link': result.get('link'),
            'slug': result.get('slug'),
            'original_post_id': post_id,
        }


# ============================================================
# Re-audit after rewrite (for before/after comparison)
# ============================================================

def audit_html_content(content_html, focus_keywords=''):
    indicators = {}

    wc = count_words(content_html)
    indicators['word_count'] = {'value': wc, 'mock': False}

    fc = extract_faq_count(content_html)
    indicators['faq_count'] = {'value': fc, 'mock': False}

    uc = extract_use_case_count(content_html)
    indicators['use_case_count'] = {'value': uc, 'mock': False}

    density = calculate_info_density(content_html)
    indicators['info_density'] = {'value': density, 'mock': False}

    coverage = assess_semantic_coverage(content_html, focus_keywords)
    indicators['semantic_coverage'] = {'value': coverage, 'mock': False}

    il = count_internal_links(content_html)
    indicators['internal_links'] = {'value': il, 'mock': False}

    results = {}
    for key in ['word_count', 'faq_count', 'use_case_count', 'info_density', 'semantic_coverage', 'internal_links']:
        framework = INDICATOR_FRAMEWORK[key]
        ind = indicators[key]
        value = ind['value']
        is_passed = value >= framework['threshold']
        results[key] = {
            'key': key,
            'label': framework['label'],
            'value': value,
            'threshold': framework['threshold'],
            'unit': framework['unit'],
            'passed': is_passed,
            'mock': False,
        }

    return results


# ============================================================
# Feishu Export
# ============================================================

AUDIT_FEISHU_HEADERS = [
    '文章标题', 'URL', 'Focus Keywords',
    '指标', '维度',
    '修改前值', '修改前状态',
    '修改后值', '修改后状态',
    '诊断', '优化动作',
]


def _get_feishu_token():
    url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
    payload = {'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu auth failed: {data}")
    return data['tenant_access_token']


def _get_feishu_sheet_id(token):
    url = f'https://open.feishu.cn/open-apis/sheets/v3/spreadsheets/{FEISHU_AUDIT_SHEET_TOKEN}/sheets/query'
    resp = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu query sheets failed: {data}")
    sheets = data.get('data', {}).get('sheets', [])
    if not sheets:
        raise RuntimeError('Feishu spreadsheet has no sheets.')
    return sheets[0]['sheet_id']


def push_audit_to_feishu(audit_results):
    if not FEISHU_AUDIT_SHEET_TOKEN:
        return {'skipped': True, 'reason': 'FEISHU_AUDIT_SHEET_TOKEN not configured'}

    token = _get_feishu_token()
    sheet_id = _get_feishu_sheet_id(token)

    rows = []
    for item in audit_results:
        title = item.get('title', '')
        link = item.get('link', '')
        focus_kw = item.get('focus_keywords', '')
        before = item.get('before_indicators', {})
        after = item.get('after_indicators', {})

        for key, framework in INDICATOR_FRAMEWORK.items():
            b = before.get(key, {})
            a = after.get(key, {})
            b_val = b.get('value', '-')
            b_status = '合格' if b.get('passed') else '不合格'
            a_val = a.get('value', '-')
            a_status = '合格' if a.get('passed') else ('不合格' if a else '-')

            if b.get('mock'):
                b_val = f"{b_val} (mock)"
                b_status = 'mock'
            if a.get('mock'):
                a_val = f"{a_val} (mock)"
                a_status = 'mock'

            rows.append([
                title, link, focus_kw,
                framework['label'], DIMENSION_LABELS.get(framework['dimension'], ''),
                str(b_val), b_status,
                str(a_val) if a else '-', a_status if a else '-',
                framework['diagnosis'] if not b.get('passed') else '',
                framework['action'] if not b.get('passed') else '',
            ])

    end_col = chr(64 + len(AUDIT_FEISHU_HEADERS))
    value_range = {
        'range': f'{sheet_id}!A2:{end_col}',
        'values': rows,
    }

    url = f'https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{FEISHU_AUDIT_SHEET_TOKEN}/values_append'
    headers = {
        'Authorization': f'Bearer {token}',
        'Content-Type': 'application/json',
    }
    resp = requests.post(url, headers=headers, json={'valueRange': value_range}, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get('code') != 0:
        raise RuntimeError(f"Feishu write failed: {result}")

    return {'success': True, 'rows_written': len(rows)}
