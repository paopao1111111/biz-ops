from datetime import datetime, timedelta, timezone

from .sql_metrics import fetch_window_metrics


THRESHOLDS = {
    'registration_users': 0.10,
    'paid_users': 0.20,
    'renewal_orders': 0.20,
    'payment_amount': 0.20,
}


def check_alerts(now=None, window_hours=6):
    now_dt = parse_datetime(now) if now else datetime.now(timezone.utc).replace(microsecond=0)
    current_end = now_dt
    current_start = current_end - timedelta(hours=int(window_hours))
    previous_end = current_start
    previous_start = previous_end - timedelta(hours=int(window_hours))

    current = fetch_window_metrics(fmt(current_start), fmt(current_end))
    previous = fetch_window_metrics(fmt(previous_start), fmt(previous_end))

    alerts = []
    for name, threshold in THRESHOLDS.items():
        cur = current.get(name)
        prev = previous.get(name)
        if cur is None or prev is None:
            alerts.append({
                'metric_name': name,
                'current_value': cur,
                'previous_value': prev,
                'change_ratio': None,
                'threshold': threshold,
                'triggered': False,
                'window_start': fmt(current_start),
                'window_end': fmt(current_end),
                'previous_window_start': fmt(previous_start),
                'previous_window_end': fmt(previous_end),
                'details': {'status': 'unavailable', 'reason': 'metric denominator or query is not configured'},
            })
            continue
        ratio = abs(float(cur) - float(prev)) / max(abs(float(prev)), 1.0)
        alerts.append({
            'metric_name': name,
            'current_value': float(cur),
            'previous_value': float(prev),
            'change_ratio': ratio,
            'threshold': threshold,
            'triggered': ratio > threshold,
            'window_start': fmt(current_start),
            'window_end': fmt(current_end),
            'previous_window_start': fmt(previous_start),
            'previous_window_end': fmt(previous_end),
            'details': {'window_hours': int(window_hours)},
        })
    return alerts


def parse_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace('Z', '+00:00')
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def fmt(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
