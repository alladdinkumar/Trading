# Phase 12.6 — Sector data (NSE sectoral indices + relative strength)

**Date:** 2026-05-26
**Status:** Approved
**Predecessor:** [Phase 12.5 data quality design](2026-05-15-phase-12-5-data-quality-design.md)

## 1. Context & motivation

Phase 12.5 downgraded `sector_commentary.md` to optional and stubbed the
`## Sector commentary` body with a placeholder, because no module was
computing NSE sectoral relative strength. Phase 12.6 builds that module,
persists daily rows into the existing `sector_daily` table (created back in
Phase 1's schema), and rewires three consumers that already have hooks for
this data:

1. `assemble_context` — currently has no sector section at all; will gain
   a `## Sector momentum` table that the analyst can cite.
2. `pre_open_iep` — already accepts `sector_map` / `sector_momentum`
   parameters with no runtime source. Phase 12.6 makes the auto-load path
   live by default.
3. `compile_brief` / analyst SKILL.md — placeholder wording softens and
   the "optional while sector_daily is unwired" caveat is removed.

The sector universe is 11 standard NSE sectoral indices. RS is computed
against Nifty 50 (`^NSEI`) over 5d / 20d / 60d windows. Per-sector regime
labels (`LEADING` / `NEUTRAL` / `LAGGING`) are derived from `rs_20d`.

## 2. Sector universe

11 sectoral indices, all fetched via yfinance. Per-ticker failure becomes
a warning, never aborts the snapshot — same pattern as `data/macro.py`.

| Sector code | yfinance ticker |
|---|---|
| `NIFTYBANK` | `^NSEBANK` |
| `IT` | `^CNXIT` |
| `AUTO` | `^CNXAUTO` |
| `FMCG` | `^CNXFMCG` |
| `PHARMA` | `^CNXPHARMA` |
| `METAL` | `^CNXMETAL` |
| `ENERGY` | `^CNXENERGY` |
| `REALTY` | `^CNXREALTY` |
| `PSUBANK` | `^CNXPSUBANK` |
| `FINSERV` | `^CNXFIN` |
| `INFRA` | `^CNXINFRA` |

Benchmark: Nifty 50 (`^NSEI`).

## 3. Data model

`sector_daily` table already exists from Phase 1; no schema change.

Columns (recap): `date TEXT`, `sector TEXT`, `close REAL NOT NULL`,
`rs_5d REAL`, `rs_20d REAL`, `rs_60d REAL`,
`regime TEXT CHECK (regime IN ('LEADING','NEUTRAL','LAGGING'))`,
`PRIMARY KEY (date, sector)`.

**RS formula:** `rs_Nd = sector_return_Nd - benchmark_return_Nd` where
`return = (close_today / close_N_days_ago) - 1`. Positive ⇒ outperformed
Nifty 50 over the window.

**Regime thresholds (on `rs_20d`):**

- `rs_20d > +0.02` (2%) → `LEADING`
- `rs_20d < -0.02` (2%) → `LAGGING`
- else → `NEUTRAL`

`None` RS values (insufficient history, fetch failure) mean `regime`
stays `None` for that row — schema allows it.

## 4. Sub-tasks

### 12.6.1 — Sector fetcher + RS module (`data/sector.py`)

New module mirrors `data/macro.py`'s structure.

**Datatypes:**

```python
@dataclass(frozen=True)
class SectorRow:
    date: str            # YYYY-MM-DD
    sector: str          # one of SECTOR_TICKERS keys
    close: float
    rs_5d: float | None
    rs_20d: float | None
    rs_60d: float | None
    regime: str | None   # 'LEADING' | 'NEUTRAL' | 'LAGGING' | None
```

**Constants:**

```python
SECTOR_TICKERS: dict[str, str] = { ... }   # the 11-entry table above
BENCHMARK_TICKER = "^NSEI"
RS_WINDOWS = (5, 20, 60)
LEADING_THRESHOLD = 0.02
LAGGING_THRESHOLD = -0.02
```

**Public functions:**

- `fetch_sector_history(ticker, lookback_days=90) -> pd.DataFrame | None`
  — yfinance wrapper, returns DataFrame indexed by date with a `close`
  column, or `None` on any error (HTTP, rate-limit, deprecated ticker).
  Defensive identical to `fetch_yf_quote`.

- `compute_rs(sector_closes, benchmark_closes, *, window) -> float | None`
  — pure function. Returns the simple-difference RS. `None` if either
  series has fewer than `window+1` bars or zero at the lookback bar.

- `_regime_for(rs_20d: float | None) -> str | None` — applies the
  threshold table; returns `None` if `rs_20d is None`.

- `fetch_all_sectors(as_of: date) -> list[SectorRow]` — orchestrator.
  Pulls benchmark once, then iterates sectors. Per-sector failures yield
  no row (rather than a row with all-None RS) so the count of returned
  rows reflects fetch success. Returns rows whose `date` column equals
  `as_of.isoformat()`; if yfinance's last bar is from D-1 (after market
  close) we still tag it with `as_of` since the snapshot is "as of"
  today's pre-open or post-close run.

- `load_sector_map(paths: Paths | None = None) -> dict[str, str]` —
  reads `data/static/sector_map.csv`. Header row `symbol,sector`. Skips
  blank lines and `#` comments. Returns `{symbol: sector_code}`. Missing
  file returns `{}` with no error (graceful — IEP just skips the sector
  axis).

### 12.6.2 — Persistence (`store/sector_store.py`)

New module mirrors `store/macro_store.py`.

```python
def upsert_sector_daily(conn, rows: list[SectorRow]) -> int:
    """INSERT ON CONFLICT(date, sector) DO UPDATE per row. Returns count."""

def get_sector_daily(conn, as_of: date) -> list[SectorRow]:
    """All sectors for one date, ordered by sector. Empty list if none."""
```

Both use the columns in their declared order. `get_sector_daily` reads
the CHECK-constrained `regime` text back as-is.

### 12.6.3 — Sector map file

New file `data/static/sector_map.csv`:

```
# Symbol → NSE sectoral index map. One symbol per line.
# Sector codes must match data/sector.py::SECTOR_TICKERS keys.
symbol,sector
HDFCBANK,NIFTYBANK
ICICIBANK,NIFTYBANK
AXISBANK,NIFTYBANK
KOTAKBANK,NIFTYBANK
INDUSINDBK,NIFTYBANK
SBIN,PSUBANK
IDFCFIRSTB,NIFTYBANK
INFY,IT
TCS,IT
HCLTECH,IT
WIPRO,IT
TECHM,IT
LTIM,IT
M&M,AUTO
MARUTI,AUTO
EICHERMOT,AUTO
TATAMOTORS,AUTO
BAJAJ-AUTO,AUTO
HEROMOTOCO,AUTO
ITC,FMCG
NESTLEIND,FMCG
BRITANNIA,FMCG
HINDUNILVR,FMCG
TATACONSUM,FMCG
DRREDDY,PHARMA
SUNPHARMA,PHARMA
CIPLA,PHARMA
APOLLOHOSP,PHARMA
TATASTEEL,METAL
JSWSTEEL,METAL
HINDALCO,METAL
COALINDIA,METAL
NTPC,ENERGY
POWERGRID,ENERGY
TATAPOWER,ENERGY
ONGC,ENERGY
BPCL,ENERGY
RELIANCE,ENERGY
BAJFINANCE,FINSERV
BAJAJFINSV,FINSERV
SHRIRAMFIN,FINSERV
HDFCLIFE,FINSERV
SBILIFE,FINSERV
PFC,FINSERV
RECLTD,FINSERV
JIOFIN,FINSERV
LT,INFRA
ULTRACEMCO,INFRA
GRASIM,INFRA
ASIANPAINT,INFRA
ADANIENT,INFRA
ADANIPORTS,INFRA
RVNL,INFRA
IRB,INFRA
BEL,INFRA
MAZDOCK,INFRA
IREDA,FINSERV
```

Symbols left unmapped (BHARTIARTL, TRENT, TITAN — no clean NSE
sectoral index, e.g. telecom is not in the 11-index set) are tolerated:
the IEP path treats them as "no sector bonus" rather than vetoing. This
file is user-editable; mappings are opinion, not law.

### 12.6.4 — Wire into `pre_open` (`jobs/pre_open.py`)

- Add `_step_sector(conn, as_of, warnings) -> bool` after `_step_macro`.
  Calls `fetch_all_sectors` + `upsert_sector_daily`. On any error
  (yfinance down, etc.) appends warning, returns `False`. Pre-open does
  not abort.
- `PreOpenResult` gets `sector_written: bool` field.
- `pre_open_cmd` in `cli.py` adds a `sector_written` row to its Rich
  table.

### 12.6.5 — Wire into `pre_open_iep` (`jobs/pre_open_iep.py`)

`run_pre_open_iep` signature unchanged. Behaviour change:

- When `sector_map is None`, auto-load via `data/sector.load_sector_map()`.
- When `sector_momentum is None`, auto-load via `get_sector_daily(conn, as_of)`
  (open a connection inside the function — current implementation
  already imports `get_conn`). Build `{sector_code: rs_5d}` from rows.
- If `sector_daily` empty for `as_of`, try D-1 (one calendar day back)
  as a graceful fallback; emit a warning naming the fallback date.
- If still empty, IEP runs with `sector_map={}` (effective no-op on the
  sector axis) and emits a warning.

Callers that want to *suppress* sector consideration pass `sector_map={}`
explicitly. The existing tests that pass non-`None` overrides keep
working unchanged.

### 12.6.6 — Wire into `assemble_context` (`llm/context.py`)

- New `_render_sector_snapshot(conn, as_of) -> str` reads
  `sector_daily` rows for `as_of`, orders by `rs_20d` descending (`None`
  values last), renders the markdown table from §3 above.
- Empty result: `## Sector momentum\n\n_(no data)_`.
- Inserted in `assemble_context` between the macro section and the
  candidates section (sector context belongs before per-candidate
  decisions).
- Per-candidate rendering in `_render_candidates`: load
  `load_sector_map` once at the top of `assemble_context`; for each
  candidate symbol, if a sector mapping exists and we have a
  `sector_daily` row, append one bullet
  `- sector: IT — 20d RS +3.5% (LEADING)`. Skipped silently when
  unmapped or no row.

### 12.6.7 — Briefing + analyst skill

- `briefing.py`: change `SECTOR_COMMENTARY_PLACEHOLDER` to
  `"_(analyst did not write a sector commentary for this run)_"`. The
  fallback now means "the analyst chose not to write one", not "this
  feature isn't built yet". Logic in `compile_brief` unchanged.
- `.claude/skills/analyst/SKILL.md`:
  - Drop the sentence `sector_commentary.md is OPTIONAL while sector_daily
    is unwired (Phase 12.6 will build it). …`.
  - Reframe: "Optional. Write when the bundle's `## Sector momentum`
    section is non-empty. Cite specific sector codes + their 20d RS."

### 12.6.8 — CLI command

`trading sector --date YYYY-MM-DD [--dry-run]` — same shape as `trading
macro`. Pulls live, prints a Rich table (Sector / Close / 5d RS / 20d RS
/ 60d RS / Regime), then upserts unless `--dry-run`. Exit code 1 if
zero sectors were fetched (all sources down).

### 12.6.9 — Tests

10–12 new tests across:

- `tests/test_data_sector.py` (5–6):
  - `compute_rs` returns expected value on a hand-built series.
  - `compute_rs` returns `None` when history < window+1.
  - `_regime_for` boundary cases (just above / below thresholds, `None`).
  - `fetch_sector_history` returns `None` on yfinance error (mocked).
  - `load_sector_map` parses CSV with comments + blank lines.
  - `load_sector_map` returns `{}` when file missing.
- `tests/test_store_sector.py` (3):
  - upsert + get round-trip.
  - upsert overwrites on (date, sector) conflict.
  - `get_sector_daily` returns `[]` when no rows for date.
- `tests/test_jobs_pre_open.py` (1 new): `_step_sector` happy path
  (mocked `fetch_all_sectors`) writes rows, sets `sector_written=True`,
  CLI result reflects it. Plus one failure-degrades-to-warning case.
- `tests/test_jobs_pre_open_iep.py` (1–2 new): auto-load reads
  `sector_daily`; D-1 fallback when today is empty; graceful
  degradation to empty map when both missing.
- `tests/test_llm_context.py` (1 new + snapshot re-record): sector
  section renders correctly; empty case yields `_(no data)_`. Existing
  bundle snapshots get re-recorded since the section ordering changed.
- `tests/test_cli.py` (1 new): `trading sector` happy path with mocked
  fetcher; `--dry-run` doesn't write.

### 12.6.10 — Smoke + PROGRESS + commit

1. `uv run trading sector --date 2026-05-26` against live yfinance.
   Verify Rich table renders 11 sectors + sane values + a regime label
   per row.
2. `uv run trading pre-open --date 2026-05-26` — verify `sector_written:
   yes` in the CLI table; inspect `_context.md` for the populated
   `## Sector momentum` section and the per-candidate sector bullets.
3. `uv run trading pre-open-iep --date 2026-05-26` — verify the CLI
   summary mentions sector filter/rerank is active (no
   "sector_map/sector_momentum not provided" warning).
4. `pytest -q`, `ruff check .`, `mypy src/` all green.
5. PROGRESS.md: flip `12.6` to `[x]` in the status snapshot; mark all
   sub-task lines `[x]` under Phase 12.6 (replacing the current `[ ]`
   stubs); update `Currently working on` / `Next up`.
6. Commit `feat(data): sector daily + RS (Phase 12.6)` and push to
   origin/main.

## 5. Dependencies between sub-tasks

- 12.6.1, 12.6.2, 12.6.3 are independent and can be done in any order.
- 12.6.4 (pre_open wiring) depends on 12.6.1 + 12.6.2.
- 12.6.5 (pre_open_iep wiring) depends on 12.6.1 + 12.6.2 + 12.6.3.
- 12.6.6 (context renderer) depends on 12.6.1 + 12.6.2 + 12.6.3.
- 12.6.7 (briefing + skill) is independent of the data work; can
  happen anytime.
- 12.6.8 (CLI) depends on 12.6.1 + 12.6.2.
- 12.6.9 (tests) folded into each task as it lands.
- 12.6.10 (smoke + commit) last.

## 6. Out of scope (Phase 18 or future)

- Backfill of historical `sector_daily` rows. Table starts populating
  from first run; 60d RS will be `None` for the first 60 trading days.
- Surfacing sector RS on the Streamlit dashboard. Phase 15 follow-up.
- Per-sector news rollups. News attribution stays per-symbol via the
  alias map.
- Routing the sector data into Phase 16's ranker feature set (would be
  a sensible Phase 18 retrain experiment).
