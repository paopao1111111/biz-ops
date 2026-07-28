from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


REQUEST_TYPES = (
    "like", "unlike", "repost", "unrepost",
    "post_create", "post_delete", "reply", "article_draft_publish",
)
REQUEST_STATUSES = (
    "draft", "pending_approval", "approved", "queued", "executing",
    "succeeded", "failed", "manual_reconciliation_required", "cancelled",
)
OPERATION_STATUSES = (
    "queued", "running", "awaiting_approval", "succeeded", "failed_known",
    "uncertain", "reconciled_succeeded", "reconciled_failed", "cancelled",
)
ATTEMPT_STATES = ("not_started", "sending", "confirmed", "uncertain")
STEP_STATUSES = ("pending", "running", "succeeded", "failed", "uncertain", "skipped")

MIGRATIONS = [
    """
    CREATE TABLE write_accounts (
        id INTEGER PRIMARY KEY,
        account_key TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL,
        x_user_id TEXT UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
        paused INTEGER NOT NULL DEFAULT 1 CHECK (paused IN (0, 1)),
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE write_requests (
        id INTEGER PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE,
        account_id INTEGER NOT NULL REFERENCES write_accounts(id) ON DELETE RESTRICT,
        request_type TEXT NOT NULL CHECK (request_type IN ('like','unlike','repost','unrepost','post_create','post_delete','article_draft_publish')),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft','pending_approval','approved','queued','executing','succeeded','failed','manual_reconciliation_required','cancelled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_write_requests_account_status ON write_requests(account_id, status);
    CREATE TABLE write_approvals (
        id INTEGER PRIMARY KEY,
        request_id INTEGER NOT NULL REFERENCES write_requests(id) ON DELETE CASCADE,
        request_version INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('approved','rejected')),
        actor TEXT NOT NULL,
        reason TEXT,
        confirmation_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(confirmation_json)),
        expires_at INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX idx_write_approvals_request ON write_approvals(request_id, created_at);
    CREATE TABLE write_operations (
        id INTEGER PRIMARY KEY,
        operation_key TEXT NOT NULL UNIQUE,
        request_id INTEGER NOT NULL REFERENCES write_requests(id) ON DELETE RESTRICT,
        account_id INTEGER NOT NULL REFERENCES write_accounts(id) ON DELETE RESTRICT,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('queued','running','awaiting_approval','succeeded','failed_known','uncertain','reconciled_succeeded','reconciled_failed','cancelled')),
        attempt_state TEXT NOT NULL DEFAULT 'not_started' CHECK (attempt_state IN ('not_started','sending','confirmed','uncertain')),
        idempotency_key TEXT NOT NULL UNIQUE,
        lease_token TEXT,
        lease_expires_at INTEGER,
        current_step INTEGER NOT NULL DEFAULT 1 CHECK (current_step >= 1),
        attempt_started_at INTEGER,
        attempt_finished_at INTEGER,
        external_object_id TEXT,
        response_json TEXT,
        error_code TEXT,
        error_message TEXT,
        retry_after INTEGER,
        reconciliation_note TEXT,
        reconciled_by TEXT,
        reconciled_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_write_operations_account_status ON write_operations(account_id, status);
    CREATE INDEX idx_write_operations_claim ON write_operations(status, id);
    CREATE TABLE write_operation_steps (
        id INTEGER PRIMARY KEY,
        operation_id INTEGER NOT NULL REFERENCES write_operations(id) ON DELETE CASCADE,
        step_name TEXT NOT NULL,
        step_order INTEGER NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending','running','succeeded','failed','uncertain','skipped')),
        attempt_state TEXT NOT NULL DEFAULT 'not_started' CHECK (attempt_state IN ('not_started','sending','confirmed','uncertain')),
        external_object_id TEXT,
        detail_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(detail_json)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(operation_id, step_order)
    );
    CREATE TABLE media_assets (
        id INTEGER PRIMARY KEY,
        asset_key TEXT NOT NULL UNIQUE,
        account_id INTEGER REFERENCES write_accounts(id) ON DELETE RESTRICT,
        sha256 TEXT NOT NULL,
        mime_type TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        local_path TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending','ready','failed','expired')),
        metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_media_assets_account_status ON media_assets(account_id, status);
    CREATE TABLE quota_policies (
        id INTEGER PRIMARY KEY,
        account_id INTEGER REFERENCES write_accounts(id) ON DELETE CASCADE,
        operation_type TEXT NOT NULL,
        window_seconds INTEGER NOT NULL CHECK (window_seconds > 0),
        max_operations INTEGER NOT NULL CHECK (max_operations >= 0),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        UNIQUE(account_id, operation_type, window_seconds)
    );
    CREATE TABLE quota_usage (
        id INTEGER PRIMARY KEY,
        policy_id INTEGER NOT NULL REFERENCES quota_policies(id) ON DELETE CASCADE,
        window_start INTEGER NOT NULL,
        used_count INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
        updated_at INTEGER NOT NULL,
        UNIQUE(policy_id, window_start)
    );
    CREATE TABLE write_audit_log (
        id INTEGER PRIMARY KEY,
        event_type TEXT NOT NULL,
        actor TEXT NOT NULL,
        target_type TEXT NOT NULL,
        target_id TEXT,
        detail_json TEXT NOT NULL CHECK (json_valid(detail_json)),
        created_at INTEGER NOT NULL
    );
    CREATE INDEX idx_write_audit_created ON write_audit_log(created_at, id);
    CREATE INDEX idx_write_audit_target ON write_audit_log(target_type, target_id, id);
    CREATE TABLE write_settings (
        setting_key TEXT PRIMARY KEY,
        value_json TEXT NOT NULL CHECK (json_valid(value_json)),
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE used_nonces (
        nonce TEXT PRIMARY KEY,
        used_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL
    );
    CREATE INDEX idx_used_nonces_expires ON used_nonces(expires_at);
    """,
    """
    ALTER TABLE write_accounts ADD COLUMN credential_ref TEXT;
    ALTER TABLE write_accounts ADD COLUMN auth_type TEXT;
    ALTER TABLE write_accounts ADD COLUMN credential_generation INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE write_accounts ADD COLUMN authorization_status TEXT NOT NULL DEFAULT 'unconfigured';
    ALTER TABLE write_accounts ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(capabilities_json));
    ALTER TABLE write_accounts ADD COLUMN source_profile_id TEXT;
    CREATE UNIQUE INDEX idx_write_accounts_credential_ref ON write_accounts(credential_ref) WHERE credential_ref IS NOT NULL;
    CREATE UNIQUE INDEX idx_write_accounts_source_profile ON write_accounts(source_profile_id) WHERE source_profile_id IS NOT NULL;
    CREATE TABLE oauth_authorization_flows (
        id INTEGER PRIMARY KEY,
        flow_key TEXT NOT NULL UNIQUE,
        state_hash TEXT NOT NULL UNIQUE,
        code_verifier TEXT NOT NULL,
        developer_app TEXT NOT NULL,
        requested_scopes_json TEXT NOT NULL CHECK (json_valid(requested_scopes_json)),
        redirect_uri TEXT NOT NULL,
        account_id INTEGER REFERENCES write_accounts(id) ON DELETE SET NULL,
        source_profile_id TEXT,
        source_label TEXT,
        display_name TEXT,
        expected_x_user_id TEXT,
        credential_ref TEXT,
        status TEXT NOT NULL CHECK (status IN ('pending','processing','succeeded','failed','cancelled','expired')),
        error_code TEXT,
        result_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(result_json)),
        actor TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        claimed_at INTEGER,
        consumed_at INTEGER,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX idx_oauth_flows_status_expiry ON oauth_authorization_flows(status, expires_at);
    CREATE INDEX idx_oauth_flows_account ON oauth_authorization_flows(account_id, id);
    """,
    """
    -- Relax write_requests.request_type CHECK to include 'reply' (SQLite cannot ALTER a CHECK in place).
    -- FK enforcement is disabled for the whole migration transaction by migrate().
    CREATE TABLE write_requests_new (
        id INTEGER PRIMARY KEY,
        request_key TEXT NOT NULL UNIQUE,
        account_id INTEGER NOT NULL REFERENCES write_accounts(id) ON DELETE RESTRICT,
        request_type TEXT NOT NULL CHECK (request_type IN ('like','unlike','repost','unrepost','post_create','post_delete','reply','article_draft_publish')),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('draft','pending_approval','approved','queued','executing','succeeded','failed','manual_reconciliation_required','cancelled')),
        version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    INSERT INTO write_requests_new(id, request_key, account_id, request_type, payload_json, content_hash, status, version, created_by, created_at, updated_at)
        SELECT id, request_key, account_id, request_type, payload_json, content_hash, status, version, created_by, created_at, updated_at FROM write_requests;
    DROP TABLE write_requests;
    ALTER TABLE write_requests_new RENAME TO write_requests;
    CREATE INDEX idx_write_requests_account_status ON write_requests(account_id, status);
    """,
    """
    -- Scheduled publishing: operations may be released only at/after scheduled_at.
    ALTER TABLE write_operations ADD COLUMN scheduled_at INTEGER;
    CREATE INDEX IF NOT EXISTS idx_write_operations_scheduled ON write_operations(scheduled_at, id) WHERE scheduled_at IS NOT NULL;
    """,
    """
    -- Media upload: record the X media_id once the executor uploads the asset
    -- at send time (status transitions pending -> ready). nullable so existing
    -- rows survive.
    ALTER TABLE media_assets ADD COLUMN x_media_id TEXT;
    ALTER TABLE media_assets ADD COLUMN uploaded_at INTEGER;
    """,
]

DEFAULT_SETTINGS = {
    "global_write_paused": True,
    "approval_required": True,
    "max_active_operations": 1,
    "credit_guard": {"daily_limit": 0, "costs": {}},
}


class Database:
    def __init__(self, path: str, busy_timeout_ms: int = 5000):
        self.path = path
        self.busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.busy_timeout_ms / 1000, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        return conn

    def migrate(self) -> None:
        conn = self.connect()
        try:
            # Migrations may rebuild tables referenced by foreign keys; disable FK
            # enforcement for the migration transaction and validate integrity
            # with PRAGMA foreign_key_check before commit (no-op outside a txn).
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL)"
                )
                applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
                for version, sql in enumerate(MIGRATIONS, start=1):
                    if version not in applied:
                        for statement in sql.split(";"):
                            if statement.strip():
                                conn.execute(statement)
                        conn.execute(
                            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                            (version, int(time.time())),
                        )
                now = int(time.time())
                for key, value in DEFAULT_SETTINGS.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO write_settings(setting_key, value_json, updated_at) VALUES (?, ?, ?)",
                        (key, json.dumps(value), now),
                    )
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise sqlite3.IntegrityError("foreign key check failed")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
