import os
import json
import hashlib
import logging
import time
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .html_generat import _run_coze_workflow, _parse_json_string_if_possible, clean_output_content, WORKFLOW_PROVIDER
from .coze_llm import call_coze_llm

SUPERSET_URL = os.getenv('SUPERSET_URL', 'http://galaxy.iweaver.ai')
SUPERSET_USER = os.getenv('SUPERSET_USER', 'admin')
SUPERSET_PASS = os.getenv('SUPERSET_PASS', '')
SUPERSET_DB_ID = int(os.getenv('SUPERSET_DB_ID', '1'))
CASE_ANALYSIS_SOURCE_PROFILE = os.getenv('CASE_ANALYSIS_SOURCE_PROFILE', 'legacy').strip().lower() or 'legacy'
CASE_ANALYSIS_SUPERSET_DB_ID = int(os.getenv('CASE_ANALYSIS_SUPERSET_DB_ID', str(SUPERSET_DB_ID)))
CASE_ANALYSIS_SUPERSET_DB_NAME = os.getenv('CASE_ANALYSIS_SUPERSET_DB_NAME', 'iweaver-hermes-ai')
CASE_ANALYSIS_MAX_CASES = int(os.getenv('CASE_ANALYSIS_MAX_CASES', '500'))

CASE_ANALYSIS_WORKFLOW_ID = os.getenv('CASE_ANALYSIS_WORKFLOW_ID', '7631449377805959168')
NEW_COZE_WORKFLOW_ID = os.getenv('NEW_COZE_WORKFLOW_ID', '').strip()

logger = logging.getLogger(__name__)

FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
FEISHU_CASE_SHEET_TOKEN = os.getenv('FEISHU_CASE_SHEET_TOKEN', '')

CASE_FEISHU_HEADERS = [
    '日期',
    'Case ID',
    '用户ID',
    'Message ID',
    '用户类型',
    '用户版本',
    '意图标签（一级）',
    '意图标签（二级）',
    '意图标签（三级）',
    '问题归因',
    '对话内容(中文)',
    '分析点',
    '结论',
    '满意度评分',
    '满意度原因',
    '置信度',
    '是否点赞',
    '是否点踩',
    '用户反馈文本',
    '点踩原因标签',
    '反馈归因',
    '反馈原因总结',
]


import threading

_source_errors = []
_source_errors_lock = threading.Lock()


def _reset_source_errors():
    with _source_errors_lock:
        _source_errors.clear()


def _record_source_error(source, exc, context=None):
    entry = {'source': str(source), 'error': str(exc)[:500]}
    if context:
        entry.update({k: str(v)[:200] for k, v in context.items() if v not in (None, '')})
    with _source_errors_lock:
        _source_errors.append(entry)
    logger.exception('Case source failure: source=%s context=%s', source, context or {})


def _source_errors_snapshot():
    with _source_errors_lock:
        return [dict(item) for item in _source_errors]


def _case_input_hash(cases):
    prompt_hashes = []
    for case_data in cases:
        prompt = format_case_for_llm(case_data)
        prompt_hashes.append({
            'case_type': str(case_data.get('case_type') or ''),
            'user_id': str(case_data.get('user_id') or ''),
            'topic_id': str(case_data.get('topic_id') or ''),
            'message_id': str(case_data.get('message_id') or ''),
            'prompt_sha256': hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        })
    encoded = json.dumps(prompt_hashes, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


class SupersetClient:
    def __init__(self, db_id=None):
        self.db_id = SUPERSET_DB_ID if db_id is None else int(db_id)
        self._local = threading.local()

    def _ensure_session(self):
        session = getattr(self._local, 'session', None)
        if session is not None:
            return
        session = requests.Session()
        session.trust_env = False
        resp = session.post(
            f'{SUPERSET_URL}/api/v1/security/login',
            json={'username': SUPERSET_USER, 'password': SUPERSET_PASS, 'provider': 'db'},
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json()['access_token']
        session.headers.update({'Authorization': f'Bearer {token}'})
        csrf_resp = session.get(f'{SUPERSET_URL}/api/v1/security/csrf_token/', timeout=10)
        csrf = csrf_resp.json().get('result', '')
        session.headers.update({'X-CSRFToken': csrf})
        self._local.session = session

    def _reset_session(self):
        self._local.session = None

    def execute_sql(self, sql, limit=1000):
        self._ensure_session()
        for attempt in range(2):
            try:
                resp = self._local.session.post(
                    f'{SUPERSET_URL}/api/v1/sqllab/execute/',
                    json={
                        'database_id': self.db_id,
                        'sql': sql,
                        'runAsync': False,
                        'queryLimit': limit,
                    },
                    headers={'Referer': f'{SUPERSET_URL}/sqllab'},
                    timeout=120,
                )
            except Exception:
                logger.exception('Superset SQL request failed. limit=%s sql=%s', limit, ' '.join(sql.split())[:800])
                raise
            if resp.status_code == 401 and attempt == 0:
                self._reset_session()
                self._ensure_session()
                continue
            try:
                resp.raise_for_status()
            except Exception:
                logger.error('Superset SQL returned error. status=%s limit=%s body=%s sql=%s', resp.status_code, limit, resp.text[:1000], ' '.join(sql.split())[:800])
                raise
            break
        result = resp.json()
        if 'errors' in result and result['errors']:
            raise RuntimeError(f"Superset SQL error: {result['errors'][0].get('message', '')}")
        columns = [c.get('column_name') or c.get('name', '') for c in result.get('columns', [])]
        return result.get('data', []), columns

    def get_database_identity(self):
        self._ensure_session()
        resp = self._local.session.get(f'{SUPERSET_URL}/api/v1/database/{self.db_id}', timeout=15)
        resp.raise_for_status()
        result = (resp.json().get('result') or {})
        return {'id': result.get('id', self.db_id), 'database_name': result.get('database_name', '')}


_superset = SupersetClient(CASE_ANALYSIS_SUPERSET_DB_ID if CASE_ANALYSIS_SOURCE_PROFILE == 'new_site' else SUPERSET_DB_ID)


def get_source_database_identity():
    return _superset.get_database_identity()


def _is_new_site():
    return CASE_ANALYSIS_SOURCE_PROFILE == 'new_site'


def _day_window(target_date):
    start = datetime.strptime(target_date, '%Y-%m-%d')
    return start.strftime('%Y-%m-%d %H:%M:%S'), (start + timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')


def _sql_quote(value):
    return str(value).replace("'", "''")


def _run_query(sql, limit=1000):
    for attempt in range(3):
        try:
            return _superset.execute_sql(sql, limit=limit)
        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            if attempt < 2:
                logger.warning('Superset connection error, retry %d/2: %s', attempt + 1, str(e)[:200])
                _superset._reset_session()
                time.sleep(2 * (attempt + 1))
            else:
                raise


def extract_message_content(message, role):
    if not message or not isinstance(message, dict):
        return ''
    direct = message.get('content')
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    data = message.get('data')
    if role == 'user':
        if isinstance(data, dict):
            content = (data.get('content') or '').strip()
            if content:
                return content
            attachments = data.get('experimental_attachments') or []
            if attachments:
                parts = []
                for att in attachments:
                    name = att.get('name', '未知文件')
                    ctype = att.get('contentType', '')
                    parts.append(f"{name}({ctype})" if ctype else name)
                return f"[上传附件: {', '.join(parts)}]"
        return ''

    # --- assistant message ---
    top_type = message.get('type', '')

    # data 是 dict 的情况 (touch_limit 等)
    if isinstance(data, dict):
        data_type = data.get('type', '')
        data_content = (data.get('content') or '').strip()
        if data_type == 'touch_limit':
            return f'[触达免费上限] {data_content}' if data_content else '[触达免费上限]'
        if data.get('error'):
            return f'[系统错误] {data_content}' if data_content else '[系统错误]'
        if data_content:
            return data_content
        return ''

    # data 是空 list 的情况 (async_task_created, agent 中间状态)
    if isinstance(data, list) and not data:
        if top_type == 'async_task_created':
            return '[异步任务创���中]'
        if top_type == 'agent':
            return '[Agent任务分发中]'
        return '[等待中]'

    # data 是 list 且有内容 — 正常 agent 回复
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            inner_data = first.get('data', {})
            if isinstance(inner_data, dict):
                result = inner_data.get('result', {})
                if isinstance(result, dict):
                    output = result.get('output', {})
                    agent_name = result.get('agentName', '')

                    # 正常文本输出
                    if isinstance(output, dict):
                        out_text = (output.get('output') or '').strip()

                        # output.error 字段 (服务繁忙等)
                        out_error = output.get('error', '')
                        if out_error and not out_text:
                            return f'[服务繁忙] {out_error}'
                        if out_text.startswith('err:'):
                            return f'[服务繁忙] {out_text}'

                        # 有文本直接返回
                        if out_text:
                            return out_text

                        # output 为空 — 检查 tools_invoked 里的图片/内容
                        tools = output.get('tools_invoked', [])
                        for tool in tools:
                            if isinstance(tool, dict):
                                tool_result = tool.get('result', '')
                                if isinstance(tool_result, str):
                                    try:
                                        tr = json.loads(tool_result)
                                    except (json.JSONDecodeError, ValueError):
                                        tr = {}
                                elif isinstance(tool_result, dict):
                                    tr = tool_result
                                else:
                                    tr = {}
                                if tr.get('image_url'):
                                    return f'[图片输出] {tr["image_url"]}'
                                if tr.get('s3Url'):
                                    return f'[文件输出] {tr["s3Url"]}'
                                if tr.get('content'):
                                    content_val = tr['content']
                                    if isinstance(content_val, str) and len(content_val) > 10:
                                        return content_val

                        # 还是没有 — 标记为空回复 + agent 名
                        if agent_name:
                            return f'[{agent_name} 回复为空]'
                        return '[助手回复为空]'

                    elif isinstance(output, str) and output.strip():
                        return output.strip()

    return ''


def _parse_message_json(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
        import ast
        try:
            result = ast.literal_eval(raw)
            if isinstance(result, dict):
                return result
        except (ValueError, SyntaxError):
            pass
    return {}


def _fetch_conversations(user_id, start_time, end_time, limit_topics=10, topic_id=None):
    if _is_new_site():
        sql = f"""
        SELECT id, topic_id, role, message, message_id, created_at, agent_name, agent_id
        FROM public.chat_logs
        WHERE user_id = '{_sql_quote(user_id)}' AND deleted = false
          AND created_at >= '{start_time}' AND created_at < '{end_time}'
          {f"AND topic_id = '{_sql_quote(topic_id)}'" if topic_id not in (None, '') else ''}
        ORDER BY created_at ASC
        LIMIT 500
    """
    else:
        sql = f"""
        SELECT topic_id, role, message, message_id, created_at, agent_type
        FROM chat_logs
        WHERE user_id = '{_sql_quote(user_id)}' AND deleted = false
          AND created_at >= '{start_time}' AND created_at <= '{end_time}'
          {f"AND topic_id = '{_sql_quote(topic_id)}'" if topic_id not in (None, '') else ''}
        ORDER BY created_at ASC
        LIMIT 500
    """
    rows, _ = _run_query(sql, limit=500)

    topics = {}
    topic_agent_types = {}
    for row in rows:
        tid = str(row.get('topic_id') or '0')
        if tid not in topics:
            topics[tid] = []
        agent_type = (row.get('agent_name') or row.get('agent_id') or '') if _is_new_site() else (row.get('agent_type') or '')
        if agent_type and tid not in topic_agent_types:
            topic_agent_types[tid] = agent_type
        msg_data = _parse_message_json(row.get('message'))
        role = row.get('role', '')
        content = extract_message_content(msg_data, role)
        created = row.get('created_at', '')
        if isinstance(created, str) and len(created) > 19:
            created = created[:19]
        topics[tid].append({
            'role': role,
            'content': content,
            'time': created,
            'message_id': str(row.get('message_id') or ''),
            'record_id': str(row.get('id') or ''),
        })

    result = []
    for tid, msgs in list(topics.items())[:limit_topics]:
        result.append({'topic_id': tid, 'messages': msgs, 'agent_type': topic_agent_types.get(tid, '')})
    return result


def _get_first_message_id(conversations):
    for convo in conversations:
        for msg in convo.get('messages', []):
            if msg.get('role') == 'user' and msg.get('message_id'):
                return msg['message_id']
    return ''


def _get_first_chat_log_id(conversations):
    for convo in conversations:
        for msg in convo.get('messages', []):
            if msg.get('role') == 'user' and msg.get('record_id'):
                return msg['record_id']
    return ''


def _get_user_version(user_id):
    table = 'public.user_version' if _is_new_site() else 'user_version'
    sql = f"""
        SELECT version FROM {table}
        WHERE user_id = '{_sql_quote(user_id)}' AND active = true AND deleted = false
        ORDER BY created_at DESC LIMIT 1
    """
    rows, _ = _run_query(sql, limit=1)
    return rows[0]['version'] if rows else 'Free'


def _batch_get_versions(user_ids):
    if not user_ids:
        return {}
    result = {}
    for i in range(0, len(user_ids), 50):
        chunk = user_ids[i:i+50]
        uid_list = ','.join(f"'{_sql_quote(uid)}'" for uid in chunk)
        table = 'public.user_version' if _is_new_site() else 'user_version'
        sql = f"""
            SELECT DISTINCT ON (user_id) user_id, version
            FROM {table}
            WHERE user_id IN ({uid_list}) AND active = true AND deleted = false
            ORDER BY user_id, created_at DESC
        """
        rows, _ = _run_query(sql, limit=len(chunk))
        for r in rows:
            result[r['user_id']] = r['version']
    return result


def _map_new_site_feedback(row):
    kind = row.get('kind', '')
    return {'type': {'like': 'thumbs_up', 'dislike': 'thumbs_down', 'feedback': 'feedback'}.get(kind, kind),
            'feedback_content': row.get('comment') or '', 'reason_tags': row.get('reason_tags'),
            'message_id': str(row.get('message_id') or ''), 'topic_id': str(row.get('topic_id') or ''),
            'created_at': row.get('created_at')}


def _map_new_site_attitude(row):
    kind = row.get('kind', '')
    if kind not in ('like', 'dislike'):
        return None
    return {'type': 1 if kind == 'like' else 2, 'created_time': row.get('created_at'),
            'message_id': str(row.get('message_id') or ''), 'topic_id': str(row.get('topic_id') or '')}


def _batch_get_feedback(user_ids, start_time, end_time):
    if not user_ids:
        return {}
    result = {}
    for i in range(0, len(user_ids), 50):
        chunk = user_ids[i:i+50]
        uid_list = ','.join(f"'{_sql_quote(uid)}'" for uid in chunk)
        if _is_new_site():
            sql = f"""SELECT user_id, topic_id, message_id, kind, reason_tags, comment, created_at
                FROM public.chat_feedback WHERE user_id IN ({uid_list})
                AND created_at >= '{start_time}' AND created_at < '{end_time}' ORDER BY created_at DESC"""
        else:
            sql = f"""SELECT user_id, type, feedback_content, created_at FROM feedback_info
                WHERE user_id IN ({uid_list}) AND created_at >= '{start_time}' AND created_at <= '{end_time}'
                ORDER BY created_at DESC"""
        rows, _ = _run_query(sql, limit=len(chunk) * 10)
        for r in rows:
            result.setdefault(r['user_id'], []).append(_map_new_site_feedback(r) if _is_new_site() else r)
    return result


def _batch_get_attitude(user_ids, start_time, end_time):
    if not user_ids:
        return {}
    result = {}
    for i in range(0, len(user_ids), 50):
        chunk = user_ids[i:i+50]
        uid_list = ','.join(f"'{_sql_quote(uid)}'" for uid in chunk)
        if _is_new_site():
            sql = f"""SELECT user_id, topic_id, message_id, kind, created_at FROM public.chat_feedback
                WHERE user_id IN ({uid_list}) AND kind IN ('like', 'dislike')
                AND created_at >= '{start_time}' AND created_at < '{end_time}' ORDER BY created_at DESC"""
        else:
            sql = f"""SELECT user_id, type, created_time FROM attitude
                WHERE user_id IN ({uid_list}) AND deleted = false
                AND created_time >= '{start_time}' AND created_time <= '{end_time}' ORDER BY created_time DESC"""
        rows, _ = _run_query(sql, limit=len(chunk) * 10)
        for r in rows:
            mapped = _map_new_site_attitude(r) if _is_new_site() else r
            if mapped:
                result.setdefault(r['user_id'], []).append(mapped)
    return result


def _fetch_case_details(user_id, start_time, end_time, topic_id=None):
    convos = _fetch_conversations(user_id, start_time, end_time, topic_id=topic_id)
    version = _get_user_version(user_id)
    return convos, version


def _fetch_user_feedback(user_id, start_time, end_time):
    sql = f"""
        SELECT type, feedback_content, created_at
        FROM feedback_info
        WHERE user_id = '{user_id}'
          AND created_at >= '{start_time}' AND created_at <= '{end_time}'
        ORDER BY created_at DESC
    """
    rows, _ = _run_query(sql, limit=50)
    return rows


def _fetch_user_attitude(user_id, start_time, end_time):
    sql = f"""
        SELECT type, created_time
        FROM attitude
        WHERE user_id = '{user_id}' AND deleted = false
          AND created_time >= '{start_time}' AND created_time <= '{end_time}'
        ORDER BY created_time DESC
    """
    rows, _ = _run_query(sql, limit=50)
    return rows


def fetch_type_a_cases(target_date, count=None):
    """A类：T日首次付费用户，拉付费前24h + 后1h的对话"""
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    sql = f"""
        SELECT t.id as trigger_id, t.user_id, t.created_at as paid_at, t.version, t.total_fee
        FROM {'public.trades' if _is_new_site() else 'trades'} t
        WHERE t.is_success = true
          AND t.created_at >= '{day_start}' AND t.created_at {'<' if _is_new_site() else '<='} '{day_end}'
          AND NOT EXISTS (
            SELECT 1 FROM {'public.trades' if _is_new_site() else 'trades'} t2
            WHERE t2.user_id = t.user_id AND t2.is_success = true
              AND t2.created_at < t.created_at
          )
        ORDER BY t.created_at ASC
    """
    candidate_limit = 10000 if count is None else max(count * 10, count)
    rows, _ = _run_query(sql, limit=candidate_limit)

    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build_a(u):
            paid_at_str = str(u['paid_at'])[:19].replace('T', ' ')
            paid_at = datetime.strptime(paid_at_str, '%Y-%m-%d %H:%M:%S')
            start = (paid_at - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            end = (paid_at + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            convos, version = _fetch_case_details(u['user_id'], start, end)
            if not _has_conversation_content(convos):
                return None
            return {
                'case_type': 'A',
                'user_id': u['user_id'],
                'message_id': _get_first_message_id(convos),
                'chat_log_id': _get_first_chat_log_id(convos),
                'topic_id': str(convos[0]['topic_id'] if convos else ''),
                'trigger_id': str(u.get('trigger_id') or ''),
                'user_version': u.get('version', '') or version,
                'paid_at': paid_at_str,
                'total_fee': str(u.get('total_fee', '')),
                'conversations': convos,
            }
        futures = {pool.submit(_build_a, u): u for u in rows}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    cases.append(result)
            except Exception as exc:
                _record_source_error('type_a_worker', exc, futures[f])
    cases.sort(key=lambda case: (
        str(case.get('paid_at') or ''),
        str(case.get('user_id') or ''),
        str(case.get('topic_id') or ''),
    ))
    return cases if count is None else cases[:count]


def fetch_type_b_cases(target_date, count=None):
    """B类：T日首次出现且仅1轮对话的用户（不依赖users表）"""
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    sql = f"""
        SELECT cl.user_id, min(cl.created_at) as first_msg_time
        FROM {'public.chat_logs' if _is_new_site() else 'chat_logs'} cl
        WHERE cl.deleted = false AND cl.role = 'user'
          AND cl.created_at >= '{day_start}' AND cl.created_at {'<' if _is_new_site() else '<='} '{day_end}'
        GROUP BY cl.user_id
        HAVING count(*) = 1
          AND NOT EXISTS (
            SELECT 1 FROM {'public.chat_logs' if _is_new_site() else 'chat_logs'} cl2
            WHERE cl2.user_id = cl.user_id AND cl2.deleted = false AND cl2.role = 'user'
              AND cl2.created_at < '{day_start}'
          )
        ORDER BY min(cl.created_at) ASC
    """
    candidate_limit = 10000 if count is None else max(count * 10, count)
    rows, _ = _run_query(sql, limit=candidate_limit)

    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build_b(u):
            convos, version = _fetch_case_details(u['user_id'], day_start, day_end)
            if not _has_conversation_content(convos):
                return None
            return {
                'case_type': 'B',
                'user_id': u['user_id'],
                'message_id': _get_first_message_id(convos),
                'chat_log_id': _get_first_chat_log_id(convos),
                'topic_id': str(convos[0]['topic_id'] if convos else ''),
                'user_version': version,
                'first_msg_time': str(u.get('first_msg_time', ''))[:19],
                'conversations': convos,
            }
        futures = {pool.submit(_build_b, u): u for u in rows}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    cases.append(result)
            except Exception as exc:
                _record_source_error('type_b_worker', exc, futures[f])
    cases.sort(key=lambda case: (
        str(case.get('first_msg_time') or ''),
        str(case.get('user_id') or ''),
        str(case.get('topic_id') or ''),
    ))
    return cases if count is None else cases[:count]


def fetch_type_c_cases(target_date, count=None):
    """C类：T日 ≥3轮 + 未付费"""
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    sql = f"""
        SELECT cl.user_id, cl.topic_id, count(*) as user_turns
        FROM {'public.chat_logs' if _is_new_site() else 'chat_logs'} cl
        WHERE cl.deleted = false AND cl.role = 'user'
          AND cl.created_at >= '{day_start}' AND cl.created_at {'<' if _is_new_site() else '<='} '{day_end}'
          AND NOT EXISTS (
            SELECT 1 FROM {'public.trades' if _is_new_site() else 'trades'} t
            WHERE t.user_id = cl.user_id AND t.is_success = true
          )
        GROUP BY cl.user_id, cl.topic_id
        HAVING count(*) >= 3 AND count(*) < 5
        ORDER BY count(*) DESC
    """
    rows, _ = _run_query(sql, limit=10000 if count is None else count)

    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build_c(r):
            convos, version = _fetch_case_details(r['user_id'], day_start, day_end, topic_id=r.get('topic_id'))
            if not _has_conversation_content(convos):
                return None
            return {
                'case_type': 'C',
                'user_id': r['user_id'],
                'message_id': _get_first_message_id(convos),
                'chat_log_id': _get_first_chat_log_id(convos),
                'user_version': version,
                'topic_id': str(r['topic_id']),
                'user_turns': r['user_turns'],
                'conversations': convos,
            }
        futures = {pool.submit(_build_c, r): r for r in rows}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    cases.append(result)
            except Exception as exc:
                _record_source_error('type_c_worker', exc, futures[f])
    return cases


def fetch_type_d_cases(target_date, count=None):
    """D类：T日 ≥5轮 + 未付费"""
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    sql = f"""
        SELECT cl.user_id, cl.topic_id, count(*) as user_turns
        FROM {'public.chat_logs' if _is_new_site() else 'chat_logs'} cl
        WHERE cl.deleted = false AND cl.role = 'user'
          AND cl.created_at >= '{day_start}' AND cl.created_at {'<' if _is_new_site() else '<='} '{day_end}'
          AND NOT EXISTS (
            SELECT 1 FROM {'public.trades' if _is_new_site() else 'trades'} t
            WHERE t.user_id = cl.user_id AND t.is_success = true
          )
        GROUP BY cl.user_id, cl.topic_id
        HAVING count(*) >= 5
        ORDER BY count(*) DESC
    """
    rows, _ = _run_query(sql, limit=10000 if count is None else count)

    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build_d(r):
            convos, version = _fetch_case_details(r['user_id'], day_start, day_end, topic_id=r.get('topic_id'))
            if not _has_conversation_content(convos):
                return None
            return {
                'case_type': 'D',
                'user_id': r['user_id'],
                'message_id': _get_first_message_id(convos),
                'chat_log_id': _get_first_chat_log_id(convos),
                'user_version': version,
                'topic_id': str(r['topic_id']),
                'user_turns': r['user_turns'],
                'conversations': convos,
            }
        futures = {pool.submit(_build_d, r): r for r in rows}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    cases.append(result)
            except Exception as exc:
                _record_source_error('type_d_worker', exc, futures[f])
    return cases


def fetch_type_e_cases(target_date, count=None):
    """E类：点踩(attitude type=2)"""
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    target_count = count if count is not None else 100

    if _is_new_site():
        sql = f"""SELECT id as trigger_id, user_id, topic_id, message_id, created_at FROM public.chat_feedback WHERE kind = 'dislike'
          AND created_at >= '{day_start}' AND created_at < '{day_end}'
          ORDER BY created_at ASC, user_id, topic_id, message_id"""
    else:
        sql = f"""SELECT DISTINCT user_id FROM attitude WHERE deleted = false AND type = 2
          AND created_time >= '{day_start}' AND created_time <= '{day_end}' ORDER BY user_id"""
    rows, _ = _run_query(sql, limit=target_count * 2)

    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build_e(r):
            event_time = str(r.get('created_at') or '')[:19].replace('T', ' ')
            if _is_new_site() and event_time:
                event_dt = datetime.strptime(event_time, '%Y-%m-%d %H:%M:%S')
                start = (event_dt - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
                end = (event_dt + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            else:
                start, end = day_start, day_end
            event_topic = str(r.get('topic_id') or '')
            convos, version = _fetch_case_details(r['user_id'], start, end, topic_id=event_topic or None)
            if not _has_conversation_content(convos):
                return None
            return {
                'case_type': 'E',
                'user_id': r['user_id'],
                'message_id': str(r.get('message_id') or _get_first_message_id(convos)),
                'chat_log_id': _get_first_chat_log_id(convos),
                'trigger_id': str(r.get('trigger_id') or ''),
                'user_version': version,
                'topic_id': event_topic or str(convos[0]['topic_id'] if convos else ''),
                'event_time': event_time,
                'trigger': 'thumbs_down',
                'conversations': convos,
            }
        futures = {pool.submit(_build_e, r): r for r in rows}
        for f in as_completed(futures):
            try:
                result = f.result()
                if result:
                    cases.append(result)
            except Exception as exc:
                _record_source_error('type_e_worker', exc, futures[f])
    cases.sort(key=lambda case: (
        str(case.get('event_time') or ''),
        str(case.get('user_id') or ''),
        str(case.get('topic_id') or ''),
        str(case.get('message_id') or ''),
    ))
    return cases[:target_count]


def format_case_for_llm(case_data):
    lines = []
    lines.append(f"Case Type: {case_data['case_type']}")
    lines.append(f"User Version: {case_data.get('user_version', 'unknown')}")

    if case_data['case_type'] == 'A':
        lines.append(f"Paid At: {case_data.get('paid_at', '')}")
        lines.append(f"Amount: {case_data.get('total_fee', '')}")
    elif case_data['case_type'] == 'E':
        lines.append(f"Trigger: {case_data.get('trigger', '')}")

    lines.append("")
    lines.append("=== Conversation ===")

    max_topics = 3
    max_msgs_per_topic = 20
    max_content_len = 800
    topic_count = 0
    for convo in case_data.get('conversations', []):
        if topic_count >= max_topics:
            lines.append(f"\n[... {len(case_data.get('conversations', [])) - max_topics} more topics truncated]")
            break
        topic_count += 1
        agent_type = convo.get('agent_type', '')
        topic_header = f"\n--- Topic: {convo['topic_id']} ---"
        if agent_type:
            topic_header += f" [Agent: {agent_type}]"
        lines.append(topic_header)
        msgs = convo['messages']
        for msg in msgs[:max_msgs_per_topic]:
            role_label = "User" if msg['role'] == 'user' else "Assistant"
            content = msg['content'] or '[empty]'
            if len(content) > max_content_len:
                content = content[:max_content_len] + '...[truncated]'
            lines.append(f"[{msg['time']}] {role_label}: {content}")
        if len(msgs) > max_msgs_per_topic:
            lines.append(f"[... {len(msgs) - max_msgs_per_topic} more messages truncated]")

    feedback_list = case_data.get('feedback', [])
    attitude_list = case_data.get('attitude', [])

    if feedback_list or attitude_list:
        lines.append("")
        lines.append("=== User Feedback & Attitude ===")
        for fb in feedback_list:
            fb_type = fb.get('type', '')
            fb_content = fb.get('feedback_content', '') or ''
            lines.append(f"[Feedback] type={fb_type}, content={fb_content}")
        for att in attitude_list:
            att_label = 'thumbs_up' if att.get('type') == 1 else 'thumbs_down'
            lines.append(f"[Attitude] {att_label} at {att.get('created_time', '')}")

    lines.append("")
    lines.append("=== Output Instructions ===")
    lines.append("Output a single JSON object with ONLY these fields (no extra fields):")
    lines.append("")
    lines.append('1. "intent_label": {"level1": "一级意图(场景)", "level2": "二级意图", "level3": "三级意图"}')
    lines.append("   注意：看手相、解梦、星座等必须归类为 休闲娱乐")
    lines.append('2. "attribution": string, 问题归因 (e.g. "模型问题 - 理解能力")')
    lines.append('3. "analysis_point": string, 核心分析点 (一句话)')
    lines.append('4. "conclusion": string, 结论 (e.g. "首次体验差 | 优化引导")')
    lines.append('5. "conversation_summary_zh": string, 将上述对话内容翻译为简洁的中文摘要，保留关键信息。注意：遇到 [触达免费上限]、[服务繁忙]、[图片输出]、[文件输出]、[Agent任务分发中]、[上传附件:...] 等标签请保留原样不翻译。重要：结合对话所属的 Agent 类型(如 ai-pdf-summarizer、ai-mind-map-generator 等)理解上下文语境，不要简单将所有外语内容理解为"翻译需求"')
    lines.append("")
    lines.append("=== 满意度评分 (0-10分) ===")
    lines.append('6. "satisfaction_score": integer 0-10 or "无法判断"')
    lines.append("评分本质：系统输出对于用户目标的完成程度 + 输出质量 + 用户行为信号")
    lines.append("注意：[触达免费上限]的输出不计入满意度分析")
    lines.append("")
    lines.append("0-1分：严重失败 — 完全答非所问/严重错误/幻觉/用户明显放弃/明确负反馈(点踩)/网络超时无回复")
    lines.append("2-3分：低质量 — 略相关但不可用/内容空泛/用户需重问换问法/目标几乎未推进")
    lines.append("4-5分：基础可用 — 有部分帮助/可作起点/仍需大量修改/只完成部分需求")
    lines.append("6-7分：中等满意 — 基本解决核心问题/方向正确/明显减少工作量/用户继续是提升而非救火")
    lines.append("8-9分：高满意 — 高度贴合目标/质量高/基本可直接用/用户追问偏延展/有明显认可或点赞")
    lines.append("10分：超预期 — 同时满足:完整解决+高质量+超出预期+几乎无需修改+有高价值洞察")
    lines.append("无法判断：信息不足/对话过短/上下文缺失/用户目标不明确时，禁止猜测")
    lines.append("")
    lines.append("评分流程：Step1判断用户目标 → Step2判断完成程度 → Step3判断输出质量(准确性/结构化/可执行性/深度) → Step4判断行为信号(正向:深入追问/延展; 负向:重复纠正/不满)")
    lines.append("约束：不允许仅根据谢谢/没谢谢判断; 用户沉默≠不满意; 继续追问需区分优化vs纠错; 被免费次数限制打断≠低满意")
    lines.append("附件说明：[上传附件:...]表示用户上传了文件/图片，这是用户的有效输入(非空消息)，需结合 Agent 类型判断用户目的(如 PDF摘要agent 上传PDF = 用户想要摘要)")
    lines.append("Agent上下文：对话标注的 Agent 类型代表用户选择的功能场景，评分时需考虑该场景下的任务完成度")
    lines.append("")
    lines.append('7. "satisfaction_reason": string, 一句话中文解释评分原因')
    lines.append('8. "confidence_level": one of "高"/"中"/"低"')
    lines.append("")
    lines.append("=== 反馈分析 ===")
    lines.append('9. "dislike_reason_tags": array from ["理解错误","答非所问","内容空洞","不可执行","结构混乱","太复杂","太长","错误信息","幻觉/编造","需要多轮才能完成"], empty array if no dislike')
    lines.append('10. "feedback_root_cause": one of "模型问题"/"产品问题"/"引导问题"/"商业化问题"/"用户表达问题"/"无"')
    lines.append('11. "feedback_reason_summary": 一句话中文总结用户反馈原因 (无反馈则"无反馈")')

    return "\n".join(lines)


def _has_conversation_content(conversations):
    return any(
        msg.get('content')
        for convo in conversations
        for msg in convo.get('messages', [])
    )


def _fetch_feedback_cases(target_date):
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"
    if _is_new_site():
        sql = f"""SELECT id as trigger_id, user_id, topic_id, message_id, created_at as earliest_fb FROM public.chat_feedback
            WHERE kind = 'feedback' AND created_at >= '{day_start}' AND created_at < '{day_end}'
            ORDER BY created_at ASC, user_id, topic_id, message_id"""
    else:
        sql = f"""SELECT user_id, min(created_at) as earliest_fb FROM feedback_info
            WHERE created_at >= '{day_start}' AND created_at <= '{day_end}' GROUP BY user_id ORDER BY user_id"""
    return _build_trigger_cases(sql, 'earliest_fb', 'feedback')


def _fetch_attitude_cases(target_date, attitude_type, trigger):
    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"
    if _is_new_site():
        kind = 'like' if attitude_type == 1 else 'dislike'
        sql = f"""SELECT id as trigger_id, user_id, topic_id, message_id, created_at as earliest_att FROM public.chat_feedback
            WHERE kind = '{kind}' AND created_at >= '{day_start}' AND created_at < '{day_end}'
            ORDER BY created_at ASC, user_id, topic_id, message_id"""
    else:
        sql = f"""SELECT user_id, min(created_time) as earliest_att FROM attitude
            WHERE deleted = false AND type = {attitude_type} AND created_time >= '{day_start}'
            AND created_time <= '{day_end}' GROUP BY user_id ORDER BY user_id"""
    return _build_trigger_cases(sql, 'earliest_att', trigger)


def _build_trigger_cases(sql, time_field, trigger):
    rows, _ = _run_query(sql, limit=1000)
    cases = []
    with ThreadPoolExecutor(max_workers=min(len(rows) or 1, 10)) as pool:
        def _build(r):
            event_time = datetime.strptime(str(r[time_field])[:19].replace('T', ' '), '%Y-%m-%d %H:%M:%S')
            start = (event_time - timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
            end = (event_time + timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
            event_topic = str(r.get('topic_id') or '')
            convos, version = _fetch_case_details(r['user_id'], start, end, topic_id=event_topic or None)
            if not _has_conversation_content(convos): return None
            return {'case_type': 'F', 'user_id': r['user_id'],
                    'message_id': str(r.get('message_id') or _get_first_message_id(convos)),
                    'chat_log_id': _get_first_chat_log_id(convos),
                    'trigger_id': str(r.get('trigger_id') or ''),
                    'user_version': version, 'topic_id': event_topic or str(convos[0]['topic_id'] if convos else ''),
                    'event_time': str(r.get(time_field) or '')[:19].replace('T', ' '),
                    'trigger': trigger, 'conversations': convos}
        futures = [pool.submit(_build, row) for row in rows]
        for future in as_completed(futures):
            try:
                result = future.result()
                if result: cases.append(result)
            except Exception as exc:
                _record_source_error(f'trigger_{trigger}_worker', exc)
    return cases


def _merge_cases(sampled_cases, extra_cases):
    merged = []
    seen = set()

    ordered = list(sampled_cases) + list(extra_cases)
    for index, case_data in enumerate(ordered):
        user_id = str(case_data.get('user_id') or '')
        topic_id = str(case_data.get('topic_id') or '')
        message_id = str(case_data.get('message_id') or '')
        stable_id = topic_id or message_id
        key = (user_id, stable_id) if stable_id else (user_id, f'__missing__:{index}')
        if key in seen:
            continue
        seen.add(key)
        merged.append(case_data)

    return sorted(merged, key=lambda c: (str(c.get('case_type', '')), str(c.get('user_id', '')), str(c.get('topic_id', '')), str(c.get('message_id', '')), str(c.get('event_time', ''))))


def sample_all_cases(target_date, progress_cb=None):
    def _p(msg):
        if progress_cb:
            progress_cb('sampling', msg)

    _p('正在并发查询 A/B/C/D/E 类案例...')

    with ThreadPoolExecutor(max_workers=5) as pool:
        fa = pool.submit(fetch_type_a_cases, target_date, 5)
        fb = pool.submit(fetch_type_b_cases, target_date, 5)
        fc = pool.submit(fetch_type_c_cases, target_date, 5)
        fd = pool.submit(fetch_type_d_cases, target_date, 3)
        fe = pool.submit(fetch_type_e_cases, target_date, 2)

    sampled_cases = []
    for label, fut in [('A', fa), ('B', fb), ('C', fc), ('D', fd), ('E', fe)]:
        try:
            result = fut.result()
            sampled_cases.extend(result)
            _p(f'{label} 类完成 ({len(result)} 条)')
        except Exception as exc:
            _record_source_error(f'type_{label}_query', exc)

    _p(f'采样完成 ({len(sampled_cases)} 条)，正在并发查询反馈/点赞/点踩用户...')

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_fb = pool.submit(_fetch_feedback_cases, target_date)
        f_like = pool.submit(_fetch_attitude_cases, target_date, 1, 'thumbs_up')
        f_dislike = pool.submit(_fetch_attitude_cases, target_date, 2, 'thumbs_down')

    extra_cases = []
    for label, fut in [('反馈', f_fb), ('点赞', f_like), ('点踩', f_dislike)]:
        try:
            result = fut.result()
            extra_cases.extend(result)
            _p(f'{label}用户完成 ({len(result)} 条)')
        except Exception as exc:
            _record_source_error(f'{label}_query', exc)

    _p(f'全部查询完成，采样 {len(sampled_cases)} + 额外 {len(extra_cases)} 条，正在去重合并...')

    merged = _merge_cases(sampled_cases, extra_cases)
    if _is_new_site() and CASE_ANALYSIS_MAX_CASES > 0 and len(merged) > CASE_ANALYSIS_MAX_CASES:
        _record_source_error('case_cap', RuntimeError(f'case count {len(merged)} exceeded CASE_ANALYSIS_MAX_CASES={CASE_ANALYSIS_MAX_CASES}'))
        merged = merged[:CASE_ANALYSIS_MAX_CASES]
    _p(f'合并完成，共 {len(merged)} 条案例')
    return merged


def analyze_single_case(case_data):
    if not CASE_ANALYSIS_WORKFLOW_ID:
        return {'success': False, 'error': 'CASE_ANALYSIS_WORKFLOW_ID not configured'}

    input_text = format_case_for_llm(case_data)

    if WORKFLOW_PROVIDER == "local":
        if not NEW_COZE_WORKFLOW_ID:
            return {'success': False, 'error': 'NEW_COZE_WORKFLOW_ID not configured'}
        r = call_coze_llm(NEW_COZE_WORKFLOW_ID, {"prompt": input_text})
        if not r.get("success"):
            return {'success': False, 'error': r.get('error'), 'provider': r.get('provider', 'unknown')}
        result = {'success': True, 'content': r['output'], 'provider': r.get('provider', 'cliproxy')}
    else:
        result = None
        for attempt in range(3):
            result = _run_coze_workflow(input_text, workflow_id=CASE_ANALYSIS_WORKFLOW_ID, timeout_seconds=180)
            if result.get('success'):
                break
            err_msg = str(result.get('error') or result.get('content') or '')
            if 'deadline exceeded' in err_msg or 'timeout' in err_msg.lower():
                logger.warning('LLM timeout, retry %d/2: type=%s uid=%s', attempt + 1, case_data.get('case_type'), case_data.get('user_id'))
                time.sleep(3 * (attempt + 1))
                continue
            break
        if result is not None:
            result.setdefault('provider', 'coze')

    if not result.get('success'):
        return result

    raw_data = result['content']
    import re as _re

    def _fix_broken_json(s):
        result_chars = []
        i = 0
        in_string = False
        while i < len(s):
            ch = s[i]
            if ch == '\\' and in_string:
                result_chars.append(ch)
                i += 1
                if i < len(s):
                    result_chars.append(s[i])
                i += 1
                continue
            if ch == '"':
                if not in_string:
                    in_string = True
                    result_chars.append(ch)
                else:
                    rest = s[i+1:].lstrip()
                    if not rest or rest[0] in ':,}]\n':
                        in_string = False
                        result_chars.append(ch)
                    else:
                        result_chars.append('\\"')
                i += 1
                continue
            result_chars.append(ch)
            i += 1
        return ''.join(result_chars)

    def _try_parse(text):
        if not isinstance(text, str):
            return None
        text = text.strip()
        text = clean_output_content(text)
        text = _re.sub(r'^```\w*\s*', '', text)
        text = _re.sub(r'\s*```$', '', text)
        text = text.strip()

        try:
            obj = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            try:
                obj = json.loads(_fix_broken_json(text))
            except (json.JSONDecodeError, ValueError):
                brace = text.find('{')
                if brace > 0:
                    try:
                        obj = json.loads(_fix_broken_json(text[brace:]))
                    except (json.JSONDecodeError, ValueError):
                        return None
                else:
                    return None

        if isinstance(obj, dict) and 'output' in obj and isinstance(obj['output'], str):
            return _try_parse(obj['output'])
        return obj if isinstance(obj, dict) else None

    parsed = _try_parse(raw_data)

    if not isinstance(parsed, dict):
        return {
            'success': False,
            'error': f'Failed to parse analysis: {str(raw_data)[:300]}',
            'provider': result.get('provider', 'unknown'),
        }

    top_level_keys = [
        'satisfaction_score', 'satisfaction_reason', 'confidence_level',
        'dislike_reason_tags', 'feedback_root_cause', 'feedback_reason_summary',
        'intent_label', 'attribution', 'analysis_point', 'conclusion',
        'conversation_summary_zh',
    ]
    top_vals = {k: parsed.get(k) for k in top_level_keys if parsed.get(k) is not None}

    cases_list = parsed.get('cases', [])
    if not cases_list:
        cases_list = [parsed]

    results = []
    for case_analysis in cases_list:
        for k, v in top_vals.items():
            if k not in case_analysis:
                case_analysis[k] = v
        results.append({
            'case_data': case_data,
            'analysis': case_analysis,
        })
    return {
        'success': True,
        'results': results,
        'provider': result.get('provider', 'unknown'),
    }


def _get_feishu_token():
    url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
    payload = {'app_id': FEISHU_APP_ID, 'app_secret': FEISHU_APP_SECRET}
    resp = requests.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu auth failed: {data}")
    return data['tenant_access_token']


FEISHU_CASE_SHEET_ID = os.getenv('FEISHU_CASE_SHEET_ID', 'D0Vx4y')


def _get_feishu_sheet_id(token):
    return FEISHU_CASE_SHEET_ID


def push_results_to_feishu(results, target_date=''):
    if not FEISHU_CASE_SHEET_TOKEN:
        return {'skipped': True, 'reason': 'FEISHU_CASE_SHEET_TOKEN not configured'}

    token = _get_feishu_token()
    sheet_id = _get_feishu_sheet_id(token)

    rows = []
    for i, item in enumerate(results):
        case_data = item.get('case_data', {})
        analysis = item.get('analysis', {})

        intent = analysis.get('intent_label', {})
        if isinstance(intent, dict):
            intent_l1 = intent.get('level1', '')
            intent_l2 = intent.get('level2', '')
            intent_l3 = intent.get('level3', '')
        else:
            intent_l1 = str(intent)
            intent_l2 = ''
            intent_l3 = ''

        attribution = analysis.get('attribution', '')
        if isinstance(attribution, dict):
            attr_str = f"{attribution.get('primary', '')} - {attribution.get('category', '')}"
        else:
            attr_str = str(attribution)

        conclusion = analysis.get('conclusion', '')
        if isinstance(conclusion, dict):
            conclusion_str = f"{conclusion.get('problem_type', '')} | {conclusion.get('action', '')}"
        else:
            conclusion_str = str(conclusion)

        convo_zh = analysis.get('conversation_summary_zh', '')

        satisfaction_score = analysis.get('satisfaction_score', '无法判断')
        satisfaction_reason = analysis.get('satisfaction_reason', '')
        confidence = analysis.get('confidence_level', '')

        feedback_list = case_data.get('feedback', [])
        attitude_list = case_data.get('attitude', [])

        has_like = any(att.get('type') == 1 for att in attitude_list) or \
                   any(fb.get('type') == 'thumbs_up' for fb in feedback_list)
        has_dislike = any(att.get('type') == 2 for att in attitude_list) or \
                      any(fb.get('type') == 'thumbs_down' for fb in feedback_list)
        is_like = '1' if has_like else '0'
        is_dislike = '1' if has_dislike else '0'

        feedback_texts = []
        for fb in feedback_list:
            if fb.get('feedback_content'):
                feedback_texts.append(fb['feedback_content'])
        feedback_text = '; '.join(feedback_texts) if feedback_texts else ''

        dislike_tags = analysis.get('dislike_reason_tags', [])
        dislike_tags_str = ', '.join(dislike_tags) if isinstance(dislike_tags, list) and dislike_tags else ''

        feedback_root = analysis.get('feedback_root_cause', '无')
        feedback_summary = analysis.get('feedback_reason_summary', '无反馈')

        rows.append([
            target_date,
            f"CASE-{i+1:03d}",
            str(case_data.get('user_id', '')),
            str(case_data.get('message_id', '')),
            case_data.get('case_type', ''),
            case_data.get('user_version', ''),
            intent_l1,
            intent_l2,
            intent_l3,
            attr_str,
            convo_zh,
            analysis.get('analysis_point', ''),
            conclusion_str,
            str(satisfaction_score),
            satisfaction_reason,
            confidence,
            is_like,
            is_dislike,
            feedback_text,
            dislike_tags_str,
            feedback_root,
            feedback_summary,
        ])

    end_col = chr(64 + len(CASE_FEISHU_HEADERS))
    value_range = {
        'range': f'{sheet_id}!A2:{end_col}',
        'values': rows,
    }

    url = f'https://open.feishu.cn/open-apis/sheets/v2/spreadsheets/{FEISHU_CASE_SHEET_TOKEN}/values_append'
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


def run_case_analysis(target_date, progress_cb=None, sheet_output=True, fail_on_source_errors=False):
    def _progress(step, detail=''):
        if progress_cb:
            progress_cb(step, detail)

    _reset_source_errors()
    _progress('sampling', '正在抽样和查询反馈/点赞案例...')
    cases = sample_all_cases(target_date, progress_cb=progress_cb)

    source_errors = _source_errors_snapshot()
    if fail_on_source_errors and source_errors:
        return {
            'target_date': target_date,
            'source': {
                'profile': CASE_ANALYSIS_SOURCE_PROFILE,
                'database_id': _superset.db_id,
                'database_name': CASE_ANALYSIS_SUPERSET_DB_NAME if _is_new_site() else '',
            },
            'total_sampled': len(cases),
            'by_type': {
                case_type: len([case for case in cases if case.get('case_type') == case_type])
                for case_type in ['A', 'B', 'C', 'D', 'E', 'F']
            },
            'results': [],
            'errors': [],
            'source_errors': source_errors,
            'provider_counts': {},
            'input_hash': '',
        }

    day_start, next_day_start = _day_window(target_date)
    day_end = next_day_start if _is_new_site() else f"{target_date} 23:59:59"

    all_uids = list({c['user_id'] for c in cases})
    _progress('enriching', f'正在批量查询 {len(all_uids)} 个用户的反馈/点赞数据...')

    with ThreadPoolExecutor(max_workers=3) as pool:
        fb_future = pool.submit(_batch_get_feedback, all_uids, day_start, day_end)
        att_future = pool.submit(_batch_get_attitude, all_uids, day_start, day_end)
        ver_future = pool.submit(_batch_get_versions, all_uids)

        feedback_map = fb_future.result()
        attitude_map = att_future.result()
        version_map = ver_future.result()

    for case_data in cases:
        uid = case_data['user_id']
        case_data['feedback'] = feedback_map.get(uid, [])
        case_data['attitude'] = attitude_map.get(uid, [])
        if not case_data.get('user_version') or case_data['user_version'] == 'Free':
            case_data['user_version'] = version_map.get(uid, case_data.get('user_version', 'Free'))

    _progress('enriching', '反馈数据补充完成')
    input_hash = _case_input_hash(cases)

    summary = {
        'target_date': target_date,
        'source': {'profile': CASE_ANALYSIS_SOURCE_PROFILE, 'database_id': _superset.db_id, 'database_name': CASE_ANALYSIS_SUPERSET_DB_NAME if _is_new_site() else ''},
        'total_sampled': len(cases),
        'by_type': {},
        'results': [],
        'errors': [],
        'source_errors': _source_errors_snapshot(),
        'provider_counts': {},
        'input_hash': input_hash,
    }

    for case_type in ['A', 'B', 'C', 'D', 'E', 'F']:
        type_cases = [c for c in cases if c['case_type'] == case_type]
        summary['by_type'][case_type] = len(type_cases)

    valid_cases = []
    for case_data in cases:
        has_content = _has_conversation_content(case_data.get('conversations', []))
        if not has_content:
            summary['errors'].append({
                'case_type': case_data['case_type'],
                'user_id': case_data['user_id'],
                'error': 'No conversation content found',
            })
        else:
            valid_cases.append(case_data)

    _progress('analyzing', f'共 {len(valid_cases)} 个有效案例，开始 LLM 分析 (0/{len(valid_cases)})...')
    done_count = 0

    if CASE_ANALYSIS_WORKFLOW_ID and valid_cases:
        with ThreadPoolExecutor(max_workers=min(len(valid_cases), 15)) as pool:
            future_map = {
                pool.submit(analyze_single_case, cd): cd for cd in valid_cases
            }
            for future in as_completed(future_map):
                cd = future_map[future]
                done_count += 1
                try:
                    analysis_result = future.result(timeout=180)
                    provider = str(analysis_result.get('provider') or 'unknown')
                    summary['provider_counts'][provider] = summary['provider_counts'].get(provider, 0) + 1
                    if analysis_result.get('success'):
                        summary['results'].extend(analysis_result['results'])
                        _progress('analyzing', f'LLM 分析中 ({done_count}/{len(valid_cases)}) [Type {cd["case_type"]}] 成功 ({provider})')
                    else:
                        summary['errors'].append({
                            'case_type': cd['case_type'],
                            'user_id': cd['user_id'],
                            'error': analysis_result.get('error', 'Unknown'),
                        })
                        _progress('analyzing', f'LLM 分析中 ({done_count}/{len(valid_cases)}) [Type {cd["case_type"]}] 失败')
                except TimeoutError:
                    logger.warning('Case timed out: type=%s uid=%s', cd['case_type'], cd['user_id'])
                    summary['errors'].append({
                        'case_type': cd['case_type'],
                        'user_id': cd['user_id'],
                        'error': 'LLM request timed out',
                    })
                    _progress('analyzing', f'LLM 分析中 ({done_count}/{len(valid_cases)}) [Type {cd["case_type"]}] 超时')
                except Exception as e:
                    summary['errors'].append({
                        'case_type': cd['case_type'],
                        'user_id': cd['user_id'],
                        'error': str(e),
                    })
                    _progress('analyzing', f'LLM 分析中 ({done_count}/{len(valid_cases)}) [Type {cd["case_type"]}] 异常')
    elif valid_cases:
        for case_data in valid_cases:
            summary['results'].append({
                'case_data': case_data,
                'analysis': {},
            })

    _progress('pushing', f'分析完成，正在写入飞书 ({len(summary["results"])} 条)...')
    if sheet_output and summary['results'] and FEISHU_CASE_SHEET_TOKEN:
        try:
            feishu_result = push_results_to_feishu(summary['results'], target_date=target_date)
            summary['feishu'] = feishu_result
        except Exception as e:
            summary['feishu'] = {'error': str(e)}

    _progress('done', f'完成! 成功 {len(summary["results"])} 条, 失败 {len(summary["errors"])} 条')
    return summary
