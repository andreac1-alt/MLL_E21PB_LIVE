from __future__ import annotations

import os
from pathlib import Path


PROJECT_BASE_DIR = Path(__file__).resolve().parent


def resolve_market_data_root() -> Path:
    raw_root = os.getenv("MARKET_DATA_ROOT", "").strip()
    if not raw_root:
        raise RuntimeError(
            "MARKET_DATA_ROOT non impostata. "
            "Imposta la variabile ambiente verso la data root condivisa, "
            "ad esempio: export MARKET_DATA_ROOT=/Users/andreacecchini/market_data",
        )
    return Path(raw_root).expanduser()


MARKET_DATA_ROOT = resolve_market_data_root()
CACHE_DIR = MARKET_DATA_ROOT / "cache"
PRICE_CACHE_DIR = CACHE_DIR / "price_history"
PRICE_CACHE_METADATA_DIR = CACHE_DIR / "price_history_meta"
COMPANY_PROFILE_CACHE_DIR = CACHE_DIR / "company_profile"
MARKET_CAP_CACHE_PATH = CACHE_DIR / "market_caps.csv"
PRICE_UNAVAILABLE_CACHE_PATH = CACHE_DIR / "unavailable_history.json"
UNIVERSE_DIR = MARKET_DATA_ROOT / "universe"
UNIVERSE_PATH = UNIVERSE_DIR / "nasdaq_nyse.csv"
REFERENCE_DATA_DIR = MARKET_DATA_ROOT / "reference"
REFERENCE_ETF_MAP_PATH = REFERENCE_DATA_DIR / "reference_etf_map.csv"
