"""Phase 6 — exit state-machine tests.

Spec §4.4:
- Target = min(entry × 1.20, entry + 2.5 × (entry − initial_stop))
- Time stop = 25 trading days if not in profit
- Trail = +10% → breakeven; +15% → 1 × ATR
- Stop wins same-bar tie (conservative, §8)
"""

from __future__ import annotations

import pytest

from trading.strategy.exits import (
    TARGET_PCT,
    TARGET_RR,
    TIME_STOP_DAYS,
    TRAIL_TRIGGER_ATR,
    TRAIL_TRIGGER_BREAKEVEN,
    Bar,
    ExitDecision,
    TradeState,
    evaluate_exit,
)

# ---------------------------------------------------------------------------
# Constants — anchored to the spec so accidental edits show up here
# ---------------------------------------------------------------------------


def test_constants_match_spec() -> None:
    assert TIME_STOP_DAYS == 25
    assert TARGET_PCT == 0.20
    assert TARGET_RR == 2.5
    assert TRAIL_TRIGGER_BREAKEVEN == 0.10
    assert TRAIL_TRIGGER_ATR == 0.15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    entry: float = 100.0,
    initial_stop: float = 95.0,
    current_stop: float | None = None,
    atr_at_entry: float = 2.0,
    days_held: int = 1,
) -> TradeState:
    return TradeState(
        entry=entry,
        initial_stop=initial_stop,
        current_stop=initial_stop if current_stop is None else current_stop,
        atr_at_entry=atr_at_entry,
        days_held=days_held,
    )


def _bar(o: float = 100.0, h: float = 101.0, low: float = 99.0, c: float = 100.0) -> Bar:
    return Bar(open=o, high=h, low=low, close=c)


# ---------------------------------------------------------------------------
# Stop branch
# ---------------------------------------------------------------------------


def test_stop_hit_exits_at_current_stop() -> None:
    """bar.low ≤ current_stop → EXIT_STOP at current_stop price."""
    trade = _trade(entry=100, initial_stop=95)
    bar = _bar(o=98, h=99, low=94, c=96)  # low pierced 95
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_STOP"
    assert decision.exit_price == 95.0
    assert decision.new_stop == 95.0
    assert "stop" in decision.reason.lower()


def test_stop_exact_touch_exits() -> None:
    """bar.low == current_stop should still trigger (≤, not <)."""
    trade = _trade(entry=100, initial_stop=95)
    bar = _bar(o=98, h=99, low=95, c=96)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_STOP"


# ---------------------------------------------------------------------------
# Target branch — two paths (R/R first, % first)
# ---------------------------------------------------------------------------


def test_target_hit_rr_first_with_tight_stop() -> None:
    """Tight stop (entry-stop = 2) → 2.5 R/R target = 105 < +20% (120). R/R fires first."""
    trade = _trade(entry=100, initial_stop=98)  # 2.5 × 2 = 5 → target=105
    bar = _bar(o=104, h=106, low=103, c=105)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_TARGET"
    assert decision.exit_price == 105.0


def test_target_hit_pct_first_with_wide_stop() -> None:
    """Wide stop (entry-stop = 10) → 2.5 R/R target = 125 > +20% (120). +20% fires first."""
    trade = _trade(entry=100, initial_stop=90)
    bar = _bar(o=119, h=121, low=118, c=120)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_TARGET"
    assert decision.exit_price == 120.0


def test_target_exact_touch_exits() -> None:
    """bar.high == target_price → ≥, fires."""
    trade = _trade(entry=100, initial_stop=98)  # target 105
    bar = _bar(o=103, h=105, low=102, c=104)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_TARGET"
    assert decision.exit_price == 105.0


def test_target_below_high_no_exit() -> None:
    """Target just above today's high → HOLD."""
    trade = _trade(entry=100, initial_stop=98)  # target 105
    bar = _bar(o=103, h=104.99, low=102, c=104)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"


# ---------------------------------------------------------------------------
# Tie-break: stop + target both hit same bar → stop wins
# ---------------------------------------------------------------------------


def test_stop_wins_when_tied_same_bar() -> None:
    """Volatile bar: high pierces target AND low pierces stop → stop fills (conservative §8)."""
    trade = _trade(entry=100, initial_stop=98)  # target 105
    bar = _bar(o=100, h=106, low=97, c=101)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_STOP"
    assert decision.exit_price == 98.0


# ---------------------------------------------------------------------------
# Time stop
# ---------------------------------------------------------------------------


def test_time_stop_at_loss_after_25d() -> None:
    """days_held=25, close ≤ entry → EXIT_TIME at close."""
    trade = _trade(entry=100, initial_stop=95, days_held=25)
    bar = _bar(o=99, h=100, low=98, c=99)  # close 99 < entry 100
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_TIME"
    assert decision.exit_price == 99.0


def test_time_stop_at_breakeven_fires() -> None:
    """close == entry → still 'not in profit' → time stop fires."""
    trade = _trade(entry=100, initial_stop=95, days_held=25)
    bar = _bar(o=99, h=100, low=98, c=100)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "EXIT_TIME"


def test_time_stop_skipped_when_in_profit() -> None:
    """days_held=25 but close > entry → don't exit; let trail handle it."""
    trade = _trade(entry=100, initial_stop=95, days_held=25)
    bar = _bar(o=104, h=106, low=103, c=105)  # close 105 > entry
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"


def test_time_stop_skipped_before_25d() -> None:
    """days_held<25 → never fires, even at loss."""
    trade = _trade(entry=100, initial_stop=95, days_held=24)
    bar = _bar(o=99, h=100, low=98, c=99)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"


# ---------------------------------------------------------------------------
# Trailing — breakeven and ATR
# ---------------------------------------------------------------------------


def test_trail_breakeven_at_plus_10_pct() -> None:
    """close = entry × 1.10 → new_stop = entry (breakeven), action HOLD."""
    trade = _trade(entry=100, initial_stop=95, atr_at_entry=2.0)
    bar = _bar(o=109, h=111, low=108, c=110)  # +10% close
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.new_stop == 100.0  # entry


def test_trail_atr_at_plus_15_pct() -> None:
    """close = entry × 1.15 → new_stop = close − 1×ATR.

    Use initial_stop=90 so target = min(120, 100 + 2.5×10) = 120, not the tighter 112.5
    we'd get from initial_stop=95 (which would fire EXIT_TARGET before trail can update).
    """
    trade = _trade(entry=100, initial_stop=90, atr_at_entry=2.0)
    bar = _bar(o=114, h=115.5, low=113, c=115)  # high < 120, low > 90
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.new_stop == pytest.approx(113.0)  # 115 − 2


def test_trail_never_ratchets_down() -> None:
    """Already-trailed stop > entry; today's softer close shouldn't lower it."""
    # current_stop=110 (from a previous +15% close at 112, ATR=2 → stop=110).
    trade = _trade(entry=100, initial_stop=95, current_stop=110, atr_at_entry=2.0)
    bar = _bar(o=111, h=112, low=111, c=111)  # close +11%, would imply breakeven trail
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.new_stop == 110.0  # unchanged — max(110, entry=100) = 110


def test_trail_atr_never_lowers_existing_atr_trail() -> None:
    """Today's close ATR-trail < yesterday's current_stop → keep yesterday's."""
    # initial_stop=90 → target=120. current_stop=115 from prior bar.
    # Today close 116 → atr-trail = 114, which is < 115, so stop must stay at 115.
    trade = _trade(entry=100, initial_stop=90, current_stop=115, atr_at_entry=2.0)
    bar = _bar(o=116, h=117, low=116, c=116)  # low > 115 stop, high < 120 target
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.new_stop == 115.0  # max(115, 116-2=114) = 115


def test_trail_inactive_below_plus_10_pct() -> None:
    """close +5% → no trailing yet, stop stays at initial."""
    trade = _trade(entry=100, initial_stop=95)
    bar = _bar(o=104, h=106, low=103, c=105)  # +5% close
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.new_stop == 95.0


# ---------------------------------------------------------------------------
# HOLD / general
# ---------------------------------------------------------------------------


def test_quiet_hold_bar() -> None:
    """No exit hit, no trail trigger → HOLD with unchanged stop and exit_price=None."""
    trade = _trade(entry=100, initial_stop=95)
    bar = _bar(o=100, h=102, low=98, c=101)
    decision = evaluate_exit(trade, bar)
    assert decision.action == "HOLD"
    assert decision.exit_price is None
    assert decision.new_stop == 95.0


def test_exit_decision_is_frozen_dataclass() -> None:
    decision = evaluate_exit(_trade(), _bar())
    with pytest.raises((AttributeError, TypeError)):
        decision.action = "HOLD"  # type: ignore[misc]


def test_exit_branches_return_current_stop_as_new_stop() -> None:
    """When exiting, new_stop should reflect the *active* stop, not a re-computed trail."""
    trade = _trade(entry=100, initial_stop=95, current_stop=95)
    bar = _bar(o=98, h=99, low=94, c=96)  # stop hit
    decision = evaluate_exit(trade, bar)
    assert decision.new_stop == 95.0


def test_decision_type_is_exit_decision() -> None:
    assert isinstance(evaluate_exit(_trade(), _bar()), ExitDecision)


# ---------------------------------------------------------------------------
# A small end-to-end progression (sanity check the state-machine composition)
# ---------------------------------------------------------------------------


def test_full_winner_sequence() -> None:
    """Walk a winning trade through entry → +10% (breakeven) → +15% (ATR trail) → target.

    Initial_stop=90 so target = min(120, 100 + 2.5×10) = 120 — wide enough that
    +10% / +15% bars don't accidentally cross the target before trail can update.
    """
    entry, init_stop, atr = 100.0, 90.0, 2.0
    current_stop = init_stop

    # Day 5: close +10%, breakeven trail (high < target=120)
    d1 = evaluate_exit(
        _trade(
            entry=entry,
            initial_stop=init_stop,
            current_stop=current_stop,
            atr_at_entry=atr,
            days_held=5,
        ),
        _bar(o=109, h=111, low=108, c=110),
    )
    assert d1.action == "HOLD" and d1.new_stop == 100.0
    current_stop = d1.new_stop

    # Day 8: close +15%, ATR trail → 115 − 2 = 113. High 115.5 < target 120.
    d2 = evaluate_exit(
        _trade(
            entry=entry,
            initial_stop=init_stop,
            current_stop=current_stop,
            atr_at_entry=atr,
            days_held=8,
        ),
        _bar(o=114, h=115.5, low=113.5, c=115),
    )
    assert d2.action == "HOLD" and d2.new_stop == pytest.approx(113.0)
    current_stop = d2.new_stop

    # Day 10: target +20% hits (high 125 ≥ 120).  low 121 > trailed stop 113.
    d3 = evaluate_exit(
        _trade(
            entry=entry,
            initial_stop=init_stop,
            current_stop=current_stop,
            atr_at_entry=atr,
            days_held=10,
        ),
        _bar(o=121, h=125, low=121, c=124),
    )
    assert d3.action == "EXIT_TARGET"
    assert d3.exit_price == 120.0  # min(+20%=120, +R/R=125) → +20% wins
