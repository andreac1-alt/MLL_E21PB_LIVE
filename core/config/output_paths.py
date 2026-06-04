from __future__ import annotations

import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parents[1]


def resolve_output_root() -> Path:
    raw_root = os.getenv("BACKTEST_OUTPUT_ROOT", "").strip()
    if raw_root:
        return Path(raw_root).expanduser()
    return PROJECT_ROOT / "output"
