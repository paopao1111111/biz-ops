import ast
import hashlib
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_case_analysis(profile='legacy'):
    os.environ['CASE_ANALYSIS_SOURCE_PROFILE'] = profile
    sys.modules.pop('adapters.wp_tes.runtime.case_analysis', None)
    html = types.ModuleType('adapters.wp_tes.runtime.html_generat')
    html._run_coze_workflow = lambda *a, **k: {'success': True, 'content': '{}'}
    html._parse_json_string_if_possible = lambda x: x
    html.clean_output_content = lambda x: x
    html.WORKFLOW_PROVIDER = 'remote'
    coze = types.ModuleType('adapters.wp_tes.runtime.coze_llm')
    coze.call_coze_llm = lambda *a, **k: {'success': True, 'output': '{}'}
    sys.modules[html.__name__] = html
    sys.modules[coze.__name__] = coze
    return importlib.import_module('adapters.wp_tes.runtime.case_analysis')


class Response:
    def __init__(self, data, status=200, headers=None):
        self._data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self._data


class Session:
    def __init__(self, responses):
        self.responses, self.calls, self.trust_env = list(responses), [], True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class CaseAnalysisTests(unittest.TestCase):
    def test_formatter_ast_source_hash(self):
        path = ROOT / 'adapters/wp_tes/runtime/case_analysis.py'
        source = path.read_text()
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'format_case_for_llm')
        self.assertEqual(hashlib.sha256(ast.get_source_segment(source, node).encode()).hexdigest(),
                         '74efdf94e75f49fc6e769b0b09e1124102744477c0998eaa4d366c804b9ccda8')

    def test_daily_and_manual_formatter_same(self):
        legacy = load_case_analysis('legacy')
        case = {'case_type': 'B', 'user_version': 'Free', 'conversations': [{'topic_id': '1', 'agent_type': 'agent-x',
                'messages': [{'role': 'user', 'content': 'hello', 'time': '2026-01-01', 'message_id': 'm'}]}]}
        manual = legacy.format_case_for_llm(case)
        daily = load_case_analysis('new_site').format_case_for_llm(case)
        self.assertEqual(manual, daily)

    def test_local_provider_metadata_is_preserved(self):
        module = load_case_analysis('new_site')
        module.WORKFLOW_PROVIDER = 'local'
        module.NEW_COZE_WORKFLOW_ID = 'legacy-compatible-id'
        module.call_coze_llm = lambda *args, **kwargs: {
            'success': True,
            'output': '{}',
            'provider': 'cliproxy',
        }
        case = {'case_type': 'B', 'user_version': 'Free', 'conversations': []}
        result = module.analyze_single_case(case)
        self.assertTrue(result['success'])
        self.assertEqual(result['provider'], 'cliproxy')

    def test_new_site_feedback_mapping(self):
        module = load_case_analysis('new_site')
        self.assertEqual(module._map_new_site_feedback({'kind': 'like'})['type'], 'thumbs_up')
        self.assertEqual(module._map_new_site_feedback({'kind': 'dislike'})['type'], 'thumbs_down')
        self.assertEqual(module._map_new_site_feedback({'kind': 'feedback', 'comment': 'x'})['feedback_content'], 'x')
        self.assertEqual(module._map_new_site_attitude({'kind': 'like'})['type'], 1)
        self.assertEqual(module._map_new_site_attitude({'kind': 'dislike'})['type'], 2)

    def test_new_site_conversation_preserves_database_record_id(self):
        module = load_case_analysis('new_site')
        module._run_query = lambda *args, **kwargs: ([{
            'id': 'chat-row-1', 'topic_id': 7356, 'role': 'user',
            'message': {'content': 'hello'}, 'message_id': 'message-1',
            'created_at': '2026-01-01 12:00:00', 'agent_name': 'agent-x', 'agent_id': '',
        }], [])
        conversations = module._fetch_conversations('user-1', '2026-01-01 00:00:00', '2026-01-02 00:00:00')
        message = conversations[0]['messages'][0]
        self.assertEqual(message['record_id'], 'chat-row-1')
        self.assertEqual(message['message_id'], 'message-1')
        self.assertEqual(module._get_first_chat_log_id(conversations), 'chat-row-1')

    def test_new_site_sql_public_feedback_half_open(self):
        module = load_case_analysis('new_site')
        captured = []
        module._run_query = lambda sql, limit=1000: (captured.append(sql) or ([], []))
        module._batch_get_feedback(['u'], '2026-01-01 00:00:00', '2026-01-02 00:00:00')
        module.fetch_type_e_cases('2026-01-01', 2)
        sql = '\n'.join(captured)
        self.assertIn('public.chat_feedback', sql)
        self.assertIn("kind = 'dislike'", sql)
        self.assertIn("created_at < '2026-01-02 00:00:00'", sql)
        self.assertNotIn("created_at <= '2026-01-02 00:00:00'", sql)

    def test_trigger_event_topic_message_and_dedup(self):
        module = load_case_analysis('new_site')
        module._run_query = lambda *a, **k: ([{'user_id': 'u', 'topic_id': 'topic-event', 'message_id': 'msg-event',
                                               'earliest_fb': '2026-01-01 12:00:00'}], [])
        captured = []
        module._fetch_case_details = lambda uid, start, end, topic_id=None: (
            captured.append(topic_id) or ([{'topic_id': topic_id, 'agent_type': 'agent-x',
                                            'messages': [{'role': 'user', 'content': 'x', 'message_id': 'other'}]}], 'Free'))
        cases = module._fetch_feedback_cases('2026-01-01')
        self.assertEqual(captured, ['topic-event'])
        self.assertEqual(cases[0]['topic_id'], 'topic-event')
        self.assertEqual(cases[0]['message_id'], 'msg-event')
        merged = module._merge_cases([{'user_id': 'u', 'topic_id': 'topic-event', 'message_id': 'm1'}], cases)
        self.assertEqual(len(merged), 1)

    def test_fail_on_source_errors_stops_before_analysis(self):
        module = load_case_analysis('new_site')
        case = {'case_type': 'B', 'user_id': 'u', 'topic_id': 't', 'message_id': 'm',
                'conversations': [{'topic_id': 't', 'messages': [{'role': 'user', 'content': 'x'}]}]}
        def sample(*args, **kwargs):
            module._record_source_error('source', RuntimeError('failed'))
            return [case]
        module.sample_all_cases = sample
        with mock.patch.object(module, 'analyze_single_case') as analyze, \
             mock.patch.object(module.logger, 'exception'):
            summary = module.run_case_analysis('2026-01-01', sheet_output=False, fail_on_source_errors=True)
        analyze.assert_not_called()
        self.assertEqual(summary['results'], [])
        self.assertEqual(summary['source_errors'][0]['source'], 'source')

    def test_sheet_output_false_never_pushes(self):
        module = load_case_analysis('new_site')
        module.sample_all_cases = lambda *a, **k: []
        module.FEISHU_CASE_SHEET_TOKEN = 'configured'
        with mock.patch.object(module, 'push_results_to_feishu') as push:
            module.run_case_analysis('2026-01-01', sheet_output=False)
            push.assert_not_called()


class ReportTests(unittest.TestCase):
    def test_agent_summaries_and_privacy(self):
        from adapters.wp_tes.runtime.case_analysis_report import build_report
        summary = {'target_date': '2026-01-01', 'total_sampled': 1,
                   'provider_counts': {'cliproxy': 1},
                   'errors': [{'error': 'Failed to parse analysis: invalid'}],
                   'results': [{'case_data': {
            'case_type': 'B', 'user_id': 'someone@example.com', 'message_id': 'message-1',
            'conversations': [{'secret': 'RAW', 'agent_type': 'agent-pdf'}]},
            'analysis': {'conclusion': 'ok', 'conversation_summary_zh': '中文摘要',
                         'attribution': '模型问题', 'feedback_root_cause': '产品问题'}}]}
        report = build_report(summary)
        encoded = json.dumps(report)
        row = report['cases'][0]
        self.assertEqual(row['user_id'], 'so***@example.com')
        self.assertEqual(row['agent'], 'agent-pdf')
        self.assertEqual(row['conversation_summary_zh'], '中文摘要')
        self.assertEqual(report['attribution_summary'], [('模型问题', 1)])
        self.assertEqual(report['issue_summary'], [('产品问题', 1)])
        self.assertEqual(report['provider_counts'], {'cliproxy': 1})
        self.assertEqual(report['error_summary'], [('LLM输出解析失败', 1)])
        self.assertNotIn('RAW', encoded)
        self.assertNotIn('conversations', encoded)

    def test_source_messages_stabilize_no_output_detection(self):
        from adapters.wp_tes.runtime.case_analysis_report import build_report
        summary = {'target_date': '2026-01-01', 'total_sampled': 1, 'errors': [], 'results': [{
            'case_data': {'case_type': 'B', 'user_id': 'u', 'topic_id': '1', 'message_id': 'm',
                          'conversations': [{'topic_id': '1', 'messages': [
                              {'role': 'user', 'content': 'hello'},
                              {'role': 'assistant', 'content': '[agent-x 回复为空]'},
                          ]}]},
            'analysis': {'analysis_point': '任务失败', 'conclusion': '体验差',
                         'attribution': '模型问题', 'feedback_root_cause': '模型问题 - 无回复'},
        }]}
        report = build_report(summary)
        self.assertEqual(report['overview']['no_output'], 1)
        self.assertEqual(report['executive_summary']['model_issues'], 1)
        self.assertEqual(report['cases'][0]['feedback_root_cause'], '模型问题')
        self.assertEqual(report['cases'][0]['priority'], 'P0')

    def test_positive_signal_prevents_false_empty_output_classification(self):
        from adapters.wp_tes.runtime.case_analysis_report import build_report
        summary = {'target_date': '2026-01-01', 'total_sampled': 1, 'errors': [], 'results': [{
            'case_data': {'case_type': 'F', 'user_id': 'u', 'topic_id': '1', 'message_id': 'm',
                          'attitude': [{'type': 1}],
                          'conversations': [{'topic_id': '1', 'messages': [
                              {'role': 'user', 'content': 'hello'},
                              {'role': 'assistant', 'content': '[agent-x 回复为空]'},
                          ]}]},
            'analysis': {'analysis_point': '用户认可结果', 'conclusion': '体验优秀',
                         'attribution': '无', 'feedback_root_cause': '无',
                         'satisfaction_score': 9, 'confidence_level': '高'},
        }]}
        report = build_report(summary)
        self.assertEqual(report['overview']['no_output'], 0)
        self.assertEqual(report['cases'][0]['priority'], 'P2')

    def test_executive_summary_priorities_and_database_locators(self):
        from adapters.wp_tes.runtime.case_analysis_report import build_report
        summary = {
            'target_date': '2026-07-19', 'total_sampled': 2,
            'by_type': {'B': 1, 'F': 1}, 'provider_counts': {'cliproxy': 2}, 'errors': [],
            'results': [
                {'case_data': {
                    'case_type': 'B', 'user_id': '2c29a18a-aa59-452a-94b8-9060cf7c',
                    'topic_id': '7356', 'message_id': 'message-empty', 'chat_log_id': 'chat-row-empty',
                    'user_version': 'Free',
                    'conversations': [{'topic_id': '7356', 'agent_type': '', 'messages': []}],
                }, 'analysis': {
                    'conversation_summary_zh': '用户提问后模型始终返回空回复。',
                    'analysis_point': '模型无输出', 'conclusion': '严重失败',
                    'attribution': '模型问题 - 无回复', 'feedback_root_cause': '模型问题',
                    'satisfaction_score': '无法判断', 'confidence_level': '高',
                }},
                {'case_data': {
                    'case_type': 'F', 'user_id': 'user-2', 'topic_id': '7109',
                    'message_id': 'message-like', 'chat_log_id': 'chat-row-like',
                    'trigger_id': 'feedback-row-like', 'user_version': 'Pro',
                    'conversations': [{'topic_id': '7109', 'agent_type': 'quiz-agent', 'messages': []}],
                }, 'analysis': {
                    'conversation_summary_zh': '用户认可生成结果。', 'analysis_point': '可优化主动追问',
                    'conclusion': '体验优秀', 'attribution': '引导问题 - 缺乏主动询问',
                    'feedback_root_cause': '引导问题', 'feedback_reason_summary': '用户点赞',
                    'satisfaction_score': {'score': 9, 'reason': '用户点赞'}, 'confidence_level': '高',
                }},
            ],
        }
        report = build_report(summary)
        self.assertEqual(report['layout_version'], '3.1')
        self.assertEqual(report['overview']['no_output'], 1)
        self.assertEqual(report['overview']['no_output_rate'], '50%')
        self.assertEqual(report['executive_summary']['high_confidence'], 2)
        self.assertEqual(report['priority_summary'][0]['priority'], 'P0')
        first = report['cases'][0]
        self.assertEqual(first['priority'], 'P0')
        self.assertEqual(first['topic_ids'], ['7356'])
        self.assertEqual(first['chat_log_id'], 'chat-row-empty')
        self.assertEqual(first['agent'], '通用对话/未识别')
        self.assertRegex(first['source_key'], r'^SRC-[A-F0-9]{12}$')
        feedback_case = next(case for case in report['cases'] if case['trigger_id'])
        self.assertEqual(feedback_case['trigger_id'], 'feedback-row-like')
        self.assertEqual(feedback_case['satisfaction_score'], '9')
        self.assertEqual(report['type_breakdown'][1], {
            'type': 'B', 'name': '当日首次出现且仅一轮对话', 'count': 1,
        })


class FeishuTests(unittest.TestCase):
    def setUp(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        FeishuDocxClient._token_cache.clear()

    @staticmethod
    def table_shape(table_id='t1', rows=2, columns=2):
        cell_ids = [f'c{i}' for i in range(rows * columns)]
        blocks = [{'block_id': table_id, 'block_type': 31, 'children': cell_ids,
                   'table': {'cells': cell_ids}}]
        for i, cell_id in enumerate(cell_ids):
            text_id = f'x{i}'
            blocks.extend([{'block_id': cell_id, 'block_type': 32, 'children': [text_id]},
                           {'block_id': text_id, 'block_type': 2, 'text': {'elements': []}}])
        return cell_ids, blocks

    def test_actual_table_shape_and_batch_update_payload(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        _, blocks = self.table_shape()
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        client.list_blocks = mock.Mock(return_value=blocks)
        client._request = mock.Mock(return_value={'code': 0})
        result = client.fill_table_cells('d1', blocks[0], [['a', 'b'], ['c', 'd']])
        self.assertEqual(result['cells_filled'], 4)
        method, path, operation = client._request.call_args.args
        payload = client._request.call_args.kwargs['json']
        self.assertEqual((method, operation), ('PATCH', 'batch_update_text'))
        self.assertIn('/blocks/batch_update?document_revision_id=-1', path)
        self.assertEqual([request['block_id'] for request in payload['requests']], ['x0', 'x1', 'x2', 'x3'])
        self.assertEqual(payload['requests'][0]['update_text_elements']['elements'][0]['text_run']['content'], 'a')

    def test_table_dimensions_respect_feishu_nine_by_nine_limit(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        with self.assertRaises(ValueError):
            client.create_table('d', 'root', 10, 2)
        with self.assertRaises(ValueError):
            client.create_table('d', 'root', 2, 10)

    def test_batch_update_chunks_at_50(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        client._request = mock.Mock(return_value={'code': 0})
        client.batch_update_text('d', [(f'x{i}', i) for i in range(101)])
        self.assertEqual([len(call.kwargs['json']['requests']) for call in client._request.call_args_list], [50, 50, 1])

    def test_table_count_and_text_child_mismatch(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuAPIError, FeishuDocxClient
        _, blocks = self.table_shape()
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        client.list_blocks = mock.Mock(return_value=blocks[:-1])
        with self.assertRaises(FeishuAPIError):
            client.fill_table_cells('d', blocks[0], [['a', 'b'], ['c', 'd']])
        bad = list(blocks)
        bad[0] = dict(bad[0], children=['c0'])
        client.list_blocks.return_value = bad
        with self.assertRaises(FeishuAPIError):
            client.fill_table_cells('d', bad[0], [['a', 'b'], ['c', 'd']])

    def test_transient_retry_honors_retry_after(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        session = Session([Response({}, 429, {'Retry-After': '2'}), Response({'code': 0})])
        sleeps = []
        client = FeishuDocxClient('app', 'secret', session=session, sleep=sleeps.append)
        response = client._send('GET', 'https://example.invalid', 'retry')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sleeps, [2.0])

    def test_network_error_retry(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        session = Session([requests.exceptions.ReadTimeout('slow'), Response({'code': 0})])
        sleeps = []
        client = FeishuDocxClient('app', 'secret', session=session, sleep=sleeps.append)
        response = client._send('GET', 'https://example.invalid', 'retry')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sleeps, [0.5])

    def test_public_permission_unwraps_feishu_response(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        client._request = mock.Mock(return_value={
            'code': 0,
            'data': {'permission_public': {'link_share_entity': 'tenant_readable'}},
        })
        self.assertEqual(client.get_public_permission('d')['link_share_entity'], 'tenant_readable')

    def test_native_heading_payload_and_text_extraction(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        block = FeishuDocxClient.heading_block('今日核心结论', 2)
        self.assertEqual(block['block_type'], 4)
        self.assertEqual(block['heading2']['elements'][0]['text_run']['content'], '今日核心结论')
        self.assertEqual(FeishuDocxClient._block_text(block), '今日核心结论')
        with self.assertRaises(ValueError):
            FeishuDocxClient.heading_block('invalid', 4)

    def test_redesigned_render_uses_readable_table_shapes_and_bottom_marker(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        client.list_blocks = mock.Mock(side_effect=[
            [{'block_id': 'root', 'block_type': 1, 'children': []}],
            [{'block_id': 'root', 'block_type': 1, 'children': ['new-root-child']}],
        ])
        client._append_heading = mock.Mock()
        client._append_text = mock.Mock()
        client._append_table = mock.Mock()
        client.verify_document = mock.Mock(return_value={'readable': True})
        report = {
            'layout_version': '3.1', 'title': 'Report', 'target_date': '2026-01-01',
            'source': {'profile': 'new_site', 'database_name': 'iweaver-hermes-ai'},
            'overview': {'sampled': 1, 'analyzed': 1, 'errors': 0, 'no_output': 1,
                         'high_confidence': 1, 'judged_satisfaction': 0},
            'executive_summary': {'no_output_count': 1, 'no_output_rate': '100%',
                                  'model_issues': 1, 'product_issues': 0, 'guidance_issues': 0},
            'priority_summary': [{'priority': 'P0', 'priority_label': 'P0', 'issue': '空回复/无输出',
                                  'count': 1, 'case_ids': ['CASE-001'], 'direction': '检查链路'}],
            'issue_summary': [('模型问题', 1)],
            'type_breakdown': [{'type': 'B', 'name': '单轮用户', 'count': 1}],
            'provider_counts': {'cliproxy': 1}, 'error_summary': [],
            'cases': [{'case_id': 'CASE-001', 'source_key': 'SRC-ABCDEF123456', 'priority': 'P0',
                       'priority_label': 'P0', 'issue_label': '空回复/无输出', 'case_type': 'B',
                       'case_type_name': '单轮用户', 'agent': '通用对话/未识别', 'feedback_root_cause': '模型问题',
                       'attribution': '模型问题 - 无回复', 'topic_ids': ['7356'], 'message_id': 'm1',
                       'chat_log_id': 'row1', 'trigger_id': '', 'user_id': 'user', 'user_version': 'Free',
                       'intent': '知识问答', 'conclusion': '严重失败', 'analysis_point': '无输出',
                       'conversation_summary_zh': '模型未回复', 'satisfaction_score': '无法判断',
                       'confidence': '高', 'satisfaction_reason': '无输出', 'feedback_reason_summary': '无反馈'}],
        }
        base_case = dict(
            report['cases'][0],
            conversation_summary_zh='摘要内容' * 200,
            analysis_point='核心问题' * 100,
            intent='用户意图' * 100,
            conclusion='分析结论' * 100,
            feedback_reason_summary='反馈说明' * 100,
        )
        report['cases'] = [dict(
            base_case,
            case_id=f'CASE-{index:03d}',
            source_key=f'SRC-{index:012X}',
            message_id=f'message-{index}',
            chat_log_id=f'row-{index}',
        ) for index in range(1, 19)]
        client.render_report('d', report, 'hash')
        tables = [call.args[2] for call in client._append_table.call_args_list]
        self.assertTrue(all(1 <= len(rows) <= 9 and 1 <= len(rows[0]) <= 9 for rows in tables))
        analysis_tables = [rows for rows in tables if rows[0] == ['Case', '类型/版本', 'Agent', '意图', '对话摘要', '问题/归因', '结论', '满意度/置信度']]
        locator_tables = [rows for rows in tables if rows[0] == ['Case', 'Source Key', 'Topic ID', 'Message ID', '记录 ID']]
        self.assertEqual(len(analysis_tables), 3)
        self.assertEqual(len(locator_tables), 3)
        self.assertLessEqual(max(len(str(cell)) for rows in analysis_tables for row in rows[1:] for cell in row), 130)
        self.assertFalse(any('\n' in str(cell) for rows in analysis_tables for row in rows[1:] for cell in row))
        self.assertFalse(any(len(rows) == 9 and len(rows[0]) == 2 for rows in tables))
        headings = [call.args[2] for call in client._append_heading.call_args_list]
        self.assertIn('今日概览', headings)
        self.assertIn('案例分析明细', headings)
        self.assertIn('数据库查询索引', headings)
        self.assertFalse(any(str(heading).startswith('CASE-') for heading in headings))
        root_blocks = len(client._append_heading.call_args_list) + len(client._append_text.call_args_list) + len(tables)
        self.assertLessEqual(root_blocks, 19)
        self.assertEqual(client._append_text.call_args_list[-1].args[2], client.report_marker('hash', 18))

    def test_marker_and_public_permission_verification(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        marker = client.report_marker('abc', 2)
        client.list_blocks = mock.Mock(return_value=[{'block_id': 'r', 'block_type': 1}, client.text_block(marker)])
        client.get_public_permission = mock.Mock(return_value={'link_share_entity': 'tenant_readable'})
        self.assertTrue(client.verify_document('d', 'abc', 2)['readable'])
        self.assertFalse(client.verify_document('d', 'abc', 3)['readable'])
        client.get_public_permission.return_value = {'link_share_entity': 'closed'}
        self.assertFalse(client.verify_document('d', 'abc', 2)['readable'])

    def test_transactional_render_preserves_old_on_failure(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient('app', 'secret', session=Session([]))
        snapshots = [
            [{'block_id': 'root', 'block_type': 1, 'children': ['old']}],
            [{'block_id': 'root', 'block_type': 1, 'children': ['old', 'new1', 'new2']}],
        ]
        client.list_blocks = mock.Mock(side_effect=snapshots)
        client._append_heading = mock.Mock(side_effect=RuntimeError('boom'))
        client.batch_delete_children = mock.Mock()
        with self.assertRaises(RuntimeError):
            client.render_report('d', {'cases': [], 'overview': {}}, 'hash')
        client.batch_delete_children.assert_called_once_with('d', 'root', 1, 3)

    def test_api_error_does_not_include_secrets(self):
        from adapters.wp_tes.runtime.feishu_docx import FeishuAPIError, FeishuDocxClient
        session = Session([Response({'code': 999, 'msg': 'denied'}, 403, {'X-Request-Id': 'rid'})])
        client = FeishuDocxClient('app', 'TOPSECRET', session=session)
        with self.assertRaises(FeishuAPIError) as ctx:
            client.create_document('x')
        text = str(ctx.exception)
        self.assertIn('rid', text)
        self.assertNotIn('TOPSECRET', text)


class FakeClient:
    instances = []
    existing_readable = True

    def __init__(self):
        self.calls = []
        self.__class__.instances.append(self)

    @staticmethod
    def document_url(document_id):
        return f'https://www.feishu.cn/docx/{document_id}'

    def create_document(self, title):
        self.calls.append(('create', title))
        return {'document_id': 'doc-1', 'url': self.document_url('doc-1')}

    def verify_document(self, document_id, report_hash=None, expected_case_count=None):
        self.calls.append(('verify', document_id, report_hash, expected_case_count))
        return {'readable': self.existing_readable, 'marker_ok': self.existing_readable,
                'tenant_readable': self.existing_readable}

    def set_tenant_readable(self, document_id):
        self.calls.append(('permission', document_id))

    def render_report(self, document_id, report, report_hash):
        self.calls.append(('render', document_id, report_hash))
        return {'verification': {'readable': True, 'marker_ok': True, 'tenant_readable': True}}

    def send_document_card(self, title, url, summary):
        self.calls.append(('notify', title, url, summary))
        return 'msg-1'


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('daily_runner', ROOT / 'scripts/run_case_analysis_daily.py')
        cls.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.runner)

    def setUp(self):
        FakeClient.instances.clear()
        FakeClient.existing_readable = True

    def test_same_date_state_notification_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = self.runner.connect_state(Path(tmp) / 'state.db')
            self.runner.update_state(conn, '2026-01-01', document_id='doc-1', source_hash='h', report_hash='report-h', notification_state='sent')
            state = self.runner.get_state(conn, '2026-01-01')
            self.assertFalse(self.runner.should_notify(state, 'h'))
            self.assertTrue(self.runner.should_notify(state, 'h', force=True))
            self.assertTrue(self.runner.should_notify(state, 'new'))

    def run_main(self, summary, initial_state=None, args=None):
        report = {'title': 'Report', 'target_date': '2026-01-01', 'source': {'profile': 'new_site'},
                  'overview': {'sampled': 1, 'analyzed': 1, 'errors': 0}, 'type_counts': {'B': 1},
                  'attribution_summary': [], 'issue_summary': [], 'cases': [{'case_id': 'CASE-001'}]}
        fake_case = types.ModuleType('adapters.wp_tes.runtime.case_analysis')
        fake_case.CASE_ANALYSIS_SUPERSET_DB_NAME = 'iweaver-hermes-ai'
        fake_case.get_source_database_identity = lambda: {'database_name': 'iweaver-hermes-ai'}
        fake_case.run_case_analysis = lambda *a, **k: summary
        fake_report = types.ModuleType('adapters.wp_tes.runtime.case_analysis_report')
        fake_report.build_report = lambda value: report
        fake_feishu = types.ModuleType('adapters.wp_tes.runtime.feishu_docx')
        fake_feishu.FeishuDocxClient = FakeClient
        old_modules = {name: sys.modules.get(name) for name in [fake_case.__name__, fake_report.__name__, fake_feishu.__name__]}
        sys.modules[fake_case.__name__] = fake_case
        sys.modules[fake_report.__name__] = fake_report
        sys.modules[fake_feishu.__name__] = fake_feishu
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / 'state.db'
            lock_path = Path(tmp) / 'lock'
            if initial_state:
                conn = self.runner.connect_state(state_path)
                self.runner.update_state(conn, '2026-01-01', **initial_state)
                conn.close()
            with mock.patch.object(self.runner, 'STATE_PATH', state_path), \
                 mock.patch.object(self.runner, 'LOCK_PATH', lock_path), \
                 mock.patch.object(self.runner, 'connect_state', side_effect=lambda path=state_path: self.runner.sqlite3.connect(str(state_path))):
                try:
                    rc = self.runner.main(args or ['--date', '2026-01-01'])
                    conn = self.runner.sqlite3.connect(str(state_path))
                    try:
                        state = self.runner.get_state(conn, '2026-01-01')
                    except self.runner.sqlite3.OperationalError:
                        state = None
                    conn.close()
                finally:
                    for name, value in old_modules.items():
                        if value is None:
                            sys.modules.pop(name, None)
                        else:
                            sys.modules[name] = value
        return rc, state, FakeClient.instances[-1]

    def test_unchanged_valid_document_skips_render_and_notify(self):
        report = {'title': 'Report', 'target_date': '2026-01-01', 'source': {'profile': 'new_site'},
                  'overview': {'sampled': 1, 'analyzed': 1, 'errors': 0}, 'type_counts': {'B': 1},
                  'attribution_summary': [], 'issue_summary': [], 'cases': [{'case_id': 'CASE-001'}]}
        previous_report_hash = self.runner.canonical_hash(report)
        rc, state, client = self.run_main({'source_errors': [], 'input_hash': 'source-h'}, {
            'document_id': 'doc-1', 'document_url': FakeClient.document_url('doc-1'),
            'source_hash': 'source-h', 'report_hash': previous_report_hash, 'notification_state': 'sent'})
        self.assertEqual(rc, 0)
        self.assertNotIn('render', [call[0] for call in client.calls])
        self.assertNotIn('notify', [call[0] for call in client.calls])
        self.assertEqual(state['notification_state'], 'sent')
        self.assertEqual(state['report_hash'], previous_report_hash)

    def test_layout_change_renders_without_duplicate_notification(self):
        rc, state, client = self.run_main({'source_errors': [], 'input_hash': 'same-source'}, {
            'document_id': 'doc-1', 'document_url': FakeClient.document_url('doc-1'),
            'source_hash': 'same-source', 'report_hash': 'old-layout-hash', 'notification_state': 'sent'})
        self.assertEqual(rc, 0)
        self.assertEqual([call[0] for call in client.calls].count('render'), 1)
        self.assertNotIn('notify', [call[0] for call in client.calls])
        self.assertNotEqual(state['report_hash'], 'old-layout-hash')
        self.assertEqual(state['notification_state'], 'sent')

    def test_changed_report_renders_and_notifies_once(self):
        rc, state, client = self.run_main({'source_errors': [], 'input_hash': 'new-source'}, {
            'document_id': 'doc-1', 'document_url': FakeClient.document_url('doc-1'),
            'source_hash': 'old-source', 'report_hash': 'old', 'notification_state': 'sent'})
        self.assertEqual(rc, 0)
        self.assertEqual([call[0] for call in client.calls].count('render'), 1)
        self.assertEqual([call[0] for call in client.calls].count('notify'), 1)
        self.assertEqual(state['notification_state'], 'sent')
        self.assertEqual(state['notification_message_id'], 'msg-1')

    def test_source_errors_stop_before_publish(self):
        rc, state, client = self.run_main({'source_errors': [{'source': 'type_a', 'error': 'db failed'}]})
        self.assertEqual(rc, self.runner.EXIT_DATA)
        self.assertIsNone(state)
        self.assertEqual(client.calls, [])

    def test_dry_run_help_and_db_identity_path(self):
        help_text = self.runner.parse_args.__doc__ or ''
        with self.assertRaises(SystemExit):
            with mock.patch('sys.stdout'):
                self.runner.parse_args(['--help'])
        source = (ROOT / 'scripts/run_case_analysis_daily.py').read_text()
        self.assertIn('Run database validation and the existing LLM analysis path, but do not publish or notify', source)
        self.assertLess(source.index('get_source_database_identity()'), source.index('if args.dry_run:'))
        self.assertEqual(help_text, '')


class SystemdTests(unittest.TestCase):
    def test_service_paths_and_timer(self):
        service = (ROOT / 'systemd/iweaver-case-analysis-daily.service').read_text()
        timer = (ROOT / 'systemd/iweaver-case-analysis-daily.timer').read_text()
        base = '/srv/cloudcli-workspaces/default/agentos_mcp_orchestrator_transfer'
        self.assertIn(f'WorkingDirectory={base}', service)
        self.assertIn(f'ExecStart={base}/.venv/bin/python {base}/scripts/run_case_analysis_daily.py', service)
        self.assertNotIn('/home/cloudcli/iweaver', service)
        self.assertIn('OnCalendar=*-*-* 09:13:00 Asia/Shanghai', timer)


if __name__ == '__main__':
    unittest.main()
