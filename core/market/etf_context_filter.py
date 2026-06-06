from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from core.config.data_paths import REFERENCE_ETF_MAP_PATH
from core.config.data_paths import MARKET_DATA_ROOT
from core.market.market import load_cached_price_history_any_end, normalize_reference_etfs


SMA50_WINDOW = 50
SMA200_WINDOW = 200
ADX_WINDOW = 14
RS_WINDOWS = ((21, 0.50), (63, 0.30), (126, 0.20))
MIN_CONTEXT_SCORE = 60.0
MIN_REFERENCE_ETF_PERCENTILE = 70.0
CORE_ETF_WEIGHTS = [0.60, 0.40]
SPY_TICKER = "SPY"
ETF_UNIVERSE_PATH = MARKET_DATA_ROOT / "reference" / "etf" / "etf_universe.csv"

PriceLoader = Callable[[str], Optional[pd.DataFrame]]


@dataclass(frozen=True)
class EtfScore:
    etf: str
    score: float
    close_above_sma50: bool
    sma50_above_sma200: bool
    adx: float | None
    adx_strong: bool
    relative_strength_pct: float | None
    relative_strength_1m_pct: float | None
    relative_strength_3m_pct: float | None
    relative_strength_6m_pct: float | None
    relative_strength_positive: bool
    reason: str = ""


@dataclass(frozen=True)
class ContextScore:
    ticker: str
    allowed: bool
    context_score: float | None
    average_relative_strength_pct: float | None
    reference_etf_percentile: float | None
    etf_scores: tuple[EtfScore, ...]
    reason: str


def _normalize_ticker(ticker: str) -> str:
    return str(ticker).strip().upper()


def _load_reference_map(map_path: Path = REFERENCE_ETF_MAP_PATH) -> dict[str, list[str]]:
    if not map_path.exists():
        return {}
    rows = pd.read_csv(map_path, keep_default_na=False)
    reference_map: dict[str, list[str]] = {}
    for _, row in rows.iterrows():
        ticker = _normalize_ticker(row.get("ticker", ""))
        if not ticker:
            continue
        reference_map[ticker] = normalize_reference_etfs(
            [
                row.get("reference_etf_1"),
                row.get("reference_etf_2"),
                row.get("reference_etf_3"),
                row.get("reference_etf_4"),
            ],
        )
    return reference_map


def load_etf_universe(path: Path = ETF_UNIVERSE_PATH) -> set[str]:
    if not path.exists():
        return set()
    rows = pd.read_csv(path, keep_default_na=False)
    if "ticker" not in rows.columns:
        return set()
    return {
        _normalize_ticker(ticker)
        for ticker in rows["ticker"].tolist()
        if _normalize_ticker(ticker)
    }


def get_core_reference_etfs(
    ticker: str,
    map_path: Path = REFERENCE_ETF_MAP_PATH,
    etf_universe_path: Path = ETF_UNIVERSE_PATH,
) -> list[str]:
    reference_etfs = _load_reference_map(map_path).get(_normalize_ticker(ticker), [])
    return reference_etfs[:3]


def get_core_reference_etf_weight_pairs(
    ticker: str,
    map_path: Path = REFERENCE_ETF_MAP_PATH,
    etf_universe_path: Path = ETF_UNIVERSE_PATH,
) -> list[tuple[str, float]]:
    reference_etfs = _load_reference_map(map_path).get(_normalize_ticker(ticker), [])
    pairs: list[tuple[str, float]] = []
    for index, etf in enumerate(reference_etfs[:2]):
        pairs.append((etf, CORE_ETF_WEIGHTS[index]))
    return pairs


def _prepare_history(history: pd.DataFrame | None, screen_date: pd.Timestamp | None = None) -> pd.DataFrame:
    if history is None or history.empty:
        return pd.DataFrame()
    prepared = history.copy()
    prepared.index = pd.to_datetime(prepared.index, errors="coerce").normalize()
    prepared = prepared[prepared.index.notna()].sort_index()
    if screen_date is not None:
        prepared = prepared.loc[prepared.index <= pd.Timestamp(screen_date).normalize()]
    required_columns = {"High", "Low", "Close"}
    if prepared.empty or not required_columns.issubset(set(prepared.columns)):
        return pd.DataFrame()
    for column in required_columns:
        prepared[column] = pd.to_numeric(prepared[column], errors="coerce")
    return prepared.dropna(subset=["High", "Low", "Close"])


def _compute_adx(history: pd.DataFrame, window: int = ADX_WINDOW) -> pd.Series:
    high = history["High"]
    low = history["Low"]
    close = history["Close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    true_range = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = true_range.rolling(window, min_periods=window).mean()
    plus_di = 100 * plus_dm.rolling(window, min_periods=window).mean() / atr
    minus_di = 100 * minus_dm.rolling(window, min_periods=window).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    return dx.rolling(window, min_periods=window).mean()


def _relative_strength_for_window(aligned: pd.DataFrame, window: int) -> float | None:
    if len(aligned) <= window:
        return None
    start = aligned.iloc[-window - 1]
    end = aligned.iloc[-1]
    if start["etf_close"] <= 0 or start["spy_close"] <= 0:
        return None

    etf_return = (end["etf_close"] / start["etf_close"]) - 1
    spy_return = (end["spy_close"] / start["spy_close"]) - 1
    return float((etf_return - spy_return) * 100)


def _relative_strength_components(
    etf_history: pd.DataFrame,
    spy_history: pd.DataFrame,
) -> tuple[float | None, dict[int, float | None]]:
    aligned = pd.concat(
        [
            etf_history["Close"].rename("etf_close"),
            spy_history["Close"].rename("spy_close"),
        ],
        axis=1,
        join="inner",
    ).dropna()
    components = {
        window: _relative_strength_for_window(aligned, window)
        for window, _ in RS_WINDOWS
    }
    usable = [
        (components[window], weight)
        for window, weight in RS_WINDOWS
        if components[window] is not None
    ]
    if not usable:
        return None, components
    total_weight = sum(weight for _, weight in usable)
    composite = sum(float(value) * weight for value, weight in usable) / total_weight
    return float(composite), components


def _percentile_rank(value: float, values: list[float]) -> float | None:
    if not values:
        return None
    lower_or_equal = sum(1 for item in values if item <= value)
    return float((lower_or_equal / len(values)) * 100)


def compute_etf_score(
    etf: str,
    *,
    screen_date: pd.Timestamp | str | None = None,
    price_loader: PriceLoader = load_cached_price_history_any_end,
    spy_history: pd.DataFrame | None = None,
) -> EtfScore:
    etf_ticker = _normalize_ticker(etf)
    effective_screen_date = None if screen_date is None else pd.Timestamp(screen_date)
    etf_history = _prepare_history(price_loader(etf_ticker), effective_screen_date)
    if spy_history is None:
        spy_history = price_loader(SPY_TICKER)
    prepared_spy = _prepare_history(spy_history, effective_screen_date)

    min_rs_window = min(window for window, _ in RS_WINDOWS)
    if len(etf_history) < SMA200_WINDOW or len(prepared_spy) <= min_rs_window:
        return EtfScore(
            etf=etf_ticker,
            score=0.0,
            close_above_sma50=False,
            sma50_above_sma200=False,
            adx=None,
            adx_strong=False,
            relative_strength_pct=None,
            relative_strength_1m_pct=None,
            relative_strength_3m_pct=None,
            relative_strength_6m_pct=None,
            relative_strength_positive=False,
            reason="insufficient_history",
        )

    close = etf_history["Close"]
    sma50 = close.rolling(SMA50_WINDOW, min_periods=SMA50_WINDOW).mean()
    sma200 = close.rolling(SMA200_WINDOW, min_periods=SMA200_WINDOW).mean()
    adx = _compute_adx(etf_history).iloc[-1]
    rs_pct, rs_components = _relative_strength_components(etf_history, prepared_spy)

    close_above_sma50 = bool(close.iloc[-1] > sma50.iloc[-1])
    sma50_above_sma200 = bool(sma50.iloc[-1] > sma200.iloc[-1])
    adx_value = None if pd.isna(adx) else float(adx)
    trend_bullish = close_above_sma50 and sma50_above_sma200
    adx_strong = bool(adx_value is not None and adx_value > 25 and trend_bullish)
    rs_positive = bool(rs_pct is not None and rs_pct > 0)

    score = 0.0
    if close_above_sma50:
        score += 30.0
    if sma50_above_sma200:
        score += 30.0
    if adx_strong:
        score += 20.0
    if rs_positive:
        score += 20.0

    reason = ""
    if rs_pct is None:
        reason = "relative_strength_unavailable"

    return EtfScore(
        etf=etf_ticker,
        score=score,
        close_above_sma50=close_above_sma50,
        sma50_above_sma200=sma50_above_sma200,
        adx=adx_value,
        adx_strong=adx_strong,
        relative_strength_pct=rs_pct,
        relative_strength_1m_pct=rs_components.get(21),
        relative_strength_3m_pct=rs_components.get(63),
        relative_strength_6m_pct=rs_components.get(126),
        relative_strength_positive=rs_positive,
        reason=reason,
    )


def compute_etf_universe_scores(
    *,
    screen_date: pd.Timestamp | str | None = None,
    etf_universe_path: Path = ETF_UNIVERSE_PATH,
    price_loader: PriceLoader = load_cached_price_history_any_end,
) -> tuple[EtfScore, ...]:
    effective_screen_date = None if screen_date is None else pd.Timestamp(screen_date)
    spy_history = _prepare_history(price_loader(SPY_TICKER), effective_screen_date)
    scores: list[EtfScore] = []
    for etf in sorted(load_etf_universe(etf_universe_path)):
        score = compute_etf_score(
            etf,
            screen_date=effective_screen_date,
            price_loader=price_loader,
            spy_history=spy_history,
        )
        if not score.reason.startswith("insufficient_history"):
            scores.append(score)
    return tuple(scores)


def compute_context_score(
    ticker: str,
    *,
    screen_date: pd.Timestamp | str | None = None,
    map_path: Path = REFERENCE_ETF_MAP_PATH,
    etf_universe_path: Path = ETF_UNIVERSE_PATH,
    price_loader: PriceLoader = load_cached_price_history_any_end,
    universe_scores: tuple[EtfScore, ...] | None = None,
) -> ContextScore:
    normalized_ticker = _normalize_ticker(ticker)
    core_etf_weight_pairs = get_core_reference_etf_weight_pairs(
        normalized_ticker,
        map_path=map_path,
        etf_universe_path=etf_universe_path,
    )
    if not core_etf_weight_pairs:
        return ContextScore(normalized_ticker, False, None, None, None, (), "missing_reference_etfs")

    effective_screen_date = None if screen_date is None else pd.Timestamp(screen_date)
    spy_history = _prepare_history(price_loader(SPY_TICKER), effective_screen_date)
    if spy_history.empty:
        return ContextScore(normalized_ticker, False, None, None, None, (), "missing_spy_history")

    scored_candidates = tuple(
        compute_etf_score(
            etf,
            screen_date=effective_screen_date,
            price_loader=price_loader,
            spy_history=spy_history,
        )
        for etf, _ in core_etf_weight_pairs
    )
    usable_scores = [
        (score, weight)
        for score, (_, weight) in zip(scored_candidates, core_etf_weight_pairs)
        if not score.reason.startswith("insufficient_history")
    ]
    if not usable_scores:
        return ContextScore(
            normalized_ticker,
            False,
            None,
            None,
            None,
            scored_candidates,
            "missing_etf_history",
        )
    scored_etfs = tuple(score for score, _ in usable_scores)

    total_weight = sum(weight for _, weight in usable_scores)
    context_score = sum(score.score * weight for score, weight in usable_scores) / total_weight
    rs_values = [
        (score.relative_strength_pct, weight)
        for score, weight in usable_scores
        if score.relative_strength_pct is not None
    ]
    rs_weight = sum(weight for _, weight in rs_values)
    average_rs = None if not rs_values or rs_weight == 0 else float(
        sum(float(value) * weight for value, weight in rs_values) / rs_weight
    )
    if universe_scores is None:
        universe_scores = compute_etf_universe_scores(
            screen_date=effective_screen_date,
            etf_universe_path=etf_universe_path,
            price_loader=price_loader,
        )
    universe_score_values = [score.score for score in universe_scores]
    reference_etf_percentile = None
    for candidate_score in scored_candidates[:2]:
        if candidate_score.reason.startswith("insufficient_history"):
            continue
        percentile = _percentile_rank(candidate_score.score, universe_score_values)
        if percentile is not None:
            reference_etf_percentile = percentile
            break

    if average_rs is None:
        return ContextScore(
            normalized_ticker,
            False,
            float(context_score),
            None,
            reference_etf_percentile,
            scored_etfs,
            "relative_strength_unavailable",
        )
    if average_rs < 0:
        return ContextScore(
            normalized_ticker,
            False,
            float(context_score),
            average_rs,
            reference_etf_percentile,
            scored_etfs,
            "average_relative_strength_negative",
        )
    if context_score < MIN_CONTEXT_SCORE:
        return ContextScore(
            normalized_ticker,
            False,
            float(context_score),
            average_rs,
            reference_etf_percentile,
            scored_etfs,
            "context_score_below_threshold",
        )
    if reference_etf_percentile is None:
        return ContextScore(
            normalized_ticker,
            False,
            float(context_score),
            average_rs,
            None,
            scored_etfs,
            "reference_etf_percentile_unavailable",
        )
    if reference_etf_percentile < MIN_REFERENCE_ETF_PERCENTILE:
        return ContextScore(
            normalized_ticker,
            False,
            float(context_score),
            average_rs,
            reference_etf_percentile,
            scored_etfs,
            "reference_etf_percentile_below_top_30_pct",
        )
    reason = "allowed"
    return ContextScore(
        normalized_ticker,
        True,
        float(context_score),
        average_rs,
        reference_etf_percentile,
        scored_etfs,
        reason,
    )


def allow_trade(
    ticker: str,
    *,
    screen_date: pd.Timestamp | str | None = None,
    map_path: Path = REFERENCE_ETF_MAP_PATH,
    etf_universe_path: Path = ETF_UNIVERSE_PATH,
    price_loader: PriceLoader = load_cached_price_history_any_end,
) -> str:
    context_score = compute_context_score(
        ticker,
        screen_date=screen_date,
        map_path=map_path,
        etf_universe_path=etf_universe_path,
        price_loader=price_loader,
    )
    return "recommended" if context_score.allowed else "not recommended"


def format_context_output(context: ContextScore) -> dict[str, object]:
    return {
        "ticker": context.ticker,
        "decision": bool(context.allowed),
        "recommended": "recommended" if context.allowed else "not recommended",
        "context_score": context.context_score,
        "reference_etf_percentile": context.reference_etf_percentile,
        "average_relative_strength_pct": context.average_relative_strength_pct,
        "reason": context.reason,
        "selected_reference_etf": "+".join(score.etf for score in context.etf_scores) if context.etf_scores else None,
        "etf_count": len(context.etf_scores),
        "etfs": [
            {
                "ticker": score.etf,
                "score": score.score,
                "trend_ok": bool(score.close_above_sma50 and score.sma50_above_sma200),
                "close_above_sma50": score.close_above_sma50,
                "sma50_above_sma200": score.sma50_above_sma200,
                "adx": score.adx,
                "adx_strong": score.adx_strong,
                "rs": score.relative_strength_pct,
                "relative_strength_pct": score.relative_strength_pct,
                "relative_strength_1m_pct": score.relative_strength_1m_pct,
                "relative_strength_3m_pct": score.relative_strength_3m_pct,
                "relative_strength_6m_pct": score.relative_strength_6m_pct,
                "reason": score.reason,
            }
            for score in context.etf_scores
        ],
    }
