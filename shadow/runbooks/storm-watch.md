| Symptom | Fix |
|---|---|
| No alerts for a long time, Actions tab shows no recent "Storm watch" runs | GitHub paused the schedule (no repo activity ~60 days). Actions -> Storm watch -> Run workflow once. |
| Runs are red | Open the run log. `fetch failed` = Open-Meteo hiccup, next run retries in 3 h. A JS error after editing `index.html` = the ===SHARED=== / ===ENGINE=== / ===ADVISOR=== markers were broken; restore them. |
| Want to test the whole alert path | Actions -> Run workflow with `alert_level` = 1 and `horizon_hours` = 240. It opens a real issue + push. Locally (no issue): `run storm-watch drill`. |
| Phone gets nothing but issues appear | Subscribe the ntfy app to the `ntfyTopic` in `weather/agent/config.json`; check GitHub notification settings for issues. |
| Wrong place / too many alerts | Edit `weather/agent/config.json` only: `places`, `alertLevel` (3 Warning, 4 Severe), `horizonHours`. |

Runs entirely on GitHub; there is nothing to keep alive on a PC.
