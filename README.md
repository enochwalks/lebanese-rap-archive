# Lebanese Rap Archives — Automation

Separate, independent project from the Shorts channel bot. Own Google account,
own OAuth credentials, own upload quota (6/day, untouched by the other channel).

## What this does, per day

One run of `main.py` = one video:
1. Picks the next song from the tracker that has **confirmed permission** and
   hasn't been uploaded yet.
2. Picks the next anime clip in rotation (alphabetical order through `anime_clips/`).
3. Loops/trims that clip to the song's exact length, overlays a 5-second plain
   text intro card (title / artist / date) on a dimmed background, mutes the
   clip's own audio and replaces it with the song.
4. Uploads the result to YouTube.
5. Updates the tracker so it never repeats a song and always knows what's left.

## One-time setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
   Also requires `ffmpeg` and `ffprobe` installed and on your system PATH.

2. **YouTube OAuth (second account):**
   - Go to Google Cloud Console, create/select a project tied to the
     **second** Google account (the one that owns the Lebanese Rap Archives
     channel — NOT the Shorts channel's account).
   - Enable "YouTube Data API v3".
   - Create OAuth Client ID credentials (type: Desktop app).
   - Download the JSON, save it as `client_secrets.json` in this folder.
   - First time you run anything that uploads, a browser window will open —
     log in with the SECOND account and approve. After that, `token.pickle`
     stores the session and you won't be asked again (until it expires).

3. **Add your anime clips:**
   Drop your `.mp4` files into `anime_clips/`. Rotation order = alphabetical,
   so prefix filenames with numbers (`01_clip.mp4`, `02_clip.mp4`, ...) if you
   want a specific sequence instead of whatever alphabetical order falls out
   of the original filenames.

4. **Add songs to the queue:**
   ```
   python add_song.py
   ```
   Walks you through artist, title, date, file path, and whether permission
   is already confirmed. If you say no, the song sits in the tracker but
   `main.py` will skip it until you run:
   ```
   python -c "import tracker; tracker.confirm_permission('song-id', 'evidence note')"
   ```

5. **Check status anytime:**
   ```
   python tracker.py
   ```
   Prints every song and its permission/video status in one table.

## Daily run

```
python main.py
```

Schedule this once a day (Task Scheduler on Windows, cron on Linux/Mac).
Logs go to `logs/run.log`.

If a run fails mid-way (render crashes, upload times out), the next run
picks up from where it left off — it won't re-render a video that already
rendered successfully, and it won't skip ahead to a different song.

## Folder structure

```
lebanese-rap-archive/
├── main.py                 <- daily entry point, run this on a schedule
├── add_song.py             <- CLI to add new songs to the queue
├── tracker.py              <- SQLite state tracker (also runnable standalone for status)
├── video_builder.py        <- ffmpeg assembly logic
├── youtube_uploader.py     <- separate OAuth + upload logic for this channel
├── client_secrets.json     <- YOU provide this (OAuth creds, 2nd Google account)
├── token.pickle            <- auto-created after first successful login
├── lebanese_rap_archive.db <- auto-created SQLite tracker
├── songs/                  <- put song audio files here (or anywhere; path is stored per-song)
├── anime_clips/            <- put your anime video files here
├── output/                 <- rendered final videos land here before upload
└── logs/
    └── run.log
```

## Important reminder — permission gating

Every song needs **logged, verifiable** permission before it can be uploaded —
not just "I'm pretty sure it's fine." Even with an artist's direct OK, if
they're distributed through something like DistroKid/CD Baby, a Content ID
claim can still trigger automatically on their behalf without them realizing
it. Worth mentioning that risk to artists upfront so an automated claim isn't
a surprise later.

Also worth keeping in mind: the anime clips themselves are a separate
copyright layer from the music. Using licensed anime footage as a backdrop
carries its own Content ID / rights risk independent of the song permission
question above.
