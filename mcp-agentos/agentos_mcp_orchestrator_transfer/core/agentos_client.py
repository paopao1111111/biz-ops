import json
import time
import requests


class AgentOSClient:
    def __init__(self, base_url, token, timeout=300, poll_interval=3):
        self.base_url = str(base_url or "").rstrip("/")
        self.token = str(token or "").strip()
        self.timeout = int(timeout or 300)
        self.poll_interval = float(poll_interval or 3)

    def run(self, workflow_id, parameters, timeout=None):
        if not self.token:
            return {"success": False, "error": "Missing AgentOS token."}
        if not workflow_id:
            return {"success": False, "error": "Missing AgentOS workflow_id."}

        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }
        start_url = f"{self.base_url}/api/public/workflow_api/test_run"
        poll_url = f"{self.base_url}/api/public/workflow_api/get_process"

        try:
            resp = requests.post(start_url, headers=headers, json={"input": parameters, "commit_id": ""}, timeout=30)
            resp.raise_for_status()
            started = resp.json()
        except requests.exceptions.Timeout:
            return {"success": False, "error": "AgentOS start request timed out."}
        except Exception as exc:
            return {"success": False, "error": f"AgentOS start request failed: {exc}"}

        if started.get("code") != 0:
            return {"success": False, "error": f"AgentOS start error: {started}", "raw": started}

        execute_id = (started.get("data") or {}).get("execute_id")
        if not execute_id:
            return {"success": False, "error": f"AgentOS missing execute_id: {started}", "raw": started}

        deadline = time.time() + int(timeout or self.timeout)
        last_payload = None
        while time.time() < deadline:
            time.sleep(self.poll_interval)
            try:
                proc_resp = requests.get(
                    poll_url,
                    headers=headers,
                    params={"workflow_id": workflow_id, "execute_id": execute_id},
                    timeout=15,
                )
                payload = proc_resp.json()
                last_payload = payload
            except Exception:
                continue

            data = payload.get("data") or {}
            status = data.get("executeStatus")
            if status == 2:
                extracted = self._extract_output(data)
                extracted["execute_id"] = execute_id
                extracted["raw"] = payload
                return extracted
            if status in (3, 4):
                errors = []
                for node in data.get("nodeResults", []):
                    err = str(node.get("errorInfo") or "").strip()
                    if err:
                        errors.append(err)
                return {
                    "success": False,
                    "error": f"AgentOS workflow failed: {'; '.join(errors) or 'unknown'}",
                    "execute_id": execute_id,
                    "raw": payload,
                }

        return {
            "success": False,
            "error": "AgentOS workflow polling timed out.",
            "execute_id": execute_id,
            "raw": last_payload,
        }

    def _extract_output(self, data):
        for node in data.get("nodeResults", []):
            node_type = node.get("NodeType") or node.get("nodeType") or ""
            output = node.get("output", "")
            if node_type == "Start":
                continue
            if node_type in ("LLM", "End") and isinstance(output, str) and output.strip():
                parsed = self._maybe_json(output)
                if isinstance(parsed, dict):
                    for key in ("output", "result", "text", "content", "greeting"):
                        value = parsed.get(key)
                        if isinstance(value, str) and value.strip():
                            return {"success": True, "output": value.strip()}
                    if parsed:
                        return {"success": True, "output": json.dumps(parsed, ensure_ascii=False)}
                return {"success": True, "output": output.strip()}

        for node in data.get("nodeResults", []):
            node_type = node.get("NodeType") or node.get("nodeType") or ""
            output = node.get("output", "")
            if node_type != "Start" and isinstance(output, str) and output.strip():
                return {"success": True, "output": output.strip()}

        return {"success": False, "error": "AgentOS finished but no output found."}

    @staticmethod
    def _maybe_json(value):
        try:
            return json.loads(value)
        except Exception:
            return None
