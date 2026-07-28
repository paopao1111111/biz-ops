#!/opt/edm-system/.venv/bin/python
"""Print recent unified monitoring events and report status."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MONITORING_HOME = Path(__file__).resolve().parents[1]
if str(MONITORING_HOME) not in sys.path:
    sys.path.insert(0, str(MONITORING_HOME))

from monitoring.event_store import MonitoringEventStore


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    store = MonitoringEventStore()
    events = store.list_events(limit=args.limit)
    print(json.dumps({"success": True, "events": events}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
