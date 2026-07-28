from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from dataclasses import dataclass

from .db import Database


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class AuthHeaders:
    timestamp: str
    nonce: str
    signature: str


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_message(timestamp: str, nonce: str, method: str, path: str, body: bytes) -> bytes:
    return "\n".join((timestamp, nonce, method.upper(), path, body_hash(body))).encode("utf-8")


def sign(secret: str, timestamp: str, nonce: str, method: str, path: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), canonical_message(timestamp, nonce, method, path, body), hashlib.sha256).hexdigest()


class HMACAuthenticator:
    def __init__(self, db: Database, secret: str, max_skew_seconds: int = 300, nonce_ttl_seconds: int = 600):
        self.db = db
        self.secret = secret
        self.max_skew_seconds = max_skew_seconds
        self.nonce_ttl_seconds = nonce_ttl_seconds

    def verify(self, headers: AuthHeaders, method: str, path: str, body: bytes, now: int | None = None) -> None:
        now = int(time.time()) if now is None else now
        try:
            timestamp = int(headers.timestamp)
        except (TypeError, ValueError) as exc:
            raise AuthenticationError("invalid timestamp") from exc
        if abs(now - timestamp) > self.max_skew_seconds:
            raise AuthenticationError("timestamp outside allowed window")
        if not headers.nonce or len(headers.nonce) > 200:
            raise AuthenticationError("invalid nonce")
        expected = sign(self.secret, headers.timestamp, headers.nonce, method, path, body)
        if not hmac.compare_digest(expected, headers.signature):
            raise AuthenticationError("invalid signature")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM used_nonces WHERE expires_at < ?", (now,))
            try:
                conn.execute(
                    "INSERT INTO used_nonces(nonce, used_at, expires_at) VALUES (?, ?, ?)",
                    (headers.nonce, now, now + self.nonce_ttl_seconds),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthenticationError("replayed nonce") from exc
