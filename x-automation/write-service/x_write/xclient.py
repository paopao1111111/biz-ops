from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets as _secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from .credentials import (
    AccountCredentials,
    OAuth2AppCredentials,
    OAuth2Credentials,
    ResolvedCredentials,
)


class XAPIError(RuntimeError):
    """Safe X API error: never contains credentials or request headers."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 code: str = "api_error", retry_after: int | None = None,
                 outcome_uncertain: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after
        self.outcome_uncertain = outcome_uncertain


@dataclass(frozen=True)
class OAuth2TokenResponse:
    access_token: str
    refresh_token: str
    expires_at: int
    scopes: tuple[str, ...]
    token_type: str = "bearer"


def _pct(value: str) -> str:
    return urllib.parse.quote(str(value), safe="~-._")


def _oauth1_header(creds: AccountCredentials, method: str, url: str) -> str:
    split = urllib.parse.urlsplit(url)
    base_url = f"{split.scheme}://{split.netloc}{split.path}"
    params: dict[str, str] = {
        "oauth_consumer_key": creds.consumer_key,
        "oauth_nonce": _secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": creds.access_token,
        "oauth_version": "1.0",
    }
    for key, values in urllib.parse.parse_qsl(split.query, keep_blank_values=True):
        params.setdefault(key, values)
    encoded = sorted((_pct(k), _pct(v)) for k, v in params.items())
    param_string = "&".join(f"{k}={v}" for k, v in encoded)
    base_string = "&".join((method.upper(), _pct(base_url), _pct(param_string)))
    signing_key = f"{_pct(creds.consumer_secret)}&{_pct(creds.access_token_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    header_params = {k: v for k, v in params.items() if k.startswith("oauth_")}
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(header_params.items()))


def _parse_retry_after(headers: Any) -> int | None:
    reset = headers.get("x-rate-limit-reset")
    if reset:
        try:
            return int(reset)
        except (TypeError, ValueError):
            pass
    retry = headers.get("retry-after")
    if retry:
        try:
            return int(time.time()) + max(0, int(retry))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(retry)
                return int(parsed.timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
    return None


def _safe_error(body: bytes, fallback: str) -> str:
    try:
        payload = json.loads(body.decode("utf-8", "replace"))
        if isinstance(payload, dict):
            value = payload.get("detail") or payload.get("title") or payload.get("error_description")
            if value:
                return str(value)[:300]
            errors = payload.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                return str(errors[0].get("message") or fallback)[:300]
    except (ValueError, TypeError):
        pass
    return fallback


def _opener(proxy_url: str | None) -> urllib.request.OpenerDirector:
    handlers: list[Any] = []
    if proxy_url:
        proxy = urllib.parse.urlsplit(proxy_url)
        if proxy.scheme not in {"http", "https"} or not proxy.netloc:
            raise XAPIError("X API proxy must be a valid HTTP(S) URL", code="invalid_proxy")
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    return urllib.request.build_opener(*handlers)


def build_oauth2_authorization_url(*, authorize_url: str, app: OAuth2AppCredentials,
                                   state: str, code_challenge: str,
                                   scopes: list[str] | tuple[str, ...]) -> str:
    parsed = urllib.parse.urlsplit(authorize_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise XAPIError("OAuth authorization endpoint must use HTTPS", code="invalid_oauth_endpoint")
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": app.client_id,
        "redirect_uri": app.redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def exchange_oauth2_code(*, app: OAuth2AppCredentials, code: str, code_verifier: str,
                         token_url: str, proxy_url: str | None = None,
                         timeout_seconds: float = 20.0,
                         require_https: bool = True) -> OAuth2TokenResponse:
    return _oauth2_token_request(
        app=app,
        token_url=token_url,
        form={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": app.redirect_uri,
            "code_verifier": code_verifier,
        },
        current_refresh_token="",
        current_scopes=(),
        proxy_url=proxy_url,
        timeout_seconds=timeout_seconds,
        require_https=require_https,
    )


def _oauth2_token_request(*, app: OAuth2AppCredentials, token_url: str,
                          form: dict[str, str], current_refresh_token: str,
                          current_scopes: tuple[str, ...], proxy_url: str | None,
                          timeout_seconds: float, require_https: bool) -> OAuth2TokenResponse:
    parsed = urllib.parse.urlsplit(token_url)
    allowed = {"https"} if require_https else {"https", "http"}
    if parsed.scheme not in allowed or not parsed.netloc or parsed.username or parsed.password:
        raise XAPIError("OAuth token endpoint must use HTTPS", code="invalid_oauth_endpoint")
    payload = dict(form)
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    if app.client_secret:
        basic = base64.b64encode(f"{app.client_id}:{app.client_secret}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {basic}"
    else:
        payload["client_id"] = app.client_id
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(token_url, data=data, headers=headers, method="POST")
    try:
        response = _opener(proxy_url).open(request, timeout=max(1.0, float(timeout_seconds)))
        status = response.getcode()
        response_headers = response.headers
        body = response.read(1024 * 1024)
    except urllib.error.HTTPError as exc:
        status = exc.code
        response_headers = exc.headers or {}
        body = exc.read(1024 * 1024) if exc.fp else b""
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
        raise XAPIError("OAuth token endpoint connection failed", code="token_refresh_transient") from exc
    if status == 429:
        raise XAPIError("OAuth token endpoint rate limit reached", status_code=429,
                        code="token_refresh_transient", retry_after=_parse_retry_after(response_headers))
    if status >= 400:
        error_code = "token_refresh_reauth_required" if status in {400, 401} else "token_refresh_transient"
        raise XAPIError(_safe_error(body, "OAuth token request failed"), status_code=status, code=error_code)
    try:
        raw = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise XAPIError("OAuth token endpoint returned invalid JSON", code="token_refresh_invalid") from exc
    if not isinstance(raw, dict):
        raise XAPIError("OAuth token endpoint returned an invalid response", code="token_refresh_invalid")
    access_token = str(raw.get("access_token") or "")
    refresh_token = str(raw.get("refresh_token") or current_refresh_token)
    try:
        expires_in = int(raw.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    token_type = str(raw.get("token_type") or "bearer").lower()
    scope_value = raw.get("scope")
    if isinstance(scope_value, str):
        scopes = tuple(sorted({item for item in scope_value.split() if item}))
    elif isinstance(raw.get("scopes"), list):
        scopes = tuple(sorted({str(item) for item in raw["scopes"] if str(item)}))
    else:
        scopes = tuple(current_scopes)
    if not access_token or not refresh_token or expires_in <= 0 or token_type != "bearer" or not scopes:
        raise XAPIError("OAuth token response is incomplete", code="token_refresh_invalid")
    return OAuth2TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=int(time.time()) + expires_in,
        scopes=scopes,
        token_type=token_type,
    )


class XAPIClient:
    """Per-account official X API client using OAuth user context.

    Never share instances across accounts. OAuth2 clients may refresh before a
    business send; the executor calls ``prepare_for_request`` before persisting
    the business send marker.
    """

    _refresh_locks_guard = threading.Lock()
    _refresh_locks: dict[str, threading.Lock] = {}

    def __init__(self, credentials: ResolvedCredentials, *, base_url: str = "https://api.x.com",
                 proxy_url: str | None = None, timeout_seconds: float = 20.0,
                 require_https: bool = True, token_url: str = "https://api.x.com/2/oauth2/token",
                 refresh_leeway_seconds: int = 300,
                 credential_reloader: Callable[[str], ResolvedCredentials] | None = None,
                 token_persister: Callable[..., OAuth2Credentials] | None = None,
                 media_upload_url: str = "https://upload.x.com") -> None:
        parsed = urllib.parse.urlsplit(base_url)
        allowed_schemes = ("https",) if require_https else ("https", "http")
        if parsed.scheme not in allowed_schemes or not parsed.netloc or parsed.username or parsed.password:
            raise XAPIError("X API base must be a credential-free HTTPS URL", code="invalid_base_url")
        media_parsed = urllib.parse.urlsplit(media_upload_url)
        if media_parsed.scheme not in allowed_schemes or not media_parsed.netloc or media_parsed.username or media_parsed.password:
            raise XAPIError("X media upload endpoint must be a credential-free HTTPS URL", code="invalid_base_url")
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.media_upload_url = media_upload_url.rstrip("/")
        self.proxy_url = proxy_url
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.require_https = require_https
        self.token_url = token_url
        self.refresh_leeway_seconds = max(30, int(refresh_leeway_seconds))
        self.credential_reloader = credential_reloader
        self.token_persister = token_persister
        self._opener = _opener(proxy_url)

    @property
    def credential_ref(self) -> str:
        return str(getattr(self.credentials, "credential_ref", "") or "")

    @property
    def credential_generation(self) -> int:
        return max(1, int(getattr(self.credentials, "generation", 1) or 1))

    @property
    def auth_type(self) -> str:
        return str(getattr(self.credentials, "auth_type", "oauth1"))

    def prepare_for_request(self) -> None:
        if not isinstance(self.credentials, OAuth2Credentials):
            return
        if self.credentials.expires_at > int(time.time()) + self.refresh_leeway_seconds:
            return
        ref = self.credentials.credential_ref
        if not ref or self.token_persister is None:
            raise XAPIError("OAuth 2.0 token requires reconnection", code="token_refresh_reauth_required")
        lock = self._refresh_lock(ref)
        with lock:
            if self.credential_reloader is not None:
                latest = self.credential_reloader(ref)
                if not isinstance(latest, OAuth2Credentials):
                    raise XAPIError("credential authorization type changed", code="credential_changed")
                self.credentials = latest
            if self.credentials.expires_at > int(time.time()) + self.refresh_leeway_seconds:
                return
            app = OAuth2AppCredentials(
                name=self.credentials.developer_app,
                client_id=self.credentials.client_id,
                client_secret=self.credentials.client_secret,
                redirect_uri="",
                generation=self.credentials.generation,
            )
            token = _oauth2_token_request(
                app=app,
                token_url=self.token_url,
                form={
                    "grant_type": "refresh_token",
                    "refresh_token": self.credentials.refresh_token,
                },
                current_refresh_token=self.credentials.refresh_token,
                current_scopes=self.credentials.scopes,
                proxy_url=self.proxy_url,
                timeout_seconds=self.timeout_seconds,
                require_https=self.require_https,
            )
            missing_scopes = sorted(set(self.credentials.scopes) - set(token.scopes))
            if missing_scopes:
                raise XAPIError(
                    "refreshed OAuth token is missing required scopes",
                    code="token_scope_insufficient",
                )
            try:
                self.credentials = self.token_persister(
                    ref,
                    expected_generation=self.credentials.generation,
                    access_token=token.access_token,
                    refresh_token=token.refresh_token,
                    expires_at=token.expires_at,
                    scopes=token.scopes,
                )
            except Exception as exc:
                code = getattr(exc, "code", "token_refresh_persist_failed")
                raise XAPIError("refreshed OAuth token could not be persisted", code=code) from exc

    @classmethod
    def _refresh_lock(cls, ref: str) -> threading.Lock:
        with cls._refresh_locks_guard:
            return cls._refresh_locks.setdefault(ref, threading.Lock())

    def _authorization_header(self, method: str, url: str) -> str:
        if isinstance(self.credentials, OAuth2Credentials):
            return f"Bearer {self.credentials.access_token}"
        return _oauth1_header(self.credentials, method, url)

    def _request(self, method: str, path: str, *, json_body: dict | None = None,
                 write: bool = False) -> dict:
        url = self.base_url + path
        data = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = urllib.request.Request(url, data=data, method=method.upper())
        request.add_header("Authorization", self._authorization_header(method, url))
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "x-write-service/0.2")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
            status = response.getcode()
            headers = response.headers
            body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = exc.headers or {}
            body = exc.read(2 * 1024 * 1024) if exc.fp else b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise XAPIError("X API connection failed", code="connection_error",
                            outcome_uncertain=write) from exc
        if status == 429:
            raise XAPIError("X API rate limit reached", status_code=429, code="rate_limited",
                            retry_after=_parse_retry_after(headers))
        if status >= 400:
            uncertain = write and status >= 500
            raise XAPIError(_safe_error(body, f"X API returned HTTP {status}"),
                            status_code=status, code="http_error", outcome_uncertain=uncertain)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise XAPIError("X API returned invalid JSON", status_code=status,
                            code="invalid_response", outcome_uncertain=write) from exc
        if not isinstance(payload, dict):
            raise XAPIError("X API returned an invalid response shape", status_code=status,
                            code="invalid_response", outcome_uncertain=write)
        return payload

    @staticmethod
    def _data(payload: dict, context: str) -> dict:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise XAPIError(f"X {context} response is missing data", code="invalid_response",
                            outcome_uncertain=True)
        return data

    def _request_upload(self, path: str, *, body_bytes: bytes, content_type: str,
                        write: bool = True) -> dict:
        """Raw-bytes POST to the media upload host (upload.x.com).

        Mirrors ``_request``'s error contract: connection errors and 5xx on
        write operations are marked outcome-uncertain so the executor routes
        them to manual reconciliation instead of a silent retry. The OAuth2
        bearer header is host-agnostic; OAuth1 signs only query params, so a
        multipart body and a different host are both signature-safe.
        """
        url = self.media_upload_url + path
        request = urllib.request.Request(url, data=body_bytes, method="POST")
        request.add_header("Authorization", self._authorization_header("POST", url))
        request.add_header("Content-Type", content_type)
        request.add_header("User-Agent", "x-write-service/0.2")
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
            status = response.getcode()
            headers = response.headers
            body = response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = exc.headers or {}
            body = exc.read(2 * 1024 * 1024) if exc.fp else b""
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise XAPIError("X media upload connection failed", code="connection_error",
                            outcome_uncertain=write) from exc
        if status == 429:
            raise XAPIError("X media upload rate limit reached", status_code=429,
                            code="rate_limited", retry_after=_parse_retry_after(headers))
        if status >= 400:
            uncertain = write and status >= 500
            raise XAPIError(_safe_error(body, f"X media upload returned HTTP {status}"),
                            status_code=status, code="http_error", outcome_uncertain=uncertain)
        if not body:
            return {}
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise XAPIError("X media upload returned invalid JSON", status_code=status,
                            code="invalid_response", outcome_uncertain=write) from exc
        if not isinstance(payload, dict):
            raise XAPIError("X media upload returned an invalid response shape", status_code=status,
                            code="invalid_response", outcome_uncertain=write)
        return payload

    def verify_account(self) -> dict:
        data = self._data(self._request("GET", "/2/users/me"), "verify")
        if not data.get("id"):
            raise XAPIError("X verify response is missing user id", code="invalid_response")
        return {"id": str(data["id"]), "username": str(data.get("username") or ""),
                "name": str(data.get("name") or "")}

    def like_post(self, user_id: str, tweet_id: str) -> dict:
        payload = self._request("POST", f"/2/users/{user_id}/likes",
                                json_body={"tweet_id": tweet_id}, write=True)
        data = self._data(payload, "like")
        return {"liked": bool(data.get("liked", True))}

    def unlike_post(self, user_id: str, tweet_id: str) -> dict:
        payload = self._request("DELETE", f"/2/users/{user_id}/likes/{tweet_id}", write=True)
        data = self._data(payload, "unlike")
        return {"liked": bool(data.get("liked", False))}

    def repost_post(self, user_id: str, tweet_id: str) -> dict:
        payload = self._request("POST", f"/2/users/{user_id}/retweets",
                                json_body={"tweet_id": tweet_id}, write=True)
        data = self._data(payload, "repost")
        return {"retweeted": bool(data.get("retweeted", True))}

    def unrepost_post(self, user_id: str, tweet_id: str) -> dict:
        payload = self._request("DELETE", f"/2/users/{user_id}/retweets/{tweet_id}", write=True)
        data = self._data(payload, "unrepost")
        return {"retweeted": bool(data.get("retweeted", False))}

    def create_post(self, text: str, media_ids: list[str] | None = None) -> dict:
        body: dict[str, Any] = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": [str(mid) for mid in media_ids]}
        payload = self._request("POST", "/2/tweets", json_body=body, write=True)
        data = self._data(payload, "create-post")
        if not data.get("id"):
            raise XAPIError("X create-post response did not include a post id",
                            code="invalid_response", outcome_uncertain=True)
        return {"id": str(data["id"]), "text": str(data.get("text") or "")}

    def upload_media(self, media_bytes: bytes, *, mime_type: str,
                     media_category: str = "tweet_image") -> dict:
        """Upload image bytes to X and return ``{"media_id", "expires_at"}``.

        Uses the simple (non-chunked) ``POST /2/media/upload`` path for images
        under 5MB, which covers operator-provided post images. The media id is
        short-lived (~24h); the executor calls this at send time (after the
        operation is claimed), so expiry is not a concern for scheduled posts.
        """
        if not media_bytes:
            raise XAPIError("media upload requires non-empty bytes", code="invalid_media")
        boundary = "----xwriteservice" + _secrets.token_hex(8)
        parts: list[bytes] = []
        for name, value in (("media_category", media_category),):
            parts.append(f"--{boundary}\r\n".encode("ascii"))
            parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"))
            parts.append(value.encode("utf-8") + b"\r\n")
        parts.append(f"--{boundary}\r\n".encode("ascii"))
        parts.append(
            'Content-Disposition: form-data; name="media"; filename="image"\r\n'
            f"Content-Type: {mime_type}\r\n\r\n".encode("ascii")
        )
        parts.append(media_bytes)
        parts.append(b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode("ascii"))
        body = b"".join(parts)
        content_type = f"multipart/form-data; boundary={boundary}"
        payload = self._request_upload("/2/media/upload", body_bytes=body,
                                       content_type=content_type, write=True)
        media_id = payload.get("media_id_string") or payload.get("media_id")
        if not media_id:
            raise XAPIError("X media upload response did not include a media id",
                            code="invalid_response", outcome_uncertain=True)
        return {"media_id": str(media_id), "expires_at": int(time.time()) + 86400}

    def reply_post(self, text: str, tweet_id: str) -> dict:
        payload = self._request(
            "POST", "/2/tweets",
            json_body={"text": text, "reply": {"in_reply_to_tweet_id": str(tweet_id)}}, write=True)
        data = self._data(payload, "reply")
        if not data.get("id"):
            raise XAPIError("X reply response did not include a post id",
                            code="invalid_response", outcome_uncertain=True)
        return {"id": str(data["id"]), "text": str(data.get("text") or "")}

    def delete_post(self, tweet_id: str) -> dict:
        payload = self._request("DELETE", f"/2/tweets/{tweet_id}", write=True)
        data = self._data(payload, "delete-post")
        return {"deleted": bool(data.get("deleted", True))}

    def create_article_draft(self, article: dict) -> dict:
        # Article capability remains gated until the current official request
        # schema and entitlement are confirmed by a canary account.
        payload = self._request("POST", "/2/articles/draft", json_body=article, write=True)
        data = self._data(payload, "article-draft")
        article_id = data.get("article_id") or data.get("id")
        if not article_id:
            raise XAPIError("X article draft response did not include an article id",
                            code="invalid_response", outcome_uncertain=True)
        return {"article_id": str(article_id)}

    def publish_article(self, article_id: str) -> dict:
        payload = self._request("POST", f"/2/articles/{article_id}/publish", write=True)
        data = self._data(payload, "article-publish")
        post_id = data.get("post_id") or data.get("tweet_id")
        return {"article_id": str(article_id), "post_id": str(post_id) if post_id else None}
