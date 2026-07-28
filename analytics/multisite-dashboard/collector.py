"""Atomic V3 weekly metric collector for the multisite dashboard."""

import argparse
import fcntl
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent))

from metrics import (
    METRIC_CATALOG,
    MULTI_GRAIN_METRICS,
    NORMAL_SAMPLE_THRESHOLD,
    PRODUCTS,
    RULE_VERSION,
    SMALL_SAMPLE_THRESHOLD,
    domain_quality_sql,
    freshness_sql,
    learning_activation_sql,
    overlap_quality_sql,
    palmly_followup_sql,
    palmly_link_quality_sql,
    registration_counts_sql,
    registration_sql,
    reports_sql,
    retention_sql,
    source_ranges_sql,
    topic_completion_sql,
    usage_sql,
)
from storage import connect_database, migrate_connection
from superset_client import SupersetClient

logger = logging.getLogger("multisite_collector")

DB_PATH = Path(os.getenv("METRICS_DB_PATH", "/var/lib/multisite-weekly-dashboard/metrics.db"))
LOCK_PATH = Path(os.getenv("COLLECTOR_LOCK_PATH", "/var/lib/multisite-weekly-dashboard/collector.lock"))
TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Shanghai")

SERIES_STATUSES = {
    "available",
    "source_unavailable",
    "pre_launch",
    "immature",
    "partial_maturity",
    "insufficient_sample",
    "linkage_incomplete",
    "left_censored",
    "not_applicable",
}
QUALITY_STATUSES = {
    "available",
    "source_unavailable",
    "pre_launch",
    "partial",
    "linkage_incomplete",
    "not_applicable",
}
NULL_VALUE_STATUSES = {"source_unavailable", "pre_launch", "immature", "not_applicable"}
SOURCE_NAMES = {"users", "chat_logs", "lunara_reports", "learning_coach"}


def business_now():
    return datetime.now(ZoneInfo(TIMEZONE))


def parse_as_of(value):
    if not value:
        return business_now()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(TIMEZONE))
    return parsed.astimezone(ZoneInfo(TIMEZONE))


def monday_start(value):
    return value.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=value.weekday()
    )


def month_start(value):
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def add_months(value, months):
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    return value.replace(year=year, month=month_zero + 1, day=1)


def sql_timestamp(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def iso_date(value):
    return value.strftime("%Y-%m-%d")


def date_key(value):
    return str(value)[:10]


def parse_date(value):
    return date.fromisoformat(date_key(value))


def parse_timestamp(value):
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None)
    return parsed


def naive(value):
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None)


def as_int(value, default=0):
    return default if value is None else int(value)


def as_float(value, default=None):
    return default if value is None else float(value)


def percentage(numerator, denominator):
    if denominator in (None, 0) or numerator is None:
        return None
    return round(float(numerator) / float(denominator) * 100, 4)


def ratio(numerator, denominator):
    if denominator in (None, 0) or numerator is None:
        return None
    return round(float(numerator) / float(denominator), 4)


def row_map(rows, key_fields, label):
    result = {}
    for row in rows:
        key = tuple(date_key(row[name]) if name == "week_start" else str(row[name]) for name in key_fields)
        if key in result:
            raise RuntimeError(f"Duplicate {label} row for {key}")
        result[key] = row
    return result


def nonnegative(row, fields, label):
    for field in fields:
        value = row.get(field)
        if value is not None and float(value) < 0:
            raise RuntimeError(f"Negative {label}.{field}: {value}")


def validate_fraction(row, numerator, denominator, label):
    num = as_int(row.get(numerator))
    den = as_int(row.get(denominator))
    if num > den:
        raise RuntimeError(f"{label}: {numerator}={num} exceeds {denominator}={den}")


def fetch_dataset(client, range_start, range_end, maturity_as_of):
    """Run every range-dependent query before any local write transaction starts."""
    start_text = sql_timestamp(range_start)
    end_text = sql_timestamp(range_end)
    maturity_text = sql_timestamp(maturity_as_of)
    queries = {
        "usage": usage_sql(start_text, end_text),
        "reports": reports_sql(start_text, end_text),
        "registration": registration_sql(start_text, end_text, maturity_text),
        "learning_activation": learning_activation_sql(start_text, end_text, maturity_text),
        "retention": retention_sql(start_text, end_text, maturity_text),
        "palm_followup": palmly_followup_sql(start_text, end_text, maturity_text),
        "topic_completion": topic_completion_sql(start_text, end_text),
        "domain_quality": domain_quality_sql(start_text, end_text),
        "palm_quality": palmly_link_quality_sql(start_text, end_text),
        "overlap": overlap_quality_sql(start_text, end_text),
    }
    results = {}
    for name, sql in queries.items():
        logger.info("Fetching V3 query: %s", name)
        results[name] = client.execute_sql(sql, limit=10000)
    return results


def fetch_operational_dataset(client, range_start, range_end, grain):
    """Fetch day/month operational series directly from DB2."""
    start_text = sql_timestamp(range_start)
    end_text = sql_timestamp(range_end)
    queries = {
        "usage": usage_sql(start_text, end_text, grain),
        "reports": reports_sql(start_text, end_text, grain),
        "registration": registration_counts_sql(start_text, end_text, grain),
        "domain_quality": domain_quality_sql(start_text, end_text, grain),
        "palm_quality": palmly_link_quality_sql(start_text, end_text, grain),
        "overlap": overlap_quality_sql(start_text, end_text, grain),
    }
    results = {}
    for name, sql in queries.items():
        logger.info("Fetching %s %s query", grain, name)
        results[name] = client.execute_sql(sql, limit=10000)
    return results


def fetch_sources(client):
    ranges = client.execute_sql(source_ranges_sql(), limit=20)
    freshness = client.execute_sql(freshness_sql(), limit=20)
    return ranges, freshness


def validate_dataset(dataset):
    usage_keys = set()
    for row in dataset["usage"]:
        key = (date_key(row["week_start"]), str(row["product"]))
        if key in usage_keys:
            raise RuntimeError(f"Duplicate usage row for {key}")
        usage_keys.add(key)
        if key[1] not in PRODUCTS:
            raise RuntimeError(f"Unexpected product from usage query: {key[1]}")
        fields = (
            "user_turns",
            "assistant_turns",
            "active_users",
            "topics",
            "new_active_users",
            "returning_active_users",
            "depth_1_turn",
            "depth_2_3_turns",
            "depth_4_9_turns",
            "depth_10_plus_turns",
            "median_user_turns",
        )
        nonnegative(row, fields, "usage")
        active = as_int(row.get("active_users"))
        if as_int(row.get("new_active_users")) + as_int(row.get("returning_active_users")) != active:
            raise RuntimeError(f"Usage new + returning does not equal active users for {key}")
        depth_total = sum(
            as_int(row.get(field))
            for field in ("depth_1_turn", "depth_2_3_turns", "depth_4_9_turns", "depth_10_plus_turns")
        )
        if depth_total != active:
            raise RuntimeError(f"Usage depth buckets do not equal active users for {key}")

    report_keys = set()
    for row in dataset["reports"]:
        week = date_key(row["week_start"])
        if week in report_keys:
            raise RuntimeError(f"Duplicate reports row for {week}")
        report_keys.add(week)
        fields = (
            "reports",
            "report_users",
            "new_report_users",
            "returning_report_users",
            "median_reports",
            "report_depth_1",
            "report_depth_2_3",
            "report_depth_4_9",
            "report_depth_10_plus",
        )
        nonnegative(row, fields, "reports")
        users = as_int(row.get("report_users"))
        if as_int(row.get("new_report_users")) + as_int(row.get("returning_report_users")) != users:
            raise RuntimeError(f"Report new + returning does not equal report users for {week}")
        depth_total = sum(
            as_int(row.get(field))
            for field in ("report_depth_1", "report_depth_2_3", "report_depth_4_9", "report_depth_10_plus")
        )
        if depth_total != users:
            raise RuntimeError(f"Report depth buckets do not equal report users for {week}")
        if users > as_int(row.get("reports")):
            raise RuntimeError(f"Report users exceed reports for {week}")

    registration_keys = set()
    for row in dataset["registration"]:
        key = (date_key(row["week_start"]), str(row["product"]))
        if key in registration_keys:
            raise RuntimeError(f"Duplicate registration row for {key}")
        registration_keys.add(key)
        if key[1] not in PRODUCTS:
            raise RuntimeError(f"Unexpected product from registration query: {key[1]}")
        nonnegative(
            row,
            ("registration_exact", "registration_attributed", "activation_numerator", "activation_denominator"),
            "registration",
        )
        validate_fraction(row, "activation_numerator", "activation_denominator", f"registration {key}")
        if key[1] == "iWeaver":
            registrations = as_int(row.get("registration_exact"))
            denominator = as_int(row.get("activation_denominator"))
            if denominator > registrations:
                raise RuntimeError(f"iWeaver activation denominator exceeds cohort for {key}")

    for label, rows, total_field, fractions in (
        (
            "learning_activation",
            dataset["learning_activation"],
            "attributed_users",
            (("numerator", "denominator"),),
        ),
        (
            "retention",
            dataset["retention"],
            "total_users",
            (("d1_numerator", "d1_denominator"), ("d7_numerator", "d7_denominator"), ("w1_numerator", "w1_denominator")),
        ),
        (
            "palm_followup",
            dataset["palm_followup"],
            "first_report_users",
            (("numerator", "denominator"),),
        ),
    ):
        seen = set()
        for row in rows:
            week = date_key(row["week_start"])
            if week in seen:
                raise RuntimeError(f"Duplicate {label} row for {week}")
            seen.add(week)
            fields = [total_field]
            for numerator, denominator in fractions:
                fields.extend((numerator, denominator))
                validate_fraction(row, numerator, denominator, f"{label} {week}")
                if as_int(row.get(denominator)) > as_int(row.get(total_field)):
                    raise RuntimeError(f"{label} denominator exceeds cohort for {week}")
            nonnegative(row, fields, label)

    topic_keys = set()
    for row in dataset["topic_completion"]:
        key = (date_key(row["week_start"]), str(row["product"]))
        if key in topic_keys:
            raise RuntimeError(f"Duplicate topic completion row for {key}")
        topic_keys.add(key)
        if key[1] not in PRODUCTS or key[1] == "Palmly":
            raise RuntimeError(f"Unexpected topic completion product: {key[1]}")
        validate_fraction(row, "numerator", "denominator", f"topic completion {key}")
        nonnegative(row, ("numerator", "denominator"), "topic_completion")

    for row in dataset["domain_quality"]:
        nonnegative(row, ("total_users", "domain_populated", "iweaver_domain"), "domain_quality")
        total = as_int(row.get("total_users"))
        populated = as_int(row.get("domain_populated"))
        iweaver = as_int(row.get("iweaver_domain"))
        if iweaver > populated or populated > total:
            raise RuntimeError(f"Invalid domain coverage hierarchy for {date_key(row['week_start'])}")

    for row in dataset["palm_quality"]:
        nonnegative(row, ("reports", "with_message_id", "linked_reports"), "palm_quality")
        reports = as_int(row.get("reports"))
        with_message = as_int(row.get("with_message_id"))
        linked = as_int(row.get("linked_reports"))
        if linked > with_message or with_message > reports:
            raise RuntimeError(f"Invalid Palmly linkage hierarchy for {date_key(row['week_start'])}")

    for row in dataset["overlap"]:
        nonnegative(row, ("active_users", "overlap_users"), "overlap")
        if as_int(row.get("overlap_users")) > as_int(row.get("active_users")):
            raise RuntimeError(f"Overlap users exceed active users for {date_key(row['week_start'])}")


def validate_period_dataset(dataset, grain):
    for rows in dataset.values():
        for row in rows:
            period = parse_date(row["period_start"])
            if grain == "month" and period.day != 1:
                raise RuntimeError(f"Monthly period is not month-aligned: {period}")
    normalized = {}
    for name, rows in dataset.items():
        normalized[name] = [dict(row, week_start=row["period_start"]) for row in rows]
    normalized.update(
        learning_activation=[],
        retention=[],
        palm_followup=[],
        topic_completion=[],
    )
    validate_dataset(normalized)


def validate_sources(range_rows, freshness_rows):
    ranges = {}
    for row in range_rows:
        source = str(row.get("source"))
        if source not in SOURCE_NAMES:
            raise RuntimeError(f"Unexpected source range: {source}")
        if source in ranges:
            raise RuntimeError(f"Duplicate source range: {source}")
        ranges[source] = {
            "first_at": parse_timestamp(row.get("first_at")),
            "last_at": parse_timestamp(row.get("last_at")),
            "first_text": None if row.get("first_at") is None else str(row.get("first_at")),
            "last_text": None if row.get("last_at") is None else str(row.get("last_at")),
        }
    missing = SOURCE_NAMES - set(ranges)
    if missing:
        raise RuntimeError(f"Missing source ranges: {', '.join(sorted(missing))}")

    freshness = {}
    for row in freshness_rows:
        source = str(row.get("source"))
        latest = parse_timestamp(row.get("latest"))
        if source in freshness:
            raise RuntimeError(f"Duplicate source freshness row: {source}")
        freshness[source] = {
            "latest": latest,
            "latest_text": None if row.get("latest") is None else str(row.get("latest")),
        }
    expected_freshness = {"users", "chat_logs", "lunara_reports"}
    missing = expected_freshness - set(freshness)
    if missing:
        raise RuntimeError(f"Missing freshness rows: {', '.join(sorted(missing))}")
    return ranges, freshness


def source_freshness_text(freshness):
    candidates = [item for item in freshness.values() if item["latest"] is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda item: item["latest"])["latest_text"]


def default_summary(product):
    return {
        "registration_exact": 0 if product in ("All", "iWeaver") else None,
        "registration_attributed": 0 if product in ("Palmly", "LearningCoach") else None,
        "activation_numerator": 0 if product in ("iWeaver", "LearningCoach") else None,
        "activation_denominator": 0 if product in ("iWeaver", "LearningCoach") else None,
        "user_turns": 0,
        "assistant_turns": 0,
        "active_users": 0,
        "topics": 0,
        "reports": 0,
    }


def empty_usage():
    return {
        "user_turns": 0,
        "assistant_turns": 0,
        "active_users": 0,
        "topics": 0,
        "new_active_users": 0,
        "returning_active_users": 0,
        "depth_1_turn": 0,
        "depth_2_3_turns": 0,
        "depth_4_9_turns": 0,
        "depth_10_plus_turns": 0,
        "median_user_turns": None,
    }


def empty_reports():
    return {
        "reports": 0,
        "report_users": 0,
        "new_report_users": 0,
        "returning_report_users": 0,
        "median_reports": None,
        "report_depth_1": 0,
        "report_depth_2_3": 0,
        "report_depth_4_9": 0,
        "report_depth_10_plus": 0,
    }


def empty_registration(product):
    return {
        "registration_exact": 0 if product in ("All", "iWeaver") else None,
        "registration_attributed": 0 if product in ("Palmly", "LearningCoach") else None,
        "activation_numerator": 0 if product == "iWeaver" else None,
        "activation_denominator": 0 if product == "iWeaver" else None,
    }


def window_metadata(week_start, current_week, range_end, forced_kind=None):
    week_end = week_start + timedelta(days=7)
    if forced_kind:
        return forced_kind, range_end
    if week_start == current_week:
        return "partial", range_end
    return "full", week_end


def source_interval_status(source, window_start, window_end, sources, pre_launch=False, quality=False):
    first_at = sources[source]["first_at"]
    if first_at is None:
        return "pre_launch" if pre_launch else "source_unavailable"
    start = naive(window_start)
    end = naive(window_end)
    if end <= first_at:
        return "pre_launch" if pre_launch else "source_unavailable"
    if start < first_at < end:
        return "partial" if quality else "left_censored"
    return "available"


def usage_status(product, window_start, window_end, sources):
    if product in ("All", "iWeaver"):
        return source_interval_status("chat_logs", window_start, window_end, sources)
    if product == "LearningCoach":
        return source_interval_status(
            "learning_coach", window_start, window_end, sources, pre_launch=True
        )
    return source_interval_status(
        "lunara_reports", window_start, window_end, sources, pre_launch=True
    )


def domain_status(domain_row):
    total = as_int(domain_row.get("total_users"))
    populated = as_int(domain_row.get("domain_populated"))
    coverage = percentage(populated, total)
    if total == 0:
        return "insufficient_sample", coverage, "no_registration_sample"
    if populated == 0:
        return "source_unavailable", coverage, "domain_not_populated"
    if coverage < 80:
        return "linkage_incomplete", coverage, "domain_coverage_incomplete"
    return "available", coverage, None


def quality_domain_status(domain_row):
    total = as_int(domain_row.get("total_users"))
    populated = as_int(domain_row.get("domain_populated"))
    coverage = percentage(populated, total)
    if total == 0:
        return "partial", coverage
    if populated == 0:
        return "source_unavailable", coverage
    if coverage < 80:
        return "partial", coverage
    return "available", coverage


def apply_sample_status(value, denominator, base_status="available", hide_tiny=True):
    if base_status != "available":
        return value, base_status, None, None
    denominator = as_int(denominator)
    if denominator == 0:
        return None, "insufficient_sample", "no_sample", 0.0
    if denominator < SMALL_SAMPLE_THRESHOLD:
        return (None if hide_tiny else value), "insufficient_sample", "tiny_sample", float(denominator)
    if denominator < NORMAL_SAMPLE_THRESHOLD:
        return value, "insufficient_sample", "small_sample", float(denominator)
    return value, "available", None, None


def maturity_status(total, denominator, cohort_end, maturity_as_of, lag_days, source_censored=False):
    total = as_int(total)
    denominator = as_int(denominator)
    if total == 0:
        return "insufficient_sample"
    if denominator >= total:
        return "available"
    fully_mature = naive(maturity_as_of) >= naive(cohort_end) + timedelta(days=lag_days)
    if fully_mature and source_censored:
        return "left_censored"
    if denominator == 0:
        return "immature"
    return "partial_maturity"


def add_series(
    rows,
    week_start,
    window_kind,
    product,
    metric_key,
    value,
    status,
    window_start,
    window_end,
    collected_at,
    source_freshness,
    numerator=None,
    denominator=None,
    quality_code=None,
    quality_value=None,
):
    definition = METRIC_CATALOG.get(metric_key)
    if definition is None:
        raise RuntimeError(f"Unknown metric key: {metric_key}")
    if product not in definition["products"]:
        raise RuntimeError(f"Metric {metric_key} does not apply to {product}")
    rows.append(
        {
            "week_start": iso_date(week_start),
            "window_kind": window_kind,
            "product": product,
            "metric_key": metric_key,
            "value": None if value is None else float(value),
            "numerator": None if numerator is None else int(numerator),
            "denominator": None if denominator is None else int(denominator),
            "status": status,
            "quality_code": quality_code,
            "quality_value": None if quality_value is None else float(quality_value),
            "window_start": sql_timestamp(window_start),
            "window_end": sql_timestamp(window_end),
            "rule_version": RULE_VERSION,
            "collected_at": collected_at,
            "source_freshness": source_freshness,
        }
    )


def add_period_series(
    rows,
    grain,
    period_start,
    period_end,
    window_kind,
    product,
    metric_key,
    value,
    status,
    collected_at,
    source_freshness,
    numerator=None,
    denominator=None,
    quality_code=None,
    quality_value=None,
):
    definition = METRIC_CATALOG.get(metric_key)
    if definition is None or metric_key not in MULTI_GRAIN_METRICS:
        raise RuntimeError(f"Metric {metric_key} is not a multi-grain metric")
    if product not in definition["products"]:
        raise RuntimeError(f"Metric {metric_key} does not apply to {product}")
    rows.append(
        {
            "grain": grain,
            "period_start": iso_date(period_start),
            "period_end": sql_timestamp(period_end),
            "window_kind": window_kind,
            "product": product,
            "metric_key": metric_key,
            "value": None if value is None else float(value),
            "numerator": None if numerator is None else int(numerator),
            "denominator": None if denominator is None else int(denominator),
            "status": status,
            "quality_code": quality_code,
            "quality_value": None if quality_value is None else float(quality_value),
            "rule_version": RULE_VERSION,
            "collected_at": collected_at,
            "source_freshness": source_freshness,
        }
    )


def add_quality(
    rows,
    week_start,
    window_kind,
    scope,
    quality_key,
    status,
    collected_at,
    source_freshness,
    numerator=None,
    denominator=None,
    value_pct=None,
    details=None,
):
    rows.append(
        {
            "week_start": iso_date(week_start),
            "window_kind": window_kind,
            "scope": scope,
            "quality_key": quality_key,
            "numerator": None if numerator is None else int(numerator),
            "denominator": None if denominator is None else int(denominator),
            "value_pct": None if value_pct is None else float(value_pct),
            "status": status,
            "details": None
            if details is None
            else json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "rule_version": RULE_VERSION,
            "collected_at": collected_at,
            "source_freshness": source_freshness,
        }
    )


def materialize_bundle(
    dataset,
    week_starts,
    current_week,
    range_end,
    maturity_as_of,
    sources,
    freshness,
    collected_at,
    forced_kind=None,
):
    usage = row_map(dataset["usage"], ("week_start", "product"), "usage")
    reports = row_map(dataset["reports"], ("week_start",), "reports")
    registrations = row_map(dataset["registration"], ("week_start", "product"), "registration")
    learning = row_map(dataset["learning_activation"], ("week_start",), "learning activation")
    retention = row_map(dataset["retention"], ("week_start",), "retention")
    followup = row_map(dataset["palm_followup"], ("week_start",), "Palmly follow-up")
    topics = row_map(dataset["topic_completion"], ("week_start", "product"), "topic completion")
    domains = row_map(dataset["domain_quality"], ("week_start",), "domain quality")
    palm_quality = row_map(dataset["palm_quality"], ("week_start",), "Palmly quality")
    overlap = row_map(dataset["overlap"], ("week_start",), "overlap")
    source_freshness = source_freshness_text(freshness)

    summary_rows = []
    series_rows = []
    quality_rows = []

    for week_start in week_starts:
        week = iso_date(week_start)
        window_kind, window_end = window_metadata(
            week_start, current_week, range_end, forced_kind
        )
        week_end = week_start + timedelta(days=7)
        domain_row = domains.get((week,), {"total_users": 0, "domain_populated": 0, "iweaver_domain": 0})
        domain_metric_status, domain_coverage, domain_code = domain_status(domain_row)
        report_row = reports.get((week,), empty_reports())
        palm_quality_row = palm_quality.get(
            (week,), {"reports": 0, "with_message_id": 0, "linked_reports": 0}
        )
        report_count = as_int(palm_quality_row.get("reports"))
        with_message = as_int(palm_quality_row.get("with_message_id"))
        linked_reports = as_int(palm_quality_row.get("linked_reports"))
        message_coverage = percentage(with_message, report_count)
        linkage_coverage = percentage(linked_reports, with_message)
        palm_base_status = source_interval_status(
            "lunara_reports", week_start, window_end, sources, pre_launch=True
        )
        palm_quality_base_status = source_interval_status(
            "lunara_reports",
            week_start,
            window_end,
            sources,
            pre_launch=True,
            quality=True,
        )
        palm_turn_status = palm_base_status
        palm_turn_code = None
        palm_turn_quality = linkage_coverage
        if palm_base_status == "available" and (with_message < report_count or linked_reports < with_message):
            palm_turn_status = "linkage_incomplete"
            palm_turn_code = "palm_message_linkage_incomplete"

        summary_by_product = {}
        for product in PRODUCTS:
            usage_row = usage.get((week, product), empty_usage())
            registration_row = registrations.get((week, product), empty_registration(product))
            item = default_summary(product)
            item.update(
                user_turns=as_int(usage_row.get("user_turns")),
                assistant_turns=as_int(usage_row.get("assistant_turns")),
                active_users=as_int(usage_row.get("active_users")),
                topics=as_int(usage_row.get("topics")),
            )
            for field in (
                "registration_exact",
                "registration_attributed",
                "activation_numerator",
                "activation_denominator",
            ):
                if registration_row.get(field) is not None:
                    item[field] = as_int(registration_row.get(field))
            if product == "Palmly":
                item["reports"] = as_int(report_row.get("reports"))
                item["active_users"] = as_int(report_row.get("report_users"))
                item["topics"] = 0
            elif product == "LearningCoach":
                learning_row = learning.get((week,), {"numerator": 0, "denominator": 0})
                item["activation_numerator"] = as_int(learning_row.get("numerator"))
                item["activation_denominator"] = as_int(learning_row.get("denominator"))
            summary_by_product[product] = item

        critical_statuses = {
            "All": usage_status("All", week_start, window_end, sources),
            "iWeaver": usage_status("iWeaver", week_start, window_end, sources),
            "Palmly": palm_base_status,
            "LearningCoach": usage_status("LearningCoach", week_start, window_end, sources),
        }
        for product, item in summary_by_product.items():
            summary_rows.append(
                {
                    "week_start": week,
                    "week_end": iso_date(week_end),
                    "window_start": sql_timestamp(week_start),
                    "window_end": sql_timestamp(window_end),
                    "window_kind": window_kind,
                    "product": product,
                    **item,
                    "data_complete": int(
                        window_kind == "full" and critical_statuses[product] == "available"
                    ),
                    "rule_version": RULE_VERSION,
                    "collected_at": collected_at,
                    "source_freshness": source_freshness,
                }
            )

        all_registration = registrations.get((week, "All"), empty_registration("All"))
        registration_total = as_int(all_registration.get("registration_exact"))
        user_source_status = source_interval_status("users", week_start, window_end, sources)
        add_series(
            series_rows,
            week_start,
            window_kind,
            "All",
            "registration_total",
            None if user_source_status in NULL_VALUE_STATUSES else registration_total,
            user_source_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
        )

        iweaver_registration = registrations.get((week, "iWeaver"), empty_registration("iWeaver"))
        iweaver_reg_count = as_int(iweaver_registration.get("registration_exact"))
        add_series(
            series_rows,
            week_start,
            window_kind,
            "iWeaver",
            "registration_domain_attributed",
            None if domain_metric_status in NULL_VALUE_STATUSES else iweaver_reg_count,
            domain_metric_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            quality_code=domain_code,
            quality_value=domain_coverage,
        )

        for product in ("Palmly", "LearningCoach"):
            first_use_row = registrations.get((week, product), empty_registration(product))
            first_use = as_int(first_use_row.get("registration_attributed"))
            first_status = usage_status(product, week_start, window_end, sources)
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "first_use_users",
                None if first_status in NULL_VALUE_STATUSES else first_use,
                first_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                quality_code="launch_week" if first_status == "left_censored" else None,
            )

        for product in PRODUCTS:
            usage_row = usage.get((week, product), empty_usage())
            base_status = usage_status(product, week_start, window_end, sources)
            primary_status = base_status
            primary_active = as_int(usage_row.get("active_users"))
            primary_new = as_int(usage_row.get("new_active_users"))
            primary_returning = as_int(usage_row.get("returning_active_users"))
            if product == "Palmly":
                primary_status = palm_base_status
                primary_active = as_int(report_row.get("report_users"))
                primary_new = as_int(report_row.get("new_report_users"))
                primary_returning = as_int(report_row.get("returning_report_users"))

            for metric_key, raw_value in (
                ("active_users", primary_active),
                ("new_active_users", primary_new),
                ("returning_active_users", primary_returning),
            ):
                add_series(
                    series_rows,
                    week_start,
                    window_kind,
                    product,
                    metric_key,
                    None if primary_status in NULL_VALUE_STATUSES else raw_value,
                    primary_status,
                    week_start,
                    window_end,
                    collected_at,
                    source_freshness,
                )

            returning_value = percentage(primary_returning, primary_active)
            returning_value, returning_status, sample_code, sample_value = apply_sample_status(
                returning_value, primary_active, primary_status
            )
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "returning_share",
                returning_value,
                returning_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                numerator=primary_returning,
                denominator=primary_active,
                quality_code=sample_code,
                quality_value=sample_value,
            )

            turn_status = palm_turn_status if product == "Palmly" else base_status
            turn_code = palm_turn_code if product == "Palmly" else None
            turn_quality = palm_turn_quality if product == "Palmly" else None
            for metric_key in ("user_turns", "assistant_turns"):
                raw_value = as_int(usage_row.get(metric_key))
                add_series(
                    series_rows,
                    week_start,
                    window_kind,
                    product,
                    metric_key,
                    None if turn_status in NULL_VALUE_STATUSES else raw_value,
                    turn_status,
                    week_start,
                    window_end,
                    collected_at,
                    source_freshness,
                    quality_code=turn_code,
                    quality_value=turn_quality,
                )

            linked_active = as_int(usage_row.get("active_users"))
            median_value = as_float(usage_row.get("median_user_turns"))
            median_status = turn_status
            median_code = turn_code
            median_quality = turn_quality
            if median_value is None and median_status == "available":
                median_status = "insufficient_sample"
                median_code = "no_sample"
                median_quality = 0.0
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "median_user_turns",
                None if median_status in NULL_VALUE_STATUSES else median_value,
                median_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                quality_code=median_code,
                quality_value=median_quality,
            )

            avg_value = ratio(as_int(usage_row.get("user_turns")), linked_active)
            avg_status = turn_status
            avg_code = turn_code
            avg_quality = turn_quality
            if linked_active == 0 and avg_status == "available":
                avg_status = "insufficient_sample"
                avg_code = "no_sample"
                avg_quality = 0.0
            elif linked_active < NORMAL_SAMPLE_THRESHOLD and avg_status == "available":
                avg_status = "insufficient_sample"
                avg_code = "tiny_sample" if linked_active < SMALL_SAMPLE_THRESHOLD else "small_sample"
                avg_quality = float(linked_active)
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "avg_user_turns",
                None if avg_status in NULL_VALUE_STATUSES else avg_value,
                avg_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                numerator=as_int(usage_row.get("user_turns")),
                denominator=linked_active,
                quality_code=avg_code,
                quality_value=avg_quality,
            )

            for metric_key in (
                "depth_1_turn",
                "depth_2_3_turns",
                "depth_4_9_turns",
                "depth_10_plus_turns",
            ):
                raw_value = as_int(usage_row.get(metric_key))
                add_series(
                    series_rows,
                    week_start,
                    window_kind,
                    product,
                    metric_key,
                    None if turn_status in NULL_VALUE_STATUSES else raw_value,
                    turn_status,
                    week_start,
                    window_end,
                    collected_at,
                    source_freshness,
                    quality_code=turn_code,
                    quality_value=turn_quality,
                )

        add_series(
            series_rows,
            week_start,
            window_kind,
            "Palmly",
            "reports",
            None if palm_base_status in NULL_VALUE_STATUSES else as_int(report_row.get("reports")),
            palm_base_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
        )
        report_users = as_int(report_row.get("report_users"))
        reports_per_user = ratio(as_int(report_row.get("reports")), report_users)
        report_ratio_status = palm_base_status
        report_ratio_code = None
        report_ratio_quality = None
        if report_users == 0 and report_ratio_status == "available":
            report_ratio_status = "insufficient_sample"
            report_ratio_code = "no_sample"
            report_ratio_quality = 0.0
        elif report_users < NORMAL_SAMPLE_THRESHOLD and report_ratio_status == "available":
            report_ratio_status = "insufficient_sample"
            report_ratio_code = "tiny_sample" if report_users < SMALL_SAMPLE_THRESHOLD else "small_sample"
            report_ratio_quality = float(report_users)
        add_series(
            series_rows,
            week_start,
            window_kind,
            "Palmly",
            "reports_per_user",
            None if report_ratio_status in NULL_VALUE_STATUSES else reports_per_user,
            report_ratio_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=as_int(report_row.get("reports")),
            denominator=report_users,
            quality_code=report_ratio_code,
            quality_value=report_ratio_quality,
        )

        activation_num = as_int(iweaver_registration.get("activation_numerator"))
        activation_den = as_int(iweaver_registration.get("activation_denominator"))
        activation_status = domain_metric_status
        activation_value = percentage(activation_num, activation_den)
        activation_code = domain_code
        activation_quality = domain_coverage
        if activation_status not in NULL_VALUE_STATUSES:
            if iweaver_reg_count == 0:
                activation_status = "insufficient_sample"
                activation_value = None
                activation_code = "no_sample"
                activation_quality = 0.0
            elif activation_den == 0:
                activation_status = "immature"
                activation_value = None
            elif activation_den < iweaver_reg_count:
                activation_status = "partial_maturity"
            elif domain_metric_status == "linkage_incomplete":
                activation_status = "linkage_incomplete"
            else:
                activation_status = "available"
        add_series(
            series_rows,
            week_start,
            window_kind,
            "iWeaver",
            "activation_24h",
            activation_value,
            activation_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=activation_num,
            denominator=activation_den,
            quality_code=activation_code,
            quality_value=activation_quality,
        )

        learning_row = learning.get((week,), {"numerator": 0, "denominator": 0, "attributed_users": 0})
        learning_num = as_int(learning_row.get("numerator"))
        learning_den = as_int(learning_row.get("denominator"))
        learning_total = as_int(learning_row.get("attributed_users"))
        learning_base = usage_status("LearningCoach", week_start, window_end, sources)
        learning_status = learning_base
        learning_value = percentage(learning_num, learning_den)
        if learning_status == "available":
            if learning_total > learning_den:
                learning_status = "immature" if learning_den == 0 else "partial_maturity"
            else:
                learning_value, learning_status, learning_code, learning_quality = apply_sample_status(
                    learning_value, learning_den, learning_status
                )
        else:
            learning_code = None
            learning_quality = None
        if learning_status in ("immature", "partial_maturity"):
            learning_code = "maturity_incomplete"
            learning_quality = percentage(learning_den, learning_total)
        add_series(
            series_rows,
            week_start,
            window_kind,
            "LearningCoach",
            "learning_activation_weekly",
            None if learning_status in NULL_VALUE_STATUSES else learning_value,
            learning_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=learning_num,
            denominator=learning_den,
            quality_code=learning_code,
            quality_value=learning_quality,
        )

        rolling_rows = []
        for offset in range(3, -1, -1):
            rolling_week = iso_date(week_start - timedelta(weeks=offset))
            rolling_rows.append(
                learning.get((rolling_week,), {"numerator": 0, "denominator": 0, "attributed_users": 0})
            )
        rolling_num = sum(as_int(row.get("numerator")) for row in rolling_rows)
        rolling_den = sum(as_int(row.get("denominator")) for row in rolling_rows)
        rolling_total = sum(as_int(row.get("attributed_users")) for row in rolling_rows)
        rolling_value = percentage(rolling_num, rolling_den)
        rolling_status = learning_base
        rolling_code = None
        rolling_quality = None
        if rolling_status == "available":
            if rolling_total > rolling_den:
                rolling_status = "immature" if rolling_den == 0 else "partial_maturity"
                rolling_code = "maturity_incomplete"
                rolling_quality = percentage(rolling_den, rolling_total)
            else:
                rolling_value, rolling_status, rolling_code, rolling_quality = apply_sample_status(
                    rolling_value, rolling_den, rolling_status
                )
        add_series(
            series_rows,
            week_start,
            window_kind,
            "LearningCoach",
            "learning_activation_4w",
            None if rolling_status in NULL_VALUE_STATUSES else rolling_value,
            rolling_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=rolling_num,
            denominator=rolling_den,
            quality_code=rolling_code,
            quality_value=rolling_quality,
        )

        followup_row = followup.get((week,), {"first_report_users": 0, "numerator": 0, "denominator": 0})
        followup_total = as_int(followup_row.get("first_report_users"))
        followup_num = as_int(followup_row.get("numerator"))
        followup_den = as_int(followup_row.get("denominator"))
        followup_status = palm_base_status
        followup_value = percentage(followup_num, followup_den)
        followup_code = None
        followup_quality = None
        if followup_status == "available":
            followup_status = maturity_status(
                followup_total,
                followup_den,
                window_end,
                maturity_as_of,
                7,
            )
            if followup_status == "available":
                followup_value, followup_status, followup_code, followup_quality = apply_sample_status(
                    followup_value, followup_den, followup_status
                )
            elif followup_status in ("immature", "partial_maturity"):
                followup_code = "maturity_incomplete"
                followup_quality = percentage(followup_den, followup_total)
        if 0 < followup_den < SMALL_SAMPLE_THRESHOLD:
            followup_value = None
            followup_code = "tiny_sample"
            followup_quality = float(followup_den)
            if followup_status == "available":
                followup_status = "insufficient_sample"
        add_series(
            series_rows,
            week_start,
            window_kind,
            "Palmly",
            "palm_followup_7d",
            None if followup_status in NULL_VALUE_STATUSES else followup_value,
            followup_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=followup_num,
            denominator=followup_den,
            quality_code=followup_code,
            quality_value=followup_quality,
        )

        retention_row = retention.get(
            (week,),
            {
                "total_users": registration_total,
                "d1_numerator": 0,
                "d1_denominator": 0,
                "d7_numerator": 0,
                "d7_denominator": 0,
                "w1_numerator": 0,
                "w1_denominator": 0,
            },
        )
        retention_total = as_int(retention_row.get("total_users"))
        for metric_key, prefix, lag_days in (
            ("retention_d1", "d1", 2),
            ("retention_d7", "d7", 8),
            ("retention_w1", "w1", 14),
        ):
            num = as_int(retention_row.get(f"{prefix}_numerator"))
            den = as_int(retention_row.get(f"{prefix}_denominator"))
            value = percentage(num, den)
            status = user_source_status
            code = None
            quality_value = None
            if status == "available":
                status = maturity_status(
                    retention_total,
                    den,
                    window_end,
                    maturity_as_of,
                    lag_days,
                    source_censored=True,
                )
                if status == "available":
                    value, status, code, quality_value = apply_sample_status(value, den, status)
                elif status in ("immature", "partial_maturity"):
                    code = "maturity_incomplete"
                    quality_value = percentage(den, retention_total)
                elif status == "left_censored":
                    code = "chat_source_left_censored"
                    quality_value = percentage(den, retention_total)
            add_series(
                series_rows,
                week_start,
                window_kind,
                "All",
                metric_key,
                None if status in NULL_VALUE_STATUSES else value,
                status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                numerator=num,
                denominator=den,
                quality_code=code,
                quality_value=quality_value,
            )

        for product in ("All", "iWeaver", "LearningCoach"):
            topic_row = topics.get((week, product), {"numerator": 0, "denominator": 0})
            topic_num = as_int(topic_row.get("numerator"))
            topic_den = as_int(topic_row.get("denominator"))
            topic_status = usage_status(product, week_start, window_end, sources)
            topic_value = percentage(topic_num, topic_den)
            topic_code = None
            topic_quality = None
            if window_kind != "full" and topic_status == "available":
                topic_status = "immature"
                topic_value = None
                topic_code = "incomplete_week"
            elif topic_status == "available":
                topic_value, topic_status, topic_code, topic_quality = apply_sample_status(
                    topic_value, topic_den, topic_status
                )
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "topic_completion_rate",
                None if topic_status in NULL_VALUE_STATUSES else topic_value,
                topic_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                numerator=topic_num,
                denominator=topic_den,
                quality_code=topic_code,
                quality_value=topic_quality,
            )

        for product in ("All", "iWeaver"):
            add_series(
                series_rows,
                week_start,
                window_kind,
                product,
                "domain_coverage",
                None if domain_metric_status in NULL_VALUE_STATUSES else domain_coverage,
                domain_metric_status,
                week_start,
                window_end,
                collected_at,
                source_freshness,
                numerator=as_int(domain_row.get("domain_populated")),
                denominator=as_int(domain_row.get("total_users")),
                quality_code=domain_code,
                quality_value=domain_coverage,
            )

        palm_link_metric_status = palm_base_status
        palm_link_value = linkage_coverage
        palm_link_code = None
        if palm_link_metric_status == "available":
            if with_message == 0:
                palm_link_metric_status = "insufficient_sample"
                palm_link_value = None
                palm_link_code = "no_linkable_reports"
            elif linked_reports < with_message:
                palm_link_metric_status = "linkage_incomplete"
                palm_link_code = "palm_chat_linkage_incomplete"
        add_series(
            series_rows,
            week_start,
            window_kind,
            "Palmly",
            "palm_linkage_coverage",
            None if palm_link_metric_status in NULL_VALUE_STATUSES else palm_link_value,
            palm_link_metric_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=linked_reports,
            denominator=with_message,
            quality_code=palm_link_code,
            quality_value=linkage_coverage,
        )

        overlap_row = overlap.get((week,), {"active_users": 0, "overlap_users": 0})
        overlap_active = as_int(overlap_row.get("active_users"))
        overlap_users = as_int(overlap_row.get("overlap_users"))
        overlap_status = usage_status("All", week_start, window_end, sources)
        overlap_value = percentage(overlap_users, overlap_active)
        overlap_code = None
        overlap_quality_value = None
        if overlap_status == "available":
            overlap_value, overlap_status, overlap_code, overlap_quality_value = apply_sample_status(
                overlap_value, overlap_active, overlap_status
            )
        add_series(
            series_rows,
            week_start,
            window_kind,
            "All",
            "multi_product_overlap",
            None if overlap_status in NULL_VALUE_STATUSES else overlap_value,
            overlap_status,
            week_start,
            window_end,
            collected_at,
            source_freshness,
            numerator=overlap_users,
            denominator=overlap_active,
            quality_code=overlap_code,
            quality_value=overlap_quality_value,
        )

        domain_quality_status, domain_quality_value = quality_domain_status(domain_row)
        total_users = as_int(domain_row.get("total_users"))
        domain_populated = as_int(domain_row.get("domain_populated"))
        add_quality(
            quality_rows,
            week_start,
            window_kind,
            "registrations",
            "domain_coverage",
            domain_quality_status,
            collected_at,
            source_freshness,
            numerator=domain_populated,
            denominator=total_users,
            value_pct=domain_quality_value,
            details={"iweaver_domain": as_int(domain_row.get("iweaver_domain"))},
        )
        add_quality(
            quality_rows,
            week_start,
            window_kind,
            "registrations",
            "unclassified_registration_share",
            domain_quality_status,
            collected_at,
            source_freshness,
            numerator=max(total_users - domain_populated, 0),
            denominator=total_users,
            value_pct=percentage(max(total_users - domain_populated, 0), total_users),
        )

        palm_message_quality_status = palm_quality_base_status
        if palm_message_quality_status == "available" and with_message < report_count:
            palm_message_quality_status = "linkage_incomplete"
        add_quality(
            quality_rows,
            week_start,
            window_kind,
            "Palmly",
            "message_id_coverage",
            palm_message_quality_status,
            collected_at,
            source_freshness,
            numerator=with_message,
            denominator=report_count,
            value_pct=message_coverage,
        )
        palm_chat_quality_status = palm_quality_base_status
        if palm_chat_quality_status == "available" and linked_reports < with_message:
            palm_chat_quality_status = "linkage_incomplete"
        add_quality(
            quality_rows,
            week_start,
            window_kind,
            "Palmly",
            "chat_linkage_coverage",
            palm_chat_quality_status,
            collected_at,
            source_freshness,
            numerator=linked_reports,
            denominator=with_message,
            value_pct=linkage_coverage,
        )

        overlap_quality_status = source_interval_status(
            "chat_logs", week_start, window_end, sources, quality=True
        )
        add_quality(
            quality_rows,
            week_start,
            window_kind,
            "All",
            "multi_product_overlap",
            overlap_quality_status,
            collected_at,
            source_freshness,
            numerator=overlap_users,
            denominator=overlap_active,
            value_pct=percentage(overlap_users, overlap_active),
            details={"active_users_are_not_additive": True},
        )

        for source in sorted(SOURCE_NAMES):
            is_product_launch = source in {"lunara_reports", "learning_coach"}
            source_status = source_interval_status(
                source,
                week_start,
                window_end,
                sources,
                pre_launch=is_product_launch,
                quality=True,
            )
            add_quality(
                quality_rows,
                week_start,
                window_kind,
                source,
                "source_availability",
                source_status,
                collected_at,
                source_freshness,
                details={
                    "first_at": sources[source]["first_text"],
                    "last_at": sources[source]["last_text"],
                    "freshness": freshness.get(source, {}).get("latest_text"),
                },
            )

    return summary_rows, series_rows, quality_rows


def materialize_period_series(
    dataset,
    grain,
    period_starts,
    current_period,
    range_end,
    sources,
    freshness,
    collected_at,
):
    usage = row_map(dataset["usage"], ("period_start", "product"), f"{grain} usage")
    reports = row_map(dataset["reports"], ("period_start",), f"{grain} reports")
    registrations = row_map(
        dataset["registration"], ("period_start", "product"), f"{grain} registration"
    )
    domains = row_map(dataset["domain_quality"], ("period_start",), f"{grain} domain")
    palm_quality = row_map(dataset["palm_quality"], ("period_start",), f"{grain} Palmly quality")
    overlap = row_map(dataset["overlap"], ("period_start",), f"{grain} overlap")
    source_freshness = source_freshness_text(freshness)
    rows = []

    for period_start in period_starts:
        period = iso_date(period_start)
        natural_end = period_start + timedelta(days=1) if grain == "day" else add_months(period_start, 1)
        period_end = range_end if period_start == current_period else natural_end
        window_kind = "partial" if period_start == current_period else "full"
        domain_row = domains.get((period,), {"total_users": 0, "domain_populated": 0, "iweaver_domain": 0})
        domain_metric_status, domain_coverage, domain_code = domain_status(domain_row)
        report_row = reports.get((period,), empty_reports())
        palm_quality_row = palm_quality.get(
            (period,), {"reports": 0, "with_message_id": 0, "linked_reports": 0}
        )
        report_count = as_int(palm_quality_row.get("reports"))
        with_message = as_int(palm_quality_row.get("with_message_id"))
        linked_reports = as_int(palm_quality_row.get("linked_reports"))
        linkage_coverage = percentage(linked_reports, with_message)
        palm_base_status = source_interval_status(
            "lunara_reports", period_start, period_end, sources, pre_launch=True
        )
        palm_turn_status = palm_base_status
        palm_turn_code = None
        if palm_base_status == "available" and (
            with_message < report_count or linked_reports < with_message
        ):
            palm_turn_status = "linkage_incomplete"
            palm_turn_code = "palm_message_linkage_incomplete"

        all_registration = registrations.get((period, "All"), empty_registration("All"))
        total_registrations = as_int(all_registration.get("registration_exact"))
        users_status = source_interval_status("users", period_start, period_end, sources)
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "All", "registration_total",
            None if users_status in NULL_VALUE_STATUSES else total_registrations,
            users_status, collected_at, source_freshness,
        )

        iweaver_registration = registrations.get(
            (period, "iWeaver"), empty_registration("iWeaver")
        )
        iweaver_count = as_int(iweaver_registration.get("registration_exact"))
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "iWeaver",
            "registration_domain_attributed",
            None if domain_metric_status in NULL_VALUE_STATUSES else iweaver_count,
            domain_metric_status, collected_at, source_freshness,
            quality_code=domain_code, quality_value=domain_coverage,
        )

        for product in ("Palmly", "LearningCoach"):
            registration_row = registrations.get((period, product), empty_registration(product))
            status = usage_status(product, period_start, period_end, sources)
            add_period_series(
                rows, grain, period_start, period_end, window_kind, product, "first_use_users",
                None if status in NULL_VALUE_STATUSES else as_int(registration_row.get("registration_attributed")),
                status, collected_at, source_freshness,
            )

        for product in PRODUCTS:
            usage_row = usage.get((period, product), empty_usage())
            base_status = usage_status(product, period_start, period_end, sources)
            primary_status = base_status
            primary_active = as_int(usage_row.get("active_users"))
            primary_new = as_int(usage_row.get("new_active_users"))
            primary_returning = as_int(usage_row.get("returning_active_users"))
            if product == "Palmly":
                primary_status = palm_base_status
                primary_active = as_int(report_row.get("report_users"))
                primary_new = as_int(report_row.get("new_report_users"))
                primary_returning = as_int(report_row.get("returning_report_users"))

            for metric_key, value in (
                ("active_users", primary_active),
                ("new_active_users", primary_new),
                ("returning_active_users", primary_returning),
            ):
                add_period_series(
                    rows, grain, period_start, period_end, window_kind, product, metric_key,
                    None if primary_status in NULL_VALUE_STATUSES else value,
                    primary_status, collected_at, source_freshness,
                )

            share = percentage(primary_returning, primary_active)
            share, share_status, sample_code, sample_value = apply_sample_status(
                share, primary_active, primary_status
            )
            add_period_series(
                rows, grain, period_start, period_end, window_kind, product, "returning_share",
                share, share_status, collected_at, source_freshness,
                numerator=primary_returning, denominator=primary_active,
                quality_code=sample_code, quality_value=sample_value,
            )

            turn_status = palm_turn_status if product == "Palmly" else base_status
            turn_code = palm_turn_code if product == "Palmly" else None
            turn_quality = linkage_coverage if product == "Palmly" else None
            for metric_key in ("user_turns", "assistant_turns"):
                add_period_series(
                    rows, grain, period_start, period_end, window_kind, product, metric_key,
                    None if turn_status in NULL_VALUE_STATUSES else as_int(usage_row.get(metric_key)),
                    turn_status, collected_at, source_freshness,
                    quality_code=turn_code, quality_value=turn_quality,
                )

            linked_active = as_int(usage_row.get("active_users"))
            median_value = as_float(usage_row.get("median_user_turns"))
            median_status = turn_status
            median_code = turn_code
            median_quality = turn_quality
            if median_value is None and median_status == "available":
                median_status, median_code, median_quality = "insufficient_sample", "no_sample", 0.0
            add_period_series(
                rows, grain, period_start, period_end, window_kind, product, "median_user_turns",
                None if median_status in NULL_VALUE_STATUSES else median_value,
                median_status, collected_at, source_freshness,
                quality_code=median_code, quality_value=median_quality,
            )

            turns = as_int(usage_row.get("user_turns"))
            average = ratio(turns, linked_active)
            average_status = turn_status
            average_code = turn_code
            average_quality = turn_quality
            if linked_active == 0 and average_status == "available":
                average_status, average_code, average_quality = "insufficient_sample", "no_sample", 0.0
            elif linked_active < NORMAL_SAMPLE_THRESHOLD and average_status == "available":
                average_status = "insufficient_sample"
                average_code = "tiny_sample" if linked_active < SMALL_SAMPLE_THRESHOLD else "small_sample"
                average_quality = float(linked_active)
            add_period_series(
                rows, grain, period_start, period_end, window_kind, product, "avg_user_turns",
                None if average_status in NULL_VALUE_STATUSES else average,
                average_status, collected_at, source_freshness,
                numerator=turns, denominator=linked_active,
                quality_code=average_code, quality_value=average_quality,
            )

            for metric_key in (
                "depth_1_turn", "depth_2_3_turns", "depth_4_9_turns", "depth_10_plus_turns"
            ):
                add_period_series(
                    rows, grain, period_start, period_end, window_kind, product, metric_key,
                    None if turn_status in NULL_VALUE_STATUSES else as_int(usage_row.get(metric_key)),
                    turn_status, collected_at, source_freshness,
                    quality_code=turn_code, quality_value=turn_quality,
                )

        reports_count = as_int(report_row.get("reports"))
        report_users = as_int(report_row.get("report_users"))
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "Palmly", "reports",
            None if palm_base_status in NULL_VALUE_STATUSES else reports_count,
            palm_base_status, collected_at, source_freshness,
        )
        report_ratio = ratio(reports_count, report_users)
        report_ratio_status = palm_base_status
        report_code = None
        report_quality = None
        if report_users == 0 and report_ratio_status == "available":
            report_ratio_status, report_code, report_quality = "insufficient_sample", "no_sample", 0.0
        elif report_users < NORMAL_SAMPLE_THRESHOLD and report_ratio_status == "available":
            report_ratio_status = "insufficient_sample"
            report_code = "tiny_sample" if report_users < SMALL_SAMPLE_THRESHOLD else "small_sample"
            report_quality = float(report_users)
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "Palmly", "reports_per_user",
            None if report_ratio_status in NULL_VALUE_STATUSES else report_ratio,
            report_ratio_status, collected_at, source_freshness,
            numerator=reports_count, denominator=report_users,
            quality_code=report_code, quality_value=report_quality,
        )

        for product in ("All", "iWeaver"):
            add_period_series(
                rows, grain, period_start, period_end, window_kind, product, "domain_coverage",
                None if domain_metric_status in NULL_VALUE_STATUSES else domain_coverage,
                domain_metric_status, collected_at, source_freshness,
                numerator=as_int(domain_row.get("domain_populated")),
                denominator=as_int(domain_row.get("total_users")),
                quality_code=domain_code, quality_value=domain_coverage,
            )

        palm_link_status = palm_base_status
        palm_link_value = linkage_coverage
        palm_link_code = None
        if palm_link_status == "available":
            if with_message == 0:
                palm_link_status, palm_link_value, palm_link_code = (
                    "insufficient_sample", None, "no_linkable_reports"
                )
            elif linked_reports < with_message:
                palm_link_status, palm_link_code = (
                    "linkage_incomplete", "palm_chat_linkage_incomplete"
                )
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "Palmly", "palm_linkage_coverage",
            None if palm_link_status in NULL_VALUE_STATUSES else palm_link_value,
            palm_link_status, collected_at, source_freshness,
            numerator=linked_reports, denominator=with_message,
            quality_code=palm_link_code, quality_value=linkage_coverage,
        )

        overlap_row = overlap.get((period,), {"active_users": 0, "overlap_users": 0})
        overlap_active = as_int(overlap_row.get("active_users"))
        overlap_users = as_int(overlap_row.get("overlap_users"))
        overlap_status = usage_status("All", period_start, period_end, sources)
        overlap_value = percentage(overlap_users, overlap_active)
        overlap_code = None
        overlap_quality = None
        if overlap_status == "available":
            overlap_value, overlap_status, overlap_code, overlap_quality = apply_sample_status(
                overlap_value, overlap_active, overlap_status
            )
        add_period_series(
            rows, grain, period_start, period_end, window_kind, "All", "multi_product_overlap",
            None if overlap_status in NULL_VALUE_STATUSES else overlap_value,
            overlap_status, collected_at, source_freshness,
            numerator=overlap_users, denominator=overlap_active,
            quality_code=overlap_code, quality_value=overlap_quality,
        )

    return rows


def validate_materialized(summary_rows, series_rows, quality_rows, period_rows=None):
    summary_keys = set()
    for row in summary_rows:
        key = (row["week_start"], row["window_kind"], row["product"])
        if key in summary_keys:
            raise RuntimeError(f"Duplicate summary primary key: {key}")
        summary_keys.add(key)
        if row["product"] not in PRODUCTS:
            raise RuntimeError(f"Unknown summary product: {row['product']}")
        nonnegative(
            row,
            (
                "registration_exact",
                "registration_attributed",
                "activation_numerator",
                "activation_denominator",
                "user_turns",
                "assistant_turns",
                "active_users",
                "topics",
                "reports",
            ),
            "summary",
        )
        validate_fraction(row, "activation_numerator", "activation_denominator", f"summary {key}")

    series_keys = set()
    for row in series_rows:
        key = (row["week_start"], row["window_kind"], row["product"], row["metric_key"])
        if key in series_keys:
            raise RuntimeError(f"Duplicate series primary key: {key}")
        series_keys.add(key)
        if row["status"] not in SERIES_STATUSES:
            raise RuntimeError(f"Unknown metric status: {row['status']}")
        if row["metric_key"] not in METRIC_CATALOG:
            raise RuntimeError(f"Unknown metric: {row['metric_key']}")
        if row["product"] not in METRIC_CATALOG[row["metric_key"]]["products"]:
            raise RuntimeError(f"Invalid product for metric: {key}")
        nonnegative(row, ("value", "numerator", "denominator", "quality_value"), "series")
        unit = METRIC_CATALOG[row["metric_key"]]["unit"]
        if unit == "percent" and row["numerator"] is not None and row["denominator"] is not None:
            if row["numerator"] > row["denominator"]:
                raise RuntimeError(f"Metric numerator exceeds denominator: {key}")
        if unit == "percent" and row["value"] is not None:
            if not 0 <= row["value"] <= 100:
                raise RuntimeError(f"Metric percent outside 0-100: {key}")
        if row["status"] in NULL_VALUE_STATUSES and row["value"] is not None:
            raise RuntimeError(f"Unavailable metric carries a value: {key}")

    quality_keys = set()
    for row in quality_rows:
        key = (row["week_start"], row["window_kind"], row["scope"], row["quality_key"])
        if key in quality_keys:
            raise RuntimeError(f"Duplicate quality primary key: {key}")
        quality_keys.add(key)
        if row["status"] not in QUALITY_STATUSES:
            raise RuntimeError(f"Unknown quality status: {row['status']}")
        nonnegative(row, ("numerator", "denominator", "value_pct"), "quality")
        if row["numerator"] is not None and row["denominator"] is not None:
            if row["numerator"] > row["denominator"]:
                raise RuntimeError(f"Quality numerator exceeds denominator: {key}")
        if row["value_pct"] is not None and not 0 <= row["value_pct"] <= 100:
            raise RuntimeError(f"Quality percent outside 0-100: {key}")

    period_keys = set()
    for row in period_rows or []:
        key = (row["grain"], row["period_start"], row["product"], row["metric_key"])
        if key in period_keys:
            raise RuntimeError(f"Duplicate period primary key: {key}")
        period_keys.add(key)
        if row["grain"] not in {"day", "month"}:
            raise RuntimeError(f"Unknown period grain: {row['grain']}")
        if row["window_kind"] not in {"full", "partial"}:
            raise RuntimeError(f"Unknown period window kind: {key}")
        if row["metric_key"] not in MULTI_GRAIN_METRICS:
            raise RuntimeError(f"Week-only metric found in period series: {key}")
        if row["product"] not in METRIC_CATALOG[row["metric_key"]]["products"]:
            raise RuntimeError(f"Invalid period product: {key}")
        if row["status"] not in SERIES_STATUSES:
            raise RuntimeError(f"Unknown period status: {key}")
        start = parse_timestamp(row["period_start"])
        end = parse_timestamp(row["period_end"])
        if end <= start or (row["grain"] == "month" and start.day != 1):
            raise RuntimeError(f"Invalid period boundary: {key}")
        nonnegative(row, ("value", "numerator", "denominator", "quality_value"), "period")
        unit = METRIC_CATALOG[row["metric_key"]]["unit"]
        if unit == "percent" and row["numerator"] is not None and row["denominator"] is not None:
            if row["numerator"] > row["denominator"]:
                raise RuntimeError(f"Period numerator exceeds denominator: {key}")
        if unit == "percent" and row["value"] is not None and not 0 <= row["value"] <= 100:
            raise RuntimeError(f"Period percent outside 0-100: {key}")
        if row["status"] in NULL_VALUE_STATUSES and row["value"] is not None:
            raise RuntimeError(f"Unavailable period metric carries a value: {key}")


def insert_rows(connection, table, rows, fields):
    if not rows:
        return
    placeholders = ",".join("?" for _ in fields)
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(fields)}) VALUES ({placeholders})"
    connection.executemany(sql, [[row[field] for field in fields] for row in rows])


def record_failed_run(started_at, finished_at, weeks, error):
    try:
        connection = connect_database(DB_PATH, read_only=False)
        try:
            migrate_connection(connection)
            with connection:
                connection.execute(
                    """INSERT INTO collector_runs
                       (started_at, finished_at, status, weeks_requested, rows_written,
                        source_freshness, rule_version, error_summary)
                       VALUES (?, ?, 'failed', ?, 0, NULL, ?, ?)""",
                    (started_at, finished_at, weeks, RULE_VERSION, str(error)[:1000]),
                )
        finally:
            connection.close()
    except Exception:
        logger.exception("Unable to record failed collector run")


def run_collection(weeks=12, as_of=None, dry_run=False):
    if weeks < 2 or weeks > 104:
        raise ValueError("weeks must be between 2 and 104")

    now = parse_as_of(as_of)
    started_at = business_now().isoformat()
    current_week = monday_start(now)
    week_starts = sorted(current_week - timedelta(weeks=index) for index in range(weeks))
    oldest_query_week = week_starts[0] - timedelta(weeks=3)

    client = SupersetClient()
    database_name = client.validate_database()
    logger.info("Validated Superset DB2: %s", database_name)

    main_dataset = fetch_dataset(client, oldest_query_week, now, now)

    elapsed = min(now - current_week, timedelta(days=7))
    aligned_week = current_week - timedelta(days=7)
    aligned_end = aligned_week + elapsed
    aligned_dataset = fetch_dataset(
        client,
        aligned_week - timedelta(weeks=3),
        aligned_end,
        aligned_end,
    )

    current_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_starts = sorted(current_day - timedelta(days=index) for index in range(90))
    day_dataset = fetch_operational_dataset(client, day_starts[0], now, "day")

    current_month = month_start(now)
    month_starts = [add_months(current_month, -index) for index in range(12)]
    month_starts.sort()
    month_dataset = fetch_operational_dataset(client, month_starts[0], now, "month")

    source_rows, freshness_rows = fetch_sources(client)

    validate_dataset(main_dataset)
    validate_dataset(aligned_dataset)
    validate_period_dataset(day_dataset, "day")
    validate_period_dataset(month_dataset, "month")
    sources, freshness = validate_sources(source_rows, freshness_rows)

    collected_at = business_now().isoformat()
    main_summary, main_series, main_quality = materialize_bundle(
        main_dataset,
        week_starts,
        current_week,
        now,
        now,
        sources,
        freshness,
        collected_at,
    )
    aligned_summary, aligned_series, aligned_quality = materialize_bundle(
        aligned_dataset,
        [aligned_week],
        current_week,
        aligned_end,
        aligned_end,
        sources,
        freshness,
        collected_at,
        forced_kind="aligned_previous",
    )
    summary_rows = main_summary + aligned_summary
    series_rows = main_series + aligned_series
    quality_rows = main_quality + aligned_quality
    day_rows = materialize_period_series(
        day_dataset,
        "day",
        day_starts,
        current_day,
        now,
        sources,
        freshness,
        collected_at,
    )
    month_rows = materialize_period_series(
        month_dataset,
        "month",
        month_starts,
        current_month,
        now,
        sources,
        freshness,
        collected_at,
    )
    period_rows = day_rows + month_rows
    validate_materialized(summary_rows, series_rows, quality_rows, period_rows)

    status_counts = Counter(row["status"] for row in series_rows)
    period_status_counts = Counter(row["status"] for row in period_rows)
    rows_written = len(summary_rows) + len(series_rows) + len(quality_rows) + len(period_rows)
    source_freshness = source_freshness_text(freshness)
    summary = {
        "status": "dry_run" if dry_run else "success",
        "database": database_name,
        "weeks": weeks,
        "summary_rows": len(summary_rows),
        "series_rows": len(series_rows),
        "quality_rows": len(quality_rows),
        "daily_rows": len(day_rows),
        "monthly_rows": len(month_rows),
        "rows": rows_written,
        "series_statuses": dict(sorted(status_counts.items())),
        "period_statuses": dict(sorted(period_status_counts.items())),
        "current_week": iso_date(current_week),
        "current_day": iso_date(current_day),
        "current_month": iso_date(current_month),
        "aligned_previous_end": sql_timestamp(aligned_end),
        "source_freshness": source_freshness,
        "rule_version": RULE_VERSION,
    }
    if dry_run:
        summary["sample_series"] = series_rows[-12:]
        summary["sample_periods"] = period_rows[-12:]
        summary["sample_quality"] = quality_rows[-8:]
        return summary

    connection = connect_database(DB_PATH, read_only=False)
    try:
        migrate_connection(connection)
        with connection:
            for week_start in week_starts:
                week = iso_date(week_start)
                connection.execute(
                    "DELETE FROM weekly_product_metrics WHERE week_start=? AND window_kind IN ('full','partial')",
                    (week,),
                )
                connection.execute(
                    "DELETE FROM weekly_metric_series WHERE week_start=? AND window_kind IN ('full','partial')",
                    (week,),
                )
                connection.execute(
                    "DELETE FROM weekly_data_quality WHERE week_start=? AND window_kind IN ('full','partial')",
                    (week,),
                )
            for table in ("weekly_product_metrics", "weekly_metric_series", "weekly_data_quality"):
                connection.execute(f"DELETE FROM {table} WHERE window_kind='aligned_previous'")
            connection.execute("DELETE FROM period_metric_series WHERE grain IN ('day','month')")

            insert_rows(
                connection,
                "weekly_product_metrics",
                summary_rows,
                (
                    "week_start",
                    "week_end",
                    "window_start",
                    "window_end",
                    "window_kind",
                    "product",
                    "registration_exact",
                    "registration_attributed",
                    "activation_numerator",
                    "activation_denominator",
                    "user_turns",
                    "assistant_turns",
                    "active_users",
                    "topics",
                    "reports",
                    "data_complete",
                    "rule_version",
                    "collected_at",
                    "source_freshness",
                ),
            )
            insert_rows(
                connection,
                "weekly_metric_series",
                series_rows,
                (
                    "week_start",
                    "window_kind",
                    "product",
                    "metric_key",
                    "value",
                    "numerator",
                    "denominator",
                    "status",
                    "quality_code",
                    "quality_value",
                    "window_start",
                    "window_end",
                    "rule_version",
                    "collected_at",
                    "source_freshness",
                ),
            )
            insert_rows(
                connection,
                "weekly_data_quality",
                quality_rows,
                (
                    "week_start",
                    "window_kind",
                    "scope",
                    "quality_key",
                    "numerator",
                    "denominator",
                    "value_pct",
                    "status",
                    "details",
                    "rule_version",
                    "collected_at",
                    "source_freshness",
                ),
            )
            insert_rows(
                connection,
                "period_metric_series",
                period_rows,
                (
                    "grain",
                    "period_start",
                    "period_end",
                    "window_kind",
                    "product",
                    "metric_key",
                    "value",
                    "numerator",
                    "denominator",
                    "status",
                    "quality_code",
                    "quality_value",
                    "rule_version",
                    "collected_at",
                    "source_freshness",
                ),
            )
            connection.execute(
                """INSERT INTO collector_runs
                   (started_at, finished_at, status, weeks_requested, rows_written,
                    source_freshness, rule_version, error_summary)
                   VALUES (?, ?, 'success', ?, ?, ?, ?, NULL)""",
                (
                    started_at,
                    collected_at,
                    weeks,
                    rows_written,
                    source_freshness,
                    RULE_VERSION,
                ),
            )
    finally:
        connection.close()
    return summary


def main():
    parser = argparse.ArgumentParser(description="Collect verified weekly DB2 V3 metrics")
    parser.add_argument("--weeks", type=int, default=12)
    parser.add_argument("--as-of", help="ISO timestamp in the business timezone")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Collector already running; exiting")
            return 0

        started = business_now().isoformat()
        try:
            result = run_collection(args.weeks, args.as_of, args.dry_run)
        except Exception as error:
            logger.exception("Collector failed; previous successful metrics remain untouched")
            if not args.dry_run:
                record_failed_run(started, business_now().isoformat(), args.weeks, error)
            return 1

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
