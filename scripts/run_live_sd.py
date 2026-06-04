from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

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


def screening_day_dir(screen_date: pd.Timestamp) -> Path:
    normalized = pd.Timestamp(screen_date).normalize()
    return (
        PROJECT_ROOT
        / "output"
        / "screening_day"
        / normalized.strftime("%Y")
        / normalized.strftime("%m")
        / normalized.strftime("%Y%m%d")
    )


def screening_outputs_exist(screen_date: pd.Timestamp) -> bool:
    normalized = pd.Timestamp(screen_date).normalize()
    stamp = normalized.strftime("%Y%m%d")
    day_dir = screening_day_dir(normalized)
    required_files = [
        day_dir / f"first_screen_all_{stamp}.csv",
        day_dir / f"first_screen_passed_{stamp}.csv",
        day_dir / f"first_screen_summary_{stamp}.txt",
        day_dir / f"second_screen_all_{stamp}.csv",
        day_dir / f"second_screen_passed_{stamp}.csv",
        day_dir / f"second_screen_symbols_{stamp}.txt",
    ]
    return all(path.exists() for path in required_files)


def first_missing_screen_date() -> pd.Timestamp | None:
    latest_bd = latest_portfolio_date()
    if latest_bd is None:
        return None
    candidate = next_market_session(latest_bd)
    for _ in range(260):
        if not screening_outputs_exist(candidate):
            return candidate
        candidate = next_market_session(candidate)
    raise SystemExit("Impossibile trovare una SD senza screening gia' fatto entro 260 sedute.")


def prompt_screen_date() -> pd.Timestamp:
    suggested_sd = first_missing_screen_date()
    suffix = f" [Invio = prima SD senza screening {suggested_sd.strftime('%Y-%m-%d')}]" if suggested_sd is not None else ""
    raw = input(f"SD (YYYY-MM-DD){suffix}: ").strip()
    if not raw:
        if suggested_sd is None:
            raise SystemExit("SD obbligatoria: nessun portfolio_state esistente da cui suggerire la prima SD mancante.")
        print(f"Uso SD suggerita: {suggested_sd.strftime('%Y-%m-%d')}", flush=True)
        return suggested_sd
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise SystemExit(f"Formato SD non valido: {raw}. Usa YYYY-MM-DD, es. 2026-05-29.")
    parsed = pd.to_datetime(raw, errors="coerce")
    if pd.isna(parsed):
        raise SystemExit(f"SD non valida: {raw}")
    return pd.Timestamp(parsed).normalize()


def previous_market_session(before_date: pd.Timestamp) -> pd.Timestamp:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(before_date).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized - pd.Timedelta(days=14),
        end_date=normalized - pd.Timedelta(days=1),
    )
    sessions = [pd.Timestamp(session_date).normalize() for session_date in schedule.index]
    if not sessions:
        raise SystemExit(f"Nessuna seduta precedente trovata prima di {normalized.strftime('%Y-%m-%d')}.")
    return sessions[-1]


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


def is_market_session(date_value: pd.Timestamp) -> bool:
    import pandas_market_calendars as mcal

    normalized = pd.Timestamp(date_value).normalize()
    schedule = mcal.get_calendar(DEFAULT_MARKET_CALENDAR).schedule(
        start_date=normalized,
        end_date=normalized,
    )
    return any(pd.Timestamp(session_date).normalize() == normalized for session_date in schedule.index)


def existing_portfolio_dates() -> set[pd.Timestamp]:
    path = portfolio_state_path()
    if not path.exists():
        return set()
    df = pd.read_csv(path, keep_default_na=False)
    if df.empty or "date" not in df.columns:
        return set()
    return {
        pd.Timestamp(value).normalize()
        for value in pd.to_datetime(df["date"], errors="coerce").dropna().tolist()
    }


def validate_sequence(screen_date: pd.Timestamp, buy_date: pd.Timestamp) -> None:
    if not is_market_session(screen_date):
        raise SystemExit(f"SD non e' una seduta {DEFAULT_MARKET_CALENDAR}: {screen_date.strftime('%Y-%m-%d')}.")
    if not is_market_session(buy_date):
        raise SystemExit(f"BD calcolata non e' una seduta {DEFAULT_MARKET_CALENDAR}: {buy_date.strftime('%Y-%m-%d')}.")

    last_date = latest_portfolio_date()
    if last_date is None:
        print("Nessun portfolio_state esistente: prima BD consentita.", flush=True)
        return

    if buy_date in existing_portfolio_dates():
        print(f"Rerun consentito per BD gia' presente: {buy_date.strftime('%Y-%m-%d')}", flush=True)
        return

    expected = next_market_session(last_date)
    if buy_date != expected:
        raise SystemExit(
            "BD fuori sequenza. "
            f"Ultima BD portfolio: {last_date.strftime('%Y-%m-%d')}; "
            f"prossima attesa: {expected.strftime('%Y-%m-%d')}; "
            f"BD calcolata da SD {screen_date.strftime('%Y-%m-%d')}: {buy_date.strftime('%Y-%m-%d')}."
        )
    print(f"Continuita' OK: {last_date.strftime('%Y-%m-%d')} -> {buy_date.strftime('%Y-%m-%d')}", flush=True)


def run_screen_day(screen_date: pd.Timestamp) -> None:
    command = [sys.executable, str(PROJECT_ROOT / "1_run_day.py")]
    print(f"\n=== STEP 1 run_day | SD={screen_date.strftime('%Y-%m-%d')} ===", flush=True)
    user_input = f"{screen_date.strftime('%Y-%m-%d')}\n\n"
    result = subprocess.run(command, cwd=PROJECT_ROOT, input=user_input, text=True)
    if result.returncode != 0:
        raise SystemExit(f"STEP 1 run_day fallito con exit code {result.returncode}.")


def run_trade_step(label: str, script_name: str, buy_date: pd.Timestamp) -> None:
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


def main() -> None:
    screen_date = prompt_screen_date()
    buy_date = previous_market_session(screen_date)
    print(f"BD precedente alla SD {screen_date.strftime('%Y-%m-%d')}: {buy_date.strftime('%Y-%m-%d')}", flush=True)
    validate_sequence(screen_date, buy_date)
    run_screen_day(screen_date)
    run_trade_step("STEP 2 trade_state", "2_run_trade_state_day.py", buy_date)
    run_trade_step("STEP 3 portfolio", "3_build_portfolio_day.py", buy_date)
    print(
        f"\nSD completata: {screen_date.strftime('%Y-%m-%d')} -> BD {buy_date.strftime('%Y-%m-%d')}",
        flush=True,
    )


if __name__ == "__main__":
    main()
