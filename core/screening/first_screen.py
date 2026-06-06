from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.io.archive_utils import archive_day_dir
from core.market.market import (
    ensure_cache_dirs,
    has_required_price_columns,
    load_market_caps_from_cache_only,
    load_price_cache_metadata,
    load_price_history_from_cache_only,
)
from core.market.operational_universe import load_operational_universe_for_date
from core.config.output_paths import resolve_output_root
from core.config.script_version import build_script_signature


BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = resolve_output_root()
OUTPUT_ARCHIVE_DIR = OUTPUT_DIR / "archivio"
BREADTH_DIR = OUTPUT_DIR / "breadth"
BREADTH_HISTORY_DIR = BREADTH_DIR / "history"
BREADTH_SNAPSHOTS_DIR = BREADTH_DIR / "universe_snapshots"
BREADTH_HISTORY_CSV = BREADTH_HISTORY_DIR / "universe_breadth_daily.csv"
SCRIPT_NAME, SCRIPT_SIGNATURE = build_script_signature(__file__)


def expanding_quantile(series: pd.Series, quantile: float) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce")
    history: list[float] = []
    result: list[float | None] = []
    for value in values:
        if pd.isna(value):
            result.append(None)
            continue
        history.append(float(value))
        result.append(round(float(pd.Series(history).quantile(quantile)), 2))
    return pd.Series(result, index=series.index, dtype="float64")


@dataclass
class FirstScreenConfig:
    min_market_cap: float = 1_000_000_000
    min_price_x_volume_30d: float = 1_000_000_000
    min_avg_volume_30d: float = 600_000
    min_price: float = 9.0
    max_price: float = 350.0
    min_adr14_pct: float = 3.5
    max_adr14_pct: float = 15.0
    min_perf_6m_pct: float = 50.0
    max_distance_from_52w_high_pct: float = 20.0
    sma50_window: int = 50
    sma200_window: int = 200
    avg_volume_window: int = 30
    adr_window: int = 14
    history_buffer_days: int = 420
    batch_size: int = 25
    request_pause_seconds: float = 1.0
    force_refresh_prices: bool = False
    force_refresh_market_caps: bool = False
    incremental_refresh_threshold_days: int = 30
    incremental_refresh_overlap_days: int = 5


@dataclass
class FirstScreenRunResult:
    screen_date: pd.Timestamp
    universe_total: int
    universe_filtered: int
    cache_covered_before_run: int
    price_stats: dict[str, int]
    market_cap_stats: dict[str, int]
    failed_price_tickers: list[str]
    results_df: pd.DataFrame
    passed_df: pd.DataFrame


EXCLUDED_KEYWORDS = [
    " ETF",
    "ETF ",
    "ETF-",
    " EXCHANGE TRADED FUND",
    " ADR",
    "DEPOSITARY",
    "SPONSORED ADS",
    "SPAC",
    " BLANK CHECK",
    "ACQUISITION CORP",
    " ACQUISITION CORPORATION",
    " ACQUISITION COMPANY",
    " ACQUISITION CO",
    " ACQUISITION INC",
    " ACQUISITION LTD",
    " MERGER CORP",
    " MERGER CORPORATION",
    " CONSUMMATION",
    " CAPITAL CORP",
    " CAPITAL CORPORATION",
    " CAPITAL COMPANY",
    " CAPITAL INC.",
    " PFD",
    "PREFERRED",
    "WARRANT",
    " WARRANTS",
    " WT",
    "RIGHT",
    " RIGHTS",
    " UNIT",
    " UNITS",
]

ALLOWED_COMMON_STOCK_KEYWORDS = [
    " COMMON STOCK",
    " COMMON SHARES",
    " ORDINARY SHARES",
    " ORDINARY SHARE",
]


def prompt_screen_date() -> pd.Timestamp:
    raw_value = input("Screen date (YYYY-MM-DD): ").strip()
    try:
        return pd.Timestamp(raw_value).normalize()
    except Exception as exc:  # noqa: BLE001
        raise ValueError("Data non valida. Usa il formato YYYY-MM-DD.") from exc


def prompt_sample_size() -> int | None:
    raw_value = input("Numero ticker da testare (Invio = universo completo): ").strip()
    if not raw_value:
        return None

    try:
        sample_size = int(raw_value)
    except ValueError as exc:
        raise ValueError("Inserisci un numero intero valido.") from exc

    if sample_size <= 0:
        raise ValueError("Il numero di ticker deve essere maggiore di zero.")

    return sample_size


def prompt_yes_no(prompt: str) -> bool:
    answer = input(f"{prompt} [y/N]: ").strip().lower()
    return answer in {"y", "yes", "s", "si"}


def load_universe(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Universe file non trovato: {path}")

    universe = pd.read_csv(path)
    required_columns = {"ticker", "exchange", "name"}
    missing_columns = required_columns - set(universe.columns)
    if missing_columns:
        raise ValueError(f"Colonne mancanti nel file universo: {sorted(missing_columns)}")

    prepared = universe.copy()
    prepared["ticker"] = prepared["ticker"].astype(str).str.strip().str.upper()
    prepared["name"] = prepared["name"].fillna("").astype(str)
    prepared = prepared[~prepared["ticker"].str.startswith("FILE CREATION TIME")]
    prepared = prepared[prepared["ticker"] != ""].drop_duplicates(subset=["ticker"])
    return prepared.reset_index(drop=True)


def is_common_stock(name: str) -> bool:
    normalized_name = f" {name.upper()} "
    has_allowed_common_stock_hint = any(
        keyword in normalized_name for keyword in ALLOWED_COMMON_STOCK_KEYWORDS
    )
    has_excluded_keyword = any(keyword in normalized_name for keyword in EXCLUDED_KEYWORDS)
    return has_allowed_common_stock_hint and not has_excluded_keyword


def filter_universe(universe: pd.DataFrame) -> pd.DataFrame:
    benchmark_overrides = {"SPY", "QQQ"}
    filtered = universe[
        universe["name"].map(is_common_stock) | universe["ticker"].isin(benchmark_overrides)
    ].copy()
    return filtered.reset_index(drop=True)


def compute_download_start(screen_date: pd.Timestamp, config: FirstScreenConfig) -> str:
    start = screen_date - pd.Timedelta(days=config.history_buffer_days)
    return start.strftime("%Y-%m-%d")


def calculate_adr_pct(df: pd.DataFrame, window: int) -> pd.Series:
    daily_range_pct = ((df["High"] - df["Low"]) / df["Close"]) * 100
    return daily_range_pct.rolling(window=window, min_periods=window).mean()


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
    atr = true_range.rolling(window=window, min_periods=window).mean()
    return (atr / df["Close"]) * 100


def round_or_none(value: Any, digits: int = 2) -> float | None:
    if pd.isna(value):
        return None
    return round(float(value), digits)


def analyze_ticker(
    ticker: str,
    name: str,
    exchange: str,
    history: pd.DataFrame,
    market_cap: float | None,
    cache_metadata: dict[str, Any] | None,
    screen_date: pd.Timestamp,
    config: FirstScreenConfig,
) -> dict[str, Any] | None:
    if history.empty or screen_date not in history.index or not has_required_price_columns(history):
        return None

    scoped = history.loc[:screen_date].copy()
    history_sessions = len(scoped)
    if len(scoped) < config.sma50_window:
        return None

    close = scoped["Close"]
    volume = scoped["Volume"]
    high = scoped["High"]

    sma5 = close.rolling(5, min_periods=5).mean()
    sma20 = close.rolling(20, min_periods=20).mean()
    sma50 = close.rolling(config.sma50_window, min_periods=config.sma50_window).mean()
    sma200 = close.rolling(config.sma200_window, min_periods=config.sma200_window).mean()
    avg_volume_30d = volume.rolling(
        config.avg_volume_window,
        min_periods=config.avg_volume_window,
    ).mean()
    adr14 = calculate_adr_pct(scoped, config.adr_window)
    atr14 = calculate_atr_pct(scoped, config.adr_window)

    latest_close = close.iloc[-1]
    latest_sma5 = sma5.iloc[-1]
    latest_sma20 = sma20.iloc[-1]
    latest_sma50 = sma50.iloc[-1]
    latest_sma200 = sma200.iloc[-1]
    latest_avg_volume_30d = avg_volume_30d.iloc[-1]
    volume_30d = volume.rolling(
        config.avg_volume_window,
        min_periods=config.avg_volume_window,
    ).sum()
    latest_volume_30d = volume_30d.iloc[-1]
    latest_adr14 = adr14.iloc[-1]
    latest_atr14 = atr14.iloc[-1]
    high_52w = high.tail(252).max() if len(high) >= 252 else pd.NA
    latest_price_x_volume_30d = latest_close * latest_volume_30d

    if any(
        pd.isna(value)
        for value in [
            latest_close,
            latest_sma50,
            latest_avg_volume_30d,
            latest_volume_30d,
            latest_price_x_volume_30d,
            latest_adr14,
            latest_atr14,
        ]
    ):
        return None

    sma200_missing = pd.isna(latest_sma200)
    high_52w_missing = pd.isna(high_52w)
    perf_6m_missing = len(close) < 126
    perf_6m_pct = (
        ((latest_close / close.iloc[-126]) - 1) * 100
        if not perf_6m_missing
        else pd.NA
    )
    distance_from_52w_high_pct = (
        ((high_52w - latest_close) / high_52w) * 100
        if not high_52w_missing
        else pd.NA
    )

    market_cap_missing = market_cap is None
    oldest_data_reason = None if cache_metadata is None else cache_metadata.get("oldest_data_reason")
    is_recent_ipo = oldest_data_reason == "ipo_or_late_listing"
    if history_sessions < 126:
        history_regime = "too_short_history"
    elif is_recent_ipo:
        history_regime = "recent_ipo"
    elif oldest_data_reason == "requested_window_limit":
        history_regime = "requested_window_limit"
    else:
        history_regime = "full_history"

    rules = {
        "rule_market_cap_gt_1b": market_cap_missing or market_cap > config.min_market_cap,
        "rule_avg_dollar_volume_30d_gt_1b": (
            latest_price_x_volume_30d > config.min_price_x_volume_30d
        ),
        "rule_price_above_sma50": latest_close > latest_sma50,
        "rule_price_above_sma200": sma200_missing or latest_close > latest_sma200,
        "rule_distance_from_52w_high_le_20pct": (
            high_52w_missing
            or 0 <= distance_from_52w_high_pct <= config.max_distance_from_52w_high_pct
        ),
        "rule_perf_6m_gt_50pct": perf_6m_missing or perf_6m_pct > config.min_perf_6m_pct,
        "rule_avg_volume_30d_gt_600k": latest_avg_volume_30d > config.min_avg_volume_30d,
        "rule_adr14_between_3_5_and_15": (
            config.min_adr14_pct <= latest_adr14 <= config.max_adr14_pct
        ),
        "rule_price_between_9_and_350": config.min_price < latest_close < config.max_price,
    }

    return {
        "ticker": ticker,
        "exchange": exchange,
        "name": name,
        "screen_date": screen_date.strftime("%Y-%m-%d"),
        "market_cap": round_or_none(market_cap / 1_000_000_000 if market_cap else None),
        "market_cap_missing": market_cap_missing,
        "history_sessions": history_sessions,
        "oldest_data_reason": oldest_data_reason,
        "is_recent_ipo": is_recent_ipo,
        "history_regime": history_regime,
        "sma200_missing": sma200_missing,
        "high_52w_missing": high_52w_missing,
        "perf_6m_missing": perf_6m_missing,
        "close": round_or_none(latest_close),
        "sma5": round_or_none(latest_sma5),
        "sma20": round_or_none(latest_sma20),
        "sma50": round_or_none(latest_sma50),
        "sma200": round_or_none(latest_sma200),
        "above_sma5": bool(not pd.isna(latest_sma5) and latest_close > latest_sma5),
        "above_sma20": bool(not pd.isna(latest_sma20) and latest_close > latest_sma20),
        "avg_volume_30d": round_or_none(latest_avg_volume_30d, 0),
        "volume_30d": round_or_none(latest_volume_30d, 0),
        "avg_dollar_volume_30d": round_or_none(latest_price_x_volume_30d / 1_000_000_000),
        "price_x_volume_30d": round_or_none(latest_price_x_volume_30d / 1_000_000_000),
        "high_52w": round_or_none(high_52w),
        "distance_from_52w_high_pct": round_or_none(distance_from_52w_high_pct),
        "perf_6m_pct": round_or_none(perf_6m_pct),
        "adr14_pct": round_or_none(latest_adr14),
        "atr14_pct": round_or_none(latest_atr14),
        **rules,
        "passed_first_screen": all(rules.values()),
    }


def ensure_output_dir() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    BREADTH_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    BREADTH_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def dated_output_dir(screen_date: pd.Timestamp) -> Path:
    return archive_day_dir(screen_date)


def archive_existing_outputs(screen_date: pd.Timestamp) -> None:
    ensure_output_dir()
    OUTPUT_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    suffix = screen_date.strftime("%Y%m%d")
    target_dir = dated_output_dir(screen_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    existing_paths = [
        target_dir / f"first_screen_all_{suffix}.csv",
        target_dir / f"first_screen_passed_{suffix}.csv",
        target_dir / f"first_screen_summary_{suffix}.txt",
    ]
    paths_to_archive = [path for path in existing_paths if path.exists()]
    if not paths_to_archive:
        return

    archive_batch = pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = target_dir / archive_batch
    archive_dir.mkdir(parents=True, exist_ok=True)

    for source_path in paths_to_archive:
        source_path.replace(archive_dir / source_path.name)


def save_results(results_df: pd.DataFrame, passed_df: pd.DataFrame, screen_date: pd.Timestamp) -> None:
    ensure_output_dir()
    suffix = screen_date.strftime("%Y%m%d")
    target_dir = dated_output_dir(screen_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    results_to_save = results_df.copy()
    passed_to_save = passed_df.copy()
    results_to_save["writer_script"] = SCRIPT_NAME
    results_to_save["writer_version"] = SCRIPT_SIGNATURE
    passed_to_save["writer_script"] = SCRIPT_NAME
    passed_to_save["writer_version"] = SCRIPT_SIGNATURE
    results_to_save.to_csv(target_dir / f"first_screen_all_{suffix}.csv", index=False)
    passed_to_save.to_csv(target_dir / f"first_screen_passed_{suffix}.csv", index=False)


def save_breadth_universe_snapshot(filtered_universe: pd.DataFrame, screen_date: pd.Timestamp) -> Path:
    ensure_output_dir()
    snapshot_dir = BREADTH_SNAPSHOTS_DIR / screen_date.strftime("%Y-%m-%d")
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "universe_operativo.csv"
    snapshot_df = filtered_universe.copy()
    snapshot_df.insert(0, "effective_date", screen_date.strftime("%Y-%m-%d"))
    snapshot_df.to_csv(snapshot_path, index=False)
    return snapshot_path


def save_breadth_history(
    *,
    screen_date: pd.Timestamp,
    filtered_universe: pd.DataFrame,
    results_df: pd.DataFrame,
    snapshot_path: Path,
) -> None:
    ensure_output_dir()

    universe_total = int(len(filtered_universe))
    valid_sma5_count = int(results_df["sma5"].notna().sum()) if "sma5" in results_df.columns else 0
    valid_sma20_count = int(results_df["sma20"].notna().sum()) if "sma20" in results_df.columns else 0
    above_sma5_count = int(results_df["above_sma5"].fillna(False).astype(bool).sum()) if "above_sma5" in results_df.columns else 0
    above_sma20_count = int(results_df["above_sma20"].fillna(False).astype(bool).sum()) if "above_sma20" in results_df.columns else 0

    row = pd.DataFrame(
        [
            {
                "date": screen_date.strftime("%Y-%m-%d"),
                "effective_date": screen_date.strftime("%Y-%m-%d"),
                "universe_snapshot_path": str(snapshot_path.relative_to(PROJECT_ROOT)),
                "universe_total": universe_total,
                "valid_sma5_count": valid_sma5_count,
                "valid_sma20_count": valid_sma20_count,
                "above_sma5_count": above_sma5_count,
                "above_sma20_count": above_sma20_count,
                "above_sma5_pct": round((above_sma5_count / valid_sma5_count) * 100, 2) if valid_sma5_count else None,
                "above_sma20_pct": round((above_sma20_count / valid_sma20_count) * 100, 2) if valid_sma20_count else None,
                "missing_sma5_count": universe_total - valid_sma5_count,
                "missing_sma20_count": universe_total - valid_sma20_count,
                "created_at": pd.Timestamp.now().isoformat(),
                "writer_script": SCRIPT_NAME,
                "writer_version": SCRIPT_SIGNATURE,
            }
        ]
    )

    if BREADTH_HISTORY_CSV.exists():
        existing_df = pd.read_csv(BREADTH_HISTORY_CSV, keep_default_na=False)
        existing_df = existing_df[existing_df["effective_date"].astype(str) != screen_date.strftime("%Y-%m-%d")]
        history_df = pd.concat([existing_df, row], ignore_index=True)
    else:
        history_df = row

    history_df["effective_date"] = pd.to_datetime(history_df["effective_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    history_df = history_df.sort_values("effective_date").reset_index(drop=True)
    history_df["above_sma20_pct"] = pd.to_numeric(history_df.get("above_sma20_pct"), errors="coerce")
    history_df["above_sma20_pct_p20"] = expanding_quantile(history_df["above_sma20_pct"], 0.20)
    history_df["above_sma20_pct_p10"] = expanding_quantile(history_df["above_sma20_pct"], 0.10)
    history_df.to_csv(BREADTH_HISTORY_CSV, index=False)


def classify_failed_price_tickers(
    failed_price_tickers: list[str],
) -> tuple[list[str], list[str], list[str]]:
    recent_ipo_tickers: list[str] = []
    yahoo_source_limit_tickers: list[str] = []
    other_failed_tickers: list[str] = []

    for ticker in failed_price_tickers:
        metadata = load_price_cache_metadata(ticker) or {}
        if metadata.get("oldest_data_reason") == "ipo_or_late_listing":
            recent_ipo_tickers.append(ticker)
        elif ticker in {"CFLT", "EXAS", "IROQ", "QIPT"}:
            yahoo_source_limit_tickers.append(ticker)
        else:
            other_failed_tickers.append(ticker)

    return recent_ipo_tickers, yahoo_source_limit_tickers, other_failed_tickers


def save_summary(
    screen_date: pd.Timestamp,
    universe_total: int,
    universe_filtered: int,
    cache_covered_before_run: int,
    price_stats: dict[str, int],
    market_cap_stats: dict[str, int],
    results_df: pd.DataFrame,
    passed_df: pd.DataFrame,
    failed_price_tickers: list[str],
) -> None:
    ensure_output_dir()
    suffix = screen_date.strftime("%Y%m%d")
    target_dir = dated_output_dir(screen_date)
    target_dir.mkdir(parents=True, exist_ok=True)
    summary_path = target_dir / f"first_screen_summary_{suffix}.txt"
    coverage_pct = (cache_covered_before_run / universe_filtered * 100) if universe_filtered else 0.0
    (
        recent_ipo_tickers,
        yahoo_source_limit_tickers,
        other_failed_tickers,
    ) = classify_failed_price_tickers(failed_price_tickers)

    lines = [
        f"Screen date: {screen_date.strftime('%Y-%m-%d')}",
        f"Writer script: {SCRIPT_NAME}",
        f"Writer version: {SCRIPT_SIGNATURE}",
        f"Universe total: {universe_total}",
        f"Universe after exclusions: {universe_filtered}",
        f"Price cache coverage before run: {cache_covered_before_run}/{universe_filtered} ({coverage_pct:.2f}%)",
        f"Price history loaded from cache: {price_stats['cached_tickers']}",
        f"Price history requested from Yahoo: {price_stats['requested_from_yahoo']}",
        f"Price history downloaded from Yahoo: {price_stats['downloaded_tickers']}",
        f"Tickers skipped from unavailable-history cache: {price_stats['skipped_unavailable_tickers']}",
        f"Market caps loaded from cache: {market_cap_stats['cached_market_caps']}",
        f"Market caps requested from Yahoo: {market_cap_stats['fetched_market_caps']}",
        f"Tickers analyzed: {len(results_df)}",
        f"Tickers passed first screen: {len(passed_df)}",
        f"Tickers without price history: {len(failed_price_tickers)}",
        f"Tickers excluded as recent IPO / short listed history: {len(recent_ipo_tickers)}",
        f"Tickers excluded for other Yahoo source limits: {len(yahoo_source_limit_tickers)}",
        f"Tickers excluded for other cache/history issues: {len(other_failed_tickers)}",
    ]

    if failed_price_tickers:
        lines.append("Cache prezzi insufficiente: fai warm_cache")
        if recent_ipo_tickers:
            lines.append("Recent IPO / short listed history tickers (first 50):")
            lines.extend(recent_ipo_tickers[:50])
        if yahoo_source_limit_tickers:
            lines.append("Other Yahoo source limit tickers (first 50):")
            lines.extend(yahoo_source_limit_tickers[:50])
        if other_failed_tickers:
            lines.append("Other cache/history issue tickers (first 50):")
            lines.extend(other_failed_tickers[:50])

    summary_path.write_text("\n".join(lines) + "\n")


def run_first_screen_for_date(
    screen_date: pd.Timestamp,
    sample_size: int | None = None,
    config: FirstScreenConfig | None = None,
) -> FirstScreenRunResult:
    def log_timing(step: str, started_at: float) -> None:
        elapsed = time.perf_counter() - started_at
        print(f"[first_screen timing] {step}: {elapsed:.2f}s", flush=True)

    effective_config = config or FirstScreenConfig()
    step_started_at = time.perf_counter()
    ensure_cache_dirs()
    archive_existing_outputs(screen_date)
    log_timing("setup cache dirs + archive outputs", step_started_at)

    step_started_at = time.perf_counter()
    filtered_universe = load_operational_universe_for_date(screen_date)
    log_timing("load operational universe", step_started_at)
    universe = filtered_universe
    if sample_size is not None:
        filtered_universe = filtered_universe.head(sample_size).reset_index(drop=True)
    tickers = filtered_universe["ticker"].tolist()

    step_started_at = time.perf_counter()
    history_by_ticker, failed_price_tickers, price_stats = load_price_history_from_cache_only(
        tickers,
        screen_date,
        effective_config,
    )
    log_timing("load price history from cache only", step_started_at)
    cache_covered_before_run = len(history_by_ticker)

    step_started_at = time.perf_counter()
    market_caps, market_cap_stats = load_market_caps_from_cache_only(list(history_by_ticker.keys()))
    log_timing("load market caps from cache only", step_started_at)

    step_started_at = time.perf_counter()
    results: list[dict[str, Any]] = []
    for row in filtered_universe.itertuples(index=False):
        history = history_by_ticker.get(row.ticker)
        if history is None:
            continue

        result = analyze_ticker(
            ticker=row.ticker,
            name=row.name,
            exchange=row.exchange,
            history=history,
            market_cap=market_caps.get(row.ticker),
            cache_metadata=load_price_cache_metadata(row.ticker),
            screen_date=screen_date,
            config=effective_config,
        )
        if result is not None:
            results.append(result)
    log_timing("analyze ticker loop", step_started_at)

    step_started_at = time.perf_counter()
    if not results:
        print("Nessun risultato disponibile per la data scelta.")
        results_df = pd.DataFrame(columns=["ticker", "exchange", "passed_first_screen"])
        passed_df = pd.DataFrame(columns=["ticker", "exchange", "passed_first_screen"])
    else:
        results_df = pd.DataFrame(results)
        passed_df = results_df[results_df["passed_first_screen"]].reset_index(drop=True)
    log_timing("build result dataframes", step_started_at)

    step_started_at = time.perf_counter()
    save_results(results_df, passed_df, screen_date)
    log_timing("save first screen results", step_started_at)

    step_started_at = time.perf_counter()
    snapshot_path = save_breadth_universe_snapshot(filtered_universe, screen_date)
    log_timing("save breadth universe snapshot", step_started_at)

    step_started_at = time.perf_counter()
    save_breadth_history(
        screen_date=screen_date,
        filtered_universe=filtered_universe,
        results_df=results_df,
        snapshot_path=snapshot_path,
    )
    log_timing("save breadth history", step_started_at)

    step_started_at = time.perf_counter()
    save_summary(
        screen_date=screen_date,
        universe_total=len(universe),
        universe_filtered=len(filtered_universe),
        cache_covered_before_run=cache_covered_before_run,
        price_stats=price_stats,
        market_cap_stats=market_cap_stats,
        results_df=results_df,
        passed_df=passed_df,
        failed_price_tickers=failed_price_tickers,
    )
    log_timing("save summary", step_started_at)

    return FirstScreenRunResult(
        screen_date=screen_date,
        universe_total=len(universe),
        universe_filtered=len(filtered_universe),
        cache_covered_before_run=cache_covered_before_run,
        price_stats=price_stats,
        market_cap_stats=market_cap_stats,
        failed_price_tickers=failed_price_tickers,
        results_df=results_df,
        passed_df=passed_df,
    )


def print_first_screen_run_summary(result: FirstScreenRunResult) -> None:
    print("Carico universo...")
    print(f"Titoli nel file universo: {result.universe_total}")
    print(f"Titoli dopo esclusioni: {result.universe_filtered}")
    print(
        "Copertura cache prezzi prima del run: "
        f"{result.cache_covered_before_run}/{result.universe_filtered}"
    )
    print("Carico dati storici solo da cache...")
    print("Carico market cap solo da cache...")

    print()
    print(f"Titoli analizzati: {len(result.results_df)}")
    print(f"Titoli passati al secondo screen: {len(result.passed_df)}")
    print(f"Ticker senza cache prezzi valida: {len(result.failed_price_tickers)}")
    (
        recent_ipo_tickers,
        yahoo_source_limit_tickers,
        other_failed_tickers,
    ) = classify_failed_price_tickers(result.failed_price_tickers)
    print(f"Di cui IPO troppo recenti / storico corto: {len(recent_ipo_tickers)}")
    print(f"Di cui altri limiti sorgente Yahoo: {len(yahoo_source_limit_tickers)}")
    print(f"Di cui altri problemi di cache/storico: {len(other_failed_tickers)}")
    print(f"Storico da cache: {result.price_stats['cached_tickers']}")
    print(f"Market cap da cache: {result.market_cap_stats['cached_market_caps']}")
    print()
    if result.failed_price_tickers:
        print("Cache prezzi insufficiente: fai warm_cache")
        print()
    if not result.passed_df.empty:
        print(
            result.passed_df[["ticker", "close", "perf_6m_pct", "adr14_pct"]]
            .head(20)
            .to_string(index=False)
        )
    else:
        print("Nessun titolo ha passato il primo screen.")


def main() -> None:
    screen_date = prompt_screen_date()
    sample_size = prompt_sample_size()
    result = run_first_screen_for_date(screen_date, sample_size=sample_size)
    print_first_screen_run_summary(result)


if __name__ == "__main__":
    main()
