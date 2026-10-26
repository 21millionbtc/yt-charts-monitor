# YouTube Charts monitor

Watches the daily global view counts for specific artists on
[charts.youtube.com](https://charts.youtube.com) and sends a Discord message when
a new day of data appears or when already-published numbers get revised.

Currently tracking **Michael Jackson** (`/m/09889g`), **Taylor Swift**
(`/m/0dl567`) and **Drake** (`/m/05mt_q`).

## Request budget and the tripwire

There is no published rate limit for this endpoint, so the goal is to stay
obviously modest rather than to find the ceiling.

All artists are published by the same pipeline on the same schedule — verified
2026-09-02, when all three then-tracked artists shared a trailing edge of
2026-08-30. So there is no need to poll all of them. **Tripwire mode polls one
artist** to detect that new data landed, and only then fetches everyone and
alerts. With 16 artists that divides the steady-state rate by 16:

| Interval | Naive (16 artists) | With tripwire |
|---|---|---|
| 5 min | 4,608/day | 288/day |
| 1 min | 23,040/day | 1,440/day |

At the configured 5-minute interval that is **~288 requests/day**, plus one
16-request burst on the day's single trip.

### The full-pass trap

A full pass costs one request *per artist*, so anything that can trigger one
every cycle destroys the budget above. Both runners used to request a full pass
whenever an artist was missing from state, assuming a missing artist gets
baselined on the next pass.

**That assumption fails under throttling.** An artist that keeps returning HTTP
429 never enters state, so `missing` never empties and *every* tick runs a full
pass. On 2026-10-25, expanding from 3 to 16 artists on the Worker's 1-minute
cron did exactly this: ~23,000 requests/day, the top-right cell of that table.
The 3-artist roster had masked it, because all three baselined on the first try.

Baselining is therefore rate limited by `FULL_PASS_MIN_INTERVAL` (15 minutes),
recorded in state under `_meta.last_full_pass`. The timestamp is written even
when nothing changed — otherwise the interval never advances and the guard does
nothing.

The lesson generalises: **any retry whose trigger condition is "data is
missing" will spin forever when the thing that fills it is failing.** Rate limit
the retry, not just the request.

Tripwire is on by default; set `TRIPWIRE=0` to poll every artist every cycle. The
only cost is that revisions to the non-tripwire artists are noticed just on
cycles where the edge moved, which is irrelevant while revision alerts are off.

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

### The wider endpoint surface

The same endpoint serves the whole of charts.youtube.com, not just artist
insights. The authoritative description of how the query string is built is the
site's own bundle — `charts_polymer_v2.js`, in the function that switches on
`browseId` (search it for `FEmusic_analytics_charts_home`).

**The parameter that makes or breaks every request is `perspective`.** Without
it the API ignores everything else and returns the default US weekly chart with
HTTP 200 — the same silent failure described above. Note also that chart *detail*
requests still send `browseId: FEmusic_analytics_charts_home`; `perspective` is
what distinguishes them.

| browseId | `perspective` | Returns |
|---|---|---|
| `..._charts_home` | `CHART_HOME` | top 100 artists + tracks + videos, plus `featuredSections` |
| `..._charts_home` | `CHART_DETAILS` | one chart, 100 rows (see chart types below) |
| `..._charts_home` | `PODCAST_SHOW` | podcast shows by watch time |
| `..._insights_artist` | `ARTIST` | daily view series — what `monitor.py` uses |
| `..._insights_location` | `LOCATION` | per-country daily view series |

Chart params: `chart_params_country_code` (`us`, `gb`, `jp`, …, or `global`),
`chart_params_chart_type`, `chart_params_period_type` (`DAILY` / `WEEKLY`),
`chart_params_end_date`, `chart_params_chart_attribute`.

The chart types the bundle can emit are `ARTISTS`, `TRACKS`, `VIDEOS`,
`TRENDING_VIDEOS`, `TRENDING_MOVIES`, `SHORTS_TRACKS_BY_VIEWS`,
`SHORTS_TRACKS_BY_USAGE`, `PODCAST_SHOWS_BY_WATCH_TIME` and `VIDEOS_LOP`.

Being in that list does **not** mean a given chart exists for a given country and
period — availability varies by both. Observed for `us`: `TRACKS` is weekly-only,
and of the two Shorts charts only `SHORTS_TRACKS_BY_USAGE` is published, so
`SHORTS_TRACKS_BY_VIEWS` returns HTTP 400 there. Rather than guess, read
`perspectiveMetadata.availableChartsInfo` in any response — it lists the valid
`chartType` × `chartPeriodType` combinations along with each one's
`earliestEndDate` and `latestEndDate`. An invalid combination returns HTTP 400.

Rows carry more than view counts: `currentPosition`, `previousPosition`,
`percentViewsChange`, `periodsOnChart`, thumbnails, `encryptedVideoId`, artist
MIDs, release dates and channel names.

**A browser cannot call this endpoint directly** — there is no CORS header, and
the request depends on `Origin`/`Referer` headers that browser JavaScript is
forbidden from setting. Anything with a frontend needs a server-side proxy, which
is why `viewer.py` exists rather than a plain HTML file.

## Important: the data lags 1–2 days

The newest available day trails today by one to two days, and **which one depends
on the hour you ask**. Measured:

| Checked (UTC) | Newest published day | Lag |
|---|---|---|
| 2026-09-02 | 2026-08-30 | 3 days |
| 2026-09-07 15:52 | 2026-09-05 | 2 days |
| 2026-10-25 23:14 | 2026-10-24 | 1 day |

The pattern is consistent with day *D* being published at some point during day
*D+1*: ask early in the UTC day and you see a 2-day lag, ask late and the newest
day has usually landed. (This section previously claimed a flat 2–3 days, based
only on the 2026-09-02 reading. The later measurements show that overstated it.)

Two independent perspectives agree on the trailing edge, which is a useful
cross-check if you ever suspect a date bug: `perspective=CHART_HOME` reports
`availableChartsInfo[].latestEndDate` for the daily charts, and that matches the
newest date returned by `perspective=ARTIST` for an individual artist.

So "a new day was added" does **not** mean "yesterday's numbers are in" — it
means the trailing edge advanced by one day.

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

## Cloudflare Worker (the recommended deployment)

**GitHub Actions cannot be used for timely alerts.** Measured 2026-09-07 on this
repo: a `*/5` schedule ran at a **191-minute median gap**, with a worst case of
5.6 hours — 14 runs across 41 hours where there should have been ~500. GitHub
documents scheduled workflows as best-effort and deprioritises high-frequency
crons on free public repos. No configuration fixes this.

The Worker in `worker/` runs the same logic on Cloudflare Cron Triggers, which do
fire on time. Free plan, no credit card, 1-minute minimum interval (configured
here at 5 — see the note in `worker/wrangler.toml` about why it was lowered).

### Deploy

You need a Cloudflare account first (free, no card): https://dash.cloudflare.com/sign-up

```bash
cd worker
npx wrangler login                          # opens a browser to authorise
npx wrangler kv namespace create STATE      # paste the returned id into wrangler.toml
npx wrangler secret put DISCORD_WEBHOOK_URL # paste the webhook when prompted
npx wrangler deploy
```

**Gotcha: you must load the Workers dashboard page once before the first deploy.**
Cloudflare refuses to attach *any* cron schedule to an account that has no
`workers.dev` subdomain — even for a Worker like this one that has no public URL
and sets `workers_dev = false`. It fails with error 10063. Simply visiting
`https://dash.cloudflare.com/<account-id>/workers-and-pages` creates the
subdomain automatically; then `wrangler deploy` succeeds. Wrangler 4 has no CLI
command for this, so the dashboard visit is unavoidable.

Watch it live:

```bash
npx wrangler tail
```

The Worker also serves a **read-only status page** at its `*.workers.dev` URL —
current dates per artist plus recent events. It has no side effects and never
posts to Discord, so the URL being public cannot be used to spam your channel.

### Free-tier limits that shaped the code

| Limit | Free plan | Our usage at `*/2` |
|---|---|---|
| Worker invocations | 100,000/day | 720/day |
| KV reads | 100,000/day | 720/day |
| **KV writes** | **1,000/day** | a few/day |

The write limit is the binding one. State is written **only when something
changes** — never per-poll bookkeeping like a "last checked" timestamp, which by
itself would nearly exhaust the daily write budget.

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

## Local viewer

`viewer.py` serves the last 14 days of daily views for every configured artist as
a chart you can read in a browser:

```bash
python3 viewer.py           # then open http://localhost:8765
```

It binds to `127.0.0.1` only. `?theme=light` / `?theme=dark` forces a theme; the
in-page toggle remembers your choice.

**It never writes `state.json` or `history.jsonl`.** That is deliberate: the
README warns above that running the monitor locally and committing its state
makes the next scheduled run report a spurious revision, because your home IP
returns different numbers than the runner. The viewer reads the same API through
`monitor.py`'s `fetch_artist`, but keeps its own cache in `.views_cache.json`
(gitignored), so looking at the data locally can never corrupt the monitor.

### The throttle, and how the viewer survives it

The endpoint rate-limits with **HTTP 429**, and the important detail is that the
throttle is **probabilistic, not a block**. Observed over ~20 attempts from one
IP: failures spread evenly across all three artists, with a *different* artist
succeeding on each pass — roughly one request in three got through. Nothing is
banned; requests are simply being thinned.

That shape dictates the design: **the cache is keyed per artist, not per pass.**

1. **Bank every success.** An artist that returns is cached for 30 minutes and
   is not requested again. Repeated loads therefore *converge* on a complete set
   instead of re-rolling the dice on all three every time.
2. **Only ask for what expired.** A plain page load requests just the artists
   whose cache has lapsed — usually none, often one. "Refresh" uses a 60-second
   floor rather than 0, because under a probabilistic throttle, re-fetching an
   artist you just got spends one of the few requests that succeed on data you
   already have.
3. **Fall back for the rest.** An artist never successfully fetched falls back to
   `state.json`, and one whose data predates the 14-day window is drawn as *no
   recent data* rather than silently dropped. The page always names its sources.

Requests are spaced ~4s apart and retry once, rather than using `monitor.py`'s
3-attempt exponential backoff — when the endpoint is already throttling, retrying
harder is what keeps it throttling.

**If you are being throttled, the fix is to need fewer requests, not to dodge the
limit.** Rotating IPs or visitor tokens to evade it risks turning a soft, self-
clearing throttle into a hard block, which would take the monitor down with it.
The tripwire (above) and this per-artist cache both exist for that reason. Note
also that the limit is tied to the source IP: the Cloudflare Worker and GitHub
runners have their own, so local probing does not affect the deployed monitor.

## Files

| File | Purpose |
|---|---|
| `monitor.py` | The poller, change detection, and Discord notifier |
| `state.json` | Last-seen values (committed — this is the memory between runs) |
| `history.jsonl` | Append-only log of every detected change, with UTC timestamps |
| `viewer.py` | Localhost server for the 14-day view — read-only, never writes state |
| `viewer.html` | The viewer's page (chart, stat tiles, table view) |
| `.views_cache.json` | The viewer's own cache (gitignored, transient) |
| `worker/` | The Cloudflare Worker port of the monitor |

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
