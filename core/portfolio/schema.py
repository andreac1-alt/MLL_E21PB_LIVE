from __future__ import annotations


PORTFOLIO_POSITIONS_DAILY_COLUMNS = [
    "date",
    "portfolio_id",
    "strategy_id",
    "variant_id",
    "position_id",
    "ticker",
    "bd",
    "entry_seq",
    "r_multiplier",
    "days_in_trade",
    "shares_initial",
    "shares_open",
    "realized_r_partial",
    "unrealized_r",
    "total_mtm_r",
    "is_open",
    "position_status",
    "opened_today",
    "closed_today",
    "carry_in_from_prev_year",
    "carry_out_to_next_year",
]


PORTFOLIO_ACTIONS_DAILY_COLUMNS = [
    "action_date",
    "portfolio_id",
    "strategy_id",
    "variant_id",
    "position_id",
    "ticker",
    "action_seq",
    "action_type",
    "bd",
    "shares_delta",
    "shares_open_after",
    "realized_r_delta",
    "realized_r_cum_position",
    "note",
]


PORTFOLIO_STATE_DAILY_COLUMNS = [
    "date",
    "portfolio_id",
    "strategy_id",
    "variant_id",
    "equity_mtm_r",
    "realized_r_cum",
    "unrealized_r",
    "mtm_r_day",
    "drawdown_mtm_r",
    "open_positions_count",
    "new_entries_count",
    "partial_exit_count",
    "full_exit_count",
    "semaphore_color",
    "blue_on",
    "has_carry_positions",
    "open_tickers",
]
