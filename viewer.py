#!/usr/bin/env python3
"""
Localhost viewer for the last 14 days of artist daily views.

    python3 viewer.py            # then open http://localhost:8765

Reuses monitor.py's fetcher. STRICTLY READ-ONLY with respect to the monitor's
own files: it never writes state.json or history.jsonl, so running this locally
cannot cause the spurious-revision problem the README warns about. Its own cache
lives in .views_cache.json (gitignored).

Two things shape the design, both consequences of tracking many artists against
a throttled endpoint:

  * The HTTP request NEVER fetches upstream. A background thread tops the cache
    up; /api/views answers instantly from whatever is cached, so a page load is
    not held hostage to N sequential upstream calls.
  * The cache is keyed PER ARTIST. The endpoint throttles probabilistically, so
    every success is banked for the full TTL and never re-requested. Repeated
    passes converge on a complete set instead of re-rolling the dice on all of
    them each time.
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from monitor import ARTISTS, fetch_artist, STATE_PATH  # noqa: E402

CACHE_PATH = os.path.join(HERE, ".views_cache.json")
# Optional: a dump of the Cloudflare Worker's KV state, which is collected from
# Cloudflare IPs and so is never subject to the throttling that hits a home IP.
#   cd worker && npx wrangler kv key get state \
#     --namespace-id=f2707ffcb553477abb9e841fc426b674 --remote > ../kv-state.json
KV_PATH = os.path.join(HERE, "kv-state.json")
HTML_PATH = os.path.join(HERE, "viewer.html")
DAYS = 14
PORT = int(os.environ.get("PORT", "8765"))

CACHE_TTL = 30 * 60      # an artist's data is good for this long
IDLE_SLEEP = 20          # pause between filler sweeps when nothing is due
# If another source (the Worker's KV dump, or state.json committed by the GitHub
# workflow) already has an artist within this many days of the newest data we
# know about, do not spend a throttled local request re-fetching it.
FALLBACK_GRACE_DAYS = 1
FETCH_SPACING = 4.0      # base gap between upstream calls
MAX_SPACING = 60.0       # ceiling when the endpoint is throttling us

_lock = threading.Lock()
_status = {"filling": False, "errors": [], "spacing": FETCH_SPACING}


def _read_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def load_entries():
    return (_read_json(CACHE_PATH) or {}).get("artists") or {}


def save_entries(entries):
    """Persist the cache, merging with whatever is already on disk.

    This write can only ever ADD or UPDATE artists, never drop them. That
    matters because _read_json() returns None on any read failure, which
    load_entries() turns into {} - without this merge a single transient read
    error would shrink the cache to just the artist being written, discarding
    successfully-fetched data that cost scarce un-throttled requests to get.
    """
    try:
        existing = (_read_json(CACHE_PATH) or {}).get("artists") or {}
        merged = {**existing}
        for name, entry in entries.items():
            prev = merged.get(name)
            # Keep whichever version has the more recent trailing date.
            if (not prev or not prev.get("views")
                    or max(entry["views"]) >= max(prev["views"])):
                merged[name] = entry
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump({"artists": merged}, fh)
        os.replace(tmp, CACHE_PATH)       # atomic: readers never see a partial file
    except (OSError, ValueError, KeyError):
        pass


def _state_views(path):
    """Read a monitor-shaped state file into {artist name: {date: views}}."""
    state = _read_json(path)
    if not isinstance(state, dict):
        return {}
    out = {}
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        name, views = entry.get("name"), entry.get("views") or {}
        if name and views:
            out[name] = {d: int(v) for d, v in views.items()}
    return out


def from_state():
    """Fallback data: the Worker's KV dump if present, else local state.json.

    Both files share the monitor's state shape. They are merged per artist,
    keeping whichever has the more recent trailing date - so a KV dump covering
    all 16 artists supplements, rather than replaces, anything local.
    """
    merged = {}
    for path in (STATE_PATH, KV_PATH):
        for name, views in _state_views(path).items():
            if not views:
                continue
            if name not in merged or max(views) > max(merged[name]):
                merged[name] = views
    return merged


def _newest_known(entries, fallback):
    """The most recent date any source has, so staleness is judged relatively."""
    dates = [max(e["views"]) for e in entries.values() if e.get("views")]
    dates += [max(v) for v in fallback.values() if v]
    return max(dates) if dates else None


def due_artists(entries, now=None, fallback=None):
    """Artists worth spending a local request on.

    Skips any artist that another source already covers at (or within
    FALLBACK_GRACE_DAYS of) the newest data anyone has - fetching those locally
    would burn scarce successful requests to re-acquire data we already hold.
    """
    now = now or time.time()
    fallback = fallback if fallback is not None else from_state()
    newest = _newest_known(entries, fallback)
    cutoff = None
    if newest:
        y, m, d = (int(x) for x in newest.split("-"))
        cutoff = (datetime(y, m, d, tzinfo=timezone.utc)
                  - timedelta(days=FALLBACK_GRACE_DAYS)).strftime("%Y-%m-%d")

    due = []
    for a in ARTISTS:
        name = a["name"]
        if now - (entries.get(name, {}).get("ts") or 0) < CACHE_TTL:
            continue                      # already banked locally
        covered = fallback.get(name)
        if cutoff and covered and max(covered) >= cutoff:
            continue                      # another IP already got this one
        due.append(a)
    return due


def filler_loop():
    """Continuously top up expired artists, one at a time, politely.

    Each success is written to the cache immediately, so progress is durable and
    visible in the UI as it happens rather than only at the end of a sweep.
    Spacing backs off while the endpoint is throttling and recovers on success.
    """
    spacing = FETCH_SPACING
    while True:
        entries = load_entries()
        due = due_artists(entries)
        if not due:
            with _lock:
                _status["filling"] = False
            time.sleep(IDLE_SLEEP)
            continue

        with _lock:
            _status["filling"] = True

        for artist in due:
            name = artist["name"]
            try:
                rows = fetch_artist(artist["id"], retries=1)
                entries = load_entries()          # re-read; the file may have moved on
                entries[name] = {
                    "views": {r["date"]: int(r["viewCount"]) for r in rows},
                    "fetched_at": _now_str(), "ts": time.time(),
                }
                save_entries(entries)
                spacing = max(FETCH_SPACING, spacing * 0.7)   # recover on success
                with _lock:
                    _status["errors"] = [e for e in _status["errors"]
                                         if not e.startswith(name + ":")]
                print(f"  + {name}: {len(rows)} days", flush=True)
            except RuntimeError as exc:
                msg = str(exc)
                throttled = "429" in msg
                if throttled:
                    # Back off hard: retrying faster is what keeps a throttle on.
                    spacing = min(MAX_SPACING, max(spacing, FETCH_SPACING) * 2)
                with _lock:
                    _status["errors"] = [e for e in _status["errors"]
                                         if not e.startswith(name + ":")]
                    _status["errors"].append(
                        f"{name}: {'throttled (429)' if throttled else msg[:70]}")
                print(f"  ! {name}: {msg[:80]} (spacing -> {spacing:.0f}s)",
                      file=sys.stderr, flush=True)
            with _lock:
                _status["spacing"] = spacing
            time.sleep(spacing)


def build():
    """Assemble the current best answer. Never touches the network."""
    entries = load_entries()
    state = from_state()
    now = time.time()

    artists, cached, from_st, pending = {}, [], [], []
    for a in ARTISTS:
        name = a["name"]
        if entries.get(name, {}).get("views"):
            artists[name] = entries[name]["views"]
            cached.append(name)
        else:
            if state.get(name):
                artists[name] = state[name]
                from_st.append(name)
            pending.append(name)

    bits = []
    if cached:
        bits.append(f"{len(cached)}/{len(ARTISTS)} live")
    if from_st:
        label = "KV + state" if os.path.exists(KV_PATH) else "state.json"
        bits.append(f"{len(from_st)} from {label}")
    if pending:
        bits.append(f"{len(pending)} still fetching")

    stamps = [e["fetched_at"] for e in entries.values() if e.get("fetched_at")]
    with _lock:
        status = dict(_status)
    return {
        "artists": artists,
        "fetched_at": max(stamps) if stamps else "",
        "source": " · ".join(bits) if bits else "no data yet",
        "pending": pending,
        "errors": status["errors"],
        "filling": status["filling"],
        "spacing": round(status["spacing"]),
        "roster": len(ARTISTS),
        "complete": not pending,
        "degraded": bool(pending or from_st),
    }


def shape(payload):
    """Add per-artist series and summaries.

    Each panel carries its OWN last-14-days window rather than a single shared
    one. These are small multiples - separate charts, not a shared x-axis - so a
    single artist arriving with fresher data must not blank every other panel,
    which is what a union window does during a partial fill. Any panel whose
    range ends earlier than the newest is labelled with its own end date.
    """
    artists = payload.get("artists") or {}
    if not artists:
        return {**payload, "dates": [], "series": [], "summary": [], "newest": None}

    newest = max(max(v) for v in artists.values() if v)
    # A shared window, used only by the table view.
    table_dates = sorted(set().union(*(d.keys() for d in artists.values())))[-DAYS:]

    series, summary = [], []
    for a in ARTISTS:
        name = a["name"]
        views = artists.get(name)
        if not views:
            continue
        own = sorted(views)[-DAYS:]
        vals = [views[d] for d in own]
        if not vals:
            continue
        peak = max(vals)
        behind = own[-1] < newest
        series.append({"name": name, "dates": own, "values": vals,
                       "end_date": own[-1], "behind": behind,
                       "table_values": [views.get(d) for d in table_dates]})
        summary.append({
            "name": name, "behind": behind, "end_date": own[-1],
            "total": sum(vals), "avg": sum(vals) // len(vals),
            "peak": peak, "peak_date": own[vals.index(peak)],
            "latest": vals[-1], "prev": vals[-2] if len(vals) > 1 else None,
        })
    return {**payload, "dates": table_dates, "series": series,
            "summary": summary, "newest": newest}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass                                  # the filler's own logging is enough

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            try:
                with open(HTML_PATH, "rb") as fh:
                    self._send(200, fh.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"viewer.html is missing", "text/plain")
        elif path == "/api/views":
            self._send(200, json.dumps(shape(build())).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    threading.Thread(target=filler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"\n  Artist daily views - last {DAYS} days, {len(ARTISTS)} artists")
    print(f"  http://localhost:{PORT}")
    print(f"  (fetching in the background; the page fills in as data arrives)\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
