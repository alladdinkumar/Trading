# Findings-file format

The findings files under `docs/architecture/` are the system's defect ledger.
They are written to be *actionable months later by a session with no memory of
the audit* — every entry carries its own evidence.

## Numbering and files

- Numbering is **global and monotonic** across all findings files. A new pass
  continues from the highest existing F-0xx anywhere (`findings.md`,
  `findings_second_phase.md`, …); it never restarts at F-001.
- A new major pass gets its own file (`findings_<phase>_phase.md`) with the
  same structure; small follow-ups append to the existing file.

## File skeleton

```markdown
# Findings — <pass name>

> One-paragraph framing: what this pass covered, when, and the headline
> (N findings: X High, Y Med, Z Low).

## How this pass was run
<method: domain split, agent/inline, verification discipline. Enough that the
next pass can replicate or deliberately deviate.>

## Executive summary
<The story of the findings, not a list: what theme the Highs share, what the
Med band means operationally, what was verified sound. 2–4 paragraphs.>

## Breakdown

| Severity | Count | IDs |
|---|---|---|

| Category | Count |
|---|---|

## Audit coverage

| Domain | Files/areas | Status | Verified sound |
|---|---|---|---|
<one row per domain — including domains that produced zero findings. The
"Verified sound" column records negative results: what was checked and found
correct. This is what keeps the next pass from re-auditing solved ground.>

## Findings
<entries, ordered by ID>
```

## Entry template

```markdown
### F-0xx — <Title stating the defect, not the topic> (<CATEGORY>, <Sev>, <area>) — <Status>

**Where:** `path/to/file.py:123` (+ related sites)

**What:** The defect in 2–5 sentences. State the mechanism, not just the
symptom: *what* is wrong, *why* the code does it, and the concrete failure
scenario (inputs/state → wrong output). Quote the load-bearing line(s).

**Impact:** Who reads the wrong number / what breaks, and how it connects to
the north star (go/no-go honesty, unattended survival). This is what justifies
the severity.

**Fix direction:** 1–3 sentences. A direction, not a patch — the audit doesn't
change code.
```

## Category taxonomy

| Category | Meaning | Typical example |
|---|---|---|
| `VULN` | Correctness/security defect — the code does the wrong thing | Zero embargo between train/test folds; webhook URL logged on failure |
| `GAP` | Something that should exist and doesn't | No reminder slot for the one block that opens trades; unmapped sectors in the live universe |
| `INACC` | A number or label the operator reads is wrong/misleading | "Total P&L" tile that only sums open positions; three inconsistent Sharpe definitions |
| `DEBT` | Works today, will bite later | Dead parameters still accepted; misnomer directories; copy-pasted constants |
| `RISK` | Correct code, dangerous behavior under plausible conditions | Silent fallback marking positions at a stale price; host-clock staleness check |

## Severity calibration (this system)

- **High** — the finding can corrupt or flatter the number the go/no-go rests
  on (portfolio_snapshots equity, OOS Sharpe, walk-forward results,
  calibration inputs), or can silently lose/duplicate/mis-price paper trades.
- **Med** — a *control that silently doesn't work* (a gate that can never
  fire, a guard that isn't wired, a lifecycle block invisible to run-status)
  or a *number the operator can't trust* (mislabeled metric, inconsistent
  definition) that doesn't directly feed the gate.
- **Low** — bounded-blast-radius debt: naming, doc drift, hygiene, dead code.

Two agents independently flagging the same line is evidence *for* severity;
an agent grading High is not — re-grade everything yourself. Common downgrades:
a leaked credential that is post-only and lives in a gitignored local log is
Med, not High; a dead parameter is Med (operator believes a lever exists) or
Low (internal only), not High.

## Status lifecycle

`Open` → `✅ Fixed <date> — <how, one line>` (strike through the original claim
with `~~…~~` and keep it — the ledger is also the memory of past mistakes).
Other terminal states: `Suspended (<why>)`, `Won't fix (<why>)`,
`Folded into F-0yy`.

When a later pass re-confirms or extends an existing finding, append a
`**2nd-pass note (<domain> audit):**` block to the existing entry instead of
filing a new number.
