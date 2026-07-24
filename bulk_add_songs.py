"""
bulk_add_songs.py
Scans the songs/ folder RECURSIVELY and adds every mp3 found to the tracker.

Parsing logic:
  - Artist / Title: split filename on the FIRST " - " (space-dash-space).
    Text before it = artist, text after it = title.
  - If there's no " - " separator, the parent folder name is used as the
    artist (the archive is organized as songs/<Archive>/<Crew>/track.mp3)
    and the whole filename becomes the title.
  - Anything in (parentheses) or [brackets] is stripped from the title
    (these are usually feature credits or YouTube video IDs, not the title).
  - '+' and '_' are treated as spaces (common in the archive filenames).
  - Release date: tries to read the ID3 "date"/"year" tag from the mp3 itself.
    If not present, left blank.

All songs are added with permission_status='confirmed' and a placeholder
evidence note (since you said permission was already confirmed for all of
these) -- edit per-song details later via tracker.py if needed.

Run from inside the activated venv:
    python bulk_add_songs.py
"""

import re
from pathlib import Path

import tracker

try:
    from mutagen import File as MutagenFile
except ImportError:
    print("ERROR: mutagen is not installed. Run:")
    print("    pip install mutagen")
    raise SystemExit(1)

SONGS_DIR = Path(__file__).parent / "songs"
PLACEHOLDER_NOTE = "Permission confirmed with artist/label - PLACEHOLDER, edit with specifics"


def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def clean_title(raw_title):
    """Strip (feature credits) and [video ids] out of the title portion."""
    cleaned = re.sub(r"\[.*?\]", "", raw_title)   # remove [anything]
    cleaned = re.sub(r"\(.*?\)", "", cleaned)      # remove (anything)
    cleaned = re.sub(r"\s+", " ", cleaned)         # collapse repeated spaces
    return cleaned.strip(" -_'\"()!")              # strip stray edge punctuation


def _name_similarity(a, b):
    """Fuzzy match for artist names written in leetspeak/arabizi variants,
    e.g. '@b0u 3@Mm@R' vs folder 'Abou 3@mm@r'."""
    import difflib

    def norm(s):
        s = s.lower().replace("0", "o").replace("@", "a")
        return re.sub(r"[^a-z0-9]+", "", s)

    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def parse_filename(filename_stem, folder_artist=None):
    """
    Returns (artist, title, needs_review).

    The archive mixes 'Artist - Title' and 'Title - Artist' filename orders,
    so when the file lives in a crew/artist folder we compare BOTH sides of
    the dash against the folder name and treat the closer match as the
    artist. Without a folder hint we assume 'Artist - Title'.
    needs_review is True whenever the parse is uncertain.
    """
    # normalize separators common in the archive filenames
    stem = filename_stem.replace("+", " ").replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()

    sep = " - " if " - " in stem else ("-" if "-" in stem else None)

    if sep:
        left, right = stem.split(sep, 1)
        left, right = left.strip(), right.strip()
        if folder_artist:
            sim_left = _name_similarity(left, folder_artist)
            sim_right = _name_similarity(right, folder_artist)
            if sim_right > sim_left and sim_right > 0.5:
                # 'Title - Artist' order
                artist, title = folder_artist, clean_title(left)
            else:
                artist = left if sim_left > 0.5 else (left or folder_artist)
                title = clean_title(right)
        else:
            artist, title = left, clean_title(right)
        needs_review = (title == "" or artist == "")
    else:
        artist = folder_artist or "Unknown"
        title = clean_title(stem)
        needs_review = folder_artist is None
    return artist, title, needs_review


def try_get_release_date(file_path):
    """Attempt to read year/date from ID3 tags. Returns string or None."""
    try:
        audio = MutagenFile(file_path, easy=True)
        if audio is None or audio.tags is None:
            return None
        for key in ("date", "year", "TDRC", "TYER"):
            if key in audio.tags:
                value = str(audio.tags[key][0])
                return value
    except Exception:
        pass
    return None


def main():
    tracker.init_db()

    if not SONGS_DIR.exists():
        print(f"ERROR: {SONGS_DIR} does not exist.")
        return

    mp3_files = sorted(SONGS_DIR.rglob("*.mp3"))
    if not mp3_files:
        print(f"No .mp3 files found in {SONGS_DIR} (searched recursively)")
        return

    print(f"Found {len(mp3_files)} mp3 file(s). Parsing and adding to tracker...\n")

    added = 0
    skipped = 0
    flagged = 0

    for file_path in mp3_files:
        stem = file_path.stem
        # crew/artist folder the file lives in (skip the top-level songs dir)
        folder_artist = file_path.parent.name if file_path.parent != SONGS_DIR else None
        artist, title, needs_review = parse_filename(stem, folder_artist=folder_artist)
        release_date = try_get_release_date(file_path)
        song_id = slugify(f"{artist}-{title}" if title else f"{artist}-{stem}")

        conn = tracker.get_connection()
        # same file already tracked -> true duplicate, skip (safe on re-runs)
        already_tracked = conn.execute(
            "SELECT 1 FROM songs WHERE song_file_path = ?", (str(file_path),)
        ).fetchone()
        if already_tracked:
            conn.close()
            print(f"SKIP (already in tracker): {file_path.name}")
            skipped += 1
            continue
        # different file but same slug -> distinct song, disambiguate the id
        base_id = song_id
        n = 2
        while conn.execute("SELECT 1 FROM songs WHERE song_id = ?", (song_id,)).fetchone():
            song_id = f"{base_id}-{n}"
            n += 1
        conn.close()

        if needs_review:
            # Don't silently store a blank/garbage title -- fall back to the
            # full filename as the title so nothing is lost, but flag it
            # clearly so it gets a manual look before this song reaches
            # the upload queue.
            title = title or stem
            print(f"⚠ FLAGGED for manual review: '{file_path.name}'")
            print(f"   -> parsed as artist='{artist}' | title='{title}'")
            print(f"   -> reason: could not confidently split artist/title from filename")
            flagged += 1

        tracker.add_song(
            song_id=song_id,
            artist=artist,
            title=title,
            song_file_path=str(file_path),
            release_date=release_date,
            permission_status="confirmed",
            permission_evidence=PLACEHOLDER_NOTE,
        )
        if not needs_review:
            date_note = release_date if release_date else "(no date found in tags)"
            print(f"  -> artist='{artist}' | title='{title}' | date={date_note}")
        added += 1

    print(f"\nDone. Added {added}, skipped {skipped} (already existed), {flagged} flagged for review.")
    print("\nRun 'python tracker.py' to see the full list.")
    print("IMPORTANT: artist/title were auto-parsed from filenames -- skim the")
    print("output above and fix any that look wrong using tracker.py or by")
    print("editing the database directly. The permission note is a placeholder")
    print("-- update it per song with real specifics (who confirmed, when, how).")
    if flagged:
        print(f"\n{flagged} song(s) above were FLAGGED -- their artist/title may be")
        print("wrong or incomplete. Fix these before letting main.py upload them.")


if __name__ == "__main__":
    main()
