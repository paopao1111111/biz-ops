"""Authenticated HTTP application for the multisite weekly dashboard."""

import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import date, timedelta
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn

sys.path.insert(0, str(Path(__file__).parent))

from auth import (
    SESSION_MAX_AGE,
    authenticate,
    create_session_token,
    validate_configuration,
    validate_session_token,
)
from metrics import (
    DEFAULT_GRAIN_RANGES,
    DEFINITIONS,
    GRAIN_RANGES,
    METRIC_CATALOG,
    NORMAL_SAMPLE_THRESHOLD,
    PRODUCT_LABELS_ZH,
    PRODUCTS,
    SMALL_SAMPLE_THRESHOLD,
    RULE_VERSION,
    STATUS_LABELS_ZH,
)
from storage import connect_database, migrate_connection

logger = logging.getLogger("multisite_app")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
TEMPLATE_DIR = BASE_DIR / "templates"
SCHEMA_PATH = BASE_DIR / "schema.sql"
DB_PATH = Path(os.getenv("METRICS_DB_PATH", "/var/lib/multisite-weekly-dashboard/metrics.db"))
HOST = os.getenv("APP_HOST", "0.0.0.0")
PORT = int(os.getenv("APP_PORT", "8780"))
COOKIE_NAME = "multisite_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
WEEK_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ALLOWED_TREND_WEEKS = set(GRAIN_RANGES["week"])
GRAIN_LABELS_ZH = {"day": "日", "week": "周", "month": "月"}
MAX_TREND_PRODUCTS = 4


class LoginLimiter:
    def __init__(self, limit=5, window_seconds=300):
        self.limit = limit
        self.window_seconds = window_seconds
        self._attempts = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key, now):
        attempts = self._attempts[key]
        cutoff = now - self.window_seconds
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return attempts

    def blocked(self, key):
        now = time.time()
        with self._lock:
            return len(self._prune(key, now)) >= self.limit

    def fail(self, key):
        now = time.time()
        with self._lock:
            self._prune(key, now).append(now)

    def clear(self, key):
        with self._lock:
            self._attempts.pop(key, None)


LOGIN_LIMITER = LoginLimiter()


def connect_db(read_only=True):
    return connect_database(DB_PATH, read_only=read_only, timeout=10 if read_only else 30)


def initialize_database():
    connection = connect_db(read_only=False)
    try:
        migrate_connection(connection, SCHEMA_PATH)
    finally:
        connection.close()


def rows_by_product(rows):
    result = {}
    for row in rows:
        data = dict(row)
        denominator = data.get("activation_denominator")
        numerator = data.get("activation_numerator")
        data["activation_rate"] = (
            round(numerator / denominator * 100, 1)
            if denominator not in (None, 0) and numerator is not None
            else None
        )
        data["label"] = PRODUCT_LABELS_ZH.get(data["product"], data["product"])
        result[data["product"]] = data
    return result


def metric_row_data(row):
    data = dict(row)
    data["status_label"] = STATUS_LABELS_ZH.get(data.get("status"), data.get("status"))
    return data


def series_by_product(rows):
    result = {product: {} for product in PRODUCTS}
    for row in rows:
        data = metric_row_data(row)
        result.setdefault(data["product"], {})[data["metric_key"]] = data
    return result


def quality_row_data(row):
    data = dict(row)
    data["status_label"] = STATUS_LABELS_ZH.get(data.get("status"), data.get("status"))
    details = data.get("details")
    if details:
        try:
            data["details"] = json.loads(details)
        except json.JSONDecodeError:
            data["details"] = {"raw": details}
    return data


def choose_window_kind(connection, week_start):
    row = connection.execute(
        """SELECT window_kind
           FROM weekly_product_metrics
           WHERE week_start=? AND window_kind IN ('partial','full')
           ORDER BY CASE window_kind WHEN 'partial' THEN 0 ELSE 1 END
           LIMIT 1""",
        (week_start,),
    ).fetchone()
    return row["window_kind"] if row else None


def load_week_rows(connection, week_start, window_kind):
    return connection.execute(
        """SELECT * FROM weekly_product_metrics
           WHERE week_start=? AND window_kind=?
           ORDER BY CASE product
             WHEN 'All' THEN 0 WHEN 'iWeaver' THEN 1
             WHEN 'Palmly' THEN 2 ELSE 3 END""",
        (week_start, window_kind),
    ).fetchall()


def load_metric_rows(connection, week_start, window_kind):
    return connection.execute(
        """SELECT * FROM weekly_metric_series
           WHERE week_start=? AND window_kind=?
           ORDER BY product, metric_key""",
        (week_start, window_kind),
    ).fetchall()


def load_quality_rows(connection, week_start, window_kind):
    return connection.execute(
        """SELECT * FROM weekly_data_quality
           WHERE week_start=? AND window_kind=?
           ORDER BY scope, quality_key""",
        (week_start, window_kind),
    ).fetchall()


def previous_week(week_start):
    value = date.fromisoformat(week_start) - timedelta(days=7)
    return value.isoformat()


def get_overview(week_start=None):
    connection = connect_db()
    try:
        if week_start is None:
            row = connection.execute(
                """SELECT week_start FROM weekly_product_metrics
                   WHERE window_kind IN ('partial','full')
                   ORDER BY week_start DESC LIMIT 1"""
            ).fetchone()
            if not row:
                return {"available": False, "products": {}}
            week_start = row["week_start"]
        if not WEEK_PATTERN.match(week_start):
            raise ValueError("Invalid week format")

        window_kind = choose_window_kind(connection, week_start)
        if not window_kind:
            return {"available": False, "week_start": week_start, "products": {}}
        rows = load_week_rows(connection, week_start, window_kind)
        products = rows_by_product(rows)

        metric_rows = load_metric_rows(connection, week_start, window_kind)
        quality_rows = load_quality_rows(connection, week_start, window_kind)

        comparison_week = previous_week(week_start)
        comparison_kind = "aligned_previous" if window_kind == "partial" else "full"
        comparison_rows = load_week_rows(connection, comparison_week, comparison_kind)
        comparison_metric_rows = load_metric_rows(
            connection, comparison_week, comparison_kind
        )

        first = dict(rows[0]) if rows else {}
        last_run = connection.execute(
            "SELECT status,finished_at,error_summary FROM collector_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "available": True,
            "week_start": week_start,
            "week_end": first.get("week_end"),
            "window_start": first.get("window_start"),
            "window_end": first.get("window_end"),
            "window_kind": window_kind,
            "collected_at": first.get("collected_at"),
            "source_freshness": first.get("source_freshness"),
            "rule_version": first.get("rule_version"),
            "products": products,
            "metrics": series_by_product(metric_rows),
            "quality": [quality_row_data(row) for row in quality_rows],
            "comparison_week_start": comparison_week,
            "comparison_window_kind": comparison_kind,
            "comparison_products": rows_by_product(comparison_rows),
            "comparison_metrics": series_by_product(comparison_metric_rows),
            "collector": dict(last_run) if last_run else None,
        }
    finally:
        connection.close()


def get_available_weeks():
    connection = connect_db()
    try:
        rows = connection.execute(
            """SELECT DISTINCT week_start
               FROM weekly_product_metrics
               WHERE window_kind IN ('partial','full')
               ORDER BY week_start DESC"""
        ).fetchall()
        return [row["week_start"] for row in rows]
    finally:
        connection.close()


def get_legacy_trends(weeks=12, product=None):
    if product and product not in PRODUCTS:
        raise ValueError("Unknown product")
    weeks = max(2, min(int(weeks), 52))
    connection = connect_db()
    try:
        week_rows = connection.execute(
            """SELECT DISTINCT week_start
               FROM weekly_product_metrics
               WHERE window_kind IN ('partial','full')
               ORDER BY week_start DESC LIMIT ?""",
            (weeks,),
        ).fetchall()
        selected = [row["week_start"] for row in week_rows]
        result = []
        for week_start in reversed(selected):
            kind = choose_window_kind(connection, week_start)
            rows = load_week_rows(connection, week_start, kind)
            if product:
                rows = [row for row in rows if row["product"] == product]
            result.append(
                {
                    "week_start": week_start,
                    "window_kind": kind,
                    "products": rows_by_product(rows),
                }
            )
        return result
    finally:
        connection.close()


def parse_trend_weeks(value):
    try:
        weeks = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("weeks must be one of 4, 8, 12, 26, 52") from error
    if weeks not in ALLOWED_TREND_WEEKS:
        raise ValueError("weeks must be one of 4, 8, 12, 26, 52")
    return weeks


def parse_grain_range(grain, value):
    grain = str(grain or "week").lower()
    if grain not in GRAIN_RANGES:
        raise ValueError("grain must be day, week, or month")
    try:
        range_value = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid range for grain={grain}") from error
    if range_value not in GRAIN_RANGES[grain]:
        allowed = ", ".join(str(item) for item in GRAIN_RANGES[grain])
        raise ValueError(f"range for grain={grain} must be one of {allowed}")
    return grain, range_value


def parse_include_partial(value):
    text = str(value).lower()
    if text in {"1", "true", "yes"}:
        return True
    if text in {"0", "false", "no"}:
        return False
    raise ValueError("include_partial must be 0 or 1")


def parse_metric_products(metric_key, raw_products):
    definition = METRIC_CATALOG[metric_key]
    if raw_products in (None, ""):
        products = list(definition["products"][:1])
    else:
        products = []
        for product in str(raw_products).split(","):
            product = product.strip()
            if product and product not in products:
                products.append(product)
    if not products:
        raise ValueError("At least one product is required")
    if len(products) > MAX_TREND_PRODUCTS:
        raise ValueError(f"At most {MAX_TREND_PRODUCTS} products may be selected")
    for product in products:
        if product not in PRODUCTS:
            raise ValueError(f"Unknown product: {product}")
        if product not in definition["products"]:
            raise ValueError(f"Metric {metric_key} does not apply to {product}")
    return products


def metric_definition(metric_key):
    item = dict(METRIC_CATALOG[metric_key])
    item["key"] = metric_key
    item["product_labels"] = {
        product: PRODUCT_LABELS_ZH[product] for product in item["products"]
    }
    item["small_sample_rule"] = {
        "tiny": f"分母 1–{SMALL_SAMPLE_THRESHOLD - 1} 时隐藏比例，只显示分子/分母。",
        "small": f"分母 {SMALL_SAMPLE_THRESHOLD}–{NORMAL_SAMPLE_THRESHOLD - 1} 时显示并标记小样本。",
        "normal": f"分母达到 {NORMAL_SAMPLE_THRESHOLD} 后正常显示。",
    }
    return item


def get_metric_catalog():
    return {
        "rule_version": RULE_VERSION,
        "timezone": os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        "products": [
            {"key": product, "label": PRODUCT_LABELS_ZH[product]}
            for product in PRODUCTS
        ],
        "statuses": [
            {"key": key, "label": label}
            for key, label in STATUS_LABELS_ZH.items()
        ],
        "week_options": sorted(ALLOWED_TREND_WEEKS),
        "grain_options": {
            grain: {
                "label": GRAIN_LABELS_ZH[grain],
                "ranges": list(ranges),
                "default_range": DEFAULT_GRAIN_RANGES[grain],
            }
            for grain, ranges in GRAIN_RANGES.items()
        },
        "default_grain": "week",
        "max_products": MAX_TREND_PRODUCTS,
        "metrics": [metric_definition(key) for key in METRIC_CATALOG],
    }


def select_metric_weeks(connection, weeks, include_partial):
    kinds = "('full','partial')" if include_partial else "('full')"
    rows = connection.execute(
        f"""SELECT DISTINCT week_start
            FROM weekly_metric_series
            WHERE window_kind IN {kinds}
            ORDER BY week_start DESC LIMIT ?""",
        (weeks,),
    ).fetchall()
    return [row["week_start"] for row in reversed(rows)]


def choose_series_kind(connection, week_start, include_partial):
    allowed = ("partial", "full") if include_partial else ("full",)
    placeholders = ",".join("?" for _ in allowed)
    row = connection.execute(
        f"""SELECT window_kind FROM weekly_metric_series
            WHERE week_start=? AND window_kind IN ({placeholders})
            ORDER BY CASE window_kind WHEN 'partial' THEN 0 ELSE 1 END LIMIT 1""",
        (week_start, *allowed),
    ).fetchone()
    return row["window_kind"] if row else None


def get_metric_trends(
    metric_key,
    raw_products=None,
    range_value=12,
    chart=None,
    include_partial=True,
    grain="week",
):
    if metric_key not in METRIC_CATALOG:
        raise ValueError("Unknown metric")
    grain, range_value = parse_grain_range(grain, range_value)
    include_partial = parse_include_partial(include_partial)
    products = parse_metric_products(metric_key, raw_products)
    definition = METRIC_CATALOG[metric_key]
    if grain not in definition.get("grains", ["week"]):
        raise ValueError(f"Metric {metric_key} does not support grain={grain}")
    chart = chart or definition["default_chart"]
    if chart not in definition["charts"]:
        raise ValueError(f"Chart {chart} is not allowed for metric {metric_key}")
    if include_partial and not definition.get("partial_allowed", False):
        include_partial = False

    connection = connect_db()
    try:
        series = [
            {"product": product, "label": PRODUCT_LABELS_ZH[product], "points": []}
            for product in products
        ]
        period_metadata = []

        if grain == "week":
            selected_periods = select_metric_weeks(connection, range_value, include_partial)
            for period_start in selected_periods:
                kind = choose_series_kind(connection, period_start, include_partial)
                if not kind:
                    continue
                rows = connection.execute(
                    f"""SELECT * FROM weekly_metric_series
                        WHERE week_start=? AND window_kind=? AND metric_key=?
                          AND product IN ({','.join('?' for _ in products)})""",
                    (period_start, kind, metric_key, *products),
                ).fetchall()
                by_product = {row["product"]: metric_row_data(row) for row in rows}
                first = by_product.get(products[0], {})
                period_end = first.get("window_end")
                period_metadata.append(
                    {
                        "period_start": period_start,
                        "period_end": period_end,
                        "week_start": period_start,
                        "window_kind": kind,
                        "window_start": first.get("window_start"),
                        "window_end": period_end,
                    }
                )
                for item in series:
                    row = by_product.get(item["product"])
                    point = {
                        "period_start": period_start,
                        "period_end": period_end,
                        "week_start": period_start,
                        "window_kind": kind,
                        "value": None,
                        "numerator": None,
                        "denominator": None,
                        "status": "source_unavailable",
                        "status_label": STATUS_LABELS_ZH["source_unavailable"],
                        "quality_code": "not_collected",
                        "quality_value": None,
                    }
                    if row is not None:
                        point.update(
                            {
                                key: row.get(key)
                                for key in (
                                    "value", "numerator", "denominator", "status",
                                    "status_label", "quality_code", "quality_value",
                                    "collected_at", "source_freshness",
                                )
                            }
                        )
                        point["period_end"] = row.get("window_end")
                    item["points"].append(point)
        else:
            kind_filter = "" if include_partial else "AND window_kind='full'"
            selected = connection.execute(
                f"""SELECT DISTINCT period_start FROM period_metric_series
                    WHERE grain=? {kind_filter}
                    ORDER BY period_start DESC LIMIT ?""",
                (grain, range_value),
            ).fetchall()
            selected_periods = [row["period_start"] for row in reversed(selected)]
            for period_start in selected_periods:
                rows = connection.execute(
                    f"""SELECT * FROM period_metric_series
                        WHERE grain=? AND period_start=? AND metric_key=?
                          AND product IN ({','.join('?' for _ in products)})""",
                    (grain, period_start, metric_key, *products),
                ).fetchall()
                by_product = {row["product"]: metric_row_data(row) for row in rows}
                first = by_product.get(products[0], {})
                kind = first.get("window_kind", "full")
                period_end = first.get("period_end")
                period_metadata.append(
                    {
                        "period_start": period_start,
                        "period_end": period_end,
                        "window_kind": kind,
                    }
                )
                for item in series:
                    row = by_product.get(item["product"])
                    point = {
                        "period_start": period_start,
                        "period_end": period_end,
                        "window_kind": kind,
                        "value": None,
                        "numerator": None,
                        "denominator": None,
                        "status": "source_unavailable",
                        "status_label": STATUS_LABELS_ZH["source_unavailable"],
                        "quality_code": "not_collected",
                        "quality_value": None,
                    }
                    if row is not None:
                        point.update(
                            {
                                key: row.get(key)
                                for key in (
                                    "value", "numerator", "denominator", "status",
                                    "status_label", "quality_code", "quality_value",
                                    "collected_at", "source_freshness",
                                )
                            }
                        )
                    item["points"].append(point)

        result = {
            "metric": metric_definition(metric_key),
            "grain": grain,
            "grain_label": GRAIN_LABELS_ZH[grain],
            "range_requested": range_value,
            "chart": chart,
            "products": products,
            "include_partial": include_partial,
            "period_metadata": period_metadata,
            "series": series,
            "notes": [
                "value=null 的周期表示不可用、未上线或样本不足，折线不得跨越该缺口。",
                "日活、周活、月活均按各自周期独立去重，产品活跃不可相加。",
            ],
        }
        if grain == "week":
            result["weeks_requested"] = range_value
            result["week_metadata"] = period_metadata
        return result
    finally:
        connection.close()


def get_quality(weeks=12):
    weeks = parse_trend_weeks(weeks)
    connection = connect_db()
    try:
        selected_weeks = select_metric_weeks(connection, weeks, True)
        result = []
        sources = {}
        for week_start in selected_weeks:
            kind = choose_series_kind(connection, week_start, True)
            rows = load_quality_rows(connection, week_start, kind)
            facts = [quality_row_data(row) for row in rows]
            result.append(
                {
                    "week_start": week_start,
                    "window_kind": kind,
                    "facts": facts,
                }
            )
            for fact in facts:
                if fact["quality_key"] == "source_availability":
                    sources[fact["scope"]] = fact.get("details") or {}
        return {
            "rule_version": RULE_VERSION,
            "weeks_requested": weeks,
            "weeks": result,
            "sources": sources,
            "notes": [
                "domain 覆盖不足时只展示观察值，不外推缺失注册。",
                "Palmly 聊天指标只统计 message_id 精确关联，并同时展示关联率。",
                "产品活跃用户存在重叠，不可相加为全站活跃。",
            ],
        }
    finally:
        connection.close()


def get_definitions():
    return {
        "rule_version": RULE_VERSION,
        "timezone": os.getenv("APP_TIMEZONE", "Asia/Shanghai"),
        "activation_window_hours": 24,
        "products": {
            key: {"label": PRODUCT_LABELS_ZH[key], **value}
            for key, value in DEFINITIONS.items()
        },
        "metrics": [metric_definition(key) for key in METRIC_CATALOG],
        "statuses": STATUS_LABELS_ZH,
        "notes": [
            "周区间为周一 00:00 至下周一 00:00，采用半开区间。",
            "本周环比使用上周相同已过时长，不与完整上周直接比较。",
            "不可验证的官方注册或激活显示 —，不会用 0 代替。",
            "Palmly 仅使用 Lunara 报告及其精确消息关联，不纳入通用掌纹 Agent。",
        ],
    }


def get_health():
    if not DB_PATH.exists():
        return {"status": "degraded", "reason": "metrics database missing"}
    connection = connect_db()
    try:
        metric = connection.execute(
            "SELECT collected_at,source_freshness FROM weekly_product_metrics ORDER BY collected_at DESC LIMIT 1"
        ).fetchone()
        run = connection.execute(
            "SELECT status,finished_at,error_summary FROM collector_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        status = "ok" if metric and run and run["status"] == "success" else "degraded"
        return {
            "status": status,
            "last_collection": metric["collected_at"] if metric else None,
            "source_freshness": metric["source_freshness"] if metric else None,
            "last_run": dict(run) if run else None,
            "rule_version": RULE_VERSION,
        }
    finally:
        connection.close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WeeklyDashboard"
    sys_version = ""

    def log_message(self, message_format, *args):
        logger.info("%s - %s", self.address_string(), message_format % args)

    def _security_headers(self, cache_control="no-store"):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        self.send_header("Cache-Control", cache_control)

    def _send(self, status, body, content_type, extra_headers=None, cache_control="no-store"):
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self._security_headers(cache_control)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data, status=200, extra_headers=None):
        self._send(
            status,
            json.dumps(data, ensure_ascii=False, default=str),
            "application/json; charset=utf-8",
            extra_headers,
        )

    def _html(self, path, status=200):
        self._send(status, path.read_bytes(), "text/html; charset=utf-8")

    def _redirect(self, location):
        self._send(302, b"", "text/plain; charset=utf-8", {"Location": location})

    def _session_user(self):
        raw = self.headers.get("Cookie", "")
        parsed = cookies.SimpleCookie()
        try:
            parsed.load(raw)
        except cookies.CookieError:
            return None
        morsel = parsed.get(COOKIE_NAME)
        return validate_session_token(morsel.value) if morsel else None

    def _cookie_header(self, token, clear=False):
        value = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict"
        value += "; Max-Age=0" if clear else f"; Max-Age={SESSION_MAX_AGE}"
        if COOKIE_SECURE:
            value += "; Secure"
        return value

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 8192:
            raise ValueError("Invalid request body length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_static(self, request_path):
        name = request_path.removeprefix("/static/")
        if name not in {"app.css", "app.js"}:
            self.send_error(404)
            return
        path = STATIC_DIR / name
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), content_type, cache_control="public, max-age=300")

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon", cache_control="public, max-age=86400")
            return
        if path.startswith("/static/"):
            self._serve_static(path)
            return
        if path == "/login":
            if self._session_user():
                self._redirect("/")
            else:
                self._html(TEMPLATE_DIR / "login.html")
            return
        if path == "/api/health":
            self._json(get_health())
            return

        user = self._session_user()
        if not user:
            if path.startswith("/api/"):
                self._json({"error": "unauthorized"}, 401)
            else:
                self._redirect("/login")
            return

        try:
            query = urllib.parse.parse_qs(parsed.query)
            if path in ("/", "/index.html"):
                self._html(TEMPLATE_DIR / "index.html")
            elif path == "/api/overview":
                self._json(get_overview(query.get("week", [None])[0]))
            elif path == "/api/weeks":
                self._json(get_available_weeks())
            elif path == "/api/metrics":
                self._json(get_metric_catalog())
            elif path == "/api/trends":
                metric_key = query.get("metric", [None])[0]
                if metric_key:
                    grain = query.get("grain", ["week"])[0]
                    default_range = DEFAULT_GRAIN_RANGES.get(grain, 12)
                    range_value = query.get(
                        "range", query.get("weeks", [default_range])
                    )[0]
                    self._json(
                        get_metric_trends(
                            metric_key,
                            query.get("products", [None])[0],
                            range_value,
                            query.get("chart", [None])[0],
                            query.get("include_partial", ["1"])[0],
                            grain,
                        )
                    )
                else:
                    self._json(
                        get_legacy_trends(
                            query.get("weeks", [12])[0],
                            query.get("product", [None])[0],
                        )
                    )
            elif path == "/api/quality":
                self._json(get_quality(query.get("weeks", [12])[0]))
            elif path == "/api/definitions":
                self._json(get_definitions())
            else:
                self.send_error(404)
        except ValueError as error:
            self._json({"error": str(error)}, 400)
        except Exception:
            logger.exception("GET %s failed", path)
            self._json({"error": "internal_error"}, 500)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/login":
            client_key = self.client_address[0]
            if LOGIN_LIMITER.blocked(client_key):
                self._json({"error": "too_many_attempts"}, 429)
                return
            try:
                body = self._read_json()
                username = str(body.get("username", ""))
                password = str(body.get("password", ""))
            except Exception:
                self._json({"error": "invalid_request"}, 400)
                return
            if not authenticate(username, password):
                LOGIN_LIMITER.fail(client_key)
                self._json({"error": "invalid_credentials"}, 401)
                return
            LOGIN_LIMITER.clear(client_key)
            token = create_session_token(username)
            self._json(
                {"ok": True},
                extra_headers={"Set-Cookie": self._cookie_header(token)},
            )
            return
        if path == "/logout":
            self._json(
                {"ok": True},
                extra_headers={"Set-Cookie": self._cookie_header("", clear=True)},
            )
            return
        self.send_error(404)


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    validate_configuration()
    initialize_database()
    server = ThreadedHTTPServer((HOST, PORT), DashboardHandler)
    logger.info("Dashboard listening on %s:%s", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
