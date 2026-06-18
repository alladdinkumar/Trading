# Operations runbook

> Phase 17 — daily-job scheduling, logging, and notifications.

## First-time setup

### 1. Slack incoming webhook

1. Open https://api.slack.com/apps → **Create New App** → "From scratch".
2. Name: `trading-bot`. Workspace: pick yours.
3. Sidebar → **Incoming Webhooks** → toggle **On**.
4. Click **Add New Webhook to Workspace** → pick a private channel (e.g. `#trading-bot`).
5. Copy the webhook URL (`https://hooks.slack.com/services/T.../B.../...`).
6. Add to `.env`:
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   ```
7. Verify:
   ```
   uv run trading notify-test
   ```
   You should see a message in the Slack channel AND a Windows toast within ~5 seconds.

### 2. Import Task Scheduler entries

For each of the 12 XML files in `docs/scheduler/`:

```cmd
schtasks /Create /XML "docs\scheduler\trading_remind_pre_open_kite.xml" /TN "trading_remind_pre_open_kite"
```

Or bulk-import in PowerShell from the repo root:

```powershell
Get-ChildItem docs\scheduler\trading_remind_*.xml | ForEach-Object {
    $name = $_.BaseName
    schtasks /Create /XML $_.FullName /TN $name /F
}
```

Open **Task Scheduler** (Win+R, `taskschd.msc`) and verify all 12 tasks appear in the root library and show status **Ready**.

### 2b. Import the unattended jobs

The reminder glob above intentionally excludes the two tasks that **run commands
unattended** (not reminders). Import them once each:

```cmd
schtasks /Create /XML "docs\scheduler\trading_weekly_train.xml" /TN "trading_weekly_train"
schtasks /Create /XML "docs\scheduler\trading_daily_unattended.xml" /TN "trading_daily_unattended"
```

- `trading_weekly_train` — Sunday 10:00 IST: rolling retrain + weekly review +
  data-retention prune (see *Data retention* below).
- `trading_daily_unattended` — Mon–Fri 10:00 IST: broker-free pre-open spine
  gap-filler (F-032); skips any day the operator already covered.

Both have `StartWhenAvailable` set, so a missed window (machine off) is caught up
on the next wake. Unlike the reminders, a failure here is recorded to
`data/logs/failures.log` and the task is marked failed.

### 3. Confirm "Run whether user is logged on or not"

For each task: right-click → **Properties** → **General** tab → tick **Run whether user is logged on or not** if you want reminders even while locked. (The default `InteractiveToken` in the XML only fires when you're logged in.)

## Daily workflow (what you'll see)

A normal trading Monday:

```
08:30  🔔 Pre-open step 1/4 — Run /kite-snapshot in Claude Code
08:35  🔔 Pre-open step 2/4 — Then trading pre-open 2026-05-25
08:40  🔔 Pre-open step 3/4 — Run /analyst in Claude Code
08:45  🔔 Pre-open step 4/4 — Finally trading brief compile 2026-05-25
08:55  🔔 IEP step 1/2 — Run /kite-quotes-snapshot
09:00  🔔 IEP step 2/2 — Then trading pre-open-iep --date 2026-05-25
12:25  🔔 Mid-day step 1/3 — Run trading mid-day 2026-05-25
12:30  🔔 Mid-day step 2/3 — Run /kite-quotes-snapshot
12:35  🔔 Mid-day step 3/3 — Then trading mid-day 2026-05-25 --apply
16:05  🔔 Post-close step 1/3 — Run trading post-close 2026-05-25
16:10  🔔 Post-close step 2/3 — Run /kite-quotes-snapshot
16:15  🔔 Post-close step 3/3 — Then trading post-close 2026-05-25 --apply
```

Weekends and NSE holidays: reminders are silent. Task Scheduler still fires the entry, but the holiday gate in `fire_reminder` short-circuits with a log-only entry — no Slack message reaches the channel.

## Failure alerts

When any of `trading pre-open`, `trading pre-open-iep`, `trading mid-day`, `trading post-close` raises, you'll see one extra Slack post:

```
❌ pre_open FAILED  (2026-05-25, exit 1)
```

Followed by a code-fenced block with the Python traceback and the last 20 lines of `data/logs/pre_open_2026-05-25.log`.

## Logs

- **Location:** `data/logs/{job}_YYYY-MM-DD.log`
- **Rotation:** daily at midnight (local time)
- **Retention:** 60 days
- **Compression:** gzip on rotation (`*.log.gz`)

To inspect a failed run, open the corresponding file. To trace a specific symbol, `grep` for the ticker.

## Data retention

`data/raw/<YYYY-MM-DD>/` snapshot dirs and the `news_items` table are append-only
and would otherwise grow without bound (F-013). The Sunday `weekly_train` job
prunes them automatically, but you can also run it by hand:

```
uv run trading prune                 # dry-run: report the stale tail, delete nothing
uv run trading prune --apply         # actually delete
uv run trading prune --raw-days 14 --news-days 180 --apply   # custom windows
```

Defaults keep **30 days** of `raw/<date>/` dirs and **365 days** of `news_items`
rows. Only directories named like an ISO date are eligible — a stray file or the
`static/` helper dir is never touched. The derived `sentiment_daily` rollups are
kept regardless (they feed the live 30-day health scorer), so pruning old raw
news is lossless for the live path.

## Holiday list maintenance

The NSE holiday gate uses `nsepython.holiday_master()` first and falls back to `data/static/nse_holidays_<year>.json`. The bundled JSON is a best-effort seed — refresh it annually:

```
uv run python -c "from nsepython import holiday_master; import json; print(json.dumps(holiday_master()))" > nse_raw.json
```

…then translate into the bundled JSON format (see existing `data/static/nse_holidays_2026.json`). Add a new file for each year — the calendar module looks up `nse_holidays_<year>.json` automatically.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| No Slack messages arriving | `SLACK_WEBHOOK_URL` not in `.env`; or `.env` not being loaded | `uv run trading notify-test`; check `.env` parses; check workspace channel |
| No Windows toasts | Focus Assist on; or non-Windows host; or plyer install broken | Toggle Focus Assist; `uv run python -c "from plyer import notification; notification.notify(title='t', message='m')"` |
| Task Scheduler entry "did not run" | `LogonType` mismatch; uv not on PATH | Properties → General → tick "Run whether user is logged on or not"; use full path `C:\Users\<you>\.local\bin\uv.exe run trading ...` in the XML if needed |
| `trading remind` ImportError | uv environment out of date | `uv sync` from repo root |
| Failure alert formatting broken | Slack rate-limit (too many alerts in burst) | Wait a minute and resend; review log files directly |

## Manual verification checklist

1. `uv run trading notify-test` → Slack + toast both arrive.
2. Trigger one Task Scheduler entry manually (right-click → Run) → reminder arrives.
3. Deliberately break a job (`mv data/raw/<today>/holdings.json data/raw/<today>/holdings.bak`) → run `trading pre-open <today>` → `❌ pre_open FAILED` Slack post arrives with traceback.
4. Leave the laptop on overnight on a weekday → 08:30 reminder fires automatically.
5. Spot-check on a Sunday: Task Scheduler still fires but no Slack post arrives; `data/logs/<job>_*.log` shows `INFO skipped: not a trading day`.
