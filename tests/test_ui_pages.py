"""Smoke tests for Streamlit pages — render-without-exception per page.

Uses ``streamlit.testing.v1.AppTest`` to load each page headless and assert
the run completes with no exception, the title is present, and at least one
key marker (header text) appears. Empty-DB and seeded-DB variants ensure
the empty-state branches work too.
"""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from trading.config import get_paths
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.ui import data as ui_data

# Page entry points (resolved at runtime so tests work from any cwd)
_REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_APP = _REPO_ROOT / "src" / "trading" / "ui" / "Home.py"
PAGE_PORTFOLIO = _REPO_ROOT / "src" / "trading" / "ui" / "pages" / "1_Kite_Portfolio.py"
PAGE_SIGNALS = _REPO_ROOT / "src" / "trading" / "ui" / "pages" / "2_Today_Signals.py"
PAGE_JOURNAL = _REPO_ROOT / "src" / "trading" / "ui" / "pages" / "3_Paper_Journal.py"
PAGE_PAPER_PORTFOLIO = _REPO_ROOT / "src" / "trading" / "ui" / "pages" / "4_Paper_Portfolio.py"

DEFAULT_TIMEOUT = 30  # seconds


@pytest.fixture(autouse=True)
def _clear_cache():
    for fn_name in (
        "available_research_dates",
        "load_macro_snapshot",
        "load_macro_history",
        "load_portfolio_snapshots",
        "load_signals_by_date",
        "load_paper_trades",
        "load_cash_ledger",
        "load_paper_positions",
        "load_predictions",
        "load_holdings",
        "load_gtts",
        "load_quote_snapshot",
        "load_ohlcv_enriched",
        "load_brief_section",
        "list_candidate_briefs",
    ):
        with contextlib.suppress(Exception):
            getattr(ui_data, fn_name).clear()
    yield


@pytest.fixture
def empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An initialised but empty project root + DB."""
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    paths = get_paths()
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
    return paths


@pytest.fixture
def seeded_project(empty_project):
    """Empty project + a research dir + a portfolio snapshot + a macro snapshot."""
    paths = empty_project
    today = "2026-05-22"
    (paths.research_dir / today).mkdir(parents=True, exist_ok=True)
    (paths.research_dir / today / "brief.md").write_text(
        "# Today brief\nMarket NEUTRAL.\n", encoding="utf-8"
    )
    with get_conn(paths.db_path) as conn:
        conn.execute(
            "INSERT INTO macro_snapshot (date, vix, usdinr, fii_flow_cr, regime) VALUES (?,?,?,?,?)",
            (today, 19.4, 95.7, 187.0, "NEUTRAL"),
        )
        conn.execute(
            "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity, drawdown_pct) VALUES (?,?,?,?,?)",
            (today, 100000.0, "[]", 100000.0, 0.0),
        )
        conn.commit()
    return paths


# ---------------------------------------------------------------------------
# Overview (app.py)
# ---------------------------------------------------------------------------


def test_overview_renders_with_empty_db(empty_project):
    at = AppTest.from_file(str(PAGE_APP), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    # Title should appear
    titles = [m.value for m in at.markdown if "Overview" in (m.value or "")]
    assert titles, "Expected an Overview header to be rendered"


def test_overview_renders_with_seeded_data(seeded_project):
    at = AppTest.from_file(str(PAGE_APP), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    # Equity tile should show the seeded value
    metric_values = [m.value for m in at.metric]
    assert any("100,000" in v for v in metric_values if v)


# ---------------------------------------------------------------------------
# Portfolio
# ---------------------------------------------------------------------------


def test_portfolio_renders_empty_state_without_kite_snapshot(empty_project):
    at = AppTest.from_file(str(PAGE_PORTFOLIO), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    # Should hit st.stop() and render the empty-state — page didn't crash
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "No Kite snapshot" in markdown_text


def test_portfolio_renders_with_seeded_kite_snapshot(seeded_project):
    from tests.conftest import seed_kite_snapshot

    seed_kite_snapshot(
        seeded_project,
        date(2026, 5, 22),
        holdings=[
            {
                "tradingsymbol": "RELIANCE",
                "exchange": "NSE",
                "isin": "INE002A01018",
                "quantity": 10,
                "average_price": 1200.0,
                "last_price": 1250.0,
                "close_price": 1245.0,
                "pnl": 500.0,
                "day_change": 5.0,
                "day_change_percentage": 0.4,
            },
        ],
        gtts=[],
    )
    at = AppTest.from_file(str(PAGE_PORTFOLIO), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    # KPI tiles should render
    assert any("Holdings value" in m.label for m in at.metric)


# ---------------------------------------------------------------------------
# Paper Portfolio
# ---------------------------------------------------------------------------


def test_paper_portfolio_renders_empty_state(empty_project):
    at = AppTest.from_file(str(PAGE_PAPER_PORTFOLIO), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "Paper Portfolio" in markdown_text
    assert "No open paper positions" in markdown_text


def test_paper_portfolio_renders_with_seeded_position(empty_project):
    from trading.paper.funds import add_funds
    from trading.paper.ledger import log_signal_and_open_trade
    from trading.store.repo import Signal

    paths = empty_project
    with get_conn(paths.db_path) as conn:
        sig = Signal(
            id=None, ts="2026-06-01T09:20:00", symbol="ACME", side="LONG",
            entry=100.0, stop=90.0, target=120.0, horizon_days=15, created_by="auto",
        )
        log_signal_and_open_trade(
            conn, signal=sig, entry_ts="2026-06-01T09:20:00",
            entry_price=100.0, qty=10, atr_at_entry=2.0,
        )
        conn.execute(
            "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) VALUES (?,?,?,?)",
            ("2026-06-02", 99000.0, '{"ACME": {"qty": 10, "value": 1300.0}}', 100300.0),
        )
        add_funds(conn, amount=20_000.0, date="2026-06-01")
        conn.commit()
    at = AppTest.from_file(str(PAGE_PAPER_PORTFOLIO), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    assert any("Invested" in m.label for m in at.metric)


# ---------------------------------------------------------------------------
# Today Signals
# ---------------------------------------------------------------------------


def test_today_signals_renders_empty_state(empty_project):
    at = AppTest.from_file(str(PAGE_SIGNALS), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "No signals persisted" in markdown_text or "No candidate briefs" in markdown_text


def test_today_signals_renders_regime_banner_when_macro_present(seeded_project):
    at = AppTest.from_file(str(PAGE_SIGNALS), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    # NEUTRAL banner — st.info renders inside `info` collection
    info_messages = [m.value for m in at.info]
    assert any("NEUTRAL" in (v or "") for v in info_messages)


# ---------------------------------------------------------------------------
# Paper Journal
# ---------------------------------------------------------------------------


def test_paper_journal_renders_empty_state(empty_project):
    at = AppTest.from_file(str(PAGE_JOURNAL), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    markdown_text = " ".join(m.value or "" for m in at.markdown)
    assert "Open trades" in markdown_text


def test_paper_journal_renders_metric_tiles(empty_project):
    at = AppTest.from_file(str(PAGE_JOURNAL), default_timeout=DEFAULT_TIMEOUT).run()
    assert not at.exception
    labels = [m.label for m in at.metric]
    assert "Closed trades" in labels
    assert "Hit rate" in labels
    assert "Profit factor" in labels
