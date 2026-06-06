from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.market.etf_context_filter import compute_context_score, compute_etf_universe_scores, format_context_output


BASE_DIR = Path(__file__).resolve().parents[2]
ETF_CONTEXT_COLUMNS = [
    "screen_date",
    "ticker",
    "decision",
    "recommended",
    "context_score",
    "reference_etf_percentile",
    "average_relative_strength_pct",
    "reason",
    "selected_reference_etf",
    "etf_count",
]


def etf_context_path_for_sd(screen_date: pd.Timestamp) -> Path:
    sd = pd.Timestamp(screen_date).normalize()
    return (
        BASE_DIR
        / "output"
        / "etf_context"
        / sd.strftime("%Y")
        / sd.strftime("%m")
        / sd.strftime("%Y%m%d")
        / f"etf_context_{sd.strftime('%Y%m%d')}.csv"
    )


def screening_passed_path_for_sd(screen_date: pd.Timestamp) -> Path:
    sd = pd.Timestamp(screen_date).normalize()
    return (
        BASE_DIR
        / "output"
        / "screening_day"
        / sd.strftime("%Y")
        / sd.strftime("%m")
        / sd.strftime("%Y%m%d")
        / f"second_screen_passed_{sd.strftime('%Y%m%d')}.csv"
    )


def load_etf_context_for_sd(screen_date: pd.Timestamp) -> pd.DataFrame:
    path = etf_context_path_for_sd(screen_date)
    if not path.exists():
        return pd.DataFrame(columns=ETF_CONTEXT_COLUMNS)
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty:
        return pd.DataFrame(columns=ETF_CONTEXT_COLUMNS)
    df = df.reindex(columns=ETF_CONTEXT_COLUMNS)
    df["screen_date"] = pd.to_datetime(df["screen_date"], errors="coerce").dt.normalize()
    df["ticker"] = df["ticker"].astype(str).str.strip().str.upper()
    df["decision"] = df["decision"].astype(str).str.strip().str.lower().isin({"true", "1", "yes"})
    return df.dropna(subset=["screen_date"])


def build_etf_context_for_sd(screen_date: pd.Timestamp, tickers: list[str] | None = None) -> pd.DataFrame:
    sd = pd.Timestamp(screen_date).normalize()
    if tickers is None:
        path = screening_passed_path_for_sd(sd)
        if not path.exists():
            raise FileNotFoundError(f"Second screen passed non trovato per SD={sd.date()}: {path}")
        source_df = pd.read_csv(path, keep_default_na=False)
        tickers = source_df.get("ticker", pd.Series(dtype=str)).astype(str).str.strip().str.upper().tolist()

    normalized_tickers = sorted({str(ticker).strip().upper() for ticker in tickers if str(ticker).strip()})
    universe_scores = compute_etf_universe_scores(screen_date=sd)
    rows: list[dict[str, object]] = []
    for ticker in normalized_tickers:
        context = compute_context_score(ticker, screen_date=sd, universe_scores=universe_scores)
        formatted = format_context_output(context)
        rows.append(
            {
                "screen_date": sd.strftime("%Y-%m-%d"),
                "ticker": ticker,
                "decision": bool(formatted["decision"]),
                "recommended": formatted["recommended"],
                "context_score": formatted["context_score"],
                "reference_etf_percentile": formatted["reference_etf_percentile"],
                "average_relative_strength_pct": formatted["average_relative_strength_pct"],
                "reason": formatted["reason"],
                "selected_reference_etf": formatted["selected_reference_etf"],
                "etf_count": formatted["etf_count"],
            }
        )
    return pd.DataFrame(rows, columns=ETF_CONTEXT_COLUMNS)


def save_etf_context_for_sd(screen_date: pd.Timestamp, tickers: list[str] | None = None) -> Path:
    df = build_etf_context_for_sd(screen_date, tickers=tickers)
    path = etf_context_path_for_sd(screen_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return path


def ensure_etf_context_for_sd(screen_date: pd.Timestamp, tickers: list[str] | None = None) -> pd.DataFrame:
    existing = load_etf_context_for_sd(screen_date)
    if not existing.empty:
        return existing
    save_etf_context_for_sd(screen_date, tickers=tickers)
    return load_etf_context_for_sd(screen_date)
