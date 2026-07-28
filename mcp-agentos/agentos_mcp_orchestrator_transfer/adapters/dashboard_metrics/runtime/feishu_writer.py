import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests


TOKEN_URL = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
WIKI_NODE_URL = 'https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node'
SHEETS_BASE_URL = 'https://open.feishu.cn/open-apis/sheets/v2/spreadsheets'
SHEETS_META_URL = 'https://open.feishu.cn/open-apis/sheets/v3/spreadsheets'
PROJECT_DIR = Path(__file__).resolve().parents[3]
EXCEL_EPOCH = date(1899, 12, 30)

DEFAULT_WIKI_TOKEN = 'PtgSwYoHsi6RalkB0IycL1Qinph'
DEFAULT_SPREADSHEET_TOKEN = 'WiTSsiBx0hfm8TtV0lNcW2Kgnwd'
DEFAULT_SHEET_ID = '5211d0'
DEFAULT_END_COL = 'BE'
DEFAULT_MAX_ROWS = 300

METRIC_TO_HEADER = {
    'gsc_impressions': '官网曝光量',
    'gsc_clicks': '官网点击量',
    'ga4_new_uv': '官网新UV',
    'registration_users': '注册用户数',
    'registration_rate': '注册率',
    'first_day_activation_users': '首日激活用户数',
    'activation_rate': '激活率',
    'new_uv_activation_rate': '新UV激活率',
    'd1_retention_users': '次留',
    'd1_retention_rate': '次留率',
    'd7_retention_users': '周留',
    'd7_retention_rate': '周留率',
    'dau': '日活',
    'paid_users': '当日付费用户',
    'paid_orders': '付费订单',
    'new_orders': '新订单',
    'renewal_orders': '续费订单',
    'payment_amount': '付费金额',
}


def sync_dashboard_metrics(metrics, config=None, dry_run=False):
    config = dict(config or {})
    load_project_env()
    token = get_tenant_access_token()
    if not token:
        return {'success': False, 'error': 'Feishu credentials are not configured'}

    spreadsheet_token = first_value(
        config.get('spreadsheet_token'),
        os.getenv('DASHBOARD_FEISHU_SPREADSHEET_TOKEN'),
        os.getenv('FEISHU_DASHBOARD_SPREADSHEET_TOKEN'),
        DEFAULT_SPREADSHEET_TOKEN,
    )
    wiki_token = first_value(
        config.get('wiki_token'),
        os.getenv('DASHBOARD_FEISHU_WIKI_TOKEN'),
        DEFAULT_WIKI_TOKEN,
    )
    if not spreadsheet_token and wiki_token:
        spreadsheet_token = resolve_wiki_sheet_token(token, wiki_token)
    if not spreadsheet_token:
        return {'success': False, 'error': 'spreadsheet_token is required'}

    sheet_id = first_value(
        config.get('sheet_id'),
        os.getenv('DASHBOARD_FEISHU_SHEET_ID'),
        DEFAULT_SHEET_ID,
    )
    if not sheet_id:
        sheet_id = first_sheet_id(token, spreadsheet_token)
    if not sheet_id:
        return {'success': False, 'error': 'sheet_id is required'}

    end_col = str(config.get('end_col') or DEFAULT_END_COL)
    max_rows = int(config.get('max_rows') or DEFAULT_MAX_ROWS)
    layout = read_sheet_layout(token, spreadsheet_token, sheet_id, end_col=end_col, max_rows=max_rows)
    updates, skipped = build_updates(metrics, layout)

    result = {
        'success': True,
        'dry_run': bool(dry_run),
        'spreadsheet_token': spreadsheet_token,
        'sheet_id': sheet_id,
        'sheet_range': f'{sheet_id}!A1:{end_col}{max_rows}',
        'updates_planned': len(updates),
        'skipped': skipped,
        'preview': updates[:20],
    }
    if dry_run or not updates:
        return result

    write_result = write_cell_updates(token, spreadsheet_token, sheet_id, updates)
    result.update(write_result)
    result['success'] = bool(write_result.get('success'))
    return result


def load_project_env():
    env_path = PROJECT_DIR / '.env'
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_tenant_access_token():
    app_id = first_value(os.getenv('FEISHU_APP_ID'), os.getenv('FEISHU_BOT_APP_ID'))
    app_secret = first_value(os.getenv('FEISHU_APP_SECRET'), os.getenv('FEISHU_BOT_APP_SECRET'))
    if not app_id or not app_secret:
        return ''
    session = no_proxy_session()
    resp = session.post(TOKEN_URL, json={'app_id': app_id, 'app_secret': app_secret}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu auth failed: {data.get('msg') or data}")
    return data['tenant_access_token']


def resolve_wiki_sheet_token(token, wiki_token):
    session = no_proxy_session()
    resp = session.get(
        WIKI_NODE_URL,
        params={'token': wiki_token},
        headers={'Authorization': f'Bearer {token}'},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu wiki resolve failed: {data.get('msg') or data}")
    node = data.get('data', {}).get('node', {})
    return node.get('obj_token') or ''


def first_sheet_id(token, spreadsheet_token):
    session = no_proxy_session()
    resp = session.get(
        f'{SHEETS_META_URL}/{spreadsheet_token}/sheets/query',
        headers={'Authorization': f'Bearer {token}'},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu sheet query failed: {data.get('msg') or data}")
    sheets = data.get('data', {}).get('sheets') or []
    return sheets[0].get('sheet_id') if sheets else ''


def read_sheet_layout(token, spreadsheet_token, sheet_id, end_col=DEFAULT_END_COL, max_rows=DEFAULT_MAX_ROWS):
    range_name = f'{sheet_id}!A1:{end_col}{max_rows}'
    values = get_values(token, spreadsheet_token, range_name)
    headers = values[0] if values else []
    header_columns = {}
    for index, header in enumerate(headers, 1):
        name = str(header or '').strip()
        if name and name not in header_columns:
            header_columns[name] = index

    # Find date column index by header name
    date_col_idx = None
    for idx, h in enumerate(headers):
        if h and str(h).strip() in ('日期', 'date'):
            date_col_idx = idx
            break
    if date_col_idx is None:
        date_col_idx = 0  # fallback to first column

    date_rows = {}
    for row_number, row in enumerate(values[1:], 2):
        cell = row[date_col_idx] if row and date_col_idx < len(row) else ''
        key = date_serial_key(cell)
        if key and key not in date_rows:
            date_rows[key] = row_number

    return {'headers': headers, 'header_columns': header_columns, 'date_rows': date_rows}


def get_values(token, spreadsheet_token, range_name):
    session = no_proxy_session()
    resp = session.get(
        f'{SHEETS_BASE_URL}/{spreadsheet_token}/values/{range_name}',
        headers={'Authorization': f'Bearer {token}'},
        params={'valueRenderOption': 'UnformattedValue'},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get('code') != 0:
        raise RuntimeError(f"Feishu read values failed: {data.get('msg') or data}")
    return data.get('data', {}).get('valueRange', {}).get('values') or []


def build_updates(metrics, layout):
    grouped = group_metrics(metrics)
    add_computed_rates(grouped)
    header_columns = layout['header_columns']
    date_rows = layout['date_rows']
    updates = []
    skipped = {
        'missing_dates': [],
        'missing_columns': [],
        'unmapped_metrics': [],
        'invalid_metrics': 0,
    }
    missing_dates = set()
    missing_columns = set()
    unmapped_metrics = set()

    for metric_date in sorted(grouped):
        row_number = date_rows.get(date_serial_key(metric_date))
        if not row_number:
            missing_dates.add(metric_date)
            continue
        for metric_name, value in grouped[metric_date].items():
            header = METRIC_TO_HEADER.get(metric_name)
            if not header:
                unmapped_metrics.add(metric_name)
                continue
            column_number = header_columns.get(header)
            if not column_number:
                missing_columns.add(header)
                continue
            updates.append({
                'date': metric_date,
                'metric_name': metric_name,
                'header': header,
                'row': row_number,
                'column': column_number,
                'cell': f'{column_letter(column_number)}{row_number}',
                'value': normalise_value(value),
            })

    skipped['missing_dates'] = sorted(missing_dates)
    skipped['missing_columns'] = sorted(missing_columns)
    skipped['unmapped_metrics'] = sorted(unmapped_metrics)
    skipped['invalid_metrics'] = sum(1 for item in metrics or [] if not isinstance(item, dict))
    return updates, skipped


def group_metrics(metrics):
    grouped = defaultdict(dict)
    for item in metrics or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get('status') or 'ok').lower()
        if status and status != 'ok':
            continue
        metric_date = str(item.get('metric_date') or item.get('date') or '').strip()
        metric_name = str(item.get('metric_name') or item.get('name') or '').strip()
        if not metric_date or not metric_name:
            continue
        if 'metric_value' in item:
            value = item.get('metric_value')
        else:
            value = item.get('value')
        if value is None or value == '':
            continue
        grouped[metric_date][metric_name] = value
    return grouped


def add_computed_rates(grouped):
    for values in grouped.values():
        registrations = to_float(values.get('registration_users'))
        activations = to_float(values.get('first_day_activation_users'))
        new_uv = to_float(values.get('ga4_new_uv'))
        paid_orders = to_float(values.get('paid_orders'))
        renewal_orders = to_float(values.get('renewal_orders'))
        if activations is not None and registrations and registrations > 0 and 'activation_rate' not in values:
            values['activation_rate'] = round(activations / registrations, 6)
        if activations is not None and new_uv and new_uv > 0 and 'new_uv_activation_rate' not in values:
            values['new_uv_activation_rate'] = round(activations / new_uv, 6)
        if paid_orders is not None and renewal_orders is not None and 'new_orders' not in values:
            values['new_orders'] = max(paid_orders - renewal_orders, 0)


def write_cell_updates(token, spreadsheet_token, sheet_id, updates):
    row_groups = {}
    for item in updates:
        row_groups.setdefault(item['row'], []).append(item)
    value_ranges = []
    for row, items in row_groups.items():
        items_sorted = sorted(items, key=lambda x: x['column'])
        segments = []
        seg_start = 0
        while seg_start < len(items_sorted):
            seg_end = seg_start + 1
            while seg_end < len(items_sorted) and items_sorted[seg_end]['column'] == items_sorted[seg_end - 1]['column'] + 1:
                seg_end += 1
            segment = items_sorted[seg_start:seg_end]
            first_col = segment[0]['column']
            last_col = segment[-1]['column']
            values = [item['value'] for item in segment]
            value_ranges.append({
                'range': f"{sheet_id}!{column_letter(first_col)}{row}:{column_letter(last_col)}{row}",
                'values': [values],
            })
            seg_start = seg_end
    session = no_proxy_session()
    resp = session.put(
        f'{SHEETS_BASE_URL}/{spreadsheet_token}/values_batch_update',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        json={'valueRanges': value_ranges},
        timeout=30,
    )
    try:
        data = resp.json()
    except Exception:
        data = {'code': -1, 'msg': resp.text[:500]}
    if resp.status_code == 404 or data.get('code') != 0:
        return write_row_ranges_one_by_one(token, spreadsheet_token, value_ranges)
    return {'success': True, 'updates_written': len(updates), 'write_result': data}


def write_row_ranges_one_by_one(token, spreadsheet_token, value_ranges):
    session = no_proxy_session()
    written = 0
    failures = []
    for item in value_ranges:
        resp = session.put(
            f'{SHEETS_BASE_URL}/{spreadsheet_token}/values',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'valueRange': item},
            timeout=20,
        )
        try:
            data = resp.json()
        except Exception:
            data = {'code': -1, 'msg': resp.text[:500]}
        if data.get('code') == 0:
            written += 1
        else:
            failures.append({'range': item.get('range'), 'result': data})
    return {
        'success': not failures,
        'updates_written': written,
        'failures': failures[:10],
        'failure_count': len(failures),
    }


def date_serial_key(value):
    if isinstance(value, datetime):
        return str((value.date() - EXCEL_EPOCH).days)
    if isinstance(value, date):
        return str((value - EXCEL_EPOCH).days)
    if isinstance(value, (int, float)):
        return str(int(round(float(value))))
    text = str(value or '').strip()
    if not text:
        return ''
    try:
        return str(int(round(float(text))))
    except ValueError:
        pass
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d'):
        try:
            parsed = datetime.strptime(text[:10], fmt).date()
            return str((parsed - EXCEL_EPOCH).days)
        except ValueError:
            continue
    return ''


def normalise_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() else value
    text = str(value).strip()
    try:
        number = float(text)
        return int(number) if number.is_integer() else number
    except ValueError:
        return text


def to_float(value):
    try:
        if value is None or value == '':
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def column_letter(number):
    letters = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def first_value(*values):
    for value in values:
        if value not in (None, ''):
            return str(value).strip()
    return ''


def no_proxy_session():
    session = requests.Session()
    session.trust_env = False
    return session


sync_to_feishu = sync_dashboard_metrics
