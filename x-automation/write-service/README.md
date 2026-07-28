# X Write Service

Isolated, internal-only service that executes **official X API** write operations for the standalone X console. It never touches AdsPower, browser cookies, or the Windows worker, and it never weakens the browse console's strict read-only guarantees.

Safety defaults: `global_write_paused=true` on every fresh database, every new account starts disabled and paused, and every operation requires an explicit human approval of the exact frozen request (content hash + version) before anything is sent.

## Guarantees

- No automatic engagement: no bulk targets, no auto-like/repost, no scheduled writes.
- Durable send marker: the executor persists `attempt_state=sending` before every mutating API call.
- No automatic retry after send: timeouts, crashes, or unparseable responses become `uncertain` / `manual_reconciliation_required` and wait for human reconciliation.
- Identity binding: writes only execute for an account whose immutable X user ID was verified through `/2/users/me`; an ID mismatch pauses the account.
- Transactional audit: every state change and its audit row commit in the same SQLite transaction.
- Loopback only: the listener is restricted to `127.0.0.1`/`::1` by config validation.

## Run

Python 3.11+, standard library only.

```sh
cp x_write.example.json x_write.json
# Set a random HMAC secret (>=32 bytes), the service-owned data paths,
# and the exact public HTTPS callback registered in the X Developer Portal.
python3.11 -m x_write --config x_write.json
```

Dynamic credentials live in the service-owned `secrets_path` (normally `/var/lib/x-write-service/credentials.json`, mode `0600`). The control panel writes this store atomically after OAuth verification; operators do not edit it over SSH. The database stores only credential references, and secret/token values never appear in API responses, audit rows, or logs. Legacy OAuth 1.0a JSON remains readable and is migrated to schema v2 on the first successful mutation.

## Account authorization

API keys or an OAuth client ID identify the developer app, not an X account. Every account needs user-context authorization:

1. Configure the X Developer App once in the 8790 panel. The callback must exactly match `oauth_callback_url` and use trusted HTTPS.
2. Select the matching Windows AdsPower Profile and generate a 10-minute PKCE link.
3. Open that link manually inside the selected Windows Profile, approve on X, then return to the panel.
4. The callback verifies `/2/users/me`, binds the immutable X user ID, and creates/updates the write account as **disabled and paused**.
5. Enable/resume only after separate human review. Authorization never resumes the global write pause.

Required OAuth 2.0 scopes are `tweet.read tweet.write users.read like.write offline.access`. OAuth 1.0a four-value entry remains available in the panel as a compatibility path and is verified before storage.

## Internal authentication

All `/api/*` requests (except unsigned `GET /health`) require:

- `X-Internal-Timestamp`: Unix seconds within `auth_max_skew_seconds`
- `X-Internal-Nonce`: unique nonce, persisted for replay rejection
- `X-Internal-Signature`: hex HMAC-SHA256 over `timestamp\nnonce\nmethod\npath\nsha256(body)`

## API

Global/accounts:

- `GET /api/status`, `GET /api/config`
- `POST /api/global/pause`, `POST /api/global/resume`
- `GET /api/accounts`, `POST /api/accounts`
- `PATCH /api/accounts/{id}/metadata`
- `POST /api/accounts/{id}/enable|disable|pause|resume`
- `POST /api/accounts/{id}/verify` (calls `/2/users/me`, binds immutable user ID)
- `GET|POST /api/accounts/{id}/quota`

Authorization and credentials:

- `GET /api/oauth/status`
- `POST /api/oauth/app` — one-way developer app configuration; secrets are never returned
- `POST /api/oauth/start`, `POST /api/oauth/callback`
- `GET /api/oauth/flows/{flow_key}`, `POST /api/oauth/flows/{flow_key}/cancel`
- `POST /api/credentials/oauth1` — verify first, then save
- `POST /api/credentials/{ref}/delete` — rejected while referenced
- `GET /api/credential-refs` — safe metadata only

Requests (all writes require the two-step create→submit→approve flow):

- `POST /api/requests` — types: `like`, `unlike`, `repost`, `unrepost`, `post_create`, `post_delete`, `reply`, `article_draft_publish` (`reply` and `post_create` accept optional `scheduled_at` for timed publishing)
- `GET /api/requests`, `GET /api/requests/{id}`
- `POST /api/requests/{id}/submit|approve|cancel` (approve requires `content_hash` + `request_version` of the frozen request)

Operations:

- `GET /api/operations`, `GET /api/operations/{id}`
- `POST /api/operations/{id}/approve-next-step` — second human approval, required before an X Article draft is published
- `POST /api/operations/{id}/reconcile` — for `uncertain` operations only; never resends

Audit: `GET /api/audit?limit=&target_type=&target_id=`

## Executor rules

1. Global pause, disabled account, paused account, expired approval, exceeded quota, or exceeded credit guard ⇒ no send.
2. OAuth 2.0 refresh and `/2/users/me` identity verification happen before the durable business send marker. `invalid_grant` or scope loss pauses the account and requires reconnection; transient token failures are known failures, never `uncertain` sends.
3. Per tick at most one operation; steps run in order.
4. `article_draft_publish` stops after draft creation until a second human approval.
5. Crash recovery: running ops without a send marker requeue; ops that crashed after the send marker become `uncertain`.

## Test

```sh
python3.11 -m unittest discover -s tests
```
