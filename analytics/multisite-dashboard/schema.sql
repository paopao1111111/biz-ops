PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS weekly_product_metrics (
    week_start               TEXT NOT NULL,
    week_end                 TEXT NOT NULL,
    window_start             TEXT NOT NULL,
    window_end               TEXT NOT NULL,
    window_kind              TEXT NOT NULL CHECK (window_kind IN ('full', 'partial', 'aligned_previous')),
    product                  TEXT NOT NULL CHECK (product IN ('All', 'iWeaver', 'Palmly', 'LearningCoach')),
    registration_exact       INTEGER,
    registration_attributed  INTEGER,
    activation_numerator     INTEGER,
    activation_denominator   INTEGER,
    user_turns               INTEGER NOT NULL DEFAULT 0,
    assistant_turns          INTEGER NOT NULL DEFAULT 0,
    active_users             INTEGER NOT NULL DEFAULT 0,
    topics                   INTEGER NOT NULL DEFAULT 0,
    reports                  INTEGER NOT NULL DEFAULT 0,
    data_complete            INTEGER NOT NULL DEFAULT 1 CHECK (data_complete IN (0, 1)),
    rule_version             TEXT NOT NULL,
    collected_at             TEXT NOT NULL,
    source_freshness         TEXT,
    PRIMARY KEY (week_start, product, window_kind)
);

CREATE TABLE IF NOT EXISTS weekly_metric_series (
    week_start        TEXT NOT NULL,
    window_kind       TEXT NOT NULL CHECK (window_kind IN ('full', 'partial', 'aligned_previous')),
    product           TEXT NOT NULL CHECK (product IN ('All', 'iWeaver', 'Palmly', 'LearningCoach')),
    metric_key        TEXT NOT NULL,
    value             REAL,
    numerator         INTEGER,
    denominator       INTEGER,
    status            TEXT NOT NULL CHECK (status IN (
        'available', 'source_unavailable', 'pre_launch', 'immature',
        'partial_maturity', 'insufficient_sample', 'linkage_incomplete',
        'left_censored', 'not_applicable'
    )),
    quality_code      TEXT,
    quality_value     REAL,
    window_start      TEXT NOT NULL,
    window_end        TEXT NOT NULL,
    rule_version      TEXT NOT NULL,
    collected_at      TEXT NOT NULL,
    source_freshness  TEXT,
    PRIMARY KEY (week_start, window_kind, product, metric_key)
);

CREATE TABLE IF NOT EXISTS weekly_data_quality (
    week_start        TEXT NOT NULL,
    window_kind       TEXT NOT NULL CHECK (window_kind IN ('full', 'partial', 'aligned_previous')),
    scope              TEXT NOT NULL,
    quality_key        TEXT NOT NULL,
    numerator          INTEGER,
    denominator        INTEGER,
    value_pct          REAL,
    status             TEXT NOT NULL CHECK (status IN (
        'available', 'source_unavailable', 'pre_launch', 'partial',
        'linkage_incomplete', 'not_applicable'
    )),
    details            TEXT,
    rule_version       TEXT NOT NULL,
    collected_at       TEXT NOT NULL,
    source_freshness   TEXT,
    PRIMARY KEY (week_start, window_kind, scope, quality_key)
);

CREATE TABLE IF NOT EXISTS period_metric_series (
    grain             TEXT NOT NULL CHECK (grain IN ('day', 'month')),
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    window_kind       TEXT NOT NULL CHECK (window_kind IN ('full', 'partial')),
    product           TEXT NOT NULL CHECK (product IN ('All', 'iWeaver', 'Palmly', 'LearningCoach')),
    metric_key        TEXT NOT NULL,
    value             REAL,
    numerator         INTEGER,
    denominator       INTEGER,
    status            TEXT NOT NULL CHECK (status IN (
        'available', 'source_unavailable', 'pre_launch', 'immature',
        'partial_maturity', 'insufficient_sample', 'linkage_incomplete',
        'left_censored', 'not_applicable'
    )),
    quality_code      TEXT,
    quality_value     REAL,
    rule_version      TEXT NOT NULL,
    collected_at      TEXT NOT NULL,
    source_freshness  TEXT,
    PRIMARY KEY (grain, period_start, product, metric_key)
);

CREATE TABLE IF NOT EXISTS collector_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT NOT NULL,
    finished_at       TEXT NOT NULL,
    status            TEXT NOT NULL CHECK (status IN ('success', 'failed', 'dry_run')),
    weeks_requested   INTEGER NOT NULL,
    rows_written      INTEGER NOT NULL DEFAULT 0,
    source_freshness  TEXT,
    rule_version      TEXT NOT NULL,
    error_summary     TEXT
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version       INTEGER PRIMARY KEY,
    applied_at    TEXT NOT NULL,
    description   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metrics_week_kind
    ON weekly_product_metrics(week_start DESC, window_kind);
CREATE INDEX IF NOT EXISTS idx_metrics_product_week
    ON weekly_product_metrics(product, week_start DESC);
CREATE INDEX IF NOT EXISTS idx_series_metric_week
    ON weekly_metric_series(metric_key, week_start DESC, window_kind);
CREATE INDEX IF NOT EXISTS idx_series_product_week
    ON weekly_metric_series(product, week_start DESC, window_kind);
CREATE INDEX IF NOT EXISTS idx_quality_key_week
    ON weekly_data_quality(quality_key, week_start DESC, window_kind);
CREATE INDEX IF NOT EXISTS idx_period_series_metric
    ON period_metric_series(grain, metric_key, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_period_series_product
    ON period_metric_series(grain, product, period_start DESC);
CREATE INDEX IF NOT EXISTS idx_runs_finished
    ON collector_runs(finished_at DESC);
