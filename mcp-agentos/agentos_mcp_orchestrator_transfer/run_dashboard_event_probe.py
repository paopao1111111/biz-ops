import json
from pathlib import Path
from core.config import load_config
load_config(Path("config.yaml"))
from adapters.dashboard_metrics.runtime.superset_client import run_query

out = {}
for name, sql in [
    ("recent_events", "SELECT event_name, event_type, COUNT(*) AS cnt FROM events WHERE created_at >= NOW() - INTERVAL '7 days' GROUP BY event_name, event_type ORDER BY cnt DESC LIMIT 50"),
    ("recent_user_sources", "SELECT signin_provider, platform, COUNT(*) AS cnt FROM users WHERE created_at::timestamp >= NOW() - INTERVAL '7 days' GROUP BY signin_provider, platform ORDER BY cnt DESC LIMIT 30"),
    ("recent_trade_types", "SELECT trade_type, version, time_unit, COUNT(*) AS cnt, SUM(total_fee) AS amount FROM trades WHERE created_at >= NOW() - INTERVAL '30 days' AND is_success = true GROUP BY trade_type, version, time_unit ORDER BY cnt DESC LIMIT 30"),
]:
    try:
        rows, cols = run_query(sql, limit=50)
        out[name] = rows
    except Exception as exc:
        out[name] = {"error": str(exc)}

print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
