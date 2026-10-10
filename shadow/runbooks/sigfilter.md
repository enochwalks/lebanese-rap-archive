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
