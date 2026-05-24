"""Phase 17 — operations layer.

Notification primitives, NSE calendar, loguru configuration, and the
Task Scheduler reminder dispatcher. Import the submodules directly:

    from trading.ops import notify       # the submodule
    from trading.ops.notify import notify  # the dispatcher function
    from trading.ops.calendar import is_trading_day
    from trading.ops.logging_setup import configure_logging
    from trading.ops.runner import SCHEDULE, fire_reminder
"""

from __future__ import annotations
