from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from core.portfolio.ids import build_portfolio_id
from core.portfolio.paths import get_portfolio_full_dir, get_portfolio_yearly_dir
from core.portfolio.schema import PORTFOLIO_STATE_DAILY_COLUMNS


BASE_DIR = Path(__file__).resolve().parents[2]
STORICO_SPY_PATH = BASE_DIR / "output" / "storico_SPY.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build portfolio_state_daily.csv from portfolio positions/actions outputs."
    )
    parser.add_argument("--strategy-id", required=True, help="Strategy id, for example EMA21_SMA50.")
    parser.add_argument("--variant-id", required=True, help="Portfolio variant id.")
    return parser.parse_args()


def load_positions_df(strategy_id: str, variant_id: str, *, layer: str = "live") -> pd.DataFrame:
    path = get_portfolio_full_dir(strategy_id, variant_id, layer=layer) / "portfolio_positions_daily.csv"
    if not path.exists():
        raise FileNotFoundError(f"Portfolio positions non trovato: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    bool_cols = ["is_open", "opened_today", "closed_today", "carry_in_from_prev_year"]
    for col in bool_cols:
        df[col] = df[col].fillna(False).astype(bool)
    numeric_cols = ["unrealized_r", "total_mtm_r", "shares_open"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "ticker", "position_id"]).copy()


def load_actions_df(strategy_id: str, variant_id: str, *, layer: str = "live") -> pd.DataFrame:
    path = get_portfolio_full_dir(strategy_id, variant_id, layer=layer) / "portfolio_actions_daily.csv"
    if not path.exists():
        raise FileNotFoundError(f"Portfolio actions non trovato: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce").dt.normalize()
    df["action_type"] = df["action_type"].astype(str).str.strip().str.upper()
    numeric_cols = ["realized_r_delta", "shares_delta"]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["action_date", "position_id", "ticker"]).copy()


def load_storico_spy_df() -> pd.DataFrame:
    if not STORICO_SPY_PATH.exists():
        return pd.DataFrame(columns=["date", "market_street_light", "blue_on"])
    storico_df = pd.read_csv(STORICO_SPY_PATH, usecols=["date", "market_street_light", "blue_on"])
    storico_df["date"] = pd.to_datetime(storico_df["date"], errors="coerce").dt.normalize()
    storico_df["market_street_light"] = storico_df["market_street_light"].astype(str).str.strip().str.upper()
    storico_df["blue_on"] = storico_df["blue_on"].fillna(False).astype(bool)
    return storico_df.dropna(subset=["date"]).copy()


def build_state_df(strategy_id: str, variant_id: str) -> pd.DataFrame:
    positions_df = load_positions_df(strategy_id, variant_id)
    actions_df = load_actions_df(strategy_id, variant_id)
    if positions_df.empty:
        return pd.DataFrame(columns=PORTFOLIO_STATE_DAILY_COLUMNS)

    positions_daily = (
        positions_df.groupby("date", dropna=False)
        .agg(
            unrealized_r=("unrealized_r", "sum"),
            open_positions_count=("is_open", "sum"),
            new_entries_count=("opened_today", "sum"),
            full_exit_count=("closed_today", "sum"),
        )
        .reset_index()
    )

    partial_exit_daily = pd.DataFrame(columns=["date", "partial_exit_count"])
    realized_daily = pd.DataFrame(columns=["date", "realized_r_day"])
    if not actions_df.empty:
        actions_daily = actions_df.copy()
        actions_daily["date"] = actions_daily["action_date"]
        realized_daily = (
            actions_daily.groupby("date", dropna=False)["realized_r_delta"]
            .sum()
            .reset_index(name="realized_r_day")
        )
        partial_exit_mask = (
            actions_daily["action_type"].eq("SELL")
            & (pd.to_numeric(actions_daily["shares_open_after"], errors="coerce").fillna(0).astype(int) > 0)
        )
        partial_exit_daily = (
            actions_daily.loc[partial_exit_mask]
            .groupby("date", dropna=False)
            .size()
            .reset_index(name="partial_exit_count")
        )

    ticker_daily = (
        positions_df.loc[positions_df["is_open"]]
        .groupby("date", dropna=False)["ticker"]
        .agg(lambda values: ",".join(sorted({str(value).upper() for value in values if str(value).strip()})))
        .reset_index(name="open_tickers")
    )

    carry_daily = (
        positions_df.loc[positions_df["is_open"] & positions_df["carry_in_from_prev_year"]]
        .groupby("date", dropna=False)
        .size()
        .reset_index(name="carry_positions_count")
    )

    state_df = positions_daily.merge(realized_daily, on="date", how="left")
    state_df = state_df.merge(partial_exit_daily, on="date", how="left")
    state_df = state_df.merge(ticker_daily, on="date", how="left")
    state_df = state_df.merge(carry_daily, on="date", how="left")

    storico_df = load_storico_spy_df().rename(
        columns={"market_street_light": "semaphore_color"}
    )
    calendar_df = pd.DataFrame(columns=["date"])
    if not storico_df.empty:
        min_state_date = pd.Timestamp(positions_df["date"].min()).normalize()
        max_calendar_date = pd.Timestamp(storico_df["date"].max()).normalize()
        calendar_df = storico_df.loc[
            (storico_df["date"] >= min_state_date) & (storico_df["date"] <= max_calendar_date),
            ["date"],
        ].drop_duplicates().sort_values("date").reset_index(drop=True)
    if calendar_df.empty:
        calendar_df = pd.DataFrame(
            {"date": positions_df["date"].dropna().drop_duplicates().sort_values().reset_index(drop=True)}
        )

    state_df = calendar_df.merge(state_df, on="date", how="left")
    state_df = state_df.merge(storico_df, on="date", how="left")

    state_df["unrealized_r"] = pd.to_numeric(state_df["unrealized_r"], errors="coerce").fillna(0.0)
    state_df["open_positions_count"] = pd.to_numeric(state_df["open_positions_count"], errors="coerce").fillna(0).astype(int)
    state_df["new_entries_count"] = pd.to_numeric(state_df["new_entries_count"], errors="coerce").fillna(0).astype(int)
    state_df["full_exit_count"] = pd.to_numeric(state_df["full_exit_count"], errors="coerce").fillna(0).astype(int)
    state_df["realized_r_day"] = pd.to_numeric(state_df["realized_r_day"], errors="coerce").fillna(0.0)
    state_df["partial_exit_count"] = pd.to_numeric(state_df["partial_exit_count"], errors="coerce").fillna(0).astype(int)
    state_df["open_tickers"] = state_df["open_tickers"].fillna("")
    state_df["carry_positions_count"] = (
        pd.to_numeric(state_df["carry_positions_count"], errors="coerce").fillna(0).astype(int)
    )
    state_df["blue_on"] = state_df["blue_on"].fillna(False).astype(bool)
    state_df["semaphore_color"] = (
        state_df["semaphore_color"].astype(str).str.strip().str.upper().replace({"NAN": ""})
    )

    state_df = state_df.sort_values("date").reset_index(drop=True)
    state_df["realized_r_cum"] = state_df["realized_r_day"].cumsum().round(4)
    state_df["unrealized_r"] = state_df["unrealized_r"].round(4)
    state_df["equity_mtm_r"] = (state_df["realized_r_cum"] + state_df["unrealized_r"]).round(4)
    state_df["mtm_r_day"] = state_df["equity_mtm_r"].diff().fillna(state_df["equity_mtm_r"]).round(4)
    state_df["drawdown_mtm_r"] = (
        state_df["equity_mtm_r"] - state_df["equity_mtm_r"].cummax()
    ).round(4)
    state_df["has_carry_positions"] = state_df["carry_positions_count"] > 0

    portfolio_id = build_portfolio_id(strategy_id, variant_id)
    result_df = pd.DataFrame(
        {
            "date": state_df["date"].dt.strftime("%Y-%m-%d"),
            "portfolio_id": portfolio_id,
            "strategy_id": strategy_id,
            "variant_id": variant_id,
            "equity_mtm_r": state_df["equity_mtm_r"].round(4),
            "realized_r_cum": state_df["realized_r_cum"].round(4),
            "unrealized_r": state_df["unrealized_r"].round(4),
            "mtm_r_day": state_df["mtm_r_day"].round(4),
            "drawdown_mtm_r": state_df["drawdown_mtm_r"].round(4),
            "open_positions_count": pd.to_numeric(state_df["open_positions_count"], errors="coerce").fillna(0).astype(int),
            "new_entries_count": pd.to_numeric(state_df["new_entries_count"], errors="coerce").fillna(0).astype(int),
            "partial_exit_count": state_df["partial_exit_count"].astype(int),
            "full_exit_count": pd.to_numeric(state_df["full_exit_count"], errors="coerce").fillna(0).astype(int),
            "semaphore_color": state_df["semaphore_color"],
            "blue_on": state_df["blue_on"].astype(bool),
            "has_carry_positions": state_df["has_carry_positions"].astype(bool),
            "open_tickers": state_df["open_tickers"].astype(str),
        }
    )
    return result_df[PORTFOLIO_STATE_DAILY_COLUMNS].copy()


def save_state_outputs(
    strategy_id: str,
    variant_id: str,
    state_df: pd.DataFrame,
    *,
    layer: str = "live",
) -> list[Path]:
    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []

    full_path = full_dir / "portfolio_state_daily.csv"
    state_df.sort_values("date").to_csv(full_path, index=False)
    output_paths.append(full_path)

    temp_df = state_df.copy()
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.normalize()
    temp_df["year"] = temp_df["date"].dt.year
    for year, group in temp_df.groupby("year", dropna=False):
        year_int = int(year)
        year_dir = get_portfolio_yearly_dir(year_int, strategy_id, variant_id, layer=layer)
        year_dir.mkdir(parents=True, exist_ok=True)
        output_path = year_dir / "portfolio_state_daily.csv"
        group.drop(columns=["year"]).sort_values("date").to_csv(output_path, index=False)
        output_paths.append(output_path)
    return output_paths


def run_build(
    strategy_id: str,
    variant_id: str,
    *,
    layer: str = "live",
) -> dict[str, object]:
    original_positions_loader = load_positions_df
    original_actions_loader = load_actions_df
    def _scoped_positions_loader(strategy_id_inner: str, variant_id_inner: str) -> pd.DataFrame:
        return original_positions_loader(strategy_id_inner, variant_id_inner, layer=layer)
    def _scoped_actions_loader(strategy_id_inner: str, variant_id_inner: str) -> pd.DataFrame:
        return original_actions_loader(strategy_id_inner, variant_id_inner, layer=layer)

    globals()["load_positions_df"] = _scoped_positions_loader
    globals()["load_actions_df"] = _scoped_actions_loader
    try:
        state_df = build_state_df(strategy_id, variant_id)
    finally:
        globals()["load_positions_df"] = original_positions_loader
        globals()["load_actions_df"] = original_actions_loader
    output_paths = save_state_outputs(strategy_id, variant_id, state_df, layer=layer)
    return {
        "strategy_id": strategy_id,
        "variant_id": variant_id,
        "layer": layer,
        "rows": len(state_df),
        "output_paths": output_paths,
    }


def main() -> None:
    args = parse_args()
    result = run_build(args.strategy_id, args.variant_id)
    print(f"Strategy: {result['strategy_id']}")
    print(f"Variant: {result['variant_id']}")
    print(f"Rows: {result['rows']}")
    print(f"portfolio_state_daily.csv written: {len(result['output_paths'])} files")


if __name__ == "__main__":
    main()
