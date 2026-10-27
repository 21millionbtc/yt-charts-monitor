/**
 * YouTube Charts artist-view monitor - Cloudflare Worker.
 *
 * A port of monitor.py. Exists because GitHub Actions' scheduler does not honour
 * high-frequency crons: a five-minute schedule was measured running at a ~191
 * minute median. Cloudflare Cron Triggers actually run when they say they will.
 *
 * Free tier notes that shaped this code:
 *   - Workers: 100k invocations/day. At two-minute polling that is 720. Fine.
 *   - KV: 100k reads/day but only 1k WRITES/day. So state is read every poll and
 *     written ONLY when something actually changed. Never write per-poll fields
 *     like "last checked" - that alone would nearly exhaust the write budget.
 */

// Michael Jackson stays first: he is the tripwire canary (ARTISTS[0]).
const ARTISTS = [
  { name: "Michael Jackson", id: "/m/09889g" },
  { name: "Ariana Grande", id: "/m/09gkdy4" },
  { name: "Bad Bunny", id: "/g/11gdq15782" },
  { name: "Drake", id: "/m/05mt_q" },
  { name: "Fuerza Regida", id: "/g/11fy19q209" },
  { name: "Future", id: "/m/0hhwdgn" },
  { name: "Justin Bieber", id: "/m/06w2sn5" },
  { name: "Kanye West", id: "/m/02l840" },
  { name: "KATSEYE", id: "/g/11vj6xh7_6" },
  { name: "Kendrick Lamar", id: "/m/0g9x698" },
  { name: "Morgan Wallen", id: "/g/11g7ntnqs5" },
  { name: "YoungBoy Never Broke Again", id: "/g/11g9nh3b7y" },
  { name: "Olivia Rodrigo", id: "/g/11j0_8y5xw" },
  { name: "Post Malone", id: "/g/11bw82cs0m" },
  { name: "The Weeknd", id: "/m/0gjdn4c" },
  { name: "Taylor Swift", id: "/m/0dl567" },
];

const ENDPOINT = "https://charts.youtube.com/youtubei/v1/browse?alt=json";
const BROWSE_ID = "FEmusic_analytics_insights_artist";
const FLAGS = "MusicCharts__enable_apac_and_shorts_charts_expansion";

// Minimum gap between full (all-artist) passes. Guards the request budget when
// an artist cannot be baselined because the endpoint is throttling.
const FULL_PASS_MIN_INTERVAL_MS = 15 * 60 * 1000;
// A full pass that fetches NOTHING (every artist throttled) still costs one
// request per artist while teaching us nothing, so the gap doubles after each
// such pass, up to this ceiling, and resets the moment one succeeds.
const FULL_PASS_MAX_INTERVAL_MS = 4 * 60 * 60 * 1000;
const FULL_PASS_MAX_BACKOFF_STEPS = 4;
const USER_AGENT =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36";

const CONTEXT = {
  client: {
    clientName: "WEB_MUSIC_ANALYTICS",
    clientVersion: "2.0",
    hl: "en",
    gl: "US",
    experimentIds: [],
    theme: "MUSIC",
  },
  capabilities: {},
  request: { internalExperimentFlags: [] },
};

const LOOKBACK_DAYS = 14;
const HISTORY_MAX = 200;

const GREEN = 0x1db954;
const ORANGE = 0xe67e22;

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

function ymd(d) {
  return d.toISOString().slice(0, 10);
}

/**
 * The critical detail, same as the Python version: the parameters are a
 * URL-encoded string nested INSIDE the JSON body under "query". Not URL params,
 * not JSON fields. Get it wrong and the API returns 200 with the default weekly
 * Top-Artists chart, which looks like success.
 */
function buildQuery(artistId) {
  const now = new Date();
  const start = new Date(now.getTime() - LOOKBACK_DAYS * 86400000);
  const end = new Date(now.getTime() + 86400000);
  const encodedId = artistId.replace(/\//g, "%2F");
  return (
    `flags=${FLAGS}` +
    "&perspective=ARTIST&entity_params_entity=ARTIST" +
    `&artist_params_id=${encodedId}` +
    `&date_params_start_time=${ymd(start)}T07:00:00Z` +
    `&date_params_end_time=${ymd(end)}T07:00:00Z` +
    "&date_params_interval=DAY"
  );
}

async function fetchArtist(artistId) {
  const body = JSON.stringify({
    context: CONTEXT,
    browseId: BROWSE_ID,
    query: buildQuery(artistId),
  });

  const resp = await fetch(ENDPOINT, {
    method: "POST",
    headers: {
      "User-Agent": USER_AGENT,
      "Content-Type": "application/json",
      Referer: "https://charts.youtube.com/",
      Origin: "https://charts.youtube.com",
    },
    body,
  });

  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();

  const content =
    data?.contents?.sectionListRenderer?.contents?.[0]
      ?.musicAnalyticsSectionRenderer?.content;

  // If our params were ignored the response is the default chart, which has no
  // "dates". Treat that as an error rather than silently reporting no change.
  if (!content?.dates) {
    throw new Error("API ignored parameters - payload format may have changed");
  }

  const out = {};
  for (const row of content.dates[0].dateViews) out[row.date] = row.viewCount;
  return out;
}

// ---------------------------------------------------------------------------
// State (KV)
// ---------------------------------------------------------------------------

async function loadState(env) {
  return (await env.STATE.get("state", { type: "json" })) || {};
}

async function saveState(env, state) {
  await env.STATE.put("state", JSON.stringify(state));
}

async function appendHistory(env, records) {
  if (!records.length) return;
  const hist = (await env.STATE.get("history", { type: "json" })) || [];
  hist.push(...records);
  await env.STATE.put(
    "history",
    JSON.stringify(hist.slice(-HISTORY_MAX))
  );
}

// ---------------------------------------------------------------------------
// Discord
// ---------------------------------------------------------------------------

function fmt(n) {
  return Number(n).toLocaleString("en-US");
}

/**
 * One embed covering every artist that gained a new day.
 *
 * Discord allows at most 10 embeds per webhook message. All artists publish on
 * the same day, so one embed each would blow that limit on exactly the days
 * this monitor exists to report - a 16-artist roster would notify nothing.
 */
function buildSummaryEmbed(events, detectedAt) {
  const date = events.map((e) => e.date).sort().at(-1);
  const lines = events
    .slice()
    .sort((a, b) => Number(b.views) - Number(a.views))
    .map((e) => {
      let delta = "";
      if (e.prev != null && e.prev !== "") {
        const d = Number(e.views) - Number(e.prev);
        const pct = Number(e.prev) ? (d / Number(e.prev)) * 100 : 0;
        delta = `  (${d >= 0 ? "+" : "-"}${Math.abs(pct).toFixed(1)}%)`;
      }
      return `\`${fmt(e.views).padStart(12)}\`  ${e.name}${delta}`;
    });
  return {
    title: `New day of data - ${date}`,
    description: lines.join("\n").slice(0, 4000),
    color: GREEN,
    footer: { text: `${events.length} artist(s) - detected ${detectedAt} UTC` },
  };
}

function buildEmbed(artist, kind, date, views, prevViews, detectedAt) {
  const fields = [
    { name: "Date", value: date, inline: true },
    { name: "Views", value: fmt(views), inline: true },
  ];
  if (prevViews != null) {
    const delta = Number(views) - Number(prevViews);
    fields.push({
      name: "Change",
      value: `${delta >= 0 ? "+" : "-"}${fmt(Math.abs(delta))} (was ${fmt(prevViews)})`,
      inline: true,
    });
  }
  return {
    title: kind === "new_day" ? `New day of data - ${artist}` : `Revised numbers - ${artist}`,
    description:
      kind === "new_day"
        ? `**${date}** is now published.`
        : `YouTube changed already-published figures for **${date}**.`,
    color: kind === "new_day" ? GREEN : ORANGE,
    fields,
    footer: { text: `detected ${detectedAt} UTC` },
  };
}

async function postDiscord(webhook, embeds) {
  if (!webhook || !embeds.length) return false;
  if (embeds.length > 10) {
    let all = true;
    for (let i = 0; i < embeds.length; i += 10) {
      all = (await postDiscord(webhook, embeds.slice(i, i + 10))) && all;
    }
    return all;
  }
  const resp = await fetch(webhook, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ embeds }),
  });
  return resp.ok;
}

// ---------------------------------------------------------------------------
// Core
// ---------------------------------------------------------------------------

async function checkAll(env, state, log) {
  const alertRevisions = String(env.ALERT_ON_REVISIONS || "") === "1";
  const detectedAt = new Date().toISOString().replace("T", " ").slice(0, 19);
  const embeds = [];
  const newDays = [];   // reported as ONE summary embed
  const history = [];
  let dirty = false;
  let ok = 0;                  // artists actually fetched this pass

  for (const artist of ARTISTS) {
    let byDate;
    try {
      byDate = await fetchArtist(artist.id);
      ok++;
    } catch (err) {
      log.push(`! ${artist.name}: ${err.message}`);
      continue;
    }

    const dates = Object.keys(byDate);
    if (!dates.length) {
      log.push(`! ${artist.name}: no data`);
      continue;
    }

    const latest = dates.sort().at(-1);
    const prev = state[artist.id] || {};
    const prevViews = { ...(prev.views || {}) };
    const pending = { ...(prev.pending || {}) };
    let maxAlerted = prev.max_alerted_date || prev.latest_date || null;

    if (!prev.latest_date) {
      // First sighting: record a baseline, never alert. Otherwise adding an
      // artist would fire a bogus "new day" for data that was already there.
      log.push(`${artist.name}: baseline ${latest}`);
      maxAlerted = latest;
      dirty = true;
    } else if (latest > maxAlerted) {
      log.push(`${artist.name}: NEW DAY ${prev.latest_date} -> ${latest}`);
      newDays.push({
        name: artist.name, date: latest, views: byDate[latest],
        prev: prevViews[prev.latest_date],
      });
      history.push({ ts: detectedAt, artist: artist.name, kind: "new_day", date: latest });
      maxAlerted = latest;
      dirty = true;
    }

    // Revisions need CONFIRMATION: YouTube serves different figures for the same
    // date depending on the requesting region, so one differing read proves
    // nothing. Only act when the same new value shows up twice in a row.
    const newlyPending = {};
    const confirmed = [];
    for (const [d, v] of Object.entries(byDate)) {
      if (!(d in prevViews) || prevViews[d] === v) continue;
      if (pending[d] === v) confirmed.push(d);
      else newlyPending[d] = v;
    }

    if (confirmed.length) {
      const d = confirmed.sort().at(-1);
      history.push({
        ts: detectedAt, artist: artist.name, kind: "revision",
        date: d, alerted: alertRevisions,
      });
      if (alertRevisions) {
        embeds.push(buildEmbed(artist.name, "revision", d, byDate[d], prevViews[d], detectedAt));
      }
      log.push(`${artist.name}: revised ${confirmed.join(", ")}`);
      for (const d2 of confirmed) prevViews[d2] = byDate[d2];
      dirty = true;
    }

    for (const [d, v] of Object.entries(byDate)) {
      if (!(d in prevViews)) { prevViews[d] = v; dirty = true; }
    }

    if (JSON.stringify(pending) !== JSON.stringify(newlyPending)) dirty = true;

    state[artist.id] = {
      name: artist.name,
      latest_date: latest,
      views: prevViews,
      pending: newlyPending,
      max_alerted_date: maxAlerted,
    };
  }

  if (newDays.length) embeds.unshift(buildSummaryEmbed(newDays, detectedAt));

  if (embeds.length) {
    const ok = await postDiscord(env.DISCORD_WEBHOOK_URL, embeds);
    log.push(`Discord: ${ok ? "sent" : "FAILED"}`);
  }
  await appendHistory(env, history);
  return { dirty, ok };
}

/**
 * One polling cycle. Tripwire mode polls a single artist to detect that new data
 * landed, then fetches everyone. All artists publish on the same schedule, so
 * this cuts steady-state requests by 3x.
 */
async function poll(env) {
  const log = [];
  const state = await loadState(env);
  const tripwire = String(env.TRIPWIRE ?? "1") !== "0";

  const missing = ARTISTS.filter((a) => !state[a.id]).map((a) => a.name);
  const canary = ARTISTS[0];
  const baseline =
    state[canary.id]?.max_alerted_date || state[canary.id]?.latest_date || null;

  // A full pass costs one request PER ARTIST. Baselining must therefore be rate
  // limited, because an artist that keeps failing (the endpoint throttles with
  // HTTP 429) never enters state - so `missing` never empties and every single
  // cron tick would run a full pass. At a 1-minute cron and 16 artists that is
  // ~23,000 requests/day, which is exactly what the tripwire exists to avoid,
  // and it would also blow the free tier's 1,000 KV writes/day.
  const lastFullPass = state._meta?.last_full_pass || 0;
  const backoffSteps = state._meta?.full_pass_backoff || 0;
  const fullPassInterval = Math.min(
    FULL_PASS_MIN_INTERVAL_MS * 2 ** backoffSteps,
    FULL_PASS_MAX_INTERVAL_MS
  );
  const sinceFullPass = Date.now() - lastFullPass;
  const mayBaseline = sinceFullPass >= fullPassInterval;

  if (!tripwire || !baseline || (missing.length && mayBaseline)) {
    if (missing.length) log.push(`baselining new artist(s): ${missing.join(", ")}`);
    const { dirty, ok } = await checkAll(env, state, log);
    if (ok === 0) log.push(`full pass fetched nothing; backing off`);
    // Record the attempt even when nothing changed, otherwise the interval
    // never advances and the rate limit above has no effect.
    state._meta = {
      ...(state._meta || {}),
      last_full_pass: Date.now(),
      full_pass_backoff:
        ok > 0 ? 0 : Math.min(backoffSteps + 1, FULL_PASS_MAX_BACKOFF_STEPS),
    };
    await saveState(env, state);
    return log;
  }

  if (missing.length) {
    const mins = Math.ceil((fullPassInterval - sinceFullPass) / 60000);
    log.push(`${missing.length} artist(s) still unbaselined; next full pass in ~${mins}m`);
  }

  let byDate;
  try {
    byDate = await fetchArtist(canary.id);
  } catch (err) {
    log.push(`! tripwire (${canary.name}): ${err.message}`);
    return log;
  }

  const latest = Object.keys(byDate).sort().at(-1);
  if (latest > baseline) {
    log.push(`tripwire TRIPPED: ${baseline} -> ${latest}`);
    const { dirty } = await checkAll(env, state, log);
    if (dirty) await saveState(env, state);
  } else {
    log.push(`tripwire (${canary.name}): ${latest}, no change`);
    // Deliberately no KV write here - this is the common path and writes are
    // limited to 1,000/day on the free plan.
  }
  return log;
}

// ---------------------------------------------------------------------------
// Entry points
// ---------------------------------------------------------------------------

export default {
  async scheduled(event, env, ctx) {
    // TEST_DISCORD=1 posts one sample alert through the real webhook and skips
    // the poll, so the notification path can be verified without waiting for a
    // genuine new day. Set it back to "0" immediately afterwards.
    if (String(env.TEST_DISCORD || "") === "1") {
      ctx.waitUntil(
        postDiscord(env.DISCORD_WEBHOOK_URL, [
          {
            title: "Test alert - notification path check",
            description:
              "If you can read this, the Worker can reach Discord.\n" +
              "This is a test, not a new day of data.",
            color: GREEN,
            footer: { text: `sent ${new Date().toISOString().slice(0, 19)} UTC` },
          },
        ]).then((ok) => console.log(`TEST_DISCORD: ${ok ? "sent" : "FAILED"}`))
      );
      return;
    }
    ctx.waitUntil(
      poll(env).then((log) => console.log(log.join(" | ")))
    );
  },

  // Read-only status page. Deliberately has no side effects and never posts to
  // Discord, so the URL being public cannot be used to spam your channel.
  async fetch(request, env) {
    const state = await loadState(env);
    const history = (await env.STATE.get("history", { type: "json" })) || [];
    const body = {
      artists: Object.entries(state)
        .filter(([k]) => k !== "_meta")
        .map(([, a]) => ({
        name: a.name,
        latest_date: a.latest_date,
      })),
      recent_events: history.slice(-20).reverse(),
      now: new Date().toISOString(),
    };
    return new Response(JSON.stringify(body, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};
