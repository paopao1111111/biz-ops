from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Event, Thread
import time


class TaskRunner:
    def __init__(self, store, max_workers=3):
        self.store = store
        self.max_workers = int(max_workers or 3)

    def submit(self, task_type, payload, runner, parent_id=None):
        task_id = self.store.create_task(task_type, payload, parent_id=parent_id)
        thread = Thread(target=self._run_task, args=(task_id, runner), daemon=True)
        thread.start()
        return task_id

    def run_inline(self, task_type, payload, runner, parent_id=None):
        task_id = self.store.create_task(task_type, payload, parent_id=parent_id)
        self._run_task(task_id, runner)
        return task_id

    def _run_task(self, task_id, runner):
        task = self.store.get_task(task_id)
        started_at = time.time()
        stop_heartbeat = Event()
        self.store.update_task(
            task_id,
            status="running",
            progress="started",
            heartbeat_at=self.store._now(),
            runtime_seconds=0,
        )
        heartbeat = Thread(
            target=self._heartbeat,
            args=(task_id, started_at, stop_heartbeat),
            daemon=True,
        )
        heartbeat.start()
        try:
            result = runner(task_id, task.get("payload") or {})
            success = bool(isinstance(result, dict) and result.get("success", True))
            status = "success" if success else "fail"
            error = "" if success else str(result.get("error", "Task failed"))
            self.store.update_task(
                task_id,
                status=status,
                result=result,
                error=error,
                progress="finished",
                heartbeat_at=self.store._now(),
                runtime_seconds=int(time.time() - started_at),
            )
        except Exception as exc:
            self.store.update_task(
                task_id,
                status="fail",
                error=str(exc),
                progress="failed",
                heartbeat_at=self.store._now(),
                runtime_seconds=int(time.time() - started_at),
            )
        finally:
            stop_heartbeat.set()

    def _heartbeat(self, task_id, started_at, stop_event):
        while not stop_event.wait(30):
            task = self.store.get_task(task_id)
            if not task or task.get("status") != "running":
                break
            self.store.update_task(
                task_id,
                heartbeat_at=self.store._now(),
                runtime_seconds=int(time.time() - started_at),
            )

    def run_parallel(self, items, worker, max_concurrency=None):
        workers = int(max_concurrency or self.max_workers)
        results = []
        errors = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            futures = {executor.submit(worker, item): item for item in items}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    result = future.result()
                    results.append({"item": item, "result": result})
                    if isinstance(result, dict) and not result.get("success", True):
                        errors.append({"item": item, "error": result.get("error", "failed")})
                except Exception as exc:
                    errors.append({"item": item, "error": str(exc)})
                    results.append({"item": item, "result": {"success": False, "error": str(exc)}})
        return {
            "success": not errors,
            "total": len(items),
            "succeeded": len(items) - len(errors),
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }
