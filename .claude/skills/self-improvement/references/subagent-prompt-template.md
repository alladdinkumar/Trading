# Phase-2 domain-agent prompt template

Agents are only as good as their briefing. A bare "audit this directory" prompt
returns generic lint; the elements below are what made past agents return real
candidates. Every prompt must contain all seven sections.

Dispatch all domain agents **in one message** (parallel), read-only agent type
(`Explore` or equivalent), and never read their `.output` transcripts — the
structured return below is the whole interface.

## Template

```
You are one of N parallel read-only auditors of a Python paper-trading system
for Indian equities (NSE). Your domain: <DOMAIN>.

## System context (trust this, don't re-derive it)
<5–10 lines: what the system does, the two-layer decision model, the daily
lifecycle, and where YOUR domain sits in it. Include the north star: findings
matter in proportion to how they distort the paper-trading track record
(equity curve / OOS Sharpe) or break unattended operation.>

## Your files
<Explicit list of the files/dirs to read, with one-phrase role each. An agent
that has to discover the map spends its budget on discovery.>

## Hypotheses to chase (in priority order)
<3–6 pre-formed hypotheses from references/hypothesis-catalog.md, phrased as
falsifiable claims with the exact file to check, e.g.:
"H1: walkforward.py may have no embargo between train_end and test_start —
check the fold-boundary arithmetic against the label horizon in
forward_return.py.">

## Already filed — do NOT re-report
<The exclusion list from Phase 1: every existing F-0xx in one line each.
Candidates that match these are wasted budget.>

## Hard constraints
- READ-ONLY. Do not edit, create, or delete any file. Do not run code that
  writes (no pytest, no CLI commands that touch data/).
- Never print or quote secrets (Kite keys, Slack webhook values).
- Cap: at most 6 findings. Fewer, well-evidenced candidates beat a long list.

## Return format (your final message, nothing else)
For each candidate:
- **Claim:** one falsifiable sentence.
- **Evidence:** file:line + the quoted load-bearing line(s).
- **Failure scenario:** concrete inputs/state → wrong output.
- **Proposed severity:** High/Med/Low + one-line justification tied to the
  north star.
Then a **"Checked and sound"** list: hypotheses you investigated that did NOT
pan out, with the disproving evidence. This is as valuable as the findings.
```

## Worked example (backtest/ML domain, abridged)

```
Your domain: backtest & ML validation integrity.

## Hypotheses to chase
H1: walk-forward folds may leak — check `backtest/walkforward.py` fold
arithmetic: is there an embargo ≥ the label horizon (`forward_return.py`
max_days) between train_end and test_start?
H2: Sharpe may be annualised inconsistently across the codebase — compare
`backtest/metrics.py` (daily √252) with `ranking/ranker_train.py`
(periods_per_year=…) and the weekly review.
H3: fold-stitched equity curves may reset capital per fold, overstating
drawdown-adjusted metrics — check how fold results concatenate.

## Already filed — do NOT re-report
F-049 (walkforward test window <...>), F-053 (<...>), ...
```

## Handling the results

- Expect overlap: two agents flagging the same line is one finding with
  corroboration.
- Expect over-grading: proposed High usually lands Med after the main-session
  read.
- Expect one hypothesis per agent to be wrong in an interesting way — the
  "checked and sound" list feeds the coverage table's "Verified sound" column.
- If dispatch is rejected (session cap): run the same prompt structure inline,
  one domain at a time, in the main session. Same hypotheses, same return
  discipline, same verification pass afterward.
