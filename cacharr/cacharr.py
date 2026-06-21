#!/usr/bin/env python3
"""
Cacharr
=======
Finds items stuck in Riven (Scraped/Failed with no cached streams on RD),
searches Prowlarr for torrent hashes, adds them to Real-Debrid to trigger
server-side caching, then resets the item in Riven to re-process once cached.

Nothing is downloaded locally — RD fetches from seeders to their own servers.
State is persisted to /data/cacharr_state.json across restarts.
Web UI available on port 8484.

Usage:
  python /data/cacharr.py          # run as daemon (default)
  python /data/cacharr.py --once   # single cycle then exit
"""

import json
import os
import sys
import time
import logging
import argparse
import threading
import requests
import psycopg2
import psycopg2.extras
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from collections import deque

# ── Config ────────────────────────────────────────────────────────────────────
# All settings can be overridden via environment variables.
# When running as a standalone container, set DB_DSN, PROWLARR_URL,
# PROWLARR_KEY, and RD_API_KEY.  When running inside DUMB via
# riven_scraper_patch.sh the defaults (localhost) still work.

DB_DSN         = os.getenv("DB_DSN",         "postgresql://DUMB:postgres@127.0.0.1:5432/riven")
PROWLARR_URL   = os.getenv("PROWLARR_URL",   "http://localhost:9696")
PROWLARR_KEY   = os.getenv("PROWLARR_KEY",   "")
RD_BASE        = "https://api.real-debrid.com/rest/1.0"
RIVEN_SETTINGS = "/riven/backend/data/settings.json"
STATE_FILE     = os.getenv("STATE_FILE",     "/data/cacharr_state.json")
LOG_FILE       = os.getenv("LOG_FILE",       "/log/cacharr.log")
UI_PORT        = int(os.getenv("UI_PORT",    "8484"))
_RD_API_KEY    = os.getenv("RD_API_KEY",     "")   # set in standalone container; falls back to settings.json
CACHARR_CONFIG_FILE = os.getenv("CONFIG_FILE", "/data/cacharr_config.json")

# ── Force-cycle signal (set via /api/force-cycle) ─────────────────────────────
_force_cycle = threading.Event()

# ── Force-stale-check signal (set via /api/force-stale-check) ─────────────────
_force_stale = threading.Event()

# ── Stuck-items cache (populated each cycle, served to /api/stuck) ─────────────
_stuck_lock  = threading.Lock()
_stuck_cache: dict = {"items": [], "fetched_at": None}

LOOP_INTERVAL     = 600   # seconds between cycles
CACHE_TIMEOUT_H   = 8     # hours before giving up on an RD torrent
MAX_NEW_PER_CYCLE = 20    # max new torrents added to RD per cycle
MIN_SCRAPED_TIMES = 1     # item must have failed this many scrape rounds
MIN_STUCK_HOURS   = 1     # item must have been stuck at least this long
MIN_SEEDERS       = 1     # require at least 1 seeder so RD can actually download the torrent
STALE_ZERO_MINS      = 60   # minutes a torrent can stay at 0% downloading before we drop it
STALE_SELECTING_MINS = 20   # minutes stuck in waiting_files_selection / magnet_conversion before drop
SEARCH_WORKERS    = 8     # parallel Prowlarr search threads
TRIED_EXPIRY_DAYS = 7     # retry a failed hash after this many days
NONE_STRIKE_LIMIT = 3     # remove from pending after this many consecutive None-status checks
# Search miss cooldown: after N consecutive zero-result or wrong-season searches, back off.
# Schedule (minutes): 1st miss=30m, 2nd=60m, 3rd=2h, 4th=4h, 5th+=6h
SEARCH_MISS_BACKOFF_MINS = [30, 60, 120, 240, 360]

STALE_CHECK_INTERVAL_H = 24  # hours between RD health checks on Completed items

TRASH_WORDS = {
    "cam", "camrip", "hdcam", "screener", "scr",
    "telesync", "r5", "pdtv", "ts",
    # Google Drive / fake dump torrents — never contain real video files
    "gdrive", "g-drive",
}

# ── Logging ───────────────────────────────────────────────────────────────────

# Ring buffer for the UI to read from
_log_buffer = deque(maxlen=200)

class _BufferHandler(logging.Handler):
    def emit(self, record):
        _log_buffer.append(self.format(record))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [hunter] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger("hunter")
_buf_handler = _BufferHandler()
_buf_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
log.addHandler(_buf_handler)

# ── Live cycle progress (thread-safe) ─────────────────────────────────────────

_progress_lock = threading.Lock()
_cycle_progress = {
    "phase":        "idle",      # idle | searching | rd_check | adding | pending_check
    "phase_label":  "Idle",
    "search_done":  0,
    "search_total": 0,
    "search_items": [],          # list of {label, done, found} for each search target
    "next_cycle_at": None,       # ISO timestamp of when next cycle fires
    "cycle_running": False,
}

def set_progress(**kwargs):
    with _progress_lock:
        _cycle_progress.update(kwargs)

def get_progress():
    with _progress_lock:
        return dict(_cycle_progress)


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_rd_key():
    if _RD_API_KEY:
        return _RD_API_KEY
    with open(RIVEN_SETTINGS) as f:
        return json.load(f)["downloaders"]["real_debrid"]["api_key"]

def rd_headers():
    return {"Authorization": f"Bearer {load_rd_key()}"}

def utcnow():
    return datetime.utcnow()

def fmt_dt(iso):
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %H:%M")
    except Exception:
        return iso or "—"

def time_left(iso):
    try:
        delta = datetime.fromisoformat(iso) - utcnow()
        mins = int(delta.total_seconds() / 60)
        if mins < 0:
            return "expired"
        return f"{mins}m"
    except Exception:
        return "—"


# ── State ─────────────────────────────────────────────────────────────────────

_state_lock = threading.Lock()

def load_state():
    p = Path(STATE_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"pending": [], "tried_hashes": {}, "stats": {"resolved": 0, "timed_out": 0, "added": 0}}

def load_tried_set(state):
    """Return set of non-expired tried hashes, migrating old list format if needed."""
    raw = state.get("tried_hashes", {})
    cutoff = utcnow() - timedelta(days=TRIED_EXPIRY_DAYS)
    if isinstance(raw, list):
        # Migrate old format (plain list) — treat all as tried today
        now = utcnow().isoformat()
        state["tried_hashes"] = {h: now for h in raw}
        return set(raw)
    # New format: dict of hash -> ISO timestamp
    valid = {h: ts for h, ts in raw.items()
             if datetime.fromisoformat(ts) > cutoff}
    expired = len(raw) - len(valid)
    if expired:
        log.info(f"  Expired {expired} tried hash(es) older than {TRIED_EXPIRY_DAYS} days")
    state["tried_hashes"] = valid
    return set(valid.keys())

def save_tried(state, tried_set):
    """Persist tried set back to state, keeping existing timestamps for old entries."""
    existing = state.get("tried_hashes", {})
    if isinstance(existing, list):
        existing = {}
    now = utcnow().isoformat()
    state["tried_hashes"] = {h: existing.get(h, now) for h in tried_set}


# ── Search miss cooldown ───────────────────────────────────────────────────────

def _search_key(item):
    """Stable cooldown key: 'show:s04' for episode groups, 'movie:tt1234' for movies."""
    if item.get("kind") in ("season_group", "episode"):
        return f"{item['show_title'].lower()}:s{item.get('season_num', 0):02d}"
    return f"movie:{(item.get('imdb_id') or item.get('title', '')).lower()}"

def _is_in_cooldown(state, item):
    cd = state.get("search_cooldowns", {}).get(_search_key(item))
    if not cd:
        return False
    try:
        return utcnow() < datetime.fromisoformat(cd["next_search_at"])
    except Exception:
        return False

def _record_search_miss(state, item):
    key = _search_key(item)
    cds = state.setdefault("search_cooldowns", {})
    failures = cds.get(key, {}).get("failures", 0) + 1
    backoff  = SEARCH_MISS_BACKOFF_MINS[min(failures - 1, len(SEARCH_MISS_BACKOFF_MINS) - 1)]
    next_at  = (utcnow() + timedelta(minutes=backoff)).isoformat()
    cds[key] = {"failures": failures, "next_search_at": next_at}
    log.info(f"    Search miss #{failures} for '{item_label(item)}' — cooldown {backoff}m")

def _clear_search_miss(state, item):
    state.get("search_cooldowns", {}).pop(_search_key(item), None)

def _prune_cooldowns(state):
    """Drop expired cooldown entries so the dict doesn't grow forever."""
    cds = state.get("search_cooldowns", {})
    if not cds:
        return
    now = utcnow()
    expired = [k for k, v in cds.items()
               if datetime.fromisoformat(v.get("next_search_at", "2000-01-01")) <= now]
    for k in expired:
        del cds[k]
    if expired:
        log.info(f"  Pruned {len(expired)} expired search cooldown(s)")


_state_mem = None  # in-memory cache — avoids disk read on every 3s status poll

def save_state(state):
    global _state_mem
    with _state_lock:
        _state_mem = dict(state)
        Path(STATE_FILE).write_text(json.dumps(state, indent=2, default=str))

def get_state():
    global _state_mem
    with _state_lock:
        if _state_mem is None:
            _state_mem = load_state()
        return dict(_state_mem)


# ── Database ──────────────────────────────────────────────────────────────────

STUCK_MOVIES_SQL = """
    SELECT mv.id, mi.title, mi.imdb_id, mi.last_state, mi.scraped_times, mi.scraped_at, 'movie' AS kind
    FROM "Movie" mv
    JOIN "MediaItem" mi ON mv.id = mi.id
    WHERE mi.last_state IN ('Scraped', 'Failed', 'Indexed')
      AND mi.scraped_times >= %s
      AND mi.scraped_at IS NOT NULL
      AND mi.scraped_at < NOW() - (%s * INTERVAL '1 hour')
      AND mi.imdb_id IS NOT NULL
    ORDER BY mi.scraped_times DESC
    LIMIT 100
"""

STUCK_EPISODES_SQL = """
    SELECT ep_mi.id, show_mi.title AS show_title, show_mi.imdb_id,
           season_mi.number AS season_num, ep_mi.number AS ep_num,
           ep_mi.last_state, ep_mi.scraped_times, ep_mi.scraped_at, 'episode' AS kind
    FROM "Episode" ep
    JOIN "MediaItem" ep_mi     ON ep.id         = ep_mi.id
    JOIN "Season"   sn         ON ep.parent_id  = sn.id
    JOIN "MediaItem" season_mi ON sn.id         = season_mi.id
    JOIN "Show"     sh         ON sn.parent_id  = sh.id
    JOIN "MediaItem" show_mi   ON sh.id         = show_mi.id
    WHERE ep_mi.last_state IN ('Scraped', 'Failed', 'Indexed')
      AND ep_mi.scraped_times >= %s
      AND ep_mi.scraped_at IS NOT NULL
      AND ep_mi.scraped_at < NOW() - (%s * INTERVAL '1 hour')
      AND show_mi.imdb_id IS NOT NULL
    ORDER BY ep_mi.scraped_times DESC
    LIMIT 100
"""

def get_stuck_items(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(STUCK_MOVIES_SQL, (MIN_SCRAPED_TIMES, MIN_STUCK_HOURS))
    movies = [dict(r) for r in cur.fetchall()]
    cur.execute(STUCK_EPISODES_SQL, (MIN_SCRAPED_TIMES, MIN_STUCK_HOURS))
    episodes = [dict(r) for r in cur.fetchall()]
    return movies + episodes

def reset_item(conn, item_id, label):
    cur = conn.cursor()
    cur.execute('DELETE FROM "StreamRelation" WHERE parent_id = %s', (item_id,))
    cur.execute('DELETE FROM "StreamBlacklistRelation" WHERE media_item_id = %s', (item_id,))
    cur.execute("""
        UPDATE "MediaItem"
        SET last_state      = 'Indexed',
            scraped_at      = NULL,
            scraped_times   = 0,
            failed_attempts = 0
        WHERE id = %s
    """, (item_id,))
    conn.commit()
    log.info(f"  ✓ Reset '{label}' → Indexed (streams cleared)")


# ── Stale RD content checker ──────────────────────────────────────────────────

def _rd_torrent_exists(torrent_id):
    """Return True if the RD torrent ID still exists in the user's account."""
    try:
        r = requests.get(
            f"{RD_BASE}/torrents/info/{torrent_id}",
            headers=rd_headers(), timeout=15,
        )
        return r.status_code != 404
    except Exception:
        return True  # assume still alive on network error — safer than false-positive reset


def check_stale_completed(conn):
    """Check Completed items whose RD torrent ID no longer exists in the user's account.

    Uses GET /torrents/info/{id} — a 404 means the torrent was removed from My Torrents
    (deleted, expired, or purged by RD).  Network errors are treated as 'still alive'
    to avoid false-positive resets.

    Only processes items that have both an infohash AND a torrent id in active_stream
    (items completed via sync_library have neither and are skipped).
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, type, title,
               active_stream->>'infohash' AS infohash,
               active_stream->>'id'       AS rd_torrent_id
        FROM "MediaItem"
        WHERE last_state = 'Completed'
          AND type IN ('movie', 'episode')
          AND active_stream->>'id' IS NOT NULL
          AND active_stream->>'id' != ''
    """)
    rows = cur.fetchall()
    if not rows:
        log.info("Stale check: no Completed items with RD torrent IDs to check")
        return 0

    log.info(f"Stale check: verifying {len(rows)} RD torrent IDs in user account...")

    stale = []
    for row in rows:
        rd_id = row["rd_torrent_id"]
        if not _rd_torrent_exists(rd_id):
            stale.append(row)

    log.info(
        f"Stale check: {len(rows) - len(stale)}/{len(rows)} torrents still in account, "
        f"{len(stale)} missing"
    )

    if not stale:
        return 0

    reset_count = 0
    reset_cur = conn.cursor()
    for row in stale:
        item_id   = row["id"]
        item_type = row["type"]
        title     = row["title"] or item_id
        h         = (row["infohash"] or "")[:8]
        try:
            reset_cur.execute('DELETE FROM "StreamRelation" WHERE parent_id = %s', (item_id,))
            reset_cur.execute('DELETE FROM "StreamBlacklistRelation" WHERE media_item_id = %s', (item_id,))
            reset_cur.execute("""
                UPDATE "MediaItem" SET
                    last_state      = 'Indexed',
                    symlinked       = false,
                    symlinked_at    = NULL,
                    symlink_path    = NULL,
                    file            = NULL,
                    folder          = NULL,
                    active_stream   = '{}',
                    scraped_at      = NULL,
                    scraped_times   = 0,
                    failed_attempts = 0
                WHERE id = %s
            """, (item_id,))
            log.info(f"  [stale] Reset {item_type} '{title}' — RD torrent {row['rd_torrent_id']} no longer in account")
            reset_count += 1
        except Exception as e:
            log.warning(f"  [stale] Failed to reset {item_id}: {e}")

    conn.commit()
    log.info(f"Stale check complete: reset {reset_count} item(s) → queued for re-scrape")
    return reset_count


# ── Prowlarr ──────────────────────────────────────────────────────────────────

def prowlarr_search(item):
    kind    = item["kind"]
    label   = item_label(item)
    imdb_id = (item.get("imdb_id") or "").strip()
    if kind == "season_group":
        params = {
            "query":  item["show_title"],
            "type":   "tvsearch",
            "season": item["season_num"],
            "apikey": PROWLARR_KEY,
        }
        if imdb_id:
            params["imdbId"] = imdb_id
    elif kind == "episode":
        params = {
            "query":  item["show_title"],
            "type":   "tvsearch",
            "season": item["season_num"],
            "ep":     item["ep_num"],
            "apikey": PROWLARR_KEY,
        }
        if imdb_id:
            params["imdbId"] = imdb_id
    else:
        params = {
            "query":  item["title"],
            "type":   "search",
            "apikey": PROWLARR_KEY,
        }
        if imdb_id:
            params["imdbId"] = imdb_id
    try:
        resp = requests.get(f"{PROWLARR_URL}/api/v1/search", params=params, timeout=120)
        resp.raise_for_status()
        results = resp.json()
        log.info(f"  Prowlarr: {len(results)} results for '{label}'")
        return results
    except Exception as e:
        log.warning(f"  Prowlarr search failed for '{label}': {e}")
        return []


def group_episodes_by_season(episodes):
    """Group episode DB rows into season-level search targets."""
    groups = {}
    for ep in episodes:
        key = (ep["show_title"], ep["imdb_id"], ep["season_num"])
        if key not in groups:
            groups[key] = {
                "kind":       "season_group",
                "show_title": ep["show_title"],
                "imdb_id":    ep["imdb_id"],
                "season_num": ep["season_num"],
                "item_ids":   [],
                "ep_nums":    [],
            }
        groups[key]["item_ids"].append(ep["id"])
        groups[key]["ep_nums"].append(ep["ep_num"])
    result = []
    for g in groups.values():
        g["id"] = g["item_ids"][0]   # representative id for dedup
        result.append(g)
    # Sort by most episodes stuck first (most urgent)
    result.sort(key=lambda x: len(x["item_ids"]), reverse=True)
    return result

def item_label(item):
    if item["kind"] == "season_group":
        eps = sorted(item.get("ep_nums", []))
        ep_str = f" ({len(eps)} eps)" if eps else ""
        return f"{item['show_title']} S{item['season_num']:02d}{ep_str}"
    if item["kind"] == "episode":
        return f"{item['show_title']} S{item['season_num']:02d}E{item['ep_num']:02d}"
    return item["title"]

def extract_hashes(results):
    seen = set()
    hashes = []
    for r in results:
        h = (r.get("infoHash") or "").strip().lower()
        if h and len(h) == 40 and h not in seen:
            seen.add(h)
            hashes.append(h)
    return hashes

_STOP_WORDS = {"the", "a", "an", "and", "or", "of", "in", "to", "with", "for", "is", "at"}

def title_is_relevant(query_title, result_title):
    """Return True if the result title shares at least one significant word with the query.

    Prevents GDrive dumps, multi-movie packs, and totally unrelated results from
    passing the filter. Uses words >= 4 chars to avoid stop-word noise.
    """
    import re
    query_words = {w for w in re.split(r'\W+', query_title.lower())
                   if len(w) >= 4 and w not in _STOP_WORDS}
    if not query_words:
        return True  # can't judge — let it through
    result_lower = result_title.lower()
    return any(w in result_lower for w in query_words)


def season_score(title, season_num):
    """Boost score for results that match the target season or are multi-season packs."""
    import re
    t = title.lower()
    # Exact season match — preferred: smaller, less likely to be DMCA blocked
    if re.search(rf'\bs0*{season_num}\b|\bseason\s*0*{season_num}\b', t):
        return 2
    # Multi-season pack (e.g. "S01-S05 Complete") — fallback if no exact match
    if re.search(r's\d+\s*[-–]\s*s\d+', t) or any(w in t for w in ("complete", "collection", "seasons")):
        return 1
    # Wrong season present — small penalty
    if re.search(r'\bs\d{1,2}e\d|season\s*\d', t):
        return -1
    return 0

def pick_best(results, tried_hashes, label="", season_num=None, query_title=""):
    no_hash = no_tried = no_trash = no_seed = no_rel = 0
    candidates = []
    for r in results:
        h = (r.get("infoHash") or "").strip().lower()
        if not h or len(h) != 40:
            no_hash += 1; continue
        if h in tried_hashes:
            no_tried += 1; continue
        words = set(r.get("title", "").lower().split())
        if words & TRASH_WORDS:
            no_trash += 1; continue
        if (r.get("seeders") or 0) < MIN_SEEDERS:
            no_seed += 1; continue
        if query_title and not title_is_relevant(query_title, r.get("title", "")):
            no_rel += 1; continue
        candidates.append(r)
    if not candidates:
        log.info(f"    '{label}' rejected: {no_hash} no-hash, {no_tried} already-tried, "
                 f"{no_trash} trash, {no_seed} no-seeders, {no_rel} irrelevant")
        return None
    # Sort: season relevance first, then seeder count
    if season_num is not None:
        candidates.sort(key=lambda x: (season_score(x.get("title",""), season_num), x.get("seeders") or 0), reverse=True)
        best = candidates[0]
        sc = season_score(best.get("title", ""), season_num)
        if sc < 0:
            log.info(f"    '{label}': best candidate is wrong season ('{best.get('title','')[:60]}') — skipping to avoid bad content")
            return None
    else:
        candidates.sort(key=lambda x: x.get("seeders") or 0, reverse=True)
        best = candidates[0]
    return best


# ── Real-Debrid ───────────────────────────────────────────────────────────────

def check_rd_cache(hashes):
    cached = set()
    for i in range(0, len(hashes), 100):
        batch = hashes[i:i + 100]
        try:
            resp = requests.get(
                f"{RD_BASE}/torrents/instantAvailability/{'/'.join(batch)}",
                headers=rd_headers(), timeout=30,
            )
            if not resp.ok:
                continue
            for h, info in resp.json().items():
                if info and isinstance(info, dict) and info.get("rd"):
                    cached.add(h.lower())
        except Exception as e:
            log.warning(f"  RD cache check error: {e}")
    return cached

def rd_add(info_hash):
    magnet = f"magnet:?xt=urn:btih:{info_hash}"
    try:
        r = requests.post(
            f"{RD_BASE}/torrents/addMagnet",
            headers=rd_headers(), data={"magnet": magnet}, timeout=30,
        )
        r.raise_for_status()
        torrent_id = r.json().get("id")
        if not torrent_id:
            return None
        requests.post(
            f"{RD_BASE}/torrents/selectFiles/{torrent_id}",
            headers=rd_headers(), data={"files": "all"}, timeout=30,
        )
        return torrent_id
    except Exception as e:
        log.warning(f"  RD add failed for {info_hash}: {e}")
        return None

def rd_status(torrent_id):
    try:
        r = requests.get(
            f"{RD_BASE}/torrents/info/{torrent_id}",
            headers=rd_headers(), timeout=30,
        )
        if not r.ok:
            return None, 0, 0, 0, 0
        d = r.json()
        return (
            d.get("status"),
            d.get("progress", 0),
            d.get("speed", 0) or 0,       # bytes/s
            d.get("seeders", 0) or 0,
            d.get("bytes_left", 0) or 0,
        )
    except Exception:
        return None, 0, 0, 0, 0

def rd_delete(torrent_id):
    try:
        requests.delete(
            f"{RD_BASE}/torrents/delete/{torrent_id}",
            headers=rd_headers(), timeout=15,
        )
    except Exception:
        pass

# ── RD status cache (avoids N sequential API calls on every 3s UI poll) ────────
_rd_status_cache: dict = {}
_RD_CACHE_TTL = 12  # seconds — fast enough for live progress, slow enough to not spam RD

def rd_status_cached(torrent_id):
    now = time.time()
    hit = _rd_status_cache.get(torrent_id)
    if hit and now - hit[0] < _RD_CACHE_TTL:
        return hit[1]
    result = rd_status(torrent_id)
    _rd_status_cache[torrent_id] = (now, result)
    # Evict entries older than 3× TTL
    stale = [k for k, v in _rd_status_cache.items() if now - v[0] > _RD_CACHE_TTL * 3]
    for k in stale:
        _rd_status_cache.pop(k, None)
    return result


# ── Config management ─────────────────────────────────────────────────────────

def get_config_dict():
    return {
        "db_dsn":            DB_DSN,
        "prowlarr_url":      PROWLARR_URL,
        "prowlarr_key":      PROWLARR_KEY,
        "rd_api_key":        _RD_API_KEY,
        "loop_interval":     LOOP_INTERVAL,
        "cache_timeout_h":   CACHE_TIMEOUT_H,
        "max_new_per_cycle": MAX_NEW_PER_CYCLE,
        "min_scraped_times": MIN_SCRAPED_TIMES,
        "min_stuck_hours":   MIN_STUCK_HOURS,
        "min_seeders":       MIN_SEEDERS,
        "stale_zero_mins":        STALE_ZERO_MINS,
        "stale_selecting_mins":   STALE_SELECTING_MINS,
        "search_workers":    SEARCH_WORKERS,
        "tried_expiry_days": TRIED_EXPIRY_DAYS,
        "none_strike_limit":       NONE_STRIKE_LIMIT,
        "stale_check_interval_h":  STALE_CHECK_INTERVAL_H,
    }

def apply_config(cfg, persist=True):
    global DB_DSN, PROWLARR_URL, PROWLARR_KEY, _RD_API_KEY
    global LOOP_INTERVAL, CACHE_TIMEOUT_H, MAX_NEW_PER_CYCLE
    global MIN_SCRAPED_TIMES, MIN_STUCK_HOURS, MIN_SEEDERS, STALE_ZERO_MINS
    global SEARCH_WORKERS, TRIED_EXPIRY_DAYS, NONE_STRIKE_LIMIT, STALE_CHECK_INTERVAL_H
    if "db_dsn"            in cfg: DB_DSN            = str(cfg["db_dsn"])
    if "prowlarr_url"      in cfg: PROWLARR_URL      = str(cfg["prowlarr_url"])
    if "prowlarr_key"      in cfg: PROWLARR_KEY      = str(cfg["prowlarr_key"])
    if "rd_api_key"        in cfg: _RD_API_KEY       = str(cfg["rd_api_key"])
    if "loop_interval"     in cfg: LOOP_INTERVAL     = int(cfg["loop_interval"])
    if "cache_timeout_h"   in cfg: CACHE_TIMEOUT_H   = int(cfg["cache_timeout_h"])
    if "max_new_per_cycle" in cfg: MAX_NEW_PER_CYCLE = int(cfg["max_new_per_cycle"])
    if "min_scraped_times" in cfg: MIN_SCRAPED_TIMES = int(cfg["min_scraped_times"])
    if "min_stuck_hours"   in cfg: MIN_STUCK_HOURS   = int(cfg["min_stuck_hours"])
    if "min_seeders"       in cfg: MIN_SEEDERS       = int(cfg["min_seeders"])
    if "stale_zero_mins"        in cfg: STALE_ZERO_MINS        = int(cfg["stale_zero_mins"])
    if "stale_selecting_mins"   in cfg: STALE_SELECTING_MINS   = int(cfg["stale_selecting_mins"])
    if "search_workers"    in cfg: SEARCH_WORKERS    = int(cfg["search_workers"])
    if "tried_expiry_days" in cfg: TRIED_EXPIRY_DAYS = int(cfg["tried_expiry_days"])
    if "none_strike_limit"      in cfg: NONE_STRIKE_LIMIT      = int(cfg["none_strike_limit"])
    if "stale_check_interval_h" in cfg: STALE_CHECK_INTERVAL_H = int(cfg["stale_check_interval_h"])
    if persist:
        Path(CACHARR_CONFIG_FILE).write_text(json.dumps(cfg, indent=2))
        log.info("Config saved")

def load_cacharr_config():
    p = Path(CACHARR_CONFIG_FILE)
    if not p.exists():
        return
    try:
        apply_config(json.loads(p.read_text()), persist=False)
        log.info(f"Config loaded from {CACHARR_CONFIG_FILE}")
    except Exception as e:
        log.warning(f"Failed to load config file: {e}")

def get_cached_stuck():
    with _stuck_lock:
        return {"items": list(_stuck_cache["items"]), "fetched_at": _stuck_cache["fetched_at"]}

def set_cached_stuck(items):
    with _stuck_lock:
        _stuck_cache["items"]      = [dict(i) for i in items]
        _stuck_cache["fetched_at"] = utcnow().isoformat()


# ── Web UI ────────────────────────────────────────────────────────────────────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cacharr</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#1e2127;color:#d0d5de;font-family:'Segoe UI',system-ui,-apple-system,sans-serif;font-size:14px;height:100vh;display:flex;overflow:hidden}

/* ── Sidebar ── */
.sb{width:210px;height:100vh;background:#161b22;border-right:1px solid #2c3242;display:flex;flex-direction:column;flex-shrink:0;overflow-y:auto}
.sb-logo{padding:14px 16px 10px;display:flex;align-items:center;gap:10px;border-bottom:1px solid #2c3242;margin-bottom:6px}
.sb-logo svg{color:#35c5f4;flex-shrink:0}
.sb-logo-text{font-size:17px;font-weight:700;color:#35c5f4;letter-spacing:-.02em}
.sb-logo-sub{font-size:10px;color:#3d4a5e;margin-top:1px}
.sb-group{padding:8px 12px 2px;font-size:10px;font-weight:700;color:#4a5568;text-transform:uppercase;letter-spacing:.09em}
.sb-item{display:flex;align-items:center;gap:10px;padding:9px 14px;margin:1px 8px;border-radius:6px;cursor:pointer;color:#8892a4;font-size:13px;font-weight:500;text-decoration:none;transition:.15s;border-left:3px solid transparent;position:relative}
.sb-item:hover{background:#1e2535;color:#d0d5de}
.sb-item.active{background:#172538;color:#35c5f4;border-left-color:#35c5f4}
.sb-item svg{width:16px;height:16px;flex-shrink:0}
.sb-ct{margin-left:auto;padding:1px 7px;border-radius:4px;font-size:11px;font-weight:700}
.sb-ct-blue{background:#0e2a43;color:#35c5f4}
.sb-ct-yellow{background:#2d2000;color:#f5a623}
.sb-ct-green{background:#0e2e1f;color:#3ec97f}
.sb-spacer{flex:1}
.sb-foot{padding:10px 14px;font-size:11px;color:#3d4a5e;border-top:1px solid #2c3242}

/* ── Main area ── */
.main{flex:1;height:100vh;overflow-y:auto}
.topbar{height:48px;background:#161b22;border-bottom:1px solid #2c3242;display:flex;align-items:center;padding:0 22px;gap:12px;position:sticky;top:0;z-index:50;flex-shrink:0}
.topbar-title{font-size:15px;font-weight:600;color:#e2e8f0;flex:1}
.topbar-right{display:flex;align-items:center;gap:10px}
.tb-next{font-size:12px;color:#4a5568}
.tb-next span{color:#f5a623;font-weight:600}
.tb-live{font-size:12px;color:#4a5568}
.btn{display:inline-flex;align-items:center;gap:6px;padding:6px 14px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;border:none;transition:.15s;text-decoration:none}
.btn svg{width:13px;height:13px}
.btn-primary{background:#35c5f4;color:#071525}.btn-primary:hover{background:#5dd3f8}
.btn-outline{background:transparent;color:#5d6b81;border:1px solid #2c3242}.btn-outline:hover{border-color:#35c5f4;color:#35c5f4}
.btn-sm{padding:4px 10px;font-size:12px}
.content{padding:18px 22px}

/* ── Stat cards ── */
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}
.stat{background:#242933;border:1px solid #2c3242;border-radius:8px;padding:14px 18px;border-left:3px solid}
.stat-blue{border-left-color:#35c5f4}.stat-green{border-left-color:#3ec97f}.stat-yellow{border-left-color:#f5a623}.stat-red{border-left-color:#e05252}.stat-purple{border-left-color:#a78bfa}.stat-teal{border-left-color:#2dd4bf}
.stat .n{font-size:28px;font-weight:700;line-height:1;margin-bottom:4px}
.stat .l{font-size:11px;color:#5d6b81;text-transform:uppercase;letter-spacing:.06em}
.c-blue{color:#35c5f4}.c-green{color:#3ec97f}.c-yellow{color:#f5a623}.c-red{color:#e05252}.c-purple{color:#a78bfa}.c-teal{color:#2dd4bf}

/* ── Cycle bar ── */
.cyc-bar{background:#242933;border:1px solid #2c3242;border-radius:8px;padding:12px 18px;margin-bottom:18px;display:flex;align-items:center;gap:14px}
.cyc-phase{font-size:13px;font-weight:600;color:#35c5f4;min-width:200px}
.prog-track{flex:1;background:#191e27;border-radius:4px;height:5px;overflow:hidden}
.prog-fill{height:5px;border-radius:4px;transition:width .4s ease;background:#35c5f4}
.prog-fill.g{background:#3ec97f}.prog-fill.y{background:#f5a623}
.prog-pct{font-size:12px;color:#5d6b81;min-width:32px;text-align:right}
.cyc-last{font-size:12px;color:#4a5568;min-width:160px;text-align:right}
.cyc-last span{color:#d0d5de}
.cyc-sync{font-size:12px;color:#4a5568;text-align:right}
.cyc-sync span{color:#3ec97f}

/* ── Search grid ── */
.srch-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:7px;margin-bottom:18px}
.si{background:#1c2130;border:1px solid #2c3242;border-radius:6px;padding:7px 11px;display:flex;align-items:center;gap:8px}
.si-dot{width:7px;height:7px;border-radius:50%;background:#2c3242;flex-shrink:0}
.si-dot.done{background:#3ec97f}.si-dot.active{background:#35c5f4;animation:blink 1s ease infinite}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.3}}
.si-name{font-size:12px;color:#8892a4;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.si-ct{font-size:11px;color:#4a5568;flex-shrink:0}

/* ── Table card ── */
.tbl-card{background:#242933;border:1px solid #2c3242;border-radius:8px;overflow:hidden;margin-bottom:18px}
.tbl-hdr{display:flex;align-items:center;padding:10px 16px 6px;border-bottom:1px solid #2c3242;gap:12px}
.tbl-hdr-title{font-size:11px;font-weight:700;color:#5d6b81;text-transform:uppercase;letter-spacing:.07em;flex:1}
table{width:100%;border-collapse:collapse}
thead{background:#1c2130}
th{padding:8px 16px;font-size:11px;font-weight:700;color:#4a5568;text-transform:uppercase;letter-spacing:.06em;text-align:left;border-bottom:1px solid #2c3242}
td{padding:9px 16px;border-bottom:1px solid #1e2435;color:#d0d5de}
tr:last-child td{border-bottom:none}
tbody tr:hover td{background:#1e2535}
.td-empty{color:#4a5568;font-style:italic;text-align:center;padding:26px}

/* ── Badges ── */
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.03em}
.b-blue{background:#0e2a43;color:#35c5f4}.b-green{background:#0e2e1f;color:#3ec97f}
.b-yellow{background:#2d2000;color:#f5a623}.b-red{background:#2d0e0e;color:#e05252}
.b-gray{background:#1e2435;color:#4a5568}.b-purple{background:#1e1a2e;color:#a78bfa}
.b-teal{background:#0e2422;color:#2dd4bf}

/* ── RD progress ── */
.rd-bar{display:flex;align-items:center;gap:7px}
.rd-track{background:#191e27;border-radius:3px;height:4px;width:64px;flex-shrink:0}
.rd-fill{height:4px;border-radius:3px;background:#35c5f4}
.rd-pct{font-size:12px;color:#5d6b81}

/* ── Section label ── */
.sec-lbl{font-size:11px;font-weight:700;color:#4a5568;text-transform:uppercase;letter-spacing:.08em;margin-bottom:9px}

/* ── Progress bar (library) ── */
.lib-prog{display:flex;flex-direction:column;gap:8px;margin-bottom:18px}
.lib-row{background:#242933;border:1px solid #2c3242;border-radius:8px;padding:14px 18px}
.lib-row-hdr{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.lib-row-title{font-size:13px;font-weight:600;color:#e2e8f0}
.lib-row-pct{font-size:13px;font-weight:700}
.lib-track{background:#191e27;border-radius:6px;height:8px;overflow:hidden}
.lib-fill{height:8px;border-radius:6px;transition:width .6s ease}
.lib-fill.movie{background:#35c5f4}
.lib-fill.episode{background:#3ec97f}
.lib-detail{display:flex;gap:18px;margin-top:10px;flex-wrap:wrap}
.lib-detail-item{font-size:12px;color:#5d6b81}
.lib-detail-item span{font-weight:600;color:#8892a4}

/* ── Settings ── */
.cfg-section{background:#242933;border:1px solid #2c3242;border-radius:8px;padding:18px 20px;margin-bottom:14px}
.cfg-section h3{font-size:13px;font-weight:700;color:#8892a4;margin-bottom:14px;padding-bottom:9px;border-bottom:1px solid #2c3242;display:flex;align-items:center;gap:8px;text-transform:uppercase;letter-spacing:.05em}
.cfg-section h3 svg{color:#35c5f4}
.cfg-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 28px}
.form-row{display:grid;grid-template-columns:180px 1fr;align-items:start;gap:10px;margin-bottom:12px}
.form-row:last-child{margin-bottom:0}
.form-lbl{font-size:13px;color:#8892a4;padding-top:8px}
.form-hint{font-size:11px;color:#4a5568;margin-top:2px}
.form-input{width:100%;background:#191e27;border:1px solid #2c3242;border-radius:6px;padding:7px 11px;color:#e2e8f0;font-size:13px;outline:none;font-family:inherit;transition:.15s}
.form-input:focus{border-color:#35c5f4;box-shadow:0 0 0 2px #35c5f420}
.form-row-wide{display:grid;grid-template-columns:180px 1fr;align-items:start;gap:10px;margin-bottom:12px}
.input-wrap{display:flex;gap:8px;align-items:center;flex:1}
.pw-wrap{position:relative;flex:1}
.pw-wrap .form-input{padding-right:44px}
.pw-toggle{position:absolute;right:9px;top:50%;transform:translateY(-50%);font-size:11px;color:#4a5568;cursor:pointer;user-select:none}
.pw-toggle:hover{color:#35c5f4}
.test-res{font-size:12px;padding:3px 9px;border-radius:4px;display:none;align-self:center}
.test-ok{background:#0e2e1f;color:#3ec97f}
.test-fail{background:#2d0e0e;color:#e05252}
.test-info{background:#0e2a43;color:#35c5f4}
.cfg-actions{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:4px}
.save-ok{font-size:12px;color:#3ec97f;display:none}

/* ── System status ── */
.sys-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:18px}
.sys-card{background:#242933;border:1px solid #2c3242;border-radius:8px;padding:13px 16px}
.sys-card .k{font-size:11px;color:#4a5568;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.sys-card .v{font-size:14px;color:#e2e8f0;font-weight:500}
.sys-card .v2{font-size:12px;color:#5d6b81;margin-top:2px}

/* ── Filter bar ── */
.filter-bar{display:flex;gap:6px;align-items:center;margin-bottom:10px}
.filter-btn{padding:3px 11px;border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;border:1px solid #2c3242;background:transparent;color:#5d6b81;transition:.15s}
.filter-btn.active{border-color:#35c5f4;color:#35c5f4;background:#0e2a43}
.mini-input{background:#191e27;border:1px solid #2c3242;border-radius:6px;padding:5px 9px;color:#e2e8f0;font-size:12px;min-width:180px;outline:none}
.mini-input:focus{border-color:#35c5f4;box-shadow:0 0 0 2px #35c5f420}

/* ── Log ── */
.log-panel{background:#191e27;border:1px solid #2c3242;border-radius:8px;padding:13px 15px;font-family:'Cascadia Code',Consolas,'Courier New',monospace;font-size:12px;line-height:1.65;max-height:440px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.log-ERROR{color:#e05252}.log-WARN{color:#f5a623}.log-INFO{color:#4a5568}
.log-SYNC{color:#3ec97f}.log-STALE{color:#a78bfa}
.toast-wrap{position:fixed;right:14px;bottom:14px;display:flex;flex-direction:column;gap:8px;z-index:2000;pointer-events:none}
.toast{background:#161b22;border:1px solid #2c3242;color:#d0d5de;border-left:3px solid #35c5f4;padding:8px 10px;border-radius:6px;min-width:220px;max-width:320px;font-size:12px;box-shadow:0 8px 20px #00000050;opacity:0;transform:translateY(8px);transition:opacity .2s ease,transform .2s ease}
.toast.show{opacity:1;transform:translateY(0)}
.toast.ok{border-left-color:#3ec97f}
.toast.err{border-left-color:#e05252}
.toast.warn{border-left-color:#f5a623}

/* Accessibility */
.sb-item:focus-visible,.btn:focus-visible,.filter-btn:focus-visible,.form-input:focus-visible,.pw-toggle:focus-visible{outline:2px solid #35c5f4;outline-offset:2px}

/* Responsive */
@media (max-width:1200px){
  .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .sys-grid{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media (max-width:900px){
  body{flex-direction:column;overflow:auto;height:auto}
  .sb{width:100%;height:auto;border-right:none;border-bottom:1px solid #2c3242;flex-direction:row;align-items:center;overflow-x:auto;overflow-y:hidden}
  .sb-logo{padding:10px 12px;border-bottom:none;margin-bottom:0;border-right:1px solid #2c3242;flex-shrink:0}
  .sb-logo-sub,.sb-group,.sb-spacer,.sb-foot{display:none}
  .sb-item{border-left:none;border-bottom:3px solid transparent;border-radius:0;margin:0;padding:12px;flex-shrink:0}
  .sb-item.active{border-left-color:transparent;border-bottom-color:#35c5f4}
  .main{height:auto}
  .content{padding:12px}
  .topbar{padding:0 12px}
  .topbar-right{flex-wrap:wrap;justify-content:flex-end}
  .cyc-bar{flex-wrap:wrap}
  .cyc-phase,.cyc-last,.cyc-sync{min-width:unset;text-align:left}
  .tbl-card{overflow-x:auto}
  table{min-width:780px}
  .cfg-grid{grid-template-columns:1fr}
  .form-row,.form-row-wide{grid-template-columns:1fr;gap:6px}
}
@media (max-width:600px){
  .stats,.sys-grid{grid-template-columns:1fr}
  .stat .n{font-size:24px}
}
</style>
</head>
<body>

<!-- Sidebar -->
<nav class="sb">
  <div class="sb-logo">
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
    <div><div class="sb-logo-text">Cacharr</div><div class="sb-logo-sub">Cache Recovery</div></div>
  </div>
  <div class="sb-group">Main</div>
  <a class="sb-item active" data-page="dashboard" href="#" aria-current="page" onclick="event.preventDefault();nav('dashboard')" onkeydown="navFromKey(event,'dashboard')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
    Dashboard
  </a>
  <a class="sb-item" data-page="queue" href="#" onclick="event.preventDefault();nav('queue')" onkeydown="navFromKey(event,'queue')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg>
    Queue
    <span id="sb-q" class="sb-ct sb-ct-blue" style="display:none"></span>
  </a>
  <a class="sb-item" data-page="wanted" href="#" onclick="event.preventDefault();nav('wanted')" onkeydown="navFromKey(event,'wanted')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
    Wanted
    <span id="sb-w" class="sb-ct sb-ct-yellow" style="display:none"></span>
  </a>
  <a class="sb-item" data-page="library" href="#" onclick="event.preventDefault();nav('library')" onkeydown="navFromKey(event,'library')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>
    Library
    <span id="sb-l" class="sb-ct sb-ct-green" style="display:none"></span>
  </a>
  <div class="sb-group">Config</div>
  <a class="sb-item" data-page="settings" href="#" onclick="event.preventDefault();nav('settings')" onkeydown="navFromKey(event,'settings')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l-.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
    Settings
  </a>
  <a class="sb-item" data-page="system" href="#" onclick="event.preventDefault();nav('system')" onkeydown="navFromKey(event,'system')">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
    System
  </a>
  <div class="sb-spacer"></div>
  <div class="sb-foot">Cacharr · port 8484</div>
</nav>

<!-- Main -->
<div class="main">
  <div class="topbar">
    <div class="topbar-title" id="tb-title">Dashboard</div>
    <div class="topbar-right">
      <div class="tb-next">Next cycle: <span id="countdown">—</span></div>
      <div class="tb-live">Updated: <span id="last-poll">never</span></div>
      <button class="btn btn-outline btn-sm" id="btn-auto" onclick="toggleAutoPoll()">Pause Refresh</button>
      <button class="btn btn-primary btn-sm" onclick="forceCycle()">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        Force Cycle
      </button>
    </div>
  </div>

  <div class="content">

    <!-- ── Dashboard ── -->
    <div id="pg-dashboard" class="page">
      <div class="stats">
        <div class="stat stat-blue">  <div class="n c-blue"   id="st-p">—</div><div class="l">Caching on RD</div></div>
        <div class="stat stat-green"> <div class="n c-green"  id="st-r">—</div><div class="l">Resolved All-Time</div></div>
        <div class="stat stat-teal">  <div class="n c-teal"   id="st-sr">—</div><div class="l">Success Rate</div></div>
        <div class="stat stat-red">   <div class="n c-red"    id="st-t">—</div><div class="l">Timed Out</div></div>
      </div>
      <div class="stats">
        <div class="stat stat-teal">  <div class="n c-teal"   id="st-mc">—</div><div class="l">Movies Complete</div></div>
        <div class="stat stat-green"> <div class="n c-green"  id="st-ec">—</div><div class="l">Episodes Complete</div></div>
        <div class="stat stat-purple"><div class="n c-purple" id="st-ls">—</div><div class="l">Synced This Cycle</div></div>
        <div class="stat stat-yellow"><div class="n c-yellow" id="st-mi">—</div><div class="l">Missing (Indexed)</div></div>
      </div>
      <div class="cyc-bar">
        <div class="cyc-phase" id="cyc-phase">Idle</div>
        <div class="prog-track"><div class="prog-fill" id="prog-fill" style="width:0%"></div></div>
        <div class="prog-pct" id="prog-pct">0%</div>
        <div class="cyc-last">Last: <span id="last-cycle">—</span></div>
        <div class="cyc-sync" id="cyc-summary" style="display:none"><span id="cyc-summary-text"></span></div>
        <div class="cyc-sync" id="sync-pill" style="display:none">Synced: <span id="sync-count">—</span></div>
      </div>
      <div id="srch-sec" style="display:none">
        <div class="sec-lbl">Searching Prowlarr</div>
        <div class="srch-grid" id="srch-grid"></div>
      </div>
      <div class="sec-lbl">Currently Caching on Real-Debrid</div>
      <div class="tbl-card">
        <table><thead><tr><th>Item</th><th>Status</th><th>Progress</th><th>Speed</th><th>ETA</th><th>Seeders</th><th>Added</th></tr></thead>
        <tbody id="dash-body"><tr><td class="td-empty" colspan="7">No torrents currently being cached.</td></tr></tbody></table>
      </div>
      <div class="tbl-card" id="hist-card" style="display:none">
        <div class="tbl-hdr"><div class="tbl-hdr-title">Recent Resolutions</div><div style="font-size:11px;color:#4a5568" id="hist-count"></div></div>
        <table><thead><tr><th>Item</th><th>Torrent Used</th><th>Cache Time</th><th>Resolved</th></tr></thead>
        <tbody id="hist-body"></tbody></table>
      </div>
    </div>

    <!-- ── Queue ── -->
    <div id="pg-queue" class="page" style="display:none">
      <div class="tbl-card">
        <div class="tbl-hdr"><div class="tbl-hdr-title">Active RD Queue</div><div style="font-size:11px;color:#334155" id="queue-count"></div><input class="mini-input" id="queue-q" placeholder="Filter queue..." oninput="setQueueQuery(this.value)"></div>
        <table><thead><tr><th>Item</th><th>Status</th><th>Progress</th><th>Speed</th><th>ETA</th><th>Seeders</th><th>Added</th><th>Timeout In</th><th></th></tr></thead>
        <tbody id="queue-body"><tr><td class="td-empty" colspan="9">No torrents currently being cached.</td></tr></tbody></table>
      </div>
    </div>

    <!-- ── Wanted ── -->
    <div id="pg-wanted" class="page" style="display:none">
      <div class="filter-bar">
        <span style="font-size:12px;color:#4a5568;margin-right:2px">Type:</span>
        <button class="filter-btn active" data-wf="all" onclick="setWF('all',this)">All</button>
        <button class="filter-btn" data-wf="movie" onclick="setWF('movie',this)">Movies</button>
        <button class="filter-btn" data-wf="episode" onclick="setWF('episode',this)">Episodes</button>
        <input class="mini-input" id="wanted-q" placeholder="Search title / imdb..." oninput="setWantedQuery(this.value)">
        <span style="flex:1"></span>
        <span style="font-size:12px;color:#4a5568" id="wanted-at"></span>
      </div>
      <div class="tbl-card">
        <div class="tbl-hdr">
          <div class="tbl-hdr-title">Stuck in Riven</div>
          <div style="font-size:11px;color:#4a5568" id="wanted-count"></div>
          <button class="btn btn-outline btn-sm" onclick="loadWanted()" style="margin-left:8px">Refresh</button>
        </div>
        <table><thead><tr><th>Title</th><th>Type</th><th>Season / Ep</th><th>State</th><th>Attempts</th><th>Last Scraped</th><th></th></tr></thead>
        <tbody id="wanted-body"><tr><td class="td-empty" colspan="7">Loading…</td></tr></tbody></table>
      </div>
    </div>

    <!-- ── Library ── -->
    <div id="pg-library" class="page" style="display:none">
      <div style="display:flex;gap:10px;margin-bottom:14px;align-items:center">
        <button class="btn btn-primary btn-sm" onclick="forceLibrarySync()" id="btn-lib-sync">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M13.5 8A5.5 5.5 0 1 1 8 2.5"/><path d="M13.5 2.5v3h-3"/></svg>
          Sync Library Now
        </button>
        <button class="btn btn-outline btn-sm" onclick="loadLibrary()">Refresh Stats</button>
        <span style="font-size:12px;color:#4a5568">Scans library folders and marks matching Riven items as Completed</span>
      </div>
      <div class="lib-prog" id="lib-bars">
        <div class="lib-row">
          <div class="lib-row-hdr">
            <div class="lib-row-title">🎬 Movies</div>
            <div class="lib-row-pct c-teal" id="lib-movie-pct">—%</div>
          </div>
          <div class="lib-track"><div class="lib-fill movie" id="lib-movie-fill" style="width:0%"></div></div>
          <div class="lib-detail" id="lib-movie-detail"></div>
        </div>
        <div class="lib-row">
          <div class="lib-row-hdr">
            <div class="lib-row-title">📺 Episodes</div>
            <div class="lib-row-pct c-green" id="lib-ep-pct">—%</div>
          </div>
          <div class="lib-track"><div class="lib-fill episode" id="lib-ep-fill" style="width:0%"></div></div>
          <div class="lib-detail" id="lib-ep-detail"></div>
        </div>
      </div>

      <div class="tbl-card">
        <div class="tbl-hdr">
          <div class="tbl-hdr-title">Riven State Breakdown</div>
          <button class="btn btn-outline btn-sm" onclick="loadLibrary()">Refresh</button>
        </div>
        <table>
          <thead><tr><th>Type</th><th>Completed</th><th>Indexed</th><th>Scraped</th><th>Paused</th><th>Failed</th><th>Other</th><th>Total</th></tr></thead>
          <tbody id="lib-body"><tr><td class="td-empty" colspan="8">Loading…</td></tr></tbody>
        </table>
      </div>

      <div class="tbl-card">
        <div class="tbl-hdr"><div class="tbl-hdr-title">Last Library Sync</div></div>
        <table>
          <thead><tr><th>Type</th><th>Synced This Cycle</th><th>Run At</th></tr></thead>
          <tbody id="lib-sync-body"><tr><td class="td-empty" colspan="3">No sync data yet.</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- ── Settings ── -->
    <div id="pg-settings" class="page" style="display:none">
      <div class="cfg-section">
        <h3><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>Real-Debrid</h3>
        <div class="form-row">
          <div><div class="form-lbl">API Key</div><div class="form-hint">Your RD API token</div></div>
          <div class="input-wrap">
            <div class="pw-wrap"><input class="form-input" type="password" id="cfg-rd" placeholder="RD API key"><span class="pw-toggle" role="button" tabindex="0" onclick="pw('cfg-rd',this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pw('cfg-rd',this)}">show</span></div>
            <button class="btn btn-outline btn-sm" onclick="test('rd')">Test</button>
            <span class="test-res" id="tr-rd"></span>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <h3><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>Prowlarr</h3>
        <div class="form-row">
          <div><div class="form-lbl">URL</div><div class="form-hint">Base URL</div></div>
          <input class="form-input" type="text" id="cfg-purl" placeholder="http://DUMB:9696">
        </div>
        <div class="form-row">
          <div><div class="form-lbl">API Key</div><div class="form-hint">Prowlarr API key</div></div>
          <div class="input-wrap">
            <div class="pw-wrap"><input class="form-input" type="password" id="cfg-pkey" placeholder="Prowlarr API key"><span class="pw-toggle" role="button" tabindex="0" onclick="pw('cfg-pkey',this)" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();pw('cfg-pkey',this)}">show</span></div>
            <button class="btn btn-outline btn-sm" onclick="test('prowlarr')">Test</button>
            <span class="test-res" id="tr-prowlarr"></span>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <h3><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>Database</h3>
        <div class="form-row">
          <div><div class="form-lbl">Connection String</div><div class="form-hint">PostgreSQL DSN</div></div>
          <div class="input-wrap">
            <input class="form-input" type="text" id="cfg-dsn" placeholder="postgresql://user:pass@host:5432/db" style="flex:1">
            <button class="btn btn-outline btn-sm" onclick="test('db')">Test</button>
            <span class="test-res" id="tr-db"></span>
          </div>
        </div>
      </div>

      <div class="cfg-section">
        <h3><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l-.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Behaviour</h3>
        <div class="cfg-grid">
          <div class="form-row"><div><div class="form-lbl">Loop Interval (s)</div><div class="form-hint">Seconds between cycles</div></div><input class="form-input" type="number" id="cfg-li" min="60"></div>
          <div class="form-row"><div><div class="form-lbl">Cache Timeout (h)</div><div class="form-hint">Hours before giving up on RD torrent</div></div><input class="form-input" type="number" id="cfg-ct" min="1"></div>
          <div class="form-row"><div><div class="form-lbl">Max Adds / Cycle</div><div class="form-hint">New RD torrents per cycle</div></div><input class="form-input" type="number" id="cfg-ma" min="1"></div>
          <div class="form-row"><div><div class="form-lbl">Min Scraped Times</div><div class="form-hint">Riven failures before hunting</div></div><input class="form-input" type="number" id="cfg-ms" min="1"></div>
          <div class="form-row"><div><div class="form-lbl">Min Stuck Hours</div><div class="form-hint">Hours stuck before hunting</div></div><input class="form-input" type="number" id="cfg-mh" min="0"></div>
          <div class="form-row"><div><div class="form-lbl">Min Seeders</div><div class="form-hint">0 = accept all (recommended)</div></div><input class="form-input" type="number" id="cfg-se" min="0"></div>
          <div class="form-row"><div><div class="form-lbl">Tried Expiry (days)</div><div class="form-hint">Days before retrying a failed hash</div></div><input class="form-input" type="number" id="cfg-te" min="1"></div>
          <div class="form-row"><div><div class="form-lbl">None Strike Limit</div><div class="form-hint">RD None-status hits before drop</div></div><input class="form-input" type="number" id="cfg-ns" min="1"></div>
          <div class="form-row"><div><div class="form-lbl">Stale Check Interval (h)</div><div class="form-hint">Hours between RD health checks</div></div><input class="form-input" type="number" id="cfg-sci" min="1"></div>
        </div>
      </div>

      <div class="cfg-actions">
        <span class="save-ok" id="save-ok">Settings saved.</span>
        <button class="btn btn-primary" onclick="saveSettings()">Save Changes</button>
      </div>
    </div>

    <!-- ── System ── -->
    <div id="pg-system" class="page" style="display:none">
      <div class="sys-grid">
        <div class="sys-card"><div class="k">Status</div><div class="v" id="sys-status" style="color:#3ec97f">Running</div></div>
        <div class="sys-card"><div class="k">Last Cycle</div><div class="v" id="sys-last">—</div></div>
        <div class="sys-card"><div class="k">Pending Torrents</div><div class="v" id="sys-pend">—</div></div>
        <div class="sys-card"><div class="k">Library Sync</div><div class="v" id="sys-sync-movies">—</div><div class="v2" id="sys-sync-eps"></div></div>
        <div class="sys-card"><div class="k">Last Sync At</div><div class="v" id="sys-sync-at">—</div></div>
        <div class="sys-card"><div class="k">Movies Complete</div><div class="v" id="sys-mc">—</div></div>
        <div class="sys-card"><div class="k">Tried Hashes</div><div class="v c-yellow" id="sys-tried">—</div><div class="v2">unique torrents attempted</div></div>
        <div class="sys-card" style="border-left-color:#a78bfa"><div class="k">RD Health Check</div><div class="v" id="sys-stale-reset">—</div><div class="v2" id="sys-stale-at" style="font-size:11px;color:#5d6b81;margin-top:2px">Never run</div></div>
      </div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
        <button class="btn btn-outline btn-sm" onclick="forceStaleCheck()" id="btn-stale-check">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M13.5 8A5.5 5.5 0 1 1 8 2.5"/><path d="M13.5 2.5v3h-3"/></svg>
          Check RD Health Now
        </button>
        <span style="font-size:12px;color:#4a5568">Checks all completed items are still cached on RD — resets any that have expired</span>
        <button class="btn btn-outline btn-sm" onclick="clearTried()" id="btn-clear-tried" style="margin-left:16px;border-color:#f5a623;color:#f5a623">
          <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 4h12M6 4V2h4v2M13 4l-1 10H4L3 4"/></svg>
          Clear Tried Hashes
        </button>
        <span style="font-size:12px;color:#4a5568">Lets Cacharr retry all previously-attempted torrents</span>
      </div>
      <div class="filter-bar">
        <span style="font-size:12px;color:#4a5568;margin-right:2px">Filter:</span>
        <button class="filter-btn active" data-lf="all" onclick="setLF('all',this)">All</button>
        <button class="filter-btn" data-lf="WARNING" onclick="setLF('WARNING',this)">Warnings</button>
        <button class="filter-btn" data-lf="ERROR" onclick="setLF('ERROR',this)">Errors</button>
        <button class="filter-btn" data-lf="sync" onclick="setLF('sync',this)">Sync</button>
        <button class="filter-btn" data-lf="stale" onclick="setLF('stale',this)">Stale</button>
      </div>
      <div class="sec-lbl">Log</div>
      <div class="log-panel" id="sys-log">Waiting for first cycle...</div>
    </div>

  </div><!-- /content -->
</div><!-- /main -->
<div class="toast-wrap" id="toast-wrap" aria-live="polite" aria-atomic="true"></div>

<script>
function getPref(key, fallback){
  try{
    const v=window.localStorage.getItem(key);
    return v||fallback;
  }catch{
    return fallback;
  }
}
function setPref(key, value){
  try{window.localStorage.setItem(key, value)}catch{}
}

let curPage=getPref('cacharr.page','dashboard'), nextAt=null, logFilter=getPref('cacharr.logFilter','all'), lastD=null, wantedFilter=getPref('cacharr.wantedFilter','all'), wantedItems=[], queueQuery=getPref('cacharr.queueQuery',''), wantedQuery=getPref('cacharr.wantedQuery',''), autoPoll=getPref('cacharr.autoPoll','1')!=='0';
const TITLES={dashboard:'Dashboard',queue:'Queue',wanted:'Wanted',library:'Library',settings:'Settings',system:'System'};
let pollTimer=null;

function navFromKey(ev,p){
  if(ev.key==='Enter'||ev.key===' '){
    ev.preventDefault();
    nav(p);
  }
}

function nav(p){
  if(!TITLES[p]) p='dashboard';
  document.querySelectorAll('.page').forEach(el=>el.style.display='none');
  document.getElementById('pg-'+p).style.display='';
  document.querySelectorAll('.sb-item').forEach(el=>{
    const active=el.dataset.page===p;
    el.classList.toggle('active',active);
    if(active) el.setAttribute('aria-current','page');
    else el.removeAttribute('aria-current');
  });
  document.getElementById('tb-title').textContent=TITLES[p]||p;
  curPage=p;
  setPref('cacharr.page',p);
  if(p==='settings') loadCfg();
  if(p==='wanted')   loadWanted();
  if(p==='library')  loadLibrary();
  if(p==='system'&&lastD) renderSys(lastD);
}

async function forceCycle(){
  try{ await fetch('/api/force-cycle',{method:'POST'}); }catch{}
  const b=document.querySelector('button[onclick="forceCycle()"]');
  const orig=b.innerHTML; b.textContent='Triggered!';
  setTimeout(()=>{b.innerHTML=orig},2000);
  toast('Force cycle requested','ok');
}

function toast(msg, kind='ok'){
  const w=document.getElementById('toast-wrap');
  if(!w) return;
  const t=document.createElement('div');
  t.className='toast '+kind;
  t.textContent=msg;
  w.appendChild(t);
  requestAnimationFrame(()=>t.classList.add('show'));
  setTimeout(()=>{t.classList.remove('show');setTimeout(()=>t.remove(),220);},2600);
}

const BD={downloading:['blue','downloading'],magnet_conversion:['yellow','converting'],waiting_files_selection:['yellow','selecting'],queued:['yellow','queued'],uploading:['blue','uploading'],seeding:['green','seeding'],downloaded:['green','done'],error:['red','error'],magnet_error:['red','magnet err'],dead:['red','dead'],virus:['red','virus']};
function bdg(s){const[c,l]=BD[s]||['gray',s||'unknown'];return`<span class="badge b-${c}">${l}</span>`}
function rdBar(p){p=Math.min(100,Math.max(0,parseInt(p)||0));return`<div class="rd-bar"><div class="rd-track"><div class="rd-fill" style="width:${p}%"></div></div><span class="rd-pct">${p}%</span></div>`}
function fmtT(iso){if(!iso)return'—';try{return new Date(iso+'Z').toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}catch{return iso}}
function fmtLeft(iso){if(!iso)return'—';try{const s=Math.max(0,Math.round((new Date(iso+'Z')-Date.now())/1000));return s>3600?`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`:s>60?`${Math.floor(s/60)}m ${s%60}s`:`${s}s`}catch{return'—'}}
function fmtSpeed(bps){if(!bps)return'—';if(bps>=1e6)return(bps/1e6).toFixed(1)+' MB/s';if(bps>=1e3)return Math.round(bps/1e3)+' KB/s';return bps+' B/s'}
function fmtEta(bps,bl){if(!bps||!bl)return'—';const s=Math.round(bl/bps);return s>3600?`${Math.floor(s/3600)}h ${Math.floor(s%3600/60)}m`:s>60?`${Math.floor(s/60)}m ${s%60}s`:`${s}s`}
function fmtDT(iso){if(!iso)return'—';return String(iso).substring(0,16).replace('T',' ')}
function num(n){return n==null?'—':n.toLocaleString()}

function tick(){const el=document.getElementById('countdown');if(!nextAt){el.textContent='—';return}const s=Math.max(0,Math.round((new Date(nextAt+'Z')-Date.now())/1000));el.textContent=s>=60?`${Math.floor(s/60)}m ${s%60}s`:`${s}s`}
setInterval(tick,1000);

function renderDash(d){
  const prog=d.progress||{},stats=d.stats||{},pnd=d.pending||[],ls=d.library_sync||{};
  document.getElementById('st-p').textContent=pnd.length;
  document.getElementById('st-r').textContent=num(stats.resolved);
  document.getElementById('st-t').textContent=num(stats.timed_out);
  const tot=(stats.resolved||0)+(stats.timed_out||0);
  document.getElementById('st-sr').textContent=tot>0?Math.round((stats.resolved||0)/tot*100)+'%':'—';
  document.getElementById('last-cycle').textContent=stats.last_cycle?stats.last_cycle.replace(/ UTC$/,''):'not yet';
  // Last cycle summary
  const cs=d.last_cycle_summary||{};
  const csel=document.getElementById('cyc-summary');
  if(cs.stuck_found!=null||cs.resolved!=null){
    const parts=[];
    if(cs.stuck_found) parts.push(cs.stuck_found+' found');
    if(cs.new_adds)    parts.push(cs.new_adds+' added');
    if(cs.resolved)    parts.push(cs.resolved+' resolved');
    if(parts.length){csel.style.display='';document.getElementById('cyc-summary-text').textContent=parts.join(' · ');}
    else csel.style.display='none';
  }
  // Library sync stats
  const syncTotal=(ls.movies||0)+(ls.episodes||0);
  document.getElementById('st-ls').textContent=syncTotal;
  // Sync pill
  const sp=document.getElementById('sync-pill');
  if(syncTotal>0){sp.style.display='';document.getElementById('sync-count').textContent=syncTotal+' items';}
  else sp.style.display='none';
  nextAt=prog.next_cycle_at||null; tick();
  const qb=document.getElementById('sb-q');
  if(pnd.length>0){qb.textContent=pnd.length;qb.style.display=''}else qb.style.display='none';
  const phase=prog.phase||'idle';
  document.getElementById('cyc-phase').textContent=prog.phase_label||'Idle';
  let pct=0,fc='';
  if(phase==='searching'){pct=Math.round((prog.search_done||0)/Math.max(prog.search_total||1,1)*100)}
  else if(phase==='rd_check'){pct=100;fc='y'}else if(phase==='adding'){pct=100;fc='g'}
  else if(phase==='pending_check'){pct=20}else if(phase==='db_query'){pct=10}
  const f=document.getElementById('prog-fill');f.style.width=pct+'%';f.className='prog-fill '+fc;
  document.getElementById('prog-pct').textContent=pct+'%';
  const ss=document.getElementById('srch-sec'),items=prog.search_items||[],done=prog.search_done||0;
  if(phase==='searching'&&items.length>0){
    ss.style.display='';
    document.getElementById('srch-grid').innerHTML=items.map((it,i)=>{
      const st=it.done?'done':(i<done+2?'active':'');
      return`<div class="si"><div class="si-dot ${st}"></div><div class="si-name">${it.label}</div><div class="si-ct">${it.done&&it.found!=null?it.found+' results':''}</div></div>`;
    }).join('');
  }else if(phase==='idle') setTimeout(()=>{ss.style.display='none'},5000);
  const tb=document.getElementById('dash-body');
  tb.innerHTML=pnd.length===0?'<tr><td class="td-empty" colspan="7">No torrents currently being cached.</td></tr>'
    :pnd.map(r=>`<tr>
      <td style="max-width:260px"><div style="font-weight:500;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.torrent_title||r.label}">${r.torrent_title||r.label}</div><div style="font-size:11px;color:#5d6b81;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.torrent_title?r.label:''}</div></td>
      <td>${bdg(r.live_status)}</td>
      <td>${rdBar(r.live_progress)}</td>
      <td style="color:#35c5f4;font-weight:600">${fmtSpeed(r.live_speed)}</td>
      <td style="color:#8892a4">${fmtEta(r.live_speed,r.live_bytes_left)}</td>
      <td style="color:#5d6b81">${r.live_seeders||'—'}</td>
      <td style="color:#5d6b81">${fmtT(r.added_at)}</td>
    </tr>`).join('');
  // History table
  const hist=d.history||[];
  const hc=document.getElementById('hist-card');
  if(hist.length>0){
    hc.style.display='';
    document.getElementById('hist-count').textContent=hist.length+' recent';
    document.getElementById('hist-body').innerHTML=hist.map(h=>`<tr>
      <td style="font-weight:500;color:#e2e8f0;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${h.label}</td>
      <td style="font-size:12px;color:#8892a4;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${h.torrent_title||''}">${h.torrent_title||'—'}</td>
      <td style="color:#35c5f4;font-size:12px">${h.was_pending?h.time_to_cache_mins+'m':'instant'}</td>
      <td style="color:#5d6b81;font-size:12px">${fmtDT(h.resolved_at)}</td>
    </tr>`).join('');
  }
}

function renderQueue(d){
  const pnd=d.pending||[];
  const q=(queueQuery||'').trim().toLowerCase();
  const rows=q?pnd.filter(r=>((r.torrent_title||r.label||'')+' '+(r.label||'')).toLowerCase().includes(q)):pnd;
  document.getElementById('queue-count').textContent=rows.length?rows.length+' active':'';
  document.getElementById('queue-body').innerHTML=rows.length===0
    ?'<tr><td class="td-empty" colspan="9">No torrents currently being cached.</td></tr>'
    :rows.map((r,i)=>`<tr>
      <td style="max-width:240px"><div style="font-weight:500;color:#e2e8f0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${r.torrent_title||r.label}">${r.torrent_title||r.label}</div><div style="font-size:11px;color:#5d6b81;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${r.torrent_title?r.label:''}</div></td>
      <td>${bdg(r.live_status)}</td>
      <td>${rdBar(r.live_progress)}</td>
      <td style="color:#35c5f4;font-weight:600">${fmtSpeed(r.live_speed)}</td>
      <td style="color:#8892a4">${fmtEta(r.live_speed,r.live_bytes_left)}</td>
      <td style="color:#5d6b81">${r.live_seeders||'—'}</td>
      <td style="color:#5d6b81">${fmtT(r.added_at)}</td>
      <td style="color:#f5a623;font-weight:600">${fmtLeft(r.timeout_at)}</td>
      <td><button class="btn btn-outline btn-sm" style="color:#e05252;border-color:#e05252;padding:2px 8px" onclick="cancelPending('${r.rd_torrent_id}',this)">Cancel</button></td>
    </tr>`).join('');
}

async function forceLibrarySync(){
  const b=document.getElementById('btn-lib-sync');
  const orig=b.innerHTML; b.textContent='Syncing…'; b.disabled=true;
  try{ await fetch('/api/force-library-sync',{method:'POST'}); }catch{}
  toast('Library sync requested','ok');
  setTimeout(()=>{b.innerHTML=orig;b.disabled=false;loadLibrary();},3000);
}

async function clearTried(){
  if(!confirm('Clear all tried hashes? Cacharr will retry every previously-attempted torrent on the next cycle.')) return;
  const b=document.getElementById('btn-clear-tried');
  b.textContent='Clearing…'; b.disabled=true;
  try{
    const r=await fetch('/api/clear-tried',{method:'POST'});
    const d=await r.json();
    await poll();
    b.textContent=`Cleared ${d.cleared||0} hashes`;
    toast(`Cleared ${d.cleared||0} tried hashes`,'warn');
  }catch{b.textContent='Error';}
  setTimeout(()=>{b.innerHTML='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M2 4h12M6 4V2h4v2M13 4l-1 10H4L3 4"/></svg> Clear Tried Hashes';b.disabled=false;},3000);
}

async function retryItem(itemId, btn){
  btn.textContent='…'; btn.disabled=true;
  try{
    const r=await fetch('/api/retry-item',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({item_id:itemId})});
    const d=await r.json();
    btn.textContent=d.ok?'✓ Queued':'Error';
    if(d.ok) toast('Item queued for retry','ok');
    else toast('Retry failed','err');
  }catch{btn.textContent='Error';}
  setTimeout(()=>loadWanted(),1500);
}

async function cancelPending(rdId, btn){
  btn.textContent='…'; btn.disabled=true;
  try{
    const r=await fetch('/api/cancel-pending',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({rd_torrent_id:rdId})});
    const d=await r.json();
    await poll();
    btn.textContent=d.ok?'✓':'Error';
    if(d.ok) toast('Pending torrent cancelled','warn');
    else toast('Cancel failed','err');
  }catch{btn.textContent='Error';}
  setTimeout(()=>{if(btn&&btn.isConnected)btn.disabled=false;},1000);
}

async function forceStaleCheck(){
  const b=document.getElementById('btn-stale-check');
  b.textContent='Triggered…'; b.disabled=true;
  try{ await fetch('/api/force-stale-check',{method:'POST'}); }catch{}
  setTimeout(()=>{b.innerHTML='<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2"><path d="M13.5 8A5.5 5.5 0 1 1 8 2.5"/><path d="M13.5 2.5v3h-3"/></svg> Check RD Health Now';b.disabled=false;},3000);
  toast('RD health check requested','ok');
}

function renderSys(d){
  const stats=d.stats||{},pnd=d.pending||[],ls=d.library_sync||{},sc=d.stale_check||{};
  document.getElementById('sys-last').textContent=stats.last_cycle||'not yet';
  document.getElementById('sys-pend').textContent=pnd.length;
  document.getElementById('sys-tried').textContent=(d.tried_count||0).toLocaleString();
  document.getElementById('sys-sync-movies').textContent=(ls.movies||0)+' movies';
  document.getElementById('sys-sync-eps').textContent=(ls.episodes||0)+' episodes';
  document.getElementById('sys-sync-at').textContent=fmtDT(ls.run_at)||'—';
  if(sc.run_at){
    document.getElementById('sys-stale-reset').textContent=(sc.reset||0)+' item(s) reset';
    document.getElementById('sys-stale-reset').style.color=sc.reset>0?'#a78bfa':'#3ec97f';
    document.getElementById('sys-stale-at').textContent='Last: '+fmtDT(sc.run_at);
  }
  const lines=(d.log_lines||[]).filter(l=>{
    if(logFilter==='all') return true;
    if(logFilter==='sync') return l.toLowerCase().includes('sync')||l.toLowerCase().includes('library');
    if(logFilter==='stale') return l.toLowerCase().includes('stale');
    return l.includes(logFilter);
  });
  const lp=document.getElementById('sys-log');
  lp.innerHTML=lines.map(l=>{
    const c=l.includes('ERROR')?'log-ERROR':l.includes('WARNING')?'log-WARN':l.toLowerCase().includes('stale')?'log-STALE':l.toLowerCase().includes('sync')?'log-SYNC':'log-INFO';
    return`<span class="${c}">${l}</span>`;
  }).join('\\n')||'No log entries.';
  lp.scrollTop=lp.scrollHeight;
}

function setLF(f,btn){
  logFilter=f;
  setPref('cacharr.logFilter',f);
  document.querySelectorAll('[data-lf]').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  if(lastD) renderSys(lastD);
}

async function poll(){
  if(!autoPoll) return;
  try{
    const r=await fetch('/api/status');if(!r.ok)return;
    const d=await r.json();lastD=d;
    document.getElementById('last-poll').textContent=new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    renderDash(d);
    if(curPage==='queue') renderQueue(d);
    if(curPage==='system') renderSys(d);
    // Library stats — now populated from status directly (cached each cycle)
    if(d.library_stats){
      const ms=d.library_stats;
      const mComp=(ms.movie||{}).Completed||0;
      const eComp=(ms.episode||{}).Completed||0;
      const mTotal=Object.values(ms.movie||{}).reduce((a,b)=>a+b,0)||1;
      const eTotal=Object.values(ms.episode||{}).reduce((a,b)=>a+b,0)||1;
      const mIdx=mTotal-mComp;
      const eIdx=eTotal-eComp;
      document.getElementById('st-mc').textContent=num(mComp);
      document.getElementById('st-ec').textContent=num(eComp);
      document.getElementById('st-mi').textContent=num(mIdx+eIdx);
      document.getElementById('sys-mc').textContent=mComp+' / '+num(mTotal);
    }
  }catch{}
}
function setQueueQuery(v){
  queueQuery=v||'';
  setPref('cacharr.queueQuery',queueQuery);
  if(lastD&&curPage==='queue') renderQueue(lastD);
}

function setWantedQuery(v){
  wantedQuery=v||'';
  setPref('cacharr.wantedQuery',wantedQuery);
  renderWanted();
}

function toggleAutoPoll(){
  autoPoll=!autoPoll;
  setPref('cacharr.autoPoll',autoPoll?'1':'0');
  const b=document.getElementById('btn-auto');
  if(b) b.textContent=autoPoll?'Pause Refresh':'Resume Refresh';
  toast(autoPoll?'Auto refresh resumed':'Auto refresh paused', autoPoll?'ok':'warn');
  if(autoPoll) poll();
}

pollTimer=setInterval(poll,3000);

async function loadLibrary(){
  const tb=document.getElementById('lib-body');
  tb.innerHTML='<tr><td class="td-empty" colspan="8">Loading…</td></tr>';
  try{
    const r=await fetch('/api/library');if(!r.ok)return;
    const d=await r.json();
    const s=d.stats||{};
    const types=['movie','episode','show','season'];
    const labels={movie:'Movie',episode:'Episode',show:'Show',season:'Season'};
    tb.innerHTML=types.map(t=>{
      const row=s[t]||{};
      const comp=row.Completed||0;
      const idx=row.Indexed||0;
      const scr=row.Scraped||0;
      const pau=row.Paused||0;
      const fail=row.Failed||0;
      const other=Object.entries(row).filter(([k])=>!['Completed','Indexed','Scraped','Paused','Failed'].includes(k)).reduce((a,[,v])=>a+v,0);
      const total=Object.values(row).reduce((a,b)=>a+b,0);
      return`<tr>
        <td style="font-weight:600;color:#e2e8f0">${labels[t]||t}</td>
        <td><span class="c-green" style="font-weight:600">${num(comp)}</span></td>
        <td style="color:#94a3b8">${num(idx)}</td>
        <td style="color:#f5a623">${num(scr)||'—'}</td>
        <td style="color:#a78bfa">${num(pau)||'—'}</td>
        <td style="color:#e05252">${num(fail)||'—'}</td>
        <td style="color:#4a5568">${other||'—'}</td>
        <td style="color:#5d6b81;font-weight:600">${num(total)}</td>
      </tr>`;
    }).join('');
    // Progress bars
    const mRow=s.movie||{},eRow=s.episode||{};
    const mComp=mRow.Completed||0,mTotal=Object.values(mRow).reduce((a,b)=>a+b,0)||1;
    const eComp=eRow.Completed||0,eTotal=Object.values(eRow).reduce((a,b)=>a+b,0)||1;
    const mPct=Math.round(mComp/mTotal*100),ePct=Math.round(eComp/eTotal*100);
    document.getElementById('lib-movie-pct').textContent=mPct+'%';
    document.getElementById('lib-movie-fill').style.width=mPct+'%';
    document.getElementById('lib-movie-detail').innerHTML=
      `<div class="lib-detail-item">Completed: <span>${num(mComp)}</span></div>`+
      `<div class="lib-detail-item">Indexed: <span>${num(mRow.Indexed||0)}</span></div>`+
      `<div class="lib-detail-item">Scraped: <span>${num(mRow.Scraped||0)}</span></div>`+
      `<div class="lib-detail-item">Paused: <span>${num(mRow.Paused||0)}</span></div>`+
      `<div class="lib-detail-item">Failed: <span>${num(mRow.Failed||0)}</span></div>`+
      `<div class="lib-detail-item">Total: <span>${num(mTotal)}</span></div>`;
    document.getElementById('lib-ep-pct').textContent=ePct+'%';
    document.getElementById('lib-ep-fill').style.width=ePct+'%';
    document.getElementById('lib-ep-detail').innerHTML=
      `<div class="lib-detail-item">Completed: <span>${num(eComp)}</span></div>`+
      `<div class="lib-detail-item">Indexed: <span>${num(eRow.Indexed||0)}</span></div>`+
      `<div class="lib-detail-item">Scraped: <span>${num(eRow.Scraped||0)}</span></div>`+
      `<div class="lib-detail-item">Paused: <span>${num(eRow.Paused||0)}</span></div>`+
      `<div class="lib-detail-item">Failed: <span>${num(eRow.Failed||0)}</span></div>`+
      `<div class="lib-detail-item">Total: <span>${num(eTotal)}</span></div>`;
    // Update sidebar badge with missing count
    const missing=(mTotal-mComp)+(eTotal-eComp);
    const lb=document.getElementById('sb-l');
    if(missing>0){lb.textContent=missing;lb.style.display='';}else lb.style.display='none';
    // Update dashboard stats
    document.getElementById('st-mc').textContent=num(mComp);
    document.getElementById('st-ec').textContent=num(eComp);
    document.getElementById('st-mi').textContent=num((mTotal-mComp)+(eTotal-eComp));
    document.getElementById('sys-mc').textContent=mComp+' / '+mTotal;
  }catch(e){tb.innerHTML=`<tr><td class="td-empty" colspan="8">Error: ${e}</td></tr>`;}
  // Sync table
  if(lastD&&lastD.library_sync){
    const ls=lastD.library_sync;
    document.getElementById('lib-sync-body').innerHTML=
      `<tr><td style="color:#35c5f4">Movies</td><td style="color:#3ec97f;font-weight:600">${ls.movies||0} synced</td><td style="color:#5d6b81">${fmtDT(ls.run_at)}</td></tr>`+
      `<tr><td style="color:#35c5f4">Episodes</td><td style="color:#3ec97f;font-weight:600">${ls.episodes||0} synced</td><td style="color:#5d6b81">${fmtDT(ls.run_at)}</td></tr>`;
  }
}

function setWF(f,btn){
  wantedFilter=f;
  setPref('cacharr.wantedFilter',f);
  document.querySelectorAll('[data-wf]').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  renderWanted();
}

function renderWanted(){
  const tb=document.getElementById('wanted-body');
  const base=wantedFilter==='all'?wantedItems:wantedItems.filter(it=>it.kind===wantedFilter);
  const q=(wantedQuery||'').trim().toLowerCase();
  const items=q?base.filter(it=>{
    const title=(it.kind==='episode'?it.show_title:(it.title||''))||'';
    const imdb=it.imdb_id||'';
    return (title+' '+imdb).toLowerCase().includes(q);
  }):base;
  document.getElementById('wanted-count').textContent=items.length+' items';
  if(!items.length){tb.innerHTML='<tr><td class="td-empty" colspan="7">No stuck items found.</td></tr>';return;}
  tb.innerHTML=items.map(it=>{
    const isEp=it.kind==='episode',isMov=it.kind==='movie';
    const title=isEp?it.show_title:it.title||'—';
    const imdbLink=it.imdb_id?`<a href="https://www.imdb.com/title/${it.imdb_id}/" target="_blank" style="margin-left:5px;font-size:10px;color:#35c5f4;text-decoration:none;opacity:.7" title="Open on IMDB">↗</a>`:'';
    const se=isEp?`S${String(it.season_num||0).padStart(2,'0')}E${String(it.ep_num||0).padStart(2,'0')}`:'—';
    const typ=isMov?'<span class="badge b-blue">Movie</span>':'<span class="badge b-gray">Episode</span>';
    const stMap={Scraped:'b-yellow',Failed:'b-red',Indexed:'b-purple',Paused:'b-teal'};
    const st=`<span class="badge ${stMap[it.last_state]||'b-gray'}">${it.last_state||'Unknown'}</span>`;
    return`<tr>
      <td style="font-weight:500;color:#e2e8f0;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${title}">${title}${imdbLink}</td>
      <td>${typ}</td>
      <td style="font-family:monospace;font-size:12px;color:#8892a4">${se}</td>
      <td>${st}</td>
      <td style="color:#5d6b81">${it.scraped_times||0}×</td>
      <td style="color:#5d6b81">${fmtDT(it.scraped_at)}</td>
      <td><button class="btn btn-outline btn-sm" style="padding:2px 8px;color:#3ec97f;border-color:#3ec97f" onclick="retryItem('${it.id}',this)">Retry</button></td>
    </tr>`;
  }).join('');
}

async function loadWanted(){
  const tb=document.getElementById('wanted-body');
  tb.innerHTML='<tr><td class="td-empty" colspan="7">Loading stuck items…</td></tr>';
  try{
    const r=await fetch('/api/stuck');if(!r.ok)return;const d=await r.json();
    wantedItems=d.items||[];
    const wb=document.getElementById('sb-w');
    if(wantedItems.length>0){wb.textContent=wantedItems.length;wb.style.display=''}else wb.style.display='none';
    if(d.fetched_at) document.getElementById('wanted-at').textContent='Updated '+fmtDT(d.fetched_at);
    renderWanted();
  }catch(e){tb.innerHTML=`<tr><td class="td-empty" colspan="7">Error: ${e}</td></tr>`;}
}

async function loadCfg(){
  try{
    const r=await fetch('/api/config');if(!r.ok)return;const c=await r.json();
    document.getElementById('cfg-rd').value=c.rd_api_key||'';
    document.getElementById('cfg-purl').value=c.prowlarr_url||'';
    document.getElementById('cfg-pkey').value=c.prowlarr_key||'';
    document.getElementById('cfg-dsn').value=c.db_dsn||'';
    document.getElementById('cfg-li').value=c.loop_interval||600;
    document.getElementById('cfg-ct').value=c.cache_timeout_h||8;
    document.getElementById('cfg-ma').value=c.max_new_per_cycle||20;
    document.getElementById('cfg-ms').value=c.min_scraped_times||2;
    document.getElementById('cfg-mh').value=c.min_stuck_hours||1;
    document.getElementById('cfg-se').value=c.min_seeders||0;
    document.getElementById('cfg-te').value=c.tried_expiry_days||7;
    document.getElementById('cfg-ns').value=c.none_strike_limit||3;
    document.getElementById('cfg-sci').value=c.stale_check_interval_h||24;
  }catch{}
}

async function saveSettings(){
  const cfg={
    rd_api_key:document.getElementById('cfg-rd').value.trim(),
    prowlarr_url:document.getElementById('cfg-purl').value.trim(),
    prowlarr_key:document.getElementById('cfg-pkey').value.trim(),
    db_dsn:document.getElementById('cfg-dsn').value.trim(),
    loop_interval:parseInt(document.getElementById('cfg-li').value)||600,
    cache_timeout_h:parseInt(document.getElementById('cfg-ct').value)||8,
    max_new_per_cycle:parseInt(document.getElementById('cfg-ma').value)||20,
    min_scraped_times:parseInt(document.getElementById('cfg-ms').value)||2,
    min_stuck_hours:parseInt(document.getElementById('cfg-mh').value)||1,
    min_seeders:parseInt(document.getElementById('cfg-se').value)||0,
    tried_expiry_days:parseInt(document.getElementById('cfg-te').value)||7,
    none_strike_limit:parseInt(document.getElementById('cfg-ns').value)||3,
    stale_check_interval_h:parseInt(document.getElementById('cfg-sci').value)||24,
  };
  try{
    const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
    if(r.ok){const el=document.getElementById('save-ok');el.style.display='inline';setTimeout(()=>{el.style.display='none'},3000);toast('Settings saved','ok');}
  }catch{}
}

async function test(type){
  const el=document.getElementById('tr-'+type);
  el.className='test-res test-info';el.textContent='testing…';el.style.display='inline';
  try{
    const r=await fetch('/api/test/'+type);const d=await r.json();
    el.className='test-res '+(d.ok?'test-ok':'test-fail');el.textContent=d.detail||(d.ok?'OK':'Failed');
  }catch(e){el.className='test-res test-fail';el.textContent='Error';}
  setTimeout(()=>{el.style.display='none'},5000);
}

function pw(id,btn){const el=document.getElementById(id);el.type=el.type==='password'?'text':'password';btn.textContent=el.type==='password'?'show':'hide';}

function initUI(){
  const lfBtn=document.querySelector(`[data-lf="${logFilter}"]`)||document.querySelector('[data-lf="all"]');
  if(lfBtn) setLF(logFilter, lfBtn);
  const wfBtn=document.querySelector(`[data-wf="${wantedFilter}"]`)||document.querySelector('[data-wf="all"]');
  if(wfBtn) setWF(wantedFilter, wfBtn);
  const qq=document.getElementById('queue-q');
  if(qq) qq.value=queueQuery;
  const wq=document.getElementById('wanted-q');
  if(wq) wq.value=wantedQuery;
  const ab=document.getElementById('btn-auto');
  if(ab) ab.textContent=autoPoll?'Pause Refresh':'Resume Refresh';
  nav(curPage);
  poll();
}

initUI();
</script>
</body>
</html>"""


def build_html():
    return HTML_TEMPLATE


def build_status_json():
    """Live status for the JS poller."""
    prog  = get_progress()
    state = get_state()
    stats = state.get("stats", {})
    pending = state.get("pending", [])
    # Fetch RD status in parallel with per-entry caching (TTL=12s)
    def _fetch_rd(e):
        st, pr, sp, se, bl = rd_status_cached(e.get("rd_torrent_id", ""))
        return {**e, "live_status": st or "checking", "live_progress": pr,
                "live_speed": sp, "live_seeders": se, "live_bytes_left": bl}
    if pending:
        with ThreadPoolExecutor(max_workers=min(len(pending), 8)) as _p:
            pending_live = list(_p.map(_fetch_rd, pending))
    else:
        pending_live = []
    return {
        "progress":     prog,
        "stats":        stats,
        "pending":      pending_live,
        "log_lines":         list(_log_buffer),
        "library_sync":      state.get("last_library_sync", {}),
        "stale_check":       state.get("last_stale_check", {}),
        "history":           state.get("history", [])[:20],
        "tried_count":       len(state.get("tried_hashes", {})),
        "library_stats":     state.get("library_stats_cache"),
        "last_cycle_summary": state.get("last_cycle_summary", {}),
    }


class UIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                body = build_html().encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path == "/api/status":
            try:
                body = json.dumps(build_status_json(), default=str).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
        elif self.path == "/api/config":
            self._json(get_config_dict())
        elif self.path == "/api/stuck":
            self._json(get_cached_stuck(), default=str)
        elif self.path == "/api/library":
            try:
                c = psycopg2.connect(DB_DSN)
                cur = c.cursor()
                cur.execute("""
                    SELECT type, last_state, COUNT(*)
                    FROM "MediaItem"
                    WHERE type IN ('movie','episode','show','season')
                    GROUP BY type, last_state
                """)
                rows = cur.fetchall()
                c.close()
                stats = {}
                for typ, st, cnt in rows:
                    stats.setdefault(typ, {})[st] = cnt
                self._json({"stats": stats})
            except Exception as ex:
                self._json({"error": str(ex)})
        elif self.path == "/api/test/rd":
            try:
                key = load_rd_key()
                r = requests.get(f"{RD_BASE}/user", headers={"Authorization": f"Bearer {key}"}, timeout=10)
                result = {"ok": r.ok, "detail": f"Hello, {r.json().get('username','user')}" if r.ok else f"HTTP {r.status_code}"}
            except Exception as e:
                result = {"ok": False, "detail": str(e)[:120]}
            self._json(result)
        elif self.path == "/api/test/prowlarr":
            try:
                r = requests.get(f"{PROWLARR_URL}/api/v1/system/status", params={"apikey": PROWLARR_KEY}, timeout=10)
                result = {"ok": r.ok, "detail": f"v{r.json().get('version','?')}" if r.ok else f"HTTP {r.status_code}"}
            except Exception as e:
                result = {"ok": False, "detail": str(e)[:120]}
            self._json(result)
        elif self.path == "/api/test/db":
            try:
                c = psycopg2.connect(DB_DSN); c.close()
                result = {"ok": True, "detail": "Connected"}
            except Exception as e:
                result = {"ok": False, "detail": str(e)[:120]}
            self._json(result)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
        except Exception:
            self.send_response(400); self.end_headers(); return

        if self.path == "/api/config":
            try:
                apply_config(json.loads(body))
                self._json({"ok": True})
            except Exception as e:
                self.send_response(500); self.end_headers(); self.wfile.write(str(e).encode())
        elif self.path == "/api/force-cycle":
            _force_cycle.set()
            self._json({"ok": True})
        elif self.path == "/api/force-stale-check":
            _force_stale.set()
            _force_cycle.set()
            self._json({"ok": True})
        elif self.path == "/api/force-library-sync":
            # Run library sync immediately in a background thread
            def _do_sync():
                try:
                    import sys as _s
                    if "/data" not in _s.path: _s.path.insert(0, "/data")
                    from sync_library import sync_movies as _sm, sync_episodes as _se, _get_conn as _sc
                    _c = _sc()
                    try:
                        m = _sm(_c); e = _se(_c)
                        log.info(f"Manual library sync: {m} movies + {e} episodes marked Completed")
                    finally:
                        _c.close()
                except Exception as ex:
                    log.warning(f"Manual library sync failed: {ex}")
            threading.Thread(target=_do_sync, daemon=True).start()
            self._json({"ok": True})
        elif self.path == "/api/retry-item":
            try:
                data = json.loads(body)
                item_id = data.get("item_id")
                if not item_id:
                    self._json({"ok": False, "error": "missing item_id"}); return
                conn = psycopg2.connect(DB_DSN)
                try:
                    reset_item(conn, item_id, item_id)
                    # Also remove from tried hashes if the item's hash is known
                    state = load_state()
                    conn2_cur = conn.cursor()
                    conn2_cur.execute("SELECT active_stream->>'infohash' FROM \"MediaItem\" WHERE id=%s", (item_id,))
                    row = conn2_cur.fetchone()
                    if row and row[0]:
                        state.get("tried_hashes", {}).pop(row[0].lower(), None)
                        save_state(state)
                finally:
                    conn.close()
                _force_cycle.set()
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif self.path == "/api/cancel-pending":
            try:
                data = json.loads(body)
                rd_id = data.get("rd_torrent_id")
                if not rd_id:
                    self._json({"ok": False, "error": "missing rd_torrent_id"}); return
                rd_delete(rd_id)
                state = load_state()
                state["pending"] = [e for e in state.get("pending", []) if e.get("rd_torrent_id") != rd_id]
                save_state(state)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif self.path == "/api/clear-tried":
            state = load_state()
            count = len(state.get("tried_hashes", {}))
            state["tried_hashes"] = {}
            save_state(state)
            log.info(f"Cleared {count} tried hash(es) — all items eligible for retry")
            self._json({"ok": True, "cleared": count})
        else:
            self.send_response(404); self.end_headers()

    def _json(self, data, **kwargs):
        body = json.dumps(data, **kwargs).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass  # suppress access logs


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def start_ui():
    server = _ThreadingHTTPServer(("0.0.0.0", UI_PORT), UIHandler)
    log.info(f"Web UI listening on http://0.0.0.0:{UI_PORT}")
    server.serve_forever()


# ── Main cycle ────────────────────────────────────────────────────────────────

def run_cycle(conn, state):
    set_progress(cycle_running=True, phase="pending_check", phase_label="Checking pending torrents",
                 search_done=0, search_total=0, search_items=[])

    # Sync Radarr/Sonarr (Decypharr) files → mark Riven items Completed
    try:
        import sys as _sys
        if "/data" not in _sys.path:
            _sys.path.insert(0, "/data")
        from sync_library import sync_movies as _sm, sync_episodes as _se, _get_conn as _sc
        _sconn = _sc()
        try:
            _m = _sm(_sconn)
            _e = _se(_sconn)
        finally:
            _sconn.close()
        if _m + _e:
            log.info(f"Library sync: {_m} movies + {_e} episodes marked Completed")
        state["last_library_sync"] = {
            "movies": _m, "episodes": _e,
            "run_at": utcnow().isoformat()
        }
    except Exception as _e2:
        log.warning(f"Library sync failed: {_e2}")

    # Cache library stats snapshot for Dashboard cards (fast COUNT GROUP BY)
    try:
        _lcur = conn.cursor()
        _lcur.execute("""
            SELECT type, last_state, COUNT(*) FROM "MediaItem"
            WHERE type IN ('movie','episode','show','season')
            GROUP BY type, last_state
        """)
        _lstats = {}
        for _ltyp, _lst, _lcnt in _lcur.fetchall():
            _lstats.setdefault(_ltyp, {})[_lst] = _lcnt
        state["library_stats_cache"] = _lstats
    except Exception as _lse:
        log.debug(f"Library stats cache: {_lse}")

    # Stale RD content check — runs every STALE_CHECK_INTERVAL_H hours or on demand
    # Uses RD /torrents/info/{id} to verify the user's personal torrent still exists.
    # Only checks items whose active_stream contains a valid RD torrent id ("id" key).
    _last_stale = state.get("last_stale_check", {})
    _last_stale_at = _last_stale.get("run_at")
    _stale_due = True
    if _last_stale_at and not _force_stale.is_set():
        try:
            _elapsed_h = (utcnow() - datetime.fromisoformat(_last_stale_at)).total_seconds() / 3600
            _stale_due = _elapsed_h >= STALE_CHECK_INTERVAL_H
        except Exception:
            pass
    if _stale_due:
        _force_stale.clear()
        set_progress(phase="stale_check", phase_label="Checking RD content health")
        try:
            _stale_reset = check_stale_completed(conn)
            state["last_stale_check"] = {
                "reset":  _stale_reset,
                "run_at": utcnow().isoformat(),
            }
        except Exception as _se:
            log.warning(f"Stale check failed: {_se}")
            try: conn.rollback()
            except Exception: pass

    now_str = utcnow().isoformat()

    pending = state.get("pending", [])
    tried   = load_tried_set(state)
    stats   = state.setdefault("stats", {"resolved": 0, "timed_out": 0, "added": 0})
    _cycle_resolved_start  = stats.get("resolved", 0)
    _cycle_timed_out_start = stats.get("timed_out", 0)
    _prune_cooldowns(state)

    # ── 1. Check pending ──────────────────────────────────────────────────────
    log.info(f"Checking {len(pending)} pending torrent(s)...")
    still_pending = []

    for entry in pending:
        # Support both old single-id and new multi-id entries
        item_ids   = entry.get("item_ids") or [entry["item_id"]]
        label      = entry["label"]
        rd_id      = entry["rd_torrent_id"]
        timeout_at = datetime.fromisoformat(entry["timeout_at"])
        status, progress, _spd, _seed, _bl = rd_status(rd_id)

        if status in ("downloaded", "seeding"):
            log.info(f"  ✓ '{label}' cached — resetting {len(item_ids)} item(s) in Riven")
            for iid in item_ids:
                reset_item(conn, iid, label)
            rd_delete(rd_id)
            stats["resolved"] = stats.get("resolved", 0) + len(item_ids)
            _hist = state.setdefault("history", [])
            _mins = int((utcnow() - datetime.fromisoformat(entry["added_at"])).total_seconds() / 60)
            _hist.insert(0, {"label": label, "torrent_title": entry.get("torrent_title", ""), "resolved_at": now_str, "was_pending": True, "time_to_cache_mins": _mins})
            state["history"] = _hist[:100]

        elif status == "downloading" and (progress or 0) < 1:
            added_at = datetime.fromisoformat(entry["added_at"])
            stale_mins = (utcnow() - added_at).total_seconds() / 60
            if stale_mins >= STALE_ZERO_MINS:
                log.info(f"  ✗ '{label}' stale — <1% for {int(stale_mins)}m — dropping, will retry next cycle")
                rd_delete(rd_id)
                stats["timed_out"] = stats.get("timed_out", 0) + len(item_ids)
            else:
                log.info(f"  ↻ '{label}': downloading <1% ({int(stale_mins)}m / {STALE_ZERO_MINS}m before drop)")
                still_pending.append(entry)

        elif utcnow() > timeout_at:
            log.info(f"  ✗ '{label}' timed out (status={status}) — giving up")
            rd_delete(rd_id)
            stats["timed_out"] = stats.get("timed_out", 0) + len(item_ids)

        elif status in ("waiting_files_selection", "magnet_conversion"):
            added_at   = datetime.fromisoformat(entry["added_at"])
            stale_mins = (utcnow() - added_at).total_seconds() / 60
            if stale_mins >= STALE_SELECTING_MINS:
                log.info(f"  ✗ '{label}' stuck in {status} for {int(stale_mins)}m — dropping, will retry")
                rd_delete(rd_id)
                stats["timed_out"] = stats.get("timed_out", 0) + len(item_ids)
            else:
                log.info(f"  ↻ '{label}': {status} ({int(stale_mins)}m / {STALE_SELECTING_MINS}m before drop)")
                still_pending.append(entry)

        elif status in ("error", "magnet_error", "virus", "dead"):
            log.info(f"  ✗ '{label}' RD error: {status} — giving up")
            rd_delete(rd_id)
            stats["timed_out"] = stats.get("timed_out", 0) + len(item_ids)

        elif status is None:
            strikes = entry.get("none_strikes", 0) + 1
            if strikes >= NONE_STRIKE_LIMIT:
                log.info(f"  ✗ '{label}' vanished from RD (None x{NONE_STRIKE_LIMIT}) — will retry with different torrent")
                stats["timed_out"] = stats.get("timed_out", 0) + len(item_ids)
                # Don't rd_delete — it's already gone. Don't remove from tried — try a different hash next cycle.
            else:
                entry["none_strikes"] = strikes
                log.info(f"  ↻ '{label}': RD status None ({strikes}/{NONE_STRIKE_LIMIT})")
                still_pending.append(entry)

        else:
            entry.pop("none_strikes", None)  # reset strike counter on any real status
            log.info(f"  ↻ '{label}': {status} {progress}%")
            still_pending.append(entry)

    state["pending"] = still_pending
    pending_ids = set()
    for e in still_pending:
        for iid in (e.get("item_ids") or [e["item_id"]]):
            pending_ids.add(iid)

    # ── 2. Find stuck items ───────────────────────────────────────────────────
    set_progress(phase="db_query", phase_label="Querying database for stuck items")
    try:
        stuck = get_stuck_items(conn)
        set_cached_stuck(stuck)
    except Exception as e:
        log.error(f"DB query failed: {e}")
        conn.rollback()
        set_progress(cycle_running=False, phase="idle", phase_label="Idle (DB error)")
        return

    # Separate movies and episodes; group episodes by season
    movies_stuck   = [i for i in stuck if i["kind"] == "movie" and i["id"] not in pending_ids]
    episodes_stuck = [i for i in stuck if i["kind"] == "episode"]
    season_groups  = group_episodes_by_season(episodes_stuck)
    season_groups  = [g for g in season_groups if not (set(g["item_ids"]) & pending_ids)]

    all_targets = movies_stuck + season_groups
    log.info(
        f"Found {len(stuck)} stuck item(s) in DB → "
        f"{len(movies_stuck)} movies, {len(season_groups)} season group(s) eligible"
    )
    new_adds = 0

    if not all_targets:
        state["last_cycle_summary"] = {"stuck_found": 0, "new_adds": 0,
            "resolved": stats.get("resolved", 0) - _cycle_resolved_start,
            "timed_out": stats.get("timed_out", 0) - _cycle_timed_out_start}
        set_progress(cycle_running=False, phase="idle", phase_label="Idle")
        return

    # ── 2a. Parallel Prowlarr searches ───────────────────────────────────────
    search_targets = all_targets[:MAX_NEW_PER_CYCLE * 4]
    # Skip items still in search cooldown (zero-result or wrong-season backoff)
    _cooling = [t for t in search_targets if _is_in_cooldown(state, t)]
    search_targets = [t for t in search_targets if not _is_in_cooldown(state, t)]
    if _cooling:
        log.info(f"  Skipping {len(_cooling)} item(s) in search cooldown: "
                 + ", ".join(item_label(t) for t in _cooling[:5]))
    log.info(f"Searching Prowlarr for {len(search_targets)} item(s) in parallel (workers={SEARCH_WORKERS})...")

    # Initialise per-item progress rows
    item_rows = [{"label": item_label(i), "done": False, "found": None} for i in search_targets]
    label_to_idx = {item_label(i): idx for idx, i in enumerate(search_targets)}
    set_progress(phase="searching", phase_label="Searching Prowlarr",
                 search_done=0, search_total=len(search_targets), search_items=list(item_rows))

    search_results = {}  # item_id -> (item, results)
    done_count = 0
    with ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
        future_map = {pool.submit(prowlarr_search, item): item for item in search_targets}
        for fut in as_completed(future_map):
            item = future_map[fut]
            lbl  = item_label(item)
            try:
                results = fut.result()
            except Exception as e:
                log.warning(f"  Search error for '{lbl}': {e}")
                results = []
            search_results[item["id"]] = (item, results)
            done_count += 1
            idx = label_to_idx.get(lbl)
            if idx is not None:
                item_rows[idx]["done"]  = True
                item_rows[idx]["found"] = len(results)
            set_progress(search_done=done_count, search_items=list(item_rows))

    # ── 2b. Batch RD availability check across ALL hashes ────────────────────
    set_progress(phase="rd_check", phase_label="Checking RD cache")
    all_item_hashes = {}   # item_id -> list of hashes
    all_hashes_flat = []
    for item_id, (item, results) in search_results.items():
        hashes = extract_hashes(results)
        all_item_hashes[item_id] = hashes
        all_hashes_flat.extend(hashes)

    log.info(f"Checking {len(all_hashes_flat)} unique hashes against RD cache in one batch...")
    all_cached = check_rd_cache(list(set(all_hashes_flat))) if all_hashes_flat else set()
    if all_cached:
        log.info(f"  {len(all_cached)} hash(es) already cached on RD")

    # ── 2c. Process each item using search+cache results ─────────────────────
    set_progress(phase="adding", phase_label="Adding to Real-Debrid")
    for item in search_targets:
        if new_adds >= MAX_NEW_PER_CYCLE:
            log.info(f"Reached max {MAX_NEW_PER_CYCLE} adds this cycle")
            break

        item_id = item["id"]
        label   = item_label(item)
        _, results = search_results.get(item_id, (item, []))
        hashes  = all_item_hashes.get(item_id, [])

        if not results:
            # Prowlarr has zero coverage — back off so we don't search every 10 min
            _record_search_miss(state, item)
            continue
        if not hashes:
            log.info(f"  '{label}': no valid hashes in results")
            _record_search_miss(state, item)
            continue

        # item_ids: list for season groups, single-element list for movies
        item_ids = item.get("item_ids") or [item_id]

        # Already cached — just reset all items in this group
        cached_for_item = set(hashes) & all_cached
        if cached_for_item:
            log.info(f"  '{label}': {len(cached_for_item)} cached hash(es) — resetting {len(item_ids)} item(s)")
            _clear_search_miss(state, item)
            for iid in item_ids:
                reset_item(conn, iid, label)
            stats["resolved"] = stats.get("resolved", 0) + len(item_ids)
            _hist = state.setdefault("history", [])
            _hist.insert(0, {"label": label, "torrent_title": "(already cached on RD)", "resolved_at": now_str, "was_pending": False, "time_to_cache_mins": 0})
            state["history"] = _hist[:100]
            continue

        query_title = item.get("show_title") or item.get("title") or ""
        sn = item.get("season_num")
        best = pick_best(results, tried, label, season_num=sn, query_title=query_title)
        if not best:
            # Record miss only when there are genuinely no viable candidates (not just all-tried).
            # "All tried" will naturally unblock once TRIED_EXPIRY_DAYS passes.
            if sn is not None:
                viable_untried = [r for r in results
                                  if (r.get("infoHash") or "").lower() not in tried
                                  and season_score(r.get("title", ""), sn) >= 0]
                if not viable_untried:
                    _record_search_miss(state, item)
            else:
                viable_untried = [r for r in results
                                  if (r.get("infoHash") or "").lower() not in tried]
                if not viable_untried:
                    _record_search_miss(state, item)
            log.info(f"  '{label}': no suitable uncached torrent (all tried or no seeders)")
            continue

        h       = best["infoHash"].lower()
        seeders = best.get("seeders") or 0
        indexer = best.get("indexer") or "?"
        title   = best.get("title", "")[:60]
        log.info(f"  '{label}' → adding: '{title}' ({seeders} seeders, {indexer})")

        rd_id = rd_add(h)
        if not rd_id:
            tried.add(h)
            continue

        tried.add(h)
        _clear_search_miss(state, item)  # found something — reset backoff
        timeout_at = (utcnow() + timedelta(hours=CACHE_TIMEOUT_H)).isoformat()
        state["pending"].append({
            "item_ids":      item_ids,
            "item_id":       item_ids[0],  # backward compat
            "label":         label,
            "torrent_title": best.get("title", "")[:80],
            "rd_torrent_id": rd_id,
            "info_hash":     h,
            "added_at":      now_str,
            "timeout_at":    timeout_at,
        })
        stats["added"] = stats.get("added", 0) + len(item_ids)
        new_adds += 1

    save_tried(state, tried)
    stats["last_cycle"] = utcnow().strftime("%b %d %H:%M:%S UTC")
    state["last_cycle_summary"] = {
        "stuck_found": len(all_targets),
        "new_adds":    new_adds,
        "resolved":    stats.get("resolved", 0) - _cycle_resolved_start,
        "timed_out":   stats.get("timed_out", 0) - _cycle_timed_out_start,
    }
    log.info(f"Cycle done — pending: {len(state['pending'])}, new: {new_adds}, resolved all-time: {stats.get('resolved',0)}")
    set_progress(cycle_running=False, phase="idle", phase_label="Idle")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one cycle then exit")
    args = parser.parse_args()

    load_cacharr_config()
    log.info("Cacharr started")
    log.info(f"  Loop interval : {LOOP_INTERVAL}s | Max adds/cycle: {MAX_NEW_PER_CYCLE}")
    log.info(f"  Cache timeout : {CACHE_TIMEOUT_H}h | Min stuck: {MIN_STUCK_HOURS}h / {MIN_SCRAPED_TIMES}x")

    # Start web UI in background thread
    ui_thread = threading.Thread(target=start_ui, daemon=True)
    ui_thread.start()

    if not args.once:
        log.info("Waiting 90s for services to be ready...")
        time.sleep(90)

    while True:
        state = load_state()
        try:
            conn = psycopg2.connect(DB_DSN)
            try:
                run_cycle(conn, state)
            finally:
                conn.close()
        except Exception as e:
            log.error(f"Cycle error: {e}", exc_info=True)

        save_state(state)

        if args.once:
            log.info("--once complete, exiting.")
            break

        next_at = (utcnow() + timedelta(seconds=LOOP_INTERVAL)).isoformat()
        set_progress(next_cycle_at=next_at)
        log.info(f"Sleeping {LOOP_INTERVAL}s (or until forced)...")
        _force_cycle.wait(timeout=LOOP_INTERVAL)
        _force_cycle.clear()


if __name__ == "__main__":
    main()
