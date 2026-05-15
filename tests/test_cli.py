"""Tests for trading.cli — typer CLI smoke + ingest-history + kite-login."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from typer.testing import CliRunner

from trading.cli import app

runner = CliRunner()


def _fake_ohlcv(rows: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=rows, freq="B")
    idx.name = "date"
    return pd.DataFrame(
        {
            "open": [100.0 + i for i in range(rows)],
            "high": [105.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [104.0 + i for i in range(rows)],
            "volume": [1_000_000 + i for i in range(rows)],
        },
        index=idx,
    )


def test_app_help_runs() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    flat = result.stdout.replace("\n", " ")
    assert "ingest-history" in flat
    assert "kite-emergency-login" in flat
    assert "scan" in flat


def test_ingest_history_writes_parquet(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    fake = _fake_ohlcv()
    with patch("trading.cli.fetch_ohlcv", return_value=fake):
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--symbols",
                "NTPC",
                "--start",
                "2025-01-01",
                "--end",
                "2025-01-10",
            ],
        )
    assert result.exit_code == 0, result.stdout
    assert (tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet").is_file()
    assert (tmp_path / "data" / "parquet" / "nifty200" / "NTPC.parquet").is_file()


def test_ingest_history_continues_on_per_symbol_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))

    from trading.data.yfinance import OhlcvFetchError

    def fake_fetch(symbol: str, start, end, **kwargs):
        if symbol == "BAD":
            raise OhlcvFetchError(f"No data for {symbol}")
        return _fake_ohlcv()

    with patch("trading.cli.fetch_ohlcv", side_effect=fake_fetch):
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--symbols",
                "BAD",
                "--symbols",
                "NTPC",
                "--start",
                "2025-01-01",
            ],
        )
    assert result.exit_code == 0
    # Good ones should have been written
    assert (tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet").is_file()
    assert (tmp_path / "data" / "parquet" / "nifty200" / "NTPC.parquet").is_file()
    # Bad one should not
    assert not (tmp_path / "data" / "parquet" / "nifty200" / "BAD.parquet").exists()


def test_ingest_history_skip_existing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    existing = tmp_path / "data" / "parquet" / "nifty200" / "RVNL.parquet"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"placeholder")  # pretend it exists

    with patch("trading.cli.fetch_ohlcv") as mock_fetch:
        result = runner.invoke(
            app,
            [
                "ingest-history",
                "--symbols",
                "RVNL",
                "--start",
                "2025-01-01",
                "--skip-existing",
            ],
        )
    assert result.exit_code == 0
    mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# kite-login
# ---------------------------------------------------------------------------


def _settings_with_kite_creds(**overrides):
    """Build a fake Settings populated with Kite creds; overrides win."""
    from trading.config import Settings

    defaults = {
        "anthropic_api_key": None,
        "kite_api_key": "test-api-key",
        "kite_api_secret": "test-api-secret",
        "kite_access_token": None,
        "log_level": "INFO",
        "news_user_agent": "test/0",
    }
    defaults.update(overrides)
    return Settings(**defaults)


def test_kite_login_missing_creds_exits_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    bad_settings = _settings_with_kite_creds(kite_api_key=None, kite_api_secret=None)
    with patch("trading.cli.get_settings", return_value=bad_settings):
        result = runner.invoke(app, ["kite-emergency-login", "--request-token", "rt"])
    assert result.exit_code == 1
    assert "KITE_API_KEY" in result.stdout


def test_kite_login_writes_token_to_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    fake_client = MagicMock()
    fake_client.profile.return_value = {"user_name": "Sandeep"}
    with (
        patch("trading.cli.get_settings", return_value=_settings_with_kite_creds()),
        patch("trading.cli.make_client", return_value=fake_client),
        patch("trading.cli.login_url", return_value="https://kite/login?x=1"),
        patch("trading.cli.generate_session", return_value="fresh-token-123"),
    ):
        result = runner.invoke(app, ["kite-emergency-login", "--request-token", "rt-456"])
    assert result.exit_code == 0, result.stdout
    env_path = tmp_path / ".env"
    assert env_path.is_file()
    assert "KITE_ACCESS_TOKEN=fresh-token-123" in env_path.read_text(encoding="utf-8")
    assert "Sandeep" in result.stdout


def test_kite_login_auth_error_exits_1(tmp_path: Path, monkeypatch) -> None:
    from trading.data.kite import KiteAuthError

    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    with (
        patch("trading.cli.get_settings", return_value=_settings_with_kite_creds()),
        patch("trading.cli.make_client", return_value=MagicMock()),
        patch("trading.cli.login_url", return_value="https://kite/login?x=1"),
        patch("trading.cli.generate_session", side_effect=KiteAuthError("bad request_token")),
    ):
        result = runner.invoke(app, ["kite-emergency-login", "--request-token", "bogus"])
    assert result.exit_code == 1
    assert "bad request_token" in result.stdout


# ---------------------------------------------------------------------------
# scan
# ---------------------------------------------------------------------------


def _seed_parquet(tmp_path: Path, symbol: str = "TEST", n: int = 250) -> None:
    """Write a 250-bar parquet for a single test symbol."""
    from trading.config import get_paths
    from trading.store.ohlcv import write_ohlcv

    paths = get_paths(root=tmp_path)
    df = pd.DataFrame(
        {
            "open": [100.0 + 0.1 * i for i in range(n)],
            "high": [101.0 + 0.1 * i for i in range(n)],
            "low": [99.0 + 0.1 * i for i in range(n)],
            "close": [100.0 + 0.1 * i for i in range(n)],
            "volume": [2_000_000] * n,
        },
        index=pd.date_range("2024-01-01", periods=n, freq="B", name="date"),
    )
    write_ohlcv(df, symbol, paths)


def test_scan_runs_on_fixture(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed_parquet(tmp_path)
    result = runner.invoke(app, ["scan", "--date", "2024-12-15", "--show-all"])
    assert result.exit_code == 0, result.stdout
    assert "TEST" in result.stdout


def test_scan_json_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _seed_parquet(tmp_path, symbol="FOO")
    result = runner.invoke(app, ["scan", "--date", "2024-12-15", "--show-all", "--json"])
    assert result.exit_code == 0
    import json as _json

    payload = _json.loads(result.stdout)
    assert isinstance(payload, list)
    assert len(payload) == 1
    assert payload[0]["symbol"] == "FOO"
    assert "rules" in payload[0]


def test_scan_no_parquet_exits_1(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    result = runner.invoke(app, ["scan"])
    assert result.exit_code == 1
    assert "ingest-history" in result.stdout


# ---------------------------------------------------------------------------
# Phase 12 — brief assemble-context / compile
# ---------------------------------------------------------------------------


def _init_db(tmp_path: Path) -> Path:
    from trading.store.db import get_conn as _get_conn
    from trading.store.migrations import run_migrations as _run_migrations
    db_path = tmp_path / "data" / "app.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _get_conn(db_path) as conn:
        _run_migrations(conn)
    return db_path


def test_brief_assemble_context_writes_bundle(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app,
        ["brief", "assemble-context", "--date", "2026-05-15", "--mode", "pre_open"],
    )
    assert result.exit_code == 0, result.stdout
    out = tmp_path / "data" / "research" / "2026-05-15" / "_context.md"
    assert out.is_file()
    assert "now run /analyst" in result.stdout


def test_brief_compile_assembles_brief(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    date_dir = tmp_path / "data" / "research" / "2026-05-15"
    date_dir.mkdir(parents=True)
    (date_dir / "_context.md").write_text(
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n_(no data)_\n",
        encoding="utf-8",
    )
    (date_dir / "macro_brief.md").write_text("x\n", encoding="utf-8")
    (date_dir / "sector_commentary.md").write_text("x\n", encoding="utf-8")
    result = runner.invoke(
        app, ["brief", "compile", "--date", "2026-05-15"]
    )
    assert result.exit_code == 0, result.stdout
    assert (date_dir / "brief.md").is_file()
    assert "brief.md" in result.stdout


def test_pre_open_cli_writes_bundle_and_prints_next_step(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    from trading.jobs import pre_open as po
    monkeypatch.setattr(po, "_step_macro",
                        lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr(po, "_step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr(po, "_step_scan", lambda p, d, w: [])
    monkeypatch.setattr(po, "_step_portfolio",
                        lambda p, s, w, *, as_of: [])
    monkeypatch.setattr(po, "_step_auto_open",
                        lambda *a, **kw: 0)
    result = runner.invoke(
        app,
        ["pre-open", "--date", "2026-05-15", "--skip-news"],
    )
    assert result.exit_code == 0, result.stdout
    out_path = tmp_path / "data" / "research" / "2026-05-15" / "_context.md"
    assert out_path.is_file()
    assert "/analyst" in result.stdout
    assert "trading brief compile" in result.stdout


def test_pre_open_cli_aborts_when_kite_snapshot_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    from trading.jobs import pre_open as po
    monkeypatch.setattr(po, "_step_macro",
                        lambda c, d, w: (False, "NEUTRAL"))
    monkeypatch.setattr(po, "_step_news", lambda c, d, w: (0, 0))
    monkeypatch.setattr(po, "_step_scan", lambda p, d, w: [])
    monkeypatch.setattr(po, "_step_auto_open", lambda *a, **kw: 0)
    # NO seed_kite_snapshot — _step_portfolio will raise PreOpenAborted
    result = runner.invoke(
        app,
        ["pre-open", "--date", "2026-05-15", "--skip-news"],
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-snapshot" in result.stdout


def test_portfolio_cli_reads_snapshot(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    from datetime import date as _d

    from tests.conftest import seed_kite_snapshot
    from trading.config import get_paths as _gp
    seed_kite_snapshot(
        _gp(),
        _d(2026, 5, 15),
        holdings=[],
        gtts=[],
    )
    result = runner.invoke(
        app, ["portfolio", "--date", "2026-05-15"]
    )
    assert result.exit_code == 0, result.stdout
    assert "0 holding" in result.stdout or "Loaded 0" in result.stdout


def test_portfolio_cli_aborts_when_snapshot_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["portfolio", "--date", "2026-05-15"]
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-snapshot" in result.stdout


def test_kite_emergency_login_present_in_help() -> None:
    result = runner.invoke(app, ["--help"])
    flat = result.stdout.replace("\n", " ")
    assert "kite-emergency-login" in flat
    assert "kite-login " not in flat  # bare name renamed away


def test_kite_emergency_snapshot_writes_files(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("KITE_API_KEY", "fake")
    monkeypatch.setenv("KITE_ACCESS_TOKEN", "fake")

    from trading.data.kite import GttOrder, Holding
    fake_holding = Holding(
        tradingsymbol="RVNL", exchange="NSE", isin="INE415G01027",
        quantity=32, average_price=305.0, last_price=329.6,
        close_price=327.1, pnl=787.2, day_change=2.5,
        day_change_percentage=0.76,
    )
    fake_gtt = GttOrder(
        id=1, type="single", status="active", tradingsymbol="RVNL",
        exchange="NSE", trigger_values=[350.0], last_price=329.6,
        created_at="2026-05-10T10:00:00",
        orders=[{"transaction_type": "SELL", "quantity": 32, "price": 350.0}],
    )
    with patch("trading.cli.make_client", return_value=MagicMock()), \
         patch("trading.cli.get_holdings", return_value=[fake_holding]), \
         patch("trading.cli.get_gtts", return_value=[fake_gtt]):
        result = runner.invoke(
            app, ["kite-emergency-snapshot", "--date", "2026-05-15"]
        )
    assert result.exit_code == 0, result.stdout
    base = tmp_path / "data" / "raw" / "2026-05-15"
    assert (base / "holdings.json").is_file()
    assert (base / "gtts.json").is_file()
    assert (base / "_meta.json").is_file()
    import json as _j
    meta = _j.loads((base / "_meta.json").read_text(encoding="utf-8"))
    assert meta["source"] == "sdk-fallback"


def test_mid_day_cli_prepare_writes_symbol_file(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16"]
    )
    assert result.exit_code == 0, result.stdout
    out_path = tmp_path / "data" / "raw" / "2026-05-16" / "_quote_symbols.txt"
    assert out_path.is_file()
    assert "/kite-quotes-snapshot" in result.stdout


def test_mid_day_cli_apply_aborts_when_quotes_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 2, result.stdout
    assert "/kite-quotes-snapshot" in result.stdout


def test_mid_day_cli_apply_happy_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Stub run_mid_day to avoid full mtm setup; verify exit-code + summary line."""
    from datetime import date as _d
    from datetime import datetime as _dt
    monkeypatch.setenv("TRADING_PROJECT_ROOT", str(tmp_path))
    _init_db(tmp_path)

    fake_update = tmp_path / "data" / "research" / "2026-05-16" / "mid_day_update.md"
    fake_update.parent.mkdir(parents=True, exist_ok=True)
    fake_update.write_text("stub", encoding="utf-8")

    from trading.jobs import mid_day as mid_day_mod
    fake_result = mid_day_mod.MidDayResult(
        as_of=_d(2026, 5, 16),
        quotes_capture_ts=_dt(2026, 5, 16, 12, 32),
        bars_built=3, trades_evaluated=2, trades_closed=1,
        trades_held=1, trades_skipped=0,
        update_path=fake_update, symbols_path=None, warnings=[],
    )
    monkeypatch.setattr(
        "trading.cli.run_mid_day", lambda *a, **kw: fake_result
    )
    result = runner.invoke(
        app, ["mid-day", "--date", "2026-05-16", "--apply"]
    )
    assert result.exit_code == 0, result.stdout
    assert "evaluated" in result.stdout
    assert "mid_day_update.md" in result.stdout
