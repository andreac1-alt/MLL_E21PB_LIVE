from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from core.portfolio.ids import build_portfolio_id, build_position_id
from core.portfolio.etf_context import ensure_etf_context_for_sd
from core.portfolio.paths import get_portfolio_full_dir, get_portfolio_yearly_dir
from core.portfolio.momentum_signal import load_momentum_signal, save_momentum_signal
from core.portfolio.schema import (
    PORTFOLIO_ACTIONS_DAILY_COLUMNS,
    PORTFOLIO_POSITIONS_DAILY_COLUMNS,
    PORTFOLIO_STATE_DAILY_COLUMNS,
)
from core.trade_state import (
    TRADE_STATE_COLUMNS,
    load_previous_trade_state,
    normalize_trade_state_df,
    previous_market_session,
    save_trade_state,
    trade_state_dir_for_date,
    trade_state_path_for_date,
)
from tools.strategy_EMA21_SMA50 import FIRST_TARGET_R_MULTIPLE, MIN_SHARES, build_tranche_sizes, load_cached_price_history


DEFAULT_STRATEGY_ID = "EMA21_SMA50"
DEFAULT_VARIANT_ID = "portfolio_live_trade_state_2026_no_carry_in"
DEFAULT_BASE_RISK_AMOUNT = 25.0
SLOPE_THRESHOLD = 0.45
SLOPE_ADD = 0.25
MOMENTUM_ADD = 0.25
BREADTH_P20_ADD = 0.25
BREADTH_P10_ADD = 0.50
ETF_PASS_MULTIPLIER = 1.25
ETF_FAIL_MULTIPLIER = 0.50
ETF_NEUTRAL_MULTIPLIER = 1.00
BREADTH_HISTORY_PATH = Path("output") / "breadth" / "history" / "universe_breadth_daily.csv"
TRADE_SIZING_COLUMNS = [
    "trade_id",
    "ticker",
    "screen_date",
    "buy_date",
    "trade_status",
    "bd",
    "strategy_id",
    "variant_id",
    "risk_amount",
    "source_ema21_slope_pct_5",
    "mult_slope_pass",
    "mult_slope_add",
    "momentum_reference_date",
    "prev_momentum_5d_top_half",
    "momentum_bootstrap_neutral",
    "mult_momentum_add",
    "above_sma20_pct",
    "breadth_p20_threshold",
    "breadth_p10_threshold",
    "mult_breadth_p20_pass",
    "mult_breadth_p10_pass",
    "mult_breadth_add",
    "etf_recommended",
    "mult_etf_multiplier",
    "r_multiplier_pre_etf",
    "r_multiplier_final",
    "shares_initial",
    "sizing_scope",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Step 3 live: aggiorna positions/actions/state portfolio da trade_state."
    )
    parser.add_argument("--buy-date", required=True, help="BD in formato YYYY-MM-DD.")
    parser.add_argument("--strategy-id", default=DEFAULT_STRATEGY_ID)
    parser.add_argument("--variant-id", default=DEFAULT_VARIANT_ID)
    parser.add_argument("--risk-amount", type=float, default=DEFAULT_BASE_RISK_AMOUNT)
    parser.add_argument("--layer", default="live", choices=["live", "frozen"])
    return parser


def load_trade_state_for_bd(buy_date: pd.Timestamp) -> pd.DataFrame:
    path = trade_state_path_for_date(buy_date)
    if not path.exists():
        raise FileNotFoundError(f"Trade state non trovato per BD={buy_date.date()}: {path}")
    df = pd.read_csv(path, keep_default_na=False)
    return normalize_trade_state_df(df)


def load_existing_output(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    df = pd.read_csv(path, keep_default_na=False)
    df = df.reindex(columns=columns)
    date_column = "action_date" if "action_date" in df.columns else "date"
    if date_column in df.columns:
        valid_dates = pd.to_datetime(df[date_column], errors="coerce").notna()
        df = df.loc[valid_dates].copy()
    return df


def screening_passed_path_for_sd(screen_date: pd.Timestamp) -> Path:
    sd = pd.Timestamp(screen_date).normalize()
    return (
        Path("output")
        / "screening_day"
        / sd.strftime("%Y")
        / sd.strftime("%m")
        / sd.strftime("%Y%m%d")
        / f"second_screen_passed_{sd.strftime('%Y%m%d')}.csv"
    )


def load_second_screen_for_sd(screen_date: pd.Timestamp) -> pd.DataFrame:
    path = screening_passed_path_for_sd(screen_date)
    if not path.exists():
        return pd.DataFrame(columns=["ticker", "ema21_slope_pct_5"])
    df = pd.read_csv(path, keep_default_na=False, usecols=["ticker", "ema21_slope_pct_5"])
    if df.empty:
        return pd.DataFrame(columns=["ticker", "ema21_slope_pct_5"])
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["ema21_slope_pct_5"] = pd.to_numeric(df["ema21_slope_pct_5"], errors="coerce")
    return df.drop_duplicates(subset=["ticker"], keep="last")


def load_breadth_signal_df() -> pd.DataFrame:
    if not BREADTH_HISTORY_PATH.exists():
        return pd.DataFrame(columns=["screen_date", "above_sma20_pct", "above_sma20_pct_p20", "above_sma20_pct_p10"])
    df = pd.read_csv(
        BREADTH_HISTORY_PATH,
        usecols=["date", "above_sma20_pct", "above_sma20_pct_p20", "above_sma20_pct_p10"],
    )
    if df.empty:
        return pd.DataFrame(columns=["screen_date", "above_sma20_pct", "above_sma20_pct_p20", "above_sma20_pct_p10"])
    df = df.rename(columns={"date": "screen_date"})
    df["screen_date"] = pd.to_datetime(df["screen_date"], errors="coerce").dt.normalize()
    for col in ["above_sma20_pct", "above_sma20_pct_p20", "above_sma20_pct_p10"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["screen_date"]).drop_duplicates(subset=["screen_date"], keep="last")


def price_close_for_date(ticker: str, date: pd.Timestamp) -> float:
    history = load_cached_price_history(ticker)
    history["Date"] = pd.to_datetime(history["Date"], errors="coerce").dt.normalize()
    match = history.loc[history["Date"].eq(pd.Timestamp(date).normalize())]
    if match.empty:
        raise ValueError(f"Close non trovata per {ticker} su {pd.Timestamp(date).date()}.")
    return float(pd.to_numeric(match.iloc[-1]["Close"], errors="coerce"))


def price_high_for_date(ticker: str, date: pd.Timestamp) -> float:
    history = load_cached_price_history(ticker)
    history["Date"] = pd.to_datetime(history["Date"], errors="coerce").dt.normalize()
    match = history.loc[history["Date"].eq(pd.Timestamp(date).normalize())]
    if match.empty:
        raise ValueError(f"High non trovata per {ticker} su {pd.Timestamp(date).date()}.")
    return float(pd.to_numeric(match.iloc[-1]["High"], errors="coerce"))


def r_per_share(row: pd.Series) -> float:
    return float(row["entry_price"]) - float(row["initial_stop_loss"])


def realized_r_from_cash_pnl(row: pd.Series, sell_price: float, shares: int, risk_amount: float) -> float:
    if risk_amount <= 0 or shares <= 0:
        return 0.0
    cash_pnl = (float(sell_price) - float(row["entry_price"])) * shares
    return cash_pnl / risk_amount


def unrealized_r_from_cash_pnl(row: pd.Series, close_price: float, shares_open: int, risk_amount: float) -> float:
    if risk_amount <= 0 or shares_open <= 0:
        return 0.0
    cash_pnl = (float(close_price) - float(row["entry_price"])) * shares_open
    return cash_pnl / risk_amount


def previous_market_session_n(buy_date: pd.Timestamp, sessions_back: int) -> pd.Timestamp:
    current = pd.Timestamp(buy_date).normalize()
    for _ in range(sessions_back):
        current = previous_market_session(current)
    return current


def load_momentum_multiplier_by_bd(
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
    layer: str,
) -> tuple[float, dict[str, object]]:
    reference_date = previous_market_session_n(buy_date, 2)
    signal_df = load_momentum_signal(strategy_id, variant_id, layer=layer)
    if signal_df.empty:
        return 0.0, {
            "momentum_reference_date": reference_date.strftime("%Y-%m-%d"),
            "prev_momentum_5d_top_half": False,
            "momentum_bootstrap_neutral": False,
        }
    matched = signal_df.loc[signal_df["bd"].eq(reference_date)]
    if matched.empty:
        return 0.0, {
            "momentum_reference_date": reference_date.strftime("%Y-%m-%d"),
            "prev_momentum_5d_top_half": False,
            "momentum_bootstrap_neutral": False,
        }
    row = matched.iloc[-1]
    bootstrap_neutral = bool(row.get("bootstrap_neutral", False))
    top_half = bool(row.get("prev_momentum_5d_top_half", False)) and not bootstrap_neutral
    return (MOMENTUM_ADD if top_half else 0.0), {
        "momentum_reference_date": reference_date.strftime("%Y-%m-%d"),
        "prev_momentum_5d_top_half": top_half,
        "momentum_bootstrap_neutral": bootstrap_neutral,
    }


def load_slope_add_by_trade(current_df: pd.DataFrame) -> dict[str, float]:
    result: dict[str, float] = {}
    if current_df.empty:
        return result
    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for screen_date, group in working.dropna(subset=["screen_date"]).groupby("screen_date", dropna=False):
        second_df = load_second_screen_for_sd(pd.Timestamp(screen_date))
        slope_lookup = second_df.set_index("ticker")["ema21_slope_pct_5"].to_dict() if not second_df.empty else {}
        for _, row in group.iterrows():
            value = pd.to_numeric(slope_lookup.get(str(row["ticker"]).strip().upper()), errors="coerce")
            result[str(row["trade_id"])] = SLOPE_ADD if pd.notna(value) and float(value) > SLOPE_THRESHOLD else 0.0
    return result


def load_slope_value_by_trade(current_df: pd.DataFrame) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    if current_df.empty:
        return result
    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for screen_date, group in working.dropna(subset=["screen_date"]).groupby("screen_date", dropna=False):
        second_df = load_second_screen_for_sd(pd.Timestamp(screen_date))
        slope_lookup = second_df.set_index("ticker")["ema21_slope_pct_5"].to_dict() if not second_df.empty else {}
        for _, row in group.iterrows():
            value = pd.to_numeric(slope_lookup.get(str(row["ticker"]).strip().upper()), errors="coerce")
            result[str(row["trade_id"])] = float(value) if pd.notna(value) else None
    return result


def load_breadth_add_by_trade(current_df: pd.DataFrame) -> dict[str, float]:
    result: dict[str, float] = {}
    if current_df.empty:
        return result
    breadth_df = load_breadth_signal_df()
    if breadth_df.empty:
        return {str(row["trade_id"]): 0.0 for _, row in current_df.iterrows()}
    breadth_lookup = breadth_df.set_index("screen_date")
    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for _, row in working.iterrows():
        trade_id = str(row["trade_id"])
        screen_date = row["screen_date"]
        if pd.isna(screen_date) or screen_date not in breadth_lookup.index:
            result[trade_id] = 0.0
            continue
        breadth_row = breadth_lookup.loc[screen_date]
        above_sma20_pct = pd.to_numeric(breadth_row.get("above_sma20_pct"), errors="coerce")
        p20 = pd.to_numeric(breadth_row.get("above_sma20_pct_p20"), errors="coerce")
        p10 = pd.to_numeric(breadth_row.get("above_sma20_pct_p10"), errors="coerce")
        breadth_add = 0.0
        if pd.notna(above_sma20_pct) and pd.notna(p20) and float(above_sma20_pct) < float(p20):
            breadth_add = BREADTH_P20_ADD
        if pd.notna(above_sma20_pct) and pd.notna(p10) and float(above_sma20_pct) < float(p10):
            breadth_add = BREADTH_P10_ADD
        result[trade_id] = breadth_add
    return result


def load_breadth_detail_by_trade(current_df: pd.DataFrame) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    if current_df.empty:
        return result
    breadth_df = load_breadth_signal_df()
    breadth_lookup = breadth_df.set_index("screen_date") if not breadth_df.empty else pd.DataFrame()
    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for _, row in working.iterrows():
        trade_id = str(row["trade_id"])
        detail = {
            "above_sma20_pct": None,
            "breadth_p20_threshold": None,
            "breadth_p10_threshold": None,
            "mult_breadth_p20_pass": False,
            "mult_breadth_p10_pass": False,
        }
        screen_date = row["screen_date"]
        if pd.notna(screen_date) and not breadth_lookup.empty and screen_date in breadth_lookup.index:
            breadth_row = breadth_lookup.loc[screen_date]
            above_sma20_pct = pd.to_numeric(breadth_row.get("above_sma20_pct"), errors="coerce")
            p20 = pd.to_numeric(breadth_row.get("above_sma20_pct_p20"), errors="coerce")
            p10 = pd.to_numeric(breadth_row.get("above_sma20_pct_p10"), errors="coerce")
            p20_pass = bool(pd.notna(above_sma20_pct) and pd.notna(p20) and float(above_sma20_pct) < float(p20))
            p10_pass = bool(pd.notna(above_sma20_pct) and pd.notna(p10) and float(above_sma20_pct) < float(p10))
            detail = {
                "above_sma20_pct": float(above_sma20_pct) if pd.notna(above_sma20_pct) else None,
                "breadth_p20_threshold": float(p20) if pd.notna(p20) else None,
                "breadth_p10_threshold": float(p10) if pd.notna(p10) else None,
                "mult_breadth_p20_pass": p20_pass,
                "mult_breadth_p10_pass": p10_pass,
            }
        result[trade_id] = detail
    return result


def load_etf_multiplier_by_trade(current_df: pd.DataFrame) -> dict[str, float]:
    result: dict[str, float] = {}
    if current_df.empty:
        return result

    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for screen_date, group in working.dropna(subset=["screen_date"]).groupby("screen_date", dropna=False):
        tickers = group["ticker"].astype(str).str.strip().str.upper().tolist()
        context_df = ensure_etf_context_for_sd(pd.Timestamp(screen_date), tickers=tickers)
        context_lookup = context_df.set_index("ticker") if not context_df.empty else pd.DataFrame()
        for _, row in group.iterrows():
            trade_id = str(row["trade_id"])
            ticker = str(row["ticker"]).strip().upper()
            if context_lookup.empty or ticker not in context_lookup.index:
                result[trade_id] = ETF_NEUTRAL_MULTIPLIER
                continue
            context_row = context_lookup.loc[ticker]
            recommended = str(context_row.get("recommended", "")).strip().lower() == "recommended"
            signal_available = str(context_row.get("reason", "")).strip() not in {
                "missing_reference_etfs",
                "missing_spy_history",
                "missing_etf_history",
            }
            if not signal_available:
                result[trade_id] = ETF_NEUTRAL_MULTIPLIER
            else:
                result[trade_id] = ETF_PASS_MULTIPLIER if recommended else ETF_FAIL_MULTIPLIER
    return result


def load_etf_recommended_by_trade(current_df: pd.DataFrame) -> dict[str, bool]:
    result: dict[str, bool] = {}
    if current_df.empty:
        return result
    working = current_df.copy()
    working["screen_date"] = pd.to_datetime(working["screen_date"], errors="coerce").dt.normalize()
    for screen_date, group in working.dropna(subset=["screen_date"]).groupby("screen_date", dropna=False):
        tickers = group["ticker"].astype(str).str.strip().str.upper().tolist()
        context_df = ensure_etf_context_for_sd(pd.Timestamp(screen_date), tickers=tickers)
        context_lookup = context_df.set_index("ticker") if not context_df.empty else pd.DataFrame()
        for _, row in group.iterrows():
            trade_id = str(row["trade_id"])
            ticker = str(row["ticker"]).strip().upper()
            if context_lookup.empty or ticker not in context_lookup.index:
                result[trade_id] = False
                continue
            context_row = context_lookup.loc[ticker]
            result[trade_id] = str(context_row.get("recommended", "")).strip().lower() == "recommended"
    return result


def initial_shares(row: pd.Series, risk_amount: float, r_multiplier: float) -> int:
    risk_per_share = r_per_share(row)
    if risk_per_share <= 0:
        return 0
    return max(MIN_SHARES, int((risk_amount * r_multiplier) // risk_per_share))


def first_partial_target_price(row: pd.Series, risk_amount: float, quantity: int) -> float:
    if quantity <= 0:
        return float("nan")
    return float(row["entry_price"]) + (float(risk_amount) * FIRST_TARGET_R_MULTIPLE / quantity)


def apply_sizing_dependent_trade_state_updates(
    current_df: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    risk_amount: float,
    r_multiplier_by_trade: dict[str, float],
) -> pd.DataFrame:
    if current_df.empty:
        return current_df.copy()
    updated = current_df.copy()
    normalized_bd = pd.Timestamp(buy_date).normalize()

    for idx, row in updated.iterrows():
        status = str(row.get("trade_status", "")).upper()
        if status != "OPEN" or pd.isna(row.get("bd")):
            continue
        trade_id = str(row["trade_id"])
        r_multiplier = float(r_multiplier_by_trade.get(trade_id, 1.0))
        quantity = initial_shares(row, risk_amount, r_multiplier)
        target_price = first_partial_target_price(row, risk_amount, quantity)
        updated.at[idx, "target_close_price"] = target_price
        high_price = price_high_for_date(str(row["ticker"]), normalized_bd)
        if high_price >= target_price:
            updated.at[idx, "trade_status"] = "PARTIAL_1"
            updated.at[idx, "first_take_profit_done"] = True
            updated.at[idx, "first_take_profit_date"] = normalized_bd
            updated.at[idx, "current_stop_loss"] = max(float(row["current_stop_loss"]), float(row["entry_price"]))

    return normalize_trade_state_df(updated)


def build_r_multiplier_by_trade(
    current_df: pd.DataFrame,
    positions_existing: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    momentum_add: float,
    slope_add_by_trade: dict[str, float],
    breadth_add_by_trade: dict[str, float],
    etf_multiplier_by_trade: dict[str, float],
) -> dict[str, float]:
    existing_lookup: dict[tuple[str, str], float] = {}
    if not positions_existing.empty:
        existing = positions_existing.copy()
        existing["bd"] = pd.to_datetime(existing["bd"], errors="coerce").dt.strftime("%Y-%m-%d")
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce").dt.normalize()
        existing = existing.loc[existing["date"] < pd.Timestamp(buy_date).normalize()].copy()
        existing = existing.sort_values("date", kind="stable")
        for _, position in existing.iterrows():
            key = (str(position["ticker"]).upper(), str(position["bd"]))
            value = pd.to_numeric(position.get("r_multiplier"), errors="coerce")
            existing_lookup[key] = float(value) if pd.notna(value) else 1.0

    result: dict[str, float] = {}
    for _, row in current_df.iterrows():
        trade_id = str(row["trade_id"])
        bd = pd.to_datetime(row.get("bd"), errors="coerce")
        if pd.isna(bd):
            result[trade_id] = 1.0
            continue
        key = (str(row["ticker"]).upper(), pd.Timestamp(bd).strftime("%Y-%m-%d"))
        if pd.Timestamp(bd).normalize() < pd.Timestamp(buy_date).normalize() and key in existing_lookup:
            result[trade_id] = existing_lookup[key]
        else:
            pre_etf_r_multiplier = (
                1.0
                + float(slope_add_by_trade.get(trade_id, 0.0))
                + float(momentum_add)
                + float(breadth_add_by_trade.get(trade_id, 0.0))
            )
            result[trade_id] = pre_etf_r_multiplier * float(etf_multiplier_by_trade.get(trade_id, ETF_NEUTRAL_MULTIPLIER))
    return result


def shares_open_for_status(row: pd.Series, risk_amount: float, r_multiplier: float) -> int:
    status = str(row["trade_status"]).upper()
    if status in {"SKIPPED", "CLOSED"}:
        return 0
    quantity = initial_shares(row, risk_amount, r_multiplier)
    first, second, final = build_tranche_sizes(quantity)
    if status == "OPEN":
        return quantity
    if status == "PARTIAL_1":
        return quantity - first
    if status == "PARTIAL_2":
        return final
    return 0


def build_position_indexes(current_df: pd.DataFrame) -> dict[str, int]:
    entered = current_df.loc[current_df["bd"].notna()].copy()
    if entered.empty:
        return {}
    entered = entered.sort_values(["ticker", "bd", "screen_date", "trade_id"])
    entered["entry_seq"] = entered.groupby(["ticker", "bd"]).cumcount() + 1
    return dict(zip(entered["trade_id"].astype(str), entered["entry_seq"].astype(int)))


def position_id_for_row(
    row: pd.Series,
    *,
    strategy_id: str,
    variant_id: str,
    entry_seq: int,
) -> str:
    bd = pd.Timestamp(row["bd"]).strftime("%Y%m%d")
    return build_position_id(strategy_id, variant_id, str(row["ticker"]), bd, entry_seq)


def build_positions_for_bd(
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
    risk_amount: float,
    r_multiplier_by_trade: dict[str, float],
    actions_all: pd.DataFrame | None = None,
) -> pd.DataFrame:
    portfolio_id = build_portfolio_id(strategy_id, variant_id)
    previous_lookup = previous_df.set_index("trade_id") if not previous_df.empty else pd.DataFrame()
    entry_seq_by_trade = build_position_indexes(current_df)
    rows: list[dict[str, object]] = []

    for _, row in current_df.iterrows():
        status = str(row["trade_status"]).upper()
        if status == "SKIPPED" or pd.isna(row["bd"]):
            continue
        r_multiplier = float(r_multiplier_by_trade.get(str(row["trade_id"]), 1.0))
        quantity = initial_shares(row, risk_amount, r_multiplier)
        shares_open = shares_open_for_status(row, risk_amount, r_multiplier)
        close_price = price_close_for_date(str(row["ticker"]), buy_date)
        unrealized_r = unrealized_r_from_cash_pnl(row, close_price, shares_open, risk_amount)

        previous_status = ""
        if not previous_lookup.empty and row["trade_id"] in previous_lookup.index:
            previous_status = str(previous_lookup.loc[row["trade_id"], "trade_status"]).upper()
        opened_today = pd.Timestamp(row["bd"]).normalize() == pd.Timestamp(buy_date).normalize()
        closed_today = status == "CLOSED" and previous_status != "CLOSED"
        position_status = "CLOSED" if status == "CLOSED" else "OPEN"
        entry_seq = entry_seq_by_trade.get(str(row["trade_id"]), 1)
        position_id = position_id_for_row(
            row,
            strategy_id=strategy_id,
            variant_id=variant_id,
            entry_seq=entry_seq,
        )
        realized_r_partial = 0.0
        if actions_all is not None and not actions_all.empty:
            actions = actions_all.copy()
            actions["action_date"] = pd.to_datetime(actions["action_date"], errors="coerce").dt.normalize()
            position_actions = actions.loc[
                actions["position_id"].astype(str).eq(position_id)
                & (actions["action_date"] <= pd.Timestamp(buy_date).normalize())
            ]
            realized_r_partial = float(
                pd.to_numeric(position_actions.get("realized_r_delta", pd.Series(dtype=float)), errors="coerce")
                .fillna(0.0)
                .sum()
            )
        total_mtm_r = realized_r_partial + unrealized_r

        rows.append(
            {
                "date": pd.Timestamp(buy_date).strftime("%Y-%m-%d"),
                "portfolio_id": portfolio_id,
                "strategy_id": strategy_id,
                "variant_id": variant_id,
                "position_id": position_id,
                "ticker": str(row["ticker"]).upper(),
                "bd": pd.Timestamp(row["bd"]).strftime("%Y-%m-%d"),
                "entry_seq": entry_seq,
                "r_multiplier": r_multiplier,
                "days_in_trade": max(1, int((pd.Timestamp(buy_date) - pd.Timestamp(row["bd"])).days) + 1),
                "shares_initial": quantity,
                "shares_open": shares_open,
                "realized_r_partial": round(realized_r_partial, 4),
                "unrealized_r": round(unrealized_r, 4),
                "total_mtm_r": round(total_mtm_r, 4),
                "is_open": status in {"OPEN", "PARTIAL_1", "PARTIAL_2"},
                "position_status": position_status,
                "opened_today": opened_today,
                "closed_today": closed_today,
                "carry_in_from_prev_year": False,
                "carry_out_to_next_year": False,
            }
        )

    return pd.DataFrame(rows, columns=PORTFOLIO_POSITIONS_DAILY_COLUMNS)


def sell_price_for_status(row: pd.Series, status: str, action_date: pd.Timestamp) -> float:
    status = status.upper()
    if status == "CLOSED" and str(row.get("close_reason", "")).upper() == "STOP_LOSS":
        return float(row["current_stop_loss"])
    if status == "PARTIAL_1":
        return float(row["target_close_price"])
    return price_close_for_date(str(row["ticker"]), action_date)


def append_action(
    rows: list[dict[str, object]],
    *,
    action_date: pd.Timestamp,
    portfolio_id: str,
    strategy_id: str,
    variant_id: str,
    position_id: str,
    ticker: str,
    action_seq: int,
    action_type: str,
    bd: pd.Timestamp,
    shares_delta: int,
    shares_open_after: int,
    realized_r_delta: float,
    realized_r_cum_position: float,
    note: str,
) -> int:
    rows.append(
        {
            "action_date": pd.Timestamp(action_date).strftime("%Y-%m-%d"),
            "portfolio_id": portfolio_id,
            "strategy_id": strategy_id,
            "variant_id": variant_id,
            "position_id": position_id,
            "ticker": ticker,
            "action_seq": action_seq,
            "action_type": action_type,
            "bd": pd.Timestamp(bd).strftime("%Y-%m-%d"),
            "shares_delta": shares_delta,
            "shares_open_after": shares_open_after,
            "realized_r_delta": round(realized_r_delta, 4),
            "realized_r_cum_position": round(realized_r_cum_position, 4),
            "note": note,
        }
    )
    return action_seq + 1


def build_actions_for_bd(
    current_df: pd.DataFrame,
    previous_df: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
    risk_amount: float,
    r_multiplier_by_trade: dict[str, float],
    actions_existing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    portfolio_id = build_portfolio_id(strategy_id, variant_id)
    previous_lookup = previous_df.set_index("trade_id") if not previous_df.empty else pd.DataFrame()
    entry_seq_by_trade = build_position_indexes(current_df)
    action_rows: list[dict[str, object]] = []

    for _, row in current_df.iterrows():
        status = str(row["trade_status"]).upper()
        if status == "SKIPPED" or pd.isna(row["bd"]):
            continue
        trade_id = str(row["trade_id"])
        r_multiplier = float(r_multiplier_by_trade.get(trade_id, 1.0))
        ticker = str(row["ticker"]).upper()
        entry_seq = entry_seq_by_trade.get(trade_id, 1)
        position_id = position_id_for_row(row, strategy_id=strategy_id, variant_id=variant_id, entry_seq=entry_seq)
        quantity = initial_shares(row, risk_amount, r_multiplier)
        first, second, final = build_tranche_sizes(quantity)
        previous_status = ""
        previous_shares_open = 0
        if not previous_lookup.empty and trade_id in previous_lookup.index:
            previous_row = previous_lookup.loc[trade_id]
            previous_status = str(previous_row["trade_status"]).upper()
            previous_shares_open = shares_open_for_status(previous_row, risk_amount, r_multiplier)

        current_shares_open = shares_open_for_status(row, risk_amount, r_multiplier)
        is_new_entry = not previous_status and pd.Timestamp(row["bd"]).normalize() == pd.Timestamp(buy_date).normalize()
        prior_position_actions = pd.DataFrame()
        if actions_existing is not None and not actions_existing.empty:
            prior_actions = actions_existing.copy()
            prior_actions["action_date"] = pd.to_datetime(prior_actions["action_date"], errors="coerce").dt.normalize()
            prior_position_actions = prior_actions.loc[
                prior_actions["position_id"].astype(str).eq(position_id)
                & (prior_actions["action_date"] < pd.Timestamp(buy_date).normalize())
            ].copy()
        action_seq = (
            int(pd.to_numeric(prior_position_actions["action_seq"], errors="coerce").fillna(0).max()) + 1
            if not prior_position_actions.empty
            else 1
        )
        realized_cum = 0.0
        if not prior_position_actions.empty:
            prior_position_actions = prior_position_actions.sort_values(["action_date", "action_seq"], kind="stable")
            realized_cum = float(
                pd.to_numeric(prior_position_actions.iloc[-1].get("realized_r_cum_position"), errors="coerce") or 0.0
            )

        if is_new_entry:
            action_seq = append_action(
                action_rows,
                action_date=buy_date,
                portfolio_id=portfolio_id,
                strategy_id=strategy_id,
                variant_id=variant_id,
                position_id=position_id,
                ticker=ticker,
                action_seq=action_seq,
                action_type="BUY",
                bd=row["bd"],
                shares_delta=quantity,
                shares_open_after=quantity,
                realized_r_delta=0.0,
                realized_r_cum_position=realized_cum,
                note=str(row["entry_mode"]),
            )
            previous_shares_open = quantity

        if previous_shares_open <= current_shares_open:
            continue

        sell_shares = previous_shares_open - current_shares_open
        sell_price = sell_price_for_status(row, status, buy_date)
        realized_delta = realized_r_from_cash_pnl(row, sell_price, sell_shares, risk_amount)
        realized_cum += realized_delta
        if status == "PARTIAL_1":
            action_type = "SELL_PARTIAL_1"
        elif status == "PARTIAL_2":
            action_type = "SELL_PARTIAL_2"
        else:
            action_type = "SELL_CLOSE"
        append_action(
            action_rows,
            action_date=buy_date,
            portfolio_id=portfolio_id,
            strategy_id=strategy_id,
            variant_id=variant_id,
            position_id=position_id,
            ticker=ticker,
            action_seq=action_seq,
            action_type=action_type,
            bd=row["bd"],
            shares_delta=-sell_shares,
            shares_open_after=current_shares_open,
            realized_r_delta=realized_delta,
            realized_r_cum_position=realized_cum,
            note=str(row.get("close_reason", "") or status),
        )

    return pd.DataFrame(action_rows, columns=PORTFOLIO_ACTIONS_DAILY_COLUMNS)


def trade_sizing_path_for_date(buy_date: pd.Timestamp) -> Path:
    bd = pd.Timestamp(buy_date).normalize()
    return trade_state_dir_for_date(bd) / f"trade_sizing_{bd.strftime('%Y%m%d')}.csv"


def build_trade_sizing_for_bd(
    current_df: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
    risk_amount: float,
    momentum_add: float,
    momentum_meta: dict[str, object],
    slope_add_by_trade: dict[str, float],
    slope_value_by_trade: dict[str, float | None],
    breadth_add_by_trade: dict[str, float],
    breadth_detail_by_trade: dict[str, dict[str, object]],
    etf_multiplier_by_trade: dict[str, float],
    etf_recommended_by_trade: dict[str, bool],
    r_multiplier_by_trade: dict[str, float],
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for _, row in current_df.iterrows():
        trade_id = str(row["trade_id"])
        slope_value = slope_value_by_trade.get(trade_id)
        slope_add = float(slope_add_by_trade.get(trade_id, 0.0))
        breadth_add = float(breadth_add_by_trade.get(trade_id, 0.0))
        etf_multiplier = float(etf_multiplier_by_trade.get(trade_id, ETF_NEUTRAL_MULTIPLIER))
        pre_etf = 1.0 + slope_add + float(momentum_add) + breadth_add
        bd = pd.to_datetime(row.get("bd"), errors="coerce")
        entered = pd.notna(bd)
        r_multiplier_final = float(r_multiplier_by_trade.get(trade_id, 1.0)) if entered else 0.0
        shares_initial = initial_shares(row, risk_amount, r_multiplier_final) if entered else 0
        breadth_detail = breadth_detail_by_trade.get(trade_id, {})
        rows.append(
            {
                "trade_id": trade_id,
                "ticker": str(row["ticker"]).upper(),
                "screen_date": pd.Timestamp(row["screen_date"]).strftime("%Y-%m-%d") if pd.notna(row["screen_date"]) else "",
                "buy_date": pd.Timestamp(buy_date).strftime("%Y-%m-%d"),
                "trade_status": str(row["trade_status"]).upper(),
                "bd": pd.Timestamp(bd).strftime("%Y-%m-%d") if entered else "",
                "strategy_id": strategy_id,
                "variant_id": variant_id,
                "risk_amount": round(float(risk_amount), 4),
                "source_ema21_slope_pct_5": round(float(slope_value), 4) if slope_value is not None else None,
                "mult_slope_pass": bool(slope_add > 0),
                "mult_slope_add": round(slope_add, 2),
                "momentum_reference_date": momentum_meta.get("momentum_reference_date", ""),
                "prev_momentum_5d_top_half": bool(momentum_meta.get("prev_momentum_5d_top_half", False)),
                "momentum_bootstrap_neutral": bool(momentum_meta.get("momentum_bootstrap_neutral", False)),
                "mult_momentum_add": round(float(momentum_add), 2),
                "above_sma20_pct": breadth_detail.get("above_sma20_pct"),
                "breadth_p20_threshold": breadth_detail.get("breadth_p20_threshold"),
                "breadth_p10_threshold": breadth_detail.get("breadth_p10_threshold"),
                "mult_breadth_p20_pass": bool(breadth_detail.get("mult_breadth_p20_pass", False)),
                "mult_breadth_p10_pass": bool(breadth_detail.get("mult_breadth_p10_pass", False)),
                "mult_breadth_add": round(breadth_add, 2),
                "etf_recommended": bool(etf_recommended_by_trade.get(trade_id, False)),
                "mult_etf_multiplier": round(etf_multiplier, 2),
                "r_multiplier_pre_etf": round(pre_etf, 2),
                "r_multiplier_final": round(r_multiplier_final, 4),
                "shares_initial": shares_initial,
                "sizing_scope": "portfolio_build",
            }
        )
    return pd.DataFrame(rows, columns=TRADE_SIZING_COLUMNS)


def save_trade_sizing_for_bd(sizing_df: pd.DataFrame, buy_date: pd.Timestamp) -> Path:
    path = trade_sizing_path_for_date(buy_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    sizing_df.to_csv(path, index=False)
    return path


def build_state_for_bd(
    positions_bd: pd.DataFrame,
    actions_all: pd.DataFrame,
    previous_state: pd.DataFrame,
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
) -> pd.DataFrame:
    portfolio_id = build_portfolio_id(strategy_id, variant_id)
    realized_r_day = 0.0
    partial_exit_count = 0
    full_exit_count = 0
    if not actions_all.empty:
        actions = actions_all.copy()
        actions["action_date"] = pd.to_datetime(actions["action_date"], errors="coerce").dt.normalize()
        today_actions = actions.loc[actions["action_date"].eq(pd.Timestamp(buy_date).normalize())].copy()
        realized_r_day = pd.to_numeric(today_actions["realized_r_delta"], errors="coerce").fillna(0.0).sum()
        partial_exit_count = int(today_actions["action_type"].astype(str).str.contains("PARTIAL").sum())
        full_exit_count = int(today_actions["action_type"].astype(str).eq("SELL_CLOSE").sum())

    previous_realized_cum = 0.0
    previous_equity = 0.0
    previous_drawdown = 0.0
    if not previous_state.empty:
        prev = previous_state.copy()
        prev["date"] = pd.to_datetime(prev["date"], errors="coerce").dt.normalize()
        prev = prev.sort_values("date")
        latest = prev.iloc[-1]
        previous_realized_cum = float(pd.to_numeric(latest.get("realized_r_cum"), errors="coerce") or 0.0)
        previous_equity = float(pd.to_numeric(latest.get("equity_mtm_r"), errors="coerce") or 0.0)
        previous_drawdown = float(pd.to_numeric(latest.get("drawdown_mtm_r"), errors="coerce") or 0.0)

    open_positions = positions_bd.loc[positions_bd["is_open"].astype(bool)].copy() if not positions_bd.empty else positions_bd
    unrealized_r = pd.to_numeric(open_positions.get("unrealized_r", pd.Series(dtype=float)), errors="coerce").fillna(0.0).sum()
    realized_r_cum = previous_realized_cum + realized_r_day
    equity_mtm_r = realized_r_cum + unrealized_r
    drawdown_mtm_r = min(previous_drawdown, equity_mtm_r - max(previous_equity, equity_mtm_r))
    open_tickers = ",".join(sorted(open_positions["ticker"].astype(str).str.upper().unique())) if not open_positions.empty else ""

    return pd.DataFrame(
        [
            {
                "date": pd.Timestamp(buy_date).strftime("%Y-%m-%d"),
                "portfolio_id": portfolio_id,
                "strategy_id": strategy_id,
                "variant_id": variant_id,
                "equity_mtm_r": round(equity_mtm_r, 4),
                "realized_r_cum": round(realized_r_cum, 4),
                "unrealized_r": round(unrealized_r, 4),
                "mtm_r_day": round(equity_mtm_r - previous_equity, 4),
                "drawdown_mtm_r": round(drawdown_mtm_r, 4),
                "open_positions_count": int(len(open_positions)),
                "new_entries_count": int(positions_bd["opened_today"].astype(bool).sum()) if not positions_bd.empty else 0,
                "partial_exit_count": partial_exit_count,
                "full_exit_count": full_exit_count,
                "semaphore_color": "",
                "blue_on": False,
                "has_carry_positions": False,
                "open_tickers": open_tickers,
            }
        ],
        columns=PORTFOLIO_STATE_DAILY_COLUMNS,
    )


def replace_day_rows(existing: pd.DataFrame, new_rows: pd.DataFrame, date_column: str, date_value: pd.Timestamp) -> pd.DataFrame:
    if existing.empty:
        return new_rows.copy()
    existing = existing.copy()
    existing[date_column] = pd.to_datetime(existing[date_column], errors="coerce").dt.normalize()
    filtered = existing.loc[~existing[date_column].eq(pd.Timestamp(date_value).normalize())].copy()
    return pd.concat([filtered, new_rows], ignore_index=True)


def save_outputs(
    *,
    positions_all: pd.DataFrame,
    actions_all: pd.DataFrame,
    state_all: pd.DataFrame,
    strategy_id: str,
    variant_id: str,
    layer: str,
) -> list[Path]:
    output_paths: list[Path] = []
    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)

    for filename, df, sort_cols in [
        ("portfolio_positions_daily.csv", positions_all, ["date", "ticker", "bd", "position_id"]),
        ("portfolio_actions_daily.csv", actions_all, ["action_date", "ticker", "bd", "action_seq"]),
        ("portfolio_state_daily.csv", state_all, ["date"]),
    ]:
        df = df.copy()
        for date_col in ["date", "action_date", "bd"]:
            if date_col in df.columns:
                df[date_col] = pd.to_datetime(df[date_col], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
        path = full_dir / filename
        df.sort_values(sort_cols).to_csv(path, index=False)
        output_paths.append(path)

        date_col = "action_date" if filename == "portfolio_actions_daily.csv" else "date"
        yearly = df.copy()
        yearly[date_col] = pd.to_datetime(yearly[date_col], errors="coerce").dt.normalize()
        yearly["year"] = yearly[date_col].dt.year
        for year, group in yearly.dropna(subset=["year"]).groupby("year"):
            year_dir = get_portfolio_yearly_dir(int(year), strategy_id, variant_id, layer=layer)
            year_dir.mkdir(parents=True, exist_ok=True)
            year_path = year_dir / filename
            group.drop(columns=["year"]).sort_values(sort_cols).to_csv(year_path, index=False)
            output_paths.append(year_path)
    return output_paths


def build_portfolio_day(
    *,
    buy_date: pd.Timestamp,
    strategy_id: str,
    variant_id: str,
    risk_amount: float,
    layer: str = "live",
) -> dict[str, object]:
    buy_date = pd.Timestamp(buy_date).normalize()
    current_df = load_trade_state_for_bd(buy_date)
    previous_df = load_previous_trade_state(buy_date)

    full_dir = get_portfolio_full_dir(strategy_id, variant_id, layer=layer)
    positions_existing = load_existing_output(full_dir / "portfolio_positions_daily.csv", PORTFOLIO_POSITIONS_DAILY_COLUMNS)
    actions_existing = load_existing_output(full_dir / "portfolio_actions_daily.csv", PORTFOLIO_ACTIONS_DAILY_COLUMNS)
    state_existing = load_existing_output(full_dir / "portfolio_state_daily.csv", PORTFOLIO_STATE_DAILY_COLUMNS)
    momentum_add, momentum_meta = load_momentum_multiplier_by_bd(
        buy_date=buy_date,
        strategy_id=strategy_id,
        variant_id=variant_id,
        layer=layer,
    )
    slope_add_by_trade = load_slope_add_by_trade(current_df)
    slope_value_by_trade = load_slope_value_by_trade(current_df)
    breadth_add_by_trade = load_breadth_add_by_trade(current_df)
    breadth_detail_by_trade = load_breadth_detail_by_trade(current_df)
    etf_multiplier_by_trade = load_etf_multiplier_by_trade(current_df)
    etf_recommended_by_trade = load_etf_recommended_by_trade(current_df)
    r_multiplier_by_trade = build_r_multiplier_by_trade(
        current_df,
        positions_existing,
        buy_date=buy_date,
        momentum_add=momentum_add,
        slope_add_by_trade=slope_add_by_trade,
        breadth_add_by_trade=breadth_add_by_trade,
        etf_multiplier_by_trade=etf_multiplier_by_trade,
    )
    current_df = apply_sizing_dependent_trade_state_updates(
        current_df,
        buy_date=buy_date,
        risk_amount=risk_amount,
        r_multiplier_by_trade=r_multiplier_by_trade,
    )
    save_trade_state(current_df, buy_date)
    trade_sizing_bd = build_trade_sizing_for_bd(
        current_df,
        buy_date=buy_date,
        strategy_id=strategy_id,
        variant_id=variant_id,
        risk_amount=risk_amount,
        momentum_add=momentum_add,
        momentum_meta=momentum_meta,
        slope_add_by_trade=slope_add_by_trade,
        slope_value_by_trade=slope_value_by_trade,
        breadth_add_by_trade=breadth_add_by_trade,
        breadth_detail_by_trade=breadth_detail_by_trade,
        etf_multiplier_by_trade=etf_multiplier_by_trade,
        etf_recommended_by_trade=etf_recommended_by_trade,
        r_multiplier_by_trade=r_multiplier_by_trade,
    )

    actions_bd = build_actions_for_bd(
        current_df,
        previous_df,
        buy_date=buy_date,
        strategy_id=strategy_id,
        variant_id=variant_id,
        risk_amount=risk_amount,
        r_multiplier_by_trade=r_multiplier_by_trade,
        actions_existing=actions_existing,
    )
    actions_all = replace_day_rows(actions_existing, actions_bd, "action_date", buy_date).reindex(columns=PORTFOLIO_ACTIONS_DAILY_COLUMNS)
    positions_bd = build_positions_for_bd(
        current_df,
        previous_df,
        buy_date=buy_date,
        strategy_id=strategy_id,
        variant_id=variant_id,
        risk_amount=risk_amount,
        r_multiplier_by_trade=r_multiplier_by_trade,
        actions_all=actions_all,
    )

    positions_all = replace_day_rows(positions_existing, positions_bd, "date", buy_date).reindex(columns=PORTFOLIO_POSITIONS_DAILY_COLUMNS)
    state_prior = state_existing.copy()
    if not state_prior.empty:
        state_prior["date"] = pd.to_datetime(state_prior["date"], errors="coerce").dt.normalize()
        state_prior = state_prior.loc[state_prior["date"] < buy_date].copy()
    state_bd = build_state_for_bd(
        positions_bd,
        actions_all,
        state_prior,
        buy_date=buy_date,
        strategy_id=strategy_id,
        variant_id=variant_id,
    )
    state_all = replace_day_rows(state_existing, state_bd, "date", buy_date).reindex(columns=PORTFOLIO_STATE_DAILY_COLUMNS)
    output_paths = save_outputs(
        positions_all=positions_all,
        actions_all=actions_all,
        state_all=state_all,
        strategy_id=strategy_id,
        variant_id=variant_id,
        layer=layer,
    )
    momentum_path = save_momentum_signal(state_all, strategy_id, variant_id, layer=layer)
    output_paths.append(momentum_path)
    trade_sizing_path = save_trade_sizing_for_bd(trade_sizing_bd, buy_date)
    output_paths.append(trade_sizing_path)
    return {
        "buy_date": buy_date,
        "strategy_id": strategy_id,
        "variant_id": variant_id,
        "positions_rows": len(positions_bd),
        "actions_rows": len(actions_bd),
        "state_rows": len(state_bd),
        "trade_sizing_rows": len(trade_sizing_bd),
        "r_multiplier": 1.0 + momentum_add,
        "momentum": momentum_meta,
        "output_paths": output_paths,
    }


def main() -> None:
    args = build_parser().parse_args()
    result = build_portfolio_day(
        buy_date=pd.Timestamp(args.buy_date).normalize(),
        strategy_id=args.strategy_id,
        variant_id=args.variant_id,
        risk_amount=float(args.risk_amount),
        layer=args.layer,
    )
    print(f"BD: {result['buy_date'].strftime('%Y-%m-%d')}")
    print(f"Strategy: {result['strategy_id']}")
    print(f"Variant: {result['variant_id']}")
    print(f"Positions rows for BD: {result['positions_rows']}")
    print(f"Actions rows for BD: {result['actions_rows']}")
    print(f"State rows for BD: {result['state_rows']}")
    print(f"Trade sizing rows for BD: {result['trade_sizing_rows']}")
    print(f"Pre-ETF R multiplier for new entries: {result['r_multiplier']:.2f}")


if __name__ == "__main__":
    main()
