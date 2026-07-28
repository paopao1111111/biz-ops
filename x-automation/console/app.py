#!/usr/bin/env python3
import os, sqlite3, json, hmac, hashlib, time, threading, logging, base64, secrets, urllib.parse, urllib.request, urllib.error, html, re, ipaddress
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http import cookies

ROOT=Path(__file__).resolve().parent
DB=os.getenv("X_CONSOLE_DB","/opt/x-browse-console/data/console.db")
HOST=os.getenv("X_CONSOLE_HOST","127.0.0.1"); PORT=int(os.getenv("X_CONSOLE_PORT","8790"))
ADMIN=os.getenv("X_CONSOLE_ADMIN_PASSWORD",""); SESSION=os.getenv("X_CONSOLE_SESSION_SECRET",""); WORKER=os.getenv("X_CONSOLE_WORKER_SECRET","")
try:MAX_CONCURRENCY=max(1,min(3,int(os.getenv("X_CONSOLE_MAX_CONCURRENCY","3"))))
except ValueError:MAX_CONCURRENCY=3
TZ=ZoneInfo("Asia/Shanghai"); LOCK=threading.RLock(); STOP=threading.Event(); LOG=logging.getLogger("x-console")
XWRITE_URL=os.getenv("X_CONSOLE_XWRITE_URL","http://127.0.0.1:8765").rstrip("/"); XWRITE_SECRET=os.getenv("X_CONSOLE_XWRITE_SECRET","")
SECURE_COOKIE=os.getenv("X_CONSOLE_SECURE_COOKIE","0").strip().lower() in ("1","true","yes","on")
def env_int(name,default,low,high):
 try:return max(low,min(high,int(os.getenv(name,str(default)))))
 except ValueError:return default
LOGIN_RATE_LIMIT=env_int("X_CONSOLE_LOGIN_RATE_LIMIT",10,1,1000);LOGIN_RATE_WINDOW=env_int("X_CONSOLE_LOGIN_RATE_WINDOW",300,10,3600)
OAUTH_RATE_LIMIT=env_int("X_CONSOLE_OAUTH_RATE_LIMIT",60,1,10000);OAUTH_RATE_WINDOW=env_int("X_CONSOLE_OAUTH_RATE_WINDOW",60,10,3600)
RATE_LOCK=threading.Lock();RATE_BUCKETS={}
VERSION="1.5.0"
TERMINAL=("succeeded","partial","cancelled","failed","manual_action_required")
ACTIVE=("queued","leased","running","cancel_requested","stopping","cleaning","quarantined")
WORKING=("leased","running","cancel_requested","stopping","cleaning")
OCCUPYING=("leased","running","cancel_requested","stopping","cleaning","quarantined")
FAILURE_CODES={"manual_cancel","manual_profile_in_use","authentication","account_challenge","handle_mismatch","configuration","infrastructure","network","proxy","browser","worker_lost","lease_expired","dom","source","timeout","webdriver_frozen","no_forward_progress","hard_runtime_exceeded","browser_start_failed","adspower_unavailable","controller_unavailable","browser_crashed","cleanup_uncertain","unknown"}
RETRYABLE_CODES={"infrastructure","network","proxy","browser","worker_lost","lease_expired","dom","source","timeout","webdriver_frozen","no_forward_progress","hard_runtime_exceeded","browser_start_failed","adspower_unavailable","controller_unavailable","browser_crashed"}
SEEDS=[(10,"k1euo8fi","Pixel Mara","@Slaxelr",10810,"logged_in"),(11,"k1euo8xv","Sato Reyes","@PixelMarayv12",10811,"logged_in"),(12,"k1euo99l","Kernel Finn","@SamiKaliber",10812,"logged_in"),(13,"k1euo9qv","Ink Sullivan","@Alper0zfd",10813,"logged_in"),(14,"k1euo9xd","Justice Wren","@RabiaKarabwqkb",10814,"logged_in"),(15,"k1euoaa0","Echo Vale","@SeherLles5u3",10815,"logged_in"),(22,"k1f2qx1l","Earl Leedy","@EarlLeedy3",10810,"pending_manual_login"),(23,"k1evtcb8","New X 02","",10811,"pending_manual_login"),(24,"k1evtcd0","New X 03","",10812,"pending_manual_login"),(25,"k1evtcen","New X 04","",10813,"pending_manual_login"),(26,"k1evtcga","New X 05","",10814,"pending_manual_login")]
def now():return int(time.time())
def rate_allowed(scope,key,limit,window):
 n=now();bucket_key=(scope,key)
 with RATE_LOCK:
  bucket=[stamp for stamp in RATE_BUCKETS.get(bucket_key,[]) if stamp>n-window]
  if len(bucket)>=limit:RATE_BUCKETS[bucket_key]=bucket;return False
  bucket.append(n);RATE_BUCKETS[bucket_key]=bucket
  if len(RATE_BUCKETS)>2048:
   for old_key,stamps in list(RATE_BUCKETS.items()):
    if not stamps or stamps[-1]<=n-max(LOGIN_RATE_WINDOW,OAUTH_RATE_WINDOW):RATE_BUCKETS.pop(old_key,None)
  return True
def today():return datetime.now(TZ).strftime("%Y-%m-%d")
def jd(x):return json.dumps(x,ensure_ascii=False,separators=(",",":"),sort_keys=True)
def clamp_capacity(value):
 try:return max(1,min(MAX_CONCURRENCY,int(value)))
 except (TypeError,ValueError):return 1
def conn():
 c=sqlite3.connect(DB,timeout=30,isolation_level=None);c.row_factory=sqlite3.Row;c.execute("PRAGMA foreign_keys=ON");c.execute("PRAGMA busy_timeout=30000");return c
def close_rollback(c):
 if c:
  try:c.rollback()
  except Exception:pass
  c.close()
def migrate(c,table,columns):
 existing={r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
 for name,definition in columns.items():
  if name not in existing:c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
def init():
 Path(DB).parent.mkdir(parents=True,exist_ok=True);c=conn();c.execute("PRAGMA journal_mode=WAL")
 c.executescript('''CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS accounts(id INTEGER PRIMARY KEY,profile_id TEXT UNIQUE NOT NULL,persona_label TEXT NOT NULL,expected_handle TEXT NOT NULL,proxy_port INTEGER NOT NULL,login_status TEXT NOT NULL,platform TEXT NOT NULL DEFAULT "x",device TEXT NOT NULL DEFAULT "Mac",group_name TEXT NOT NULL DEFAULT "Flower",os_name TEXT NOT NULL DEFAULT "Mac OS X",mode TEXT NOT NULL DEFAULT "read_only",daily_budget_seconds INTEGER NOT NULL DEFAULT 3600,timezone TEXT NOT NULL DEFAULT "Asia/Shanghai",daily_window_start TEXT NOT NULL DEFAULT "09:00",daily_window_end TEXT NOT NULL DEFAULT "22:30",schedule_priority INTEGER NOT NULL DEFAULT 100,auto_schedule_enabled INTEGER NOT NULL DEFAULT 0,keywords_json TEXT NOT NULL DEFAULT "[]",selected_count INTEGER NOT NULL DEFAULT 2,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS daily_plans(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,plan_date TEXT NOT NULL,budget_seconds INTEGER NOT NULL,reserved_seconds INTEGER NOT NULL DEFAULT 0,used_seconds INTEGER NOT NULL DEFAULT 0,attempted INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(account_id,plan_date),FOREIGN KEY(account_id) REFERENCES accounts(id)); CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY,account_id INTEGER NOT NULL,plan_id INTEGER NOT NULL,job_type TEXT NOT NULL CHECK(job_type IN ("browse","probe")),origin TEXT NOT NULL,status TEXT NOT NULL,reserved_seconds INTEGER NOT NULL DEFAULT 0,actual_seconds INTEGER NOT NULL DEFAULT 0,config_snapshot TEXT NOT NULL,started_at INTEGER,finished_at INTEGER,error TEXT,observed_handle TEXT,exit_ip TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,FOREIGN KEY(account_id) REFERENCES accounts(id),FOREIGN KEY(plan_id) REFERENCES daily_plans(id)); CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY,run_id INTEGER UNIQUE NOT NULL,account_id INTEGER NOT NULL,job_type TEXT NOT NULL CHECK(job_type IN ("browse","probe")),status TEXT NOT NULL,worker_id TEXT,lease_expires_at INTEGER,cancel_requested INTEGER NOT NULL DEFAULT 0,last_progress_at INTEGER,elapsed_seconds INTEGER NOT NULL DEFAULT 0,search_count INTEGER NOT NULL DEFAULT 0,trending_count INTEGER NOT NULL DEFAULT 0,unique_items INTEGER NOT NULL DEFAULT 0,phase TEXT,source TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,FOREIGN KEY(run_id) REFERENCES runs(id),FOREIGN KEY(account_id) REFERENCES accounts(id)); CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,job_id INTEGER NOT NULL,created_at INTEGER NOT NULL,event_type TEXT NOT NULL,payload_json TEXT NOT NULL); CREATE TABLE IF NOT EXISTS items(id INTEGER PRIMARY KEY,run_id INTEGER NOT NULL,account_id INTEGER NOT NULL,source TEXT,item_key TEXT,author_handle TEXT,text TEXT,url TEXT,observed_at INTEGER NOT NULL,payload_json TEXT NOT NULL,UNIQUE(run_id,item_key)); CREATE TABLE IF NOT EXISTS workers(worker_id TEXT PRIMARY KEY,status TEXT NOT NULL,last_seen_at INTEGER NOT NULL,current_job_id INTEGER,details_json TEXT NOT NULL DEFAULT "{}"); CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,created_at INTEGER NOT NULL,actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT,payload_json TEXT NOT NULL); CREATE TABLE IF NOT EXISTS workflow_items(item_key TEXT PRIMARY KEY,account_id INTEGER,source TEXT,author_handle TEXT,text TEXT,url TEXT,observed_at INTEGER,status TEXT NOT NULL DEFAULT 'candidate',write_request_id INTEGER,draft_text TEXT,analysis_json TEXT NOT NULL DEFAULT '{}',llm_model TEXT,note TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL); CREATE INDEX IF NOT EXISTS workflow_status ON workflow_items(status,updated_at); CREATE INDEX IF NOT EXISTS workflow_sent ON workflow_items(status,item_key); CREATE TABLE IF NOT EXISTS postflow_topics(topic_key TEXT PRIMARY KEY,account_id INTEGER NOT NULL,source_item_keys_json TEXT NOT NULL DEFAULT '[]',keyword TEXT,theme TEXT NOT NULL,key_points_json TEXT NOT NULL DEFAULT '[]',angles_json TEXT NOT NULL DEFAULT '[]',suggested_links_json TEXT NOT NULL DEFAULT '[]',risk TEXT,analysis_json TEXT NOT NULL DEFAULT '{}',status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','selected','consumed','skipped')),llm_model TEXT,note TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL); CREATE INDEX IF NOT EXISTS postflow_topics_status ON postflow_topics(account_id,status); CREATE TABLE IF NOT EXISTS postflow_drafts(draft_key TEXT PRIMARY KEY,topic_key TEXT NOT NULL,account_id INTEGER NOT NULL,post_text TEXT,link TEXT,media_asset_id INTEGER,scheduled_at INTEGER,write_request_id INTEGER NOT NULL DEFAULT 0,status TEXT NOT NULL DEFAULT 'drafting' CHECK(status IN ('drafting','draft_ready','approved','sent','failed','skipped')),llm_model TEXT,note TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL); CREATE INDEX IF NOT EXISTS postflow_drafts_status ON postflow_drafts(topic_key,status); CREATE INDEX IF NOT EXISTS postflow_drafts_pending ON postflow_drafts(status,updated_at); CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);''')
 migrate(c,"accounts",{"observed_handle":"TEXT","proxy_status":"TEXT NOT NULL DEFAULT 'unknown'","last_exit_ip":"TEXT","last_proxy_check_at":"INTEGER","last_login_check_at":"INTEGER","last_error":"TEXT","last_run_at":"INTEGER"})
 migrate(c,"workers",{"capacity":"INTEGER NOT NULL DEFAULT 1"})
 migrate(c,"jobs",{"execution_generation":"INTEGER NOT NULL DEFAULT 0","execution_token":"TEXT","execution_state":"TEXT","cleanup_state":"TEXT NOT NULL DEFAULT 'none'","cleanup_confirmed_at":"INTEGER","last_forward_progress_at":"INTEGER","quarantine_reason":"TEXT","terminal_code":"TEXT"})
 migrate(c,"runs",{"retry_of_run_id":"INTEGER","retry_number":"INTEGER NOT NULL DEFAULT 0","failure_class":"TEXT","retry_eligible":"INTEGER NOT NULL DEFAULT 0","retry_not_before":"INTEGER","failure_code":"TEXT","failure_detail":"TEXT","cleanup_confirmed":"INTEGER NOT NULL DEFAULT 0","retry_block_reason":"TEXT"})
 c.executescript('''CREATE INDEX IF NOT EXISTS jobs_active_account ON jobs(account_id,status); CREATE INDEX IF NOT EXISTS jobs_worker_active ON jobs(worker_id,status); CREATE INDEX IF NOT EXISTS runs_retry_due ON runs(retry_eligible,retry_not_before,retry_number); CREATE INDEX IF NOT EXISTS runs_retry_parent ON runs(retry_of_run_id,retry_number);''')
 try:c.execute("CREATE UNIQUE INDEX IF NOT EXISTS runs_retry_unique ON runs(retry_of_run_id,retry_number) WHERE retry_of_run_id IS NOT NULL")
 except sqlite3.IntegrityError:LOG.warning("retry uniqueness deferred because duplicate historical rows exist")
 n=now();c.execute("INSERT OR IGNORE INTO settings VALUES('global_schedule_paused','1',?)",(n,))
 for a in SEEDS:c.execute("INSERT OR IGNORE INTO accounts(id,profile_id,persona_label,expected_handle,proxy_port,login_status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",(*a,n,n))
 c.close()
def plan(c,aid):
 n=now();c.execute("INSERT OR IGNORE INTO daily_plans(account_id,plan_date,budget_seconds,created_at,updated_at) SELECT id,?,daily_budget_seconds,?,? FROM accounts WHERE id=?",(today(),n,n,aid));return c.execute("SELECT * FROM daily_plans WHERE account_id=? AND plan_date=?",(aid,today())).fetchone()
def acc(c,aid):
 a=c.execute("SELECT * FROM accounts WHERE id=?",(aid,)).fetchone()
 if not a:raise E(404,"account_not_found","Account not found")
 return a
def paused(c):
 r=c.execute("SELECT value FROM settings WHERE key='global_schedule_paused'").fetchone();return not r or r[0]=="1"
def readonly_assert(value,path="snapshot"):
 if isinstance(value,dict):
  for k,v in value.items():
   if str(k).lower() in {"click","like","follow","reply","post","retweet","repost","send","write","interaction","interactions","publish","comment","action","actions"} and v not in (False,None,0,"",[],{}):raise E(500,"unsafe_snapshot",f"Non-read-only field at {path}.{k}")
   readonly_assert(v,f"{path}.{k}")
 elif isinstance(value,list):
  for i,v in enumerate(value):readonly_assert(v,f"{path}[{i}]")
def snapshot(a,kind):
 kws=json.loads(a["keywords_json"] or "[]")[:a["selected_count"]];d=today();rng=lambda k,l,h:l+int(hashlib.sha256(k.encode()).hexdigest()[:8],16)%(h-l+1)
 s={"job_type":kind,"read_only":True,"operational_timeout_seconds":300 if kind=="probe" else None,"keywords":[{"keyword":k,"target":rng(f"{d}:{a['id']}:{k}",10,20)} for k in kws],"trending_target":rng(f"{d}:{a['id']}:trending",40,50),"dwell_seconds":[20,45],"between_actions_seconds":[1,4],"after_scroll_seconds":[2,5],"max_failures":3};readonly_assert(s);return s
def worker_details(w):
 try:d=json.loads(w["details_json"] or "{}")
 except Exception:d={}
 for key in ("active_job_ids","active_profile_ids","active_proxy_ports","capabilities","executions"):
  if not isinstance(d.get(key),list):d[key]=[]
 if d.get("current_job_id") is not None and d["current_job_id"] not in d["active_job_ids"]:d["active_job_ids"].append(d["current_job_id"])
 return d
def worker_protocol(c,w):
 row=c.execute("SELECT * FROM workers WHERE worker_id=?",(w,)).fetchone();d=worker_details(row) if row else {};return row,d,bool("execution_tokens" in d.get("capabilities",[]))
def worker_availability(w,n=None):
 if not w:return "offline"
 age=(n or now())-int(w["last_seen_at"]);return "online" if age<=90 else "stale" if age<=300 else "offline"
def heartbeat_execution_ids(d):
 result=[]
 for x in d.get("executions",[]):
  if isinstance(x,dict):
   try:result.append(int(x.get("job_id")))
   except (TypeError,ValueError):pass
 return result
def authoritative_workload(c,n=None,exclude_job_id=None):
 n=n or now();workers=[];worker_by_id={};reported={};profiles=set();ports=set();accounts=set();job_ids=set();mismatches=[]
 for w in c.execute("SELECT * FROM workers ORDER BY last_seen_at DESC"):
  d=worker_details(w);availability=worker_availability(w,n);cap=clamp_capacity(w["capacity"]) if availability=="online" else 0
  ids=set(heartbeat_execution_ids(d)) if d.get("executions") else {int(x) for x in d["active_job_ids"] if str(x).isdigit()}
  if exclude_job_id is not None:ids.discard(int(exclude_job_id))
  for x in d.get("executions",[]):
   if not isinstance(x,dict):continue
   try:xid=int(x.get("job_id"))
   except (TypeError,ValueError):xid=None
   if exclude_job_id is not None and xid==int(exclude_job_id):continue
   if x.get("profile_id") is not None:profiles.add(str(x["profile_id"]))
   if x.get("proxy_port") is not None:
    try:ports.add(int(x["proxy_port"]))
    except (TypeError,ValueError):pass
   if x.get("account_id") is not None:
    try:accounts.add(int(x["account_id"]))
    except (TypeError,ValueError):pass
  profiles.update(str(x) for x in d.get("active_profile_ids",[]) if x is not None)
  for x in d.get("active_proxy_ports",[]):
   try:ports.add(int(x))
   except (TypeError,ValueError):pass
  worker_by_id[w["worker_id"]]=(w,d,availability,cap);reported[w["worker_id"]]=ids
  workers.append({"worker_id":w["worker_id"],"availability":availability,"status":w["status"],"capacity":cap,"protocol_version":d.get("protocol_version"),"capabilities":d.get("capabilities",[]),"reported_execution_ids":sorted(ids),"last_seen_at":w["last_seen_at"]})
 jobs=[]
 sql='''SELECT j.*,a.profile_id,a.proxy_port FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined')'''
 args=[]
 if exclude_job_id is not None:sql+=' AND j.id<>?';args.append(exclude_job_id)
 sql+=' ORDER BY j.id'
 for j in c.execute(sql,args):
  state="queued";availability="offline";mismatch=None
  if j["status"]=="quarantined" or j["quarantine_reason"]:state="quarantined"
  elif j["status"] in ("stopping","cleaning") or j["cleanup_state"] in ("requested","cleaning","uncertain"):state="cleaning"
  elif j["status"]=="running":state="running"
  elif j["status"] in ("leased","cancel_requested"):state="starting" if j["status"]=="leased" else "cleaning"
  if j["worker_id"] in worker_by_id:availability=worker_by_id[j["worker_id"]][2]
  if j["status"]=="queued":
   profiles.add(j["profile_id"]);ports.add(int(j["proxy_port"]));accounts.add(j["account_id"]);job_ids.add(j["id"])
  else:
   seen=j["id"] in reported.get(j["worker_id"],set())
   if availability=="online" and not seen:mismatch="db_active_heartbeat_missing"
   elif availability!="online":mismatch="worker_"+availability
   if seen or j["status"] in OCCUPYING:
    profiles.add(j["profile_id"]);ports.add(int(j["proxy_port"]));accounts.add(j["account_id"]);job_ids.add(j["id"])
  jobs.append({"job_id":j["id"],"run_id":j["run_id"],"account_id":j["account_id"],"profile_id":j["profile_id"],"proxy_port":j["proxy_port"],"state":state,"db_status":j["status"],"worker_id":j["worker_id"],"worker_availability":availability,"heartbeat_match":mismatch is None,"mismatch":mismatch,"quarantine_reason":j["quarantine_reason"],"execution_generation":j["execution_generation"]})
  if mismatch:mismatches.append({"job_id":j["id"],"worker_id":j["worker_id"],"reason":mismatch})
 for wid,ids in reported.items():
  for jid in ids:
   row=c.execute("SELECT status FROM jobs WHERE id=?",(jid,)).fetchone()
   if not row or row["status"] in TERMINAL:
    mismatches.append({"job_id":jid,"worker_id":wid,"reason":"heartbeat_ghost_or_terminal"});job_ids.add(jid)
 fresh_capacity=sum(x[3] for x in worker_by_id.values() if x[2]=="online");capacity=min(MAX_CONCURRENCY,fresh_capacity);occupied=min(MAX_CONCURRENCY,len(job_ids));available=max(0,min(capacity-occupied,MAX_CONCURRENCY-occupied))
 return {"states":{"running":sum(x["state"]=="running" for x in jobs),"starting":sum(x["state"]=="starting" for x in jobs),"cleaning":sum(x["state"]=="cleaning" for x in jobs),"queued":sum(x["state"]=="queued" for x in jobs),"quarantined":sum(x["state"]=="quarantined" for x in jobs),"available":available},"capacity":capacity,"occupied":occupied,"available_slots":available,"active_job_ids":sorted(job_ids),"active_profile_ids":sorted(profiles),"active_proxy_ports":sorted(ports),"active_account_ids":sorted(accounts),"workers":workers,"jobs":jobs,"mismatches":mismatches}
def fresh_workers(c,n=None):return c.execute("SELECT * FROM workers WHERE last_seen_at>=? ORDER BY last_seen_at DESC",((n or now())-90,)).fetchall()
def fresh_capacity(c,n=None):return authoritative_workload(c,n)["capacity"]
def fresh_slots(c,n=None):return authoritative_workload(c,n)["available_slots"]
def active_occupants(c,exclude_job_id=None):
 sql='''SELECT j.*,a.profile_id,a.proxy_port FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined')''';args=[]
 if exclude_job_id is not None:sql+=" AND j.id<>?";args.append(exclude_job_id)
 return c.execute(sql,args).fetchall()
def active_proxy_ports(c,exclude_job_id=None):return {int(x["proxy_port"]):int(x["account_id"]) for x in active_occupants(c,exclude_job_id)}
def worker_active_count(c,w):
 d=worker_details(w);db=c.execute("SELECT COUNT(*) FROM jobs WHERE worker_id=? AND status IN ('leased','running','cancel_requested','stopping','cleaning','quarantined')",(w["worker_id"],)).fetchone()[0];return max(db,len(heartbeat_execution_ids(d)),len(d["active_job_ids"]),len(d["active_profile_ids"]),len(d["active_proxy_ports"]))
def worker_slots(c,w):
 cap=clamp_capacity(w["capacity"]);d=worker_details(w)
 if d.get("draining"):return 0
 reported=d.get("available_slots");local=max(0,cap-worker_active_count(c,w))
 if type(reported) is int:return max(0,min(local,reported))
 return local
def admission(c,a,manual=False,exclude_job_id=None):
 if c.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined')"+(" AND id<>?" if exclude_job_id is not None else ""),(a["id"],exclude_job_id) if exclude_job_id is not None else (a["id"],)).fetchone():raise E(409,"account_active","Account already has an active or quarantined run")
 workload=authoritative_workload(c,exclude_job_id=exclude_job_id)
 if workload["capacity"]<=0:raise E(409,"worker_offline","No fresh worker capacity is available")
 available=workload["available_slots"]
 if exclude_job_id is not None and any(int(x)==int(exclude_job_id) for x in workload["active_job_ids"]):available=min(workload["capacity"],available+1)
 if available<=0:raise E(409,"capacity_full","All authoritative worker slots are occupied")
 if a["profile_id"] in workload["active_profile_ids"]:raise E(409,"profile_busy","Profile is active or quarantined")
 owner=active_proxy_ports(c,exclude_job_id).get(int(a["proxy_port"]))
 if owner is not None and owner!=a["id"]:raise E(409,"proxy_port_busy","Proxy port is active or quarantined for another account")
 return workload["available_slots"]
def create(c,a,seconds,origin,kind="browse",retry_of=None,retry_number=0,manual=False):
 admission(c,a,manual);p=plan(c,a["id"]);reserve=0 if kind=="probe" else min(max(0,int(seconds)),p["budget_seconds"]-p["used_seconds"]-p["reserved_seconds"])
 if kind!="probe" and reserve<=0:raise E(409,"budget_exhausted","No daily budget remains")
 n=now()
 if kind!="probe":
  c.execute("UPDATE daily_plans SET reserved_seconds=reserved_seconds+?,attempted=1,updated_at=? WHERE id=? AND used_seconds+reserved_seconds+?<=budget_seconds",(reserve,n,p["id"],reserve))
  if c.execute("SELECT changes()").fetchone()[0]!=1:raise E(409,"budget_exhausted","No daily budget remains")
 try:rid=c.execute('INSERT INTO runs(account_id,plan_id,job_type,origin,status,reserved_seconds,config_snapshot,retry_of_run_id,retry_number,created_at,updated_at) VALUES(?,?,?,?,"queued",?,?,?,?,?,?)',(a["id"],p["id"],kind,origin,reserve,jd(snapshot(a,kind)),retry_of,retry_number,n,n)).lastrowid
 except sqlite3.IntegrityError:
  if kind!="probe":c.execute("UPDATE daily_plans SET reserved_seconds=MAX(0,reserved_seconds-?),updated_at=? WHERE id=?",(reserve,n,p["id"]))
  raise E(409,"retry_exists","Retry already exists")
 jid=c.execute('INSERT INTO jobs(run_id,account_id,job_type,status,execution_state,created_at,updated_at) VALUES(?,?,?,"queued","queued",?,?)',(rid,a["id"],kind,n,n)).lastrowid;return rid,jid,reserve
def classify_failure(status,error,manual_cancel=False):
 text=str(error or "").lower()
 if manual_cancel or status=="cancelled" or any(x in text for x in ("cancelled by administrator","manual cancel")):return "manual_cancel",False
 if "mismatch" in text:return "handle_mismatch",False
 if status=="manual_action_required" or any(x in text for x in ("login","challenge","captcha","manual action","budget","configuration","config error")):return "authentication",False
 phrases={"infrastructure":("connection reset","connection refused","service unavailable","worker unavailable"),"timeout":("timed out","timeout"),"browser":("browser crashed","browser disconnected","websocket"),"network":("network error",),"proxy":("proxy connection",),"dom":("selector not found","element not found","dom changed","stale element"),"source":("source unavailable","failed to load source","timeline unavailable","search unavailable","empty source")}
 for code,values in phrases.items():
  if any(x in text for x in values):return code,code in RETRYABLE_CODES
 return ("unknown",False) if status in ("failed","partial") else (None,False)
def failure_values(status,error,code=None,detail=None):
 code=str(code or "").strip().lower()
 if code not in FAILURE_CODES:code,eligible=classify_failure(status,error)
 else:eligible=code in RETRYABLE_CODES
 return code,detail or error,eligible
def finish(c,j,status,actual,error=None,handle=None,ip=None,proxy_status=None,login_status=None,failure_class=None,retry_eligible=None,retry_delay=None,failure_code=None,failure_detail=None,cleanup_confirmed=None,terminal_code=None):
 r=c.execute("SELECT * FROM runs WHERE id=?",(j["run_id"],)).fetchone()
 if not r or r["status"] in TERMINAL:return False
 actual=0 if r["job_type"]=="probe" else max(0,min(max(int(actual or 0),int(j["elapsed_seconds"] or 0)),r["reserved_seconds"]));n=now();confirmed=bool(cleanup_confirmed if cleanup_confirmed is not None else j["cleanup_confirmed_at"])
 c.execute("UPDATE daily_plans SET reserved_seconds=MAX(0,reserved_seconds-?),used_seconds=MIN(budget_seconds,used_seconds+?),updated_at=? WHERE id=?",(r["reserved_seconds"],actual,n,r["plan_id"]))
 expected=c.execute("SELECT expected_handle FROM accounts WHERE id=?",(j["account_id"],)).fetchone()[0];normalize=lambda v:str(v or "").strip().lstrip("@").lower();mismatch=bool(handle and expected and normalize(handle)!=normalize(expected));final_error=error or (f"Handle mismatch: expected {expected}, observed {handle}" if mismatch else None);final_status="manual_action_required" if mismatch else status
 code,detail,structured_eligible=failure_values(final_status,final_error,"handle_mismatch" if mismatch else failure_code,failure_detail)
 if failure_class is None:failure_class=code
 if retry_eligible is None:retry_eligible=structured_eligible
 cleanup_uncertain=cleanup_confirmed is False and final_status in ("failed","partial","cancelled")
 eligible=bool(retry_eligible and confirmed and not cleanup_uncertain and r["job_type"]=="browse" and int(r["retry_number"] or 0)==0 and final_status in ("failed","partial") and code in RETRYABLE_CODES)
 block="cleanup_uncertain" if cleanup_uncertain else "global_pause" if eligible and paused(c) else "probe_no_retry" if r["job_type"]=="probe" else "retry_limit" if int(r["retry_number"] or 0)>0 else None;retry_at=n+(retry_delay if retry_delay is not None else 600) if eligible else None
 c.execute("UPDATE runs SET status=?,actual_seconds=?,finished_at=?,updated_at=?,error=?,observed_handle=COALESCE(?,observed_handle),exit_ip=COALESCE(?,exit_ip),failure_class=?,failure_code=?,failure_detail=?,cleanup_confirmed=?,retry_eligible=?,retry_not_before=?,retry_block_reason=? WHERE id=?",(final_status,actual,n,n,final_error,handle,ip,failure_class,code,detail,int(confirmed),int(eligible),retry_at,block,r["id"]))
 quarantine=detail or final_error or "Cleanup was not confirmed" if cleanup_uncertain else None;job_status="quarantined" if cleanup_uncertain else final_status
 c.execute("UPDATE jobs SET status=?,execution_state=?,cleanup_state=?,cleanup_confirmed_at=CASE WHEN ? THEN COALESCE(cleanup_confirmed_at,?) ELSE cleanup_confirmed_at END,quarantine_reason=?,terminal_code=?,elapsed_seconds=MAX(elapsed_seconds,?),lease_expires_at=NULL,updated_at=? WHERE id=?",(job_status,job_status,"confirmed" if confirmed else "uncertain" if cleanup_uncertain else j["cleanup_state"],int(confirmed),n,quarantine,terminal_code or code,max(actual,int(j["elapsed_seconds"] or 0)),n,j["id"]))
 fields=["observed_handle=COALESCE(?,observed_handle)","last_exit_ip=COALESCE(?,last_exit_ip)","last_error=?","last_run_at=?","updated_at=?"];values=[handle,ip,final_error,n,n]
 if proxy_status is not None:fields.extend(["proxy_status=?","last_proxy_check_at=?"]);values.extend([proxy_status,n])
 if login_status is not None:fields.extend(["login_status=?","last_login_check_at=?"]);values.extend([login_status,n])
 values.append(j["account_id"]);c.execute("UPDATE accounts SET "+",".join(fields)+" WHERE id=?",values);return True
class E(Exception):
 def __init__(self,s,c,m):self.status=s;self.code=c;self.message=m
class H(BaseHTTPRequestHandler):
 def log_message(self,f,*a):
  if urllib.parse.urlsplit(self.path).path=="/oauth/x/callback":LOG.info("OAuth callback request received")
  else:LOG.info(f,*a)
 def out(self,s,x,auth=True,ctype="application/json; charset=utf-8",extra=None):
  b=x if isinstance(x,bytes) else json.dumps(x,ensure_ascii=False,separators=(",",":")).encode();self.send_response(s);self.send_header("Content-Security-Policy","default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'");self.send_header("X-Content-Type-Options","nosniff");self.send_header("X-Frame-Options","DENY");self.send_header("Referrer-Policy","no-referrer")
  if SECURE_COOKIE:self.send_header("Strict-Transport-Security","max-age=31536000")
  if auth:self.send_header("Cache-Control","no-store")
  self.send_header("Content-Type",ctype);self.send_header("Content-Length",str(len(b)))
  for k,v in (extra or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(b)
 def error(self,e):self.out(e.status,{"ok":False,"error":{"code":e.code,"message":e.message}},e.status!=401)
 def body(self):
  try:n=int(self.headers.get("Content-Length","0"))
  except ValueError:raise E(400,"invalid_length","Invalid Content-Length")
  if n>2000000:raise E(413,"body_too_large","Request body too large")
  return self.rfile.read(n)
 def data(self,b):
  ct=self.headers.get("Content-Type","").split(";",1)[0].strip().lower()
  if ct!="application/json":raise E(415,"content_type","Content-Type must be application/json")
  try:d=json.loads(b or b"{}")
  except Exception:raise E(400,"invalid_json","Invalid JSON body")
  if not isinstance(d,dict):raise E(400,"invalid_json","JSON body must be an object")
  return d
 def parts(self):return [urllib.parse.unquote(x) for x in urllib.parse.urlsplit(self.path).path.split("/") if x]
 def session(self):
  try:q=cookies.SimpleCookie();q.load(self.headers.get("Cookie",""));t=q["x_console_session"].value;b,s=t.rsplit(".",1);p=json.loads(base64.urlsafe_b64decode(b+"="*(-len(b)%4)));return p if p["exp"]>=now() and hmac.compare_digest(s,hmac.new(SESSION.encode(),b.encode(),hashlib.sha256).hexdigest()) else None
  except Exception:return None
 def admin(self,csrf=False):
  s=self.session()
  if not s:raise E(401,"unauthorized","Authentication required")
  if csrf and not hmac.compare_digest(self.headers.get("X-CSRF-Token",""),s["csrf"]):raise E(403,"csrf","Invalid CSRF token")
  return s
 def wauth(self,b):
  w=self.headers.get("X-Worker-ID","");t=self.headers.get("X-Timestamp","");sig=self.headers.get("X-Signature","")
  try:valid=bool(w and WORKER and abs(now()-int(t))<=300)
  except Exception:valid=False
  msg=f"{t}\n{self.command}\n{urllib.parse.urlsplit(self.path).path}\n{hashlib.sha256(b).hexdigest()}"
  if not valid or not hmac.compare_digest(sig,hmac.new(WORKER.encode(),msg.encode(),hashlib.sha256).hexdigest()):raise E(401,"worker_auth","Worker authentication failed")
  return w
 def client_ip(self):
  direct=str(self.client_address[0])
  if direct in ("127.0.0.1","::1"):
   forwarded=self.headers.get("X-Forwarded-For","").split(",",1)[0].strip()
   if forwarded:
    try:return str(ipaddress.ip_address(forwarded))
    except ValueError:pass
  return direct
 def xwrite_upstream(self,method,suffix,data=None,forward_query=True):
  if not XWRITE_SECRET:raise E(503,"write_service_unconfigured","X write service is not configured on this console")
  if not isinstance(suffix,str) or not re.fullmatch(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*",suffix):raise E(404,"not_found","Not found")
  if any(part in (".","..") for part in suffix.split("/")):raise E(404,"not_found","Not found")
  path="/api"+suffix
  raw=jd(data).encode() if data is not None else b"";ts=str(now());nonce=secrets.token_hex(16)
  msg=f"{ts}\n{nonce}\n{method}\n{path}\n{hashlib.sha256(raw).hexdigest()}"
  sig=hmac.new(XWRITE_SECRET.encode(),msg.encode(),hashlib.sha256).hexdigest();query=urllib.parse.urlsplit(self.path).query if forward_query else ""
  req=urllib.request.Request(XWRITE_URL+path+("?"+query if query else ""),data=raw if method!="GET" else None,method=method)
  req.add_header("X-Internal-Timestamp",ts);req.add_header("X-Internal-Nonce",nonce);req.add_header("X-Internal-Signature",sig)
  if method!="GET":req.add_header("Content-Type","application/json")
  try:
   with urllib.request.urlopen(req,timeout=20) as r:status=r.getcode();payload=r.read(2000000)
  except urllib.error.HTTPError as e:status=e.code;payload=e.read(2000000)
  except Exception:raise E(502,"write_service_unavailable","X write service is unavailable")
  try:parsed=json.loads(payload.decode("utf-8")) if payload else {}
  except Exception:raise E(502,"write_service_bad_response","X write service returned an invalid response")
  if status==401:raise E(502,"write_service_auth_failed","Console to write-service authentication failed")
  if status>=400:
   code=parsed.get("error","write_error") if isinstance(parsed,dict) else "write_error";message=parsed.get("message",str(code)) if isinstance(parsed,dict) else str(code)
   raise E(status if 400<=status<600 else 502,str(code),str(message))
  return parsed
 def xwrite(self,method,suffix,body):
  self.admin(method!="GET")
  if method!="GET" and suffix in ("/oauth/app","/credentials/oauth1") and self.client_address[0] not in ("127.0.0.1","::1"):raise E(403,"https_required","Sensitive credentials must be submitted through the local HTTPS gateway")
  data=None
  if body is not None:data=self.data(body);data["actor"]="console-admin"
  self.out(200,{"ok":True,"data":self.xwrite_upstream(method,suffix,data)})
 def oauth_callback(self):
  if not rate_allowed("oauth-callback",self.client_ip(),OAUTH_RATE_LIMIT,OAUTH_RATE_WINDOW):return self.oauth_callback_page(False,"授权回调请求过多，请稍后回到控制面板重新生成链接。","rate_limited",429)
  raw_query=urllib.parse.urlsplit(self.path).query
  if len(raw_query)>4096:return self.oauth_callback_page(False,"授权参数过长，请重新发起连接。")
  try:values=urllib.parse.parse_qs(raw_query,keep_blank_values=True,strict_parsing=False,max_num_fields=8)
  except ValueError:return self.oauth_callback_page(False,"授权参数无效，请重新发起连接。")
  if set(values)-{"state","code","error","error_description"} or any(len(v)!=1 for v in values.values()):return self.oauth_callback_page(False,"授权参数无效或重复，请重新发起连接。")
  state=(values.get("state") or [""])[0];code=(values.get("code") or [None])[0];error=(values.get("error") or [None])[0]
  if (code is None)==(error is None):return self.oauth_callback_page(False,"授权结果不完整，请重新发起连接。")
  if not re.fullmatch(r"[A-Za-z0-9_-]{20,256}",state or ""):return self.oauth_callback_page(False,"授权状态无效或已过期，请返回控制面板重新生成链接。")
  if code is not None and not re.fullmatch(r"[A-Za-z0-9._~+/=-]{1,2048}",code):return self.oauth_callback_page(False,"授权结果格式无效，请重新发起连接。")
  if error is not None and (len(error)>80 or not re.fullmatch(r"[A-Za-z0-9._~-]+",error)):error="oauth_denied"
  payload={"state":state,"actor":"oauth-callback"}
  if code is not None:payload["code"]=code
  if error is not None:payload["error"]=error
  try:self.xwrite_upstream("POST","/oauth/callback",payload,False)
  except E as e:return self.oauth_callback_page(False,"授权未完成或链接已经失效。请关闭此页面并在控制面板重新生成链接。",e.code)
  return self.oauth_callback_page(True,"服务器已安全接收账号授权。该账号仍保持停用和暂停，请关闭此页面并回到控制面板继续。")
 def oauth_callback_page(self,success,message,code="",status=None):
  p=ROOT/"templates"/"oauth_callback.html"
  if not p.is_file():raise E(404,"template_missing","templates/oauth_callback.html not found")
  content=p.read_text().replace("{{OAUTH_TITLE}}","授权已接收" if success else "授权未完成").replace("{{OAUTH_MESSAGE}}",html.escape(message,quote=True)).replace("{{OAUTH_CODE}}",html.escape(code,quote=True)).replace("{{OAUTH_TONE}}","ok" if success else "error")
  self.out(status if status is not None else 200 if success else 400,content.encode(),True,"text/html; charset=utf-8")
 def do_GET(self):
  try:
   p=urllib.parse.urlsplit(self.path).path
   if p=="/healthz":return self.out(200,{"ok":True},False)
   if p=="/oauth/x/callback":return self.oauth_callback()
   if p=="/login":return self.login()
   if p.startswith("/static/"):return self.static(p[8:])
   if p.startswith("/api/x-write/"):return self.xwrite("GET",p[12:],None)
   if p.startswith("/api/worker/jobs/") and p.endswith("/control"):return self.control()
   if p=="/":
    s=self.session()
    if not s:return self.out(303,b"",False,"text/plain; charset=utf-8",{"Location":"/login"})
    return self.index(s)
   self.admin()
   if p=="/api/x/overview":return self.overview()
   if p=="/api/x/system-info":return self.system_info()
   q=self.parts()
   if q==["api","x","accounts"]:return self.accounts()
   if len(q)>=4 and q[:3]==["api","x","accounts"]:
    try:aid=int(q[3])
    except ValueError:raise E(404,"not_found","Not found")
    if len(q)==4:return self.account(aid)
    if len(q)==5 and q[4] in ("runs","items"):return self.listing(aid,q[4])
   if len(q)==4 and q[:3]==["api","x","runs"]:
    try:return self.run(int(q[3]))
    except ValueError:raise E(404,"not_found","Not found")
   if q==["api","x","workflow","candidates"]:return self.workflow_candidates()
   if q==["api","x","workflow","sync"]:return self.workflow_sync()
   if len(q)==6 and q[:4]==["api","x","workflow","items"]:return self.workflow_item_action(q[4],q[5])
   if q==["api","x","postflow","topics"]:return self.postflow_topics()
   if q==["api","x","postflow","sync"]:return self.postflow_sync()
   raise E(404,"not_found","Not found")
  except E as e:self.error(e)
  except Exception:LOG.exception("GET");self.error(E(500,"internal_error","Internal server error"))
 def do_POST(self):
  try:
   b=self.body();p=urllib.parse.urlsplit(self.path).path
   if p=="/login":return self.login_post(b)
   if p.startswith("/api/x-write/"):return self.xwrite("POST",p[12:],b)
   if p.startswith("/api/worker/"):return self.worker(p,b)
   self.admin(True)
   if p=="/logout":return self.logout()
   if p in ("/api/x/schedule/pause","/api/x/schedule/resume"):return self.gpause(p.endswith("pause"))
   q=self.parts()
   if len(q)==5 and q[:3]==["api","x","jobs"]:
    try:jid=int(q[3])
    except ValueError:raise E(404,"not_found","Not found")
    if q[4] in ("reconcile","cleanup"):return self.admin_job(jid,q[4],self.data(b))
   if len(q)==6 and q[:4]==["api","x","workflow","items"]:
    return self.workflow_item_action(q[4],q[5],b)
   if q==["api","x","postflow","summarize"]:return self.postflow_summarize(b)
   if len(q)==6 and q[:4]==["api","x","postflow","topics"]:
    return self.postflow_topic_action(q[4],q[5],b)
   if len(q)!=5 or q[:3]!=["api","x","accounts"]:raise E(404,"not_found","Not found")
   try:aid=int(q[3])
   except ValueError:raise E(404,"not_found","Not found")
   act=q[4]
   if act=="start":return self.start(aid,self.data(b))
   if act=="pause-schedule":return self.asched(aid,False)
   if act=="resume-schedule":return self.asched(aid,True)
   if act=="cancel-current":return self.cancel(aid)
   if act=="probe":return self.probe(aid)
   if act=="settings":return self.settings(aid,self.data(b))
   if act=="keywords":return self.keywords(aid,self.data(b))
   if act=="configuration":return self.configuration(aid,self.data(b))
   raise E(404,"not_found","Not found")
  except E as e:self.error(e)
  except Exception:LOG.exception("POST");self.error(E(500,"internal_error","Internal server error"))
 def login(self,msg="",status=200):
  p=ROOT/"templates"/"login.html"
  if not p.is_file():raise E(404,"template_missing","templates/login.html not found")
  content=p.read_text().replace("{{LOGIN_ERROR}}",html.escape(msg,quote=True)).replace("{{ERROR_CLASS}}","" if msg else "hidden");self.out(status,content.encode(),False,"text/html; charset=utf-8")
 def login_post(self,b):
  if self.headers.get("Content-Type","").split(";",1)[0].strip().lower()!="application/x-www-form-urlencoded":raise E(415,"content_type","Login requires form encoding")
  try:pwd=urllib.parse.parse_qs(b.decode("utf-8"),keep_blank_values=True).get("password",[""])[0]
  except UnicodeDecodeError:return self.login("密码格式无效")
  if not ADMIN or not hmac.compare_digest(pwd,ADMIN):
   if not rate_allowed("login",self.client_ip(),LOGIN_RATE_LIMIT,LOGIN_RATE_WINDOW):return self.login("登录尝试过多，请稍后重试。",429)
   return self.login("密码错误，请重试。")
  with RATE_LOCK:RATE_BUCKETS.pop(("login",self.client_ip()),None)
  p={"exp":now()+43200,"csrf":secrets.token_urlsafe(32)};x=base64.urlsafe_b64encode(jd(p).encode()).decode().rstrip("=");t=x+"."+hmac.new(SESSION.encode(),x.encode(),hashlib.sha256).hexdigest();secure="; Secure" if SECURE_COOKIE else "";self.out(303,b"",True,"text/plain; charset=utf-8",{"Location":"/","Set-Cookie":f"x_console_session={t}; Path=/; Max-Age=43200; HttpOnly; SameSite=Strict{secure}"})
 def logout(self):
  secure="; Secure" if SECURE_COOKIE else "";self.out(303,b"",True,"text/plain; charset=utf-8",{"Location":"/login","Set-Cookie":f"x_console_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"})
 def index(self,s):
  p=ROOT/"templates"/"index.html"
  if not p.is_file():raise E(404,"template_missing","templates/index.html not found")
  self.out(200,p.read_text().replace("{{CSRF_TOKEN}}",html.escape(s["csrf"],quote=True)).encode(),True,"text/html; charset=utf-8")
 def static(self,name):
  r=(ROOT/"static").resolve();p=(r/name).resolve()
  if r not in p.parents or not p.is_file():raise E(404,"not_found","Not found")
  self.out(200,p.read_bytes(),False,{".css":"text/css; charset=utf-8",".js":"application/javascript; charset=utf-8"}.get(p.suffix,"application/octet-stream"))
 def block_reason(self,c,a,p,action="auto"):
  if action=="settings":return None
  if c.execute("SELECT 1 FROM jobs WHERE account_id=? AND status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined')",(a["id"],)).fetchone():return "account_active"
  latest=c.execute("SELECT status,retry_eligible,retry_not_before,retry_block_reason FROM runs WHERE account_id=? ORDER BY id DESC LIMIT 1",(a["id"],)).fetchone()
  if action!="probe" and latest and latest["status"]=="manual_action_required":return "manual_action_required"
  if action=="auto" and latest and latest["retry_eligible"] and latest["retry_not_before"] and latest["retry_not_before"]>now():return "retry_cooldown"
  if action!="probe" and a["login_status"] not in ("logged_in","ok"):return "manual_action_required"
  if action!="probe" and p["used_seconds"]+p["reserved_seconds"]>=p["budget_seconds"]:return "budget_exhausted"
  workload=authoritative_workload(c)
  if workload["capacity"]<=0:return "worker_offline"
  if workload["available_slots"]<=0:return "capacity_full"
  if int(a["proxy_port"]) in workload["active_proxy_ports"]:return "proxy_port_busy"
  if action=="auto":
   if paused(c):return "global_schedule_paused"
   if not a["auto_schedule_enabled"]:return "auto_schedule_disabled"
   hm=datetime.now(TZ).strftime("%H:%M")
   if not (a["daily_window_start"]<=hm<=a["daily_window_end"]):return "outside_window"
  return None
 def normalized_account(self,c,a):
  p=plan(c,a["id"]);j=c.execute('''SELECT j.*,r.status run_status,r.reserved_seconds,r.id joined_run_id,r.retry_of_run_id,r.retry_number,r.failure_class,r.failure_code,r.failure_detail,r.retry_eligible,r.retry_not_before,r.retry_block_reason,r.cleanup_confirmed FROM jobs j JOIN runs r ON r.id=j.run_id WHERE j.account_id=? AND j.status IN ('queued','leased','running','cancel_requested','stopping','cleaning','quarantined') ORDER BY j.id DESC LIMIT 1''',(a["id"],)).fetchone();remaining=max(0,p["budget_seconds"]-p["used_seconds"]-p["reserved_seconds"])
  latest=c.execute("SELECT id,retry_of_run_id,retry_number,failure_class,failure_code,failure_detail,retry_eligible,retry_not_before,retry_block_reason,cleanup_confirmed,status,error,finished_at FROM runs WHERE account_id=? ORDER BY id DESC LIMIT 1",(a["id"],)).fetchone();d=dict(a);manual=self.block_reason(c,a,p,"manual");probe=self.block_reason(c,a,p,"probe");auto=self.block_reason(c,a,p,"auto");capabilities={"manual_browse":{"allowed":manual is None,"block_reason":manual,"ignores_auto_window":True},"probe":{"allowed":probe is None,"block_reason":probe,"ignores_daily_budget":True},"auto_schedule":{"allowed":auto is None,"block_reason":auto},"cancel":{"allowed":bool(j),"block_reason":None if j else "active_job_not_found"},"settings":{"allowed":True,"block_reason":None}}
  current_issue=None
  if j and (j["quarantine_reason"] or j["cleanup_state"] in ("requested","cleaning","uncertain") or j["status"] in ("stopping","cleaning","quarantined")):
   code="cleanup_uncertain" if j["quarantine_reason"] or j["cleanup_state"]=="uncertain" or j["status"]=="quarantined" else "cleanup_pending"
   current_issue={"code":code,"message":j["quarantine_reason"] or "Cleanup is currently in progress","severity":"critical" if code=="cleanup_uncertain" else "warning"}
  elif a["login_status"] not in ("logged_in","ok"):
   context=(latest["failure_detail"] or latest["error"]) if latest else None
   current_issue={"code":"authentication","message":f"Login status: {a['login_status']}"+(f"; latest detail: {context}" if context else ""),"severity":"warning","facts":{"login_status":a["login_status"]},"context":{"latest_run_id":latest["id"],"latest_status":latest["status"],"latest_detail":context} if latest else None}
  d.update({"serial_number":a["id"],"browser_kernel":"Flower","fingerprint_os":"Mac OS X","proxy_local_port":a["proxy_port"],"keywords":json.loads(a["keywords_json"] or "[]"),"selected_count":int(a["selected_count"]),"auto_schedule_enabled":bool(a["auto_schedule_enabled"]),"schedule_paused":not bool(a["auto_schedule_enabled"]),"daily_budget_seconds":p["budget_seconds"],"used_seconds":p["used_seconds"],"reserved_seconds":p["reserved_seconds"],"remaining_seconds":remaining,"plan_status":"completed" if remaining==0 else "available","block_reason":auto,"retry":dict(latest) if latest else None,"capabilities":capabilities,"account_capabilities":capabilities,"effective_schedule":{"global_paused":paused(c),"account_enabled":bool(a["auto_schedule_enabled"]),"window_start":a["daily_window_start"],"window_end":a["daily_window_end"],"effective":auto is None},"current_issue":current_issue,"last_failure":dict(latest) if latest and latest["status"] in ("failed","partial","manual_action_required") else None})
  d["current_run"]={"id":j["joined_run_id"],"job_id":j["id"],"status":j["run_status"],"job_status":j["status"],"phase":j["phase"],"current_source":j["source"],"search_count":j["search_count"],"trending_count":j["trending_count"],"unique_items":j["unique_items"],"elapsed_seconds":j["elapsed_seconds"],"reserved_seconds":j["reserved_seconds"],"cancel_requested":bool(j["cancel_requested"]),"retry_of_run_id":j["retry_of_run_id"],"retry_number":j["retry_number"],"failure_class":j["failure_class"],"retry_eligible":bool(j["retry_eligible"]),"retry_not_before":j["retry_not_before"]} if j else None
  d["current_execution"]={"job_id":j["id"],"generation":j["execution_generation"],"state":j["execution_state"],"cleanup_state":j["cleanup_state"],"worker_id":j["worker_id"],"quarantine_reason":j["quarantine_reason"]} if j else None;return d
 def overview(self):
  c=conn()
  try:
   workload=authoritative_workload(c);accounts=[self.normalized_account(c,a) for a in c.execute("SELECT * FROM accounts ORDER BY id")];w=c.execute("SELECT * FROM workers ORDER BY last_seen_at DESC LIMIT 1").fetchone();worker={"status":"offline","capacity":0,"active_count":0,"available_slots":0,"active_job_ids":[],"active_profile_ids":[],"active_proxy_ports":[]}
   if w:
    d=worker_details(w);availability=worker_availability(w);worker=dict(w);worker.update({"availability":availability,"capacity":clamp_capacity(w["capacity"]) if availability=="online" else 0,"active_job_ids":workload["active_job_ids"],"active_profile_ids":workload["active_profile_ids"],"active_proxy_ports":workload["active_proxy_ports"],"active_count":workload["occupied"],"available_slots":workload["available_slots"],"activity":"busy" if workload["occupied"] else "idle","protocol_version":d.get("protocol_version"),"capabilities":d.get("capabilities",[]) });worker["status"]=availability if availability!="online" else worker["activity"]
   totals=c.execute("SELECT COALESCE(SUM(budget_seconds),0),COALESCE(SUM(used_seconds),0),COALESCE(SUM(reserved_seconds),0) FROM daily_plans WHERE plan_date=?",(today(),)).fetchone();summary={"total_accounts":len(accounts),"completed_accounts":sum(1 for a in accounts if a["plan_status"]=="completed"),"daily_budget_seconds":totals[0],"used_seconds":totals[1],"reserved_seconds":totals[2],"running_count":workload["states"]["running"]+workload["states"]["starting"]+workload["states"]["cleaning"],"queue_count":workload["states"]["queued"],"capacity":workload["capacity"],"available_slots":workload["available_slots"]}
   runs=[dict(x) for x in c.execute('''SELECT r.id,r.account_id,r.job_type,r.origin,r.origin trigger,r.status,r.reserved_seconds,r.actual_seconds,r.retry_of_run_id,r.retry_number,r.failure_class,r.failure_code,r.failure_detail,r.cleanup_confirmed,r.retry_eligible,r.retry_not_before,r.retry_block_reason,j.elapsed_seconds,j.search_count,j.trending_count,j.unique_items,j.phase,j.source current_source,j.cancel_requested,j.cleanup_state,j.quarantine_reason,r.error,r.observed_handle,r.exit_ip,r.started_at,r.finished_at,r.created_at,r.updated_at,a.persona_label,a.id serial_number,a.expected_handle,COALESCE(r.observed_handle,a.observed_handle,a.expected_handle) handle FROM runs r JOIN accounts a ON a.id=r.account_id LEFT JOIN jobs j ON j.run_id=r.id ORDER BY r.id DESC LIMIT 30''')];is_paused=paused(c);attention=[x for x in workload["mismatches"]]+[{"account_id":a["id"],"issue":a["current_issue"]} for a in accounts if a["current_issue"]]
  finally:c.close()
  self.out(200,{"ok":True,"data":{"server_time":now(),"worker":worker,"summary":summary,"global_schedule_paused":is_paused,"accounts":accounts,"recent_runs":runs,"workload":workload,"attention":attention,"system":{"app_version":VERSION,"read_only":True,"sqlite_wal":True,"max_concurrency":MAX_CONCURRENCY},"controller":{"version":VERSION,"authoritative":True},"protocol":{"execution_tokens":"optional","legacy_compatible":True,"directives":["continue","cancel","stop_and_cleanup","forget","quarantine"]},"worker_metadata":workload["workers"]}})
 def workflow_candidates(self):
  try:limit=max(1,min(100,int(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("limit",["50"])[0])))
  except ValueError:limit=50
  c=conn()
  try:
   window=now()-30*86400
   rows=c.execute('''SELECT i.item_key,i.account_id,i.source,i.author_handle,i.text,i.url,i.observed_at,i.payload_json,COALESCE(w.status,'candidate') wf_status,COALESCE(w.draft_text,'') draft_text,COALESCE(w.write_request_id,0) write_request_id,COALESCE(w.analysis_json,'{}') analysis_json,COALESCE(w.note,'') note,a.persona_label,a.profile_id,a.id serial_number,a.expected_handle FROM items i JOIN accounts a ON a.id=i.account_id LEFT JOIN workflow_items w ON w.item_key=i.item_key WHERE i.source LIKE 'search:%' AND i.observed_at>=? AND i.text IS NOT NULL AND length(i.text)>0 AND COALESCE(w.status,'candidate') NOT IN ('sent','skipped','drafting') ORDER BY i.observed_at DESC LIMIT ?''',(window,limit)).fetchall()
  finally:c.close()
  import json as _j
  items=[]
  for r in rows:
   try:metrics=_j.loads(r["payload_json"]).get("metrics",{})
   except Exception:metrics={}
   items.append({"item_key":r["item_key"],"account_id":r["account_id"],"persona_label":r["persona_label"],"profile_id":r["profile_id"],"serial_number":r["serial_number"],"expected_handle":r["expected_handle"],"source":r["source"],"author_handle":r["author_handle"],"text":r["text"],"url":r["url"],"observed_at":r["observed_at"],"metrics":metrics,"status":r["wf_status"],"draft_text":r["draft_text"],"write_request_id":r["write_request_id"],"analysis":_j.loads(r["analysis_json"]),"note":r["note"]})
  self.out(200,{"ok":True,"data":{"candidates":items,"count":len(items)}})
 def workflow_sync(self):
  # Poll write-service for draft_ready workflow items whose reply has been approved & sent,
  # mark them sent, and record to Feishu bitable. Idempotent; called by the UI poll.
  import feishu_records
  c=conn()
  try:
   pending=c.execute("SELECT item_key,account_id,write_request_id,draft_text,analysis_json,author_handle,url FROM workflow_items WHERE status='draft_ready' AND write_request_id>0").fetchall()
  finally:c.close()
  synced=[];recorded=0;errors=[]
  for r in pending:
   rid=r["write_request_id"]
   try:
    req=self.xwrite_upstream("GET",f"/requests/{rid}",None,True)
   except E as e:
    errors.append({"item_key":r["item_key"],"code":e.code});continue
   data=req.get("data",req) if isinstance(req,dict) else {}
   status=data.get("status")
   if status not in ("succeeded","failed","manual_reconciliation_required"):continue
   n=now()
   if status=="succeeded":
    try:
     import json as _j;ana=_j.loads(r["analysis_json"] or "{}")
     feishu_records.record_reply(account_name=str(r["author_handle"] or ""),post_url=r["url"] or "",post_time="",summary=str(ana.get("summary","")),angle=str(ana.get("angle","")),comment=r["draft_text"] or "",sent_at=str(n))
     recorded+=1
    except feishu_records.FeishuRecordError as fe:errors.append({"item_key":r["item_key"],"code":"feishu_"+fe.code})
    c2=conn()
    try:c2.execute("UPDATE workflow_items SET status='sent',updated_at=? WHERE item_key=? AND status='draft_ready'",(n,r["item_key"]))
    finally:c2.close()
    synced.append({"item_key":r["item_key"],"status":"sent"})
   else:
    c2=conn()
    try:c2.execute("UPDATE workflow_items SET status='failed',note=?,updated_at=? WHERE item_key=? AND status='draft_ready'",(f"写入状态：{status}",n,r["item_key"]))
    finally:c2.close()
    synced.append({"item_key":r["item_key"],"status":status})
  self.out(200,{"ok":True,"data":{"synced":synced,"recorded":recorded,"errors":errors}})
 def workflow_item_action(self,item_key,action,body=b""):
  if not re.fullmatch(r"[A-Za-z0-9_:-]{1,200}",item_key or ""):raise E(404,"not_found","Not found")
  if action=="status" and self.command=="GET":
   c=conn()
   try:
    row=c.execute("SELECT * FROM workflow_items WHERE item_key=?",(item_key,)).fetchone()
   finally:c.close()
   if not row:raise E(404,"not_found","workflow item not found")
   self.out(200,{"ok":True,"data":dict(row)})
   return
  if action not in ("draft","regenerate","skip","manual_sent"):raise E(404,"not_found","Not found")
  data=self.data(body) if body else {}
  c=conn()
  try:
   item=c.execute("SELECT i.*,a.id serial_number,a.persona_label,a.profile_id,a.expected_handle FROM items i JOIN accounts a ON a.id=i.account_id WHERE i.item_key=? ORDER BY i.id DESC LIMIT 1",(item_key,)).fetchone()
   if not item:raise E(404,"not_found","item not found")
   keyword=(item["source"] or "").split(":",1)[1] if (item["source"] or "").startswith("search:") else (item["source"] or "")
   persona=item["persona_label"] or item["expected_handle"] or ""
   if action=="skip":
    n=now();c.execute("INSERT INTO workflow_items(item_key,account_id,source,author_handle,text,url,observed_at,status,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'skipped',?,?,?) ON CONFLICT(item_key) DO UPDATE SET status='skipped',note=excluded.note,updated_at=excluded.updated_at",(item_key,item["account_id"],item["source"],item["author_handle"],item["text"],item["url"],item["observed_at"],str(data.get("note",""))[:200],n,n));c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","workflow.skip",item_key,jd({"note":str(data.get("note",""))[:200]})));c.close()
    self.out(200,{"ok":True,"data":{"item_key":item_key,"status":"skipped"}})
    return
   if action=="manual_sent":
    row=c.execute("SELECT draft_text,analysis_json FROM workflow_items WHERE item_key=?",(item_key,)).fetchone()
    draft_text=(row["draft_text"] if row else "") or ""
    if not draft_text:raise E(409,"no_draft","该条还没有评论草稿，请先生成评论草稿。")
    import json as _j
    try:ana=_j.loads((row["analysis_json"] if row else "") or "{}")
    except Exception:ana={}
    import feishu_records
    n=now()
    try:
     recorded=feishu_records.record_reply(account_name=str(item["author_handle"] or ""),post_url=item["url"] or "",post_time="",summary=str(ana.get("summary","")),angle=str(ana.get("angle","")),comment=draft_text,sent_at=str(n))
     feishu_note="飞书评论记录已写入" if recorded else "飞书未配置，已跳过记录"
    except feishu_records.FeishuRecordError as fe:
     feishu_note=f"飞书记录写入失败：{fe.code}"
    c.execute("INSERT INTO workflow_items(item_key,account_id,source,author_handle,text,url,observed_at,status,note,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'sent','手动发布',?,?) ON CONFLICT(item_key) DO UPDATE SET status='sent',note='手动发布',updated_at=excluded.updated_at",(item_key,item["account_id"],item["source"],item["author_handle"],item["text"],item["url"],item["observed_at"],n,n));c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","workflow.manual_sent",item_key,"{}"));c.close()
    self.out(200,{"ok":True,"data":{"item_key":item_key,"status":"sent","feishu_note":feishu_note}})
    return
   recent=[r[0] for r in c.execute("SELECT draft_text FROM workflow_items WHERE status='sent' AND draft_text IS NOT NULL AND length(draft_text)>0 ORDER BY updated_at DESC LIMIT 8",()).fetchall()]
   c.close()
  except E:raise
  except Exception as e:raise E(500,"workflow_error",str(e)[:200])
  import workflow_llm
  n=now();c=conn()
  try:c.execute("INSERT INTO workflow_items(item_key,account_id,source,author_handle,text,url,observed_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'drafting',?,?) ON CONFLICT(item_key) DO UPDATE SET status='drafting',updated_at=excluded.updated_at",(item_key,item["account_id"],item["source"],item["author_handle"],item["text"],item["url"],item["observed_at"],n,n))
  finally:c.close()
  try:
   if action=="draft":
    analysis=workflow_llm.analyze_post(post_text=item["text"] or "",author_handle=item["author_handle"] or "",keyword=keyword)
    if not analysis.get("recommend"):
     n=now();c=conn()
     try:c.execute("UPDATE workflow_items SET status='skipped',note=?,analysis_json=?,updated_at=? WHERE item_key=?",(f"不建议回复：{analysis.get('risk','')}",jd(analysis),n,item_key))
     finally:c.close()
     self.out(200,{"ok":True,"data":{"item_key":item_key,"status":"skipped","analysis":analysis,"reason":"not_recommended"}})
     return
   else:
    analysis={}
   draft=workflow_llm.draft_comment(post_text=item["text"] or "",author_handle=item["author_handle"] or "",account_persona=persona,comment_style=str(data.get("style","")),recent_comments=recent,extra_instruction=str(data.get("instruction","")))
  except workflow_llm.LLMError as e:
   n=now();c=conn()
   try:c.execute("UPDATE workflow_items SET status='candidate',note=?,updated_at=? WHERE item_key=?",(f"LLM失败：{e.code}",n,item_key))
   finally:c.close()
   raise E(502,"llm_failed",str(e)[:200])
  # Create a reply draft in the write service via BFF.
  # Resolve the write-service account by AdsPower profile (browse account ids
  # do NOT match write-service account ids; passing them through causes
  # "account not found" on the write service).
  try:
   write_account_id=self._postflow_resolve_write_account(item["profile_id"])
  except E as e:
   n=now();c=conn()
   try:c.execute("UPDATE workflow_items SET status='candidate',note=?,updated_at=? WHERE item_key=?",(f"写入账号未连接：{e.code}",n,item_key))
   finally:c.close()
   raise
  payload={"account_id":write_account_id,"request_type":"reply","payload":{"target":item["url"] or item["item_key"],"text":draft["comment"]},"actor":"console-admin"}
  try:
   created=self.xwrite_upstream("POST","/requests",payload,False)
  except E as e:
   n=now();c=conn()
   try:c.execute("UPDATE workflow_items SET status='candidate',note=?,updated_at=? WHERE item_key=?",(f"写入服务失败：{e.code}",n,item_key))
   finally:c.close()
   raise
  req=created.get("data",created) if isinstance(created,dict) else {}
  n=now();c=conn()
  try:c.execute("UPDATE workflow_items SET status='draft_ready',draft_text=?,write_request_id=?,analysis_json=?,llm_model=?,note=?,updated_at=? WHERE item_key=?",(draft["comment"],int(req.get("id") or 0),jd(analysis) if analysis else jd({}),workflow_llm.DEFAULT_MODEL,draft["rationale"],n,item_key));c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","workflow.draft",item_key,jd({"write_request_id":int(req.get("id") or 0)})))
  finally:c.close()
  self.out(200,{"ok":True,"data":{"item_key":item_key,"status":"draft_ready","draft":draft,"analysis":analysis,"write_request":req}})
 def _postflow_resolve_write_account(self,profile_id):
  # Map a browse account's AdsPower profile_id to its write-service account id
  # by matching source_profile_id. Avoids the latent browse-id passthrough bug.
  try:resp=self.xwrite_upstream("GET","/accounts",None,True)
  except E:raise
  accounts=(resp.get("data",resp) if isinstance(resp,dict) else {}).get("accounts") or []
  for a in accounts:
   if str(a.get("source_profile_id") or "")==str(profile_id or ""):
    return int(a.get("id") or 0)
  raise E(409,"no_write_account","该浏览器 Profile 未绑定写入账号，请先在写入面板授权连接。")
 def postflow_topics(self):
  qs=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
  try:account_id=int(qs.get("account_id",[""])[0])
  except ValueError:raise E(400,"invalid_account","account_id is required")
  try:limit=max(1,min(200,int(qs.get("limit",["100"])[0])))
  except ValueError:limit=100
  c=conn()
  try:
   topics=[dict(t) for t in c.execute("SELECT * FROM postflow_topics WHERE account_id=? ORDER BY updated_at DESC LIMIT ?",(account_id,limit)).fetchall()]
   tkeys=[t["topic_key"] for t in topics]
   drafts={}
   if tkeys:
    placeholders=",".join("?" for _ in tkeys)
    for d in c.execute(f"SELECT * FROM postflow_drafts WHERE topic_key IN ({placeholders})",tkeys).fetchall():
     drafts.setdefault(d["topic_key"],[]).append(dict(d))
  finally:c.close()
  import json as _j
  for t in topics:
   t["source_item_keys"]=_j.loads(t["source_item_keys_json"] or "[]")
   t["key_points"]=_j.loads(t["key_points_json"] or "[]")
   t["angles"]=_j.loads(t["angles_json"] or "[]")
   t["suggested_links"]=_j.loads(t["suggested_links_json"] or "[]")
   t["drafts"]=drafts.get(t["topic_key"],[])
  self.out(200,{"ok":True,"data":{"topics":topics}})
 def postflow_summarize(self,b):
  data=self.data(b)
  try:account_id=int(data.get("account_id") or 0)
  except (TypeError,ValueError):raise E(400,"invalid_account","account_id is required")
  if not account_id:raise E(400,"invalid_account","account_id is required")
  keyword=str(data.get("keyword") or "").strip()
  instruction=str(data.get("instruction") or "").strip()
  c=conn()
  try:
   acc=c.execute("SELECT id,profile_id,persona_label,expected_handle FROM accounts WHERE id=?",(account_id,)).fetchone()
   if not acc:raise E(404,"not_found","account not found")
   window=now()-30*86400
   q="search:%"+keyword+"%" if keyword else "search:%"
   rows=c.execute("SELECT item_key,author_handle,text FROM items WHERE account_id=? AND source LIKE ? AND observed_at>=? AND text IS NOT NULL AND length(text)>0 ORDER BY observed_at DESC LIMIT 30",(account_id,q,window)).fetchall()
  finally:c.close()
  if not rows:raise E(404,"no_candidates","该账号近 30 天没有搜到可用候选帖，请先跑一轮浏览。")
  candidate_posts=[{"author_handle":r["author_handle"] or "","text":r["text"] or ""} for r in rows]
  import workflow_llm
  persona=acc["persona_label"] or acc["expected_handle"] or ""
  try:
   topics=workflow_llm.summarize_topics(candidate_posts=candidate_posts,keyword=keyword,account_persona=persona,extra_instruction=instruction)
  except workflow_llm.LLMError as e:raise E(502,"llm_failed",str(e)[:200])
  n=now();c=conn()
  try:
   for idx,tp in enumerate(topics,start=1):
    tkey=secrets.token_hex(16)
    c.execute("INSERT INTO postflow_topics(topic_key,account_id,source_item_keys_json,keyword,theme,key_points_json,angles_json,suggested_links_json,risk,analysis_json,status,llm_model,note,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,'{}','candidate',?,?,?,?)",
     (tkey,account_id,jd([r["item_key"] for r in rows[:20]]),keyword,tp["theme"],jd(tp["key_points"]),jd(tp["extension_angles"]),jd(tp["suggested_links"]),tp["risk"],workflow_llm.DEFAULT_MODEL,tp["topic_id"],n,n))
   c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","postflow.summarize",str(account_id),jd({"keyword":keyword,"count":len(topics)})))
  finally:c.close()
  self.out(200,{"ok":True,"data":{"topics":topics}})
 def postflow_topic_action(self,topic_key,action,body=b""):
  if not re.fullmatch(r"[A-Za-z0-9_:-]{1,200}",topic_key or ""):raise E(404,"not_found","Not found")
  if action not in ("generate","regenerate","image","submit","skip","manual_sent"):raise E(404,"not_found","Not found")
  data=self.data(body) if body else {}
  c=conn()
  try:
   tp=c.execute("SELECT * FROM postflow_topics WHERE topic_key=?",(topic_key,)).fetchone()
   if not tp:raise E(404,"not_found","topic not found")
   acc_row=c.execute("SELECT profile_id,persona_label,expected_handle FROM accounts WHERE id=?",(tp["account_id"],)).fetchone()
  finally:c.close()
  if action=="skip":
   n=now();c=conn()
   try:c.execute("UPDATE postflow_topics SET status='skipped',note=?,updated_at=? WHERE topic_key=?",(str(data.get("note",""))[:200],n,topic_key))
   finally:c.close()
   self.out(200,{"ok":True,"data":{"topic_key":topic_key,"status":"skipped"}})
   return
  import json as _j,workflow_llm
  topic={"theme":tp["theme"],"key_points":_j.loads(tp["key_points_json"] or "[]"),"extension_angles":_j.loads(tp["angles_json"] or "[]"),"suggested_links":_j.loads(tp["suggested_links_json"] or "[]")}
  if action in ("generate","regenerate"):
   # recent posts dedup across this account
   c2=conn()
   try:recent=[r[0] for r in c2.execute("SELECT post_text FROM postflow_drafts WHERE account_id=? AND status='sent' AND post_text IS NOT NULL AND length(post_text)>0 ORDER BY updated_at DESC LIMIT 8",(tp["account_id"],)).fetchall()]
   finally:c2.close()
   persona=acc_row["persona_label"] or acc_row["expected_handle"] or ""
   try:
    draft=workflow_llm.generate_post_text(topic=topic,account_persona=persona,post_style=str(data.get("style","")),recent_posts=recent,suggested_link=(data.get("link") or None),language=str(data.get("language") or "en"),extra_instruction=str(data.get("instruction","")))
   except workflow_llm.LLMError as e:raise E(502,"llm_failed",str(e)[:200])
   dkey=secrets.token_hex(16);n=now();c=conn()
   try:
    c.execute("UPDATE postflow_topics SET status='selected',updated_at=? WHERE topic_key=?",(n,topic_key))
    c.execute("INSERT INTO postflow_drafts(draft_key,topic_key,account_id,post_text,link,status,llm_model,note,created_at,updated_at) VALUES(?,?,?,?,?, 'drafting',?,?,?,?)",
     (dkey,topic_key,tp["account_id"],draft["text"],draft["link"],workflow_llm.DEFAULT_MODEL,draft["rationale"],n,n))
   finally:c.close()
   self.out(200,{"ok":True,"data":{"draft_key":dkey,"post_text":draft["text"],"link":draft["link"],"rationale":draft["rationale"]}})
   return
  if action=="image":
   import base64,hashlib
   bytes_b64=str(data.get("bytes_base64") or "")
   mime=str(data.get("mime_type") or "").strip()
   if not bytes_b64 or not mime:raise E(400,"invalid_media","bytes_base64 and mime_type are required")
   try:raw=base64.b64decode(bytes_b64,validate=True)
   except Exception:raise E(400,"invalid_media","bytes_base64 is not valid base64")
   if len(raw)>5*1024*1024:raise E(400,"invalid_media","图片不能超过 5MB")
   sha=hashlib.sha256(raw).hexdigest()
   try:write_account_id=self._postflow_resolve_write_account(acc_row["profile_id"])
   except E:raise
   try:
    created=self.xwrite_upstream("POST","/media-assets",{"asset_key":None,"account_id":write_account_id,"sha256":sha,"mime_type":mime,"byte_size":len(raw),"bytes_base64":bytes_b64,"actor":"console-admin"},False)
   except E as e:raise E(502,"write_service_failed",f"注册图片失败：{e.code}")
   asset=(created.get("data",created) if isinstance(created,dict) else {}).get("media_asset") or {}
   media_asset_id=int(asset.get("id") or 0)
   if not media_asset_id:raise E(502,"write_service_failed","写入服务未返回 media_asset id")
   draft_key=str(data.get("draft_key") or "")
   n=now();c=conn()
   try:
    if draft_key:
     c.execute("UPDATE postflow_drafts SET media_asset_id=?,updated_at=? WHERE draft_key=?",(media_asset_id,n,draft_key))
    else:
     dkey=secrets.token_hex(16)
     c.execute("INSERT INTO postflow_drafts(draft_key,topic_key,account_id,media_asset_id,status,created_at,updated_at) VALUES(?,?,?,?, 'drafting',?,?)",(dkey,topic_key,tp["account_id"],media_asset_id,n,n))
     draft_key=dkey
   finally:c.close()
   self.out(200,{"ok":True,"data":{"draft_key":draft_key,"media_asset_id":media_asset_id}})
   return
  if action=="submit":
   draft_key=str(data.get("draft_key") or "")
   if not draft_key:raise E(400,"invalid_draft","draft_key is required")
   scheduled_at=data.get("scheduled_at")
   text_override=str(data.get("text") or "").strip()
   c=conn()
   try:
    d=c.execute("SELECT * FROM postflow_drafts WHERE draft_key=?",(draft_key,)).fetchone()
    if not d or d["topic_key"]!=topic_key:raise E(404,"not_found","draft not found")
    if not text_override and not (d["post_text"] or ""):raise E(400,"invalid_draft","该草稿还没有正文，请先生成。")
   finally:c.close()
   text=text_override or (d["post_text"] or "")
   media_asset_ids=[d["media_asset_id"]] if d["media_asset_id"] else []
   try:write_account_id=self._postflow_resolve_write_account(acc_row["profile_id"])
   except E:raise
   payload={"account_id":write_account_id,"request_type":"post_create","payload":{"text":text},"actor":"console-admin"}
   if media_asset_ids:payload["payload"]["media_asset_ids"]=media_asset_ids
   if scheduled_at is not None:
    try:scheduled_at=int(scheduled_at)
    except (TypeError,ValueError):raise E(400,"invalid_scheduled_at","scheduled_at must be an integer epoch")
    payload["payload"]["scheduled_at"]=scheduled_at
   try:
    created=self.xwrite_upstream("POST","/requests",payload,False)
   except E as e:
    n=now();c=conn()
    try:c.execute("UPDATE postflow_drafts SET note=?,updated_at=? WHERE draft_key=?",(f"写入服务失败：{e.code}",n,draft_key))
    finally:c.close()
    raise
   req=created.get("data",created) if isinstance(created,dict) else {}
   n=now();c=conn()
   try:
    c.execute("UPDATE postflow_drafts SET post_text=?,write_request_id=?,scheduled_at=?,status='draft_ready',note=?,updated_at=? WHERE draft_key=?",
     (text,int(req.get("id") or 0),int(scheduled_at) if scheduled_at is not None else None,"已提交，等待审批发送",n,draft_key))
    c.execute("UPDATE postflow_topics SET status='consumed',updated_at=? WHERE topic_key=?",(n,topic_key))
    c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","postflow.submit",draft_key,jd({"write_request_id":int(req.get("id") or 0),"scheduled":scheduled_at is not None})))
   finally:c.close()
   self.out(200,{"ok":True,"data":{"draft_key":draft_key,"write_request_id":int(req.get("id") or 0),"status":"draft_ready"}})
   return
  if action=="manual_sent":
   # Manual-post mode: operator copied the text and posted by hand on x.com.
   # No X API call, no write-service request — just mark sent and record to Feishu.
   draft_key=str(data.get("draft_key") or "")
   post_url=str(data.get("post_url") or "").strip()[:300]
   if not draft_key:raise E(400,"invalid_draft","draft_key is required")
   c=conn()
   try:
    d=c.execute("SELECT * FROM postflow_drafts WHERE draft_key=?",(draft_key,)).fetchone()
    if not d or d["topic_key"]!=topic_key:raise E(404,"not_found","draft not found")
   finally:c.close()
   n=now();c=conn()
   try:
    c.execute("UPDATE postflow_drafts SET status='sent',note=?,updated_at=? WHERE draft_key=?",("手动发布",n,draft_key))
    c.execute("INSERT INTO audit_log(id,created_at,actor,action,target,payload_json) VALUES(NULL,?,?,?,?,?)",(n,"console-admin","postflow.manual_sent",draft_key,jd({"post_url":post_url})))
   finally:c.close()
   feishu_note=""
   try:
    import feishu_records
    account_name=str(acc_row["persona_label"] or acc_row["expected_handle"] or "")
    ok=feishu_records.record_post(account_name=account_name,body_text=d["post_text"] or "",image_url="",published_at=str(n),post_url=post_url)
    if not ok:feishu_note="飞书未配置，已跳过记录"
   except Exception as fe:
    feishu_note=f"飞书记录失败：{getattr(fe,'code','error')}"
   self.out(200,{"ok":True,"data":{"draft_key":draft_key,"status":"sent","feishu_note":feishu_note}})
   return
 def postflow_sync(self):
  import feishu_records,json as _j
  c=conn()
  try:
   pending=c.execute("SELECT d.draft_key,d.topic_key,d.account_id,d.post_text,d.write_request_id,d.scheduled_at,t.theme FROM postflow_drafts d JOIN postflow_topics t ON t.topic_key=d.topic_key WHERE d.status='draft_ready' AND d.write_request_id>0").fetchall()
  finally:c.close()
  synced=[];recorded=0;errors=[]
  for r in pending:
   rid=r["write_request_id"]
   try:req=self.xwrite_upstream("GET",f"/requests/{rid}",None,True)
   except E as e:errors.append({"draft_key":r["draft_key"],"code":e.code});continue
   data=req.get("data",req) if isinstance(req,dict) else {}
   status=data.get("status")
   if status not in ("succeeded","failed","manual_reconciliation_required"):continue
   n=now()
   if status=="succeeded":
    ops=data.get("operations") or []
    post_url="";published_at=str(n)
    for op in ops:
     if op.get("status")=="succeeded" and op.get("external_object_id"):
      post_url=f"https://x.com/i/web/status/{op['external_object_id']}";break
    try:
     feishu_records.record_post(account_name=str(r["theme"] or ""),body_text=r["post_text"] or "",image_url="",published_at=published_at,post_url=post_url)
     recorded+=1
    except feishu_records.FeishuRecordError as fe:errors.append({"draft_key":r["draft_key"],"code":"feishu_"+fe.code})
    c2=conn()
    try:c2.execute("UPDATE postflow_drafts SET status='sent',note=?,updated_at=? WHERE draft_key=? AND status='draft_ready'",("已发布",n,r["draft_key"]))
    finally:c2.close()
    synced.append({"draft_key":r["draft_key"],"status":"sent"})
   else:
    c2=conn()
    try:c2.execute("UPDATE postflow_drafts SET status='failed',note=?,updated_at=? WHERE draft_key=? AND status='draft_ready'",(f"写入状态：{status}",n,r["draft_key"]))
    finally:c2.close()
    synced.append({"draft_key":r["draft_key"],"status":status})
  self.out(200,{"ok":True,"data":{"synced":synced,"recorded":recorded,"errors":errors}})
 def accounts(self):
  c=conn()
  try:r=[self.normalized_account(c,a) for a in c.execute("SELECT * FROM accounts ORDER BY id")]
  finally:c.close()
  self.out(200,{"ok":True,"data":{"accounts":r}})
 def account(self,aid):
  c=conn()
  try:a=self.normalized_account(c,acc(c,aid));a["today_plan"]=dict(plan(c,aid))
  finally:c.close()
  self.out(200,{"ok":True,"data":{"account":a}})
 def listing(self,aid,t):
  c=conn()
  try:
   acc(c,aid);r=[dict(x) for x in c.execute(f"SELECT * FROM {t} WHERE account_id=? ORDER BY id DESC LIMIT 500",(aid,))]
   if t=="items":r=[x for x in r if valid_x_url(x.get("url"))]
  finally:c.close()
  self.out(200,{"ok":True,"data":{t:r}})
 def run(self,rid):
  c=conn()
  try:
   r=c.execute("SELECT r.*,j.elapsed_seconds,j.search_count,j.trending_count,j.unique_items,j.phase,j.source current_source,j.cancel_requested,j.execution_generation,j.execution_state,j.cleanup_state,j.quarantine_reason,j.terminal_code FROM runs r LEFT JOIN jobs j ON j.run_id=r.id WHERE r.id=?",(rid,)).fetchone()
   if not r:raise E(404,"run_not_found","Run not found")
   events=[]
   for x in c.execute("SELECT * FROM events WHERE run_id=? ORDER BY id",(rid,)):
    d=dict(x)
    try:payload=json.loads(d["payload_json"] or "{}")
    except Exception:payload={}
    d["payload"]=payload;d["type"]=payload.get("type") or d["event_type"];d["detail"]=payload.get("detail") or payload.get("message") or payload.get("phase");events.append(d)
   items=[dict(x) for x in c.execute("SELECT * FROM items WHERE run_id=? ORDER BY id",(rid,)) if valid_x_url(x["url"])]
  finally:c.close()
  self.out(200,{"ok":True,"data":{"run":dict(r),"events":events,"items":items}})
 def system_info(self):
  c=conn()
  try:is_paused=paused(c);workload=authoritative_workload(c)
  finally:c.close()
  self.out(200,{"ok":True,"data":{"app_version":VERSION,"db_path_basename":Path(DB).name,"scheduler_state":"paused" if is_paused else "running","read_only":True,"max_concurrency":MAX_CONCURRENCY,"workload":workload}})
 def transaction(self,fn):
  c=None
  try:c=conn();c.execute("BEGIN IMMEDIATE");result=fn(c);c.commit();c.close();return result
  except Exception:close_rollback(c);raise
 def start(self,aid,d):
  def op(c):
   a=acc(c,aid);p=plan(c,aid);v=d.get("duration_minutes")
   if v=="remaining":sec=p["budget_seconds"]-p["used_seconds"]-p["reserved_seconds"]
   elif type(v) is int and v in (5,15,30):sec=v*60
   else:raise E(400,"invalid_duration","duration_minutes must be 5, 15, 30, or remaining")
   reason=self.block_reason(c,a,p,"manual")
   if reason:raise E(409,reason,"Manual browse is not currently allowed")
   return create(c,a,sec,"manual",manual=True)
  with LOCK:rid,jid,res=self.transaction(op)
  self.out(200,{"ok":True,"data":{"run_id":rid,"job_id":jid,"reserved_seconds":res}})
 def probe(self,aid):
  def op(c):
   a=acc(c,aid);reason=self.block_reason(c,a,plan(c,aid),"probe")
   if reason:raise E(409,reason,"Probe is not currently allowed")
   return create(c,a,300,"manual","probe",manual=True)
  with LOCK:rid,jid,res=self.transaction(op)
  self.out(200,{"ok":True,"data":{"run_id":rid,"job_id":jid,"reserved_seconds":res,"operational_timeout_seconds":300}})
 def gpause(self,v):
  with LOCK:self.transaction(lambda c:c.execute("UPDATE settings SET value=?,updated_at=? WHERE key='global_schedule_paused'",("1" if v else "0",now())))
  self.out(200,{"ok":True,"data":{"global_schedule_paused":v}})
 def asched(self,aid,v):
  def op(c):acc(c,aid);c.execute("UPDATE accounts SET auto_schedule_enabled=?,updated_at=? WHERE id=?",(int(v),now(),aid))
  with LOCK:self.transaction(op)
  self.out(200,{"ok":True,"data":{"auto_schedule_enabled":v,"schedule_paused":not v}})
 def cancel(self,aid):
  def op(c):
   acc(c,aid);j=c.execute("SELECT * FROM jobs WHERE account_id=? AND status IN ('queued','leased','running','cancel_requested','stopping','cleaning') ORDER BY id DESC LIMIT 1",(aid,)).fetchone()
   if not j:raise E(409,"active_job_not_found","No active job")
   coop=j["status"] in WORKING
   if coop:c.execute("UPDATE jobs SET status='cancel_requested',execution_state='cancel_requested',cancel_requested=1,cleanup_state='requested',updated_at=? WHERE id=?",(now(),j["id"]));c.execute("UPDATE runs SET status='cancel_requested',updated_at=? WHERE id=?",(now(),j["run_id"]))
   else:finish(c,j,"cancelled",0,"Cancelled by administrator",failure_code="manual_cancel",cleanup_confirmed=True)
   return j["id"],coop
  with LOCK:jid,coop=self.transaction(op)
  self.out(200,{"ok":True,"data":{"job_id":jid,"cooperative":coop}})
 def validate_settings(self,d):
  allow={"persona_label","expected_handle","daily_window_start","daily_window_end","schedule_priority","auto_schedule_enabled"}
  if not isinstance(d,dict) or any(k not in allow for k in d):raise E(400,"invalid_field","Unsupported settings field")
  if "schedule_priority" in d and (type(d["schedule_priority"]) is not int or d["schedule_priority"] not in (50,100,150)):raise E(400,"invalid_priority","schedule_priority must be 50, 100, or 150")
  for key in ("daily_window_start","daily_window_end"):
   if key in d and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d",str(d[key])):raise E(400,"invalid_window","Time window must use HH:MM")
  return {k:(int(bool(v)) if k=="auto_schedule_enabled" else v) for k,v in d.items()}
 def validate_keywords(self,d):
  k=d.get("keywords");n=d.get("selected_count")
  if not isinstance(k,list) or any(not isinstance(x,str) or not x.strip() for x in k) or type(n) is not int or not 1<=n<=5:raise E(400,"invalid_keywords","keywords array and selected_count 1-5 required")
  return list(dict.fromkeys(x.strip() for x in k)),n
 def settings(self,aid,d):
  if not d:raise E(400,"invalid_field","Unsupported or empty settings")
  vals=self.validate_settings(d);sql=",".join(k+"=?" for k in vals)
  def op(c):acc(c,aid);c.execute("UPDATE accounts SET "+sql+",updated_at=? WHERE id=?",list(vals.values())+[now(),aid])
  with LOCK:self.transaction(op)
  self.out(200,{"ok":True})
 def keywords(self,aid,d):
  values,n=self.validate_keywords(d)
  def op(c):acc(c,aid);c.execute("UPDATE accounts SET keywords_json=?,selected_count=?,updated_at=? WHERE id=?",(jd(values),n,now(),aid))
  with LOCK:self.transaction(op)
  self.out(200,{"ok":True})
 def configuration(self,aid,d):
  settings=d.get("settings",{});keywords=d.get("keywords")
  if not settings and keywords is None:raise E(400,"invalid_configuration","settings and/or keywords required")
  vals=self.validate_settings(settings) if settings else {};kw=None
  if keywords is not None:
   if not isinstance(keywords,dict):raise E(400,"invalid_keywords","keywords must be an object")
   kw=self.validate_keywords(keywords)
  def op(c):
   acc(c,aid);sets=[];args=[]
   for k,v in vals.items():sets.append(k+"=?");args.append(v)
   if kw:sets.extend(["keywords_json=?","selected_count=?"]);args.extend([jd(kw[0]),kw[1]])
   sets.append("updated_at=?");args.extend([now(),aid]);c.execute("UPDATE accounts SET "+",".join(sets)+" WHERE id=?",args)
  with LOCK:self.transaction(op)
  self.out(200,{"ok":True})
 def admin_job(self,jid,action,d):
  def op(c):
   j=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
   if not j:raise E(404,"job_not_found","Job not found")
   n=now();state="requested" if action=="cleanup" else j["cleanup_state"];execution="stopping" if action=="cleanup" else "reconcile_requested";c.execute("UPDATE jobs SET execution_state=?,cleanup_state=?,cancel_requested=CASE WHEN ?='cleanup' THEN 1 ELSE cancel_requested END,updated_at=? WHERE id=?",(execution,state,action,n,jid));c.execute("INSERT INTO audit_log(created_at,actor,action,target,payload_json) VALUES(?,?,?,?,?)",(n,"admin",f"job_{action}",str(jid),jd(d)));return j["status"]
  with LOCK:status=self.transaction(op)
  self.out(200,{"ok":True,"data":{"job_id":jid,"requested":action,"authoritative_status":status,"force_terminal":False}})
 def worker(self,p,b):
  w=self.wauth(b);d=self.data(b)
  if p=="/api/worker/heartbeat":return self.heartbeat(w,d)
  if p=="/api/worker/claim":return self.claim(w)
  q=self.parts()
  if len(q)!=5:raise E(404,"not_found","Not found")
  try:jid=int(q[3])
  except ValueError:raise E(404,"not_found","Not found")
  a=q[4]
  if a=="start":return self.wstart(w,jid,d)
  if a=="progress":return self.progress(w,jid,d)
  if a=="complete":return self.complete(w,jid,d)
  raise E(404,"not_found","Not found")
 def directive_for_execution(self,c,w,x,tokens):
  try:jid=int(x.get("job_id"))
  except (TypeError,ValueError):return {"job_id":x.get("job_id"),"directive":"forget","reason":"invalid_job_id"}
  j=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
  cleanup=bool(x.get("cleanup_confirmed") is True)
  if not j:return {"job_id":jid,"directive":"forget" if cleanup else "stop_and_cleanup","reason":"unknown_job_cleanup_confirmed" if cleanup else "unknown_job"}
  if j["status"] in TERMINAL:
   if cleanup:
    n=now();c.execute("UPDATE jobs SET cleanup_state='confirmed',cleanup_confirmed_at=COALESCE(cleanup_confirmed_at,?),quarantine_reason=NULL,updated_at=? WHERE id=?",(n,n,jid));c.execute("UPDATE runs SET cleanup_confirmed=1,retry_block_reason=CASE WHEN retry_block_reason='cleanup_uncertain' THEN CASE WHEN ? THEN 'global_pause' ELSE NULL END ELSE retry_block_reason END,updated_at=? WHERE id=?",(int(paused(c)),n,j["run_id"]));return {"job_id":jid,"directive":"forget","reason":"terminal_cleanup_confirmed"}
   return {"job_id":jid,"directive":"stop_and_cleanup","reason":"terminal_cleanup_unconfirmed"}
  if j["status"]=="quarantined":return {"job_id":jid,"directive":"quarantine","reason":j["quarantine_reason"]}
  if j["worker_id"]!=w:return {"job_id":jid,"directive":"stop_and_cleanup","reason":"wrong_owner"}
  if tokens and (not x.get("execution_token") or not hmac.compare_digest(str(x.get("execution_token")),str(j["execution_token"] or ""))):return {"job_id":jid,"directive":"stop_and_cleanup","reason":"stale_token","execution_generation":j["execution_generation"]}
  if j["cancel_requested"]:return {"job_id":jid,"directive":"cancel","reason":"cancel_requested"}
  if j["cleanup_state"] in ("requested","cleaning","uncertain") or j["status"] in ("stopping","cleaning"):return {"job_id":jid,"directive":"stop_and_cleanup","reason":"cleanup_required"}
  return {"job_id":jid,"directive":"continue","execution_generation":j["execution_generation"]}
 def heartbeat(self,w,d):
  capabilities=d.get("capabilities",[]);capabilities=capabilities if isinstance(capabilities,list) else [];executions=d.get("executions",[]);executions=executions if isinstance(executions,list) else [];details={k:d.get(k) for k in ("current_job_id","active_job_ids","active_profile_ids","active_proxy_ports","available_slots","draining","version","platform","read_only","protocol_version") if k in d};details.update({"capacity":clamp_capacity(d.get("capacity",1)),"status":str(d.get("status","idle")),"capabilities":capabilities,"executions":executions})
  def op(c):
   directives=[self.directive_for_execution(c,w,x,"execution_tokens" in capabilities) for x in executions if isinstance(x,dict)];c.execute('''INSERT INTO workers(worker_id,status,last_seen_at,current_job_id,details_json,capacity) VALUES(?,?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET status=excluded.status,last_seen_at=excluded.last_seen_at,current_job_id=excluded.current_job_id,details_json=excluded.details_json,capacity=excluded.capacity''',(w,details["status"],now(),d.get("current_job_id"),jd(details),details["capacity"]));return directives
  with LOCK:directives=self.transaction(op)
  self.out(200,{"ok":True,"server_time":now(),"capacity":details["capacity"],"protocol_version":details.get("protocol_version"),"directives":directives},False)
 def claim(self,w):
  def op(c):
   n=now();worker=c.execute("SELECT * FROM workers WHERE worker_id=? AND last_seen_at>=?",(w,n-90)).fetchone()
   if not worker or worker_slots(c,worker)<=0:return None
   workload=authoritative_workload(c,n);own=c.execute('''SELECT a.profile_id,a.proxy_port,j.account_id FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.worker_id=? AND j.status IN ('leased','running','cancel_requested','stopping','cleaning','quarantined')''',(w,)).fetchall();profiles={x["profile_id"] for x in own}|set(workload["active_profile_ids"]);ports={int(x["proxy_port"]) for x in own}|set(workload["active_proxy_ports"]);accounts={x["account_id"] for x in own}|set(workload["active_account_ids"])
   _,d,tokens=worker_protocol(c,w)
   for candidate in c.execute('''SELECT j.*,a.profile_id,a.proxy_port FROM jobs j JOIN accounts a ON a.id=j.account_id WHERE j.status="queued" ORDER BY j.created_at,j.id''').fetchall():
    candidate_workload=authoritative_workload(c,n,exclude_job_id=candidate["id"])
    candidate_accounts=set(candidate_workload["active_account_ids"]);candidate_profiles=set(candidate_workload["active_profile_ids"]);candidate_ports=set(candidate_workload["active_proxy_ports"])
    if candidate["account_id"] in candidate_accounts or candidate["profile_id"] in candidate_profiles or int(candidate["proxy_port"]) in candidate_ports:continue
    generation=int(candidate["execution_generation"] or 0)+1;token=secrets.token_urlsafe(32) if tokens else None;c.execute('UPDATE jobs SET status="leased",worker_id=?,lease_expires_at=?,execution_generation=?,execution_token=?,execution_state="leased",cleanup_state="none",updated_at=? WHERE id=? AND status="queued"',(w,n+120,generation,token,n,candidate["id"]))
    if c.execute("SELECT changes()").fetchone()[0]!=1:continue
    c.execute('UPDATE runs SET status="leased",updated_at=? WHERE id=? AND status="queued"',(n,candidate["run_id"]));return dict(c.execute('''SELECT j.*,r.config_snapshot,r.reserved_seconds,r.retry_of_run_id,r.retry_number,a.profile_id,a.proxy_port,a.expected_handle,a.persona_label,a.id serial_number FROM jobs j JOIN runs r ON r.id=j.run_id JOIN accounts a ON a.id=j.account_id WHERE j.id=?''',(candidate["id"],)).fetchone())
   return None
  with LOCK:j=self.transaction(op)
  self.out(200,{"ok":True,"job":j},False)
 def owned(self,c,w,jid):
  j=c.execute("SELECT * FROM jobs WHERE id=?",(jid,)).fetchone()
  if not j:raise E(404,"job_not_found","Job not found")
  if j["worker_id"]!=w:raise E(403,"job_owner","Job is leased to another worker")
  return j
 def validate_execution(self,c,w,j,d,terminal_stable=False):
  _,_,tokens=worker_protocol(c,w)
  if tokens:
   token=d.get("execution_token")
   if not token or not j["execution_token"] or not hmac.compare_digest(str(token),str(j["execution_token"])):
    if terminal_stable:return False,"stop_and_cleanup"
    raise E(409,"stale_execution_token","Execution token is stale")
  return True,None
 def wstart(self,w,jid,d):
  def op(c):
   j=self.owned(c,w,jid);self.validate_execution(c,w,j,d)
   if j["status"]!="leased":raise E(409,"invalid_state","Only a leased job can start")
   a=acc(c,j["account_id"]);admission(c,a,exclude_job_id=jid);other=c.execute('''SELECT 1 FROM jobs x JOIN accounts ax ON ax.id=x.account_id WHERE x.id<>? AND x.status IN ('leased','running','cancel_requested','stopping','cleaning','quarantined') AND (x.account_id=? OR ax.profile_id=? OR ax.proxy_port=?)''',(jid,a["id"],a["profile_id"],a["proxy_port"])).fetchone()
   if other:raise E(409,"start_conflict","Account, profile, or proxy is already active")
   worker=c.execute("SELECT * FROM workers WHERE worker_id=? AND last_seen_at>=?",(w,now()-90)).fetchone()
   if not worker:raise E(409,"worker_offline","Worker heartbeat is stale")
   dworker=worker_details(worker);cap=clamp_capacity(worker["capacity"]);reported={int(x) for x in heartbeat_execution_ids(dworker)}|{int(x) for x in dworker.get("active_job_ids",[]) if str(x).isdigit()};reported.discard(jid);db_count=c.execute("SELECT COUNT(*) FROM jobs WHERE worker_id=? AND id<>? AND status IN ('leased','running','cancel_requested','stopping','cleaning','quarantined')",(w,jid)).fetchone()[0]
   if dworker.get("draining") or max(db_count,len(reported),len(dworker.get("active_profile_ids",[])),len(dworker.get("active_proxy_ports",[])))>=cap:raise E(409,"capacity_full","Worker has no slot for this leased job")
   n=now();c.execute('UPDATE jobs SET status="running",execution_state="running",lease_expires_at=?,last_progress_at=?,last_forward_progress_at=?,updated_at=? WHERE id=?',(n+120,n,n,n,jid));c.execute('UPDATE runs SET status="running",started_at=COALESCE(started_at,?),updated_at=? WHERE id=?',(n,n,j["run_id"]))
  with LOCK:self.transaction(op)
  self.out(200,{"ok":True},False)
 def progress(self,w,jid,d):
  def op(c):
   j=self.owned(c,w,jid);valid,directive=self.validate_execution(c,w,j,d,True)
   if not valid or j["status"] in TERMINAL or j["status"]=="quarantined":return {"accepted":False,"authoritative_status":j["status"],"directive":"stop_and_cleanup"}
   if j["status"] not in ("running","cancel_requested","stopping","cleaning"):return {"accepted":False,"authoritative_status":j["status"],"directive":"stop_and_cleanup"}
   if j["status"] in ("stopping","cleaning") or j["cleanup_state"] in ("requested","cleaning","uncertain"):return {"accepted":False,"authoritative_status":j["status"],"directive":"stop_and_cleanup"}
   n=now();el=max(j["elapsed_seconds"],max(0,int(d.get("elapsed_seconds",0))));search=max(j["search_count"],max(0,int(d.get("search_count",0))));trend=max(j["trending_count"],max(0,int(d.get("trending_count",0))));unique=max(j["unique_items"],max(0,int(d.get("unique_items",0))));forward=el>j["elapsed_seconds"] or search>j["search_count"] or trend>j["trending_count"] or unique>j["unique_items"] or d.get("phase")!=j["phase"];c.execute("UPDATE jobs SET phase=?,source=?,search_count=?,trending_count=?,unique_items=?,elapsed_seconds=?,last_progress_at=?,last_forward_progress_at=CASE WHEN ? THEN ? ELSE last_forward_progress_at END,lease_expires_at=?,updated_at=? WHERE id=?",(d.get("phase"),d.get("current_source",d.get("source")),search,trend,unique,el,n,int(forward),n,n+120,n,jid))
   for e in d.get("events",[])[:200]:
    if isinstance(e,dict):c.execute("INSERT INTO events(run_id,job_id,created_at,event_type,payload_json) VALUES(?,?,?,?,?)",(j["run_id"],jid,int(e.get("created_at",n)),str(e.get("event_type") or e.get("type") or "progress"),jd(e)))
   for x in d.get("items",[])[:500]:
    if isinstance(x,dict) and valid_x_url(x.get("url")):c.execute("INSERT OR IGNORE INTO items(run_id,account_id,source,item_key,author_handle,text,url,observed_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",(j["run_id"],j["account_id"],x.get("source"),str(x.get("item_key") or x["url"]),x.get("author_handle"),x.get("text"),x["url"],int(x.get("observed_at",n)),jd(x)))
   return {"accepted":True,"authoritative_status":j["status"],"directive":"cancel" if j["cancel_requested"] else "continue","cancel_requested":bool(j["cancel_requested"])}
  with LOCK:result=self.transaction(op)
  self.out(200,{"ok":True,**result},False)
 def complete(self,w,jid,d):
  if d.get("status") not in TERMINAL:raise E(400,"invalid_status","Invalid completion status")
  def op(c):
   j=self.owned(c,w,jid);valid,_=self.validate_execution(c,w,j,d,True)
   if not valid:return False,"stop_and_cleanup"
   if c.execute("SELECT status FROM runs WHERE id=?",(j["run_id"],)).fetchone()[0] in TERMINAL:
    if d.get("cleanup_confirmed") is True:
     n=now();c.execute("UPDATE jobs SET cleanup_state='confirmed',cleanup_confirmed_at=COALESCE(cleanup_confirmed_at,?),quarantine_reason=NULL,status=CASE WHEN status='quarantined' THEN COALESCE((SELECT status FROM runs WHERE id=?),status) ELSE status END,updated_at=? WHERE id=?",(n,j["run_id"],n,jid));c.execute("UPDATE runs SET cleanup_confirmed=1,retry_block_reason=CASE WHEN retry_block_reason='cleanup_uncertain' THEN CASE WHEN ? THEN 'global_pause' ELSE NULL END ELSE retry_block_reason END,updated_at=? WHERE id=?",(int(paused(c)),n,j["run_id"]));return False,"forget"
    return False,"stop_and_cleanup"
   if j["status"] not in ("running","cancel_requested","leased","stopping","cleaning"):raise E(409,"invalid_state","Job cannot be completed from its current state")
   cleanup=d.get("cleanup_confirmed");terminal_code=j["terminal_code"] or d.get("terminal_code");status=d["status"];error=d.get("error");failure_code=d.get("failure_code");failure_detail=d.get("failure_detail")
   if j["terminal_code"]:
    failure_code=j["terminal_code"];failure_detail=j["terminal_code"];status="failed";error="Worker lease expired" if j["terminal_code"]=="lease_expired" else (error or j["terminal_code"])
   return finish(c,j,status,d.get("actual_seconds",j["elapsed_seconds"]),error,d.get("observed_handle"),d.get("exit_ip"),d.get("proxy_status"),d.get("login_status"),failure_code=failure_code,failure_detail=failure_detail,cleanup_confirmed=cleanup,terminal_code=terminal_code),"forget" if cleanup else "stop_and_cleanup"
  with LOCK:changed,directive=self.transaction(op)
  self.out(200,{"ok":True,"already_completed":not changed,"directive":directive},False)
 def control(self):
  w=self.wauth(b"");c=conn()
  try:
   j=self.owned(c,w,int(self.parts()[3]));valid,_=self.validate_execution(c,w,j,{"execution_token":self.headers.get("X-Execution-Token")},True);directive="stop_and_cleanup" if not valid or j["status"] in TERMINAL or j["status"]=="quarantined" or j["cleanup_state"] in ("requested","cleaning","uncertain") else "cancel" if j["cancel_requested"] else "continue";result={"ok":True,"status":j["status"],"cancel_requested":bool(j["cancel_requested"]),"directive":directive,"execution_generation":j["execution_generation"]}
  finally:c.close()
  self.out(200,result,False)
def valid_x_url(value):
 try:u=urllib.parse.urlsplit(str(value or ""));return u.scheme=="https" and u.hostname=="x.com" and bool(u.path and u.path!="/") and not u.username and not u.password and u.port is None
 except Exception:return False
def scheduler_fill(c,n):
 if paused(c):return
 workload=authoritative_workload(c,n);slots=workload["available_slots"]
 if slots<=0:return
 hm=datetime.now(TZ).strftime("%H:%M");due=c.execute('''SELECT r.id retry_run_id,r.account_id,r.reserved_seconds,r.retry_not_before FROM runs r WHERE r.retry_eligible=1 AND r.retry_number=0 AND r.retry_not_before<=? AND r.job_type="browse" AND r.cleanup_confirmed=1 AND NOT EXISTS(SELECT 1 FROM runs rr WHERE rr.retry_of_run_id=r.id) ORDER BY r.retry_not_before,r.id''',(n,)).fetchall()
 for prior in due:
  if slots<=0:break
  try:
   a=acc(c,prior["account_id"]);p=plan(c,a["id"]);remaining=p["budget_seconds"]-p["used_seconds"]-p["reserved_seconds"]
   if not a["auto_schedule_enabled"] or a["login_status"] not in ("logged_in","ok") or not (a["daily_window_start"]<=hm<=a["daily_window_end"]):continue
   if remaining<=0:continue
   old_job=c.execute("SELECT * FROM jobs WHERE run_id=?",(prior["retry_run_id"],)).fetchone()
   if old_job and old_job["status"]=="quarantined" and c.execute("SELECT cleanup_confirmed FROM runs WHERE id=?",(prior["retry_run_id"],)).fetchone()[0]:
    c.execute("UPDATE jobs SET status=(SELECT status FROM runs WHERE id=?),execution_state=(SELECT status FROM runs WHERE id=?),cleanup_state='confirmed',cleanup_confirmed_at=COALESCE(cleanup_confirmed_at,?),quarantine_reason=NULL,updated_at=? WHERE id=?",(prior["retry_run_id"],prior["retry_run_id"],n,n,old_job["id"]))
   seconds=min(prior["reserved_seconds"] or 300,remaining)
   create(c,a,seconds,"retry","browse",prior["retry_run_id"],1);c.execute("UPDATE runs SET retry_eligible=0,updated_at=? WHERE id=?",(n,prior["retry_run_id"]));slots-=1
  except E:continue
 if slots<=0:return
 rows=c.execute('''SELECT a.*,p.budget_seconds,p.reserved_seconds,p.used_seconds FROM accounts a JOIN daily_plans p ON p.account_id=a.id AND p.plan_date=? WHERE a.auto_schedule_enabled=1 AND a.login_status IN ("logged_in","ok") AND p.used_seconds+p.reserved_seconds<p.budget_seconds AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.account_id=a.id AND r.plan_id=p.id AND r.origin IN ("scheduled","retry")) ORDER BY a.schedule_priority DESC,COALESCE(a.last_run_at,0),a.id''',(today(),)).fetchall()
 for a in rows:
  if slots<=0:break
  if not (a["daily_window_start"]<=hm<=a["daily_window_end"]):continue
  try:create(c,a,a["budget_seconds"]-a["used_seconds"]-a["reserved_seconds"],"scheduled");slots-=1
  except E:continue
def expire_leases(c,n):
 for j in c.execute("SELECT * FROM jobs WHERE status IN ('leased','running','cancel_requested','stopping','cleaning') AND lease_expires_at<?",(n,)).fetchall():
  worker=c.execute("SELECT * FROM workers WHERE worker_id=?",(j["worker_id"],)).fetchone();tokens=bool(worker and "execution_tokens" in worker_details(worker).get("capabilities",[]))
  if tokens and j["status"] in ("running","cancel_requested","stopping","cleaning"):
   c.execute("UPDATE jobs SET status='stopping',execution_state='stopping',cleanup_state='requested',cancel_requested=1,lease_expires_at=NULL,terminal_code='lease_expired',updated_at=? WHERE id=?",(n,j["id"]));c.execute("UPDATE runs SET status='cancel_requested',failure_code='lease_expired',failure_detail='Worker lease expired; cleanup requested',retry_eligible=0,retry_block_reason='cleanup_pending',updated_at=? WHERE id=?",(n,j["run_id"]))
  else:finish(c,j,"failed",j["elapsed_seconds"],"Worker lease expired; cleanup uncertain",failure_code="lease_expired",retry_eligible=False,cleanup_confirmed=False)
def scheduler():
 while not STOP.wait(15):
  c=None
  try:
   with LOCK:
    c=conn();c.execute("BEGIN IMMEDIATE");n=now();[plan(c,x["id"]) for x in c.execute("SELECT id FROM accounts")];expire_leases(c,n);scheduler_fill(c,n);c.commit();c.close();c=None
  except Exception:close_rollback(c);LOG.exception("scheduler")
def main():
 if not ADMIN or not SESSION or not WORKER:raise SystemExit("X_CONSOLE_ADMIN_PASSWORD, X_CONSOLE_SESSION_SECRET and X_CONSOLE_WORKER_SECRET are required")
 logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s");init();threading.Thread(target=scheduler,daemon=True).start();s=ThreadingHTTPServer((HOST,PORT),H);LOG.info("listening on %s:%s",HOST,PORT)
 try:s.serve_forever()
 finally:STOP.set();s.server_close()
if __name__=="__main__":main()
