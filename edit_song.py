from datetime import datetime

import tracker


def main():
    tracker.init_db()
    conn = tracker.get_connection()
    rows = conn.execute(
        "SELECT song_id, artist, title, release_date FROM songs ORDER BY created_at"
    ).fetchall()
    conn.close()

    if not rows:
        print("No songs in tracker yet.")
        return

    print("\nExisting songs:\n")
    for r in rows:
        print(f"  {r['song_id']}")
        print(f"      artist='{r['artist']}' | title='{r['title']}' | date='{r['release_date']}'")
    print()

    song_id = input("Enter the song_id to edit: ").strip()

    conn = tracker.get_connection()
    existing = conn.execute("SELECT * FROM songs WHERE song_id = ?", (song_id,)).fetchone()
    conn.close()

    if not existing:
        print(f"No song found with song_id='{song_id}'. Nothing changed.")
        return

    print(f"\nCurrent values for '{song_id}':")
    print(f"  artist: {existing['artist']}")
    print(f"  title: {existing['title']}")
    print(f"  release_date: {existing['release_date']}")
    print("\nPress Enter on any field to leave it unchanged.\n")

    new_artist = input(f"New artist [{existing['artist']}]: ").strip()
    new_title = input(f"New title [{existing['title']}]: ").strip()
    new_date = input(f"New release date [{existing['release_date'] or ''}]: ").strip()

    conn = tracker.get_connection()
    conn.execute(
        """UPDATE songs SET
             artist = ?,
             title = ?,
             release_date = ?,
             updated_at = ?
           WHERE song_id = ?""",
        (
            new_artist or existing["artist"],
            new_title or existing["title"],
            new_date or existing["release_date"],
            datetime.now().isoformat(),
            song_id,
        )
    )
    conn.commit()
    conn.close()

    print(f"\nUpdated '{song_id}'.")


if __name__ == "__main__":
    main()
