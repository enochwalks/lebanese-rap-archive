"""
channel_sync.py
Compares your YouTube channel's uploads with the local songs folder and
splits the music into "already used" vs "not used yet".

What it does:
  1. Logs into YouTube (read-only) with the same client_secrets.json as the
     uploader. First run opens a browser to authorize; after that the token
     is reused (token_readonly.pickle).
  2. Downloads the title of EVERY video on the channel.
  3. Fuzzy-matches those titles against every .mp3 under songs/.
  4. Confident matches are MOVED to songs/_already_uploaded/ (folder
     structure preserved) and marked 'uploaded' in the tracker DB.
  5. Uncertain matches are NOT moved - they're listed in the report for you
     to decide.
  6. Writes a full report to channel_sync_report.txt.

Usage:
  python channel_sync.py --dry-run   (preview only, moves nothing - run this first)
  python channel_sync.py             (do the split for real)
"""

import re
import sys
import shutil
import pickle
import difflib
from pathlib import Path
from datetime import datetime

from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import tracker

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
BASE_DIR = Path(__file__).parent
CLIENT_SECRETS_FILE = BASE_DIR / "client_secrets.json"
TOKEN_FILE = BASE_DIR / "token_readonly.pickle"
SONGS_DIR = BASE_DIR / "songs"
USED_DIR = SONGS_DIR / "_already_uploaded"
REPORT_FILE = BASE_DIR / "channel_sync_report.txt"

MATCH_THRESHOLD = 0.78   # >= this -> confident, gets moved
REVIEW_THRESHOLD = 0.60  # between this and MATCH -> listed for manual review


def get_youtube():
    creds = None
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRETS_FILE.exists():
                raise FileNotFoundError(f"Missing {CLIENT_SECRETS_FILE}")
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRETS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("youtube", "v3", credentials=creds)


def get_all_channel_videos(youtube):
    """Returns [{'id': ..., 'title': ...}] for every upload on the channel."""
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        raise RuntimeError(
            "No channel found for this account. This usually means you picked "
            "the wrong identity at the Google login screen. Fix: delete "
            "token_readonly.pickle, run this again, and when Google shows the "
            "account chooser, pick the CHANNEL (Lebanese Rap Archive) entry, "
            "not your personal name."
        )
    uploads_playlist = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    videos = []
    page_token = None
    while True:
        resp = youtube.playlistItems().list(
            part="snippet", playlistId=uploads_playlist,
            maxResults=50, pageToken=page_token
        ).execute()
        for it in resp.get("items", []):
            videos.append({
                "id": it["snippet"]["resourceId"]["videoId"],
                "title": it["snippet"]["title"],
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return videos


def normalize(s):
    """Lowercase, strip YouTube-id suffixes and punctuation. Keeps Arabic letters."""
    s = s.lower()
    s = re.sub(r"\[[A-Za-z0-9_-]{8,}\]", " ", s)      # [W12FB0Mvwh0] style ids
    s = re.sub(r"[^\w؀-ۿ]+", " ", s)         # punctuation -> space
    s = re.sub(r"\b(official|video|audio|lyrics|hd|mp3)\b", " ", s)
    return " ".join(s.split())


def match_score(a, b):
    """0..1 similarity between two normalized strings."""
    if not a or not b:
        return 0.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / max(1, len(ta | tb))
    contain = 1.0 if (a in b or b in a) else 0.0
    return max(seq, jac, contain)


def mark_db_uploaded(mp3_path, video_id):
    """If the tracker has a row for this file, mark it uploaded."""
    conn = tracker.get_connection()
    row = conn.execute(
        "SELECT song_id FROM songs WHERE song_file_path LIKE ?",
        (f"%{mp3_path.name}",)
    ).fetchone()
    conn.close()
    if row:
        tracker.mark_uploaded(row["song_id"], video_id)
        return row["song_id"]
    return None


def main():
    dry_run = "--dry-run" in sys.argv

    print("[channel_sync] Connecting to YouTube...")
    youtube = get_youtube()
    videos = get_all_channel_videos(youtube)
    print(f"[channel_sync] Found {len(videos)} videos on the channel.")
    for v in videos:
        v["norm"] = normalize(v["title"])

    mp3s = [p for p in SONGS_DIR.rglob("*.mp3") if USED_DIR not in p.parents]
    print(f"[channel_sync] Found {len(mp3s)} local songs to check.")

    matched, review, unused = [], [], []
    for mp3 in mp3s:
        # try both "filename" and "artist-folder filename" as the local label
        stem = normalize(mp3.stem)
        with_artist = normalize(f"{mp3.parent.name} {mp3.stem}")
        best, best_video = 0.0, None
        for v in videos:
            s = max(match_score(stem, v["norm"]), match_score(with_artist, v["norm"]))
            if s > best:
                best, best_video = s, v
        if best >= MATCH_THRESHOLD:
            matched.append((mp3, best_video, best))
        elif best >= REVIEW_THRESHOLD:
            review.append((mp3, best_video, best))
        else:
            unused.append(mp3)

    lines = [f"Channel sync report - {datetime.now().isoformat()}",
             f"Channel videos: {len(videos)} | Local songs: {len(mp3s)}",
             f"Matched (used before): {len(matched)} | Needs review: {len(review)} | Not used yet: {len(unused)}",
             ""]

    lines.append("=== USED BEFORE (moved to songs/_already_uploaded/) ===")
    for mp3, v, s in matched:
        rel = mp3.relative_to(SONGS_DIR)
        lines.append(f"  [{s:.2f}] {rel}  ->  \"{v['title']}\" (https://youtu.be/{v['id']})")
        if not dry_run:
            dest = USED_DIR / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(mp3), str(dest))
            song_id = mark_db_uploaded(mp3, v["id"])
            if song_id:
                lines.append(f"          tracker: '{song_id}' marked uploaded")

    lines.append("")
    lines.append("=== NEEDS REVIEW (not moved - check these yourself) ===")
    for mp3, v, s in review:
        lines.append(f"  [{s:.2f}] {mp3.relative_to(SONGS_DIR)}  ~?~  \"{v['title']}\" (https://youtu.be/{v['id']})")

    lines.append("")
    lines.append(f"=== NOT USED YET ({len(unused)} songs, ready for the daily bot) ===")
    for mp3 in sorted(unused):
        lines.append(f"  {mp3.relative_to(SONGS_DIR)}")

    report = "\n".join(lines)
    REPORT_FILE.write_text(report, encoding="utf-8")
    print(report)
    print(f"\n[channel_sync] Report saved to {REPORT_FILE}")
    if dry_run:
        print("[channel_sync] DRY RUN - nothing was moved. Run without --dry-run to apply.")


if __name__ == "__main__":
    main()
