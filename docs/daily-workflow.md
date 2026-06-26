# Daily workflow

The canonical day-in-the-life sequence, pulled from `src/trading/ops/runner.py::SCHEDULE`.
All times are IST. `<date>` = today's ISO date (e.g. `2026-05-25`).

## Once per day (anytime before 08:30)

| Step | Command | Where |
|---|---|---|
| 0 | `mcp__kite__login` → click link, complete browser handshake | Claude Code |

## Pre-open block (08:30 – 08:45)

| Time | Step | Command |
|---|---|---|
| 08:30 | 1/5 | `/kite-snapshot` |
| 08:35 | 2/5 | `trading pre-open --date <date>` |
| 08:38 | 3/5 | `/macro-doctor` *(Kite cross-source gap-fill + reconcile; best-effort)* |
| 08:40 | 4/5 | `/analyst` |
| 08:45 | 5/5 | `trading brief compile --date <date>` |

## IEP block (08:55 – 09:00)

| Time | Step | Command |
|---|---|---|
| 08:55 | 1/2 | `/kite-quotes-snapshot` |
| 09:00 | 2/2 | `trading pre-open-iep --date <date>` |

## Mid-day MTM (12:25 – 12:35)

| Time | Step | Command |
|---|---|---|
| 12:25 | 1/3 | `trading mid-day --date <date>` *(prepare)* |
| 12:30 | 2/3 | `/kite-quotes-snapshot` |
| 12:35 | 3/3 | `trading mid-day --date <date> --apply` |

## Post-close (16:05 – 16:15)

| Time | Step | Command |
|---|---|---|
| 16:05 | 1/3 | `trading post-close --date <date>` *(prepare)* |
| 16:10 | 2/3 | `/kite-quotes-snapshot` |
| 16:15 | 3/3 | `trading post-close --date <date> --apply` |

## Optional post-close recap

```
trading brief assemble-context --date <date> --mode post_close
/analyst        # generates post_close_recap.md
```

## Checking what ran (half-run detection)

`trading status [--date <date>]` (default: today IST) reports which checkpoints
of the day's flow have run, inferred from the artifacts each step leaves on
disk. It's time-aware — a step shows ❌ **missing** only once its slot time has
passed, · **pending** before then, ✅ **done** once its artifact exists. The
command exits non-zero if any *due* step is missing, so it can gate a script or
a "did I miss a block?" check:

```
trading status                 # today
trading status --date 2026-06-17
```

## Notes

- Weekends and NSE holidays: Task Scheduler still fires reminders, but the
  holiday gate in `fire_reminder` short-circuits — no Slack message goes out
  and no job runs.
- For setup (Slack webhook, Task Scheduler import, logging, troubleshooting)
  see `docs/operations.md`.
