from __future__ import annotations


def build_portfolio_id(strategy_id: str, variant_id: str) -> str:
    return f"{strategy_id}__{variant_id}"


def build_position_id(
    strategy_id: str,
    variant_id: str,
    ticker: str,
    entry_date_yyyymmdd: str,
    entry_seq: int,
) -> str:
    return (
        f"{strategy_id}__{variant_id}__{ticker.upper()}__"
        f"{entry_date_yyyymmdd}__{entry_seq:02d}"
    )
