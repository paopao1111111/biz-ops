"""
用户点踩点赞自动化回复工作流
- 每5分钟轮询 attitude + feedback_info 表
- 调用 Coze 工作流分析 + 匹配FAQ + 生成邮件
- Gmail API 发送邮件
- 飞书群 webhook 通知（匹配失败/退费类）
"""
import os
import sys
import io
import json
import time
import logging
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import requests

from .html_generat import _run_coze_workflow, clean_output_content, WORKFLOW_PROVIDER
from .coze_llm import call_coze_llm
from .prompts import FEEDBACK_REPLY_PROMPT, format_prompt
from .case_analysis import (
    _run_query, _fetch_conversations, extract_message_content,
    _parse_message_json, _has_conversation_content, _get_user_version,
)

COZE_BASE_URL = os.getenv('COZE_BASE_URL', 'http://localhost:8888')
COZE_EMAIL = os.getenv('COZE_EMAIL', '')
COZE_PASSWORD = os.getenv('COZE_PASSWORD', '')
COZE_SPACE_ID = os.getenv('COZE_SPACE_ID', '7628895420063678464')

if sys.stdout and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

FEEDBACK_WORKFLOW_ID = os.getenv('FEEDBACK_WORKFLOW_ID', '7637414629525684224')
NEW_COZE_WORKFLOW_ID = os.getenv('NEW_COZE_WORKFLOW_ID', '').strip()
POLL_INTERVAL = int(os.getenv('FEEDBACK_POLL_INTERVAL', '60'))
from .paths import STORAGE_DIR

PROCESSED_IDS_FILE = os.getenv('PROCESSED_FEEDBACK_IDS_FILE', str(STORAGE_DIR / 'processed_feedback_ids.json'))

GMAIL_SERVICE_ACCOUNT_FILE = os.getenv('GMAIL_SERVICE_ACCOUNT_FILE', '')
GMAIL_SENDER = os.getenv('GMAIL_SENDER', 'iweaver@iweaver.ai')
HTTP_PROXY = os.getenv('HTTP_PROXY', os.getenv('http_proxy', 'http://127.0.0.1:10808'))
HTTPS_PROXY = os.getenv('HTTPS_PROXY', os.getenv('https_proxy', 'http://127.0.0.1:10808'))

_runtime_state = {
    'loop_alive': False,
    'polling_now': False,
    'last_poll_time': None,
    'recent_records': [],
}
_poll_lock = threading.Lock()

FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', '')
FEISHU_FEEDBACK_SHEET_TOKEN = os.getenv('FEISHU_FEEDBACK_SHEET_TOKEN', '')
FEISHU_FEEDBACK_SHEET_ID = os.getenv('FEISHU_FEEDBACK_SHEET_ID', '6ad4cb')


def _load_processed_ids():
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {'attitude_ids': [], 'feedback_ids': []}


def _save_processed_ids(data):
    max_keep = 5000
    data['attitude_ids'] = data['attitude_ids'][-max_keep:]
    data['feedback_ids'] = data['feedback_ids'][-max_keep:]
    with open(PROCESSED_IDS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)


def fetch_new_records(processed):
    now = datetime.utcnow()
    since = (now - timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')

    att_exclude = ','.join(str(i) for i in processed['attitude_ids'][-200:]) if processed['attitude_ids'] else '0'
    sql_att = f"""
        SELECT a.id, a.user_id, a.type, a.chat_logs_id, a.created_time
        FROM attitude a
        WHERE a.deleted = false
          AND a.created_time >= '{since}'
          AND a.id NOT IN ({att_exclude})
        ORDER BY a.created_time ASC
        LIMIT 50
    """
    att_rows, _ = _run_query(sql_att, limit=50)

    fb_exclude = ','.join(str(i) for i in processed['feedback_ids'][-200:]) if processed['feedback_ids'] else '0'
    sql_fb = f"""
        SELECT f.id, f.user_id, f.type, f.message_id, f.feedback_content, f.email, f.created_at
        FROM feedback_info f
        WHERE f.created_at >= '{since}'
          AND f.id NOT IN ({fb_exclude})
        ORDER BY f.created_at ASC
        LIMIT 50
    """
    fb_rows, _ = _run_query(sql_fb, limit=50)

    return att_rows, fb_rows


def _get_user_email(user_id):
    # feedback_info 自带的 email 字段
    sql = f"""
        SELECT email FROM feedback_info
        WHERE user_id = '{user_id}' AND email IS NOT NULL AND email != ''
        ORDER BY created_at DESC LIMIT 1
    """
    rows, _ = _run_query(sql, limit=1)
    if rows and rows[0].get('email'):
        return rows[0]['email']

    # feedback_info.user_id 对应 users.signin_openid 或 users.email
    sql2 = f"""
        SELECT email FROM users
        WHERE (signin_openid = '{user_id}' OR email = '{user_id}')
          AND email IS NOT NULL AND email != ''
        LIMIT 1
    """
    try:
        rows2, _ = _run_query(sql2, limit=1)
        if rows2 and rows2[0].get('email'):
            return rows2[0]['email']
    except Exception:
        pass

    return None


def _get_user_name_from_email(email):
    if not email:
        return None
    local = email.split('@')[0]
    return local.replace('.', ' ').replace('_', ' ').title()


def _fetch_conversation_by_message_id(message_id, user_id):
    """通过 message_id 直接定位到对应的对话（topic）"""
    rows, _ = _run_query(
        f"SELECT topic_id FROM chat_logs WHERE id = '{message_id}' LIMIT 1", limit=1
    )
    if not rows:
        return None
    topic_id = str(rows[0].get('topic_id') or '0')
    logs, _ = _run_query(
        f"SELECT role, message, created_at FROM chat_logs WHERE user_id = '{user_id}' AND topic_id = '{topic_id}' AND deleted = false ORDER BY created_at ASC LIMIT 100",
        limit=100
    )
    convo_text = []
    for row in logs:
        msg_data = _parse_message_json(row.get('message'))
        role = row.get('role', '')
        content = extract_message_content(msg_data, role) or '[empty]'
        if len(content) > 500:
            content = content[:500] + '...'
        convo_text.append(f"{'User' if role == 'user' else 'Assistant'}: {content}")
    return convo_text


def build_feedback_case(user_id, action_type, feedback_content='', email=None, message_id=''):
    if not email:
        email = _get_user_email(user_id)
    user_name = _get_user_name_from_email(email)

    # 有 message_id 时直接定位到那条对话，否则查最近24小时
    convo_text = None
    if message_id:
        convo_text = _fetch_conversation_by_message_id(message_id, user_id)

    if not convo_text:
        now = datetime.utcnow()
        start = (now - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        end = now.strftime('%Y-%m-%d %H:%M:%S')
        convos = _fetch_conversations(user_id, start, end, limit_topics=3)
        convo_text = []
        for convo in convos[:2]:
            for msg in convo.get('messages', [])[:10]:
                role_label = "User" if msg['role'] == 'user' else "Assistant"
                content = msg.get('content', '') or '[empty]'
                if len(content) > 500:
                    content = content[:500] + '...'
                convo_text.append(f"{role_label}: {content}")

    input_data = {
        'action': action_type,
        'user_id': user_id,
        'user_email': email or '',
        'user_name': user_name or '',
        'feedback_content': feedback_content or '',
        'conversation': convo_text,
    }
    return input_data, email


_coze_session_cache = {'key': None, 'ts': 0}


def _get_coze_session(force_refresh=False):
    now = time.time()
    if not force_refresh and _coze_session_cache['key'] and (now - _coze_session_cache['ts'] < 600):
        return _coze_session_cache['key']

    resp = requests.post(
        f'{COZE_BASE_URL}/api/passport/web/email/login/',
        json={'email': COZE_EMAIL, 'password': COZE_PASSWORD},
        timeout=15,
    )
    key = None
    for part in resp.headers.get('Set-Cookie', '').split(';'):
        if part.strip().startswith('session_key='):
            key = part.strip().split('=', 1)[1]
            if key:
                break
    if not key:
        key = resp.cookies.get('session_key')
    if not key:
        logger.error(f'Coze login failed, status={resp.status_code}, body={resp.text[:200]}')
        raise RuntimeError('Failed to get Coze session')

    _coze_session_cache['key'] = key
    _coze_session_cache['ts'] = now
    logger.info('Coze session refreshed')
    return key


def _run_feedback_workflow(input_str, timeout_seconds=120):
    if WORKFLOW_PROVIDER == "local":
        if not NEW_COZE_WORKFLOW_ID:
            return None, 'NEW_COZE_WORKFLOW_ID not configured'
        prompt = format_prompt(FEEDBACK_REPLY_PROMPT, input=input_str)
        r = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": prompt})
        if not r.get("success"):
            return None, r.get("error", "Unknown error")
        return r["output"], None
    session_key = _get_coze_session()
    cookies = {'session_key': session_key}
    headers = {'Content-Type': 'application/json'}

    test_resp = requests.post(
        f'{COZE_BASE_URL}/api/workflow_api/test_run',
        cookies=cookies, headers=headers,
        json={
            'workflow_id': FEEDBACK_WORKFLOW_ID,
            'input': {'input': input_str},
            'space_id': COZE_SPACE_ID,
        },
        timeout=30,
    )
    tr = test_resp.json()
    if tr.get('code') != 0:
        msg = tr.get('msg', '')
        if 'session' in msg.lower() or 'auth' in msg.lower():
            logger.warning('Session expired, refreshing and retrying...')
            session_key = _get_coze_session(force_refresh=True)
            cookies = {'session_key': session_key}
            test_resp = requests.post(
                f'{COZE_BASE_URL}/api/workflow_api/test_run',
                cookies=cookies, headers=headers,
                json={
                    'workflow_id': FEEDBACK_WORKFLOW_ID,
                    'input': {'input': input_str},
                    'space_id': COZE_SPACE_ID,
                },
                timeout=30,
            )
            tr = test_resp.json()
            if tr.get('code') != 0:
                return None, f"test_run failed after retry: {tr.get('msg')}"
        else:
            return None, f"test_run failed: {msg}"

    execute_id = tr['data']['execute_id']

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        time.sleep(5)
        proc_resp = requests.get(
            f'{COZE_BASE_URL}/api/workflow_api/get_process',
            cookies=cookies, headers=headers,
            params={
                'workflow_id': FEEDBACK_WORKFLOW_ID,
                'execute_id': execute_id,
                'space_id': COZE_SPACE_ID,
            },
            timeout=15,
        )
        data = proc_resp.json().get('data', {})
        status = data.get('executeStatus')
        if status == 2:
            for node in data.get('nodeResults', []):
                if node.get('NodeType') == 'End':
                    output_raw = node.get('output', '')
                    try:
                        output_obj = json.loads(output_raw)
                        return output_obj.get('output', ''), None
                    except (json.JSONDecodeError, ValueError):
                        return output_raw, None
            return None, 'No End node output found'
        elif status == 3 or status == 4:
            error_info = ''
            for node in data.get('nodeResults', []):
                if node.get('errorInfo'):
                    error_info = node['errorInfo']
            return None, f'Workflow failed: {error_info}'

    return None, 'Workflow timeout'


def analyze_feedback(input_data):
    input_str = json.dumps(input_data, ensure_ascii=False)
    raw, error = _run_feedback_workflow(input_str, timeout_seconds=120)

    if error:
        return None, error
    if not raw:
        return None, 'Empty output'

    import re
    raw = clean_output_content(raw)
    raw = re.sub(r'^```\w*\s*', '', raw.strip())
    raw = re.sub(r'\s*```$', '', raw.strip())

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        brace = raw.find('{')
        if brace >= 0:
            fragment = raw[brace:]
            try:
                parsed = json.loads(fragment)
            except (json.JSONDecodeError, ValueError):
                parsed = _try_fix_truncated_json(fragment)
                if parsed is None:
                    return None, f'JSON parse failed: {raw[:200]}'
        else:
            return None, f'No JSON in output: {raw[:200]}'

    return parsed, None


def _try_fix_truncated_json(fragment):
    """尝试修复被截断的 JSON，补全缺失的引号和花括号"""
    s = fragment.rstrip()
    # 策略1: 简单补全尝试
    for suffix in ['"}', '""}\n', 'null}', '}']:
        try:
            return json.loads(s + suffix)
        except (json.JSONDecodeError, ValueError):
            continue
    # 策略2: 逐行回退，找到最后一个能构成合法JSON的位置
    lines = s.split('\n')
    for i in range(len(lines) - 1, 0, -1):
        attempt = '\n'.join(lines[:i]).rstrip().rstrip(',') + '\n}'
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
    # 策略3: 找最后一个完整逗号分隔，截断到那里
    last_comma = s.rfind(',')
    if last_comma > 0:
        try:
            return json.loads(s[:last_comma] + '}')
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def send_gmail(to_email, subject, body):
    if not to_email or not subject or not body:
        return False, 'Missing email params'

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        import httplib2
        import google_auth_httplib2
    except ImportError:
        return False, 'google-api-python-client or google-auth-httplib2 not installed'

    try:
        credentials = service_account.Credentials.from_service_account_file(
            GMAIL_SERVICE_ACCOUNT_FILE,
            scopes=['https://www.googleapis.com/auth/gmail.send'],
            subject=GMAIL_SENDER,
        )
        if HTTPS_PROXY:
            from urllib.parse import urlparse
            p = urlparse(HTTPS_PROXY)
            proxy_info = httplib2.ProxyInfo(
                proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
                proxy_host=p.hostname or '127.0.0.1',
                proxy_port=p.port or 10809,
            )
            http = httplib2.Http(proxy_info=proxy_info)
        else:
            http = httplib2.Http()
        authed_http = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
        service = build('gmail', 'v1', http=authed_http)

        message = MIMEText(body, 'plain', 'utf-8')
        message['to'] = to_email
        message['from'] = GMAIL_SENDER
        message['subject'] = subject

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        send_result = service.users().messages().send(
            userId='me', body={'raw': raw_message}
        ).execute()

        return True, send_result.get('id', '')
    except Exception as e:
        return False, str(e)


def _get_feishu_tenant_token():
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"获取飞书 token 失败: {data.get('msg')}")
    return data['tenant_access_token']


def send_feishu_card(title, fields, color='red'):
    """发送飞书消息卡片
    title: 卡片标题
    fields: list of (label, value) 元组
    color: 标题颜色 red/orange/green/blue
    """
    if not FEISHU_CHAT_ID:
        logger.warning('飞书 chat_id 未配置，跳过通知')
        return False

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
        token = _get_feishu_tenant_token()
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            'https://open.feishu.cn/open-apis/im/v1/messages',
            params={'receive_id_type': 'chat_id'},
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json; charset=utf-8',
            },
            json={
                'receive_id': FEISHU_CHAT_ID,
                'msg_type': 'interactive',
                'content': json.dumps(card),
            },
            timeout=15,
        )
        result = resp.json()
        if result.get('code') == 0:
            return True
        logger.error(f"飞书卡片发送失败: {result.get('msg')}")
        return False
    except Exception as e:
        logger.error(f'飞书卡片发送异常: {e}')
        return False


FEISHU_SHEET_APP_ID = os.getenv('FEISHU_SHEET_APP_ID', '')
FEISHU_SHEET_APP_SECRET = os.getenv('FEISHU_SHEET_APP_SECRET', '')


def _get_feishu_sheet_token():
    session = requests.Session()
    session.trust_env = False
    resp = session.post(
        'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
        json={'app_id': FEISHU_SHEET_APP_ID, 'app_secret': FEISHU_SHEET_APP_SECRET},
        timeout=15,
    )
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"获取飞书表格 token 失败: {data.get('msg')}")
    return data['tenant_access_token']


def push_feedback_to_feishu(record_time, user_id, action_type, feedback_content,
                            translation_zh, convo_summary, reason_category,
                            problem_summary, email, process_status,
                            message_id='', user_version=''):
    if not FEISHU_FEEDBACK_SHEET_TOKEN:
        return False

    try:
        token = _get_feishu_sheet_token()
        row = [
            record_time,
            user_id,
            message_id or '',
            user_version or '',
            action_type,
            feedback_content or '',
            translation_zh or '',
            convo_summary or '',
            reason_category or '',
            problem_summary or '',
            email or '',
            process_status or '',
        ]

        value_range = {
            'range': f'{FEISHU_FEEDBACK_SHEET_ID}!A2:L',
            'values': [row],
        }

        session = requests.Session()
        session.trust_env = False
        url = f'https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{FEISHU_FEEDBACK_SHEET_TOKEN}/values_append'
        resp = session.post(url, headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }, json={'valueRange': value_range}, timeout=30)

        result = resp.json()
        if result.get('code') == 0:
            logger.info(f'飞书表格写入成功: user={user_id}')
            return True
        logger.error(f"飞书表格写入失败: {result.get('msg')}")
        return False
    except Exception as e:
        logger.error(f'飞书表格写入异常: {e}')
        return False


def process_single_record(user_id, action_type, feedback_content='', email=None, record_id=None, message_id=''):
    logger.info(f'处理记录: user={user_id}, action={action_type}, id={record_id}')

    if not feedback_content or not feedback_content.strip():
        logger.info(f'用户 {user_id} 无反馈文本，跳过')
        return {'status': 'skipped', 'reason': 'no_feedback_content'}

    input_data, user_email = build_feedback_case(user_id, action_type, feedback_content, email, message_id=message_id)

    analysis, error = analyze_feedback(input_data)
    if error:
        logger.error(f'分析失败: {error}')
        send_feishu_card(
            title="AI分析失败",
            fields=[
                ("用户 ID", str(user_id)),
                ("邮箱", user_email or '无'),
                ("行为", action_type),
                ("反馈原文", (feedback_content or '')[:200]),
                ("错误", str(error)[:200]),
                ("状态", "请人工处理"),
            ],
            color="red",
        )
        return {'status': 'error', 'error': error}

    result = {'status': 'processed', 'analysis': analysis}

    matched_faq = analysis.get('matched_faq')
    match_confidence = analysis.get('match_confidence', '无')
    is_refund = analysis.get('is_refund', False)
    email_subject = analysis.get('email_subject', '')
    email_body = analysis.get('email_body', '')
    feishu_summary = analysis.get('feishu_summary', '')

    if user_email and email_subject and email_body:
        success, msg_id = send_gmail(user_email, email_subject, email_body)
        if success:
            logger.info(f'邮件发送成功: {user_email}, msg_id={msg_id}')
            result['email_sent'] = True
        else:
            logger.error(f'邮件发送失败: {msg_id}')
            result['email_sent'] = False
            result['email_error'] = msg_id
    else:
        result['email_sent'] = False
        if not user_email:
            result['email_error'] = 'no_email'
        else:
            result['email_error'] = 'no_email_content'

    # 每条反馈必发一张飞书卡片，颜色和内容根据情况不同
    if is_refund:
        card_title = "退费/取消订阅请求"
        card_color = "red"
        card_status = "<at id=ou_a88146ad16b8d6889a4f8557f74fc54e></at> 请手动处理"
    elif not matched_faq or match_confidence in ('低', '无'):
        card_title = "待人工处理反馈"
        card_color = "orange"
        card_status = "知识库未命中，请及时介入"
    elif result.get('email_sent'):
        card_title = "已自动回复用户"
        card_color = "green"
        card_status = "邮件已发送"
    elif not user_email:
        card_title = "已匹配但无邮箱"
        card_color = "orange"
        card_status = "用户无邮箱，无法发送邮件"
    else:
        card_title = "邮件发送失败"
        card_color = "red"
        card_status = f"错误: {result.get('email_error', '未知')}"

    send_feishu_card(
        title=card_title,
        fields=[
            ("用户 ID", str(user_id)),
            ("邮箱", user_email or '无'),
            ("行为", action_type),
            ("反馈原文", (feedback_content or '')[:200]),
            ("匹配FAQ", matched_faq or '无'),
            ("邮件主题", email_subject or '无'),
            ("AI 总结", feishu_summary),
            ("状态", card_status),
        ],
        color=card_color,
    )

    # 写入飞书表格
    if matched_faq and match_confidence not in ('低', '无'):
        process_status = '已自动回复' if result.get('email_sent') else '邮件失败'
    elif is_refund:
        process_status = '退费-待人工处理'
    else:
        process_status = '待人工介入'

    convo_summary = '\n'.join(input_data.get('conversation', [])[:6])
    if len(convo_summary) > 500:
        convo_summary = convo_summary[:500] + '...'

    user_version = _get_user_version(user_id)

    push_feedback_to_feishu(
        record_time=datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
        user_id=user_id,
        action_type=action_type,
        feedback_content=feedback_content,
        translation_zh=analysis.get('translation_zh', ''),
        convo_summary=convo_summary,
        reason_category=analysis.get('reason_category', ''),
        problem_summary=analysis.get('problem_summary', ''),
        email=user_email,
        process_status=process_status,
        message_id=message_id,
        user_version=user_version,
    )

    _runtime_state['recent_records'].append({
        'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
        'user_id': str(user_id),
        'action_type': action_type,
        'problem_summary': analysis.get('problem_summary', ''),
        'matched_faq': matched_faq,
        'status': 'replied' if result.get('email_sent') else ('failed' if not matched_faq else 'pending'),
    })
    if len(_runtime_state['recent_records']) > 500:
        _runtime_state['recent_records'] = _runtime_state['recent_records'][-500:]

    return result


def run_poll_cycle():
    if not _poll_lock.acquire(blocking=False):
        logger.info('上一轮轮询还在进行中，跳过')
        return 0
    _runtime_state['polling_now'] = True
    _runtime_state['last_poll_time'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
    try:
        processed = _load_processed_ids()
        att_rows, fb_rows = fetch_new_records(processed)

        if not att_rows and not fb_rows:
            logger.info('无新记录')
            return 0

        total = len(att_rows) + len(fb_rows)
        logger.info(f'发现 {len(att_rows)} 条 attitude + {len(fb_rows)} 条 feedback 新记录')

        count = 0

        for row in fb_rows:
            fb_type = row.get('type', '')
            action = 'thumbs_up' if fb_type == 'thumbs_up' else 'thumbs_down'
            processed['feedback_ids'].append(row['id'])
            _save_processed_ids(processed)
            try:
                result = process_single_record(
                    user_id=row['user_id'],
                    action_type=action,
                    feedback_content=row.get('feedback_content', ''),
                    email=row.get('email'),
                    record_id=row['id'],
                    message_id=row.get('message_id', ''),
                )
                count += 1
                logger.info(f"Feedback #{row['id']} 处理完成: {result.get('status')}")
            except Exception as e:
                logger.exception(f"处理 feedback #{row['id']} 异常: {e}")

        seen_users = {row['user_id'] for row in fb_rows}
        for row in att_rows:
            processed['attitude_ids'].append(row['id'])
            if row['user_id'] in seen_users:
                _save_processed_ids(processed)
                continue
            action = 'thumbs_up' if row.get('type') == 1 else 'thumbs_down'
            seen_users.add(row['user_id'])
            _save_processed_ids(processed)
            try:
                result = process_single_record(
                    user_id=row['user_id'],
                    action_type=action,
                    record_id=row['id'],
                    message_id=row.get('chat_logs_id', ''),
                )
                count += 1
                logger.info(f"Attitude #{row['id']} 处理完成: {result.get('status')}")
            except Exception as e:
                logger.exception(f"处理 attitude #{row['id']} 异常: {e}")

        _save_processed_ids(processed)
        logger.info(f'本轮处理完成: {count}/{total}')
        return count
    finally:
        _runtime_state['polling_now'] = False
        _poll_lock.release()


def get_status():
    records = _runtime_state['recent_records']
    today = datetime.utcnow().strftime('%Y-%m-%d')
    today_records = [r for r in records if r.get('time', '').startswith(today)]
    return {
        'running': _runtime_state['loop_alive'],
        'polling_now': _runtime_state['polling_now'],
        'last_poll_time': _runtime_state['last_poll_time'],
        'today_total': len(today_records),
        'today_replied': sum(1 for r in today_records if r.get('status') == 'replied'),
        'today_pending': sum(1 for r in today_records if r.get('status') in ('pending', 'failed')),
        'today_thumbs_up': sum(1 for r in today_records if r.get('action_type') == 'thumbs_up'),
    }


def get_recent_records(limit=20, offset=0):
    records = list(reversed(_runtime_state['recent_records']))
    total = len(records)
    page = records[offset:offset + limit]
    return {'records': page, 'total': total}


def main():
    logger.info('=== 用户反馈自动回复工作流启动 ===')
    logger.info(f'轮询间隔: {POLL_INTERVAL}s')
    logger.info(f'Coze 工作流 ID: {FEEDBACK_WORKFLOW_ID}')
    logger.info(f'Gmail 发送方: {GMAIL_SENDER}')
    logger.info(f'飞书群 Chat ID: {FEISHU_CHAT_ID}')

    while True:
        try:
            run_poll_cycle()
        except Exception as e:
            logger.exception(f'轮询异常: {e}')
        time.sleep(POLL_INTERVAL)


if __name__ == '__main__':
    main()
