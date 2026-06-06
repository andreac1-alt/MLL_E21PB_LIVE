from __future__ import annotations


def build_portfolio_id(strategy_id: str, variant_id: str) -> str:
    return "base_port"


def build_position_id(
    strategy_id: str,
    variant_id: str,
    ticker: str,
    entry_date_yyyymmdd: str,
    entry_seq: int,
) -> str:
    return f"{ticker.upper()}__{entry_date_yyyymmdd}__{entry_seq:02d}"
