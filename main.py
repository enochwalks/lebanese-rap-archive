"""
main.py
Daily orchestrator for the Lebanese Rap Archives channel.

Run once per day (via Task Scheduler / cron). Each run:
  1. Asks the tracker for the next confirmed, not-yet-processed song
  2. Picks the next anime clip in rotation order
  3. Builds the final video (intro text card + looped anime clip + song audio)
  4. Marks the song as done so the next run moves on to the next song

After rendering, the video is uploaded to YouTube as PRIVATE so you can
review it and publish manually from YouTube Studio.
If there's nothing left to process, it says so and exits cleanly.
This script does ONE video per run by design, matching the 1/day plan.
"""

import traceback
from pathlib import Path
from datetime import datetime

import tracker
import video_builder

BASE_DIR = Path(__file__).parent
ANIME_CLIPS_DIR = BASE_DIR / "anime_clips"
OUTPUT_DIR = BASE_DIR / "output"
LOG_DIR = BASE_DIR / "logs"

ANIME_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}

# Video style for daily renders: "combo" = artwork/ imagery moving on the beat
# + waveform/EQ overlay. Options: clips | visualizer | kenburns | combo
VIDEO_STYLE = "combo"


def log(message):
    LOG_DIR.mkdir(exist_ok=True)
    line = f"[{datetime.now().isoformat()}] {message}"
    print(line)
    with open(LOG_DIR / "run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def get_sorted_clip_list():
    """
    Returns anime clips sorted alphabetically -> this defines rotation order.
    Rename files (01_clip.mp4, 02_clip.mp4...) if you want a specific order.
    """
    clips = sorted(
        [p for p in ANIME_CLIPS_DIR.iterdir() if p.suffix.lower() in ANIME_EXTENSIONS]
    )
    return clips


LOCK_FILE = BASE_DIR / "run.lock"
LOCK_MAX_AGE_HOURS = 3


def acquire_lock():
    """Prevents two runs from overlapping (this caused double uploads)."""
    import os, time
    if LOCK_FILE.exists():
        age_hours = (time.time() - LOCK_FILE.stat().st_mtime) / 3600
        if age_hours < LOCK_MAX_AGE_HOURS:
            return False
        # stale lock from a crashed run - take over
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock():
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


def run_once():
    if not acquire_lock():
        log("Another run is already in progress (run.lock exists). Exiting.")
        return
    try:
        _run_once_locked()
    finally:
        release_lock()


def _run_once_locked():
    log("=== Daily run started ===")
    tracker.init_db()

    # Walk the queue until we find a song whose audio file actually exists.
    # Missing files are logged and skipped (NOT marked done) so you can fix
    # the path in the tracker and they'll be picked up again.
    song = None
    skipped_ids = []
    while True:
        candidate = tracker.get_next_unprocessed_song(exclude_ids=skipped_ids)
        if candidate is None:
            break
        if Path(candidate["song_file_path"]).exists():
            song = candidate
            break
        log(f"WARNING: audio file missing for '{candidate['song_id']}': "
            f"{candidate['song_file_path']} -- skipping (fix the path in the tracker).")
        skipped_ids.append(candidate["song_id"])

    if song is None:
        if skipped_ids:
            log(f"No processable songs: {len(skipped_ids)} queued song(s) have missing audio files. Exiting.")
        else:
            log("Nothing to process today (no confirmed, un-processed songs in queue). Exiting.")
        return

    log(f"Selected song: {song['song_id']} ({song['artist']} - {song['title']})")

    # If it was already rendered in a previous run, don't re-render --
    # just reuse the existing file.
    if song["video_status"] == "rendered" and song["final_video_path"] and Path(song["final_video_path"]).exists():
        log("Song was already rendered in a previous run; reusing the existing file.")
        final_path = Path(song["final_video_path"])
    else:
        chosen_clip = None
        if VIDEO_STYLE == "clips":
            clips = get_sorted_clip_list()
            if not clips:
                log("ERROR: no anime clips found in anime_clips/. Add some .mp4 files and rerun.")
                return
            clip_index = tracker.get_next_clip_index(len(clips))
            chosen_clip = clips[clip_index]
            log(f"Lead anime clip (rotation index {clip_index}): {chosen_clip.name}")
        else:
            log(f"Video style: {VIDEO_STYLE} (no anime clips needed)")

        output_filename = f"{song['song_id']}.mp4"
        final_path = OUTPUT_DIR / output_filename
        OUTPUT_DIR.mkdir(exist_ok=True)

        try:
            video_builder.build_video(
                song_path=song["song_file_path"],
                anime_clip_path=chosen_clip or "",
                output_path=final_path,
                artist=song["artist"],
                title=song["title"],
                release_date=song["release_date"],
                style=VIDEO_STYLE,
            )
        except Exception as e:
            log(f"ERROR during video build: {e}")
            log(traceback.format_exc())
            return

        tracker.mark_rendered(song["song_id"], str(final_path),
                              chosen_clip.name if chosen_clip else VIDEO_STYLE)

    log(f"Build complete: {final_path}")

    # Upload as PRIVATE so you can review before publishing.
    # If the upload fails, the song stays 'rendered' and the next run
    # reuses the file and retries the upload.
    try:
        import youtube_uploader
        video_title = f"{song['artist']} - {song['title']}"
        video_id = youtube_uploader.upload_video(
            final_path,
            title=video_title,
            description=f"{video_title}\n\nLebanese Rap Archive",
            privacy_status="private",
        )
    except Exception as e:
        log(f"ERROR during upload: {e}")
        log(traceback.format_exc())
        log("Video is rendered and saved; upload will be retried on the next run.")
        return

    tracker.mark_uploaded(song["song_id"], video_id)
    log(f"Uploaded (private) -> https://youtu.be/{video_id}")


if __name__ == "__main__":
    run_once()