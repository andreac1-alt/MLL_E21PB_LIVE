from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.io.archive_utils import archive_day_dir, resolve_archive_file
from core.config.data_paths import PRICE_CACHE_DIR
from core.config.output_paths import resolve_output_root
from core.market.semaforo import compute_blue_on_for_date, compute_market_street_light_for_date
from core.config.script_version import build_script_signature

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = resolve_output_root()
OUTPUT_ARCHIVE_DIR = OUTPUT_DIR / "archivio"
SCRIPT_NAME, SCRIPT_SIGNATURE = build_script_signature(__file__)


@dataclass
class SecondScreenConfig:
    sma10_window: int = 10
    ema21_window: int = 21
    sma50_window: int = 50
    adr_window: int = 14
    max_dist_sma10_pct: float = 10.0
    max_dist_ema21_pct: float = 5.0
    min_ema21_slope_pct_5: float = -0.10
    min_sessions_since_high: int = 5
    max_sessions_since_high: int = 50
    max_prev_day_open_close_diff_pct: float = 3.5
    max_intraday_vs_adr14_multiple: float = 3.0
    max_last_day_excursion_pct: float = 3.5


@dataclass
class SecondScreenRunResult:
    requested_screen_date: pd.Timestamp
    screen_date: pd.Timestamp
    first_passed_count: int
    missing_cache: list[str]
    results_df: pd.DataFrame
    screened_df: pd.DataFrame


def prompt_screen_date() -> pd.Timestamp:
    raw_value = input("Screen date (YYYY-MM-DD): ").strip()
    try:
        return pd.Timestamp(raw_value).normalize()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Data non valida. Usa il formato YYYY-MM-DD.") from exc


def round_or_none(value: Any, digits: int = 2) -> float | None:
    if pd.isna(value):
        return None
    return round(float(value), digits)


def dated_output_dir(screen_date: pd.Timestamp) -> Path:
    return archive_day_dir(screen_date)


def load_first_screen_passed(screen_date: pd.Timestamp) -> pd.DataFrame:
    suffix = screen_date.strftime("%Y%m%d")
    candidate_paths = [
        resolve_archive_file(screen_date, f"first_screen_passed_{suffix}.csv"),
        OUTPUT_DIR / f"first_screen_passed_{suffix}.csv",
    ]
    input_path = next((path for path in candidate_paths if path.exists()), None)
    if input_path is None:
        raise FileNotFoundError(
            "File del primo screen non trovato. Esegui prima first_screen.py "
            f"per la data {screen_date.strftime('%Y-%m-%d')}."
        )
    return pd.read_csv(input_path)


def load_cached_price_history(ticker: str, screen_date: pd.Timestamp) -> pd.DataFrame | None:
    cache_path = PRICE_CACHE_DIR / f"{ticker}.csv"
    if not cache_path.exists():
        return None

    df = pd.read_csv(cache_path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    df = df.sort_index()

    if df.empty or screen_date not in df.index:
        return None
    return df.loc[:screen_date].copy()


def calculate_adr_pct(df: pd.DataFrame, window: int) -> pd.Series:
    daily_range_pct = ((df["High"] - df["Low"]) / df["Close"]) * 100
    return daily_range_pct.rolling(window, min_periods=window).mean()


def calculate_atr_pct(df: pd.DataFrame, window: int) -> pd.Series:
    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(window, min_periods=window).mean()
    return (atr / df["Close"]) * 100


def analyze_ticker(
    ticker: str,
    history: pd.DataFrame,
    config: SecondScreenConfig,
    *,
    exchange: str = "",
    requested_screen_date: pd.Timestamp,
    effective_screen_date: pd.Timestamp,
    semaforo_color: str,
    blue_on: bool,
    blue_on_weak_count: int,
) -> dict[str, Any] | None:
    if history.empty or len(history) < config.sma50_window:
        return None

    close = history["Close"]
    high = history["High"]
    low = history["Low"]
    open_ = history["Open"]
    volume = history["Volume"]

    sma10 = close.rolling(config.sma10_window).mean()
    ema21 = close.ewm(span=config.ema21_window, adjust=False).mean()
    sma50 = close.rolling(config.sma50_window).mean()

    adr_pct = (high - low) / close * 100
    adr14 = adr_pct.rolling(config.adr_window).mean()
    atr14 = calculate_atr_pct(history, config.adr_window)

    adr14_last5 = adr14.dropna().tail(5)
    ema21_last5 = ema21.dropna().tail(5)
    if len(adr14_last5) < 5 or len(ema21_last5) < 5 or len(close) < 6:
        return None

    adr_x = np.arange(len(adr14_last5))
    adr_slope = np.polyfit(adr_x, adr14_last5.values, 1)[0]

    ema_x = np.arange(len(ema21_last5))
    ema21_slope = np.polyfit(ema_x, ema21_last5.values, 1)[0]
    ema21_slope_pct = ema21_slope / ema21_last5.mean() * 100

    last_close = close.iloc[-1]
    last_sma10 = sma10.iloc[-1]
    last_ema21 = ema21.iloc[-1]
    last_sma50 = sma50.iloc[-1]
    last_atr14 = atr14.iloc[-1]
    last_open = open_.iloc[-1]
    prev_open = open_.iloc[-2]
    prev_close = close.iloc[-2]
    recent_intraday_volatility_pct = adr_pct.iloc[-6:-1]
    recent_adr14_reference = adr14.shift(1).iloc[-6:-1]
    days_since_last_price_high = int(np.argmax(high.iloc[::-1].to_numpy()))

    if (
        pd.isna(last_sma10)
        or pd.isna(last_ema21)
        or pd.isna(last_sma50)
        or pd.isna(last_atr14)
        or pd.isna(last_open)
        or pd.isna(prev_open)
        or pd.isna(prev_close)
        or last_open == 0
        or prev_open == 0
        or recent_intraday_volatility_pct.isna().any()
        or recent_adr14_reference.isna().any()
    ):
        return None

    dist_sma10 = (last_close - last_sma10) / last_sma10 * 100
    dist_ema21 = (last_close - last_ema21) / last_ema21 * 100
    dist_sma50 = (last_close - last_sma50) / last_sma50 * 100
    dist_sma50_atr14_multiple = dist_sma50 / last_atr14 if last_atr14 != 0 else pd.NA
    prev_day_open_close_diff_pct = abs(prev_close - prev_open) / prev_open * 100
    rule_sma10 = abs(dist_sma10) < config.max_dist_sma10_pct
    rule_ema21 = abs(dist_ema21) < config.max_dist_ema21_pct
    rule_ema21_slope = ema21_slope_pct >= config.min_ema21_slope_pct_5
    rule_sma50 = last_close > last_sma50
    rule_volatility = adr_slope < 0
    rule_sessions_since_high_in_range = (
        config.min_sessions_since_high <= days_since_last_price_high <= config.max_sessions_since_high
    )
    rule_prev_day_open_close_diff = (
        prev_day_open_close_diff_pct <= config.max_prev_day_open_close_diff_pct
    )
    rule_recent_intraday_vs_adr14 = (
        recent_intraday_volatility_pct <= (recent_adr14_reference * config.max_intraday_vs_adr14_multiple)
    ).all()

    last_day_up_excursion_pct = (high.iloc[-1] - last_open) / last_open * 100
    last_day_down_excursion_pct = (last_open - low.iloc[-1]) / last_open * 100
    rule_last_day_excursion = (
        last_day_up_excursion_pct <= config.max_last_day_excursion_pct
        and last_day_down_excursion_pct <= config.max_last_day_excursion_pct
    )

    close_1, close_2 = close.iloc[-1], close.iloc[-2]
    ema21_1, ema21_2 = ema21.iloc[-1], ema21.iloc[-2]
    rule_ema21_reclaim_yesterday = close_1 > ema21_1 and close_2 < ema21_2

    if len(close) >= 3:
        close_2, close_3 = close.iloc[-2], close.iloc[-3]
        ema21_2, ema21_3 = ema21.iloc[-2], ema21.iloc[-3]
        rule_ema21_reclaim_day_before = close_2 > ema21_2 and close_3 < ema21_3
    else:
        rule_ema21_reclaim_day_before = False

    rule_ema21_reclaim_2d = (
        rule_ema21_reclaim_yesterday or rule_ema21_reclaim_day_before
    )

    passed = (
        rule_sma10
        and rule_ema21
        and rule_ema21_slope
        and rule_sma50
        and rule_sessions_since_high_in_range
        and rule_prev_day_open_close_diff
        and rule_recent_intraday_vs_adr14
        and rule_last_day_excursion
    )

    return {
        "ticker": ticker,
        "exchange": exchange,
        "requested_screen_date": requested_screen_date.strftime("%Y-%m-%d"),
        "effective_screen_date": effective_screen_date.strftime("%Y-%m-%d"),
        "close": round_or_none(last_close),
        "sma50": round_or_none(last_sma50),
        "atr14_pct": round_or_none(last_atr14),
        "dist_sma10_pct": round_or_none(dist_sma10),
        "dist_ema21_pct": round_or_none(dist_ema21),
        "dist_sma50_pct": round_or_none(dist_sma50),
        "dist_sma50_atr14_multiple": round_or_none(dist_sma50_atr14_multiple, 4),
        "semaforo_color": semaforo_color,
        "blue_on": blue_on,
        "blue_on_weak_count": blue_on_weak_count,
        "ema21_slope_5": round_or_none(ema21_slope, 4),
        "ema21_slope_pct_5": round_or_none(ema21_slope_pct, 4),
        "sessions_since_last_h": days_since_last_price_high,
        "adr14_slope": round_or_none(adr_slope, 4),
        "avg_volume_last": round_or_none(volume.tail(30).mean(), 0),
        "rule_sma10": rule_sma10,
        "rule_ema21": rule_ema21,
        "rule_ema21_slope": rule_ema21_slope,
        "rule_price_above_sma50": rule_sma50,
        "rule_volatility": rule_volatility,
        "rule_sessions_since_high_5_50": rule_sessions_since_high_in_range,
        "prev_day_open_close_diff_pct": round_or_none(prev_day_open_close_diff_pct),
        "rule_prev_day_open_close_diff_le_3_5": rule_prev_day_open_close_diff,
        "max_intraday_volatility_prev_5d_pct": round_or_none(recent_intraday_volatility_pct.max()),
        "max_intraday_vs_adr14_multiple_prev_5d": round_or_none(
            (recent_intraday_volatility_pct / recent_adr14_reference).max()
        ),
        "rule_intraday_volatility_prev_5d_le_3x_adr14": rule_recent_intraday_vs_adr14,
        "last_day_up_excursion_pct": round_or_none(last_day_up_excursion_pct),
        "last_day_down_excursion_pct": round_or_none(last_day_down_excursion_pct),
        "rule_last_day_excursion_le_3_5": rule_last_day_excursion,
        "rule_ema21_reclaim_yesterday": rule_ema21_reclaim_yesterday,
        "rule_ema21_reclaim_day_before": rule_ema21_reclaim_day_before,
        "rule_ema21_reclaim_2d": rule_ema21_reclaim_2d,
        "passed_second_screen": passed,
    }


def archive_existing_outputs(screen_date: pd.Timestamp) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = screen_date.strftime("%Y%m%d")
    target_dir = dated_output_dir(screen_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = [
        target_dir / f"second_screen_all_{suffix}.csv",
        target_dir / f"second_screen_passed_{suffix}.csv",
        target_dir / f"second_screen_symbols_{suffix}.txt",
    ]
    paths_to_archive = [path for path in existing_paths if path.exists()]
    if not paths_to_archive:
        return

    archive_batch = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = target_dir / archive_batch
    archive_dir.mkdir(parents=True, exist_ok=True)

    for source_path in paths_to_archive:
        source_path.replace(archive_dir / source_path.name)


def save_results(results_df: pd.DataFrame, screened_df: pd.DataFrame, screen_date: pd.Timestamp) -> None:
    suffix = screen_date.strftime("%Y%m%d")
    target_dir = dated_output_dir(screen_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    results_to_save = results_df.copy()
    screened_to_save = screened_df.copy()
    results_to_save["writer_script"] = SCRIPT_NAME
    results_to_save["writer_version"] = SCRIPT_SIGNATURE
    screened_to_save["writer_script"] = SCRIPT_NAME
    screened_to_save["writer_version"] = SCRIPT_SIGNATURE
    results_to_save.to_csv(target_dir / f"second_screen_all_{suffix}.csv", index=False)
    screened_to_save.to_csv(target_dir / f"second_screen_passed_{suffix}.csv", index=False)

    symbols_string = ",".join(screened_df["ticker"].tolist()) if not screened_df.empty else ""
    (target_dir / f"second_screen_symbols_{suffix}.txt").write_text(symbols_string)


def empty_second_screen_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    results_columns = [
        "ticker",
        "exchange",
        "requested_screen_date",
        "effective_screen_date",
        "close",
        "sma50",
        "atr14_pct",
        "dist_sma10_pct",
        "dist_ema21_pct",
        "dist_sma50_pct",
        "dist_sma50_atr14_multiple",
        "semaforo_color",
        "blue_on",
        "blue_on_weak_count",
        "ema21_slope_5",
        "ema21_slope_pct_5",
        "sessions_since_last_h",
        "adr14_slope",
        "avg_volume_last",
        "rule_sma10",
        "rule_ema21",
        "rule_ema21_slope",
        "rule_price_above_sma50",
        "rule_volatility",
        "rule_sessions_since_high_5_50",
        "prev_day_open_close_diff_pct",
        "rule_prev_day_open_close_diff_le_3_5",
        "max_intraday_volatility_prev_5d_pct",
        "max_intraday_vs_adr14_multiple_prev_5d",
        "rule_intraday_volatility_prev_5d_le_3x_adr14",
        "last_day_up_excursion_pct",
        "last_day_down_excursion_pct",
        "rule_last_day_excursion_le_3_5",
        "rule_ema21_reclaim_yesterday",
        "rule_ema21_reclaim_day_before",
        "rule_ema21_reclaim_2d",
        "passed_second_screen",
        "second_screen_rank",
    ]
    results_df = pd.DataFrame(columns=results_columns)
    screened_df = pd.DataFrame(columns=results_columns)
    return results_df, screened_df


def run_second_screen_for_date(
    screen_date: pd.Timestamp | None = None,
    config: SecondScreenConfig | None = None,
    requested_screen_date: pd.Timestamp | None = None,
) -> SecondScreenRunResult:
    def log_timing(step: str, started_at: float) -> None:
        elapsed = time.perf_counter() - started_at
        print(f"[second_screen timing] {step}: {elapsed:.2f}s", flush=True)

    effective_config = config or SecondScreenConfig()
    if screen_date is None:
        raise ValueError("screen_date obbligatoria.")
    screen_date = pd.Timestamp(screen_date).normalize()

    step_started_at = time.perf_counter()
    archive_existing_outputs(screen_date)
    log_timing("archive existing outputs", step_started_at)

    requested_ts = (
        pd.Timestamp(requested_screen_date).normalize()
        if requested_screen_date is not None
        else pd.Timestamp(screen_date).normalize()
    )
    effective_screen_date = pd.Timestamp(screen_date).normalize()

    step_started_at = time.perf_counter()
    first_passed = load_first_screen_passed(screen_date)
    log_timing("load first screen passed", step_started_at)

    step_started_at = time.perf_counter()
    semaforo_result = compute_market_street_light_for_date(effective_screen_date)
    blue_on, blue_on_weak_count = compute_blue_on_for_date(effective_screen_date)
    log_timing("load semaforo context", step_started_at)

    step_started_at = time.perf_counter()
    results: list[dict[str, Any]] = []
    missing_cache: list[str] = []
    exchange_by_ticker = {}
    if "exchange" in first_passed.columns:
        exchange_by_ticker = (
            first_passed.assign(
                ticker=first_passed["ticker"].astype(str).str.upper().str.strip(),
                exchange=first_passed["exchange"].fillna("").astype(str).str.strip(),
            )
            .drop_duplicates("ticker")
            .set_index("ticker")["exchange"]
            .to_dict()
        )

    for ticker in first_passed["ticker"].astype(str).str.upper():
        history = load_cached_price_history(ticker, screen_date)
        if history is None:
            missing_cache.append(ticker)
            continue
        result = analyze_ticker(
            ticker,
            history,
            effective_config,
            exchange=exchange_by_ticker.get(ticker, ""),
            requested_screen_date=requested_ts,
            effective_screen_date=effective_screen_date,
            semaforo_color=semaforo_result.market_street_light,
            blue_on=blue_on,
            blue_on_weak_count=blue_on_weak_count,
        )
        if result is not None:
            results.append(result)
    log_timing("analyze ticker loop", step_started_at)

    if not results:
        step_started_at = time.perf_counter()
        results_df, screened_df = empty_second_screen_frames()
        save_results(results_df, screened_df, screen_date)
        log_timing("save empty second screen results", step_started_at)
        return SecondScreenRunResult(
            requested_screen_date=requested_ts,
            screen_date=screen_date,
            first_passed_count=len(first_passed),
            missing_cache=missing_cache,
            results_df=results_df,
            screened_df=screened_df,
        )

    step_started_at = time.perf_counter()
    results_df = pd.DataFrame(results)
    screened_df = results_df[results_df["passed_second_screen"]].copy()
    screened_df = screened_df.sort_values(
        by=[
            "rule_ema21_reclaim_yesterday",
            "rule_ema21_reclaim_day_before",
            "rule_volatility",
            "adr14_slope",
        ],
        ascending=[False, False, False, True],
    ).reset_index(drop=True)
    screened_df["second_screen_rank"] = screened_df.index + 1
    results_df = results_df.merge(
        screened_df[["ticker", "second_screen_rank"]],
        on="ticker",
        how="left",
    )
    log_timing("build and rank second screen dataframes", step_started_at)

    step_started_at = time.perf_counter()
    save_results(results_df, screened_df, screen_date)
    log_timing("save second screen results", step_started_at)

    return SecondScreenRunResult(
        requested_screen_date=requested_ts,
        screen_date=screen_date,
        first_passed_count=len(first_passed),
        missing_cache=missing_cache,
        results_df=results_df,
        screened_df=screened_df,
    )


def print_second_screen_run_summary(result: SecondScreenRunResult) -> None:
    if result.results_df.empty:
        print(f"Ticker ricevuti dal primo screen: {result.first_passed_count}")
        print("Ticker valutati nel secondo screen: 0")
        print("Ticker passati al watchlist finale: 0")
        if result.missing_cache:
            print(f"Ticker senza cache locale: {len(result.missing_cache)}")
        else:
            print("Ticker senza cache locale: 0")
        print()
        print("Nessun ticker ha passato il secondo screen.")
        return

    print()
    print(f"Ticker ricevuti dal primo screen: {result.first_passed_count}")
    print(f"Ticker valutati nel secondo screen: {len(result.results_df)}")
    print(f"Ticker passati al watchlist finale: {len(result.screened_df)}")
    print(f"Ticker senza cache locale: {len(result.missing_cache)}")
    print()
    if not result.screened_df.empty:
        print(result.screened_df.head(20).to_string(index=False))
    else:
        print("Nessun ticker ha passato il secondo screen.")


def main() -> None:
    screen_date = prompt_screen_date()
    result = run_second_screen_for_date(screen_date)
    print_second_screen_run_summary(result)


if __name__ == "__main__":
    main()
