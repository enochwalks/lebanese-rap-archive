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

## Editing engine (`edit_engine/`)

`video_builder.py` renders one fixed look by driving ffmpeg directly. The
engine is the general version of that: a frame-accurate, non-destructive
editing engine that represents a cut as *data* — a timeline you can inspect,
undo, save, export to a real NLE, and render.

```
edit_engine/
├── timebase.py       exact rational time; no float seconds anywhere above it
├── media.py          ffprobe + cache, offline media, relinking (never writes to sources)
├── model.py          Project / Sequence / Track / Clip, invariants enforced
├── commands.py       transactional command stack: undo, redo, atomic failure
├── ops.py            blade, insert, overwrite, lift, extract, ripple,
│                     roll, slip, slide, retime, link, markers
├── edl.py            CMX3600 export, so an auto-cut can be finished by a human
├── inspect.py        standalone HTML report: the timeline and every decision
├── render/
│   ├── plan.py       timeline -> RenderPlan (pure, testable, backend-agnostic)
│   ├── ffmpeg.py     RenderPlan -> one ffmpeg pass
│   └── effects.py    per-clip effect registry (punch, ken burns, fade, colour…)
└── programs/
    └── beat_edit.py  builds a beat-synced music-video timeline
```

### Using it

```python
from fractions import Fraction
from edit_engine import Project, Sequence, CommandStack, ops, make_clip
from edit_engine.render import compile_plan, FFmpegRenderer

project = Project(name="Track 12")
song    = project.import_media("songs/track.wav")
seq     = project.add_sequence(Sequence.create(rate=Fraction(30000, 1001)))
stack   = CommandStack(project)

stack.run(ops.AddClip(seq.audio_tracks[0].track_id, make_clip(song, seq, seq.zero())))
stack.run(ops.Blade(seq.seconds(12.5)))
stack.run(ops.Trim(clip_id, ops.HEAD, seq.frames(6), ripple=True))
stack.undo()                       # exactly back to where you were

FFmpegRenderer().render(compile_plan(seq, project.registry), "output/track-12.mp4")
project.save("output/track-12.json")
```

Build a whole beat-synced video in one call:

```
python engine_video_builder.py songs/track.wav output/track.mp4 "Artist" "Title" clips
```

That writes four files: the `.mp4`, a `.json` project you can reopen and keep
editing, an `.edl` you can import into Resolve or Premiere to finish by hand,
and an `.html` report.

### The report

Open `output/track.html` in any browser. It shows the edit from above: the
timeline with every shot coloured by source, the detected beat grid with the
beats actually cut on highlighted, and a shot list where each row says *why*
the program made that choice — why that source, why that in-point, why that
move. Hovering a row highlights the shot on the timeline.

It reads raw project JSON, so you can drop any other `project.json` onto the
page to inspect it — useful for comparing two seeds side by side.

It also tells you when it is flying blind: no beat data means the cut fell
back to a fixed interval, and the page says so in as many words rather than
reporting a meaningless "cuts on beat" percentage.

### What it guarantees

* **Frame accuracy.** All time is exact rationals (`30000/1001`, not `29.97`),
  so a cut placed on frame 5400 renders on frame 5400. The render tests prove
  it by reading pixels back out of the encoded file.
* **Non-destructive.** Source media is opened read-only. An edit changes only
  numbers describing which part of a file plays when.
* **Real undo.** Every edit is a command with before/after snapshots of just
  the tracks it declares it touches. A 600-edit randomised storm undoes back
  to a byte-identical timeline (`tests/test_commands.py`).
* **Fails before it wastes your time.** Offline media, invalid ranges and
  locked tracks are refused at plan time, not three hours into a batch. A
  killed render leaves no half-finished file that a resume would trust.
* **Sample-accurate audio.** Audio is delayed in samples, not milliseconds, so
  J- and L-cuts land where the model says they do.

### Performance shape

Each track is flattened with `concat` and tracks are composited with one
`overlay` each, instead of one overlay per clip. A 200-cut single-track edit
compiles to one concat and zero overlays; the naive shape would push a million
mostly-empty frame pairs through 200 chained filters.

### Switching the daily run over (opt-in)

`main.py` still uses `video_builder.py` and nothing changes on its own. To use
the engine instead, import the shim and pick a supported style:

```python
import engine_video_builder as video_builder   # in main.py
VIDEO_STYLE = "clips"     # or "kenburns"
```

**Not yet supported:** the `visualizer` and `combo` styles. Those draw an
audio-reactive waveform, EQ bars and title text across the whole programme —
sequence-level generated layers, which arrive with generator-track support in
a later phase. `video_builder.py` still owns that look and is unchanged.

### Tests

```
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

124 tests. The render tests need `ffmpeg`/`ffprobe` on PATH and skip
themselves if it is missing; everything else runs on the standard library
alone.

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
