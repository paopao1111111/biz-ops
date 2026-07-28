#!/usr/bin/env python3
"""Deploy the content-workflow release (reply op + scheduled + workflow view + LLM + Feishu)
on top of the existing paused OAuth release. Preserves global write pause, executor off,
and browse schedule paused. Backs up, migrates, restarts, verifies; rolls back on failure.
"""
from __future__ import annotations

import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime, timezone
from pathlib import Path

WRITE_LIVE = Path("/opt/x-write-service")
CONSOLE_LIVE = Path("/opt/x-browse-console")
WRITE_STAGE = Path("/private/tmp/x-write-service-staging")
CONSOLE_STAGE = Path("/private/tmp/x-browse-v2-staging/controller")
WRITE_CONFIG = Path("/etc/x-write-service.json")
WRITE_DB = Path("/var/lib/x-write-service/write.db")
CONSOLE_DB = Path("/opt/x-browse-console/data/console.db")
CONSOLE_ENV = Path("/etc/x-browse-console.env")
NEW_API_CREDS = Path("/opt/new-api/new-api-credentials.env")
STAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
WRITE_BACKUP = WRITE_LIVE / "backups" / f"pre-workflow-{STAMP}"
CONSOLE_BACKUP = CONSOLE_LIVE / "backups" / f"pre-workflow-{STAMP}"


class DeployError(RuntimeError):
    pass


def run(args, *, check=True):
    r = subprocess.run(args, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and r.returncode != 0:
        raise DeployError(f"{args[0]} failed: {(r.stderr or r.stdout).strip()[:400]}")
    return r


def active(name): return run(["systemctl", "is-active", "--quiet", name], check=False).returncode == 0


def stop(name):
    run(["systemctl", "stop", name])
    for _ in range(80):
        if not active(name):
            pid = run(["systemctl", "show", name, "-p", "MainPID", "--value"], check=False).stdout.strip()
            if pid in ("", "0"):
                return
        time.sleep(0.25)
    raise DeployError(f"{name} did not stop")


def sqlite_backup(src, dst):
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    t = sqlite3.connect(dst)
    try: s.backup(t)
    finally: t.close(); s.close()
    os.chmod(dst, 0o600)


def copy_tree(src, dst):
    if dst.exists(): shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)


def normalize(root):
    for p in root.rglob("*"):
        if p.is_symlink(): raise DeployError(f"symlink in release: {p}")
        if p.is_dir(): os.chmod(p, 0o755)
        elif p.is_file(): os.chmod(p, 0o644)
        os.chown(p, 0, 0)


def write_state():
    with sqlite3.connect(WRITE_DB) as c:
        pause = json.loads(c.execute("SELECT value_json FROM write_settings WHERE setting_key='global_write_paused'").fetchone()[0])
        cols = {r[1] for r in c.execute("PRAGMA table_info(write_operations)")}
        return {
            "integrity": c.execute("PRAGMA integrity_check").fetchone()[0],
            "paused": pause,
            "accounts": c.execute("SELECT COUNT(*) FROM write_accounts").fetchone()[0],
            "queued": c.execute("SELECT COUNT(*) FROM write_operations WHERE status='queued'").fetchone()[0],
            "enabled_unpaused": c.execute("SELECT COUNT(*) FROM write_accounts WHERE enabled=1 AND paused=0").fetchone()[0],
            "has_scheduled_at": "scheduled_at" in cols,
        }


def console_state():
    with sqlite3.connect(CONSOLE_DB) as c:
        return {
            "integrity": c.execute("PRAGMA integrity_check").fetchone()[0],
            "schedule_paused": c.execute("SELECT value FROM settings WHERE key='global_schedule_paused'").fetchone()[0],
            "active_jobs": c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined')").fetchone()[0],
            "accounts": c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0],
            "has_workflow_items": bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflow_items'").fetchone()),
        }


def backup():
    WRITE_BACKUP.mkdir(parents=True, exist_ok=True); os.chmod(WRITE_BACKUP, 0o700)
    CONSOLE_BACKUP.mkdir(parents=True, exist_ok=True); os.chmod(CONSOLE_BACKUP, 0o700)
    copy_tree(WRITE_LIVE / "x_write", WRITE_BACKUP / "x_write")
    shutil.copy2(WRITE_CONFIG, WRITE_BACKUP / "x-write-service.json")
    sqlite_backup(WRITE_DB, WRITE_BACKUP / "write.db")
    shutil.copy2(CONSOLE_LIVE / "app.py", CONSOLE_BACKUP / "app.py")
    for sub in ("static", "templates"):
        copy_tree(CONSOLE_LIVE / sub, CONSOLE_BACKUP / sub)
    shutil.copy2(CONSOLE_ENV, CONSOLE_BACKUP / "x-browse-console.env")
    sqlite_backup(CONSOLE_DB, CONSOLE_BACKUP / "console.db")
    manifest = {"stamp": STAMP, "write": write_state(), "console": console_state()}
    for d in (WRITE_BACKUP, CONSOLE_BACKUP):
        (d / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        os.chmod(d / "manifest.json", 0o600)
        (d / "BACKUP_COMPLETE").write_text(STAMP + "\n"); os.chmod(d / "BACKUP_COMPLETE", 0o600)


def env_upsert(path, key, value):
    lines = path.read_text(encoding="utf-8").splitlines()
    present = any(l.strip().startswith(f"{key}=") for l in lines)
    addition = f"{key}={value}"
    if present:
        lines = [addition if l.strip().startswith(f"{key}=") else l for l in lines]
    else:
        if lines and lines[-1].strip(): lines.append("")
        lines.append("# content workflow")
        lines.append(addition)
    payload = ("\n".join(lines) + "\n").encode("utf-8")
    st = path.stat()
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode)); os.fchown(fd, st.st_uid, st.st_gid)
        with os.fdopen(fd, "wb") as f: f.write(payload); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def deploy_files():
    copy_tree(WRITE_STAGE / "x_write", WRITE_LIVE / "x_write")
    normalize(WRITE_LIVE / "x_write")
    shutil.rmtree(WRITE_LIVE / "x_write" / "__pycache__", ignore_errors=True)
    shutil.copy2(CONSOLE_STAGE / "app.py", CONSOLE_LIVE / "app.py")
    os.chown(CONSOLE_LIVE / "app.py", 0, 0); os.chmod(CONSOLE_LIVE / "app.py", 0o644)
    for sub, names in (("static", ("app.js", "app.css")), ("templates", ("index.html", "login.html", "oauth_callback.html"))):
        for n in names:
            shutil.copy2(CONSOLE_STAGE / sub / n, CONSOLE_LIVE / sub / n)
            os.chown(CONSOLE_LIVE / sub / n, 0, 0); os.chmod(CONSOLE_LIVE / sub / n, 0o644)
    for n in ("workflow_llm.py", "feishu_records.py"):
        shutil.copy2(CONSOLE_STAGE / n, CONSOLE_LIVE / n)
        os.chown(CONSOLE_LIVE / n, 0, 0); os.chmod(CONSOLE_LIVE / n, 0o644)
    shutil.rmtree(CONSOLE_LIVE / "__pycache__", ignore_errors=True)


def configure_env():
    env_upsert(CONSOLE_ENV, "WORKFLOW_LLM_BASE_URL", "http://127.0.0.1:8318/v1")
    env_upsert(CONSOLE_ENV, "WORKFLOW_LLM_MODEL", "glm-5.1")
    llm_key = ""
    if NEW_API_CREDS.is_file():
        for line in NEW_API_CREDS.read_text().splitlines():
            if line.strip().startswith("NEW_API_TEST_TOKEN="):
                llm_key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if llm_key:
        env_upsert(CONSOLE_ENV, "WORKFLOW_LLM_KEY", llm_key)
    # Feishu workflow table env left as optional placeholders (skipped if unset).
    for k in ("FEISHU_WORKFLOW_APP_TOKEN", "FEISHU_WORKFLOW_REPLY_TABLE", "FEISHU_WORKFLOW_POST_TABLE"):
        env_upsert(CONSOLE_ENV, k, os.environ.get(k, ""))


def migrate_write_db():
    run(["runuser", "-u", "x-write", "--", "env", f"PYTHONPATH={WRITE_LIVE}",
         "python3.11", "-c",
         "from x_write.config import Config; from x_write.db import Database; "
         "c=Config.load('/etc/x-write-service.json'); Database(c.database_path).migrate()"])


def wait_url(url, timeout=20):
    deadline = time.time() + timeout; last = "n/a"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.getcode() == 200: return
        except Exception as e: last = type(e).__name__
        time.sleep(0.5)
    raise DeployError(f"{url} not healthy: {last}")


def verify(before_w, before_c):
    wait_url("http://127.0.0.1:8791/health"); wait_url("http://127.0.0.1:8790/healthz")
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    env = {}
    for line in CONSOLE_ENV.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1); env[k] = v
    form = urllib.parse.urlencode({"password": env["X_CONSOLE_ADMIN_PASSWORD"]}).encode()
    op.open(urllib.request.Request("http://127.0.0.1:8790/login", data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout=5).read()
    idx = op.open("http://127.0.0.1:8790/", timeout=5).read().decode()
    if 'id="view-workflow"' not in idx: raise DeployError("workflow view missing")
    csrf = idx.split('csrf-token" content="', 1)[1].split('"', 1)[0]
    def get(p): return json.loads(op.open("http://127.0.0.1:8790" + p, timeout=8).read())
    wf = get("/api/x/workflow/candidates?limit=5")["data"]
    wstatus = get("/api/x-write/status")["data"]
    overview = get("/api/x/overview")["data"]
    after_w = write_state(); after_c = console_state()
    if not after_w["paused"]: raise DeployError("write pause lost")
    if after_w["accounts"] != before_w["accounts"] or after_w["queued"] != before_w["queued"]:
        raise DeployError(f"write state changed: {after_w}")
    if not after_w["has_scheduled_at"]: raise DeployError("scheduled_at migration missing")
    if after_c["schedule_paused"] != before_c["schedule_paused"]: raise DeployError("schedule pause changed")
    if after_c["active_jobs"] != 0: raise DeployError("browse jobs appeared")
    if not after_c["has_workflow_items"]: raise DeployError("workflow_items table missing")
    if overview["system"]["app_version"] != "1.5.0": raise DeployError("console version wrong")
    if wstatus.get("global_write_paused") is not True or wstatus.get("write_execution_available") is not False:
        raise DeployError("write execution unexpectedly available")
    return {"candidates_count": wf["count"], "write": after_w, "console": after_c}


def rollback():
    if active("x-browse-console"): stop("x-browse-console")
    if active("x-write-service"): stop("x-write-service")
    req = (WRITE_BACKUP / "BACKUP_COMPLETE", CONSOLE_BACKUP / "BACKUP_COMPLETE",
           WRITE_BACKUP / "x_write", WRITE_BACKUP / "x-write-service.json", WRITE_BACKUP / "write.db",
           CONSOLE_BACKUP / "app.py", CONSOLE_BACKUP / "static", CONSOLE_BACKUP / "templates",
           CONSOLE_BACKUP / "x-browse-console.env", CONSOLE_BACKUP / "console.db")
    if any(not p.exists() for p in req): raise DeployError("rollback backup incomplete")
    copy_tree(WRITE_BACKUP / "x_write", WRITE_LIVE / "x_write"); normalize(WRITE_LIVE / "x_write")
    shutil.copy2(WRITE_BACKUP / "x-write-service.json", WRITE_CONFIG)
    wa = pwd.getpwnam("x-write")
    for suf in ("-wal", "-shm"):
        try: Path(str(WRITE_DB) + suf).unlink()
        except FileNotFoundError: pass
    sqlite_backup(WRITE_BACKUP / "write.db", WRITE_DB)
    os.chown(WRITE_DB, wa.pw_uid, wa.pw_gid); os.chmod(WRITE_DB, 0o600)
    shutil.copy2(CONSOLE_BACKUP / "app.py", CONSOLE_LIVE / "app.py"); os.chown(CONSOLE_LIVE / "app.py", 0, 0); os.chmod(CONSOLE_LIVE / "app.py", 0o644)
    copy_tree(CONSOLE_BACKUP / "static", CONSOLE_LIVE / "static")
    copy_tree(CONSOLE_BACKUP / "templates", CONSOLE_LIVE / "templates")
    normalize(CONSOLE_LIVE / "static"); normalize(CONSOLE_LIVE / "templates")
    for n in ("workflow_llm.py", "feishu_records.py"):
        try: (CONSOLE_LIVE / n).unlink()
        except FileNotFoundError: pass
    shutil.copy2(CONSOLE_BACKUP / "x-browse-console.env", CONSOLE_ENV)
    ca = pwd.getpwnam("x-browse-console")
    for suf in ("-wal", "-shm"):
        try: Path(str(CONSOLE_DB) + suf).unlink()
        except FileNotFoundError: pass
    sqlite_backup(CONSOLE_BACKUP / "console.db", CONSOLE_DB)
    os.chown(CONSOLE_DB, ca.pw_uid, ca.pw_gid); os.chmod(CONSOLE_DB, 0o600)
    run(["systemctl", "start", "x-write-service"]); wait_url("http://127.0.0.1:8791/health")
    run(["systemctl", "start", "x-browse-console"]); wait_url("http://127.0.0.1:8790/healthz")


def main():
    bw, bc = write_state(), console_state()
    if bw["integrity"] != "ok" or not bw["paused"]: raise DeployError("write db unsafe")
    if bc["integrity"] != "ok" or bc["schedule_paused"] != "1" or bc["active_jobs"]: raise DeployError("console unsafe")
    if not active("x-write-service") or not active("x-browse-console"): raise DeployError("services not active")
    backup_done = False; stopped = False; deployed = False
    try:
        backup(); backup_done = True
        stop("x-browse-console"); stop("x-write-service"); stopped = True
        sqlite_backup(WRITE_DB, WRITE_BACKUP / "write.db.pre")
        sqlite_backup(CONSOLE_DB, CONSOLE_BACKUP / "console.db.pre")
        deploy_files(); configure_env(); migrate_write_db(); deployed = True
        run(["systemctl", "start", "x-write-service"]); wait_url("http://127.0.0.1:8791/health")
        if write_state()["paused"] is not True: raise DeployError("write started unpaused")
        run(["systemctl", "start", "x-browse-console"]); wait_url("http://127.0.0.1:8790/healthz")
        result = verify(bw, bc)
        result.update({"status": "deployed", "write_backup": str(WRITE_BACKUP), "console_backup": str(CONSOLE_BACKUP),
                       "executor_enabled": False, "https_callback_remaining": True})
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as de:
        if backup_done and (stopped or deployed):
            try: rollback()
            except Exception as re: raise DeployError(f"deploy+rollback failed: {re}") from de
        raise


if __name__ == "__main__":
    main()
