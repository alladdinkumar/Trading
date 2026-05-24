"""Tests for trading.ui.data — cached read-only data layer."""

from __future__ import annotations

import contextlib
from datetime import date
from pathlib import Path

import pytest

from trading.config import get_paths
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.ui import data as ui_data


@pytest.fixture(autouse=True)
def _clear_cache():
    """Streamlit's @st.cache_data persists across tests; clear before each."""
    with contextlib.suppress(Exception):
        for fn_name in (
            "available_research_dates",
            "load_macro_snapshot",
            "load_macro_history",
            "load_portfolio_snapshots",
            "load_signals_by_date",
            "load_paper_trades",
            "load_predictions",
            "load_holdings",
            "load_gtts",
            "load_quote_snapshot",
            "load_ohlcv_enriched",
            "load_brief_section",
            "list_candidate_briefs",
        ):
            getattr(ui_data, fn_name).clear()
    yield


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    # Force fresh paths
    return get_paths()


@pytest.fixture
def db(paths):
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
    return paths.db_path


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_available_research_dates_empty(paths):
    assert ui_data.available_research_dates() == []


def test_available_research_dates_lists_yyyy_mm_dd_dirs(paths):
    research = paths.research_dir
    research.mkdir(parents=True, exist_ok=True)
    (research / "2026-05-22").mkdir()
    (research / "2026-05-15").mkdir()
    (research / "not-a-date").mkdir()
    out = ui_data.available_research_dates()
    assert out == ["2026-05-22", "2026-05-15"]


# ---------------------------------------------------------------------------
# Macro
# ---------------------------------------------------------------------------


def test_load_macro_snapshot_returns_none_when_absent(paths, db):
    assert ui_data.load_macro_snapshot("2026-05-22") is None


def test_load_macro_snapshot_roundtrip(paths, db):
    with get_conn(paths.db_path) as conn:
        conn.execute(
            "INSERT INTO macro_snapshot (date, vix, regime) VALUES (?,?,?)",
            ("2026-05-22", 19.4, "NEUTRAL"),
        )
        conn.commit()
    snap = ui_data.load_macro_snapshot("2026-05-22")
    assert snap is not None
    assert snap["regime"] == "NEUTRAL"
    assert snap["vix"] == 19.4


def test_load_macro_history_returns_empty_dataframe_when_absent(paths, db):
    df = ui_data.load_macro_history()
    assert df.empty
    assert {"date", "regime", "vix", "fii_flow_cr", "usdinr"}.issubset(df.columns)


# ---------------------------------------------------------------------------
# Portfolio snapshots / equity
# ---------------------------------------------------------------------------


def _seed_snapshots(db_path):
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity, drawdown_pct) VALUES (?,?,?,?,?)",
            [
                ("2026-05-20", 100000.0, "[]", 100000.0, 0.0),
                ("2026-05-21", 100100.0, "[]", 100250.0, 0.0),
                ("2026-05-22", 99800.0, "[]", 100050.0, -0.20),
            ],
        )
        conn.commit()


def test_load_portfolio_snapshots_empty(paths, db):
    df = ui_data.load_portfolio_snapshots()
    assert df.empty


def test_load_portfolio_snapshots_returns_sorted(paths, db):
    _seed_snapshots(paths.db_path)
    df = ui_data.load_portfolio_snapshots()
    assert len(df) == 3
    assert df["equity"].tolist() == [100000.0, 100250.0, 100050.0]


def test_latest_equity_snapshot_returns_triple(paths, db):
    _seed_snapshots(paths.db_path)
    equity, prev, dd = ui_data.latest_equity_snapshot()
    assert equity == 100050.0
    assert prev == 100250.0
    assert dd == -0.20


def test_latest_equity_snapshot_no_data(paths, db):
    assert ui_data.latest_equity_snapshot() == (None, None, None)


# ---------------------------------------------------------------------------
# Holdings / GTTs (via kite_snapshot)
# ---------------------------------------------------------------------------


def test_load_holdings_returns_empty_when_snapshot_missing(paths):
    # No files written → kite_snapshot raises MissingError → data layer returns []
    assert ui_data.load_holdings("2026-05-22") == []


def test_load_holdings_reads_seeded_snapshot(paths, tmp_path):
    from tests.conftest import seed_kite_snapshot

    seed_kite_snapshot(
        paths,
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
            }
        ],
        gtts=[],
        snapshot_at=None,
    )
    holdings = ui_data.load_holdings("2026-05-22")
    assert len(holdings) == 1
    assert holdings[0]["tradingsymbol"] == "RELIANCE"


# ---------------------------------------------------------------------------
# Brief markdown
# ---------------------------------------------------------------------------


def test_load_brief_section_returns_none_when_missing(paths):
    assert ui_data.load_brief_section("2026-05-22", "brief") is None


def test_load_brief_section_reads_known_filenames(paths):
    d = paths.research_dir / "2026-05-22"
    d.mkdir(parents=True, exist_ok=True)
    (d / "brief.md").write_text("# Today\n", encoding="utf-8")
    (d / "_context.md").write_text("# Ctx\n", encoding="utf-8")
    assert ui_data.load_brief_section("2026-05-22", "brief") == "# Today\n"
    assert ui_data.load_brief_section("2026-05-22", "context") == "# Ctx\n"


def test_list_candidate_briefs_empty(paths):
    assert ui_data.list_candidate_briefs("2026-05-22") == []


def test_list_candidate_briefs_lists_stems(paths):
    cdir = paths.research_dir / "2026-05-22" / "candidates"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "RELIANCE.md").write_text("# r", encoding="utf-8")
    (cdir / "NTPC.md").write_text("# n", encoding="utf-8")
    assert sorted(ui_data.list_candidate_briefs("2026-05-22")) == ["NTPC", "RELIANCE"]
