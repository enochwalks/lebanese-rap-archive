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
