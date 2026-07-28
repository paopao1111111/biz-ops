# X Browse Console Windows Worker

Independent read-only worker for panel API v1.3.0. It uses Python 3.11, Selenium, and optional `psutil` for evidence-bound cleanup; it does not import from `C:\acc-rpa`.

## Install

1. Copy this directory to the Windows host.
2. Run PowerShell as the intended current `mint` user:
   `powershell -ExecutionPolicy Bypass -File .\install-worker.ps1`
3. Edit `C:\x-browse-console-worker\worker.json`; replace placeholders with the controller URL, worker ID, shared worker secret, and optional AdsPower API key.
4. Start from Task Scheduler or rerun installation with `-StartNow` after configuration.

The task is named `X Browse Console Worker`, runs at current-user logon after 17 seconds, is hidden, and has its own restart loop. Logs are under `C:\x-browse-console-worker\logs`.

## Safety and runtime behavior

- Only `browse` and `probe` jobs with `read_only: true` are accepted. Action, interaction, publishing, like, reply, repost, and follow fields are rejected.
- The worker requires Flower/Firefox and attaches with AdsPower's returned geckodriver, `--connect-existing`, and `marionette_port`. It refuses Chrome/SunBrowser fallback.
- One parent Supervisor owns all controller and AdsPower traffic, admission locks, execution tokens, watchdogs, cleanup, and completion. It supervises up to three Windows-safe `spawn` child processes (`max_concurrent_jobs`, clamped to 1–3); each child only attaches WebDriver, performs read-only browsing, emits IPC progress/results, and detaches geckodriver.
- Heartbeats advertise protocol 2.0 capabilities and execution-level token, account/profile/proxy, child PID, forward-progress, and cleanup/reconciliation state. Controller cancel, stop-and-cleanup, quarantine, and forget directives are enforced by the parent.
- AdsPower API calls share one parent-owned client lock/rate limiter, use Bearer authentication when configured, and are spaced by at least 1.1 seconds.
- The worker queries AdsPower's active/status endpoint when supported. A confirmed active profile is treated as manual use and is never attached, stopped, or restarted. If active state is unknown, it attempts one start; a response without `marionette_port` is treated conservatively as manual use. Only a profile successfully launched by this worker is stopped during cleanup.
- `probe` uses at most 300 seconds and performs no browsing dwell budget. It starts the profile, reads the browser exit IP from `api.ipify.org`, checks X login/restriction state, and reads the profile-nav handle where safely available.
- `browse` is capped by `reserved_seconds` and 3600 seconds. It uses X latest-search pages and X Explore trending search links only. Missing trending DOM produces an explicit partial/failed result, never a home-feed substitute.
- Progress refresh is configured to 20–25 seconds. `no_forward_progress_seconds` ignores lease-only traffic; hard runtime is the browse reservation plus `hard_runtime_grace_seconds`, or fixed `probe_hard_runtime_seconds`. Escalation is cooperative cancel, terminate, kill, then parent-owned cleanup.
- Cleanup first uses AdsPower stop-and-confirm. If needed and `psutil` is installed, fallback cleanup only targets process trees matching launch time, returned ports, WebDriver executable/command evidence, and child ancestry; it never kills globally by process name or kills AdsPower itself. Uncertain cleanup remains quarantined and keeps profile/proxy locks.
- A durable reconciliation journal (default `state/reconciliation.json`, configurable with `reconciliation_journal`) is atomically replaced after ownership, launch, cleanup, result, reporting, and completion changes. Restart recovery retries cleanup only with saved ownership evidence; uncertain entries stay quarantined and locked. Journal records never contain the worker secret and are deleted only after controller `forget`.
- Completion retries are bounded per attempt and retained as heartbeat-visible reconciliation records until the controller acknowledges `forget`. Cleanup-confirmed records release profile/proxy capacity but remain heartbeat-visible; cleanup-uncertain records retain locks. Shutdown returns pending reporting IDs and never reports clean while acknowledgements remain outstanding.
- Timeout fields are finite and bounded: no-forward-progress 30–900s, browse grace 5–600s, probe runtime 60–900s, cooperative cancellation 1–60s, terminate grace 1–30s, and cleanup timeout 10–180s.

## Uninstall

`powershell -ExecutionPolicy Bypass -File C:\x-browse-console-worker\uninstall-worker.ps1`

Use `-PreserveData` to move the worker tree to a timestamped archive. Uninstall touches only the new task and independent worker processes/tree; existing ACC RPA tasks and `C:\acc-rpa` remain untouched.
