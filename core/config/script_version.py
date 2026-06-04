from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
SCRIPT_VERSIONS = {
    "1_run_day.py": "1.0.0",
    "2_run_trade_state_day.py": "1.0.0",
    "3_build_portfolio_day.py": "1.0.0",
    "scripts/run_live_bd.py": "1.0.0",
    "scripts/run_live_sd.py": "1.0.0",
    "first_screen.py": "1.0.0",
    "second_screen.py": "1.0.0",
    "run_day.py": "1.0.0",
    "run_day_all_years.py": "1.0.0",
    "semaforo.py": "1.0.0",
    "run_trading_day.py": "1.0.0",
    "tools/strategy_EMA21_SMA50.py": "1.0.0",
}

VERSIONED_SCRIPT_TARGETS = tuple(SCRIPT_VERSIONS.keys())


def normalize_script_key(script_path: str | Path) -> str:
    path = Path(script_path)
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    try:
        relative = resolved.relative_to(BASE_DIR).as_posix()
    except Exception:
        relative = resolved.as_posix()
    normalized = path.as_posix()
    if relative in SCRIPT_VERSIONS:
        return relative
    if normalized in SCRIPT_VERSIONS:
        return normalized
    if path.name in SCRIPT_VERSIONS:
        return path.name
    raise KeyError(f"Script non versionato: {script_path}")


def get_script_version(script_path: str | Path) -> str:
    return SCRIPT_VERSIONS[normalize_script_key(script_path)]


def build_script_signature(script_path: str | Path) -> tuple[str, str]:
    key = normalize_script_key(script_path)
    return Path(key).name, get_script_version(key)
