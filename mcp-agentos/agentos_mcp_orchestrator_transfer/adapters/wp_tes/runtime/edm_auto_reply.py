"""
EDM 邮件自动回复系统
- 每5分钟轮询 iweaver@iweaver.ai 收件箱未读邮件
- 调用 Coze 工作流进行分类 + FAQ匹配 + 生成回复
- Gmail API 自动发送回复
- 飞书群通知（绿卡=已回复/红卡=高风险/橙卡=待人工处理）
"""
import os
import sys
import io
import json
import time
import logging
import base64
import re
import threading
from datetime import datetime
from email.mime.text import MIMEText

import requests

if sys.stdout and hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

COZE_BASE_URL = os.getenv('COZE_BASE_URL', 'http://localhost:8888')
COZE_EMAIL = os.getenv('COZE_EMAIL', '')
COZE_PASSWORD = os.getenv('COZE_PASSWORD', '')
COZE_SPACE_ID = os.getenv('COZE_SPACE_ID', '7628895420063678464')
EDM_WORKFLOW_ID = os.getenv('EDM_WORKFLOW_ID', '7637836408522014720')
NEW_COZE_WORKFLOW_ID = os.getenv('NEW_COZE_WORKFLOW_ID', '').strip()
WORKFLOW_PROVIDER = os.getenv('WORKFLOW_PROVIDER', 'coze').strip().lower()
from .coze_llm import call_coze_llm
from .prompts import EDM_REPLY_PROMPT, format_prompt

EDM_POLL_INTERVAL = int(os.getenv('EDM_POLL_INTERVAL', '300'))
from .paths import STORAGE_DIR

PROCESSED_EMAILS_FILE = os.getenv('PROCESSED_EMAILS_FILE', str(STORAGE_DIR / 'processed_email_ids.json'))

GMAIL_SERVICE_ACCOUNT_FILE = os.getenv('GMAIL_SERVICE_ACCOUNT_FILE', '')
GMAIL_SENDER = os.getenv('GMAIL_SENDER', 'iweaver@iweaver.ai')
HTTPS_PROXY = os.getenv('HTTPS_PROXY', os.getenv('https_proxy', 'http://127.0.0.1:10808'))

GMAIL_SCOPES = [
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly',
    'https://www.googleapis.com/auth/gmail.modify',
]

FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', '')

_edm_runtime_state = {
    'loop_alive': False,
    'polling_now': False,
    'last_poll_time': None,
    'recent_records': [],
}
_edm_poll_lock = threading.Lock()

_coze_session_cache = {'key': None, 'ts': 0}

SKIP_SENDERS = ['noreply@', 'no-reply@', 'mailer-daemon@', 'postmaster@',
                'notifications@', 'alert@', 'iweaver@iweaver.ai', 'notify@',
                'marketing@', 'promo@', 'newsletter@', 'news@', 'ads@',
                'offer@', 'deals@', 'campaign@', 'bulk@', 'mass@',
                'mailchimp.com', 'sendgrid.net', 'hubspot.com',
                'constantcontact.com', 'sendinblue.com', 'mailgun.org',
                'subscriptions@medium.com', '@medium.com', '@quora.com',
                '@linkedin.com', '@facebookmail.com', '@twitter.com',
                '@x.com', '@reddit.com', '@pinterest.com',
                'digest@', 'weekly@', 'daily@', 'update@', 'updates@',
                'info@', 'hello@', 'team@', 'community@',
                '@producthunt.com', '@substack.com', '@beehiiv.com',
                '@mailbrew.com', '@revue.email', '@ghost.io',
                'notification@', 'donotreply@', 'do-not-reply@',
                'support@',
                '@anthropic.com', '@openai.com', '@elementor.com', '@ucloud-global.com']
SKIP_SUBJECTS = ['out of office', 'auto-reply', 'delivery status',
                 'undeliverable', 'automatic reply', 'autoreply',
                 'unsubscribe', 'newsletter', 'promotion', 'promotional',
                 'limited time', 'special offer', 'exclusive deal',
                 'act now', 'free trial', 'discount', 'coupon',
                 'black friday', 'cyber monday', '% off',
                 'digest', 'weekly roundup', 'daily digest',
                 'suggested spaces', 'recommended for you',
                 'trending on', 'top stories', 'what are the best',
                 'your weekly', 'your daily', 'new from',
                 'invitation to', 'join us', 'webinar',
                 'you might like', 'picks for you', 'curated for you']


# ============ Gmail Service ============

def _build_gmail_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    import httplib2
    import google_auth_httplib2

    credentials = service_account.Credentials.from_service_account_file(
        GMAIL_SERVICE_ACCOUNT_FILE,
        scopes=GMAIL_SCOPES,
        subject=GMAIL_SENDER,
    )
    if HTTPS_PROXY:
        from urllib.parse import urlparse
        p = urlparse(HTTPS_PROXY)
        proxy_info = httplib2.ProxyInfo(
            proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
            proxy_host=p.hostname or '127.0.0.1',
            proxy_port=p.port or 10808,
        )
        http = httplib2.Http(proxy_info=proxy_info)
    else:
        http = httplib2.Http()
    authed_http = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
    return build('gmail', 'v1', http=authed_http)


def fetch_unread_emails(max_results=10):
    service = _build_gmail_service()
    query = 'is:unread in:inbox -category:promotions -category:social -category:updates -in:spam'
    results = service.users().messages().list(
        userId='me', q=query, maxResults=max_results
    ).execute()
    messages = results.get('messages', [])
    emails = []
    for msg in messages:
        full_msg = service.users().messages().get(
            userId='me', id=msg['id'], format='full'
        ).execute()
        parsed = _parse_email_message(full_msg)
        if parsed:
            emails.append(parsed)
    return emails


def _parse_email_message(msg):
    headers = {}
    for h in msg.get('payload', {}).get('headers', []):
        headers[h['name'].lower()] = h['value']

    from_raw = headers.get('from', '')
    from_name, from_email = _parse_from_header(from_raw)
    subject = headers.get('subject', '')
    body_text = _extract_body_text(msg.get('payload', {}))
    received_at = headers.get('date', '')
    message_id_header = headers.get('message-id', '')

    return {
        'message_id': msg['id'],
        'thread_id': msg.get('threadId', ''),
        'from_email': from_email,
        'from_name': from_name,
        'subject': subject,
        'body_text': body_text[:3000],
        'received_at': received_at,
        'message_id_header': message_id_header,
    }


def _parse_from_header(from_raw):
    match = re.match(r'^"?(.+?)"?\s*<(.+?)>$', from_raw.strip())
    if match:
        return match.group(1).strip().strip('"'), match.group(2).strip()
    if '@' in from_raw:
        return '', from_raw.strip()
    return from_raw.strip(), ''


def _extract_body_text(payload):
    mime_type = payload.get('mimeType', '')
    if mime_type == 'text/plain':
        data = payload.get('body', {}).get('data', '')
        if data:
            return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
    parts = payload.get('parts', [])
    for part in parts:
        if part.get('mimeType') == 'text/plain':
            data = part.get('body', {}).get('data', '')
            if data:
                return base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
    for part in parts:
        if part.get('mimeType') == 'text/html':
            data = part.get('body', {}).get('data', '')
            if data:
                html = base64.urlsafe_b64decode(data).decode('utf-8', errors='replace')
                return _strip_html(html)
        if part.get('parts'):
            result = _extract_body_text(part)
            if result:
                return result
    return ''


def _strip_html(html):
    text = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<br\s*/?>|</p>|</div>|</li>|</tr>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'&amp;', '&', text)
    text = re.sub(r'&lt;', '<', text)
    text = re.sub(r'&gt;', '>', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def mark_as_read(message_id):
    service = _build_gmail_service()
    service.users().messages().modify(
        userId='me', id=message_id,
        body={'removeLabelIds': ['UNREAD']}
    ).execute()


def _is_system_email(email_data):
    sender = email_data['from_email'].lower()
    subject = email_data['subject'].lower()
    if any(skip in sender for skip in SKIP_SENDERS):
        return True
    if any(skip in subject for skip in SKIP_SUBJECTS):
        return True
    return False


# ============ Gmail Reply ============

def send_gmail_reply(to_email, subject, body, thread_id='', in_reply_to=''):
    if not to_email or not body:
        return False, 'Missing email params'

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        import httplib2
        import google_auth_httplib2
    except ImportError:
        return False, 'google-api-python-client not installed'

    try:
        credentials = service_account.Credentials.from_service_account_file(
            GMAIL_SERVICE_ACCOUNT_FILE,
            scopes=GMAIL_SCOPES,
            subject=GMAIL_SENDER,
        )
        if HTTPS_PROXY:
            from urllib.parse import urlparse
            p = urlparse(HTTPS_PROXY)
            proxy_info = httplib2.ProxyInfo(
                proxy_type=httplib2.socks.PROXY_TYPE_HTTP,
                proxy_host=p.hostname or '127.0.0.1',
                proxy_port=p.port or 10808,
            )
            http = httplib2.Http(proxy_info=proxy_info)
        else:
            http = httplib2.Http()
        authed_http = google_auth_httplib2.AuthorizedHttp(credentials, http=http)
        service = build('gmail', 'v1', http=authed_http)

        message = MIMEText(body, 'plain', 'utf-8')
        message['to'] = to_email
        message['from'] = GMAIL_SENDER
        message['subject'] = subject or 'Re: your inquiry'
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
            message['References'] = in_reply_to

        raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        send_body = {'raw': raw_message}
        if thread_id:
            send_body['threadId'] = thread_id

        send_result = service.users().messages().send(
            userId='me', body=send_body
        ).execute()
        return True, send_result.get('id', '')
    except Exception as e:
        return False, str(e)


# ============ Coze Workflow ============

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
        logger.error(f'Coze login failed, status={resp.status_code}')
        raise RuntimeError('Failed to get Coze session')

    _coze_session_cache['key'] = key
    _coze_session_cache['ts'] = now
    logger.info('Coze session refreshed (EDM)')
    return key


def _run_edm_workflow(input_str, timeout_seconds=120):
    if not EDM_WORKFLOW_ID:
        return None, 'EDM_WORKFLOW_ID not configured'

    if WORKFLOW_PROVIDER == "local":
        if not NEW_COZE_WORKFLOW_ID:
            return None, 'NEW_COZE_WORKFLOW_ID not configured'
        # Extract extra fields from input for prompt variables
        try:
            input_data = json.loads(input_str)
            from_name = input_data.get('from_name', '')
            subject = input_data.get('subject', '')
        except (json.JSONDecodeError, ValueError):
            from_name = ''
            subject = ''
        prompt = format_prompt(EDM_REPLY_PROMPT, input=input_str, **{'from_name': from_name, '原始邮件主题': subject})
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
            'workflow_id': EDM_WORKFLOW_ID,
            'input': {'input': input_str},
            'space_id': COZE_SPACE_ID,
        },
        timeout=30,
    )
    tr = test_resp.json()
    if tr.get('code') != 0:
        msg = tr.get('msg', '')
        if 'session' in msg.lower() or 'auth' in msg.lower():
            logger.warning('EDM: Session expired, refreshing...')
            session_key = _get_coze_session(force_refresh=True)
            cookies = {'session_key': session_key}
            test_resp = requests.post(
                f'{COZE_BASE_URL}/api/workflow_api/test_run',
                cookies=cookies, headers=headers,
                json={
                    'workflow_id': EDM_WORKFLOW_ID,
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
                'workflow_id': EDM_WORKFLOW_ID,
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
        elif status in (3, 4):
            error_info = ''
            for node in data.get('nodeResults', []):
                if node.get('errorInfo'):
                    error_info = node['errorInfo']
            return None, f'Workflow failed: {error_info}'

    return None, 'Workflow timeout'


def _parse_workflow_output(raw):
    if not raw:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    raw = raw.strip()
    raw = re.sub(r'^```\w*\s*', '', raw)
    raw = re.sub(r'\s*```$', '', raw)
    raw = raw.strip()

    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        brace = raw.find('{')
        if brace >= 0:
            fragment = raw[brace:]
            try:
                return json.loads(fragment)
            except (json.JSONDecodeError, ValueError):
                return _try_fix_truncated_json(fragment)
    return None


def _try_fix_truncated_json(fragment):
    s = fragment.rstrip()
    for suffix in ['"}', '""}\n', 'null}', '}']:
        try:
            return json.loads(s + suffix)
        except (json.JSONDecodeError, ValueError):
            continue
    lines = s.split('\n')
    for i in range(len(lines) - 1, 0, -1):
        attempt = '\n'.join(lines[:i]).rstrip().rstrip(',') + '\n}'
        try:
            return json.loads(attempt)
        except (json.JSONDecodeError, ValueError):
            continue
    last_comma = s.rfind(',')
    if last_comma > 0:
        try:
            return json.loads(s[:last_comma] + '}')
        except (json.JSONDecodeError, ValueError):
            pass
    return None


# ============ Feishu Notification ============

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


def send_feishu_card(title, fields, color='green'):
    if not FEISHU_CHAT_ID:
        logger.warning('飞书 chat_id 未配置')
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


# ============ Processing Pipeline ============

def process_single_email(email_data):
    input_data = {
        'from_email': email_data['from_email'],
        'from_name': email_data['from_name'],
        'subject': email_data['subject'],
        'body': email_data['body_text'],
        'received_at': email_data['received_at'],
    }
    input_str = json.dumps(input_data, ensure_ascii=False)

    raw, error = _run_edm_workflow(input_str, timeout_seconds=120)
    if error:
        logger.error(f"EDM workflow error: {error}")
        _send_feishu_edm_card(email_data, {'action': 'notify_only', 'category': 'error',
                              'feishu_summary': f'工作流错误: {error[:100]}'}, color='red')
        return {'action': 'notify_only', 'category': 'error', 'error': error}

    analysis = _parse_workflow_output(raw)
    if not analysis:
        logger.error(f"EDM parse failed: {str(raw)[:200]}")
        _send_feishu_edm_card(email_data, {'action': 'notify_only', 'category': 'parse_error',
                              'feishu_summary': f'解析失败: {str(raw)[:80]}'}, color='orange')
        return {'action': 'notify_only', 'category': 'parse_error'}

    action = analysis.get('action', 'notify_only')
    category = analysis.get('category', '').lower()

    if category in ('spam', 'ad', 'advertisement', 'irrelevant', 'subscription', 'notification'):
        logger.info(f"EDM: Skip spam/ad email from {email_data['from_email']} (category={category})")
        return analysis

    if action == 'auto_reply':
        email_subject = analysis.get('email_subject', f"Re: {email_data['subject']}")
        email_body = analysis.get('email_body', '')
        if email_body:
            success, msg_id = send_gmail_reply(
                to_email=email_data['from_email'],
                subject=email_subject,
                body=email_body,
                thread_id=email_data.get('thread_id', ''),
                in_reply_to=email_data.get('message_id_header', ''),
            )
            if success:
                logger.info(f"EDM auto-reply sent to {email_data['from_email']}")
                analysis['reply_sent'] = True
            else:
                logger.error(f"EDM reply failed: {msg_id}")
                analysis['reply_sent'] = False
                analysis['reply_error'] = msg_id
        _send_feishu_edm_card(email_data, analysis, color='green')

    elif action == 'notify_ops':
        logger.info(f"EDM: 未匹配FAQ，转人工处理 - {email_data['from_email']}")
        _send_feishu_edm_card(email_data, analysis, color='red')

    else:
        _send_feishu_edm_card(email_data, analysis, color='orange')

    return analysis


def _send_feishu_edm_card(email_data, analysis, color='green'):
    action = analysis.get('action', 'notify_only')
    action_labels = {
        'auto_reply': '已自动回复',
        'notify_ops': '⚠️ 高风险 - 需人工处理',
        'notify_only': '待人工处理',
    }
    title = f"[EDM] {action_labels.get(action, '未知')}"

    fields = [
        ("发件人", email_data.get('from_name', '') or '未知'),
        ("用户邮箱", email_data.get('from_email', '') or '未知'),
        ("邮件主题", email_data.get('subject', '')[:60]),
        ("分类", analysis.get('category', '未知')),
        ("匹配FAQ", analysis.get('matched_faq') or '无'),
        ("AI摘要", analysis.get('feishu_summary', '')),
        ("处理方式", action_labels.get(action, '未知')),
    ]

    if action == 'notify_ops':
        fields.append(("通知", "<at id=ou_a88146ad16b8d6889a4f8557f74fc54e></at> 请及时处理退费/取消请求"))

    send_feishu_card(title=title, fields=fields, color=color)


# ============ Processed IDs ============

def _load_processed_email_ids():
    if os.path.exists(PROCESSED_EMAILS_FILE):
        try:
            with open(PROCESSED_EMAILS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return set(data.get('message_ids', []))
        except (json.JSONDecodeError, ValueError):
            return set()
    return set()


def _save_processed_email_ids(ids_set):
    ids_list = list(ids_set)[-5000:]
    with open(PROCESSED_EMAILS_FILE, 'w', encoding='utf-8') as f:
        json.dump({'message_ids': ids_list}, f)


# ============ Poll Cycle ============

def run_edm_poll_cycle():
    if not _edm_poll_lock.acquire(blocking=False):
        logger.info('EDM: Previous poll still running, skip')
        return 0

    _edm_runtime_state['polling_now'] = True
    _edm_runtime_state['last_poll_time'] = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')

    try:
        processed_ids = _load_processed_email_ids()

        try:
            emails = fetch_unread_emails(max_results=10)
        except Exception as e:
            logger.error(f"EDM: Failed to fetch emails: {e}")
            return 0

        new_emails = [e for e in emails if e['message_id'] not in processed_ids]

        if not new_emails:
            logger.info('EDM: 无新邮件')
            return 0

        logger.info(f'EDM: Found {len(new_emails)} new emails')
        count = 0

        for email_data in new_emails:
            processed_ids.add(email_data['message_id'])
            _save_processed_email_ids(processed_ids)

            if _is_system_email(email_data):
                logger.info(f"EDM: Skip system email from {email_data['from_email']}")
                try:
                    mark_as_read(email_data['message_id'])
                except Exception:
                    pass
                continue

            try:
                result = process_single_email(email_data)
                count += 1

                try:
                    mark_as_read(email_data['message_id'])
                except Exception as e:
                    logger.warning(f"EDM: mark_as_read failed: {e}")

                _edm_runtime_state['recent_records'].append({
                    'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
                    'from_email': email_data['from_email'],
                    'from_name': email_data['from_name'],
                    'subject': email_data['subject'][:50],
                    'category': result.get('category', ''),
                    'matched_faq': result.get('matched_faq') or '',
                    'action': result.get('action', ''),
                    'status': 'replied' if result.get('action') != 'notify_only' else 'pending',
                })

            except Exception as e:
                logger.exception(f"EDM: Error processing email {email_data['message_id']}: {e}")
                _edm_runtime_state['recent_records'].append({
                    'time': datetime.utcnow().strftime('%Y-%m-%d %H:%M'),
                    'from_email': email_data['from_email'],
                    'from_name': email_data['from_name'],
                    'subject': email_data['subject'][:50],
                    'category': 'error',
                    'matched_faq': '',
                    'action': 'error',
                    'status': 'error',
                })

        if len(_edm_runtime_state['recent_records']) > 500:
            _edm_runtime_state['recent_records'] = _edm_runtime_state['recent_records'][-500:]

        return count
    finally:
        _edm_runtime_state['polling_now'] = False
        _edm_poll_lock.release()


# ============ Status API ============

def get_edm_status():
    today = datetime.utcnow().strftime('%Y-%m-%d')
    records = _edm_runtime_state['recent_records']
    today_records = [r for r in records if r.get('time', '').startswith(today)]
    total = len(today_records)
    replied = sum(1 for r in today_records if r.get('status') == 'replied')
    pending = sum(1 for r in today_records if r.get('status') == 'pending')
    high_risk = sum(1 for r in today_records if r.get('action') == 'notify_ops')

    return {
        'running': _edm_runtime_state['loop_alive'],
        'polling_now': _edm_runtime_state['polling_now'],
        'last_poll_time': _edm_runtime_state['last_poll_time'],
        'today_total': total,
        'today_replied': replied,
        'today_pending': pending,
        'today_high_risk': high_risk,
    }


def get_edm_recent_records(limit=20, offset=0):
    records = list(reversed(_edm_runtime_state['recent_records']))
    total = len(records)
    page = records[offset:offset + limit]
    return {'records': page, 'total': total}


# ============ Main ============

def main():
    _edm_runtime_state['loop_alive'] = True
    logger.info('=== EDM 邮件自动回复后台线程启动 ===')
    while True:
        try:
            run_edm_poll_cycle()
        except Exception as e:
            logger.exception(f'EDM 轮询异常: {e}')
        time.sleep(EDM_POLL_INTERVAL)


if __name__ == '__main__':
    main()
