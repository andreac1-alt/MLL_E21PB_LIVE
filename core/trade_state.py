from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from core.config.output_paths import resolve_output_root
from core.io.archive_utils import resolve_archive_file
from tools.strategy_EMA21_SMA50 import (
    E21_BUFFER_PCT,
    SYSTEM_STOP_BUFFER_PCT,
    SYSTEM_STOP_LOOKBACK_DAYS,
    load_cached_price_history,
)


TRADE_STATE_COLUMNS = [
    "trade_id",
    "ticker",
    "screen_date",
    "buy_date",
    "trade_status",
    "entry_date",
    "entry_price",
    "entry_mode",
    "stop_loss_mode",
    "initial_stop_loss",
    "current_stop_loss",
    "target_close_price",
    "first_take_profit_done",
    "first_take_profit_date",
    "second_exit_done",
    "second_exit_date",
    "skip_reason",
    "close_reason",
    "bars_seen_until",
    "last_update_date",
]

LIVE_STATUSES = {"OPEN", "PARTIAL_1", "PARTIAL_2"}
CLOSED_STATUSES = {"SKIPPED", "CLOSED"}
DEFAULT_MARKET_CALENDAR = "NYSE"


@dataclass(frozen=True)
class EntryDecision:
    triggered: bool
    entry_date: pd.Timestamp | None
    entry_price: float | None
    entry_mode: str
    reason: str


def trade_state_dir_for_date(buy_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(buy_date).normalize()
    return (
        resolve_output_root()
        / "trade_state"
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
    )


def trade_state_path_for_date(buy_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(buy_date).normalize()
    return trade_state_dir_for_date(normalized) / f"trade_state_{normalized.strftime('%Y%m%d')}.csv"


def compute_buy_date_from_screen_date(screen_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(screen_date).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized,
        end_date=normalized + pd.Timedelta(days=10),
    )
    future_sessions = [
        pd.Timestamp(session_date).normalize()
        for session_date in schedule.index
        if pd.Timestamp(session_date).normalize() > normalized
    ]
    if not future_sessions:
        return (normalized + pd.Timedelta(days=1)).normalize()
    return future_sessions[0]


def previous_market_session(buy_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(buy_date).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized - pd.Timedelta(days=10),
        end_date=normalized,
    )
    previous_sessions = [
        pd.Timestamp(session_date).normalize()
        for session_date in schedule.index
        if pd.Timestamp(session_date).normalize() < normalized
    ]
    if not previous_sessions:
        raise ValueError(f"Nessuna seduta precedente trovata per BD={normalized.date()}.")
    return previous_sessions[-1]


def load_previous_trade_state(buy_date: pd.Timestamp) -> pd.DataFrame:
    previous_bd = previous_market_session(buy_date)
    path = trade_state_path_for_date(previous_bd)
    if not path.exists():
        return pd.DataFrame(columns=TRADE_STATE_COLUMNS)
    df = pd.read_csv(path, keep_default_na=False)
    return normalize_trade_state_df(df)


def normalize_trade_state_df(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=TRADE_STATE_COLUMNS)
    result = df.reindex(columns=TRADE_STATE_COLUMNS).copy()
    for col in ["screen_date", "buy_date", "entry_date", "bars_seen_until", "last_update_date"]:
        result[col] = pd.to_datetime(result[col], errors="coerce").dt.normalize()
    for col in ["entry_price", "initial_stop_loss", "current_stop_loss", "target_close_price"]:
        result[col] = pd.to_numeric(result[col], errors="coerce")
    for col in ["first_take_profit_done", "second_exit_done"]:
        result[col] = result[col].fillna(False).astype(bool)
    result["ticker"] = result["ticker"].astype(str).str.strip().str.upper()
    result["trade_status"] = result["trade_status"].astype(str).str.strip().str.upper()
    return result


def find_screen_dates_for_buy_date(buy_date: pd.Timestamp) -> list[pd.Timestamp]:
    normalized_bd = pd.Timestamp(buy_date).normalize()
    year_dir = resolve_output_root() / "screening_day" / normalized_bd.strftime("%Y")
    if not year_dir.exists():
        return []
    screen_dates: list[pd.Timestamp] = []
    for day_dir in sorted(year_dir.glob("*/*")):
        if not day_dir.is_dir() or not day_dir.name.isdigit():
            continue
        screen_date = pd.Timestamp(day_dir.name).normalize()
        if compute_buy_date_from_screen_date(screen_date) == normalized_bd:
            screen_dates.append(screen_date)
    return screen_dates


def load_second_screen_passed(screen_date: pd.Timestamp) -> pd.DataFrame:
    normalized_sd = pd.Timestamp(screen_date).normalize()
    stamp = normalized_sd.strftime("%Y%m%d")
    path = resolve_archive_file(normalized_sd, f"second_screen_passed_{stamp}.csv")
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty:
        return df
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    if "second_screen_rank" in df.columns:
        df["second_screen_rank"] = pd.to_numeric(df["second_screen_rank"], errors="coerce")
        df = df.sort_values(["second_screen_rank", "ticker"], kind="stable")
    return df.dropna(subset=["ticker"]).drop_duplicates(subset=["ticker"], keep="first").copy()


def history_row_for_date(history: pd.DataFrame, date: pd.Timestamp) -> pd.Series:
    normalized = pd.Timestamp(date).normalize()
    working = history.copy()
    working["Date"] = pd.to_datetime(working["Date"], errors="coerce").dt.normalize()
    match = working.loc[working["Date"].eq(normalized)]
    if match.empty:
        raise ValueError(f"Barra non trovata per {normalized.date()}.")
    return match.iloc[-1]


def resolve_entry_decision_for_bd(history: pd.DataFrame, screen_date: pd.Timestamp, buy_date: pd.Timestamp) -> EntryDecision:
    screen_row = history_row_for_date(history, screen_date)
    buy_row = history_row_for_date(history, buy_date)

    close_sd = pd.to_numeric(screen_row.get("Close"), errors="coerce")
    ema21_sd = pd.to_numeric(screen_row.get("EMA21"), errors="coerce")
    high_sd = pd.to_numeric(screen_row.get("High"), errors="coerce")
    low_sd = pd.to_numeric(screen_row.get("Low"), errors="coerce")
    open_bd = pd.to_numeric(buy_row.get("Open"), errors="coerce")
    high_bd = pd.to_numeric(buy_row.get("High"), errors="coerce")
    if any(pd.isna(value) for value in [close_sd, ema21_sd, high_sd, low_sd, open_bd, high_bd]):
        return EntryDecision(False, None, None, "", "dati SD/BD incompleti per la policy di ingresso")

    threshold = float(ema21_sd) * (1 - E21_BUFFER_PCT)
    if float(close_sd) > threshold:
        return EntryDecision(
            True,
            pd.Timestamp(buy_row["Date"]).normalize(),
            float(open_bd),
            "buy_at_open",
            "Ingresso all'open della BD (close SD sopra buffer E21)",
        )

    if float(open_bd) > float(high_sd):
        return EntryDecision(False, None, None, "", "Policy E21 buffer: open BD sopra high SD")
    if float(low_sd) <= float(open_bd) <= float(high_sd):
        return EntryDecision(False, None, None, "", "Policy E21 buffer: open BD dentro il range SD")
    if float(open_bd) < float(low_sd):
        if float(high_bd) >= float(close_sd):
            return EntryDecision(
                True,
                pd.Timestamp(buy_row["Date"]).normalize(),
                float(close_sd),
                "buy_stop_reclaim_close_sd",
                "Ingresso su reclaim del close SD dopo open sotto low SD",
            )
        return EntryDecision(False, None, None, "", "Policy E21 buffer: open sotto low SD ma nessun reclaim close SD")
    return EntryDecision(False, None, None, "", "Policy E21 buffer: configurazione ingresso non gestita")


def resolve_system_stop_loss(history: pd.DataFrame, screen_date: pd.Timestamp) -> float:
    working = history.copy()
    working["Date"] = pd.to_datetime(working["Date"], errors="coerce").dt.normalize()
    screen_date = pd.Timestamp(screen_date).normalize()
    screen_indexes = working.index[working["Date"].eq(screen_date)].tolist()
    if not screen_indexes:
        raise ValueError(f"SD={screen_date.date()} non trovata per calcolo stop.")
    screen_idx = screen_indexes[0]
    prior_history = working.iloc[: screen_idx + 1].copy()
    lookback = prior_history.tail(SYSTEM_STOP_LOOKBACK_DAYS)
    reference_row = prior_history.iloc[-1]
    ema21 = pd.to_numeric(reference_row.get("EMA21"), errors="coerce")
    atr14 = pd.to_numeric(reference_row.get("ATR14"), errors="coerce")
    if pd.isna(ema21) or pd.isna(atr14):
        raise ValueError("EMA21 o ATR14 non disponibili sulla SD per lo stop di sistema.")
    swing_low = float(lookback["Low"].min())
    ema21_minus_atr = float(ema21) - float(atr14)
    return max(swing_low, ema21_minus_atr) * (1 - SYSTEM_STOP_BUFFER_PCT)


def update_live_trade_row(row: pd.Series, buy_date: pd.Timestamp) -> dict[str, object]:
    result = row.to_dict()
    ticker = str(result["ticker"]).upper()
    history = load_cached_price_history(ticker)
    bd_row = history_row_for_date(history, buy_date)
    low_price = float(pd.to_numeric(bd_row["Low"], errors="coerce"))
    close_price = float(pd.to_numeric(bd_row["Close"], errors="coerce"))
    ema21 = pd.to_numeric(bd_row.get("EMA21"), errors="coerce")
    sma50 = pd.to_numeric(bd_row.get("SMA50"), errors="coerce")
    current_stop = float(result["current_stop_loss"])
    status = str(result["trade_status"]).upper()

    if low_price <= current_stop:
        result["trade_status"] = "CLOSED"
        result["close_reason"] = "STOP_LOSS"

    if result["trade_status"] == "PARTIAL_1" and pd.notna(ema21) and close_price < float(ema21):
        result["trade_status"] = "PARTIAL_2"
        result["second_exit_done"] = True
        result["second_exit_date"] = pd.Timestamp(buy_date).normalize()

    if result["trade_status"] == "PARTIAL_2" and pd.notna(sma50) and close_price < float(sma50):
        result["trade_status"] = "CLOSED"
        result["close_reason"] = "SMA50_EXIT"

    result["bars_seen_until"] = pd.Timestamp(buy_date).normalize()
    result["last_update_date"] = pd.Timestamp(buy_date).normalize()
    return result


def build_new_trade_row(
    *,
    ticker: str,
    screen_date: pd.Timestamp,
    buy_date: pd.Timestamp,
    stop_loss_mode: str = "system",
) -> dict[str, object]:
    ticker = str(ticker).strip().upper()
    trade_id = f"{ticker}_{pd.Timestamp(screen_date).strftime('%Y%m%d')}"
    history = load_cached_price_history(ticker)
    decision = resolve_entry_decision_for_bd(history, screen_date, buy_date)
    base = {
        "trade_id": trade_id,
        "ticker": ticker,
        "screen_date": pd.Timestamp(screen_date).normalize(),
        "buy_date": pd.Timestamp(buy_date).normalize(),
        "trade_status": "SKIPPED",
        "entry_date": pd.NaT,
        "entry_price": pd.NA,
        "entry_mode": "",
        "stop_loss_mode": stop_loss_mode,
        "initial_stop_loss": pd.NA,
        "current_stop_loss": pd.NA,
        "target_close_price": pd.NA,
        "first_take_profit_done": False,
        "first_take_profit_date": pd.NaT,
        "second_exit_done": False,
        "second_exit_date": pd.NaT,
        "skip_reason": decision.reason,
        "close_reason": "",
        "bars_seen_until": pd.Timestamp(buy_date).normalize(),
        "last_update_date": pd.Timestamp(buy_date).normalize(),
    }
    if not decision.triggered:
        return base

    if stop_loss_mode != "system":
        raise ValueError("Per il trade_state live iniziale e supportato solo stop_loss_mode=system.")
    initial_stop = resolve_system_stop_loss(history, screen_date)
    base.update(
        {
            "trade_status": "OPEN",
            "entry_date": decision.entry_date,
            "entry_price": decision.entry_price,
            "entry_mode": decision.entry_mode,
            "initial_stop_loss": initial_stop,
            "current_stop_loss": initial_stop,
            "skip_reason": "",
        }
    )
    return update_live_trade_row(pd.Series(base), buy_date)


def save_trade_state(df: pd.DataFrame, buy_date: pd.Timestamp) -> Path:
    path = trade_state_path_for_date(buy_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    output = normalize_trade_state_df(df)
    output = output.sort_values(["screen_date", "ticker"]).reset_index(drop=True)
    for col in ["screen_date", "buy_date", "entry_date", "first_take_profit_date", "second_exit_date", "bars_seen_until", "last_update_date"]:
        output[col] = pd.to_datetime(output[col], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
    output.to_csv(path, index=False)
    return path
