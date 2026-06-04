from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from core.io.archive_utils import trading_day_date_dir
from core.config.data_paths import PRICE_CACHE_DIR
from core.config.script_version import build_script_signature

BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_ARCHIVE_DIR = BASE_DIR / "output" / "archivio"
TRADE_OUTPUT_SUBDIR = "trade_engine"
SCRIPT_NAME, SCRIPT_VERSION = build_script_signature(__file__)
FIRST_TARGET_R_MULTIPLE = 2.0
MIN_SHARES = 3
ATR_WINDOW = 14
SYSTEM_STOP_LOOKBACK_DAYS = 15
SYSTEM_STOP_BUFFER_PCT = 0.01
ORDER_COST = 1.0
FX_COST_PCT = 0.005
SMA10_WINDOW = 10
SMA50_WINDOW = 50
EMA21_WINDOW = 21
MIN_RISK_PER_SHARE = 0.01
E21_BUFFER_PCT = 0.002


@dataclass
class TradeLeg:
    label: str
    date: pd.Timestamp
    price: float
    shares: int
    reason: str


@dataclass
class TradeResult:
    ticker: str
    buy_date: pd.Timestamp
    risk_amount: float
    entry_price: float
    entry_mode: str
    initial_stop_loss: float
    stop_loss: float
    stop_loss_mode: str
    stop_loss_reason: str
    quantity: int
    initial_value: float
    target_close_price: float
    first_take_profit_done: bool
    stop_hit: bool
    legs: list[TradeLeg]
    realized_pnl: float
    return_pct: float
    sessions_held: int


@dataclass
class StrategyRunResult:
    requested_buy_date: pd.Timestamp
    risk_amount: float
    stop_loss_mode: str
    results: list[TradeResult]
    skipped_tickers: list[tuple[str, str]]


def prompt_mode() -> str:
    raw = input("Modalita [S=single / M=stringa]: ").strip().lower()
    if raw in {"s", "single", "singolo"}:
        return "single"
    if raw in {"m", "string", "str", "lista", "multipla"}:
        return "string"
    raise ValueError("Modalita non valida. Usa 'S' per single o 'M' per stringa.")


def output_dir_for_trade(trade_date: pd.Timestamp) -> Path:
    return trading_day_date_dir(trade_date) / TRADE_OUTPUT_SUBDIR


def build_trade_stem(ticker: str, trade_date: pd.Timestamp) -> str:
    return f"{ticker}_{trade_date.strftime('%Y%m%d')}"


def prompt_ticker() -> str:
    ticker = input("Ticker: ").strip().upper()
    if not ticker:
        raise ValueError("Ticker non valido.")
    return ticker


def prompt_ticker_string() -> list[str]:
    raw = input("Ticker separati da virgola: ").strip()
    tickers = [item.strip().upper() for item in raw.split(",") if item.strip()]
    if not tickers:
        raise ValueError("Inserisci almeno un ticker valido.")
    return tickers


def prompt_date(label: str) -> pd.Timestamp:
    raw = input(f"{label} (YYYY-MM-DD): ").strip()
    try:
        return pd.Timestamp(raw).normalize()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{label} non valida. Usa YYYY-MM-DD.") from exc


def prompt_stop_loss_mode() -> str:
    raw = input("SL [C=custom / S=sistema]: ").strip().lower()
    if raw in {"c", "custom"}:
        return "custom"
    if raw in {"s", "sistema", "system"}:
        return "system"
    raise ValueError("Modalita SL non valida. Usa 'C' per custom o 'S' per sistema.")


def prompt_stop_loss() -> float:
    raw = input("Stop loss assoluto: ").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("Stop loss non valido.") from exc
    if value <= 0:
        raise ValueError("Lo stop loss deve essere maggiore di zero.")
    return value


def prompt_risk_amount() -> float:
    raw = input("Valore di 1R in $: ").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError("Valore di R non valido.") from exc
    if value <= 0:
        raise ValueError("Il valore di R deve essere maggiore di zero.")
    return value


def load_cached_price_history(ticker: str) -> pd.DataFrame:
    path = PRICE_CACHE_DIR / f"{ticker}.csv"
    if not path.exists():
        raise FileNotFoundError(f"Cache prezzi non trovata per {ticker}: {path}")

    df = pd.read_csv(path)
    if df.empty:
        raise ValueError(f"Cache vuota per {ticker}.")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df[df["Date"].notna()].copy()
    df = df.sort_values("Date").reset_index(drop=True)

    required_columns = {"Date", "Open", "High", "Low", "Close"}
    missing = required_columns - set(df.columns)
    if missing:
        raise ValueError(f"Colonne mancanti nella cache di {ticker}: {sorted(missing)}")

    prev_close = df["Close"].shift(1)
    true_range = pd.concat(
        [
            df["High"] - df["Low"],
            (df["High"] - prev_close).abs(),
            (df["Low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    df["ATR14"] = true_range.rolling(ATR_WINDOW, min_periods=ATR_WINDOW).mean()
    df["SMA10"] = df["Close"].rolling(SMA10_WINDOW, min_periods=SMA10_WINDOW).mean()
    df["SMA50"] = df["Close"].rolling(SMA50_WINDOW, min_periods=SMA50_WINDOW).mean()
    df["EMA21"] = df["Close"].ewm(span=EMA21_WINDOW, adjust=False).mean()
    return df


def calculate_quantity(entry_price: float, stop_loss: float, risk_amount: float) -> int:
    risk_per_share = entry_price - stop_loss
    if risk_per_share < MIN_RISK_PER_SHARE:
        raise ValueError("Lo stop loss deve essere inferiore al prezzo di ingresso.")
    quantity = int(risk_amount // risk_per_share)
    return max(quantity, MIN_SHARES)


def build_tranche_sizes(quantity: int) -> tuple[int, int, int]:
    first_tranche = max(1, quantity // 3)
    remaining_after_first = quantity - first_tranche
    second_tranche = max(1, remaining_after_first // 2)
    final_tranche = quantity - first_tranche - second_tranche
    if final_tranche < 1:
        second_tranche -= 1
        final_tranche += 1
    return first_tranche, second_tranche, final_tranche


def get_future_rows_from_requested_buy_date(
    history: pd.DataFrame,
    requested_buy_date: pd.Timestamp,
) -> pd.DataFrame:
    future = history[history["Date"] >= requested_buy_date].reset_index(drop=True)
    if future.empty:
        raise ValueError(
            f"Nessun dato disponibile dalla data richiesta {requested_buy_date.date()}."
        )
    return future


def resolve_effective_entry_row(
    history: pd.DataFrame,
    requested_buy_date: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.Series, pd.Timestamp]:
    future = get_future_rows_from_requested_buy_date(history, requested_buy_date)
    entry_row = future.iloc[0]
    entry_date = pd.Timestamp(entry_row["Date"]).normalize()
    return future, entry_row, entry_date


def resolve_entry_policy(
    history: pd.DataFrame,
    requested_buy_date: pd.Timestamp,
) -> tuple[pd.DataFrame, pd.Series, pd.Timestamp, float, str, str]:
    future, entry_row, entry_date = resolve_effective_entry_row(history, requested_buy_date)
    entry_candidates = history.index[history["Date"] == entry_date].tolist()
    if not entry_candidates:
        raise ValueError("Impossibile individuare la seduta di ingresso nello storico.")
    entry_idx = entry_candidates[0]
    if entry_idx == 0:
        raise ValueError(
            "Impossibile applicare la policy di ingresso: manca la seduta D-1 in cache."
        )

    screen_row = history.iloc[entry_idx - 1]
    close_d1 = pd.to_numeric(screen_row.get("Close"), errors="coerce")
    ema21_d1 = pd.to_numeric(screen_row.get("EMA21"), errors="coerce")
    high_d1 = pd.to_numeric(screen_row.get("High"), errors="coerce")
    low_d1 = pd.to_numeric(screen_row.get("Low"), errors="coerce")
    open_buy = pd.to_numeric(entry_row.get("Open"), errors="coerce")
    high_buy = pd.to_numeric(entry_row.get("High"), errors="coerce")

    required_values = [close_d1, ema21_d1, high_d1, low_d1, open_buy, high_buy]
    if any(pd.isna(value) for value in required_values):
        raise ValueError(
            "Impossibile applicare la policy di ingresso: dati D-1 o buy day mancanti."
        )

    threshold = float(ema21_d1) * (1 - E21_BUFFER_PCT)
    if float(close_d1) > threshold:
        return (
            future,
            entry_row,
            entry_date,
            float(open_buy),
            "buy_at_open",
            "Ingresso all'open della data indicata (bucket above buffer E21)",
        )

    if float(open_buy) > float(high_d1):
        raise ValueError("SKIP: Policy E21 buffer: open buy day sopra high D-1.")
    if float(low_d1) <= float(open_buy) <= float(high_d1):
        raise ValueError("SKIP: Policy E21 buffer: open buy day dentro il range D-1.")
    if float(open_buy) < float(low_d1):
        if float(high_buy) >= float(close_d1):
            return (
                future,
                entry_row,
                entry_date,
                float(close_d1),
                "buy_stop_reclaim_close_d1",
                "Ingresso su reclaim di close D-1 dopo open sotto low D-1",
            )
        raise ValueError("SKIP: Policy E21 buffer: open sotto low D-1 ma nessun reclaim di close D-1.")

    raise ValueError("Policy E21 buffer: configurazione ingresso non gestita.")


def resolve_stop_loss(
    history: pd.DataFrame,
    requested_buy_date: pd.Timestamp,
    stop_loss_mode: str,
    custom_stop_loss: float | None,
) -> tuple[float, str]:
    _, _, entry_date = resolve_effective_entry_row(history, requested_buy_date)
    entry_candidates = history.index[history["Date"] == entry_date].tolist()
    if not entry_candidates:
        raise ValueError("Impossibile individuare la seduta di ingresso nello storico.")
    entry_idx = entry_candidates[0]

    if stop_loss_mode == "custom":
        if custom_stop_loss is None:
            raise ValueError("Stop loss custom mancante.")
        return custom_stop_loss, "Stop loss inserito manualmente"

    if entry_idx == 0:
        raise ValueError(
            "Impossibile calcolare lo SL di sistema: manca una seduta precedente in cache."
        )

    prior_history = history.iloc[:entry_idx].copy()
    if prior_history.empty:
        raise ValueError(
            "Impossibile calcolare lo SL di sistema: manca storico precedente alla seduta di ingresso."
        )

    lookback = prior_history.tail(SYSTEM_STOP_LOOKBACK_DAYS)
    swing_low = float(lookback["Low"].min())
    reference_row = prior_history.iloc[-1]
    ema21 = reference_row["EMA21"]
    atr14 = reference_row["ATR14"]
    if pd.isna(ema21) or pd.isna(atr14):
        raise ValueError(
            "Impossibile calcolare lo SL di sistema: EMA21 o ATR14 non disponibili sulla seduta precedente."
        )

    ema21_minus_atr = float(ema21) - float(atr14)
    raw_stop_loss = max(swing_low, ema21_minus_atr)
    stop_loss = raw_stop_loss * (1 - SYSTEM_STOP_BUFFER_PCT)
    return (
        stop_loss,
        "SL di sistema: max tra minimo ultimi "
        f"{SYSTEM_STOP_LOOKBACK_DAYS} giorni ({swing_low:.2f}) e EMA21-1ATR "
        f"della seduta precedente {pd.Timestamp(reference_row['Date']).strftime('%Y-%m-%d')} "
        f"({ema21_minus_atr:.2f}), poi ridotto del {SYSTEM_STOP_BUFFER_PCT * 100:.2f}% -> {stop_loss:.2f}",
    )


def analyze_trade(
    ticker: str,
    history: pd.DataFrame,
    requested_buy_date: pd.Timestamp,
    risk_amount: float,
    stop_loss_mode: str,
    custom_stop_loss: float | None = None,
) -> TradeResult:
    future, entry_row, entry_date, entry_price, entry_mode, entry_reason = resolve_entry_policy(
        history,
        requested_buy_date,
    )
    stop_loss, stop_loss_reason = resolve_stop_loss(
        history=history,
        requested_buy_date=requested_buy_date,
        stop_loss_mode=stop_loss_mode,
        custom_stop_loss=custom_stop_loss,
    )
    initial_stop_loss = stop_loss
    quantity = calculate_quantity(entry_price, stop_loss, risk_amount)
    initial_value = entry_price * quantity
    first_target_dollars = risk_amount * FIRST_TARGET_R_MULTIPLE
    target_close_price = (initial_value + first_target_dollars) / quantity
    first_tranche, second_tranche, final_tranche = build_tranche_sizes(quantity)

    legs: list[TradeLeg] = [
        TradeLeg(
            label="BUY",
            date=entry_date,
            price=entry_price,
            shares=quantity,
            reason=entry_reason,
        )
    ]

    remaining_shares = quantity
    first_take_profit_done = False
    ema21_exit_done = False
    stop_hit = False
    final_exit_date = entry_date

    for _, row in future.iloc[1:].iterrows():
        row_date = pd.Timestamp(row["Date"]).normalize()
        low_price = float(row["Low"])
        close_price = float(row["Close"])
        ema21 = row["EMA21"]
        sma50 = row["SMA50"]

        if low_price <= stop_loss:
            legs.append(
                TradeLeg(
                    label="SELL",
                    date=row_date,
                    price=stop_loss,
                    shares=remaining_shares,
                    reason="Stop loss colpito intraday",
                )
            )
            remaining_shares = 0
            stop_hit = True
            final_exit_date = row_date
            break

        if not first_take_profit_done and close_price * quantity >= initial_value + first_target_dollars:
            legs.append(
                TradeLeg(
                    label="SELL",
                    date=row_date,
                    price=close_price,
                    shares=first_tranche,
                    reason="Primo take profit: valore teorico posizione >= valore iniziale + 2R",
                )
            )
            remaining_shares -= first_tranche
            first_take_profit_done = True
            stop_loss = max(stop_loss, entry_price)
            final_exit_date = row_date

        if (
            first_take_profit_done
            and not ema21_exit_done
            and remaining_shares > final_tranche
            and pd.notna(ema21)
            and close_price < float(ema21)
        ):
            legs.append(
                TradeLeg(
                    label="SELL",
                    date=row_date,
                    price=close_price,
                    shares=second_tranche,
                    reason="Seconda uscita: close sotto EMA21",
                )
            )
            remaining_shares -= second_tranche
            ema21_exit_done = True
            final_exit_date = row_date

        if (
            first_take_profit_done
            and ema21_exit_done
            and remaining_shares > 0
            and pd.notna(sma50)
            and close_price < float(sma50)
        ):
            legs.append(
                TradeLeg(
                    label="SELL",
                    date=row_date,
                    price=close_price,
                    shares=remaining_shares,
                    reason="Uscita finale: close sotto SMA50",
                )
            )
            remaining_shares = 0
            final_exit_date = row_date
            break

    if remaining_shares > 0:
        last_row = future.iloc[-1]
        final_exit_date = pd.Timestamp(last_row["Date"]).normalize()
        legs.append(
            TradeLeg(
                label="SELL",
                date=final_exit_date,
                price=float(last_row["Close"]),
                shares=remaining_shares,
                reason="Chiusura finale all'ultima chiusura disponibile in cache",
            )
        )
        remaining_shares = 0

    proceeds = sum(leg.price * leg.shares for leg in legs if leg.label == "SELL")
    order_count = len(legs)
    total_costs = (order_count * ORDER_COST) + (initial_value * FX_COST_PCT)
    realized_pnl = proceeds - initial_value - total_costs
    return_pct = (realized_pnl / initial_value) * 100 if initial_value else 0.0
    sessions_held = int((future["Date"] <= final_exit_date).sum())

    return TradeResult(
        ticker=ticker,
        buy_date=entry_date,
        risk_amount=risk_amount,
        entry_price=entry_price,
        entry_mode=entry_mode,
        initial_stop_loss=initial_stop_loss,
        stop_loss=stop_loss,
        stop_loss_mode=stop_loss_mode,
        stop_loss_reason=stop_loss_reason,
        quantity=quantity,
        initial_value=initial_value,
        target_close_price=target_close_price,
        first_take_profit_done=first_take_profit_done,
        stop_hit=stop_hit,
        legs=legs,
        realized_pnl=realized_pnl,
        return_pct=return_pct,
        sessions_held=sessions_held,
    )


def format_trade_summary(result: TradeResult) -> str:
    realized_r_multiple = result.realized_pnl / result.risk_amount
    lines = [
        f"Writer script: {SCRIPT_NAME}",
        f"Writer version: {SCRIPT_VERSION}",
        "",
        f"Ticker: {result.ticker}",
        f"Data acquisto effettiva: {result.buy_date.strftime('%Y-%m-%d')}",
        f"Valore di 1R: {result.risk_amount:.2f}",
        f"Modalita ingresso: {result.entry_mode}",
        f"Prezzo ingresso: {result.entry_price:.2f}",
        f"Stop loss iniziale: {result.initial_stop_loss:.2f}",
        f"Stop loss finale/corrente: {result.stop_loss:.2f}",
        f"Origine stop loss: {result.stop_loss_reason}",
        f"Quantity: {result.quantity}",
        f"Valore iniziale: {result.initial_value:.2f}",
        f"Target close primo take profit (2R): {result.target_close_price:.2f}",
        f"Primo take profit eseguito: {'si' if result.first_take_profit_done else 'no'}",
        f"Stop colpito: {'si' if result.stop_hit else 'no'}",
        f"Sedute in posizione: {result.sessions_held}",
        f"P&L realizzato: {result.realized_pnl:.2f}",
        f"P&L realizzato in R: {realized_r_multiple:.2f}",
        f"Rendimento %: {result.return_pct:.2f}",
        "",
        "Movimenti:",
    ]
    lines.extend(
        f"{leg.label} {leg.shares} @ {leg.price:.2f} "
        f"il {leg.date.strftime('%Y-%m-%d')} - {leg.reason}"
        for leg in result.legs
    )
    return "\n".join(lines) + "\n"


def save_trade_outputs(result: TradeResult) -> tuple[Path, Path]:
    output_dir = output_dir_for_trade(result.buy_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = build_trade_stem(result.ticker, result.buy_date)
    summary_path = output_dir / f"{stem}_trade_summary.txt"
    legs_path = output_dir / f"{stem}_trade_legs.csv"

    summary_path.write_text(format_trade_summary(result))
    pd.DataFrame(
        [
            {
                "writer_script": SCRIPT_NAME,
                "writer_version": SCRIPT_VERSION,
                "ticker": result.ticker,
                "buy_date": result.buy_date.strftime("%Y-%m-%d"),
                "risk_amount": round(result.risk_amount, 2),
                "stop_loss_mode": result.stop_loss_mode,
                "initial_stop_loss": round(result.initial_stop_loss, 2),
                "stop_loss": round(result.stop_loss, 2),
                "label": leg.label,
                "date": leg.date.strftime("%Y-%m-%d"),
                "price": round(leg.price, 2),
                "shares": leg.shares,
                "reason": leg.reason,
            }
            for leg in result.legs
        ]
    ).to_csv(legs_path, index=False)
    return summary_path, legs_path


def save_trade_string_outputs(
    buy_date: pd.Timestamp,
    results: list[TradeResult],
) -> tuple[Path, Path]:
    output_dir = output_dir_for_trade(buy_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / f"trade_string_{buy_date.strftime('%Y%m%d')}_summary.csv"
    text_path = output_dir / f"trade_string_{buy_date.strftime('%Y%m%d')}_summary.txt"

    rows = [
        {
            "writer_script": SCRIPT_NAME,
            "writer_version": SCRIPT_VERSION,
            "ticker": result.ticker,
            "buy_date": result.buy_date.strftime("%Y-%m-%d"),
            "risk_amount": round(result.risk_amount, 2),
            "entry_mode": result.entry_mode,
            "stop_loss_mode": result.stop_loss_mode,
            "initial_stop_loss": round(result.initial_stop_loss, 2),
            "stop_loss": round(result.stop_loss, 2),
            "realized_pnl": round(result.realized_pnl, 2),
            "realized_r": round(result.realized_pnl / result.risk_amount, 2),
            "return_pct": round(result.return_pct, 2),
        }
        for result in results
    ]
    summary_df = pd.DataFrame(rows)
    summary_df.to_csv(summary_path, index=False)

    lines = [
        f"Writer script: {SCRIPT_NAME}",
        f"Writer version: {SCRIPT_VERSION}",
        "",
        "Ticker | P&L in R | Rendimento %",
    ]
    lines.extend(
        f"{row['ticker']} | {row['realized_r']:.2f} | {row['return_pct']:.2f}"
        for row in rows
    )
    text_path.write_text("\n".join(lines) + "\n")
    return summary_path, text_path


def save_trade_string_skipped_outputs(
    buy_date: pd.Timestamp,
    skipped_tickers: list[tuple[str, str]],
) -> Path:
    output_dir = output_dir_for_trade(buy_date)
    output_dir.mkdir(parents=True, exist_ok=True)
    skipped_path = output_dir / f"trade_string_{buy_date.strftime('%Y%m%d')}_skipped.csv"

    rows = [
        {
            "writer_script": SCRIPT_NAME,
            "writer_version": SCRIPT_VERSION,
            "ticker": ticker,
            "buy_date": buy_date.strftime("%Y-%m-%d"),
            "skip_reason": reason,
        }
        for ticker, reason in skipped_tickers
    ]
    pd.DataFrame(
        rows,
        columns=["writer_script", "writer_version", "ticker", "buy_date", "skip_reason"],
    ).to_csv(skipped_path, index=False)
    return skipped_path


def run_trade_for_ticker(
    ticker: str,
    requested_buy_date: pd.Timestamp,
    risk_amount: float,
    stop_loss_mode: str,
    custom_stop_loss: float | None = None,
) -> TradeResult:
    history = load_cached_price_history(ticker)
    return analyze_trade(
        ticker=ticker,
        history=history,
        requested_buy_date=requested_buy_date,
        risk_amount=risk_amount,
        stop_loss_mode=stop_loss_mode,
        custom_stop_loss=custom_stop_loss,
    )


def run_strategy_for_tickers(
    tickers: list[str],
    requested_buy_date: pd.Timestamp,
    risk_amount: float,
    stop_loss_mode: str,
    custom_stop_losses: dict[str, float] | None = None,
) -> StrategyRunResult:
    results: list[TradeResult] = []
    skipped_tickers: list[tuple[str, str]] = []

    for ticker in tickers:
        custom_stop_loss = None
        if custom_stop_losses is not None:
            custom_stop_loss = custom_stop_losses.get(ticker)
        try:
            result = run_trade_for_ticker(
                ticker=ticker,
                requested_buy_date=requested_buy_date,
                risk_amount=risk_amount,
                stop_loss_mode=stop_loss_mode,
                custom_stop_loss=custom_stop_loss,
            )
        except ValueError as exc:
            recoverable_errors = [
                "Lo stop loss deve essere inferiore al prezzo di ingresso.",
                "Nessun dato disponibile dalla data richiesta",
                "Policy E21 buffer:",
            ]
            if not any(message in str(exc) for message in recoverable_errors):
                raise
            skipped_tickers.append((ticker, str(exc)))
            print(f"{ticker} saltato: {exc}")
            continue
        results.append(result)

    return StrategyRunResult(
        requested_buy_date=requested_buy_date,
        risk_amount=risk_amount,
        stop_loss_mode=stop_loss_mode,
        results=results,
        skipped_tickers=skipped_tickers,
    )


def print_trade_string_summary(strategy_result: StrategyRunResult) -> None:
    print()
    print("Ticker | P&L in R | Rendimento %")
    for result in strategy_result.results:
        print(
            f"{result.ticker} | "
            f"{(result.realized_pnl / result.risk_amount):.2f} | "
            f"{result.return_pct:.2f}"
        )
    if strategy_result.skipped_tickers:
        print()
        print("Ticker saltati")
        for ticker, reason in strategy_result.skipped_tickers:
            print(f"{ticker} | {reason}")
    print()


def main() -> None:
    mode = prompt_mode()
    if mode == "single":
        ticker = prompt_ticker()
        buy_date = prompt_date("Data acquisto")
        risk_amount = prompt_risk_amount()
        stop_loss_mode = prompt_stop_loss_mode()
        custom_stop_loss = prompt_stop_loss() if stop_loss_mode == "custom" else None

        result = run_trade_for_ticker(
            ticker=ticker,
            requested_buy_date=buy_date,
            risk_amount=risk_amount,
            stop_loss_mode=stop_loss_mode,
            custom_stop_loss=custom_stop_loss,
        )
        summary_path, legs_path = save_trade_outputs(result)

        print()
        print(format_trade_summary(result), end="")
        print()
        print(f"Summary salvato in: {summary_path}")
        print(f"Movimenti salvati in: {legs_path}")
        return

    tickers = prompt_ticker_string()
    buy_date = prompt_date("Data acquisto")
    risk_amount = prompt_risk_amount()
    stop_loss_mode = prompt_stop_loss_mode()
    custom_stop_losses: dict[str, float] | None = None
    if stop_loss_mode == "custom":
        custom_stop_losses = {
            ticker: prompt_stop_loss_for_ticker(ticker)
            for ticker in tickers
        }

    strategy_result = run_strategy_for_tickers(
        tickers=tickers,
        requested_buy_date=buy_date,
        risk_amount=risk_amount,
        stop_loss_mode=stop_loss_mode,
        custom_stop_losses=custom_stop_losses,
    )
    summary_path, text_path = save_trade_string_outputs(buy_date, strategy_result.results)
    print_trade_string_summary(strategy_result)
    print(f"Summary CSV salvato in: {summary_path}")
    print(f"Summary TXT salvato in: {text_path}")


def prompt_stop_loss_for_ticker(ticker: str) -> float:
    raw = input(f"Stop loss assoluto per {ticker}: ").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"Stop loss non valido per {ticker}.") from exc
    if value <= 0:
        raise ValueError(f"Lo stop loss per {ticker} deve essere maggiore di zero.")
    return value


if __name__ == "__main__":
    main()
