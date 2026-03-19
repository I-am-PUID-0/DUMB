"""
sync_library.py — Bridge between Radarr/Sonarr (Decypharr) and Riven.

Scans the library for video files placed by Radarr/Sonarr and marks the
corresponding Riven MediaItem records as Completed so Riven doesn't
re-scrape content that already exists on disk.

Run standalone:  python3 /data/sync_library.py
Or import:       from sync_library import sync_all; sync_all()
"""

import os
import re
import logging

import psycopg2

log = logging.getLogger("sync_library")

MOVIES_DIR = "/mnt/library/movies"
SHOWS_DIR  = "/mnt/library/shows"
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m4v", ".ts"}
DB_DSN     = os.getenv("DB_DSN", "postgresql://DUMB:postgres@127.0.0.1:5432/riven")

COMPLETE_SQL = """
    UPDATE "MediaItem"
    SET last_state   = 'Completed',
        symlinked    = true,
        symlinked_at = NOW(),
        symlink_path = %s,
        file         = %s,
        folder       = %s
    WHERE id = %s
"""


def _source_info(path):
    """Return (real_file, real_folder) for a path, following symlinks."""
    if os.path.islink(path):
        target = os.readlink(path)
        return os.path.basename(target), os.path.basename(os.path.dirname(target))
    return os.path.basename(path), os.path.basename(os.path.dirname(path))


def _find_video(folder_path):
    """Return the first video file path in a folder, or None.

    Accepts symlinks regardless of whether the target resolves — the debrid
    FUSE mount only exists inside the DUMB container, so os.path.exists()
    returns False here even for valid links.
    """
    try:
        entries = os.listdir(folder_path)
    except OSError:
        return None
    for fname in entries:
        if os.path.splitext(fname)[1].lower() in VIDEO_EXTS:
            return os.path.join(folder_path, fname)
    return None


# ---------------------------------------------------------------------------
def sync_movies(conn):
    """Mark Riven movie items Completed when a library file already exists."""
    cur = conn.cursor()
    synced = skipped = no_match = no_file = 0

    for folder in sorted(os.listdir(MOVIES_DIR)):
        if "{imdb-" not in folder:
            continue
        m = re.search(r"\{imdb-(tt\d+)\}", folder)
        if not m:
            continue
        imdb_id     = m.group(1)
        folder_path = os.path.join(MOVIES_DIR, folder)

        video_path = _find_video(folder_path)
        if not video_path:
            no_file += 1
            continue

        cur.execute(
            'SELECT id, last_state, symlinked FROM "MediaItem" '
            "WHERE type='movie' AND imdb_id=%s LIMIT 1",
            (imdb_id,),
        )
        row = cur.fetchone()
        if not row:
            no_match += 1
            continue

        item_id, last_state, sym = row
        if last_state == "Completed" and sym:
            skipped += 1
            continue

        real_file, real_folder = _source_info(video_path)
        cur.execute(COMPLETE_SQL, (video_path, real_file, real_folder, item_id))
        log.info("  [movie] Completed: %s", folder)
        synced += 1

    conn.commit()
    log.info(
        "Movies — synced:%d  already_done:%d  no_file:%d  no_riven_match:%d",
        synced, skipped, no_file, no_match,
    )
    return synced


# ---------------------------------------------------------------------------
def sync_episodes(conn):
    """Mark Riven episode items Completed when library files already exist."""
    cur  = conn.cursor()
    ep_re = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,2})")
    synced = skipped = no_match = no_file = 0

    for show_folder in sorted(os.listdir(SHOWS_DIR)):
        if "{imdb-" not in show_folder:
            continue
        m = re.search(r"\{imdb-(tt\d+)\}", show_folder)
        if not m:
            continue
        show_imdb = m.group(1)
        show_path = os.path.join(SHOWS_DIR, show_folder)

        cur.execute(
            'SELECT id FROM "MediaItem" WHERE type=\'show\' AND imdb_id=%s LIMIT 1',
            (show_imdb,),
        )
        row = cur.fetchone()
        if not row:
            continue
        show_id = row[0]

        try:
            season_dirs = [
                d for d in os.listdir(show_path)
                if os.path.isdir(os.path.join(show_path, d))
                and d.lower().startswith("season")
            ]
        except OSError:
            continue

        for season_dir in season_dirs:
            sm = re.search(r"(\d+)", season_dir)
            if not sm:
                continue
            season_num  = int(sm.group(1))
            season_path = os.path.join(show_path, season_dir)

            cur.execute(
                'SELECT m.id FROM "MediaItem" m '
                'JOIN "Season" s ON s.id = m.id '
                "WHERE m.type='season' AND m.number=%s AND s.parent_id=%s LIMIT 1",
                (season_num, show_id),
            )
            row = cur.fetchone()
            if not row:
                continue
            season_id = row[0]

            try:
                ep_files = [
                    f for f in os.listdir(season_path)
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS
                ]
            except OSError:
                continue

            for ep_file in ep_files:
                em = ep_re.search(ep_file)
                if not em:
                    continue
                ep_num   = int(em.group(2))
                ep_path  = os.path.join(season_path, ep_file)

                # Don't check os.path.exists — FUSE target only resolves in DUMB

                cur.execute(
                    'SELECT m.id, m.last_state, m.symlinked FROM "MediaItem" m '
                    'JOIN "Episode" e ON e.id = m.id '
                    "WHERE m.type='episode' AND m.number=%s "
                    "AND e.parent_id=%s LIMIT 1",
                    (ep_num, season_id),
                )
                row = cur.fetchone()
                if not row:
                    no_match += 1
                    continue

                ep_id, last_state, sym = row
                if last_state == "Completed" and sym:
                    skipped += 1
                    continue

                real_file, real_folder = _source_info(ep_path)
                cur.execute(COMPLETE_SQL, (ep_path, real_file, real_folder, ep_id))
                synced += 1

    conn.commit()
    log.info(
        "Episodes — synced:%d  already_done:%d  no_file:%d  no_riven_match:%d",
        synced, skipped, no_file, no_match,
    )
    return synced


# ---------------------------------------------------------------------------
def sync_all():
    conn = _get_conn()
    try:
        m = sync_movies(conn)
        e = sync_episodes(conn)
        return m + e
    finally:
        conn.close()


def _get_conn():
    return psycopg2.connect(DB_DSN)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sync_all()
