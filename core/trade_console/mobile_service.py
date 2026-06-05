from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from core.market.etf_context_filter import (
    MIN_CONTEXT_SCORE,
    MIN_REFERENCE_ETF_PERCENTILE,
    compute_context_score,
)
from core.screening.second_screen import (
    SecondScreenConfig,
    analyze_ticker as analyze_second_screen_ticker,
    load_cached_price_history as load_second_screen_price_history,
)
from core.market.semaforo import compute_blue_on_for_date, compute_market_street_light_for_date, load_daily_history_from_cache
from tools.strategy_EMA21_SMA50 import run_trade_for_ticker
from tools.strategy_EMA21_SMA50 import (
    FIRST_TARGET_R_MULTIPLE,
    calculate_quantity,
    load_cached_price_history as load_strategy_price_history,
    resolve_stop_loss,
)


SECOND_SCREEN_PATTERN = re.compile(r"second_screen_passed_(\d{8})\.csv$")
ETF_SCORE_PASS_MULTIPLIER = 1.25
ETF_SCORE_FAIL_MULTIPLIER = 0.50
DEFAULT_RISK_AMOUNT = 25.0
BASE_DIR = Path(__file__).resolve().parents[2]
SCREENING_DAY_DIR = BASE_DIR / "output" / "screening_day"


@dataclass
class SecondScreenArchive:
    screen_date: pd.Timestamp
    path: Path
    rows: pd.DataFrame


def parse_second_screen_date(path: Path) -> pd.Timestamp | None:
    match = SECOND_SCREEN_PATTERN.search(path.name)
    if not match:
        return None
    return pd.Timestamp(match.group(1)).normalize()


def screening_day_dir(screen_date: pd.Timestamp) -> Path:
    target_ts = pd.Timestamp(screen_date).normalize()
    return (
        SCREENING_DAY_DIR
        / target_ts.strftime("%Y")
        / target_ts.strftime("%m")
        / target_ts.strftime("%Y%m%d")
    )


def resolve_second_screen_passed_path(screen_date: pd.Timestamp) -> Path:
    target_ts = pd.Timestamp(screen_date).normalize()
    filename = f"second_screen_passed_{target_ts.strftime('%Y%m%d')}.csv"
    day_dir = screening_day_dir(target_ts)
    direct_path = day_dir / filename
    if direct_path.exists():
        return direct_path
    candidates = sorted(day_dir.glob(f"*/{filename}"))
    if candidates:
        return candidates[-1]
    return direct_path


def find_latest_second_screen_passed() -> Path | None:
    candidates: list[tuple[pd.Timestamp, float, Path]] = []
    for path in SCREENING_DAY_DIR.glob("**/second_screen_passed_*.csv"):
        screen_date = parse_second_screen_date(path)
        if screen_date is None:
            continue
        candidates.append((screen_date, path.stat().st_mtime, path))
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: (item[0], item[1]))[-1][2]


def load_second_screen_archive(path: Path | None = None) -> SecondScreenArchive:
    effective_path = path or find_latest_second_screen_passed()
    if effective_path is None:
        raise FileNotFoundError("Nessun second_screen_passed_*.csv trovato in output/screening_day.")
    screen_date = parse_second_screen_date(effective_path)
    if screen_date is None:
        raise ValueError(f"Nome file second screen non riconosciuto: {effective_path}")
    rows = pd.read_csv(effective_path, keep_default_na=False)
    return SecondScreenArchive(screen_date=screen_date, path=effective_path, rows=rows)


def load_second_screen_archive_for_date(screen_date: pd.Timestamp) -> SecondScreenArchive:
    target_ts = pd.Timestamp(screen_date).normalize()
    archive_path = resolve_second_screen_passed_path(target_ts)
    if not archive_path.exists():
        return SecondScreenArchive(screen_date=target_ts, path=archive_path, rows=pd.DataFrame())
    rows = pd.read_csv(archive_path, keep_default_na=False)
    return SecondScreenArchive(screen_date=target_ts, path=archive_path, rows=rows)


def find_second_screen_row_for_date(ticker: str, screen_date: pd.Timestamp) -> pd.Series | None:
    archive = load_second_screen_archive_for_date(screen_date)
    if archive.rows.empty or "ticker" not in archive.rows.columns:
        return None
    ticker_label = str(ticker).strip().upper()
    matches = archive.rows[archive.rows["ticker"].astype(str).str.strip().str.upper() == ticker_label]
    if matches.empty:
        return None
    return matches.iloc[0]


def format_value(value: Any, digits: int = 2, suffix: str = "") -> str:
    if value is None or pd.isna(value):
        return "N/D"
    try:
        return f"{float(value):.{digits}f}{suffix}"
    except Exception:
        return str(value)


def format_bool(value: Any) -> str:
    if value is None or pd.isna(value):
        return "N/D"
    if isinstance(value, str):
        return "OK" if value.strip().lower() in {"true", "1", "yes", "ok"} else "NO"
    return "OK" if bool(value) else "NO"


def get_target_day_price_snapshot(ticker: str, screen_date: pd.Timestamp) -> dict[str, float | None]:
    history = load_second_screen_price_history(ticker, screen_date)
    if history is None or history.empty:
        return {
            "close": None,
            "high": None,
            "low": None,
            "previous_close": None,
            "price_change_pct": None,
            "close_range_position_pct": None,
        }

    screen_ts = pd.Timestamp(screen_date).normalize()
    row = history.loc[screen_ts]
    close = pd.to_numeric(row.get("Close"), errors="coerce")
    high = pd.to_numeric(row.get("High"), errors="coerce")
    low = pd.to_numeric(row.get("Low"), errors="coerce")
    previous_close = None
    price_change_pct = None
    previous_rows = history.loc[pd.to_datetime(history.index) < screen_ts]
    if not previous_rows.empty:
        previous_close_raw = pd.to_numeric(previous_rows.iloc[-1].get("Close"), errors="coerce")
        if pd.notna(previous_close_raw):
            previous_close = float(previous_close_raw)
            if pd.notna(close) and previous_close != 0:
                price_change_pct = ((float(close) - previous_close) / previous_close) * 100.0
    range_position = None
    if pd.notna(close) and pd.notna(high) and pd.notna(low) and float(high) != float(low):
        range_position = ((float(close) - float(low)) / (float(high) - float(low))) * 100.0

    return {
        "close": float(close) if pd.notna(close) else None,
        "high": float(high) if pd.notna(high) else None,
        "low": float(low) if pd.notna(low) else None,
        "previous_close": previous_close,
        "price_change_pct": price_change_pct,
        "close_range_position_pct": range_position,
    }


def get_reference_etf_market_snapshot(screen_date: pd.Timestamp, ticker: str = "SPY") -> dict[str, float | None]:
    history = load_second_screen_price_history(ticker, pd.Timestamp(screen_date).normalize())
    if history is None or history.empty or len(history) < 50:
        return {
            "ticker": ticker,
            "dist_ema21_atr14_multiple": None,
            "dist_sma50_atr14_multiple": None,
        }

    close = pd.to_numeric(history["Close"], errors="coerce")
    high = pd.to_numeric(history["High"], errors="coerce")
    low = pd.to_numeric(history["Low"], errors="coerce")
    ema21 = close.ewm(span=21, adjust=False).mean()
    sma50 = close.rolling(50).mean()
    prev_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr14_abs = true_range.rolling(14, min_periods=14).mean()

    last_close = close.iloc[-1]
    last_ema21 = ema21.iloc[-1]
    last_sma50 = sma50.iloc[-1]
    last_atr14_abs = atr14_abs.iloc[-1]
    if pd.isna(last_close) or pd.isna(last_ema21) or pd.isna(last_sma50) or pd.isna(last_atr14_abs) or last_atr14_abs == 0:
        return {
            "ticker": ticker,
            "dist_ema21_atr14_multiple": None,
            "dist_sma50_atr14_multiple": None,
        }

    return {
        "ticker": ticker,
        "dist_ema21_atr14_multiple": round(float((last_close - last_ema21) / last_atr14_abs), 4),
        "dist_sma50_atr14_multiple": round(float((last_close - last_sma50) / last_atr14_abs), 4),
    }


def compute_next_market_date(screen_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    target_ts = pd.Timestamp(screen_date).normalize()
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=target_ts + pd.Timedelta(days=1),
        end_date=target_ts + pd.Timedelta(days=14),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    if not sessions:
        raise FileNotFoundError(f"Nessuna BD trovata dopo SD={target_ts.date()}.")
    return sessions[0]


def compute_previous_market_date(screen_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    target_ts = pd.Timestamp(screen_date).normalize()
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=target_ts - pd.Timedelta(days=14),
        end_date=target_ts - pd.Timedelta(days=1),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    if not sessions:
        raise FileNotFoundError(f"Nessuna SD trovata prima di BD={target_ts.date()}.")
    return sessions[-1]


def recompute_second_screen_row(ticker: str, screen_date: pd.Timestamp) -> dict[str, object] | None:
    history = load_second_screen_price_history(ticker, screen_date)
    if history is None or history.empty:
        return None
    semaforo_result = compute_market_street_light_for_date(screen_date, ticker="SPY")
    blue_on, blue_on_weak_count = compute_blue_on_for_date(screen_date)
    return analyze_second_screen_ticker(
        ticker,
        history,
        SecondScreenConfig(),
        requested_target_date=screen_date,
        effective_target_date=screen_date,
        semaforo_color=semaforo_result.market_street_light,
        blue_on=blue_on,
        blue_on_weak_count=blue_on_weak_count,
    )


def build_trade_console_payload(
    ticker: str,
    screen_date: pd.Timestamp,
    *,
    second_screen_row: pd.Series | None = None,
) -> dict[str, object]:
    ticker_label = str(ticker).strip().upper()
    screen_ts = pd.Timestamp(screen_date).normalize()
    buy_ts = compute_next_market_date(screen_ts)

    analysis_row: dict[str, object] | None = None
    if second_screen_row is not None and not second_screen_row.empty:
        analysis_row = second_screen_row.to_dict()
    if analysis_row is None:
        analysis_row = recompute_second_screen_row(ticker_label, screen_ts)

    price_snapshot = get_target_day_price_snapshot(ticker_label, screen_ts)
    reference_etf_snapshot = get_reference_etf_market_snapshot(screen_ts, ticker="SPY")
    trade_result = None
    trade_error = ""
    estimated_trade = None
    try:
        trade_result = run_trade_for_ticker(ticker_label, buy_ts, DEFAULT_RISK_AMOUNT, "system")
    except Exception as exc:  # noqa: BLE001
        trade_error = str(exc)
        try:
            estimated_trade = build_estimated_trade_payload(ticker_label, screen_ts, price_snapshot)
        except Exception:
            estimated_trade = None

    dist_ema21_atr14_multiple = None
    if analysis_row is not None:
        dist_ema21_atr14_multiple = analysis_row.get("dist_ema21_atr14_multiple")
        if dist_ema21_atr14_multiple is None or pd.isna(dist_ema21_atr14_multiple):
            close_value = analysis_row.get("close")
            atr14_pct = analysis_row.get("atr14_pct")
            dist_ema21_pct = analysis_row.get("dist_ema21_pct")
            if pd.notna(close_value) and pd.notna(atr14_pct) and pd.notna(dist_ema21_pct):
                close_float = float(close_value)
                atr_abs = (float(atr14_pct) / 100.0) * close_float
                ema21_value = close_float / (1.0 + (float(dist_ema21_pct) / 100.0))
                if atr_abs != 0:
                    dist_ema21_atr14_multiple = (close_float - ema21_value) / atr_abs

    return {
        "ticker": ticker_label,
        "screen_date": screen_ts,
        "target_date": screen_ts,
        "bd": buy_ts,
        "etf_context": compute_context_score(ticker_label, target_date=screen_ts),
        "analysis_row": analysis_row or {},
        "price_snapshot": price_snapshot,
        "reference_etf_snapshot": reference_etf_snapshot,
        "dist_ema21_atr14_multiple": dist_ema21_atr14_multiple,
        "trade_result": trade_result,
        "trade_error": trade_error,
        "estimated_trade": estimated_trade,
    }


def build_estimated_trade_payload(
    ticker: str,
    screen_date: pd.Timestamp,
    price_snapshot: dict[str, float | None],
) -> dict[str, object] | None:
    entry_proxy = price_snapshot.get("close")
    if entry_proxy is None or pd.isna(entry_proxy):
        return None

    history = load_strategy_price_history(ticker)
    screen_ts = pd.Timestamp(screen_date).normalize()
    if screen_ts not in set(pd.to_datetime(history["Date"], errors="coerce").dt.normalize()):
        return None

    stop_loss, stop_loss_reason = resolve_stop_loss(
        history=history,
        requested_buy_date=screen_ts,
        stop_loss_mode="system",
        custom_stop_loss=None,
    )
    quantity = calculate_quantity(float(entry_proxy), float(stop_loss), DEFAULT_RISK_AMOUNT)
    risk_per_share = float(entry_proxy) - float(stop_loss)
    target_2r = float(entry_proxy) + (risk_per_share * FIRST_TARGET_R_MULTIPLE)
    return {
        "entry_proxy": float(entry_proxy),
        "entry_proxy_source": "close_screen_date",
        "stop_loss": float(stop_loss),
        "stop_loss_reason": stop_loss_reason,
        "quantity": int(quantity),
        "risk_amount": float(DEFAULT_RISK_AMOUNT),
        "risk_per_share": risk_per_share,
        "initial_value": float(entry_proxy) * int(quantity),
        "target_2r": target_2r,
    }


def format_trade_console_payload(payload: dict[str, object]) -> str:
    ticker = str(payload["ticker"])
    screen_date = pd.Timestamp(payload.get("screen_date", payload["target_date"])).strftime("%Y-%m-%d")
    buy_date = pd.Timestamp(payload["bd"]).strftime("%Y-%m-%d")
    analysis = payload.get("analysis_row", {})
    price = payload.get("price_snapshot", {})
    trade = payload.get("trade_result")
    trade_error = str(payload.get("trade_error") or "")
    estimated_trade = payload.get("estimated_trade")
    etf_context = payload.get("etf_context")

    lines = [
        ticker,
        f"Close {screen_date}: {format_value(price.get('close'))}",
        f"High {screen_date}: {format_value(price.get('high'))}",
        f"Low {screen_date}: {format_value(price.get('low'))}",
        f"Close Range Position: {format_value(price.get('close_range_position_pct'), 1, '%')}",
        f"Second Screen: {format_bool(analysis.get('passed_second_screen'))}",
        f"EMA21 slope 5d: {format_value(analysis.get('ema21_slope_pct_5'), 4, '%')}",
        f"Dist EMA21: {format_value(payload.get('dist_ema21_atr14_multiple'), 2, ' ATR')}",
        f"Dist SMA50: {format_value(analysis.get('dist_sma50_atr14_multiple'), 2, ' ATR')}",
        f"Semaforo: {analysis.get('semaforo_color') or 'N/D'}",
        f"BLUE ON: {format_bool(analysis.get('blue_on'))}",
        f"BD: {buy_date}",
    ]
    if etf_context is not None:
        context_score = getattr(etf_context, "context_score", None)
        average_rs = getattr(etf_context, "average_relative_strength_pct", None)
        percentile = getattr(etf_context, "reference_etf_percentile", None)
        etf_allowed = bool(getattr(etf_context, "allowed", False))
        etf_multiplier = ETF_SCORE_PASS_MULTIPLIER if etf_allowed else ETF_SCORE_FAIL_MULTIPLIER
        lines.extend(
            [
                f"ETF filter: {'GREEN' if etf_allowed else 'RED'}",
                f"ETF score >= {MIN_CONTEXT_SCORE:.0f}: {format_value(context_score)}",
                f"ETF percentile >= {MIN_REFERENCE_ETF_PERCENTILE:.0f}: {format_value(percentile, 1, '%')}",
                f"ETF avg RS >= 0: {format_value(average_rs, 2, '%')}",
                f"ETF mult: {format_value(etf_multiplier, 2, 'x')}",
                f"ETF reason: {getattr(etf_context, 'reason', 'N/D')}",
            ]
        )
        for score in getattr(etf_context, "etf_scores", ()):
            lines.append(
                f"{score.etf}: score {format_value(score.score)} | "
                f"RS {format_value(score.relative_strength_pct, 2, '%')} "
                f"(1m {format_value(score.relative_strength_1m_pct, 2, '%')}, "
                f"3m {format_value(score.relative_strength_3m_pct, 2, '%')}, "
                f"6m {format_value(score.relative_strength_6m_pct, 2, '%')}) | "
                f"ADX {format_value(score.adx)}"
            )

    if trade is not None:
        realized_r = trade.realized_pnl / trade.risk_amount if trade.risk_amount else 0.0
        lines.extend(
            [
                f"Entry open: {format_value(trade.entry_price)}",
                f"SL iniziale: {format_value(getattr(trade, 'initial_stop_loss', trade.stop_loss))}",
                f"SL finale: {format_value(trade.stop_loss)}",
                f"Target 2R: {format_value(trade.target_close_price)}",
                f"Quantity: {trade.quantity}",
                f"Initial value: {format_value(trade.initial_value)}",
                f"Stop hit: {format_bool(trade.stop_hit)}",
                f"TP1 done: {format_bool(trade.first_take_profit_done)}",
                f"Realized R: {format_value(realized_r)}",
                f"Sessions held: {trade.sessions_held}",
            ]
        )
    elif estimated_trade:
        risk_per_share = estimated_trade.get("risk_per_share")
        if risk_per_share is None:
            risk_per_share = float(estimated_trade.get("entry_proxy", 0.0)) - float(
                estimated_trade.get("stop_loss", 0.0)
            )
        estimated_loss = float(risk_per_share) * int(estimated_trade.get("quantity", 0))
        lines.extend(
            [
                "Trade reale: non ancora simulabile",
                f"Entry stimata (Close screen): {format_value(estimated_trade.get('entry_proxy'))}",
                f"SL sistema stimato: {format_value(estimated_trade.get('stop_loss'))}",
                f"Loss per share stimata: {format_value(risk_per_share)}",
                f"Loss stimata: {format_value(estimated_loss)}",
                f"Target 2R stimato: {format_value(estimated_trade.get('target_2r'))}",
                f"Quantity stimata: {estimated_trade.get('quantity')}",
                f"Initial value stimato: {format_value(estimated_trade.get('initial_value'))}",
            ]
        )
    else:
        lines.append(f"Trade: N/D ({trade_error or 'errore non disponibile'})")

    return "\n".join(lines)


def build_latest_second_screen_message() -> str:
    archive = load_second_screen_archive()
    target_label = archive.screen_date.strftime("%Y-%m-%d")
    if archive.rows.empty:
        return f"Second Screen ultimo: {target_label}\nTicker passati: 0"

    sections = [
        f"Second Screen ultimo: {target_label}",
        f"File: {archive.path.relative_to(BASE_DIR)}",
        f"Ticker passati: {len(archive.rows)}",
    ]

    for _, row in archive.rows.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        payload = build_trade_console_payload(ticker, archive.screen_date, second_screen_row=row)
        sections.append(format_trade_console_payload(payload))

    return "\n\n".join(sections)


def build_check_message(ticker: str, screen_date: pd.Timestamp | None = None) -> str:
    if screen_date is None:
        archive = load_second_screen_archive()
        screen_date = archive.screen_date
    screen_ts = pd.Timestamp(screen_date).normalize()
    payload = build_trade_console_payload(ticker, screen_ts)
    return format_trade_console_payload(payload)


def build_buy_message(ticker: str, bd: pd.Timestamp) -> str:
    buy_ts = pd.Timestamp(bd).normalize()
    screen_ts = compute_previous_market_date(buy_ts)
    ticker_label = str(ticker).strip().upper()
    second_screen_row = find_second_screen_row_for_date(ticker_label, screen_ts)
    in_second_screen = second_screen_row is not None
    price_snapshot = get_target_day_price_snapshot(ticker_label, screen_ts)
    etf_context = compute_context_score(ticker_label, target_date=screen_ts)
    etf_allowed = bool(getattr(etf_context, "allowed", False))
    etf_multiplier = ETF_SCORE_PASS_MULTIPLIER if etf_allowed else ETF_SCORE_FAIL_MULTIPLIER
    adjusted_risk_amount = float(DEFAULT_RISK_AMOUNT) * float(etf_multiplier)

    trade_result = run_trade_for_ticker(ticker_label, buy_ts, adjusted_risk_amount, "system")
    entry_type_map = {
        "buy_at_open": "BUY AT OPEN",
        "buy_stop_reclaim_close_d1": "U&R",
    }
    entry_type = entry_type_map.get(trade_result.entry_mode, trade_result.entry_mode)
    total_initial_risk = (float(trade_result.entry_price) - float(trade_result.initial_stop_loss)) * int(
        trade_result.quantity
    )

    return "\n".join(
        [
            ticker_label,
            f"Screen Date: {screen_ts.strftime('%Y-%m-%d')}",
            f"BD: {buy_ts.strftime('%Y-%m-%d')}",
            f"Low SD: {format_value(price_snapshot.get('low'))}",
            f"Close SD: {format_value(price_snapshot.get('close'))}",
            f"MLL1_E21PB_BLUE: {'SI' if in_second_screen else 'NO'}",
            f"Tipo di entry: {entry_type}",
            f"Moltiplicatore: {format_value(etf_multiplier, 2, 'x')}",
            f"R a valle del moltiplicatore: {format_value(adjusted_risk_amount)}",
            f"Quantity: {trade_result.quantity}",
            f"SL iniziale: {format_value(trade_result.initial_stop_loss)}",
            f"Total initial risk: {format_value(total_initial_risk)}",
            f"Target primo partial sell: {format_value(trade_result.target_close_price)}",
        ]
    )


def build_buy_day_message(bd: pd.Timestamp) -> str:
    buy_ts = pd.Timestamp(bd).normalize()
    screen_ts = compute_previous_market_date(buy_ts)
    archive = load_second_screen_archive_for_date(screen_ts)
    if archive.rows.empty:
        return (
            f"BD: {buy_ts.strftime('%Y-%m-%d')}\n"
            f"Screen Date: {screen_ts.strftime('%Y-%m-%d')}\n"
            "Nessun second_screen_passed disponibile per questa screen date."
        )

    sections = [
        f"BD: {buy_ts.strftime('%Y-%m-%d')}",
        f"Screen Date: {screen_ts.strftime('%Y-%m-%d')}",
        f"Ticker passati: {len(archive.rows)}",
    ]
    for _, row in archive.rows.iterrows():
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        try:
            sections.append(build_buy_message(ticker, buy_ts))
        except Exception as exc:  # noqa: BLE001
            sections.append(f"{ticker}\nErrore: {exc}")
    return "\n\n".join(sections)
