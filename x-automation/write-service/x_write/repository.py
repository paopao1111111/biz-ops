from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from typing import Any

from .db import Database
from .validation import PayloadError, content_hash, validate_payload


class NotFoundError(LookupError):
    pass


class StateError(ValueError):
    def __init__(self, message: str, code: str = "state_error"):
        super().__init__(message)
        self.code = code


def _now() -> int:
    return int(time.time())


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Repository:
    def __init__(self, db: Database):
        self.db = db

    # ---------- audit ----------

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        event_type: str,
        actor: str,
        target_type: str,
        target_id: str | None,
        detail: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO write_audit_log(event_type, actor, target_type, target_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (event_type, actor, target_type, target_id, _dump(detail), _now()),
        )

    def list_audit(self, limit: int = 100, target_type: str | None = None,
                   target_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM write_audit_log"
        args: list[Any] = []
        clauses = []
        if target_type:
            clauses.append("target_type=?")
            args.append(target_type)
        if target_id:
            clauses.append("target_id=?")
            args.append(target_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        conn = self.db.connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        return [self._audit_row(r) for r in rows]

    @staticmethod
    def _audit_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "event_type": row["event_type"], "actor": row["actor"],
            "target_type": row["target_type"], "target_id": row["target_id"],
            "detail": json.loads(row["detail_json"]), "created_at": row["created_at"],
        }

    # ---------- settings / status ----------

    def get_setting(self, key: str, default: Any = None) -> Any:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT value_json FROM write_settings WHERE setting_key=?", (key,)).fetchone()
        finally:
            conn.close()
        return json.loads(row[0]) if row else default

    def status(self) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            paused = json.loads(
                conn.execute("SELECT value_json FROM write_settings WHERE setting_key='global_write_paused'").fetchone()[0]
            )
            counts = conn.execute(
                "SELECT COUNT(*) total, COALESCE(SUM(enabled), 0) enabled, COALESCE(SUM(CASE WHEN paused=1 THEN 1 ELSE 0 END), 0) paused FROM write_accounts"
            ).fetchone()
            ops = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) n FROM write_operations GROUP BY status")}
            reqs = {row["status"]: row["n"] for row in conn.execute(
                "SELECT status, COUNT(*) n FROM write_requests GROUP BY status")}
        finally:
            conn.close()
        return {
            "global_write_paused": paused,
            "accounts_total": counts["total"],
            "accounts_enabled": counts["enabled"],
            "accounts_paused": counts["paused"],
            "operations_by_status": ops,
            "requests_by_status": reqs,
            "uncertain_operations": ops.get("uncertain", 0),
            "write_execution_available": not paused,
        }

    def set_global_pause(self, paused: bool, actor: str) -> None:
        with self.db.transaction() as conn:
            old = json.loads(
                conn.execute("SELECT value_json FROM write_settings WHERE setting_key='global_write_paused'").fetchone()[0]
            )
            conn.execute(
                "UPDATE write_settings SET value_json=?, updated_at=? WHERE setting_key='global_write_paused'",
                (json.dumps(paused), _now()),
            )
            self._audit(conn, "global.pause" if paused else "global.resume", actor, "settings", "global_write_paused", {"from": old, "to": paused})

    # ---------- accounts ----------

    def list_accounts(self) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute("SELECT * FROM write_accounts ORDER BY id").fetchall()
        finally:
            conn.close()
        return [self._account(row) for row in rows]

    def get_account(self, account_id: int) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise NotFoundError("account not found")
        return self._account(row)

    def create_account(
        self,
        account_key: str,
        display_name: str,
        actor: str,
        x_user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or {}
        now = _now()
        with self.db.transaction() as conn:
            try:
                credential_ref = str(metadata.get("credential_ref") or account_key)
                cursor = conn.execute(
                    """INSERT INTO write_accounts(
                           account_key, display_name, x_user_id, enabled, paused, metadata_json,
                           credential_ref, authorization_status, created_at, updated_at
                       ) VALUES (?, ?, ?, 0, 1, ?, ?, 'unconfigured', ?, ?)""",
                    (account_key, display_name, x_user_id, _dump(metadata), credential_ref, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError("account_key or X user id is already registered", "duplicate_account") from exc
            account_id = cursor.lastrowid
            self._audit(conn, "account.create", actor, "account", str(account_id), {"account_key": account_key})
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row)

    def update_account_metadata(
        self,
        account_id: int,
        actor: str,
        display_name: str | None = None,
        x_user_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        changes = {key: value for key, value in {"display_name": display_name, "x_user_id": x_user_id, "metadata": metadata}.items() if value is not None}
        if not changes:
            raise StateError("at least one metadata field is required")
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if old is None:
                raise NotFoundError("account not found")
            conn.execute(
                "UPDATE write_accounts SET display_name=?, x_user_id=?, metadata_json=?, updated_at=? WHERE id=?",
                (
                    display_name if display_name is not None else old["display_name"],
                    x_user_id if x_user_id is not None else old["x_user_id"],
                    _dump(metadata) if metadata is not None else old["metadata_json"],
                    _now(),
                    account_id,
                ),
            )
            self._audit(conn, "account.metadata.update", actor, "account", str(account_id), {"fields": sorted(changes)})
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row)

    def record_verification(self, account_id: int, actor: str, verified: dict[str, Any]) -> dict[str, Any]:
        mismatch = False
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if old is None:
                raise NotFoundError("account not found")
            expected = old["x_user_id"]
            if expected and expected != verified["id"]:
                # Pause and audit must commit before the caller-facing error is raised.
                mismatch = True
                conn.execute("UPDATE write_accounts SET paused=1, updated_at=? WHERE id=?", (_now(), account_id))
                self._audit(conn, "account.verify.identity_mismatch", actor, "account", str(account_id),
                            {"expected": expected, "actual": verified["id"]})
            else:
                metadata = json.loads(old["metadata_json"])
                metadata.update({
                    "verified_x_user_id": verified["id"],
                    "verified_username": verified.get("username", ""),
                    "verified_at": _now(),
                })
                if not expected:
                    conn.execute("UPDATE write_accounts SET x_user_id=? WHERE id=?", (verified["id"], account_id))
                conn.execute(
                    "UPDATE write_accounts SET metadata_json=?, authorization_status='verified', updated_at=? WHERE id=?",
                    (_dump(metadata), _now(), account_id),
                )
                self._audit(conn, "account.verify.succeeded", actor, "account", str(account_id),
                            {"x_user_id": verified["id"], "username": verified.get("username", "")})
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        if mismatch:
            raise StateError("verified X user id does not match the expected account", "identity_mismatch")
        return self._account(row)

    def record_verification_failure(self, account_id: int, actor: str, code: str) -> None:
        with self.db.transaction() as conn:
            self._audit(conn, "account.verify.failed", actor, "account", str(account_id), {"code": code})

    def invalidate_authorization(self, account_id: int, actor: str, code: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if old is None:
                raise NotFoundError("account not found")
            conn.execute(
                "UPDATE write_accounts SET paused=1, authorization_status='reconnect_required', updated_at=? WHERE id=?",
                (_now(), account_id),
            )
            self._audit(conn, "account.authorization.invalidated", actor, "account", str(account_id), {
                "code": str(code)[:120],
            })
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row)

    def set_account_enabled(self, account_id: int, enabled: bool, actor: str) -> dict[str, Any]:
        return self._set_account_flag(account_id, "enabled", enabled, actor)

    def set_account_paused(self, account_id: int, paused: bool, actor: str) -> dict[str, Any]:
        return self._set_account_flag(account_id, "paused", paused, actor)

    def _set_account_flag(self, account_id: int, field: str, value: bool, actor: str) -> dict[str, Any]:
        if field not in {"enabled", "paused"}:
            raise ValueError("invalid account state field")
        with self.db.transaction() as conn:
            old = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if old is None:
                raise NotFoundError("account not found")
            conn.execute(f"UPDATE write_accounts SET {field}=?, updated_at=? WHERE id=?", (int(value), _now(), account_id))
            event = f"account.{field}.{'on' if value else 'off'}"
            self._audit(conn, event, actor, "account", str(account_id), {"from": bool(old[field]), "to": value})
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
        return self._account(row)

    @staticmethod
    def _account(row: sqlite3.Row) -> dict[str, Any]:
        keys = set(row.keys())
        capabilities = json.loads(row["capabilities_json"]) if "capabilities_json" in keys else {}
        return {
            "id": row["id"],
            "account_key": row["account_key"],
            "display_name": row["display_name"],
            "x_user_id": row["x_user_id"],
            "enabled": bool(row["enabled"]),
            "paused": bool(row["paused"]),
            "credential_ref": row["credential_ref"] if "credential_ref" in keys else None,
            "auth_type": row["auth_type"] if "auth_type" in keys else None,
            "credential_generation": int(row["credential_generation"] or 0) if "credential_generation" in keys else 0,
            "authorization_status": row["authorization_status"] if "authorization_status" in keys else "unconfigured",
            "capabilities": capabilities,
            "source_profile_id": row["source_profile_id"] if "source_profile_id" in keys else None,
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ---------- credentials / OAuth authorization ----------

    @staticmethod
    def credential_capabilities(auth_type: str, scopes: list[str] | tuple[str, ...]) -> dict[str, Any]:
        scope_set = set(scopes)

        def capability(required: set[str]) -> dict[str, Any]:
            if auth_type == "oauth1":
                return {"allowed": True, "reason": None, "source": "oauth1_user_context"}
            missing = sorted(required - scope_set)
            return {
                "allowed": not missing,
                "reason": None if not missing else "missing_scope:" + ",".join(missing),
                "source": "oauth2_scopes",
            }

        return {
            "like": capability({"like.write", "users.read", "tweet.read"}),
            "unlike": capability({"like.write", "users.read", "tweet.read"}),
            "repost": capability({"tweet.write", "users.read", "tweet.read"}),
            "unrepost": capability({"tweet.write", "users.read", "tweet.read"}),
            "post_create": capability({"tweet.write", "users.read", "tweet.read"}),
            "post_delete": capability({"tweet.write", "users.read", "tweet.read"}),
            "reply": capability({"tweet.write", "users.read", "tweet.read"}),
            "media_upload": capability({"media.write", "tweet.write"}),
            "article_draft": {"allowed": False, "reason": "article_entitlement_unverified", "source": "runtime_probe"},
            "article_publish": {"allowed": False, "reason": "article_entitlement_unverified", "source": "runtime_probe"},
        }

    def bind_authorized_account(
        self,
        *,
        credential_ref: str,
        auth_type: str,
        generation: int,
        verified: dict[str, Any],
        scopes: list[str] | tuple[str, ...],
        actor: str,
        account_id: int | None = None,
        display_name: str | None = None,
        source_profile_id: str | None = None,
        source_label: str | None = None,
        expected_x_user_id: str | None = None,
    ) -> dict[str, Any]:
        if auth_type not in {"oauth1", "oauth2"}:
            raise StateError("authorization type is invalid", "invalid_auth_type")
        x_user_id = str(verified.get("id") or "")
        if not x_user_id:
            raise StateError("verified identity is missing an X user id", "identity_unverified")
        if expected_x_user_id and str(expected_x_user_id) != x_user_id:
            raise StateError("authorized X user does not match the expected identity", "identity_mismatch")
        mismatch = False
        mismatch_account_id: int | None = None
        now = _now()
        with self.db.transaction() as conn:
            by_identity = conn.execute("SELECT * FROM write_accounts WHERE x_user_id=?", (x_user_id,)).fetchone()
            target = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone() if account_id else None
            if account_id and target is None:
                raise NotFoundError("account not found")
            if target is None and by_identity is not None:
                target = by_identity
            if target is None and source_profile_id:
                target = conn.execute("SELECT * FROM write_accounts WHERE source_profile_id=?", (source_profile_id,)).fetchone()
            if target is not None and target["x_user_id"] and str(target["x_user_id"]) != x_user_id:
                mismatch = True
                mismatch_account_id = int(target["id"])
                conn.execute(
                    "UPDATE write_accounts SET paused=1, authorization_status='identity_mismatch', updated_at=? WHERE id=?",
                    (now, target["id"]),
                )
                self._audit(conn, "account.authorization.identity_mismatch", actor, "account", str(target["id"]), {
                    "expected": target["x_user_id"], "actual": x_user_id,
                })
            elif by_identity is not None and target is not None and by_identity["id"] != target["id"]:
                raise StateError("X user identity is already bound to another account", "duplicate_account")
            else:
                owner = conn.execute(
                    "SELECT id FROM write_accounts WHERE credential_ref=?" + (" AND id<>?" if target is not None else ""),
                    (credential_ref, target["id"]) if target is not None else (credential_ref,),
                ).fetchone()
                if owner is not None:
                    raise StateError("credential is already bound to another account", "credential_in_use")
                if source_profile_id:
                    owner = conn.execute(
                        "SELECT id FROM write_accounts WHERE source_profile_id=?" + (" AND id<>?" if target is not None else ""),
                        (source_profile_id, target["id"]) if target is not None else (source_profile_id,),
                    ).fetchone()
                    if owner is not None:
                        raise StateError("Windows profile is already bound to another write account", "profile_in_use")
                capabilities = self.credential_capabilities(auth_type, scopes)
                metadata = json.loads(target["metadata_json"]) if target is not None else {}
                metadata.update({
                    "verified_x_user_id": x_user_id,
                    "verified_username": str(verified.get("username") or ""),
                    "verified_at": now,
                })
                if source_label:
                    metadata["source_label"] = str(source_label)[:120]
                if target is None:
                    base_key = f"x-{x_user_id}"
                    account_key = base_key
                    if conn.execute("SELECT 1 FROM write_accounts WHERE account_key=?", (account_key,)).fetchone():
                        account_key = f"{base_key}-{uuid.uuid4().hex[:6]}"
                    cursor = conn.execute(
                        """INSERT INTO write_accounts(
                               account_key, display_name, x_user_id, enabled, paused, metadata_json,
                               credential_ref, auth_type, credential_generation, authorization_status,
                               capabilities_json, source_profile_id, created_at, updated_at
                           ) VALUES (?, ?, ?, 0, 1, ?, ?, ?, ?, 'verified', ?, ?, ?, ?)""",
                        (
                            account_key,
                            (display_name or str(verified.get("username") or account_key))[:120],
                            x_user_id,
                            _dump(metadata),
                            credential_ref,
                            auth_type,
                            int(generation),
                            _dump(capabilities),
                            source_profile_id,
                            now,
                            now,
                        ),
                    )
                    target_id = int(cursor.lastrowid)
                    event = "account.authorization.create"
                else:
                    target_id = int(target["id"])
                    conn.execute(
                        """UPDATE write_accounts SET
                               display_name=?, x_user_id=?, paused=1, metadata_json=?, credential_ref=?,
                               auth_type=?, credential_generation=?, authorization_status='verified',
                               capabilities_json=?, source_profile_id=COALESCE(?, source_profile_id), updated_at=?
                           WHERE id=?""",
                        (
                            (display_name or target["display_name"])[:120],
                            x_user_id,
                            _dump(metadata),
                            credential_ref,
                            auth_type,
                            int(generation),
                            _dump(capabilities),
                            source_profile_id,
                            now,
                            target_id,
                        ),
                    )
                    event = "account.authorization.bind"
                self._audit(conn, event, actor, "account", str(target_id), {
                    "credential_ref": credential_ref,
                    "auth_type": auth_type,
                    "generation": int(generation),
                    "x_user_id": x_user_id,
                    "username": str(verified.get("username") or ""),
                    "source_profile_id": source_profile_id,
                })
                row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (target_id,)).fetchone()
        if mismatch:
            raise StateError(
                f"authorized X user does not match account {mismatch_account_id}",
                "identity_mismatch",
            )
        return self._account(row)

    def credential_usage(self, credential_ref: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            accounts = conn.execute(
                "SELECT id, display_name, enabled, paused FROM write_accounts WHERE credential_ref=? OR (credential_ref IS NULL AND account_key=?) ORDER BY id",
                (credential_ref, credential_ref),
            ).fetchall()
            active = conn.execute(
                """SELECT COUNT(*) n FROM write_operations o
                   JOIN write_accounts a ON a.id=o.account_id
                   WHERE (a.credential_ref=? OR (a.credential_ref IS NULL AND a.account_key=?))
                     AND o.status IN ('queued','running','awaiting_approval','uncertain')""",
                (credential_ref, credential_ref),
            ).fetchone()["n"]
        finally:
            conn.close()
        return {
            "accounts": [{"id": r["id"], "display_name": r["display_name"],
                          "enabled": bool(r["enabled"]), "paused": bool(r["paused"])} for r in accounts],
            "active_operations": int(active),
        }

    def audit_credential_event(self, event_type: str, actor: str, credential_ref: str,
                               detail: dict[str, Any]) -> None:
        safe = {k: v for k, v in detail.items() if k not in {
            "consumer_key", "consumer_secret", "client_secret", "access_token",
            "access_token_secret", "refresh_token", "code", "code_verifier",
        }}
        with self.db.transaction() as conn:
            self._audit(conn, event_type, actor, "credential", credential_ref, safe)

    def create_oauth_flow(
        self,
        *,
        flow_key: str,
        state_hash: str,
        code_verifier: str,
        developer_app: str,
        requested_scopes: list[str],
        redirect_uri: str,
        actor: str,
        ttl_seconds: int,
        account_id: int | None = None,
        source_profile_id: str | None = None,
        source_label: str | None = None,
        display_name: str | None = None,
        expected_x_user_id: str | None = None,
    ) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE oauth_authorization_flows SET status='expired', code_verifier='', updated_at=? WHERE status='pending' AND expires_at<?",
                (now, now),
            )
            if account_id is not None and conn.execute(
                    "SELECT 1 FROM write_accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise NotFoundError("account not found")
            conn.execute(
                """INSERT INTO oauth_authorization_flows(
                       flow_key, state_hash, code_verifier, developer_app, requested_scopes_json,
                       redirect_uri, account_id, source_profile_id, source_label, display_name,
                       expected_x_user_id, status, actor, created_at, expires_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    flow_key, state_hash, code_verifier, developer_app, _dump(requested_scopes),
                    redirect_uri, account_id, source_profile_id, source_label, display_name,
                    expected_x_user_id, actor, now, now + ttl_seconds, now,
                ),
            )
            self._audit(conn, "oauth.flow.create", actor, "oauth_flow", flow_key, {
                "developer_app": developer_app,
                "account_id": account_id,
                "source_profile_id": source_profile_id,
                "expires_at": now + ttl_seconds,
                "scopes": requested_scopes,
            })
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE flow_key=?", (flow_key,)).fetchone()
        return self._oauth_flow_public(row)

    def get_oauth_flow(self, flow_key: str) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE flow_key=?", (flow_key,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise NotFoundError("OAuth flow not found")
        return self._oauth_flow_public(row)

    def claim_oauth_flow(self, state: str) -> dict[str, Any]:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        now = _now()
        expired = False
        claimed: sqlite3.Row | None = None
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE state_hash=?", (state_hash,)).fetchone()
            if row is None:
                raise StateError("OAuth state is invalid", "oauth_state_invalid")
            if row["status"] != "pending":
                raise StateError("OAuth state has already been used", "oauth_state_replayed")
            if row["expires_at"] < now:
                conn.execute(
                    "UPDATE oauth_authorization_flows SET status='expired', code_verifier='', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                expired = True
            else:
                conn.execute(
                    "UPDATE oauth_authorization_flows SET status='processing', claimed_at=?, updated_at=? WHERE id=? AND status='pending'",
                    (now, now, row["id"]),
                )
                if conn.execute("SELECT changes()").fetchone()[0] != 1:
                    raise StateError("OAuth state has already been used", "oauth_state_replayed")
                self._audit(conn, "oauth.flow.claim", "oauth-callback", "oauth_flow", row["flow_key"], {})
                claimed = conn.execute("SELECT * FROM oauth_authorization_flows WHERE id=?", (row["id"],)).fetchone()
        if expired:
            raise StateError("OAuth state has expired", "oauth_state_expired")
        if claimed is None:
            raise StateError("OAuth state could not be claimed", "oauth_flow_state")
        return self._oauth_flow_internal(claimed)

    def complete_oauth_flow(self, flow_key: str, *, credential_ref: str,
                            account_id: int, result: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE flow_key=?", (flow_key,)).fetchone()
            if row is None:
                raise NotFoundError("OAuth flow not found")
            if row["status"] != "processing":
                raise StateError("OAuth flow is not processing", "oauth_flow_state")
            conn.execute(
                """UPDATE oauth_authorization_flows SET status='succeeded', credential_ref=?,
                   result_json=?, code_verifier='', consumed_at=?, updated_at=? WHERE id=?""",
                (credential_ref, _dump(result), now, now, row["id"]),
            )
            conn.execute("UPDATE oauth_authorization_flows SET account_id=? WHERE id=?", (account_id, row["id"]))
            self._audit(conn, "oauth.flow.succeeded", row["actor"], "oauth_flow", flow_key, {
                "credential_ref": credential_ref, "account_id": account_id,
                "x_user_id": result.get("x_user_id"), "username": result.get("username"),
            })
            updated = conn.execute("SELECT * FROM oauth_authorization_flows WHERE id=?", (row["id"],)).fetchone()
        return self._oauth_flow_public(updated)

    def fail_oauth_flow(self, flow_key: str, code: str) -> None:
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE flow_key=?", (flow_key,)).fetchone()
            if row is None:
                return
            if row["status"] in {"succeeded", "failed", "cancelled", "expired"}:
                return
            conn.execute(
                "UPDATE oauth_authorization_flows SET status='failed', error_code=?, code_verifier='', consumed_at=?, updated_at=? WHERE id=?",
                (code, now, now, row["id"]),
            )
            self._audit(conn, "oauth.flow.failed", "oauth-callback", "oauth_flow", flow_key, {"code": code})

    def cancel_oauth_flow(self, flow_key: str, actor: str) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM oauth_authorization_flows WHERE flow_key=?", (flow_key,)).fetchone()
            if row is None:
                raise NotFoundError("OAuth flow not found")
            if row["status"] not in {"pending"}:
                raise StateError("only a pending OAuth flow can be cancelled", "oauth_flow_state")
            conn.execute(
                "UPDATE oauth_authorization_flows SET status='cancelled', code_verifier='', consumed_at=?, updated_at=? WHERE id=?",
                (now, now, row["id"]),
            )
            self._audit(conn, "oauth.flow.cancel", actor, "oauth_flow", flow_key, {})
            updated = conn.execute("SELECT * FROM oauth_authorization_flows WHERE id=?", (row["id"],)).fetchone()
        return self._oauth_flow_public(updated)

    @staticmethod
    def _oauth_flow_public(row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        return {
            "flow_key": row["flow_key"],
            "developer_app": row["developer_app"],
            "requested_scopes": json.loads(row["requested_scopes_json"]),
            "account_id": row["account_id"],
            "source_profile_id": row["source_profile_id"],
            "source_label": row["source_label"],
            "display_name": row["display_name"],
            "status": row["status"],
            "error_code": row["error_code"],
            "credential_ref": row["credential_ref"],
            "result": result,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "consumed_at": row["consumed_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _oauth_flow_internal(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = cls._oauth_flow_public(row)
        result.update({
            "code_verifier": row["code_verifier"],
            "redirect_uri": row["redirect_uri"],
            "expected_x_user_id": row["expected_x_user_id"],
            "actor": row["actor"],
        })
        return result

    def pre_send_block_reason(self, account_id: int, credential_ref: str,
                              generation: int, operation_type: str | None = None) -> str | None:
        conn = self.db.connect()
        try:
            paused = json.loads(conn.execute(
                "SELECT value_json FROM write_settings WHERE setting_key='global_write_paused'"
            ).fetchone()[0])
            if paused:
                return "global_write_paused"
            row = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if row is None:
                return "account_missing"
            if not row["enabled"]:
                return "account_disabled"
            if row["paused"]:
                return "account_paused"
            expected_ref = row["credential_ref"] or row["account_key"]
            if expected_ref != credential_ref:
                return "credential_changed"
            expected_generation = int(row["credential_generation"] or 0)
            if expected_generation and expected_generation != int(generation):
                return "credential_changed"
            if row["authorization_status"] not in {"verified", "unconfigured"}:
                return "authorization_invalid"
            capabilities = json.loads(row["capabilities_json"] or "{}")
            if capabilities and operation_type:
                capability_name = {
                    "article_draft_publish": "article_draft",
                }.get(operation_type, operation_type)
                capability = capabilities.get(capability_name)
                if isinstance(capability, dict) and not capability.get("allowed"):
                    return str(capability.get("reason") or "capability_missing")
            return None
        finally:
            conn.close()

    # ---------- quota policies ----------

    def upsert_quota_policy(self, account_id: int, operation_type: str, window_seconds: int,
                            max_operations: int, actor: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM write_accounts WHERE id=?", (account_id,)).fetchone() is None:
                raise NotFoundError("account not found")
            conn.execute(
                """INSERT INTO quota_policies(account_id, operation_type, window_seconds, max_operations, enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(account_id, operation_type, window_seconds)
                   DO UPDATE SET max_operations=excluded.max_operations, enabled=1, updated_at=excluded.updated_at""",
                (account_id, operation_type, window_seconds, max_operations, _now(), _now()),
            )
            self._audit(conn, "quota.upsert", actor, "account", str(account_id),
                        {"operation_type": operation_type, "window_seconds": window_seconds, "max_operations": max_operations})
            row = conn.execute(
                "SELECT * FROM quota_policies WHERE account_id=? AND operation_type=? AND window_seconds=?",
                (account_id, operation_type, window_seconds)).fetchone()
        return dict(row)

    def list_quota_policies(self, account_id: int) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM quota_policies WHERE account_id=? ORDER BY operation_type, window_seconds",
                (account_id,)).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def quota_block_reason(self, conn: sqlite3.Connection, account_id: int, operation_type: str) -> str | None:
        now = _now()
        for policy in conn.execute(
                "SELECT * FROM quota_policies WHERE account_id=? AND operation_type=? AND enabled=1",
                (account_id, operation_type)):
            window_start = now - int(policy["window_seconds"])
            used = conn.execute(
                """SELECT COUNT(*) n FROM write_operations
                   WHERE account_id=? AND operation_type=? AND created_at>=? AND status NOT IN ('cancelled')""",
                (account_id, operation_type, window_start)).fetchone()["n"]
            if used >= int(policy["max_operations"]):
                return f"quota_exceeded:{operation_type}:{policy['window_seconds']}s"
        guard = self.get_setting("credit_guard", {}) or {}
        daily_limit = int(guard.get("daily_limit") or 0)
        costs = guard.get("costs") or {}
        if daily_limit > 0:
            day_start = now - 86400
            spent = 0
            for row in conn.execute(
                    "SELECT operation_type, COUNT(*) n FROM write_operations WHERE created_at>=? AND status NOT IN ('cancelled') GROUP BY operation_type",
                    (day_start,)):
                spent += int(costs.get(row["operation_type"], 0)) * row["n"]
            if spent >= daily_limit:
                return "credit_guard_exceeded"
        return None

    # ---------- requests ----------

    # ---------- media assets ----------

    @staticmethod
    def _media_asset(row: sqlite3.Row) -> dict[str, Any]:
        keys = row.keys()
        return {
            "id": row["id"],
            "asset_key": row["asset_key"],
            "account_id": row["account_id"] if "account_id" in keys else None,
            "sha256": row["sha256"],
            "mime_type": row["mime_type"],
            "byte_size": int(row["byte_size"] or 0),
            "local_path": row["local_path"] if "local_path" in keys else None,
            "status": row["status"],
            "x_media_id": row["x_media_id"] if "x_media_id" in keys and row["x_media_id"] is not None else None,
            "uploaded_at": int(row["uploaded_at"]) if "uploaded_at" in keys and row["uploaded_at"] is not None else None,
            "metadata": json.loads(row["metadata_json"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def register_media_asset(self, *, asset_key: str | None, account_id: int | None,
                             sha256: str, mime_type: str, byte_size: int,
                             local_path: str | None = None, metadata: dict | None = None,
                             actor: str = "system") -> dict[str, Any]:
        if not sha256 or not mime_type:
            raise StateError("media asset requires sha256 and mime_type", "invalid_media")
        key = asset_key or uuid.uuid4().hex
        now = _now()
        with self.db.transaction() as conn:
            if account_id is not None:
                acc = conn.execute("SELECT 1 FROM write_accounts WHERE id=?", (account_id,)).fetchone()
                if acc is None:
                    raise StateError("account not found for media asset", "account_not_found")
            try:
                cursor = conn.execute(
                    """INSERT INTO media_assets(asset_key, account_id, sha256, mime_type, byte_size, local_path, status, metadata_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
                    (key, account_id, sha256, mime_type, int(byte_size), local_path,
                     _dump(metadata or {}), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError("media asset key already exists", "duplicate_media_asset") from exc
            asset_id = cursor.lastrowid
            self._audit(conn, "media.register", actor, "media_asset", str(asset_id),
                        {"account_id": account_id, "sha256": sha256, "byte_size": int(byte_size)})
            row = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        return self._media_asset(row)

    def set_media_local_bytes(self, asset_id: int, *, sha256: str, byte_size: int,
                              local_path: str | None = None) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE media_assets SET sha256=?, byte_size=?, local_path=COALESCE(?, local_path), updated_at=? WHERE id=?",
                (sha256, int(byte_size), local_path, now, asset_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("media asset not found")
            row = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        return self._media_asset(row)

    def mark_media_ready(self, asset_id: int, *, x_media_id: str,
                         metadata: dict | None = None, actor: str = "system") -> dict[str, Any]:
        now = _now()
        meta_json = _dump(metadata or {}) if metadata else "{}"
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE media_assets SET status='ready', x_media_id=?, uploaded_at=?, metadata_json=?, updated_at=? WHERE id=?",
                (x_media_id, now, meta_json, now, asset_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("media asset not found")
            self._audit(conn, "media.ready", actor, "media_asset", str(asset_id),
                        {"x_media_id": x_media_id})
            row = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        return self._media_asset(row)

    def mark_media_failed(self, asset_id: int, *, reason: str, actor: str = "system") -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE media_assets SET status='failed', updated_at=? WHERE id=?",
                (now, asset_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("media asset not found")
            self._audit(conn, "media.failed", actor, "media_asset", str(asset_id),
                        {"reason": str(reason)[:200]})
            row = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
        return self._media_asset(row)

    def get_media_asset(self, asset_id: int) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM media_assets WHERE id=?", (asset_id,)).fetchone()
            if row is None:
                raise NotFoundError("media asset not found")
            return self._media_asset(row)
        finally:
            conn.close()

    def list_media_assets(self, *, account_id: int | None = None,
                          status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM media_assets"
        args: list[Any] = []
        clauses: list[str] = []
        if account_id is not None:
            clauses.append("account_id=?")
            args.append(account_id)
        if status:
            clauses.append("status=?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT 200"
        conn = self.db.connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        return [self._media_asset(r) for r in rows]

    def media_assets_for_ids(self, ids: list[int], *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if not ids:
            return []
        own = conn is not None
        if not own:
            conn = self.db.connect()
        try:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM media_assets WHERE id IN ({placeholders})", [int(i) for i in ids]
            ).fetchall()
            return [self._media_asset(r) for r in rows]
        finally:
            if not own:
                conn.close()

    def _check_media_assets(self, conn: sqlite3.Connection, account_id: int, media_ids: list[int]) -> None:
        """Validate media assets at request-create time: owned by the account,
        in a sendable state, and not duplicated. The X media_id is uploaded at
        send time by the executor, so 'pending' (registered, awaiting upload)
        is the expected state here — 'ready' is also accepted for re-use."""
        rows = self.media_assets_for_ids(media_ids, conn=conn)
        if len(rows) != len(media_ids):
            raise StateError("one or more media assets were not found", "media_not_owned")
        seen: set[int] = set()
        for row in rows:
            if row["account_id"] is not None and int(row["account_id"]) != int(account_id):
                raise StateError("media asset does not belong to this account", "media_account_mismatch")
            if row["status"] not in {"pending", "ready"}:
                raise StateError("media asset is not in a sendable state", "media_not_ready")
            if row["id"] in seen:
                raise StateError("media asset ids must be unique", "invalid_media")
            seen.add(row["id"])

    # ---------- write requests ----------

    def create_request(self, account_id: int, request_type: str, payload: Any, actor: str) -> dict[str, Any]:
        try:
            clean = validate_payload(request_type, payload)
        except PayloadError as exc:
            raise StateError(str(exc), exc.code) from exc
        now = _now()
        request_key = uuid.uuid4().hex
        with self.db.transaction() as conn:
            account = conn.execute("SELECT * FROM write_accounts WHERE id=?", (account_id,)).fetchone()
            if account is None:
                raise NotFoundError("account not found")
            if request_type == "post_create" and clean.get("media_asset_ids"):
                self._check_media_assets(conn, account_id, clean["media_asset_ids"])
            cursor = conn.execute(
                """INSERT INTO write_requests(request_key, account_id, request_type, payload_json, content_hash, status, version, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'draft', 1, ?, ?, ?)""",
                (request_key, account_id, request_type, _dump(clean), content_hash(clean), actor, now, now),
            )
            request_id = cursor.lastrowid
            self._audit(conn, "request.create", actor, "request", str(request_id),
                        {"request_type": request_type, "account_id": account_id})
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
        return self._request(row)

    def get_request(self, request_id: int) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            result = self._request(row)
            ops = conn.execute(
                "SELECT * FROM write_operations WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
            result["operations"] = [self._operation(o) for o in ops]
        finally:
            conn.close()
        return result

    def list_requests(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM write_requests"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        conn = self.db.connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        return [self._request(r) for r in rows]

    def submit_request(self, request_id: int, actor: str) -> dict[str, Any]:
        return self._transition_request(request_id, {"draft"}, "pending_approval", actor, "request.submit")

    def cancel_request(self, request_id: int, actor: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            if row["status"] not in ("draft", "pending_approval", "approved", "queued"):
                raise StateError(f"cannot cancel a request in status {row['status']}")
            conn.execute(
                "UPDATE write_operations SET status='cancelled', updated_at=? WHERE request_id=? AND status='queued'",
                (_now(), request_id))
            conn.execute("UPDATE write_requests SET status='cancelled', updated_at=? WHERE id=?", (_now(), request_id))
            self._audit(conn, "request.cancel", actor, "request", str(request_id), {"from": row["status"]})
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
        return self._request(row)

    def _transition_request(self, request_id: int, allowed: set[str], to: str,
                            actor: str, event: str) -> dict[str, Any]:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            if row["status"] not in allowed:
                raise StateError(f"cannot move request from {row['status']} to {to}")
            conn.execute("UPDATE write_requests SET status=?, updated_at=? WHERE id=?", (to, _now(), request_id))
            self._audit(conn, event, actor, "request", str(request_id), {"from": row["status"], "to": to})
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
        return self._request(row)

    @staticmethod
    def _request(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "request_key": row["request_key"], "account_id": row["account_id"],
            "request_type": row["request_type"], "payload": json.loads(row["payload_json"]),
            "content_hash": row["content_hash"], "status": row["status"], "version": row["version"],
            "created_by": row["created_by"], "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # ---------- approvals ----------

    def approve_request(self, request_id: int, actor: str, confirmation: dict[str, Any],
                        approval_ttl_seconds: int = 3600) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM write_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise NotFoundError("request not found")
            if row["status"] != "pending_approval":
                raise StateError(f"cannot approve a request in status {row['status']}")
            if confirmation.get("content_hash") != row["content_hash"]:
                raise StateError("confirmation content hash does not match the frozen request", "approval_hash_mismatch")
            if confirmation.get("request_version") != row["version"]:
                raise StateError("confirmation version does not match the frozen request", "approval_version_mismatch")
            conn.execute(
                """INSERT INTO write_approvals(request_id, request_version, content_hash, decision, actor, reason, confirmation_json, expires_at, created_at)
                   VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                (request_id, row["version"], row["content_hash"], actor,
                 confirmation.get("reason", ""), _dump(confirmation), now + approval_ttl_seconds, now),
            )
            operation = self._create_operation(conn, row)
            # A scheduled operation must remain approved until its scheduled release time; extend the
            # approval window so approval_expired_for_operation does not cancel it before it fires.
            scheduled_at = operation.get("scheduled_at")
            if scheduled_at and scheduled_at > now + approval_ttl_seconds:
                conn.execute("UPDATE write_approvals SET expires_at=? WHERE request_id=? AND request_version=?",
                             (scheduled_at + approval_ttl_seconds, request_id, row["version"]))
            conn.execute("UPDATE write_requests SET status='queued', updated_at=? WHERE id=?", (now, request_id))
            self._audit(conn, "request.approve", actor, "request", str(request_id),
                        {"operation_id": operation["id"], "expires_at": now + approval_ttl_seconds,
                         "scheduled_at": scheduled_at})
        return self.get_request(request_id)

    def _create_operation(self, conn: sqlite3.Connection, request_row: sqlite3.Row) -> dict[str, Any]:
        now = _now()
        request_type = request_row["request_type"]
        operation_key = uuid.uuid4().hex
        idem = hashlib.sha256(
            f"{request_row['account_id']}|{request_type}|{request_row['request_key']}|v{request_row['version']}".encode("utf-8")
        ).hexdigest()
        cursor = conn.execute(
            """INSERT INTO write_operations(operation_key, request_id, account_id, operation_type, status, idempotency_key, scheduled_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (operation_key, request_row["id"], request_row["account_id"], request_type, idem,
             json.loads(request_row["payload_json"]).get("scheduled_at"), now, now),
        )
        operation_id = cursor.lastrowid
        if request_type == "article_draft_publish":
            steps = ["article_create_draft", "article_publish"]
        elif request_type == "post_create":
            payload = json.loads(request_row["payload_json"])
            steps = ["media_upload", "post_create"] if payload.get("media_asset_ids") else ["post_create"]
        else:
            steps = [request_type]
        for order, name in enumerate(steps, start=1):
            conn.execute(
                "INSERT INTO write_operation_steps(operation_id, step_name, step_order, status, created_at, updated_at) VALUES (?, ?, ?, 'pending', ?, ?)",
                (operation_id, name, order, now, now),
            )
        self._audit(conn, "operation.create", "system", "operation", str(operation_id),
                    {"request_id": request_row["id"], "operation_type": request_type})
        return self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone())

    def list_approvals(self, request_id: int) -> list[dict[str, Any]]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM write_approvals WHERE request_id=? ORDER BY id", (request_id,)).fetchall()
        finally:
            conn.close()
        return [dict(r) | {"confirmation": json.loads(r["confirmation_json"])} for r in rows]

    # ---------- operations ----------

    def get_operation(self, operation_id: int) -> dict[str, Any]:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise NotFoundError("operation not found")
            result = self._operation(row)
            steps = conn.execute(
                "SELECT * FROM write_operation_steps WHERE operation_id=? ORDER BY step_order",
                (operation_id,)).fetchall()
            result["steps"] = [self._step(s) for s in steps]
            req = conn.execute("SELECT * FROM write_requests WHERE id=?", (row["request_id"],)).fetchone()
            result["request"] = self._request(req)
        finally:
            conn.close()
        return result

    def list_operations(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(500, int(limit)))
        sql = "SELECT * FROM write_operations"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        conn = self.db.connect()
        try:
            rows = conn.execute(sql, args).fetchall()
        finally:
            conn.close()
        return [self._operation(r) for r in rows]

    @staticmethod
    def _operation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "operation_key": row["operation_key"], "request_id": row["request_id"],
            "account_id": row["account_id"], "operation_type": row["operation_type"],
            "status": row["status"], "attempt_state": row["attempt_state"],
            "current_step": row["current_step"], "external_object_id": row["external_object_id"],
            "error_code": row["error_code"], "error_message": row["error_message"],
            "retry_after": row["retry_after"],
            "attempt_started_at": row["attempt_started_at"], "attempt_finished_at": row["attempt_finished_at"],
            "reconciliation_note": row["reconciliation_note"], "reconciled_by": row["reconciled_by"],
            "reconciled_at": row["reconciled_at"],
            "scheduled_at": row["scheduled_at"] if "scheduled_at" in row.keys() else None,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    @staticmethod
    def _step(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"], "operation_id": row["operation_id"], "step_name": row["step_name"],
            "step_order": row["step_order"], "status": row["status"], "attempt_state": row["attempt_state"],
            "external_object_id": row["external_object_id"], "detail": json.loads(row["detail_json"]),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    # ---------- executor-facing transitions ----------

    def claim_next_operation(self, lease_seconds: int = 300) -> dict[str, Any] | None:
        """Claims the oldest queued operation for an enabled, unpaused account.

        Global pause is honored here so a paused service never starts sends.
        """
        now = _now()
        with self.db.transaction() as conn:
            if json.loads(conn.execute(
                    "SELECT value_json FROM write_settings WHERE setting_key='global_write_paused'").fetchone()[0]):
                return None
            row = conn.execute(
                """SELECT o.* FROM write_operations o
                   JOIN write_accounts a ON a.id = o.account_id
                   WHERE o.status='queued' AND a.enabled=1 AND a.paused=0
                     AND (o.scheduled_at IS NULL OR o.scheduled_at <= ?)
                   ORDER BY o.id LIMIT 1""", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_hex(16)
            conn.execute(
                "UPDATE write_operations SET status='running', lease_token=?, lease_expires_at=?, updated_at=? WHERE id=?",
                (token, now + lease_seconds, now, row["id"]))
            conn.execute("UPDATE write_requests SET status='executing', updated_at=? WHERE id=? AND status='queued'",
                         (now, row["request_id"]))
            self._audit(conn, "operation.claim", "executor", "operation", str(row["id"]),
                        {"lease_expires_at": now + lease_seconds})
            claimed = self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (row["id"],)).fetchone())
            claimed["lease_token"] = token
            return claimed

    def release_operation(self, operation_id: int, lease_token: str, reason: str) -> None:
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if row is None or row["lease_token"] != lease_token:
                raise StateError("operation lease does not match")
            conn.execute(
                "UPDATE write_operations SET status='queued', lease_token=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?",
                (_now(), operation_id))
            conn.execute("UPDATE write_requests SET status='queued', updated_at=? WHERE id=? AND status='executing'",
                         (_now(), row["request_id"]))
            self._audit(conn, "operation.release", "executor", "operation", str(operation_id), {"reason": reason})

    def mark_step_sending(self, operation_id: int, step_order: int) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                """UPDATE write_operation_steps SET status='running', attempt_state='sending', updated_at=?
                   WHERE operation_id=? AND step_order=?""",
                (now, operation_id, step_order))
            conn.execute(
                """UPDATE write_operations SET attempt_state='sending', current_step=?, attempt_started_at=?, updated_at=?
                   WHERE id=?""",
                (step_order, now, now, operation_id))
            self._audit(conn, "operation.step.sending", "executor", "operation", str(operation_id),
                        {"step_order": step_order})
            return self._step(conn.execute(
                "SELECT * FROM write_operation_steps WHERE operation_id=? AND step_order=?",
                (operation_id, step_order)).fetchone())

    def complete_step_success(self, operation_id: int, step_order: int, external_object_id: str | None,
                              response: dict[str, Any], *, await_next_approval: bool) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise NotFoundError("operation not found")
            conn.execute(
                """UPDATE write_operation_steps SET status='succeeded', attempt_state='confirmed', external_object_id=?,
                   detail_json=?, updated_at=? WHERE operation_id=? AND step_order=?""",
                (external_object_id, _dump(response), now, operation_id, step_order))
            total = conn.execute(
                "SELECT COUNT(*) n FROM write_operation_steps WHERE operation_id=?", (operation_id,)).fetchone()["n"]
            if await_next_approval:
                conn.execute(
                    """UPDATE write_operations SET status='awaiting_approval', attempt_state='confirmed',
                       external_object_id=?, response_json=?, updated_at=? WHERE id=?""",
                    (external_object_id, _dump(response), now, operation_id))
                new_status = "awaiting_approval"
            elif step_order >= total:
                conn.execute(
                    """UPDATE write_operations SET status='succeeded', attempt_state='confirmed',
                       external_object_id=?, response_json=?, attempt_finished_at=?, updated_at=? WHERE id=?""",
                    (external_object_id, _dump(response), now, now, operation_id))
                conn.execute("UPDATE write_requests SET status='succeeded', updated_at=? WHERE id=?",
                             (now, op["request_id"]))
                new_status = "succeeded"
            else:
                conn.execute(
                    "UPDATE write_operations SET attempt_state='not_started', updated_at=? WHERE id=?",
                    (now, operation_id))
                new_status = "running"
            self._audit(conn, "operation.step.succeeded", "executor", "operation", str(operation_id),
                        {"step_order": step_order, "external_object_id": external_object_id, "operation_status": new_status})
            return self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone())

    def complete_step_failure(self, operation_id: int, step_order: int, code: str, message: str,
                              retry_after: int | None, *, uncertain: bool) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise NotFoundError("operation not found")
            if uncertain:
                conn.execute(
                    """UPDATE write_operation_steps SET status='uncertain', attempt_state='uncertain', updated_at=?
                       WHERE operation_id=? AND step_order=?""",
                    (now, operation_id, step_order))
                conn.execute(
                    """UPDATE write_operations SET status='uncertain', attempt_state='uncertain', error_code=?, error_message=?,
                       attempt_finished_at=?, updated_at=? WHERE id=?""",
                    (code, message[:300], now, now, operation_id))
                conn.execute("UPDATE write_requests SET status='manual_reconciliation_required', updated_at=? WHERE id=?",
                             (now, op["request_id"]))
                event = "operation.uncertain"
            else:
                conn.execute(
                    """UPDATE write_operation_steps SET status='failed', attempt_state='confirmed', updated_at=?
                       WHERE operation_id=? AND step_order=?""",
                    (now, operation_id, step_order))
                conn.execute(
                    """UPDATE write_operations SET status='failed_known', attempt_state='confirmed', error_code=?, error_message=?,
                       retry_after=?, attempt_finished_at=?, updated_at=? WHERE id=?""",
                    (code, message[:300], retry_after, now, now, operation_id))
                conn.execute("UPDATE write_requests SET status='failed', updated_at=? WHERE id=?",
                             (now, op["request_id"]))
                event = "operation.failed"
            self._audit(conn, event, "executor", "operation", str(operation_id),
                        {"step_order": step_order, "code": code, "uncertain": uncertain, "retry_after": retry_after})
            return self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone())

    def approve_next_step(self, operation_id: int, actor: str, confirmation: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise NotFoundError("operation not found")
            if op["status"] != "awaiting_approval":
                raise StateError(f"operation is not waiting for approval (status {op['status']})")
            req = conn.execute("SELECT * FROM write_requests WHERE id=?", (op["request_id"],)).fetchone()
            if confirmation.get("content_hash") != req["content_hash"]:
                raise StateError("confirmation content hash does not match the frozen request", "approval_hash_mismatch")
            conn.execute(
                """INSERT INTO write_approvals(request_id, request_version, content_hash, decision, actor, reason, confirmation_json, expires_at, created_at)
                   VALUES (?, ?, ?, 'approved', ?, ?, ?, ?, ?)""",
                (op["request_id"], req["version"], req["content_hash"], actor,
                 confirmation.get("reason", "step-approval"), _dump(confirmation), now + 3600, now))
            conn.execute(
                """UPDATE write_operations SET status='queued', attempt_state='not_started', lease_token=NULL,
                   lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (now, operation_id))
            self._audit(conn, "operation.step.approve", actor, "operation", str(operation_id),
                        {"resume_step": op["current_step"] + 1})
            return self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone())

    def approval_expired_for_operation(self, operation_id: int) -> bool:
        conn = self.db.connect()
        try:
            row = conn.execute(
                """SELECT ap.expires_at FROM write_approvals ap
                   JOIN write_operations o ON o.request_id = ap.request_id
                   WHERE o.id=? ORDER BY ap.id DESC LIMIT 1""",
                (operation_id,)).fetchone()
        finally:
            conn.close()
        return bool(row and row["expires_at"] and row["expires_at"] < _now())

    def cancel_operation_for_expired_approval(self, operation_id: int) -> None:
        now = _now()
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if op is None or op["status"] not in ("queued", "running"):
                return
            conn.execute(
                """UPDATE write_operations SET status='cancelled', error_code='approval_expired',
                   lease_token=NULL, lease_expires_at=NULL, updated_at=? WHERE id=?""",
                (now, operation_id))
            conn.execute("UPDATE write_requests SET status='pending_approval', updated_at=? WHERE id=?",
                         (now, op["request_id"]))
            self._audit(conn, "operation.cancel.approval_expired", "executor", "operation", str(operation_id), {})

    def reconcile_operation(self, operation_id: int, outcome: str, note: str, actor: str) -> dict[str, Any]:
        if outcome not in {"succeeded", "failed"}:
            raise StateError("outcome must be 'succeeded' or 'failed'")
        if not note or not note.strip():
            raise StateError("a reconciliation note is required")
        now = _now()
        with self.db.transaction() as conn:
            op = conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise NotFoundError("operation not found")
            if op["status"] != "uncertain":
                raise StateError(f"only uncertain operations can be reconciled (status {op['status']})")
            new_status = "reconciled_succeeded" if outcome == "succeeded" else "reconciled_failed"
            conn.execute(
                """UPDATE write_operations SET status=?, reconciliation_note=?, reconciled_by=?, reconciled_at=?, updated_at=?
                   WHERE id=?""",
                (new_status, note.strip()[:500], actor, now, now, operation_id))
            conn.execute(
                "UPDATE write_requests SET status=?, updated_at=? WHERE id=?",
                ("succeeded" if outcome == "succeeded" else "failed", now, op["request_id"]))
            self._audit(conn, "operation.reconcile", actor, "operation", str(operation_id),
                        {"outcome": outcome, "note": note.strip()[:200]})
            return self._operation(conn.execute("SELECT * FROM write_operations WHERE id=?", (operation_id,)).fetchone())

    def recover_running_operations(self) -> int:
        """Crash recovery: stale running ops without a send marker requeue;
        ops that crashed after the durable send marker become uncertain."""
        now = _now()
        recovered = 0
        with self.db.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM write_operations WHERE status='running' AND (lease_expires_at IS NULL OR lease_expires_at<?)",
                (now,)).fetchall()
            for op in rows:
                if op["attempt_state"] == "sending":
                    conn.execute(
                        """UPDATE write_operations SET status='uncertain', attempt_state='uncertain', error_code='service_restarted',
                           error_message='service restarted while a send was in flight', updated_at=? WHERE id=?""",
                        (now, op["id"]))
                    conn.execute(
                        "UPDATE write_requests SET status='manual_reconciliation_required', updated_at=? WHERE id=?",
                        (now, op["request_id"]))
                    self._audit(conn, "operation.recover.uncertain", "executor", "operation", str(op["id"]),
                                {"reason": "service_restarted_after_send_marker"})
                else:
                    conn.execute(
                        """UPDATE write_operations SET status='queued', lease_token=NULL, lease_expires_at=NULL,
                           attempt_state='not_started', updated_at=? WHERE id=?""",
                        (now, op["id"]))
                    self._audit(conn, "operation.recover.requeue", "executor", "operation", str(op["id"]), {})
                recovered += 1
        return recovered
