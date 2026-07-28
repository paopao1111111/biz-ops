import json
import os
import threading
import time
from email.utils import parsedate_to_datetime

import requests


BASE_URL = 'https://open.feishu.cn/open-apis'
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


class FeishuAPIError(RuntimeError):
    def __init__(self, operation, status=None, code=None, msg='', request_id=''):
        self.operation = operation
        self.status = status
        self.code = code
        self.msg = str(msg or '')[:300]
        self.request_id = str(request_id or '')[:128]
        super().__init__(f'{operation} failed: http={status} code={code} msg={self.msg} request_id={self.request_id}')


class FeishuDocxClient:
    _token_cache = {}
    _token_lock = threading.Lock()

    def __init__(self, app_id=None, app_secret=None, bot_app_id=None, bot_app_secret=None,
                 chat_id=None, session=None, base_url=BASE_URL, max_retries=3, sleep=None):
        self.app_id = app_id if app_id is not None else os.getenv('FEISHU_APP_ID', '')
        self.app_secret = app_secret if app_secret is not None else os.getenv('FEISHU_APP_SECRET', '')
        self.bot_app_id = bot_app_id if bot_app_id is not None else os.getenv('FEISHU_BOT_APP_ID', '')
        self.bot_app_secret = bot_app_secret if bot_app_secret is not None else os.getenv('FEISHU_BOT_APP_SECRET', '')
        self.chat_id = chat_id if chat_id is not None else os.getenv('FEISHU_CHAT_ID', '')
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.base_url = base_url.rstrip('/')
        self.max_retries = max(0, int(max_retries))
        self._sleep = sleep or time.sleep

    @staticmethod
    def _request_id(response):
        return response.headers.get('X-Request-Id') or response.headers.get('X-Tt-Logid') or ''

    @staticmethod
    def _retry_delay(response, attempt):
        value = response.headers.get('Retry-After')
        if value:
            try:
                return max(0.0, float(value))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
                except Exception:
                    pass
        return min(8.0, 0.5 * (2 ** attempt))

    def _send(self, method, url, operation, **kwargs):
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.request(method, url, **kwargs)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise FeishuAPIError(operation, msg=f'network error: {type(exc).__name__}') from exc
                self._sleep(min(8.0, 0.5 * (2 ** attempt)))
                continue
            if response.status_code not in TRANSIENT_STATUSES or attempt >= self.max_retries:
                return response
            self._sleep(self._retry_delay(response, attempt))
        raise FeishuAPIError(operation, msg='retry loop exhausted')

    def _token(self, app_id, app_secret):
        if not app_id or not app_secret:
            raise FeishuAPIError('auth', msg='application credentials are not configured')
        key = app_id
        with self._token_lock:
            cached = self._token_cache.get(key)
            if cached and cached[1] > time.time() + 60:
                return cached[0]
        response = self._send('POST', f'{self.base_url}/auth/v3/tenant_access_token/internal', 'auth',
                              json={'app_id': app_id, 'app_secret': app_secret}, timeout=15)
        data = self._decode(response, 'auth')
        token = data.get('tenant_access_token')
        if not token:
            raise FeishuAPIError('auth', response.status_code, data.get('code'), 'token missing', self._request_id(response))
        with self._token_lock:
            self._token_cache[key] = (token, time.time() + int(data.get('expire') or 7200))
        return token

    def _decode(self, response, operation):
        try:
            data = response.json()
        except Exception:
            data = {}
        code = data.get('code')
        if response.status_code >= 400 or (code not in (None, 0)):
            raise FeishuAPIError(operation, response.status_code, code, data.get('msg') or 'API request failed', self._request_id(response))
        return data

    def _request(self, method, path, operation, token=None, **kwargs):
        headers = dict(kwargs.pop('headers', {}))
        headers['Authorization'] = f'Bearer {token or self._token(self.app_id, self.app_secret)}'
        headers.setdefault('Content-Type', 'application/json')
        response = self._send(method, f'{self.base_url}{path}', operation, headers=headers, timeout=30, **kwargs)
        return self._decode(response, operation)

    def create_document(self, title):
        data = self._request('POST', '/docx/v1/documents', 'create_document', json={'title': title})
        doc = (data.get('data') or {}).get('document') or {}
        if not doc.get('document_id'):
            raise FeishuAPIError('create_document', msg='document_id missing from response')
        return {'document_id': doc.get('document_id'), 'revision_id': doc.get('revision_id'),
                'url': self.document_url(doc.get('document_id'))}

    def list_blocks(self, document_id, page_size=500):
        items, page_token = [], ''
        while True:
            suffix = f'&page_token={page_token}' if page_token else ''
            data = self._request('GET', f'/docx/v1/documents/{document_id}/blocks?page_size={page_size}&document_revision_id=-1{suffix}', 'list_blocks')
            body = data.get('data') or {}
            items.extend(body.get('items') or [])
            if not body.get('has_more'):
                return items
            page_token = body.get('page_token') or ''
            if not page_token:
                raise FeishuAPIError('list_blocks', msg='has_more response omitted page_token')

    @staticmethod
    def _root(blocks):
        return next((block for block in blocks if block.get('block_type') == 1), blocks[0] if blocks else None)

    def batch_delete_children(self, document_id, parent_block_id, start_index, end_index):
        if end_index <= start_index:
            return {'deleted': 0}
        path = f'/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children/batch_delete?document_revision_id=-1'
        self._request('DELETE', path, 'batch_delete_children',
                      json={'start_index': int(start_index), 'end_index': int(end_index)})
        return {'deleted': end_index - start_index}

    def clear_document(self, document_id):
        blocks = self.list_blocks(document_id)
        root = self._root(blocks)
        children = list((root or {}).get('children') or [])
        if not root or not children:
            return {'deleted': 0}
        return self.batch_delete_children(document_id, root['block_id'], 0, len(children))

    def append_blocks(self, document_id, parent_block_id, blocks, index=-1):
        data = self._request('POST', f'/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children?document_revision_id=-1',
                             'append_blocks', json={'children': blocks, 'index': index})
        return (data.get('data') or {}).get('children') or []

    def create_table(self, document_id, parent_block_id, rows, columns):
        if rows < 1 or rows > 9 or columns < 1 or columns > 9:
            raise ValueError('Feishu table dimensions must be between 1 and 9 rows/columns')
        children = self.append_blocks(document_id, parent_block_id, [{'block_type': 31, 'table': {'property': {'row_size': rows, 'column_size': columns}}}])
        if not children:
            raise FeishuAPIError('create_table', msg='table block missing from response')
        return children[0]

    def batch_update_text(self, document_id, updates):
        updates = list(updates)
        for offset in range(0, len(updates), 50):
            requests_body = []
            for block_id, value in updates[offset:offset + 50]:
                requests_body.append({'block_id': block_id, 'update_text_elements': {
                    'elements': [{'text_run': {'content': str(value or '')[:2000]}}]}})
            self._request('PATCH', f'/docx/v1/documents/{document_id}/blocks/batch_update?document_revision_id=-1',
                          'batch_update_text', json={'requests': requests_body})
        return {'updated': len(updates)}

    def fill_table_cells(self, document_id, table_block, values):
        rows = [list(row) for row in values]
        if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
            raise ValueError('table values must be a non-empty rectangular matrix')
        expected = len(rows) * len(rows[0])
        blocks = self.list_blocks(document_id)
        by_id = {block.get('block_id'): block for block in blocks}
        table_id = table_block.get('block_id')
        table = by_id.get(table_id) or table_block
        child_cells = list(table.get('children') or [])
        table_cells = list((table.get('table') or {}).get('cells') or [])
        if len(child_cells) != expected or len(table_cells) != expected or child_cells != table_cells:
            raise FeishuAPIError('fill_table_cells', msg=f'table cell count/order mismatch: expected {expected}, children={len(child_cells)}, table.cells={len(table_cells)}')
        updates = []
        flat = [value for row in rows for value in row]
        for cell_id, value in zip(child_cells, flat):
            cell = by_id.get(cell_id)
            children = list((cell or {}).get('children') or [])
            if not cell or cell.get('block_type') != 32 or len(children) != 1:
                raise FeishuAPIError('fill_table_cells', msg=f'cell {cell_id} must be type 32 with exactly one text child')
            text = by_id.get(children[0])
            if not text or text.get('block_type') != 2:
                raise FeishuAPIError('fill_table_cells', msg=f'cell {cell_id} child must be a text block')
            updates.append((text['block_id'], value))
        if len(updates) != expected:
            raise FeishuAPIError('fill_table_cells', msg=f'text block count mismatch: expected {expected}, actual {len(updates)}')
        self.batch_update_text(document_id, updates)
        return {'cells_filled': len(updates)}

    def set_tenant_readable(self, document_id):
        return self._request('PATCH', f'/drive/v2/permissions/{document_id}/public?type=docx', 'set_tenant_readable',
                             json={'link_share_entity': 'tenant_readable'})

    def get_public_permission(self, document_id):
        data = self._request('GET', f'/drive/v2/permissions/{document_id}/public?type=docx', 'get_public_permission')
        body = data.get('data') or {}
        return body.get('permission_public') or body

    @staticmethod
    def _block_text(block):
        body = {}
        for key in ('text', 'heading1', 'heading2', 'heading3', 'heading4', 'heading5',
                    'heading6', 'heading7', 'heading8', 'heading9', 'bullet', 'ordered',
                    'quote', 'todo'):
            if block.get(key):
                body = block[key]
                break
        parts = []
        for element in body.get('elements') or []:
            parts.append(str((element.get('text_run') or {}).get('content') or ''))
        return ''.join(parts)

    @staticmethod
    def report_marker(report_hash, case_count):
        return f'CASE_ANALYSIS_REPORT hash={report_hash} cases={case_count}'

    def verify_document(self, document_id, report_hash=None, expected_case_count=None):
        blocks = self.list_blocks(document_id)
        permission = self.get_public_permission(document_id)
        tenant_readable = permission.get('link_share_entity') == 'tenant_readable'
        marker = None
        for block in blocks:
            text = self._block_text(block)
            if 'CASE_ANALYSIS_REPORT hash=' in text:
                marker = text
        marker_ok = True
        if report_hash is not None or expected_case_count is not None:
            marker_ok = marker == self.report_marker(report_hash, expected_case_count)
        readable = bool(blocks) and tenant_readable and marker_ok
        return {'document_id': document_id, 'block_count': len(blocks), 'readable': readable,
                'tenant_readable': tenant_readable, 'marker': marker, 'marker_ok': marker_ok}

    def send_document_card(self, title, document_url, summary='', chat_id=None):
        receive_id = chat_id or self.chat_id
        if not receive_id:
            raise FeishuAPIError('send_document_card', msg='FEISHU_CHAT_ID is not configured')
        card = {'config': {'wide_screen_mode': True}, 'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': 'blue'},
                'elements': [{'tag': 'div', 'text': {'tag': 'lark_md', 'content': summary[:1000]}},
                             {'tag': 'action', 'actions': [{'tag': 'button', 'text': {'tag': 'plain_text', 'content': '查看报告'}, 'url': document_url, 'type': 'primary'}]}]}
        token = self._token(self.bot_app_id, self.bot_app_secret)
        data = self._request('POST', '/im/v1/messages?receive_id_type=chat_id', 'send_document_card', token=token,
                             json={'receive_id': receive_id, 'msg_type': 'interactive', 'content': json.dumps(card, ensure_ascii=False)})
        return (data.get('data') or {}).get('message_id')

    @staticmethod
    def document_url(document_id):
        return f'https://www.feishu.cn/docx/{document_id}'

    @staticmethod
    def rich_text_block(text, block_type=2):
        field = {2: 'text', 3: 'heading1', 4: 'heading2', 5: 'heading3'}.get(block_type)
        if not field:
            raise ValueError(f'unsupported rich text block type: {block_type}')
        return {'block_type': block_type, field: {'elements': [{'text_run': {'content': str(text)}}]}}

    @staticmethod
    def text_block(text):
        return FeishuDocxClient.rich_text_block(text, 2)

    @staticmethod
    def heading_block(text, level=2):
        block_type = {1: 3, 2: 4, 3: 5}.get(int(level))
        if not block_type:
            raise ValueError('heading level must be between 1 and 3')
        return FeishuDocxClient.rich_text_block(text, block_type)

    def _append_text(self, document_id, root_id, text):
        return self.append_blocks(document_id, root_id, [self.text_block(text)])

    def _append_heading(self, document_id, root_id, text, level=2):
        return self.append_blocks(document_id, root_id, [self.heading_block(text, level)])

    def _append_table(self, document_id, root_id, rows):
        if not rows or not rows[0] or any(len(row) != len(rows[0]) for row in rows):
            raise ValueError('table rows must be a non-empty rectangular matrix')
        table = self.create_table(document_id, root_id, len(rows), len(rows[0]))
        self.fill_table_cells(document_id, table, rows)

    def render_report(self, document_id, report, report_hash, chunk_size=20):
        blocks = self.list_blocks(document_id)
        root = self._root(blocks)
        if not root:
            raise FeishuAPIError('render_report', msg='document root block missing')
        root_id = root['block_id']
        old_children = list(root.get('children') or [])
        old_count = len(old_children)
        cases = report.get('cases') or []
        case_count = len(cases)
        marker = self.report_marker(report_hash, case_count)
        case_chunk_size = max(1, min(int(chunk_size), 8))
        try:
            overview = report.get('overview') or {}
            executive = report.get('executive_summary') or {}
            analyzed = int(overview.get('analyzed') or 0)
            model_issues = int(executive.get('model_issues') or 0)
            product_issues = int(executive.get('product_issues') or 0)
            guidance_issues = int(executive.get('guidance_issues') or 0)
            no_output_count = int(executive.get('no_output_count') or 0)
            no_output_rate = executive.get('no_output_rate') or '0%'

            def compact(value, limit):
                text = ' '.join(str(value or '').split())
                return text if len(text) <= limit else f'{text[:limit - 1]}…'

            self._append_heading(document_id, root_id, report.get('title', 'Daily Case Analysis'), 1)
            self._append_heading(document_id, root_id, '今日概览', 2)
            risk_text = (
                f'严重风险：{no_output_count}/{analyzed} 个案例出现空回复或无输出，占比 {no_output_rate}。'
                '优先检查模型调用、Agent 链路和异常返回处理。'
                if no_output_count else '当日案例未发现集中性的空回复或无输出问题。'
            )
            self._append_text(document_id, root_id, risk_text)
            self._append_table(document_id, root_id, [
                ['案例', '成功', '失败', '空回复类', '模型问题', '产品问题', '引导问题'],
                [overview.get('sampled', 0), analyzed, int(overview.get('errors') or 0),
                 f'{no_output_count} / {no_output_rate}', model_issues, product_issues, guidance_issues],
            ])

            priority_summary = report.get('priority_summary') or []
            if priority_summary:
                self._append_heading(document_id, root_id, '重点问题', 2)
                priority_rows = [['优先级', '问题 / 数量', '代表案例', '建议']]
                for item in priority_summary[:8]:
                    priority_rows.append([
                        item.get('priority_label') or item.get('priority'),
                        f"{item.get('issue')} / {item.get('count', 0)}",
                        '、'.join(item.get('case_ids') or []) or '—',
                        item.get('direction') or '—',
                    ])
                self._append_table(document_id, root_id, priority_rows)

            self._append_heading(document_id, root_id, '采样构成', 2)
            type_breakdown = report.get('type_breakdown') or []
            short_type_names = {
                'A': '首次付费', 'B': '新用户单轮', 'C': '未付费3–4轮',
                'D': '未付费≥5轮', 'E': '点踩', 'F': '反馈触发',
            }
            type_rows = []
            for offset in range(0, min(len(type_breakdown), 6), 3):
                row = []
                for item in type_breakdown[offset:offset + 3]:
                    case_type = item.get('type')
                    row.append(f"{case_type}｜{short_type_names.get(case_type, item.get('name'))}（{item.get('count', 0)}）")
                while len(row) < 3:
                    row.append('—')
                type_rows.append(row)
            self._append_table(document_id, root_id, type_rows or [['暂无样本', '—', '—']])

            self._append_heading(document_id, root_id, '案例分析明细', 2)
            analysis_headers = ['Case', '类型/版本', 'Agent', '意图', '对话摘要', '问题/归因', '结论', '满意度/置信度']
            for offset in range(0, len(cases), case_chunk_size):
                analysis_rows = [analysis_headers]
                for case in cases[offset:offset + case_chunk_size]:
                    intent_parts = [part.strip() for part in str(case.get('intent') or '未分类').split('/') if part.strip()]
                    intent_text = ' / '.join(intent_parts[-2:]) if len(intent_parts) > 1 else (intent_parts[0] if intent_parts else '未分类')
                    problem_text = '；'.join(filter(None, [
                        compact(case.get('analysis_point') or case.get('issue_label') or '—', 75),
                        compact(case.get('attribution') or '未分类', 55),
                    ]))
                    analysis_rows.append([
                        f"{case.get('case_id')}｜{case.get('priority_label')}",
                        f"{case.get('case_type')} / {case.get('user_version') or '未知'}",
                        compact(case.get('agent') or '通用对话/未识别', 36),
                        compact(intent_text, 65),
                        compact(case.get('conversation_summary_zh') or '—', 105),
                        compact(problem_text, 130),
                        compact(case.get('conclusion') or '—', 75),
                        f"{case.get('satisfaction_score') or '无法判断'} / {case.get('confidence') or '未记录'}",
                    ])
                self._append_table(document_id, root_id, analysis_rows)

            self._append_heading(document_id, root_id, '数据库查询索引', 2)
            locator_headers = ['Case', 'Source Key', 'Topic ID', 'Message ID', '记录 ID']
            for offset in range(0, len(cases), case_chunk_size):
                locator_rows = [locator_headers]
                for case in cases[offset:offset + case_chunk_size]:
                    locator_rows.append([
                        case.get('case_id') or '—',
                        case.get('source_key') or '—',
                        ','.join(case.get('topic_ids') or []) or '—',
                        case.get('message_id') or '—',
                        f"chat={case.get('chat_log_id') or '—'}；trigger={case.get('trigger_id') or '—'}",
                    ])
                self._append_table(document_id, root_id, locator_rows)

            if report.get('error_summary'):
                self._append_heading(document_id, root_id, '失败摘要', 2)
                error_rows = [['失败类型', '数量']] + [[key, value] for key, value in report.get('error_summary')[:8]]
                self._append_table(document_id, root_id, error_rows)

            provider_text = '，'.join(
                f'{provider}: {count}'
                for provider, count in (report.get('provider_counts') or {}).items()
            ) or '未记录'
            source = report.get('source') or {}
            source_name = source.get('database_name') or source.get('name') or 'iweaver-hermes-ai'
            source_profile = source.get('profile') or 'new_site'
            self._append_heading(document_id, root_id, '运行信息', 2)
            self._append_text(
                document_id, root_id,
                f"{report.get('target_date') or '—'}｜{source_name}/{source_profile}｜{provider_text}｜"
                f"高置信度 {int(overview.get('high_confidence') or 0)}｜版式 {report.get('layout_version') or '3.1'}",
            )
            self._append_text(document_id, root_id, marker)

            verification = self.verify_document(document_id, report_hash, case_count)
            if not verification.get('readable'):
                raise FeishuAPIError('render_report', msg='new report marker/public permission verification failed')
            refreshed = self.list_blocks(document_id)
            refreshed_root = self._root(refreshed)
            new_count = len((refreshed_root or {}).get('children') or []) - old_count
            if new_count <= 0:
                raise FeishuAPIError('render_report', msg='new report root children missing')
            if old_count:
                self.batch_delete_children(document_id, root_id, 0, old_count)
            return {'document_id': document_id, 'url': self.document_url(document_id), 'case_count': case_count,
                    'report_hash': report_hash, 'verification': verification}
        except Exception:
            try:
                refreshed = self.list_blocks(document_id)
                refreshed_root = self._root(refreshed)
                total = len((refreshed_root or {}).get('children') or [])
                if total > old_count:
                    self.batch_delete_children(document_id, root_id, old_count, total)
            except Exception:
                pass
            raise
