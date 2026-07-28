from __future__ import annotations

import http.client
import importlib.util
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CONTROLLER_APP = Path("/private/tmp/x-browse-v2-staging/controller/app.py")
WORKFLOW_LLM = Path("/private/tmp/x-browse-v2-staging/controller/workflow_llm.py")


class FakeWriteHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    response_status = 201
    response_payload: dict = {"id": 42}

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).calls.append({"path": self.path, "body": body})
        raw = json.dumps(type(self).response_payload).encode("utf-8")
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        return


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="x-wf-", dir="/private/tmp")
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeWriteHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        upstream_url = f"http://127.0.0.1:{cls.upstream.server_address[1]}"

        cls.env = {
            "X_CONSOLE_DB": os.path.join(cls.tmp, "console.db"),
            "X_CONSOLE_HOST": "127.0.0.1",
            "X_CONSOLE_PORT": "0",
            "X_CONSOLE_ADMIN_PASSWORD": "wf-admin",
            "X_CONSOLE_SESSION_SECRET": "wf-session-secret",
            "X_CONSOLE_WORKER_SECRET": "wf-worker-secret",
            "X_CONSOLE_XWRITE_URL": upstream_url,
            "X_CONSOLE_XWRITE_SECRET": "wf-xwrite-" + "x" * 32,
            "WORKFLOW_LLM_KEY": "test-llm-key",
            "WORKFLOW_LLM_MODEL": "glm-5.1",
        }
        cls.old_env = {k: os.environ.get(k) for k in cls.env}
        os.environ.update(cls.env)

        # Pre-import a fake workflow_llm that shadows the real one on sys.path.
        cls.llm_calls: list[dict] = []
        fake_llm = type("M", (), {})()
        class _LLMError(RuntimeError):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code
        fake_llm.LLMError = _LLMError
        fake_llm.DEFAULT_MODEL = "glm-5.1"
        def analyze_post(*, post_text, author_handle, keyword, **_):
            cls.llm_calls.append({"kind": "analyze", "keyword": keyword})
            return {"summary": "s", "intent": "i", "angle": "a", "risk": "无",
                    "pain_points": "", "recommend": True}
        def draft_comment(*, post_text, author_handle, account_persona, comment_style,
                          recent_comments, extra_instruction="", **_):
            cls.llm_calls.append({"kind": "draft", "recent": list(recent_comments)})
            return {"comment": "good point about " + keyword_from_persona(account_persona),
                    "rationale": "adds value"}
        def keyword_from_persona(p):
            return p
        def summarize_topics(*, candidate_posts, keyword, account_persona="", extra_instruction="", **_):
            cls.llm_calls.append({"kind": "summarize", "keyword": keyword, "posts": len(candidate_posts)})
            return [{"topic_id": "t1", "theme": "AI 工具实测角度",
                     "key_points": ["工具提效明显"], "extension_angles": ["实测对比"],
                     "suggested_links": [], "risk": "无", "recommend": True}]
        def generate_post_text(*, topic, account_persona="", post_style="", recent_posts=None,
                               suggested_link=None, extra_instruction="", **_):
            cls.llm_calls.append({"kind": "generate_post", "theme": topic.get("theme")})
            return {"text": "这周实测了 5 个 AI 工具，分享真实对比。",
                    "rationale": "实测角度", "link": ""}
        fake_llm.analyze_post = analyze_post
        fake_llm.draft_comment = draft_comment
        fake_llm.summarize_topics = summarize_topics
        fake_llm.generate_post_text = generate_post_text
        import sys
        sys.modules["workflow_llm"] = fake_llm

        # Stub feishu_records so manual_sent can be asserted without a real tenant.
        cls.feishu_calls: list[dict] = []
        fake_feishu = type("M", (), {})()
        class _FeishuRecordError(RuntimeError):
            def __init__(self, code, message):
                super().__init__(message)
                self.code = code
        fake_feishu.FeishuRecordError = _FeishuRecordError
        def record_post(*, account_name, body_text, image_url, published_at, post_url, config=None):
            cls.feishu_calls.append({"account_name": account_name, "body_text": body_text,
                                     "post_url": post_url, "published_at": published_at})
            return True
        def record_reply(**kwargs):
            cls.feishu_calls.append({"kind": "reply", **kwargs})
            return True
        fake_feishu.record_post = record_post
        fake_feishu.record_reply = record_reply
        sys.modules["feishu_records"] = fake_feishu

        spec = importlib.util.spec_from_file_location("controller_wf_test", CONTROLLER_APP)
        cls.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.app)
        cls.app.init()
        cls._seed_item()
        cls.server = cls.app.ThreadingHTTPServer(("127.0.0.1", 0), cls.app.H)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        jar_cookie = None
        cls.jar = None

    @classmethod
    def _seed_item(cls):
        import time
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            now = int(time.time())
            # init() already seeds accounts 10-15,22-26; just add a run+item for account 10.
            c.execute("INSERT INTO daily_plans(account_id,plan_date,budget_seconds,created_at,updated_at) VALUES(10,'2026-07-27',3600,?,?)", (now, now))
            c.execute("INSERT INTO runs(id,account_id,plan_id,job_type,origin,status,reserved_seconds,actual_seconds,config_snapshot,created_at,updated_at) VALUES(1,10,1,'browse','manual','succeeded',0,0,'{}',?,?)", (now, now))
            c.execute("INSERT INTO items(run_id,account_id,source,item_key,author_handle,text,url,observed_at,payload_json) VALUES(1,10,'search:ai','tweet-100','@someone','Just shipped a new feature using AI agents, really proud of the result.','https://x.com/someone/status/100',?,?)", (now, json.dumps({"metrics":{"like_count":"50"}})))
            c.commit()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)
        cls.upstream.shutdown(); cls.upstream.server_close(); cls.upstream_thread.join(timeout=5)
        import sys
        sys.modules.pop("workflow_llm", None)
        sys.modules.pop("feishu_records", None)
        for k, v in cls.old_env.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        FakeWriteHandler.calls = []
        FakeWriteHandler.response_status = 201
        FakeWriteHandler.response_payload = {"id": 42}
        type(self).llm_calls.clear()
        type(self).feishu_calls.clear()
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            c.execute("DELETE FROM workflow_items")
            c.execute("DELETE FROM postflow_topics")
            c.execute("DELETE FROM postflow_drafts")
            c.commit()

    def _session(self):
        import http.cookiejar
        import urllib.request
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        form = urllib.parse.urlencode({"password": "wf-admin"}).encode()
        req = urllib.request.Request("http://127.0.0.1:%d/login" % self.port, data=form,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"},
                                     method="POST")
        opener.open(req, timeout=5).read()
        index = opener.open("http://127.0.0.1:%d/" % self.port, timeout=5).read().decode()
        csrf = index.split('csrf-token" content="', 1)[1].split('"', 1)[0]
        return opener, csrf

    def _post(self, opener, csrf, path, payload):
        import urllib.request
        body = json.dumps(payload).encode()
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), data=body,
                                     headers={"Content-Type": "application/json",
                                              "X-CSRF-Token": csrf}, method="POST")
        with opener.open(req, timeout=8) as r:
            return r.getcode(), json.loads(r.read())

    def _get(self, opener, path):
        import urllib.request
        with opener.open("http://127.0.0.1:%d%s" % (self.port, path), timeout=8) as r:
            return r.getcode(), json.loads(r.read())

    def test_candidates_return_search_items_with_metrics(self):
        opener, _ = self._session()
        status, payload = self._get(opener, "/api/x/workflow/candidates?limit=10")
        self.assertEqual(200, status)
        candidates = payload["data"]["candidates"]
        self.assertEqual(1, len(candidates))
        item = candidates[0]
        self.assertEqual("tweet-100", item["item_key"])
        self.assertEqual("search:ai", item["source"])
        self.assertIn("AI agents", item["text"])
        self.assertEqual({"like_count": "50"}, item["metrics"])
        self.assertEqual("candidate", item["status"])

    def test_draft_creates_reply_request_and_marks_draft_ready(self):
        opener, csrf = self._session()
        status, payload = self._post(opener, csrf, "/api/x/workflow/items/tweet-100/draft", {})
        self.assertEqual(200, status)
        self.assertEqual("draft_ready", payload["data"]["status"])
        self.assertEqual("good point about Pixel Mara", payload["data"]["draft"]["comment"])
        # A reply request was forwarded to the write service.
        self.assertEqual(1, len(FakeWriteHandler.calls))
        call = FakeWriteHandler.calls[0]
        self.assertEqual("/api/requests", call["path"])
        body = json.loads(call["body"])
        self.assertEqual("reply", body["request_type"])
        self.assertEqual("https://x.com/someone/status/100", body["payload"]["target"])
        self.assertEqual("good point about Pixel Mara", body["payload"]["text"])
        self.assertEqual("console-admin", body["actor"])
        # workflow_items recorded the draft.
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            row = c.execute("SELECT status,draft_text,write_request_id FROM workflow_items WHERE item_key='tweet-100'").fetchone()
        self.assertEqual(("draft_ready", "good point about Pixel Mara", 42), row)
        self.assertEqual(["analyze", "draft"], [c["kind"] for c in self.llm_calls])

    def test_skip_marks_skipped_without_llm_or_write(self):
        opener, csrf = self._session()
        status, payload = self._post(opener, csrf, "/api/x/workflow/items/tweet-100/skip",
                                     {"note": "off-topic"})
        self.assertEqual(200, status)
        self.assertEqual("skipped", payload["data"]["status"])
        self.assertEqual([], FakeWriteHandler.calls)
        self.assertEqual([], self.llm_calls)
        # Skipped items do not reappear in candidates.
        status, payload = self._get(opener, "/api/x/workflow/candidates")
        self.assertEqual(0, payload["data"]["count"])

    def test_manual_sent_marks_sent_and_records_feishu_without_write_service(self):
        # Seed a postflow topic + draft directly (no LLM / write-service needed).
        import time
        now = int(time.time())
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            c.execute("INSERT INTO postflow_topics(topic_key,account_id,source_item_keys_json,keyword,theme,status,created_at,updated_at) VALUES('tk-1',10,'[]','ai','AI 工具实测角度','selected',?,?)", (now, now))
            c.execute("INSERT INTO postflow_drafts(draft_key,topic_key,account_id,post_text,status,created_at,updated_at) VALUES('dk-1','tk-1',10,'手动发帖的正文内容','draft_ready',?,?)", (now, now))
            c.commit()
        opener, csrf = self._session()
        status, payload = self._post(opener, csrf, "/api/x/postflow/topics/tk-1/manual_sent",
                                     {"draft_key": "dk-1", "post_url": "https://x.com/pixel/status/777"})
        self.assertEqual(200, status)
        self.assertEqual("sent", payload["data"]["status"])
        # No write-service call, no LLM call.
        self.assertEqual([], FakeWriteHandler.calls)
        self.assertEqual([], self.llm_calls)
        # Draft marked sent with 手动发布 note.
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            row = c.execute("SELECT status,note FROM postflow_drafts WHERE draft_key='dk-1'").fetchone()
        self.assertEqual(("sent", "手动发布"), row)
        # Feishu record written with the seeded account persona and post url.
        self.assertEqual(1, len(self.feishu_calls))
        call = self.feishu_calls[0]
        self.assertEqual("Pixel Mara", call["account_name"])
        self.assertEqual("手动发帖的正文内容", call["body_text"])
        self.assertEqual("https://x.com/pixel/status/777", call["post_url"])

    def test_summarize_inserts_topics_from_search_items(self):
        opener, csrf = self._session()
        status, payload = self._post(opener, csrf, "/api/x/postflow/summarize",
                                     {"account_id": 10, "keyword": "ai", "instruction": ""})
        self.assertEqual(200, status)
        topics = payload["data"]["topics"]
        self.assertEqual(1, len(topics))
        self.assertEqual("AI 工具实测角度", topics[0]["theme"])
        # Topic persisted with correct column mapping (status='candidate', llm_model set).
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            row = c.execute("SELECT theme,status,llm_model,note,keyword FROM postflow_topics").fetchone()
        self.assertEqual("AI 工具实测角度", row[0])
        self.assertEqual("candidate", row[1])
        self.assertEqual("glm-5.1", row[2])
        self.assertEqual("t1", row[3])
        self.assertEqual("ai", row[4])
        self.assertEqual(["summarize"], [c["kind"] for c in self.llm_calls])
        # Topics list endpoint returns the topic with parsed json fields.
        status, payload = self._get(opener, "/api/x/postflow/topics?account_id=10")
        self.assertEqual(200, status)
        listed = payload["data"]["topics"]
        self.assertEqual(1, len(listed))
        self.assertEqual(["工具提效明显"], listed[0]["key_points"])

    def test_generate_creates_draft_without_write_service(self):
        import time
        now = int(time.time())
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            c.execute("INSERT INTO postflow_topics(topic_key,account_id,source_item_keys_json,keyword,theme,status,created_at,updated_at) VALUES('tk-g',10,'[]','ai','AI 工具','candidate',?,?)", (now, now))
            c.commit()
        opener, csrf = self._session()
        status, payload = self._post(opener, csrf, "/api/x/postflow/topics/tk-g/generate", {})
        self.assertEqual(200, status)
        self.assertEqual("这周实测了 5 个 AI 工具，分享真实对比。", payload["data"]["post_text"])
        self.assertEqual([], FakeWriteHandler.calls)
        with sqlite3.connect(os.environ["X_CONSOLE_DB"]) as c:
            row = c.execute("SELECT post_text,status,topic_key FROM postflow_drafts WHERE topic_key='tk-g'").fetchone()
        self.assertEqual("这周实测了 5 个 AI 工具，分享真实对比。", row[0])
        self.assertEqual("drafting", row[1])


if __name__ == "__main__":
    unittest.main()
