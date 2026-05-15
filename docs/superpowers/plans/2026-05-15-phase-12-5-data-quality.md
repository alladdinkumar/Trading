# Phase 12.5 — Data Quality Cleanup: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three upstream data-quality bugs (parquet last-bar NaN, future-dated news leak, sentiment-aggregate empty) and downgrade `sector_commentary.md` to optional, so the Phase 13 pre_open MVP launches against clean data.

**Architecture:** Five small sub-tasks, each its own commit. Three are targeted edits in existing modules (`store/ohlcv.py`, `llm/context.py`, `llm/briefing.py`); one is a one-shot CLI invocation; one is the wrap-up. No new modules, no new tables.

**Tech Stack:** Python 3.11 · sqlite3 · pandas · `typer` (CLI) · `syrupy` (snapshot tests) · `pytest`. Existing project conventions: frozen dataclasses, Ruff/mypy strict, in-memory SQLite for tests.

**Spec:** [`docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md`](../specs/2026-05-15-phase-12-5-data-quality-design.md)

---

## File structure

| Path | Created/Modified | Responsibility |
|------|------------------|----------------|
| `src/trading/store/ohlcv.py` | Modify | Add `_drop_trailing_nan_close(df)` helper; call it in `read_ohlcv` |
| `tests/test_ohlcv_store.py` | Modify | Tests for `_drop_trailing_nan_close` + integration with `read_ohlcv` |
| `src/trading/llm/context.py` | Modify | Filter future-dated rows in `_render_news_for_symbol`'s SQL |
| `tests/test_llm_context.py` | Modify | Test future-dated headline excluded; re-record snapshot |
| `tests/__snapshots__/test_llm_context.ambr` | Re-record (auto) | Updated by syrupy after the SQL change |
| `src/trading/llm/briefing.py` | Modify | Split `expected_parts` → `required_parts` + `optional_parts`; placeholder substitution in `compile_brief` |
| `tests/test_llm_briefing.py` | Modify | Replace `test_expected_parts_*` with `required_parts` / `optional_parts` tests; add placeholder-substitution test; re-record snapshots |
| `tests/__snapshots__/test_llm_briefing.ambr` | Re-record (auto) | Updated by syrupy after `compile_brief` changes |
| `.claude/skills/analyst/SKILL.md` | Modify | Mark `sector_commentary.md` optional in the outputs table |
| `data/app.db` | Mutate at runtime (12.5.3) | Sentiment aggregate refresh — no code change, just a CLI run |
| `PROGRESS.md` | Modify | Insert Phase 12.5 block; add Phase 12.5 + 12.6 rows to status table; update pointers |

---

## Task 1: Drop trailing NaN-close rows in `read_ohlcv` (12.5.1)

**Files:**
- Modify: `src/trading/store/ohlcv.py`
- Modify: `tests/test_ohlcv_store.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_ohlcv_store.py`:

```python
import math


def _df_with_trailing_nan(valid_rows: int = 3, nan_rows: int = 1) -> pd.DataFrame:
    total = valid_rows + nan_rows
    idx = pd.date_range("2025-01-01", periods=total, freq="B")
    idx.name = "date"
    closes = [100.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    opens  = [99.0  + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    highs  = [101.0 + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    lows   = [98.0  + i for i in range(valid_rows)] + [float("nan")] * nan_rows
    vols   = [1_000_000 + i for i in range(valid_rows)] + [5_000_000] * nan_rows
    return pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": vols},
        index=idx,
    )


def test_read_drops_trailing_nan_close_rows(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=3, nan_rows=1)
    write_ohlcv(df, "RECLTD", paths)
    back = read_ohlcv("RECLTD", paths)
    assert len(back) == 3
    assert not math.isnan(back["close"].iloc[-1])


def test_read_drops_multiple_trailing_nan_rows(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=2, nan_rows=3)
    write_ohlcv(df, "X", paths)
    back = read_ohlcv("X", paths)
    assert len(back) == 2


def test_read_keeps_interior_nan_close(tmp_path: Path) -> None:
    """Only TRAILING NaN-close rows are stripped — a NaN in the middle stays."""
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=3, nan_rows=0)
    df.iloc[1, df.columns.get_loc("close")] = float("nan")  # interior NaN
    write_ohlcv(df, "Y", paths)
    back = read_ohlcv("Y", paths)
    assert len(back) == 3
    assert math.isnan(back["close"].iloc[1])


def test_read_all_nan_close_returns_empty(tmp_path: Path) -> None:
    paths = get_paths(root=tmp_path)
    df = _df_with_trailing_nan(valid_rows=0, nan_rows=3)
    write_ohlcv(df, "Z", paths)
    back = read_ohlcv("Z", paths)
    assert len(back) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_ohlcv_store.py -v -k "trailing_nan or all_nan or interior_nan"`
Expected: FAIL — `len(back) == 3` is wrong (it's 4 because the NaN row is included).

- [ ] **Step 3: Write minimal implementation**

In `src/trading/store/ohlcv.py`, after `list_symbols`:

```python
def _drop_trailing_nan_close(df: pd.DataFrame) -> pd.DataFrame:
    """Strip trailing rows where `close` is NaN.

    yfinance returns a row for the current trading day with NaN OHLC
    before the day has closed. That stub row breaks downstream indicator
    math (RSI/SMA propagate NaN), so we drop it here at the storage
    boundary. Interior NaN rows are preserved — only trailing NaN-close
    rows are stripped.
    """
    if df.empty:
        return df
    last_valid_idx = df["close"].last_valid_index()
    if last_valid_idx is None:
        return df.iloc[0:0]
    return df.loc[:last_valid_idx]
```

Modify `read_ohlcv` to call it. Replace the function body's last block:

```python
def read_ohlcv(
    symbol: str,
    paths: Paths,
    *,
    start: date | str | None = None,
    end: date | str | None = None,
) -> pd.DataFrame:
    """Read a symbol's parquet, optionally filter by inclusive [start, end].

    Trailing rows with NaN `close` (yfinance current-day stub) are dropped
    before slicing.
    """
    target = parquet_path(symbol, paths)
    if not target.is_file():
        raise FileNotFoundError(f"No parquet for {symbol} at {target}")
    df = pd.read_parquet(target)
    df = _drop_trailing_nan_close(df)
    if start is not None:
        df = df.loc[pd.Timestamp(start) :]
    if end is not None:
        df = df.loc[: pd.Timestamp(end)]
    return df
```

- [ ] **Step 4: Run all ohlcv_store tests**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_ohlcv_store.py -v`
Expected: all passing (existing 6 + 4 new = 10).

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/store/ohlcv.py tests/test_ohlcv_store.py
git commit -m "fix(store): drop trailing NaN-close rows in read_ohlcv (12.5.1)

yfinance returns a row for the current trading day with NaN OHLC before
the day has closed. The scanner then propagates NaN through indicators.
Strip trailing NaN-close rows at the storage boundary. Interior NaN
rows are preserved.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 2: Filter future-dated news in context renderer (12.5.2)

**Files:**
- Modify: `src/trading/llm/context.py`
- Modify: `tests/test_llm_context.py`
- Re-record: `tests/__snapshots__/test_llm_context.ambr`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_llm_context.py`:

```python
def test_assemble_context_excludes_future_dated_news(
    conn: sqlite3.Connection, paths
) -> None:
    """NSE event-calendar entries arrive with future ts and must not leak."""
    # Past headline (within 7d window)
    conn.execute(
        "INSERT INTO news_items (ts, symbol, source, headline, url, "
        "sentiment, category, is_critical) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-13T10:00:00", "RVNL", "moneycontrol",
         "Past RVNL story", "https://example.com/p", 0.3, "results", 0),
    )
    # Future headline (NSE event scheduled for next week)
    conn.execute(
        "INSERT INTO news_items (ts, symbol, source, headline, url, "
        "sentiment, category, is_critical) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("2026-05-21T15:00:00", "RVNL", "nse_events",
         "RVNL: Financial Results on 21-May-2026", "https://nse.example", None, None, 0),
    )
    conn.commit()
    out = assemble_context(
        conn=conn, paths=paths, as_of=date(2026, 5, 15),
        mode="pre_open",
        inputs=ContextInputs(candidates=[_candidate("RVNL", n_passed=9)],
                             holdings_health=[]),
    )
    body = out.read_text(encoding="utf-8")
    assert "Past RVNL story" in body
    assert "Financial Results on 21-May-2026" not in body
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_context.py::test_assemble_context_excludes_future_dated_news -v`
Expected: FAIL — assertion `"Financial Results on 21-May-2026" not in body` fires (the future headline currently leaks).

- [ ] **Step 3: Write minimal implementation**

In `src/trading/llm/context.py`, modify `_render_news_for_symbol`. Replace the `cutoff = ...` block + the `news_items` query with:

```python
def _render_news_for_symbol(
    conn: sqlite3.Connection, symbol: str, as_of: date
) -> list[str]:
    """Last 7 days of headlines + sentiment_daily summary + critical flag."""
    cutoff = (as_of - timedelta(days=7)).isoformat()
    # Filter future-dated rows (NSE event-calendar entries arrive with
    # future ts; long-term home is event_calendar table — see Phase 12.5
    # spec §12.5.2). End-of-day cap keeps same-day intraday news visible.
    upper = as_of.isoformat() + "T23:59:59"
    rows = conn.execute(
        "SELECT ts, headline, sentiment, category, is_critical "
        "FROM news_items WHERE symbol = ? AND ts >= ? AND ts <= ? "
        "ORDER BY ts DESC LIMIT 5",
        (symbol, cutoff, upper),
    ).fetchall()
    sd = conn.execute(
        "SELECT score_7d, news_count, negative_news_count, has_critical "
        "FROM sentiment_daily WHERE date = ? AND symbol = ?",
        (as_of.isoformat(), symbol),
    ).fetchone()
    out: list[str] = []
    if sd is not None:
        score_7d = sd["score_7d"]
        score_str = f"{score_7d:+.2f}" if score_7d is not None else "—"
        out.append(
            f"- sentiment 7d {score_str} · "
            f"{sd['news_count']} headlines ({sd['negative_news_count']} negative)"
        )
        out.append(
            f"- Critical news flag: {'YES' if sd['has_critical'] else 'NO'}"
        )
    else:
        out.append("- sentiment: _(no daily aggregate)_")
        out.append("- Critical news flag: NO")
    if rows:
        out.append("- Recent headlines:")
        for r in rows:
            score = f"{r['sentiment']:+.2f}" if r["sentiment"] is not None else "—"
            cat = r["category"] or "—"
            out.append(f"  - {r['ts'][:10]} · [{cat}] {r['headline']} ({score})")
    return out
```

- [ ] **Step 4: Run new test + re-record snapshots**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_context.py -v`
If the existing `test_full_pre_open_bundle_snapshot` fails (the seeded JIOFIN headline at 2026-05-13 is past today's 2026-05-15, so it should still render — but verify no regressions), re-record:

```bash
cd D:/Projects/Trading
uv run pytest tests/test_llm_context.py --snapshot-update -v
uv run pytest tests/test_llm_context.py -v
```

Expected: all 13 passing (12 existing + 1 new).

- [ ] **Step 5: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/llm/context.py tests/test_llm_context.py tests/__snapshots__/test_llm_context.ambr
git commit -m "fix(llm): filter future-dated news in context renderer (12.5.2)

NSE event-calendar entries are written into news_items with their
future event date as ts. Render-time filter caps the upper bound at
as_of end-of-day. Long-term fix is to route NSE events into
event_calendar table (Phase 13 prep).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 3: Split `expected_parts` and substitute placeholder for missing optional (12.5.4)

**Files:**
- Modify: `src/trading/llm/briefing.py`
- Modify: `tests/test_llm_briefing.py`
- Modify: `.claude/skills/analyst/SKILL.md`
- Modify: `src/trading/llm/__init__.py`
- Re-record: `tests/__snapshots__/test_llm_briefing.ambr`

- [ ] **Step 1: Replace existing `expected_parts` tests + add placeholder test**

In `tests/test_llm_briefing.py`, replace the existing `test_expected_parts_pre_open` and `test_expected_parts_post_close` with:

```python
from trading.llm.briefing import (
    MissingNarrativeError,
    compile_brief,
    optional_parts,
    required_parts,
)


def test_required_parts_pre_open() -> None:
    parts = required_parts("pre_open", candidate_symbols=["RVNL", "NTPC"])
    assert parts == [
        "macro_brief.md",
        "candidates/RVNL.md",
        "candidates/NTPC.md",
    ]


def test_required_parts_post_close() -> None:
    parts = required_parts("post_close", candidate_symbols=["RVNL"])
    assert parts == [
        "macro_brief.md",
        "candidates/RVNL.md",
        "post_close_recap.md",
    ]


def test_optional_parts_returns_sector_commentary() -> None:
    assert optional_parts("pre_open") == ["sector_commentary.md"]
    assert optional_parts("post_close") == ["sector_commentary.md"]
```

Update the existing `test_compile_brief_raises_when_parts_missing` to drop `sector_commentary.md` from the expected error message:

```python
def test_compile_brief_raises_when_parts_missing(tmp_path: Path) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    (date_dir / "_context.md").write_text(
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
        encoding="utf-8",
    )
    with pytest.raises(MissingNarrativeError) as exc_info:
        compile_brief(date_dir, mode="pre_open")
    msg = str(exc_info.value)
    assert "macro_brief.md" in msg
    assert "candidates/RVNL.md" in msg
    # sector_commentary.md is OPTIONAL since 12.5.4 — must NOT be in error
    assert "sector_commentary.md" not in msg
```

Add a placeholder-substitution test:

```python
def test_compile_brief_substitutes_placeholder_when_sector_missing(
    tmp_path: Path,
) -> None:
    date_dir = tmp_path / "2026-05-15"
    date_dir.mkdir()
    _write_part(
        date_dir, "_context.md",
        "# Trading context bundle — 2026-05-15  (mode: pre_open)\n"
        "\n## Today's candidates\n\n### RVNL — passes 9/10 rules\n",
    )
    _write_part(date_dir, "macro_brief.md", "Regime: NEUTRAL.\n")
    # NO sector_commentary.md written
    _write_part(date_dir, "candidates/RVNL.md", "# RVNL — Conviction: HIGH\n")
    out = compile_brief(date_dir, mode="pre_open")
    body = out.read_text(encoding="utf-8")
    assert "## Sector commentary" in body
    assert "_(sector commentary not yet wired — see Phase 12.6)_" in body
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd D:/Projects/Trading && uv run pytest tests/test_llm_briefing.py -v`
Expected: FAIL — `required_parts`/`optional_parts` don't exist; `test_compile_brief_raises_when_parts_missing` fails because sector_commentary IS in the error message currently; placeholder test fails because compile_brief raises.

- [ ] **Step 3: Implement the split + placeholder substitution**

In `src/trading/llm/briefing.py`, replace the `expected_parts` function with:

```python
SECTOR_COMMENTARY_PLACEHOLDER = "_(sector commentary not yet wired — see Phase 12.6)_"


def required_parts(mode: Mode, candidate_symbols: list[str]) -> list[str]:
    """Parts that MUST exist or compile_brief raises MissingNarrativeError."""
    parts = ["macro_brief.md"]
    parts.extend(f"candidates/{sym}.md" for sym in candidate_symbols)
    if mode == "post_close":
        parts.append("post_close_recap.md")
    return parts


def optional_parts(mode: Mode) -> list[str]:
    """Parts compile_brief tolerates being absent (substitutes a placeholder)."""
    return ["sector_commentary.md"]
```

Update `compile_brief` to use `required_parts` for the missing check and to substitute the placeholder for `sector_commentary.md`:

```python
def compile_brief(date_dir: Path, *, mode: Mode | None = None) -> Path:
    """Read narrative parts in `date_dir`, write `brief.md`, return its path.

    Raises `MissingNarrativeError` listing any required parts that are
    absent. Optional parts (sector_commentary.md) get a hardcoded
    placeholder body when missing — the section header stays. Orphan
    candidate files (symbols not in the bundle) are skipped with a
    stderr warning. If `mode` is None, it is inferred from the bundle
    header.
    """
    context_path = date_dir / "_context.md"
    if not context_path.is_file():
        raise MissingNarrativeError(
            f"Cannot compile brief: bundle is missing at {context_path}. "
            "Run `trading brief assemble-context` first."
        )
    context_md = context_path.read_text(encoding="utf-8")
    if mode is None:
        mode = _infer_mode(context_md)
    symbols = _parse_candidate_symbols(context_md)
    required = required_parts(mode, symbols)
    missing = [p for p in required if not (date_dir / p).is_file()]
    if missing:
        raise MissingNarrativeError(
            "Missing analyst narrative files: " + ", ".join(missing)
        )

    candidates_dir = date_dir / "candidates"
    if candidates_dir.is_dir():
        expected_syms = set(symbols)
        for f in sorted(candidates_dir.iterdir()):
            if f.suffix == ".md" and f.stem not in expected_syms:
                print(
                    f"warning: orphan candidate file {f.relative_to(date_dir)} "
                    f"(symbol not in bundle) — skipped",
                    file=sys.stderr,
                )

    sector_path = date_dir / "sector_commentary.md"
    sector_body = (
        sector_path.read_text(encoding="utf-8").strip()
        if sector_path.is_file()
        else SECTOR_COMMENTARY_PLACEHOLDER
    )

    out_path = date_dir / "brief.md"
    parts_count = len(required) + (1 if sector_path.is_file() else 0)
    sections: list[str] = [
        f"# Daily brief — {date_dir.name}",
        f"_Compiled at {datetime.now().isoformat(timespec='seconds')} from "
        f"{parts_count} narrative parts._",
        "",
        "## Macro",
        (date_dir / "macro_brief.md").read_text(encoding="utf-8").strip(),
        "",
        "## Sector commentary",
        sector_body,
        "",
        "## Candidates",
    ]
    for sym in symbols:
        body = (date_dir / "candidates" / f"{sym}.md").read_text(encoding="utf-8")
        sections.append("")
        sections.append(body.strip())
    if mode == "post_close":
        sections.append("")
        sections.append("## Post-close recap")
        sections.append(
            (date_dir / "post_close_recap.md").read_text(encoding="utf-8").strip()
        )
    out_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    return out_path
```

Update `src/trading/llm/__init__.py` to export the new names:

```python
"""Public surface for the LLM analyst pipeline (spec §4.3)."""

from trading.llm.briefing import (
    MissingNarrativeError,
    compile_brief,
    optional_parts,
    required_parts,
)
from trading.llm.context import ContextInputs, Mode, assemble_context

__all__ = [
    "ContextInputs",
    "MissingNarrativeError",
    "Mode",
    "assemble_context",
    "compile_brief",
    "optional_parts",
    "required_parts",
]
```

- [ ] **Step 4: Re-record snapshots + run all briefing/cli tests**

Both existing snapshot tests (`test_compile_brief_pre_open_happy_path_snapshot`,
`test_compile_brief_post_close_includes_recap`) write `sector_commentary.md`
explicitly so the rendered output should be unchanged. But the
`_Compiled at ... from N narrative parts._` count formula changed
(`len(required) + 1`), so the snapshots will need re-recording.

```bash
cd D:/Projects/Trading
uv run pytest tests/test_llm_briefing.py --snapshot-update -v
uv run pytest tests/test_llm_briefing.py tests/test_cli.py -v
```

Expected: 8 briefing tests passing (4 existing — happy-path snapshot, post_close snapshot, raises-when-missing, orphan-warning — plus 4 new — required_parts × 2, optional_parts, placeholder-substitution); 12 CLI tests still passing.

- [ ] **Step 5: Update `SKILL.md` to mark sector_commentary optional**

Edit `.claude/skills/analyst/SKILL.md`. Replace the Outputs section table with:

```markdown
## Outputs

Write each file under the same `data/research/YYYY-MM-DD/` directory. Follow the skeletons in `references/output-templates.md` exactly — `compile_brief` parses fixed headings.

| Mode       | Required files                                                                                                                       | Optional |
|------------|---------------------------------------------------------------------------------------------------------------------------------------|----------|
| pre_open   | `macro_brief.md`, `candidates/{SYMBOL}.md` for every symbol in the bundle's `## Today's candidates` section                           | `sector_commentary.md` |
| post_close | `macro_brief.md`, `candidates/{SYMBOL}.md` for every candidate (if any), `post_close_recap.md`                                        | `sector_commentary.md` |

`sector_commentary.md` is OPTIONAL while `sector_daily` is unwired (Phase 12.6 will build it). If the bundle has no sector data, you may skip writing this file — `compile_brief` will substitute a placeholder under the `## Sector commentary` header.
```

- [ ] **Step 6: Commit**

```bash
cd D:/Projects/Trading
git add src/trading/llm/briefing.py src/trading/llm/__init__.py tests/test_llm_briefing.py tests/__snapshots__/test_llm_briefing.ambr .claude/skills/analyst/SKILL.md
git commit -m "feat(llm): make sector_commentary optional (12.5.4)

Split expected_parts into required_parts + optional_parts.
compile_brief substitutes a placeholder body under '## Sector
commentary' when sector_commentary.md is absent (sector_daily not
yet wired — see Phase 12.6). SKILL.md updated to mark optional.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Task 4: Sentiment aggregate refresh (12.5.3)

**Files:**
- Mutates: `data/app.db` (gitignored)

This is a one-shot CLI invocation, not a code change. Verify before + after.

- [ ] **Step 1: Inspect current sentiment_daily state**

Run:
```bash
cd D:/Projects/Trading && uv run python -c "
import sqlite3
c = sqlite3.connect('data/app.db'); c.row_factory = sqlite3.Row
print('rows for 2026-05-15:', c.execute(\"select count(*) from sentiment_daily where date = '2026-05-15'\").fetchone()[0])
for r in c.execute(\"select * from sentiment_daily where date = '2026-05-15'\"):
    print(' ', dict(r))
"
```
Expected (current state): 1 row (JIOFIN with NULL score_7d).

- [ ] **Step 2: Run ingest-news with scoring on**

Run:
```bash
cd D:/Projects/Trading && uv run trading ingest-news --date 2026-05-15
```

Expected: completes without error. May take several minutes the first time (FinBERT model downloads if not cached). Subsequent runs are fast.

- [ ] **Step 3: Verify sentiment_daily has more rows**

Run the same Python snippet from Step 1.
Expected: more rows than before for 2026-05-15. Symbols whose RSS headlines matched the alias map (Phase 8 `DEFAULT_ALIASES`) should now have non-NULL `score_7d`.

If the row count is unchanged or still very low, that means the alias map doesn't cover the universe well — note this in the smoke (12.5.5) but do NOT widen the alias map here (out of scope per spec §4).

- [ ] **Step 4: Re-run the Phase 12 real-data smoke and inspect**

Run:
```bash
cd D:/Projects/Trading
uv run python -c "
from datetime import date
from trading.config import get_paths
from trading.llm.context import ContextInputs, assemble_context
from trading.store.db import get_conn
from trading.store.migrations import run_migrations
from trading.strategy.rules import ScanContext, scan

paths = get_paths()
as_of = date(2026, 5, 15)
ctx = ScanContext(scan_date=as_of)
all_cands = scan(paths, as_of, ctx=ctx)
top5 = sorted(all_cands, key=lambda c: -sum(1 for r in c.rules if r.passed))[:5]

with get_conn(paths.db_path) as conn:
    run_migrations(conn)
    out = assemble_context(conn=conn, paths=paths, as_of=as_of,
                           mode='pre_open',
                           inputs=ContextInputs(candidates=top5, holdings_health=[]))
print('wrote', out)
"
cat data/research/2026-05-15/_context.md
```

Acceptance: at least one candidate now has `sentiment 7d {value}` instead of `sentiment: _(no daily aggregate)_`.

- [ ] **Step 5: No commit**

This sub-task changes only `data/app.db` (gitignored). No commit needed. Move on to Task 5.

---

## Task 5: Re-smoke + PROGRESS.md + commit + push (12.5.5)

**Files:**
- Modify: `PROGRESS.md`

- [ ] **Step 1: Confirm clean smoke**

The smoke run from Task 4 Step 4 should have produced a `_context.md` with:
- No `close nan` rows in candidates section (12.5.1)
- No future-dated headlines in any "Recent headlines" block (12.5.2)
- Real `sentiment 7d {value}` for at least one symbol (12.5.3)

Re-inspect:
```bash
cd D:/Projects/Trading && grep -n "close nan\|2026-05-2[0-9]\|2026-05-1[6-9]\|sentiment 7d" data/research/2026-05-15/_context.md | head -20
```

Expected: zero matches for `close nan`; zero matches for future dates (16-31 of May 2026); at least one `sentiment 7d` line.

If any of those regress, fix the corresponding sub-task before continuing.

- [ ] **Step 2: Update PROGRESS.md status table**

Edit `PROGRESS.md`. In the status snapshot table, replace the line currently reading `| 12 | LLM analyst | [x] |` immediately after to insert two new rows:

```
| 12 | LLM analyst | `[x]` |
| 12.5 | Data quality cleanup | `[x]` |
| 12.6 | Sector data | `[ ]` |
| 13 | pre_open job (MVP ⭐) | `[ ]` |
```

Update the pointers (currently say "Currently working on: Phase 13"):

```
**Currently working on:** _Phase 13 — pre_open job (MVP ⭐)_
**Next up:** _Phase 12.6 — Sector data (deferred)_
```

- [ ] **Step 3: Insert Phase 12.5 block in body**

In `PROGRESS.md`, after the existing `## Phase 12 — LLM analyst (Claude Code skill version)` block and before `## Phase 13 — pre_open job (E2E) ⭐ MVP milestone`, insert:

```markdown
## Phase 12.5 — Data quality cleanup (pre-Phase-13 prep)

> Surfaced by the Phase 12 real-data smoke (memory:
> `project_data_quality_gaps_2026_05_15`). Spec at
> [`docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md`](docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md).

- [x] 12.5.1 `src/trading/store/ohlcv.py`: `_drop_trailing_nan_close` strips
       yfinance's current-day NaN-OHLC stub row at the storage boundary;
       interior NaN preserved. 4 new tests in `test_ohlcv_store.py`.
- [x] 12.5.2 `src/trading/llm/context.py`: `_render_news_for_symbol` SQL
       caps `ts <= as_of end-of-day` so NSE event-calendar entries with
       future event dates don't leak into "Recent headlines". 1 new test;
       snapshot re-recorded.
- [x] 12.5.3 Ran `trading ingest-news --date 2026-05-15` (without
       `--skip-score`) to backfill `sentiment_daily` for the holdings +
       scanner universe. No code change.
- [x] 12.5.4 `src/trading/llm/briefing.py`: split `expected_parts` into
       `required_parts` + `optional_parts`; `compile_brief` substitutes
       a hardcoded placeholder body for missing optional parts. SKILL.md
       updated to mark sector_commentary as optional. 4 new tests +
       1 updated; both compile_brief snapshots re-recorded.
- [x] 12.5.5 Real-data smoke confirms candidate section now shows real
       sentiment scores and no NaN/future-date noise. PROGRESS.md updated;
       commit `feat(data): Phase 12.5 quality fixes` pushed to origin/main.

## Phase 12.6 — Sector data (deferred)

- [ ] 12.6.1 Spec the `data/sector.py` module (NSE sectoral indices via
       nsepython/yfinance, 5d/20d/60d relative strength vs Nifty 200).
- [ ] 12.6.2 Implement + persist into `sector_daily` table.
- [ ] 12.6.3 Wire into `assemble_context` (replace placeholder).
- [ ] 12.6.4 CLI: `trading sector --date YYYY-MM-DD`.
- [ ] 12.6.5 Tests + smoke + commit.
```

- [ ] **Step 4: Run full suite + lint + types**

Run:
```bash
cd D:/Projects/Trading
uv run ruff check . && uv run mypy src/ && uv run pytest -q
```

Expected: all clean. Test count: `442 + 4 (12.5.1) + 1 (12.5.2) + 4 (12.5.4) - 2 (replaced expected_parts tests) + 1 (updated raises test) - 1 (replaced raises test) = 449` passing, 1 skipped (live).

(If counts diverge slightly that's fine — record the actual number in the commit body.)

- [ ] **Step 5: Commit + push**

```bash
cd D:/Projects/Trading
git add PROGRESS.md
git commit -m "$(cat <<'EOF'
feat(data): Phase 12.5 quality fixes (parquet NaN, news ts, optional sector)

Three small fixes + one downgrade, all surfaced by the Phase 12
real-data smoke:

  12.5.1 read_ohlcv strips yfinance's current-day NaN-OHLC stub row.
  12.5.2 context renderer caps news ts at as_of end-of-day so future
         NSE event-calendar entries don't leak.
  12.5.3 ran ingest-news with scoring on to backfill sentiment_daily.
  12.5.4 sector_commentary.md now optional (sector_daily build deferred
         to Phase 12.6); compile_brief substitutes a placeholder.

Spec: docs/superpowers/specs/2026-05-15-phase-12-5-data-quality-design.md
Plan: docs/superpowers/plans/2026-05-15-phase-12-5-data-quality.md

Tests: <count> passed, 1 skipped (live).

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
git push origin main
```

Expected: push succeeds; Phase 13 is now unblocked on a clean foundation.

---

## Self-review notes

- **Spec coverage:** every sub-task in spec §2 (12.5.1–12.5.5) → a Task. Spec §3 dependencies (12.5.3 after 12.5.1+2) honoured by Task ordering. Spec §4 out-of-scope items not added as tasks. ✓
- **Type consistency:** `Mode` re-used unchanged from `context.py`. `required_parts` / `optional_parts` signatures match between briefing.py, __init__.py, and tests. `SECTOR_COMMENTARY_PLACEHOLDER` is a module-level constant referenced once in production code and once in the placeholder-substitution test. ✓
- **Placeholder scan:** every code block is concrete; commit messages are written out; no "TBD" or "implement later". The one judgement call is in 12.5.3 Step 3 ("If the row count is unchanged or still very low, ... do NOT widen the alias map here") — this is a deliberate scope guard, not a placeholder. ✓
- **Snapshot risk:** Tasks 2 and 3 both touch syrupy snapshots. If the diff during `--snapshot-update` looks unexpected (e.g. content beyond timestamps changed), inspect before committing. The plan calls for an explicit `pytest -v` re-run after `--snapshot-update` to catch any second-order regressions.

---

**Plan complete and saved to `docs/superpowers/plans/2026-05-15-phase-12-5-data-quality.md`. Two execution options:**

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using `executing-plans`, batch execution with checkpoints.

**Which approach?**
