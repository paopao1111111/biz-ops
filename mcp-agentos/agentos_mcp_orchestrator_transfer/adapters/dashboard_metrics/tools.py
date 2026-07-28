import os
from datetime import date
from pathlib import Path


ADAPTER_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ADAPTER_DIR.parents[1]


def register(registry):
    registry.tool('dashboard_metrics_schema_probe', run_schema_probe, 'Probe Superset schema for dashboard metrics')
    registry.tool('dashboard_metrics_preview', run_preview, 'Fetch dashboard metrics without writing Feishu')
    registry.tool('dashboard_metrics_daily_update', run_daily_update, 'Fetch, persist, and sync dashboard metrics')
    registry.tool('dashboard_metrics_alert_check', run_alert_check, 'Check 6-hour dashboard metric alerts')
    registry.tool('feishu_sheet_sync', run_feishu_sheet_sync, 'Sync dashboard metrics to Feishu spreadsheet')

    registry.workflow('dashboard_metrics_schema_probe', 'dashboard_metrics_schema_probe', 'Probe dashboard metric schema', mode='async')
    registry.workflow('dashboard_metrics_preview', 'dashboard_metrics_preview', 'Preview dashboard metrics', mode='async')
    registry.workflow('dashboard_metrics_daily_update', 'dashboard_metrics_daily_update', 'Persist dashboard metrics', mode='async')
    registry.workflow('dashboard_metrics_alert_check', 'dashboard_metrics_alert_check', 'Check dashboard alerts', mode='async')
    registry.workflow('feishu_sheet_sync', 'feishu_sheet_sync', 'Sync dashboard metrics to Feishu sheet', mode='async')


def run_schema_probe(ctx, payload):
    _configure_runtime_env(ctx)
    from .runtime.schema_probe import probe_schema
    result = probe_schema(limit=int(payload.get('limit') or 200))
    return result


def run_preview(ctx, payload):
    _configure_runtime_env(ctx)
    metrics, errors = _collect_metrics(payload)
    return {
        'success': not errors,
        'dry_run': True,
        'metrics': metrics,
        'errors': errors,
        'count': len(metrics),
        'frequencies': _frequencies(),
    }


def run_daily_update(ctx, payload):
    _configure_runtime_env(ctx)
    dry_run = _bool(payload.get('dry_run'), False)
    sync_feishu = _bool(payload.get('sync_feishu'), True)
    metrics, errors = _collect_daily_sheet_metrics(payload) if _use_daily_sheet_scope(payload) else _collect_metrics(payload)
    db_path = _storage_path(ctx)
    rows_written = 0
    if not dry_run:
        from .runtime.storage import MetricStore
        rows_written = MetricStore(db_path).upsert_metrics(metrics)

    feishu = {'enabled': False, 'reason': 'sync_feishu=false'}
    if sync_feishu:
        from .runtime.feishu_writer import sync_dashboard_metrics
        feishu = sync_dashboard_metrics(
            metrics,
            config=_feishu_sheet_config(ctx, payload),
            dry_run=dry_run or _bool(payload.get('feishu_dry_run'), False),
        )

    return {
        'success': not errors and (not sync_feishu or feishu.get('success', False)),
        'dry_run': dry_run,
        'db_path': str(db_path),
        'rows_written': rows_written,
        'metrics': metrics,
        'errors': errors,
        'count': len(metrics),
        'feishu': feishu,
        'frequencies': _frequencies(),
    }


def run_alert_check(ctx, payload):
    _configure_runtime_env(ctx)
    dry_run = _bool(payload.get('dry_run'), False)
    notify = _bool(payload.get('notify'), True)
    from .runtime.alerting import check_alerts
    alerts = check_alerts(now=payload.get('now'), window_hours=int(payload.get('window_hours') or 6))
    triggered = [item for item in alerts if item.get('triggered')]
    db_path = _storage_path(ctx)
    rows_written = 0
    feishu = {'sent': False, 'reason': 'dry_run' if dry_run else 'No triggered alerts'}
    if not dry_run:
        from .runtime.storage import MetricStore
        rows_written = MetricStore(db_path).record_alerts(alerts)
        if notify and triggered:
            from .runtime.feishu_alert import send_dashboard_alert
            feishu = send_dashboard_alert(triggered, alerts)
    return {
        'success': True,
        'dry_run': dry_run,
        'notify': notify,
        'db_path': str(db_path),
        'rows_written': rows_written,
        'alerts': alerts,
        'triggered': triggered,
        'feishu': feishu,
        'sheet_sync': {'enabled': False, 'reason': 'alert_check only sends alerts and does not write the daily sheet'},
        'frequencies': _frequencies(),
    }


def run_feishu_sheet_sync(ctx, payload):
    _configure_runtime_env(ctx)
    metrics = payload.get('metrics') or payload.get('data') or []
    errors = []
    if not metrics or _bool(payload.get('collect_metrics'), False):
        metrics, errors = _collect_metrics(payload)

    from .runtime.feishu_writer import sync_dashboard_metrics
    result = sync_dashboard_metrics(
        metrics,
        config=_feishu_sheet_config(ctx, payload),
        dry_run=_bool(payload.get('dry_run'), False),
    )
    result['collect_errors'] = errors
    result['metric_count'] = len(metrics)
    if errors:
        result['success'] = False
    return result


def _collect_metrics(payload):
    include_sources = payload.get('include_sources') or ['gsc', 'ga4', 'sql']
    if isinstance(include_sources, str):
        include_sources = [item.strip() for item in include_sources.split(',') if item.strip()]
    metrics = []
    errors = []
    base_date = payload.get('base_date') or date.today().isoformat()

    google_metrics_by_date = {}
    if 'gsc' in include_sources or 'ga4' in include_sources:
        from .runtime.google_metrics import fetch_google_metrics
        google = fetch_google_metrics(
            base_date=base_date,
            gsc_offsets=payload.get('gsc_offsets'),
            ga4_offsets=payload.get('ga4_offsets'),
        )
        for item in google.get('metrics', []):
            if item.get('source') in include_sources:
                metrics.append(item)
                if item.get('metric_name') == 'ga4_new_uv' and item.get('status') == 'ok':
                    google_metrics_by_date[item['metric_date']] = float(item['metric_value'] or 0)
        errors.extend(google.get('errors') or [])

    if 'sql' in include_sources:
        from .runtime.sql_metrics import fetch_sql_daily_metrics
        sql = fetch_sql_daily_metrics(
            base_date=base_date,
            start_offset=int(payload.get('sql_start_offset') or 15),
            end_offset=int(payload.get('sql_end_offset') or 1),
        )
        sql_metrics = sql.get('metrics') or []
        reg_by_date = {}
        act_by_date = {}
        for item in sql_metrics:
            if item.get('status') != 'ok':
                continue
            if item.get('metric_name') == 'registration_users':
                reg_by_date[item['metric_date']] = float(item['metric_value'] or 0)
            elif item.get('metric_name') == 'first_day_activation_users':
                act_by_date[item['metric_date']] = float(item['metric_value'] or 0)
        for day, reg_count in reg_by_date.items():
            ga4_uv = google_metrics_by_date.get(day)
            if ga4_uv and ga4_uv > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'registration_rate',
                    'metric_value': round(reg_count / ga4_uv, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'ga4_new_uv', 'numerator': 'registration_users'},
                    'status': 'ok',
                })
            act_count = act_by_date.get(day)
            if act_count is not None and reg_count and reg_count > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'activation_rate',
                    'metric_value': round(act_count / reg_count, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'registration_users', 'numerator': 'first_day_activation_users'},
                    'status': 'ok',
                })
            if act_count is not None and ga4_uv and ga4_uv > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'new_uv_activation_rate',
                    'metric_value': round(act_count / ga4_uv, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'ga4_new_uv', 'numerator': 'first_day_activation_users'},
                    'status': 'ok',
                })
        metrics.extend(sql_metrics)
        errors.extend(sql.get('errors') or [])

    return metrics, errors


def _collect_daily_sheet_metrics(payload):
    include_sources = payload.get('include_sources') or ['gsc', 'ga4', 'sql']
    if isinstance(include_sources, str):
        include_sources = [item.strip() for item in include_sources.split(',') if item.strip()]
    metrics = []
    errors = []
    base_date = payload.get('base_date') or date.today().isoformat()

    google_metrics_by_date = {}
    if 'gsc' in include_sources or 'ga4' in include_sources:
        from .runtime.google_metrics import fetch_google_metrics
        google = fetch_google_metrics(
            base_date=base_date,
            gsc_offsets=payload.get('gsc_offsets'),
            ga4_offsets=payload.get('ga4_offsets') or [2, 1],
        )
        for item in google.get('metrics', []):
            if item.get('source') in include_sources:
                metrics.append(item)
                if item.get('metric_name') == 'ga4_new_uv' and item.get('status') == 'ok':
                    google_metrics_by_date[item['metric_date']] = float(item['metric_value'] or 0)
        errors.extend(google.get('errors') or [])

    if 'sql' in include_sources:
        from .runtime.sql_metrics import fetch_sql_sheet_update_metrics
        sql = fetch_sql_sheet_update_metrics(base_date=base_date)
        sql_metrics = sql.get('metrics') or []
        reg_by_date = {}
        act_by_date = {}
        for item in sql_metrics:
            if item.get('status') != 'ok':
                continue
            if item.get('metric_name') == 'registration_users':
                reg_by_date[item['metric_date']] = float(item['metric_value'] or 0)
            elif item.get('metric_name') == 'first_day_activation_users':
                act_by_date[item['metric_date']] = float(item['metric_value'] or 0)
        for day, reg_count in reg_by_date.items():
            ga4_uv = google_metrics_by_date.get(day)
            if ga4_uv and ga4_uv > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'registration_rate',
                    'metric_value': round(reg_count / ga4_uv, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'ga4_new_uv', 'numerator': 'registration_users'},
                    'status': 'ok',
                })
            act_count = act_by_date.get(day)
            if act_count is not None and reg_count and reg_count > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'activation_rate',
                    'metric_value': round(act_count / reg_count, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'registration_users', 'numerator': 'first_day_activation_users'},
                    'status': 'ok',
                })
            if act_count is not None and ga4_uv and ga4_uv > 0:
                sql_metrics.append({
                    'metric_date': day,
                    'metric_name': 'new_uv_activation_rate',
                    'metric_value': round(act_count / ga4_uv, 6),
                    'source': 'computed',
                    'frequency': 'daily',
                    'dimensions': {'denominator': 'ga4_new_uv', 'numerator': 'first_day_activation_users'},
                    'status': 'ok',
                })
        metrics.extend(sql_metrics)
        errors.extend(sql.get('errors') or [])

    return metrics, errors


def _use_daily_sheet_scope(payload):
    if _bool(payload.get('backfill'), False):
        return False
    return str(payload.get('daily_scope') or 'sheet').strip().lower() not in {'backfill', 'full', 'history'}


def _configure_runtime_env(ctx):
    agentos_cfg = ctx.config.get('agentos', {})
    if agentos_cfg.get('base_url'):
        os.environ['AGENTOS_BASE_URL'] = str(agentos_cfg['base_url'])
    if agentos_cfg.get('token'):
        os.environ['AGENTOS_TOKEN'] = str(agentos_cfg['token'])


def _adapter_config(ctx):
    item = (ctx.adapter_configs or {}).get('dashboard_metrics') or {}
    return item.get('config') or {}


def _feishu_sheet_config(ctx, payload):
    config = dict(_adapter_config(ctx).get('feishu_sheet') or {})
    nested = payload.get('feishu_sheet') or {}
    if isinstance(nested, dict):
        config.update({key: value for key, value in nested.items() if value not in (None, '')})
    for key in ('wiki_token', 'spreadsheet_token', 'sheet_id', 'max_rows', 'end_col'):
        if payload.get(key) not in (None, ''):
            config[key] = payload[key]
    return config


def _storage_path(ctx):
    raw = str(_adapter_config(ctx).get('storage_path') or './storage/dashboard_metrics.db')
    path = Path(raw)
    return path if path.is_absolute() else PROJECT_DIR / path


def _bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {'', '0', 'false', 'no', 'off'}


def _frequencies():
    return {
        'gsc': {'schedule': 'daily', 'offsets': ['T-3', 'T-2'], 'metrics': ['gsc_impressions', 'gsc_clicks', 'gsc_ctr']},
        'ga4': {'schedule': 'daily', 'offsets': ['T-2'], 'metrics': ['ga4_new_uv']},
        'sql_daily': {
            'schedule': 'daily',
            'offsets': ['T-1 full-day SQL', 'T-2 registration_rate + D1 retention', 'T-8 D7 retention'],
            'metrics': ['registration_users', 'first_day_activation_users', 'dau', 'paid_users', 'renewal_orders', 'payment_amount', 'd1_retention', 'd7_retention'],
        },
        'sql_6h': {'schedule': 'every 6 hours', 'metrics': ['registration_users', 'paid_users', 'renewal_orders', 'payment_amount']},
    }
