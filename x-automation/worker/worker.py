#!/usr/bin/env python3
"""Independent, strictly read-only Windows worker for X Browse Console v1.1.x."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import math
import multiprocessing
import os
import queue
import random
import re
import signal
import sys
import threading
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.actions.wheel_input import ScrollOrigin
from selenium.webdriver.firefox.options import Options as FirefoxOptions
from selenium.webdriver.firefox.service import Service as FirefoxService

VERSION = "1.3.0"
PROTOCOL_VERSION = "2.0"
CAPABILITIES = ["execution_tokens", "cleanup_confirmation", "heartbeat_reconciliation", "structured_failures", "spawn_jobs"]
TERMINAL = {"succeeded", "partial", "cancelled", "failed", "manual_action_required"}
STATUS_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})/status/([0-9]+)(?:/)?$")
HANDLE_RE = re.compile(r"^@?([A-Za-z0-9_]{1,15})$")
STOP = threading.Event()
LOG = logging.getLogger("x-browse-worker")
_LOG_CONTEXT = threading.local()


class JobLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.job_id = getattr(_LOG_CONTEXT, "job_id", "-")
        record.profile_id = getattr(_LOG_CONTEXT, "profile_id", "-")
        return True


class JobLogContext:
    def __init__(self, job_id: int, profile_id: str):
        self.job_id = job_id
        self.profile_id = profile_id

    def __enter__(self) -> None:
        _LOG_CONTEXT.job_id = self.job_id
        _LOG_CONTEXT.profile_id = self.profile_id

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        _LOG_CONTEXT.job_id = "-"
        _LOG_CONTEXT.profile_id = "-"


class WorkerError(RuntimeError):
    pass


class Cancelled(WorkerError):
    pass


class ManualActionRequired(WorkerError):
    pass


class SourceUnavailable(WorkerError):
    pass


class BudgetExpired(WorkerError):
    pass


class AdsPowerError(WorkerError):
    pass


@dataclass(frozen=True)
class Config:
    controller_url: str
    worker_id: str
    worker_secret: str
    adspower_base_url: str
    adspower_api_key: str
    poll_seconds: float
    heartbeat_seconds: float
    request_timeout_seconds: float
    progress_seconds: float
    max_concurrent_jobs: int
    log_file: str
    no_forward_progress_seconds: float
    hard_runtime_grace_seconds: float
    probe_hard_runtime_seconds: float
    cooperative_cancel_seconds: float
    terminate_grace_seconds: float
    cleanup_timeout_seconds: float
    reconciliation_journal: str = "state/reconciliation.json"

    @classmethod
    def load(cls, path: Path) -> "Config":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SystemExit(f"Cannot read worker config {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit("Worker config must be a JSON object")
        required = ("controller_url", "worker_id", "worker_secret", "adspower_base_url")
        missing = [key for key in required if not str(raw.get(key) or "").strip()]
        if missing:
            raise SystemExit("Missing required config fields: " + ", ".join(missing))
        controller = str(raw["controller_url"]).rstrip("/")
        adspower = str(raw["adspower_base_url"]).rstrip("/")
        for name, value in (("controller_url", controller), ("adspower_base_url", adspower)):
            parsed = urllib.parse.urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
                raise SystemExit(f"Invalid {name}")
        def number(name: str, default: float, lo: float, hi: float) -> float:
            try:
                value = float(raw.get(name, default))
            except (TypeError, ValueError) as exc:
                raise SystemExit(f"Invalid numeric config field: {name}") from exc
            if not math.isfinite(value) or not lo <= value <= hi:
                raise SystemExit(f"Config field {name} must be between {lo} and {hi}")
            return value
        try:
            configured_slots = int(raw.get("max_concurrent_jobs", 1))
        except (TypeError, ValueError) as exc:
            raise SystemExit("Invalid numeric config field: max_concurrent_jobs") from exc
        progress = number("progress_seconds", 22, 20, 25)
        return cls(
            controller_url=controller,
            worker_id=str(raw["worker_id"]).strip(),
            worker_secret=str(raw["worker_secret"]),
            adspower_base_url=adspower,
            adspower_api_key=str(raw.get("adspower_api_key") or ""),
            poll_seconds=number("poll_seconds", 5, 1, 60),
            heartbeat_seconds=number("heartbeat_seconds", 30, 10, 90),
            request_timeout_seconds=number("request_timeout_seconds", 30, 5, 120),
            progress_seconds=progress,
            max_concurrent_jobs=max(1, min(3, configured_slots)),
            log_file=str(raw.get("log_file") or "logs/worker.log"),
            no_forward_progress_seconds=number("no_forward_progress_seconds", 90, 30, 900),
            hard_runtime_grace_seconds=number("hard_runtime_grace_seconds", 60, 5, 600),
            probe_hard_runtime_seconds=number("probe_hard_runtime_seconds", 360, 60, 900),
            cooperative_cancel_seconds=number("cooperative_cancel_seconds", 10, 1, 60),
            terminate_grace_seconds=number("terminate_grace_seconds", 5, 1, 30),
            cleanup_timeout_seconds=number("cleanup_timeout_seconds", 45, 10, 180),
            reconciliation_journal=str(raw.get("reconciliation_journal") or "state/reconciliation.json"),
        )


class ControllerClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @staticmethod
    def compact_body(data: Optional[Dict[str, Any]]) -> bytes:
        return json.dumps(data or {}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def request(self, method: str, path: str, data: Optional[Dict[str, Any]] = None, execution_token: Optional[str] = None) -> Dict[str, Any]:
        method = method.upper()
        body = b"" if method == "GET" else self.compact_body(data)
        timestamp = str(int(time.time()))
        digest = hashlib.sha256(body).hexdigest()
        canonical = timestamp + "\n" + method + "\n" + path + "\n" + digest
        signature = hmac.new(self.cfg.worker_secret.encode(), canonical.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-Worker-ID": self.cfg.worker_id,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        }
        if method != "GET":
            headers["Content-Type"] = "application/json"
        if execution_token:
            headers["X-Execution-Token"] = str(execution_token)
        req = urllib.request.Request(self.cfg.controller_url + path, data=body if method != "GET" else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:2000]
            try:
                parsed_error = json.loads(detail)
            except Exception:
                parsed_error = {"error": {"code": "http_error", "message": detail}}
            error = parsed_error.get("error") if isinstance(parsed_error, dict) else None
            code = error.get("code") if isinstance(error, dict) else "http_error"
            message = error.get("message") if isinstance(error, dict) else detail
            err = WorkerError(f"controller HTTP {exc.code} {code}: {message}")
            err.controller_data = parsed_error
            err.controller_code = code
            err.http_status = exc.code
            raise err from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerError(f"controller connection failed: {exc}") from exc
        try:
            result = json.loads(raw or b"{}")
        except Exception as exc:
            raise WorkerError("controller returned invalid JSON") from exc
        if not isinstance(result, dict) or result.get("ok") is False:
            raise WorkerError(f"controller rejected request: {result}")
        return result

    def heartbeat(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = dict(payload)
        body.update({"version": VERSION, "platform": "windows", "read_only": True})
        return self.request("POST", "/api/worker/heartbeat", body)


class AdsPowerClient:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._last_call = 0.0

    def _request(self, path: str, params: Dict[str, Any], *, tolerate_business_error: bool = False) -> Dict[str, Any]:
        with self._lock:
            wait = 1.1 - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            url = self.cfg.adspower_base_url + path + "?" + urllib.parse.urlencode(params)
            headers = {"Accept": "application/json"}
            if self.cfg.adspower_api_key:
                headers["Authorization"] = "Bearer " + self.cfg.adspower_api_key
            req = urllib.request.Request(url, headers=headers, method="GET")
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.request_timeout_seconds) as response:
                    raw = response.read()
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise AdsPowerError(f"AdsPower HTTP {exc.code}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise AdsPowerError(f"AdsPower unavailable: {exc}") from exc
        try:
            data = json.loads(raw or b"{}")
        except Exception as exc:
            raise AdsPowerError("AdsPower returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise AdsPowerError("AdsPower returned non-object JSON")
        if data.get("code") not in (0, "0") and not tolerate_business_error:
            raise AdsPowerError(f"AdsPower API error: {data.get('msg') or data.get('code')}")
        return data

    def active_state(self, profile_id: str) -> Optional[bool]:
        """Return True/False when status API is conclusive, otherwise None."""
        for path in ("/api/v1/browser/active", "/api/v1/browser/status"):
            try:
                data = self._request(path, {"user_id": profile_id}, tolerate_business_error=True)
            except AdsPowerError as exc:
                LOG.warning("AdsPower active-state check unavailable at %s: %s", path, short(exc))
                continue
            if data.get("code") not in (0, "0"):
                continue
            payload = data.get("data")
            if isinstance(payload, dict):
                status = str(payload.get("status") or payload.get("active") or payload.get("state") or "").lower()
                if status in {"active", "running", "open", "opened", "1", "true"}:
                    return True
                if status in {"inactive", "stopped", "closed", "0", "false"}:
                    return False
            if isinstance(payload, bool):
                return payload
        return None

    def start(self, profile_id: str) -> Dict[str, Any]:
        data = self._request("/api/v1/browser/start", {"user_id": profile_id, "launch_args": "[]", "open_tabs": 1})
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise AdsPowerError("AdsPower start response has no data object")
        webdriver_path = str(payload.get("webdriver") or "")
        if "geckodriver" not in webdriver_path.lower() and "flower" not in webdriver_path.lower():
            raise AdsPowerError("Profile is not Flower/Firefox; refusing non-Flower attachment")
        port = str(payload.get("marionette_port") or "").strip()
        if not port.isdigit():
            raise AdsPowerError("Flower start response lacks marionette_port; profile may already be open")
        return payload

    def stop(self, profile_id: str) -> None:
        self._request("/api/v1/browser/stop", {"user_id": profile_id})

    def stop_and_confirm(self, profile_id: str) -> None:
        self.stop(profile_id)
        for attempt in range(6):
            time.sleep(2)
            state = self.active_state(profile_id)
            if state is False:
                return
            if attempt == 2:
                self.stop(profile_id)
        raise AdsPowerError("AdsPower reported the worker-owned profile still active after stop")

    @staticmethod
    def attach(payload: Dict[str, Any]) -> webdriver.Firefox:
        path = str(payload.get("webdriver") or "")
        port = str(payload.get("marionette_port") or "")
        service = FirefoxService(executable_path=path, service_args=["--connect-existing", "--marionette-port", port])
        return webdriver.Firefox(service=service, options=FirefoxOptions())

    @staticmethod
    def detach(driver: webdriver.Remote) -> None:
        service = getattr(driver, "service", None)
        if service is not None:
            try:
                service.stop()
            except Exception:
                pass


class BrowserRun:
    def __init__(self, driver: webdriver.Remote, state: JobState, deadline: float):
        self.driver = driver
        self.state = state
        self.deadline = deadline

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def require_time(self, minimum: float = 0.0) -> None:
        if self.remaining() <= minimum:
            raise BudgetExpired("Time budget exhausted")
        self.state.checkpoint()

    def navigate(self, url: str, timeout: int = 35) -> None:
        self.require_time(2)
        self.driver.set_page_load_timeout(max(5, min(timeout, int(max(5, self.remaining())))))
        try:
            self.driver.get(url)
        except TimeoutException:
            try:
                self.driver.execute_script("window.stop();")
            except Exception:
                pass
        self.state.checkpoint()
        self.state.forward("navigation_returned", url=url)
        self.detect_account_state()

    def detect_account_state(self) -> str:
        current_url = str(getattr(self.driver, "current_url", "") or "")
        url = current_url.lower()
        title = str(getattr(self.driver, "title", "") or "").lower()
        text = ""
        try:
            text = (self.driver.find_element(By.TAG_NAME, "body").text or "")[:20000].lower()
        except Exception:
            pass
        restricted_markers = ("/account/access", "/account/suspended", "account is suspended", "account locked", "temporarily limited")
        challenge_markers = ("/i/flow/login", "/login", "verify your identity", "unusual activity", "enter your phone", "enter your email")
        if any(marker in url or marker in text for marker in restricted_markers):
            raise ManualActionRequired("X account is restricted or suspended")
        if any(marker in url or marker in text for marker in challenge_markers):
            raise ManualActionRequired("X login or verification challenge requires manual action")
        parsed = urllib.parse.urlsplit(current_url)
        landing_path = parsed.hostname in {"x.com", "www.x.com"} and parsed.path in {"", "/"}
        signed_out_markers = ("what's happening", "what’s happening", "happening now", "join today", "create account", "sign up", "sign in", "log in", "登录", "注册")
        signed_out_score = sum(marker in title or marker in text for marker in signed_out_markers)
        login_links = []
        if landing_path:
            try:
                login_links = self.driver.find_elements(By.CSS_SELECTOR, 'a[href="/login"],a[href^="/i/flow/login"],[data-testid="loginButton"]')
            except Exception:
                pass
        if landing_path and (signed_out_score >= 2 or login_links):
            raise ManualActionRequired("X login is required before automation can run")
        self.state.forward("account_state_dom_batch", url=current_url)
        return "logged_in"

    def observed_handle(self) -> str:
        selectors = (
            'a[data-testid="AppTabBar_Profile_Link"]',
            'a[aria-label="Profile"][href^="/"]',
            'a[aria-label="个人资料"][href^="/"]',
        )
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                href = str(element.get_attribute("href") or "")
                parsed = urllib.parse.urlsplit(href)
                parts = [part for part in parsed.path.split("/") if part]
                if len(parts) == 1 and HANDLE_RE.fullmatch(parts[0]):
                    return "@" + HANDLE_RE.fullmatch(parts[0]).group(1)
        return ""

    def verify_handle(self, expected: str) -> str:
        observed = self.observed_handle()
        expected_norm = normalize_handle(expected)
        observed_norm = normalize_handle(observed)
        if expected_norm and observed_norm and expected_norm.lower() != observed_norm.lower():
            raise ManualActionRequired(f"Handle mismatch: expected @{expected_norm}, observed @{observed_norm}")
        return observed

    def scroll(self, pixels: int = 850) -> None:
        self.require_time(1)
        try:
            w, h = self.driver.execute_script("return [window.innerWidth,window.innerHeight]")
            origin = ScrollOrigin.from_viewport(int((w or 1000) / 2), int((h or 700) / 2))
            ActionChains(self.driver).scroll_from_origin(origin, 0, int(pixels)).perform()
        except Exception:
            self.driver.execute_script("window.scrollBy(0,arguments[0]);", int(pixels))
        self.state.sleep(random.uniform(2.0, 5.0))
        self.state.forward("scroll_returned", pixels=int(pixels))

    def collect_status_links(self) -> List[Dict[str, str]]:
        self.detect_account_state()
        out: List[Dict[str, str]] = []
        local = set()
        for article in self.driver.find_elements(By.CSS_SELECTOR, 'article[data-testid="tweet"]'):
            if article.find_elements(By.CSS_SELECTOR, '[data-testid="promotedIndicator"], [data-testid="placementTracking"]'):
                continue
            article_text = (getattr(article, "text", "") or "").lower()
            if "promoted" in article_text or "推广" in article_text:
                continue
            for link in article.find_elements(By.CSS_SELECTOR, 'a[href*="/status/"]'):
                url = canonical_status_url(link.get_attribute("href"))
                if url and url not in local and not self.state.has_seen(url):
                    local.add(url)
                    out.append({"url": url})
                    break
        return out

    def extract_item(self, source: str) -> Optional[Dict[str, Any]]:
        url = canonical_status_url(getattr(self.driver, "current_url", ""))
        if not url:
            return None
        articles = self.driver.find_elements(By.CSS_SELECTOR, 'article[data-testid="tweet"]')
        if not articles:
            return None
        article = None
        for candidate in articles:
            for link in candidate.find_elements(By.CSS_SELECTOR, 'a[href*="/status/"]'):
                if canonical_status_url(link.get_attribute("href")) == url:
                    article = candidate
                    break
            if article is not None:
                break
        if article is None:
            return None
        if article.find_elements(By.CSS_SELECTOR, '[data-testid="promotedIndicator"], [data-testid="placementTracking"]'):
            return None
        text_elements = article.find_elements(By.CSS_SELECTOR, '[data-testid="tweetText"]')
        user_elements = article.find_elements(By.CSS_SELECTOR, '[data-testid="User-Name"]')
        user_text = (user_elements[0].text if user_elements else "") or ""
        handle = ""
        for line in user_text.splitlines():
            if line.startswith("@") and HANDLE_RE.fullmatch(line.strip()):
                handle = line.strip()
                break
        if not handle:
            handle = "@" + url.split("/")[3]
        status_id = url.rsplit("/", 1)[-1]
        item = {
            "item_key": status_id,
            "url": url,
            "source": str(source),
            "author_handle": handle,
            "text": ((text_elements[0].text if text_elements else "") or "")[:10000],
            "observed_at": int(time.time()),
        }
        metrics: Dict[str, Optional[str]] = {}
        for key, selector in (("reply_count", '[data-testid="reply"]'), ("repost_count", '[data-testid="retweet"]'), ("like_count", '[data-testid="like"]'), ("view_count", 'a[href$="/analytics"]')):
            elements = article.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                metrics[key] = (elements[0].get_attribute("aria-label") or elements[0].text or "")[:200]
        if metrics:
            item["metrics"] = metrics
        self.state.forward("item_extracted", url=url)
        return item


def canonical_status_url(value: Any) -> Optional[str]:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
    except Exception:
        return None
    if parsed.scheme != "https" or parsed.hostname not in {"x.com", "www.x.com"} or parsed.username or parsed.password or parsed.port:
        return None
    match = STATUS_RE.fullmatch(parsed.path)
    return f"https://x.com/{match.group(1)}/status/{match.group(2)}" if match else None


def normalize_handle(value: Any) -> str:
    match = HANDLE_RE.fullmatch(str(value or "").strip())
    return match.group(1) if match else ""


def short(exc: BaseException) -> str:
    return str(exc).replace("\r", " ").replace("\n", " ")[:500]


def contains_forbidden_action(value: Any) -> bool:
    forbidden = {"action", "actions", "interaction", "interactions", "publish", "post", "like", "reply", "follow", "repost", "comment"}
    if isinstance(value, dict):
        return any(str(key).strip().lower() in forbidden or contains_forbidden_action(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_forbidden_action(item) for item in value)
    return False


def parse_snapshot(job: Dict[str, Any]) -> Dict[str, Any]:
    raw = job.get("config_snapshot")
    try:
        snapshot = json.loads(raw) if isinstance(raw, str) else raw
    except Exception as exc:
        raise WorkerError("config_snapshot is invalid JSON") from exc
    if not isinstance(snapshot, dict):
        raise WorkerError("config_snapshot must decode to an object")
    if snapshot.get("read_only") is not True:
        raise WorkerError("Job snapshot is not explicitly read_only")
    if snapshot.get("job_type") != job.get("job_type"):
        raise WorkerError("Job type and snapshot job_type differ")
    if contains_forbidden_action(snapshot):
        raise WorkerError("Snapshot contains unsupported action/interaction/publish fields")
    return snapshot


def validate_job(job: Any) -> Dict[str, Any]:
    if not isinstance(job, dict):
        raise WorkerError("Claim returned an invalid job")
    for key in ("id", "profile_id", "job_type", "config_snapshot", "reserved_seconds", "execution_token"):
        if key not in job:
            raise WorkerError(f"Claim lacks {key}")
    if not str(job.get("execution_token") or "").strip():
        raise WorkerError("Protocol 2 claim lacks execution_token")
    if job["job_type"] not in {"browse", "probe"}:
        raise WorkerError("Unsupported job type")
    if job["job_type"] == "browse" and int(job.get("reserved_seconds") or 0) <= 0:
        raise WorkerError("Browse job has no reserved time budget")
    profile_id = str(job.get("profile_id") or "").strip()
    if not profile_id or len(profile_id) > 128:
        raise WorkerError("Invalid profile_id")
    proxy_port = job.get("proxy_port")
    if proxy_port not in (None, ""):
        try:
            port = int(proxy_port)
        except (TypeError, ValueError) as exc:
            raise WorkerError("Invalid proxy_port") from exc
        if not 1 <= port <= 65535:
            raise WorkerError("Invalid proxy_port")
    parse_snapshot(job)
    return job


def get_exit_ip(browser: BrowserRun) -> str:
    browser.state.set_phase_source("probe_exit_ip", "https://api.ipify.org")
    browser.navigate("https://api.ipify.org")
    try:
        value = (browser.driver.find_element(By.TAG_NAME, "body").text or "").strip()
    except Exception as exc:
        raise WorkerError("Could not read exit IP in browser") from exc
    if not re.fullmatch(r"[0-9A-Fa-f:.]{3,64}", value):
        raise WorkerError("Browser returned an invalid exit IP")
    browser.state.event("exit_ip_observed")
    return value


def run_probe(browser: BrowserRun, expected_handle: str) -> Dict[str, Any]:
    exit_ip = get_exit_ip(browser)
    browser.state.set_phase_source("probe_x_login", "https://x.com/home")
    browser.navigate("https://x.com/home")
    browser.state.sleep(3)
    login_status = browser.detect_account_state()
    observed = browser.verify_handle(expected_handle)
    browser.state.set_observed_handle(observed)
    if not observed and expected_handle:
        login_status = "pending_manual_login"
        raise ManualActionRequired("Logged-in handle could not be established safely")
    browser.state.event("probe_complete", login_status=login_status)
    browser.state.checkpoint(force=True)
    return {"exit_ip": exit_ip, "observed_handle": observed, "proxy_status": "ok", "login_status": login_status}


def browse_one_item(browser: BrowserRun, candidate: Dict[str, str], source: str, dwell_range: Tuple[float, float]) -> bool:
    url = candidate["url"]
    browser.state.set_phase_source(browser.state.phase, source)
    browser.navigate(url)
    browser.state.sleep(2)
    item = browser.extract_item(source)
    if not item:
        browser.state.event("item_unavailable", url=url)
        return False
    browser.state.add_seen_item(url, item)
    browser.state.event("item_observed", url=url, source=source)
    dwell = random.uniform(*dwell_range)
    dwell = min(dwell, max(0.0, browser.remaining() - 3.0))
    if dwell > 0:
        browser.state.sleep(dwell)
    return True


def browse_source(browser: BrowserRun, source_url: str, source: str, target: int, phase: str, dwell_range: Tuple[float, float], max_failures: int) -> int:
    count = 0
    failures = 0
    browser.state.set_phase_source(phase, source)
    browser.navigate(source_url)
    browser.state.sleep(3)
    while count < target:
        browser.require_time(5)
        candidates = browser.collect_status_links()
        if not candidates:
            failures += 1
            if failures >= max_failures:
                break
            browser.scroll()
            continue
        for candidate in candidates:
            if count >= target:
                break
            browser.require_time(5)
            if browse_one_item(browser, candidate, source, dwell_range):
                count += 1
                failures = 0
                browser.state.increment(phase)
            else:
                failures += 1
            browser.state.checkpoint()
            if count < target:
                browser.state.set_phase_source(phase, source)
                browser.navigate(source_url)
                browser.state.sleep(random.uniform(1.0, 4.0))
            if failures >= max_failures:
                break
        if count < target and failures < max_failures:
            browser.scroll()
    return count


def discover_trends(browser: BrowserRun) -> List[Tuple[str, str]]:
    browser.state.set_phase_source("trending_discovery", "X Explore trending")
    browser.navigate("https://x.com/explore/tabs/trending")
    links: List[Tuple[str, str]] = []
    seen = set()
    selectors = ('a[href^="/search?q="]', 'a[href*="/search?q="]')
    for attempt in range(6):
        browser.state.sleep(5 if attempt == 0 else 2)
        for selector in selectors:
            for element in browser.driver.find_elements(By.CSS_SELECTOR, selector):
                href = str(element.get_attribute("href") or "")
                parsed = urllib.parse.urlsplit(href)
                if parsed.hostname not in {"x.com", "www.x.com"} or parsed.path != "/search":
                    continue
                query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
                if not query:
                    continue
                url = "https://x.com/search?q=" + urllib.parse.quote(query, safe="") + "&src=trend_click&f=live"
                if url not in seen:
                    seen.add(url)
                    links.append((query, url))
        if links:
            break
    if not links:
        trend_cells = len(browser.driver.find_elements(By.CSS_SELECTOR, '[data-testid="trend"]'))
        raise SourceUnavailable(f"X Explore trending DOM/source unavailable; {trend_cells} trend cells but no usable search links")
    browser.state.event("trends_discovered", count=len(links))
    return links


def run_browse(browser: BrowserRun, snapshot: Dict[str, Any], expected_handle: str) -> Dict[str, Any]:
    browser.state.set_phase_source("account_check", "https://x.com/home")
    browser.navigate("https://x.com/home")
    browser.state.sleep(3)
    observed = browser.verify_handle(expected_handle)
    if expected_handle and not observed:
        raise ManualActionRequired("Logged-in handle could not be established safely")
    dwell_raw = snapshot.get("dwell_seconds", [20, 45])
    if not isinstance(dwell_raw, list) or len(dwell_raw) != 2:
        raise WorkerError("snapshot dwell_seconds must be a two-number array")
    dwell = (max(20.0, float(dwell_raw[0])), min(45.0, float(dwell_raw[1])))
    if dwell[0] > dwell[1]:
        raise WorkerError("snapshot dwell_seconds is invalid")
    max_failures = max(1, min(20, int(snapshot.get("max_failures", 3))))
    requested = 0
    completed = 0
    keywords = snapshot.get("keywords") or []
    if not isinstance(keywords, list):
        raise WorkerError("snapshot keywords must be an array")
    for spec in keywords:
        if not isinstance(spec, dict):
            raise WorkerError("keyword entry must be an object")
        keyword = str(spec.get("keyword") or "").strip()
        target = max(0, min(500, int(spec.get("target", 0))))
        if not keyword or not target:
            continue
        requested += target
        url = "https://x.com/search?q=" + urllib.parse.quote(keyword, safe="") + "&src=typed_query&f=live"
        completed += browse_source(browser, url, f"search:{keyword}", target, "search", dwell, max_failures)
    trending_target = max(0, min(500, int(snapshot.get("trending_target", 0))))
    requested += trending_target
    if trending_target:
        trends = discover_trends(browser)
        remaining = trending_target
        for label, url in trends:
            if remaining <= 0:
                break
            got = browse_source(browser, url, f"trending:{label}", remaining, "trending", dwell, max_failures)
            completed += got
            remaining -= got
    browser.state.checkpoint(force=True)
    if completed < requested:
        raise SourceUnavailable(f"Completed {completed} of {requested} requested items before source limits")
    return {"observed_handle": observed, "login_status": "logged_in"}



@dataclass
class ChildState:
    phase: str = "starting"
    source: str = ""
    search_count: int = 0
    trending_count: int = 0
    useful_progress: bool = False
    observed_handle: str = ""
    seen: set = field(default_factory=set)
    events: List[Dict[str, Any]] = field(default_factory=list)
    pending_items: List[Dict[str, Any]] = field(default_factory=list)
    last_forward_progress_at: float = field(default_factory=time.monotonic)

    def _emit(self, kind: str, **payload: Any) -> None:
        sink = getattr(self, "ipc_queue", None)
        if sink is not None:
            sink.put({"kind": kind, **payload})

    def forward(self, milestone: str, **payload: Any) -> None:
        self.last_forward_progress_at = time.monotonic()
        self._emit("forward_progress", milestone=milestone, at=time.time(), **payload)

    def set_phase_source(self, phase: str, source: str) -> None:
        self.phase, self.source = phase, source
        self._emit("snapshot", phase=phase, current_source=source)
        self.forward("phase", phase=phase, source=source)

    def set_observed_handle(self, value: str) -> None:
        self.observed_handle = value
        self._emit("snapshot", observed_handle=value)

    def mark_useful(self) -> None:
        self.useful_progress = True

    def increment(self, phase: str) -> None:
        if phase == "search": self.search_count += 1
        else: self.trending_count += 1
        self._emit("snapshot", search_count=self.search_count, trending_count=self.trending_count)
        self.forward("item_count", phase=phase)

    def has_seen(self, value: str) -> bool:
        return value in self.seen

    def add_seen_item(self, url: str, item: Dict[str, Any]) -> None:
        self.seen.add(url); self.pending_items.append(item); self.useful_progress = True
        self._emit("item", item=item)
        self.forward("item", url=url)

    def event(self, event_type: str, **payload: Any) -> None:
        event = {"event_type": event_type, "created_at": int(time.time()), **payload}
        self.events.append(event); self._emit("event", event=event)
        if event_type in {"exit_ip_observed", "probe_complete", "trends_discovered", "item_observed"}:
            self.forward(event_type)

    def checkpoint(self, *, force: bool = False) -> None:
        if getattr(self, "cancel_event", None) is not None and self.cancel_event.is_set():
            raise Cancelled("Cancelled by supervisor")
        if force: self.forward("checkpoint")

    def sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            self.checkpoint()
            time.sleep(min(.25, max(0.0, deadline - time.monotonic())))

    def result_snapshot(self) -> Tuple[bool, str]:
        return self.useful_progress, self.observed_handle


def child_job_target(config_payload: Dict[str, Any], job: Dict[str, Any], launch: Dict[str, Any], ipc_queue: Any, cancel_event: Any) -> None:
    state = ChildState(); state.ipc_queue = ipc_queue; state.cancel_event = cancel_event
    driver = None
    result = {"status": "failed", "error": None, "observed_handle": None, "exit_ip": None, "proxy_status": None, "login_status": None}
    started = time.monotonic()
    try:
        snapshot = parse_snapshot(job)
        driver = AdsPowerClient.attach(launch)
        state.forward("attached")
        timeout = float(config_payload["probe_hard_runtime_seconds"]) if job["job_type"] == "probe" else min(3600, max(1, int(job.get("reserved_seconds") or 0)))
        browser = BrowserRun(driver, state, started + timeout)
        details = run_probe(browser, str(job.get("expected_handle") or "")) if job["job_type"] == "probe" else run_browse(browser, snapshot, str(job.get("expected_handle") or ""))
        result.update(details); result["status"] = "succeeded"
    except Cancelled as exc:
        result.update(status="cancelled", error=short(exc), failure_code="manual_cancel")
    except ManualActionRequired as exc:
        message = short(exc); code = "handle_mismatch" if "mismatch" in message.lower() else "authentication"
        result.update(status="manual_action_required", error=message, failure_code=code, login_status="restricted" if any(x in message.lower() for x in ("restricted", "suspended", "locked")) else "pending_manual_login")
    except BudgetExpired as exc:
        result.update(status="partial" if state.useful_progress else "failed", error=short(exc), failure_code="timeout")
    except SourceUnavailable as exc:
        result.update(status="partial" if state.useful_progress else "failed", error=short(exc), failure_code="source")
    except (WebDriverException, WorkerError, ValueError) as exc:
        result.update(status="partial" if state.useful_progress else "failed", error=short(exc), failure_code="browser")
    except BaseException as exc:
        result.update(status="partial" if state.useful_progress else "failed", error="Unhandled child error: " + short(exc), failure_code="browser")
    finally:
        if driver is not None: AdsPowerClient.detach(driver)
        state.forward("child_cleanup")
        if not result.get("observed_handle") and state.observed_handle: result["observed_handle"] = state.observed_handle
        result["useful_progress"] = state.useful_progress
        ipc_queue.put({"kind": "result", "result": result})


@dataclass
class Execution:
    job: Dict[str, Any]
    profile_id: str
    proxy_port: Optional[int]
    execution_token: str
    profile_lock: threading.Lock
    process: Any = None
    ipc_queue: Any = None
    cancel_event: Any = None
    launch: Optional[Dict[str, Any]] = None
    launch_evidence: Optional[Dict[str, Any]] = None
    owned_launch: bool = False
    ownership_uncertain: bool = False
    prelaunch_active: Optional[bool] = None
    state: str = "registered"
    cleanup_state: str = "none"
    cleanup_confirmed: bool = False
    started_epoch: float = field(default_factory=time.time)
    started_monotonic: float = field(default_factory=time.monotonic)
    last_forward_progress_at: float = field(default_factory=time.monotonic)
    last_progress_sent: float = 0.0
    phase: str = "starting"
    source: str = ""
    search_count: int = 0
    trending_count: int = 0
    seen: set = field(default_factory=set)
    events: List[Dict[str, Any]] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    result: Optional[Dict[str, Any]] = None
    watchdog_code: Optional[str] = None
    reporting: bool = False
    completion_payload: Optional[Dict[str, Any]] = None
    next_completion_attempt: float = 0.0
    completion_attempts: int = 0
    cancel_requested_at: Optional[float] = None
    cancel_reason: Optional[str] = None
    resources_released: bool = False

    @property
    def job_id(self) -> int: return int(self.job["id"])


class Supervisor:
    def __init__(self, cfg: Config, controller: ControllerClient, ads: AdsPowerClient, mp_context: Any = None, psutil_module: Any = None, journal_path: Optional[Path] = None):
        self.cfg, self.controller, self.ads = cfg, controller, ads
        self.mp = mp_context or multiprocessing.get_context("spawn")
        self.psutil = psutil_module
        if self.psutil is None:
            try: import psutil as imported_psutil
            except ImportError: imported_psutil = None
            self.psutil = imported_psutil
        self.lock = threading.RLock(); self.executions: Dict[int, Execution] = {}; self.reporting: Dict[int, Execution] = {}
        self.active_profiles = set(); self.active_proxy_ports = set(); self.profile_locks: Dict[str, threading.Lock] = {}; self.draining = False
        self.journal_path = Path(journal_path or cfg.reconciliation_journal)
        self.journal_corrupt = False
        self.load_journal()

    def available_slots(self) -> int:
        return max(0, self.cfg.max_concurrent_jobs - len(self.executions)) if not self.draining else 0

    def _entry_data(self, x: Execution) -> Dict[str, Any]:
        return {"job": x.job, "profile_id": x.profile_id, "proxy_port": x.proxy_port, "execution_token": x.execution_token,
                "launch": x.launch, "launch_evidence": x.launch_evidence, "owned_launch": x.owned_launch,
                "ownership_uncertain": x.ownership_uncertain, "prelaunch_active": x.prelaunch_active, "state": x.state,
                "cleanup_state": x.cleanup_state, "cleanup_confirmed": x.cleanup_confirmed, "started_epoch": x.started_epoch, "result": x.result,
                "watchdog_code": x.watchdog_code, "reporting": x.reporting, "completion_payload": x.completion_payload,
                "completion_attempts": x.completion_attempts, "cancel_reason": x.cancel_reason}

    def persist_journal(self) -> None:
        entries = [self._entry_data(x) for x in list(self.executions.values()) + list(self.reporting.values())]
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.journal_path.with_name(self.journal_path.name + ".tmp-" + uuid.uuid4().hex)
        try:
            with temp.open("w", encoding="utf-8") as handle:
                json.dump({"version": 1, "entries": entries}, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, self.journal_path)
        finally:
            try:
                if temp.exists(): temp.unlink()
            except OSError: pass

    def _lock_recovered(self, x: Execution) -> None:
        self.active_profiles.add(x.profile_id)
        if x.proxy_port is not None: self.active_proxy_ports.add(x.proxy_port)

    def load_journal(self) -> None:
        if not self.journal_path.exists(): return
        try:
            raw=json.loads(self.journal_path.read_text(encoding="utf-8")); entries=raw.get("entries")
            if not isinstance(entries,list): raise ValueError("entries is not a list")
        except Exception as exc:
            self.journal_corrupt=True; self.draining=True
            LOG.error("Reconciliation journal is corrupt; claims disabled: %s", short(exc)); return
        for data in entries:
            try:
                job=data["job"]; profile=str(data["profile_id"]); port=data.get("proxy_port"); lock=self.profile_locks.setdefault(profile,threading.Lock()); lock.acquire(False)
                x=Execution(job,profile,port,str(data.get("execution_token") or ""),lock)
                recovered_state=str(data.get("state") or x.state)
                for key in ("launch","launch_evidence","owned_launch","ownership_uncertain","prelaunch_active","cleanup_state","cleanup_confirmed","started_epoch","result","watchdog_code","reporting","completion_payload","completion_attempts","cancel_reason"):
                    if key in data: setattr(x,key,data[key])
                x.state=recovered_state; x.reporting=True; self.reporting[x.job_id]=x
                if x.cleanup_confirmed: self._release_resources(x)
                else: self._lock_recovered(x)
            except Exception as exc:
                self.journal_corrupt=True; self.draining=True; LOG.error("Invalid reconciliation entry retained conservatively: %s",short(exc))

    def recover_journal(self) -> None:
        for x in list(self.reporting.values()):
            if x.cleanup_confirmed: continue
            if x.state in {"registered","controller_starting","ownership_check"} and not x.owned_launch and not x.ownership_uncertain:
                x.cleanup_state="not_required"; x.cleanup_confirmed=True
                x.result=x.result or {"status":"failed","error":"Worker restarted before AdsPower launch","failure_code":"controller_unavailable","failure_detail":"restart_before_launch"}
                self._release_resources(x)
            elif x.owned_launch and x.launch_evidence:
                if x.result is None: x.result={"status":"failed","error":"Worker restarted during execution","failure_code":"cleanup_uncertain","failure_detail":"restart_recovery"}
                self.cleanup(x)
                if x.cleanup_confirmed: self._release_resources(x)
            else:
                x.ownership_uncertain=True; x.cleanup_state="uncertain"; x.cleanup_confirmed=False
                x.result=x.result or {"status":"failed","error":"Insufficient ownership evidence after restart","failure_code":"cleanup_uncertain","failure_detail":"cleanup_uncertain"}
        self.persist_journal()
        for x in list(self.reporting.values()):
            if self.try_complete(x): self.forget(x)

    def register(self, job: Dict[str, Any]) -> Tuple[Optional[Execution], Optional[str]]:
        jid=int(job["id"]); profile=str(job["profile_id"]); port=int(job["proxy_port"]) if job.get("proxy_port") not in (None, "") else None
        token=str(job.get("execution_token") or "").strip()
        if not token: return None, "Protocol 2 claim lacks execution_token"
        with self.lock:
            if self.available_slots() <= 0: return None, "Worker capacity was filled before registration"
            if jid in self.executions or jid in self.reporting: return None, "Controller leased a duplicate active job_id"
            if profile in self.active_profiles: return None, "Controller leased duplicate active profile"
            if port is not None and port in self.active_proxy_ports: return None, "Controller leased duplicate active proxy port"
            lock=self.profile_locks.setdefault(profile, threading.Lock())
            if not lock.acquire(False): return None, "Profile lock is already held"
            entry=Execution(job, profile, port, token, lock); self.executions[jid]=entry; self.active_profiles.add(profile)
            if port is not None: self.active_proxy_ports.add(port)
            return entry, None

    def _release_resources(self, x: Execution) -> None:
        if x.resources_released: return
        self.active_profiles.discard(x.profile_id)
        if x.proxy_port is not None: self.active_proxy_ports.discard(x.proxy_port)
        try: x.profile_lock.release()
        except RuntimeError: pass
        x.resources_released=True

    def heartbeat_payload(self, *, draining: Optional[bool] = None) -> Dict[str, Any]:
        draining=self.draining if draining is None else draining; all_x=list(self.executions.values())+list(self.reporting.values())
        executions=[{"job_id":x.job_id,"execution_token":x.execution_token,"account_id":x.job.get("account_id"),"profile_id":x.profile_id,"proxy_port":x.proxy_port,"state":x.state,"child_pid":getattr(x.process,"pid",None),"last_forward_progress_at":int(time.time()-(time.monotonic()-x.last_forward_progress_at)),"cleanup_state":x.cleanup_state,"cleanup_confirmed":x.cleanup_confirmed} for x in all_x]
        ids=sorted(x.job_id for x in all_x)
        return {"status":"draining" if draining else ("busy" if ids else "idle"),"draining":draining,"capacity":self.cfg.max_concurrent_jobs,"available_slots":0 if draining else self.available_slots(),"active_job_ids":ids,"active_profile_ids":sorted(self.active_profiles),"active_proxy_ports":sorted(self.active_proxy_ports),"current_job_id":ids[0] if len(ids)==1 else None,"protocol_version":PROTOCOL_VERSION,"capabilities":CAPABILITIES,"executions":executions}

    def send_heartbeat(self, *, draining: Optional[bool] = None) -> Dict[str, Any]:
        response=self.controller.heartbeat(self.heartbeat_payload(draining=draining)); self.apply_directives(response.get("directives", [])); return response

    def forget(self, x: Execution) -> None:
        self.reporting.pop(x.job_id,None); x.reporting=False; self.persist_journal()

    def apply_directives(self, directives: Any) -> None:
        for directive in directives if isinstance(directives,list) else []:
            if not isinstance(directive,dict): continue
            try: jid=int(directive.get("job_id"))
            except (TypeError,ValueError): continue
            x=self.executions.get(jid) or self.reporting.get(jid); action=directive.get("directive")
            if not x: continue
            if action in {"cancel","stop_and_cleanup","quarantine"}: self.request_cancel(x, str(action))
            elif action=="forget" and x.cleanup_confirmed: self.forget(x)

    @staticmethod
    def _failure(code: str, detail: str, error: str, status: str = "failed") -> Dict[str, Any]:
        return {"status":status,"error":error,"failure_code":code,"failure_detail":detail,"useful_progress":False}

    def start_registered(self, x: Execution) -> None:
        try:
            x.state="controller_starting"; self.persist_journal()
            self.controller.request("POST",f"/api/worker/jobs/{x.job_id}/start",{"execution_token":x.execution_token},execution_token=x.execution_token)
            x.state="ownership_check"; self.persist_journal()
            active=self.ads.active_state(x.profile_id); x.prelaunch_active=active; self.persist_journal()
            if active is True:
                x.result=self._failure("manual_profile_in_use","manual_profile_in_use","Profile was active before this execution; manual use is protected","manual_action_required")
                x.cleanup_confirmed=True; x.cleanup_state="not_required"; self.finalize(x); return
            launch_epoch=time.time()
            x.launch_evidence={"profile_id":x.profile_id,"job_id":x.job_id,"run_id":x.job.get("run_id"),"launch_started_at":launch_epoch,"launch_epoch":launch_epoch}
            x.state="launching"; self.persist_journal()
            try: launch=self.ads.start(x.profile_id)
            except AdsPowerError as exc:
                message=short(exc); post_active=self.ads.active_state(x.profile_id)
                if active is False and post_active is True:
                    x.owned_launch=True; x.result=self._failure("browser_start_failed","ambiguous_start_became_active",message)
                elif post_active is False:
                    x.result=self._failure("browser_start_failed","start_failed_inactive",message); x.cleanup_confirmed=True; x.cleanup_state="not_required"
                elif active is None:
                    x.ownership_uncertain=True; x.result=self._failure("cleanup_uncertain","ownership_uncertain",message); x.cleanup_state="uncertain"; x.cleanup_confirmed=False
                elif "marionette_port" in message:
                    x.ownership_uncertain=True; x.result=self._failure("cleanup_uncertain","ownership_uncertain",message); x.cleanup_state="uncertain"; x.cleanup_confirmed=False
                else:
                    x.result=self._failure("adspower_unavailable","adspower_unavailable",message)
                self.finalize(x); return
            x.owned_launch=True; x.launch=launch
            x.launch_evidence.update({"webdriver":launch.get("webdriver"),"marionette_port":launch.get("marionette_port"),"debug_port":launch.get("debug_port"),"ws_port":launch.get("ws_port") or launch.get("websocket_port")})
            self.persist_journal()
            x.ipc_queue=self.mp.Queue(); x.cancel_event=self.mp.Event(); cfg_payload={"probe_hard_runtime_seconds":self.cfg.probe_hard_runtime_seconds}
            x.process=self.mp.Process(target=child_job_target,args=(cfg_payload,x.job,launch,x.ipc_queue,x.cancel_event),name=f"x-browse-job-{x.job_id}"); x.process.daemon=False; x.process.start()
            x.state="running"; x.last_forward_progress_at=time.monotonic(); self.persist_journal()
        except BaseException as exc:
            if x.result is None:
                code="browser_start_failed" if x.owned_launch or x.state in {"launching","running"} else "controller_unavailable"
                x.result=self._failure(code,code,short(exc))
            self.finalize(x)

    def drain_ipc(self, x: Execution) -> None:
        if x.ipc_queue is None:return
        changed=False
        while True:
            try: msg=x.ipc_queue.get_nowait()
            except queue.Empty: break
            kind=msg.get("kind"); changed=True
            if kind=="forward_progress": x.last_forward_progress_at=time.monotonic(); x.events.append({"event_type":"forward_progress","created_at":int(time.time()),"milestone":msg.get("milestone")})
            elif kind=="snapshot":
                x.phase=msg.get("phase",x.phase); x.source=msg.get("current_source",x.source); x.search_count=max(x.search_count,int(msg.get("search_count",0))); x.trending_count=max(x.trending_count,int(msg.get("trending_count",0)))
            elif kind=="event": x.events.append(msg["event"])
            elif kind=="item":
                item=msg["item"]; x.items.append(item); x.seen.add(str(item.get("url") or item.get("item_key")))
            elif kind=="result": x.result=msg["result"]
        if changed:self.persist_journal()

    def elapsed_seconds(self, x: Execution) -> int:
        elapsed=max(0,int(time.time()-float(x.started_epoch or time.time())))
        if x.job["job_type"]=="probe": return elapsed
        return min(elapsed,min(3600,max(0,int(x.job.get("reserved_seconds") or 0))))

    def progress_payload(self, x: Execution) -> Dict[str, Any]:
        events,x.events=x.events,[]; items,x.items=x.items,[]
        return {"execution_token":x.execution_token,"phase":x.phase,"current_source":x.source,"elapsed_seconds":self.elapsed_seconds(x),"search_count":x.search_count,"trending_count":x.trending_count,"unique_items":len(x.seen),"events":events,"items":items}

    def send_progress(self, x: Execution) -> None:
        body=self.progress_payload(x)
        try: response=self.controller.request("POST",f"/api/worker/jobs/{x.job_id}/progress",body,execution_token=x.execution_token)
        except Exception:
            x.events=body["events"]+x.events; x.items=body["items"]+x.items; raise
        x.last_progress_sent=time.monotonic(); directive=response.get("directive")
        if response.get("accepted") is False or directive in {"cancel","stop_and_cleanup","quarantine"}: self.request_cancel(x, str(directive or "not_accepted"))

    def check_control(self, x: Execution) -> None:
        response=self.controller.request("GET",f"/api/worker/jobs/{x.job_id}/control",execution_token=x.execution_token); directive=response.get("directive")
        if response.get("cancel_requested") or directive in {"cancel","stop_and_cleanup","quarantine"}: self.request_cancel(x,str(directive or "cancel"))

    def request_cancel(self, x: Execution, reason: str) -> None:
        if x.cancel_requested_at is None: x.cancel_requested_at=time.monotonic(); x.cancel_reason=reason
        if x.cancel_event is not None:x.cancel_event.set()
        x.state="stopping"; x.events.append({"event_type":"cancel_requested","created_at":int(time.time()),"reason":reason}); self.persist_journal()

    def hard_runtime(self, x: Execution) -> float:
        return self.cfg.probe_hard_runtime_seconds if x.job["job_type"]=="probe" else min(3600,max(1,int(x.job.get("reserved_seconds") or 0)))+self.cfg.hard_runtime_grace_seconds

    def check_watchdogs(self, x: Execution, now: Optional[float]=None) -> Optional[str]:
        now=time.monotonic() if now is None else now
        if now-x.started_monotonic>=self.hard_runtime(x): return "hard_runtime_exceeded"
        if now-x.last_forward_progress_at>=self.cfg.no_forward_progress_seconds: return "no_forward_progress"
        return None

    def terminate_child(self, x: Execution, cooperative: bool=True) -> None:
        p=x.process
        if p is None or not p.is_alive(): return
        if x.cancel_event is not None:x.cancel_event.set()
        if cooperative:p.join(self.cfg.cooperative_cancel_seconds)
        if p.is_alive(): p.terminate(); p.join(self.cfg.terminate_grace_seconds)
        if p.is_alive(): p.kill(); p.join(self.cfg.terminate_grace_seconds)

    @staticmethod
    def _basename(value: str) -> str:
        return value.replace("\\","/").rsplit("/",1)[-1].lower()

    def _proc_data(self, proc: Any) -> Optional[Dict[str, Any]]:
        try:
            exe=str(proc.exe() or ""); cmd_parts=[str(v) for v in proc.cmdline()]; cmd=" ".join(cmd_parts); name=str(proc.name() if hasattr(proc,"name") else self._basename(exe)); created=float(proc.create_time()); pid=int(proc.pid)
        except Exception:return None
        ports=set()
        for method in ("net_connections","connections"):
            if not hasattr(proc,method):continue
            try:
                for conn in getattr(proc,method)(kind="inet"):
                    status=str(getattr(conn,"status","") or "").upper(); laddr=getattr(conn,"laddr",None); port=getattr(laddr,"port",None) if laddr is not None else None
                    if port is None and isinstance(laddr,(tuple,list)) and len(laddr)>1:port=laddr[1]
                    if port is not None and status in {"LISTEN","LISTENING","NONE",""}:ports.add(str(port))
                break
            except Exception:continue
        return {"proc":proc,"pid":pid,"exe":exe.lower(),"base":self._basename(exe),"name":name.lower(),"cmd":cmd.lower(),"created":created,"ports":ports}

    def matched_processes(self, x: Execution) -> List[Any]:
        e=x.launch_evidence or {}; started=float(e.get("launch_started_at") or e.get("launch_epoch") or 0); profile=str(e.get("profile_id") or x.profile_id).lower()
        if not self.psutil or not started or not profile:return []
        expected_ports={str(e[k]) for k in ("marionette_port","debug_port","ws_port") if e.get(k) not in (None,"")}; webdriver=str(e.get("webdriver") or "").replace("\\","/").lower()
        rows=[]
        for proc in self.psutil.process_iter():
            data=self._proc_data(proc)
            if data:rows.append(data)
        roots=set()
        for data in rows:
            if data["created"]+2 < started:continue
            if data["base"] in {"adspower.exe","adspower"} or data["name"] in {"adspower.exe","adspower"}:continue
            flower=data["base"] in {"flowerbrowser.exe","flowerbrowser"} or data["name"] in {"flowerbrowser.exe","flowerbrowser"}
            profile_match=flower and (profile in data["cmd"] or ("--user-data-dir" in data["cmd"] and profile in data["cmd"]))
            port_match=bool(expected_ports & data["ports"])
            driver_match=bool(webdriver and data["exe"].replace("\\","/")==webdriver)
            if profile_match or port_match or driver_match:roots.add(data["pid"])
        if not roots:return []
        matched=[]
        for data in rows:
            include=data["pid"] in roots
            if not include:
                try: include=any(int(parent.pid) in roots for parent in data["proc"].parents())
                except Exception:pass
            if include:matched.append(data["proc"])
        return matched

    def owns_process(self, proc: Any, x: Execution) -> bool:
        return any(int(item.pid)==int(proc.pid) for item in self.matched_processes(x))

    def targeted_cleanup(self, x: Execution, timeout: Optional[float]=None) -> bool:
        candidates=self.matched_processes(x)
        if not candidates:return False
        budget=max(0.0,float(self.cfg.cleanup_timeout_seconds if timeout is None else timeout)); deadline=time.monotonic()+budget
        def depth(proc: Any) -> int:
            try:return len(proc.parents())
            except Exception:return 0
        candidates.sort(key=depth,reverse=True)
        for proc in candidates:
            try:proc.terminate()
            except Exception:pass
        remaining=max(0.0,deadline-time.monotonic()); _,alive=self.psutil.wait_procs(candidates,timeout=remaining)
        for proc in sorted(alive,key=depth,reverse=True):
            try:proc.kill()
            except Exception:pass
        if alive:self.psutil.wait_procs(alive,timeout=max(0.0,deadline-time.monotonic()))
        return True

    def _bounded_ads_stop(self, profile_id: str, timeout: float) -> None:
        done=threading.Event(); failure=[]
        def invoke() -> None:
            try:
                try:self.ads.stop_and_confirm(profile_id, timeout=timeout)
                except TypeError:self.ads.stop_and_confirm(profile_id)
            except BaseException as exc:failure.append(exc)
            finally:done.set()
        thread=threading.Thread(target=invoke,name=f"cleanup-{profile_id}",daemon=True); thread.start()
        if not done.wait(max(0.0,timeout)):raise AdsPowerError("AdsPower cleanup timed out")
        if failure:raise failure[0]

    def cleanup(self, x: Execution) -> bool:
        if x.ownership_uncertain:
            x.cleanup_state="uncertain"; x.cleanup_confirmed=False; self.persist_journal(); return False
        if not x.owned_launch: x.cleanup_state="not_required"; x.cleanup_confirmed=True; self.persist_journal(); return True
        x.cleanup_state="cleaning"; self.persist_journal(); deadline=time.monotonic()+self.cfg.cleanup_timeout_seconds
        try:self._bounded_ads_stop(x.profile_id,max(0.0,deadline-time.monotonic())); x.cleanup_confirmed=True
        except Exception:
            self.targeted_cleanup(x,max(0.0,deadline-time.monotonic()))
            try:self._bounded_ads_stop(x.profile_id,max(0.0,deadline-time.monotonic())); x.cleanup_confirmed=True
            except Exception:x.cleanup_confirmed=False
        x.cleanup_state="confirmed" if x.cleanup_confirmed else "uncertain"; self.persist_journal(); return x.cleanup_confirmed

    def completion_payload(self, x: Execution) -> Dict[str, Any]:
        result=dict(x.result or {}); useful=bool(result.pop("useful_progress",False)); status=result.get("status","failed")
        if x.watchdog_code:
            status="partial" if useful else "failed"; result.update(error=x.watchdog_code.replace("_"," "),failure_code=x.watchdog_code,failure_detail=x.watchdog_code)
        if not x.cleanup_confirmed:
            status="partial" if useful else "failed"; result.update(error="Worker-owned browser cleanup could not be confirmed",failure_code="cleanup_uncertain",failure_detail=result.get("failure_detail") or "cleanup_uncertain")
        result.update(status=status,actual_seconds=0 if x.job["job_type"]=="probe" else self.elapsed_seconds(x),cleanup_confirmed=x.cleanup_confirmed,execution_token=x.execution_token)
        return result

    def try_complete(self, x: Execution) -> bool:
        x.completion_payload=x.completion_payload or self.completion_payload(x); x.completion_attempts+=1; self.persist_journal()
        try:
            response=self.controller.request("POST",f"/api/worker/jobs/{x.job_id}/complete",x.completion_payload,execution_token=x.execution_token)
            if response.get("directive")=="forget":return True
        except Exception:pass
        x.next_completion_attempt=time.monotonic()+min(60,2**min(x.completion_attempts,5)); self.persist_journal(); return False

    def finalize(self, x: Execution) -> None:
        if x.process is not None:self.terminate_child(x,cooperative=False)
        self.cleanup(x); x.state="reconciling"; x.reporting=True
        self.executions.pop(x.job_id,None)
        if x.cleanup_confirmed:self._release_resources(x)
        self.reporting[x.job_id]=x; self.persist_journal()
        if self.try_complete(x):self.forget(x)

    def tick(self) -> None:
        now=time.monotonic()
        for x in list(self.executions.values()):
            self.drain_ipc(x)
            if x.cancel_requested_at is not None and now-x.cancel_requested_at>=self.cfg.cooperative_cancel_seconds:
                self.terminate_child(x,cooperative=False); self.drain_ipc(x)
                if x.result is None:x.result={"status":"cancelled","error":"Cancelled by administrator","failure_code":"manual_cancel","failure_detail":x.cancel_reason or "cancel","useful_progress":bool(x.seen)}
                self.finalize(x); continue
            code=self.check_watchdogs(x,now)
            if code:
                x.watchdog_code=code; self.terminate_child(x,cooperative=False); self.drain_ipc(x)
                if x.result is None:x.result={"status":"failed","error":code,"failure_code":code,"failure_detail":code,"useful_progress":bool(x.seen)}
            if now-x.last_progress_sent>=self.cfg.progress_seconds:
                try:self.send_progress(x); self.check_control(x)
                except Exception:pass
            if x.process is not None and not x.process.is_alive():
                self.drain_ipc(x)
                if x.result is None:x.result={"status":"failed","error":"webdriver child exited without result","failure_code":"webdriver_frozen","failure_detail":"webdriver_frozen","useful_progress":bool(x.seen)}
                self.finalize(x)
        for x in list(self.reporting.values()):
            if now>=x.next_completion_attempt and self.try_complete(x):self.forget(x)

    def reject_mislease(self, job: Dict[str,Any], reason: str) -> None:
        payload={"status":"failed","actual_seconds":0,"error":reason,"failure_code":"configuration","failure_detail":reason,"cleanup_confirmed":True,"execution_token":str(job.get("execution_token") or "")}
        try:self.controller.request("POST",f"/api/worker/jobs/{int(job['id'])}/complete",payload,execution_token=str(job.get("execution_token") or ""))
        except Exception:pass

    def reap(self) -> None:self.tick()
    def cancel_active(self) -> None:
        self.draining=True
        for x in list(self.executions.values()):self.request_cancel(x,"shutdown")

    def wait_for_shutdown(self, timeout: float=90.0) -> List[int]:
        self.draining=True; deadline=time.monotonic()+timeout
        for x in list(self.executions.values()):
            self.terminate_child(x)
            if x.result is None:x.result={"status":"cancelled","error":"Worker shutting down","failure_code":"manual_cancel","failure_detail":"shutdown","useful_progress":bool(x.seen)}
            self.finalize(x)
        while time.monotonic()<deadline and self.reporting:
            self.tick(); time.sleep(.05)
        return sorted(self.reporting)

def configure_logging(cfg: Config, config_path: Path) -> None:
    log_path = Path(cfg.log_file)
    if not log_path.is_absolute():
        log_path = config_path.parent / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s job=%(job_id)s profile=%(profile_id)s %(message)s")
    context_filter = JobLogFilter()
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, console])


def install_signals() -> None:
    def stop(_signum: int, _frame: Any) -> None:
        STOP.set()
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="worker.json")
    args = parser.parse_args()
    config_path = Path(args.config).resolve()
    cfg = Config.load(config_path)
    configure_logging(cfg, config_path)
    install_signals()
    STOP.clear()
    multiprocessing.freeze_support()
    LOG.info("Starting X Browse Console Worker %s as %s with %s slots", VERSION, cfg.worker_id, cfg.max_concurrent_jobs)
    controller = ControllerClient(cfg)
    ads = AdsPowerClient(cfg)
    journal_path=Path(cfg.reconciliation_journal)
    if not journal_path.is_absolute(): journal_path=config_path.parent/journal_path
    supervisor = Supervisor(cfg, controller, ads, journal_path=journal_path)
    supervisor.recover_journal()
    backoff = 2.0
    last_heartbeat = 0.0
    next_claim = 0.0
    while not STOP.is_set():
        try:
            supervisor.reap()
            now = time.monotonic()
            if now - last_heartbeat >= cfg.heartbeat_seconds:
                supervisor.send_heartbeat()
                last_heartbeat = now
            claimed_any = False
            while not STOP.is_set() and supervisor.available_slots() > 0 and time.monotonic() >= next_claim:
                response = controller.request("POST", "/api/worker/claim", {})
                job = response.get("job")
                if job is None:
                    next_claim = time.monotonic() + cfg.poll_seconds
                    break
                validated = validate_job(job)
                entry, reason = supervisor.register(validated)
                if entry is None:
                    supervisor.reject_mislease(validated, reason or "Local registration rejected the lease")
                else:
                    supervisor.start_registered(entry)
                    claimed_any = True
                    supervisor.send_heartbeat()
                    last_heartbeat = time.monotonic()
                backoff = 2.0
            if not claimed_any:
                STOP.wait(min(0.25, max(0.05, next_claim - time.monotonic()) if supervisor.available_slots() else 0.25))
        except Exception as exc:
            LOG.warning("Worker loop reconnecting after error: %s", short(exc))
            STOP.wait(backoff + random.uniform(0, min(1.0, backoff / 4)))
            backoff = min(60.0, backoff * 2)
    try:
        supervisor.send_heartbeat(draining=True)
    except Exception as exc:
        LOG.warning("Draining heartbeat failed: %s", short(exc))
    supervisor.cancel_active()
    remaining = supervisor.wait_for_shutdown(90.0)
    try:
        supervisor.send_heartbeat(draining=True)
    except Exception as exc:
        LOG.warning("Final draining heartbeat failed: %s", short(exc))
    if remaining:
        LOG.error("Worker shutdown timed out with pending reporting IDs: %s", remaining)
        return 1
    LOG.info("Worker stopped cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
