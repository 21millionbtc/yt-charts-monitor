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

## Important: the same date returns different numbers by region

YouTube serves different figures for the *same date* depending on where the
request comes from. Measured between a home connection and a GitHub Actions
runner on 2026-09-02:

| Date | GitHub runner | Home IP | Delta |
|---|---|---|---|
| 2026-08-20 | 16,749,431 | 16,823,787 | +74,356 |
| 2026-08-27 | 18,514,459 | 18,213,170 | -301,289 |
| 2026-08-28 | 20,190,433 | 20,052,686 | -137,747 |

Up to ~1.7%, in both directions, on dates more than a week old — so this is not
recent days still settling. It means **a single differing reading is not evidence
of a revision.**

Three consequences:

1. **Revision alerts are off by default.** Runners move between regions from one
   run to the next, so a "revision" is usually just a different machine asking.
   Revisions are still written to `history.jsonl` every time — the flag only
   controls whether they interrupt you. Turn them on by setting
   `ALERT_ON_REVISIONS: "1"` in the workflow's `env:` block.
2. Even when enabled, revisions require **confirmation**: the same new value must
   appear on two consecutive polls. `state.json` holds unconfirmed values under
   `pending`. This filters single flaps, though not a sustained run of polls from
   the same non-baseline region.
3. **New-day alerts fire immediately and are unaffected by any of this.** The
   trailing edge advancing is unambiguous, and `max_alerted_date` ensures a given
   day is never announced twice if it flaps backwards.

New-day detection — the thing this exists for — is reliable. Revision detection
through this API is not, which is why the two are treated differently.

Do not run `monitor.py` locally and commit the resulting `state.json` — your home
IP's numbers will differ from the runner's and the next scheduled run will report
a spurious revision.

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

## Running 24/7 at 30-second intervals

GitHub Actions gives you 30-second polling *inside a window*, not around the
clock. Cron's floor is 5 minutes, and while you could chain 6-hour jobs to cover
a full day, using Actions as an always-on server is against GitHub's Actions
policy and risks the repo being disabled. Don't.

For genuine 24/7, run the container. `SPRINT_DURATION_SECONDS=-1` means "poll
forever, never exit":

```bash
docker build -t yt-charts-monitor .
docker run -d --restart=always \
  -e DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  -v yt-charts-state:/app/data -e STATE_DIR=/app/data \
  --name yt-charts yt-charts-monitor
```

The loop is failure-tolerant: a bad poll backs off exponentially (capped at 30
min) and the process keeps going, so a network blip or a YouTube hiccup can't
silently kill the monitor.

### Where to host it

| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free** | $0 forever | A genuinely free ARM VM. Card required for identity check only. Most setup friction. |
| **Fly.io** (`fly.toml` included) | ~$2/mo | Smallest machine. Easiest deploy. No longer has a free tier. |
| **A Raspberry Pi / spare box** | $0 | Best option if you have one sitting around. |

There is no free, always-on, no-credit-card host — that combination essentially
does not exist any more. Oracle is the closest.

### Is 24/7 actually worth it?

The data updates **once a day**. Polling every 30s around the clock is ~2,880
requests/day to an undocumented endpoint to catch a single event, versus ~120 for
a one-hour window at identical precision. The main risk is not cost, it is that
YouTube rate-limits or blocks the endpoint, which means *no* alerts rather than
slower ones. A window is strictly better once the update hour is known.

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

$0 either way, but the repo's visibility constrains the polling cadence.

**Public repo — unlimited Actions minutes.** The 10-minute watch cadence is fine.
Nothing sensitive lives in this repo (the webhook is a GitHub Secret, never a
file), so public is the recommended setup.

**Private repo — 2,000 minutes/month.** Runs are billed per minute, rounded up,
and each run costs ~2 min after checkout and Python setup. That is a budget of
roughly 33 runs/day, so a 10-minute cadence (144/day) would exhaust the
allowance in about a week and silently stop.

To run private, slow the watcher down in `.github/workflows/watch.yml`:

```yaml
- cron: "*/45 * * * *"   # every 45 min — ~32 runs/day, fits the free tier
```

The sprint workflow is cheap regardless: it runs once a day for under an hour and
exits early on detection.

## If it breaks

This depends on an undocumented internal endpoint, which YouTube can change
without warning. The likely failure is the payload format changing, which
`monitor.py` reports as `API ignored parameters`. Re-derive the payload by opening
a chart page in Chrome with DevTools → Network, finding the `browse` request, and
copying its request body.
