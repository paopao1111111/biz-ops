"""SQLite email queue with guarded, durable delivery state transitions."""
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class EmailQueue:
    PROCESSED_COLUMNS = {
        "thread_id": "TEXT", "outcome": "TEXT", "outcome_reason": "TEXT",
        "draft_id": "TEXT", "error": "TEXT", "updated_at": "TEXT",
    }
    TERMINAL_OUTCOMES = {
        "auto_sent", "sent_unconfirmed", "draft_created", "manual", "discarded",
        "auto_sent_legacy", "legacy_processed",
    }
    SEND_BLOCKING_OUTCOMES = {
        "sending", "auto_sent", "sent_unconfirmed", "auto_sent_legacy", "legacy_processed",
    }

    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db_path)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT UNIQUE NOT NULL,
                sender TEXT, subject TEXT, body TEXT, category TEXT DEFAULT 'pending',
                status TEXT DEFAULT 'pending', priority INTEGER DEFAULT 0, retries INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3, error TEXT, created_at TEXT, updated_at TEXT, processed_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS processed (
                message_id TEXT PRIMARY KEY, category TEXT, reply_sent INTEGER DEFAULT 0,
                faq_id INTEGER, processed_at TEXT)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, received INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
                vip_count INTEGER DEFAULT 0, refund_count INTEGER DEFAULT 0)""")
            existing = {row[1] for row in conn.execute("PRAGMA table_info(processed)")}
            for name, column_type in self.PROCESSED_COLUMNS.items():
                if name not in existing:
                    conn.execute("ALTER TABLE processed ADD COLUMN %s %s" % (name, column_type))
            # Every pre-outcome row was already processed by the legacy service. Backfill it
            # in the same schema-migration transaction so polling can never observe a NULL
            # outcome and reprocess historical mail. Preserve the legacy reply_sent flag.
            conn.execute("""UPDATE processed
                SET outcome=CASE WHEN reply_sent=1 THEN 'auto_sent_legacy' ELSE 'legacy_processed' END,
                    outcome_reason=CASE WHEN reply_sent=1 THEN 'legacy_reply_sent' ELSE 'legacy_processed' END,
                    updated_at=COALESCE(updated_at, processed_at)
                WHERE outcome IS NULL OR TRIM(outcome)=''""")

    def is_processed(self, message_id):
        """Return true only for terminal outcomes; retryable failures remain eligible."""
        outcome = self.get_outcome(message_id)
        return bool(outcome and outcome.get("outcome") in self.TERMINAL_OUTCOMES)

    def enqueue(self, message_id, sender, subject, body, priority=0):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute("""INSERT INTO queue
                    (message_id, sender, subject, body, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (message_id, sender, subject, body, priority, now, now))
                return True
            except sqlite3.IntegrityError:
                return False

    def record_outcome(self, message_id, category, outcome, outcome_reason="", faq_id=None,
                       thread_id="", draft_id="", error="", allowed_from=None):
        """Guarded UPSERT that never downgrades successful or uncertain send states."""
        now = datetime.now().isoformat()
        reply_sent = 1 if outcome == "auto_sent" else 0
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute("SELECT outcome FROM processed WHERE message_id=?", (message_id,)).fetchone()
            current = existing[0] if existing else None
            if current in {"auto_sent", "sent_unconfirmed"} and outcome != current:
                return False
            if allowed_from is not None and current not in set(allowed_from):
                return False
            conn.execute("""INSERT INTO processed
                (message_id, category, reply_sent, faq_id, processed_at, thread_id,
                 outcome, outcome_reason, draft_id, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id) DO UPDATE SET
                  category=excluded.category, reply_sent=excluded.reply_sent,
                  faq_id=excluded.faq_id, processed_at=excluded.processed_at,
                  thread_id=excluded.thread_id, outcome=excluded.outcome,
                  outcome_reason=excluded.outcome_reason, draft_id=excluded.draft_id,
                  error=excluded.error, updated_at=excluded.updated_at""",
                (message_id, category, reply_sent, faq_id, now, thread_id,
                 outcome, outcome_reason, draft_id, error, now))
            return True

    def claim_send(self, message_id, category, faq_id=None, thread_id=""):
        """Persist an exclusive pre-send claim. Missing thread IDs are never claimable."""
        if not message_id or not thread_id:
            return False
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT outcome FROM processed WHERE message_id=?", (message_id,)).fetchone()
            if row and row[0] in self.SEND_BLOCKING_OUTCOMES | self.TERMINAL_OUTCOMES:
                return False
            conn.execute("""INSERT INTO processed
                (message_id, category, reply_sent, faq_id, processed_at, thread_id,
                 outcome, outcome_reason, draft_id, error, updated_at)
                VALUES (?, ?, 0, ?, ?, ?, 'sending', 'pre_send_claim', '', '', ?)
                ON CONFLICT(message_id) DO UPDATE SET category=excluded.category,
                  faq_id=excluded.faq_id, processed_at=excluded.processed_at,
                  thread_id=excluded.thread_id, outcome='sending',
                  outcome_reason='pre_send_claim', error='', updated_at=excluded.updated_at""",
                (message_id, category, faq_id, now, thread_id, now))
            return True

    def confirm_sent(self, message_id, category, reason, faq_id=None, thread_id=""):
        return self.record_outcome(message_id, category, "auto_sent", reason, faq_id=faq_id,
                                   thread_id=thread_id, allowed_from={"sending"})

    def preserve_sent_unconfirmed(self, message_id, category, reason, faq_id=None, thread_id="", error=""):
        return self.record_outcome(message_id, category, "sent_unconfirmed", reason, faq_id=faq_id,
                                   thread_id=thread_id, error=error, allowed_from={"sending", "sent_unconfirmed"})

    def release_send_claim(self, message_id, category, reason, faq_id=None, thread_id="", error=""):
        return self.record_outcome(message_id, category, "retryable_error", reason, faq_id=faq_id,
                                   thread_id=thread_id, error=error, allowed_from={"sending", "retryable_error", None})

    def mark_processed(self, message_id, category, faq_id=None):
        self.record_outcome(message_id, category, "manual", "legacy_mark_processed", faq_id=faq_id)

    def mark_failed(self, message_id, error):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""UPDATE queue SET status='failed', error=?, retries=retries+1,
                updated_at=? WHERE message_id=?""", (error, now, message_id))

    def thread_auto_sent_recently(self, thread_id, cooldown_seconds):
        if not thread_id:
            return False
        cutoff = (datetime.now() - timedelta(seconds=cooldown_seconds)).isoformat()
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("""SELECT 1 FROM processed
                WHERE thread_id=? AND outcome IN ('sending','auto_sent','sent_unconfirmed')
                  AND processed_at>=? LIMIT 1""", (thread_id, cutoff)).fetchone() is not None

    def get_outcome(self, message_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM processed WHERE message_id=?", (message_id,)).fetchone()
            return dict(row) if row else None

    def get_stats(self, days=7):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("""SELECT * FROM stats WHERE date >= date('now', '-%d days')
                ORDER BY date DESC""" % int(days)).fetchall()

    def cleanup_old(self, days=30):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM queue WHERE status='success' AND created_at < date('now', '-%d days')" % int(days))
            conn.execute("DELETE FROM processed WHERE processed_at < date('now', '-%d days')" % int(days))
