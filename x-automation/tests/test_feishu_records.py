from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path


def _load(path: Path):
    spec = importlib.util.spec_from_file_location("feishu_records_test_mod", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["feishu_records_test_mod"] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload, status=200):
        self._buf = BytesIO(json.dumps(payload).encode("utf-8"))
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, n=-1):
        return self._buf.read(n if n > 0 else -1)

    def getcode(self):
        return self._status


class FeishuRecordsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load(Path("/private/tmp/x-browse-v2-staging/controller/feishu_records.py"))
        cls.keys = ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_WORKFLOW_SHEET_TOKEN",
                    "FEISHU_WORKFLOW_REPLY_SHEET", "FEISHU_WORKFLOW_POST_SHEET")
        cls.old_env = {k: os.environ.get(k) for k in cls.keys}
        os.environ.update({
            "FEISHU_APP_ID": "cli_test",
            "FEISHU_APP_SECRET": "secret_value",
            "FEISHU_WORKFLOW_SHEET_TOKEN": "sheetTokenABC",
            "FEISHU_WORKFLOW_REPLY_SHEET": "P4Xcs",
            "FEISHU_WORKFLOW_POST_SHEET": "PpGgg",
        })
        cls.mod._token_cache.clear()

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def setUp(self):
        self.mod._token_cache.clear()
        self.calls = []
        self.original_urlopen = self.mod.urllib.request.urlopen
        self.mod.urllib.request.urlopen = self._fake_urlopen

    def tearDown(self):
        self.mod.urllib.request.urlopen = self.original_urlopen

    def _fake_urlopen(self, request, timeout=None):
        body = request.data.decode("utf-8") if request.data else ""
        url = request.full_url
        self.calls.append({"url": url, "body": body})
        if "tenant_access_token" in url:
            return FakeResponse({"code": 0, "tenant_access_token": "tn-test", "expire": 7200})
        if "values_append" in url:
            payload = json.loads(body)
            self.assertEqual(1, len(payload["valueRange"]["values"]))
            return FakeResponse({"code": 0, "data": {"revision": 3, "updates": {}}})
        raise AssertionError("unexpected url " + url)

    def test_record_reply_appends_seven_columns(self):
        ok = self.mod.record_reply(
            account_name="Pixel Mara", post_url="https://x.com/s/status/100",
            post_time="2026-07-28", summary="关于AI的帖子", angle="补充实战细节",
            comment="good point", sent_at="1700000000")
        self.assertTrue(ok)
        append = [c for c in self.calls if "values_append" in c["url"]][0]
        self.assertIn("sheetTokenABC", append["url"])
        payload = json.loads(append["body"])
        self.assertEqual("P4Xcs!A1:G1", payload["valueRange"]["range"])
        row = payload["valueRange"]["values"][0]
        self.assertEqual(["Pixel Mara", "https://x.com/s/status/100", "2026-07-28",
                          "关于AI的帖子", "补充实战细节", "good point", "1700000000"], row)

    def test_record_post_appends_five_columns(self):
        ok = self.mod.record_post(
            account_name="Pixel Mara", body_text="hello world",
            image_url="img_token_xyz", published_at="1700000123",
            post_url="https://x.com/pix/status/200")
        self.assertTrue(ok)
        append = [c for c in self.calls if "values_append" in c["url"]][0]
        payload = json.loads(append["body"])
        self.assertEqual("PpGgg!A1:E1", payload["valueRange"]["range"])

    def test_secret_never_leaks_to_url_or_record_body(self):
        self.mod.record_reply(account_name="x", post_url="", post_time="", summary="",
                              angle="", comment="", sent_at="")
        for call in self.calls:
            self.assertNotIn("secret_value", call["url"])
        appends = [c for c in self.calls if "values_append" in c["url"]]
        self.assertTrue(appends)
        for call in appends:
            self.assertNotIn("secret_value", call["body"])

    def test_skips_when_not_configured(self):
        self.mod.urllib.request.urlopen = self.original_urlopen
        saved = os.environ.pop("FEISHU_WORKFLOW_SHEET_TOKEN")
        try:
            self.assertFalse(self.mod.record_reply(
                account_name="x", post_url="", post_time="", summary="",
                angle="", comment="", sent_at=""))
        finally:
            os.environ["FEISHU_WORKFLOW_SHEET_TOKEN"] = saved


if __name__ == "__main__":
    unittest.main()
