from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf

from core.market.semaforo import (
    SMA5_SLOPE_LOOKBACK_DAYS,
    classify_market_street_light,
    compute_linear_slope,
    compute_market_street_light_for_date,
    load_daily_history_from_cache,
)


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"


@dataclass
class MarketStreetLightConfig:
    ticker: str = "SPY"
    output_years: int = 2
    download_years: int = 4


def normalize_downloaded_data(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    normalized = df.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        normalized = normalized.droplevel(1, axis=1)

    normalized.index = pd.to_datetime(normalized.index)
    if getattr(normalized.index, "tz", None) is not None:
        normalized.index = normalized.index.tz_localize(None)
    return normalized.sort_index().dropna(how="all")


def download_daily_history(ticker: str, years: int) -> pd.DataFrame:
    history = yf.download(
        ticker,
        period=f"{years}y",
        interval="1d",
        auto_adjust=False,
        progress=False,
        actions=False,
        threads=False,
    )
    history = normalize_downloaded_data(history)
    if history.empty:
        raise ValueError(f"Nessun dato daily disponibile per {ticker}.")
    return history


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


def compute_daily_signals(daily_df: pd.DataFrame) -> pd.DataFrame:
    signals = daily_df.copy()
    signals["SMA5"] = signals["Close"].rolling(5).mean()
    signals["SMA10"] = signals["Close"].rolling(10).mean()
    signals["SMA20"] = signals["Close"].rolling(20).mean()
    signals["sma5_slope"] = (
        signals["SMA5"].rolling(SMA5_SLOPE_LOOKBACK_DAYS).apply(
            lambda window: compute_linear_slope(pd.Series(window)),
            raw=False,
        )
    )

    signals["rule_5dma"] = (
        (signals["Close"] > signals["SMA5"])
        & (signals["sma5_slope"] > 0)
    )
    signals["rule_daily_buy"] = (
        (signals["Close"] > signals["SMA10"])
        & (signals["Close"] > signals["SMA20"])
        & (signals["SMA10"] > signals["SMA20"])
    )
    return signals


def compute_weekly_signals(daily_df: pd.DataFrame) -> pd.Series:
    values: list[bool | None] = []

    for idx in range(len(daily_df)):
        scoped_daily = daily_df.iloc[: idx + 1].copy()
        weekly_df = build_weekly_from_daily(scoped_daily)

        if len(weekly_df) < 20:
            values.append(None)
            continue

        weekly_df["SMA10"] = weekly_df["Close"].rolling(10).mean()
        weekly_df["SMA20"] = weekly_df["Close"].rolling(20).mean()

        last_week = weekly_df.iloc[-1]
        if pd.isna(last_week["SMA10"]) or pd.isna(last_week["SMA20"]):
            values.append(None)
            continue

        rule_weekly_buy = (
            last_week["Close"] > last_week["SMA10"]
            and last_week["Close"] > last_week["SMA20"]
            and last_week["SMA10"] > last_week["SMA20"]
        )
        values.append(rule_weekly_buy)

    return pd.Series(values, index=daily_df.index, dtype="object")


def compute_market_street_light_history(
    daily_df: pd.DataFrame,
    config: MarketStreetLightConfig,
) -> pd.DataFrame:
    signals = compute_daily_signals(daily_df)
    signals["rule_weekly_buy"] = compute_weekly_signals(daily_df)

    valid = signals.dropna(subset=["SMA5", "SMA10", "SMA20", "rule_weekly_buy"]).copy()
    valid["core_score"] = (
        valid["rule_5dma"].astype(int)
        + valid["rule_daily_buy"].astype(int)
        + valid["rule_weekly_buy"].astype(int)
    )
    valid["market_street_light"] = valid.apply(
        lambda row: classify_market_street_light(
            rule_5dma=bool(row["rule_5dma"]),
            rule_daily_buy=bool(row["rule_daily_buy"]),
            rule_weekly_buy=bool(row["rule_weekly_buy"]),
        ),
        axis=1,
    )

    cutoff_date = valid.index.max() - pd.DateOffset(years=config.output_years)
    result = valid.loc[valid.index >= cutoff_date].copy()
    result = result.reset_index().rename(columns={"Date": "date"})
    result["date"] = result["date"].dt.strftime("%Y-%m-%d")

    return result[
        [
            "date",
            "Close",
            "SMA5",
            "SMA10",
            "SMA20",
            "rule_5dma",
            "rule_daily_buy",
            "rule_weekly_buy",
            "core_score",
            "market_street_light",
        ]
    ].rename(columns={"Close": "close"})


def save_results(result_df: pd.DataFrame, ticker: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = OUTPUT_DIR / f"storico_{ticker}.csv"
    txt_path = OUTPUT_DIR / f"storico_{ticker}.txt"

    result_df.to_csv(csv_path, index=False)

    latest = result_df.iloc[-1]
    lines = [
        f"Ticker reference: {ticker}",
        f"Rows saved: {len(result_df)}",
        f"Latest date: {latest['date']}",
        f"Latest core score: {int(latest['core_score'])}/3",
        f"Latest market street light: {latest['market_street_light']}",
    ]
    txt_path.write_text("\n".join(lines) + "\n")


def update_storico_from_cache(ticker: str = "SPY") -> pd.DataFrame:
    history = load_daily_history_from_cache(ticker)
    trading_dates = pd.Index(pd.to_datetime(history.index)).sort_values().unique()

    rows: list[dict[str, object]] = []
    for target_date in trading_dates:
        target_ts = pd.Timestamp(target_date).normalize()
        try:
            result = compute_market_street_light_for_date(
                target_ts,
                ticker=ticker,
                daily_df=history,
            )
        except Exception:
            continue

        rows.append(
            {
                "date": target_ts.strftime("%Y-%m-%d"),
                "rule_5dma": result.rule_5dma,
                "rule_daily_buy": result.rule_daily_buy,
                "rule_weekly_buy": result.rule_weekly_buy,
                "core_score": result.core_score,
                "market_street_light": result.market_street_light,
            }
        )

    if not rows:
        raise ValueError(f"Impossibile costruire lo storico semaforo per {ticker} dalla cache.")

    result_df = pd.DataFrame(rows)
    weak_flag = result_df["market_street_light"].isin(["RED", "YELLOW"]).astype(int)
    result_df["blue_on_weak_count"] = (
        weak_flag.shift(1).rolling(30, min_periods=1).sum().fillna(0).astype(int)
    )
    result_df["blue_on"] = result_df["blue_on_weak_count"].between(5, 7)
    result_df = result_df[
        [
            "date",
            "rule_5dma",
            "rule_daily_buy",
            "rule_weekly_buy",
            "core_score",
            "market_street_light",
            "blue_on",
            "blue_on_weak_count",
        ]
    ]
    save_results(result_df, ticker)
    return result_df


def main() -> None:
    config = MarketStreetLightConfig()

    print(f"Aggiorno storico semaforo da cache per {config.ticker}...")
    result_df = update_storico_from_cache(config.ticker)

    print()
    print(f"Righe salvate: {len(result_df)}")
    print(f"Periodo coperto: {result_df.iloc[0]['date']} -> {result_df.iloc[-1]['date']}")
    print("Ultime 10 righe:")
    print(result_df.tail(10).to_string(index=False))


if __name__ == "__main__":
    main()
