from __future__ import annotations

import argparse

import pandas as pd

from core.trade_state import (
    CLOSED_STATUSES,
    TRADE_STATE_COLUMNS,
    build_new_trade_row,
    find_screen_dates_for_buy_date,
    load_previous_trade_state,
    load_second_screen_passed,
    save_trade_state,
    update_live_trade_row,
)


def is_trade_source_allowed(second_row: pd.Series, buy_date: pd.Timestamp) -> bool:
    semaforo_color = str(second_row.get("semaforo_color", "")).strip().upper()
    if semaforo_color != "BLUE":
        return False
    if pd.Timestamp(buy_date).day_name().upper() != "MONDAY":
        return True
    return str(second_row.get("blue_on", "")).strip().lower() in {"true", "1", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Step 2 live: aggiorna il trade_state alla chiusura di una buy date."
    )
    parser.add_argument("--buy-date", required=True, help="BD in formato YYYY-MM-DD.")
    parser.add_argument(
        "--stop-loss-mode",
        default="system",
        choices=["system"],
        help="Modalita stop iniziale. Per ora il live supporta solo system.",
    )
    return parser


def build_trade_state_for_buy_date(
    buy_date: pd.Timestamp,
    *,
    stop_loss_mode: str = "system",
) -> pd.DataFrame:
    normalized_bd = pd.Timestamp(buy_date).normalize()
    previous_df = load_previous_trade_state(normalized_bd)

    carried_rows: list[dict[str, object]] = []
    if not previous_df.empty:
        for _, row in previous_df.iterrows():
            status = str(row.get("trade_status", "")).strip().upper()
            if status in CLOSED_STATUSES:
                carried = row.to_dict()
                carried["last_update_date"] = normalized_bd
                carried_rows.append(carried)
            else:
                carried_rows.append(update_live_trade_row(row, normalized_bd))

    existing_ids = {str(row.get("trade_id")) for row in carried_rows}
    new_rows: list[dict[str, object]] = []
    for screen_date in find_screen_dates_for_buy_date(normalized_bd):
        second_df = load_second_screen_passed(screen_date)
        if second_df.empty:
            continue
        for _, second_row in second_df.iterrows():
            if not is_trade_source_allowed(second_row, normalized_bd):
                continue
            ticker = str(second_row.get("ticker", "")).strip().upper()
            if not ticker:
                continue
            trade_id = f"{ticker}_{pd.Timestamp(screen_date).strftime('%Y%m%d')}"
            if trade_id in existing_ids:
                continue
            new_rows.append(
                build_new_trade_row(
                    ticker=ticker,
                    screen_date=screen_date,
                    buy_date=normalized_bd,
                    stop_loss_mode=stop_loss_mode,
                )
            )

    if not carried_rows and not new_rows:
        return pd.DataFrame(columns=TRADE_STATE_COLUMNS)
    return pd.DataFrame([*carried_rows, *new_rows]).reindex(columns=TRADE_STATE_COLUMNS)


def main() -> None:
    args = build_parser().parse_args()
    buy_date = pd.Timestamp(args.buy_date).normalize()
    trade_state_df = build_trade_state_for_buy_date(
        buy_date,
        stop_loss_mode=args.stop_loss_mode,
    )
    output_path = save_trade_state(trade_state_df, buy_date)
    print(f"BD: {buy_date.strftime('%Y-%m-%d')}")
    print(f"Trade state rows: {len(trade_state_df)}")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
