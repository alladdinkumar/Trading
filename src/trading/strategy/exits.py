"""Exit state machine — spec §4.4.

One end-of-day bar at a time. The caller (Phase 7 backtest, Phase 11 paper
ledger) is responsible for:

- Bumping `days_held` between calls.
- Threading `decision.new_stop` into the next bar's `current_stop`.
- Applying slippage / costs on top of the returned `exit_price`.

Exit precedence (within one bar):
    1. Stop  — fires if bar.low ≤ current_stop (intra-bar, conservative).
    2. Target — fires if bar.high ≥ target_price (intra-bar).
    3. Time stop — fires if days_held ≥ 25 AND bar.close ≤ entry (at close).
    4. Otherwise HOLD, with trailing stop possibly ratcheted up.

Tie-break: stop wins same-bar ties with target (spec §8: conservative fills).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ExitAction = Literal["HOLD", "EXIT_TARGET", "EXIT_STOP", "EXIT_TIME"]

TIME_STOP_DAYS = 25
TARGET_PCT = 0.20
TARGET_RR = 2.5
TRAIL_TRIGGER_BREAKEVEN = 0.10
TRAIL_TRIGGER_ATR = 0.15


@dataclass(frozen=True)
class TradeState:
    entry: float
    initial_stop: float
    current_stop: float
    atr_at_entry: float
    days_held: int


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class ExitDecision:
    action: ExitAction
    new_stop: float
    exit_price: float | None
    reason: str


def target_price(entry: float, initial_stop: float) -> float:
    """min(+20%, 2.5 × R/R) — whichever comes first.

    Public so signal-emitting code (e.g. `pre_open._step_auto_open`) can set
    `signal.target` to the exact price the exit engine aims for, instead of a
    bare +20% that disagrees with the engine (F-029).
    """
    pct_target = entry * (1 + TARGET_PCT)
    rr_target = entry + TARGET_RR * (entry - initial_stop)
    return min(pct_target, rr_target)


def _trail_new_stop(entry: float, current_stop: float, atr_at_entry: float, close: float) -> float:
    """Ratchet stop up based on closing P&L. Never lowers."""
    move_pct = (close - entry) / entry
    if move_pct >= TRAIL_TRIGGER_ATR:
        return max(current_stop, close - atr_at_entry)
    if move_pct >= TRAIL_TRIGGER_BREAKEVEN:
        return max(current_stop, entry)
    return current_stop


def evaluate_exit(trade: TradeState, bar: Bar) -> ExitDecision:
    # 1. Intra-bar stop — conservative: stop wins ties with target.
    if bar.low <= trade.current_stop:
        return ExitDecision(
            action="EXIT_STOP",
            new_stop=trade.current_stop,
            exit_price=trade.current_stop,
            reason=f"stop hit at ₹{trade.current_stop:.2f} (low ₹{bar.low:.2f})",
        )

    # 2. Intra-bar target.
    target = target_price(trade.entry, trade.initial_stop)
    if bar.high >= target:
        return ExitDecision(
            action="EXIT_TARGET",
            new_stop=trade.current_stop,
            exit_price=target,
            reason=f"target hit at ₹{target:.2f} (high ₹{bar.high:.2f})",
        )

    # 3. Time stop at close, only when not in profit.
    if trade.days_held >= TIME_STOP_DAYS and bar.close <= trade.entry:
        pnl_pct = (bar.close - trade.entry) / trade.entry * 100
        return ExitDecision(
            action="EXIT_TIME",
            new_stop=trade.current_stop,
            exit_price=bar.close,
            reason=f"time stop at {trade.days_held}d, P&L {pnl_pct:+.1f}%",
        )

    # 4. HOLD — maybe ratchet stop.
    new_stop = _trail_new_stop(trade.entry, trade.current_stop, trade.atr_at_entry, bar.close)
    return ExitDecision(
        action="HOLD",
        new_stop=new_stop,
        exit_price=None,
        reason=f"hold (close ₹{bar.close:.2f}, stop ₹{new_stop:.2f})",
    )
