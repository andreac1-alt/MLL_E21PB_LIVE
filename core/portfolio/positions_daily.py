from __future__ import annotations

import argparse
from functools import lru_cache
from pathlib import Path

import pandas as pd

from core.io.archive_utils import resolve_archive_file
from core.market.semaforo import compute_market_street_light_for_date
from core.portfolio.ids import build_portfolio_id, build_position_id
from core.portfolio.paths import get_portfolio_full_dir, get_portfolio_yearly_dir, get_trade_lifecycle_filename
from core.portfolio.schema import PORTFOLIO_POSITIONS_DAILY_COLUMNS


BASE_DIR = Path(__file__).resolve().parents[2]
TRADE_TIMELINE_VARIANT_ID = "trade_timeline"
DEFAULT_TRADE_LIFECYCLE_FILENAME = get_trade_lifecycle_filename("live")
STORICO_SPY_PATH = BASE_DIR / "output" / "storico_SPY.csv"
INVESTED_ONLY_MOMENTUM_DAILY_PATH = (
    BASE_DIR
    / "output"
    / "analysis"
    / "PORTFOLIO"
    / "invested_only_momentum"
    / "experimental_invested_only_sizing_ema21_sma50_blue_ex_monday_keep_blue_on_monday_2020_2026-04-17_daily.csv"
)
BREADTH_DAILY_PATH = BASE_DIR / "output" / "breadth" / "history" / "universe_breadth_daily.csv"
ETF_CONTEXT_TRADES_PATH = (
    BASE_DIR
    / "output"
    / "analysis"
    / "ETF_CONTEXT"
    / "pnl"
    / "etf_context_pnl_ema21_sma50_blue_ex_monday_keep_blue_on_monday_1999_2026_trades.csv"
)
NO_CARRY_IN_2026_START = pd.Timestamp("2026-01-01")

SUPPORTED_VARIANTS = {
    "blue_ex_monday_keep_blue_on_monday": (
        "Take BLUE trades only; exclude Monday entries unless the source target date is BLUE ON."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_10": (
        "Same BLUE weekday filter, with 1.10x R sizing when source EMA21 slope pct 5 is > 0.30."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_25": (
        "Same BLUE weekday filter, with 1.25x R sizing when source EMA21 slope pct 5 is > 0.30."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_25": (
        "Same BLUE weekday filter, with 1.25x R sizing when source EMA21 slope pct 5 is > 0.45."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_10_then_momentum_top_half_x1_25": (
        "Same BLUE weekday filter, with 1.10x R sizing when source EMA21 slope pct 5 is > 0.45 and 1.25x when the same trade also has previous-close invested-only momentum 5d in the top half."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45 and +0.25x if previous-close invested-only momentum 5d is in the top half."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45 and +0.15x if previous-close invested-only momentum 5d is in the top half."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45, +0.15x if previous-close invested-only momentum 5d is in the top half, +0.25x if source %SMA20 is below 40.64, and +0.50x total breadth add if source %SMA20 is below 35.45."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45, +0.15x if previous-close invested-only momentum 5d is in the top half, +0.25x if source %SMA20 is below 40.64, and +0.50x total breadth add if source %SMA20 is below 35.45, then multiply by 1.25x when ETF context is recommended and 0.50x otherwise."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45, +0.25x if previous-close invested-only momentum 5d is in the top half, +0.25x if source %SMA20 is below 40.64, and +0.50x total breadth add if source %SMA20 is below 35.45, then multiply by 1.25x when ETF context is recommended and 0.50x otherwise."
    ),
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_etf_mult_1_25_0_50": (
        "Same BLUE weekday filter, with additive sizing: +0.25x if source EMA21 slope pct 5 is > 0.45, +0.25x if previous-close invested-only momentum 5d is in the top half, then multiply by 1.25x when ETF context is recommended and 0.50x otherwise."
    ),
    "green_always": (
        "Take GREEN trades only with no weekday exclusions."
    ),
    "yellow_always": (
        "Take YELLOW trades only with no weekday exclusions."
    ),
    "red_always_top10": (
        "Take RED trades only with no weekday exclusions."
    ),
    "all_colors_always": (
        "Take trades from all semaphore colors with no weekday exclusions."
    ),
    "blue_ex_monday_keep_blue_on_monday_2026_no_carry_in": (
        "Same BLUE rule set, but keep only positions with bd >= 2026-01-01."
    ),
    "portfolio_live_blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50_2026_no_carry_in": (
        "Same official BLUE_M sizing logic, but keep only positions with bd >= 2026-01-01 for the live reminder layer."
    ),
}

SLOPE_SIZING_VARIANTS = {
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_10": {
        "threshold": 0.30,
        "multiplier": 1.10,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_25": {
        "threshold": 0.30,
        "multiplier": 1.25,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_25": {
        "threshold": 0.45,
        "multiplier": 1.25,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_10_then_momentum_top_half_x1_25": {
        "threshold": 0.45,
        "slope_only_multiplier": 1.10,
        "slope_plus_momentum_multiplier": 1.25,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.25,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.15,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.15,
        "breadth_metric": "above_sma20_pct",
        "breadth_p20_threshold": 40.64,
        "breadth_p10_threshold": 35.45,
        "breadth_p20_add": 0.25,
        "breadth_p10_add": 0.50,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.15,
        "breadth_metric": "above_sma20_pct",
        "breadth_p20_threshold": 40.64,
        "breadth_p10_threshold": 35.45,
        "breadth_p20_add": 0.25,
        "breadth_p10_add": 0.50,
        "etf_pass_multiplier": 1.25,
        "etf_fail_multiplier": 0.50,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.25,
        "breadth_metric": "above_sma20_pct",
        "breadth_p20_threshold": 40.64,
        "breadth_p10_threshold": 35.45,
        "breadth_p20_add": 0.25,
        "breadth_p10_add": 0.50,
        "etf_pass_multiplier": 1.25,
        "etf_fail_multiplier": 0.50,
    },
    "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_etf_mult_1_25_0_50": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.25,
        "etf_pass_multiplier": 1.25,
        "etf_fail_multiplier": 0.50,
    },
    "portfolio_live_blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50_2026_no_carry_in": {
        "threshold": 0.45,
        "slope_add": 0.25,
        "momentum_add": 0.25,
        "breadth_metric": "above_sma20_pct",
        "breadth_p20_threshold": 40.64,
        "breadth_p10_threshold": 35.45,
        "breadth_p20_add": 0.25,
        "breadth_p10_add": 0.50,
        "etf_pass_multiplier": 1.25,
        "etf_fail_multiplier": 0.50,
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build portfolio_positions_daily.csv from trade reconstruction outputs."
    )
    parser.add_argument("--strategy-id", required=True, help="Strategy id, for example EMA21_SMA50.")
    parser.add_argument(
        "--variant-id",
        required=True,
        choices=sorted(SUPPORTED_VARIANTS.keys()),
        help="Portfolio variant rule set to apply.",
    )
    return parser.parse_args()


def load_trade_lifecycle_df(
    strategy_id: str,
    *,
    layer: str = "live",
    lifecycle_filename: str = DEFAULT_TRADE_LIFECYCLE_FILENAME,
) -> pd.DataFrame:
    path = get_portfolio_full_dir(strategy_id, TRADE_TIMELINE_VARIANT_ID, layer=layer) / lifecycle_filename
    if not path.exists():
        raise FileNotFoundError(f"Trade lifecycle non trovato: {path}")
    df = pd.read_csv(path)
    if df.empty:
        return df
    if "source_screen_date" not in df.columns and "source_target_date" in df.columns:
        df["source_screen_date"] = df["source_target_date"]
    for col in ["date", "source_screen_date"]:
        df[col] = pd.to_datetime(df[col], errors="coerce").dt.normalize()
    bd_source_col = "bd" if "bd" in df.columns else "entry_date" if "entry_date" in df.columns else None
    if bd_source_col is None:
        raise KeyError("Trade lifecycle privo di colonna BD/entry_date.")
    df["bd"] = pd.to_datetime(df[bd_source_col], errors="coerce").dt.normalize()
    bool_cols = ["is_entry_day", "is_exit_day"]
    for col in bool_cols:
        df[col] = df[col].fillna(False).astype(bool)
    numeric_cols = [
        "trade_index",
        "risk_amount",
        "total_entry_shares",
        "shares_open_end",
        "realized_pnl_cum",
        "unrealized_r_close",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["date", "bd", "source_screen_date", "ticker"]).copy()


def load_storico_spy_df() -> pd.DataFrame:
    if not STORICO_SPY_PATH.exists():
        return pd.DataFrame(columns=["date", "market_street_light", "blue_on"])
    storico_df = pd.read_csv(STORICO_SPY_PATH, usecols=["date", "market_street_light", "blue_on"])
    storico_df["date"] = pd.to_datetime(storico_df["date"], errors="coerce").dt.normalize()
    storico_df["market_street_light"] = storico_df["market_street_light"].astype(str).str.strip().str.upper()
    storico_df["blue_on"] = storico_df["blue_on"].fillna(False).astype(bool)
    return storico_df.dropna(subset=["date"]).copy()


@lru_cache(maxsize=None)
def resolve_blue_on_for_source_screen_date(source_screen_date_str: str) -> bool:
    source_screen_date = pd.Timestamp(source_screen_date_str).normalize()
    semaphore = compute_market_street_light_for_date(source_screen_date)
    return bool(getattr(semaphore, "blue_on", False))


@lru_cache(maxsize=None)
def load_second_screen_passed_df(screen_date_str: str) -> pd.DataFrame:
    screen_date = pd.Timestamp(screen_date_str).normalize()
    stamp = screen_date.strftime("%Y%m%d")
    second_path = resolve_archive_file(screen_date, f"second_screen_passed_{stamp}.csv")
    if not second_path.exists():
        return pd.DataFrame(columns=["ticker", "ema21_slope_pct_5"])
    df = pd.read_csv(second_path, usecols=["ticker", "ema21_slope_pct_5"])
    if df.empty:
        return pd.DataFrame(columns=["ticker", "ema21_slope_pct_5"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["ema21_slope_pct_5"] = pd.to_numeric(df["ema21_slope_pct_5"], errors="coerce")
    return df.dropna(subset=["ticker"]).drop_duplicates(subset=["ticker"], keep="first").copy()


def build_entry_slope_df(entry_rows: pd.DataFrame) -> pd.DataFrame:
    unique_dates = (
        entry_rows["source_screen_date"]
        .dropna()
        .drop_duplicates()
        .sort_values()
    )
    rows: list[pd.DataFrame] = []
    for source_screen_date in unique_dates:
        second_df = load_second_screen_passed_df(source_screen_date.strftime("%Y-%m-%d")).copy()
        if second_df.empty:
            continue
        second_df["source_screen_date"] = pd.Timestamp(source_screen_date).normalize()
        rows.append(second_df[["source_screen_date", "ticker", "ema21_slope_pct_5"]])
    if not rows:
        return pd.DataFrame(columns=["source_screen_date", "ticker", "ema21_slope_pct_5"])
    return pd.concat(rows, ignore_index=True)


@lru_cache(maxsize=1)
def load_invested_only_momentum_signal_df() -> pd.DataFrame:
    if not INVESTED_ONLY_MOMENTUM_DAILY_PATH.exists():
        return pd.DataFrame(columns=["bd", "prev_momentum_5d_top_half"])
    df = pd.read_csv(
        INVESTED_ONLY_MOMENTUM_DAILY_PATH,
        usecols=["date", "sizing_rule", "prev_momentum_5d_top_half"],
    )
    if df.empty:
        return pd.DataFrame(columns=["bd", "prev_momentum_5d_top_half"])
    df = df[df["sizing_rule"].astype(str) == "baseline"].copy()
    df["bd"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["prev_momentum_5d_top_half"] = df["prev_momentum_5d_top_half"].fillna(False).astype(bool)
    return df.dropna(subset=["bd"])[["bd", "prev_momentum_5d_top_half"]].drop_duplicates(
        subset=["bd"],
        keep="last",
    )


@lru_cache(maxsize=1)
def load_breadth_signal_df() -> pd.DataFrame:
    if not BREADTH_DAILY_PATH.exists():
        return pd.DataFrame(columns=["source_screen_date", "above_sma5_pct", "above_sma20_pct"])
    df = pd.read_csv(BREADTH_DAILY_PATH, usecols=["date", "above_sma5_pct", "above_sma20_pct"])
    if df.empty:
        return pd.DataFrame(columns=["source_screen_date", "above_sma5_pct", "above_sma20_pct"])
    df["source_screen_date"] = pd.to_datetime(df["date"], errors="coerce").dt.normalize()
    df["above_sma5_pct"] = pd.to_numeric(df["above_sma5_pct"], errors="coerce")
    df["above_sma20_pct"] = pd.to_numeric(df["above_sma20_pct"], errors="coerce")
    return df.dropna(subset=["source_screen_date"])[
        ["source_screen_date", "above_sma5_pct", "above_sma20_pct"]
    ].drop_duplicates(
        subset=["source_screen_date"],
        keep="last",
    )


@lru_cache(maxsize=1)
def load_etf_context_signal_df() -> pd.DataFrame:
    if not ETF_CONTEXT_TRADES_PATH.exists():
        return pd.DataFrame(columns=["ticker", "bd", "etf_recommended"])
    df = pd.read_csv(
        ETF_CONTEXT_TRADES_PATH,
        usecols=["ticker", "requested_buy_date", "recommended"],
    )
    if df.empty:
        return pd.DataFrame(columns=["ticker", "bd", "etf_recommended"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["bd"] = pd.to_datetime(df["requested_buy_date"], errors="coerce").dt.normalize()
    df["etf_recommended"] = df["recommended"].astype(str).str.strip().str.lower().eq("recommended")
    return df.dropna(subset=["ticker", "bd"])[
        ["ticker", "bd", "etf_recommended"]
    ].drop_duplicates(
        subset=["ticker", "bd"],
        keep="last",
    )


def apply_breadth_additive_sizing(result: pd.DataFrame, config: dict) -> pd.DataFrame:
    metric = config.get("breadth_metric")
    if not metric:
        return result

    breadth_df = load_breadth_signal_df()
    if breadth_df.empty or metric not in breadth_df.columns:
        return result

    result = result.merge(breadth_df[["source_screen_date", metric]], on="source_screen_date", how="left")
    result[metric] = pd.to_numeric(result[metric], errors="coerce")
    p20_threshold = float(config["breadth_p20_threshold"])
    p10_threshold = float(config["breadth_p10_threshold"])
    p20_add = float(config["breadth_p20_add"])
    p10_add = float(config["breadth_p10_add"])

    p20_mask = result[metric] < p20_threshold
    p10_mask = result[metric] < p10_threshold
    result.loc[p20_mask, "r_multiplier"] = (
        result.loc[p20_mask, "r_multiplier"].astype(float) + p20_add
    )
    result.loc[p10_mask, "r_multiplier"] = (
        result.loc[p10_mask, "r_multiplier"].astype(float) - p20_add + p10_add
    )
    return result


def assign_r_multiplier(entry_rows: pd.DataFrame, variant_id: str) -> pd.DataFrame:
    result = entry_rows.copy()
    result["source_screen_date"] = pd.to_datetime(result["source_screen_date"], errors="coerce").dt.normalize()
    result["bd"] = pd.to_datetime(result["bd"], errors="coerce").dt.normalize()
    result["r_multiplier"] = 1.0
    if variant_id not in SLOPE_SIZING_VARIANTS:
        return result

    slope_df = build_entry_slope_df(result)
    slope_df["source_screen_date"] = pd.to_datetime(slope_df["source_screen_date"], errors="coerce").dt.normalize()
    result = result.merge(slope_df, on=["source_screen_date", "ticker"], how="left")
    result["ema21_slope_pct_5"] = pd.to_numeric(result["ema21_slope_pct_5"], errors="coerce")

    config = SLOPE_SIZING_VARIANTS[variant_id]
    slope_mask = result["ema21_slope_pct_5"] > float(config["threshold"])

    if "multiplier" in config:
        result.loc[slope_mask, "r_multiplier"] = float(config["multiplier"])
        return result

    momentum_df = load_invested_only_momentum_signal_df()
    momentum_df["bd"] = pd.to_datetime(momentum_df["bd"], errors="coerce").dt.normalize()
    result = result.merge(momentum_df, on="bd", how="left")
    result["prev_momentum_5d_top_half"] = result["prev_momentum_5d_top_half"].fillna(False).astype(bool)

    if "slope_add" in config:
        result.loc[slope_mask, "r_multiplier"] = (
            result.loc[slope_mask, "r_multiplier"].astype(float) + float(config["slope_add"])
        )
        momentum_mask = result["prev_momentum_5d_top_half"]
        result.loc[momentum_mask, "r_multiplier"] = (
            result.loc[momentum_mask, "r_multiplier"].astype(float) + float(config["momentum_add"])
        )
        result = apply_breadth_additive_sizing(result, config)
        if "etf_pass_multiplier" in config and "etf_fail_multiplier" in config:
            etf_df = load_etf_context_signal_df()
            etf_df["bd"] = pd.to_datetime(etf_df["bd"], errors="coerce").dt.normalize()
            result = result.merge(etf_df, on=["ticker", "bd"], how="left")
            etf_recommended = result["etf_recommended"]
            result["etf_recommended"] = etf_recommended.where(etf_recommended.notna(), False).astype(bool)
            result.loc[result["etf_recommended"], "r_multiplier"] = (
                result.loc[result["etf_recommended"], "r_multiplier"].astype(float)
                * float(config["etf_pass_multiplier"])
            )
            result.loc[~result["etf_recommended"], "r_multiplier"] = (
                result.loc[~result["etf_recommended"], "r_multiplier"].astype(float)
                * float(config["etf_fail_multiplier"])
            )
        return result

    result.loc[slope_mask, "r_multiplier"] = float(config["slope_only_multiplier"])
    result.loc[
        slope_mask & result["prev_momentum_5d_top_half"],
        "r_multiplier",
    ] = float(config["slope_plus_momentum_multiplier"])
    return result


def build_position_index(lifecycle_df: pd.DataFrame) -> pd.DataFrame:
    entry_rows = lifecycle_df[lifecycle_df["is_entry_day"]].copy()
    entry_rows = entry_rows.sort_values(["ticker", "bd", "source_screen_date", "trade_index", "source_event_id"])
    entry_rows["entry_seq"] = (
        entry_rows.groupby(["ticker", "bd"]).cumcount() + 1
    )
    return entry_rows[
        [
            "source_event_id",
            "ticker",
            "bd",
            "entry_seq",
            "total_entry_shares",
        ]
    ].drop_duplicates(subset=["source_event_id", "ticker"]).rename(
        columns={"total_entry_shares": "shares_initial"}
    )


def filter_variant_positions(lifecycle_df: pd.DataFrame, variant_id: str) -> pd.DataFrame:
    base_variants = {
        "blue_ex_monday_keep_blue_on_monday",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_10",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_30_x1_25",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_25",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_x1_10_then_momentum_top_half_x1_25",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_15_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50",
        "blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_etf_mult_1_25_0_50",
        "green_always",
        "yellow_always",
        "red_always_top10",
        "all_colors_always",
        "blue_ex_monday_keep_blue_on_monday_2026_no_carry_in",
        "portfolio_live_blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50_2026_no_carry_in",
    }
    if variant_id not in base_variants:
        raise ValueError(f"Variant non supportata: {variant_id}")

    entry_rows = lifecycle_df[lifecycle_df["is_entry_day"]].copy()
    storico_df = load_storico_spy_df().rename(
        columns={
            "date": "source_screen_date",
            "market_street_light": "source_market_street_light",
            "blue_on": "source_blue_on",
        }
    )
    entry_rows = entry_rows.merge(storico_df, on="source_screen_date", how="left")
    entry_rows["source_market_street_light"] = (
        entry_rows["source_market_street_light"].astype(str).str.strip().str.upper()
    )
    missing_blue_on_mask = entry_rows["source_blue_on"].isna()
    if missing_blue_on_mask.any():
        entry_rows.loc[missing_blue_on_mask, "source_blue_on"] = entry_rows.loc[
            missing_blue_on_mask, "source_screen_date"
        ].dt.strftime("%Y-%m-%d").map(resolve_blue_on_for_source_screen_date)
    entry_rows["source_blue_on"] = entry_rows["source_blue_on"].fillna(False).astype(bool)
    entry_rows["entry_weekday"] = entry_rows["bd"].dt.day_name().str.upper()
    entry_rows["semaforo_color_source"] = entry_rows["semaforo_color_source"].astype(str).str.strip().str.upper()

    if variant_id == "green_always":
        filtered = entry_rows.loc[
            entry_rows["semaforo_color_source"] == "GREEN",
            ["source_event_id", "ticker", "bd", "source_screen_date"],
        ].copy()
    elif variant_id == "yellow_always":
        filtered = entry_rows.loc[
            entry_rows["semaforo_color_source"] == "YELLOW",
            ["source_event_id", "ticker", "bd", "source_screen_date"],
        ].copy()
    elif variant_id == "red_always_top10":
        filtered = entry_rows.loc[
            entry_rows["semaforo_color_source"] == "RED",
            ["source_event_id", "ticker", "bd", "source_screen_date"],
        ].copy()
    elif variant_id == "all_colors_always":
        filtered = entry_rows.loc[
            entry_rows["semaforo_color_source"].isin(["BLUE", "GREEN", "YELLOW", "RED"]),
            ["source_event_id", "ticker", "bd", "source_screen_date"],
        ].copy()
    else:
        is_blue_source = entry_rows["semaforo_color_source"] == "BLUE"
        is_non_monday_entry = entry_rows["entry_weekday"] != "MONDAY"
        keep_monday_entry = entry_rows["entry_weekday"].eq("MONDAY") & entry_rows["source_blue_on"]

        filtered = entry_rows.loc[
            is_blue_source & (is_non_monday_entry | keep_monday_entry),
            ["source_event_id", "ticker", "bd", "source_screen_date"],
        ].copy()

    bds = (
        entry_rows[["source_event_id", "ticker", "bd"]]
        .drop_duplicates(subset=["source_event_id", "ticker"])
        .copy()
    )
    filtered = filtered.merge(bds, on=["source_event_id", "ticker"], how="left")

    if variant_id in {
        "blue_ex_monday_keep_blue_on_monday_2026_no_carry_in",
        "portfolio_live_blue_ex_monday_keep_blue_on_monday_slope_gt_0_45_plus_0_25_momentum_top_half_plus_0_25_sma20_p20_p10_plus_0_25_0_50_etf_mult_1_25_0_50_2026_no_carry_in",
    }:
        filtered = filtered.loc[filtered["bd"] >= NO_CARRY_IN_2026_START].copy()

    filtered = assign_r_multiplier(filtered, variant_id)
    return filtered[["source_event_id", "ticker", "r_multiplier"]]


def build_positions_df(strategy_id: str, variant_id: str) -> pd.DataFrame:
    lifecycle_df = load_trade_lifecycle_df(strategy_id)
    if lifecycle_df.empty:
        return pd.DataFrame(columns=PORTFOLIO_POSITIONS_DAILY_COLUMNS)

    selected_positions = filter_variant_positions(lifecycle_df, variant_id)
    if selected_positions.empty:
        return pd.DataFrame(columns=PORTFOLIO_POSITIONS_DAILY_COLUMNS)

    filtered_df = lifecycle_df.merge(selected_positions, on=["source_event_id", "ticker"], how="inner")
    position_index = build_position_index(filtered_df)
    filtered_df = filtered_df.merge(position_index, on=["source_event_id", "ticker", "bd"], how="left")

    portfolio_id = build_portfolio_id(strategy_id, variant_id)
    filtered_df = filtered_df.sort_values(["bd", "ticker", "date", "trade_index"]).reset_index(drop=True)
    filtered_df["bd_yyyymmdd"] = filtered_df["bd"].dt.strftime("%Y%m%d")
    filtered_df["position_id"] = filtered_df.apply(
        lambda row: build_position_id(
            strategy_id,
            variant_id,
            str(row["ticker"]),
            str(row["bd_yyyymmdd"]),
            int(row["entry_seq"]),
        ),
        axis=1,
    )

    filtered_df["days_in_trade"] = filtered_df.groupby("position_id").cumcount() + 1
    filtered_df["realized_r_partial"] = (
        filtered_df["realized_pnl_cum"].astype(float) / filtered_df["risk_amount"].astype(float)
    ).round(4)
    filtered_df["r_multiplier"] = pd.to_numeric(filtered_df["r_multiplier"], errors="coerce").fillna(1.0)
    filtered_df["realized_r_partial"] = (
        filtered_df["realized_r_partial"].astype(float) * filtered_df["r_multiplier"].astype(float)
    ).round(4)
    filtered_df["unrealized_r"] = (
        filtered_df["unrealized_r_close"].astype(float) * filtered_df["r_multiplier"].astype(float)
    ).round(4)
    filtered_df["total_mtm_r"] = (
        filtered_df["realized_r_partial"].astype(float) + filtered_df["unrealized_r"].astype(float)
    ).round(4)
    filtered_df["is_open"] = filtered_df["shares_open_end"].fillna(0).astype(int) > 0
    filtered_df["carry_in_from_prev_year"] = filtered_df["bd"].dt.year < filtered_df["date"].dt.year
    last_position_year = filtered_df.groupby("position_id")["date"].transform("max").dt.year
    filtered_df["carry_out_to_next_year"] = last_position_year > filtered_df["date"].dt.year

    positions_df = pd.DataFrame(
        {
            "date": filtered_df["date"].dt.strftime("%Y-%m-%d"),
            "portfolio_id": portfolio_id,
            "strategy_id": strategy_id,
            "variant_id": variant_id,
            "position_id": filtered_df["position_id"],
            "ticker": filtered_df["ticker"].astype(str).str.upper(),
            "bd": filtered_df["bd"].dt.strftime("%Y-%m-%d"),
            "entry_seq": filtered_df["entry_seq"].astype(int),
            "r_multiplier": filtered_df["r_multiplier"].astype(float).round(4),
            "days_in_trade": filtered_df["days_in_trade"].astype(int),
            "shares_initial": filtered_df["shares_initial"].fillna(0).astype(int),
            "shares_open": filtered_df["shares_open_end"].fillna(0).astype(int),
            "realized_r_partial": filtered_df["realized_r_partial"].astype(float).round(4),
            "unrealized_r": filtered_df["unrealized_r"].astype(float).round(4),
            "total_mtm_r": filtered_df["total_mtm_r"].astype(float).round(4),
            "is_open": filtered_df["is_open"].astype(bool),
            "position_status": filtered_df["position_status"].astype(str),
            "opened_today": filtered_df["is_entry_day"].astype(bool),
            "closed_today": filtered_df["is_exit_day"].astype(bool),
            "carry_in_from_prev_year": filtered_df["carry_in_from_prev_year"].astype(bool),
            "carry_out_to_next_year": filtered_df["carry_out_to_next_year"].astype(bool),
        }
    )
    return positions_df[PORTFOLIO_POSITIONS_DAILY_COLUMNS].copy()


def save_positions_outputs(
    strategy_id: str,
    variant_id: str,
    positions_df: pd.DataFrame,
    *,
    layer: str = "live",
) -> list[Path]:
    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []

    full_path = full_dir / "portfolio_positions_daily.csv"
    positions_df.sort_values(["date", "ticker", "bd", "entry_seq"]).to_csv(full_path, index=False)
    output_paths.append(full_path)

    temp_df = positions_df.copy()
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.normalize()
    temp_df["year"] = temp_df["date"].dt.year
    for year, group in temp_df.groupby("year", dropna=False):
        year_int = int(year)
        year_dir = get_portfolio_yearly_dir(year_int, strategy_id, variant_id, layer=layer)
        year_dir.mkdir(parents=True, exist_ok=True)
        output_path = year_dir / "portfolio_positions_daily.csv"
        group.drop(columns=["year"]).sort_values(["date", "ticker", "bd", "entry_seq"]).to_csv(
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
    original_loader = load_trade_lifecycle_df
    def _scoped_loader(strategy_id_inner: str) -> pd.DataFrame:
        return original_loader(
            strategy_id_inner,
            layer=layer,
            lifecycle_filename=lifecycle_filename,
        )

    globals()["load_trade_lifecycle_df"] = _scoped_loader
    try:
        positions_df = build_positions_df(strategy_id, variant_id)
    finally:
        globals()["load_trade_lifecycle_df"] = original_loader
    output_paths = save_positions_outputs(strategy_id, variant_id, positions_df, layer=layer)
    return {
        "strategy_id": strategy_id,
        "variant_id": variant_id,
        "layer": layer,
        "rows": len(positions_df),
        "positions": positions_df["position_id"].nunique() if not positions_df.empty else 0,
        "output_paths": output_paths,
    }


def main() -> None:
    args = parse_args()
    result = run_build(args.strategy_id, args.variant_id)
    print(f"Strategy: {result['strategy_id']}")
    print(f"Variant: {result['variant_id']}")
    print(f"Rows: {result['rows']}")
    print(f"Unique positions: {result['positions']}")
    print(f"portfolio_positions_daily.csv written: {len(result['output_paths'])} files")


if __name__ == "__main__":
    main()
