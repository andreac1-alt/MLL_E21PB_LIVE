from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from core.analysis.trade_lifecycle import (
    build_portfolio_state_df,
    load_progress_df,
    run_build as run_trade_lifecycle_build,
    save_split_by_year,
    save_trade_lifecycle_outputs,
)
from core.portfolio.actions_daily import run_build as run_actions_build
from core.portfolio.paths import get_trade_lifecycle_filename
from core.portfolio.positions_daily import run_build as run_positions_build
from core.portfolio.state_daily import run_build as run_state_build


DEFAULT_STRATEGY_ID = "EMA21_SMA50"
DEFAULT_VARIANT_ID = (
    "portfolio_live_blue_ex_monday_keep_blue_on_monday_"
    "slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_"
    "sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50_2026_no_carry_in"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Costruisce il portfolio live giorno per giorno a partire dal trading_day_progress."
        )
    )
    parser.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    parser.add_argument("--variant-id", default=DEFAULT_VARIANT_ID)
    parser.add_argument("--start-year", type=int, default=2026)
    parser.add_argument("--through-buy-date", default="", help="Limite BD in formato YYYY-MM-DD.")
    parser.add_argument("--layer", default="live", choices=["live", "frozen"])
    return parser.parse_args()


def _load_full_lifecycle_path(result: dict[str, object], layer: str) -> Path:
    lifecycle_filename = get_trade_lifecycle_filename(layer)
    for path in result["lifecycle_paths"]:
        if path.name == lifecycle_filename and "/full/" in str(path):
            return Path(path)
    raise FileNotFoundError("File full trade_lifecycle non trovato tra gli output del rebuild.")


def _rebuild_outputs_for_filtered_lifecycle(
    lifecycle_df: pd.DataFrame,
    *,
    strategy_id: str,
    variant_id: str,
    layer: str,
) -> dict[str, object]:
    save_trade_lifecycle_outputs(lifecycle_df, strategy_id, layer=layer)
    trade_timeline_state_df = (
        build_portfolio_state_df(lifecycle_df, strategy_id) if not lifecycle_df.empty else pd.DataFrame()
    )
    save_split_by_year(trade_timeline_state_df, strategy_id, "portfolio_state_daily.csv")

    lifecycle_filename = get_trade_lifecycle_filename(layer)
    positions_result = run_positions_build(
        strategy_id,
        variant_id,
        layer=layer,
        lifecycle_filename=lifecycle_filename,
    )
    actions_result = run_actions_build(
        strategy_id,
        variant_id,
        layer=layer,
        lifecycle_filename=lifecycle_filename,
    )
    state_result = run_state_build(
        strategy_id,
        variant_id,
        layer=layer,
    )
    return {
        "trade_timeline_state_rows": len(trade_timeline_state_df),
        "positions_rows": positions_result["rows"],
        "actions_rows": actions_result["rows"],
        "state_rows": state_result["rows"],
    }


def build_portfolio_live_day_by_day(
    *,
    strategy_id: str,
    variant_id: str,
    start_year: int,
    through_buy_date: pd.Timestamp | None = None,
    layer: str = "live",
) -> dict[str, object]:
    progress_df = load_progress_df(strategy_id, start_year, pd.Timestamp.today().year)
    if progress_df.empty:
        raise FileNotFoundError("Nessun trading_day progress disponibile per il range richiesto.")

    progress_df = progress_df.sort_values(["buy_date", "target_date"]).reset_index(drop=True)
    if through_buy_date is not None:
        limit = pd.Timestamp(through_buy_date).normalize()
        progress_df = progress_df.loc[progress_df["buy_date"] <= limit].copy()
    if progress_df.empty:
        raise ValueError("Nessuna buy date disponibile dopo l'applicazione del filtro richiesto.")

    lifecycle_result = run_trade_lifecycle_build(
        strategy_id,
        start_year,
        int(progress_df["buy_date"].dt.year.max()),
        layer=layer,
    )
    lifecycle_path = _load_full_lifecycle_path(lifecycle_result, layer)
    lifecycle_df = pd.read_csv(lifecycle_path)
    if lifecycle_df.empty:
        filtered_lifecycle_df = lifecycle_df.copy()
    else:
        lifecycle_df["date"] = pd.to_datetime(lifecycle_df["date"], errors="coerce").dt.normalize()
        lifecycle_df["requested_buy_date"] = pd.to_datetime(
            lifecycle_df["requested_buy_date"], errors="coerce"
        ).dt.normalize()
        allowed_buy_dates = set(pd.to_datetime(progress_df["buy_date"], errors="coerce").dropna().tolist())
        filtered_lifecycle_df = lifecycle_df.loc[
            lifecycle_df["requested_buy_date"].isin(allowed_buy_dates)
        ].copy()

    day_results: list[dict[str, object]] = []
    buy_dates = sorted(progress_df["buy_date"].dropna().drop_duplicates())
    current_lifecycle_df = filtered_lifecycle_df.iloc[0:0].copy()

    for buy_date in buy_dates:
        buy_date = pd.Timestamp(buy_date).normalize()
        day_slice = filtered_lifecycle_df.loc[filtered_lifecycle_df["date"] <= buy_date].copy()
        current_lifecycle_df = day_slice
        rebuild_result = _rebuild_outputs_for_filtered_lifecycle(
            current_lifecycle_df,
            strategy_id=strategy_id,
            variant_id=variant_id,
            layer=layer,
        )
        day_results.append(
            {
                "buy_date": buy_date.strftime("%Y-%m-%d"),
                "lifecycle_rows": len(current_lifecycle_df),
                **rebuild_result,
            }
        )

    final_buy_date = pd.Timestamp(buy_dates[-1]).normalize()
    return {
        "strategy_id": strategy_id,
        "variant_id": variant_id,
        "layer": layer,
        "days_processed": len(day_results),
        "final_buy_date": final_buy_date,
        "lifecycle_rows": len(current_lifecycle_df),
        "day_results": day_results,
    }


def main() -> None:
    args = parse_args()
    through_buy_date = pd.Timestamp(args.through_buy_date).normalize() if args.through_buy_date else None
    result = build_portfolio_live_day_by_day(
        strategy_id=args.strategy_id,
        variant_id=args.variant_id,
        start_year=args.start_year,
        through_buy_date=through_buy_date,
        layer=args.layer,
    )
    print(f"Strategy: {result['strategy_id']}")
    print(f"Variant: {result['variant_id']}")
    print(f"Layer: {result['layer']}")
    print(f"Days processed: {result['days_processed']}")
    print(f"Final buy date: {result['final_buy_date'].strftime('%Y-%m-%d')}")
    print(f"Final lifecycle rows: {result['lifecycle_rows']}")


if __name__ == "__main__":
    main()
