# How to use this file
Paste everything below into any AI chat (ChatGPT, Gemini, Claude, a local
model...) and then ask your question. To the AI reading this: you are helping
the owner run the projects described here while their usual assistant is away.
Answer only from this document; give the exact command from the Tasks lists
(run them from the repository root as `python shadow/shadow.py run <id> <task>`);
never suggest weakening a rule in the Notes; say plainly when the answer isn't here.

# Shadow knowledge pack
Generated 2026-10-10 22:17. Projects in dependency order: edit-engine, rap-archive, sigfilter, storm-watch

## edit-engine - edit_engine - frame-accurate video editing engine

General version of video_builder.py: a cut is a timeline (data) you can
inspect, undo, export as an EDL for Resolve/Premiere, and render in one
ffmpeg pass. engine_video_builder.py is an opt-in drop-in for main.py
(styles "clips" and "kenburns" only; "combo"/"visualizer" stay on
video_builder.py).

- Code: https://github.com/enochwalks/lebanese-rap-archive.git (branch claude/video-editing-engine-5psfw3)
- Local folder: ~/shadow-projects/lra-edit-engine
- Runs on: Owner's PC, as a library used by the rap-archive pipeline
- Schedule: Not scheduled. Used when main.py imports it.
- Feeds: rap-archive
- Secrets (names only):
    - ANTHROPIC_API_KEY (optional, for vision)
- Where it uses Claude:
    - edit_engine/analysis/vision.py: Claude looks at footage to direct the edit. Without a key it falls back to measurement (motion, exposure, music energy) automatically.
- Needs: programs ['python', 'ffmpeg', 'ffprobe'], files ['edit_engine/__init__.py', 'engine_video_builder.py'], env []

Tasks (python shadow/shadow.py run edit-engine <task>):
- install-dev: Install test tooling (pytest)  ->  `python -m pip install -r requirements-dev.txt`
- tests: Run the engine's test suite  ->  `python -m pytest -q tests`

Notes:
- Needs only ffmpeg/ffprobe + standard library. librosa optional (beats).
- Opt-in only: main.py uses video_builder.py unless someone switches the import.
- Code is on branch claude/video-editing-engine-5psfw3 (not merged to master).

Runbook:
- Health: `run edit-engine install-dev` once, then `run edit-engine tests`. Needs ffmpeg/ffprobe.
- To use it in the daily pipeline, in rap-archive's `main.py` change
  `import video_builder` to `import engine_video_builder as video_builder` and set
  `VIDEO_STYLE` to "clips" or "kenburns". "combo"/"visualizer" are NOT supported by the
  engine; it refuses rather than rendering something different. Undo = revert that line.
- Claude vision (`edit_engine/analysis/vision.py`) is optional: no `ANTHROPIC_API_KEY`
  -> it directs the edit from measured motion/exposure/music energy instead. Model can
  be changed with env `EDIT_ENGINE_MODEL`.
- Lives on branch `claude/video-editing-engine-5psfw3`, not merged into master.

## rap-archive - Lebanese Rap Archive - daily YouTube pipeline

One run of main.py = one video. Picks the next song with confirmed permission
from the SQLite tracker, renders it (artwork moving on the beat + waveform,
style "combo"), uploads it to YouTube as PRIVATE, marks it done. Resumable:
a failed run is retried next time without re-rendering or skipping a song.
A run.lock file stops two runs overlapping (that once caused double uploads).

- Code: https://github.com/enochwalks/lebanese-rap-archive.git (branch master)
- Local folder: ~/shadow-projects/lebanese-rap-archive
- Runs on: Owner's PC (Windows Task Scheduler / cron)
- Schedule: Once a day: python main.py
- Secrets (names only):
    - client_secrets.json (Google OAuth, 2nd Google account)
    - token.pickle (auto-created after first login)
    - token_readonly.pickle (channel_sync)
- Where it uses Claude:
    - None. Runs with no AI at all.
- Needs: programs ['python', 'ffmpeg', 'ffprobe'], files ['main.py', 'tracker.py', 'client_secrets.json'], env []

Tasks (python shadow/shadow.py run rap-archive <task>):
- install: Install Python dependencies (once per machine)  ->  `python -m pip install -r requirements.txt`
- status: Table of every song: permission, rendered, uploaded  ->  `python tracker.py`
- daily [DANGER: asks first]: Render the next song and upload it to YouTube as private  ->  `python main.py`
- add-song: Add one song to the queue (interactive questions)  ->  `python add_song.py`
- bulk-add: Add every mp3 under songs/ to the tracker (marks them confirmed!)  ->  `python bulk_add_songs.py`
- edit-song: Fix a song's artist, title or release date (interactive)  ->  `python edit_song.py`
- fix-path: Point a song at its audio file: run rap-archive fix-path <song-id> "<path>"  ->  `python -c "import sys,tracker; c=tracker.get_connection(); n=c.execute('UPDATE songs SET song_file_path=?, updated_at=datetime() WHERE song_id=?', (sys.argv[2], sys.argv[1])).rowcount; c.commit(); print('updated' if n else 'no such song_id')"`
- sync-preview: Compare YouTube channel to songs/ folder, change nothing  ->  `python channel_sync.py --dry-run`
- sync [DANGER: asks first]: Move already-uploaded songs to songs/_already_uploaded and update tracker  ->  `python channel_sync.py`
- unlock: Remove a stuck run.lock (only if NO run is actually going)  ->  `python -c "import pathlib; p=pathlib.Path('run.lock'); print('removed' if p.exists() and not p.unlink() else 'no lock')"`

Notes:
- Permission gate: a song is only uploaded once permission_status = confirmed.
  Never bypass this. Content ID can still claim via DistroKid/CD Baby.
- Upload quota: 6/day on this channel's own Google account. Separate from the
  Shorts channel bot (different account, different project, not in this repo).
- Uploads are PRIVATE on purpose; the owner publishes by hand in YouTube Studio.
- Missing audio file = song skipped (not marked done). Fix the path with
  `shadow.py run rap-archive fix-path <song-id> "<new path>"` and it is picked up again.
- If token.pickle expires, delete it and run once interactively to log in again.

Runbook:
**Daily check (30 seconds):** `run rap-archive status`, then `logs rap-archive`.
The last log line should be `Uploaded (private) -> https://youtu.be/...` or
`Nothing to process today`.

| Symptom (in logs/run.log) | What it means | Fix |
|---|---|---|
| `Another run is already in progress (run.lock exists)` | A run is going, or one crashed in the last 3 h | Wait. If nothing is running, `run rap-archive unlock`. Stale locks (>3 h) are taken over automatically. |
| `Nothing to process today` | Queue empty or no song has permission confirmed | `run rap-archive status`; add songs (`add-song` / `bulk-add`) or confirm permission: `python -c "import tracker; tracker.confirm_permission('song-id', 'evidence')"` |
| `WARNING: audio file missing for '<id>'` | The song's path is wrong / drive not mounted | `run rap-archive fix-path <id> "<real path>"` |
| `ERROR during video build` | ffmpeg failed | `ffmpeg -version` works? Re-run `daily`; the traceback in the log names the stage. Artwork for style "combo" comes from `artwork/`. |
| `ERROR during upload` ... `upload will be retried` | Network / quota / auth | Just run `daily` again later; it reuses the rendered file. Quota is 6 uploads/day. |
| `invalid_grant` / `Token has been expired or revoked` | Google login expired | Delete `token.pickle`, run `daily` at the PC (a browser opens), log in with the **second** Google account (the archive channel, not the Shorts one). |
| `Missing client_secrets.json` | New machine | Google Cloud Console -> the archive channel's project -> Credentials -> OAuth client (Desktop) -> download JSON as `client_secrets.json`. |

**Moving to a new PC:** `fetch rap-archive`, `run rap-archive install`, install ffmpeg,
copy over `lebanese_rap_archive.db` (the whole history!), `client_secrets.json`,
`token.pickle`, `songs/`, `artwork/`. Then `doctor rap-archive`.

**Schedule it** - Windows: Task Scheduler -> Create Basic Task -> Daily ->
Program `python`, Arguments `main.py`, Start in = the project folder.
Linux/Mac: `crontab -e` -> `0 18 * * * cd /path/to/lebanese-rap-archive && python main.py`.

**Never:** mark a song confirmed without real evidence; switch uploads to public in code;
run two schedulers at once.

## sigfilter - sigfilter - Telegram trading signal filter

Reads the Telegram signal channels the owner is in, parses each post into a
trade, dedupes copy-pasted relays, scores it (trust, completeness, R:R,
consensus, discipline, freshness) and forwards only what passes the gate to
Saved Messages / a channel / ntfy. Grades outcomes against real prices
(MT5 -> Binance -> Yahoo) so channel trust is earned. Dashboard on
http://127.0.0.1:8765.

- Code: https://github.com/enochwalks/lebanese-rap-archive.git (branch claude/nifty-pasteur-rcppo9), folder signals/
- Local folder: ~/shadow-projects/lra-signals/signals
- Runs on: Windows PC (scheduled task) OR Android phone (Termux) OR GitHub Actions poll - ONLY ONE AT A TIME
- Schedule: Always on (watch). Cloud option: every 10 min (signal-filter.yml, poll mode)
- Secrets (names only):
    - .env: TG_API_ID, TG_API_HASH, TG_SESSION, NTFY_TOPIC, ANTHROPIC_API_KEY (optional)
    - repo secrets for the Actions option: TG_API_ID, TG_API_HASH, TG_SESSION
    - signals.db (trust history - back it up)
- Where it uses Claude:
    - config.yaml llm_fallback (off by default): unparseable posts go to Claude. If Claude is unavailable keep enabled: false - strict regex parsing still works.
- Needs: programs ['python'], files ['sigfilter/cli.py', '.env', 'config.yaml'], env []

Tasks (python shadow/shadow.py run sigfilter <task>):
- setup: Virtual env + dependencies + config files (once)  ->  `python -m pip install -r requirements.txt`
- check: Health check: is everything working?  ->  `python -m sigfilter.cli check`
- login: One-time Telegram login (interactive)  ->  `python -m sigfilter.cli login`
- pick: Choose which channels to filter (rewrites sources: in config.yaml)  ->  `python -m sigfilter.cli pick`
- watch [DANGER: asks first]: Run the live filter forever (forwards signals). Stop other hosts first!  ->  `python -m sigfilter.cli watch`
- poll [DANGER: asks first]: Process new messages once and exit  ->  `python -m sigfilter.cli poll`
- stats: Per-channel hit rate and trust  ->  `python -m sigfilter.cli stats`
- recent: Last scored signals and why they passed or failed  ->  `python -m sigfilter.cli recent`
- backfill: Grade recent history to build channel trust (never forwards)  ->  `python -m sigfilter.cli backfill`
- dashboard: Open the live dashboard (127.0.0.1:8765)  ->  `python -m sigfilter.cli dashboard`
- testsend [DANGER: asks first]: Send a sample signal to Telegram + ntfy to test delivery  ->  `python -m sigfilter.cli testsend`
- tests: Run the test suite  ->  `python -m pytest -q tests`

Notes:
- Never run the agent in two places on one Telegram account: every signal arrives twice.
  Moving hosts: stop the old one first (PC: .\install-task.ps1 -Remove).
- Week one is near-silent by design (all channels start at 0.5 trust).
  Start min_score 60, raise to 70-75 after stats show real records.
- `check` is the health check. Run it first when something seems wrong.
- Not financial advice; paper-trade the output before risking money.
- Code is on branch claude/nifty-pasteur-rcppo9 (not merged to master).

Runbook:
**First move for anything:** `run sigfilter check` (the built-in health check).

| Symptom | Fix |
|---|---|
| Every signal arrives twice | Two hosts are running on one account. Keep one: PC (`.\install-task.ps1 -Remove` to stop), phone (close Termux), or the Actions workflow (disable it). |
| Dashboard says listener dead / no heartbeat | Restart: PC -> log out/in (scheduled task) or `run sigfilter watch`; phone -> `bash termux-run.sh`, Termux battery = Unrestricted + Acquire wakelock. |
| Telegram asks to log in again / AuthKey errors | `run sigfilter login` and paste the new TG_SESSION into `.env` (and into repo secrets if using Actions). |
| Nothing passes the gate | Normal in week one. `run sigfilter recent` shows why each was rejected. Lower with `python -m sigfilter.cli set min_score 55` while trust builds. |
| Joined/left channels | `run sigfilter pick` (keeps a .bak of config.yaml). |
| Delivery test | `run sigfilter testsend`. |

**Claude dependency:** only `llm_fallback` in config.yaml (off by default). Leave it
`enabled: false` when Claude is unavailable; strict parsing keeps working.

**Back up `signals.db`** - it holds every channel's earned trust record.

Not financial advice. The filter measures discipline, not whether a call is right.

## storm-watch - Lebanon Storm Watch - weather app + storm alert agent

weather/index.html is a single-file app (open in any browser, no key).
weather/agent/watch.mjs is a GitHub Actions agent that runs every 3 hours,
scores the forecast for the places in agent/config.json, and opens a GitHub
issue + ntfy push when a storm crosses alertLevel. Closes it when it passes.
The agent reads its scoring logic out of index.html between the ===SHARED===,
===ENGINE=== and ===ADVISOR=== markers, so the two never drift apart.

- Code: https://github.com/enochwalks/lebanese-rap-archive.git (branch master), folder weather/
- Local folder: ~/shadow-projects/lebanese-rap-archive/weather
- Runs on: GitHub Actions (.github/workflows/storm-watch.yml) - no PC needed
- Schedule: Every 3 hours UTC (cron 0 */3 * * *) + manual Run workflow
- Secrets (names only):
    - None. Uses the repo's own GITHUB_TOKEN. ntfy topic is in agent/config.json.
- Where it uses Claude:
    - None. Data is Open-Meteo + RainViewer, both free, no key.
- Needs: programs ['node'], files ['index.html', 'agent/watch.mjs', 'agent/config.json'], env []

Tasks (python shadow/shadow.py run storm-watch <task>):
- dry-run: Check the forecast now and print the alert it WOULD open  ->  `DRY_RUN=1 node agent/watch.mjs`
- drill: Drill: alert on anything in the next 10 days (prints only)  ->  `DRY_RUN=1 ALERT_LEVEL=1 HORIZON_HOURS=240 node agent/watch.mjs`
- open-app: Open the weather app in the browser  ->  `python -c "import webbrowser,pathlib; webbrowser.open(pathlib.Path('index.html').resolve().as_uri())"`
- cloud-run: Trigger the real cloud agent / revive a paused schedule  ->  `MANUAL: github.com/enochwalks/lebanese-rap-archive -> Actions -> Storm watch -> Run workflow (leave inputs empty for a normal run, alert_level=1 for a drill)`

Notes:
- GitHub pauses scheduled workflows after ~60 days with no repo activity.
  If alerts stop: Actions -> Storm watch -> Run workflow, once.
- Tune places / alertLevel (3 Warning, 4 Severe) in weather/agent/config.json only.
- Phone push: subscribe to the ntfyTopic from config.json in the ntfy app.
- Not official warnings. Civil Defence is 125.

Runbook:
| Symptom | Fix |
|---|---|
| No alerts for a long time, Actions tab shows no recent "Storm watch" runs | GitHub paused the schedule (no repo activity ~60 days). Actions -> Storm watch -> Run workflow once. |
| Runs are red | Open the run log. `fetch failed` = Open-Meteo hiccup, next run retries in 3 h. A JS error after editing `index.html` = the ===SHARED=== / ===ENGINE=== / ===ADVISOR=== markers were broken; restore them. |
| Want to test the whole alert path | Actions -> Run workflow with `alert_level` = 1 and `horizon_hours` = 240. It opens a real issue + push. Locally (no issue): `run storm-watch drill`. |
| Phone gets nothing but issues appear | Subscribe the ntfy app to the `ntfyTopic` in `weather/agent/config.json`; check GitHub notification settings for issues. |
| Wrong place / too many alerts | Edit `weather/agent/config.json` only: `places`, `alertLevel` (3 Warning, 4 Severe), `horizonHours`. |

Runs entirely on GitHub; there is nothing to keep alive on a PC.

# Memory (things learned while running these projects)
# Shadow memory

Durable lessons learned while running the projects. The shadow appends here
with its `remember` action; you can edit it by hand. Commit it.