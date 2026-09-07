/**
 * YouTube Charts artist-view monitor - Cloudflare Worker.
 *
 * A port of monitor.py. Exists because GitHub Actions' scheduler does not honour
 * high-frequency crons: a */5 schedule was measured running at a ~191 minute
 * median. Cloudflare Cron Triggers actually run when they say they will.
 *
 * Free tier notes that shaped this code:
 *   - Workers: 100k invocations/day. At */2 that is 720. Fine.
 *   - KV: 100k reads/day but only 1k WRITES/day. So state is read every poll and
 *     written ONLY when something actually changed. Never write per-poll fields
 *     like "last checked" - that alone would nearly exhaust the write budget.
 */

const ARTISTS = [
  { name: "Michael Jackson", id: "/m/09889g" },
  { name: "Taylor Swift", id: "/m/0dl567" },
  { name: "Drake", id: "/m/05mt_q" },
];

const ENDPOINT = "https://charts.youtube.com/youtubei/v1/browse?alt=json";
const BROWSE_ID = "FEmusic_analytics_insights_artist";
const FLAGS = "MusicCharts__enable_apac_and_shorts_charts_expansion";
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
  const history = [];
  let dirty = false;

  for (const artist of ARTISTS) {
    let byDate;
    try {
      byDate = await fetchArtist(artist.id);
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
      embeds.push(
        buildEmbed(artist.name, "new_day", latest, byDate[latest],
                   prevViews[prev.latest_date], detectedAt)
      );
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

  if (embeds.length) {
    const ok = await postDiscord(env.DISCORD_WEBHOOK_URL, embeds);
    log.push(`Discord: ${ok ? "sent" : "FAILED"}`);
  }
  await appendHistory(env, history);
  return dirty;
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

  if (!tripwire || !baseline || missing.length) {
    if (missing.length) log.push(`baselining new artist(s): ${missing.join(", ")}`);
    const dirty = await checkAll(env, state, log);
    if (dirty) await saveState(env, state);
    return log;
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
    const dirty = await checkAll(env, state, log);
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
      artists: Object.values(state).map((a) => ({
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
