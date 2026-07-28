"""SQLite-based email queue with retry support"""
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path

class EmailQueue:
    def __init__(self, db_path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE NOT NULL,
                sender TEXT,
                subject TEXT,
                body TEXT,
                category TEXT DEFAULT 'pending',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                retries INTEGER DEFAULT 0,
                max_retries INTEGER DEFAULT 3,
                error TEXT,
                created_at TEXT,
                updated_at TEXT,
                processed_at TEXT
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS processed (
                message_id TEXT PRIMARY KEY,
                category TEXT,
                reply_sent INTEGER DEFAULT 0,
                faq_id INTEGER,
                processed_at TEXT
            )''')
            conn.execute('''CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT,
                received INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0,
                failed INTEGER DEFAULT 0,
                vip_count INTEGER DEFAULT 0,
                refund_count INTEGER DEFAULT 0
            )''')
    
    def is_processed(self, message_id):
        """Check if message was already processed"""
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM processed WHERE message_id=?", 
                (message_id,)
            ).fetchone()
            return row is not None
    
    def enqueue(self, message_id, sender, subject, body, priority=0):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            try:
                conn.execute('''INSERT INTO queue 
                    (message_id, sender, subject, body, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (message_id, sender, subject, body, priority, now, now))
                return True
            except sqlite3.IntegrityError:
                return False
    
    def mark_processed(self, message_id, category, faq_id=None):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''INSERT OR REPLACE INTO processed 
                (message_id, category, reply_sent, faq_id, processed_at)
                VALUES (?, ?, 1, ?, ?)''',
                (message_id, category, faq_id, now))
    
    def mark_failed(self, message_id, error):
        now = datetime.now().isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''UPDATE queue SET status='failed', error=?,
                retries=retries+1, updated_at=? WHERE message_id=?''',
                (error, now, message_id))
    
    def get_stats(self, days=7):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute('''SELECT * FROM stats 
                WHERE date >= date('now', '-{} days')
                ORDER BY date DESC'''.format(days)).fetchall()
    
    def cleanup_old(self, days=30):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''DELETE FROM queue 
                WHERE status='success' AND created_at < date('now', '-{} days')'''.format(days))
            conn.execute('''DELETE FROM processed 
                WHERE processed_at < date('now', '-{} days')'''.format(days))
