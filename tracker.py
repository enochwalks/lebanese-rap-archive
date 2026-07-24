"""
tracker.py
Single source of truth for "where are we" on every song:
- which songs are queued
- which have permission confirmed
- which have been rendered
- which have been uploaded

Storage: SQLite (lebanese_rap_archive.db)
"""

import sqlite3
from pathlib import Path
from datetime import datetime

DB_PATH = Path(__file__).parent / "lebanese_rap_archive.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id TEXT UNIQUE NOT NULL,       -- unique slug, e.g. "artist-title"
    artist TEXT NOT NULL,
    title TEXT NOT NULL,
    release_date TEXT,                  -- free text, e.g. "1998" or "March 2003"
    song_file_path TEXT NOT NULL,       -- path to the audio file

    permission_status TEXT NOT NULL DEFAULT 'pending',  -- pending | confirmed
    permission_evidence TEXT,           -- e.g. "DM screenshot 2026-06-10"

    anime_clip_used TEXT,               -- filled in automatically at render time

    video_status TEXT NOT NULL DEFAULT 'not_started',   -- not_started | rendered | uploaded
    final_video_path TEXT,

    youtube_video_id TEXT,
    upload_date TEXT,

    queue_priority INTEGER DEFAULT 0,   -- lower = sooner. Ties broken by created_at.
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clip_rotation_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_clip_index INTEGER NOT NULL DEFAULT -1
);
"""


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    # ensure the single rotation-state row exists
    conn.execute(
        "INSERT OR IGNORE INTO clip_rotation_state (id, last_clip_index) VALUES (1, -1)"
    )
    conn.commit()
    conn.close()
    print(f"[tracker] DB ready at {DB_PATH}")


def add_song(song_id, artist, title, song_file_path, release_date=None,
             permission_status="pending", permission_evidence=None, queue_priority=0):
    """Add a new song to the tracker. Raises if song_id already exists."""
    now = datetime.now().isoformat()
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO songs
               (song_id, artist, title, release_date, song_file_path,
                permission_status, permission_evidence, queue_priority,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (song_id, artist, title, release_date, song_file_path,
             permission_status, permission_evidence, queue_priority, now, now)
        )
        conn.commit()
        print(f"[tracker] Added song '{song_id}' ({artist} - {title})")
    finally:
        conn.close()


def confirm_permission(song_id, evidence=None):
    """Mark a song as cleared for use."""
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        """UPDATE songs SET permission_status = 'confirmed',
           permission_evidence = COALESCE(?, permission_evidence),
           updated_at = ?
           WHERE song_id = ?""",
        (evidence, now, song_id)
    )
    conn.commit()
    conn.close()
    print(f"[tracker] Permission confirmed for '{song_id}'")


def get_next_unprocessed_song(exclude_ids=None):
    """
    Returns the next song that:
      - has permission confirmed
      - has not yet been uploaded
      - is not in exclude_ids (used to skip songs with missing files)
    ordered by queue_priority then created_at (oldest first).
    Returns None if nothing is left to process.
    """
    exclude_ids = list(exclude_ids or [])
    placeholders = ",".join("?" for _ in exclude_ids)
    exclusion = f"AND song_id NOT IN ({placeholders})" if exclude_ids else ""
    conn = get_connection()
    row = conn.execute(
        f"""SELECT * FROM songs
           WHERE permission_status = 'confirmed'
             AND video_status != 'uploaded'
             {exclusion}
           ORDER BY queue_priority ASC, created_at ASC
           LIMIT 1""",
        exclude_ids
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def mark_rendered(song_id, final_video_path, anime_clip_used):
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        """UPDATE songs SET video_status = 'rendered',
           final_video_path = ?, anime_clip_used = ?, updated_at = ?
           WHERE song_id = ?""",
        (final_video_path, anime_clip_used, now, song_id)
    )
    conn.commit()
    conn.close()
    print(f"[tracker] '{song_id}' marked as rendered -> {final_video_path}")


def mark_uploaded(song_id, youtube_video_id):
    now = datetime.now().isoformat()
    conn = get_connection()
    conn.execute(
        """UPDATE songs SET video_status = 'uploaded',
           youtube_video_id = ?, upload_date = ?, updated_at = ?
           WHERE song_id = ?""",
        (youtube_video_id, now, now, song_id)
    )
    conn.commit()
    conn.close()
    print(f"[tracker] '{song_id}' marked as uploaded -> https://youtu.be/{youtube_video_id}")


def get_next_clip_index(total_clips):
    """
    Rotation logic: returns the next clip index in round-robin order
    and persists the new state. total_clips must be > 0.
    """
    if total_clips <= 0:
        raise ValueError("No anime clips found in anime_clips/ folder")
    conn = get_connection()
    row = conn.execute("SELECT last_clip_index FROM clip_rotation_state WHERE id = 1").fetchone()
    next_index = (row["last_clip_index"] + 1) % total_clips
    conn.execute("UPDATE clip_rotation_state SET last_clip_index = ? WHERE id = 1", (next_index,))
    conn.commit()
    conn.close()
    return next_index


def status_report():
    """Quick human-readable summary of where everything stands."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT song_id, artist, title, permission_status, video_status FROM songs ORDER BY created_at"
    ).fetchall()
    conn.close()

    if not rows:
        print("No songs in tracker yet.")
        return

    print(f"\n{'SONG ID':<25} {'ARTIST':<20} {'TITLE':<20} {'PERMISSION':<12} {'VIDEO STATUS':<12}")
    print("-" * 90)
    for r in rows:
        print(f"{r['song_id']:<25} {r['artist']:<20} {r['title']:<20} {r['permission_status']:<12} {r['video_status']:<12}")
    print()


if __name__ == "__main__":
    init_db()
    status_report()
