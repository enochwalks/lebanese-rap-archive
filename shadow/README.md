# Shadow — keeps your projects running when Claude isn't there

If Claude is down, out of credits, or gone: **start here.**

The shadow knows every project you run (`projects.toml`): where the code lives,
how to set it up on a new machine, every command, how the projects connect,
which secrets each one needs, where each one itself uses Claude and what
happens without it, and what to do when something breaks (`runbooks/`).

It works at three levels, so losing any one of them loses nothing:

| Level | What you need | What you get |
|---|---|---|
| 1. Plain commands | Python 3.11+ | list, health check, run any task, logs, status, clone everything |
| 2. Any AI | Ollama on your PC (free, offline) **or** a key for OpenAI / Gemini / Groq / OpenRouter / DeepSeek… | `ask` / `chat`: an agent that reads the code, runs tasks (asking before anything risky), and remembers what it learns |
| 3. Nothing reachable | — | `ask` still answers by searching the registry, runbooks and memory |
| 0. Not even a PC | A phone | Open `SHADOW_BRIEF.md` on GitHub, copy it into any AI chat app, ask your question |

## The 2-minute version

```bash
python shadow/shadow.py list                       # every project and its tasks
python shadow/shadow.py status                     # is everything healthy?
python shadow/shadow.py doctor                     # what's missing on this machine
python shadow/shadow.py run rap-archive status     # run any registered task
python shadow/shadow.py logs rap-archive           # last log lines
python shadow/shadow.py ask "yesterday's video didn't upload, why?"
python shadow/shadow.py chat                       # talk to it
```

In the runbooks, `run X Y` is short for `python shadow/shadow.py run X Y`.

## Give it a brain (optional, 5 minutes)

1. `cp shadow/config.example.toml shadow/config.toml`
2. Pick one or more brains in it. They are tried top to bottom:
   - **Local, nobody can switch it off:** install [Ollama](https://ollama.com), then
     `ollama pull qwen2.5:14b` (smaller PC: `qwen2.5:7b`). No key, no internet.
   - **Cloud:** set the key in your environment (e.g. `setx GEMINI_API_KEY ...` on
     Windows, `export GEMINI_API_KEY=...` elsewhere). Keys never go in the file.
3. `python shadow/shadow.py brains` shows which ones answer.

One-off without a config file: `SHADOW_PROVIDER=ollama SHADOW_MODEL=qwen2.5:7b python shadow/shadow.py chat`.

## What the AI agent may do

- Read project files and list folders — **never** `.env`, tokens, `client_secrets.json`
  or other secret files, so nothing secret is ever sent to a model.
- Run the tasks in `projects.toml`. Tasks marked `danger` (YouTube uploads, live
  trading signals, sending messages) always ask you first.
- Propose any other shell command — it runs only if you type `y`.
- `remember` durable lessons into `memory.md` (commit it; it's the shadow's memory).
- Everything it runs is logged to `journal.log`.

## Your projects right now

| id | what | where it runs |
|---|---|---|
| `rap-archive` | Daily song → video → private YouTube upload | your PC, daily |
| `storm-watch` | Weather app + storm alert agent | GitHub Actions, every 3 h |
| `sigfilter` | Telegram trading-signal filter | PC **or** phone **or** Actions — one only |
| `edit-engine` | Video editing engine, feeds `rap-archive` (opt-in) | library |

`sigfilter` and `edit-engine` live on their own branches (not merged into master);
`fetch` clones each one to its own folder.

## New machine, from zero

```bash
git clone https://github.com/enochwalks/lebanese-rap-archive.git
cd lebanese-rap-archive
python shadow/shadow.py fetch        # clones every project into ~/shadow-projects
python shadow/shadow.py doctor       # tells you exactly what's missing
```

Then copy your secret files and databases over (each project's `secrets` and
runbook list them), and run `doctor` again until it says *All good*.

## Keep it current — this is the one habit that matters

The shadow only knows what's in `projects.toml`. Whenever a project is added or
changes how it runs:

1. New project: `python shadow/shadow.py scan path/to/project` prints a filled-in
   block — paste it into `projects.toml` and fix the TODOs.
2. `python shadow/shadow.py brief` to refresh `SHADOW_BRIEF.md`, then commit.
3. While Claude is still around, end a work session with: *"update the shadow
   registry with anything that changed."*

**Not registered yet:** the YouTube Shorts channel bot (mentioned in the
rap-archive README; its code isn't in this repository). Add it with `scan`.

## Tests

```bash
python -m unittest discover -s shadow/tests -v
```
