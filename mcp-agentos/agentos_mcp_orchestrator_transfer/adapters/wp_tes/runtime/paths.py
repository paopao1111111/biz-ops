import os
from pathlib import Path


RUNTIME_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = RUNTIME_DIR.parent
PROJECT_DIR = ADAPTER_DIR.parent.parent
ASSETS_DIR = Path(os.getenv("WP_TES_ASSETS_DIR", ADAPTER_DIR / "assets"))
STORAGE_DIR = Path(os.getenv("WP_TES_STORAGE_DIR", PROJECT_DIR / "storage" / "wp_tes"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)
