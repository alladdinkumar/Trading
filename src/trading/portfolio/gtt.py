"""GTT viability projection — Monte Carlo over 60-day realised vol (spec §7.2).

For each Good-Till-Triggered order on the user's Kite account, we simulate
`n_paths` geometric-Brownian-motion paths over the remaining horizon. The
drift is the 60-day mean log-return; the diffusion uses the 60-day std.
We report:

  probability_hit       — fraction of paths that touch the trigger value
  expected_days_to_hit  — mean number of trading days the hitting paths took

The simulator is a pure function over a single GttOrder + an OHLCV history
slice so the algorithm stays unit-testable without a Kite session — the
spec design called for this decoupling explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

if TYPE_CHECKING:
    import pandas as pd

    from trading.data.kite import GttOrder


DEFAULT_VOL_WINDOW_DAYS = 60
DEFAULT_HORIZON_DAYS = 60
DEFAULT_N_PATHS = 1000


@dataclass(frozen=True)
class GttViability:
    """Per-GTT viability projection — what the daily brief renders."""

    gtt_id: int
    symbol: str
    type: str                 # "single" / "two-leg" etc.
    trigger_values: list[float] = field(default_factory=list)
    last_price: float | None = None
    probability_hit: float | None = None
    expected_days_to_hit: float | None = None
    horizon_days: int = DEFAULT_HORIZON_DAYS
    note: str | None = None    # populated when projection couldn't be computed


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------


def _log_returns(closes: pd.Series) -> npt.NDArray[np.float64]:
    """Daily log-returns, dropping NaNs introduced by the diff."""
    arr = np.log(closes.to_numpy(dtype=float))
    diffs = np.diff(arr)
    out: npt.NDArray[np.float64] = diffs[np.isfinite(diffs)]
    return out


def simulate_target_hit(
    start_price: float,
    target: float,
    mu_daily: float,
    sigma_daily: float,
    *,
    horizon_days: int,
    n_paths: int,
    seed: int | None = None,
) -> tuple[float, float | None]:
    """Run an n-path GBM simulation; return (probability_hit, expected_days_to_hit).

    The "hit" definition is direction-aware: targets above the start price
    are checked with `>=`, targets below with `<=`. Paths walk by daily
    shocks `dlog ~ N(mu - 0.5*sigma**2, sigma)` so the resulting price is
    geometric (Itô-corrected GBM).

    `expected_days_to_hit` is None when no path hit (so callers can render
    the cell as "—" instead of misreading a 0).
    """
    if sigma_daily <= 0 or horizon_days <= 0 or n_paths <= 0:
        return 0.0, None
    if start_price <= 0 or target <= 0:
        return 0.0, None

    rng = np.random.default_rng(seed)
    drift = mu_daily - 0.5 * sigma_daily**2
    # shocks: shape (n_paths, horizon_days)
    shocks = rng.normal(loc=drift, scale=sigma_daily, size=(n_paths, horizon_days))
    log_paths = np.cumsum(shocks, axis=1)
    price_paths = start_price * np.exp(log_paths)

    hit_mask = price_paths >= target if target >= start_price else price_paths <= target

    # First-hit day per path (1-indexed); rows that never hit get 0.
    any_hit = hit_mask.any(axis=1)
    n_hit = int(any_hit.sum())
    if n_hit == 0:
        return 0.0, None

    first_hit_idx = hit_mask.argmax(axis=1) + 1  # 1-based day count
    hit_days = first_hit_idx[any_hit]
    return float(n_hit / n_paths), float(hit_days.mean())


def _primary_trigger(gtt: GttOrder) -> float | None:
    """Pick the first usable trigger value from a GTT."""
    for v in gtt.trigger_values:
        try:
            fv = float(v)
        except (TypeError, ValueError):
            continue
        if fv > 0:
            return fv
    return None


def project_gtt_viability(
    gtt: GttOrder,
    history: pd.DataFrame,
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    vol_window: int = DEFAULT_VOL_WINDOW_DAYS,
    n_paths: int = DEFAULT_N_PATHS,
    seed: int | None = None,
) -> GttViability:
    """Project P(target hit before horizon expires) for one GTT.

    `history` must have at least `vol_window + 1` daily bars with a `close`
    column; otherwise the viability comes back with `note` set and the
    probability fields left None so the brief can show a graceful skip.
    """
    base = GttViability(
        gtt_id=gtt.id,
        symbol=gtt.tradingsymbol,
        type=gtt.type,
        trigger_values=list(gtt.trigger_values),
        last_price=gtt.last_price,
        horizon_days=horizon_days,
    )

    trigger = _primary_trigger(gtt)
    if trigger is None:
        return GttViability(**{**base.__dict__, "note": "no usable trigger value"})

    if "close" not in history.columns or len(history) < vol_window + 1:
        return GttViability(
            **{**base.__dict__, "note": f"insufficient history (<{vol_window + 1} bars)"}
        )

    closes = history["close"].dropna().tail(vol_window + 1)
    rets = _log_returns(closes)
    if len(rets) < 2:
        return GttViability(**{**base.__dict__, "note": "no usable returns"})

    mu = float(rets.mean())
    sigma = float(rets.std(ddof=1))
    if sigma == 0:
        return GttViability(**{**base.__dict__, "note": "zero realised volatility"})

    start_price = (
        float(gtt.last_price) if gtt.last_price is not None
        else float(closes.iloc[-1])
    )

    prob, exp_days = simulate_target_hit(
        start_price=start_price,
        target=trigger,
        mu_daily=mu,
        sigma_daily=sigma,
        horizon_days=horizon_days,
        n_paths=n_paths,
        seed=seed,
    )

    return GttViability(
        **{
            **base.__dict__,
            "last_price": start_price,
            "probability_hit": prob,
            "expected_days_to_hit": exp_days,
        }
    )


def project_all_gtts(
    gtts: list[GttOrder],
    histories: dict[str, pd.DataFrame],
    *,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
    vol_window: int = DEFAULT_VOL_WINDOW_DAYS,
    n_paths: int = DEFAULT_N_PATHS,
    seed: int | None = None,
) -> list[GttViability]:
    """Project viability for every GTT whose underlying has parquet history.

    Symbols without a matching history come back with `note` populated so
    the markdown briefing can list them as "history unavailable" rather
    than silently dropping rows.
    """
    out: list[GttViability] = []
    import pandas as pd  # local: keep this file importable without pandas at module load

    for gtt in gtts:
        hist = histories.get(gtt.tradingsymbol)
        if hist is None or not isinstance(hist, pd.DataFrame):
            out.append(
                GttViability(
                    gtt_id=gtt.id,
                    symbol=gtt.tradingsymbol,
                    type=gtt.type,
                    trigger_values=list(gtt.trigger_values),
                    last_price=gtt.last_price,
                    horizon_days=horizon_days,
                    note="no OHLCV history on disk",
                )
            )
            continue
        out.append(
            project_gtt_viability(
                gtt,
                hist,
                horizon_days=horizon_days,
                vol_window=vol_window,
                n_paths=n_paths,
                seed=seed,
            )
        )
    return out
