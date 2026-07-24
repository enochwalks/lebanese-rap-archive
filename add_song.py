"""
add_song.py
Quick CLI helper to add a new song to the tracker without writing Python.

Usage:
    python add_song.py

Then answer the prompts. The song_id is auto-generated from artist+title
but you can override it.
"""

import re
from pathlib import Path

import tracker


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def main():
    tracker.init_db()

    print("=== Add a new song to the Lebanese Rap Archives queue ===\n")

    artist = input("Artist name: ").strip()
    title = input("Song title: ").strip()
    release_date = input("Release date (optional, e.g. '1998'): ").strip() or None
    song_file_path = input("Full path to the song audio file: ").strip()

    if not Path(song_file_path).exists():
        print(f"WARNING: {song_file_path} does not exist on disk. Double check the path.")

    default_slug = slugify(f"{artist}-{title}")
    song_id = input(f"Song ID [{default_slug}]: ").strip() or default_slug

    perm = input("Has permission been confirmed already? (y/n) [n]: ").strip().lower()
    permission_status = "confirmed" if perm == "y" else "pending"
    permission_evidence = None
    if permission_status == "confirmed":
        permission_evidence = input("Brief note on how permission was obtained: ").strip()

    tracker.add_song(
        song_id=song_id,
        artist=artist,
        title=title,
        song_file_path=song_file_path,
        release_date=release_date,
        permission_status=permission_status,
        permission_evidence=permission_evidence,
    )

    if permission_status == "pending":
        print(f"\nNOTE: '{song_id}' was added but permission is PENDING.")
        print("It will NOT be picked up by main.py until you confirm permission with:")
        print(f"    python -c \"import tracker; tracker.confirm_permission('{song_id}', 'evidence note')\"")


if __name__ == "__main__":
    main()
