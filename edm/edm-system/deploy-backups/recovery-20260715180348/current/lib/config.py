"""Configuration loader"""
import os
from dotenv import load_dotenv

load_dotenv('/opt/edm-system/.env')

class Config:
    # Gmail
    GMAIL_SERVICE_ACCOUNT_FILE = os.getenv('GMAIL_SERVICE_ACCOUNT_FILE')
    GMAIL_SENDER = os.getenv('GMAIL_SENDER', 'iweaver@iweaver.ai')
    EDM_POLL_INTERVAL = int(os.getenv('EDM_POLL_INTERVAL', '300'))
    EDM_AUTO_REPLY_ENABLED = os.getenv('EDM_AUTO_REPLY_ENABLED', 'false').lower() == 'true'
    GMAIL_HTTP_TIMEOUT = int(os.getenv('GMAIL_HTTP_TIMEOUT', '30'))
    EDM_SERVICE_ERROR_ALERT_THRESHOLD = int(os.getenv('EDM_SERVICE_ERROR_ALERT_THRESHOLD', '6'))
    EDM_THREAD_COOLDOWN_SECONDS = int(os.getenv('EDM_THREAD_COOLDOWN_SECONDS', '86400'))
    EDM_AUTO_REPLY_MIN_BODY_CHARS = int(os.getenv('EDM_AUTO_REPLY_MIN_BODY_CHARS', '20'))
    
    # LLM
    LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'http://127.0.0.1:8317/v1')
    LLM_API_KEY = os.getenv('LLM_API_KEY', '')
    LLM_MODEL = os.getenv('LLM_MODEL', 'qwen3.7-plus')
    LLM_MAX_TOKENS = int(os.getenv('LLM_MAX_TOKENS', '2000'))
    LLM_TEMPERATURE = float(os.getenv('LLM_TEMPERATURE', '0.7'))
    
    # Feishu
    FEISHU_APP_ID = os.getenv('FEISHU_APP_ID', '')
    FEISHU_APP_SECRET = os.getenv('FEISHU_APP_SECRET', '')
    FEISHU_CHAT_ID = os.getenv('FEISHU_CHAT_ID', '')
    FEISHU_NOTIFY_USER_ID = os.getenv('FEISHU_NOTIFY_USER_ID', '')
    
    # Queue
    QUEUE_DB_PATH = os.getenv('QUEUE_DB_PATH', '/opt/edm-system/data/queue.db')
    MAX_RETRIES = int(os.getenv('MAX_RETRIES', '3'))
    RETRY_DELAY = int(os.getenv('RETRY_DELAY', '60'))
    
    # Sender
    RATE_LIMIT = int(os.getenv('RATE_LIMIT', '10'))
    BATCH_SIZE = int(os.getenv('BATCH_SIZE', '5'))
    TRACK_OPENS = os.getenv('TRACK_OPENS', 'true').lower() == 'true'
    TRACK_CLICKS = os.getenv('TRACK_CLICKS', 'true').lower() == 'true'
    
    # Dashboard
    DASHBOARD_HOST = os.getenv('DASHBOARD_HOST', '0.0.0.0')
    DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', '8765'))
    DASHBOARD_PASSWORD = os.getenv('DASHBOARD_PASSWORD', '')

    # Feedback System (Superset)
    SUPERSET_URL = os.getenv('SUPERSET_URL', 'http://galaxy.iweaver.ai')
    SUPERSET_USER = os.getenv('SUPERSET_USER', 'admin')
    SUPERSET_PASS = os.getenv('SUPERSET_PASS', '')
    SUPERSET_DB_ID = int(os.getenv('SUPERSET_DB_ID', '1'))
    FEEDBACK_POLL_INTERVAL = int(os.getenv('FEEDBACK_POLL_INTERVAL', '60'))
    FEEDBACK_TIME_WINDOW_MINUTES = int(os.getenv('FEEDBACK_TIME_WINDOW_MINUTES', '10'))
    FEEDBACK_AUTO_REPLY_ENABLED = os.getenv('FEEDBACK_AUTO_REPLY_ENABLED', 'false').lower() == 'true'
    
    # Feishu Sheet (for feedback records)
    FEISHU_SHEET_APP_ID = os.getenv('FEISHU_SHEET_APP_ID', '')
    FEISHU_SHEET_APP_SECRET = os.getenv('FEISHU_SHEET_APP_SECRET', '')
    FEISHU_FEEDBACK_SHEET_TOKEN = os.getenv('FEISHU_FEEDBACK_SHEET_TOKEN', '')
    FEISHU_FEEDBACK_SHEET_ID = os.getenv('FEISHU_FEEDBACK_SHEET_ID', '')
