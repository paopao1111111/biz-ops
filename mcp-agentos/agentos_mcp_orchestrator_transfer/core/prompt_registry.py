import json
import re
from pathlib import Path


class PromptRegistry:
    def __init__(self):
        self._prompts = {}

    def load_json(self, path, namespace=""):
        prompt_path = Path(path)
        if not prompt_path.is_file():
            return
        data = json.loads(prompt_path.read_text(encoding="utf-8"))
        prefix = f"{namespace}." if namespace else ""
        for name, value in data.items():
            self._prompts[f"{prefix}{name}"] = value
            if namespace:
                self._prompts.setdefault(name, value)

    def list(self):
        return sorted(self._prompts.keys())

    def get(self, name):
        if name not in self._prompts:
            raise KeyError(f"Unknown prompt: {name}")
        return self._prompts[name]

    def render(self, name, variables=None, enforce_json=False):
        text = self.get(name)
        variables = variables or {}

        def replace(match):
            key = match.group(1)
            if key in variables:
                return str(variables[key])
            return match.group(0)

        rendered = re.sub(r"\{(\w+)\}", replace, text)
        if enforce_json:
            rendered += (
                "\n\n--- CRITICAL OUTPUT INSTRUCTION ---\n"
                "You MUST respond with ONLY a valid JSON object. Nothing else.\n"
                "Do NOT include text before or after the JSON.\n"
                "Do NOT use markdown code fences.\n"
                "Your entire response must start with { and end with }.\n"
                "--- END INSTRUCTION ---"
            )
        return rendered
