"""Forward-return replay — the pure backtest primitive behind both ML labels
and factor evaluation.

Replays Phase 6 exit logic forward from ``signal_date + 1`` and returns the
net **fractional return** of the resolved trade. Depends only on
``backtest.costs`` and ``strategy.exits`` (both at/below the backtest layer),
so it is the architecturally correct home for this logic: the ranking layer
(ML labels) and the backtest layer (factor eval) both consume it downward.
"""

from __future__ import annotations

import math
from dataclasses import replace

import pandas as pd

from trading.backtest.costs import (
    CostConfig,
    apply_slippage,
    buy_charges,
    sell_charges,
)
from trading.strategy.exits import Bar, TradeState, evaluate_exit

# Forward-looking label horizon in trading days: how far past signal_date a
# label's outcome (`realized_return`) can be realised. This is also the
# minimum embargo a walk-forward fold must leave between its training signals
# and its test window (F-055) — a training label whose outcome window reaches
# a bar on/after the fold's test_start would be graded on data the OOS fold
# is supposed to judge, leaking information across the split.
LABEL_HORIZON_DAYS = 25


def realized_return(
    enriched_df: pd.DataFrame,
    signal_date: pd.Timestamp,
    *,
    atr_stop_multiple: float = 1.5,
    max_days: int = LABEL_HORIZON_DAYS,
    cost_config: CostConfig | None = None,
) -> float | None:
    """Replay Phase 6 exit logic forward from signal_date+1, returning the
    net **fractional return** (net P&L / entry) of the resolved trade.

    Returns None when the forward window is too short to resolve
    (< max_days + 1 bars after signal_date) or the ATR stop is undefined.
    Stop is `signal_date close − atr_stop_multiple × atr_14`; target / trail
    / time-exit are delegated to `strategy.exits.evaluate_exit` unchanged.
    Magnitude (not just sign) is what lets the ranker train on and be judged by
    expectancy rather than hit rate (F-044).
    """
    if signal_date not in enriched_df.index:
        return None
    pos = enriched_df.index.get_loc(signal_date)
    if isinstance(pos, slice):  # duplicate index → ambiguous
        return None
    if pos + max_days >= len(enriched_df):
        return None

    sd_row = enriched_df.iloc[pos]
    sd_close = float(sd_row["close"])
    atr = float(sd_row.get("atr_14", math.nan))
    if math.isnan(atr) or atr <= 0:
        return None
    initial_stop = sd_close - atr_stop_multiple * atr
    if initial_stop <= 0 or initial_stop >= sd_close:
        return None

    cfg = cost_config if cost_config is not None else CostConfig()
    fill_open = float(enriched_df.iloc[pos + 1]["open"])
    entry_price = apply_slippage(fill_open, "buy", cfg)
    qty = 1
    buy_value = entry_price * qty
    buy_breakdown = buy_charges(buy_value, cfg)

    state = TradeState(
        entry=entry_price,
        initial_stop=initial_stop,
        current_stop=initial_stop,
        atr_at_entry=atr,
        days_held=0,
    )

    exit_price: float | None = None
    for offset in range(1, max_days + 1):
        bar_row = enriched_df.iloc[pos + offset]
        bar = Bar(
            open=float(bar_row["open"]),
            high=float(bar_row["high"]),
            low=float(bar_row["low"]),
            close=float(bar_row["close"]),
        )
        decision = evaluate_exit(state, bar)
        if decision.action == "HOLD":
            state = replace(state, current_stop=decision.new_stop, days_held=state.days_held + 1)
            continue
        assert decision.exit_price is not None
        exit_price = apply_slippage(decision.exit_price, "sell", cfg)
        break

    if exit_price is None:
        # Walked through max_days without an exit signal — close at last close.
        last_close = float(enriched_df.iloc[pos + max_days]["close"])
        exit_price = apply_slippage(last_close, "sell", cfg)

    sell_value = exit_price * qty
    sell_breakdown = sell_charges(sell_value, cfg)
    gross = (exit_price - entry_price) * qty
    net = gross - buy_breakdown.total - sell_breakdown.total
    return net / entry_price
