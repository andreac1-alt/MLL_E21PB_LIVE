from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.config.data_paths import MARKET_DATA_ROOT


OPERATIONAL_UNIVERSE_MONTHLY_DIR = (
    MARKET_DATA_ROOT / "reference" / "operational_universe" / "monthly"
)
BENCHMARK_OVERRIDE_TICKERS = ["SPY", "QQQ"]


def operational_universe_monthly_path(screen_date: pd.Timestamp) -> Path:
    month_label = pd.Timestamp(screen_date).strftime("%Y-%m")
    return OPERATIONAL_UNIVERSE_MONTHLY_DIR / f"operational_universe_{month_label}.csv"


def load_operational_universe_for_date(screen_date: pd.Timestamp) -> pd.DataFrame:
    target_ts = pd.Timestamp(screen_date).normalize()
    path = operational_universe_monthly_path(target_ts)
    if not path.exists():
        raise FileNotFoundError(
            "Universo operativo mensile non trovato: "
            f"{path}. Generarlo prima con "
            "/Users/andreacecchini/market_data/venv/bin/python "
            "/Users/andreacecchini/market_data/scripts/build_operational_universe_monthly.py "
            f"--month {target_ts.strftime('%Y-%m')}"
        )

    universe = pd.read_csv(path, dtype=str).fillna("")
    required_columns = {"ticker", "exchange", "name", "listing_date", "delisting_date"}
    missing_columns = required_columns - set(universe.columns)
    if missing_columns:
        raise ValueError(
            f"Colonne mancanti nell'universo operativo {path}: {sorted(missing_columns)}"
        )

    prepared = universe.copy()
    prepared["ticker"] = prepared["ticker"].astype(str).str.strip().str.upper()
    prepared["exchange"] = prepared["exchange"].astype(str).str.strip().str.upper()
    prepared["name"] = prepared["name"].astype(str).str.strip()
    prepared = prepared[prepared["ticker"].ne("")]

    listing_dates = pd.to_datetime(
        prepared["listing_date"].replace("", pd.NA),
        errors="coerce",
    ).dt.normalize()
    delisting_dates = pd.to_datetime(
        prepared["delisting_date"].replace("", pd.NA),
        errors="coerce",
    ).dt.normalize()

    daily_valid = (
        (listing_dates.isna() | (listing_dates <= target_ts))
        & (delisting_dates.isna() | (target_ts <= delisting_dates))
    )
    prepared = prepared[daily_valid].copy()

    benchmark_rows = pd.DataFrame(
        [
            {"ticker": ticker, "exchange": "", "name": ticker}
            for ticker in BENCHMARK_OVERRIDE_TICKERS
            if ticker not in set(prepared["ticker"])
        ]
    )
    if not benchmark_rows.empty:
        prepared = pd.concat([prepared, benchmark_rows], ignore_index=True, sort=False)

    return prepared.drop_duplicates(subset=["ticker"]).sort_values("ticker").reset_index(drop=True)
