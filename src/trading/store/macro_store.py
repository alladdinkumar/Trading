"""Persistence helpers for `macro_snapshot` (one row per trading day)."""

from __future__ import annotations

import sqlite3

from trading.data.macro import MacroSnapshot


def upsert_macro_snapshot(conn: sqlite3.Connection, row: MacroSnapshot) -> None:
    """Insert-or-replace one daily snapshot. `date` is the primary key."""
    conn.execute(
        """
        INSERT INTO macro_snapshot (
          date, sgx_nifty, dow_fut, nasdaq_fut, sp500, usdinr, crude,
          vix, us_10y, fii_flow_cr, dii_flow_cr, regime
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
          sgx_nifty   = excluded.sgx_nifty,
          dow_fut     = excluded.dow_fut,
          nasdaq_fut  = excluded.nasdaq_fut,
          sp500       = excluded.sp500,
          usdinr      = excluded.usdinr,
          crude       = excluded.crude,
          vix         = excluded.vix,
          us_10y      = excluded.us_10y,
          fii_flow_cr = excluded.fii_flow_cr,
          dii_flow_cr = excluded.dii_flow_cr,
          regime      = excluded.regime
        """,
        (
            row.date,
            row.sgx_nifty,
            row.dow_fut,
            row.nasdaq_fut,
            row.sp500,
            row.usdinr,
            row.crude,
            row.vix,
            row.us_10y,
            row.fii_flow_cr,
            row.dii_flow_cr,
            row.regime,
        ),
    )


def get_macro_snapshot(conn: sqlite3.Connection, date_iso: str) -> MacroSnapshot | None:
    row = conn.execute(
        "SELECT * FROM macro_snapshot WHERE date = ?", (date_iso,)
    ).fetchone()
    if row is None:
        return None
    return MacroSnapshot(
        date=row["date"],
        sgx_nifty=row["sgx_nifty"],
        dow_fut=row["dow_fut"],
        nasdaq_fut=row["nasdaq_fut"],
        sp500=row["sp500"],
        usdinr=row["usdinr"],
        crude=row["crude"],
        vix=row["vix"],
        us_10y=row["us_10y"],
        fii_flow_cr=row["fii_flow_cr"],
        dii_flow_cr=row["dii_flow_cr"],
        regime=row["regime"],
    )
