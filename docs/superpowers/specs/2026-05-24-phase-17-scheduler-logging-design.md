# Phase 17 — Task Scheduler + logging (design)

**Date:** 2026-05-24
**Status:** approved, ready for plan
**Related:** [PROGRESS.md §17](../../../PROGRESS.md), [trading-system-design §17 / §11](../2026-05-11-trading-system-design.md)

---

## 1. Goal

Phase 17 wraps the four daily jobs (`pre_open`, `pre_open_iep`, `mid_day`, `post_close`) with:

1. **Scheduled reminders** delivered to Slack + Windows toast that prompt the user to run each step of the daily workflow.
2. **Rotating loguru logs** under `data/logs/{job}_YYYY-MM-DD.log`.
3. **Automatic failure alerts** posted to the same channels when any job's `main()` raises.
4. **An operations runbook** for first-time setup of Slack, Task Scheduler, and the env file.

The deliverable unblocks Phase 18 (live paper-trading) by giving the human operator a reliable prompt every trading day plus an audit trail and immediate notification when something breaks.

## 2. Scope

### In

- `src/trading/ops/` subpackage: `notify.py`, `calendar.py`, `logging_setup.py`, `runner.py`.
- Two new CLI subcommands: `trading remind --slot <name>` and `trading notify-test`.
- Loguru wiring (`configure_logging` + two-line shim) into the four existing job entrypoints — no changes to the underlying job logic.
- Twelve Windows Task Scheduler entries exported as `.xml` under `docs/scheduler/`.
- NSE holiday gate via `nsepython.nse_holiday_list()` with bundled JSON fallback at `data/static/nse_holidays_2026.json`.
- Operations runbook at `docs/operations.md`.

### Out

- `weekly_train` and `monthly_sip` schedules (deferred; Phase 16 is paused and the SIP cron will land with its own mini-phase).
- Unattended execution of any Python job. The model is **reminder-driven**: Task Scheduler only fires Slack/toast pings; the user runs the actual commands manually.
- SDK fallback for live quotes. Mid-day / post-close / IEP continue to require the `/kite-quotes-snapshot` Claude Code skill.
- E-mail / SMS / PagerDuty integrations. Slack + Windows toast only.

### Non-goals (explicit)

- **No retry logic on failure.** The user investigates and re-runs manually.
- **No success-path Slack chatter.** Success runs are silent on Slack; the rotating log file is the audit trail.
- **No dynamic schedule.** The schedule is hard-coded in `SCHEDULE: dict[str, ReminderSlot]`. Changing times means editing the dict and re-exporting the XML.

## 3. User-facing workflow

A normal Monday looks like this from the operator's perspective:

```
08:30  Slack: 🔔 Pre-open step 1/4 — Run `/kite-snapshot` in Claude Code
08:35  Slack: 🔔 Pre-open step 2/4 — Then `trading pre-open 2026-05-25`
08:40  Slack: 🔔 Pre-open step 3/4 — Run `/analyst` in Claude Code
08:45  Slack: 🔔 Pre-open step 4/4 — Finally `trading brief compile 2026-05-25`
08:55  Slack: 🔔 IEP step 1/2 — Run `/kite-quotes-snapshot`
09:00  Slack: 🔔 IEP step 2/2 — Then `trading pre-open-iep --date 2026-05-25`
12:25  Slack: 🔔 Mid-day step 1/3 — Run `trading mid-day 2026-05-25`
12:30  Slack: 🔔 Mid-day step 2/3 — Run `/kite-quotes-snapshot`
12:35  Slack: 🔔 Mid-day step 3/3 — Then `trading mid-day 2026-05-25 --apply`
16:05  Slack: 🔔 Post-close step 1/3 — Run `trading post-close 2026-05-25`
16:10  Slack: 🔔 Post-close step 2/3 — Run `/kite-quotes-snapshot`
16:15  Slack: 🔔 Post-close step 3/3 — Then `trading post-close 2026-05-25 --apply`
```

On a Saturday, Sunday, or NSE holiday, no reminders fire at all (Task Scheduler skips weekends natively; the holiday gate inside `fire_reminder` short-circuits with `info` log only — no Slack).

If a Python job raises mid-run (after the user pastes a command), one extra Slack post appears at the moment of failure:

```
❌ pre_open FAILED  (2026-05-25, exit 1)
```
+ a code-fenced block containing the last 20 lines of `data/logs/pre_open_2026-05-25.log` and the Python traceback.

## 4. Architecture

### 4.1 Module layout

```
src/trading/ops/
├── __init__.py
├── notify.py            # Slack webhook + plyer toast primitive
├── calendar.py          # is_trading_day(date)
├── logging_setup.py     # loguru sinks (file + stderr + Slack)
└── runner.py            # SCHEDULE dict + fire_reminder(slot)
```

### 4.2 `ops/notify.py`

```python
def post_slack(text: str) -> bool: ...           # POST to SLACK_WEBHOOK_URL; False on any failure
def post_toast(title: str, message: str) -> bool: ...  # plyer.notification.notify; no-op on non-Windows
def notify(
    level: Literal["info", "warn", "error"],
    title: str,
    body: str = "",
) -> None: ...
```

`notify` is the single public entrypoint. It dispatches to both channels best-effort, formats the Slack payload as:

```
{emoji} *{title}*
{body if body else ""}
```

where emoji is `🔔` / `⚠️` / `❌`. Body is rendered inside a fenced code block when it contains newlines or when level is `error`. Windows toast shows `title` + `body[:200]`.

`SLACK_WEBHOOK_URL` is read from `.env` via the existing `trading.config` loader; missing value warns once per process and continues.

### 4.3 `ops/calendar.py`

```python
@functools.cache
def nse_holidays(year: int) -> frozenset[date]: ...
def is_trading_day(d: date) -> bool: ...
```

`nse_holidays` calls `nsepython.nse_holiday_list()` once per year. On any error, falls back to `data/static/nse_holidays_{year}.json` (a hand-curated list seeded for 2026). If both fail, returns an empty frozenset and logs ERROR (best-effort: Mon-Fri is still respected).

`is_trading_day(d)` returns `False` for Sat/Sun OR any date in `nse_holidays(d.year)`.

### 4.4 `ops/logging_setup.py`

```python
def configure_logging(job: str, *, slack_on_error: bool = True) -> None: ...
```

Idempotent within a process (guarded by a module-level `_configured: set[str]`). Removes loguru's default handler then adds:

1. **File sink** — `data/logs/{job}_YYYY-MM-DD.log`, level INFO+, daily rotation, 60-day retention, gzip compression on rotation.
2. **stderr sink** — level INFO+, human format (`{time:HH:mm:ss} | {level: <8} | {message}`).
3. **Slack sink** (when `slack_on_error=True`) — level ERROR+, calls `ops.notify.notify("error", title=f"{job} FAILED", body=...)`. The body is built from the loguru record's `exception` field plus the last 20 lines tailed from the current file sink.

### 4.5 `ops/runner.py`

```python
@dataclass(frozen=True)
class ReminderSlot:
    when: str        # "HH:MM" IST, informational only
    title: str
    body: str = ""

SCHEDULE: dict[str, ReminderSlot] = { ... }    # 12 entries; see §5

def fire_reminder(slot: str, today: date | None = None) -> None: ...
```

`fire_reminder`:

1. Looks up `SCHEDULE[slot]`; raises `KeyError` (becomes CLI exit 2) on unknown slot.
2. Resolves `today` to current date in `Asia/Kolkata` if not provided.
3. Calls `is_trading_day(today)`. If False, logs `INFO` "skipped: not a trading day" and returns (no Slack on holidays — we trust the operator to enjoy their day off).
4. Substitutes `<date>` token in `body` with `today.isoformat()`.
5. Calls `ops.notify.notify("info", slot.title, slot.body)`.

### 4.6 CLI surface

In `src/trading/cli.py`:

```python
@app.command()
def remind(slot: str = typer.Option(..., help="Slot name from ops.runner.SCHEDULE")) -> None:
    """Fire a single reminder. Used by Windows Task Scheduler entries."""

@app.command(name="notify-test")
def notify_test() -> None:
    """Post a sanity-check notification to both Slack and Windows toast."""
```

### 4.7 Job entrypoint shim

Each of `jobs/pre_open.py`, `jobs/pre_open_iep.py`, `jobs/mid_day.py`, `jobs/post_close.py` gets a two-line guard around its existing `main()`:

```python
def main() -> None:
    configure_logging("pre_open")
    try:
        _main()                # current body, unchanged
    except Exception:
        logger.exception("pre_open failed")
        raise
```

`logger.exception` triggers the Slack sink. `raise` preserves the exit code for the `.bat` caller and any future Task Scheduler entry.

## 5. The schedule (single source of truth)

| Slot | Time IST | Title | Body |
|---|---|---|---|
| `pre_open_kite` | 08:30 | 🔔 Pre-open step 1/4 | Run `/kite-snapshot` in Claude Code |
| `pre_open_scan` | 08:35 | 🔔 Pre-open step 2/4 | Then `trading pre-open <date>` |
| `pre_open_analyst` | 08:40 | 🔔 Pre-open step 3/4 | Run `/analyst` in Claude Code |
| `pre_open_compile` | 08:45 | 🔔 Pre-open step 4/4 | Finally `trading brief compile <date>` |
| `iep_quotes` | 08:55 | 🔔 IEP step 1/2 | Run `/kite-quotes-snapshot` |
| `iep_filter` | 09:00 | 🔔 IEP step 2/2 | Then `trading pre-open-iep --date <date>` |
| `mid_day_prepare` | 12:25 | 🔔 Mid-day step 1/3 | Run `trading mid-day <date>` |
| `mid_day_quotes` | 12:30 | 🔔 Mid-day step 2/3 | Run `/kite-quotes-snapshot` |
| `mid_day_apply` | 12:35 | 🔔 Mid-day step 3/3 | Then `trading mid-day <date> --apply` |
| `post_close_prepare` | 16:05 | 🔔 Post-close step 1/3 | Run `trading post-close <date>` |
| `post_close_quotes` | 16:10 | 🔔 Post-close step 2/3 | Run `/kite-quotes-snapshot` |
| `post_close_apply` | 16:15 | 🔔 Post-close step 3/3 | Then `trading post-close <date> --apply` |

Task Scheduler XML entries (one per row) live under `docs/scheduler/`. Filenames mirror the slot name: `trading_remind_pre_open_kite.xml`, etc.

## 6. Data flow

### 6.1 Reminder firing

```
08:30 IST, Mon–Fri — Task Scheduler entry "trading_remind_pre_open_kite"
  ↓
cmd /c "cd D:\Projects\Trading && uv run trading remind --slot pre_open_kite"
  ↓
trading.cli.remind() → ops.runner.fire_reminder("pre_open_kite")
  ↓
ops.calendar.is_trading_day(today_ist())
    ├─ False → log INFO "skipped: holiday" → exit 0  (no Slack)
    └─ True  → ops.notify.notify("info", "🔔 Pre-open step 1/4", "Run /kite-snapshot in Claude Code")
                  ├─ post_slack(...)   # best-effort
                  └─ post_toast(...)   # best-effort
                exit 0
```

### 6.2 Job execution + failure path

```
User pastes `uv run trading pre-open 2026-05-25` after the reminder
  ↓
configure_logging("pre_open")
    ├─ file sink → data/logs/pre_open_2026-05-25.log
    ├─ stderr sink → terminal
    └─ Slack sink (ERROR+ only)
  ↓
try: run_pre_open(date=2026-05-25, ...)
    ├─ success → exit 0, silent on Slack, log persists for 60 days
    └─ Exception
         ↓
         logger.exception("pre_open failed")
            ↓
         Slack sink fires once → posts title + traceback + tail of log file
            ↓
         re-raise → exit 1
```

## 7. Error handling

| Failure mode | Behaviour |
|---|---|
| `SLACK_WEBHOOK_URL` env not set | `post_slack` returns False; logs WARN once per process; job continues. |
| Slack POST returns 4xx/5xx or network errors | Caught, logged at WARN; toast still attempted; job continues. |
| `plyer` import fails or notify raises | Caught, logged at WARN; non-Windows hosts silent by default. |
| `nsepython.nse_holiday_list()` raises | Falls back to bundled `data/static/nse_holidays_{year}.json`; logs WARN. |
| Bundled holiday JSON missing | `nse_holidays(year)` returns `frozenset()`; logs ERROR; weekday-only check still works. |
| `configure_logging` called twice for same job in one process | Second call is a no-op (idempotency guard); logs DEBUG. |
| Job raises | Slack ERROR alert with traceback + last 20 log lines; re-raised so `.bat` exit code propagates. |
| Slack sink itself raises | Caught and swallowed inside the sink (never crash the job from the logging layer). |

**Design principle:** the notification layer never crashes the job. Notification is best-effort; the underlying work is the source of truth.

## 8. Testing

| File | Tests | Coverage |
|---|---|---|
| `tests/test_ops_notify.py` | ~10 | Slack payload shape; toast call; missing-env path; 4xx/5xx graceful; non-Windows toast no-op; emoji-by-level. |
| `tests/test_ops_calendar.py` | ~8 | Weekday/weekend; known holiday; year boundary; nsepython stubbed success + failure→fallback; missing fallback file. |
| `tests/test_ops_logging_setup.py` | ~8 | File created at expected path; rotation pattern; retention setting; Slack sink fires only at ERROR+; idempotent on re-call; tail-of-log correctness. |
| `tests/test_ops_runner.py` | ~8 | SCHEDULE coverage (every slot key resolvable); holiday short-circuit; `<date>` substitution; unknown slot raises; today resolution honours IST. |
| `tests/test_cli.py` *(extend)* | ~4 | `trading remind --slot pre_open_kite` happy path; holiday path (mock `is_trading_day`); unknown slot exit code 2; `trading notify-test` happy path. |

All Slack calls are mocked. No `@pytest.mark.live` additions — the manual `trading notify-test` invocation is the human smoke step.

Expected suite total: **~566 + 38 ≈ 604** passing.

## 9. Dependencies

Added to `pyproject.toml` `[project] dependencies`:

```toml
"plyer>=2.1",      # cross-platform notifications (Windows toast)
```

`loguru>=0.7` and `nsepython>=2.95` and `requests>=2.31` are already pinned.

## 10. Operations runbook (`docs/operations.md`)

Sections to write:

1. **Slack setup** — create an Incoming Webhook in workspace, copy URL into `.env` as `SLACK_WEBHOOK_URL`, verify with `uv run trading notify-test`.
2. **Task Scheduler import** — instructions for `schtasks /Create /XML docs\scheduler\<file>.xml /TN "trading_remind_<slot>"` for each of the 12 XML files, plus the prerequisite "Run whether user is logged on or not" toggle.
3. **Holiday list maintenance** — when to refresh `data/static/nse_holidays_{year}.json`; the nsepython API is best-effort so a once-a-year manual update is the safety net.
4. **Log rotation behaviour** — where files live, how rotation triggers, how to inspect a failed run.
5. **Troubleshooting** — common failure modes (env not loaded, webhook revoked, toast suppressed by Focus Assist, Task Scheduler entry disabled).

## 11. Manual verification (item 17.4 in PROGRESS.md)

1. `uv run trading notify-test` → confirm Slack + toast both appear.
2. Import one Task Scheduler XML and trigger it manually via "Run" → confirm Slack ping.
3. Deliberately break a job (e.g. rename `data/raw/<today>/holdings.json`) → run `trading pre-open <today>` → confirm `❌ pre_open FAILED` Slack post + traceback + log tail.
4. Roll forward one weekday: leave the laptop on overnight, verify the 08:30 reminder fires (Slack + toast).
5. Spot-check a Sunday or NSE holiday: Task Scheduler still triggers but no Slack post arrives (holiday gate silent-skips); the log shows `INFO skipped: not a trading day`.

## 12. Out-of-scope items recorded for future phases

- **`weekly_train`** scheduling — returns with Phase 16 (LightGBM ranker).
- **`monthly_sip`** scheduling — its own mini-phase; will wrap `portfolio.allocator.allocate_sip` with a Slack-delivered summary on the 1st of each month.
- **SDK fallback for live quotes** — enables `mid_day` / `post_close` / `pre_open_iep` to run unattended; substantial scope (auth handling, kite-emergency-quotes CLI, removal of quote_snapshot indirection).
- **Holiday-aware schedule for nsepython 2027+** — bundled JSON only covers 2026; refresh needed at year boundary.
- **E-mail / SMS / PagerDuty channels** — Slack alone is acceptable for solo operator. `notify()` accepts arbitrary channels but only Slack + toast are implemented.
