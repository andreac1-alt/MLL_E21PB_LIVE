from __future__ import annotations

from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]
PORTFOLIO_LIVE_DIR = BASE_DIR / "output" / "portfolio_live"
PORTFOLIO_FROZEN_DIR = BASE_DIR / "output" / "portfolio_frozen"
TRADE_TIMELINE_VARIANT_ID = "trade_timeline"
VALID_PORTFOLIO_LAYERS = {"live", "frozen"}

TRADE_LIFECYCLE_FILENAMES = {
    "live": "trade_lifecycle_live.csv",
    "frozen": "trade_lifecycle_frozen.csv",
}

def get_portfolio_root_dir(layer: str = "live") -> Path:
    normalized = str(layer).strip().lower()
    if normalized not in VALID_PORTFOLIO_LAYERS:
        raise ValueError(f"Portfolio layer non valido: {layer}")
    return PORTFOLIO_LIVE_DIR if normalized == "live" else PORTFOLIO_FROZEN_DIR


def normalize_portfolio_layer(layer: str = "live") -> str:
    normalized = str(layer).strip().lower()
    if normalized not in VALID_PORTFOLIO_LAYERS:
        raise ValueError(f"Portfolio layer non valido: {layer}")
    return normalized


def get_portfolio_full_dir(strategy_id: str, variant_id: str, *, layer: str = "live") -> Path:
    return get_portfolio_root_dir(layer) / "full" / strategy_id / variant_id


def get_portfolio_yearly_dir(year: int, strategy_id: str, variant_id: str, *, layer: str = "live") -> Path:
    return get_portfolio_root_dir(layer) / "yearly" / str(year) / strategy_id / variant_id


def get_trade_timeline_full_dir(strategy_id: str, *, layer: str = "live") -> Path:
    return get_portfolio_full_dir(strategy_id, TRADE_TIMELINE_VARIANT_ID, layer=layer)


def get_trade_timeline_yearly_dir(year: int, strategy_id: str, *, layer: str = "live") -> Path:
    return get_portfolio_yearly_dir(year, strategy_id, TRADE_TIMELINE_VARIANT_ID, layer=layer)


def get_trade_lifecycle_filename(layer: str = "live") -> str:
    normalized = normalize_portfolio_layer(layer)
    return TRADE_LIFECYCLE_FILENAMES[normalized]


def ensure_portfolio_dirs(
    strategy_id: str,
    variant_id: str,
    years: list[int] | None = None,
    *,
    layer: str = "live",
) -> dict[str, Path]:
    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)

    yearly_dirs: dict[str, Path] = {}
    for year in years or []:
        year_dir = get_portfolio_yearly_dir(year, strategy_id, variant_id, layer=layer)
        year_dir.mkdir(parents=True, exist_ok=True)
        yearly_dirs[str(year)] = year_dir

    return {"full": full_dir, **yearly_dirs}
