from datetime import date, datetime, timedelta

from .superset_client import run_query


def fetch_sql_daily_metrics(base_date=None, start_offset=15, end_offset=1):
    base = parse_date(base_date) if base_date else date.today()
    start_date = (base - timedelta(days=int(start_offset))).isoformat()
    end_date = (base - timedelta(days=int(end_offset))).isoformat()
    end_exclusive = (parse_date(end_date) + timedelta(days=1)).isoformat()

    metrics = []
    errors = []
    for fetcher in (
        fetch_registration_metrics,
        fetch_activity_metrics,
        fetch_retention_metrics,
        fetch_payment_metrics,
    ):
        try:
            metrics.extend(fetcher(start_date, end_exclusive))
        except Exception as exc:
            errors.append({'source': 'sql', 'step': fetcher.__name__, 'error': str(exc)})

    return {'metrics': metrics, 'errors': errors, 'start_date': start_date, 'end_date': end_date}


def fetch_registration_metrics(start_date, end_exclusive):
    rows, columns = run_query(
        f"""
        WITH regs AS (
          SELECT id AS reg_user_id, signin_openid AS activity_user_id, DATE(created_at::timestamp) AS reg_date
          FROM users
          WHERE created_at::timestamp >= '{start_date}'
            AND created_at::timestamp < '{end_exclusive}'
        ), activated AS (
          SELECT DISTINCT r.reg_user_id, r.reg_date
          FROM regs r
          JOIN chat_logs c
            ON c.user_id = r.activity_user_id
           AND DATE(c.created_at) = r.reg_date
           AND (c.deleted = false OR c.deleted IS NULL)
        )
        SELECT r.reg_date AS metric_date,
               COUNT(DISTINCT r.reg_user_id) AS registration_users,
               COUNT(DISTINCT a.reg_user_id) AS first_day_activation_users
        FROM regs r
        LEFT JOIN activated a
          ON a.reg_user_id = r.reg_user_id
         AND a.reg_date = r.reg_date
        GROUP BY r.reg_date
        ORDER BY metric_date
        """,
        limit=1000,
    )
    metrics = []
    for row in rows:
        day = str(row['metric_date'])[:10]
        metrics.append(metric(day, 'registration_users', row.get('registration_users'), dimensions={'registration_source': 'users_created_at'}))
        metrics.append(metric(day, 'first_day_activation_users', row.get('first_day_activation_users'), dimensions={'activation_source': 'users_signin_openid_chat_logs_same_day'}))
    return metrics


def fetch_activity_metrics(start_date, end_exclusive):
    rows, columns = run_query(
        f"""
        SELECT DATE(created_at) AS metric_date,
               COUNT(DISTINCT user_id) AS dau
        FROM chat_logs
        WHERE created_at >= '{start_date}'
          AND created_at < '{end_exclusive}'
          AND (deleted = false OR deleted IS NULL)
        GROUP BY DATE(created_at)
        ORDER BY metric_date
        """,
        limit=1000,
    )
    return [metric(str(row['metric_date'])[:10], 'dau', row.get('dau'), dimensions={'activity_source': 'chat_logs'}) for row in rows]


def fetch_retention_metrics(start_date, end_exclusive):
    rows, columns = run_query(
        f"""
        WITH cohort AS (
          SELECT id AS reg_user_id, signin_openid AS activity_user_id, DATE(created_at::timestamp) AS cohort_date
          FROM users
          WHERE created_at::timestamp >= '{start_date}'
            AND created_at::timestamp < '{end_exclusive}'
        ), activity AS (
          SELECT DISTINCT user_id, DATE(created_at) AS active_date
          FROM chat_logs
          WHERE created_at >= '{start_date}'::date
            AND created_at < '{end_exclusive}'::date + INTERVAL '8 days'
            AND (deleted = false OR deleted IS NULL)
        )
        SELECT c.cohort_date AS metric_date,
               COUNT(DISTINCT c.reg_user_id) AS cohort_users,
               COUNT(DISTINCT CASE WHEN a1.user_id IS NOT NULL THEN c.reg_user_id END) AS d1_retention_users,
               COUNT(DISTINCT CASE WHEN a7.user_id IS NOT NULL THEN c.reg_user_id END) AS d7_retention_users
        FROM cohort c
        LEFT JOIN activity a1
          ON a1.user_id = c.activity_user_id
         AND a1.active_date = c.cohort_date + INTERVAL '1 day'
        LEFT JOIN activity a7
          ON a7.user_id = c.activity_user_id
         AND a7.active_date = c.cohort_date + INTERVAL '7 days'
        GROUP BY c.cohort_date
        ORDER BY c.cohort_date
        """,
        limit=1000,
    )
    metrics = []
    for row in rows:
        day = str(row['metric_date'])[:10]
        cohort = float(row.get('cohort_users') or 0)
        d1 = float(row.get('d1_retention_users') or 0)
        d7 = float(row.get('d7_retention_users') or 0)
        metrics.extend([
            metric(day, 'cohort_users', cohort, dimensions={'cohort_source': 'users_created_at'}),
            metric(day, 'd1_retention_users', d1, dimensions={'activity_source': 'chat_logs'}),
            metric(day, 'd1_retention_rate', round(d1 / cohort, 6) if cohort else 0, dimensions={'activity_source': 'chat_logs'}),
            metric(day, 'd7_retention_users', d7, dimensions={'activity_source': 'chat_logs'}),
            metric(day, 'd7_retention_rate', round(d7 / cohort, 6) if cohort else 0, dimensions={'activity_source': 'chat_logs'}),
        ])
    return metrics


def fetch_payment_metrics(start_date, end_exclusive):
    rows, columns = run_query(
        f"""
        SELECT DATE(t.created_at) AS metric_date,
               COUNT(DISTINCT t.user_id) AS paid_users,
               COUNT(*) AS paid_orders,
               SUM(t.total_fee) AS payment_amount,
               COUNT(CASE WHEN EXISTS (
                 SELECT 1
                 FROM trades p
                 WHERE p.user_id = t.user_id
                   AND p.is_success = true
                   AND p.created_at < t.created_at
               ) THEN 1 END) AS renewal_orders
        FROM trades t
        WHERE t.created_at >= '{start_date}'
          AND t.created_at < '{end_exclusive}'
          AND t.is_success = true
        GROUP BY DATE(t.created_at)
        ORDER BY metric_date
        """,
        limit=1000,
    )
    metrics = []
    for row in rows:
        day = str(row['metric_date'])[:10]
        metrics.extend([
            metric(day, 'paid_users', row.get('paid_users'), dimensions={'payment_source': 'trades'}),
            metric(day, 'paid_orders', row.get('paid_orders'), dimensions={'payment_source': 'trades'}),
            metric(day, 'renewal_orders', row.get('renewal_orders'), dimensions={'renewal_definition': 'has_prior_successful_trade'}),
            metric(day, 'payment_amount', row.get('payment_amount') or 0, dimensions={'payment_source': 'trades'}),
        ])
    return metrics


def fetch_window_metrics(window_start, window_end):
    metrics = {}
    rows, columns = run_query(
        f"""
        SELECT COUNT(*) AS registration_users
        FROM users
        WHERE created_at::timestamp >= '{window_start}'
          AND created_at::timestamp < '{window_end}'
        """,
        limit=1,
    )
    metrics['registration_users'] = _value(rows, 'registration_users')
    metrics['registration_rate'] = None

    rows, columns = run_query(
        f"""
        SELECT COUNT(DISTINCT t.user_id) AS paid_users,
               SUM(t.total_fee) AS payment_amount,
               COUNT(CASE WHEN EXISTS (
                 SELECT 1 FROM trades p
                 WHERE p.user_id = t.user_id
                   AND p.is_success = true
                   AND p.created_at < t.created_at
               ) THEN 1 END) AS renewal_orders
        FROM trades t
        WHERE t.created_at >= '{window_start}'
          AND t.created_at < '{window_end}'
          AND t.is_success = true
        """,
        limit=1,
    )
    metrics['paid_users'] = _value(rows, 'paid_users')
    metrics['renewal_orders'] = _value(rows, 'renewal_orders')
    metrics['payment_amount'] = _value(rows, 'payment_amount')
    return metrics


def fetch_sql_sheet_update_metrics(base_date=None):
    base = parse_date(base_date) if base_date else date.today()
    yesterday = (base - timedelta(days=1)).isoformat()
    ga_ready_day = (base - timedelta(days=2)).isoformat()
    d1_ready_day = (base - timedelta(days=1)).isoformat()
    d7_ready_day = (base - timedelta(days=8)).isoformat()

    metrics = []
    errors = []
    try:
        metrics.extend(fetch_registration_metrics(yesterday, next_day(yesterday)))
        if ga_ready_day != yesterday:
            metrics.extend(fetch_registration_metrics(ga_ready_day, next_day(ga_ready_day)))
    except Exception as exc:
        errors.append({'source': 'sql', 'step': 'sheet_registration_metrics', 'error': str(exc)})
    try:
        metrics.extend(fetch_activity_metrics(yesterday, next_day(yesterday)))
    except Exception as exc:
        errors.append({'source': 'sql', 'step': 'sheet_activity_metrics', 'error': str(exc)})
    try:
        metrics.extend(fetch_payment_metrics(yesterday, next_day(yesterday)))
    except Exception as exc:
        errors.append({'source': 'sql', 'step': 'sheet_payment_metrics', 'error': str(exc)})
    try:
        metrics.extend(filter_metric_names(fetch_retention_metrics(d1_ready_day, next_day(d1_ready_day)), {'d1_retention_users', 'd1_retention_rate'}))
    except Exception as exc:
        errors.append({'source': 'sql', 'step': 'sheet_d1_retention_metrics', 'error': str(exc)})
    try:
        metrics.extend(filter_metric_names(fetch_retention_metrics(d7_ready_day, next_day(d7_ready_day)), {'d7_retention_users', 'd7_retention_rate'}))
    except Exception as exc:
        errors.append({'source': 'sql', 'step': 'sheet_d7_retention_metrics', 'error': str(exc)})

    return {
        'metrics': metrics,
        'errors': errors,
        'dates': {
            'yesterday_sql': yesterday,
            'ga_ready_day': ga_ready_day,
            'd1_ready_day': d1_ready_day,
            'd7_ready_day': d7_ready_day,
        },
    }


def filter_metric_names(metrics, names):
    return [item for item in metrics if item.get('metric_name') in names]


def next_day(value):
    return (parse_date(value) + timedelta(days=1)).isoformat()


def fetch_today_cumulative_metrics(target_date=None, as_of=None):
    day = cumulative_day(target_date, as_of)
    start_at = day.isoformat()
    end_at = cumulative_end(day, as_of)
    metrics = []
    errors = []
    for fetcher in (
        fetch_today_registration_metrics,
        fetch_today_activity_metrics,
        fetch_today_payment_metrics,
    ):
        try:
            metrics.extend(fetcher(day.isoformat(), start_at, end_at))
        except Exception as exc:
            errors.append({'source': 'sql', 'step': fetcher.__name__, 'error': str(exc)})
    return {'metrics': metrics, 'errors': errors, 'target_date': day.isoformat(), 'window_start': start_at, 'window_end': end_at}


def fetch_today_registration_metrics(metric_date, start_at, end_at):
    rows, columns = run_query(
        f"""
        WITH regs AS (
          SELECT id AS reg_user_id, signin_openid AS activity_user_id, DATE(created_at::timestamp) AS reg_date
          FROM users
          WHERE created_at::timestamp >= '{start_at}'
            AND created_at::timestamp < '{end_at}'
        ), activated AS (
          SELECT DISTINCT r.reg_user_id
          FROM regs r
          JOIN chat_logs c
            ON c.user_id = r.activity_user_id
           AND DATE(c.created_at) = r.reg_date
           AND c.created_at < '{end_at}'
           AND (c.deleted = false OR c.deleted IS NULL)
        )
        SELECT COUNT(DISTINCT r.reg_user_id) AS registration_users,
               COUNT(DISTINCT a.reg_user_id) AS first_day_activation_users
        FROM regs r
        LEFT JOIN activated a ON a.reg_user_id = r.reg_user_id
        """,
        limit=1,
    )
    return [
        metric(metric_date, 'registration_users', _value(rows, 'registration_users'), dimensions={'sync_scope': 'today_cumulative', 'registration_source': 'users_created_at'}),
        metric(metric_date, 'first_day_activation_users', _value(rows, 'first_day_activation_users'), dimensions={'sync_scope': 'today_cumulative', 'activation_source': 'users_signin_openid_chat_logs_same_day'}),
    ]


def fetch_today_activity_metrics(metric_date, start_at, end_at):
    rows, columns = run_query(
        f"""
        SELECT COUNT(DISTINCT user_id) AS dau
        FROM chat_logs
        WHERE created_at >= '{start_at}'
          AND created_at < '{end_at}'
          AND (deleted = false OR deleted IS NULL)
        """,
        limit=1,
    )
    return [metric(metric_date, 'dau', _value(rows, 'dau'), dimensions={'sync_scope': 'today_cumulative'})]


def fetch_today_payment_metrics(metric_date, start_at, end_at):
    rows, columns = run_query(
        f"""
        SELECT COUNT(DISTINCT t.user_id) AS paid_users,
               COUNT(*) AS paid_orders,
               SUM(t.total_fee) AS payment_amount,
               COUNT(CASE WHEN EXISTS (
                 SELECT 1
                 FROM trades p
                 WHERE p.user_id = t.user_id
                   AND p.is_success = true
                   AND p.created_at < t.created_at
               ) THEN 1 END) AS renewal_orders
        FROM trades t
        WHERE t.created_at >= '{start_at}'
          AND t.created_at < '{end_at}'
          AND t.is_success = true
        """,
        limit=1,
    )
    return [
        metric(metric_date, 'paid_users', _value(rows, 'paid_users'), dimensions={'sync_scope': 'today_cumulative'}),
        metric(metric_date, 'paid_orders', _value(rows, 'paid_orders'), dimensions={'sync_scope': 'today_cumulative'}),
        metric(metric_date, 'renewal_orders', _value(rows, 'renewal_orders'), dimensions={'sync_scope': 'today_cumulative'}),
        metric(metric_date, 'payment_amount', _value(rows, 'payment_amount'), dimensions={'sync_scope': 'today_cumulative'}),
    ]


def cumulative_day(target_date=None, as_of=None):
    if target_date:
        return parse_date(target_date)
    if as_of:
        return parse_datetime(as_of).date()
    return date.today()


def cumulative_end(day, as_of=None):
    if as_of:
        return sql_datetime(parse_datetime(as_of))
    if day == date.today():
        return sql_datetime(datetime.now())
    return (day + timedelta(days=1)).isoformat()


def parse_datetime(value):
    if isinstance(value, datetime):
        return value
    text = str(value).replace('Z', '+00:00')
    return datetime.fromisoformat(text).replace(tzinfo=None)


def sql_datetime(value):
    return value.replace(microsecond=0).isoformat(sep=' ')


def parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def date_range(start_date, end_date):
    current = parse_date(start_date)
    end = parse_date(end_date)
    while current <= end:
        yield current.isoformat()
        current += timedelta(days=1)


def metric(metric_date, name, value, source='sql', frequency='daily', dimensions=None):
    return {
        'metric_date': metric_date,
        'metric_name': name,
        'metric_value': float(value) if value is not None else None,
        'source': source,
        'frequency': frequency,
        'dimensions': dimensions or {},
        'status': 'ok',
    }


def unavailable(metric_date, name, reason):
    return {
        'metric_date': metric_date,
        'metric_name': name,
        'metric_value': None,
        'source': 'sql',
        'frequency': 'daily',
        'dimensions': {},
        'status': 'unavailable',
        'reason': reason,
    }


def _value(rows, column):
    if not rows:
        return 0
    value = rows[0].get(column)
    return float(value or 0)
