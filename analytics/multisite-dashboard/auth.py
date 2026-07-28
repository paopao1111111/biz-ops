"""Password verification and signed stateless sessions for the dashboard."""

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

AUTH_FILE = Path(
    os.getenv("AUTH_FILE", "/etc/multisite-weekly-dashboard-auth.json")
)
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", "28800"))
PBKDF2_ITERATIONS = 600_000


def load_auth_config():
    if not AUTH_FILE.exists():
        return {}
    return json.loads(AUTH_FILE.read_text(encoding="utf-8"))


def session_secret():
    value = os.getenv("SESSION_SECRET", "")
    if len(value) < 32:
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters")
    return value.encode("utf-8")


def validate_configuration():
    config = load_auth_config()
    required = {"username", "password_hash", "salt"}
    if not required.issubset(config):
        raise RuntimeError(f"Authentication file is missing required fields: {AUTH_FILE}")
    session_secret()


def hash_password(password, salt=None):
    if not password:
        raise ValueError("Password must not be empty")
    salt = salt or secrets.token_hex(32)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("ascii"),
        PBKDF2_ITERATIONS,
    )
    return digest.hex(), salt


def verify_password(password, stored_hash, salt):
    candidate, _ = hash_password(password, salt)
    return hmac.compare_digest(candidate, stored_hash)


def authenticate(username, password):
    config = load_auth_config()
    if not config or not hmac.compare_digest(
        str(username), str(config.get("username", ""))
    ):
        # Run a dummy hash so unknown usernames do not return much faster.
        hash_password(password or "invalid", "0" * 64)
        return False
    return verify_password(
        password,
        config.get("password_hash", ""),
        config.get("salt", ""),
    )


def create_session_token(username, now=None):
    issued_at = int(now if now is not None else time.time())
    payload = f"{issued_at}.{username}"
    signature = hmac.new(session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def validate_session_token(token, now=None):
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    issued_text, username, signature = parts
    try:
        issued_at = int(issued_text)
    except ValueError:
        return None
    current = int(now if now is not None else time.time())
    if issued_at > current + 60 or current - issued_at > SESSION_MAX_AGE:
        return None
    payload = f"{issued_text}.{username}"
    expected = hmac.new(session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    return username


def write_auth_config(path, username, password):
    password_hash, salt = hash_password(password)
    payload = {
        "username": username,
        "password_hash": password_hash,
        "salt": salt,
        "iterations": PBKDF2_ITERATIONS,
        "created_at": int(time.time()),
    }
    target = Path(path)
    target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    target.chmod(0o640)
    return target
