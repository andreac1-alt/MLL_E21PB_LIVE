from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from core.screening.first_screen import (
    FirstScreenConfig,
    FirstScreenRunResult,
    print_first_screen_run_summary,
    prompt_sample_size,
    prompt_screen_date,
    run_first_screen_for_date,
)
from core.market.market import load_cached_price_history_any_end
from core.screening.second_screen import (
    SecondScreenConfig,
    SecondScreenRunResult,
    print_second_screen_run_summary,
    run_second_screen_for_date,
)


@dataclass
class RunDayResult:
    requested_screen_date: pd.Timestamp
    screen_date: pd.Timestamp
    first_screen: FirstScreenRunResult
    second_screen: SecondScreenRunResult


def resolve_effective_screen_date(
    requested_screen_date: pd.Timestamp,
) -> pd.Timestamp:
    history = load_cached_price_history_any_end("SPY", config=FirstScreenConfig())
    if history is None or history.empty:
        raise ValueError("Cache SPY non disponibile per risolvere la screen date effettiva.")

    trading_dates = pd.Index(pd.to_datetime(history.index)).sort_values().unique()
    requested_ts = pd.Timestamp(requested_screen_date).normalize()
    effective_idx = trading_dates.searchsorted(requested_ts, side="right") - 1
    if effective_idx < 0:
        raise ValueError(
            f"Nessuna seduta disponibile in cache uguale o precedente a {requested_ts.strftime('%Y-%m-%d')}."
        )
    return pd.Timestamp(trading_dates[effective_idx]).normalize()


def run_day(
    screen_date: pd.Timestamp | None = None,
    sample_size: int | None = None,
    first_screen_config: FirstScreenConfig | None = None,
    second_screen_config: SecondScreenConfig | None = None,
) -> RunDayResult:
    if screen_date is None:
        raise ValueError("screen_date obbligatoria.")
    requested_screen_date = pd.Timestamp(screen_date).normalize()
    effective_screen_date = resolve_effective_screen_date(requested_screen_date)
    first_screen_result = run_first_screen_for_date(
        screen_date=effective_screen_date,
        sample_size=sample_size,
        config=first_screen_config,
    )
    second_screen_result = run_second_screen_for_date(
        screen_date=effective_screen_date,
        config=second_screen_config,
        requested_screen_date=requested_screen_date,
    )
    return RunDayResult(
        requested_screen_date=requested_screen_date,
        screen_date=effective_screen_date,
        first_screen=first_screen_result,
        second_screen=second_screen_result,
    )


def print_run_day_summary(result: RunDayResult) -> None:
    if result.requested_screen_date != result.screen_date:
        print("=== SCREEN DATE ===")
        print(f"SD richiesta: {result.requested_screen_date.strftime('%Y-%m-%d')}")
        print(f"SD usata: {result.screen_date.strftime('%Y-%m-%d')}")
        print("Nota: la data richiesta non era disponibile in cache, uso l'ultima seduta disponibile.")
        print()
    print("=== FIRST SCREEN ===")
    print_first_screen_run_summary(result.first_screen)
    print()
    print("=== SECOND SCREEN ===")
    print_second_screen_run_summary(result.second_screen)


def main() -> None:
    screen_date = prompt_screen_date()
    sample_size = prompt_sample_size()
    result = run_day(screen_date=screen_date, sample_size=sample_size)
    print_run_day_summary(result)


if __name__ == "__main__":
    main()
