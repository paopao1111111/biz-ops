from dataclasses import dataclass


@dataclass
class ToolContext:
    config: dict
    agentos: object
    prompts: object
    registry: object
    store: object
    runner: object
    adapter_configs: dict
