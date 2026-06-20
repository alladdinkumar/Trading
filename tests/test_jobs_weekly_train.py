"""Tests for trading.jobs.weekly_train."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from trading.config import get_paths
from trading.data.news import NewsItem
from trading.store.migrations import run_migrations

AS_OF = date(2026, 6, 14)  # a Sunday


def test_walkforward_start_leaves_room_for_oos_folds() -> None:
    """F-037: the walk-forward start must precede `end − train_years` so at least
    one train(3y)+test(6mo) fold fits. The old 3y span yielded zero folds, so
    `oos_sharpe` was always NaN and no model ever promoted."""
    from trading.backtest.walkforward import WalkForwardConfig, windows
    from trading.jobs.weekly_train import walkforward_start

    end = date(2026, 6, 16)
    wf_start = walkforward_start(end)
    folds = windows(pd.Timestamp(wf_start), pd.Timestamp(end), WalkForwardConfig())
    assert len(folds) >= 1, "walk-forward start must leave room for >=1 OOS fold"

    # Regression: the buggy 3y span produced exactly zero folds.
    three_y = (pd.Timestamp(end) - pd.DateOffset(years=3)).date()
    zero = windows(pd.Timestamp(three_y), pd.Timestamp(end), WalkForwardConfig())
    assert len(zero) == 0


@pytest.fixture
def paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    return get_paths()


@pytest.fixture
def notify_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "trading.jobs.weekly_train.notify",
        lambda level, title, body="": calls.append((level, title, body)),
    )
    return calls


def _memdb() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    run_migrations(c)
    return c


def _seed_week(conn: sqlite3.Connection) -> None:
    """One closed trade in-window, one open trade, one matured prediction,
    two portfolio snapshots."""
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, created_by) "
        "VALUES ('2026-06-08T08:30:00', 'RVNL', 'LONG', 100.0, 95.0, 120.0, 25, 'test')"
    )
    sig1 = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, ts_exit, "
        "exit_price, exit_reason, pnl, pnl_pct, days_held) "
        "VALUES (?, '2026-06-08T09:15:00', 100.0, 10, '2026-06-12T15:30:00', 106.0, "
        "'TARGET', 60.0, 6.0, 4)",
        (sig1,),
    )
    cur = conn.execute(
        "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, created_by) "
        "VALUES ('2026-06-10T08:30:00', 'NTPC', 'LONG', 300.0, 290.0, 360.0, 25, 'test')"
    )
    sig2 = cur.lastrowid
    conn.execute(
        "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, current_stop) "
        "VALUES (?, '2026-06-10T09:15:00', 300.0, 5, 292.0)",
        (sig2,),
    )
    conn.execute(
        "INSERT INTO predictions (ts, symbol, predicted_return_pct, predicted_horizon_days, "
        "actual_return_at_horizon, error_pct, evaluated_at) "
        "VALUES ('2026-05-01T08:30:00', 'RVNL', 20.0, 25, 6.0, 14.0, '2026-06-05T16:00:00')"
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
        "VALUES ('2026-06-11', 100000, '{}', 100000)"
    )
    conn.execute(
        "INSERT INTO portfolio_snapshots (date, cash, holdings_json, equity) "
        "VALUES ('2026-06-12', 100060, '{}', 100060)"
    )
    conn.commit()


def _ohlcv_frame(n: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    idx.name = "date"
    closes = [100.0 + i * 0.1 for i in range(n)]
    return pd.DataFrame(
        {
            "open": [c - 0.5 for c in closes],
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _neg(symbol: str, ts: str, sentiment: float) -> NewsItem:
    return NewsItem(
        ts=ts,
        symbol=symbol,
        source="mc",
        headline=f"{symbol} {ts}",
        url=f"https://x/{symbol}/{ts}",
        sentiment=sentiment,
    )


def test_step_retrain_feeds_negative_news_lookup(paths, monkeypatch) -> None:
    """F-031: the retrain step must pass the populated negative_news_lookup to
    train_walkforward (not the old `{}`), so the feature is no longer NaN in
    training while populated at inference."""
    import trading.jobs.weekly_train as wt
    from trading.store.db import get_conn
    from trading.store.news_store import insert_news_items
    from trading.store.ohlcv import write_ohlcv
    from trading.strategy.ranker_train import InsufficientDataError

    write_ohlcv(_ohlcv_frame(300), "LONGSYM", paths)

    captured: dict[str, object] = {}

    def fake_train(**kwargs: object) -> None:
        captured.update(kwargs)
        raise InsufficientDataError("stub — capture only")

    monkeypatch.setattr(wt, "train_walkforward", fake_train)

    with get_conn(paths.db_path) as conn:
        run_migrations(conn)
        insert_news_items(
            conn,
            [
                _neg("LONGSYM", "2024-03-03T10:00:00+00:00", -0.5),
                _neg("LONGSYM", "2024-03-05T10:00:00+00:00", -0.3),
            ],
        )
        outcome = wt._step_retrain(paths, conn, date(2024, 12, 31), skip_train=False)

    assert outcome.ran is False  # InsufficientDataError → graceful skip
    lookup = captured["negative_news_lookup"]
    assert lookup, "weekly_train starved negative_news_lookup (F-031)"
    assert lookup[("2024-03-06", "LONGSYM")] == 2


def test_lag_attribution_groups_by_conviction_and_regime() -> None:
    """F-040: matured predictions are sliced by entry-condition cohort so the
    laggard cohorts (low hit rate / negative mean error) are visible."""
    import pytest

    from trading.jobs.weekly_train import gather_lag_attribution

    conn = _memdb()
    conn.execute(
        "INSERT INTO predictions (ts, symbol, predicted_return_pct, predicted_horizon_days, "
        "actual_return_at_horizon, error_pct, evaluated_at, regime, conviction) VALUES "
        "('2026-05-01','A',10,25, 5.0, -5.0, '2026-06-01','RISK_OFF','LOW'),"
        "('2026-05-02','B',10,25, -3.0, -13.0, '2026-06-02','RISK_OFF','LOW')"
    )
    # An un-matured (evaluated_at NULL) row must be ignored.
    conn.execute(
        "INSERT INTO predictions (ts, symbol, predicted_return_pct, predicted_horizon_days, "
        "regime, conviction) VALUES ('2026-06-10','C',10,25,'RISK_ON','HIGH')"
    )
    cohorts = {(c.dimension, c.value): c for c in gather_lag_attribution(conn)}

    conv = cohorts[("conviction", "LOW")]
    assert conv.n == 2
    assert conv.hit_rate == pytest.approx(0.5)  # one +ve actual, one -ve
    reg = cohorts[("regime", "RISK_OFF")]
    assert reg.n == 2
    assert reg.mean_error_pct == pytest.approx(-9.0)
    assert ("conviction", "HIGH") not in cohorts  # not yet matured


def test_gather_review_data_empty() -> None:
    from trading.jobs.weekly_train import gather_review_data

    data = gather_review_data(_memdb(), AS_OF)
    assert data.closed_week == []
    assert data.closed_all == []
    assert data.open_trades == []
    assert data.equity.empty
    assert data.calibration == []


def test_gather_review_data_seeded() -> None:
    from trading.jobs.weekly_train import gather_review_data

    conn = _memdb()
    _seed_week(conn)
    data = gather_review_data(conn, AS_OF)
    assert len(data.closed_week) == 1
    assert data.closed_week[0].symbol == "RVNL"
    assert data.closed_week[0].net_pnl == 60.0
    assert data.closed_week[0].initial_stop == 95.0
    assert len(data.closed_all) == 1
    assert len(data.open_trades) == 1
    assert data.open_trades[0].current_stop == 292.0
    assert list(data.equity) == [100000.0, 100060.0]
    assert len(data.calibration) == 1
    assert data.calibration[0].predicted_pct == 20.0
    assert data.calibration[0].n == 1


def test_week_window_boundaries() -> None:
    """Exit exactly on week_start (as_of − 7) excluded; on as_of included."""
    from trading.jobs.weekly_train import gather_review_data

    conn = _memdb()
    for i, ts_exit in enumerate(["2026-06-07T15:30:00", "2026-06-14T15:30:00"]):
        cur = conn.execute(
            "INSERT INTO signals (ts, symbol, side, entry, stop, target, horizon_days, "
            "created_by) VALUES (?, ?, 'LONG', 100.0, 95.0, 120.0, 25, 'test')",
            (f"2026-06-0{i + 1}T08:30:00", f"SYM{i}"),
        )
        conn.execute(
            "INSERT INTO paper_trades (signal_id, ts_entry, entry_price, qty, ts_exit, "
            "exit_price, exit_reason, pnl, pnl_pct, days_held) "
            "VALUES (?, '2026-06-01T09:15:00', 100.0, 10, ?, 101.0, 'TIME', 10.0, 1.0, 5)",
            (cur.lastrowid, ts_exit),
        )
    conn.commit()

    data = gather_review_data(conn, AS_OF)
    assert len(data.closed_all) == 2
    assert len(data.closed_week) == 1
    assert data.closed_week[0].symbol == "SYM1"


def test_render_review_empty_placeholders() -> None:
    from trading.jobs.weekly_train import (
        RetrainOutcome,
        ReviewData,
        render_weekly_review,
    )

    data = ReviewData(
        week_start=date(2026, 6, 7),
        as_of=AS_OF,
        closed_week=[],
        closed_all=[],
        open_trades=[],
        equity=pd.Series(dtype=float),
        calibration=[],
    )
    text = render_weekly_review(
        data, RetrainOutcome(False, "skip_train requested", None, None, False, None)
    )
    assert text.count("_(no data)_") >= 3
    assert "# Weekly review — 2026-06-14" in text
    assert "Trained: no — skip_train requested" in text


def test_render_review_seeded_snapshot(snapshot) -> None:
    from trading.jobs.weekly_train import (
        RetrainOutcome,
        gather_review_data,
        render_weekly_review,
    )

    conn = _memdb()
    _seed_week(conn)
    data = gather_review_data(conn, AS_OF)
    text = render_weekly_review(
        data,
        RetrainOutcome(True, None, 42, float("nan"), False, "models/ranker_2026-06-14.pkl"),
    )
    assert "| RVNL | 100.00 | 106.00 |" in text
    assert "| NTPC |" in text
    assert "Trained: yes — 42 examples" in text
    assert text == snapshot


def test_run_skip_train_writes_review(paths, notify_calls) -> None:
    from trading.jobs.weekly_train import run_weekly_train

    result = run_weekly_train(AS_OF, paths=paths, skip_train=True)
    assert result.retrain_ran is False
    assert result.retrain_skip_reason == "skip_train requested"
    assert result.window_end == AS_OF
    assert result.window_start == date(2023, 6, 14)
    assert result.review_path.is_file()
    text = result.review_path.read_text(encoding="utf-8")
    assert text.count("_(no data)_") >= 3
    assert len(notify_calls) == 1
    level, title, _body = notify_calls[0]
    assert level == "info"
    assert "2026-06-14" in title


def test_run_weekly_train_prunes_stale_data(paths, notify_calls) -> None:
    """The Sunday housekeeping slot prunes old raw date-dirs + news rows (F-013)."""
    from trading.config import get_paths
    from trading.jobs.weekly_train import run_weekly_train
    from trading.store.db import get_conn
    from trading.store.migrations import run_migrations
    from trading.store.news_store import insert_news_items

    p = get_paths()
    stale_dir = p.raw_dir / "2025-01-01"  # well past the 30d raw window
    stale_dir.mkdir(parents=True)
    (stale_dir / "holdings.json").write_text("{}", encoding="utf-8")
    with get_conn(p.db_path) as conn:
        run_migrations(conn)
        insert_news_items(
            conn,
            [
                NewsItem(
                    ts="2024-01-01T10:00:00+00:00",  # past the 365d news window
                    symbol="RELIANCE",
                    source="mc",
                    headline="ancient",
                    url="https://x/old",
                    sentiment=0.0,
                    category="results",
                    is_critical=False,
                )
            ],
        )

    result = run_weekly_train(AS_OF, paths=paths, skip_train=True)

    assert result.retention.applied is True
    assert result.retention.raw_dirs_deleted == ["2025-01-01"]
    assert result.retention.news_rows_deleted == 1
    assert not stale_dir.exists()
    with get_conn(p.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM news_items").fetchone()["c"] == 0


def test_retrain_guard_skips_duplicate_window(paths, notify_calls, monkeypatch) -> None:
    from trading.jobs import weekly_train as wt
    from trading.store.model_registry import RegistryRow, register

    register(
        paths,
        row=RegistryRow(
            version="2026-06-14",
            trained_at="2026-06-14T05:00:00+00:00",
            train_start="2023-06-14",
            train_end="2026-06-14",
            oos_sharpe=float("nan"),
            oos_hit_rate=float("nan"),
            n_train_examples=40,
            n_features=20,
            path="models/ranker_2026-06-14.pkl",
            active=False,
            notes="",
        ),
        promote=False,
    )
    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: pytest.fail("guard must short-circuit before loading inputs"),
    )

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is False
    assert "2026-06-14" in (result.retrain_skip_reason or "")
    assert result.review_path.is_file()


def test_insufficient_data_still_writes_review(paths, notify_calls, monkeypatch) -> None:
    from trading.jobs import weekly_train as wt
    from trading.strategy.ranker_io import TrainingInputs
    from trading.strategy.ranker_train import InsufficientDataError

    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: TrainingInputs(
            enriched={"RVNL": pd.DataFrame({"close": [1.0]})},
            macro_history=pd.DataFrame(),
            sentiment_lookup={},
            negative_news_lookup={},
        ),
    )

    def _raise(**_kw):
        raise InsufficientDataError("only 3 examples")

    monkeypatch.setattr(wt, "train_walkforward", _raise)

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is False
    assert "only 3 examples" in (result.retrain_skip_reason or "")
    assert result.review_path.is_file()
    assert len(notify_calls) == 1


def test_retrain_success_registers_and_saves_model(paths, notify_calls, monkeypatch) -> None:
    import lightgbm as lgb

    from trading.jobs import weekly_train as wt
    from trading.store.model_registry import all_rows
    from trading.strategy.ranker_features import FEATURE_NAMES
    from trading.strategy.ranker_io import TrainingInputs
    from trading.strategy.ranker_train import TrainResult

    monkeypatch.setattr(
        wt,
        "load_training_inputs",
        lambda p, c: TrainingInputs(
            enriched={"RVNL": pd.DataFrame({"close": [1.0]})},
            macro_history=pd.DataFrame(),
            sentiment_lookup={},
            negative_news_lookup={},
        ),
    )
    stub = TrainResult(
        folds=(),
        final_model=lgb.LGBMClassifier(),
        final_train_start=pd.Timestamp("2023-06-14"),
        final_train_end=pd.Timestamp("2026-06-14"),
        n_final_examples=42,
        oos_sharpe_mean=float("nan"),
        oos_hit_rate_mean=float("nan"),
        feature_names=FEATURE_NAMES,
    )
    monkeypatch.setattr(wt, "train_walkforward", lambda **_kw: stub)

    result = wt.run_weekly_train(AS_OF, paths=paths)
    assert result.retrain_ran is True
    assert result.examples == 42
    assert result.promoted is False  # NaN sharpe never promotes
    assert result.model_path == "models/ranker_2026-06-14.pkl"
    assert (paths.project_root / "models" / "ranker_2026-06-14.pkl").is_file()
    rows = all_rows(paths)
    assert len(rows) == 1
    assert rows[0].train_end == "2026-06-14"
    assert rows[0].notes == "weekly_train"
    text = result.review_path.read_text(encoding="utf-8")
    assert "Trained: yes — 42 examples" in text
