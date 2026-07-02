# Hypothesis catalog — where this system's bugs live

Distilled from every prior findings pass (F-001…F-066). These are the *classes*
of defect that keep recurring, phrased as starting hypotheses for the next
pass. They encode market/domain knowledge the tests don't: markets punish
optimistic accounting, and a paper-trading system's whole output is one number
whose honesty everything else serves.

Use 3–6 per domain agent, updated with whatever changed recently (`git log`).
A hypothesis is good when it names the file AND the mechanism, and would be
*falsified* by a specific line you can quote.

## 1. Backtest / ML validation (`backtest/`, `ranking/`)

- **Leakage at fold boundaries.** Any place `train_end == test_start` with no
  embargo ≥ the label horizon leaks the label window into training. Check the
  fold arithmetic against `forward_return.py`'s `max_days`. (Lineage: F-055.)
- **Label horizon vs. holding rules drift.** The label's forward window, the
  signal's `horizon_days`, and the exit engine's time stop must agree; they
  are defined in three files and have drifted before.
- **Metric convention splits.** Sharpe (daily √252 vs per-trade @12/yr vs
  fold-level), returns (gross vs net of costs), and capital basis (per-fold
  reset vs compounding) — grep every `sharpe(`/`periods_per_year` call site
  and demand one convention. (Lineage: F-061, F-066.)
- **Train mask off-by-inclusion.** `df.index < train_end` vs `<=` — one row of
  test data in train is invisible in aggregate metrics.
- **Promotion-gate arithmetic.** The floor/deadband/t-stat/persistence gate is
  only as sound as its inputs — check what population the t-stat runs over.

## 2. Strategy / decision math (`strategy/`)

- **Dead parameters.** Parameters declared and documented but never read again
  (grep the name after its assignment). The operator believes a lever exists.
  (Lineage: F-056.)
- **Calibration feeding on immature outcomes.** `matured_score_outcomes` must
  exclude open trades and respect the horizon; premature outcomes bias p_win.
- **Cap interactions.** Lot caps, sector caps, cash caps, and regime scaling
  compose — look for orderings where one cap silently disables another.
- **Stop/target asymmetry.** `stop = entry − k×ATR` recomputed at a new price
  must keep R-multiple semantics; check every place the 1.5×ATR constant is
  re-derived rather than imported.

## 3. Jobs / ops / lifecycle (`jobs/`, `ops/`)

- **Blocks invisible to run-status.** Every lifecycle block must be tracked by
  `run_status` AND have a reminder slot in `runner.py` — a block that opens
  trades but is untracked/unprompted is a silent no-trade day. (Lineage:
  F-062.)
- **Clock provenance.** Every time comparison should go through `clock.py`
  (IST). Grep `datetime.now()`, `date.today()` outside clock.py — a naive host
  clock breaks staleness checks the day the host isn't IST. (Lineage: F-004,
  F-058.)
- **Idempotency asymmetry.** Guards (`already_opened_today`, UPSERTs, partial
  unique indexes) must cover *every* insert path a re-run hits, including
  visibility/skip rows, not just the happy path. (Lineage: F-030.)
- **Degradation that's too graceful.** A soft step that catches its own error
  and continues is correct only if downstream consumers see the absence —
  check for warn-and-continue paths that leave a *stale* artifact in place a
  consumer will happily read.
- **Two-phase races.** prepare/apply pairs: what happens if apply runs twice,
  or prepare re-runs after apply, or the skill writes between them?

## 4. Data layer / contracts (`data/`, `data/static/`)

- **Silent price fallbacks.** Any `except`/`get(default)` that substitutes a
  price (yesterday's close, avg cost, 0.0) and feeds a persisted metric is a
  RISK finding even when documented. (Lineage: F-052.)
- **Static-file completeness vs the LIVE universe.** Every symbol in
  `nifty50.txt` must have a row in every static map (sector_map, fundamentals,
  aliases); index rebalances add names nobody maps. (Lineage: F-057.)
- **Staleness checks that lie.** Freshness = capture timestamp vs *IST now*,
  never host now; filename-encoded times (quotes_HHMM) parsed naive are a
  recurring source. (Lineage: F-058.)
- **Schema validation gaps.** `snapshot_schema` checks shape, not semantics —
  wrong-exchange or wrong-day-but-well-formed writes pass; check the readers'
  date/exchange cross-checks.

## 5. Paper accounting / UI (`paper/`, `ui/`)

- **P&L definition splits.** "Total P&L" must mean the same population
  (open ∪ closed) everywhere it's shown; open-only totals labeled "Total" have
  shipped before. (Lineage: F-059.)
- **Cash/equity derivation drift.** `compute_paper_cash` (debit/credit ledger)
  is the single source of truth; anything recomputing cash independently
  (UI reader, report renderer) will disagree eventually.
- **Cost symmetry.** Buy-side and sell-side charges both netted, in both pnl
  and cash, matching `backtest/costs` — asymmetry flatters paper P&L.
  (Lineage: F-021, F-025.)
- **Tile/label honesty.** Metric names in the UI vs what the query computes —
  read the SQL, then the label.

## 6. Security / secrets (cross-cutting)

- **Credentials in error paths.** Exception messages that interpolate a URL or
  token (webhook in `requests` errors, api_key in repr) reach the log sink.
  (Lineage: F-060.)
- **Secrets in artifacts.** Grep the research/raw writers for anything from
  `Settings` that isn't market data.
- **Config surface.** `.env` loading, UI pages, and skills — nothing should
  echo settings wholesale.

## 7. Docs / findings hygiene (Phase 4 fuel)

- **Renamed/moved things.** Grep docs for old step names (`_step_auto_open`),
  old package paths (`strategy/ranker`), old counts (test files, UI pages,
  job counts) after any refactor.
- **Fixed findings still described as open** in doc robustness notes, and
  vice versa — the docs and the ledger must agree on status.
- **Memory files vs code.** Project memories recording "pending" work that
  code shows implemented (or the reverse) — update them in Phase 6.
