"""Verified product scopes, metric catalog, and DB2 SQL builders."""

RULE_VERSION = "3.1"
ACTIVATION_WINDOW_HOURS = 24
SMALL_SAMPLE_THRESHOLD = 10
NORMAL_SAMPLE_THRESHOLD = 30
PRODUCTS = ("All", "iWeaver", "Palmly", "LearningCoach")
GRAIN_RANGES = {
    "day": (7, 14, 30, 90),
    "week": (4, 8, 12, 26, 52),
    "month": (3, 6, 12),
}
DEFAULT_GRAIN_RANGES = {"day": 30, "week": 12, "month": 6}
MULTI_GRAIN_METRICS = {
    "registration_total",
    "registration_domain_attributed",
    "first_use_users",
    "active_users",
    "new_active_users",
    "returning_active_users",
    "returning_share",
    "user_turns",
    "assistant_turns",
    "median_user_turns",
    "avg_user_turns",
    "depth_1_turn",
    "depth_2_3_turns",
    "depth_4_9_turns",
    "depth_10_plus_turns",
    "reports",
    "reports_per_user",
    "domain_coverage",
    "palm_linkage_coverage",
    "multi_product_overlap",
}

LEARNING_COACH_ADJACENT = (
    "active-recall-coach",
    "ai-practice-coach",
    "ai-revision-coach",
    "spaced-repetition-coach",
    "certification-study-coach",
    "last-minute-study-coach",
    "wrong-answer-practice-coach",
    "ai-learning-game",
)

PRODUCT_LABELS_ZH = {
    "All": "总计",
    "iWeaver": "iWeaver / 其他",
    "Palmly": "Palmly",
    "LearningCoach": "学习教练",
}

STATUS_LABELS_ZH = {
    "available": "可用",
    "source_unavailable": "源数据未接入",
    "pre_launch": "尚未上线",
    "immature": "观察窗口未成熟",
    "partial_maturity": "部分成熟",
    "insufficient_sample": "样本不足",
    "linkage_incomplete": "关联不完整",
    "left_censored": "历史左截断",
    "not_applicable": "不适用",
    "partial": "部分可用",
}

METRIC_CATALOG = {
    "registration_total": {
        "label": "总注册用户", "unit": "count", "products": ["All"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": True,
        "description": "DB2 全部新增账号，未做产品拆分。",
    },
    "registration_domain_attributed": {
        "label": "domain 归因注册", "unit": "count", "products": ["iWeaver"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "attributed", "partial_allowed": True,
        "description": "users.domain 明确包含 iweaver 的观察值，必须结合覆盖率解释。",
    },
    "first_use_users": {
        "label": "首次使用用户", "unit": "count", "products": ["Palmly", "LearningCoach"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "attributed", "partial_allowed": True,
        "description": "Palmly 为首次报告用户；Learning Coach 为首次进入教练家族的用户。",
    },
    "active_users": {
        "label": "活跃用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "mixed", "partial_allowed": True,
        "description": "所选周期内至少有一个有效产品行为的去重用户；跨产品用户可能重叠。",
    },
    "new_active_users": {
        "label": "新增活跃用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "observed_first_use", "partial_allowed": True,
        "description": "首次有效行为落在所选周期内的活跃用户。",
    },
    "returning_active_users": {
        "label": "回访活跃用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "observed_first_use", "partial_allowed": True,
        "description": "所选周期之前已有该产品行为、周期内再次活跃的用户。",
    },
    "returning_share": {
        "label": "回访用户占比", "unit": "percent", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "observed_first_use", "partial_allowed": True,
        "description": "回访活跃用户 / 活跃用户。",
    },
    "user_turns": {
        "label": "用户轮次", "unit": "count", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "mixed", "partial_allowed": True,
        "description": "role=user 的有效聊天记录数。",
    },
    "assistant_turns": {
        "label": "助手轮次", "unit": "count", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "mixed", "partial_allowed": True,
        "description": "role=assistant 的有效聊天记录数。",
    },
    "median_user_turns": {
        "label": "用户轮次中位数", "unit": "median", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "mixed", "partial_allowed": True,
        "description": "每活跃用户在所选周期内用户轮次的中位数。",
    },
    "avg_user_turns": {
        "label": "人均用户轮次", "unit": "ratio", "products": list(PRODUCTS),
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "mixed", "partial_allowed": True,
        "description": "用户轮次 / 活跃用户。",
    },
    "depth_1_turn": {
        "label": "1 轮用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["bar"], "default_chart": "bar",
        "classification": "mixed", "partial_allowed": True,
        "description": "所选周期内只有 1 个用户轮次的活跃用户。",
    },
    "depth_2_3_turns": {
        "label": "2–3 轮用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["bar"], "default_chart": "bar",
        "classification": "mixed", "partial_allowed": True,
        "description": "所选周期内有 2–3 个用户轮次的活跃用户。",
    },
    "depth_4_9_turns": {
        "label": "4–9 轮用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["bar"], "default_chart": "bar",
        "classification": "mixed", "partial_allowed": True,
        "description": "所选周期内有 4–9 个用户轮次的活跃用户。",
    },
    "depth_10_plus_turns": {
        "label": "10+ 轮用户", "unit": "count", "products": list(PRODUCTS),
        "charts": ["bar"], "default_chart": "bar",
        "classification": "mixed", "partial_allowed": True,
        "description": "所选周期内有至少 10 个用户轮次的活跃用户。",
    },
    "reports": {
        "label": "Lunara 报告", "unit": "count", "products": ["Palmly"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": True,
        "description": "lunara_reports 精确报告数。",
    },
    "reports_per_user": {
        "label": "人均报告数", "unit": "ratio", "products": ["Palmly"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": True,
        "description": "报告数 / 报告用户数。",
    },
    "palm_followup_7d": {
        "label": "报告后 7 日回访率", "unit": "percent", "products": ["Palmly"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": False,
        "description": "首次报告用户在 7 日内再次生成报告的比例。",
    },
    "activation_24h": {
        "label": "24h 激活率", "unit": "percent", "products": ["iWeaver"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "attributed", "partial_allowed": True,
        "description": "成熟 domain 归因注册用户 24 小时内产生有效 iWeaver / Other 用户轮次。",
    },
    "learning_activation_weekly": {
        "label": "学习教练周深度达成率", "unit": "percent", "products": ["LearningCoach"],
        "charts": ["line", "bar"], "default_chart": "bar",
        "classification": "attributed", "partial_allowed": True,
        "description": "注册后 24 小时内进入学习教练且达到 3 个用户轮次和 1 个助手回复。",
    },
    "learning_activation_4w": {
        "label": "学习教练近 4 周深度达成率", "unit": "percent", "products": ["LearningCoach"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "attributed", "partial_allowed": True,
        "description": "连续 4 周分子之和 / 分母之和，不平均周百分比。",
    },
    "topic_completion_rate": {
        "label": "话题响应完成率", "unit": "percent", "products": ["All", "iWeaver", "LearningCoach"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "proxy", "partial_allowed": False,
        "description": "存在助手回复的话题 / 有用户发言且 topic_id 有效的话题。",
    },
    "retention_d1": {
        "label": "全站 D1 留存", "unit": "percent", "products": ["All"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": False,
        "description": "注册后第 1 个自然日有用户聊天的成熟注册 cohort。",
    },
    "retention_d7": {
        "label": "全站 D7 留存", "unit": "percent", "products": ["All"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": False,
        "description": "注册后第 7 个自然日有用户聊天的成熟注册 cohort。",
    },
    "retention_w1": {
        "label": "全站 W1 留存", "unit": "percent", "products": ["All"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "exact", "partial_allowed": False,
        "description": "注册后 7–14 日内有用户聊天的成熟注册 cohort。",
    },
    "domain_coverage": {
        "label": "domain 覆盖率", "unit": "percent", "products": ["All", "iWeaver"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "quality", "partial_allowed": True,
        "description": "新增账号中 domain 字段已填充的比例。",
    },
    "palm_linkage_coverage": {
        "label": "Palmly 消息关联率", "unit": "percent", "products": ["Palmly"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "quality", "partial_allowed": True,
        "description": "有 message_id 的报告中能关联到 chat_logs 的比例。",
    },
    "multi_product_overlap": {
        "label": "多产品活跃重叠率", "unit": "percent", "products": ["All"],
        "charts": ["line", "bar"], "default_chart": "line",
        "classification": "quality", "partial_allowed": True,
        "description": "同一周期使用两个及以上产品的活跃用户占比。",
    },
}

for metric_key, definition in METRIC_CATALOG.items():
    definition["grains"] = (
        ["day", "week", "month"] if metric_key in MULTI_GRAIN_METRICS else ["week"]
    )

DEFINITIONS = {
    "All": {
        "registration": "DB2 全部新增账号，未做站点拆分。",
        "activation": "总计不计算统一激活率，提供全站 D1/D7/W1 注册 cohort 留存。",
        "usage": "全部有效聊天记录。",
    },
    "iWeaver": {
        "registration": "domain 归因观察值，必须结合覆盖率解释，不外推缺失用户。",
        "activation": "成熟 domain 归因注册用户在 24 小时内产生有效 residual 用户轮次。",
        "usage": "排除精确 Lunara 报告消息和学习教练家族后的 residual 聊天。",
    },
    "Palmly": {
        "registration": "暂无独立注册来源；首次生成报告用户是产品首次使用代理。",
        "activation": "使用首次报告后 7 日再次生成报告作为产品回访指标。",
        "usage": "报告和报告用户精确；聊天仅统计 message_id 精确关联并显示关联覆盖率。",
    },
    "LearningCoach": {
        "registration": "尚未独立上线；首次使用用户基于教练家族行为。",
        "activation": "归因深度达成，主视图使用连续 4 周分子/分母汇总。",
        "usage": "学习教练命名家族及经核验的学习行为 Agent allowlist。",
    },
}


def learning_condition(alias="c"):
    names = ",".join(f"'{name}'" for name in LEARNING_COACH_ADJACENT)
    return (
        f"(COALESCE({alias}.agent_name, '') ILIKE '%learning-coach%' "
        f"OR COALESCE({alias}.agent_name, '') IN ({names}))"
    )


def classified_chat_cte():
    lc = learning_condition("c")
    return f"""
palm_messages AS (
    SELECT DISTINCT message_id FROM public.lunara_reports WHERE message_id IS NOT NULL
), classified_chat AS (
    SELECT c.created_at, c.user_id, c.message_id, NULLIF(c.topic_id, 0) AS topic_id, c.role,
           CASE WHEN pm.message_id IS NOT NULL THEN 'Palmly'
                WHEN {lc} THEN 'LearningCoach'
                ELSE 'iWeaver' END AS product
    FROM public.chat_logs c
    LEFT JOIN palm_messages pm ON pm.message_id = c.message_id
    WHERE c.deleted = false
), scoped_chat AS (
    SELECT * FROM classified_chat
    UNION ALL
    SELECT created_at, user_id, message_id, topic_id, role, 'All' AS product
    FROM classified_chat
)
"""


def period_parts(grain, column):
    if grain not in GRAIN_RANGES:
        raise ValueError(f"Unsupported grain: {grain}")
    key = "week_start" if grain == "week" else "period_start"
    return key, f"DATE_TRUNC('{grain}',{column})::date"


def usage_sql(range_start, range_end, grain="week"):
    period_key, created_period = period_parts(grain, "created_at")
    _, scoped_created_period = period_parts(grain, "c.created_at")
    _, first_period = period_parts(grain, "first_at")
    return f"""
WITH {classified_chat_cte()},
first_use AS (
    SELECT user_id, product, MIN(created_at) AS first_at
    FROM scoped_chat WHERE role='user' GROUP BY 1,2
), in_range AS (
    SELECT * FROM scoped_chat
    WHERE created_at >= '{range_start}' AND created_at < '{range_end}'
), usage AS (
    SELECT {created_period} AS {period_key}, product,
           COUNT(*) FILTER(WHERE role='user') AS user_turns,
           COUNT(*) FILTER(WHERE role='assistant') AS assistant_turns,
           COUNT(DISTINCT user_id) FILTER(WHERE role='user') AS active_users,
           COUNT(DISTINCT topic_id) FILTER(WHERE role='user') AS topics
    FROM in_range GROUP BY 1,2
), per_user AS (
    SELECT {scoped_created_period} AS {period_key},c.product,c.user_id,
           COUNT(*) AS user_turns,MIN(f.first_at) AS first_at
    FROM in_range c JOIN first_use f ON f.user_id=c.user_id AND f.product=c.product
    WHERE c.role='user' GROUP BY 1,2,3
), depth AS (
    SELECT {period_key},product,
           COUNT(*) FILTER(WHERE {first_period}={period_key}) AS new_active_users,
           COUNT(*) FILTER(WHERE {first_period}<{period_key}) AS returning_active_users,
           COUNT(*) FILTER(WHERE user_turns=1) AS depth_1_turn,
           COUNT(*) FILTER(WHERE user_turns BETWEEN 2 AND 3) AS depth_2_3_turns,
           COUNT(*) FILTER(WHERE user_turns BETWEEN 4 AND 9) AS depth_4_9_turns,
           COUNT(*) FILTER(WHERE user_turns>=10) AS depth_10_plus_turns,
           PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY user_turns) AS median_user_turns
    FROM per_user GROUP BY 1,2
)
SELECT u.*,COALESCE(d.new_active_users,0) new_active_users,
       COALESCE(d.returning_active_users,0) returning_active_users,
       COALESCE(d.depth_1_turn,0) depth_1_turn,
       COALESCE(d.depth_2_3_turns,0) depth_2_3_turns,
       COALESCE(d.depth_4_9_turns,0) depth_4_9_turns,
       COALESCE(d.depth_10_plus_turns,0) depth_10_plus_turns,
       d.median_user_turns
FROM usage u LEFT JOIN depth d USING({period_key},product)
ORDER BY {period_key},product
"""


def reports_sql(range_start, range_end, grain="week"):
    period_key, report_period = period_parts(grain, "lr.created_at")
    _, first_period = period_parts(grain, "first_at")
    return f"""
WITH first_report AS (
    SELECT user_id,MIN(created_at) first_at FROM public.lunara_reports GROUP BY 1
), per_user AS (
    SELECT {report_period} {period_key},lr.user_id,
           COUNT(*) reports,MIN(fr.first_at) first_at
    FROM public.lunara_reports lr JOIN first_report fr USING(user_id)
    WHERE lr.created_at>='{range_start}' AND lr.created_at<'{range_end}'
    GROUP BY 1,2
)
SELECT {period_key},SUM(reports)::bigint reports,COUNT(*)::bigint report_users,
       COUNT(*) FILTER(WHERE {first_period}={period_key})::bigint new_report_users,
       COUNT(*) FILTER(WHERE {first_period}<{period_key})::bigint returning_report_users,
       PERCENTILE_CONT(0.5) WITHIN GROUP(ORDER BY reports) median_reports,
       COUNT(*) FILTER(WHERE reports=1)::bigint report_depth_1,
       COUNT(*) FILTER(WHERE reports BETWEEN 2 AND 3)::bigint report_depth_2_3,
       COUNT(*) FILTER(WHERE reports BETWEEN 4 AND 9)::bigint report_depth_4_9,
       COUNT(*) FILTER(WHERE reports>=10)::bigint report_depth_10_plus
FROM per_user GROUP BY 1 ORDER BY 1
"""


def registration_sql(range_start, range_end, maturity_as_of):
    lc_c = learning_condition("c")
    return f"""
WITH users_in_range AS (
    SELECT uuid,created_at::timestamp reg_at,DATE_TRUNC('week',created_at::timestamp)::date week_start,domain
    FROM public.users WHERE deleted=false AND created_at::timestamp>='{range_start}' AND created_at::timestamp<'{range_end}'
), palm_messages AS (
    SELECT DISTINCT message_id FROM public.lunara_reports WHERE message_id IS NOT NULL
), iweaver_cohort AS (
    SELECT u.*,(u.reg_at+INTERVAL '{ACTIVATION_WINDOW_HOURS} hours'<='{maturity_as_of}'::timestamp) mature,
           EXISTS(SELECT 1 FROM public.chat_logs c LEFT JOIN palm_messages pm ON pm.message_id=c.message_id
                  WHERE c.deleted=false AND c.role='user' AND c.user_id=u.uuid AND pm.message_id IS NULL
                    AND NOT {lc_c} AND c.created_at>=u.reg_at AND c.created_at<u.reg_at+INTERVAL '{ACTIVATION_WINDOW_HOURS} hours') activated
    FROM users_in_range u WHERE COALESCE(u.domain,'') ILIKE '%iweaver%'
), first_lunara AS (
    SELECT user_id,MIN(created_at) first_at FROM public.lunara_reports GROUP BY 1
), first_lc AS (
    SELECT c.user_id,MIN(c.created_at) first_at FROM public.chat_logs c
    WHERE c.deleted=false AND c.role='user' AND {lc_c} GROUP BY 1
)
SELECT week_start,'All' product,COUNT(*)::bigint registration_exact,NULL::bigint registration_attributed,
       NULL::bigint activation_numerator,NULL::bigint activation_denominator
FROM users_in_range GROUP BY 1
UNION ALL
SELECT week_start,'iWeaver',COUNT(*)::bigint,NULL::bigint,
       COUNT(*) FILTER(WHERE mature AND activated)::bigint,COUNT(*) FILTER(WHERE mature)::bigint
FROM iweaver_cohort GROUP BY 1
UNION ALL
SELECT DATE_TRUNC('week',first_at)::date,'Palmly',NULL::bigint,COUNT(*)::bigint,NULL::bigint,NULL::bigint
FROM first_lunara WHERE first_at>='{range_start}' AND first_at<'{range_end}' GROUP BY 1
UNION ALL
SELECT DATE_TRUNC('week',first_at)::date,'LearningCoach',NULL::bigint,COUNT(*)::bigint,NULL::bigint,NULL::bigint
FROM first_lc WHERE first_at>='{range_start}' AND first_at<'{range_end}' GROUP BY 1
ORDER BY 1,2
"""


def registration_counts_sql(range_start, range_end, grain):
    period_key, registration_period = period_parts(grain, "created_at::timestamp")
    _, first_period = period_parts(grain, "first_at")
    lc_c = learning_condition("c")
    return f"""
WITH users_in_range AS (
    SELECT uuid,created_at::timestamp reg_at,{registration_period} {period_key},domain
    FROM public.users WHERE deleted=false AND created_at::timestamp>='{range_start}' AND created_at::timestamp<'{range_end}'
), first_lunara AS (
    SELECT user_id,MIN(created_at) first_at FROM public.lunara_reports GROUP BY 1
), first_lc AS (
    SELECT c.user_id,MIN(c.created_at) first_at FROM public.chat_logs c
    WHERE c.deleted=false AND c.role='user' AND {lc_c} GROUP BY 1
)
SELECT {period_key},'All' product,COUNT(*)::bigint registration_exact,NULL::bigint registration_attributed
FROM users_in_range GROUP BY 1
UNION ALL
SELECT {period_key},'iWeaver',COUNT(*)::bigint,NULL::bigint
FROM users_in_range WHERE COALESCE(domain,'') ILIKE '%iweaver%' GROUP BY 1
UNION ALL
SELECT {first_period},'Palmly',NULL::bigint,COUNT(*)::bigint
FROM first_lunara WHERE first_at>='{range_start}' AND first_at<'{range_end}' GROUP BY 1
UNION ALL
SELECT {first_period},'LearningCoach',NULL::bigint,COUNT(*)::bigint
FROM first_lc WHERE first_at>='{range_start}' AND first_at<'{range_end}' GROUP BY 1
ORDER BY 1,2
"""


def learning_activation_sql(range_start, range_end, maturity_as_of):
    lc_c = learning_condition("c")
    lc_c2 = learning_condition("c2")
    return f"""
WITH users_in_range AS (
    SELECT uuid,created_at::timestamp reg_at,DATE_TRUNC('week',created_at::timestamp)::date week_start
    FROM public.users WHERE deleted=false AND created_at::timestamp>='{range_start}' AND created_at::timestamp<'{range_end}'
), cohort AS (
    SELECT u.* FROM users_in_range u WHERE EXISTS(
        SELECT 1 FROM public.chat_logs c WHERE c.deleted=false AND c.role='user' AND c.user_id=u.uuid
          AND {lc_c} AND c.created_at>=u.reg_at AND c.created_at<u.reg_at+INTERVAL '{ACTIVATION_WINDOW_HOURS} hours')
), depth AS (
    SELECT u.uuid,u.week_start,u.reg_at,
           (u.reg_at+INTERVAL '{ACTIVATION_WINDOW_HOURS} hours'<='{maturity_as_of}'::timestamp) mature,
           COUNT(*) FILTER(WHERE c2.role='user') user_turns,
           COUNT(*) FILTER(WHERE c2.role='assistant') assistant_turns
    FROM cohort u LEFT JOIN public.chat_logs c2 ON c2.user_id=u.uuid AND c2.deleted=false AND {lc_c2}
      AND c2.created_at>=u.reg_at AND c2.created_at<u.reg_at+INTERVAL '{ACTIVATION_WINDOW_HOURS} hours'
    GROUP BY 1,2,3
)
SELECT week_start,COUNT(*) FILTER(WHERE mature AND user_turns>=3 AND assistant_turns>=1)::bigint numerator,
       COUNT(*) FILTER(WHERE mature)::bigint denominator,COUNT(*)::bigint attributed_users
FROM depth GROUP BY 1 ORDER BY 1
"""


def retention_sql(range_start, range_end, maturity_as_of):
    return f"""
WITH cohort AS (
    SELECT uuid,created_at::timestamp reg_at,DATE_TRUNC('week',created_at::timestamp)::date week_start
    FROM public.users WHERE deleted=false AND created_at::timestamp>='{range_start}' AND created_at::timestamp<'{range_end}'
), flags AS (
    SELECT u.*,
      (u.reg_at+INTERVAL '2 days'<='{maturity_as_of}'::timestamp AND u.reg_at+INTERVAL '1 day'>='2026-06-03 15:43:53'::timestamp) d1_eligible,
      (u.reg_at+INTERVAL '8 days'<='{maturity_as_of}'::timestamp AND u.reg_at+INTERVAL '7 days'>='2026-06-03 15:43:53'::timestamp) d7_eligible,
      (u.reg_at+INTERVAL '14 days'<='{maturity_as_of}'::timestamp AND u.reg_at+INTERVAL '7 days'>='2026-06-03 15:43:53'::timestamp) w1_eligible,
      EXISTS(SELECT 1 FROM public.chat_logs c WHERE c.deleted=false AND c.role='user' AND c.user_id=u.uuid
             AND c.created_at>=DATE_TRUNC('day',u.reg_at+INTERVAL '1 day') AND c.created_at<DATE_TRUNC('day',u.reg_at+INTERVAL '2 days')) d1,
      EXISTS(SELECT 1 FROM public.chat_logs c WHERE c.deleted=false AND c.role='user' AND c.user_id=u.uuid
             AND c.created_at>=DATE_TRUNC('day',u.reg_at+INTERVAL '7 days') AND c.created_at<DATE_TRUNC('day',u.reg_at+INTERVAL '8 days')) d7,
      EXISTS(SELECT 1 FROM public.chat_logs c WHERE c.deleted=false AND c.role='user' AND c.user_id=u.uuid
             AND c.created_at>=u.reg_at+INTERVAL '7 days' AND c.created_at<u.reg_at+INTERVAL '14 days') w1
    FROM cohort u
)
SELECT week_start,COUNT(*) total_users,
 COUNT(*) FILTER(WHERE d1_eligible AND d1) d1_numerator,COUNT(*) FILTER(WHERE d1_eligible) d1_denominator,
 COUNT(*) FILTER(WHERE d7_eligible AND d7) d7_numerator,COUNT(*) FILTER(WHERE d7_eligible) d7_denominator,
 COUNT(*) FILTER(WHERE w1_eligible AND w1) w1_numerator,COUNT(*) FILTER(WHERE w1_eligible) w1_denominator
FROM flags GROUP BY 1 ORDER BY 1
"""


def palmly_followup_sql(range_start, range_end, maturity_as_of):
    return f"""
WITH first_report AS (
    SELECT user_id,MIN(created_at) first_at FROM public.lunara_reports GROUP BY 1
), cohort AS (
    SELECT *,DATE_TRUNC('week',first_at)::date week_start,
      (first_at+INTERVAL '7 days'<='{maturity_as_of}'::timestamp) mature,
      EXISTS(SELECT 1 FROM public.lunara_reports later WHERE later.user_id=fr.user_id
             AND later.created_at>fr.first_at AND later.created_at<=fr.first_at+INTERVAL '7 days') followed
    FROM first_report fr WHERE first_at>='{range_start}' AND first_at<'{range_end}'
)
SELECT week_start,COUNT(*) first_report_users,
 COUNT(*) FILTER(WHERE mature AND followed) numerator,COUNT(*) FILTER(WHERE mature) denominator
FROM cohort GROUP BY 1 ORDER BY 1
"""


def topic_completion_sql(range_start, range_end):
    return f"""
WITH {classified_chat_cte()}, filtered AS (
    SELECT * FROM scoped_chat WHERE product<>'Palmly' AND topic_id IS NOT NULL
      AND created_at>='{range_start}' AND created_at<'{range_end}'
), per_topic AS (
    SELECT DATE_TRUNC('week',MIN(created_at) FILTER(WHERE role='user'))::date week_start,product,topic_id,
           MIN(created_at) FILTER(WHERE role='user') first_user_at,
           MAX(created_at) FILTER(WHERE role='assistant') last_assistant_at
    FROM filtered GROUP BY 2,3
)
SELECT week_start,product,
 COUNT(*) FILTER(WHERE first_user_at IS NOT NULL AND last_assistant_at>first_user_at) numerator,
 COUNT(*) FILTER(WHERE first_user_at IS NOT NULL) denominator
FROM per_topic WHERE week_start IS NOT NULL GROUP BY 1,2 ORDER BY 1,2
"""


def domain_quality_sql(range_start, range_end, grain="week"):
    period_key, created_period = period_parts(grain, "created_at::timestamp")
    return f"""
SELECT {created_period} {period_key},COUNT(*) total_users,
 COUNT(*) FILTER(WHERE NULLIF(domain,'') IS NOT NULL) domain_populated,
 COUNT(*) FILTER(WHERE COALESCE(domain,'') ILIKE '%iweaver%') iweaver_domain
FROM public.users WHERE deleted=false AND created_at::timestamp>='{range_start}' AND created_at::timestamp<'{range_end}'
GROUP BY 1 ORDER BY 1
"""


def palmly_link_quality_sql(range_start, range_end, grain="week"):
    period_key, report_period = period_parts(grain, "lr.created_at")
    return f"""
SELECT {report_period} {period_key},
 COUNT(*)::bigint reports,
 COUNT(*) FILTER(WHERE NULLIF(lr.message_id,'') IS NOT NULL)::bigint with_message_id,
 COUNT(*) FILTER(WHERE EXISTS(
   SELECT 1 FROM public.chat_logs c WHERE c.message_id=lr.message_id AND c.deleted=false
 ))::bigint linked_reports
FROM public.lunara_reports lr
WHERE lr.created_at>='{range_start}' AND lr.created_at<'{range_end}' GROUP BY 1 ORDER BY 1
"""


def overlap_quality_sql(range_start, range_end, grain="week"):
    period_key, created_period = period_parts(grain, "created_at")
    return f"""
WITH {classified_chat_cte()}, per_user AS (
    SELECT {created_period} {period_key},user_id,COUNT(DISTINCT product) products
    FROM classified_chat WHERE role='user' AND created_at>='{range_start}' AND created_at<'{range_end}' GROUP BY 1,2
)
SELECT {period_key},COUNT(*) active_users,COUNT(*) FILTER(WHERE products>1) overlap_users
FROM per_user GROUP BY 1 ORDER BY 1
"""


def source_ranges_sql():
    lc = learning_condition("c")
    return f"""
SELECT 'users' source,MIN(created_at::timestamp) first_at,MAX(created_at::timestamp) last_at FROM public.users
UNION ALL SELECT 'chat_logs',MIN(created_at),MAX(created_at) FROM public.chat_logs WHERE deleted=false
UNION ALL SELECT 'lunara_reports',MIN(created_at),MAX(created_at) FROM public.lunara_reports
UNION ALL SELECT 'learning_coach',MIN(c.created_at),MAX(c.created_at) FROM public.chat_logs c
 WHERE c.deleted=false AND c.role='user' AND {lc}
"""


def freshness_sql():
    return """
SELECT source,latest FROM (
 SELECT 'users' source,MAX(created_at::timestamp) latest FROM public.users
 UNION ALL SELECT 'chat_logs',MAX(created_at) FROM public.chat_logs
 UNION ALL SELECT 'lunara_reports',MAX(created_at) FROM public.lunara_reports
) freshness ORDER BY latest DESC NULLS LAST
"""
