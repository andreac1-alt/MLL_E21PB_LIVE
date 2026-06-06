from __future__ import annotations

import argparse
import re
from functools import lru_cache
from pathlib import Path

import pandas as pd

from core.io.archive_utils import resolve_archive_file, trading_day_date_dir
from core.portfolio.paths import (
    get_portfolio_full_dir,
    get_portfolio_yearly_dir,
    get_trade_lifecycle_filename,
    get_trade_timeline_full_dir,
    get_trade_timeline_yearly_dir,
)
from core.market.market import load_cached_price_history_any_end
from tools.strategy_EMA21_SMA50 import TradeResult, analyze_trade, load_cached_price_history


BASE_DIR = Path(__file__).resolve().parents[2]
BACKTEST_TRADES_DIR = BASE_DIR / "output" / "backtest_trades"
TRADING_DAY_DIR = BASE_DIR / "output" / "trading_day"
STORICO_SPY_PATH = BASE_DIR / "output" / "storico_SPY.csv"
TRADE_OUTPUT_SUBDIR = "trade_engine"
ORDER_COST = 1.0
FX_COST_PCT = 0.005
DEFAULT_BASE_RISK_AMOUNT = 25.0
ENTRY_SOURCE_DIRNAME = "entries"
ENTRY_TRADE_SOURCE_FILENAME = "entry_trade_source.csv"
TRADE_ENGINE_DIRNAME = "trade_engine"
TRADE_SUMMARY_DIRNAME = "trade_summary"
TRADE_LEGS_DIRNAME = "trade_legs"

STRATEGY_CONFIG = {
    "EMA21_SMA50": {
        "dirname": "strategy_EMA21_SMA50",
        "progress_pattern": "trading_day_progress.csv",
    },
    "SMA10_EMA21": {
        "dirname": "strategy_SMA10_EMA21",
        "progress_pattern": "backtest_trading_days_{year}_top10_all_days_progress.csv",
    },
}

TRADE_TICKER_PATTERN = re.compile(r"^trade_(\d+)_ticker$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Ricostruisce il trade lifecycle del layer selezionato e portfolio_state_daily.csv "
            "per una strategia, usando timeline continua multi-year e output annuali."
        )
    )
    parser.add_argument(
        "--strategy",
        required=True,
        choices=sorted(STRATEGY_CONFIG.keys()),
        help="Strategia da ricostruire.",
    )
    parser.add_argument("--start-year", type=int, default=1999)
    parser.add_argument("--end-year", type=int, default=pd.Timestamp.today().year)
    parser.add_argument(
        "--layer",
        default="live",
        choices=["live", "frozen"],
        help="Layer di output per trade reconstruction.",
    )
    return parser.parse_args()


def load_progress_df(strategy: str, start_year: int, end_year: int) -> pd.DataFrame:
    config = STRATEGY_CONFIG[strategy]
    rows: list[pd.DataFrame] = []
    for year in range(start_year, end_year + 1):
        progress_filename = config["progress_pattern"].format(year=year)
        preferred_progress_path = (
            TRADING_DAY_DIR
            / str(year)
            / progress_filename
        )
        legacy_progress_path = (
            BACKTEST_TRADES_DIR
            / str(year)
            / config["dirname"]
            / progress_filename
        )
        progress_path = preferred_progress_path if preferred_progress_path.exists() else legacy_progress_path
        if not progress_path.exists():
            continue
        df = pd.read_csv(progress_path)
        if df.empty:
            continue
        df["source_progress_path"] = str(progress_path)
        rows.append(df)

    if not rows:
        return pd.DataFrame()

    progress_df = pd.concat(rows, ignore_index=True)
    if "screen_date" not in progress_df.columns and "target_date" in progress_df.columns:
        progress_df["screen_date"] = progress_df["target_date"]
    progress_df["screen_date"] = pd.to_datetime(progress_df["screen_date"], errors="coerce").dt.normalize()
    progress_df["buy_date"] = pd.to_datetime(progress_df["buy_date"], errors="coerce").dt.normalize()
    progress_df["executed_trades_count"] = (
        pd.to_numeric(progress_df["executed_trades_count"], errors="coerce").fillna(0).astype(int)
    )
    progress_df["backtest_selected_count"] = (
        pd.to_numeric(progress_df["backtest_selected_count"], errors="coerce").fillna(0).astype(int)
    )
    progress_df["first_screen_count"] = (
        pd.to_numeric(progress_df["first_screen_count"], errors="coerce").fillna(0).astype(int)
    )
    progress_df["second_screen_count"] = (
        pd.to_numeric(progress_df["second_screen_count"], errors="coerce").fillna(0).astype(int)
    )
    progress_df = progress_df.dropna(subset=["screen_date", "buy_date"]).sort_values("screen_date")
    progress_df = progress_df.reset_index(drop=True)
    return progress_df


def load_entry_trade_source_df(strategy: str, start_year: int, end_year: int) -> pd.DataFrame:
    path = get_portfolio_full_dir(strategy, ENTRY_SOURCE_DIRNAME, layer="frozen") / ENTRY_TRADE_SOURCE_FILENAME
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty:
        return pd.DataFrame()

    if "source_screen_date" not in df.columns and "source_target_date" in df.columns:
        df["source_screen_date"] = df["source_target_date"]
    df["source_screen_date"] = pd.to_datetime(df["source_screen_date"], errors="coerce").dt.normalize()
    bd_source_col = "bd" if "bd" in df.columns else "requested_buy_date"
    effective_bd_source_col = "effective_bd" if "effective_bd" in df.columns else "effective_entry_date"
    df["bd"] = pd.to_datetime(df[bd_source_col], errors="coerce").dt.normalize()
    df["effective_bd"] = pd.to_datetime(df[effective_bd_source_col], errors="coerce").dt.normalize()
    df["selection_rank"] = pd.to_numeric(df["selection_rank"], errors="coerce")
    df["core_score"] = pd.to_numeric(df["core_score"], errors="coerce")
    df["first_screen_count"] = pd.to_numeric(df["first_screen_count"], errors="coerce")
    df["second_screen_count"] = pd.to_numeric(df["second_screen_count"], errors="coerce")
    df["selected_count"] = pd.to_numeric(df["selected_count"], errors="coerce")
    df["source_ema21_slope_pct_5"] = pd.to_numeric(df["source_ema21_slope_pct_5"], errors="coerce")
    df = df.dropna(subset=["source_screen_date", "bd", "ticker"])
    df = df[
        (df["source_screen_date"].dt.year >= int(start_year))
        & (df["source_screen_date"].dt.year <= int(end_year))
    ].copy()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    return df.sort_values(["source_screen_date", "selection_rank", "ticker"]).reset_index(drop=True)


def extract_trade_records(progress_row: pd.Series) -> list[dict[str, object]]:
    trade_records: list[dict[str, object]] = []
    trade_columns = sorted(
        (
            (int(match.group(1)), column_name)
            for column_name in progress_row.index
            if (match := TRADE_TICKER_PATTERN.match(column_name))
        ),
        key=lambda item: item[0],
    )
    for trade_index, ticker_column in trade_columns:
        ticker = str(progress_row.get(ticker_column, "")).strip().upper()
        if not ticker or ticker == "NAN":
            continue
        realized_r = pd.to_numeric(progress_row.get(f"trade_{trade_index}_r"), errors="coerce")
        trade_records.append(
            {
                "trade_index": trade_index,
                "ticker": ticker,
                "final_trade_r": float(realized_r) if pd.notna(realized_r) else None,
            }
        )
    return trade_records


def load_trade_legs_df(bd: pd.Timestamp, ticker: str) -> pd.DataFrame:
    stamp = bd.strftime("%Y%m%d")
    legs_filename = f"{ticker}_{stamp}_trade_legs.csv"
    legs_path = trading_day_date_dir(bd) / TRADE_OUTPUT_SUBDIR / legs_filename
    if not legs_path.exists():
        legs_path = resolve_archive_file(
            bd,
            f"prova trade/{legs_filename}",
        )
    if not legs_path.exists():
        raise FileNotFoundError(f"Trade legs non trovato: {legs_path}")
    legs_df = pd.read_csv(legs_path)
    if legs_df.empty:
        raise ValueError(f"Trade legs vuoto: {legs_path}")
    legs_df["date"] = pd.to_datetime(legs_df["date"], errors="coerce").dt.normalize()
    legs_df["price"] = pd.to_numeric(legs_df["price"], errors="coerce")
    legs_df["shares"] = pd.to_numeric(legs_df["shares"], errors="coerce").fillna(0).astype(int)
    legs_df["risk_amount"] = pd.to_numeric(legs_df["risk_amount"], errors="coerce")
    legs_df["stop_loss"] = pd.to_numeric(legs_df["stop_loss"], errors="coerce")
    return legs_df.dropna(subset=["date", "price"])


def load_trade_summary_df(bd: pd.Timestamp) -> pd.DataFrame:
    stamp = bd.strftime("%Y%m%d")
    summary_filename = f"trade_string_{stamp}_summary.csv"
    summary_path = trading_day_date_dir(bd) / TRADE_OUTPUT_SUBDIR / summary_filename
    if not summary_path.exists():
        summary_path = resolve_archive_file(
            bd,
            f"prova trade/{summary_filename}",
        )
    if not summary_path.exists():
        raise FileNotFoundError(f"Trade summary non trovato: {summary_path}")
    try:
        summary_df = pd.read_csv(summary_path)
    except pd.errors.EmptyDataError as exc:
        raise ValueError(f"Trade summary vuoto: {summary_path}") from exc
    if summary_df.empty:
        raise ValueError(f"Trade summary vuoto: {summary_path}")
    summary_df["ticker"] = summary_df["ticker"].astype(str).str.strip().str.upper()
    summary_df["buy_date"] = pd.to_datetime(summary_df["buy_date"], errors="coerce").dt.normalize()
    return summary_df.dropna(subset=["ticker", "buy_date"])


def load_trade_summary_frozen_df(strategy: str, bd: pd.Timestamp) -> pd.DataFrame:
    date_tag = bd.strftime("%Y%m%d")
    summary_path = (
        get_portfolio_full_dir(strategy, TRADE_ENGINE_DIRNAME, layer="frozen")
        / TRADE_SUMMARY_DIRNAME
        / f"trade_string_{date_tag}_summary.csv"
    )
    if not summary_path.exists():
        raise FileNotFoundError(f"Trade summary frozen non trovato: {summary_path}")
    summary_df = pd.read_csv(summary_path, keep_default_na=False)
    if summary_df.empty:
        raise ValueError(f"Trade summary frozen vuoto: {summary_path}")
    summary_df["ticker"] = summary_df["ticker"].astype(str).str.strip().str.upper()
    summary_df["buy_date"] = pd.to_datetime(summary_df["buy_date"], errors="coerce").dt.normalize()
    return summary_df.dropna(subset=["ticker", "buy_date"])


def resolve_effective_bd(bd: pd.Timestamp, ticker: str) -> pd.Timestamp:
    summary_df = load_trade_summary_df(bd)
    ticker_mask = summary_df["ticker"] == str(ticker).strip().upper()
    ticker_rows = summary_df.loc[ticker_mask].sort_values("buy_date")
    if ticker_rows.empty:
        raise ValueError(
            f"Ticker {ticker} non trovato nel trade summary della BD {bd.date()}."
        )
    return pd.Timestamp(ticker_rows.iloc[0]["buy_date"]).normalize()


def resolve_effective_bd_frozen(
    strategy: str,
    bd: pd.Timestamp,
    ticker: str,
) -> pd.Timestamp:
    summary_df = load_trade_summary_frozen_df(strategy, bd)
    ticker_mask = summary_df["ticker"] == str(ticker).strip().upper()
    ticker_rows = summary_df.loc[ticker_mask].sort_values("buy_date")
    if ticker_rows.empty:
        raise ValueError(
            f"Ticker {ticker} non trovato nel trade summary frozen della BD {bd.date()}."
        )
    return pd.Timestamp(ticker_rows.iloc[0]["buy_date"]).normalize()


def load_trade_summary_row_for_bd(
    bd: pd.Timestamp,
    ticker: str,
) -> pd.Series:
    summary_df = load_trade_summary_df(bd)
    ticker_mask = summary_df["ticker"] == str(ticker).strip().upper()
    ticker_rows = summary_df.loc[ticker_mask].sort_values("buy_date")
    if ticker_rows.empty:
        raise ValueError(
            f"Ticker {ticker} non trovato nel trade summary della BD {bd.date()}."
        )
    return ticker_rows.iloc[0]


def load_trade_summary_frozen_row_for_bd(
    strategy: str,
    bd: pd.Timestamp,
    ticker: str,
) -> pd.Series:
    summary_df = load_trade_summary_frozen_df(strategy, bd)
    ticker_mask = summary_df["ticker"] == str(ticker).strip().upper()
    ticker_rows = summary_df.loc[ticker_mask].sort_values("buy_date")
    if ticker_rows.empty:
        raise ValueError(
            f"Ticker {ticker} non trovato nel trade summary frozen della BD {bd.date()}."
        )
    return ticker_rows.iloc[0]


def load_trade_legs_df_for_bd(bd: pd.Timestamp, ticker: str) -> tuple[pd.DataFrame, pd.Timestamp]:
    effective_bd = resolve_effective_bd(bd, ticker)
    stamp = effective_bd.strftime("%Y%m%d")
    legs_filename = f"{ticker}_{stamp}_trade_legs.csv"
    legs_path = trading_day_date_dir(effective_bd) / TRADE_OUTPUT_SUBDIR / legs_filename
    if not legs_path.exists():
        legs_path = resolve_archive_file(
            effective_bd,
            f"prova trade/{legs_filename}",
        )
    if not legs_path.exists():
        raise FileNotFoundError(
            f"Trade legs non trovato: {legs_path} "
            f"(ticker={ticker}, bd={bd.date()}, "
            f"effective_bd={effective_bd.date()})"
        )
    legs_df = pd.read_csv(legs_path)
    if legs_df.empty:
        raise ValueError(
            f"Trade legs vuoto: {legs_path} "
            f"(ticker={ticker}, bd={bd.date()}, "
            f"effective_bd={effective_bd.date()})"
        )
    legs_df["date"] = pd.to_datetime(legs_df["date"], errors="coerce").dt.normalize()
    legs_df["price"] = pd.to_numeric(legs_df["price"], errors="coerce")
    legs_df["shares"] = pd.to_numeric(legs_df["shares"], errors="coerce").fillna(0).astype(int)
    legs_df["risk_amount"] = pd.to_numeric(legs_df["risk_amount"], errors="coerce")
    legs_df["stop_loss"] = pd.to_numeric(legs_df["stop_loss"], errors="coerce")
    return legs_df.dropna(subset=["date", "price"]), effective_bd


def load_trade_legs_frozen_df_for_bd(
    strategy: str,
    bd: pd.Timestamp,
    ticker: str,
) -> tuple[pd.DataFrame, pd.Timestamp]:
    effective_bd = resolve_effective_bd_frozen(strategy, bd, ticker)
    stamp = effective_bd.strftime("%Y%m%d")
    legs_path = (
        get_portfolio_full_dir(strategy, TRADE_ENGINE_DIRNAME, layer="frozen")
        / TRADE_LEGS_DIRNAME
        / f"{ticker}_{stamp}_trade_legs.csv"
    )
    if not legs_path.exists():
        raise FileNotFoundError(
            f"Trade legs frozen non trovato: {legs_path} "
            f"(ticker={ticker}, bd={bd.date()}, "
            f"effective_bd={effective_bd.date()})"
        )
    legs_df = pd.read_csv(legs_path, keep_default_na=False)
    if legs_df.empty:
        raise ValueError(
            f"Trade legs frozen vuoto: {legs_path} "
            f"(ticker={ticker}, bd={bd.date()}, "
            f"effective_bd={effective_bd.date()})"
        )
    legs_df["date"] = pd.to_datetime(legs_df["date"], errors="coerce").dt.normalize()
    legs_df["price"] = pd.to_numeric(legs_df["price"], errors="coerce")
    legs_df["shares"] = pd.to_numeric(legs_df["shares"], errors="coerce").fillna(0).astype(int)
    legs_df["risk_amount"] = pd.to_numeric(legs_df["risk_amount"], errors="coerce")
    legs_df["stop_loss"] = pd.to_numeric(legs_df["stop_loss"], errors="coerce")
    return legs_df.dropna(subset=["date", "price"]), effective_bd


@lru_cache(maxsize=None)
def load_close_history(ticker: str) -> pd.DataFrame:
    history = load_cached_price_history_any_end(ticker)
    if history is None or history.empty:
        raise FileNotFoundError(f"Storico prezzi non disponibile in cache per {ticker}.")
    close_history = history.copy()
    close_history.index = pd.to_datetime(close_history.index, errors="coerce").normalize()
    close_history = close_history[close_history.index.notna()]
    if "Close" not in close_history.columns:
        raise ValueError(f"Colonna Close mancante per {ticker}.")
    return close_history.sort_index()


def build_lifecycle_rows(
    *,
    strategy: str,
    progress_row: pd.Series,
    trade_record: dict[str, object],
) -> list[dict[str, object]]:
    bd = pd.Timestamp(progress_row["buy_date"]).normalize()
    screen_date = pd.Timestamp(progress_row["screen_date"]).normalize()
    ticker = str(trade_record["ticker"])
    final_trade_r = trade_record["final_trade_r"]
    source_event_id = f"{strategy}|{screen_date.strftime('%Y-%m-%d')}|{bd.strftime('%Y-%m-%d')}"

    legs_df, effective_bd = load_trade_legs_df_for_bd(bd, ticker)
    close_history = load_close_history(ticker)

    buy_legs = legs_df[legs_df["label"].astype(str).str.upper() == "BUY"].copy()
    if buy_legs.empty:
        raise ValueError(
            f"Nessun BUY leg trovato per {ticker} "
            f"(bd={bd.date()}, effective_bd={effective_bd.date()})."
        )

    entry_leg = buy_legs.sort_values("date").iloc[0]
    bd = pd.Timestamp(entry_leg["date"]).normalize()
    entry_price = float(entry_leg["price"])
    risk_amount = float(entry_leg["risk_amount"])
    stop_loss = float(entry_leg["stop_loss"])
    total_entry_shares = int(buy_legs["shares"].sum())
    if total_entry_shares <= 0:
        raise ValueError(
            f"Quantita iniziale non valida per {ticker} "
            f"(bd={bd.date()}, effective_bd={effective_bd.date()})."
        )

    last_leg_date = pd.Timestamp(legs_df["date"].max()).normalize()
    scoped_history = close_history.loc[(close_history.index >= bd) & (close_history.index <= last_leg_date)].copy()
    if scoped_history.empty:
        raise ValueError(
            f"Nessun close disponibile tra {bd.date()} e {last_leg_date.date()} per {ticker}."
        )

    legs_by_date: dict[pd.Timestamp, pd.DataFrame] = {
        pd.Timestamp(date).normalize(): group.copy()
        for date, group in legs_df.groupby("date", dropna=False)
    }

    initial_value = entry_price * total_entry_shares
    entry_costs = ORDER_COST + (initial_value * FX_COST_PCT)

    shares_open_start = 0
    realized_pnl_cum = 0.0
    lifecycle_rows: list[dict[str, object]] = []

    for current_date, price_row in scoped_history.iterrows():
        current_date = pd.Timestamp(current_date).normalize()
        day_legs = legs_by_date.get(current_date, pd.DataFrame(columns=legs_df.columns))
        shares_bought_day = 0
        shares_sold_day = 0
        realized_pnl_gross_day = 0.0
        realized_costs_day = 0.0
        realized_pnl_day = 0.0
        realized_r_day = 0.0
        buy_reason = ""
        sell_reason = ""

        shares_open_end = shares_open_start
        if not day_legs.empty:
            for _, leg in day_legs.iterrows():
                label = str(leg.get("label", "")).strip().upper()
                leg_shares = int(leg.get("shares", 0))
                leg_price = float(leg.get("price", 0.0))
                if label == "BUY":
                    shares_open_end += leg_shares
                    shares_bought_day += leg_shares
                    buy_reason = str(leg.get("reason", ""))
                elif label == "SELL":
                    shares_open_end -= leg_shares
                    shares_sold_day += leg_shares
                    realized_pnl_gross_leg = (leg_price - entry_price) * leg_shares
                    entry_costs_share = entry_costs * (leg_shares / total_entry_shares)
                    realized_costs_leg = ORDER_COST + entry_costs_share
                    realized_pnl_leg = realized_pnl_gross_leg - realized_costs_leg
                    realized_pnl_gross_day += realized_pnl_gross_leg
                    realized_costs_day += realized_costs_leg
                    realized_pnl_day += realized_pnl_leg
                    realized_r_day += realized_pnl_leg / risk_amount if risk_amount else 0.0
                    sell_reason = str(leg.get("reason", ""))

        close_price = float(price_row["Close"])
        unrealized_pnl_close = (close_price - entry_price) * shares_open_end if shares_open_end > 0 else 0.0
        unrealized_r_close = unrealized_pnl_close / risk_amount if risk_amount else 0.0
        realized_pnl_cum += realized_pnl_day
        open_risk_amount = (risk_amount * shares_open_end / total_entry_shares) if shares_open_end > 0 else 0.0

        lifecycle_rows.append(
            {
                "date": current_date.strftime("%Y-%m-%d"),
                "strategy": strategy,
                "ticker": ticker,
                "source_event_id": source_event_id,
                "source_screen_date": screen_date.strftime("%Y-%m-%d"),
                "bd": bd.strftime("%Y-%m-%d"),
                "trade_index": int(trade_record["trade_index"]),
                "risk_amount": round(risk_amount, 4),
                "entry_price": round(entry_price, 4),
                "stop_loss": round(stop_loss, 4),
                "total_entry_shares": total_entry_shares,
                "shares_open_start": shares_open_start,
                "shares_bought_day": shares_bought_day,
                "shares_sold_day": shares_sold_day,
                "shares_open_end": shares_open_end,
                "close_price": round(close_price, 4),
                "realized_pnl_gross_day": round(realized_pnl_gross_day, 4),
                "realized_costs_day": round(realized_costs_day, 4),
                "realized_pnl_day": round(realized_pnl_day, 4),
                "realized_r_day": round(realized_r_day, 4),
                "realized_pnl_cum": round(realized_pnl_cum, 4),
                "unrealized_pnl_close": round(unrealized_pnl_close, 4),
                "unrealized_r_close": round(unrealized_r_close, 4),
                "open_risk_amount": round(open_risk_amount, 4),
                "position_status": "closed" if shares_open_end == 0 else "open",
                "is_entry_day": current_date == bd,
                "is_exit_day": shares_open_start > 0 and shares_open_end == 0,
                "buy_reason": buy_reason,
                "sell_reason": sell_reason,
                "semaforo_color_source": progress_row["semaforo_color"],
                "core_score_source": int(progress_row["core_score"]),
                "first_screen_count_source": int(progress_row["first_screen_count"]),
                "second_screen_count_source": int(progress_row["second_screen_count"]),
                "selected_count_source": int(progress_row["backtest_selected_count"]),
                "final_trade_r": final_trade_r,
            }
        )
        shares_open_start = shares_open_end

    return lifecycle_rows


def build_lifecycle_rows_from_entry_trade_row(
    *,
    strategy: str,
    entry_row: pd.Series,
) -> list[dict[str, object]]:
    bd_source_col = "bd" if "bd" in entry_row.index else "requested_buy_date"
    bd = pd.Timestamp(entry_row[bd_source_col]).normalize()
    screen_date = pd.Timestamp(entry_row["source_screen_date"]).normalize()
    ticker = str(entry_row["ticker"]).strip().upper()
    trade_index = pd.to_numeric(entry_row.get("selection_rank"), errors="coerce")
    source_event_id = f"{strategy}|{screen_date.strftime('%Y-%m-%d')}|{bd.strftime('%Y-%m-%d')}|{ticker}"
    legs_df, effective_bd = load_trade_legs_frozen_df_for_bd(strategy, bd, ticker)
    close_history = load_close_history(ticker)
    summary_row = load_trade_summary_frozen_row_for_bd(strategy, bd, ticker)
    final_trade_r = pd.to_numeric(summary_row.get("realized_r"), errors="coerce")

    buy_legs = legs_df[legs_df["label"].astype(str).str.upper() == "BUY"].copy()
    if buy_legs.empty:
        raise ValueError(
            f"Nessun BUY leg trovato per {ticker} "
            f"(bd={bd.date()}, effective_bd={effective_bd.date()})."
        )

    entry_leg = buy_legs.sort_values("date").iloc[0]
    bd = pd.Timestamp(entry_leg["date"]).normalize()
    entry_price = float(entry_leg["price"])
    risk_amount = float(entry_leg["risk_amount"])
    stop_loss = float(entry_leg["initial_stop_loss"])
    total_entry_shares = int(buy_legs["shares"].sum())
    if total_entry_shares <= 0:
        raise ValueError(
            f"Quantita iniziale non valida per {ticker} "
            f"(bd={bd.date()}, effective_bd={effective_bd.date()})."
        )

    last_leg_date = pd.Timestamp(legs_df["date"].max()).normalize()
    scoped_history = close_history.loc[(close_history.index >= bd) & (close_history.index <= last_leg_date)].copy()
    if scoped_history.empty:
        raise ValueError(
            f"Nessun close disponibile tra {bd.date()} e {last_leg_date.date()} per {ticker}."
        )

    legs_by_date: dict[pd.Timestamp, pd.DataFrame] = {
        pd.Timestamp(date).normalize(): group.copy()
        for date, group in legs_df.groupby("date", dropna=False)
    }

    initial_value = entry_price * total_entry_shares
    entry_costs = ORDER_COST + (initial_value * FX_COST_PCT)

    shares_open_start = 0
    realized_pnl_cum = 0.0
    lifecycle_rows: list[dict[str, object]] = []

    for current_date, price_row in scoped_history.iterrows():
        current_date = pd.Timestamp(current_date).normalize()
        day_legs = legs_by_date.get(current_date, pd.DataFrame(columns=legs_df.columns))
        shares_bought_day = 0
        shares_sold_day = 0
        realized_pnl_gross_day = 0.0
        realized_costs_day = 0.0
        realized_pnl_day = 0.0
        realized_r_day = 0.0
        buy_reason = ""
        sell_reason = ""

        shares_open_end = shares_open_start
        if not day_legs.empty:
            for _, leg in day_legs.iterrows():
                label = str(leg.get("label", "")).strip().upper()
                leg_shares = int(leg.get("shares", 0))
                leg_price = float(leg.get("price", 0.0))
                if label == "BUY":
                    shares_open_end += leg_shares
                    shares_bought_day += leg_shares
                    buy_reason = str(leg.get("reason", ""))
                elif label == "SELL":
                    shares_open_end -= leg_shares
                    shares_sold_day += leg_shares
                    realized_pnl_gross_leg = (leg_price - entry_price) * leg_shares
                    entry_costs_share = entry_costs * (leg_shares / total_entry_shares)
                    realized_costs_leg = ORDER_COST + entry_costs_share
                    realized_pnl_leg = realized_pnl_gross_leg - realized_costs_leg
                    realized_pnl_gross_day += realized_pnl_gross_leg
                    realized_costs_day += realized_costs_leg
                    realized_pnl_day += realized_pnl_leg
                    realized_r_day += realized_pnl_leg / risk_amount if risk_amount else 0.0
                    sell_reason = str(leg.get("reason", ""))

        close_price = float(price_row["Close"])
        unrealized_pnl_close = (close_price - entry_price) * shares_open_end if shares_open_end > 0 else 0.0
        unrealized_r_close = unrealized_pnl_close / risk_amount if risk_amount else 0.0
        realized_pnl_cum += realized_pnl_day
        open_risk_amount = (risk_amount * shares_open_end / total_entry_shares) if shares_open_end > 0 else 0.0

        lifecycle_rows.append(
            {
                "date": current_date.strftime("%Y-%m-%d"),
                "strategy": strategy,
                "ticker": ticker,
                "source_event_id": source_event_id,
                "source_screen_date": screen_date.strftime("%Y-%m-%d"),
                "bd": bd.strftime("%Y-%m-%d"),
                "trade_index": int(trade_index) if pd.notna(trade_index) else None,
                "risk_amount": round(risk_amount, 4),
                "entry_price": round(entry_price, 4),
                "stop_loss": round(stop_loss, 4),
                "total_entry_shares": total_entry_shares,
                "shares_open_start": shares_open_start,
                "shares_bought_day": shares_bought_day,
                "shares_sold_day": shares_sold_day,
                "shares_open_end": shares_open_end,
                "close_price": round(close_price, 4),
                "realized_pnl_gross_day": round(realized_pnl_gross_day, 4),
                "realized_costs_day": round(realized_costs_day, 4),
                "realized_pnl_day": round(realized_pnl_day, 4),
                "realized_r_day": round(realized_r_day, 4),
                "realized_pnl_cum": round(realized_pnl_cum, 4),
                "unrealized_pnl_close": round(unrealized_pnl_close, 4),
                "unrealized_r_close": round(unrealized_r_close, 4),
                "open_risk_amount": round(open_risk_amount, 4),
                "position_status": "closed" if shares_open_end == 0 else "open",
                "is_entry_day": current_date == bd,
                "is_exit_day": shares_open_start > 0 and shares_open_end == 0,
                "buy_reason": buy_reason,
                "sell_reason": sell_reason,
                "semaforo_color_source": entry_row["semaforo_color"],
                "core_score_source": int(entry_row["core_score"]) if pd.notna(entry_row["core_score"]) else None,
                "first_screen_count_source": int(entry_row["first_screen_count"]) if pd.notna(entry_row["first_screen_count"]) else 0,
                "second_screen_count_source": int(entry_row["second_screen_count"]) if pd.notna(entry_row["second_screen_count"]) else 0,
                "selected_count_source": int(entry_row["selected_count"]) if pd.notna(entry_row["selected_count"]) else 0,
                "final_trade_r": float(final_trade_r) if pd.notna(final_trade_r) else None,
            }
        )
        shares_open_start = shares_open_end

    return lifecycle_rows


def load_storico_spy_context() -> pd.DataFrame:
    if not STORICO_SPY_PATH.exists():
        return pd.DataFrame(columns=["date", "market_street_light", "core_score", "blue_on"])
    storico_df = pd.read_csv(STORICO_SPY_PATH)
    storico_df["date"] = pd.to_datetime(storico_df["date"], errors="coerce").dt.normalize()
    storico_df["blue_on"] = storico_df["blue_on"].fillna(False).astype(bool)
    return storico_df.dropna(subset=["date"]).copy()


def build_portfolio_state_df(lifecycle_df: pd.DataFrame, strategy: str) -> pd.DataFrame:
    if lifecycle_df.empty:
        return pd.DataFrame()

    lifecycle_df = lifecycle_df.copy()
    lifecycle_df["date"] = pd.to_datetime(lifecycle_df["date"], errors="coerce").dt.normalize()
    if "source_screen_date" not in lifecycle_df.columns and "source_target_date" in lifecycle_df.columns:
        lifecycle_df["source_screen_date"] = lifecycle_df["source_target_date"]
    lifecycle_df["source_screen_date"] = pd.to_datetime(
        lifecycle_df["source_screen_date"], errors="coerce"
    ).dt.normalize()
    lifecycle_df["bd"] = pd.to_datetime(
        lifecycle_df["bd"], errors="coerce"
    ).dt.normalize()
    lifecycle_df["bd"] = pd.to_datetime(lifecycle_df["bd"], errors="coerce").dt.normalize()

    daily_rows: list[dict[str, object]] = []
    realized_r_cum = 0.0
    storico_df = load_storico_spy_context()

    for current_date, group in lifecycle_df.groupby("date", dropna=False):
        current_date = pd.Timestamp(current_date).normalize()
        new_entries_count = int(group["is_entry_day"].sum())
        closed_positions_count = int(group["is_exit_day"].sum())
        open_positions_count = int((group["shares_open_end"] > 0).sum())
        realized_r_day = round(float(group["realized_r_day"].sum()), 4)
        realized_r_cum = round(realized_r_cum + realized_r_day, 4)
        unrealized_r_close = round(float(group["unrealized_r_close"].sum()), 4)
        equity_mtm_r = round(realized_r_cum + unrealized_r_close, 4)
        open_risk_amount = round(float(group["open_risk_amount"].sum()), 4)

        new_entry_sources = group[group["is_entry_day"]][
            [
                "source_event_id",
                "semaforo_color_source",
                "core_score_source",
                "first_screen_count_source",
                "second_screen_count_source",
                "selected_count_source",
            ]
        ].drop_duplicates(subset=["source_event_id"])

        if len(new_entry_sources) == 1:
            entry_context = new_entry_sources.iloc[0]
            semaforo_color_source = entry_context["semaforo_color_source"]
            core_score_source = int(entry_context["core_score_source"])
            first_screen_count_source = int(entry_context["first_screen_count_source"])
            second_screen_count_source = int(entry_context["second_screen_count_source"])
            selected_count_source = int(entry_context["selected_count_source"])
        elif len(new_entry_sources) > 1:
            semaforo_color_source = "MULTI"
            core_score_source = None
            first_screen_count_source = int(new_entry_sources["first_screen_count_source"].sum())
            second_screen_count_source = int(new_entry_sources["second_screen_count_source"].sum())
            selected_count_source = int(new_entry_sources["selected_count_source"].sum())
        else:
            semaforo_color_source = None
            core_score_source = None
            first_screen_count_source = 0
            second_screen_count_source = 0
            selected_count_source = 0

        storico_match = storico_df[storico_df["date"] == current_date]
        if not storico_match.empty:
            storico_row = storico_match.iloc[-1]
            market_semaforo_color = str(storico_row.get("market_street_light", "")).strip() or None
            market_core_score = pd.to_numeric(storico_row.get("core_score"), errors="coerce")
            blue_on = bool(storico_row.get("blue_on", False))
        else:
            market_semaforo_color = None
            market_core_score = None
            blue_on = False

        daily_rows.append(
            {
                "date": current_date.strftime("%Y-%m-%d"),
                "strategy": strategy,
                "open_positions_count": open_positions_count,
                "new_entries_count": new_entries_count,
                "closed_positions_count": closed_positions_count,
                "realized_r_day": realized_r_day,
                "realized_r_cum": realized_r_cum,
                "unrealized_r_close": unrealized_r_close,
                "equity_mtm_r": equity_mtm_r,
                "open_risk_amount": open_risk_amount,
                "entry_semaforo_color_source": semaforo_color_source,
                "entry_core_score_source": core_score_source,
                "entry_first_screen_count_source": first_screen_count_source,
                "entry_second_screen_count_source": second_screen_count_source,
                "entry_selected_count_source": selected_count_source,
                "market_semaforo_color": market_semaforo_color,
                "market_core_score": int(market_core_score) if pd.notna(market_core_score) else None,
                "blue_on": blue_on,
            }
        )

    portfolio_df = pd.DataFrame(daily_rows).sort_values("date").reset_index(drop=True)
    equity_curve = portfolio_df["equity_mtm_r"].astype(float)
    portfolio_df["drawdown_mtm_r"] = (equity_curve - equity_curve.cummax()).round(4)
    return portfolio_df


def save_split_by_year(df: pd.DataFrame, strategy: str, filename: str) -> list[Path]:
    if df.empty:
        return []
    output_paths: list[Path] = []
    temp_df = df.copy()
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.normalize()
    temp_df["year"] = temp_df["date"].dt.year

    full_dir = get_trade_timeline_full_dir(strategy, layer="live")
    full_dir.mkdir(parents=True, exist_ok=True)
    full_path = full_dir / filename
    temp_df.drop(columns=["year"], errors="ignore").sort_values(
        ["date", "ticker"] if "ticker" in temp_df.columns else ["date"]
    ).to_csv(full_path, index=False)
    output_paths.append(full_path)

    for year, group in temp_df.groupby("year", dropna=False):
        year_int = int(year)
        output_dir = get_trade_timeline_yearly_dir(year_int, strategy, layer="live")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / filename
        group.drop(columns=["year"]).sort_values(["date", "ticker"] if "ticker" in group.columns else ["date"]).to_csv(
            output_path,
            index=False,
        )
        output_paths.append(output_path)
    return output_paths


def save_trade_lifecycle_outputs(df: pd.DataFrame, strategy: str, *, layer: str = "live") -> list[Path]:
    if df.empty:
        return []

    output_paths: list[Path] = []
    lifecycle_filename = get_trade_lifecycle_filename(layer)
    full_dir = get_trade_timeline_full_dir(strategy, layer=layer)
    full_dir.mkdir(parents=True, exist_ok=True)
    full_path = full_dir / lifecycle_filename
    df.sort_values(["date", "ticker"]).to_csv(full_path, index=False)
    output_paths.append(full_path)

    temp_df = df.copy()
    temp_df["date"] = pd.to_datetime(temp_df["date"], errors="coerce").dt.normalize()
    temp_df["year"] = temp_df["date"].dt.year

    for year, group in temp_df.groupby("year", dropna=False):
        year_int = int(year)
        output_dir = get_trade_timeline_yearly_dir(year_int, strategy, layer=layer)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / lifecycle_filename
        group.drop(columns=["year"]).sort_values(["date", "ticker"]).to_csv(output_path, index=False)
        output_paths.append(output_path)

    return output_paths


def run_build(
    strategy: str,
    start_year: int,
    end_year: int,
    *,
    layer: str = "live",
) -> dict[str, object]:
    lifecycle_rows: list[dict[str, object]] = []
    missing_legs: list[str] = []
    missing_prices: list[str] = []

    if layer == "frozen":
        entry_trade_df = load_entry_trade_source_df(strategy, start_year, end_year)
        if entry_trade_df.empty:
            raise FileNotFoundError("Nessun entry_trade_source trovato per il range richiesto.")
        for _, entry_row in entry_trade_df.iterrows():
            try:
                lifecycle_rows.extend(
                    build_lifecycle_rows_from_entry_trade_row(
                        strategy=strategy,
                        entry_row=entry_row,
                    )
                )
            except FileNotFoundError as exc:
                missing_message = str(exc)
                if "Storico prezzi" in missing_message:
                    missing_prices.append(missing_message)
                else:
                    missing_legs.append(missing_message)
            except Exception as exc:  # noqa: BLE001
                missing_legs.append(
                    f"{entry_row['ticker']} {entry_row['bd']}: {exc}"
                )
    else:
        progress_df = load_progress_df(strategy, start_year, end_year)
        if progress_df.empty:
            raise FileNotFoundError("Nessun progress trovato per il range richiesto.")

        for _, progress_row in progress_df.iterrows():
            if int(progress_row["executed_trades_count"]) <= 0:
                continue
            for trade_record in extract_trade_records(progress_row):
                try:
                    lifecycle_rows.extend(
                        build_lifecycle_rows(
                            strategy=strategy,
                            progress_row=progress_row,
                            trade_record=trade_record,
                        )
                    )
                except FileNotFoundError as exc:
                    missing_message = str(exc)
                    if "Storico prezzi" in missing_message:
                        missing_prices.append(missing_message)
                    else:
                        missing_legs.append(missing_message)
                except Exception as exc:  # noqa: BLE001
                    missing_legs.append(
                        f"{trade_record['ticker']} {progress_row['buy_date']}: {exc}"
                    )

    lifecycle_df = pd.DataFrame(lifecycle_rows)
    portfolio_df = build_portfolio_state_df(lifecycle_df, strategy) if not lifecycle_df.empty else pd.DataFrame()

    lifecycle_paths = save_trade_lifecycle_outputs(lifecycle_df, strategy, layer=layer)
    portfolio_paths = save_split_by_year(portfolio_df, strategy, "portfolio_state_daily.csv")

    return {
        "strategy": strategy,
        "layer": layer,
        "lifecycle_rows": len(lifecycle_df),
        "portfolio_rows": len(portfolio_df),
        "lifecycle_paths": lifecycle_paths,
        "portfolio_paths": portfolio_paths,
        "missing_legs": missing_legs,
        "missing_prices": missing_prices,
    }


def main() -> None:
    args = parse_args()
    result = run_build(args.strategy, args.start_year, args.end_year, layer=args.layer)

    print(f"Strategy: {result['strategy']}")
    print(f"Layer: {result['layer']}")
    print(f"Lifecycle rows: {result['lifecycle_rows']}")
    print(f"Portfolio rows: {result['portfolio_rows']}")
    print(f"{get_trade_lifecycle_filename(result['layer'])} written: {len(result['lifecycle_paths'])} files")
    print(f"portfolio_state_daily.csv written: {len(result['portfolio_paths'])} files")

    if result["missing_legs"]:
        print(f"Missing/unreadable trade legs: {len(result['missing_legs'])}")
        for message in result["missing_legs"][:20]:
            print(f"  - {message}")
    if result["missing_prices"]:
        print(f"Missing price histories: {len(result['missing_prices'])}")
        for message in result["missing_prices"][:20]:
            print(f"  - {message}")


if __name__ == "__main__":
    main()
