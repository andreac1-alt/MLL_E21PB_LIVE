from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from core.screening.first_screen import FirstScreenConfig
from core.market.market import ensure_cache_dirs, load_cached_price_history_any_end


DEFAULT_TICKER = "SPY"
US_MARKET_TIMEZONE = ZoneInfo("America/New_York")
US_DAILY_CLOSE_CONFIRM_TIME = time(16, 15)
SMA5_SLOPE_LOOKBACK_DAYS = 3


def compute_linear_slope(values: pd.Series) -> float:
    cleaned = values.dropna().astype(float)
    if len(cleaned) < 2:
        return 0.0

    x = pd.Series(range(len(cleaned)), dtype="float64")
    y = cleaned.reset_index(drop=True)
    x_mean = x.mean()
    y_mean = y.mean()
    denominator = ((x - x_mean) ** 2).sum()
    if denominator == 0:
        return 0.0
    numerator = ((x - x_mean) * (y - y_mean)).sum()
    return float(numerator / denominator)


@dataclass
class MarketStreetLightResult:
    screen_date: pd.Timestamp
    ticker: str
    rule_5dma: bool
    rule_daily_buy: bool
    rule_weekly_buy: bool
    core_score: int
    market_street_light: str
    close: float
    sma5: float
    sma10: float
    sma20: float
    weekly_close: float
    weekly_sma10: float
    weekly_sma20: float


def classify_market_street_light(
    rule_5dma: bool,
    rule_daily_buy: bool,
    rule_weekly_buy: bool,
) -> str:
    if rule_5dma and rule_daily_buy and rule_weekly_buy:
        return "GREEN"
    if rule_weekly_buy:
        return "BLUE"
    if not rule_5dma and not rule_daily_buy and not rule_weekly_buy:
        return "RED"
    return "YELLOW"


def load_daily_history_from_cache(ticker: str) -> pd.DataFrame:
    ensure_cache_dirs()
    history = load_cached_price_history_any_end(ticker, config=FirstScreenConfig())
    if history is None or history.empty:
        raise ValueError(f"Nessun dato daily disponibile in cache per {ticker}.")
    return history.sort_index().dropna(how="all")


def filter_confirmed_daily_history(
    daily_df: pd.DataFrame,
    *,
    now: datetime | None = None,
) -> pd.DataFrame:
    if daily_df.empty:
        return daily_df

    current_us_time = now.astimezone(US_MARKET_TIMEZONE) if now is not None else datetime.now(US_MARKET_TIMEZONE)
    latest_date = pd.Timestamp(daily_df.index.max()).normalize()
    current_us_date = pd.Timestamp(current_us_time.date()).normalize()

    if latest_date == current_us_date and current_us_time.time() < US_DAILY_CLOSE_CONFIRM_TIME:
        return daily_df.loc[daily_df.index < latest_date]

    return daily_df


def build_weekly_from_daily(daily_df: pd.DataFrame) -> pd.DataFrame:
    weekly = daily_df.resample("W-FRI").agg(
        {
            "Open": "first",
            "High": "max",
            "Low": "min",
            "Close": "last",
            "Adj Close": "last" if "Adj Close" in daily_df.columns else "last",
            "Volume": "sum",
        }
    )
    return weekly.dropna(subset=["Open", "High", "Low", "Close"])


def compute_market_street_light_for_date(
    screen_date: pd.Timestamp,
    ticker: str = DEFAULT_TICKER,
    daily_df: pd.DataFrame | None = None,
) -> MarketStreetLightResult:
    history = daily_df.copy() if daily_df is not None else load_daily_history_from_cache(ticker)
    history = filter_confirmed_daily_history(history)
    scoped = history.loc[:screen_date].copy()

    if len(scoped) < 30:
        raise ValueError(
            f"Non ci sono abbastanza dati daily per calcolare il semaforo su {screen_date.date()}."
        )

    scoped["SMA5"] = scoped["Close"].rolling(5).mean()
    scoped["SMA10"] = scoped["Close"].rolling(10).mean()
    scoped["SMA20"] = scoped["Close"].rolling(20).mean()

    latest = scoped.iloc[-1]
    sma5_slope = compute_linear_slope(scoped["SMA5"].tail(SMA5_SLOPE_LOOKBACK_DAYS))
    rule_5dma = (
        latest["Close"] > latest["SMA5"]
        and sma5_slope > 0
    )
    rule_daily_buy = (
        latest["Close"] > latest["SMA10"]
        and latest["Close"] > latest["SMA20"]
        and latest["SMA10"] > latest["SMA20"]
    )

    weekly = build_weekly_from_daily(scoped)
    if len(weekly) < 20:
        raise ValueError(
            f"Non ci sono abbastanza dati weekly per calcolare il semaforo su {screen_date.date()}."
        )

    weekly["SMA10"] = weekly["Close"].rolling(10).mean()
    weekly["SMA20"] = weekly["Close"].rolling(20).mean()
    latest_week = weekly.iloc[-1]
    rule_weekly_buy = (
        latest_week["Close"] > latest_week["SMA10"]
        and latest_week["Close"] > latest_week["SMA20"]
        and latest_week["SMA10"] > latest_week["SMA20"]
    )

    core_score = int(rule_5dma) + int(rule_daily_buy) + int(rule_weekly_buy)
    market_street_light = classify_market_street_light(
        rule_5dma=rule_5dma,
        rule_daily_buy=rule_daily_buy,
        rule_weekly_buy=rule_weekly_buy,
    )

    return MarketStreetLightResult(
        screen_date=screen_date.normalize(),
        ticker=ticker,
        rule_5dma=rule_5dma,
        rule_daily_buy=rule_daily_buy,
        rule_weekly_buy=rule_weekly_buy,
        core_score=core_score,
        market_street_light=market_street_light,
        close=float(latest["Close"]),
        sma5=float(latest["SMA5"]),
        sma10=float(latest["SMA10"]),
        sma20=float(latest["SMA20"]),
        weekly_close=float(latest_week["Close"]),
        weekly_sma10=float(latest_week["SMA10"]),
        weekly_sma20=float(latest_week["SMA20"]),
    )


def compute_blue_on_for_date(
    screen_date: pd.Timestamp,
    *,
    ticker: str = DEFAULT_TICKER,
    lookback_days: int = 30,
    daily_df: pd.DataFrame | None = None,
) -> tuple[bool, int]:
    history = daily_df.copy() if daily_df is not None else load_daily_history_from_cache(ticker)
    history = filter_confirmed_daily_history(history)
    trading_dates = pd.Index(pd.to_datetime(history.index)).sort_values().unique()
    target_ts = pd.Timestamp(screen_date).normalize()
    current_idx = trading_dates.searchsorted(target_ts, side="left")
    if current_idx >= len(trading_dates) or pd.Timestamp(trading_dates[current_idx]).normalize() != target_ts:
        current_idx = trading_dates.searchsorted(target_ts, side="right") - 1
    if current_idx < 0:
        return False, 0

    start_idx = max(0, current_idx - lookback_days)
    previous_dates = trading_dates[start_idx:current_idx]
    weak_count = 0
    for dt in previous_dates:
        color = compute_market_street_light_for_date(
            pd.Timestamp(dt),
            ticker=ticker,
            daily_df=history,
        ).market_street_light
        if color in {"RED", "YELLOW"}:
            weak_count += 1
    return 5 <= weak_count <= 7, weak_count


def main() -> None:
    screen_date = pd.Timestamp.today().normalize()
    result = compute_market_street_light_for_date(screen_date)
    print(f"Ticker reference: {result.ticker}")
    print(f"Screen date: {result.screen_date.strftime('%Y-%m-%d')}")
    print(f"Rule 5DMA: {result.rule_5dma}")
    print(f"Rule daily buy: {result.rule_daily_buy}")
    print(f"Rule weekly buy: {result.rule_weekly_buy}")
    print(f"Core score: {result.core_score}/3")
    print(f"Market street light: {result.market_street_light}")


if __name__ == "__main__":
    main()
