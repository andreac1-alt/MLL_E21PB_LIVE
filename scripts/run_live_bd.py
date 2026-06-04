from __future__ import annotations

import subprocess
import sys
from pathlib import Path
import re

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STRATEGY_ID = "EMA21_SMA50"
DEFAULT_VARIANT_ID = "portfolio_live_trade_state_2026_no_carry_in"
DEFAULT_MARKET_CALENDAR = "NYSE"


def portfolio_state_path() -> Path:
    return (
        PROJECT_ROOT
        / "output"
        / "portfolio_live"
        / "full"
        / DEFAULT_STRATEGY_ID
        / DEFAULT_VARIANT_ID
        / "portfolio_state_daily.csv"
    )


def latest_portfolio_date() -> pd.Timestamp | None:
    path = portfolio_state_path()
    if not path.exists():
        return None
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty or "date" not in df.columns:
        return None
    dates = pd.to_datetime(df["date"], errors="coerce").dropna().dt.normalize().sort_values()
    if dates.empty:
        return None
    return pd.Timestamp(dates.iloc[-1]).normalize()


def prompt_buy_date() -> pd.Timestamp:
    latest_date = latest_portfolio_date()
    next_label = ""
    if latest_date is not None:
        next_label = f" [Invio = prossima BD {next_market_session(latest_date).strftime('%Y-%m-%d')}]"
    raw = input(f"BD (YYYY-MM-DD){next_label}: ").strip()
    if not raw:
        if latest_date is None:
            raise SystemExit("BD obbligatoria: nessun portfolio_state esistente da cui ricavare la prossima BD.")
        next_bd = next_market_session(latest_date)
        print(f"Uso prossima BD attesa: {next_bd.strftime('%Y-%m-%d')}", flush=True)
        return next_bd
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise SystemExit(f"Formato BD non valido: {raw}. Usa YYYY-MM-DD, es. 2026-01-08.")
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        raise SystemExit(f"BD non valida: {raw}")
    return pd.Timestamp(parsed).normalize()


def run_step(label: str, script_name: str, buy_date: pd.Timestamp) -> None:
    command = [
        sys.executable,
        str(PROJECT_ROOT / script_name),
        "--buy-date",
        buy_date.strftime("%Y-%m-%d"),
    ]
    print(f"\n=== {label} | BD={buy_date.strftime('%Y-%m-%d')} ===", flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        raise SystemExit(f"{label} fallito con exit code {result.returncode}.")


def next_market_session(after_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(after_date).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized + pd.Timedelta(days=1),
        end_date=normalized + pd.Timedelta(days=14),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    if not sessions:
        raise SystemExit(f"Nessuna seduta successiva trovata dopo {normalized.strftime('%Y-%m-%d')}.")
    return sessions[0]


def is_market_session(buy_date: pd.Timestamp) -> bool:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(buy_date).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized,
        end_date=normalized,
    )
    return any(pd.Timestamp(session_date).normalize() == normalized for session_date in schedule.index)


def validate_sequence(buy_date: pd.Timestamp) -> None:
    if not is_market_session(buy_date):
        raise SystemExit(f"BD non e' una seduta {DEFAULT_MARKET_CALENDAR}: {buy_date.strftime('%Y-%m-%d')}.")

    last_date = latest_portfolio_date()
    if last_date is None:
        print("Nessun portfolio_state esistente: prima BD consentita.", flush=True)
        return

    path = portfolio_state_path()
    df = pd.read_csv(path, keep_default_na=False)
    dates = pd.to_datetime(df["date"], errors="coerce").dropna().dt.normalize().sort_values()

    if buy_date in set(pd.Timestamp(value).normalize() for value in dates.tolist()):
        print(f"Rerun consentito per BD gia' presente: {buy_date.strftime('%Y-%m-%d')}", flush=True)
        return

    expected = next_market_session(last_date)
    if buy_date != expected:
        raise SystemExit(
            "BD fuori sequenza. "
            f"Ultima BD portfolio: {last_date.strftime('%Y-%m-%d')}; "
            f"prossima attesa: {expected.strftime('%Y-%m-%d')}; "
            f"ricevuta: {buy_date.strftime('%Y-%m-%d')}."
        )
    print(f"Continuita' OK: {last_date.strftime('%Y-%m-%d')} -> {buy_date.strftime('%Y-%m-%d')}", flush=True)


def main() -> None:
    buy_date = prompt_buy_date()
    validate_sequence(buy_date)
    run_step("STEP 2 trade_state", "2_run_trade_state_day.py", buy_date)
    run_step("STEP 3 portfolio", "3_build_portfolio_day.py", buy_date)
    print(f"\nBD completata: {buy_date.strftime('%Y-%m-%d')}", flush=True)


if __name__ == "__main__":
    main()
