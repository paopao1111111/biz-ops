"""Gmail API client with proxy support"""
import json
import base64
import os
import re
import html as html_lib
from email.mime.text import MIMEText
from google.oauth2 import service_account
from googleapiclient.discovery import build
import google.auth.transport.requests
import httplib2
import google_auth_httplib2
from lib.config import Config

SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

class GmailClient:
    def __init__(self):
        self.service = self._build_service()
    
    def _build_service(self):
        creds = service_account.Credentials.from_service_account_file(
            Config.GMAIL_SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        delegated = creds.with_subject(Config.GMAIL_SENDER)
        
        # Setup proxy for google-auth
        proxy = os.getenv('HTTPS_PROXY') or os.getenv('https_proxy') or ''
        if proxy:
            # google-auth uses requests library, which respects env vars
            os.environ['HTTP_PROXY'] = proxy
            os.environ['HTTPS_PROXY'] = proxy
        
        # googleapiclient uses httplib2. Give it an explicit timeout and
        # disable discovery cache to avoid slow/stale cache behavior. httplib2
        # keeps proxy_info_from_environment by default, so HTTPS_PROXY/HTTP_PROXY
        # from the service environment still route Gmail traffic through xray.
        http = httplib2.Http(timeout=Config.GMAIL_HTTP_TIMEOUT)
        authed_http = google_auth_httplib2.AuthorizedHttp(delegated, http=http)
        return build('gmail', 'v1', http=authed_http, cache_discovery=False)
    
    def poll_messages(self, max_results=10):
        """Poll for new messages"""
        results = self.service.users().messages().list(
            userId='me', maxResults=max_results, q='is:unread').execute()
        messages = results.get('messages', [])
        
        emails = []
        for msg in messages:
            full_msg = self.service.users().messages().get(
                userId='me', id=msg['id'], format='full').execute()
            emails.append(self._parse_message(full_msg))
        return emails
    
    def _parse_message(self, msg):
        headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
        snippet = html_lib.unescape(msg.get('snippet', '') or '')
        body = self._extract_body(msg['payload']) or snippet
        return {
            'message_id': msg['id'],
            'thread_id': msg.get('threadId', ''),
            'from': headers.get('From', ''),
            'to': headers.get('To', ''),
            'subject': headers.get('Subject', ''),
            'date': headers.get('Date', ''),
            'headers': headers,
            'body': body,
            'snippet': snippet
        }

    def _decode_part_body(self, payload):
        data = (payload.get('body') or {}).get('data') or ''
        if not data:
            return ''
        padding = '=' * (-len(data) % 4)
        return base64.urlsafe_b64decode((data + padding).encode('utf-8')).decode('utf-8', 'replace')

    def _find_mime_body(self, payload, mime_prefix):
        if payload.get('mimeType', '').startswith(mime_prefix):
            text = self._decode_part_body(payload)
            if text.strip():
                return text
        for part in payload.get('parts', []) or []:
            text = self._find_mime_body(part, mime_prefix)
            if text.strip():
                return text
        return ''

    def _html_to_text(self, html):
        html = html or ''
        html = re.sub(r'(?is)<(script|style).*?>.*?</\1>', ' ', html)
        html = re.sub(r'(?i)<\s*br\s*/?>', '\n', html)
        html = re.sub(r'(?i)</\s*(p|div|li|tr|h[1-6])\s*>', '\n', html)
        html = re.sub(r'(?s)<[^>]+>', ' ', html)
        text = html_lib.unescape(html)
        text = text.replace('\xa0', ' ')
        text = re.sub(r'[ \t]+', ' ', text)
        text = re.sub(r'\n[ \t]+', '\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _extract_body(self, payload):
        plain = self._find_mime_body(payload, 'text/plain')
        if plain.strip():
            return plain.strip()
        html = self._find_mime_body(payload, 'text/html')
        if html.strip():
            return self._html_to_text(html)
        return ''

    def _build_reply_message(self, thread_id, to, subject, body_html, in_reply_to=None):
        """Build a Gmail API message payload for a reply/draft."""
        message = MIMEText(body_html, 'html')
        message['to'] = to
        message['from'] = Config.GMAIL_SENDER
        message['subject'] = subject
        if in_reply_to:
            message['In-Reply-To'] = in_reply_to
            message['References'] = in_reply_to

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        body = {'raw': raw}
        if thread_id:
            body['threadId'] = thread_id
        return body

    def send_reply(self, thread_id, to, subject, body_html, in_reply_to=None):
        """Send reply email"""
        body = self._build_reply_message(thread_id, to, subject, body_html, in_reply_to)
        return self.service.users().messages().send(userId='me', body=body).execute()

    def create_reply_draft(self, thread_id, to, subject, body_html, in_reply_to=None):
        """Create a Gmail reply draft without sending it."""
        message = self._build_reply_message(thread_id, to, subject, body_html, in_reply_to)
        return self.service.users().drafts().create(userId='me', body={'message': message}).execute()

    def mark_read(self, message_id):
        """Mark message as read"""
        return self.service.users().messages().modify(
            userId='me', id=message_id, body={'removeLabelIds': ['UNREAD']}).execute()
