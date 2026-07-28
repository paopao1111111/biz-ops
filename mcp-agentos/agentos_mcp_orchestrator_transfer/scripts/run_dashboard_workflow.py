#!/usr/bin/env python3
import argparse
import json
import os
import sys
import traceback
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
LOCK_DIR = PROJECT_DIR / 'storage'


def load_env():
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


def read_payload(args):
    if args.payload_file:
        return json.loads(Path(args.payload_file).read_text(encoding='utf-8'))
    if args.payload:
        return json.loads(args.payload)
    return {}


def main():
    parser = argparse.ArgumentParser(description='Run dashboard_metrics MCP workflow once')
    parser.add_argument('--workflow', required=True)
    parser.add_argument('--payload', default='')
    parser.add_argument('--payload-file', default='')
    args = parser.parse_args()

    os.chdir(PROJECT_DIR)
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))
    load_env()
    payload = read_payload(args)
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = LOCK_DIR / f'{args.workflow}.lock'

    import fcntl
    with lock_path.open('w') as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'success': False, 'error': f'workflow already running: {args.workflow}'}, ensure_ascii=False))
            return 75

        import mcp_server
        result = mcp_server.registry.run_operation(mcp_server.ctx, args.workflow, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get('success', True) else 2


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'success': False, 'error': str(exc), 'traceback': traceback.format_exc()}, ensure_ascii=False, indent=2), file=sys.stderr)
        raise SystemExit(1)
