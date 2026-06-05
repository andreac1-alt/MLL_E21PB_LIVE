from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.portfolio.paths import get_portfolio_full_dir


MOMENTUM_SIGNAL_FILENAME = "momentum_inv5d_signal.csv"
BOOTSTRAP_TRADING_DAYS = 30
MOMENTUM_WINDOW = 5
LAG_DAYS = 2

MOMENTUM_SIGNAL_COLUMNS = [
    "bd",
    "prev_momentum_5d_sum",
    "prev_momentum_5d_bucket",
    "prev_momentum_5d_top_half",
    "bootstrap_neutral",
]


def momentum_signal_path(strategy_id: str, variant_id: str, *, layer: str = "live") -> Path:
    return get_portfolio_full_dir(strategy_id, variant_id, layer=layer) / "market" / MOMENTUM_SIGNAL_FILENAME


def empty_momentum_signal_df() -> pd.DataFrame:
    return pd.DataFrame(columns=MOMENTUM_SIGNAL_COLUMNS)


def load_momentum_signal(strategy_id: str, variant_id: str, *, layer: str = "live") -> pd.DataFrame:
    path = momentum_signal_path(strategy_id, variant_id, layer=layer)
    if not path.exists():
        return empty_momentum_signal_df()
    df = pd.read_csv(path)
    if df.empty:
        return empty_momentum_signal_df()
    df = df.reindex(columns=MOMENTUM_SIGNAL_COLUMNS)
    df["bd"] = pd.to_datetime(df["bd"], errors="coerce").dt.normalize()
    df["prev_momentum_5d_sum"] = pd.to_numeric(df["prev_momentum_5d_sum"], errors="coerce")
    df["prev_momentum_5d_top_half"] = df["prev_momentum_5d_top_half"].fillna(False).astype(bool)
    df["bootstrap_neutral"] = df["bootstrap_neutral"].fillna(False).astype(bool)
    return df.dropna(subset=["bd"]).sort_values("bd", kind="stable").reset_index(drop=True)


def assign_expanding_bucket(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    buckets: list[str | None] = []
    historical: list[float] = []

    for value in numeric:
        if pd.isna(value):
            buckets.append(None)
            continue
        historical.append(float(value))
        if len(historical) < 4:
            buckets.append(None)
            continue
        rank = pd.Series(historical).rank(method="average", pct=True).iloc[-1]
        if rank <= 0.25:
            buckets.append("Q1")
        elif rank <= 0.50:
            buckets.append("Q2")
        elif rank <= 0.75:
            buckets.append("Q3")
        else:
            buckets.append("Q4")

    return pd.Series(buckets, index=values.index, dtype="object")


def build_momentum_signal_df(state_df: pd.DataFrame) -> pd.DataFrame:
    if state_df.empty:
        return empty_momentum_signal_df()
    result = state_df.copy()
    result["bd"] = pd.to_datetime(result["date"], errors="coerce").dt.normalize()
    result["portfolio_mtm_r_day"] = pd.to_numeric(result["mtm_r_day"], errors="coerce").fillna(0.0)
    result = result.dropna(subset=["bd"]).sort_values("bd", kind="stable").reset_index(drop=True)
    result["bootstrap_neutral"] = result.index < BOOTSTRAP_TRADING_DAYS
    result["prev_momentum_5d_sum"] = (
        result["portfolio_mtm_r_day"]
        .shift(LAG_DAYS)
        .rolling(MOMENTUM_WINDOW, min_periods=MOMENTUM_WINDOW)
        .sum()
        .round(4)
    )
    result["prev_momentum_5d_bucket"] = assign_expanding_bucket(result["prev_momentum_5d_sum"])
    result["prev_momentum_5d_top_half"] = result["prev_momentum_5d_bucket"].isin(["Q3", "Q4"])
    result.loc[result["bootstrap_neutral"], "prev_momentum_5d_bucket"] = None
    result.loc[result["bootstrap_neutral"], "prev_momentum_5d_top_half"] = False
    return result[MOMENTUM_SIGNAL_COLUMNS].copy()


def save_momentum_signal(
    state_df: pd.DataFrame,
    strategy_id: str,
    variant_id: str,
    *,
    layer: str = "live",
) -> Path:
    signal_df = build_momentum_signal_df(state_df)
    path = momentum_signal_path(strategy_id, variant_id, layer=layer)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = signal_df.copy()
    if not output.empty:
        output["bd"] = pd.to_datetime(output["bd"], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    output.to_csv(path, index=False)
    return path
