from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import yfinance as yf

from core.config.data_paths import (
    COMPANY_PROFILE_CACHE_DIR,
    MARKET_CAP_CACHE_PATH,
    PRICE_CACHE_DIR,
    PRICE_CACHE_METADATA_DIR,
    PRICE_UNAVAILABLE_CACHE_PATH,
)

BASE_DIR = Path(__file__).resolve().parent
REQUIRED_PRICE_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}


def normalize_downloaded_data(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    normalized = df.copy()
    if isinstance(normalized.columns, pd.MultiIndex):
        flattened_columns = []
        for col in normalized.columns.to_flat_index():
            parts = [str(part).strip() for part in col if str(part).strip()]
            flattened_columns.append(parts[-1] if parts else "")
        normalized.columns = flattened_columns
    normalized.index = pd.to_datetime(normalized.index, errors="coerce", format="mixed")
    normalized = normalized[normalized.index.notna()]
    if getattr(normalized.index, "tz", None) is not None:
        normalized.index = normalized.index.tz_localize(None)
    return normalized.sort_index()


def normalize_downloaded_index_only(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    normalized = df.copy()
    normalized.index = pd.to_datetime(normalized.index, errors="coerce", format="mixed")
    normalized = normalized[normalized.index.notna()]
    if getattr(normalized.index, "tz", None) is not None:
        normalized.index = normalized.index.tz_localize(None)
    return normalized.sort_index()


def has_required_price_columns(df: pd.DataFrame) -> bool:
    columns = {str(column).strip() for column in df.columns}
    return REQUIRED_PRICE_COLUMNS.issubset(columns)


def ensure_cache_dirs() -> None:
    PRICE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PRICE_CACHE_METADATA_DIR.mkdir(parents=True, exist_ok=True)
    COMPANY_PROFILE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    MARKET_CAP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not PRICE_UNAVAILABLE_CACHE_PATH.exists():
        PRICE_UNAVAILABLE_CACHE_PATH.write_text("{}\n")


def price_cache_path_for_ticker(ticker: str) -> Path:
    return PRICE_CACHE_DIR / f"{ticker}.csv"


def price_cache_metadata_path_for_ticker(ticker: str) -> Path:
    return PRICE_CACHE_METADATA_DIR / f"{ticker}.json"


def company_profile_cache_path_for_ticker(ticker: str) -> Path:
    return COMPANY_PROFILE_CACHE_DIR / f"{ticker}.json"


def load_price_cache_metadata(ticker: str) -> dict[str, Any] | None:
    metadata_path = price_cache_metadata_path_for_ticker(ticker)
    if not metadata_path.exists():
        return None
    try:
        return json.loads(metadata_path.read_text())
    except Exception:  # noqa: BLE001
        return None


def load_company_profile(ticker: str) -> dict[str, Any] | None:
    profile_path = company_profile_cache_path_for_ticker(ticker)
    if not profile_path.exists():
        return None
    try:
        loaded = json.loads(profile_path.read_text())
    except Exception:  # noqa: BLE001
        return None
    return loaded if isinstance(loaded, dict) else None


def normalize_profile_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def save_company_profile(
    ticker: str,
    *,
    source: str,
    quote_type: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    long_name: str | None = None,
    short_name: str | None = None,
) -> None:
    profile_path = company_profile_cache_path_for_ticker(ticker)
    payload = {
        "ticker": ticker,
        "source": source,
        "quote_type": normalize_profile_text(quote_type),
        "sector": normalize_profile_text(sector),
        "industry": normalize_profile_text(industry),
        "long_name": normalize_profile_text(long_name),
        "short_name": normalize_profile_text(short_name),
    }
    profile_path.write_text(json.dumps(payload, indent=2) + "\n")


def extract_company_profile_payload(ticker: str, instrument: Any) -> dict[str, Any]:
    info: dict[str, Any] | None = None

    try:
        raw_info = getattr(instrument, "info", None)
        if isinstance(raw_info, dict):
            info = raw_info
    except Exception:  # noqa: BLE001
        info = None

    if info is None:
        try:
            get_info = getattr(instrument, "get_info", None)
            if callable(get_info):
                fetched = get_info()
                if isinstance(fetched, dict):
                    info = fetched
        except Exception:  # noqa: BLE001
            info = None

    info = info or {}
    return {
        "ticker": ticker,
        "source": "yahoo_profile",
        "quote_type": normalize_profile_text(info.get("quoteType")),
        "sector": normalize_profile_text(info.get("sector")),
        "industry": normalize_profile_text(info.get("industry")),
        "long_name": normalize_profile_text(info.get("longName")),
        "short_name": normalize_profile_text(info.get("shortName")),
    }


def normalize_listing_date(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except Exception:  # noqa: BLE001
        return None
    if pd.isna(timestamp):
        return None
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert(None)
    return timestamp.normalize().strftime("%Y-%m-%d")


def normalize_reference_etfs(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, (list, tuple, set)):
        raw_values = list(values)
    else:
        return []

    normalized: list[str] = []
    for value in raw_values:
        text = str(value).strip().upper()
        if not text:
            continue
        if text in {"NONE", "NULL"}:
            continue
        if text in normalized:
            continue
        normalized.append(text)
        if len(normalized) == 4:
            break
    return normalized


def normalize_etf4_type(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"", "none", "null"}:
        return None
    if normalized in {"driver", "sentiment"}:
        return normalized
    return None


def fetch_external_listing_date(ticker: str) -> str | None:
    try:
        instrument = yf.Ticker(ticker)
        metadata = instrument.get_history_metadata()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(metadata, dict):
        return None
    return normalize_listing_date(metadata.get("firstTradeDate"))


def classify_oldest_data_reason(
    oldest_cached_date: pd.Timestamp,
    requested_start_date: pd.Timestamp,
) -> str:
    if oldest_cached_date <= requested_start_date:
        return "requested_window_limit"
    return "ipo_or_late_listing"


def save_price_cache_metadata(
    ticker: str,
    history: pd.DataFrame,
    requested_start_date: pd.Timestamp,
    requested_end_date: pd.Timestamp,
    source: str,
    listing_date: str | None = None,
    reference_etfs: list[str] | None = None,
    etf4_type: str | None = None,
) -> None:
    metadata_path = price_cache_metadata_path_for_ticker(ticker)
    oldest_cached_date = pd.Timestamp(history.index.min()).normalize()
    newest_cached_date = pd.Timestamp(history.index.max()).normalize()
    existing_metadata = load_price_cache_metadata(ticker) or {}
    metadata = {
        "ticker": ticker,
        "source": source,
        "requested_start_date": requested_start_date.strftime("%Y-%m-%d"),
        "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
        "oldest_cached_date": oldest_cached_date.strftime("%Y-%m-%d"),
        "newest_cached_date": newest_cached_date.strftime("%Y-%m-%d"),
        "listing_date": normalize_listing_date(listing_date),
        "oldest_data_reason": classify_oldest_data_reason(
            oldest_cached_date=oldest_cached_date,
            requested_start_date=requested_start_date,
        ),
        "history_rows": int(len(history)),
        "reference_etfs": normalize_reference_etfs(
            existing_metadata.get("reference_etfs") if reference_etfs is None else reference_etfs,
        ),
        "etf4_type": normalize_etf4_type(
            existing_metadata.get("etf4_type") if etf4_type is None else etf4_type,
        ),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")


def backfill_legacy_price_cache_metadata(
    ticker: str,
    history: pd.DataFrame,
    config: Any,
) -> None:
    metadata_path = price_cache_metadata_path_for_ticker(ticker)
    if metadata_path.exists() or history.empty:
        return

    inferred_end_date = pd.Timestamp(history.index.max()).normalize()
    inferred_start_date = inferred_end_date - pd.Timedelta(days=config.history_buffer_days)
    oldest_cached_date = pd.Timestamp(history.index.min()).normalize()
    metadata = {
        "ticker": ticker,
        "source": "legacy_cache_backfill",
        "requested_start_date": inferred_start_date.strftime("%Y-%m-%d"),
        "requested_end_date": inferred_end_date.strftime("%Y-%m-%d"),
        "oldest_cached_date": oldest_cached_date.strftime("%Y-%m-%d"),
        "newest_cached_date": inferred_end_date.strftime("%Y-%m-%d"),
        "listing_date": None,
        "oldest_data_reason": classify_oldest_data_reason(
            oldest_cached_date=oldest_cached_date,
            requested_start_date=inferred_start_date,
        ),
        "history_rows": int(len(history)),
        "is_inferred": True,
        "reference_etfs": [],
        "etf4_type": None,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n")


def load_unavailable_history_cache() -> dict[str, dict[str, str]]:
    if not PRICE_UNAVAILABLE_CACHE_PATH.exists():
        return {}
    try:
        loaded = json.loads(PRICE_UNAVAILABLE_CACHE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(loaded, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for ticker, payload in loaded.items():
        if isinstance(payload, dict):
            normalized[str(ticker)] = {
                str(key): str(value)
                for key, value in payload.items()
            }
    return normalized


def save_unavailable_history_cache(cache: dict[str, dict[str, str]]) -> None:
    PRICE_UNAVAILABLE_CACHE_PATH.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n"
    )


def unavailable_history_key(
    requested_start_date: pd.Timestamp,
    requested_end_date: pd.Timestamp,
) -> str:
    return (
        f"{requested_start_date.strftime('%Y-%m-%d')}"
        f"__{requested_end_date.strftime('%Y-%m-%d')}"
    )


def should_skip_unavailable_ticker(
    ticker: str,
    unavailable_cache: dict[str, dict[str, str]],
    requested_start_date: pd.Timestamp,
    requested_end_date: pd.Timestamp,
    grace_days: int = 7,
) -> bool:
    payload = unavailable_cache.get(ticker)
    if not payload:
        return False

    availability_kind = payload.get("availability_kind")
    if not availability_kind:
        legacy_reason = payload.get("reason", "")
        if legacy_reason in {"empty_ticker_frame"}:
            availability_kind = "structural_no_history"

    if availability_kind != "structural_no_history":
        return False

    cached_start = payload.get("requested_start_date")
    cached_end = payload.get("requested_end_date")
    if cached_start and cached_end:
        try:
            cached_start_ts = pd.Timestamp(cached_start).normalize()
            cached_end_ts = pd.Timestamp(cached_end).normalize()
        except Exception:  # noqa: BLE001
            return False

        if requested_start_date >= cached_start_ts and requested_end_date >= cached_end_ts:
            delta_days = (requested_end_date - cached_end_ts).days
            return 0 <= delta_days <= grace_days

        return False

    return payload.get("window") == unavailable_history_key(
        requested_start_date,
        requested_end_date,
    )


def get_known_listing_date(ticker: str) -> pd.Timestamp | None:
    metadata = load_price_cache_metadata(ticker) or {}
    listing_date = normalize_listing_date(metadata.get("listing_date"))
    if listing_date is None:
        listing_date = fetch_external_listing_date(ticker)
    if listing_date is None:
        return None
    try:
        return pd.Timestamp(listing_date).normalize()
    except Exception:  # noqa: BLE001
        return None


def window_ends_before_listing_date(
    ticker: str,
    requested_end_date: pd.Timestamp,
) -> pd.Timestamp | None:
    listing_date = get_known_listing_date(ticker)
    if listing_date is None:
        return None
    if requested_end_date < listing_date:
        return listing_date
    return None


def resolve_effective_download_start_date(
    ticker: str,
    requested_start_date: pd.Timestamp,
    requested_end_date: pd.Timestamp,
) -> tuple[pd.Timestamp, pd.Timestamp | None]:
    listing_date = get_known_listing_date(ticker)
    if listing_date is None:
        return requested_start_date, None
    if requested_end_date < listing_date:
        return requested_start_date, listing_date
    return max(requested_start_date, listing_date), None


def load_cached_price_history(
    ticker: str,
    target_date: pd.Timestamp,
    min_required_sessions: int | None = None,
    config: Any | None = None,
) -> pd.DataFrame | None:
    cache_path = price_cache_path_for_ticker(ticker)
    if not cache_path.exists():
        return None

    try:
        cached = pd.read_csv(cache_path, index_col=0)
        if str(cached.index[0]).strip().lower() == "price":
            cached = pd.read_csv(cache_path, header=[0, 1], index_col=0)
            if isinstance(cached.columns, pd.MultiIndex):
                flattened_columns = []
                for col in cached.columns.to_flat_index():
                    parts = [str(part).strip() for part in col if str(part).strip()]
                    flattened_columns.append(parts[-1] if parts else "")
                cached.columns = flattened_columns
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"price cache read failed ticker={ticker} path={cache_path} target_date={target_date.strftime('%Y-%m-%d')}"
        ) from exc

    cached = normalize_downloaded_data(cached)
    if cached.empty or not has_required_price_columns(cached):
        return None
    if target_date not in cached.index:
        return None

    if config is not None:
        backfill_legacy_price_cache_metadata(ticker, cached, config)

    if min_required_sessions is not None:
        scoped = cached.loc[:target_date]
        if len(scoped) < min_required_sessions:
            return None

    return cached


def load_cached_price_history_any_end(
    ticker: str,
    config: Any | None = None,
) -> pd.DataFrame | None:
    cache_path = price_cache_path_for_ticker(ticker)
    if not cache_path.exists():
        return None

    try:
        cached = pd.read_csv(cache_path, index_col=0)
        if str(cached.index[0]).strip().lower() == "price":
            cached = pd.read_csv(cache_path, header=[0, 1], index_col=0)
            if isinstance(cached.columns, pd.MultiIndex):
                flattened_columns = []
                for col in cached.columns.to_flat_index():
                    parts = [str(part).strip() for part in col if str(part).strip()]
                    flattened_columns.append(parts[-1] if parts else "")
                cached.columns = flattened_columns
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"price cache read failed ticker={ticker} path={cache_path}"
        ) from exc

    cached = normalize_downloaded_data(cached)
    if cached.empty or not has_required_price_columns(cached):
        return None

    if config is not None:
        backfill_legacy_price_cache_metadata(ticker, cached, config)

    return cached


def get_min_required_sessions(config: Any) -> int:
    return max(
        config.sma50_window,
        config.avg_volume_window,
        config.adr_window,
    )


def cached_history_is_sufficient(
    ticker: str,
    target_date: pd.Timestamp,
    config: Any,
) -> pd.DataFrame | None:
    min_required_sessions = get_min_required_sessions(config)
    cached = load_cached_price_history(
        ticker,
        target_date,
        min_required_sessions=min_required_sessions,
        config=config,
    )
    if cached is None:
        return None
    return cached


def get_incremental_fetch_start_date(
    ticker: str,
    target_date: pd.Timestamp,
    config: Any,
) -> pd.Timestamp | None:
    cached = load_cached_price_history_any_end(ticker, config=config)
    if cached is None:
        return None

    scoped = cached.loc[:target_date]
    min_required_sessions = get_min_required_sessions(config)
    if len(scoped) < min_required_sessions:
        return None

    newest_cached_date = pd.Timestamp(scoped.index.max()).normalize()
    if newest_cached_date >= target_date:
        return None

    missing_days = (target_date - newest_cached_date).days
    if missing_days <= 0 or missing_days > config.incremental_refresh_threshold_days:
        return None

    overlap_days = min(config.incremental_refresh_overlap_days, missing_days)
    return newest_cached_date - pd.Timedelta(days=overlap_days)


def save_cached_price_history(
    ticker: str,
    history: pd.DataFrame,
    requested_start_date: pd.Timestamp,
    requested_end_date: pd.Timestamp,
    source: str,
) -> None:
    cache_path = price_cache_path_for_ticker(ticker)
    history_to_save = normalize_downloaded_data(history)
    existing_requested_start_date = requested_start_date
    existing_requested_end_date = requested_end_date
    existing_metadata = load_price_cache_metadata(ticker)
    listing_date = None if existing_metadata is None else normalize_listing_date(
        existing_metadata.get("listing_date"),
    )
    had_existing_cache = cache_path.exists()
    existing_valid_history: pd.DataFrame | None = None

    if had_existing_cache:
        try:
            existing = pd.read_csv(cache_path, index_col=0)
            if str(existing.index[0]).strip().lower() == "price":
                existing = pd.read_csv(cache_path, header=[0, 1], index_col=0)
            existing = normalize_downloaded_data(existing)
            if not existing.empty and has_required_price_columns(existing):
                existing_valid_history = existing.copy()
                history_to_save = pd.concat([existing, history_to_save]).sort_index()
                history_to_save = history_to_save[~history_to_save.index.duplicated(keep="last")]
        except Exception:  # noqa: BLE001
            history_to_save = normalize_downloaded_data(history)

    if existing_valid_history is not None:
        if not has_required_price_columns(history_to_save):
            history_to_save = existing_valid_history
        else:
            existing_first = pd.Timestamp(existing_valid_history.index.min()).normalize()
            existing_last = pd.Timestamp(existing_valid_history.index.max()).normalize()
            merged_first = pd.Timestamp(history_to_save.index.min()).normalize()
            merged_last = pd.Timestamp(history_to_save.index.max()).normalize()
            if (
                len(history_to_save) < len(existing_valid_history)
                or merged_first > existing_first
                or merged_last < existing_last
            ):
                history_to_save = existing_valid_history

    if existing_metadata is not None:
        metadata_start = existing_metadata.get("requested_start_date")
        metadata_end = existing_metadata.get("requested_end_date")
        if metadata_start:
            try:
                existing_requested_start_date = min(
                    requested_start_date,
                    pd.Timestamp(metadata_start).normalize(),
                )
            except Exception:  # noqa: BLE001
                existing_requested_start_date = requested_start_date
        if metadata_end:
            try:
                existing_requested_end_date = max(
                    requested_end_date,
                    pd.Timestamp(metadata_end).normalize(),
                )
            except Exception:  # noqa: BLE001
                existing_requested_end_date = requested_end_date

    history_to_save.to_csv(cache_path)
    if listing_date is None:
        listing_date = fetch_external_listing_date(ticker)
    save_price_cache_metadata(
        ticker=ticker,
        history=history_to_save,
        requested_start_date=existing_requested_start_date,
        requested_end_date=existing_requested_end_date,
        source="merged_cache" if had_existing_cache else source,
        listing_date=listing_date,
        reference_etfs=None if existing_metadata is None else existing_metadata.get("reference_etfs"),
        etf4_type=None if existing_metadata is None else existing_metadata.get("etf4_type"),
    )


def load_market_cap_cache() -> dict[str, float | None]:
    if not MARKET_CAP_CACHE_PATH.exists():
        return {}

    cached = pd.read_csv(MARKET_CAP_CACHE_PATH)
    if cached.empty:
        return {}

    cache: dict[str, float | None] = {}
    for row in cached.itertuples(index=False):
        cache[str(row.ticker)] = None if pd.isna(row.market_cap) else float(row.market_cap)
    return cache


def save_market_cap_cache(cache: dict[str, float | None]) -> None:
    rows = [
        {"ticker": ticker, "market_cap": value}
        for ticker, value in sorted(cache.items())
    ]
    pd.DataFrame(rows).to_csv(MARKET_CAP_CACHE_PATH, index=False)


def get_price_cache_coverage(
    tickers: list[str],
    target_date: pd.Timestamp,
    config: Any,
) -> tuple[int, int]:
    covered = 0
    min_required_sessions = get_min_required_sessions(config)
    for ticker in tickers:
        try:
            cached_history = load_cached_price_history(
                ticker,
                target_date,
                min_required_sessions=min_required_sessions,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"price cache coverage failed ticker={ticker} target_date={target_date.strftime('%Y-%m-%d')}"
            ) from exc
        if cached_history is not None:
            covered += 1
    return covered, len(tickers)


def load_price_history_from_cache_only(
    tickers: list[str],
    target_date: pd.Timestamp,
    config: Any,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, int]]:
    history_by_ticker: dict[str, pd.DataFrame] = {}
    missing_tickers: list[str] = []

    for ticker in tickers:
        try:
            cached_history = cached_history_is_sufficient(ticker, target_date, config)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"price cache load failed ticker={ticker} target_date={target_date.strftime('%Y-%m-%d')}"
            ) from exc
        if cached_history is None:
            missing_tickers.append(ticker)
            continue
        history_by_ticker[ticker] = cached_history

    return history_by_ticker, missing_tickers, {
        "cached_tickers": len(history_by_ticker),
        "downloaded_tickers": 0,
        "requested_from_yahoo": 0,
        "skipped_unavailable_tickers": 0,
    }


def fetch_price_history(
    tickers: list[str],
    target_date: pd.Timestamp,
    config: Any,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str], dict[str, int]]:
    history_by_ticker: dict[str, pd.DataFrame] = {}
    failed_tickers: list[str] = []
    cached_tickers = 0
    downloaded_tickers = 0
    requested_start_date = target_date - pd.Timedelta(days=config.history_buffer_days)
    requested_end_date = target_date + pd.Timedelta(days=1)
    unavailable_cache = load_unavailable_history_cache()
    skipped_unavailable_tickers = 0
    tickers_to_download_by_start: dict[pd.Timestamp, list[str]] = {}

    def emit(payload: dict[str, Any]) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    for ticker in tickers:
        if not config.force_refresh_prices:
            cached_history = cached_history_is_sufficient(ticker, target_date, config)
            if cached_history is not None:
                history_by_ticker[ticker] = cached_history
                cached_tickers += 1
                continue
        effective_requested_start_date, listing_date_after_window = (
            resolve_effective_download_start_date(
                ticker=ticker,
                requested_start_date=requested_start_date,
                requested_end_date=requested_end_date,
            )
        )
        if listing_date_after_window is not None:
            failed_tickers.append(ticker)
            unavailable_cache[ticker] = {
                "window": unavailable_history_key(requested_start_date, requested_end_date),
                "requested_start_date": requested_start_date.strftime("%Y-%m-%d"),
                "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
                "availability_kind": "structural_no_history",
                "reason": "listing_date_after_requested_window",
                "listing_date": listing_date_after_window.strftime("%Y-%m-%d"),
            }
            skipped_unavailable_tickers += 1
            continue
        if (
            not config.force_refresh_prices
            and should_skip_unavailable_ticker(
                ticker=ticker,
                unavailable_cache=unavailable_cache,
                requested_start_date=requested_start_date,
                requested_end_date=requested_end_date,
            )
        ):
            failed_tickers.append(ticker)
            skipped_unavailable_tickers += 1
            continue
        download_start_date = effective_requested_start_date
        if not config.force_refresh_prices:
            incremental_start_date = get_incremental_fetch_start_date(
                ticker,
                target_date,
                config,
            )
            if incremental_start_date is not None:
                download_start_date = incremental_start_date
        tickers_to_download_by_start.setdefault(download_start_date, []).append(ticker)

    if cached_tickers:
        print(f"Storico caricato da cache per {cached_tickers} ticker.")

    total_download_tickers = sum(len(batch) for batch in tickers_to_download_by_start.values())
    total_batches = sum(
        (len(batch) + config.batch_size - 1) // config.batch_size
        for batch in tickers_to_download_by_start.values()
    )
    if total_batches == 0:
        emit(
            {
                "phase": "price_done",
                "total_tickers": len(tickers),
                "cached_tickers": cached_tickers,
                "downloaded_tickers": downloaded_tickers,
                "failed_tickers": len(set(failed_tickers)),
                "requested_from_yahoo": 0,
                "skipped_unavailable_tickers": skipped_unavailable_tickers,
            }
        )
        return history_by_ticker, failed_tickers, {
            "cached_tickers": cached_tickers,
            "downloaded_tickers": downloaded_tickers,
            "requested_from_yahoo": 0,
            "skipped_unavailable_tickers": skipped_unavailable_tickers,
        }

    batch_number = 0
    emit(
        {
            "phase": "price_start",
            "total_tickers": len(tickers),
            "cached_tickers": cached_tickers,
            "download_candidates": total_download_tickers,
            "total_batches": total_batches,
            "requested_from_yahoo": total_download_tickers,
            "skipped_unavailable_tickers": skipped_unavailable_tickers,
        }
    )
    for batch_start_date in sorted(tickers_to_download_by_start):
        tickers_to_download = tickers_to_download_by_start[batch_start_date]
        start = batch_start_date.strftime("%Y-%m-%d")
        end = requested_end_date.strftime("%Y-%m-%d")
        batch_unavailable_key = unavailable_history_key(batch_start_date, requested_end_date)

        for start_idx in range(0, len(tickers_to_download), config.batch_size):
            batch = tickers_to_download[start_idx : start_idx + config.batch_size]
            batch_number += 1
            print(
                f"Download prezzi batch {batch_number}/{total_batches} "
                f"({len(batch)} ticker, start={start})..."
            )
            emit(
                {
                    "phase": "price_batch_start",
                    "batch_number": batch_number,
                    "total_batches": total_batches,
                    "batch_size": len(batch),
                    "batch_start": start,
                    "total_tickers": len(tickers),
                    "cached_tickers": cached_tickers,
                    "downloaded_tickers": downloaded_tickers,
                    "failed_tickers": len(set(failed_tickers)),
                    "requested_from_yahoo": total_download_tickers,
                    "skipped_unavailable_tickers": skipped_unavailable_tickers,
                }
            )

            try:
                downloaded = yf.download(
                    batch,
                    start=start,
                    end=end,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=False,
                    progress=False,
                    actions=False,
                    threads=True,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"Batch fallito: {exc}")
                failed_tickers.extend(batch)
                emit(
                    {
                        "phase": "price_batch_error",
                        "batch_number": batch_number,
                        "total_batches": total_batches,
                        "batch_size": len(batch),
                        "batch_start": start,
                        "error": str(exc),
                        "total_tickers": len(tickers),
                        "cached_tickers": cached_tickers,
                        "downloaded_tickers": downloaded_tickers,
                        "failed_tickers": len(set(failed_tickers)),
                        "requested_from_yahoo": total_download_tickers,
                        "skipped_unavailable_tickers": skipped_unavailable_tickers,
                    }
                )
                time.sleep(config.request_pause_seconds)
                continue

            downloaded = normalize_downloaded_index_only(downloaded)

            if downloaded.empty:
                failed_tickers.extend(batch)
                for ticker in batch:
                    unavailable_cache[ticker] = {
                        "window": batch_unavailable_key,
                        "requested_start_date": batch_start_date.strftime("%Y-%m-%d"),
                        "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
                        "availability_kind": "transient_yahoo_error",
                        "reason": "empty_download",
                    }
                emit(
                    {
                        "phase": "price_batch_done",
                        "batch_number": batch_number,
                        "total_batches": total_batches,
                        "batch_size": len(batch),
                        "batch_start": start,
                        "total_tickers": len(tickers),
                        "cached_tickers": cached_tickers,
                        "downloaded_tickers": downloaded_tickers,
                        "failed_tickers": len(set(failed_tickers)),
                        "requested_from_yahoo": total_download_tickers,
                        "skipped_unavailable_tickers": skipped_unavailable_tickers,
                    }
                )
                time.sleep(config.request_pause_seconds)
                continue

            if len(batch) == 1:
                single_df = normalize_downloaded_data(downloaded.dropna(how="all").copy())
                if single_df.empty:
                    failed_tickers.append(batch[0])
                    unavailable_cache[batch[0]] = {
                        "window": batch_unavailable_key,
                        "requested_start_date": batch_start_date.strftime("%Y-%m-%d"),
                        "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
                        "availability_kind": "structural_no_history",
                        "reason": "empty_ticker_frame",
                    }
                else:
                    history_by_ticker[batch[0]] = single_df
                    save_cached_price_history(
                        batch[0],
                        single_df,
                        requested_start_date=batch_start_date,
                        requested_end_date=requested_end_date,
                        source="yahoo_download",
                    )
                    unavailable_cache.pop(batch[0], None)
                    downloaded_tickers += 1
                emit(
                    {
                        "phase": "price_batch_done",
                        "batch_number": batch_number,
                        "total_batches": total_batches,
                        "batch_size": len(batch),
                        "batch_start": start,
                        "total_tickers": len(tickers),
                        "cached_tickers": cached_tickers,
                        "downloaded_tickers": downloaded_tickers,
                        "failed_tickers": len(set(failed_tickers)),
                        "requested_from_yahoo": total_download_tickers,
                        "skipped_unavailable_tickers": skipped_unavailable_tickers,
                    }
                )
                time.sleep(config.request_pause_seconds)
                continue

            for ticker in batch:
                if ticker not in downloaded.columns.get_level_values(0):
                    failed_tickers.append(ticker)
                    unavailable_cache[ticker] = {
                        "window": batch_unavailable_key,
                        "requested_start_date": batch_start_date.strftime("%Y-%m-%d"),
                        "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
                        "availability_kind": "transient_yahoo_error",
                        "reason": "missing_from_batch_response",
                    }
                    continue
                ticker_df = normalize_downloaded_data(downloaded[ticker].dropna(how="all").copy())
                if ticker_df.empty:
                    failed_tickers.append(ticker)
                    unavailable_cache[ticker] = {
                        "window": batch_unavailable_key,
                        "requested_start_date": batch_start_date.strftime("%Y-%m-%d"),
                        "requested_end_date": requested_end_date.strftime("%Y-%m-%d"),
                        "availability_kind": "structural_no_history",
                        "reason": "empty_ticker_frame",
                    }
                else:
                    history_by_ticker[ticker] = ticker_df
                    save_cached_price_history(
                        ticker,
                        ticker_df,
                        requested_start_date=batch_start_date,
                        requested_end_date=requested_end_date,
                        source="yahoo_download",
                    )
                    unavailable_cache.pop(ticker, None)
                    downloaded_tickers += 1

            emit(
                {
                    "phase": "price_batch_done",
                    "batch_number": batch_number,
                    "total_batches": total_batches,
                    "batch_size": len(batch),
                    "batch_start": start,
                    "total_tickers": len(tickers),
                    "cached_tickers": cached_tickers,
                    "downloaded_tickers": downloaded_tickers,
                    "failed_tickers": len(set(failed_tickers)),
                    "requested_from_yahoo": total_download_tickers,
                    "skipped_unavailable_tickers": skipped_unavailable_tickers,
                }
            )
            time.sleep(config.request_pause_seconds)

    save_unavailable_history_cache(unavailable_cache)
    emit(
        {
            "phase": "price_done",
            "total_tickers": len(tickers),
            "cached_tickers": cached_tickers,
            "downloaded_tickers": downloaded_tickers,
            "failed_tickers": len(set(failed_tickers)),
            "requested_from_yahoo": total_download_tickers,
            "skipped_unavailable_tickers": skipped_unavailable_tickers,
        }
    )
    return history_by_ticker, sorted(set(failed_tickers)), {
        "cached_tickers": cached_tickers,
        "downloaded_tickers": downloaded_tickers,
        "requested_from_yahoo": total_download_tickers,
        "skipped_unavailable_tickers": skipped_unavailable_tickers,
    }


def fetch_market_caps(
    tickers: list[str],
    config: Any,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[dict[str, float | None], dict[str, int]]:
    market_caps = load_market_cap_cache()
    tickers_to_fetch = tickers if config.force_refresh_market_caps else [
        ticker for ticker in tickers if ticker not in market_caps
    ]
    cached_market_caps = len(tickers) - len(tickers_to_fetch)

    if market_caps and not config.force_refresh_market_caps:
        print(f"Market cap caricato da cache per {cached_market_caps} ticker.")

    def emit(payload: dict[str, Any]) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    total_tickers = len(tickers_to_fetch)
    emit(
        {
            "phase": "market_cap_start",
            "total_tickers": len(tickers),
            "cached_market_caps": cached_market_caps,
            "to_fetch_market_caps": total_tickers,
            "fetched_market_caps": 0,
        }
    )
    for idx, ticker in enumerate(tickers_to_fetch, start=1):
        if idx == 1 or idx % 100 == 0 or idx == total_tickers:
            print(f"Market cap {idx}/{total_tickers}...")
        market_cap = None
        try:
            instrument = yf.Ticker(ticker)

            fast_info = getattr(instrument, "fast_info", None)
            if fast_info:
                market_cap = fast_info.get("market_cap")

            if market_cap is None:
                info = getattr(instrument, "info", None)
                if info:
                    market_cap = info.get("marketCap")
        except Exception:  # noqa: BLE001
            market_cap = None

        market_caps[ticker] = float(market_cap) if market_cap else None
        emit(
            {
                "phase": "market_cap_progress",
                "total_tickers": len(tickers),
                "cached_market_caps": cached_market_caps,
                "to_fetch_market_caps": total_tickers,
                "fetched_market_caps": idx,
                "ticker": ticker,
            }
        )
        time.sleep(config.request_pause_seconds)

    save_market_cap_cache(market_caps)
    emit(
        {
            "phase": "market_cap_done",
            "total_tickers": len(tickers),
            "cached_market_caps": cached_market_caps,
            "to_fetch_market_caps": total_tickers,
            "fetched_market_caps": len(tickers_to_fetch),
        }
    )
    return market_caps, {
        "cached_market_caps": cached_market_caps,
        "fetched_market_caps": len(tickers_to_fetch),
    }


def load_market_caps_from_cache_only(
    tickers: list[str],
) -> tuple[dict[str, float | None], dict[str, int]]:
    market_caps = load_market_cap_cache()
    return {ticker: market_caps.get(ticker) for ticker in tickers}, {
        "cached_market_caps": sum(1 for ticker in tickers if ticker in market_caps),
        "fetched_market_caps": 0,
    }


def fetch_company_profiles(
    tickers: list[str],
    config: Any,
    force_refresh: bool = False,
) -> tuple[dict[str, dict[str, Any] | None], dict[str, int]]:
    profiles: dict[str, dict[str, Any] | None] = {}
    cached_profiles = 0
    tickers_to_fetch: list[str] = []

    for ticker in tickers:
        cached_profile = None if force_refresh else load_company_profile(ticker)
        if cached_profile is not None:
            profiles[ticker] = cached_profile
            cached_profiles += 1
            continue
        tickers_to_fetch.append(ticker)

    if cached_profiles and not force_refresh:
        print(f"Company profile caricato da cache per {cached_profiles} ticker.")

    total_tickers = len(tickers_to_fetch)
    for idx, ticker in enumerate(tickers_to_fetch, start=1):
        if idx == 1 or idx % 100 == 0 or idx == total_tickers:
            print(f"Company profile {idx}/{total_tickers}...")

        profile_payload: dict[str, Any] | None = None
        try:
            instrument = yf.Ticker(ticker)
            profile_payload = extract_company_profile_payload(ticker, instrument)
        except Exception:  # noqa: BLE001
            profile_payload = None

        if profile_payload is None:
            profiles[ticker] = None
        else:
            save_company_profile(
                ticker,
                source=str(profile_payload.get("source") or "yahoo_profile"),
                quote_type=profile_payload.get("quote_type"),
                sector=profile_payload.get("sector"),
                industry=profile_payload.get("industry"),
                long_name=profile_payload.get("long_name"),
                short_name=profile_payload.get("short_name"),
            )
            profiles[ticker] = load_company_profile(ticker)

        time.sleep(config.request_pause_seconds)

    return profiles, {
        "cached_company_profiles": cached_profiles,
        "fetched_company_profiles": len(tickers_to_fetch),
    }
