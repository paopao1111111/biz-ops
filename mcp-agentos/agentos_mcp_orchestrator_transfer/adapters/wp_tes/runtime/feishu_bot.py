"""
飞书机器人消息处理模块
在群里 @机器人 发送命令，触发 iWeaver 自动化工作流。
使用 WebSocket 长连接模式，本地主动连接飞书服务器，无需公网 URL。
"""
import os
import sys
import re
import json
import logging
import threading
from datetime import datetime, timedelta
from collections import OrderedDict

import subprocess
import requests
import lark_oapi as lark
from lark_oapi.api.im.v1 import *

logger = logging.getLogger('feishu_bot')
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] Bot: %(message)s'))
    logger.addHandler(handler)

FEISHU_BOT_APP_ID = os.getenv('FEISHU_BOT_APP_ID', '')
FEISHU_BOT_APP_SECRET = os.getenv('FEISHU_BOT_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', '')

_processed_messages = OrderedDict()
MAX_MSG_CACHE = 1000

_insight_topics_cache = {}

_lark_client = None


def _get_lark_client():
    global _lark_client
    if _lark_client is None:
        _lark_client = lark.Client.builder() \
            .app_id(FEISHU_BOT_APP_ID) \
            .app_secret(FEISHU_BOT_APP_SECRET) \
            .log_level(lark.LogLevel.WARNING) \
            .build()
    return _lark_client


# ========== 飞书 API 基础 ==========

def _get_bot_token():
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_BOT_APP_ID, 'app_secret': FEISHU_BOT_APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")
    return data['tenant_access_token']


def reply_text(message_id, text, title='Processing', color='blue'):
    """以卡片形式回复消息"""
    try:
        card = {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": color,
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": text}},
            ],
        }
        client = _get_lark_client()
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(ReplyMessageRequestBody.builder()
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()) \
            .build()
        resp = client.im.v1.message.reply(req)
        if not resp.success():
            logger.error(f'回复消息失败: code={resp.code}, msg={resp.msg}')
    except Exception as e:
        logger.error(f'回复消息异常: {e}')


def send_card(chat_id, title, fields, color='blue'):
    elements = []
    for label, value in fields:
        elements.append({
            "tag": "div",
            "fields": [
                {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**"}},
                {"is_short": True, "text": {"tag": "lark_md", "content": str(value)}},
            ]
        })
        elements.append({"tag": "hr"})
    if elements and elements[-1].get('tag') == 'hr':
        elements.pop()

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": color,
        },
        "elements": elements,
    }
    try:
        client = _get_lark_client()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("interactive")
                .content(json.dumps(card))
                .build()) \
            .build()
        resp = client.im.v1.message.create(req)
        if not resp.success():
            logger.error(f'发送卡片失败: code={resp.code}, msg={resp.msg}')
    except Exception as e:
        logger.error(f'发送卡片异常: {e}')


# ========== 命令执行器 ==========

def execute_publish_blog(keyword, chat_id, message_id):
    try:
        from .blog_generat import generate_blog as _gen_blog, assemble_and_publish
        from .content_audit import fetch_published_posts, audit_post, rewrite_post

        blog_data = _gen_blog(keyword)
        if not blog_data or not blog_data.get('title'):
            send_card(chat_id, 'Blog Failed', [('关键词', keyword), ('错误', '博客生成失败')], 'red')
            return

        wp_result = assemble_and_publish(blog_data, wp_status='draft')
        title = blog_data.get('title', '')
        link = wp_result.get('link', '')

        posts = fetch_published_posts(search=title, per_page=1, content_type='post')
        post_id = None
        if isinstance(posts, list) and posts:
            post_id = posts[0].get('id')
        elif isinstance(posts, dict) and posts.get('posts'):
            post_id = posts['posts'][0].get('id')

        audit_result = None
        rewritten = False
        if post_id:
            audit_result = audit_post(post_id=post_id, content_type='post')
            if audit_result and audit_result.get('success'):
                failed = [i for i in audit_result.get('indicators', []) if not i.get('pass')]
                if failed:
                    rewrite_post(
                        original_html=audit_result.get('content_html', ''),
                        failed_indicators=failed,
                        focus_keywords=audit_result.get('focus_keywords', ''),
                        article_title=audit_result.get('title', ''),
                    )
                    rewritten = True

        passed = 0
        total_ind = 0
        if audit_result and audit_result.get('indicators'):
            total_ind = len(audit_result['indicators'])
            passed = sum(1 for i in audit_result['indicators'] if i.get('pass'))

        color = 'green' if not rewritten else 'orange'
        fields = [
            ('关键词', keyword),
            ('文章标题', title),
            ('链接', link or '—'),
            ('审计', f'{passed}/{total_ind} 通过' if total_ind else '未审计'),
            ('AI 重写', '是' if rewritten else '否'),
        ]
        send_card(chat_id, 'Blog Published', fields, color)

    except Exception as e:
        logger.exception(f'execute_publish_blog error: {e}')
        send_card(chat_id, 'Blog Error', [('关键词', keyword), ('错误', str(e)[:200])], 'red')


def execute_hot_topic(niche, chat_id, message_id):
    try:
        from .blog_generat import generate_hot_topic_blog, assemble_and_publish
        from .content_audit import fetch_published_posts, audit_post, rewrite_post

        blog_data = generate_hot_topic_blog(niche)
        if not blog_data or not blog_data.get('title'):
            send_card(chat_id, 'Hot Topic Failed', [('Niche', niche), ('错误', '热点生成失败')], 'red')
            return

        wp_result = assemble_and_publish(blog_data, wp_status='draft')
        kw = blog_data.get('keyword', '')
        title = blog_data.get('title', '')
        link = wp_result.get('link', '')

        posts = fetch_published_posts(search=title, per_page=1, content_type='post')
        post_id = None
        if isinstance(posts, list) and posts:
            post_id = posts[0].get('id')
        elif isinstance(posts, dict) and posts.get('posts'):
            post_id = posts['posts'][0].get('id')

        rewritten = False
        if post_id:
            audit_result = audit_post(post_id=post_id, content_type='post')
            if audit_result and audit_result.get('success'):
                failed = [i for i in audit_result.get('indicators', []) if not i.get('pass')]
                if failed:
                    rewrite_post(
                        original_html=audit_result.get('content_html', ''),
                        failed_indicators=failed,
                        focus_keywords=audit_result.get('focus_keywords', ''),
                        article_title=audit_result.get('title', ''),
                    )
                    rewritten = True

        color = 'green' if not rewritten else 'orange'
        fields = [
            ('Niche', niche),
            ('发现热词', kw),
            ('文章标题', title),
            ('链接', link or '—'),
            ('AI 重写', '是' if rewritten else '否'),
        ]
        send_card(chat_id, 'Hot Topic Published', fields, color)

    except Exception as e:
        logger.exception(f'execute_hot_topic error: {e}')
        send_card(chat_id, 'Hot Topic Error', [('Niche', niche), ('错误', str(e)[:200])], 'red')


def _load_layout_html(layout):
    """Load a page layout template by name."""
    if layout == 'none':
        return ''
    tpl_path = os.path.join(BASE_DIR, 'page_layouts', f'{layout}.html')
    if os.path.isfile(tpl_path):
        with open(tpl_path, encoding='utf-8') as f:
            return f.read()
    return ''  # fallback: use default in html_text


def execute_gen_page(params_text, chat_id, message_id):
    try:
        from .html_generat import html_text, post_to_wp, build_seo_payload

        kv = {}
        for part in re.split(r'\s+', params_text):
            if '=' in part:
                k, v = part.split('=', 1)
                kv[k.strip().lower()] = v.strip()

        keyword1 = kv.get('keyword1') or kv.get('keyword') or (params_text.split()[0] if params_text.strip() else '')
        if not keyword1:
            reply_text(message_id, '请提供关键词，格式:\n`生成页面 keyword=xxx layout=upload`', title='Parameter Required', color='orange')
            return

        wp_title = kv.get('title', f'Best {keyword1} Tool')
        wp_slug = kv.get('slug', keyword1.lower().replace(' ', '-'))
        page_type = kv.get('type', 'AI Summary')
        layout = kv.get('layout', '')

        input_html = _load_layout_html(layout) if layout else ''

        result = html_text(keyword=keyword1, input_2_html=input_html, use_case_image_list={}, page_slug=wp_slug)
        if not result.get('success'):
            send_card(chat_id, 'Page Failed', [('关键词', keyword1), ('错误', result.get('error', ''))], 'red')
            return

        wp_tag_ids = {'AI Summary': '139', 'AI Writing': '140', 'AI Analysis': '141',
                      'AI Mind Map': '142', 'AI Converter': '145'}.get(page_type, '139')
        seo_data = build_seo_payload(result.get('seo_data', {}), keyword1)
        wp_result = post_to_wp(
            complete_html=result['html'], title=wp_title, slug=wp_slug,
            status='draft', wp_tag_ids=[wp_tag_ids], seo_data=seo_data,
        )

        fields = [
            ('关键词', keyword1),
            ('页面标题', wp_title),
            ('Slug', wp_slug),
            ('类型', page_type),
            ('状态', f'WP {wp_result.get("status_code", "?")}'),
        ]
        send_card(chat_id, 'Page Generated', fields, 'green')

    except Exception as e:
        logger.exception(f'execute_gen_page error: {e}')
        send_card(chat_id, 'Page Error', [('错误', str(e)[:200])], 'red')


def execute_insight(category, keywords, chat_id, message_id):
    try:
        from .insight_article import generate_topics, run_full_pipeline

        if not keywords:
            topic_result = generate_topics(category)
            if not topic_result.get('success'):
                send_card(chat_id, 'Insight Failed', [('分类', category), ('错误', '选题生成失败')], 'red')
                return
            topics = topic_result.get('topics', [])
            topic_keywords = []
            for t in topics[:10]:
                kw = t.get('keyword', t) if isinstance(t, dict) else t
                topic_keywords.append(kw)

            _insight_topics_cache[chat_id] = {
                'category': category,
                'topics': topic_keywords,
            }

            topic_list = '\n'.join(f"**{i+1}.** {kw}" for i, kw in enumerate(topic_keywords))
            reply_text(message_id, f'{topic_list}\n\n回复 `选 1,3,5` 生成对应文章', title=f'Insight Topics — {category}', color='blue')
            return

        success_count = 0
        fail_count = 0
        articles = []
        for kw in keywords:
            result = run_full_pipeline(keyword=kw, category_name=category, generate_images=True)
            if result and result.get('success'):
                success_count += 1
                articles.append(f"{result.get('article_title', kw)}")
            else:
                fail_count += 1

        fields = [
            ('分类', category),
            ('成功', str(success_count)),
            ('失败', str(fail_count)),
            ('文章', '\n'.join(articles) if articles else '—'),
        ]
        color = 'green' if fail_count == 0 else 'orange'
        send_card(chat_id, 'Insight Articles', fields, color)

    except Exception as e:
        logger.exception(f'execute_insight error: {e}')
        send_card(chat_id, 'Insight Error', [('分类', category), ('错误', str(e)[:200])], 'red')


def execute_audit_fix(search, chat_id, message_id):
    try:
        from .content_audit import fetch_published_posts, audit_post, rewrite_post

        posts_data = fetch_published_posts(page=1, per_page=20, search=search, content_type='post')
        posts = []
        if isinstance(posts_data, list):
            posts = posts_data
        elif isinstance(posts_data, dict):
            posts = posts_data.get('posts', [])

        if not posts:
            reply_text(message_id, f'未找到匹配的文章 (搜索: "{search or "全部"}")', title='No Results', color='orange')
            return

        reply_text(message_id, f'找到 **{len(posts)}** 篇文章，开始逐篇审计...', title='Audit Started', color='blue')

        total = len(posts)
        passed_all = 0
        rewritten_count = 0
        failed_audit = 0

        for p in posts:
            pid = p.get('id')
            if not pid:
                continue
            audit_result = audit_post(post_id=pid, content_type='post')
            if not audit_result or not audit_result.get('success'):
                failed_audit += 1
                continue

            failed_indicators = [i for i in audit_result.get('indicators', []) if not i.get('pass')]
            if not failed_indicators:
                passed_all += 1
                continue

            rewrite_result = rewrite_post(
                original_html=audit_result.get('content_html', ''),
                failed_indicators=failed_indicators,
                focus_keywords=audit_result.get('focus_keywords', ''),
                article_title=audit_result.get('title', ''),
            )
            if rewrite_result and rewrite_result.get('success'):
                rewritten_count += 1
            else:
                failed_audit += 1

        fields = [
            ('搜索', search or '全部'),
            ('审计总数', str(total)),
            ('全部通过', str(passed_all)),
            ('AI 修复', str(rewritten_count)),
            ('审计失败', str(failed_audit)),
        ]
        color = 'green' if failed_audit == 0 else 'orange'
        send_card(chat_id, 'Audit Report', fields, color)

    except Exception as e:
        logger.exception(f'execute_audit_fix error: {e}')
        send_card(chat_id, 'Audit Error', [('错误', str(e)[:200])], 'red')


def execute_case_analysis(date_str, chat_id, message_id):
    try:
        from .case_analysis import run_case_analysis as _run

        if date_str in ('昨天', 'yesterday'):
            target = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%d')
        elif date_str in ('今天', 'today', ''):
            target = datetime.now().strftime('%Y-%m-%d')
        else:
            target = date_str

        reply_text(message_id, f'开始运行案例分析 (**{target}**)，预计 5-10 分钟...', title='Case Analysis Started', color='blue')

        result = _run(target_date=target)
        if not result:
            send_card(chat_id, 'Case Analysis Failed', [('日期', target), ('错误', '无返回结果')], 'red')
            return

        fields = [
            ('日期', target),
            ('采样总数', str(result.get('total_sampled', '?'))),
            ('分析完成', str(result.get('analysis_count', '?'))),
            ('错误数', str(result.get('error_count', 0))),
        ]

        by_type = result.get('by_type', {})
        if by_type:
            type_str = ' | '.join(f'{k}:{v}' for k, v in by_type.items())
            fields.append(('类型分布', type_str))

        feishu_info = result.get('feishu', {})
        if feishu_info:
            fields.append(('飞书写入', f"{feishu_info.get('rows_written', '?')} 行"))

        color = 'green' if result.get('success') else 'red'
        send_card(chat_id, 'Case Analysis Done', fields, color)

    except Exception as e:
        logger.exception(f'execute_case_analysis error: {e}')
        send_card(chat_id, 'Case Analysis Error', [('错误', str(e)[:200])], 'red')


def execute_ops_check(chat_id, message_id):
    try:
        from .feedback_auto_reply import get_status as get_fb_status, get_recent_records as get_fb_records
        from .edm_auto_reply import get_edm_status, get_edm_recent_records

        fb_status = {}
        edm_status = {}

        try:
            fb_status = get_fb_status()
        except Exception as e:
            fb_status = {'error': str(e)}

        try:
            edm_status = get_edm_status()
        except Exception as e:
            edm_status = {'error': str(e)}

        has_alert = False
        alert_reasons = []

        fb_running = fb_status.get('running', False)
        edm_running = edm_status.get('running', False)

        if 'error' in fb_status:
            has_alert = True
            alert_reasons.append(f'Feedback: 无法连接 ({fb_status["error"][:50]})')
        elif not fb_running:
            has_alert = True
            alert_reasons.append('Feedback: 系统已停止')

        if 'error' in edm_status:
            has_alert = True
            alert_reasons.append(f'EDM: 无法连接 ({edm_status["error"][:50]})')
        elif not edm_running:
            has_alert = True
            alert_reasons.append('EDM: 系统已停止')

        fb_fields = [
            ('Feedback 状态', '运行中' if fb_running else ('错误' if 'error' in fb_status else '已停止')),
            ('上次轮询', fb_status.get('last_poll_time', '—')),
            ('今日统计', f"处理 {fb_status.get('today_total', 0)} | 回复 {fb_status.get('today_replied', 0)} | 待处理 {fb_status.get('today_pending', 0)}"),
        ]

        edm_fields = [
            ('EDM 状态', '运行中' if edm_running else ('错误' if 'error' in edm_status else '已停止')),
            ('上次轮询', edm_status.get('last_poll_time', '—')),
            ('今日统计', f"处理 {edm_status.get('today_total', 0)} | 回复 {edm_status.get('today_replied', 0)} | 待处理 {edm_status.get('today_pending', 0)} | 高风险 {edm_status.get('today_high_risk', 0)}"),
        ]

        if has_alert:
            alert_fields = [('告警', '\n'.join(alert_reasons))] + fb_fields + edm_fields
            send_card(chat_id, 'Ops Alert', alert_fields, 'red')
        else:
            all_fields = fb_fields + edm_fields + [('整体状态', '全部正常')]
            send_card(chat_id, 'Ops Report', all_fields, 'green')

    except Exception as e:
        logger.exception(f'execute_ops_check error: {e}')
        send_card(chat_id, 'Ops Check Error', [('错误', str(e)[:200])], 'red')


# ========== Claude CLI 按需调用 ==========

def _invoke_claude(chat_id, message_id, text):
    """在后台线程中调用 claude CLI 处理模糊消息，完成后通过 MCP 回复飞书。"""
    prompt = (
        f"飞书群里收到一条消息需要你处理。\n"
        f"chat_id: {chat_id}\n"
        f"message_id: {message_id}\n"
        f"用户消息: {text}\n\n"
        f"请理解用户意图，判断属于哪个工作流并执行：\n"
        f"- 博客相关 → generate_blog(keyword)\n"
        f"- 热点相关 → generate_hot_topic(niche)\n"
        f"- 洞察/insight → generate_insight_topics(category) 或 generate_insight_article(keyword, category)\n"
        f"- 审计 → list_posts() + audit_post() + rewrite_post()\n"
        f"- 页面 → generate_page(...)\n"
        f"- 分析/case → run_case_analysis(target_date)\n"
        f"- 巡检/状态 → feedback_status() + edm_status()\n\n"
        f"执行完成后，用 reply_feishu(chat_id='{chat_id}', text=结果摘要, title=标题, color=颜色, message_id='{message_id}') 将结果发回飞书。\n"
        f"如果无法理解意图，用 reply_feishu 回复可用命令提示。"
    )
    try:
        result = subprocess.run(
            ['claude', '-p', prompt, '--allowedTools', 'mcp__iweaver-tools__*'],
            capture_output=True, text=True, timeout=600, cwd=os.path.expanduser('~'),
        )
        if result.returncode != 0:
            logger.error(f'Claude CLI 失败: {result.stderr[:500]}')
            reply_text(message_id, f'Claude 处理失败，请稍后重试', title='Error', color='red')
    except subprocess.TimeoutExpired:
        logger.error('Claude CLI 超时 (10min)')
        reply_text(message_id, '处理超时，请稍后重试', title='Timeout', color='red')
    except FileNotFoundError:
        logger.error('claude 命令未找到，请确认已安装 Claude Code CLI')
        reply_text(message_id, '系统配置错误: claude CLI 未安装', title='Error', color='red')
    except Exception as e:
        logger.error(f'调用 Claude CLI 异常: {e}')
        reply_text(message_id, f'处理异常: {str(e)[:200]}', title='Error', color='red')


# ========== 命令路由 ==========

COMMAND_PREFIXES = {
    '发博客': 'publish_blog', '博客': 'publish_blog',
    '热点': 'hot_topic', 'hot topic': 'hot_topic',
    '生成页面': 'gen_page', '页面': 'gen_page',
    '洞察': 'insight', 'insight': 'insight',
    '选': 'pick_insight', 'pick': 'pick_insight',
    '审计修复': 'audit_fix', '审计': 'audit_fix', 'audit': 'audit_fix',
    '分析': 'case_analysis', 'analysis': 'case_analysis', 'case': 'case_analysis',
    '巡检': 'ops_check', '状态': 'ops_check', 'check': 'ops_check', 'ops': 'ops_check',
    '帮助': 'help', 'help': 'help', '命令': 'help',
}

HELP_TEXT = """**博客** <关键词> — 生成SEO博客并审计
**热点** [niche] — 自动发现热点并生成博客
**生成页面** keyword=xxx layout=upload — 生成SEO页面（layout: upload/upload-input/upload-link/all/none）
**洞察** <分类> — 生成选题列表
**选** 1,3,5 — 选择选题序号生成文章
**审计** [搜索词] — 批量审计并自动修复
**分析** [日期/昨天/今天] — 运行每日案例分析
**巡检** — 检查Feedback和EDM系统状态
**帮助** — 显示此列表"""


def route_command(text, chat_id, message_id, sender_open_id):
    text = text.strip()
    if not text:
        return

    # Try prefix matching (longest match first)
    cmd_name = None
    params = ''
    for prefix in sorted(COMMAND_PREFIXES.keys(), key=len, reverse=True):
        if text.startswith(prefix):
            cmd_name = COMMAND_PREFIXES[prefix]
            params = text[len(prefix):].strip()
            break

    if not cmd_name:
        # No command matched, send to Claude
        reply_text(message_id, '收到，Claude 正在思考中...\n预计 1-2 分钟内回复', title='Thinking', color='blue')
        threading.Thread(target=_invoke_claude, args=(chat_id, message_id, text), daemon=True).start()
        return

    if cmd_name == 'help':
        reply_text(message_id, HELP_TEXT, title='Help', color='blue')

    elif cmd_name == 'publish_blog':
        keyword = params
        if not keyword:
            reply_text(message_id, '请提供关键词，例如: `博客 AI客服`', title='Need Keyword', color='orange')
            return
        reply_text(message_id, f'正在生成博客: **{keyword}**\n预计 1-2 分钟，完成后发送结果卡片', title='Blog Started', color='blue')
        threading.Thread(target=execute_publish_blog, args=(keyword, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'hot_topic':
        niche = params or 'AI productivity tools'
        reply_text(message_id, f'正在扫描热点: **{niche}**\n预计 2-3 分钟，完成后发送结果卡片', title='Hot Topic Started', color='blue')
        threading.Thread(target=execute_hot_topic, args=(niche, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'gen_page':
        if not params:
            reply_text(message_id, '请提供参数，例如:\n`生成页面 keyword=PDF转换器 layout=upload`', title='Need Params', color='orange')
            return
        reply_text(message_id, '正在生成 SEO 页面...\n预计 1-2 分钟，完成后发送结果卡片', title='Page Started', color='blue')
        threading.Thread(target=execute_gen_page, args=(params, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'insight':
        if not params:
            reply_text(message_id, '请提供分类，例如: `洞察 客服管理`', title='Need Category', color='orange')
            return
        parts = params.split(None, 1)
        category = parts[0]
        kw_str = parts[1] if len(parts) > 1 else ''
        keywords = [k.strip() for k in kw_str.split(',') if k.strip()] if kw_str else []
        if not keywords:
            reply_text(message_id, f'正在为 **{category}** 生成选题...', title='Insight Started', color='blue')
        else:
            reply_text(message_id, f'正在生成 {len(keywords)} 篇 Insight 文章...\n预计 1-3 分钟', title='Insight Started', color='blue')
        threading.Thread(target=execute_insight, args=(category, keywords, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'pick_insight':
        cache = _insight_topics_cache.get(chat_id)
        if not cache:
            reply_text(message_id, '没有待选的选题列表\n请先发 `洞察 <分类>` 生成选题', title='No Topics', color='orange')
            return
        indices = []
        for part in re.split(r'[,，\s]+', params):
            part = part.strip()
            if part.isdigit():
                indices.append(int(part))
        if not indices:
            reply_text(message_id, '请输入序号，例如: `选 1,3,5`', title='Invalid Input', color='orange')
            return
        category = cache['category']
        topics = cache['topics']
        keywords = []
        for idx in indices:
            if 1 <= idx <= len(topics):
                keywords.append(topics[idx - 1])
        if not keywords:
            reply_text(message_id, f'序号超出范围 (1-{len(topics)})', title='Invalid Input', color='orange')
            return
        selected = '\n'.join(f'**{i+1}.** {kw}' for i, kw in enumerate(keywords))
        reply_text(message_id, f'正在生成 {len(keywords)} 篇文章:\n{selected}\n\n预计 1-3 分钟/篇', title='Insight Generating', color='blue')
        del _insight_topics_cache[chat_id]
        threading.Thread(target=execute_insight, args=(category, keywords, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'audit_fix':
        search = params
        reply_text(message_id, f'正在审计文章{" (" + search + ")" if search else ""}...\n逐篇检查并自动修复', title='Audit Started', color='blue')
        threading.Thread(target=execute_audit_fix, args=(search, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'case_analysis':
        date_str = params
        threading.Thread(target=execute_case_analysis, args=(date_str, chat_id, message_id), daemon=True).start()

    elif cmd_name == 'ops_check':
        reply_text(message_id, '正在检查 Feedback 和 EDM 系统状态...', title='Ops Check Started', color='blue')
        threading.Thread(target=execute_ops_check, args=(chat_id, message_id), daemon=True).start()


# ========== WebSocket 消息处理 ==========

def _on_receive_message(data):
    """lark_oapi ws 事件回调"""
    try:
        event = data.event
        message = event.message
        message_id = message.message_id

        # 去重
        if message_id in _processed_messages:
            return
        _processed_messages[message_id] = True
        while len(_processed_messages) > MAX_MSG_CACHE:
            _processed_messages.popitem(last=False)

        if message.message_type != 'text':
            return

        chat_id = message.chat_id
        chat_type = message.chat_type
        sender_open_id = event.sender.sender_id.open_id if event.sender and event.sender.sender_id else ''

        content = json.loads(message.content or '{}')
        text = content.get('text', '')

        mentions = message.mentions or []
        for mention in mentions:
            if mention.key:
                text = text.replace(mention.key, '')
        text = text.strip()

        if chat_type == 'group' and not mentions:
            return

        if text:
            logger.info(f'收到命令: "{text}" from={sender_open_id} chat={chat_id}')
            route_command(text, chat_id, message_id, sender_open_id)

    except Exception as e:
        logger.exception(f'处理消息异常: {e}')


# ========== 启动 WebSocket 客户端 ==========

_ws_client = None


def start_ws_client():
    """启动飞书 WebSocket 长连接，阻塞运行。"""
    global _ws_client

    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(_on_receive_message) \
        .build()

    _ws_client = lark.ws.Client(
        app_id=FEISHU_BOT_APP_ID,
        app_secret=FEISHU_BOT_APP_SECRET,
        event_handler=event_handler,
        log_level=lark.LogLevel.INFO,
    )

    logger.info('飞书机器人 WebSocket 连接启动中...')
    _ws_client.start()


def start_ws_client_background():
    """在后台线程启动 WebSocket 客户端"""
    t = threading.Thread(target=start_ws_client, daemon=True)
    t.start()
    logger.info('飞书机器人后台线程已启动')
    return t


# ========== Webhook 模式（备用） ==========

def handle_feishu_event(data):
    """Flask webhook 模式入口（备用，需要公网 URL）"""
    if not data:
        return {'code': 0}

    if data.get('type') == 'url_verification':
        return {'challenge': data.get('challenge', '')}

    header = data.get('header', {})
    event = data.get('event', {})

    if header.get('event_type') != 'im.message.receive_v1':
        return {'code': 0}

    message = event.get('message', {})
    message_id = message.get('message_id', '')

    if message_id in _processed_messages:
        return {'code': 0}
    _processed_messages[message_id] = True
    while len(_processed_messages) > MAX_MSG_CACHE:
        _processed_messages.popitem(last=False)

    if message.get('message_type') != 'text':
        return {'code': 0}

    chat_id = message.get('chat_id', '')
    chat_type = message.get('chat_type', '')
    sender = event.get('sender', {})
    sender_open_id = sender.get('sender_id', {}).get('open_id', '')

    content = json.loads(message.get('content', '{}'))
    text = content.get('text', '')

    mentions = message.get('mentions', [])
    for mention in mentions:
        key = mention.get('key', '')
        if key:
            text = text.replace(key, '')
    text = text.strip()

    if chat_type == 'group' and not mentions:
        return {'code': 0}

    if text:
        logger.info(f'[Webhook] 收到命令: "{text}" from={sender_open_id}')
        route_command(text, chat_id, message_id, sender_open_id)

    return {'code': 0}


if __name__ == '__main__':
    print('=== 飞书机器人独立运行模式 ===')
    print(f'App ID: {FEISHU_BOT_APP_ID}')
    print(f'Chat ID: {FEISHU_CHAT_ID}')
    print('正在连接飞书 WebSocket...')
    start_ws_client()
