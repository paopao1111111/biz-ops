#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / 'storage'
STATE_PATH = STORAGE / 'case_analysis_daily.db'
LOCK_PATH = STORAGE / 'case_analysis_daily.lock'

EXIT_CONFIG = 2
EXIT_DATA = 3
EXIT_ANALYSIS = 4
EXIT_DOC = 5
EXIT_NOTIFY = 6
EXIT_LOCKED = 7


def load_dotenv(path):
    if not path.exists():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def configure_environment():
    load_dotenv(ROOT / '.env')
    os.environ['CASE_ANALYSIS_SOURCE_PROFILE'] = 'new_site'
    os.environ.setdefault('CASE_ANALYSIS_SUPERSET_DB_ID', '2')


def connect_state(path=STATE_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute('''CREATE TABLE IF NOT EXISTS daily_runs (
        target_date TEXT PRIMARY KEY, document_id TEXT, document_url TEXT,
        source_hash TEXT, report_hash TEXT, phase_status TEXT,
        notification_state TEXT, notification_message_id TEXT, error TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
    conn.commit()
    return conn


def get_state(conn, target_date):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM daily_runs WHERE target_date=?', (target_date,)).fetchone()
    return dict(row) if row else None


def update_state(conn, target_date, **values):
    now = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    existing = get_state(conn, target_date)
    payload = dict(existing or {})
    payload.update(values)
    payload.setdefault('created_at', now)
    payload['updated_at'] = now
    columns = ['target_date', 'document_id', 'document_url', 'source_hash', 'report_hash', 'phase_status',
               'notification_state', 'notification_message_id', 'error', 'created_at', 'updated_at']
    payload['target_date'] = target_date
    conn.execute(f"INSERT OR REPLACE INTO daily_runs ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 [payload.get(column) for column in columns])
    conn.commit()
    return get_state(conn, target_date)


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def should_notify(state, source_hash, force=False):
    if force:
        return True
    return not state or state.get('source_hash') != source_hash or state.get('notification_state') != 'sent'


def acquire_lock(path=LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+')
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def probe_feishu(client):
    doc = client.create_document('[PROBE] Daily Case Analysis')
    blocks = client.list_blocks(doc['document_id'])
    root = next((b for b in blocks if b.get('block_type') == 1), blocks[0])
    client.append_blocks(doc['document_id'], root['block_id'], [client.text_block('Offline-compatible Feishu capability probe')])
    table = client.create_table(doc['document_id'], root['block_id'], 2, 2)
    client.fill_table_cells(doc['document_id'], table, [['capability', 'result'], ['docx', 'ok']])
    client.set_tenant_readable(doc['document_id'])
    verified = client.verify_document(doc['document_id'])
    return {'document_id': doc['document_id'], 'url': doc['url'], 'verified': verified,
            'cleanup': 'not required; clear_document supported, document deletion optional/unavailable'}


def compact_summary(report):
    return {
        'title': report.get('title'),
        'overview': report.get('overview'),
        'type_counts': report.get('type_counts'),
        'executive_summary': report.get('executive_summary'),
        'provider_counts': report.get('provider_counts'),
        'error_summary': report.get('error_summary'),
        'source': report.get('source'),
        'source_hash': str(report.get('input_hash') or '')[:12],
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--date')
    parser.add_argument('--dry-run', action='store_true', help='Run database validation and the existing LLM analysis path, but do not publish or notify')
    parser.add_argument('--probe-feishu', action='store_true')
    parser.add_argument('--no-notify', action='store_true')
    parser.add_argument('--force-notify', action='store_true')
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_environment()
    sys.path.insert(0, str(ROOT))
    try:
        lock = acquire_lock()
    except BlockingIOError:
        print('Daily case analysis is already running', file=sys.stderr)
        return EXIT_LOCKED
    try:
        if not args.probe_feishu:
            target_date = args.date or (datetime.now(ZoneInfo('Asia/Shanghai')) - timedelta(days=1)).strftime('%Y-%m-%d')
            try:
                datetime.strptime(target_date, '%Y-%m-%d')
            except ValueError:
                print('Invalid --date; expected YYYY-MM-DD', file=sys.stderr)
                return EXIT_CONFIG
        from adapters.wp_tes.runtime.feishu_docx import FeishuDocxClient
        client = FeishuDocxClient()
        if args.probe_feishu:
            print(json.dumps(probe_feishu(client), ensure_ascii=False))
            return 0
        from adapters.wp_tes.runtime.case_analysis import (CASE_ANALYSIS_SUPERSET_DB_NAME,
            get_source_database_identity, run_case_analysis)
        from adapters.wp_tes.runtime.case_analysis_report import build_report
        try:
            identity = get_source_database_identity()
        except Exception as exc:
            print(f'Database identity check failed: {exc}', file=sys.stderr)
            return EXIT_DATA
        if identity.get('database_name') != CASE_ANALYSIS_SUPERSET_DB_NAME:
            print('Database identity does not match configured DB2 name', file=sys.stderr)
            return EXIT_CONFIG
        try:
            summary = run_case_analysis(target_date, sheet_output=False, fail_on_source_errors=True)
        except Exception as exc:
            print(f'Analysis failed: {exc}', file=sys.stderr)
            return EXIT_ANALYSIS
        report = build_report(summary)
        if summary.get('source_errors'):
            print(f"Source query failed: {json.dumps(summary['source_errors'], ensure_ascii=False)[:1000]}", file=sys.stderr)
            return EXIT_DATA
        if args.dry_run:
            print(json.dumps(compact_summary(report), ensure_ascii=False, sort_keys=True))
            return 0
        conn = connect_state()
        source_hash = str(summary.get('input_hash') or canonical_hash(report.get('source') or {}))
        report_hash = canonical_hash(report)
        previous_state = get_state(conn, target_date)
        notify = not args.no_notify and should_notify(previous_state, source_hash, args.force_notify)
        state = previous_state
        active_report_hash = report_hash
        try:
            if state and state.get('document_id'):
                document_id = state['document_id']
                document_url = state.get('document_url') or client.document_url(document_id)
            else:
                doc = client.create_document(report['title'])
                document_id, document_url = doc['document_id'], doc['url']
                state = update_state(conn, target_date, document_id=document_id, document_url=document_url,
                                     source_hash=source_hash, phase_status='document_created')
            unchanged = bool(
                previous_state
                and previous_state.get('source_hash') == source_hash
                and previous_state.get('report_hash') == report_hash
            )
            if unchanged:
                active_report_hash = previous_state['report_hash']
                verification = client.verify_document(
                    document_id,
                    active_report_hash,
                    len(report.get('cases') or []),
                )
            else:
                verification = None
            if not verification or not verification.get('readable'):
                active_report_hash = report_hash
                client.set_tenant_readable(document_id)
                rendered = client.render_report(document_id, report, active_report_hash)
                verification = rendered.get('verification') or client.verify_document(
                    document_id, active_report_hash, len(report.get('cases') or []))
            if not verification or not verification.get('readable'):
                raise RuntimeError('document marker/case-count/public-permission verification failed')
            state = update_state(conn, target_date, document_id=document_id, document_url=document_url,
                                 source_hash=source_hash, report_hash=active_report_hash,
                                 phase_status='document_verified', error=None)
        except Exception as exc:
            update_state(conn, target_date, phase_status='document_failed', error=str(exc)[:500])
            print(f'Document operation failed: {exc}', file=sys.stderr)
            return EXIT_DOC
        if notify:
            try:
                executive = report.get('executive_summary') or {}
                message_id = client.send_document_card(
                    report['title'],
                    document_url,
                    f"分析 {report['overview']['analyzed']} 条，失败 {report['overview']['errors']} 条；"
                    f"空回复类 {executive.get('no_output_count', 0)} 条（{executive.get('no_output_rate', '0%')}），"
                    f"模型问题 {executive.get('model_issues', 0)} 条，产品问题 {executive.get('product_issues', 0)} 条",
                )
                state = update_state(conn, target_date, notification_state='sent',
                                     notification_message_id=message_id, phase_status='complete', error=None)
            except Exception as exc:
                update_state(conn, target_date, notification_state='failed', phase_status='notify_failed', error=str(exc)[:500])
                print(f'Notification failed: {exc}', file=sys.stderr)
                return EXIT_NOTIFY
        else:
            update_state(conn, target_date, phase_status='complete',
                         notification_state=state.get('notification_state') if state else None)
        print(json.dumps({'target_date': target_date, 'document_url': document_url,
                          'overview': report['overview'], 'source_hash': source_hash[:12],
                          'report_hash': active_report_hash[:12]}, ensure_ascii=False))
        return 0
    finally:
        lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
