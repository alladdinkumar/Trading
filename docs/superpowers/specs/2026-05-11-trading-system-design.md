# AI-Assisted Trading & Portfolio Intelligence System — Design Spec

**Author:** Sandeep Kumar
**Date:** 2026-05-11
**Status:** Approved (brainstorming phase)
**Successor doc:** Implementation plan (writing-plans skill, next)

---

## 1. Executive Summary

A single-user, local Python application that runs three scheduled jobs per trading day to (a) scan Nifty 200 for swing-trade dip-buy opportunities, (b) health-check the user's existing Zerodha holdings, (c) recommend a monthly ₹1L SIP allocation, and (d) log every signal to an internal paper-trade ledger that is mark-to-market against live Kite quotes. All trades are paper-only in v1; real-money execution is explicitly out of scope until 3-6 months of out-of-sample signal accuracy has been validated.

Strategy is a three-layer funnel: deterministic rules generate candidates → a LightGBM ranker scores them → Claude (LLM) writes the narrative for the top 3-5. Sentiment (per-stock, sector, macro) plugs into all three layers as hard filters, ML features, and narrative context. Kite-native indicators (order book depth, OI, circuits, F&O ban, GTT events, margins) feed the same layers.

Tech stack is intentionally lean: Python 3.11, uv, SQLite + Parquet + Markdown, Streamlit dashboard, Windows Task Scheduler, Claude API for LLM. No Kafka, Airflow, K8s, Docker, MLflow, or other production-grade infra — single laptop, single user.

Realistic target: **Nifty 200 + 3-6% CAGR with shallower drawdowns than buy-and-hold.**

---

## 2. Goals & Non-Goals

### Goals

1. **Portfolio health check** on 11 current holdings — fundamental + technical scoring, GTT-target reality check via Monte Carlo
2. **Daily dip-scanner** across Nifty 200 + holdings → ranked swing-trade watchlist (5-30 day horizon)
3. **Monthly SIP allocator** for ₹1L deployment — split between topping up existing holdings vs. new entries vs. cash reserve
4. **Backtest engine** to validate any rule/strategy with realistic costs and walk-forward validation before it earns a place in production
5. **Paper-trade ledger** auto-logs every signal, marks-to-market via Kite quotes, computes prediction accuracy
6. **Continuous learning** — weekly retrain of a LightGBM ranker on the paper-trade ledger; Claude writes daily narratives

### Non-Goals (explicit out-of-scope)

- ❌ Real-money order placement in v1 (paper-trades only until proven)
- ❌ Intraday / HFT
- ❌ Multi-user, web hosting, SaaS, cloud infra
- ❌ Deep learning (LSTM/Transformer), GPUs
- ❌ Kafka / Airflow / K8s / Docker / MLflow / vector DBs / PostgreSQL
- ❌ Beating institutional alpha — we target consistency, not max return

---

## 3. High-Level Architecture

```
   ┌───────────────────────────────────────────────────────────┐
   │  External Sources                                         │
   │  Kite MCP / Connect · yfinance · NSE files · news RSS     │
   │  Anthropic API (Claude)                                   │
   └────────────────────────────┬──────────────────────────────┘
                                ▼
   ┌──────────────────┐    ┌─────────────────────────────────┐
   │  Data Layer      │───▶│  Local Storage                  │
   │  fetch + cache   │    │  SQLite (signals, trades, etc.) │
   └──────────────────┘    │  Parquet (OHLCV history)         │
                           │  Markdown (data/research/…)      │
                           └────────────┬────────────────────┘
                                        ▼
                           ┌──────────────────────────────────┐
                           │  Feature Engineering             │
                           │  technicals · regime · sentiment │
                           └────────────┬────────────────────┘
                                        ▼
              ┌─────────────────────────┴─────────────────────┐
              ▼                         ▼                     ▼
     ┌─────────────────┐   ┌─────────────────────┐  ┌─────────────────┐
     │ Strategy Layer  │   │ Backtest Engine     │  │ Portfolio Layer │
     │ rules+LightGBM  │   │ vectorbt walk-fwd   │  │ holdings + GTT  │
     └────────┬────────┘   └─────────────────────┘  └────────┬────────┘
              ▼                                              │
     ┌─────────────────┐                                     │
     │ LLM Analyst     │  ◀── Anthropic API (Claude)         │
     │ (narrative why) │                                     │
     └────────┬────────┘                                     │
              ▼                                              ▼
     ┌──────────────────────────────────────────────────────────┐
     │  Outputs                                                 │
     │  • Streamlit dashboard (Portfolio / Signals / Backtest)  │
     │  • Markdown briefings under data/research/YYYY-MM-DD/    │
     │  • SQLite paper-trade ledger                             │
     └──────────────────────────────────────────────────────────┘
```

Each layer is independently testable with cached fixture data. Entire stack runs on one laptop.

---

## 4. Strategy: Three-Layer Signal Pipeline

### 4.1 Layer A — Rules (deterministic candidate generator)

Runs daily over Nifty 200 + holdings. A stock surfaces only when ALL filters pass:

| Check | Threshold | Rationale |
|---|---|---|
| Price > 200-DMA AND 50-DMA > 200-DMA | strict | Only buy dips in uptrends — avoid falling knives |
| Price within 3% of 20-DMA or 50-DMA | 3% band | A real pullback, not a parabolic name |
| RSI(14) between 30 and 45 | inclusive | Oversold but not crashing |
| Selling-day volume < 20-day avg volume | strict | Selling exhaustion, not capitulation |
| 20-day avg turnover (close × volume) > ₹10 cr/day | strict | Liquidity floor |
| No earnings in next 3 trading days | strict | Avoid event-risk traps |
| India VIX < 25 AND Nifty 200 not in 5%+ 5-day drawdown | strict | Regime filter |
| Stock not down > 15% in last 30 days | strict | Avoid genuine breakdowns |
| Not in NSE F&O ban list | strict | Regulatory + liquidity risk |
| Not in T2T (trade-to-trade) segment | strict | No intraday liquidity |
| No upper-circuit hit today | strict | No exit liquidity tomorrow |
| Pre-open gap (IEP vs prev close) not > +2% | 2% cap | Avoid chase risk (applied in 08:55 IEP sub-job, after main scanner) |
| No CRITICAL sentiment events in last 30d | strict | SEBI/SAT/fraud/auditor veto |

### 4.2 Layer B — LightGBM Ranker

A gradient-boosted ranker trained on backtest labels (`did this setup hit +10% before stop within 30 days?`).

**Features** (~25-30):
- Setup metrics: RSI(14), distance to 20/50-DMA, ATR%, days since signal-rule met
- Trend: 20/50/200-DMA slopes, ADX(14), distance from 52w high/low
- Volume: volume vs 20d avg, on-balance-volume slope
- Sector: sector relative strength (5d, 20d), sector regime tag
- Macro: India VIX level + 5d change, FII flow 5d sum, USDINR change, risk regime tag
- Sentiment: 7d and 30d sentiment scores, news_volume, negative_news_count, has_critical_in_30d
- F&O: PCR (index), OI buildup type, bid-ask spread bps
- Behavioral: bulk_deal_buy_30d, bulk_deal_sell_30d, insider_buy_net_90d

**Output:** probability score for ranking. Top 3-5 surface to watchlist.
**Training:** walk-forward, retrained quarterly. Initial training requires Phase 7 backtest to label data.

### 4.3 Layer C — Claude Analyst (LLM)

For the top 3-5 candidates each day, Claude reads:
- Recent news headlines (last 7 days)
- Latest quarterly result commentary
- Sector & macro context
- Known event risks inside the trade horizon (earnings, ex-div, RBI, FOMC)

Outputs (per stock):
- Bullish case (3-4 sentences)
- Bearish case / risks (3-4 sentences)
- Conviction: HIGH / MEDIUM / LOW
- Event-risk flags in the holding window

Also writes:
- Macro paragraph opening the daily brief
- Post-close recap with prediction-error analysis
- 3-line sector commentary

Model choice: **Sonnet 4.6** for narrative, **Haiku 4.5** for high-volume classification tasks. Prompt caching enabled to keep cost predictable (~₹5-15/day).

### 4.4 Entry / Exit / Sizing Rules

- **Entry:** limit order at close OR ±1% buy-zone (configurable per signal)
- **Stop:** `min(1.5 × ATR(14), prev swing low)` below entry, floored at 4% to avoid noise
- **Target:** +20% OR 1:2.5 reward/risk — whichever comes first
- **Time stop:** exit at flat/small loss after 25 trading days if not in profit
- **Trailing:** at +10% move stop to breakeven; at +15% trail by 1×ATR
- **Position size:** `floor((₹100,000 × 0.02) / (entry − stop)) × entry`
  → max loss per trade ≤ 2% of monthly capital (₹2,000)
- **Concurrency caps:**
  - Max 5 open paper-positions
  - ≤25% capital in any one stock
  - ≤30% capital in any one sector

### 4.5 Risk Overrides (Kill-Switches)

| Trigger | Action |
|---|---|
| India VIX > 25 OR Nifty 200 -2% in a day | Halve new position sizes |
| Paper-trade 30-day drawdown > 8% | Freeze new entries, auto post-mortem |
| Earnings inside trade horizon | Auto-halve size for that trade |
| Single-day Nifty -3% (black swan) | All-cash for 5 trading days |
| Margin shortfall on Kite account | Freeze new entries, alert |
| Stock in LAGGING sector (sector regime) | Auto-size at 0.75× |
| Risk regime = RISK_OFF | Position-size multiplier 0.5× |
| Risk regime = NEUTRAL | Position-size multiplier 0.75× |

---

## 5. Sentiment & News Integration

Three streams, each plugged in at the right layer.

### 5.1 Per-stock sentiment

**Sources:** NSE/BSE corporate announcements, Moneycontrol/ET/BS RSS filtered by ticker, result transcripts (Trendlyne/BSE filings), block & bulk deals (NSE), Screener fundamentals deltas.

**Processing:**
- Each headline → **FinBERT** (local, ~50MB, free) → score [-1, +1] + category tag
- Categories: results, management, regulatory, M&A, downgrade, dividend, pledge
- Critical keywords (SEBI/SAT case, auditor resignation, fraud, large pledge change, qualified opinion) → flagged as **CRITICAL** → hard veto
- Aggregated daily into `sentiment_daily` (7d & 30d scores)

**Plugs in at:**
- Layer A: critical events → hard veto
- Layer B: `sentiment_7d`, `sentiment_30d`, `news_volume`, `negative_news_count` features
- Layer C: top 10 headlines per surfaced candidate → narrative input

### 5.2 Sector sentiment & rotation

**Sources:** NSE sectoral indices (NIFTY BANK/IT/AUTO/PHARMA/FMCG/ENERGY/METAL/REALTY/MEDIA/PSU BANK + others), sector-specific commodity/rate cues.

**Processing:**
- Daily sector relative strength vs Nifty 200 (5d, 20d, 60d)
- Sector regime tag: LEADING / NEUTRAL / LAGGING
- LLM writes 3-line sector commentary

**Plugs in at:**
- Layer A: stock in bottom-quartile sector AND down >10% in 20d → filtered out
- Layer B: `sector_rs_5d`, `sector_rs_20d`, `sector_regime` features
- Position-sizing: LAGGING sectors auto-sized at 0.75×

### 5.3 Macro / global sentiment

**Sources (all free):** GIFT Nifty / SGX Nifty futures, US closes (S&P, Nasdaq, Dow via yfinance), USDINR (INR=X), Brent crude (BZ=F), US 10y yield (^TNX), India VIX, NSE daily FII/DII flows, TradingEconomics India calendar, RBI/Fed event dates.

**Processing:**
- 08:30 IST pre-open snapshot
- Composite Risk Regime score → `RISK_ON / NEUTRAL / RISK_OFF`
- Weighted from: VIX direction, global futures, FII flow, USDINR move

**Plugs in at:**
- Kill-switches (see 4.5)
- Layer B: `risk_regime`, `vix`, `vix_change_5d`, `fii_flow_5d_sum`, `usdinr_change` features
- Position-size multiplier (RISK_ON 1.0× / NEUTRAL 0.75× / RISK_OFF 0.5×)
- Top of every morning briefing markdown

### 5.4 Cost

FinBERT runs locally — headline classification is free. Claude Haiku used for category tagging where needed; Claude Sonnet only for the daily narrative on top 3-5 candidates + macro paragraph. Total LLM API spend target: **₹5-15/day**.

---

## 6. Kite-Sourced Indicators & Events

Everything below comes directly from Kite Connect / Kite MCP.

### 6.1 Market microstructure (intraday)

Polled via `get_quotes` for watchlist + holdings every 5-15 min during market hours.

| Indicator | Source | Use |
|---|---|---|
| 5-level order book depth | `get_quotes.depth` | Liquidity check before paper entry; bid/ask spread feature |
| Volume vs avg | `get_quotes` + history | Volume spike >2× by 11 AM → unusual interest flag |
| Upper/lower circuit prices | `get_quotes` | Hard veto on upper-circuit hit; flag if within 2% of lower |
| Open Interest (F&O underlyings) | `get_quotes` derivatives | Long/short buildup classifier → feature + LLM narrative |
| 52w high/low proximity | OHLC + history | "Near 52w low + uptrend" sweet spot |
| Last trade time staleness | `last_trade_time` | Catch illiquid prints → auto-exclude |

### 6.2 Pre-open auction (08:55-09:08 IST)

- **Indicative Equilibrium Price (IEP)** vs prev close → gap detection
  - Gap up >2% on watchlist → drop from today's buy list
  - Gap down >2% on existing position → trigger overnight-analysis re-run
- Pre-open volume vs avg → unusual pre-market interest flag

### 6.3 Options-derived sentiment (where applicable)

| Signal | Source | Use |
|---|---|---|
| Put-Call Ratio (PCR) | OI on near-month puts/calls | Index fear/greed feature; PCR > 1.3 = contrarian buy bias |
| Max Pain strike | Cumulative OI across strikes | Resistance/support magnet |
| OI buildup pattern | OI Δ + price Δ | Long/short buildup tag → feature |
| India VIX (`INDIA VIX`) | Kite symbol, real-time | Kill-switch input |

### 6.4 Your-account events

Polled via `get_holdings`, `get_positions`, `get_gtts`, `get_margins`, `get_orders`.

| Event | Source | Action |
|---|---|---|
| GTT triggered | `get_gtts` diff | Log to ledger; LLM mentions in briefing |
| Holding qty change | `get_holdings` diff | Detect dividends/bonuses; reconcile |
| Margin shortfall | `get_margins.available` | Hard alert; freeze new entries |
| Realized P&L delta | `get_holdings.day_change` | Daily snapshot |

### 6.5 NSE daily files

| File | Use |
|---|---|
| F&O ban list | Hard veto |
| Bulk deal report | LLM narrative; institutional accumulation signal |
| Block deal report | Same as above; flag in briefing |
| T2T segment list | Hard veto |
| Insider trading (SAST) disclosures | Sentiment feature + narrative |

### 6.6 Corporate actions (from Kite instrument master)

`search_instruments` + daily instrument dump:
- Ex-dividend dates → adjust backtests; flag in horizon
- Stock splits / bonuses → reconcile holding qty; adjust cost basis
- Rights issues → flag in briefing for HOLD-rated holdings

### 6.7 Polling cadence (respects Kite rate limits)

| Time | Action |
|---|---|
| 08:30 | Daily files: F&O ban, bulk/block, corp actions, NSE indices close |
| 08:55-09:08 | Pre-open quotes for watchlist + holdings (one snapshot) |
| 09:15 - 15:30 | Live `get_quotes` aligned with scheduled jobs (mid_day at 12:30) for ≤25 symbols — not continuous polling |
| 15:30 | Final close snapshot + account state |
| 16:00 | Post-close reconciliation + signal review |

Kite rate limit is 3 req/sec — at this cadence we use a tiny fraction.

---

## 7. Portfolio Strategy (Existing Holdings + Monthly SIP)

### 7.1 Per-holding health scoring

For each of the user's current holdings (RVNL, TATAPOWER, NTPC, IRB, PFC, RECLTD, COALINDIA, IDFCFIRSTB, IREDA, JIOFIN, MAZDOCK):

**Inputs:**
- Fundamentals: profit growth (YoY, 3y CAGR), debt/equity, valuation percentile (P/E vs 5y range), ROE
- Technicals: trend regime (above/below 200-DMA), drawdown from ATH, RSI(14), distance to 52w high
- Sentiment: 30d sentiment score, has_critical_in_30d

**Output:** **HOLD / TRIM / EXIT** recommendation with rationale.

### 7.2 GTT viability

For each active GTT order, project `P(target hit before GTT expiry)`:
- ATR-based Monte Carlo: 1,000 paths using realized 60-day volatility
- Includes drift from 60-day mean return
- Outputs probability + expected days-to-target
- LLM writes the verdict ("Your TATAPOWER GTT at ₹500 has ~22% chance of triggering before 30 Jun 2026 — consider lowering to ₹470")

### 7.3 Monthly ₹1L SIP Allocator

Runs on 1st of month. Splits ₹1L across:
- (a) **Topping up** HOLD-rated existing holdings that fire a fresh dip signal (up to 50% of monthly capital)
- (b) **New entries** from the scanner (up to 50% of monthly capital)
- (c) **Cash reserve** if quality signals are insufficient
- Never more than 60% deployed in one batch — staggered over 2-3 entries across the month
- Respects concurrency caps from 4.4

### 7.4 Portfolio concentration warning

User's current portfolio is **~70% PSU/infra** (RVNL, NTPC, TATAPOWER, IRB, PFC, RECLTD, COALINDIA, IREDA). System will warn before stacking more in those sectors and bias SIP allocation toward under-represented sectors when quality opportunities exist.

---

## 8. Backtest Framework

### 8.1 Engine

- **Library:** `vectorbt` (vectorized, walk-forward native)
- **Universe:** Historical Nifty 200 constituents from each rebalance date (survivorship-bias-free)
- **Period:** 3+ years of daily bars (longer where available)
- **Frequency:** End-of-day signals → next-day open entry (realistic for retail)

### 8.2 Cost model (`backtest/costs.py`)

Zerodha equity-delivery costs:
- Brokerage: ₹0 (Zerodha delivery is free as of design date — but assume `min(₹20, 0.03%)` for safety)
- STT: 0.1% on buy + 0.1% on sell
- Exchange transaction charge: 0.00297% on turnover
- SEBI charges: 0.0001% on turnover
- Stamp duty: 0.015% on buy side
- GST: 18% on (brokerage + transaction charges)
- **Slippage:** 0.1% per side (conservative for liquid Nifty 200)

Total round-trip cost ≈ 0.4% — folded into every backtest trade.

### 8.3 Walk-forward validation

- Train window: rolling 3 years
- Test window: 6 months out-of-sample
- Step: 3 months
- Retrain LightGBM ranker on each train window, evaluate on test window
- Aggregate out-of-sample metrics → these are the trust-worthy numbers

### 8.4 Metrics

| Metric | Definition |
|---|---|
| CAGR | Annualized return on equity curve |
| Sharpe | (Mean return − RF) / stdev, annualized |
| Sortino | (Mean return − RF) / downside-stdev, annualized |
| Max Drawdown | Largest peak-to-trough loss |
| Hit Rate | % of trades closed at profit |
| Profit Factor | Gross profit / gross loss |
| Avg R-multiple | Mean trade return / initial risk |
| Expectancy | (Hit rate × Avg win) − (Miss rate × Avg loss) |
| Beta vs Nifty 200 | Linear regression of strategy returns on Nifty 200 |
| Alpha (annualized) | Intercept × 252 |

### 8.5 Regression test

A frozen historical period (e.g. 2022-01 to 2024-12) on a frozen rule set must produce known metrics within tolerance (±5% on Sharpe). Catches accidental regressions in indicators, costs, or sizing math.

### 8.6 Anti-bias safeguards

- **No lookahead:** every backtest decision uses only data available at the bar's close
- **Survivorship:** historical Nifty 200 membership from monthly snapshots
- **Realistic fills:** entries at next-day open with slippage; stops triggered intra-day at the stop price (not at low — conservative)
- **No selection bias in ML training:** walk-forward only, no train/test contamination

---

## 9. Paper-Trading System

### 9.1 Ledger schema (SQLite)

```sql
signals (
  id, ts, symbol, side, entry, stop, target,
  horizon_days, rules_passed_json, ml_score, conviction,
  rationale, created_by  -- 'auto' or 'manual'
)

paper_trades (
  id, signal_id, ts_entry, entry_price, qty,
  ts_exit, exit_price, exit_reason,  -- TARGET / STOP / TIME / MANUAL
  pnl, pnl_pct, days_held
)

predictions (
  id, ts, symbol, predicted_return_pct,
  predicted_horizon_days, actual_return_at_horizon,
  error_pct, evaluated_at
)

portfolio_snapshots (
  date, cash, holdings_json, equity, drawdown_pct
)
```

### 9.2 Auto-logging

Every signal that passes Layers A + B is auto-logged to `signals` with `created_by='auto'`. If the auto-execute toggle is on (default in v1), a corresponding row in `paper_trades` is opened at the next-day open price fetched from Kite.

### 9.3 Mark-to-market

Daily during market hours (mid_day + post_close jobs):
1. Fetch live LTP via Kite `get_ltp` for all open paper positions
2. Apply exit logic (`strategy/exits.py`): STOP / TARGET / TIME / TRAILING
3. Close trades that hit any exit; write to `paper_trades`
4. Snapshot `portfolio_snapshots` end-of-day

### 9.4 Prediction accuracy

Each signal carries a predicted horizon. When that horizon matures (or the trade closes earlier), a row is written to `predictions` with the actual return and error. Weekly stats:
- Hit-rate by conviction tier
- Average error by sector
- Calibration plot (predicted vs realized)
- Used to retrain ranker quarterly

---

## 10. Daily Operational Schedule (all times IST)

```
08:30   pre_open.py
         ├─ Refresh Kite token (manual handshake if expired)
         ├─ Pull NSE daily files (F&O ban, bulk/block, corp actions)
         ├─ Snapshot macro (SGX/GIFT, US closes, USDINR, crude, VIX)
         ├─ Fetch overnight news → FinBERT → sentiment_daily
         ├─ Compute risk regime → macro_snapshot
         ├─ Scan Nifty 200: Layer A → Layer B → top 5
         ├─ Portfolio: HOLD/TRIM/EXIT + GTT viability
         ├─ Claude writes: macro brief + per-stock narratives + portfolio summary
         ├─ Auto-log paper-trades for fired signals
         └─ Write data/research/YYYY-MM-DD/{briefing,signals,portfolio}.md

08:55   pre_open_iep.py
         ├─ Snapshot pre-open IEP for watchlist
         └─ Drop gap-up >2% candidates; re-analyze gap-down >2% holdings

12:30   mid_day.py
         ├─ Live quotes (watchlist + open paper-positions + holdings)
         ├─ Mark-to-market open paper trades
         ├─ Apply exit logic (stop/target/trailing/time)
         ├─ Check kill-switches (VIX, Nifty intraday)
         ├─ Volume-spike scan on watchlist
         └─ Append mid-day section to today's briefing

16:00   post_close.py
         ├─ Final close snapshots (Kite quotes + account state)
         ├─ Close time-stopped paper trades at close
         ├─ Compute paper-portfolio daily P&L
         ├─ Evaluate matured predictions → update accuracy stats
         ├─ Reconcile real holdings vs yesterday (divs/splits/GTT triggers)
         ├─ Update sentiment_daily, sector_daily, oi_daily
         ├─ Claude writes: recap + errors + tomorrow's preview
         └─ Write data/research/YYYY-MM-DD/post_close.md

Sunday  weekly_train.py
         ├─ Retrain LightGBM on rolling 3-year window
         ├─ Walk-forward validate on last 6 months
         ├─ Promote new model if it beats current (models/registry.csv)
         └─ Generate weekly performance review markdown

1st-of-month  monthly_sip.py
         └─ Compute ₹1L SIP split (HOLD-topups + new entries + cash reserve)
```

---

## 11. Repository Structure

```
Trading/
├── .env                          # API keys — gitignored
├── .env.example
├── .gitignore  .python-version
├── CLAUDE.md  README.md
├── pyproject.toml  uv.lock
├── working-prompt.md             # session-bootstrap prompt
├── PROGRESS.md                   # granular task tracker
│
├── docs/superpowers/specs/
│   └── 2026-05-11-trading-system-design.md
│
├── src/trading/
│   ├── config.py
│   │
│   ├── data/
│   │   ├── kite.py        yfinance.py
│   │   ├── nse.py         news.py
│   │   ├── macro.py       cache.py
│   │
│   ├── store/
│   │   ├── db.py          migrations.py
│   │   ├── ohlcv.py       repo.py
│   │
│   ├── features/
│   │   ├── technicals.py  regime.py
│   │   ├── sentiment.py   builder.py
│   │
│   ├── strategy/
│   │   ├── rules.py       ranker.py
│   │   ├── analyst.py     sizing.py
│   │   └── exits.py
│   │
│   ├── backtest/
│   │   ├── engine.py      costs.py
│   │   ├── walkforward.py metrics.py
│   │
│   ├── paper/
│   │   ├── ledger.py      mtm.py
│   │   └── reconcile.py
│   │
│   ├── portfolio/
│   │   ├── health.py      gtt.py
│   │   └── allocator.py
│   │
│   ├── llm/
│   │   ├── client.py      prompts.py
│   │   └── briefing.py
│   │
│   ├── jobs/
│   │   ├── pre_open.py    mid_day.py
│   │   └── post_close.py
│   │
│   ├── ui/
│   │   ├── app.py
│   │   └── pages/
│   │       ├── 1_Portfolio.py     2_Today_Signals.py
│   │       ├── 3_Backtest.py      4_Paper_Journal.py
│   │
│   └── cli.py
│
├── scripts/                      # Windows Task Scheduler launchers
│   ├── pre_open.bat   mid_day.bat   post_close.bat
│
├── models/
│   ├── ranker_2026-05-11.pkl
│   └── registry.csv
│
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   ├── test_rules.py        test_sizing.py
│   ├── test_exits.py        test_backtest_costs.py
│   ├── test_paper_ledger.py test_portfolio_health.py
│
└── data/                         # existing — DO NOT change shape
    ├── app.db                    # SQLite (gitignored)
    ├── parquet/nifty200/         # OHLCV (gitignored)
    ├── cache/                    # HTTP cache (gitignored)
    ├── raw/YYYY-MM-DD/           # Kite snapshots (existing)
    └── research/YYYY-MM-DD/      # Daily briefings (now auto-generated)
```

---

## 12. Tech Stack

### Core
Python 3.11 · uv · ruff · mypy · pytest

### Data & numerics
`pandas` 2.x · `polars` · `pyarrow` · `numpy` · `pandas-ta`

### Sources
`kiteconnect` (Python SDK for scheduled jobs) · Kite MCP (for interactive Claude Code sessions) · `yfinance` · `nsepython` · `feedparser` · `requests` + `requests-cache`

### ML
`lightgbm` · `scikit-learn` · `joblib` · `transformers` + `torch` (FinBERT, CPU)

### Backtest
`vectorbt`

### LLM
`anthropic` (Sonnet 4.6 for narrative, Haiku 4.5 for classification)

### Dashboard
`streamlit` · `plotly`

### Utilities
`python-dotenv` · `typer` · `rich` · `loguru`

### Deliberately excluded
Kafka, Redis, Airflow, K8s, Docker, FastAPI, MLflow, PostgreSQL, TimescaleDB, vector DBs, TA-Lib (C lib), Backtrader, Zipline.

### Secrets

`.env` (gitignored), loaded via `python-dotenv`:

```
ANTHROPIC_API_KEY=sk-ant-...
KITE_API_KEY=...
KITE_API_SECRET=...
KITE_ACCESS_TOKEN=...    # rotates daily (Kite requires browser handshake)
LOG_LEVEL=INFO
```

---

## 13. Storage Layout

| Tier | What lives here | Why |
|---|---|---|
| SQLite (`data/app.db`) | signals, paper_trades, sentiment_daily, sector_daily, macro_snapshot, news_items, oi_daily, fno_ban_list, bulk_block_deals, corp_actions, account_events, preopen_snapshot, predictions, portfolio_snapshots, live_quotes, event_calendar | Relational, transactional, single-file, zero-ops. Streamlit reads directly. |
| Parquet (`data/parquet/`) | Per-symbol OHLCV (3+ years) | 5-10× faster than SQLite for backtest columnar scans. Tiny on disk. |
| Markdown (`data/research/YYYY-MM-DD/`) | Daily briefings, per-stock narratives, portfolio analysis, post-close recaps | Human-readable, greppable, matches existing convention. |

---

## 14. Build Phases (Step-by-Step)

Each phase ≈ 1-3 days of focused work. Order is dependency-driven. **Phase 13 is the MVP milestone — the first end-to-end "it works" moment.**

| # | Phase | Deliverable | Tests |
|---|---|---|---|
| 0 | **Project setup** | uv init, pyproject.toml, ruff/mypy/pytest config, empty module tree, .env.example | `pytest -q` & `ruff check` pass clean |
| 1 | **Config + SQLite schema** | `config.py`, `store/db.py`, `store/migrations.py` v1 — all tables | Round-trip insert/select per table |
| 2 | **Historical OHLCV (yfinance)** | `data/yfinance.py`, `store/ohlcv.py`, fetch 3y for Nifty 200 + holdings | Schema + fixture round-trip |
| 3 | **Kite MCP wrapper** | `data/kite.py` — holdings, GTTs, quotes, LTP, margins; daily token rotation | Mocked SDK + `@pytest.mark.live` integration |
| 4 | **Technical indicators** | `features/technicals.py` over pandas-ta — RSI/MACD/ATR/SMA/EMA/BB/ADX/VWAP | Known-input/known-output per indicator |
| 5 | **Rule scanner (Layer A)** | `strategy/rules.py` + CLI `trading scan --date …` | Synthetic data hits/misses each filter |
| 6 | **Sizing + exits** | `strategy/sizing.py`, `strategy/exits.py` (state machine: stop/target/trailing/time) | Scenarios per exit branch |
| 7 | **Backtest engine** | `backtest/costs.py`, `backtest/engine.py` (vectorbt), `backtest/metrics.py`; CLI `trading backtest --years 3` | Cost-model units + regression test (frozen period → known Sharpe ±5%) |
| 8 | **News + sentiment** | `data/news.py`, `features/sentiment.py` (FinBERT + critical-event veto) | Cached HTML fixtures + FinBERT snapshot |
| 9 | **Macro + regime** | `data/macro.py`, `features/regime.py` composite, daily snapshot | Regime classifier rules |
| 10 | **Portfolio analyzer** | `portfolio/health.py`, `portfolio/gtt.py` (Monte Carlo), `portfolio/allocator.py`; CLI `trading portfolio` | Synthetic Kite fixtures |
| 11 | **Paper-trade ledger** | `paper/ledger.py`, `paper/mtm.py`, `paper/reconcile.py` | Full-lifecycle in-memory SQLite |
| 12 | **LLM analyst** | `llm/client.py` (anthropic + retry + prompt cache), `llm/prompts.py`, `llm/briefing.py` | Mocked anthropic, snapshot prompts |
| 13 | **pre_open job (E2E)** ⭐ MVP | `jobs/pre_open.py` orchestrates 1-12; writes daily brief; auto-logs paper-trades; `scripts/pre_open.bat` | Integration test with cached fixtures |
| 14 | **mid_day + post_close jobs** | `jobs/mid_day.py`, `jobs/post_close.py`; `.bat` launchers | Integration with cached fixtures |
| 15 | **Streamlit dashboard** | `ui/app.py` + Portfolio / Today's Signals / Backtest / Paper Journal pages | `streamlit.testing.v1` smoke |
| 16 | **LightGBM ranker (Layer B)** | `strategy/ranker.py` — training, walk-forward, integration; first model checkpoint | Pipeline with synthetic labels + frozen-input ranker output |
| 17 | **Task Scheduler + logging** | Documented scheduler entries, `loguru` logs in `data/logs/`, Windows toast on errors | Manual verification |
| 18 | **Live paper-trading + iteration** | 3-6 month run; weekly performance review; quarterly retrain | Accuracy stats are the test |

### Definition of "done" per phase

A phase is done only when:
1. Code is implemented and lints clean (`ruff check`, `mypy`)
2. Tests for that phase pass
3. The deliverable is demonstrable via CLI command or passing integration test
4. `PROGRESS.md` is updated and a commit is made

---

## 15. Testing Strategy

| Layer | Test type | Tool |
|---|---|---|
| Indicators, sizing, exits, cost model | Pure unit tests — deterministic | pytest |
| Rules scanner, ranker, regime classifier | Synthetic DataFrames | pytest + pandas |
| Backtest engine | Regression test — frozen period must produce known metrics ±5% | pytest + cached parquet |
| Data fetchers (Kite, yfinance, NSE, news) | Mocked for shape/parsing; `@pytest.mark.live` for real fetches | pytest-mock |
| LLM prompts | Snapshot tests of assembled prompts (not LLM behavior) | pytest + syrupy |
| Jobs (pre_open, mid_day, post_close) | Integration with cached fixtures, no live calls | pytest + frozen fixtures |
| Streamlit | Smoke tests — pages render without error | `streamlit.testing.v1` |

**CI:** GitHub Actions on push — runs ruff + mypy + pytest (excluding `@live`). No live API calls in CI.
**Local live verification:** `pytest -m live` when you want to hit real Kite/news APIs.
**Coverage target:** 70%+ on `strategy/`, `backtest/`, `paper/`, `portfolio/`, `features/`. Data fetchers and UI exempt.

---

## 16. Compliance & Risk Notes (SEBI / Zerodha)

- **No automated real-money order placement in v1.** Paper-trades only.
- **Algo trading registration:** if real-money auto-execution is ever added, SEBI requires registered algo + broker approval (Zerodha specific). Not in scope for v1.
- **No leverage / no F&O in v1** — equity delivery only.
- **No short-selling** in v1 (intraday only on cash; we're swing trading).
- **Personal data only** — no third-party data resold or shared.
- **Tax tracking** — paper-trade ledger logs holding period to compute STCG (<12mo) vs LTCG (≥12mo) for future real-money reporting.

---

## 17. Realistic Expectations

This is a **positive-expectancy mean-reversion-in-uptrends** strategy — documented (Connors RSI2, Larry Williams). Realistic profile:

- Hit rate: 55-65%
- Average win > average loss (1:2.5 R/R floor)
- CAGR target: **Nifty 200 + 3-6%**
- Drawdown target: **shallower than buy-and-hold**

Anyone promising "20% intraday daily" is selling something. The deep-research doc's framing of that target was not honest about retail-vs-HFT realities, and this design deliberately steps back from it toward a sustainable swing approach.

---

## 18. Open Risks / What Could Still Go Wrong

| Risk | Mitigation |
|---|---|
| LightGBM overfits on small Indian-market sample (3 yrs × 200 stocks ≈ small) | Walk-forward only; conservative feature count; gate by out-of-sample Sharpe |
| Kite daily-token rotation breaks unattended jobs | Manual login at 08:25 IST or alert → user logs in via browser; document handshake; consider Kite Connect with persistent flow |
| FinBERT trained on English/US news may misread Indian context | Audit periodically; build a small Indian-finance keyword overlay for critical events |
| Survivorship bias in Nifty 200 reconstitution | Use historical NSE constituent lists per rebalance date |
| Backtest looks great, paper-trading underperforms | Expected — that's why paper trades for 3-6 months before any real-money mode |
| Streamlit dashboard becomes the trader's "casino UI" tempting overrides | Keep dashboard read-only; never expose a "place real order" button until Phase 18+ |
| News RSS sources go down or change format | Each source isolated behind an adapter; graceful degradation; FinBERT runs even if some sources fail |

---

## 19. Out of Spec / Future (post-MVP)

- Real-money auto-execution behind a feature flag (requires SEBI algo registration)
- Options-based hedging (e.g. long-put protection on existing holdings)
- Sector-rotation overlay strategy
- Alternative-data feeds (Twitter/X, Reddit — not free at scale, deferred)
- A small local LLM (e.g. Llama 3.1 8B) for sentiment classification to remove FinBERT
- Mobile push notifications (Pushover / Telegram bot)
- Multi-account support
