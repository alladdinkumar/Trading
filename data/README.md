# Trading Data Directory

## Structure

```
data/
├── raw/                          # Snapshots pulled directly from Kite MCP
│   └── YYYY-MM-DD/
│       ├── profile.md            # Zerodha account profile
│       ├── holdings.md           # All equity holdings with P&L
│       └── gtt_orders.md         # All active GTT orders
│
└── research/                     # Analysis compiled per session
    └── YYYY-MM-DD/
        ├── market_context.md     # Macro/sector backdrop for the date
        ├── portfolio_analysis.md # Full portfolio P&L, GTT viability, projections
        └── stocks/
            ├── SYMBOL.md         # Per-stock deep dive
            └── ...
```

## Sessions

| Date | Raw Data | Research | Notes |
|------|----------|----------|-------|
| [2026-05-09](raw/2026-05-09/) | [Holdings](raw/2026-05-09/holdings.md) · [GTTs](raw/2026-05-09/gtt_orders.md) · [Profile](raw/2026-05-09/profile.md) | [Market](research/2026-05-09/market_context.md) · [Portfolio](research/2026-05-09/portfolio_analysis.md) · [Stocks](research/2026-05-09/stocks/) | Initial analysis; GTT quantity bug found; RVNL has no GTT |

## Portfolio at a Glance (2026-05-09)
- **Total Invested**: ~₹5,62,806
- **Current Value**: ~₹5,79,485
- **Unrealized P&L**: +₹16,682 (+2.97%)
- **Stocks**: COALINDIA, IDFCFIRSTB, IRB, IREDA, JIOFIN, MAZDOCK, NTPC, PFC, RECLTD, RVNL, TATAPOWER

## Known Issues (as of 2026-05-09)
1. All GTTs have `quantity = 1` — need to update to actual holding sizes
2. Exchange mismatches: IDFCFIRSTB and JIOFIN GTTs on BSE, holdings on NSE
3. RVNL has no GTT set (largest losing position, -₹13,784)
