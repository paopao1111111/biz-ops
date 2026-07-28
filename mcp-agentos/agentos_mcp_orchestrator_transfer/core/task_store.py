import json
import uuid
from datetime import datetime
from pathlib import Path
from threading import RLock


class TaskStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def create_task(self, task_type, payload=None, parent_id=None):
        task_id = self._new_id()
        now = self._now()
        task = {
            "task_id": task_id,
            "type": task_type,
            "status": "pending",
            "payload": payload or {},
            "parent_id": parent_id or "",
            "children": [],
            "progress": "",
            "result": None,
            "error": "",
            "created_at": now,
            "updated_at": now,
        }
        self.save(task)
        if parent_id:
            parent = self.get_task(parent_id)
            if parent:
                children = parent.get("children") or []
                if task_id not in children:
                    children.append(task_id)
                self.update_task(parent_id, children=children)
        return task_id

    def save(self, task):
        with self._lock:
            path = self._path(task["task_id"])
            path.write_text(json.dumps(task, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def update_task(self, task_id, **updates):
        with self._lock:
            task = self.get_task(task_id)
            if not task:
                return None
            task.update({key: value for key, value in updates.items() if value is not None})
            task["updated_at"] = self._now()
            self.save(task)
            return task

    def get_task(self, task_id):
        path = self._path(task_id)
        if not path.is_file():
            return None
        with self._lock:
            return json.loads(path.read_text(encoding="utf-8"))

    def list_tasks(self, limit=50):
        tasks = []
        for path in self.root.glob("*.json"):
            try:
                tasks.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return tasks[: int(limit or 50)]

    def _path(self, task_id):
        return self.root / f"{task_id}.json"

    @staticmethod
    def _new_id():
        return datetime.now().strftime("%Y%m%d") + "-" + uuid.uuid4().hex[:6]

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
