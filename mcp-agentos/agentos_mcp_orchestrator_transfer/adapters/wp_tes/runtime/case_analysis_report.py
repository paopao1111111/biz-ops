import hashlib
import re
from collections import Counter


_EMAIL_RE = re.compile(r'^([^@]+)@(.+)$')

TYPE_DEFINITIONS = {
    'A': '当日首次付费用户',
    'B': '当日首次出现且仅一轮对话',
    'C': '未付费、3–4 轮对话',
    'D': '未付费、5 轮及以上对话',
    'E': '当日点踩案例',
    'F': '反馈、点赞或点踩触发的补充案例',
}

NO_OUTPUT_TERMS = (
    '空回复', '回复为空', '返回为空', '返回空', '空内容', '无回复', '未回复',
    '无输出', '未输出', '无响应', '未响应', '没有回复', '未生成任何',
    '没有生成', '全程无任何回复', '始终未生成', '始终返回空',
)

PRIORITY_META = {
    'P0': {'label': 'P0', 'title': '严重故障', 'direction': '检查模型调用、Agent 链路和异常返回处理'},
    'P1': {'label': 'P1', 'title': '质量问题', 'direction': '检查生成质量、上下文与任务完成度'},
    'P2': {'label': 'P2', 'title': '体验优化', 'direction': '优化交互引导和主动追问'},
}



def mask_user_id(value, max_length=32):
    value = str(value or '')
    match = _EMAIL_RE.match(value)
    if match:
        local, domain = match.groups()
        return f'{local[:2]}***@{domain[:24]}'
    if len(value) > 16:
        return f'{value[:8]}…{value[-4:]}'
    return value[:max_length]



def _text(value):
    if isinstance(value, dict):
        return ' - '.join(str(v) for v in value.values() if v not in (None, ''))
    if isinstance(value, list):
        return ', '.join(str(v) for v in value)
    return str(value or '')



def _error_category(value):
    text = str(value or '').lower()
    if 'failed to parse analysis' in text:
        return 'LLM输出解析失败'
    if 'timeout' in text or 'timed out' in text:
        return 'LLM请求超时'
    if 'no conversation content' in text:
        return '无有效对话内容'
    if 'cliproxy' in text:
        return 'CLIProxy调用失败'
    if 'agentos' in text:
        return 'AgentOS兜底失败'
    return '其他分析错误'



def _source_key(case):
    topic_ids = [str(convo.get('topic_id') or '') for convo in case.get('conversations') or []]
    parts = [
        str(case.get('case_type') or ''),
        str(case.get('user_id') or ''),
        ','.join(topic_ids),
        str(case.get('topic_id') or ''),
        str(case.get('message_id') or ''),
        str(case.get('chat_log_id') or ''),
        str(case.get('trigger_id') or ''),
    ]
    digest = hashlib.sha256('\x1f'.join(parts).encode('utf-8')).hexdigest()[:12].upper()
    return f'SRC-{digest}'



def _first_user_record_id(case):
    explicit = str(case.get('chat_log_id') or '')
    if explicit:
        return explicit
    for conversation in case.get('conversations') or []:
        for message in conversation.get('messages') or []:
            if message.get('role') == 'user' and message.get('record_id'):
                return str(message['record_id'])
    return ''



def _topic_ids(case):
    values = []
    primary = str(case.get('topic_id') or '')
    if primary and primary != '0':
        values.append(primary)
    for conversation in case.get('conversations') or []:
        topic_id = str(conversation.get('topic_id') or '')
        if topic_id and topic_id != '0' and topic_id not in values:
            values.append(topic_id)
    return values



def _score_value(value):
    if isinstance(value, dict):
        for key in ('score', 'value', 'satisfaction_score'):
            if key in value:
                return _score_value(value.get(key))
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None



def _source_has_no_output(case):
    user_messages = 0
    assistant_messages = []
    for conversation in case.get('conversations') or []:
        for message in conversation.get('messages') or []:
            if message.get('role') == 'user':
                user_messages += 1
            elif message.get('role') == 'assistant':
                assistant_messages.append(str(message.get('content') or '').strip())
    if not user_messages:
        return False
    if not assistant_messages:
        return True
    return all(
        not content or content == '[empty]' or '回复为空]' in content or content == '[助手回复为空]'
        for content in assistant_messages
    )



def _is_no_output(analysis):
    fields = (
        analysis.get('attribution'), analysis.get('analysis_point'), analysis.get('conclusion'),
        analysis.get('conversation_summary_zh'), analysis.get('satisfaction_reason'),
        analysis.get('feedback_reason_summary'),
    )
    combined = ' '.join(_text(value) for value in fields)
    return any(term in combined for term in NO_OUTPUT_TERMS)



def _root_category(value):
    text = _text(value).strip()
    for category in ('模型问题', '产品问题', '引导问题', '商业化问题', '用户表达问题', '无'):
        if text == category or text.startswith(f'{category} -') or text.startswith(f'{category}：'):
            return category
    return text or '无'



def _priority(root, score, no_output, has_dislike):
    if no_output:
        return 'P0', '空回复/无输出'
    if score is not None and score <= 3:
        return 'P1', '内容质量/任务完成度'
    if has_dislike or root in ('模型问题', '产品问题'):
        return 'P1', root or '质量问题'
    if root == '引导问题':
        return 'P2', '交互引导优化'
    return 'P2', root if root not in ('', '无') else '一般体验观察'



def _percentage(count, total):
    return f'{count / total * 100:.0f}%' if total else '0%'



def build_report(summary):
    results = summary.get('results') or []
    type_counts = Counter()
    attribution_counts = Counter()
    issue_counts = Counter()
    confidence_counts = Counter()
    error_counts = Counter(
        _error_category(item.get('error'))
        for item in (summary.get('errors') or [])
    )
    rows = []
    for item in results:
        case = item.get('case_data') or {}
        analysis = item.get('analysis') or {}
        case_type = str(case.get('case_type') or '')
        type_counts[case_type] += 1
        attribution = _text(analysis.get('attribution')) or '未分类'
        attribution_counts[attribution] += 1
        root = _root_category(analysis.get('feedback_root_cause'))
        issue_counts[root] += 1
        confidence = _text(analysis.get('confidence_level')) or '未记录'
        confidence_counts[confidence] += 1
        intent = analysis.get('intent_label') or {}
        intent_text = ' / '.join(str(intent.get(k) or '') for k in ('level1', 'level2', 'level3')).strip(' /') if isinstance(intent, dict) else _text(intent)
        feedback = case.get('feedback') or []
        conversations = case.get('conversations') or []
        agent = next((str(convo.get('agent_type') or '') for convo in conversations if convo.get('agent_type')), '')
        dislike_tags = analysis.get('dislike_reason_tags') or []
        has_like = any(x.get('type') == 1 for x in case.get('attitude') or []) or any(x.get('type') == 'thumbs_up' for x in feedback)
        has_dislike = any(x.get('type') == 2 for x in case.get('attitude') or []) or any(x.get('type') == 'thumbs_down' for x in feedback)
        raw_score = analysis.get('satisfaction_score')
        score = _score_value(raw_score)
        score_text = str(score) if score is not None else _text(raw_score or '无法判断')[:16]
        no_output = _is_no_output(analysis) or (
            _source_has_no_output(case) and not has_like and (score is None or score <= 1)
        )
        priority, issue_label = _priority(root, score, no_output, has_dislike)
        rows.append({
            'case_id': '',
            'source_key': _source_key(case),
            'case_type': case_type,
            'case_type_name': TYPE_DEFINITIONS.get(case_type, '其他样本'),
            'user_id': mask_user_id(case.get('user_id')),
            'topic_ids': _topic_ids(case),
            'message_id': str(case.get('message_id') or ''),
            'chat_log_id': _first_user_record_id(case),
            'trigger_id': str(case.get('trigger_id') or ''),
            'user_version': str(case.get('user_version') or '未知')[:24],
            'agent': (agent or '通用对话/未识别')[:80],
            'conversation_summary_zh': _text(analysis.get('conversation_summary_zh'))[:500],
            'intent': intent_text[:160],
            'attribution': attribution[:160],
            'analysis_point': _text(analysis.get('analysis_point'))[:320],
            'conclusion': _text(analysis.get('conclusion'))[:240],
            'satisfaction_score': score_text,
            'satisfaction_reason': _text(analysis.get('satisfaction_reason'))[:320],
            'confidence': confidence[:16],
            'has_like': has_like,
            'has_dislike': has_dislike,
            'feedback_text': '; '.join(str(x.get('feedback_content') or '') for x in feedback if x.get('feedback_content'))[:320],
            'dislike_reason_tags': ', '.join(str(x) for x in dislike_tags)[:200],
            'feedback_root_cause': root[:80],
            'feedback_reason_summary': _text(analysis.get('feedback_reason_summary'))[:320],
            'no_output': no_output,
            'priority': priority,
            'priority_label': PRIORITY_META[priority]['label'],
            'issue_label': issue_label,
        })

    priority_order = {'P0': 0, 'P1': 1, 'P2': 2}
    rows.sort(key=lambda row: (
        priority_order.get(row['priority'], 9),
        row['issue_label'], row['case_type'], row['source_key'],
    ))
    for index, row in enumerate(rows, 1):
        row['case_id'] = f'CASE-{index:03d}'

    priority_groups = {}
    for row in rows:
        key = (row['priority'], row['issue_label'])
        group = priority_groups.setdefault(key, {'count': 0, 'case_ids': [], 'agents': []})
        group['count'] += 1
        if len(group['case_ids']) < 4:
            group['case_ids'].append(row['case_id'])
        if row['agent'] not in group['agents'] and len(group['agents']) < 3:
            group['agents'].append(row['agent'])
    priority_summary = []
    for (priority, issue_label), group in sorted(
            priority_groups.items(), key=lambda item: (priority_order.get(item[0][0], 9), -item[1]['count'], item[0][1])):
        priority_summary.append({
            'priority': priority,
            'priority_label': PRIORITY_META[priority]['label'],
            'issue': issue_label,
            'count': group['count'],
            'case_ids': group['case_ids'],
            'agents': group['agents'],
            'direction': PRIORITY_META[priority]['direction'],
        })

    target_date = str(summary.get('target_date') or '')
    actual_type_counts = dict(summary.get('by_type') or type_counts)
    type_breakdown = [
        {'type': case_type, 'name': TYPE_DEFINITIONS[case_type], 'count': int(actual_type_counts.get(case_type) or 0)}
        for case_type in TYPE_DEFINITIONS
    ]
    analyzed = len(results)
    no_output_count = sum(1 for row in rows if row['no_output'])
    judged_count = sum(1 for row in rows if _score_value(row['satisfaction_score']) is not None)
    return {
        'layout_version': '3.1',
        'title': f'iWeaver 每日案例分析｜{target_date}',
        'target_date': target_date,
        'source': dict(summary.get('source') or {}),
        'input_hash': str(summary.get('input_hash') or ''),
        'overview': {
            'sampled': int(summary.get('total_sampled') or 0),
            'analyzed': analyzed,
            'errors': len(summary.get('errors') or []),
            'no_output': no_output_count,
            'no_output_rate': _percentage(no_output_count, analyzed),
            'high_confidence': int(confidence_counts.get('高') or 0),
            'judged_satisfaction': judged_count,
            'unjudged_satisfaction': analyzed - judged_count,
        },
        'executive_summary': {
            'no_output_count': no_output_count,
            'no_output_rate': _percentage(no_output_count, analyzed),
            'model_issues': int(issue_counts.get('模型问题') or 0),
            'product_issues': int(issue_counts.get('产品问题') or 0),
            'guidance_issues': int(issue_counts.get('引导问题') or 0),
            'high_confidence': int(confidence_counts.get('高') or 0),
        },
        'type_counts': dict(sorted(actual_type_counts.items())),
        'type_breakdown': type_breakdown,
        'attribution_summary': attribution_counts.most_common(10),
        'issue_summary': issue_counts.most_common(10),
        'confidence_summary': confidence_counts.most_common(10),
        'priority_summary': priority_summary,
        'error_summary': error_counts.most_common(10),
        'provider_counts': dict(sorted((summary.get('provider_counts') or {}).items())),
        'cases': rows,
    }
