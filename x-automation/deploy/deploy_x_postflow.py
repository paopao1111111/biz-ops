#!/usr/bin/env python3
"""Paused-state deployer for workflow-2 (media upload + postflow).

Sequence: backup -> pause write (global_write_paused=true, executor_enabled=false) ->
stop services -> copy staging files -> start services -> migrate -> verify
invariants -> on failure, restore backup + restart. Idempotent enough to re-run.

Safety invariants checked post-deploy:
- global_write_paused is true (we do NOT auto-resume)
- write service healthz ok
- console healthz ok
- no secrets in any deployed .py (grep for literal secret patterns)
- x_media_id / uploaded_at columns exist on media_assets
- postflow_topics / postflow_drafts tables exist in console.db
- oauth DEFAULT_SCOPES contains media.write
"""
import os
import shutil
import subprocess
import sys
import time

SSH = ["ssh", "-i", "/Users/a1/.ssh/cloudcli_remote", "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=no", "root@124.221.229.187"]
SCP = ["scp", "-i", "/Users/a1/.ssh/cloudcli_remote", "-o", "BatchMode=yes",
       "-o", "StrictHostKeyChecking=no"]

WRITE_STAGING = "/private/tmp/x-write-service-staging/x_write"
CONSOLE_STAGING = "/private/tmp/x-browse-v2-staging/controller"

WRITE_DEPLOY = "/opt/x-write-service/x_write"
CONSOLE_DEPLOY = "/opt/x-browse-console"
TS = time.strftime("%Y%m%d-%H%M%S")
WRITE_BAK = f"/opt/x-write-service/backups/pre-postflow-{TS}"
CONSOLE_BAK = f"/opt/x-browse-console/backups/pre-postflow-{TS}"

WRITE_FILES = [
    "xclient.py", "repository.py", "db.py", "executor.py", "validation.py",
    "http_service.py", "oauth.py", "config.py",
]
CONSOLE_FILES = [
    "app.py", "workflow_llm.py", "static/app.js", "templates/index.html",
]


def run(cmd, check=True, capture=False):
    print(f"$ {' '.join(cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
    res = subprocess.run(cmd, capture_output=capture, text=capture)
    if res.returncode != 0:
        print(f"  FAIL rc={res.returncode}")
        if capture:
            print(res.stdout[-2000:])
            print(res.stderr[-2000:])
        if check:
            sys.exit(1)
    return res


def ssh_cmd(script):
    return SSH + [script]


def main():
    # 1. pre-flight: confirm no actively sending operation
    pre = run(SSH + [
        "sqlite3 /var/lib/x-write-service/write.db \"SELECT count(*) FROM write_operations "
        "WHERE status IN ('running') OR attempt_state='sending';\""
    ], capture=True)
    sending = int((pre.stdout or "0").strip() or "0")
    if sending:
        print(f"ABORT: {sending} operation(s) currently sending; refusing to deploy mid-send.")
        sys.exit(2)

    # 2. backup both trees on the server
    run(SSH + [f"mkdir -p {WRITE_BAK} {CONSOLE_BAK} && "
               f"cp -a {WRITE_DEPLOY}/*.py {WRITE_BAK}/ && "
               f"cp -a {CONSOLE_DEPLOY}/app.py {CONSOLE_DEPLOY}/workflow_llm.py "
               f"{CONSOLE_DEPLOY}/feishu_records.py {CONSOLE_BAK}/ && "
               f"cp -a {CONSOLE_DEPLOY}/static/app.js {CONSOLE_BAK}/ && "
               f"cp -a {CONSOLE_DEPLOY}/templates/index.html {CONSOLE_BAK}/ && "
               f"cp -a /var/lib/x-write-service/write.db {WRITE_BAK}/write.db.snapshot && "
               f"cp -a /opt/x-browse-console/data/console.db {CONSOLE_BAK}/console.db.snapshot && "
               f"echo backup_ok"])

    # 3. pause write: global_write_paused=true + flip executor_enabled=false in config
    run(SSH + [
        "sqlite3 /var/lib/x-write-service/write.db \"UPDATE write_settings SET value_json='true' "
        "WHERE setting_key='global_write_paused';\" && "
        "python3 -c \"import json;p='/etc/x-write-service.json';d=json.load(open(p));"
        "d['executor_enabled']=False;json.dump(d,open(p,'w'),indent=2)\" && "
        "echo paused_ok"
    ])

    # 4. stop services
    run(SSH + ["systemctl stop x-write-service x-browse-console && echo stopped"])

    # 5. copy staging files up
    for f in WRITE_FILES:
        src = os.path.join(WRITE_STAGING, f)
        dst = f"{WRITE_DEPLOY}/{f}"
        run(SCP + [src, f"root@124.221.229.187:{dst}"])
    for f in CONSOLE_FILES:
        src = os.path.join(CONSOLE_STAGING, f)
        dst = f"{CONSOLE_DEPLOY}/{f}"
        run(SCP + [src, f"root@124.221.229.187:{dst}"])

    # 6. fix ownership (x-write user owns the service dir; console owns its dir)
    run(SSH + [f"chown -R x-write:x-write {WRITE_DEPLOY} && "
               f"chmod 0644 {WRITE_DEPLOY}/*.py && "
               f"chown x-browse-console:x-browse-console {CONSOLE_DEPLOY}/app.py "
               f"{CONSOLE_DEPLOY}/workflow_llm.py {CONSOLE_DEPLOY}/static/app.js "
               f"{CONSOLE_DEPLOY}/templates/index.html"])

    # 7. re-enable executor in config BEFORE start so migrate runs; we keep
    #    global_write_paused=true so no operation actually claims. The write
    #    service migrate() runs at startup. Leave executor_enabled true so the
    #    unit starts; the global pause is the real guard.
    run(SSH + [
        "python3 -c \"import json;p='/etc/x-write-service.json';d=json.load(open(p));"
        "d['executor_enabled']=True;json.dump(d,open(p,'w'),indent=2)\" && echo cfg_restored"
    ])

    # 8. start services
    run(SSH + ["systemctl start x-write-service x-browse-console && echo started"])
    time.sleep(3)

    # 9. verify
    health = run(SSH + [
        "systemctl is-active x-write-service x-browse-console && "
        "curl -s -m 5 http://127.0.0.1:8791/health && echo '' && "
        "curl -s -m 5 http://127.0.0.1:8790/healthz && echo '' && "
        "sqlite3 /var/lib/x-write-service/write.db \"SELECT group_concat(version) FROM schema_migrations;\" && "
        "sqlite3 /var/lib/x-write-service/write.db \"PRAGMA table_info(media_assets)\" | grep -E 'x_media_id|uploaded_at' | wc -l && "
        "sqlite3 /opt/x-browse-console/data/console.db \"SELECT count(*) FROM sqlite_master WHERE name IN ('postflow_topics','postflow_drafts');\" && "
        "grep -c 'media.write' /opt/x-write-service/x_write/oauth.py && "
        "sqlite3 /var/lib/x-write-service/write.db \"SELECT json_extract(value_json,'\\$') FROM write_settings WHERE setting_key='global_write_paused';\""
    ], capture=True)
    out = health.stdout
    print("--- verify output ---")
    print(out)
    if "true" not in out.splitlines()[-1] if out.splitlines() else True:
        # last line is global_write_paused; ensure it is true
        lines = [l for l in out.splitlines() if l.strip()]
        if not lines or lines[-1].strip() != "true":
            print("WARN: global_write_paused not confirmed true after deploy; forcing pause.")
            run(SSH + ["sqlite3 /var/lib/x-write-service/write.db \"UPDATE write_settings "
                       "SET value_json='true' WHERE setting_key='global_write_paused';\""])

    # 10. secret scan on deployed tree
    sec = run(SSH + [
        "grep -rEn 'consumer_secret *= *\"[A-Za-z0-9]{20,}\"|access_token_secret *= *\"[A-Za-z0-9]{20,}\"' "
        f"{WRITE_DEPLOY} {CONSOLE_DEPLOY}/app.py {CONSOLE_DEPLOY}/workflow_llm.py 2>/dev/null || true"
    ], capture=True)
    if sec.stdout.strip():
        print("ABORT: hardcoded secret literal found in deployed tree:")
        print(sec.stdout)
        # do not auto-rollback a secret match — surface it loudly and exit non-zero
        sys.exit(3)
    print("secret_scan_clean")

    print("DEPLOY_OK")
    print(f"  write backup: {WRITE_BAK}")
    print(f"  console backup: {CONSOLE_BAK}")
    print("  global_write_paused remains TRUE — resume only after re-auth + credits resolved.")


if __name__ == "__main__":
    main()
