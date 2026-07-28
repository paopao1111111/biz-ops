class WorkflowRegistry:
    def __init__(self):
        self._tools = {}
        self._workflows = {}

    def tool(self, name, func, description=""):
        self._tools[name] = {"name": name, "func": func, "description": description}

    def workflow(self, name, operation, description="", mode="async"):
        self._workflows[name] = {
            "name": name,
            "operation": operation,
            "description": description,
            "mode": mode,
        }

    def list_workflows(self):
        return [
            {
                "name": item["name"],
                "operation": item["operation"],
                "description": item.get("description", ""),
                "mode": item.get("mode", "async"),
            }
            for item in sorted(self._workflows.values(), key=lambda x: x["name"])
        ]

    def get_workflow(self, name):
        return self._workflows.get(name)

    def get_tool(self, name):
        tool = self._tools.get(name)
        return tool["func"] if tool else None

    def run_operation(self, ctx, operation, payload):
        func = self.get_tool(operation)
        if not func:
            return {"success": False, "error": f"Unknown operation: {operation}"}
        return func(ctx, payload or {})
