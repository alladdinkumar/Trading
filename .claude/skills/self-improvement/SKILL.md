---
name: self-improvement
description: Run a structured self-improvement audit of the trading system — revisit the architecture, hunt for bugs, vulnerabilities, gaps, and inconsistencies that the (green) test suite misses, file them as F-0xx findings in docs/architecture/findings*.md, and refresh the architecture docs to match the code. Use whenever the user asks to "revisit the codebase", "re-evaluate the code", "find bugs/vulnerabilities/gaps/inconsistencies", "run a findings pass", "audit the system", "do a health/quality pass", "check what the tests miss", or asks for another phase of the findings review — even if they don't say "audit" explicitly. Also use when the user asks to update or verify the architecture docs against the code, since doc-refresh is the closing phase of this workflow.
---

# Self-Improvement Audit

A repeatable deep-audit workflow for this repo. It exists because **a green test
suite proves the code does what the tests say — not that the system makes an
honest number**. The audit hunts the class of defect tests structurally miss:
look-ahead bias, silent fallbacks, metrics computed three different ways,
controls that are wired but never fire, and docs that describe a system that no
longer exists.

**North star:** the goal of this project is to make profit in paper trading and
prove it with a trustworthy track record (the Phase 18.5/19 go/no-go gates on
OOS Sharpe). Every finding is judged by one question — *does this make the
number the operator acts on less honest, or the system less likely to survive
an unattended day?* Findings that don't move that needle are Low or not filed.
"Don't overdo it" is a standing instruction: a flood of nitpicks buries the
three findings that matter.

## Operating rules (non-negotiable)

- **Verify before filing.** Never file a finding on a subagent's word alone.
  Re-read the exact lines in the main session; grep for the disproof. Agents
  return *candidates*, the main session returns *findings*.
- **Read-only until the filing phase.** The audit changes no source code. Fixes
  are a separate, later decision by the user.
- **Consume subagent summaries only.** Never read subagent `.output` transcript
  files via shell — they overflow context. Feed agents detailed prompts and
  require a compact structured return instead.
- **Secrets stay dark.** Kite api_key/secret and the Slack webhook must never
  be printed, logged, committed, or quoted in findings. If a finding *is about*
  a secret leak, describe the sink, not the value.
- **Commit and push at each phase wrap-up** (origin/main, not local-only) —
  standing user rule.
- **Real-money execution (F-005) is suspended indefinitely.** Never file a
  finding whose "fix" is wiring live orders.

## The six phases

Create a task per phase and work them in order. Do not start Phase 2 before
Phase 1's exclusion list exists — agents without it re-discover old findings.

### Phase 1 — Orientation & baseline

1. Read the current findings file(s) in `docs/architecture/` (`findings.md`,
   `findings_second_phase.md`, …). Build two lists:
   - **Exclusion list** — every already-filed finding (one line each: ID,
     file:line, one-sentence claim). This goes verbatim into every agent prompt.
   - **Next F-number** — findings numbering is global and monotonic across all
     findings files; continue from the highest existing F-0xx.
2. Skim `git log --oneline -30` for what changed since the last pass — recent
   diffs are the richest hunting ground.
3. Run the health baseline: `pytest -q`, `ruff check .`, `mypy src/`. Record
   results. A green baseline is the *premise* of the audit (we hunt what tests
   miss), a red one is itself the first finding.
4. Decide the domain split for Phase 2 (see the catalog below) and write one
   prompt per domain **before** dispatching anything.

### Phase 2 — Parallel domain audits (subagents)

Dispatch one read-only agent per domain, all in a single message. Build each
prompt from `references/subagent-prompt-template.md` — the template encodes the
elements that made agents productive in past passes: pre-formed hypotheses to
chase, an explicit file list, the exclusion list, hard read-only constraints, a
≤6-finding cap, and a structured return format.

Default domain split (adjust to where recent commits landed):

| Agent | Domain | Prime hunting ground |
|---|---|---|
| A | Backtest / ML validation | `backtest/`, `ranking/` — leakage, embargo, label horizon, Sharpe conventions |
| B | Strategy / decision math | `strategy/` — sizing, calibration, budget caps, dead parameters |
| C | Jobs / ops / lifecycle | `jobs/`, `ops/` — idempotency, clock, run-status blind spots, silent fallbacks |
| D | Data / paper accounting / UI | `data/`, `paper/`, `ui/` — contract mismatches, P&L definitions, label honesty |

Pre-formed hypotheses per domain live in `references/hypothesis-catalog.md` —
read it while writing prompts; it is the distilled market-knowledge of every
prior pass (what High-severity looks like *in this system*).

**Fallback:** if agent dispatch is rejected (session cap) or agents come back
empty, run the same domain sweeps inline in the main session using the same
hypothesis catalog. The method survives without subagents; only the parallelism
is lost.

### Phase 3 — Verify, synthesize, file

For every candidate an agent returned:

1. **Re-verify against source.** Read the cited lines. Grep for the mechanism
   that would make the claim false (a guard, a caller that compensates, a test
   that pins the behavior). Classify: CONFIRMED (you saw it), or drop it.
2. **De-duplicate.** Agents independently converge on real bugs — same-line
   candidates from two agents are one finding (note the convergence; it's
   evidence of severity). A candidate matching an existing F-0xx becomes a
   "2nd-pass note" appended to that finding, not a new number.
3. **Re-grade severity yourself.** Agents over-grade. Calibration for this
   system:
   - **High** — corrupts or flatters the go/no-go metric (equity curve, OOS
     Sharpe, calibration), or can silently lose/duplicate paper trades.
   - **Med** — a control that silently doesn't work (gate never fires, guard
     not wired, block invisible to run-status), or a number the operator reads
     that is computed wrong/misleadingly.
   - **Low** — debt, misnomers, doc drift, hygiene with a bounded blast radius.
4. **File** in the findings file using the exact format in
   `references/findings-format.md` (entry template, category taxonomy
   VULN/GAP/INACC/DEBT/RISK, summary tables, audit-coverage table).
5. **Record negative results too.** The coverage table gets a "Verified sound"
   column — what was checked and found *correct* (contract matches, gate math
   sound, no look-ahead in features). This is what makes the next pass cheaper
   and the current one credible.

Commit and push the findings file at the end of this phase.

### Phase 4 — Architecture-doc refresh

The findings pass always surfaces doc drift; fix it while the knowledge is hot.
The doc set (`docs/architecture/00`–`08`) must let a future session debug the
system without re-reading all the code — that is its acceptance test.

1. Grep the docs for names of things that recently changed (new jobs, moved
   packages, renamed steps, new CLI commands) and for load-bearing counts
   (test-file count, page count, job count).
2. Verify every correction against source before writing it — the doc refresh
   has the same evidence bar as the findings. Command tables come from
   `@app.command` grep, not memory; module maps from `ls`, not recollection.
3. Update diagrams, not just prose: the daily-lifecycle flowchart (00), the
   decision funnel (00), the job sequence diagram and per-job flowcharts (07).
   A flow that exists in code but not in a diagram is invisible to the next
   debugging session.
4. Cross-link open findings from the docs' "Robustness notes" sections
   (e.g. "run_status doesn't track open-fills — F-062") so the docs and the
   findings ledger stay one system.
5. Historical notes describing *fixed* behavior stay (struck through with the
   fix date) — the doc set is also the system's memory of its own mistakes.

### Phase 5 — Verify baseline health

Re-run `pytest -q`, `ruff check .`, `mypy src/`. The audit is read-only for
`src/`, so anything newly red means environment drift or an earlier mistake —
investigate before wrapping up; never report the pass complete over a red
baseline.

### Phase 6 — Commit & push

Commit the doc refresh (separate commit from the findings; `docs(arch):` /
`docs(findings):` prefixes) and push to origin/main. Update any memory files
that the audit proved stale (e.g. a feature recorded as "pending" that the
code shows implemented).

## Final report to the user

Lead with the counts and the Highs: "N findings (X High, Y Med, Z Low); the
Highs all attack <theme>". Then the two or three findings that change what the
operator should do next, in plain sentences. Then what was verified sound. Keep
finding IDs attached to every claim so the report cross-references the ledger.

## Reference files

- `references/findings-format.md` — the exact findings-file entry template,
  category taxonomy, status lifecycle, and summary/coverage tables. Read before
  Phase 3.
- `references/subagent-prompt-template.md` — the prompt skeleton for Phase 2
  domain agents, with a worked example. Read before dispatching.
- `references/hypothesis-catalog.md` — per-domain pre-formed hypotheses (the
  market-knowledge distillation of prior passes). Read while writing Phase 2
  prompts; use directly for inline sweeps if agents are unavailable.
