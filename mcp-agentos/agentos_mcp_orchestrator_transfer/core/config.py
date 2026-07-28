import os
import re
from pathlib import Path


_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::-([^}]*))?\}")


def load_env_file(path):
    env_path = Path(path)
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _expand_env(value):
    if isinstance(value, str):
        def replace(match):
            key = match.group(1)
            default = match.group(2) or ""
            return os.getenv(key, default)
        return _ENV_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _parse_scalar(value):
    value = value.strip()
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    if value in ("null", "None"):
        return None
    if value.isdigit():
        return int(value)
    try:
        return float(value)
    except ValueError:
        return value.strip('"\'')


def _minimal_yaml_load(text):
    root = {}
    stack = [(-1, root)]
    lines = [line.rstrip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]

    for index, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if not isinstance(parent, list):
                raise ValueError(f"Invalid YAML list item at line {index + 1}: {line}")
            if not item_text:
                item = {}
                parent.append(item)
                stack.append((indent, item))
                continue
            if ":" in item_text:
                key, raw = item_text.split(":", 1)
                item = {key.strip(): _parse_scalar(raw) if raw.strip() else {}}
                parent.append(item)
                if not raw.strip():
                    stack.append((indent, item[key.strip()]))
                else:
                    stack.append((indent, item))
            else:
                parent.append(_parse_scalar(item_text))
            continue

        if ":" not in stripped:
            raise ValueError(f"Invalid YAML line {index + 1}: {line}")

        key, raw = stripped.split(":", 1)
        key = key.strip()
        raw = raw.strip()

        if raw:
            parent[key] = _parse_scalar(raw)
            continue

        next_is_list = False
        if index + 1 < len(lines):
            next_stripped = lines[index + 1].strip()
            next_indent = len(lines[index + 1]) - len(lines[index + 1].lstrip(" "))
            next_is_list = next_indent > indent and next_stripped.startswith("- ")
        value = [] if next_is_list else {}
        parent[key] = value
        stack.append((indent, value))

    return root


def load_config(config_path):
    path = Path(config_path).resolve()
    load_env_file(path.parent / ".env")

    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        data = yaml.safe_load(text) or {}
    except Exception:
        data = _minimal_yaml_load(text)
    return _expand_env(data)
