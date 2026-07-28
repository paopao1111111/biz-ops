from .superset_client import run_query


CANDIDATE_TABLES = [
    'chat_logs',
    'user_version',
    'trades',
    'users',
    'user',
    'orders',
    'payments',
    'subscriptions',
    'events',
]

DATE_COLUMNS = {
    'chat_logs': 'created_at',
    'user_version': 'created_at',
    'trades': 'created_at',
    'users': 'created_at',
    'user': 'created_at',
    'orders': 'created_at',
    'payments': 'created_at',
    'subscriptions': 'created_at',
    'events': 'created_at',
}


def probe_schema(limit=200):
    result = {
        'success': True,
        'candidate_tables': CANDIDATE_TABLES,
        'tables': [],
        'columns': [],
        'table_stats': {},
        'errors': [],
    }
    table_names = _quoted_names(CANDIDATE_TABLES)

    try:
        rows, columns = run_query(
            f"""
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_name IN ({table_names})
            ORDER BY table_schema, table_name
            LIMIT {int(limit)}
            """,
            limit=limit,
            enforce_limit=True,
        )
        result['tables'] = rows
    except Exception as exc:
        result['errors'].append({'step': 'tables', 'error': str(exc)})

    try:
        rows, columns = run_query(
            f"""
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_name IN ({table_names})
            ORDER BY table_schema, table_name, ordinal_position
            LIMIT 1000
            """,
            limit=1000,
            enforce_limit=True,
        )
        result['columns'] = rows
    except Exception as exc:
        result['errors'].append({'step': 'columns', 'error': str(exc)})

    existing = _existing_tables(result['tables']) or CANDIDATE_TABLES[:3]
    for table in existing:
        result['table_stats'][table] = _probe_table_stats(table)

    result['suggested_sources'] = suggest_sources(result)
    return result


def suggest_sources(probe_result):
    columns_by_table = {}
    for row in probe_result.get('columns') or []:
        table = row.get('table_name')
        column = row.get('column_name')
        columns_by_table.setdefault(table, set()).add(column)

    suggestions = {}
    if _has(columns_by_table, 'users', 'created_at', 'id', 'signin_openid'):
        suggestions['registration'] = {
            'table': 'users',
            'user_id_column': 'id',
            'activity_join_column': 'signin_openid',
            'created_at_column': 'created_at',
        }
        suggestions['first_day_activation'] = {
            'cohort_table': 'users',
            'activity_table': 'chat_logs',
            'join': 'users.signin_openid = chat_logs.user_id',
            'same_day_column': 'created_at',
        }
        suggestions['retention'] = {
            'cohort_table': 'users',
            'activity_table': 'chat_logs',
            'join': 'users.signin_openid = chat_logs.user_id',
            'cohort_date_column': 'users.created_at',
            'activity_date_column': 'chat_logs.created_at',
        }
    elif _has(columns_by_table, 'users', 'created_at', 'uuid'):
        suggestions['registration'] = {'table': 'users', 'user_id_column': 'uuid', 'created_at_column': 'created_at'}
    else:
        suggestions['registration'] = {'unavailable': True, 'reason': 'No registration source confirmed'}

    if _has(columns_by_table, 'chat_logs', 'created_at', 'user_id'):
        suggestions['activity'] = {'table': 'chat_logs', 'user_id_column': 'user_id', 'created_at_column': 'created_at'}
    else:
        suggestions['activity'] = {'unavailable': True, 'reason': 'chat_logs activity source not confirmed'}

    if _has(columns_by_table, 'trades', 'created_at', 'user_id', 'total_fee'):
        suggestions['payment'] = {'table': 'trades', 'user_id_column': 'user_id', 'created_at_column': 'created_at', 'amount_column': 'total_fee'}
    else:
        suggestions['payment'] = {'unavailable': True, 'reason': 'trades payment source not confirmed'}

    suggestions['registration_rate'] = {
        'source': 'computed',
        'numerator': 'registration_users',
        'denominator': 'ga4_new_uv',
        'frequency': 'daily',
    }
    return suggestions


def _probe_table_stats(table):
    stats = {'ok': False}
    try:
        rows, columns = run_query(f'SELECT COUNT(*) AS row_count FROM {table}', limit=1)
        stats['row_count'] = rows[0].get('row_count') if rows else None
        stats['ok'] = True
    except Exception as exc:
        stats['error'] = str(exc)
        return stats

    date_column = DATE_COLUMNS.get(table)
    if date_column:
        try:
            rows, columns = run_query(
                f'SELECT MIN({date_column}) AS min_date, MAX({date_column}) AS max_date FROM {table}',
                limit=1,
            )
            if rows:
                stats.update(rows[0])
        except Exception as exc:
            stats['date_error'] = str(exc)
    return stats


def _existing_tables(rows):
    tables = []
    for row in rows or []:
        name = row.get('table_name')
        if name and name not in tables:
            tables.append(name)
    return tables


def _quoted_names(names):
    return ', '.join("'" + name.replace("'", "''") + "'" for name in names)


def _has(columns_by_table, table, *columns):
    available = columns_by_table.get(table) or set()
    return all(column in available for column in columns)
