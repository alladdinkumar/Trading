"""Tests for Phase 14.C pre_open_iep — gap filter + reranking."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.data.kite import Quote
from trading.domain import MacroSnapshot
from trading.features.regime import Regime
from trading.jobs.pre_open_iep import (
    PreOpenIepAborted,
    _compute_gaps,
    _compute_scores,
    _filter_by_regime,
    _filter_by_sector,
    _parse_candidates_from_context,
    _rerank,
    _sector_percentile,
    _update_context_markdown,
    run_pre_open_iep,
)
from trading.store.db import get_conn
from trading.store.macro_store import upsert_macro_snapshot
from trading.store.migrations import run_migrations
from trading.store.ohlcv import write_ohlcv

# Unit tests for gap calculation


def test_compute_gaps_basic() -> None:
    """Gap calculation: (ltp - yesterday_close) / yesterday_close * 100."""
    quotes = {
        "RVNL": Quote(
            instrument_token=0,
            open=304.0,
            high=315.0,
            low=300.0,
            last_price=312.0,
            close=304.0,
            volume=100000,
            bid=None,
            ask=None,
            oi=0,
            upper_circuit_limit=None,
            lower_circuit_limit=None,
        ),
    }
    yesterday_closes = {"RVNL": 304.0}

    # Should compute (312 - 304) / 304 * 100 = +2.63%
    gaps = _compute_gaps({"RVNL"}, quotes, yesterday_closes)
    assert "RVNL" in gaps
    assert abs(gaps["RVNL"] - 2.631) < 0.01


def test_compute_gaps_negative() -> None:
    """Gap calculation with negative gap."""
    quotes = {
        "NTPC": Quote(
            instrument_token=0,
            open=175.0,
            high=176.0,
            low=173.0,
            last_price=172.0,
            close=175.0,
            volume=200000,
            bid=None,
            ask=None,
            oi=0,
            upper_circuit_limit=None,
            lower_circuit_limit=None,
        ),
    }
    yesterday_closes = {"NTPC": 175.0}

    # Should compute (172 - 175) / 175 * 100 = -1.71%
    gaps = _compute_gaps({"NTPC"}, quotes, yesterday_closes)
    assert abs(gaps["NTPC"] - (-1.714)) < 0.01


def test_compute_gaps_zero_gap() -> None:
    """Gap calculation when LTP equals yesterday's close."""
    quotes = {
        "RELIANCE": Quote(
            instrument_token=0,
            open=2800.0,
            high=2810.0,
            low=2789.0,
            last_price=2789.0,
            close=2789.0,
            volume=500000,
            bid=None,
            ask=None,
            oi=0,
            upper_circuit_limit=None,
            lower_circuit_limit=None,
        ),
    }
    yesterday_closes = {"RELIANCE": 2789.0}

    gaps = _compute_gaps({"RELIANCE"}, quotes, yesterday_closes)
    assert abs(gaps["RELIANCE"]) < 0.001


def test_compute_gaps_missing_quote() -> None:
    """Gap calculation skips symbols not in quotes dict."""
    quotes = {
        "RVNL": Quote(
            instrument_token=0,
            open=304.0,
            high=315.0,
            low=300.0,
            last_price=312.0,
            close=304.0,
            volume=100000,
            bid=None,
            ask=None,
            oi=0,
            upper_circuit_limit=None,
            lower_circuit_limit=None,
        )
    }
    yesterday_closes = {"RVNL": 304.0, "NTPC": 175.0}

    gaps = _compute_gaps({"RVNL", "NTPC"}, quotes, yesterday_closes)
    assert "RVNL" in gaps
    assert "NTPC" not in gaps


# Unit tests for regime filter


def test_filter_risk_on_keeps_positive_gaps() -> None:
    """RISK_ON regime keeps candidates with gap >= 0%."""
    candidates = ["RVNL", "NTPC", "RELIANCE"]
    gaps = {"RVNL": 2.63, "NTPC": 1.15, "RELIANCE": 0.0}
    regime: Regime = "RISK_ON"

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    assert set(kept) == {"RVNL", "NTPC", "RELIANCE"}
    assert set(removed) == set()


def test_filter_risk_on_removes_negative_gaps() -> None:
    """RISK_ON regime removes candidates with gap < 0%."""
    candidates = ["RVNL", "COALINDIA"]
    gaps = {"RVNL": 2.63, "COALINDIA": -0.63}
    regime: Regime = "RISK_ON"

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    assert set(kept) == {"RVNL"}
    assert set(removed) == {"COALINDIA"}


def test_filter_risk_off_keeps_negative_gaps() -> None:
    """RISK_OFF regime keeps candidates with gap <= 0%."""
    candidates = ["RVNL", "COALINDIA", "RELIANCE"]
    gaps = {"RVNL": -2.0, "COALINDIA": -0.63, "RELIANCE": 0.0}
    regime: Regime = "RISK_OFF"

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    assert set(kept) == {"RVNL", "COALINDIA", "RELIANCE"}
    assert set(removed) == set()


def test_filter_risk_off_removes_positive_gaps() -> None:
    """RISK_OFF regime removes candidates with gap > 0%."""
    candidates = ["RVNL", "NTPC"]
    gaps = {"RVNL": 2.63, "NTPC": 1.15}
    regime: Regime = "RISK_OFF"

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    assert set(kept) == set()
    assert set(removed) == {"RVNL", "NTPC"}


def test_filter_neutral_keeps_all() -> None:
    """NEUTRAL regime keeps all candidates regardless of gap direction."""
    candidates = ["RVNL", "COALINDIA", "RELIANCE"]
    gaps = {"RVNL": 2.63, "COALINDIA": -0.63, "RELIANCE": 0.0}
    regime: Regime = "NEUTRAL"

    kept, removed = _filter_by_regime(candidates, gaps, regime)
    assert set(kept) == {"RVNL", "COALINDIA", "RELIANCE"}
    assert set(removed) == set()


# Unit tests for sector momentum filter


def test_sector_filter_removes_lagging_in_risk_on() -> None:
    """RISK_ON: remove candidates in sectors with negative momentum."""
    candidates = ["RELIANCE", "RVNL"]
    sector_map = {"RELIANCE": "Energy", "RVNL": "PSU/Infra"}
    sector_momentum = {"Energy": -0.3, "PSU/Infra": 1.8}
    regime: Regime = "RISK_ON"

    kept, removed = _filter_by_sector(candidates, sector_map, sector_momentum, regime)
    assert set(kept) == {"RVNL"}
    assert set(removed) == {"RELIANCE"}


def test_sector_filter_keeps_leading_in_risk_on() -> None:
    """RISK_ON: keep candidates in sectors with positive momentum."""
    candidates = ["RVNL", "TATAPOWER"]
    sector_map = {"RVNL": "PSU/Infra", "TATAPOWER": "Power"}
    sector_momentum = {"PSU/Infra": 1.8, "Power": 0.9}
    regime: Regime = "RISK_ON"

    kept, removed = _filter_by_sector(candidates, sector_map, sector_momentum, regime)
    assert set(kept) == {"RVNL", "TATAPOWER"}
    assert set(removed) == set()


def test_sector_percentile_ranking() -> None:
    """Sector momentum percentile ranking: higher momentum = higher percentile."""
    sector_momentum = {"Energy": -0.5, "PSU/Infra": 1.8, "Power": 0.9, "IT": 2.1}

    percentiles = _sector_percentile(sector_momentum)

    # Descending order: IT (2.1) = 100%, PSU/Infra (1.8) ≈ 67%, Power (0.9) ≈ 33%, Energy (-0.5) = 0%
    assert percentiles["IT"] == 100
    assert 60 < percentiles["PSU/Infra"] < 80
    assert 25 < percentiles["Power"] < 40
    assert percentiles["Energy"] < 15


# Unit tests for reranking score


def test_rerank_score_formula() -> None:
    """Rerank score = (gap_normalized × 0.6) + (sector_pct × 0.4)."""
    gaps = {"RVNL": 2.63, "NTPC": 1.15}
    sector_percentiles = {"RVNL": 100.0, "NTPC": 50.0}

    # gap_max = 2.63
    # RVNL: (2.63/2.63 × 0.6) + (100 × 0.4) / 100 = 0.60 + 0.40 = 1.00
    # NTPC: (1.15/2.63 × 0.6) + (50 × 0.4) / 100 = 0.26 + 0.20 = 0.46
    scores = _compute_scores(gaps, sector_percentiles)
    assert abs(scores["RVNL"] - 1.00) < 0.01
    assert abs(scores["NTPC"] - 0.46) < 0.01


def test_rerank_ordering() -> None:
    """Reranked output is sorted descending by score."""
    gaps = {"RVNL": 2.63, "TATAPOWER": 2.08, "NTPC": 1.15}
    sector_percentiles = {"RVNL": 100.0, "TATAPOWER": 50.0, "NTPC": 50.0}

    ordered = _rerank(list(gaps.keys()), gaps, sector_percentiles)

    # Expected order by score: RVNL (1.00), TATAPOWER (0.67), NTPC (0.46)
    assert ordered[0] == "RVNL"
    assert ordered[1] == "TATAPOWER"
    assert ordered[2] == "NTPC"


def test_rerank_ties_stable() -> None:
    """Tied scores preserve original order (stable sort)."""
    gaps = {"RVNL": 2.0, "NTPC": 2.0, "TATAPOWER": 1.5}
    sector_percentiles = {"RVNL": 50.0, "NTPC": 50.0, "TATAPOWER": 50.0}

    ordered = _rerank(["RVNL", "NTPC", "TATAPOWER"], gaps, sector_percentiles)

    # RVNL and NTPC have same score; stable sort preserves original order
    assert ordered[0] == "RVNL"
    assert ordered[1] == "NTPC"
    assert ordered[2] == "TATAPOWER"


# Unit tests for context parsing


def test_parse_candidates_from_context_basic() -> None:
    """Extract candidate symbols from _context.md markdown."""
    context_md = """# Trading context bundle — 2026-05-16

## Today's candidates

### RVNL — passes 9/10 rules

### NTPC — passes 7/10 rules

### RELIANCE — passes 8/10 rules
"""

    symbols = _parse_candidates_from_context(context_md)
    assert symbols == ["RVNL", "NTPC", "RELIANCE"]


def test_parse_candidates_from_context_hyphen_and_ampersand() -> None:
    """Symbols with '-' (BAJAJ-AUTO) or '&' (M&M) must be parsed, not dropped."""
    context_md = """# Trading context bundle — 2026-06-17

## Today's candidates

### BAJAJ-AUTO — passes 10/10 rules

### M&M — passes 8/10 rules

### COALINDIA — passes 9/10 rules
"""

    symbols = _parse_candidates_from_context(context_md)
    assert symbols == ["BAJAJ-AUTO", "M&M", "COALINDIA"]


def test_update_context_preserves_hyphenated_symbol_block() -> None:
    """A hyphenated symbol's block survives the in-place context rewrite."""
    context_md = """# Trading context bundle — 2026-06-17

## Today's candidates

### BAJAJ-AUTO — passes 10/10 rules

bajaj body

### COALINDIA — passes 9/10 rules

coal body
"""

    updated = _update_context_markdown(context_md, ["BAJAJ-AUTO", "COALINDIA"], removed_symbols=[])
    assert "### BAJAJ-AUTO — passes 10/10 rules" in updated
    assert "bajaj body" in updated


def test_parse_candidates_from_context_empty() -> None:
    """Extract candidates from context with no candidates section."""
    context_md = """# Trading context bundle — 2026-05-16

## Macro

Some macro data.
"""

    symbols = _parse_candidates_from_context(context_md)
    assert symbols == []


def test_update_context_reorders_candidates() -> None:
    """Update context markdown with reordered candidates."""
    context_md = """# Trading context bundle — 2026-05-16

## Today's candidates

### RELIANCE — passes 8/10 rules

### RVNL — passes 9/10 rules

### NTPC — passes 7/10 rules
"""

    new_order = ["RVNL", "NTPC", "RELIANCE"]
    updated = _update_context_markdown(context_md, new_order, removed_symbols=[])

    # Check that RVNL now appears first in candidates section
    idx_rvnl = updated.index("### RVNL —")
    idx_ntpc = updated.index("### NTPC —")
    idx_reliance = updated.index("### RELIANCE —")

    assert idx_rvnl < idx_ntpc < idx_reliance


def test_update_context_appends_removed_list() -> None:
    """Update context appends removed candidates with explanation."""
    context_md = """# Trading context bundle — 2026-05-16

## Today's candidates

### RVNL — passes 9/10 rules

### RELIANCE — passes 8/10 rules
"""

    new_order = ["RVNL"]
    removed = ["RELIANCE"]
    updated = _update_context_markdown(context_md, new_order, removed_symbols=removed)

    # Check that removed candidate is listed with explanation
    assert "REMOVED" in updated or "removed" in updated.lower()


# Integration tests for run_pre_open_iep orchestrator


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


def _seed_parquet(paths, symbol: str, last_close: float) -> None:
    df = pd.DataFrame(
        {
            "open": [last_close - 1, last_close],
            "high": [last_close + 1, last_close + 2],
            "low": [last_close - 2, last_close - 1],
            "close": [last_close - 0.5, last_close],
            "volume": [100_000, 120_000],
        },
        index=pd.to_datetime(["2026-05-14", "2026-05-15"]),
    )
    write_ohlcv(df, symbol, paths)


def _seed_quotes(paths, as_of: date, hhmm: str, rows: list[dict]) -> Path:
    base = paths.raw_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"quotes_{hhmm}.json"
    target.write_text(json.dumps(rows), encoding="utf-8")
    return target


def _seed_regime(paths, as_of: date, regime: str) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_macro_snapshot(
            conn,
            MacroSnapshot(
                date=as_of.isoformat(),
                sgx_nifty=None,
                dow_fut=None,
                nasdaq_fut=None,
                sp500=None,
                usdinr=None,
                crude=None,
                vix=None,
                us_10y=None,
                fii_flow_cr=None,
                dii_flow_cr=None,
                regime=regime,
            ),
        )


def _seed_context(paths, as_of: date, symbols: list[str]) -> Path:
    base = paths.research_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    body = ["# Trading context bundle — " + as_of.isoformat(), "", "## Today's candidates", ""]
    for sym in symbols:
        body.extend([f"### {sym} — passes 8/10 rules", "", "rule body here", ""])
    body.extend(["## Macro", "", "macro body here", ""])
    ctx = base / "_context.md"
    ctx.write_text("\n".join(body), encoding="utf-8")
    return ctx


def _quote_row(symbol: str, ltp: float) -> dict:
    return {
        "tradingsymbol": symbol,
        "instrument_token": 0,
        "last_price": ltp,
        "volume": 1000,
        "open": ltp,
        "high": ltp + 1,
        "low": ltp - 1,
        "close": ltp - 2,
        "bid": ltp - 0.1,
        "ask": ltp + 0.1,
        "oi": 0,
        "upper_circuit_limit": None,
        "lower_circuit_limit": None,
    }


def test_run_pre_open_iep_aborts_when_context_missing(paths) -> None:
    with pytest.raises(PreOpenIepAborted) as exc:
        run_pre_open_iep(date(2026, 5, 22), paths=paths)
    assert "_context.md" in str(exc.value)


def test_run_pre_open_iep_no_candidates_returns_early(paths) -> None:
    as_of = date(2026, 5, 22)
    base = paths.research_dir / as_of.isoformat()
    base.mkdir(parents=True, exist_ok=True)
    (base / "_context.md").write_text("# header\n\n## Macro\n", encoding="utf-8")
    result = run_pre_open_iep(as_of, paths=paths)
    assert result.candidates_input == 0
    assert result.candidates_filtered == 0
    assert result.regime == "NEUTRAL"


def test_run_pre_open_iep_risk_on_filters_and_reorders(paths, monkeypatch) -> None:
    """End-to-end: RISK_ON regime drops down-gapper and reranks survivors by gap."""
    from datetime import datetime as _real_dt

    from trading import clock

    as_of = date(2026, 5, 22)

    # F-058: freeze trading.clock.now_ist() (not the host clock) so the 08:55
    # snapshot reads as fresh.
    fake_now = _real_dt(2026, 5, 22, 8, 56, tzinfo=clock.IST)
    monkeypatch.setattr(clock, "now_ist", lambda: fake_now)

    _seed_context(paths, as_of, ["RVNL", "NTPC", "COALINDIA"])
    _seed_parquet(paths, "RVNL", 304.0)
    _seed_parquet(paths, "NTPC", 175.0)
    _seed_parquet(paths, "COALINDIA", 400.0)
    _seed_quotes(
        paths,
        as_of,
        "0855",
        [
            _quote_row("RVNL", 312.0),  # +2.63%
            _quote_row("NTPC", 177.0),  # +1.14%
            _quote_row("COALINDIA", 395.0),  # -1.25% → dropped under RISK_ON
        ],
    )
    _seed_regime(paths, as_of, "RISK_ON")

    result = run_pre_open_iep(as_of, paths=paths)
    assert result.regime == "RISK_ON"
    assert result.candidates_input == 3
    assert result.candidates_filtered == 2
    assert result.candidates_removed == 1
    assert "COALINDIA" in result.removed_symbols
    assert result.rerank_applied is True
    assert result.context_path is not None

    updated = result.context_path.read_text(encoding="utf-8")
    # RVNL (biggest gap) should appear before NTPC
    idx_rvnl = updated.index("### RVNL —")
    idx_ntpc = updated.index("### NTPC —")
    assert idx_rvnl < idx_ntpc
    # COALINDIA listed as removed somewhere
    assert "COALINDIA" in updated
    assert "REMOVED" in updated or "removed" in updated.lower()


def test_run_pre_open_iep_neutral_when_regime_missing(paths) -> None:
    """No macro row → NEUTRAL → no candidates dropped by regime filter."""
    as_of = date(2026, 5, 22)
    _seed_context(paths, as_of, ["RVNL", "NTPC"])
    _seed_parquet(paths, "RVNL", 304.0)
    _seed_parquet(paths, "NTPC", 175.0)
    # No quotes snapshot → graceful degradation; no regime row.
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)

    result = run_pre_open_iep(as_of, paths=paths)
    assert result.regime == "NEUTRAL"
    assert result.candidates_filtered == 2
    assert result.candidates_removed == 0
    assert any("Regime" in w for w in result.warnings)
    assert any("quotes" in w.lower() for w in result.warnings)


def test_pre_open_iep_main_logging_and_failure(monkeypatch, tmp_path):
    import pytest as _pytest

    from trading.jobs import pre_open_iep as job
    from trading.ops import logging_setup

    logger_calls: list[str] = []
    monkeypatch.setattr(logging_setup, "_configured", set())

    def fake_configure(job_name, slack_on_error=True):
        logger_calls.append(job_name)
        return tmp_path / f"{job_name}.log"

    monkeypatch.setattr(job, "configure_logging", fake_configure)

    def fake_run(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(job, "run_pre_open_iep", fake_run)

    with _pytest.raises(RuntimeError, match="simulated"):
        job._main("2026-05-25")

    assert logger_calls == ["pre_open_iep"]


# ---------------------------------------------------------------------------
# Phase 12.6 — auto-load sector_map + sector_momentum
# ---------------------------------------------------------------------------


def _seed_iep_context(paths, as_of: date, candidates: list[str]) -> None:
    """Write a minimal _context.md with the candidate symbols."""
    p = paths.research_dir / as_of.isoformat()
    p.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Trading context bundle — {as_of.isoformat()}  (mode: pre_open)",
        "",
        "## Today's candidates",
        "",
    ]
    for sym in candidates:
        lines.append(f"### {sym} — passes 10/10 rules")
        lines.append("- close 100.00, RSI 60.0, ATR(14) 1.50")
        lines.append("- SMA20 99.00 · SMA50 95.00 · SMA200 90.00")
        lines.append("")
    (p / "_context.md").write_text("\n".join(lines), encoding="utf-8")


def _write_sector_map(paths, mapping: dict[str, str]) -> None:
    static_dir = paths.project_root / "data" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    body = "symbol,sector\n" + "\n".join(f"{s},{c}" for s, c in mapping.items())
    (static_dir / "sector_map.csv").write_text(body, encoding="utf-8")


def test_pre_open_iep_autoloads_sector_map_and_momentum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading.domain import SectorRow
    from trading.store.sector_store import upsert_sector_daily

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_iep_context(paths, as_of, ["INFY", "TATASTEEL"])
    _write_sector_map(paths, {"INFY": "IT", "TATASTEEL": "METAL"})

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [
                SectorRow(
                    date="2026-05-26",
                    sector="IT",
                    close=36000.0,
                    rs_5d=0.02,
                    rs_20d=0.035,
                    rs_60d=0.01,
                    regime="LEADING",
                ),
                SectorRow(
                    date="2026-05-26",
                    sector="METAL",
                    close=9000.0,
                    rs_5d=-0.01,
                    rs_20d=-0.03,
                    rs_60d=-0.02,
                    regime="LAGGING",
                ),
            ],
        )

    result = run_pre_open_iep(as_of)
    assert result.candidates_input == 2
    assert result.candidates_filtered == 2
    assert not any("Sector data unavailable" in w for w in result.warnings)


def test_pre_open_iep_falls_back_to_d_minus_1_sector_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading.domain import SectorRow
    from trading.store.sector_store import upsert_sector_daily

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_iep_context(paths, as_of, ["INFY"])
    _write_sector_map(paths, {"INFY": "IT"})

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [
                SectorRow(
                    date="2026-05-25",
                    sector="IT",
                    close=36000.0,
                    rs_5d=0.02,
                    rs_20d=0.035,
                    rs_60d=0.01,
                    regime="LEADING",
                )
            ],
        )

    result = run_pre_open_iep(as_of)
    assert any("sector data fallback" in w for w in result.warnings)


def test_pre_open_iep_warns_when_no_sector_data_anywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_iep_context(paths, as_of, ["INFY"])

    result = run_pre_open_iep(as_of)
    assert any("Sector data unavailable" in w for w in result.warnings)


def test_pre_open_iep_explicit_empty_dicts_suppress_autoload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing sector_map={} should NOT trigger auto-load."""
    from trading.domain import SectorRow
    from trading.store.sector_store import upsert_sector_daily

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    as_of = date(2026, 5, 26)
    _seed_iep_context(paths, as_of, ["INFY"])
    _write_sector_map(paths, {"INFY": "IT"})
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        upsert_sector_daily(
            conn,
            [
                SectorRow(
                    date="2026-05-26",
                    sector="IT",
                    close=36000.0,
                    rs_5d=0.02,
                    rs_20d=0.035,
                    rs_60d=0.01,
                    regime="LEADING",
                )
            ],
        )

    result = run_pre_open_iep(as_of, sector_map={}, sector_momentum={})
    assert not any("Sector data unavailable" in w for w in result.warnings)
    assert not any("sector data fallback" in w for w in result.warnings)
