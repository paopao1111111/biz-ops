import json
import os
from datetime import date, datetime, timedelta


GSC_TOKEN_JSON = os.getenv('GSC_TOKEN_JSON', '')
GSC_SITE_URL = os.getenv('GSC_SITE_URL', 'sc-domain:iweaver.ai')
GSC_PROXY_HOST = os.getenv('GSC_PROXY_HOST', '127.0.0.1')
GSC_PROXY_PORT = int(os.getenv('GSC_PROXY_PORT', '10808'))
GA4_PROPERTY_ID = os.getenv('GA4_PROPERTY_ID', '435515520')
GA4_UV_METRIC = os.getenv('GA4_UV_METRIC', 'newUsers')
GA4_HOSTNAME = os.getenv('GA4_HOSTNAME', '').strip()

_gsc_service = None
_ga4_service = None


def fetch_google_metrics(base_date=None, gsc_offsets=None, ga4_offsets=None):
    base = parse_date(base_date) if base_date else date.today()
    gsc_offsets = gsc_offsets if gsc_offsets is not None else [3, 2]
    ga4_offsets = ga4_offsets if ga4_offsets is not None else [2]

    metrics = []
    errors = []

    try:
        metrics.extend(fetch_gsc_daily_metrics([offset_date(base, offset) for offset in gsc_offsets]))
    except Exception as exc:
        errors.append({'source': 'gsc', 'error': str(exc)})

    try:
        metrics.extend(fetch_ga4_uv_metrics([offset_date(base, offset) for offset in ga4_offsets]))
    except Exception as exc:
        errors.append({'source': 'ga4', 'error': str(exc)})

    return {'metrics': metrics, 'errors': errors}


def fetch_gsc_daily_metrics(dates):
    if not _gsc_configured():
        rows = []
        for day in dates:
            rows.extend([
                unavailable_metric(day, 'gsc_impressions', 'gsc', 'GSC_TOKEN_JSON is not configured'),
                unavailable_metric(day, 'gsc_clicks', 'gsc', 'GSC_TOKEN_JSON is not configured'),
                unavailable_metric(day, 'gsc_ctr', 'gsc', 'GSC_TOKEN_JSON is not configured'),
            ])
        return rows
    service = _get_gsc_service()
    rows = []
    for day in dates:
        body = {
            'startDate': day,
            'endDate': day,
            'dimensions': ['date'],
            'rowLimit': 10,
        }
        resp = service.searchanalytics().query(siteUrl=GSC_SITE_URL, body=body).execute()
        data = (resp.get('rows') or [{}])[0]
        clicks = float(data.get('clicks') or 0)
        impressions = float(data.get('impressions') or 0)
        ctr = float(data.get('ctr') or 0) * 100
        rows.extend([
            metric(day, 'gsc_impressions', impressions, 'gsc'),
            metric(day, 'gsc_clicks', clicks, 'gsc'),
            metric(day, 'gsc_ctr', round(ctr, 4), 'gsc'),
        ])
    return rows


def fetch_ga4_uv_metrics(dates):
    if not _ga4_configured():
        return [unavailable_metric(day, 'ga4_new_uv', 'ga4', 'GA4 credentials are not configured') for day in dates]
    service = _get_ga4_service()
    rows = []
    for day in dates:
        body = {
            'dateRanges': [{'startDate': day, 'endDate': day}],
            'metrics': [{'name': GA4_UV_METRIC}],
            'limit': 1,
        }
        if GA4_HOSTNAME:
            body['dimensions'] = [{'name': 'hostName'}]
            body['dimensionFilter'] = {
                'filter': {
                    'fieldName': 'hostName',
                    'stringFilter': {'matchType': 'EXACT', 'value': GA4_HOSTNAME},
                }
            }
        resp = service.properties().runReport(property=f'properties/{GA4_PROPERTY_ID}', body=body).execute()
        value = 0.0
        if resp.get('rows'):
            value = float(resp['rows'][0]['metricValues'][0]['value'])
        rows.append(metric(day, 'ga4_new_uv', value, 'ga4', dimensions={'ga4_metric': GA4_UV_METRIC}))
    return rows


def _gsc_configured():
    return bool(GSC_TOKEN_JSON and os.path.exists(GSC_TOKEN_JSON))


def _ga4_configured():
    return bool(GA4_PROPERTY_ID and GSC_TOKEN_JSON and os.path.exists(GSC_TOKEN_JSON))


def _get_gsc_service():
    global _gsc_service
    if _gsc_service:
        return _gsc_service
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    import socks

    with open(GSC_TOKEN_JSON, encoding='utf-8') as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data['token'],
        refresh_token=token_data['refresh_token'],
        token_uri=token_data.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=['https://www.googleapis.com/auth/webmasters.readonly'],
    )
    http = httplib2.Http(proxy_info=_proxy_info(socks))
    _gsc_service = build('searchconsole', 'v1', http=AuthorizedHttp(creds, http=http))
    return _gsc_service


def _get_ga4_service():
    global _ga4_service
    if _ga4_service:
        return _ga4_service
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google_auth_httplib2 import AuthorizedHttp
    import httplib2
    import socks

    with open(GSC_TOKEN_JSON, encoding='utf-8') as f:
        token_data = json.load(f)
    creds = Credentials(
        token=token_data['token'],
        refresh_token=token_data['refresh_token'],
        token_uri=token_data.get('token_uri', 'https://oauth2.googleapis.com/token'),
        client_id=token_data['client_id'],
        client_secret=token_data['client_secret'],
        scopes=token_data.get('scopes', []),
    )
    http = httplib2.Http(proxy_info=_proxy_info(socks))
    _ga4_service = build('analyticsdata', 'v1beta', http=AuthorizedHttp(creds, http=http))
    return _ga4_service


def _proxy_info(socks):
    if not GSC_PROXY_HOST or not GSC_PROXY_PORT:
        return None
    import httplib2
    return httplib2.ProxyInfo(
        proxy_type=socks.PROXY_TYPE_SOCKS5,
        proxy_host=GSC_PROXY_HOST,
        proxy_port=GSC_PROXY_PORT,
    )


def parse_date(value):
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), '%Y-%m-%d').date()


def offset_date(base, offset):
    return (base - timedelta(days=int(offset))).isoformat()


def metric(metric_date, name, value, source, frequency='daily', dimensions=None):
    return {
        'metric_date': metric_date,
        'metric_name': name,
        'metric_value': value,
        'source': source,
        'frequency': frequency,
        'dimensions': dimensions or {},
        'status': 'ok',
    }


def unavailable_metric(metric_date, name, source, reason):
    return {
        'metric_date': metric_date,
        'metric_name': name,
        'metric_value': None,
        'source': source,
        'frequency': 'daily',
        'status': 'unavailable',
        'reason': reason,
    }
