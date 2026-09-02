# YouTube Charts monitor

Watches the daily global view counts for specific artists on
[charts.youtube.com](https://charts.youtube.com) and sends a Discord message when
a new day of data appears or when already-published numbers get revised.

Currently tracking **Michael Jackson** (`/m/09889g`) and **Taylor Swift** (`/m/0dl567`).

## How the data is obtained

There is no public API. The site is a JavaScript app that calls YouTube's internal
"InnerTube" endpoint, and this project calls that same endpoint directly:

```
POST https://charts.youtube.com/youtubei/v1/browse?alt=json
```

The one non-obvious detail — the thing that makes or breaks this — is that the
parameters are a **URL-encoded string nested inside the JSON body**, under the key
`query`. They are not URL parameters and not JSON fields:

```json
{
  "context": { "client": { "clientName": "WEB_MUSIC_ANALYTICS", "clientVersion": "2.0" } },
  "browseId": "FEmusic_analytics_insights_artist",
  "query": "perspective=ARTIST&entity_params_entity=ARTIST&artist_params_id=%2Fm%2F09889g&date_params_start_time=...&date_params_end_time=...&date_params_interval=DAY"
}
```

**If you get that wrong, the API still returns HTTP 200** — but with the default
US weekly Top-Artists chart instead of what you asked for. It looks like success.
`monitor.py` guards against this by rejecting any response with no `dates` key.

No API key, no login, and no browser are required.

## Important: the data lags 2–3 days

The newest available day is roughly 2–3 days behind today. As of 2026-09-02 the
latest published day was 2026-08-30. So "a new day was added" does **not** mean
"yesterday's numbers are in" — it means the trailing edge advanced by one day.

Note also that the *artists chart* on the site is weekly-only. Daily numbers exist
only on the per-artist insights page, which is what this monitors.

## Setup

### 1. Create the repository

```bash
cd "/Users/adamdunlap/Desktop/Kalshi-Coding/Youtube-Charts"
git init && git add . && git commit -m "YouTube Charts monitor"
gh repo create yt-charts-monitor --private --source=. --push
```

### 2. Add the Discord webhook as a secret

```bash
gh secret set DISCORD_WEBHOOK_URL
```

Paste the webhook URL when prompted. **Do not put it in any file in this repo.**

### 3. Enable scheduled workflows

Scheduled runs on a fresh repo sometimes need one manual kick. Go to the
**Actions** tab, select **Watch (safety net)**, and click **Run workflow**.

## The two workflows

| Workflow | Cadence | Purpose |
|---|---|---|
| `watch.yml` | every 10 min, always on | Safety net — guarantees nothing is missed. Also records *when* updates land. |
| `sprint.yml` | manual, or scheduled window | Polls every 30 seconds and exits on detection. |

### Why two

GitHub's cron floor is one minute, so cron alone can never give you 30-second
precision. The sprint workflow gets around this: cron decides only when the job
*starts*, then the script loops internally at 30s. Running that around the clock
would mean ~2,880 API hits a day, which risks getting rate-limited. So the sprint
is pointed at a narrow window and the cheap watcher covers the rest of the day.

### Turning on the sprint

After a few days, look at `history.jsonl` — each line is timestamped in UTC:

```bash
grep new_day history.jsonl
```

Once a consistent hour emerges, uncomment the `schedule:` block in
`.github/workflows/sprint.yml` and set the cron to ~30 minutes before it.
Cron in GitHub Actions is always UTC.

## Running locally

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python3 monitor.py                                  # one check
SPRINT_DURATION_SECONDS=300 python3 monitor.py      # poll 30s for 5 min
```

The first run on a clean checkout records a baseline and deliberately sends no
alert, so it can't fire a false "new day" for data that was already there.

## Adding another artist

Find the artist's ID on their charts URL — `charts.youtube.com/artist/%2Fm%2F09889g`
means the ID is `/m/09889g`. Add it to `ARTISTS` in `monitor.py`.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The poller, change detection, and Discord notifier |
| `state.json` | Last-seen values (committed — this is the memory between runs) |
| `history.jsonl` | Append-only log of every detected change, with UTC timestamps |

## Cost

$0. A public repo has unlimited Actions minutes; this shape also fits inside the
2,000 min/month free allowance for a private repo.

## If it breaks

This depends on an undocumented internal endpoint, which YouTube can change
without warning. The likely failure is the payload format changing, which
`monitor.py` reports as `API ignored parameters`. Re-derive the payload by opening
a chart page in Chrome with DevTools → Network, finding the `browse` request, and
copying its request body.
