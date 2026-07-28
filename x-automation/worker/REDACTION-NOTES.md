# Redaction Notes

## Live source

- Host: `10.0.2.123`
- Live source path: `C:\x-browse-console-worker`
- Scheduled task exported read-only: `X Browse Console Worker`

## Included

- Worker source code
- Configuration template only (`worker.example.json`)
- Install, start, and uninstall scripts
- Existing README/requirements guidance
- Sanitized scheduled-task XML export

## Excluded or redacted

- `worker.json` and all live configuration values
- Worker/controller secrets, API keys, credentials, and SSH keys
- Logs, including `logs/worker.log`
- Virtual environments and dependency caches
- `__pycache__`, `.pyc`, and other bytecode/cache files
- Browser and AdsPower profile data
- Cookies, sessions, browser caches, and authentication state
- Generated/runtime state and temporary files
- Historical source backup files (`worker.py.backup-*`)
- Live host-specific worker ID in the configuration template
- Scheduled-task author/user identity and arguments

No Windows service or scheduled task was stopped, started, changed, registered, or unregistered while creating this copy. Only read-only file retrieval and scheduled-task XML query/export commands were used.
