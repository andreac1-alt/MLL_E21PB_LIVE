from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from core.portfolio.ids import build_portfolio_id, build_position_id
from core.portfolio.paths import get_portfolio_full_dir, get_portfolio_yearly_dir, get_trade_lifecycle_filename
from core.portfolio.schema import PORTFOLIO_ACTIONS_DAILY_COLUMNS


BASE_DIR = Path(__file__).resolve().parents[2]
TRADE_TIMELINE_VARIANT_ID = "trade_timeline"
DEFAULT_TRADE_LIFECYCLE_FILENAME = get_trade_lifecycle_filename("live")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build portfolio_actions_daily.csv from trade reconstruction outputs."
    )
    parser.add_argument("--strategy-id", required=True, help="Strategy id, for example EMA21_SMA50.")
    parser.add_argument("--variant-id", required=True, help="Portfolio variant id.")
    return parser.parse_args()


def load_trade_lifecycle_df(
    strategy_id: str,
    *,
    layer: str = "live",
    lifecycle_filename: str = DEFAULT_TRADE_LIFECYCLE_FILENAME,
) -> pd.DataFrame:
    path = (
        get_portfolio_full_dir(strategy_id, TRADE_TIMELINE_VARIANT_ID, layer=layer)
        / lifecycle_filename
    )
    if not path.exists():
        raise FileNotFoundError(f"Trade lifecycle non trovato: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    for col in ["date", "entry_date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    numeric_cols = [
        "trade_index",
        "total_entry_shares",
        "shares_bought_day",
        "shares_sold_day",
        "shares_open_end",
        "realized_pnl_cum",
        "realized_r_day",
        "risk_amount",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "entry_date", "ticker", "source_event_id"]).copy()


def load_positions_df(strategy_id: str, variant_id: str, *, layer: str = "live") -> pd.DataFrame:
    path = (
        get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
        / "portfolio_positions_daily.csv"
    )
    if not path.exists():
        raise FileNotFoundError(f"Portfolio positions non trovato: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    for col in ["date", "entry_date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    df["entry_seq"] = pd.to_numeric(df["entry_seq"], errors="coerce").fillna(0).astype(int)
    df["r_multiplier"] = pd.to_numeric(df.get("r_multiplier", 1.0), errors="coerce").fillna(1.0)
    df["shares_open"] = pd.to_numeric(df["shares_open"], errors="coerce").fillna(0).astype(int)
    return df.dropna(subset=["date", "entry_date", "ticker", "position_id"]).copy()


def build_position_index_from_positions(
    positions_df: pd.DataFrame,
    strategy_id: str,
    variant_id: str,
) -> pd.DataFrame:
    entry_rows = positions_df[positions_df["opened_today"] == True].copy()
    entry_rows["ticker"] = entry_rows["ticker"].astype(str).str.upper()
    entry_rows["entry_date"] = pd.to_datetime(entry_rows["entry_date"], errors="coerce").dt.normalize()
    entry_rows["entry_seq"] = pd.to_numeric(entry_rows["entry_seq"], errors="coerce").fillna(0).astype(int)
    entry_rows["entry_date_yyyymmdd"] = entry_rows["entry_date"].dt.strftime("%Y%m%d")
    entry_rows["position_id_check"] = entry_rows.apply(
        lambda row: build_position_id(
            strategy_id,
            variant_id,
            str(row["ticker"]),
            str(row["entry_date_yyyymmdd"]),
            int(row["entry_seq"]),
        ),
        axis=1,
    )
    return entry_rows[
        ["position_id", "position_id_check", "ticker", "entry_date", "entry_seq", "r_multiplier"]
    ].drop_duplicates(subset=["position_id", "ticker", "entry_date", "entry_seq"])


def build_lifecycle_entry_seq(lifecycle_df: pd.DataFrame) -> pd.DataFrame:
    entry_rows = lifecycle_df[lifecycle_df["shares_bought_day"].fillna(0).astype(float) > 0].copy()
    entry_rows = entry_rows.sort_values(["ticker", "entry_date", "source_event_id", "trade_index"])
    entry_rows["entry_seq"] = entry_rows.groupby(["ticker", "entry_date"]).cumcount() + 1
    return entry_rows[["source_event_id", "ticker", "entry_date", "entry_seq"]].drop_duplicates(
        subset=["source_event_id", "ticker"]
    )


def build_actions_df(strategy_id: str, variant_id: str) -> pd.DataFrame:
    lifecycle_df = load_trade_lifecycle_df(strategy_id)
    positions_df = load_positions_df(strategy_id, variant_id)
    if lifecycle_df.empty or positions_df.empty:
        return pd.DataFrame(columns=PORTFOLIO_ACTIONS_DAILY_COLUMNS)

    lifecycle_entry_index = build_lifecycle_entry_seq(lifecycle_df)
    lifecycle_df = lifecycle_df.merge(
        lifecycle_entry_index,
        on=["source_event_id", "ticker", "entry_date"],
        how="left",
    )

    position_index = build_position_index_from_positions(positions_df, strategy_id, variant_id)
    lifecycle_df = lifecycle_df.merge(
        position_index[["position_id", "ticker", "entry_date", "entry_seq", "r_multiplier"]],
        on=["ticker", "entry_date", "entry_seq"],
        how="inner",
    )

    action_rows: list[dict[str, object]] = []
    portfolio_id = build_portfolio_id(strategy_id, variant_id)

    for position_id, group in lifecycle_df.sort_values(["date", "ticker", "trade_index"]).groupby("position_id", dropna=False):
        position_group = group.sort_values(["date", "trade_index"]).copy()
        action_seq = 1
        realized_r_cum = 0.0
        entry_date = pd.Timestamp(position_group["entry_date"].iloc[0]).normalize()
        ticker = str(position_group["ticker"].iloc[0]).upper()

        for _, row in position_group.iterrows():
            action_date = pd.Timestamp(row["date"]).normalize()
            risk_amount = float(row.get("risk_amount", 0.0) or 0.0)
            r_multiplier = float(pd.to_numeric(row.get("r_multiplier"), errors="coerce") or 1.0)

            shares_bought_day = int(pd.to_numeric(row.get("shares_bought_day"), errors="coerce") or 0)
            if shares_bought_day > 0:
                action_rows.append(
                    {
                        "action_date": action_date.strftime("%Y-%m-%d"),
                        "portfolio_id": portfolio_id,
                        "strategy_id": strategy_id,
                        "variant_id": variant_id,
                        "position_id": position_id,
                        "ticker": ticker,
                        "action_seq": action_seq,
                        "action_type": "BUY",
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "shares_delta": shares_bought_day,
                        "shares_open_after": int(row["shares_open_end"]),
                        "realized_r_delta": 0.0,
                        "realized_r_cum_position": round(realized_r_cum, 4),
                        "note": str(row.get("buy_reason", "") or ""),
                    }
                )
                action_seq += 1

            shares_sold_day = int(pd.to_numeric(row.get("shares_sold_day"), errors="coerce") or 0)
            if shares_sold_day > 0:
                realized_r_delta = float(pd.to_numeric(row.get("realized_r_day"), errors="coerce") or 0.0)
                realized_r_delta = round(realized_r_delta * r_multiplier, 4)
                realized_r_cum = round(realized_r_cum + realized_r_delta, 4)
                action_rows.append(
                    {
                        "action_date": action_date.strftime("%Y-%m-%d"),
                        "portfolio_id": portfolio_id,
                        "strategy_id": strategy_id,
                        "variant_id": variant_id,
                        "position_id": position_id,
                        "ticker": ticker,
                        "action_seq": action_seq,
                        "action_type": "SELL",
                        "entry_date": entry_date.strftime("%Y-%m-%d"),
                        "shares_delta": -shares_sold_day,
                        "shares_open_after": int(row["shares_open_end"]),
                        "realized_r_delta": round(realized_r_delta, 4),
                        "realized_r_cum_position": round(realized_r_cum, 4),
                        "note": str(row.get("sell_reason", "") or ""),
                    }
                )
                action_seq += 1

    if not action_rows:
        return pd.DataFrame(columns=PORTFOLIO_ACTIONS_DAILY_COLUMNS)

    actions_df = pd.DataFrame(action_rows)
    return actions_df[PORTFOLIO_ACTIONS_DAILY_COLUMNS].copy()


def save_actions_outputs(
    strategy_id: str,
    variant_id: str,
    actions_df: pd.DataFrame,
    *,
    layer: str = "live",
) -> list[Path]:
    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []

    full_path = full_dir / "portfolio_actions_daily.csv"
    actions_df.sort_values(["action_date", "ticker", "entry_date", "action_seq"]).to_csv(full_path, index=False)
    output_paths.append(full_path)

    temp_df = actions_df.copy()
    temp_df["action_date"] = pd.to_datetime(temp_df["action_date"], errors="coerce").dt.normalize()
    temp_df["year"] = temp_df["action_date"].dt.year
    for year, group in temp_df.groupby("year", dropna=False):
        year_int = int(year)
        year_dir = get_portfolio_yearly_dir(year_int, strategy_id, variant_id, layer=layer)
        year_dir.mkdir(parents=True, exist_ok=True)
        output_path = year_dir / "portfolio_actions_daily.csv"
        group.drop(columns=["year"]).sort_values(["action_date", "ticker", "entry_date", "action_seq"]).to_csv(
            output_path,
            index=False,
        )
        output_paths.append(output_path)
    return output_paths


def run_build(
    strategy_id: str,
    variant_id: str,
    *,
    layer: str = "live",
    lifecycle_filename: str = DEFAULT_TRADE_LIFECYCLE_FILENAME,
) -> dict[str, object]:
    original_lifecycle_loader = load_trade_lifecycle_df
    original_positions_loader = load_positions_df
    def _scoped_lifecycle_loader(strategy_id_inner: str) -> pd.DataFrame:
        return original_lifecycle_loader(
            strategy_id_inner,
            layer=layer,
            lifecycle_filename=lifecycle_filename,
        )
    def _scoped_positions_loader(strategy_id_inner: str, variant_id_inner: str) -> pd.DataFrame:
        return original_positions_loader(strategy_id_inner, variant_id_inner, layer=layer)

    globals()["load_trade_lifecycle_df"] = _scoped_lifecycle_loader
    globals()["load_positions_df"] = _scoped_positions_loader
    try:
        actions_df = build_actions_df(strategy_id, variant_id)
    finally:
        globals()["load_trade_lifecycle_df"] = original_lifecycle_loader
        globals()["load_positions_df"] = original_positions_loader
    output_paths = save_actions_outputs(strategy_id, variant_id, actions_df, layer=layer)
    return {
        "strategy_id": strategy_id,
        "variant_id": variant_id,
        "layer": layer,
        "rows": len(actions_df),
        "output_paths": output_paths,
    }


def main() -> None:
    args = parse_args()
    result = run_build(args.strategy_id, args.variant_id)
    print(f"Strategy: {result['strategy_id']}")
    print(f"Variant: {result['variant_id']}")
    print(f"Rows: {result['rows']}")
    print(f"portfolio_actions_daily.csv written: {len(result['output_paths'])} files")


if __name__ == "__main__":
    main()
