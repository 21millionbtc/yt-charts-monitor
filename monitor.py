#!/usr/bin/env python3
"""
YouTube Charts artist-view monitor.

Polls charts.youtube.com's internal analytics API for each configured artist,
detects when a new day of data appears (or when previously-published numbers are
revised), and posts a Discord notification.

Stdlib only - no pip install required.

The API is YouTube's own undocumented InnerTube endpoint, the same one the
charts.youtube.com frontend calls. The essential trick: the parameters live in a
URL-encoded string nested inside the JSON body under "query" - not in the URL and
not as JSON fields. Getting that wrong returns HTTP 200 with default data, which
looks like success but silently ignores everything you asked for.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ARTISTS = [
    {"name": "Michael Jackson", "id": "/m/09889g"},
    {"name": "Taylor Swift", "id": "/m/0dl567"},
    {"name": "Drake", "id": "/m/05mt_q"},
]

# Tripwire mode: poll only ONE artist to detect that new data has landed, then
# fetch everyone and alert. All artists are published by the same pipeline on the
# same schedule - verified 2026-09-02, when all three shared an edge of
# 2026-08-30 - so one artist is a reliable signal for all of them.
#
# This divides the steady-state request rate by the number of artists, which is
# what makes a fast interval sustainable without hammering an undocumented
# endpoint. Set TRIPWIRE=0 to poll every artist on every cycle instead.
#
# Trade-off: revisions to the non-tripwire artists are only noticed on cycles
# where the edge moved. Since revision alerts are off by default (see
# ALERT_ON_REVISIONS) this costs nothing in practice.
TRIPWIRE = os.environ.get("TRIPWIRE", "1").strip() not in ("0", "false", "no")

ENDPOINT = "https://charts.youtube.com/youtubei/v1/browse?alt=json"
BROWSE_ID = "FEmusic_analytics_insights_artist"
FLAGS = "MusicCharts__enable_apac_and_shorts_charts_expansion"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

CONTEXT = {
    "client": {
        "clientName": "WEB_MUSIC_ANALYTICS",
        "clientVersion": "2.0",
        "hl": "en",
        "gl": "US",
        "experimentIds": [],
        "theme": "MUSIC",
    },
    "capabilities": {},
    "request": {"internalExperimentFlags": []},
}

HERE = os.path.dirname(os.path.abspath(__file__))
# STATE_DIR lets a container point state at a mounted volume so it survives
# restarts. Defaults to alongside the script, which is what the GitHub workflows
# expect (they commit state.json back to the repo).
STATE_DIR = os.environ.get("STATE_DIR", "").strip() or HERE
os.makedirs(STATE_DIR, exist_ok=True)
STATE_PATH = os.path.join(STATE_DIR, "state.json")
HISTORY_PATH = os.path.join(STATE_DIR, "history.jsonl")

# How many days of history to request. The API lags roughly 2-3 days behind
# today, so a short window would return nothing at all.
LOOKBACK_DAYS = 14

DISCORD_GREEN = 0x1DB954   # new day of data
DISCORD_ORANGE = 0xE67E22  # revision to an already-published day

# Whether to send a Discord message when previously-published numbers change.
#
# Off by default. YouTube returns different figures for the same date depending
# on the requesting region, and GitHub runners move between regions between runs,
# so "revisions" are frequently just an artifact of which machine asked. They are
# always written to history.jsonl regardless; this flag only controls whether
# they interrupt you. Set ALERT_ON_REVISIONS=1 to turn the Discord alerts on.
ALERT_ON_REVISIONS = os.environ.get("ALERT_ON_REVISIONS", "").strip() in ("1", "true", "yes")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def build_query(artist_id, start, end):
    """Build the URL-encoded parameter string that goes inside the JSON body."""
    encoded_id = artist_id.replace("/", "%2F")
    return (
        f"flags={FLAGS}"
        "&perspective=ARTIST"
        "&entity_params_entity=ARTIST"
        f"&artist_params_id={encoded_id}"
        f"&date_params_start_time={start}"
        f"&date_params_end_time={end}"
        "&date_params_interval=DAY"
    )


def fetch_artist(artist_id, retries=3):
    """Return [{'date': 'YYYY-MM-DD', 'viewCount': '123'}, ...] oldest-first."""
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT07:00:00Z")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%dT07:00:00Z")

    body = json.dumps({
        "context": CONTEXT,
        "browseId": BROWSE_ID,
        "query": build_query(artist_id, start, end),
    }).encode("utf-8")

    req = urllib.request.Request(
        ENDPOINT,
        data=body,
        method="POST",
        headers={
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
            "Referer": "https://charts.youtube.com/",
            "Origin": "https://charts.youtube.com",
        },
    )

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            content = (payload["contents"]["sectionListRenderer"]["contents"][0]
                       ["musicAnalyticsSectionRenderer"]["content"])

            # Defensive: if our params were ignored, the API returns the default
            # Top-Artists chart instead, which has no "dates" key. Treat that as
            # an error rather than silently reporting "no data".
            if "dates" not in content:
                raise RuntimeError(
                    "API ignored parameters (no 'dates' in response) - the "
                    "payload format has probably changed"
                )

            return content["dates"][0]["dateViews"]

        except (urllib.error.URLError, urllib.error.HTTPError,
                KeyError, IndexError, ValueError, RuntimeError) as exc:
            last_err = exc
            if attempt < retries - 1:
                # Exponential backoff. If YouTube starts throttling, back off
                # rather than hammering it and making things worse.
                time.sleep(2 ** attempt * 5)

    raise RuntimeError(f"failed after {retries} attempts: {last_err}")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH) as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return {}


def save_state(state):
    with open(STATE_PATH, "w") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def append_history(record):
    with open(HISTORY_PATH, "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def post_discord(webhook, embeds):
    if not webhook:
        print("  ! DISCORD_WEBHOOK_URL not set - skipping notification")
        return False

    body = json.dumps({"embeds": embeds}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "yt-charts-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        print(f"  ! Discord rejected the message: HTTP {exc.code}")
        return False
    except urllib.error.URLError as exc:
        print(f"  ! Could not reach Discord: {exc}")
        return False


def fmt(n):
    return f"{int(n):,}"


def build_embed(artist, kind, latest, views, previous_views, detected_at):
    if kind == "new_day":
        title = f"New day of data - {artist}"
        color = DISCORD_GREEN
        desc = f"**{latest}** is now published."
    else:
        title = f"Revised numbers - {artist}"
        color = DISCORD_ORANGE
        desc = f"YouTube changed already-published figures for **{latest}**."

    fields = [{"name": "Date", "value": latest, "inline": True},
              {"name": "Views", "value": fmt(views), "inline": True}]

    if previous_views is not None:
        delta = int(views) - int(previous_views)
        sign = "+" if delta >= 0 else ""
        fields.append({
            "name": "Change",
            "value": f"{sign}{fmt(abs(delta)) if delta < 0 else fmt(delta)} "
                     f"(was {fmt(previous_views)})",
            "inline": True,
        })

    return {
        "title": title,
        "description": desc,
        "color": color,
        "fields": fields,
        "footer": {"text": f"detected {detected_at} UTC"},
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def check_once(webhook, verbose=True):
    """Run one poll of every artist. Returns True if anything changed."""
    state = load_state()
    detected_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    embeds = []
    changed = False

    for artist in ARTISTS:
        name, aid = artist["name"], artist["id"]
        try:
            rows = fetch_artist(aid)
        except RuntimeError as exc:
            print(f"  ! {name}: {exc}")
            continue

        if not rows:
            print(f"  ! {name}: no data returned")
            continue

        by_date = {r["date"]: r["viewCount"] for r in rows}
        latest = max(by_date)
        prev = state.get(aid, {})
        prev_latest = prev.get("latest_date")
        prev_by_date = dict(prev.get("views", {}))
        pending = dict(prev.get("pending", {}))
        # Highest date we have ever alerted on. Guards against re-alerting the
        # same day if the trailing edge flaps backwards between regions.
        max_alerted = prev.get("max_alerted_date") or prev_latest

        if verbose:
            print(f"  {name}: latest={latest} views={fmt(by_date[latest])}")

        if prev_latest is None:
            # First run - record the baseline without alerting, otherwise the
            # very first poll would fire a bogus "new day" for old data.
            print(f"    -> baseline recorded (no alert)")
            max_alerted = latest

        elif latest > (max_alerted or ""):
            # A genuinely new day. Alert immediately - this is the event the
            # monitor exists for, and delaying it would defeat the 30s sprint.
            changed = True
            yesterday = prev_by_date.get(prev_latest)
            embeds.append(build_embed(name, "new_day", latest,
                                      by_date[latest], yesterday, detected_at))
            append_history({"ts": detected_at, "artist": name, "kind": "new_day",
                            "date": latest, "views": by_date[latest],
                            "previous_date": prev_latest})
            print(f"    -> NEW DAY: {prev_latest} -> {latest}")
            max_alerted = latest

        # Revisions to already-published days need CONFIRMATION before alerting.
        #
        # YouTube serves different figures for the same date depending on which
        # region asks - observed up to ~1.7% apart, in both directions, on dates
        # more than a week old. A single differing poll is therefore not evidence
        # of a revision. We only alert once the SAME new value shows up twice in
        # a row, which filters region flapping while still catching real changes.
        newly_pending = {}
        confirmed = []
        for d, v in by_date.items():
            if d not in prev_by_date or prev_by_date[d] == v:
                continue
            if pending.get(d) == v:
                confirmed.append(d)
            else:
                newly_pending[d] = v

        if confirmed:
            d = max(confirmed)
            # Always record revisions to history - they are useful data even
            # when we choose not to interrupt anyone about them.
            append_history({"ts": detected_at, "artist": name,
                            "kind": "revision", "date": d,
                            "views": by_date[d],
                            "previous_views": prev_by_date[d],
                            "alerted": ALERT_ON_REVISIONS})

            if ALERT_ON_REVISIONS:
                changed = True
                embeds.append(build_embed(name, "revision", d, by_date[d],
                                          prev_by_date[d], detected_at))
                print(f"    -> REVISED (confirmed): {', '.join(sorted(confirmed))}")
            else:
                print(f"    -> revised (logged, no alert): "
                      f"{', '.join(sorted(confirmed))}")

            for d in confirmed:
                prev_by_date[d] = by_date[d]

        if newly_pending:
            print(f"    -> possible revision, awaiting confirmation: "
                  f"{', '.join(sorted(newly_pending))}")

        # Accept new dates and any confirmed values; leave unconfirmed ones at
        # their old value so the next poll can compare against a stable baseline.
        for d, v in by_date.items():
            if d not in prev_by_date:
                prev_by_date[d] = v

        state[aid] = {"name": name, "latest_date": latest,
                      "views": prev_by_date, "pending": newly_pending,
                      "max_alerted_date": max_alerted,
                      "last_checked": detected_at}

    save_state(state)

    if embeds:
        ok = post_discord(webhook, embeds)
        print(f"  Discord: {'sent' if ok else 'FAILED'}")

    return changed


def poll(webhook, verbose=True):
    """One polling cycle. Uses the tripwire unless it is disabled.

    Returns True if something was reported.
    """
    if not TRIPWIRE:
        return check_once(webhook, verbose)

    state = load_state()
    canary = ARTISTS[0]
    prev = state.get(canary["id"], {})
    baseline = prev.get("max_alerted_date") or prev.get("latest_date")

    # Any artist with no recorded state must be baselined by a full pass.
    # Without this, an artist added to ARTISTS after the tripwire is already
    # running would never enter state - and when the edge finally moved, their
    # first real update would be swallowed as a "baseline" with no alert.
    missing = [a["name"] for a in ARTISTS if a["id"] not in state]
    if baseline is None or missing:
        if missing:
            print(f"  new artist(s) not yet baselined: {', '.join(missing)}"
                  f" - running a full pass")
        return check_once(webhook, verbose)

    try:
        rows = fetch_artist(canary["id"])
    except RuntimeError as exc:
        print(f"  ! tripwire ({canary['name']}): {exc}")
        return False

    if not rows:
        print(f"  ! tripwire ({canary['name']}): no data returned")
        return False

    latest = max(r["date"] for r in rows)

    if latest > baseline:
        print(f"  tripwire TRIPPED: {baseline} -> {latest} "
              f"(fetching all {len(ARTISTS)} artists)")
        return check_once(webhook, verbose=True)

    if verbose:
        print(f"  tripwire ({canary['name']}): {latest}, no change")
    return False


def run_forever(webhook, interval):
    """Poll continuously, forever. For running as an always-on service.

    Unlike sprint mode this never exits on a change - it keeps watching. It also
    never dies on an error: a failed poll backs off and the loop continues, so a
    transient network blip or a YouTube hiccup can't silently end the monitor.
    """
    print(f"[forever] polling every {interval}s - press Ctrl-C to stop")
    n = 0
    consecutive_errors = 0

    while True:
        n += 1
        started = time.time()
        try:
            # Only print detail occasionally; at a fast interval this runs
            # thousands of times a day and the logs would be unreadable.
            poll(webhook, verbose=(n % 30 == 1))
            consecutive_errors = 0
        except KeyboardInterrupt:
            print("\n[forever] stopped")
            return 0
        except Exception as exc:
            consecutive_errors += 1
            print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] "
                  f"poll {n} failed ({consecutive_errors} in a row): {exc}")

        # If YouTube starts refusing us, back off progressively rather than
        # hammering it - that is what turns a temporary block into a ban.
        delay = interval
        if consecutive_errors:
            delay = min(interval * (2 ** min(consecutive_errors, 6)), 1800)
            print(f"    backing off {delay}s")

        elapsed = time.time() - started
        time.sleep(max(1, delay - elapsed))


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

    # Sprint mode: poll every N seconds for a bounded duration, exit as soon as
    # a change is seen. This is how we get 30-second precision out of a cron
    # system whose floor is one minute.
    #
    # SPRINT_DURATION_SECONDS:
    #    0  -> a single check, then exit (cron style)
    #   >0  -> poll for that many seconds, exit early on change
    #   -1  -> run forever, never exit (always-on server style)
    duration = int(os.environ.get("SPRINT_DURATION_SECONDS", "0"))
    interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "120"))

    if duration == 0:
        print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] single check")
        poll(webhook)
        return 0

    if duration < 0:
        return run_forever(webhook, interval)

    deadline = time.time() + duration
    n = 0
    print(f"[sprint] every {interval}s for up to {duration}s")
    while time.time() < deadline:
        n += 1
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] poll {n}")
        try:
            if poll(webhook, verbose=(n == 1)):
                print("[sprint] change detected - exiting early")
                return 0
        except Exception as exc:  # never let one bad poll kill the sprint
            print(f"  ! unexpected error: {exc}")
        if time.time() + interval < deadline:
            time.sleep(interval)
        else:
            break
    print(f"[sprint] finished after {n} polls, no change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
